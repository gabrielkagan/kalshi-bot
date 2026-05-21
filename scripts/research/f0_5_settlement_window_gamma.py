"""F0.5 — Settlement-window gamma falsification (CT-MDP Attack #5, Phase 0).

Full implementation post-impl-Bit ship. Helpers and main pipeline implemented;
the 15-RED + 2-GREEN scaffold suite at
`tests/research/test_f0_5_settlement_window_gamma.py` +
`tests/contracts/test_f0_5_asset_ticker_prefix_mirrors_series_tickers.py`
flips GREEN as each helper lands.

Parent plan: kb/decisions/ct-mdp-f0-5-settlement-window-gamma-plan.md
Parent ClickUp: 86ba18zhr
Umbrella: kb/decisions/ct-mdp-attack-alpha-program-plan.md (ticket 86ba18zbv)
Predecessors: F0.1 (PR #132, VERDICT SURVIVE), F0.4 (PR #137, VERDICT KILL).

Hypothesis (per plan-doc § Hypothesis):
  The last 60-120s of a 15m Kalshi window carries panic-unwind / gamma-style
  flow that's more predictive of settle direction than earlier states. A
  classifier fit on (state at T-60s before close) → settle direction should
  achieve ROC AUC ≥ 0.55 in at least one tradeable cell (vol-regime ×
  day/night × moneyness).

Kill threshold (per ticket 86ba18zhr):
  T-60s AUC upper-CI < 0.55 in EVERY tradeable cell → KILL.
  ≥1 cell with AUC ≥ 0.55 lower-CI → SURVIVE.

Data sources (per plan-doc § RCA):
  - settled_trades: outcome label + window-close timestamp (`settled_at`).
  - evaluated_opportunities: spot trajectory via *_spot_at_decision.
  - market_observations_continuous: 5d NBBO trajectory near window expiry.

Run:
  python3 scripts/research/f0_5_settlement_window_gamma.py --db state.db
"""

from __future__ import annotations

import argparse
import bisect
import datetime as dt
import math
import random
import sqlite3
from collections import defaultdict
from typing import Any, Mapping, Sequence

from bot.constants import SERIES_TICKERS

# ----- Constants ----------------------------------------------------------

#: Kill rule threshold on the AUC lower-CI (per umbrella ticket 86ba18zhr).
AUC_KILL_THRESHOLD: float = 0.55

#: Min per-cell event count below which a cell reports "insufficient".
MIN_EVENTS_PER_CELL: int = 30

#: Conservative buffer (seconds) between `settled_at` (settle print) and the
#: T-0 window close reference. The bot's Kalshi settlement print arrives
#: after the boundary moment by a few-second processing delay; sampling
#: T-Xs states from rows after `settled_at - 5s` risks label leakage.
MIN_SETTLE_PROCESSING_DELAY_S: float = 5.0

#: Default timesteps (seconds before window close) at which to sample state.
TIMESTEPS_S_DEFAULT: tuple[int, ...] = (300, 60, 30, 10)

#: Vol-regime cutoff: per-asset σ_median across the analysis-window event set.
#: (Resolved per asset at main(); kept as a documented invariant here.)
_VOL_WINDOW_SECONDS: float = 3600.0  # 60-min rolling realized vol

#: Day/night band: 14:00-22:00 UTC = day (US-equities-hours overflow envelope
#: per F0.4 precedent at `scripts/research/f0_4_cross_asset_lead_lag.py`).
_DAY_HOUR_LOW: int = 14
_DAY_HOUR_HIGH: int = 22

# NOTE: Moneyness dimension RETRACTED at impl-R1 (2026-05-21). Plan-doc § Method
# step 4 + § Methodological invariants ¶ 3 originally specified 8 cells per
# asset = vol_regime × day_night × moneyness, with moneyness via
# `|spot - strike| / strike < 0.005`. **The 15M Kalshi ticker format does NOT
# encode strike** — empirically (verified at impl-R1) every settled 15M ticker
# has the form `KX<ASSET>15M-<YYMMDDHHMM>-<MM>` where the trailing `<MM>`
# mirrors the close-MINUTE (00/15/30/45), not a strike index. Hourly markets
# carry strike in the ticker (`KXBTC-26FEB2114-B95000`); 15M markets do not.
# Without strike, moneyness cannot be computed from `settled_trades` alone;
# the cell partition reduces to `vol_regime × day_night = 4 cells` per asset.
# The verdict-doc retracts the original ITM/OTM framing in § Risk register.

#: Asset → 15M ticker prefix (`KX<ASSET>15M`). Mirrors `bot.constants.SERIES_TICKERS`
#: verbatim (key-set + value equality) for the canonical 7-asset universe; the
#: trailing `-` separator is appended at query time as `prefix + "-%"` to scope
#: out hourly + daily markets that share the `KX<ASSET>` root. Anti-drift contract
#: test: `tests/contracts/test_f0_5_asset_ticker_prefix_mirrors_series_tickers.py`
#: (D1.11.a `LEAGUES_ESPN` pattern). When the bot's 7-asset universe changes,
#: the contract test fails RED and the operator must update SERIES_TICKERS first.
ASSET_TICKER_PREFIX: dict[str, str] = dict(SERIES_TICKERS)

# ----- Schema invariants (test-pinned) -----------------------------------

SETTLED_TRADES_REQUIRED_COLUMNS: tuple[str, ...] = (
    "ticker",
    "asset",
    "market_result",
    "settled_at",
    "product_type",
)

MOC_REQUIRED_COLUMNS: tuple[str, ...] = (
    "ticker",
    "observation_time",
    "yes_bid_cents",
    "yes_ask_cents",
    "no_bid_cents",
    "no_ask_cents",
    "bid_depth",
    "ask_depth",
    "source",
    "cache_age_ms",
)

EVAL_OPPS_SPOT_COLUMNS: tuple[str, ...] = (
    "btc_spot_at_decision",
    "eth_spot_at_decision",
    "sol_spot_at_decision",
    "xrp_spot_at_decision",
    "hype_spot_at_decision",
    "doge_spot_at_decision",
    "bnb_spot_at_decision",
)


# ----- ISO timestamp parsing ---------------------------------------------


def _parse_iso(ts: str) -> dt.datetime:
    """Parse ISO-8601 timestamp with optional `Z` suffix to aware datetime.

    Lexicographic comparison on ISO strings breaks under mixed precision
    (e.g., `"...:00Z"` vs `"...:00.999999Z"`). Parse to datetime objects
    and compare numerically (F0.4 precedent).
    """
    if ts.endswith("Z"):
        ts = ts[:-1] + "+00:00"
    return dt.datetime.fromisoformat(ts)


# ----- Window close + settle-buffer guard (Invariant 7) -------------------


def compute_window_close(*, settled_at: str, settle_processing_delay_s: float) -> str:
    """Return window close timestamp = settled_at - settle_processing_delay_s.

    settle_processing_delay_s must be ≥ MIN_SETTLE_PROCESSING_DELAY_S (5s
    per plan-doc § RCA Risk-2). Below that, label leakage risk: T-Xs
    samples could include rows from the boundary processing interval.
    """
    if settle_processing_delay_s < MIN_SETTLE_PROCESSING_DELAY_S:
        raise ValueError(
            f"settle_processing_delay_s={settle_processing_delay_s} below "
            f"buffer minimum {MIN_SETTLE_PROCESSING_DELAY_S}s (plan-doc § RCA Risk-2)"
        )
    settled_dt = _parse_iso(settled_at)
    close_dt = settled_dt - dt.timedelta(seconds=settle_processing_delay_s)
    iso = close_dt.isoformat()
    # Restore Z suffix if original used Z (compatibility with comparisons
    # that expect the same shape; _parse_iso normalizes to +00:00 internally).
    if settled_at.endswith("Z") and iso.endswith("+00:00"):
        iso = iso[:-6] + "Z"
    return iso


# ----- No-look-ahead state sampling (Invariant 2) -------------------------


def sample_state_at_timestep(
    *,
    moc_rows: Sequence[Mapping[str, Any]],
    window_close: str,
    timestep_s: int,
) -> list[Mapping[str, Any]]:
    """Return moc rows with observation_time ≤ window_close - timestep_s.

    No-look-ahead invariant (Invariant 2). The returned list preserves the
    input order; the caller selects the most-recent row by checking the
    last entry in time-sorted order.
    """
    close_dt = _parse_iso(window_close)
    cutoff_dt = close_dt - dt.timedelta(seconds=timestep_s)
    kept: list[Mapping[str, Any]] = []
    for row in moc_rows:
        try:
            obs_dt = _parse_iso(row["observation_time"])
        except (ValueError, KeyError):
            continue
        if obs_dt <= cutoff_dt:
            kept.append(row)
    return kept


def extract_state_features(
    *,
    moc_row_at_t_xs: Mapping[str, Any],
    window_close: str,
    timestep_s: int,
    tainted_post_close_row: Mapping[str, Any] | None = None,
) -> dict[str, float]:
    """Extract state features from a moc row sampled at T-Xs.

    Features: yes_mid, yes_spread (raw mid + spread on the state snapshot).
    The classifier may add Δspot features upstream; this primitive emits the
    NBBO-only subset.

    `tainted_post_close_row` parameter exists ONLY as a contract surface for
    the no-look-ahead test (Invariant 2). If supplied, the function raises
    ValueError — settle outcome is the LABEL, never a feature; any feature
    derived from data at t > window_close must error.
    """
    if tainted_post_close_row is not None:
        post_dt = _parse_iso(tainted_post_close_row["observation_time"])
        close_dt = _parse_iso(window_close)
        if post_dt > close_dt:
            raise ValueError(
                "look-ahead violation: tainted_post_close_row at "
                f"{tainted_post_close_row['observation_time']} is after "
                f"window_close={window_close} (label-leak / post-close data)"
            )
    yb = float(moc_row_at_t_xs.get("yes_bid_cents") or 0.0)
    ya = float(moc_row_at_t_xs.get("yes_ask_cents") or 0.0)
    yes_mid = (yb + ya) / 2.0
    yes_spread = max(0.0, ya - yb)
    return {
        "yes_mid_cents": yes_mid,
        "yes_spread_cents": yes_spread,
        "timestep_s": float(timestep_s),
    }


# ----- Cell enumeration (Invariant 3) -------------------------------------


def enumerate_cells() -> list[tuple[str, str]]:
    """Return the 4 cells = vol_regime × day_night.

    Moneyness dimension retracted at impl-R1 — 15M Kalshi tickers don't
    encode strike (see module-level NOTE). Cell partition is 4 cells per
    asset (vol × day_night), down from the originally-planned 8.
    """
    vols = ("vol_high", "vol_low")
    daynights = ("day", "night")
    return [(v, d) for v in vols for d in daynights]


def _classify_daynight(ts: dt.datetime) -> str:
    """US-equities-hours overlap window 14:00-22:00 UTC = day; else night."""
    return "day" if _DAY_HOUR_LOW <= ts.hour < _DAY_HOUR_HIGH else "night"


# ----- AUC computation (Invariant 4) --------------------------------------


def compute_auc_with_orientation(
    *, y_true: Sequence[int], y_score: Sequence[float]
) -> float:
    """Compute ROC AUC; flip orientation if raw AUC < 0.5; error on degenerate.

    AUC bounded `[0.5, 1.0]` after orientation flip (per plan-doc Invariant 4).
    Raw AUC < 0.5 means the classifier predicts the wrong direction; report
    `1 - raw_AUC` instead. Single-class input (all y=0 or all y=1) leaves
    AUC undefined → raise ValueError (anti-degenerate).
    """
    if len(y_true) != len(y_score):
        raise ValueError(
            f"length mismatch: y_true={len(y_true)} y_score={len(y_score)}"
        )
    if len(y_true) == 0:
        raise ValueError("degenerate input: empty y_true / y_score")
    classes = set(int(y) for y in y_true)
    if len(classes) < 2:
        raise ValueError(
            f"single-class / degenerate input: y_true classes={classes} (undefined AUC)"
        )
    from sklearn.metrics import roc_auc_score

    raw = float(roc_auc_score(list(y_true), list(y_score)))
    if raw < 0.5:
        return 1.0 - raw
    return raw


# ----- Cell sufficiency (Invariant 5) -------------------------------------


def classify_cell_sufficiency(*, n_events: int) -> str:
    """Return 'sufficient' if n_events ≥ MIN_EVENTS_PER_CELL else 'insufficient'."""
    return "sufficient" if n_events >= MIN_EVENTS_PER_CELL else "insufficient"


# ----- Kelly-sized sim PnL (Invariant 6) ----------------------------------


def simulate_pnl_with_kelly(
    *,
    events: Sequence[Mapping[str, Any]],
    bankroll_dollars: float,
) -> float:
    """Sim PnL using canonical bot Kelly helper. Flat-1 sizing forbidden.

    Each event: {entry_price_cents, settle_cents, edge}. `edge` is the
    P(YES) - breakeven_implied delta; convert back to P(YES) ≈
    edge + entry_price_cents/100 for the Kelly sizer.

    Sizing delegates to `bot.helpers.tm_sweep.tm_shadow_kelly_contracts_with_bound`
    (the canonical Kelly helper per CLAUDE.md no-flat-1 rule). PnL per event =
    contracts × (settle_cents - entry_price_cents), where settle_cents is
    100 on YES-win, 0 on YES-loss (binary settle convention).
    """
    from bot.helpers.tm_sweep import tm_shadow_kelly_contracts_with_bound

    bankroll_cents = int(round(float(bankroll_dollars) * 100.0))
    total_pnl_cents = 0
    for ev in events:
        entry = int(ev["entry_price_cents"])
        settle = int(ev["settle_cents"])
        edge = float(ev["edge"])
        # Reconstruct P(YES) from edge: edge = P(YES) - entry/100 → P = edge + entry/100.
        p_yes = max(0.0, min(1.0, edge + entry / 100.0))
        # Use BTC asset cap as a neutral default — F0.5 sim is asset-agnostic
        # at the helper-contract layer; main() invokes per-asset via real ev rows.
        ct, _bound = tm_shadow_kelly_contracts_with_bound(
            price_cents=entry,
            bankroll_cents=bankroll_cents,
            asset=str(ev.get("asset", "BTC")),
            cal_mlp_p_mean=None,
            raw_prob_fallback=p_yes,
        )
        contracts = int(ct or 0)
        # Per-contract PnL: settle_cents - entry_price_cents (YES-buy convention).
        total_pnl_cents += contracts * (settle - entry)
    return float(total_pnl_cents) / 100.0  # return dollars


# ----- Verdict classification --------------------------------------------


def classify_verdict(
    *,
    cells: Sequence[Mapping[str, Any]],
    auc_threshold: float = AUC_KILL_THRESHOLD,
) -> str:
    """Return 'SURVIVE' if any cell has auc_lower ≥ threshold; 'KILL' otherwise.

    Cells with `status == 'insufficient'` are excluded from the verdict
    computation. Survival gate (per umbrella ticket 86ba18zhr): ≥1 cell
    anywhere with `auc_lower ≥ AUC_KILL_THRESHOLD`.
    """
    for cell in cells:
        if cell.get("status") == "insufficient":
            continue
        auc_lower = cell.get("auc_lower")
        if auc_lower is None:
            continue
        if float(auc_lower) >= auc_threshold:
            return "SURVIVE"
    return "KILL"


# ----- Internal DB helpers -----------------------------------------------


def _load_settled_windows(
    conn: sqlite3.Connection, start_ts: str
) -> list[dict[str, Any]]:
    """Load settled 15M windows in the analysis period.

    Returns one row per (ticker, settled_at) pair. settled_trades may have
    multiple rows per ticker (one per bot fill); we collapse via
    DISTINCT-on-ticker since market_result is per-ticker invariant.
    """
    cur = conn.cursor()
    cur.execute(
        "SELECT ticker, asset, market_result, MAX(settled_at) AS settled_at "
        "FROM settled_trades "
        "WHERE product_type='15m' AND settled_at >= ? "
        "AND market_result IN ('yes','no') "
        "GROUP BY ticker, asset, market_result "
        "ORDER BY settled_at",
        (start_ts,),
    )
    rows: list[dict[str, Any]] = []
    for ticker, asset, market_result, settled_at in cur.fetchall():
        rows.append(
            {
                "ticker": ticker,
                "asset": asset,
                "market_result": market_result,
                "settled_at": settled_at,
                "y_w": 1 if market_result == "yes" else 0,
            }
        )
    return rows


def _load_moc_rows_for_tickers(
    conn: sqlite3.Connection, tickers: Sequence[str]
) -> dict[str, list[dict[str, Any]]]:
    """Bulk-load moc rows keyed by ticker for the supplied ticker set."""
    if not tickers:
        return {}
    cur = conn.cursor()
    by_ticker: dict[str, list[dict[str, Any]]] = defaultdict(list)
    # SQLite IN-list cap is 999; chunk if needed.
    chunk = 800
    for i in range(0, len(tickers), chunk):
        sub = tickers[i : i + chunk]
        placeholders = ",".join(["?"] * len(sub))
        cur.execute(
            f"SELECT ticker, observation_time, yes_bid_cents, yes_ask_cents, "
            f"no_bid_cents, no_ask_cents, bid_depth, ask_depth, source, cache_age_ms "
            f"FROM market_observations_continuous "
            f"WHERE ticker IN ({placeholders}) "
            f"ORDER BY ticker, observation_time",
            tuple(sub),
        )
        cols = MOC_REQUIRED_COLUMNS
        for r in cur.fetchall():
            row = dict(zip(cols, r))
            by_ticker[row["ticker"]].append(row)
    return dict(by_ticker)


def _load_spot_series_by_asset(
    conn: sqlite3.Connection, start_ts: str
) -> dict[str, list[tuple[dt.datetime, float]]]:
    """Load (time, spot) series for each asset from evaluated_opportunities.

    Per-asset cadence ~30s. Each asset's series is sorted ascending; spot
    is the canonical `<asset>_spot_at_decision` column.
    """
    series: dict[str, list[tuple[dt.datetime, float]]] = {}
    cur = conn.cursor()
    for asset in ASSET_TICKER_PREFIX.keys():
        col = f"{asset.lower()}_spot_at_decision"
        cur.execute(
            f"SELECT evaluation_time, {col} FROM evaluated_opportunities "  # noqa: S608 — col from controlled mapping
            f"WHERE {col} IS NOT NULL AND evaluation_time >= ? "
            f"ORDER BY evaluation_time",
            (start_ts,),
        )
        seq: list[tuple[dt.datetime, float]] = []
        seen: set[dt.datetime] = set()
        for t, p in cur.fetchall():
            try:
                dt_t = _parse_iso(t)
            except ValueError:
                continue
            if dt_t in seen:
                continue
            seen.add(dt_t)
            seq.append((dt_t, float(p)))
        series[asset] = seq
    return series


def _compute_per_asset_vol_at_time(
    series: Sequence[tuple[dt.datetime, float]], at_time: dt.datetime
) -> float | None:
    """Rolling realized vol over the prior _VOL_WINDOW_SECONDS at `at_time`.

    Returns None if fewer than 5 prior returns available in the window.
    Mirrors F0.4 `_detect_sigma_events` per-event sigma calculation.
    """
    log_returns: list[tuple[dt.datetime, float]] = []
    for i in range(1, len(series)):
        t_prev, p_prev = series[i - 1]
        t_cur, p_cur = series[i]
        if t_cur > at_time:
            break
        if p_prev <= 0 or p_cur <= 0:
            continue
        log_returns.append((t_cur, math.log(p_cur / p_prev)))
    if len(log_returns) < 5:
        return None
    window_start = at_time - dt.timedelta(seconds=_VOL_WINDOW_SECONDS)
    window_rets = [r for (t, r) in log_returns if t >= window_start]
    if len(window_rets) < 5:
        return None
    mean = sum(window_rets) / len(window_rets)
    var = sum((r - mean) ** 2 for r in window_rets) / max(1, len(window_rets) - 1)
    return var**0.5


def _bisect_latest_at_or_before(
    times: Sequence[dt.datetime], cutoff: dt.datetime
) -> int:
    """Return the index of the latest entry with time ≤ cutoff, or -1.

    O(log n) bisect — F0.4 precedent: linear-scan over 5d × 7-asset moc
    is unworkable; pre-sort + bisect is the canonical pattern.
    """
    idx = bisect.bisect_right(times, cutoff) - 1
    return idx


def _spot_at_time(
    series: Sequence[tuple[dt.datetime, float]], at_time: dt.datetime
) -> float | None:
    """Most-recent spot value with t ≤ at_time, or None if no prior row."""
    if not series:
        return None
    times = [t for (t, _) in series]
    idx = _bisect_latest_at_or_before(times, at_time)
    if idx < 0:
        return None
    return series[idx][1]


# ----- Main pipeline -----------------------------------------------------


def main(
    *,
    db_path: str,
    days: int = 5,
    timesteps_s: tuple[int, ...] = TIMESTEPS_S_DEFAULT,
    bootstrap_n: int = 1000,
    output_path: str | None = None,
    settle_processing_delay_s: float = MIN_SETTLE_PROCESSING_DELAY_S,
    bankroll_dollars: float = 10000.0,
) -> dict[str, Any]:
    """End-to-end Phase 0 falsification on settlement-window gamma.

    Returns:
        {
            'verdict': 'KILL' | 'SURVIVE',
            'per_cell_results': {(asset, vol, dn, moneyness): {n_events, auc_T60,
                                                              auc_lower, auc_upper,
                                                              auc_T300, status, ...}, ...},
            'n_windows': int,
            'window_days': int,
            'window_start_ts': str,
            'sigma_median_per_asset': {asset: float},
            'auc_threshold': float,
            'min_events_per_cell': int,
            'cells_clearing_threshold': list,
            'drop_counters': {'no_state_at_T60': int, 'no_state_at_T300': int,
                              'no_spot': int, 'no_vol': int, 'no_strike': int},
        }
    """
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=10000")

        cur = conn.cursor()
        cur.execute("SELECT MAX(observation_time) FROM market_observations_continuous")
        max_ts_row = cur.fetchone()
        if max_ts_row is None or max_ts_row[0] is None:
            window_start_dt = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days)
        else:
            window_start_dt = _parse_iso(max_ts_row[0]) - dt.timedelta(days=days)
        window_start_ts = window_start_dt.isoformat()

        # 1. Enumerate settled 15M windows.
        windows = _load_settled_windows(conn, window_start_ts)

        # 2. Bulk-load moc for all settled tickers.
        all_tickers = sorted({w["ticker"] for w in windows})
        moc_by_ticker = _load_moc_rows_for_tickers(conn, all_tickers)
        # Pre-sort + pre-parse moc per-ticker for bisect.
        moc_index: dict[str, tuple[list[dt.datetime], list[dict[str, Any]]]] = {}
        for ticker, rows in moc_by_ticker.items():
            augmented: list[dict[str, Any]] = []
            for row in rows:
                try:
                    obs_dt = _parse_iso(row["observation_time"])
                except (ValueError, KeyError):
                    continue
                augmented.append({**row, "_obs_dt": obs_dt})
            augmented.sort(key=lambda r: r["_obs_dt"])
            moc_index[ticker] = ([r["_obs_dt"] for r in augmented], augmented)

        # 3. Load per-asset spot series (cross-asset cadence ~30s).
        spot_series = _load_spot_series_by_asset(conn, window_start_ts)

        # 4. Per-window: compute (state at T-60, state at T-300, regime cell).
        per_window: list[dict[str, Any]] = []
        drop_counters = {
            "no_state_at_T60": 0,
            "no_state_at_T300": 0,
            "no_spot": 0,
            "no_vol": 0,
            "window_close_before_data": 0,
        }
        sigma_medians: dict[str, float] = {}

        # Per-asset sigma calibration: collect all per-window sigmas first to
        # compute medians used for high/low regime split.
        per_window_sigma: dict[str, list[float]] = defaultdict(list)
        for w in windows:
            asset = w["asset"]
            if asset not in spot_series:
                continue
            try:
                close_ts = compute_window_close(
                    settled_at=w["settled_at"],
                    settle_processing_delay_s=settle_processing_delay_s,
                )
            except ValueError:
                continue
            close_dt = _parse_iso(close_ts)
            sigma = _compute_per_asset_vol_at_time(spot_series[asset], close_dt)
            if sigma is not None:
                per_window_sigma[asset].append(sigma)

        for asset, sigmas in per_window_sigma.items():
            sigmas_sorted = sorted(sigmas)
            sigma_medians[asset] = sigmas_sorted[len(sigmas_sorted) // 2]

        for w in windows:
            asset = w["asset"]
            ticker = w["ticker"]
            try:
                close_ts = compute_window_close(
                    settled_at=w["settled_at"],
                    settle_processing_delay_s=settle_processing_delay_s,
                )
            except ValueError:
                continue
            close_dt = _parse_iso(close_ts)

            # State sampling — use bisect on pre-indexed moc.
            if ticker not in moc_index:
                drop_counters["no_state_at_T60"] += 1
                continue
            times, augmented = moc_index[ticker]

            def _row_at(timestep_s: int) -> dict[str, Any] | None:
                cutoff = close_dt - dt.timedelta(seconds=timestep_s)
                idx = _bisect_latest_at_or_before(times, cutoff)
                if idx < 0:
                    return None
                return augmented[idx]

            row_t60 = _row_at(60)
            row_t300 = _row_at(300)
            if row_t60 is None:
                drop_counters["no_state_at_T60"] += 1
                continue
            if row_t300 is None:
                drop_counters["no_state_at_T300"] += 1
                # Continue with T-60 only — T-300 is secondary diagnostic
                # per plan-doc § Hypothesis (kill verdict is per-cell T-60 AUC).

            # Spot trajectory: spot_at_close ≈ spot_at_T-60 (closest available).
            spot_t60 = _spot_at_time(spot_series.get(asset, []), close_dt - dt.timedelta(seconds=60))
            spot_t300 = _spot_at_time(spot_series.get(asset, []), close_dt - dt.timedelta(seconds=300))
            if spot_t60 is None:
                drop_counters["no_spot"] += 1
                continue

            # Δspot_60s = spot(T-60) − spot(T-120). Used as a state feature.
            spot_t120 = _spot_at_time(spot_series.get(asset, []), close_dt - dt.timedelta(seconds=120))
            delta_spot_60s = (spot_t60 - spot_t120) if spot_t120 is not None else 0.0

            # Cell classification (moneyness dimension retracted at impl-R1 —
            # 15M Kalshi tickers don't encode strike; see module-level NOTE).
            sigma = _compute_per_asset_vol_at_time(spot_series.get(asset, []), close_dt)
            if sigma is None:
                drop_counters["no_vol"] += 1
                continue
            sigma_median = sigma_medians.get(asset, 0.0)
            vol_regime = "vol_high" if sigma > sigma_median else "vol_low"
            daynight = _classify_daynight(close_dt)

            # Features: yes_mid + yes_spread at T-60 (and T-300 if available).
            feat_t60 = extract_state_features(
                moc_row_at_t_xs=row_t60, window_close=close_ts, timestep_s=60
            )
            feat_t300 = (
                extract_state_features(
                    moc_row_at_t_xs=row_t300, window_close=close_ts, timestep_s=300
                )
                if row_t300 is not None
                else None
            )

            per_window.append(
                {
                    "ticker": ticker,
                    "asset": asset,
                    "y_w": w["y_w"],
                    "settled_at": w["settled_at"],
                    "cell": (asset, vol_regime, daynight),
                    "feat_t60_yes_mid": feat_t60["yes_mid_cents"],
                    "feat_t60_yes_spread": feat_t60["yes_spread_cents"],
                    "feat_t60_delta_spot_60s": delta_spot_60s,
                    "feat_t300_yes_mid": feat_t300["yes_mid_cents"] if feat_t300 else None,
                    "feat_t300_yes_spread": feat_t300["yes_spread_cents"] if feat_t300 else None,
                    "spot_at_T60": spot_t60,
                    "spot_at_T300": spot_t300,
                    "sigma": sigma,
                }
            )

        # 5. Per-cell k-fold-CV AUC with bootstrap CI on T-60 features.
        #
        # CV methodology per plan-doc § Method step 5 + § Methodological
        # invariants ¶ k-fold:
        #
        #   "Use k-fold cross-validation (k=5) for AUC estimation with
        #   strict temporal ordering of folds (no look-ahead across folds —
        #   fold k trains on windows with settled_at < fold-k boundary only)."
        #
        # **Forward-chaining** temporal split via `TimeSeriesSplit(n_splits=5)`
        # (impl-R1 fix; the initial impl used sklearn's plain KFold with
        # shuffle disabled, which is NOT forward-chaining — only 1 of 5
        # folds honors the "fold k trains on settled_at < fold-k boundary"
        # rule). Each fold trains on the leading prefix and tests on the
        # next contiguous block of events; the union of test indices is
        # the trailing (n_splits/(n_splits+1)) fraction of the cell, not
        # the full cell.
        #
        # In-sample fit+predict would yield trivially-high AUCs on small
        # cells (n=30-44 events, 3 features → perfect separation by lbfgs).
        # k-fold CV measures the GENERALIZATION-AUC, which is the honest
        # falsification signal. Bootstrap is applied on top of the OOS
        # scores to get a CI on the OOS AUC point estimate.
        from sklearn.linear_model import LogisticRegression
        from sklearn.model_selection import TimeSeriesSplit

        per_cell: dict[tuple[str, str, str], dict[str, Any]] = {}
        rng = random.Random(42)

        def _oos_scores(
            X: list[list[float]], y: list[int], n_splits: int = 5
        ) -> tuple[list[float], list[int]] | None:
            """Return OOS predicted probabilities + aligned y_true via TimeSeriesSplit.

            Forward-chaining temporal CV per plan-doc § Method step 5 +
            Invariant pin "fold k trains on windows with settled_at <
            fold-k boundary only." Cells with both classes in every
            train-split are required; single-class train-split returns
            None (cell drops to insufficient with note).
            """
            if n_splits < 2 or len(y) < n_splits + 1:
                return None
            kf = TimeSeriesSplit(n_splits=n_splits)
            oos_y: list[int] = []
            oos_score: list[float] = []
            for tr_idx, te_idx in kf.split(X):
                y_tr = [y[i] for i in tr_idx]
                if len(set(y_tr)) < 2:
                    return None
                X_tr = [X[i] for i in tr_idx]
                X_te = [X[i] for i in te_idx]
                try:
                    clf = LogisticRegression(C=1.0, solver="lbfgs", max_iter=200)
                    clf.fit(X_tr, y_tr)
                    probs = clf.predict_proba(X_te)[:, 1].tolist()
                except (ValueError, RuntimeError):
                    return None
                for i, p in zip(te_idx, probs):
                    oos_y.append(int(y[i]))
                    oos_score.append(float(p))
            return oos_score, oos_y

        for cell_key in sorted({tuple(w["cell"]) for w in per_window}):
            cell_events = [w for w in per_window if tuple(w["cell"]) == cell_key]
            n_events = len(cell_events)
            status = classify_cell_sufficiency(n_events=n_events)
            if status == "insufficient":
                per_cell[cell_key] = {
                    "n_events": n_events,
                    "status": "insufficient",
                    "auc_T60": None,
                    "auc_lower": None,
                    "auc_upper": None,
                    "auc_T300": None,
                }
                continue
            # Check class balance.
            y_arr = [int(e["y_w"]) for e in cell_events]
            if len(set(y_arr)) < 2:
                per_cell[cell_key] = {
                    "n_events": n_events,
                    "status": "insufficient",
                    "auc_T60": None,
                    "auc_lower": None,
                    "auc_upper": None,
                    "auc_T300": None,
                    "note": "single-class cell",
                }
                continue

            X_t60 = [
                [
                    e["feat_t60_yes_mid"],
                    e["feat_t60_yes_spread"],
                    e["feat_t60_delta_spot_60s"],
                ]
                for e in cell_events
            ]

            # Sort cell events by `settled_at` parsed-datetime to respect the
            # plan-doc temporal-fold ordering. Ticker-name sort (impl-R1 initial)
            # is fragile across month/year boundaries because the Kalshi date
            # encoding `26MAY200345` sorts lexicographically, not chronologically.
            order = sorted(
                range(n_events),
                key=lambda i: _parse_iso(cell_events[i]["settled_at"]),
            )
            X_ordered = [X_t60[i] for i in order]
            y_ordered = [y_arr[i] for i in order]

            scored = _oos_scores(X_ordered, y_ordered, n_splits=5)
            if scored is None:
                per_cell[cell_key] = {
                    "n_events": n_events,
                    "status": "insufficient",
                    "auc_T60": None,
                    "auc_lower": None,
                    "auc_upper": None,
                    "auc_T300": None,
                    "note": "CV fold had single-class train-split (insufficient class balance)",
                }
                continue
            oos_score, oos_y = scored
            try:
                auc_point = compute_auc_with_orientation(
                    y_true=oos_y, y_score=oos_score
                )
            except ValueError:
                per_cell[cell_key] = {
                    "n_events": n_events,
                    "status": "insufficient",
                    "auc_T60": None,
                    "auc_lower": None,
                    "auc_upper": None,
                    "auc_T300": None,
                    "note": "OOS scores degenerate (single-class union)",
                }
                continue

            # Lock orientation at the POINT-AUC level (impl-R1 M4 fix). The
            # initial impl re-flipped per-resample, which biased the lower-CI
            # upward for cells with true AUC near 0.5 (every resample contributes
            # ≥0.5, so CI lower-bound is always ≥0.5). Lock the sign once based
            # on raw point-AUC, then apply the same orientation to every resample.
            from sklearn.metrics import roc_auc_score

            raw_point_auc = float(roc_auc_score(list(oos_y), list(oos_score)))
            flip_orientation = raw_point_auc < 0.5

            def _signed_auc(yv: list[int], sv: list[float]) -> float:
                raw = float(roc_auc_score(yv, sv))
                return (1.0 - raw) if flip_orientation else raw

            # Bootstrap CI on OOS scores: resample the (oos_y, oos_score)
            # paired set with replacement and recompute AUC each iteration.
            # Pairs preserve the OOS prediction structure; we don't re-fit
            # per resample (fold-stability is captured by the OOS point).
            n_oos = len(oos_y)
            boot_aucs: list[float] = []
            for _ in range(bootstrap_n):
                idxs = [rng.randrange(n_oos) for _ in range(n_oos)]
                yb = [oos_y[i] for i in idxs]
                if len(set(yb)) < 2:
                    continue
                sb = [oos_score[i] for i in idxs]
                try:
                    boot_aucs.append(_signed_auc(yb, sb))
                except ValueError:
                    continue
            if len(boot_aucs) < 50:
                per_cell[cell_key] = {
                    "n_events": n_events,
                    "status": "insufficient",
                    "auc_T60": auc_point,
                    "auc_lower": None,
                    "auc_upper": None,
                    "auc_T300": None,
                    "note": f"bootstrap aborted ({len(boot_aucs)} successful samples)",
                }
                continue
            boot_aucs.sort()
            lo_idx = max(0, math.ceil(0.025 * len(boot_aucs)) - 1)
            hi_idx = min(len(boot_aucs) - 1, math.ceil(0.975 * len(boot_aucs)) - 1)
            auc_lower = boot_aucs[lo_idx]
            auc_upper = boot_aucs[hi_idx]

            # T-300 secondary diagnostic — same OOS approach, reporting only.
            t300_events = [
                e for e in cell_events if e.get("feat_t300_yes_mid") is not None
            ]
            auc_t300: float | None = None
            if len(t300_events) >= MIN_EVENTS_PER_CELL:
                y_t300 = [int(e["y_w"]) for e in t300_events]
                if len(set(y_t300)) >= 2:
                    X_t300 = [
                        [e["feat_t300_yes_mid"], e["feat_t300_yes_spread"]]
                        for e in t300_events
                    ]
                    order300 = sorted(
                        range(len(t300_events)),
                        key=lambda i: _parse_iso(t300_events[i]["settled_at"]),
                    )
                    X300o = [X_t300[i] for i in order300]
                    y300o = [y_t300[i] for i in order300]
                    scored300 = _oos_scores(X300o, y300o, n_splits=5)
                    if scored300 is not None:
                        oos_s300, oos_y300 = scored300
                        try:
                            auc_t300 = compute_auc_with_orientation(
                                y_true=oos_y300, y_score=oos_s300
                            )
                        except ValueError:
                            auc_t300 = None

            per_cell[cell_key] = {
                "n_events": n_events,
                "status": "sufficient",
                "auc_T60": auc_point,
                "auc_lower": auc_lower,
                "auc_upper": auc_upper,
                "auc_T300": auc_t300,
            }

        # 6. Verdict.
        cells_for_verdict = [
            {
                "asset": k[0],
                "cell": f"{k[1]}_{k[2]}",
                "status": v["status"],
                "auc_lower": v.get("auc_lower"),
                "auc_upper": v.get("auc_upper"),
            }
            for k, v in per_cell.items()
        ]
        verdict = classify_verdict(
            cells=cells_for_verdict, auc_threshold=AUC_KILL_THRESHOLD
        )
        cells_clearing = [
            k
            for k, v in per_cell.items()
            if v.get("status") == "sufficient"
            and v.get("auc_lower") is not None
            and float(v["auc_lower"]) >= AUC_KILL_THRESHOLD
        ]

        result: dict[str, Any] = {
            "verdict": verdict,
            "per_cell_results": per_cell,
            "n_windows": len(windows),
            "n_windows_with_state": len(per_window),
            "window_days": days,
            "window_start_ts": window_start_ts,
            "sigma_median_per_asset": sigma_medians,
            "auc_threshold": AUC_KILL_THRESHOLD,
            "min_events_per_cell": MIN_EVENTS_PER_CELL,
            "cells_clearing_threshold": cells_clearing,
            "drop_counters": drop_counters,
            "bootstrap_n": bootstrap_n,
            "timesteps_s": timesteps_s,
            "bankroll_dollars": bankroll_dollars,
        }

    finally:
        conn.close()

    if output_path:
        with open(output_path, "w", encoding="utf-8") as fh:
            fh.write(_format_verdict_markdown(result))
    return result


def _format_verdict_markdown(result: Mapping[str, Any]) -> str:
    """Render the verdict result dict as a markdown report body.

    SUBSET of the curated verdict-doc at `kb/findings/ct-mdp-f0-5-verdict.md`
    — auto-generated table for ad-hoc re-runs; the curated finding doc is
    source-of-truth for downstream decisions (F0.1/F0.4 precedent).
    """
    lines: list[str] = []
    lines.append("# F0.5 settlement-window gamma falsification — verdict\n")
    lines.append(
        f"**Verdict: {result['verdict']}** "
        f"(threshold AUC_lower ≥ {result['auc_threshold']} per cell)\n"
    )
    lines.append(
        f"Window: {result['window_days']} days "
        f"(start {result.get('window_start_ts', '?')})\n"
    )
    lines.append(
        f"n_windows: {result['n_windows']} (settled), "
        f"with-state: {result['n_windows_with_state']}\n"
    )
    lines.append(f"Bootstrap N: {result['bootstrap_n']}\n")
    lines.append("\n## Per-cell AUC + bootstrap CI\n")
    lines.append("| Asset | Vol | DayNight | n_events | AUC_T60 | AUC_lower | AUC_upper | AUC_T300 | status |")
    lines.append("|---|---|---|---|---|---|---|---|---|")
    for k in sorted(result["per_cell_results"].keys()):
        v = result["per_cell_results"][k]
        asset, vol, dn = k
        status = v.get("status", "?")
        n = v.get("n_events", 0)

        def _fmt(x: Any) -> str:
            if x is None:
                return "—"
            return f"{float(x):.3f}"

        lines.append(
            f"| {asset} | {vol} | {dn} | {n} | "
            f"{_fmt(v.get('auc_T60'))} | {_fmt(v.get('auc_lower'))} | "
            f"{_fmt(v.get('auc_upper'))} | {_fmt(v.get('auc_T300'))} | {status} |"
        )
    lines.append("\n## Drop counters\n")
    for k, v in (result.get("drop_counters") or {}).items():
        lines.append(f"- {k}: {v}")
    return "\n".join(lines) + "\n"


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="F0.5 settlement-window gamma falsification"
    )
    parser.add_argument(
        "--db", default="state.db", help="Path to state.db (default: ./state.db)"
    )
    parser.add_argument(
        "--days", type=int, default=5, help="Analysis window in days"
    )
    parser.add_argument(
        "--bootstrap-n", type=int, default=1000, help="Bootstrap resample count"
    )
    parser.add_argument(
        "--settle-processing-delay-s",
        type=float,
        default=MIN_SETTLE_PROCESSING_DELAY_S,
        help="Buffer (s) between settled_at and T-0 window close (≥5s, default 5)",
    )
    parser.add_argument(
        "--out", default=None, help="Output markdown path (default: stdout)"
    )
    return parser.parse_args(argv)


if __name__ == "__main__":  # pragma: no cover
    args = _parse_args()
    result = main(
        db_path=args.db,
        days=args.days,
        bootstrap_n=args.bootstrap_n,
        settle_processing_delay_s=args.settle_processing_delay_s,
        output_path=args.out,
    )
    print(result)
