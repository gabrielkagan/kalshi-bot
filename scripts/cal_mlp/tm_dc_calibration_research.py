"""TM/DC conditional band-calibration research — Spike 86b9zktp6.

Read-only, Mac-side research script. ZERO production wire-in.

Does TM/DC SHADOW & LIVE history support replacing the bespoke margin/tier
sizing formulas with an empirical-realized-rate Kelly substitution
conditioned on (asset, band, sub_signal)? This script answers the question
offline. The findings doc (`kb/findings/tm-dc-conditional-calibration-research.md`)
emits SHIP / NO-SHIP / SHIP-WITH-CAVEATS recommendation.

Hypothesis + research questions: see
`kb/decisions/tm-dc-conditional-calibration-spike-plan-may17.md`.

Locked design parameters (also pinned by
`tests/contracts/test_tm_dc_calibration_research.py`):

  - YES-side only (mirrors P4.1)
  - Lookback hybrid: 30d (bands 70-93c) / 60d (bands 94-100c)
  - Shrinkage k sweep: {30, 50, 100}
  - Fractional Kelly sweep: {0.25, 0.5, 1.0}
  - Per-cell N floor: 10 (below floor, fall back to (asset, band) aggregate
    — and the band aggregate itself is floor-gated; falls through to 0.5
    neutral when BOTH are sub-floor, see `lookup_calibrated_rate`)
  - Asset universe: {BTC, ETH, SOL, XRP, HYPE, DOGE}
  - TM sub-signal: absolute buf_pct buckets [0.0, 0.1, 0.3, 0.6, 1.0, ∞]
  - DC sub-signal: existing tier labels {T1, T1B, T2, T2_Z25, T2_Z2}

Discipline:
  - Read-only sqlite (URI `mode=ro`)
  - `PRAGMA journal_mode=WAL` + `PRAGMA busy_timeout=10000` per
    `bot/CLAUDE.md` SQLite section
  - Net PnL via `SUM(pnl_cents - COALESCE(fee_cents, 0))` per `scripts/CLAUDE.md`
    (gross/net trap — postmortem `kb/failures/audit-pnl-fee-omission-apr29.md`).
    Per-contract scaling: real per-contract net derived from `settled_trades`
    (`(pnl_cents - fee_cents) / count`) when available; synthetic fallback
    via `market_result + price - Kalshi-fee-schedule` for rows without
    a settled_trades match (un-fired evaluation rows).
  - Cohort UNION on `filter_stage` per `bot/CLAUDE.md` cell-block section
    (canonical 5-set in `bot.helpers.cohort_attribution.COHORT_PARTITION_STAGES`)
  - Mac-side only per `feedback_vps_compute_isolation` — VPS cannot host sustained compute

Idempotency caveat (per R1 M5 / R2 M2): the script ONLY writes to the
`--out` sidecar path (default `kb/findings/tm-dc-conditional-calibration-research.data.md`).
It NEVER overwrites the hand-authored prose findings doc
(`kb/findings/tm-dc-conditional-calibration-research.md`). Re-runs refresh
the data appendix safely; the prose stays human-authored.

Sister anchors:
  - bot/helpers/band_calibration.py (P4.1 helper — band geometry + shrinkage shape)
  - scripts/cal_mlp/sim_pnl.py (`_tm_size` / `_dc_size` / `_strategy_size` — read-only baseline for replay A/B)
  - tests/contracts/test_tm_dc_calibration_research.py (contract pins)

Usage:
    python scripts/cal_mlp/tm_dc_calibration_research.py \\
        --db /path/to/state.db \\
        --strategy {tm,dc,both} \\
        --out kb/findings/tm-dc-conditional-calibration-research.md
"""
from __future__ import annotations

import argparse
import json
import logging
import math
import sqlite3
import statistics
import sys
import time
from collections import defaultdict
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

# Locate canonical 6-band geometry from P4.1 helper. Read-only import.
_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from bot.helpers.band_calibration import BAND_BOUNDS as _P4_1_BAND_BOUNDS  # noqa: E402

# Cohort UNION anchor — see `bot/CLAUDE.md` Cell-block section.
try:
    from bot.helpers.cohort_attribution import COHORT_PARTITION_STAGES
except Exception:  # pragma: no cover - resilience for partial checkouts
    COHORT_PARTITION_STAGES = (
        "candidate",
        "96C_SOL_XRP_STC_DANGER_BAND",
        "TM98_97_98C_2_5MIN_BLEED",
        "SOL_TAKER_85_89C_2_5MIN_BLEED",
        "SOL_BLEED_V2_88_93C_2_5MIN",
    )


log = logging.getLogger("tm_dc_calibration_research")


# ───────────────────────────────────────────────────────────────────────────
# LOCKED design parameters (pinned by contract test). Drift caught at CI.
# ───────────────────────────────────────────────────────────────────────────

# Synthetic bankroll used for apples-to-apples A/B sizing comparison.
# Both baseline (`_tm_size`/`_dc_size`) and proposed (Kelly via calibrated
# prob) consume this.
SYNTHETIC_BANKROLL_CENTS: int = 500_000  # = $5,000

SHRINKAGE_K_SWEEP: Tuple[int, ...] = (30, 50, 100)
FRACTIONAL_KELLY_SWEEP: Tuple[float, ...] = (0.25, 0.5, 1.0)
PER_CELL_N_FLOOR: int = 10
TM_BUF_BUCKETS: Tuple[float, ...] = (0.0, 0.1, 0.3, 0.6, 1.0, float("inf"))
DC_TIER_LABELS: Tuple[str, ...] = ("T1", "T1B", "T2", "T2_Z25", "T2_Z2")
ASSET_UNIVERSE: frozenset = frozenset({"BTC", "ETH", "SOL", "XRP", "HYPE", "DOGE"})
BAND_BOUNDS: Tuple[Tuple[str, int, int], ...] = tuple(
    (label, lo, hi) for label, lo, hi in _P4_1_BAND_BOUNDS
)
BAND_WINDOW_DAYS: Dict[str, int] = {
    "70-79": 30, "80-85": 30, "86-89": 30, "90-93": 30,
    "94-96": 60, "97-98": 60, "99": 60,
}

# Production DC strategy-name → spike tier-label mapping. Production writes
# `decided_t1`, `decided_t1b`, etc. (lowercase, underscore). The spike's
# matrix display uses the canonical uppercase form for human readability.
_DC_STRATEGY_TO_TIER: Dict[str, str] = {
    "decided_t1":     "T1",
    "decided_t1b":    "T1B",
    "decided_t2":     "T2",
    "decided_t2_z25": "T2_Z25",
    "decided_t2_z2":  "T2_Z2",
}

# NULL-HYPOTHESIS threshold per spike plan §"Phase 4":
# "if proposed sizing == current sizing within ±5% across all metrics,
# recommend NO-SHIP". `apply_null_hypothesis_check` consumes this.
NULL_HYPOTHESIS_DELTA_THRESHOLD: float = 0.05


# ───────────────────────────────────────────────────────────────────────────
# Pure functions — shrinkage + classification + fees
# ───────────────────────────────────────────────────────────────────────────


def shrink_cell(n: int, raw_p: float, prior: float, k: int) -> float:
    """Hierarchical shrinkage toward a prior — mirrors P4.1 `_shrink`.

    shrunk = (n * raw_p + k * prior) / (n + k)

    For k=30: a cell with n=300 keeps 91% of its raw signal; n=30 splits
    50/50 with the prior; n=10 is 75% prior, 25% cell. Cells with n<=0
    return the prior directly (no division by zero).
    """
    if n <= 0:
        return prior
    return (n * raw_p + k * prior) / (n + k)


def lookup_calibrated_rate(
    matrix: Dict[Tuple[str, str, str], Dict[str, float]],
    band_aggregate: Dict[Tuple[str, str], Dict[str, float]],
    asset: str,
    band: str,
    sub_signal: str,
    *,
    k: int = 30,
) -> float:
    """Look up the calibrated rate for `(asset, band, sub_signal)` at shrinkage k.

    Floor cascade (per R1-Mn1 fix):
      1. cell with n >= floor → use cell's shrunk_p (at k)
      2. else band aggregate with n >= floor → use band_aggregate's shrunk_p (at k)
      3. else 0.5 neutral fallback
    """
    cell = matrix.get((asset, band, sub_signal))
    if cell is not None and cell.get("n", 0) >= PER_CELL_N_FLOOR:
        by_k = cell.get("shrunk_p_by_k", {})
        return float(by_k.get(k, cell.get("shrunk_p", 0.5)))
    agg = band_aggregate.get((asset, band))
    if agg is not None and agg.get("n", 0) >= PER_CELL_N_FLOOR:
        by_k = agg.get("shrunk_p_by_k", {})
        return float(by_k.get(k, agg.get("shrunk_p", 0.5)))
    return 0.5


def classify_band(price_cents: int) -> Optional[str]:
    """Return band label for `price_cents`, or None if outside 70-100c."""
    for label, lo, hi in BAND_BOUNDS:
        if lo <= price_cents <= hi:
            return label
    return None


def classify_tm_buf_bucket(buf_pct: Optional[float]) -> Optional[str]:
    """Bucket `buf_pct` into a TM_BUF_BUCKETS label.

    Returns `"bucket_<i>"` where i is the bucket index (0-indexed). When
    `buf_pct is None` (missing data — `spot_price`/`threshold` NULL), returns
    `None` so callers can propagate "missing" through to the baseline sizer
    rather than synthesizing 0.0 (which would force-fire the thin-buffer
    cap — see R1-C2 / R1-Mn2).
    """
    if buf_pct is None:
        return None
    for i in range(len(TM_BUF_BUCKETS) - 1):
        if TM_BUF_BUCKETS[i] <= buf_pct < TM_BUF_BUCKETS[i + 1]:
            return f"bucket_{i}"
    return f"bucket_{len(TM_BUF_BUCKETS) - 2}"  # cap to last bucket


def map_dc_strategy_to_tier(strategy: str) -> Optional[str]:
    """Map production DC strategy name → canonical tier label."""
    return _DC_STRATEGY_TO_TIER.get(strategy)


# Per R1-C3 / R2-C2 — Kalshi standard taker fee schedule is approximately
# `7 × P × (1-P)` cents per contract, where P is the entry price as a
# probability (price / 100). Fees apply to ALL filled contracts (winners
# AND losers) at trade entry — verified empirically on `settled_trades`
# for TM (60d): losers' per-contract fee 0.220c vs winners' 0.172c; both
# in the 0.07-0.22c band consistent with the 7×P×(1-P) formula plus
# tier multipliers. The R1 fix mis-applied the fee to winners only;
# this round corrects to both sides.
def kalshi_fee_cents_per_contract(price_cents: int) -> float:
    """Approximate Kalshi fee per filled contract at `price_cents`.

    Formula: `7 × P × (1-P)` cents where P = price_cents / 100. Applied
    to BOTH winning and losing contracts (fees charged at entry, deducted
    from gross PnL on settlement regardless of outcome). Empirical TM
    per-contract fees (60d snapshot): losers 0.220c, winners 0.172c —
    both within ±30% of the published formula, which understates by a
    multiplier presumably owing to tier-specific schedule fragments not
    modeled here.

    Direction-of-effect reliable; absolute magnitude may differ ±30%
    against the live Kalshi schedule. For exact net-PnL grounding the
    spike prefers `settled_trades.fee_cents` over this approximation
    (see `_per_contract_net_cents`).
    """
    if price_cents is None or price_cents <= 0 or price_cents >= 100:
        return 0.0
    p = price_cents / 100.0
    return 7.0 * p * (1.0 - p)


# ───────────────────────────────────────────────────────────────────────────
# DB access — read-only
# ───────────────────────────────────────────────────────────────────────────


@contextmanager
def open_read_only_db(db_path: str):
    """Read-only sqlite connection with WAL + busy_timeout.

    Per `bot/CLAUDE.md` SQLite section: `PRAGMA journal_mode=WAL` +
    `PRAGMA busy_timeout=10000`. Plus `mode=ro` URI form and defense-in-
    depth `PRAGMA query_only=1`.
    """
    uri = f"file:{db_path}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    try:
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA busy_timeout=10000;")
        conn.execute("PRAGMA query_only = 1;")
        conn.row_factory = sqlite3.Row
        yield conn
    finally:
        conn.close()


# ───────────────────────────────────────────────────────────────────────────
# Data pulls — TM + DC eligibility populations
# ───────────────────────────────────────────────────────────────────────────


def _cohort_stages_sql_placeholders(stages: Iterable[str]) -> Tuple[str, List[str]]:
    """Build `(?, ?, ...)` placeholders + values list for an IN-clause."""
    stages_list = list(stages)
    placeholders = ", ".join("?" for _ in stages_list)
    return placeholders, stages_list


def pull_tm_eligible_signals(
    conn: sqlite3.Connection,
    *,
    lookback_days: int,
) -> List[sqlite3.Row]:
    """TM-eligible signals — both bare `terminal_momentum` and the suffixed
    `terminal_momentum_{N}` variants (per R1-C1: bare form is also production-real
    with 916 rows in 60d; verified vs `settled_trades` which has 276 bare rows).

    Cohort UNION ensures cell-block stages aren't excluded. Pulls
    `settled_trades.count` so per-contract net PnL can be derived
    (`(pnl_cents - fee_cents) / count`) for rows where TM actually fired.
    """
    placeholders, stages = _cohort_stages_sql_placeholders(COHORT_PARTITION_STAGES)
    # NOTE: `buf_pct` is NOT persisted in evaluated_opportunities; derive it
    # from `spot_price` + `threshold`. NULL propagates when threshold≤0 or
    # spot_price is NULL — callers MUST handle None (see R1-C2 fix).
    sql = rf"""
        SELECT
            eo.ticker        AS ticker,
            eo.asset         AS asset,
            eo.market_price  AS market_price,
            CASE
                WHEN eo.threshold > 0 AND eo.spot_price IS NOT NULL
                THEN (eo.spot_price - eo.threshold) / eo.threshold * 100.0
                ELSE NULL
            END               AS buf_pct,
            eo.seconds_to_close AS seconds_to_close,
            eo.strategy      AS strategy,
            fss.market_result AS market_result,
            st.pnl_cents     AS pnl_cents,
            COALESCE(st.fee_cents, 0) AS fee_cents,
            st.count         AS settled_count
        FROM evaluated_opportunities eo
        LEFT JOIN fifteenm_shadow_signals fss
            ON fss.ticker = eo.ticker
        LEFT JOIN settled_trades st
            ON st.ticker = eo.ticker AND st.strategy = eo.strategy
        WHERE (eo.strategy = 'terminal_momentum'
               -- R2-Mn1: escape underscore to prevent LIKE wildcard matching
               -- arbitrary suffixes (e.g. `terminal_momentumX...`).
               OR eo.strategy LIKE 'terminal\_momentum\_%' ESCAPE '\')
          AND eo.filter_stage IN ({placeholders})
          AND eo.evaluation_time >= datetime('now', ?)
        -- R2-Mn5: deterministic ordering for drawdown computation.
        ORDER BY eo.evaluation_time
    """
    args = [*stages, f"-{int(lookback_days)} days"]
    return list(conn.execute(sql, args).fetchall())


def pull_dc_eligible_signals(
    conn: sqlite3.Connection,
    *,
    lookback_days: int,
) -> List[sqlite3.Row]:
    """DC-eligible signals from `evaluated_opportunities` joined to settlement."""
    placeholders, stages = _cohort_stages_sql_placeholders(COHORT_PARTITION_STAGES)
    sql = f"""
        SELECT
            eo.ticker        AS ticker,
            eo.asset         AS asset,
            eo.market_price  AS market_price,
            eo.z_score       AS z_score,
            eo.strategy      AS strategy,
            fss.market_result AS market_result,
            st.pnl_cents     AS pnl_cents,
            COALESCE(st.fee_cents, 0) AS fee_cents,
            st.count         AS settled_count
        FROM evaluated_opportunities eo
        LEFT JOIN fifteenm_shadow_signals fss
            ON fss.ticker = eo.ticker
        LEFT JOIN settled_trades st
            ON st.ticker = eo.ticker AND st.strategy = eo.strategy
        WHERE eo.strategy IN ('decided_t1', 'decided_t1b', 'decided_t2',
                              'decided_t2_z25', 'decided_t2_z2')
          AND eo.filter_stage IN ({placeholders})
          AND eo.evaluation_time >= datetime('now', ?)
        -- R2-Mn5: deterministic ordering for drawdown computation.
        ORDER BY eo.evaluation_time
    """
    args = [*stages, f"-{int(lookback_days)} days"]
    return list(conn.execute(sql, args).fetchall())


# ───────────────────────────────────────────────────────────────────────────
# Matrix builds — (asset, band, sub_signal) realized rates with shrinkage
# ───────────────────────────────────────────────────────────────────────────


def _band_aggregate_priors(
    rows: Iterable[sqlite3.Row],
    *,
    classify_band_fn=classify_band,
) -> Dict[str, float]:
    """Band-aggregate realized rate, pooled across all assets per band."""
    bucket: Dict[str, List[int]] = defaultdict(list)
    for row in rows:
        price = row["market_price"]
        if price is None:
            continue
        band = classify_band_fn(int(price))
        if band is None:
            continue
        result = row["market_result"]
        if result is None:
            continue
        bucket[band].append(1 if str(result) == "yes" else 0)
    priors: Dict[str, float] = {}
    for band, hits in bucket.items():
        if not hits:
            continue
        priors[band] = sum(hits) / len(hits)
    return priors


def _build_matrix_generic(
    rows: Iterable[sqlite3.Row],
    *,
    sub_signal_fn: Callable[[sqlite3.Row], Optional[str]],
    k_sweep: Tuple[int, ...] = SHRINKAGE_K_SWEEP,
) -> Dict[str, Any]:
    """Build (asset, band, sub_signal) and (asset, band) shrunk matrices."""
    rows_list = list(rows)
    priors = _band_aggregate_priors(rows_list)

    raw_cells: Dict[Tuple[str, str, str], List[int]] = defaultdict(list)
    raw_band_agg: Dict[Tuple[str, str], List[int]] = defaultdict(list)

    for row in rows_list:
        price = row["market_price"]
        asset = row["asset"]
        if price is None or asset not in ASSET_UNIVERSE:
            continue
        band = classify_band(int(price))
        if band is None:
            continue
        result = row["market_result"]
        if result is None:
            continue
        outcome = 1 if str(result) == "yes" else 0
        sub = sub_signal_fn(row)
        if sub is not None:
            raw_cells[(asset, band, sub)].append(outcome)
        raw_band_agg[(asset, band)].append(outcome)

    def _shrunk_pack(n: int, raw_p: float, prior: float) -> Dict[str, Any]:
        shrunk = {k: shrink_cell(n, raw_p, prior, k) for k in k_sweep}
        return {
            "n": n,
            "raw_p": raw_p,
            "shrunk_p": shrunk[k_sweep[0]],
            "shrunk_p_by_k": shrunk,
            "sub_floor": n < PER_CELL_N_FLOOR,
        }

    cells = {
        key: _shrunk_pack(len(hits), sum(hits) / len(hits), priors.get(key[1], 0.5))
        for key, hits in raw_cells.items() if hits
    }
    band_agg = {
        key: _shrunk_pack(len(hits), sum(hits) / len(hits), priors.get(key[1], 0.5))
        for key, hits in raw_band_agg.items() if hits
    }
    return {"cells": cells, "band_aggregate": band_agg, "band_priors": priors}


def _tm_sub_signal(row: sqlite3.Row) -> Optional[str]:
    buf_pct = row["buf_pct"]
    try:
        buf = float(buf_pct) if buf_pct is not None else None
    except (TypeError, ValueError):
        buf = None
    return classify_tm_buf_bucket(buf)


def _dc_sub_signal(row: sqlite3.Row) -> Optional[str]:
    strategy = row["strategy"]
    return map_dc_strategy_to_tier(str(strategy)) if strategy else None


def build_tm_matrix(rows, *, k_sweep=SHRINKAGE_K_SWEEP):
    return _build_matrix_generic(rows, sub_signal_fn=_tm_sub_signal, k_sweep=k_sweep)


def build_dc_matrix(rows, *, k_sweep=SHRINKAGE_K_SWEEP):
    return _build_matrix_generic(rows, sub_signal_fn=_dc_sub_signal, k_sweep=k_sweep)


# ───────────────────────────────────────────────────────────────────────────
# Sim PnL replay — A/B current vs proposed sizing (sweep)
# ───────────────────────────────────────────────────────────────────────────


def kelly_contracts(
    bankroll_cents: int,
    p_win: float,
    price_cents: int,
    fractional: float = 1.0,
) -> int:
    """Standard Kelly contract count.

    f* = (b*p - q) / b where b = (100-price)/price, p = p_win, q = 1-p.
    Negative-edge → 0. (Per R1: f* hits 0.5 at p=0.99 ask=98c — explosive.)
    """
    if price_cents <= 0 or price_cents >= 100 or bankroll_cents <= 0:
        return 0
    q = 1.0 - p_win
    b = (100 - price_cents) / price_cents
    if b <= 0:
        return 0
    edge = b * p_win - q
    if edge <= 0:
        return 0
    f_star = edge / b
    return max(0, int(bankroll_cents * fractional * f_star / price_cents))


def _per_contract_net_cents(row: sqlite3.Row) -> Optional[float]:
    """Per-contract net PnL for a row.

    R1-M1 fix: prefer REAL `settled_trades.pnl_cents / count` when available
    (LIVE TM rows that actually fired), and fall back to SYNTHETIC
    `(100 - price - fee) if yes else (-price)` for rows without a
    `settled_trades` match (un-fired TM/DC evaluation rows). NOTE: DC is
    LIVE for 4 of 5 tiers (T1/T1B/T2/T2_Z25 = `DECIDED_T*_ENABLED=1`); only
    T2_Z2 is shadow. The `DECIDED_CONTRACT_SHADOW=1` constant default gates
    shadow-LOGGING paths in scanner, NOT live trading.

    Returns None when the row lacks both real PnL and a known outcome.
    """
    pnl = row["pnl_cents"] if "pnl_cents" in row.keys() else None
    fee = row["fee_cents"] if "fee_cents" in row.keys() else None
    count = row["settled_count"] if "settled_count" in row.keys() else None
    if pnl is not None and count is not None and int(count) > 0:
        # Real per-contract net from settled_trades.
        return (float(pnl) - float(fee or 0)) / float(count)

    # Synthetic fallback: revenue - cost - fee (R2-C2: fee on BOTH sides).
    market_result = row["market_result"] if "market_result" in row.keys() else None
    price = row["market_price"] if "market_price" in row.keys() else None
    if market_result is None or price is None:
        return None
    p = int(price)
    fee = kalshi_fee_cents_per_contract(p)
    if str(market_result) == "yes":
        return float(100 - p) - fee
    return -float(p) - fee


def _summary_stats(xs: List[int]) -> Dict[str, float]:
    """Compact distribution stats."""
    if not xs:
        return {"n": 0, "mean": 0.0, "median": 0.0, "p90": 0.0, "max": 0, "zeros": 0}
    s = sorted(xs)
    n = len(s)
    return {
        "n": n,
        "mean": sum(s) / n,
        "median": float(s[n // 2]),
        "p90": float(s[min(n - 1, int(n * 0.9))]),
        "max": int(s[-1]),
        "zeros": sum(1 for x in s if x == 0),
    }


def _drawdown_cents(daily_pnls: List[float]) -> float:
    """Max drawdown across a cumulative-PnL sequence."""
    if not daily_pnls:
        return 0.0
    cum = 0.0
    peak = 0.0
    max_dd = 0.0
    for p in daily_pnls:
        cum += p
        if cum > peak:
            peak = cum
        if peak - cum > max_dd:
            max_dd = peak - cum
    return max_dd


def replay_pnl_a_b(
    rows: Iterable[sqlite3.Row],
    *,
    baseline_sizer: Callable[[sqlite3.Row], int],
    proposed_sizer: Callable[[sqlite3.Row], int],
) -> Dict[str, Any]:
    """A/B contract counts + per-row net PnL via `_per_contract_net_cents`
    (real settled_trades when available, synthetic fee-schedule fallback otherwise).

    R1-M3 adds: max drawdown, Sharpe-ish, avg loss / avg win, win/loss split.

    R2-C3 fix — `evaluated_opportunities` may contain multiple rows for the
    same (ticker, strategy) pair (different evaluation snapshots of the same
    decision opportunity). The same `settled_trades` row gets JOINed to each
    duplicate, multiplying realized PnL. This implementation de-duplicates
    realized PnL attribution: only the FIRST (chronological — relies on
    SQL ORDER BY evaluation_time) EO row per (ticker, strategy) pair credits
    PnL. All EO rows still contribute to the contract-distribution statistics
    (each is a distinct sizing decision).
    """
    rows_list = list(rows)
    base_counts: List[int] = []
    prop_counts: List[int] = []
    base_pnls: List[float] = []
    prop_pnls: List[float] = []
    rows_with_outcome = 0
    rows_using_real_pnl = 0
    unique_pnl_pairs = 0
    base_exception_count = 0
    prop_exception_count = 0
    seen_pairs: set = set()

    for row in rows_list:
        try:
            base_ct = int(baseline_sizer(row))
        except Exception as exc:
            log.debug("baseline_sizer raised: %s", exc)
            base_ct = 0
            base_exception_count += 1
        try:
            prop_ct = int(proposed_sizer(row))
        except Exception as exc:
            log.debug("proposed_sizer raised: %s", exc)
            prop_ct = 0
            prop_exception_count += 1
        base_counts.append(base_ct)
        prop_counts.append(prop_ct)

        per_ct = _per_contract_net_cents(row)
        if per_ct is None:
            continue
        rows_with_outcome += 1
        # R2-C3 dedup: only first EO row per (ticker, strategy) credits PnL.
        ticker = row["ticker"] if "ticker" in row.keys() else None
        strategy = row["strategy"] if "strategy" in row.keys() else None
        pair_key = (ticker, strategy)
        if pair_key in seen_pairs:
            continue
        seen_pairs.add(pair_key)
        unique_pnl_pairs += 1
        pnl = row["pnl_cents"] if "pnl_cents" in row.keys() else None
        if pnl is not None:
            rows_using_real_pnl += 1
        base_pnls.append(per_ct * base_ct)
        prop_pnls.append(per_ct * prop_ct)

    base_total = sum(base_pnls)
    prop_total = sum(prop_pnls)
    base_wins_pnl = [x for x in base_pnls if x > 0]
    base_losses_pnl = [x for x in base_pnls if x < 0]
    prop_wins_pnl = [x for x in prop_pnls if x > 0]
    prop_losses_pnl = [x for x in prop_pnls if x < 0]

    def _stdev_safe(xs: List[float]) -> float:
        if len(xs) < 2:
            return 0.0
        return statistics.pstdev(xs)

    def _sharpe(xs: List[float]) -> float:
        s = _stdev_safe(xs)
        if s == 0.0:
            return 0.0
        return (sum(xs) / len(xs)) / s if xs else 0.0

    return {
        "n_rows": len(rows_list),
        "n_rows_with_outcome": rows_with_outcome,
        "n_unique_pnl_pairs": unique_pnl_pairs,
        "n_rows_using_real_pnl": rows_using_real_pnl,
        "baseline_total_net_pnl_cents": int(base_total),
        "proposed_total_net_pnl_cents": int(prop_total),
        "baseline_max_drawdown_cents": int(_drawdown_cents(base_pnls)),
        "proposed_max_drawdown_cents": int(_drawdown_cents(prop_pnls)),
        "baseline_sharpe_ish": _sharpe(base_pnls),
        "proposed_sharpe_ish": _sharpe(prop_pnls),
        "baseline_avg_win_cents": (sum(base_wins_pnl) / len(base_wins_pnl)) if base_wins_pnl else 0.0,
        "baseline_avg_loss_cents": (sum(base_losses_pnl) / len(base_losses_pnl)) if base_losses_pnl else 0.0,
        "proposed_avg_win_cents": (sum(prop_wins_pnl) / len(prop_wins_pnl)) if prop_wins_pnl else 0.0,
        "proposed_avg_loss_cents": (sum(prop_losses_pnl) / len(prop_losses_pnl)) if prop_losses_pnl else 0.0,
        "baseline_n_wins_rows": len(base_wins_pnl),
        "baseline_n_loss_rows": len(base_losses_pnl),
        "proposed_n_wins_rows": len(prop_wins_pnl),
        "proposed_n_loss_rows": len(prop_losses_pnl),
        "baseline_contract_dist": _summary_stats(base_counts),
        "proposed_contract_dist": _summary_stats(prop_counts),
        "baseline_exception_count": base_exception_count,
        "proposed_exception_count": prop_exception_count,
    }


def _delta_pct(baseline: float, proposed: float) -> float:
    if baseline == 0.0:
        return float("inf") if proposed != 0.0 else 0.0
    return (proposed - baseline) / abs(baseline)


def apply_null_hypothesis_check(ab: Dict[str, Any]) -> Dict[str, Any]:
    """±5% NULL-hypothesis check per spike plan §"Phase 4".

    If `|proposed - baseline| / |baseline| <= 0.05` across (total PnL, max
    drawdown, contract mean), recommend NO-SHIP. Otherwise the verdict is
    surfaced to a human for synthesis (the threshold is necessary, not
    sufficient — verdict depends on direction of effect + risk).
    """
    metrics = {
        "total_pnl": _delta_pct(
            ab["baseline_total_net_pnl_cents"], ab["proposed_total_net_pnl_cents"]
        ),
        "max_drawdown": _delta_pct(
            ab["baseline_max_drawdown_cents"], ab["proposed_max_drawdown_cents"]
        ),
        "contract_mean": _delta_pct(
            ab["baseline_contract_dist"]["mean"],
            ab["proposed_contract_dist"]["mean"],
        ),
    }
    all_within = all(
        abs(d) <= NULL_HYPOTHESIS_DELTA_THRESHOLD for d in metrics.values()
    )
    return {
        "metric_deltas_fractional": metrics,
        "all_within_threshold": all_within,
        "verdict_if_only_threshold_were_used": "NO-SHIP" if all_within else "SHIP-OR-SHIP-WITH-CAVEATS",
        "threshold": NULL_HYPOTHESIS_DELTA_THRESHOLD,
    }


# ───────────────────────────────────────────────────────────────────────────
# Findings-doc emitter — DATA APPENDIX ONLY (per R1-M5 idempotency caveat,
# the prose sections of the findings doc are hand-authored and live alongside
# the auto-emitted data tables; re-running the script regenerates the data
# block but does not modify the prose sections IF the operator uses
# `--data-only`).
# ───────────────────────────────────────────────────────────────────────────


def _format_matrix_table(matrix: Dict[str, Any], heading: str) -> List[str]:
    lines: List[str] = [f"### {heading}", ""]
    lines.append("| asset | band | sub_signal | n | raw_p | shrunk_p (k=30) | shrunk_p (k=50) | shrunk_p (k=100) | sub_floor |")
    lines.append("|---|---|---|---:|---:|---:|---:|---:|:--:|")
    for (asset, band, sub), cell in sorted(matrix["cells"].items()):
        flag = "*sub-floor*" if cell.get("sub_floor") else ""
        lines.append(
            f"| {asset} | {band} | {sub} | {cell['n']} | "
            f"{cell['raw_p']:.4f} | "
            f"{cell['shrunk_p_by_k'][30]:.4f} | "
            f"{cell['shrunk_p_by_k'][50]:.4f} | "
            f"{cell['shrunk_p_by_k'][100]:.4f} | {flag} |"
        )
    lines.append("")
    return lines


def format_findings_data_block(
    tm_matrix: Optional[Dict[str, Any]],
    dc_matrix: Optional[Dict[str, Any]],
    sweep_results: Dict[str, Any],
    *,
    db_path: str,
    snapshot_ts: str,
    lookback_days: int,
    n_tm_rows: int,
    n_dc_rows: int,
) -> str:
    lines: List[str] = []
    lines.append(f"<!-- auto-generated data block — snapshot {snapshot_ts} -->")
    lines.append("")
    lines.append("## Data appendix (auto-generated)")
    lines.append("")
    lines.append(f"- **Snapshot**: {snapshot_ts}")
    lines.append(f"- **DB**: `{db_path}`")
    lines.append(f"- **Lookback**: {lookback_days}d")
    lines.append(f"- **TM rows**: {n_tm_rows}")
    lines.append(f"- **DC rows**: {n_dc_rows}")
    lines.append(f"- **Sub-floor threshold**: cell n < {PER_CELL_N_FLOOR} flagged *sub-floor* (excluded from `lookup_calibrated_rate`)")
    lines.append("")
    if tm_matrix:
        lines.extend(_format_matrix_table(tm_matrix, "TM matrix — (asset, band, buf_bucket)"))
    if dc_matrix:
        lines.extend(_format_matrix_table(dc_matrix, "DC matrix — (asset, band, tier)"))
    lines.append("### Sweep — fractional Kelly × shrinkage k")
    lines.append("")
    lines.append("```")
    lines.append(json.dumps(sweep_results, indent=2, default=str))
    lines.append("```")
    return "\n".join(lines) + "\n"


# ───────────────────────────────────────────────────────────────────────────
# Sweep — fractional Kelly × shrinkage k full grid (per R1-M2 fix)
# ───────────────────────────────────────────────────────────────────────────


def _build_tm_sizers(tm_matrix: Dict[str, Any], *, fractional: float, k: int):
    def _baseline(row):
        try:
            from sim_pnl import _tm_size  # local import — no module-level side effect
        except Exception:
            return 0
        price = row["market_price"]
        stc = row["seconds_to_close"]
        asset = row["asset"]
        # R1-C2 fix: propagate None for buf_pct so the thin-buffer cap fires
        # only when production would actually have it firing (i.e., explicit
        # low-buffer reading), not on missing-data rows.
        buf_pct = row["buf_pct"]
        try:
            if buf_pct is None:
                return _tm_size(int(price), float(stc or 0), SYNTHETIC_BANKROLL_CENTS, asset, None)
            return _tm_size(int(price), float(stc or 0), SYNTHETIC_BANKROLL_CENTS, asset, float(buf_pct))
        except Exception:
            return 0

    def _proposed(row):
        price = row["market_price"]
        asset = row["asset"]
        if price is None or asset is None:
            return 0
        band = classify_band(int(price))
        if band is None:
            return 0
        bucket = _tm_sub_signal(row)
        rate = lookup_calibrated_rate(
            tm_matrix["cells"], tm_matrix["band_aggregate"], asset, band,
            bucket if bucket is not None else "bucket_0",  # neutral fallback for missing buf
            k=k,
        )
        return kelly_contracts(SYNTHETIC_BANKROLL_CENTS, rate, int(price), fractional=fractional)

    return _baseline, _proposed


def _build_dc_sizers(dc_matrix: Dict[str, Any], *, fractional: float, k: int):
    def _baseline(row):
        try:
            from sim_pnl import _dc_size
        except Exception:
            return 0
        price = row["market_price"]
        strategy = row["strategy"]
        asset = row["asset"]
        try:
            return _dc_size(str(strategy), int(price), SYNTHETIC_BANKROLL_CENTS, asset)
        except Exception:
            return 0

    def _proposed(row):
        price = row["market_price"]
        asset = row["asset"]
        strategy = row["strategy"]
        tier = map_dc_strategy_to_tier(str(strategy)) if strategy else None
        if tier is None or price is None or asset is None:
            return 0
        band = classify_band(int(price))
        if band is None:
            return 0
        rate = lookup_calibrated_rate(
            dc_matrix["cells"], dc_matrix["band_aggregate"], asset, band, tier, k=k,
        )
        return kelly_contracts(SYNTHETIC_BANKROLL_CENTS, rate, int(price), fractional=fractional)

    return _baseline, _proposed


def sweep_replays(
    tm_rows: List[sqlite3.Row],
    dc_rows: List[sqlite3.Row],
    tm_matrix: Optional[Dict[str, Any]],
    dc_matrix: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    """Full sweep of fractional Kelly × shrinkage k for both TM and DC."""
    out: Dict[str, Any] = {"sweep_grid": []}
    for frac in FRACTIONAL_KELLY_SWEEP:
        for k in SHRINKAGE_K_SWEEP:
            entry: Dict[str, Any] = {"fractional_kelly": frac, "shrinkage_k": k}
            if tm_matrix:
                tm_base, tm_prop = _build_tm_sizers(tm_matrix, fractional=frac, k=k)
                ab = replay_pnl_a_b(tm_rows, baseline_sizer=tm_base, proposed_sizer=tm_prop)
                ab["null_hypothesis"] = apply_null_hypothesis_check(ab)
                entry["tm"] = ab
            if dc_matrix:
                dc_base, dc_prop = _build_dc_sizers(dc_matrix, fractional=frac, k=k)
                ab = replay_pnl_a_b(dc_rows, baseline_sizer=dc_base, proposed_sizer=dc_prop)
                ab["null_hypothesis"] = apply_null_hypothesis_check(ab)
                entry["dc"] = ab
            out["sweep_grid"].append(entry)
    return out


# ───────────────────────────────────────────────────────────────────────────
# CLI
# ───────────────────────────────────────────────────────────────────────────


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="TM/DC conditional band-calibration research.")
    p.add_argument("--db", required=True, help="Path to state.db (read-only)")
    p.add_argument(
        "--strategy", choices=("tm", "dc", "both"), default="both",
        help="Which strategy to analyze",
    )
    p.add_argument(
        "--out",
        default="kb/findings/tm-dc-conditional-calibration-research.data.md",
        help="Auto-generated data-block output path (markdown)",
    )
    p.add_argument(
        "--lookback-days-low-bands", type=int, default=30,
        help="Lookback for 70-93c bands (default 30)",
    )
    p.add_argument(
        "--lookback-days-high-bands", type=int, default=60,
        help="Lookback for 94-100c bands (default 60)",
    )
    return p.parse_args()


def main() -> int:
    args = _parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    snapshot_ts = time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime())
    log.info("[spike 86b9zktp6] starting research run @ %s", snapshot_ts)
    log.info("[spike 86b9zktp6] db=%s strategy=%s", args.db, args.strategy)

    max_lookback = max(args.lookback_days_low_bands, args.lookback_days_high_bands)

    tm_matrix = None
    dc_matrix = None
    tm_rows: List[sqlite3.Row] = []
    dc_rows: List[sqlite3.Row] = []

    with open_read_only_db(args.db) as conn:
        if args.strategy in ("tm", "both"):
            log.info("[spike 86b9zktp6] pulling TM-eligible signals...")
            tm_rows = pull_tm_eligible_signals(conn, lookback_days=max_lookback)
            log.info("[spike 86b9zktp6] TM rows=%d", len(tm_rows))
            tm_matrix = build_tm_matrix(tm_rows)

        if args.strategy in ("dc", "both"):
            log.info("[spike 86b9zktp6] pulling DC-eligible signals...")
            dc_rows = pull_dc_eligible_signals(conn, lookback_days=max_lookback)
            log.info("[spike 86b9zktp6] DC rows=%d", len(dc_rows))
            dc_matrix = build_dc_matrix(dc_rows)

    log.info("[spike 86b9zktp6] running fractional-Kelly × k sweep...")
    sweep = sweep_replays(tm_rows, dc_rows, tm_matrix, dc_matrix)

    data_block = format_findings_data_block(
        tm_matrix, dc_matrix, sweep,
        db_path=args.db, snapshot_ts=snapshot_ts, lookback_days=max_lookback,
        n_tm_rows=len(tm_rows), n_dc_rows=len(dc_rows),
    )

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(data_block)
    log.info("[spike 86b9zktp6] data block written to %s", out_path)
    log.info("[spike 86b9zktp6] hand-authored findings doc is kb/findings/tm-dc-conditional-calibration-research.md (do NOT overwrite)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
