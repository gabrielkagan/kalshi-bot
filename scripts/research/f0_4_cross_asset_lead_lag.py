"""F0.4 — Cross-Asset Lead-Lag Falsification (CT-MDP Attack #4, Phase 0).

Full implementation post-impl-Bit ship. Primary algorithmic helpers
(`compute_lead_edge`, `bootstrap_lead_edge_ci`, `bonferroni_adjusted_ci`,
`classify_verdict`, `survival_diagnostics`, `main`) and data-shaping
helpers (`select_m_stale`, `select_m_responded`, `_parse_iso`,
`validate_lead_edge`, `compute_event_lead_edge`) are implemented; the
18-test suite at `tests/research/test_f0_4_cross_asset_lead_lag.py`
flips GREEN as each helper lands.

Parent plan: kb/decisions/ct-mdp-f0-4-cross-asset-lead-lag-plan.md
Parent ClickUp: 86ba18zgx
Umbrella: kb/decisions/ct-mdp-attack-alpha-program-plan.md (ticket 86ba18zbv)
Predecessor: scripts/research/f0_1_stale_quote_falsification.py (F0.1, SHIPPED PR #132).

Hypothesis:
  BTC leads ETH/SOL/XRP/HYPE/DOGE/BNB by 3-30s in high-vol regimes.
  For each BTC σ-move at t_C, the laggard's Kalshi mid does not re-price
  for Δt seconds; lead-edge per event = (M_responded − M_stale) signed
  by BTC direction. Per laggard × regime (4 buckets), aggregate mean
  lead-edge with bootstrap CI; apply Bonferroni correction across 24
  simultaneous tests.

Kill threshold (per ticket 86ba18zgx):
  If no laggard asset shows >3¢/trade lead-edge in ANY regime with
  Bonferroni-adjusted CI excluding zero, kill Attack #4.

Data sources:
  - market_observations_continuous: laggard Kalshi mid trajectory (~6s cadence).
  - evaluated_opportunities: per-asset *_spot_at_decision (cross-asset cadence ~30s mean).
  - 7-asset universe pinned at ASSET_TICKER_PREFIX; 6-laggard universe pinned at LAGGARD_ASSETS.

Run:
  python3 scripts/research/f0_4_cross_asset_lead_lag.py --db state.db
"""

from __future__ import annotations

import argparse
import bisect
import datetime as dt
import math
import random
import sqlite3
from collections import defaultdict
from typing import Any, Iterable, Mapping, Sequence

# ----- Anti-fantasy clamps + Bonferroni constants ------------------------

#: Max plausible cross-asset lead-edge in cents over the Δt window. A
#: laggard Kalshi mid moving > 25¢ in 30s from a single BTC σ-event is
#: implausible (binary range is 0-100) and likely indicates a methodology
#: bug (e.g., comparing across ticker expiries).
MAX_PLAUSIBLE_EDGE_CENTS: float = 25.0

#: Per-trade lead-edge floor (cents) per umbrella ticket 86ba18zgx kill rule.
KILL_THRESHOLD_CENTS: float = 3.0

#: Family-wise α = 0.05 / 24 across 6 laggards × 4 regimes.
BONFERRONI_N_TESTS_DEFAULT: int = 24

#: Min per-cell event count below which a cell reports "insufficient".
MIN_EVENTS_PER_CELL: int = 30

# ----- Schema invariants (test-pinned) -----------------------------------

MOC_REQUIRED_COLUMNS: tuple[str, ...] = (
    "ticker",
    "observation_time",
    "yes_bid_cents",
    "yes_ask_cents",
    "no_bid_cents",
    "no_ask_cents",
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

#: 6-laggard universe (BTC excluded — it's the leader).
LAGGARD_ASSETS: tuple[str, ...] = ("ETH", "SOL", "XRP", "HYPE", "DOGE", "BNB")

#: Asset → moc ticker prefix mapping (KX<ASSET>15M-...). Includes BTC
#: because BTC's σ-events are detected from BTC spot trajectory (eval_opps),
#: NOT from BTC moc (the lead-side mid is unused).
ASSET_TICKER_PREFIX: dict[str, str] = {
    "BTC": "KXBTC",
    "ETH": "KXETH",
    "SOL": "KXSOL",
    "XRP": "KXXRP",
    "HYPE": "KXHYPE",
    "DOGE": "KXDOGE",
    "BNB": "KXBNB",
}

#: 4 regime cells per laggard × 6 laggards = 24 cells total.
REGIMES: tuple[str, ...] = (
    "vol_high_day",
    "vol_high_night",
    "vol_low_day",
    "vol_low_night",
)

#: Rolling realized-vol window for σ-normalization (per F0.1 § Open Q2).
_VOL_WINDOW_SECONDS: float = 3600.0  # 60 min


# ----- ISO timestamp parsing ---------------------------------------------


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


# ----- M_stale / M_responded selection (no-look-ahead pins) --------------


def select_m_stale(
    *,
    moc_rows: Sequence[Mapping[str, Any]],
    sigma_event_time: str,
) -> Mapping[str, Any] | None:
    """Return the latest moc row with observation_time ≤ sigma_event_time.

    No-look-ahead invariant. Compares via parsed datetime (NOT
    lexicographic) so mixed-precision timestamps (`...:00Z` vs
    `...:00.999999Z`) don't break the invariant. Returns None if no
    pre-event row exists.
    """
    event_dt = _parse_iso(sigma_event_time)
    best: Mapping[str, Any] | None = None
    best_dt: dt.datetime | None = None
    for row in moc_rows:
        try:
            obs_dt = _parse_iso(row["observation_time"])
        except (ValueError, KeyError):
            continue
        if obs_dt <= event_dt and (best_dt is None or obs_dt > best_dt):
            best = row
            best_dt = obs_dt
    return best


def select_m_responded(
    *,
    moc_rows: Sequence[Mapping[str, Any]],
    sigma_event_time: str,
    delta_t_seconds: float,
) -> Mapping[str, Any] | None:
    """Return the latest moc row in (sigma_event_time, sigma_event_time + Δt].

    No-look-ahead invariant: the responded snapshot must come strictly AFTER
    the σ-event AND within Δt seconds (the lead-lag detection window). Latest
    in-window row gives the laggard maximum time to respond. Returns None
    if no in-window row exists.
    """
    event_dt = _parse_iso(sigma_event_time)
    upper = event_dt + dt.timedelta(seconds=delta_t_seconds)
    best: Mapping[str, Any] | None = None
    best_dt: dt.datetime | None = None
    for row in moc_rows:
        try:
            obs_dt = _parse_iso(row["observation_time"])
        except (ValueError, KeyError):
            continue
        if event_dt < obs_dt <= upper and (best_dt is None or obs_dt > best_dt):
            best = row
            best_dt = obs_dt
    return best


# ----- Per-event lead-edge computation + clamp ---------------------------


def compute_event_lead_edge(
    *,
    m_stale_cents: float,
    m_responded_cents: float,
    btc_return_at_event: float,
) -> dict[str, float]:
    """Per-event raw + signed lead-edge.

    Lead-edge = (M_responded − M_stale) × sign(BTC_return). A BTC up-move
    paired with a laggard up-move registers as POSITIVE (the laggard caught
    up); a BTC up-move paired with a laggard down-move (or vice versa)
    registers as NEGATIVE (the laggard already faded the consensus, so
    the strategy would be FADING the BTC move — and lose).

    The sign multiplier is the structural fix that prevents absolute-value
    framing from systematically over-reporting edge by treating noise as
    signal (per plan-doc § Hypothesis ¶2 list-item 3 + § Method → Per-laggard
    pipeline ¶2 bullet 3).

    Edge case: `btc_return_at_event == 0` returns `lead_edge_cents = 0`
    silently (sign=0 zeroes out any raw delta). Unreachable from the main
    pipeline — `_detect_sigma_events` requires `|log_return / sigma| ≥
    sigma_threshold` AND `sigma > 0`, which together imply `log_return ≠ 0`
    at any tick fed into this helper. The behavior is preserved here for
    callers who construct events outside the σ-detection pipeline (e.g.,
    unit tests with explicit lead-edges); they should guarantee non-zero
    `btc_return_at_event` or accept the 0c result as a "no-information"
    event.
    """
    raw = float(m_responded_cents) - float(m_stale_cents)
    if btc_return_at_event > 0:
        sign = 1.0
    elif btc_return_at_event < 0:
        sign = -1.0
    else:
        sign = 0.0
    signed = raw * sign
    return {
        "lead_edge_cents": signed,
        "raw_lead_edge_cents": raw,
        "m_stale_cents": float(m_stale_cents),
        "m_responded_cents": float(m_responded_cents),
        "btc_return_at_event": float(btc_return_at_event),
    }


def validate_lead_edge(*, lead_edge_cents: float) -> None:
    """Raise ValueError if |lead_edge_cents| > MAX_PLAUSIBLE_EDGE_CENTS.

    A laggard mid moving > 25¢ in 30s from a single BTC σ-event is
    implausible (binary range is 0-100) and likely indicates a methodology
    bug (e.g., comparing across ticker expiries). This primitive always
    raises on outliers — it's the unit-test-pinned "anti-fantasy clamp"
    contract per plan-doc § Method.

    Caller policy on the raise differs from the primitive's contract:
    the main pipeline (`_per_laggard_analysis`) catches the ValueError
    and counts the event as an `anti_fantasy_skip` drop (rather than
    aborting the whole run), then surfaces the outlier rate + first-20
    event details in the verdict-doc per impl-R1-T1. Direct callers
    constructing events outside the main pipeline (unit tests / REPL)
    propagate the raise. See plan-doc § Method anti-fantasy clamp +
    § Kill threshold (formal) row 3 for the layered contract.
    """
    if abs(lead_edge_cents) > MAX_PLAUSIBLE_EDGE_CENTS:
        raise ValueError(
            f"|lead_edge_cents|={abs(lead_edge_cents)} exceeds "
            f"MAX_PLAUSIBLE_EDGE_CENTS={MAX_PLAUSIBLE_EDGE_CENTS}; "
            "likely a methodology bug (e.g., comparing across ticker expiries)."
        )


# ----- Per-laggard regime-bucketed aggregator ----------------------------


def compute_lead_edge(
    *,
    events: Iterable[Mapping[str, Any]],
    laggard: str,
    regime_conditioned: bool = True,
) -> dict[str, dict[str, Any]]:
    """Bucket per-event lead-edges by regime; report point + n_events + status.

    Each event is a mapping with `lead_edge_cents` (float) + `regime`
    (one of REGIMES). When `regime_conditioned=False`, all events fold
    into a single key `"all"` — provided for API completeness and not
    used by the main pipeline.

    A cell with fewer than MIN_EVENTS_PER_CELL events reports
    `status="insufficient"` per the plan-doc § Method
    "Min sample size per regime cell ≥ 30 events" invariant.

    The `laggard` argument is currently a label-only parameter consumed
    by the caller for downstream tagging; the function itself is laggard-
    agnostic. (Pinned in the API to make the per-laggard pipeline call
    site explicit.)
    """
    del laggard  # label-only; consumed by the caller for tagging
    if regime_conditioned:
        by_regime: dict[str, list[float]] = {r: [] for r in REGIMES}
        for ev in events:
            r = ev.get("regime")
            if r in by_regime:
                by_regime[r].append(float(ev["lead_edge_cents"]))
        result: dict[str, dict[str, Any]] = {}
        for r in REGIMES:
            vals = by_regime[r]
            n = len(vals)
            if n == 0:
                result[r] = {"point": 0.0, "n_events": 0, "status": "insufficient"}
            elif n < MIN_EVENTS_PER_CELL:
                result[r] = {"point": sum(vals) / n, "n_events": n, "status": "insufficient"}
            else:
                result[r] = {"point": sum(vals) / n, "n_events": n, "status": "ok"}
        return result
    else:
        vals = [float(ev["lead_edge_cents"]) for ev in events]
        n = len(vals)
        if n == 0:
            return {"all": {"point": 0.0, "n_events": 0, "status": "insufficient"}}
        status = "insufficient" if n < MIN_EVENTS_PER_CELL else "ok"
        return {"all": {"point": sum(vals) / n, "n_events": n, "status": status}}


# ----- Bootstrap CI + Bonferroni adjustment ------------------------------


def bootstrap_lead_edge_ci(
    *,
    per_event_lead_edges: Sequence[float],
    n_resamples: int = 1000,
    seed: int | None = None,
    confidence: float = 0.95,
) -> dict[str, float]:
    """Return {ci_low, point, ci_high} via percentile bootstrap on MEAN lead-edge.

    The cell statistic is a MEAN lead-edge (per-trade ¢-figure), NOT a sum.
    Resample the event set with replacement N times, compute the MEAN each
    time, and report:

    - point = observed mean of per_event_lead_edges (NOT mean of resamples).
    - ci_low / ci_high = lower/upper percentile of resampled means via
      nearest-rank convention: rank k = ceil(p × N), index = k - 1.

    `confidence` is the two-sided coverage probability (e.g., 0.95 for a
    standard 95% CI; `bonferroni_adjusted_ci` sets it to 1 - 0.05/n_tests).

    Edge case: empty input → all-zeros (no events ⇒ no estimate, no CI).
    """
    n = len(per_event_lead_edges)
    if n == 0:
        return {"ci_low": 0.0, "point": 0.0, "ci_high": 0.0}
    point = float(sum(per_event_lead_edges)) / n

    rng = random.Random(seed)
    values = list(per_event_lead_edges)
    resampled_means: list[float] = []
    for _ in range(n_resamples):
        total = 0.0
        for _ in range(n):
            total += values[rng.randrange(n)]
        resampled_means.append(total / n)
    resampled_means.sort()

    alpha = 1.0 - confidence
    p_lo = alpha / 2.0
    p_hi = 1.0 - alpha / 2.0
    lo_idx = max(0, math.ceil(p_lo * n_resamples) - 1)
    hi_idx = min(n_resamples - 1, math.ceil(p_hi * n_resamples) - 1)
    return {
        "ci_low": float(resampled_means[lo_idx]),
        "point": point,
        "ci_high": float(resampled_means[hi_idx]),
    }


def bonferroni_adjusted_ci(
    *,
    per_event_lead_edges: Sequence[float],
    n_resamples: int = 1000,
    seed: int | None = None,
    n_tests: int = BONFERRONI_N_TESTS_DEFAULT,
) -> dict[str, float]:
    """Return Bonferroni-adjusted CI via percentile-widening on the same bootstrap.

    Family-wise α = 0.05; per-test α = 0.05 / n_tests. The adjusted CI
    confidence is therefore 1 - 0.05/n_tests; for n_tests=24 that's
    ≈ 99.79%. Per plan-doc § Method, the adjusted CI is computed by
    re-percentiling the SAME bootstrap distribution at wider tails
    (NOT parametric scaling) — sharing the seed + n_resamples with
    `bootstrap_lead_edge_ci` ensures the underlying resampled means
    are identical and the only delta is the percentile selection.
    """
    alpha_family = 0.05
    alpha_per_test = alpha_family / n_tests
    confidence = 1.0 - alpha_per_test
    return bootstrap_lead_edge_ci(
        per_event_lead_edges=per_event_lead_edges,
        n_resamples=n_resamples,
        seed=seed,
        confidence=confidence,
    )


# ----- Verdict + program-level diagnostics --------------------------------


def _cell_clears(cell: Mapping[str, Any], threshold_cents: float) -> bool:
    """A cell clears the kill threshold iff status != insufficient AND
    its Bonferroni-adjusted lower-CI bound is > threshold."""
    if cell.get("status") == "insufficient":
        return False
    ci_low = cell.get("ci_low_bonferroni")
    if ci_low is None:
        return False
    return float(ci_low) > threshold_cents


def classify_verdict(
    *,
    per_cell_results: Mapping[Any, Mapping[str, Any]],
    threshold_cents: float = KILL_THRESHOLD_CENTS,
) -> str:
    """Return 'SURVIVE' if any cell clears the threshold; 'KILL' otherwise.

    A cell clears iff `ci_low_bonferroni > threshold_cents` AND
    `status != "insufficient"`.
    """
    for cell in per_cell_results.values():
        if _cell_clears(cell, threshold_cents):
            return "SURVIVE"
    return "KILL"


def survival_diagnostics(
    *,
    per_cell_results: Mapping[Any, Mapping[str, Any]],
    threshold_cents: float = KILL_THRESHOLD_CENTS,
) -> dict[str, Any]:
    """Return per-cell + program-level diagnostics for the verdict consumer.

    The umbrella program-level gate (`kb/decisions/ct-mdp-attack-alpha-program-plan.md`
    § Rules of engagement) is ≥2 of 3 falsifications survive their per-attack
    threshold. F0.4 survives on `n_cells_clearing_threshold ≥ 1` (one
    surviving laggard × regime cell with Bonferroni-adjusted CI excluding
    threshold). Diagnostics expose the cell list so downstream callers
    can weight a single-cell SURVIVE differently (e.g., for false-survive
    risk at small sample size).
    """
    clearing: list[Any] = []
    for key, cell in per_cell_results.items():
        if _cell_clears(cell, threshold_cents):
            clearing.append(key)
    return {
        "verdict": classify_verdict(
            per_cell_results=per_cell_results,
            threshold_cents=threshold_cents,
        ),
        "n_cells_clearing_threshold": len(clearing),
        "cells_clearing_threshold": clearing,
        "threshold_cents": threshold_cents,
    }


# ----- Internal helpers (DB I/O + σ-event detection) ---------------------


def _yes_mid_cents(row: Mapping[str, Any]) -> float | None:
    """Return the YES-side mid from a moc row, or None if either side is NULL."""
    yb, ya = row.get("yes_bid_cents"), row.get("yes_ask_cents")
    if yb is None or ya is None:
        return None
    return (float(yb) + float(ya)) / 2.0


def _classify_daynight(ts: dt.datetime) -> str:
    """US-equities-hours overlap window 14:00-22:00 UTC = day; else night.

    14:00-22:00 UTC envelope justification (per plan-doc § Method): cash
    equities open 14:30 UTC, close 21:00 UTC; the 14:00 / 22:00 ±30min
    envelope absorbs pre-market + after-hours bleed into crypto vol.
    Distinct from F0.1's 13:00-21:00 UTC band (F0.1 used the equities-
    hours center; F0.4 uses the wider overflow envelope).
    """
    return "day" if 14 <= ts.hour < 22 else "night"


def _load_spot_series(
    conn: sqlite3.Connection,
    asset: str,
    start_ts: str,
) -> list[tuple[dt.datetime, float]]:
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
    """Detect BTC σ-events on a spot time-series.

    Returns list of (t_C, log_return, sigma_at_t). sigma_at_t is the rolling
    realized vol of log-returns over the prior _VOL_WINDOW_SECONDS window.
    An event is any time index where |log_return / sigma_at_t| ≥ sigma_threshold.

    Trailing window with at least 5 prior returns required; bootstrap period
    pre-warm yields no events. Mirrors F0.1's _detect_sigma_events but is
    intentionally LEAD-SIDE-ONLY (F0.4 only computes σ on BTC, not on
    each laggard).
    """
    if len(series) < 2:
        return []
    log_returns: list[tuple[dt.datetime, float]] = []
    for i in range(1, len(series)):
        _, p_prev = series[i - 1]
        t_cur, p_cur = series[i]
        if p_prev <= 0 or p_cur <= 0:
            continue
        log_returns.append((t_cur, math.log(p_cur / p_prev)))

    events: list[tuple[dt.datetime, float, float]] = []
    for i, (t_cur, r_cur) in enumerate(log_returns):
        window_start = t_cur - dt.timedelta(seconds=_VOL_WINDOW_SECONDS)
        window_rets = [r for (t, r) in log_returns[:i] if t >= window_start]
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


def _load_moc_rows(
    conn: sqlite3.Connection,
    ticker_prefix: str,
    start_ts: str,
) -> list[dict[str, Any]]:
    """Load moc rows for a laggard's tickers in time order."""
    cur = conn.cursor()
    cur.execute(
        "SELECT ticker, observation_time, yes_bid_cents, yes_ask_cents, "
        "no_bid_cents, no_ask_cents "
        "FROM market_observations_continuous "
        "WHERE ticker LIKE ? AND observation_time >= ? "
        "ORDER BY observation_time",
        (ticker_prefix + "%", start_ts),
    )
    cols = ("ticker", "observation_time", "yes_bid_cents", "yes_ask_cents",
            "no_bid_cents", "no_ask_cents")
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def _prepare_indexed_rows(
    rows: Sequence[Mapping[str, Any]],
) -> tuple[list[dt.datetime], list[dict[str, Any]]]:
    """Pre-parse + pre-mid + sort moc rows once per ticker for O(log n) lookup.

    Returns parallel arrays (times, rows_with_mid) where rows_with_mid is
    augmented with `_obs_dt` + `_mid` and rows with NULL mid or unparseable
    timestamp are dropped.
    """
    augmented: list[dict[str, Any]] = []
    for row in rows:
        try:
            obs_dt = _parse_iso(row["observation_time"])
        except (ValueError, KeyError):
            continue
        mid = _yes_mid_cents(row)
        if mid is None:
            continue
        augmented.append({**row, "_obs_dt": obs_dt, "_mid": mid})
    augmented.sort(key=lambda r: r["_obs_dt"])
    times = [r["_obs_dt"] for r in augmented]
    return times, augmented


def _match_event_to_laggard_quotes(
    moc_by_ticker: Mapping[str, list[dict[str, Any]]],
    event_time: dt.datetime,
    delta_t_seconds: float,
    indexed_by_ticker: Mapping[str, tuple[list[dt.datetime], list[dict[str, Any]]]] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]] | None:
    """Find (M_stale, M_responded) pair for a laggard ticker at event_time + Δt.

    For each ticker, finds:
      - M_stale: latest row with obs_dt ≤ event_time + valid YES mid.
      - M_responded: latest row with event_time < obs_dt ≤ event_time + Δt
        + valid YES mid.

    Returns the (stale, responded) pair from whichever ticker has the
    SMALLEST stale-to-event gap (i.e., the "currently active" 15M market
    at event_time). Returns None if no ticker has both a pre-event AND
    in-window row.

    When `indexed_by_ticker` is provided (pre-parsed `_obs_dt`/`_mid` +
    sorted by time), uses bisect for O(log n) lookup per ticker. Falls
    back to the linear scan over `moc_by_ticker` when indexed is None
    (preserves the original API + behavior for backwards compatibility
    with any caller that hasn't pre-indexed; the linear path is unused
    by the main pipeline post-impl-Bit but kept for reference).
    """
    delta = dt.timedelta(seconds=delta_t_seconds)
    upper = event_time + delta
    best: tuple[float, dict[str, Any], dict[str, Any]] | None = None

    if indexed_by_ticker is not None:
        for _ticker, (times, augmented) in indexed_by_ticker.items():
            if not times:
                continue
            # bisect_right(times, event_time) = first index with time > event_time.
            # idx_stale = bisect_right - 1 is the last row with obs_dt ≤ event_time.
            idx_stale = bisect.bisect_right(times, event_time) - 1
            if idx_stale < 0:
                continue
            # Last in-window row: largest idx with time ≤ upper.
            idx_responded = bisect.bisect_right(times, upper) - 1
            if idx_responded <= idx_stale:
                continue
            stale = augmented[idx_stale]
            responded = augmented[idx_responded]
            gap_to_event = (event_time - stale["_obs_dt"]).total_seconds()
            if best is None or gap_to_event < best[0]:
                best = (gap_to_event, stale, responded)
    else:  # pragma: no cover — unreached from _per_laggard_analysis; kept for direct callers
        # NOTE: `_per_laggard_analysis` always passes `indexed_by_ticker`, so
        # this linear-scan fallback is not exercised by the main pipeline.
        # The branch itself IS reachable (any direct caller can omit the
        # indexed_by_ticker kwarg, e.g., REPL / debugging) and produces
        # semantically-identical output to the indexed path (verified at
        # impl-R1-N2). Distinct from "dead code": the branch has callers
        # outside the main pipeline; it just doesn't accrue coverage from
        # the test corpus which exclusively exercises the indexed path.
        for _ticker, rows in moc_by_ticker.items():
            stale: dict[str, Any] | None = None
            responded: dict[str, Any] | None = None
            for row in rows:
                try:
                    obs_dt = _parse_iso(row["observation_time"])
                except (ValueError, KeyError):
                    continue
                mid = _yes_mid_cents(row)
                if mid is None:
                    continue
                if obs_dt <= event_time:
                    if stale is None or obs_dt > stale["_obs_dt"]:
                        stale = {**row, "_obs_dt": obs_dt, "_mid": mid}
                elif obs_dt <= upper:
                    if responded is None or obs_dt > responded["_obs_dt"]:
                        responded = {**row, "_obs_dt": obs_dt, "_mid": mid}
                else:
                    break
            if stale is None or responded is None:
                continue
            gap_to_event = (event_time - stale["_obs_dt"]).total_seconds()
            if best is None or gap_to_event < best[0]:
                best = (gap_to_event, stale, responded)

    if best is None:
        return None
    return best[1], best[2]


def _per_laggard_analysis(
    *,
    conn: sqlite3.Connection,
    laggard: str,
    btc_events: Sequence[tuple[dt.datetime, float, float]],
    sigma_median: float,
    start_ts: str,
    delta_t_seconds: float,
    anti_fantasy_examples: list[dict[str, Any]] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Iterate BTC σ-events against laggard moc to produce per-event lead-edges.

    Returns (events, drop_counters). Each event has lead_edge_cents +
    regime + diagnostic metadata. drop_counters records why σ-events were
    NOT matched into laggard events:
      - `no_match`: no ticker has both M_stale AND M_responded for this event.
      - `anti_fantasy_skip`: |lead_edge| > MAX_PLAUSIBLE_EDGE_CENTS — skipped
        to avoid corrupting the cell estimate. Per plan-doc § Method, the
        clamp is a methodology-bug guard (e.g., cross-ticker contamination
        or settlement convergence); the verdict-doc surfaces the rate so
        outlier-dominated cells can be down-weighted by the consumer. The
        first few outlier event details (timestamps, ticker, mids, BTC
        return) accumulate in `anti_fantasy_examples` for RCA hand-off.
    """
    prefix = ASSET_TICKER_PREFIX[laggard]
    moc_rows = _load_moc_rows(conn, prefix, start_ts)
    moc_by_ticker: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in moc_rows:
        moc_by_ticker[r["ticker"]].append(r)
    # Pre-index for O(log n) bisect lookup per ticker.
    indexed_by_ticker = {t: _prepare_indexed_rows(rs) for t, rs in moc_by_ticker.items()}

    drop_counters: dict[str, int] = {"no_match": 0, "anti_fantasy_skip": 0}
    laggard_events: list[dict[str, Any]] = []
    for t_C, btc_ret, sigma_t in btc_events:
        pair = _match_event_to_laggard_quotes(
            moc_by_ticker,
            t_C,
            delta_t_seconds,
            indexed_by_ticker=indexed_by_ticker,
        )
        if pair is None:
            drop_counters["no_match"] += 1
            continue
        stale, responded = pair
        lead = compute_event_lead_edge(
            m_stale_cents=stale["_mid"],
            m_responded_cents=responded["_mid"],
            btc_return_at_event=btc_ret,
        )
        try:
            validate_lead_edge(lead_edge_cents=lead["lead_edge_cents"])
        except ValueError:
            drop_counters["anti_fantasy_skip"] += 1
            if anti_fantasy_examples is not None and len(anti_fantasy_examples) < 20:
                anti_fantasy_examples.append({
                    "laggard": laggard,
                    "event_time": t_C.isoformat(),
                    "lead_edge_cents": lead["lead_edge_cents"],
                    "raw_lead_edge_cents": lead["raw_lead_edge_cents"],
                    "ticker": stale.get("ticker"),
                    "stale_obs": stale.get("observation_time"),
                    "stale_yes_bid": stale.get("yes_bid_cents"),
                    "stale_yes_ask": stale.get("yes_ask_cents"),
                    "responded_obs": responded.get("observation_time"),
                    "responded_yes_bid": responded.get("yes_bid_cents"),
                    "responded_yes_ask": responded.get("yes_ask_cents"),
                    "btc_return": btc_ret,
                })
            continue
        vol_regime = "high" if sigma_t > sigma_median else "low"
        daynight = _classify_daynight(t_C)
        regime = f"vol_{vol_regime}_{daynight}"
        laggard_events.append({
            "lead_edge_cents": lead["lead_edge_cents"],
            "raw_lead_edge_cents": lead["raw_lead_edge_cents"],
            "regime": regime,
            "event_time": t_C.isoformat(),
            "btc_return": btc_ret,
            "sigma_at_event": sigma_t,
            "m_stale_cents": lead["m_stale_cents"],
            "m_responded_cents": lead["m_responded_cents"],
        })
    return laggard_events, drop_counters


# ----- Markdown formatter ------------------------------------------------


def _format_verdict_markdown(result: Mapping[str, Any]) -> str:
    """Render the verdict result dict as a markdown report body.

    Intentionally a SUBSET of the curated verdict-doc at
    `kb/findings/ct-mdp-f0-4-verdict.md` (mirrors F0.1's auto-gen vs
    curated-doc split). Auto-generated output is a numeric-table
    sanity-check artifact for ad-hoc re-runs; the curated finding doc
    is the source of truth for downstream decisions.
    """
    lines: list[str] = []
    lines.append(f"# F0.4 cross-asset lead-lag falsification — verdict\n")
    lines.append(f"**Verdict: {result['verdict']}** "
                 f"(threshold {result['threshold_cents']:.1f}¢/trade per cell, "
                 f"Bonferroni-adjusted across {result['bonferroni_n_tests']} cells)\n")
    lines.append(f"Window: {result['window_days']} days "
                 f"(start {result.get('window_start_ts', '?')})\n")
    lines.append(f"σ-threshold: {result['sigma_threshold']}, "
                 f"Δt: {result['delta_t_seconds']}s, "
                 f"bootstrap N: {result['bootstrap_n']}\n")
    lines.append(f"BTC σ-events detected: {result['n_btc_sigma_events']}\n")
    lines.append("\n## Per-cell (laggard × regime) lead-edge + Bonferroni-adjusted CI\n")
    lines.append("| Laggard | Regime | n_events | point | CI_low_95 | CI_high_95 | CI_low_bonf | CI_high_bonf | status |")
    lines.append("|---|---|---|---|---|---|---|---|---|")
    for (laggard, regime), cell in sorted(result["per_cell_results"].items()):
        status = cell.get("status", "ok")
        point = cell.get("point", 0.0)
        ci_low = cell.get("ci_low", float("nan"))
        ci_high = cell.get("ci_high", float("nan"))
        ci_low_b = cell.get("ci_low_bonferroni", float("nan"))
        ci_high_b = cell.get("ci_high_bonferroni", float("nan"))
        n_events = cell.get("n_events", 0)
        lines.append(
            f"| {laggard} | {regime} | {n_events} | {point:.3f} | "
            f"{ci_low:.3f} | {ci_high:.3f} | {ci_low_b:.3f} | {ci_high_b:.3f} | {status} |"
        )
    return "\n".join(lines) + "\n"


# ----- Main pipeline -----------------------------------------------------


def main(
    *,
    db_path: str,
    days: int = 5,
    sigma_threshold: float = 0.3,
    delta_t_seconds: float = 30.0,
    bootstrap_n: int = 1000,
    bonferroni_n_tests: int = BONFERRONI_N_TESTS_DEFAULT,
    output_path: str | None = None,
) -> dict[str, Any]:
    """End-to-end Phase 0 falsification on cross-asset lead-lag.

    Returns:
        {
            'verdict': 'KILL' | 'SURVIVE',
            'per_cell_results': {(laggard, regime): {n_events, point, status,
                                                   ci_low, ci_high,
                                                   ci_low_bonferroni, ci_high_bonferroni}, ...},
            'n_cells_clearing_threshold': int,
            'cells_clearing_threshold': list[(laggard, regime)],
            'threshold_cents': float,
            'window_days': int,
            'window_start_ts': str,
            'sigma_threshold': float,
            'delta_t_seconds': float,
            'bootstrap_n': int,
            'bonferroni_n_tests': int,
            'n_btc_sigma_events': int,
            'n_events_per_laggard': {'ETH': int, ...},
            'drop_counters_per_laggard': {'ETH': {'no_match': int}, ...},
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

        # 1. BTC spot series → σ-events (the LEAD signal).
        btc_series = _load_spot_series(conn, "BTC", window_start_ts)
        btc_events = _detect_sigma_events(btc_series, sigma_threshold)

        per_cell_results: dict[tuple[str, str], dict[str, Any]] = {}
        n_events_per_laggard: dict[str, int] = {}
        drop_counters_per_laggard: dict[str, dict[str, int]] = {}
        anti_fantasy_examples: list[dict[str, Any]] = []

        if not btc_events:
            # No σ-events ⇒ every cell is insufficient ⇒ KILL.
            for laggard in LAGGARD_ASSETS:
                n_events_per_laggard[laggard] = 0
                drop_counters_per_laggard[laggard] = {"no_match": 0, "anti_fantasy_skip": 0}
                for regime in REGIMES:
                    per_cell_results[(laggard, regime)] = {
                        "point": 0.0,
                        "n_events": 0,
                        "status": "insufficient",
                    }
            sigma_median = 0.0
        else:
            sigmas_sorted = sorted(s for _, _, s in btc_events)
            sigma_median = sigmas_sorted[len(sigmas_sorted) // 2]

            # 2. Per laggard: match each σ-event to (M_stale, M_responded)
            #    and compute regime-tagged lead-edge events.
            per_laggard_events: dict[str, list[dict[str, Any]]] = {}
            for laggard in LAGGARD_ASSETS:
                events, drops = _per_laggard_analysis(
                    conn=conn,
                    laggard=laggard,
                    btc_events=btc_events,
                    sigma_median=sigma_median,
                    start_ts=window_start_ts,
                    delta_t_seconds=delta_t_seconds,
                    anti_fantasy_examples=anti_fantasy_examples,
                )
                per_laggard_events[laggard] = events
                n_events_per_laggard[laggard] = len(events)
                drop_counters_per_laggard[laggard] = drops

            # 3. Per (laggard, regime) cell: aggregate + bootstrap CI + Bonferroni.
            for laggard in LAGGARD_ASSETS:
                cells = compute_lead_edge(
                    events=per_laggard_events[laggard],
                    laggard=laggard,
                    regime_conditioned=True,
                )
                by_regime: dict[str, list[float]] = defaultdict(list)
                for ev in per_laggard_events[laggard]:
                    by_regime[ev["regime"]].append(float(ev["lead_edge_cents"]))
                for regime, cell_info in cells.items():
                    cell: dict[str, Any] = {
                        "n_events": cell_info["n_events"],
                        "point": cell_info["point"],
                        "status": cell_info["status"],
                    }
                    if cell_info["status"] != "insufficient":
                        vals = by_regime.get(regime, [])
                        standard = bootstrap_lead_edge_ci(
                            per_event_lead_edges=vals,
                            n_resamples=bootstrap_n,
                            seed=42,
                            confidence=0.95,
                        )
                        bonferroni = bonferroni_adjusted_ci(
                            per_event_lead_edges=vals,
                            n_resamples=bootstrap_n,
                            seed=42,
                            n_tests=bonferroni_n_tests,
                        )
                        cell["ci_low"] = standard["ci_low"]
                        cell["ci_high"] = standard["ci_high"]
                        cell["ci_low_bonferroni"] = bonferroni["ci_low"]
                        cell["ci_high_bonferroni"] = bonferroni["ci_high"]
                    per_cell_results[(laggard, regime)] = cell

        diag = survival_diagnostics(
            per_cell_results=per_cell_results,
            threshold_cents=KILL_THRESHOLD_CENTS,
        )

        result: dict[str, Any] = {
            "verdict": diag["verdict"],
            "per_cell_results": per_cell_results,
            "n_cells_clearing_threshold": diag["n_cells_clearing_threshold"],
            "cells_clearing_threshold": diag["cells_clearing_threshold"],
            "threshold_cents": KILL_THRESHOLD_CENTS,
            "window_days": days,
            "window_start_ts": window_start_ts,
            "sigma_threshold": sigma_threshold,
            "delta_t_seconds": delta_t_seconds,
            "bootstrap_n": bootstrap_n,
            "bonferroni_n_tests": bonferroni_n_tests,
            "n_btc_sigma_events": len(btc_events),
            "n_events_per_laggard": n_events_per_laggard,
            "drop_counters_per_laggard": drop_counters_per_laggard,
            "sigma_median": sigma_median,
            "anti_fantasy_examples": anti_fantasy_examples if btc_events else [],
        }
    finally:
        conn.close()

    if output_path:
        with open(output_path, "w", encoding="utf-8") as fh:
            fh.write(_format_verdict_markdown(result))
    return result


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="F0.4 cross-asset lead-lag falsification")
    parser.add_argument("--db", default="state.db", help="Path to state.db (default: ./state.db)")
    parser.add_argument("--days", type=int, default=5, help="Analysis window in days")
    parser.add_argument("--sigma-threshold", type=float, default=0.3,
                        help="σ-move threshold for BTC event detection")
    parser.add_argument("--delta-t-seconds", type=float, default=30.0,
                        help="Lead-lag detection window in seconds")
    parser.add_argument("--bootstrap-n", type=int, default=1000,
                        help="Bootstrap resample count")
    parser.add_argument("--bonferroni-n-tests", type=int, default=BONFERRONI_N_TESTS_DEFAULT,
                        help="Family-wise n_tests for Bonferroni correction")
    parser.add_argument("--out", default=None,
                        help="Output markdown path (default: stdout)")
    return parser.parse_args(argv)


if __name__ == "__main__":  # pragma: no cover
    args = _parse_args()
    result = main(
        db_path=args.db,
        days=args.days,
        sigma_threshold=args.sigma_threshold,
        delta_t_seconds=args.delta_t_seconds,
        bootstrap_n=args.bootstrap_n,
        bonferroni_n_tests=args.bonferroni_n_tests,
        output_path=args.out,
    )
    print(result)
