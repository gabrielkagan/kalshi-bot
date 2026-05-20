"""F0.1 — Stale-quote sniping falsification (CT-MDP Attack #1, Phase 0).

Skeleton ship — defines the public API surface for the failing-assertion
test scaffold at `tests/research/test_f0_1_stale_quote_falsification.py`.
All algorithmic helpers raise `NotImplementedError`; subsequent commits
fill them in function-by-function (TDD GREEN progression).

Parent plan: kb/decisions/ct-mdp-f0-1-stale-quote-falsification-plan.md
Parent ClickUp: 86ba18zg8
Umbrella: kb/decisions/ct-mdp-attack-alpha-program-plan.md (ticket 86ba18zbv)

Hypothesis:
  Kalshi NBBO refreshes lag Coinbase price moves. During the lag, the
  resting Kalshi quote is takeable at a price inconsistent with current
  spot. Sum-of-(|dislocation| × min(size, MAX_TAKE) × hit_probability)
  over the available data window estimates the annual ceiling on
  Attack #1. (`duration` is recorded as honest-reporting metadata but
  is NOT a multiplicative factor in the per-event value — see
  `_compute_event_record` docstring + plan-doc § Method L74.)

Methodology note (per R1 RCA on cache_age_ms semantics):
  `cache_age_ms` from market_observations_continuous is a CONFIRMATION
  signal (wall-clock age of the bot's cached Kalshi orderbook from
  bot/snapshots/market_observations_snapshotter.py:511,528) — NOT a
  cross-venue dislocation duration measure. Coinbase σ-moves are sourced
  separately from evaluated_opportunities.*_spot_at_decision snapshots
  (cross-asset cadence ~30s per the R2-M2 empirical). Dislocation duration
  is computed as:
    duration = max(0, now − max(now − cache_age_ms/1000, t_coinbase_move))
  i.e., the binding constraint is whichever clock (last-Kalshi-update or
  the Coinbase-move-time) is most recent.

Kill threshold (per ticket 86ba18zg8):
  If 95th-pct ceiling across 7 assets < $5K/yr, kill Attack #1.

Data sources (per RCA at plan-doc kickoff 2026-05-20):
  - market_observations_continuous: ~5d of Kalshi NBBO + cache_age_ms
    (primary spine).
  - evaluated_opportunities: Coinbase spot at decision moments
    (cross-asset cadence ~30s) — drives σ-event detection per the R2-M2
    empirical.
  - 7-asset universe is pinned in-script at `ASSET_TICKER_PREFIX` (not
    derived from settled_trades — F0.1 does not consume any settled
    outcomes).

Run:
  python3 scripts/research/f0_1_stale_quote_falsification.py --db state.db
"""

from __future__ import annotations

import argparse
import datetime as dt
import math
import random
import sqlite3
from collections import defaultdict
from typing import Any, Iterable, Mapping, Sequence

# Anti-fantasy size clamp on observed available size (per plan-doc § Method).
MAX_TAKE: int = 100

# Schema invariants pinned by tests/research/test_f0_1_stale_quote_falsification.py.
MOC_REQUIRED_COLUMNS: tuple[str, ...] = (
    "ticker",
    "observation_time",
    "yes_bid_cents",
    "yes_ask_cents",
    "no_bid_cents",
    "no_ask_cents",
    "bid_depth",
    "ask_depth",
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

# SETTLED_TRADES_REQUIRED_COLUMNS removed at impl-R1-M2: the constant was
# declared + test-pinned but `settled_trades` is never queried by the F0.1
# pipeline. The 7-asset universe lives in `ASSET_TICKER_PREFIX` (below).


def _parse_iso(ts: str) -> dt.datetime:
    """Parse ISO-8601 timestamp with optional `Z` suffix to aware datetime.

    Lexicographic comparison on ISO strings breaks under mixed precision
    (e.g., `"2026-05-20T10:01:00Z"` vs `"2026-05-20T10:01:00.999999Z"` —
    `.` 0x2E < `Z` 0x5A, so a microsecond-precision row in the same second
    AFTER the event compares as `≤` a second-precision event timestamp).
    Parse to datetime objects and compare numerically instead.
    """
    if ts.endswith("Z"):
        ts = ts[:-1] + "+00:00"
    return dt.datetime.fromisoformat(ts)


def select_stale_snapshot(
    *,
    moc_rows: Sequence[Mapping[str, Any]],
    sigma_move_time: str,
) -> list[Mapping[str, Any]]:
    """Return moc rows whose observation_time ≤ sigma_move_time.

    Pinned no-look-ahead invariant. Compares via parsed datetime (NOT
    lexicographic) so mixed-precision timestamps (`...:00Z` vs
    `...:00.999999Z`) don't break the invariant.
    """
    event_dt = _parse_iso(sigma_move_time)
    return [row for row in moc_rows if _parse_iso(row["observation_time"]) <= event_dt]


def compute_ceiling(
    *,
    events: Iterable[Mapping[str, Any]],
    asset: str,
    regime_conditioned: bool = True,
    window_days: float = 5.0,
) -> dict[str, float]:
    """Per-asset annualized dollar-ceiling, regime-conditioned when flag set.

    Each event is a mapping with: dislocation_cents, available_size,
    hit_probability, and (when regime_conditioned) vol_regime in {high,low}
    + daynight in {day,night}. Per-event value in cents goes through
    `aggregate_event_value` so the MAX_TAKE clamp + hit-prob sanity check
    apply uniformly.

    Returns dict with keys: vol_high, vol_low, day, night, total.
    Values are ANNUALIZED dollars (cents → dollars × 365/window_days).
    The 4 bucket keys are orthogonal partitions of `total`:
        vol_high + vol_low = total
        day + night = total
    """
    if window_days <= 0:
        raise ValueError(f"window_days must be > 0; got {window_days}")
    annualization = 365.0 / window_days

    bucket_cents: dict[str, float] = {
        "vol_high": 0.0,
        "vol_low": 0.0,
        "day": 0.0,
        "night": 0.0,
    }
    total_cents = 0.0
    for ev in events:
        value_cents = aggregate_event_value(
            dislocation_cents=float(ev["dislocation_cents"]),
            available_size=int(ev["available_size"]),
            hit_probability=float(ev["hit_probability"]),
        )
        total_cents += value_cents
        if regime_conditioned:
            vol = ev.get("vol_regime")
            if vol in ("high", "low"):
                bucket_cents[f"vol_{vol}"] += value_cents
            dn = ev.get("daynight")
            if dn in ("day", "night"):
                bucket_cents[dn] += value_cents

    def annualize_dollars(c: float) -> float:
        return (c / 100.0) * annualization

    return {
        "vol_high": annualize_dollars(bucket_cents["vol_high"]),
        "vol_low": annualize_dollars(bucket_cents["vol_low"]),
        "day": annualize_dollars(bucket_cents["day"]),
        "night": annualize_dollars(bucket_cents["night"]),
        "total": annualize_dollars(total_cents),
    }


def bootstrap_ceiling_ci(
    *,
    per_event_values: Sequence[float],
    n_resamples: int = 1000,
    seed: int | None = None,
) -> dict[str, float]:
    """Return {ci_low, point, ci_high} via percentile bootstrap on per-event $ values.

    The ceiling is a sum (Σ per-event $ over the window). Resample the event
    set with replacement N times, compute the sum each time, and report:

    - point = observed sum of per_event_values (NOT mean of resamples — the
      point estimate of an annual ceiling is the observed total, not a
      bias-adjusted statistic).
    - ci_low / ci_high = 2.5th / 97.5th percentile of resampled sums.

    Edge case: empty input → all-zeros (no events ⇒ no ceiling, no CI to draw).
    """
    n = len(per_event_values)
    point = float(sum(per_event_values))
    if n == 0:
        return {"ci_low": 0.0, "point": 0.0, "ci_high": 0.0}

    rng = random.Random(seed)
    values = list(per_event_values)
    resampled_sums: list[float] = []
    for _ in range(n_resamples):
        # Resample with replacement; sum gives the bootstrap statistic.
        total = 0.0
        for _ in range(n):
            total += values[rng.randrange(n)]
        resampled_sums.append(total)
    resampled_sums.sort()

    # Nearest-rank percentile: rank k = ceil(p * N), index = k - 1.
    # impl-R1-M5 fix: prior impl used `int(p*N) - 1` for lo (one rank too
    # low) and `int(p*N)` for hi (one rank too high at N=1000) — drifted
    # from docstring spec.
    lo_idx = max(0, math.ceil(0.025 * n_resamples) - 1)
    hi_idx = min(n_resamples - 1, math.ceil(0.975 * n_resamples) - 1)
    return {
        "ci_low": float(resampled_sums[lo_idx]),
        "point": point,
        "ci_high": float(resampled_sums[hi_idx]),
    }


def aggregate_event_value(
    *,
    dislocation_cents: float,
    available_size: int,
    hit_probability: float,
) -> float:
    """Per-event takeable value in CENTS, with MAX_TAKE size clamp + hit-prob sanity check.

    Raises ValueError if hit_probability is outside [0.0, 1.0].
    """
    if not (0.0 <= hit_probability <= 1.0):
        raise ValueError(
            f"hit_probability must be in [0,1]; got {hit_probability} "
            "(likely a methodology bug — see plan-doc § Method)"
        )
    size = min(available_size, MAX_TAKE)
    return abs(dislocation_cents) * size * hit_probability


def classify_verdict(
    *,
    per_asset_ceilings: Mapping[str, float],
    threshold_dollars: float = 5000.0,
) -> str:
    """Return 'KILL' if all assets < threshold; 'SURVIVE' if any ≥ threshold."""
    if any(c >= threshold_dollars for c in per_asset_ceilings.values()):
        return "SURVIVE"
    return "KILL"


def survival_diagnostics(
    *,
    per_asset_ceilings: Mapping[str, float],
    threshold_dollars: float = 5000.0,
) -> dict[str, Any]:
    """Return per-asset/program-level diagnostics for the verdict consumer.

    The umbrella program-level gate (`kb/decisions/ct-mdp-attack-alpha-program-plan.md`
    § Rules of engagement) is ≥2 of 3 falsifications survive their per-attack threshold.
    Per-attack F0.1 survives on `n_assets_clearing_threshold ≥ 1` — but downstream
    callers may want to weight a single-asset SURVIVE differently (false-survive risk
    at small sample size per scaffold-R1-M5). Diagnostics let them.
    """
    clearing = [a for a, c in per_asset_ceilings.items() if c >= threshold_dollars]
    return {
        "verdict": classify_verdict(
            per_asset_ceilings=per_asset_ceilings,
            threshold_dollars=threshold_dollars,
        ),
        "n_assets_clearing_threshold": len(clearing),
        "assets_clearing_threshold": sorted(clearing),
        "threshold_dollars": threshold_dollars,
    }


# Asset → moc ticker prefix mapping (KX<ASSET>15M-...). Pinned by the 7-asset
# universe in `bot/constants.py`; per-asset KX prefixes confirmed in state.db.
ASSET_TICKER_PREFIX: dict[str, str] = {
    "BTC": "KXBTC",
    "ETH": "KXETH",
    "SOL": "KXSOL",
    "XRP": "KXXRP",
    "HYPE": "KXHYPE",
    "DOGE": "KXDOGE",
    "BNB": "KXBNB",
}

# Rolling realized-vol window for σ-normalization (per plan-doc Open Q2).
_VOL_WINDOW_SECONDS: float = 3600.0  # 60 min


def _yes_mid_cents(row: Mapping[str, Any]) -> float | None:
    """Return the YES-side mid from a moc row, or None if either side is NULL."""
    yb, ya = row.get("yes_bid_cents"), row.get("yes_ask_cents")
    if yb is None or ya is None:
        return None
    return (float(yb) + float(ya)) / 2.0


def _take_size(row: Mapping[str, Any]) -> int:
    """Anti-fantasy: take the SMALLER of bid_depth / ask_depth as available size."""
    bd = row.get("bid_depth") or 0
    ad = row.get("ask_depth") or 0
    return min(int(bd), int(ad))


def _classify_daynight(ts: dt.datetime) -> str:
    """US-market overlap window 13:00-21:00 UTC = day; else night."""
    return "day" if 13 <= ts.hour < 21 else "night"


def _load_spot_series(conn: sqlite3.Connection, asset: str, start_ts: str) -> list[tuple[dt.datetime, float]]:
    """Load (time, spot) for an asset from evaluated_opportunities snapshots."""
    col = f"{asset.lower()}_spot_at_decision"
    cur = conn.cursor()
    cur.execute(
        f"SELECT evaluation_time, {col} FROM evaluated_opportunities "  # noqa: S608 — col from controlled mapping
        f"WHERE {col} IS NOT NULL AND evaluation_time >= ? "
        f"ORDER BY evaluation_time",
        (start_ts,),
    )
    series: list[tuple[dt.datetime, float]] = []
    seen_times: set[dt.datetime] = set()
    for t, p in cur.fetchall():
        try:
            dt_t = _parse_iso(t)
        except ValueError:
            continue
        if dt_t in seen_times:
            continue
        seen_times.add(dt_t)
        series.append((dt_t, float(p)))
    return series


def _detect_sigma_events(
    series: Sequence[tuple[dt.datetime, float]],
    sigma_threshold: float,
) -> list[tuple[dt.datetime, float, float]]:
    """Detect Coinbase σ-events on a spot time-series.

    Returns list of (t_C, log_return, sigma_at_t). sigma_at_t is the rolling
    realized vol of log-returns over the prior _VOL_WINDOW_SECONDS window.
    An event is any time index where |log_return / sigma_at_t| ≥ sigma_threshold.

    Trailing window with at least 5 prior returns required; bootstrap period
    pre-warm yields no events.
    """
    if len(series) < 2:
        return []
    log_returns: list[tuple[dt.datetime, float]] = []
    for i in range(1, len(series)):
        t_prev, p_prev = series[i - 1]
        t_cur, p_cur = series[i]
        if p_prev <= 0 or p_cur <= 0:
            continue
        log_returns.append((t_cur, math.log(p_cur / p_prev)))

    events: list[tuple[dt.datetime, float, float]] = []
    for i, (t_cur, r_cur) in enumerate(log_returns):
        # Rolling window: returns within prior _VOL_WINDOW_SECONDS, excluding the current point.
        window_start = t_cur - dt.timedelta(seconds=_VOL_WINDOW_SECONDS)
        window_rets = [
            r for (t, r) in log_returns[:i]
            if t >= window_start
        ]
        if len(window_rets) < 5:
            continue
        mean = sum(window_rets) / len(window_rets)
        var = sum((r - mean) ** 2 for r in window_rets) / max(1, len(window_rets) - 1)
        sigma = var ** 0.5
        if sigma <= 0:
            continue
        if abs(r_cur / sigma) >= sigma_threshold:
            events.append((t_cur, r_cur, sigma))
    return events


def _load_moc_rows(conn: sqlite3.Connection, ticker_prefix: str, start_ts: str) -> list[dict[str, Any]]:
    """Load moc rows for an asset's tickers in time order."""
    cur = conn.cursor()
    cur.execute(
        "SELECT ticker, observation_time, yes_bid_cents, yes_ask_cents, "
        "no_bid_cents, no_ask_cents, bid_depth, ask_depth, cache_age_ms "
        "FROM market_observations_continuous "
        "WHERE ticker LIKE ? AND observation_time >= ? "
        "ORDER BY observation_time",
        (ticker_prefix + "%", start_ts),
    )
    cols = ("ticker", "observation_time", "yes_bid_cents", "yes_ask_cents",
            "no_bid_cents", "no_ask_cents", "bid_depth", "ask_depth", "cache_age_ms")
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def _match_event_to_moc(
    moc_by_ticker: Mapping[str, list[dict[str, Any]]],
    event_time: dt.datetime,
) -> tuple[dict[str, Any], dict[str, Any]] | None:
    """Find the (stale, refreshed) moc pair around event_time.

    For each ticker, walks forward to find the most-recent moc row with a
    valid YES mid at observation_time ≤ event_time (stale), then the FIRST
    subsequent row of the SAME ticker with a valid YES mid AND a different
    mid (refresh detected). Returns the pair from whichever ticker has the
    nearest refresh (= the active 15m market at event_time).
    """
    best: tuple[float, dict[str, Any], dict[str, Any]] | None = None
    for ticker, rows in moc_by_ticker.items():
        stale: dict[str, Any] | None = None
        for row in rows:
            try:
                obs_dt = _parse_iso(row["observation_time"])
            except ValueError:
                continue
            mid = _yes_mid_cents(row)
            if obs_dt <= event_time:
                if mid is not None:
                    stale = {**row, "_obs_dt": obs_dt, "_mid": mid}
                continue
            # obs_dt > event_time — candidate refresh
            if stale is None or mid is None:
                continue
            if mid == stale["_mid"]:
                # No actual refresh of mid; keep looking on this ticker.
                continue
            gap = (obs_dt - event_time).total_seconds()
            refreshed = {**row, "_obs_dt": obs_dt, "_mid": mid}
            if best is None or gap < best[0]:
                best = (gap, stale, refreshed)
            break
    if best is None:
        return None
    return best[1], best[2]


def _compute_event_record(
    stale: Mapping[str, Any],
    refreshed: Mapping[str, Any],
    event_time: dt.datetime,
    sigma_at_t: float,
    sigma_median: float,
    drop_counters: dict[str, int] | None = None,
) -> dict[str, Any] | None:
    """Build the per-event aggregate dict that feeds compute_ceiling().

    `_match_event_to_moc` already guarantees `stale.obs_dt ≤ event_time
    < refreshed.obs_dt` AND `stale._mid != refreshed._mid` (different mid
    on the post-event row of the same ticker). So `_compute_event_record`
    drops events for one of two REMAINING reasons only:
      - `available_size <= 0` (no depth on the stale snapshot's bid OR ask)
      - `dislocation == 0` — IMPOSSIBLE here given the matcher pre-filter,
        retained as a defensive guard.

    `cache_age_ms` is recorded as metadata (per plan-doc R1 RCA on the
    confirmation-signal-vs-duration-measure semantics) but does NOT gate
    the result — under the matcher's invariants `last_kalshi_update =
    stale.obs_dt - cache_age_ms/1000 ≤ stale.obs_dt ≤ event_time`, so
    the construction `max(last_kalshi_update, event_time)` always picks
    `event_time` and the cache_age_ms branch is inert. We keep
    `duration_s` for honest reporting but `hit_prob` is structurally
    1.0 whenever a mid-changing refresh occurred post-event. This is
    the strict-takeable UPPER-BOUND assumption per plan-doc Open Q3.
    """
    stale_mid = stale["_mid"]
    refresh_mid = refreshed["_mid"]
    dislocation = abs(refresh_mid - stale_mid)
    if dislocation <= 0:
        if drop_counters is not None:
            drop_counters["zero_dislocation"] = drop_counters.get("zero_dislocation", 0) + 1
        return None
    size = _take_size(stale)
    if size <= 0:
        if drop_counters is not None:
            drop_counters["zero_size"] = drop_counters.get("zero_size", 0) + 1
        return None
    cache_age_ms = stale.get("cache_age_ms")
    last_kalshi_update = stale["_obs_dt"]
    if cache_age_ms is not None:
        last_kalshi_update = stale["_obs_dt"] - dt.timedelta(milliseconds=int(cache_age_ms))
    binding = max(last_kalshi_update, event_time)
    duration_s = max(0.0, (refreshed["_obs_dt"] - binding).total_seconds())
    hit_prob = 1.0  # strict-takeable upper-bound; see docstring + plan-doc Open Q3.
    return {
        "dislocation_cents": float(dislocation),
        "available_size": int(size),
        "hit_probability": hit_prob,
        "duration_s": duration_s,
        "vol_regime": "high" if sigma_at_t > sigma_median else "low",
        "daynight": _classify_daynight(event_time),
        "event_time": event_time.isoformat(),
    }


def _per_asset_analysis(
    conn: sqlite3.Connection,
    asset: str,
    start_ts: str,
    window_days: float,
    sigma_threshold: float,
    bootstrap_n: int,
) -> dict[str, Any]:
    """Full per-asset pipeline. Returns ceiling dict + per-event values + n_events."""
    series = _load_spot_series(conn, asset, start_ts)
    sigma_events = _detect_sigma_events(series, sigma_threshold)
    if not sigma_events:
        return {
            "ceiling": {"vol_high": 0.0, "vol_low": 0.0, "day": 0.0, "night": 0.0, "total": 0.0},
            "ci": {"ci_low": 0.0, "point": 0.0, "ci_high": 0.0},
            "n_events": 0,
            "n_matched": 0,
            "events": [],
        }
    sigmas_sorted = sorted(s for _, _, s in sigma_events)
    sigma_median = sigmas_sorted[len(sigmas_sorted) // 2]

    prefix = ASSET_TICKER_PREFIX[asset]
    moc_rows = _load_moc_rows(conn, prefix, start_ts)
    moc_by_ticker: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in moc_rows:
        moc_by_ticker[r["ticker"]].append(r)

    event_records: list[dict[str, Any]] = []
    drop_counters: dict[str, int] = {
        "no_match": 0,
        "zero_dislocation": 0,
        "zero_size": 0,
    }
    for t_C, _r, sigma_t in sigma_events:
        pair = _match_event_to_moc(moc_by_ticker, t_C)
        if pair is None:
            drop_counters["no_match"] += 1
            continue
        stale, refreshed = pair
        rec = _compute_event_record(
            stale, refreshed, t_C, sigma_t, sigma_median,
            drop_counters=drop_counters,
        )
        if rec is not None:
            event_records.append(rec)

    ceiling = compute_ceiling(
        events=event_records,
        asset=asset,
        regime_conditioned=True,
        window_days=window_days,
    )
    # Bootstrap CI on per-event annualized $-values (matches the `total` key).
    annualization = 365.0 / window_days if window_days > 0 else 0.0
    per_event_dollars = [
        aggregate_event_value(
            dislocation_cents=r["dislocation_cents"],
            available_size=r["available_size"],
            hit_probability=r["hit_probability"],
        ) / 100.0 * annualization
        for r in event_records
    ]
    ci = bootstrap_ceiling_ci(
        per_event_values=per_event_dollars,
        n_resamples=bootstrap_n,
        seed=42,
    )
    return {
        "ceiling": ceiling,
        "ci": ci,
        "n_events": len(sigma_events),
        "n_matched": len(event_records),
        "events": event_records,
        "drop_counters": drop_counters,
    }


def _format_verdict_markdown(result: Mapping[str, Any]) -> str:
    """Render the verdict result dict as a markdown report body.

    Intentionally a SUBSET of the curated verdict-doc at
    `kb/findings/ct-mdp-f0-1-verdict.md` — the curated doc adds
    methodology summary, caveat register, deflation analysis, and
    drop_counters tables that this auto-generated output does NOT
    surface. Do not "fix" the gap by adding those sections here; the
    human-curated finding doc is the source of truth for downstream
    decisions, and a stale auto-gen would mask drift.
    """
    lines: list[str] = []
    lines.append(f"# F0.1 stale-quote sniping falsification — verdict\n")
    lines.append(f"**Verdict: {result['verdict']}** "
                 f"(threshold ${result['threshold_dollars']:.0f}/yr per asset)\n")
    lines.append(f"Window: {result['window_days']} days "
                 f"(start {result.get('window_start_ts', '?')})\n")
    lines.append(f"σ-threshold: {result['sigma_threshold']}, "
                 f"bootstrap N: {result['bootstrap_n']}\n")
    lines.append("\n## Per-asset ceiling + bootstrap CI\n")
    lines.append("| Asset | Total $/yr | CI low | CI high | σ-events | matched | vol_high | vol_low | day | night |")
    lines.append("|---|---|---|---|---|---|---|---|---|---|")
    for asset, ceil in result["per_asset_ceilings"].items():
        ci = result["per_asset_ci"].get(asset, {})
        n_ev = result["n_events_per_asset"].get(asset, 0)
        n_m = result["n_matched_per_asset"].get(asset, 0)
        rb = result["regime_breakdown"].get(asset, {})
        lines.append(
            f"| {asset} | ${ceil:,.0f} | ${ci.get('ci_low', 0):,.0f} | ${ci.get('ci_high', 0):,.0f} | "
            f"{n_ev} | {n_m} | ${rb.get('vol_high', 0):,.0f} | ${rb.get('vol_low', 0):,.0f} | "
            f"${rb.get('day', 0):,.0f} | ${rb.get('night', 0):,.0f} |"
        )
    return "\n".join(lines) + "\n"


def main(
    *,
    db_path: str,
    days: int = 5,
    sigma_threshold: float = 0.3,
    bootstrap_n: int = 1000,
    output_path: str | None = None,
) -> dict[str, Any]:
    """End-to-end Phase 0 falsification.

    Returns:
        {
            'verdict': 'KILL' | 'SURVIVE',
            'per_asset_ceilings': {'BTC': float, ...},
            'per_asset_ci': {'BTC': {'ci_low': float, 'ci_high': float, 'point': float}, ...},
            'window_days': int,
            'window_start_ts': str,
            'sigma_threshold': float,
            'bootstrap_n': int,
            'threshold_dollars': float,
            'n_events_per_asset': {'BTC': int, ...},
            'n_matched_per_asset': {'BTC': int, ...},
            'regime_breakdown': {'BTC': {'vol_high': float, ...}, ...},
        }
    """
    threshold_dollars = 5000.0
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=10000")
        # Anchor window to the latest moc observation_time in the DB so the
        # window slides with whatever data is present (synthetic-fixture
        # rows + production state.db share this path).
        cur = conn.cursor()
        cur.execute("SELECT MAX(observation_time) FROM market_observations_continuous")
        max_ts_row = cur.fetchone()
        if max_ts_row is None or max_ts_row[0] is None:
            window_start_ts = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days)).isoformat()
        else:
            max_dt = _parse_iso(max_ts_row[0])
            window_start_ts = (max_dt - dt.timedelta(days=days)).isoformat()

        per_asset_ceilings: dict[str, float] = {}
        per_asset_ci: dict[str, dict[str, float]] = {}
        n_events_per_asset: dict[str, int] = {}
        n_matched_per_asset: dict[str, int] = {}
        regime_breakdown: dict[str, dict[str, float]] = {}
        drop_counters_per_asset: dict[str, dict[str, int]] = {}
        for asset in ASSET_TICKER_PREFIX.keys():
            res = _per_asset_analysis(
                conn=conn,
                asset=asset,
                start_ts=window_start_ts,
                window_days=float(days),
                sigma_threshold=sigma_threshold,
                bootstrap_n=bootstrap_n,
            )
            per_asset_ceilings[asset] = res["ceiling"]["total"]
            per_asset_ci[asset] = res["ci"]
            n_events_per_asset[asset] = res["n_events"]
            n_matched_per_asset[asset] = res["n_matched"]
            regime_breakdown[asset] = {
                k: v for k, v in res["ceiling"].items() if k != "total"
            }
            drop_counters_per_asset[asset] = res["drop_counters"]
    finally:
        conn.close()

    verdict = classify_verdict(
        per_asset_ceilings=per_asset_ceilings,
        threshold_dollars=threshold_dollars,
    )
    result: dict[str, Any] = {
        "verdict": verdict,
        "per_asset_ceilings": per_asset_ceilings,
        "per_asset_ci": per_asset_ci,
        "window_days": days,
        "window_start_ts": window_start_ts,
        "sigma_threshold": sigma_threshold,
        "bootstrap_n": bootstrap_n,
        "threshold_dollars": threshold_dollars,
        "n_events_per_asset": n_events_per_asset,
        "n_matched_per_asset": n_matched_per_asset,
        "regime_breakdown": regime_breakdown,
        "drop_counters_per_asset": drop_counters_per_asset,
    }

    if output_path:
        with open(output_path, "w", encoding="utf-8") as fh:
            fh.write(_format_verdict_markdown(result))
    return result


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="F0.1 stale-quote sniping falsification")
    parser.add_argument("--db", default="state.db", help="Path to state.db (default: ./state.db)")
    parser.add_argument("--days", type=int, default=5, help="Analysis window in days")
    parser.add_argument("--sigma-threshold", type=float, default=0.3,
                        help="σ-move threshold for event detection")
    parser.add_argument("--bootstrap-n", type=int, default=1000,
                        help="Bootstrap resample count")
    parser.add_argument("--out", default=None,
                        help="Output markdown path (default: stdout)")
    return parser.parse_args(argv)


if __name__ == "__main__":  # pragma: no cover
    args = _parse_args()
    result = main(
        db_path=args.db,
        days=args.days,
        sigma_threshold=args.sigma_threshold,
        bootstrap_n=args.bootstrap_n,
        output_path=args.out,
    )
    print(result)
