"""Cohort attribution helper — Phase 1 P1.1 (Money Printer Roadmap).

Materializes the `cohort_attribution_daily` aggregate table from
`evaluated_opportunities`. Each row is one cohort × cohort_date:

    (cohort_date, asset, product_type, strategy,
     price_band_5c, stc_band_60s, cell_block_stage)

with rolling 30d + 7d windows on n / wr / cf_pnl / cal_gap and a
two-column alert-state slot (alert_state, last_alert_time) populated
via the inline write-back protocol (design doc § Alert state write-back
protocol option a; parameter-injection variant via `alerts_module`).

Single-source DDL: `ensure_schema(conn)` executes the canonical CREATE
TABLE block; both `bot.state.StateManager._create_tables` and
`run_aggregation` call it so the schema is owned in exactly one place.

Design doc: kb/decisions/cohort-measurement-design-may12.md (LOCAL-only).
Ticket: ClickUp 86b9x3kgd (P1.1).
"""
from __future__ import annotations

import datetime as _dt
import logging
import math
import sqlite3
from typing import Any, Dict, FrozenSet, Optional, Tuple


# ── Canonical partition-stages set (single source of truth) ──────────────────

COHORT_PARTITION_STAGES: FrozenSet[str] = frozenset({
    "candidate",
    "96C_SOL_XRP_STC_DANGER_BAND",
    "TM98_97_98C_2_5MIN_BLEED",
    "SOL_TAKER_85_89C_2_5MIN_BLEED",
    "SOL_BLEED_V2_88_93C_2_5MIN",
})

PRODUCT_TYPE_15M = "15m"

# STC band 11+ is the tail. Bands 0..10 cover 0..660s in 60s buckets
# (band 10 = 600-660s); seconds_to_close >= 660 collapses to band 11.
_STC_TAIL_BAND = 11

# Persistence-days lookback cap: P1.3's CALIBRATION trigger uses
# 3-day persistence; 30d is generous slack. Bounds the per-cohort
# row scan on the 73d backfill (R1 m6).
_PERSISTENCE_LOOKBACK_LIMIT = 30

# Wilson95 z-score (two-sided alpha=0.05) — matches the inline pattern
# used by scripts/audit/stc_structural_clustering.py + scripts/audit/audit_cron.py.
_WILSON_Z = 1.959963984540054


# ── Pure-function helpers (testable in isolation) ─────────────────────────────


def compute_price_band_5c(market_price_cents: int) -> int:
    """Floor-divide market price into 5¢ bands. 88¢ -> 17 (band 85-89¢)."""
    return int(market_price_cents) // 5


def compute_stc_band_60s(seconds_to_close: float) -> int:
    """Bucket seconds-to-close into 60s bands.

    Convention: inclusive lower, exclusive upper. seconds_to_close=60.0 →
    band 1 (60-120s slot). Bands 0..10 cover 0..660s in 60s buckets
    (band 10 covers 600-660s); all values ≥ 660s collapse to the tail
    band 11.
    """
    if seconds_to_close is None:
        raise TypeError("seconds_to_close must be numeric, not None")
    band = int(seconds_to_close // 60)
    return band if band < _STC_TAIL_BAND else _STC_TAIL_BAND


def cohort_key_tuple(row: Dict[str, Any]) -> Tuple[str, str, str, int, int, str]:
    """Return canonical 6-tuple cohort key for a raw evaluated_opportunities row.

    Order: (asset, product_type, strategy, price_band_5c, stc_band_60s,
    cell_block_stage). The `filter_stage` column carries `cell_block_stage`
    in the source row.
    """
    return (
        row["asset"],
        row["product_type"],
        row["strategy"],
        compute_price_band_5c(row["market_price"]),
        compute_stc_band_60s(row["seconds_to_close"]),
        row["filter_stage"],
    )


def wilson95_ci(k: int, n: int) -> Tuple[float, float]:
    """Wilson score 95% CI for binomial proportion k/n.

    Edge cases (matches scripts/audit/stc_structural_clustering.py):
      n <= 0  → (0.0, 1.0)  (no data; max uncertainty)
      k = 0   → lower = 0.0
      k = n   → upper = 1.0
    """
    if n <= 0:
        return (0.0, 1.0)
    z = _WILSON_Z
    phat = k / n
    denom = 1.0 + z * z / n
    center = phat + z * z / (2.0 * n)
    delta = z * math.sqrt(phat * (1.0 - phat) / n + z * z / (4.0 * n * n))
    lo = max(0.0, (center - delta) / denom)
    hi = min(1.0, (center + delta) / denom)
    return (lo, hi)


# ── Schema bootstrap ─────────────────────────────────────────────────────────


_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS cohort_attribution_daily (
    cohort_date          TEXT NOT NULL,
    asset                TEXT NOT NULL,
    product_type         TEXT NOT NULL,
    strategy             TEXT NOT NULL,
    price_band_5c        INTEGER NOT NULL,
    stc_band_60s         INTEGER NOT NULL,
    cell_block_stage     TEXT NOT NULL,
    n_30d                INTEGER NOT NULL,
    n_yes_30d            INTEGER NOT NULL,
    n_no_30d             INTEGER NOT NULL,
    wr_30d               REAL,
    wilson95_lo_30d      REAL,
    wilson95_hi_30d      REAL,
    sum_cf_cents_30d     INTEGER NOT NULL,
    cf_pnl_30d_dollars   REAL,
    mean_cal_prob_30d    REAL,
    cal_gap_30d          REAL,
    n_7d                 INTEGER NOT NULL,
    wr_7d                REAL,
    cf_pnl_7d_dollars    REAL,
    cal_gap_7d           REAL,
    alert_state          TEXT,
    last_alert_time      TEXT,
    PRIMARY KEY (cohort_date, asset, product_type, strategy,
                 price_band_5c, stc_band_60s, cell_block_stage)
);

CREATE INDEX IF NOT EXISTS idx_cohort_attribution_daily_date
    ON cohort_attribution_daily(cohort_date);
CREATE INDEX IF NOT EXISTS idx_cohort_attribution_daily_bleed
    ON cohort_attribution_daily(cohort_date, cf_pnl_30d_dollars);
"""


def ensure_schema(conn: sqlite3.Connection) -> None:
    """Idempotent: create the `cohort_attribution_daily` table + indexes
    if missing. Owns the single canonical DDL for the table."""
    conn.executescript(_SCHEMA_SQL)


# ── Alert-state transition ───────────────────────────────────────────────────


def compute_next_alert_state(
    prior_row: Optional[Dict[str, Any]],
    current_metrics: Dict[str, Any],
    *,
    alerts_module: Any = None,
    now: Optional[_dt.datetime] = None,
) -> Tuple[str, Optional[str]]:
    """Return (next_alert_state, next_last_alert_time) for one cohort row.

    Brand-new cohort (`prior_row is None`) → ('quiet', None) regardless of
    metrics or alerts_module presence (R7 advisory pin).

    With `alerts_module is None` (graceful fallback — P1.3 not yet shipped):
      - prior state in (None, '', 'quiet'): ('quiet', None)
      - prior state starts with 'firing': ('quiet', prior.last_alert_time)
        — preserves first-fire timestamp for P1.4 weekly-report § 4
        resolution-date attribution; conservatively treats every prior-firing
        cohort as resolved since we cannot evaluate triggers without P1.3.

    With `alerts_module` provided (P1.3 active): the module's
    `should_fire_bleed_alert` / `should_fire_calibration_alert` /
    `cooldown_active` primitives drive the transition. P1.3 is the canonical
    home for the trigger thresholds.
    """
    prior_state = (prior_row or {}).get("alert_state")
    prior_last_alert = (prior_row or {}).get("last_alert_time")

    if prior_row is None:
        return ("quiet", None)

    if alerts_module is None:
        if isinstance(prior_state, str) and prior_state.startswith("firing"):
            return ("quiet", prior_last_alert)
        return ("quiet", None)

    n_30d = int(current_metrics.get("n_30d") or 0)
    wilson95_hi = current_metrics.get("wilson95_hi_30d")
    cf_pnl_dollars = current_metrics.get("cf_pnl_30d_dollars")
    abs_cal_gap = current_metrics.get("abs_cal_gap_30d")
    persistence_days = int(current_metrics.get("persistence_days") or 0)
    stage = current_metrics.get("cell_block_stage", "candidate")

    fire_bleed = False
    fire_cal = False
    if wilson95_hi is not None and cf_pnl_dollars is not None:
        try:
            fire_bleed = bool(alerts_module.should_fire_bleed_alert(
                n=n_30d,
                wilson95_hi=float(wilson95_hi),
                cf_pnl_30d_dollars=float(cf_pnl_dollars),
                cell_block_stage=stage,
            ))
        except Exception:
            logging.exception("[COHORT_ALERTS] should_fire_bleed_alert raised; treating as no-fire")
    if abs_cal_gap is not None:
        try:
            fire_cal = bool(alerts_module.should_fire_calibration_alert(
                n=n_30d,
                abs_cal_gap=float(abs_cal_gap),
                persistence_days=persistence_days,
            ))
        except Exception:
            logging.exception("[COHORT_ALERTS] should_fire_calibration_alert raised; treating as no-fire")

    now_dt = now or _dt.datetime.now(_dt.timezone.utc)
    now_iso = now_dt.replace(tzinfo=_dt.timezone.utc).isoformat() if now_dt.tzinfo is None else now_dt.isoformat()

    if fire_bleed or fire_cal:
        in_cooldown = False
        try:
            in_cooldown = bool(alerts_module.cooldown_active(
                last_alert_time=_parse_iso_or_none(prior_last_alert),
                now=now_dt,
            ))
        except Exception:
            logging.exception("[COHORT_ALERTS] cooldown_active raised; treating as not in cooldown")
        next_state = "firing_bleed" if fire_bleed else "firing_cal"
        next_last = prior_last_alert if in_cooldown else now_iso
        return (next_state, next_last)

    if isinstance(prior_state, str) and prior_state.startswith("firing"):
        return ("quiet", prior_last_alert)
    return ("quiet", None)


def _parse_iso_or_none(value: Any) -> Optional[_dt.datetime]:
    if value is None:
        return None
    if isinstance(value, _dt.datetime):
        return value
    try:
        s = str(value)
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        return _dt.datetime.fromisoformat(s)
    except (TypeError, ValueError):
        return None


# ── Aggregation entry point ──────────────────────────────────────────────────


_PARTITION_PLACEHOLDERS = ",".join("?" for _ in COHORT_PARTITION_STAGES)


def run_aggregation(
    conn: sqlite3.Connection,
    *,
    alerts_module: Any = None,
    cohort_date: Optional[str] = None,
    now: Optional[_dt.datetime] = None,
) -> None:
    """Materialize `cohort_attribution_daily` for the given `cohort_date`.

    Default `cohort_date` is today (UTC). Idempotent — composite-PK upsert
    via INSERT OR REPLACE so rerunning on the same `cohort_date` overwrites
    any existing rows for that day with the freshly-computed values.

    `alerts_module=None` → graceful fallback (P1.3 not yet shipped): every
    row's `alert_state` is set to 'quiet' for brand-new cohorts (and the
    firing→quiet transition for prior-firing cohorts preserves
    `last_alert_time`). When `alerts_module` is provided, the trigger
    primitives are consulted per `compute_next_alert_state`.

    Honest-NULL: rows with `market_result NOT IN ('yes','no')` are filtered
    at SQL level; un-settled rows are NOT folded in as 0-outcomes.
    """
    ensure_schema(conn)

    now_dt = now or _dt.datetime.now(_dt.timezone.utc)
    date_str = cohort_date or now_dt.date().isoformat()

    if not _evaluated_opportunities_exists(conn):
        logging.info(
            "[COHORT_AGG] evaluated_opportunities table missing on this conn; "
            "nothing to aggregate (cohort_date=%s)",
            date_str,
        )
        return

    cur_30d = (now_dt - _dt.timedelta(days=30)).isoformat()
    cur_7d = (now_dt - _dt.timedelta(days=7)).isoformat()
    cur_now = now_dt.isoformat()

    cohorts_30d = _aggregate_window(conn, since_iso=cur_30d, until_iso=cur_now)
    cohorts_7d = _aggregate_window(conn, since_iso=cur_7d, until_iso=cur_now)
    cohorts_7d_map = {row[0]: row for row in cohorts_7d}

    insert_rows = []
    for row in cohorts_30d:
        cohort_key = row[0]
        (asset, product_type, strategy, price_band_5c, stc_band_60s, stage) = cohort_key
        n_30d, n_yes_30d, n_no_30d, sum_cf_cents, sum_cal_prob, n_cal = row[1:]

        wr_30d = (n_yes_30d / n_30d) if n_30d > 0 else None
        wilson_lo, wilson_hi = (wilson95_ci(n_yes_30d, n_30d) if n_30d > 0 else (None, None))
        cf_pnl_30d_dollars = (sum_cf_cents / 100.0) if sum_cf_cents is not None else None
        mean_cal_prob_30d = (sum_cal_prob / n_cal) if (sum_cal_prob is not None and n_cal > 0) else None
        cal_gap_30d = (mean_cal_prob_30d - wr_30d) if (mean_cal_prob_30d is not None and wr_30d is not None) else None

        row7 = cohorts_7d_map.get(cohort_key)
        if row7 is not None:
            _, n7, n_yes_7, _, sum_cf_7, sum_cal_7, n_cal_7 = row7
            wr_7d = (n_yes_7 / n7) if n7 > 0 else None
            cf_pnl_7d_dollars = (sum_cf_7 / 100.0) if sum_cf_7 is not None else None
            mean_cal_prob_7d = (sum_cal_7 / n_cal_7) if (sum_cal_7 is not None and n_cal_7 > 0) else None
            cal_gap_7d = (mean_cal_prob_7d - wr_7d) if (mean_cal_prob_7d is not None and wr_7d is not None) else None
        else:
            n7 = 0
            wr_7d = None
            cf_pnl_7d_dollars = None
            cal_gap_7d = None

        prior_row = _fetch_prior_cohort_row(
            conn,
            cohort_date=date_str,
            cohort_key=cohort_key,
        )

        abs_cal_gap_30d = abs(cal_gap_30d) if cal_gap_30d is not None else None
        persistence_days = _compute_persistence_days(
            conn, cohort_date=date_str, cohort_key=cohort_key, abs_threshold=0.05
        )

        next_state, next_last_alert = compute_next_alert_state(
            prior_row,
            {
                "n_30d": n_30d,
                "wilson95_hi_30d": wilson_hi,
                "cf_pnl_30d_dollars": cf_pnl_30d_dollars,
                "abs_cal_gap_30d": abs_cal_gap_30d,
                "persistence_days": persistence_days,
                "cell_block_stage": stage,
            },
            alerts_module=alerts_module,
            now=now_dt,
        )

        insert_rows.append((
            date_str, asset, product_type, strategy,
            price_band_5c, stc_band_60s, stage,
            n_30d, n_yes_30d, n_no_30d, wr_30d,
            wilson_lo, wilson_hi,
            sum_cf_cents if sum_cf_cents is not None else 0,
            cf_pnl_30d_dollars,
            mean_cal_prob_30d, cal_gap_30d,
            n7, wr_7d, cf_pnl_7d_dollars, cal_gap_7d,
            next_state, next_last_alert,
        ))

    if insert_rows:
        # Per bot/CLAUDE.md SQLite rule: batch ≤50 rows per commit. Real
        # production cardinality is ~600 cohorts/day so we chunk.
        BATCH = 50
        for i in range(0, len(insert_rows), BATCH):
            chunk = insert_rows[i:i + BATCH]
            conn.executemany(
                """
                INSERT OR REPLACE INTO cohort_attribution_daily (
                    cohort_date, asset, product_type, strategy,
                    price_band_5c, stc_band_60s, cell_block_stage,
                    n_30d, n_yes_30d, n_no_30d, wr_30d,
                    wilson95_lo_30d, wilson95_hi_30d,
                    sum_cf_cents_30d, cf_pnl_30d_dollars,
                    mean_cal_prob_30d, cal_gap_30d,
                    n_7d, wr_7d, cf_pnl_7d_dollars, cal_gap_7d,
                    alert_state, last_alert_time
                ) VALUES (
                    ?,?,?,?,
                    ?,?,?,
                    ?,?,?,?,
                    ?,?,
                    ?,?,
                    ?,?,
                    ?,?,?,?,
                    ?,?
                )
                """,
                chunk,
            )
            conn.commit()
    else:
        conn.commit()


def _evaluated_opportunities_exists(conn: sqlite3.Connection) -> bool:
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' "
        "AND name='evaluated_opportunities'"
    ).fetchone()
    return row is not None


def _aggregate_window(
    conn: sqlite3.Connection, *, since_iso: str, until_iso: str,
) -> list:
    """Group evaluated_opportunities by the 6-dim cohort key over a time window.

    Honest-NULL: market_result IN ('yes','no') filters un-settled rows at SQL
    level. Each returned record is:
      ((asset, product_type, strategy, price_band_5c, stc_band_60s, stage),
       n, n_yes, n_no, sum_cf_cents, sum_cal_prob, n_cal_observed)
    """
    sql = f"""
        SELECT asset, product_type, strategy,
               CAST(market_price / 5 AS INTEGER) AS price_band_5c,
               MIN(CAST(seconds_to_close / 60 AS INTEGER), 11) AS stc_band_60s,
               filter_stage AS cell_block_stage,
               COUNT(*) AS n,
               SUM(CASE WHEN market_result='yes' THEN 1 ELSE 0 END) AS n_yes,
               SUM(CASE WHEN market_result='no' THEN 1 ELSE 0 END) AS n_no,
               SUM(COALESCE(counterfactual_pnl, 0)) AS sum_cf_cents,
               SUM(CASE WHEN calibrated_prob IS NOT NULL THEN calibrated_prob ELSE 0.0 END) AS sum_cal_prob,
               SUM(CASE WHEN calibrated_prob IS NOT NULL THEN 1 ELSE 0 END) AS n_cal_observed
        FROM evaluated_opportunities
        WHERE product_type = ?
          AND filter_stage IN ({_PARTITION_PLACEHOLDERS})
          AND market_result IN ('yes', 'no')
          AND market_price IS NOT NULL
          AND seconds_to_close IS NOT NULL
          AND asset IS NOT NULL
          AND strategy IS NOT NULL
          AND evaluation_time >= ?
          AND evaluation_time <= ?
        GROUP BY 1, 2, 3, 4, 5, 6
    """
    params = [PRODUCT_TYPE_15M] + list(COHORT_PARTITION_STAGES) + [since_iso, until_iso]
    cur = conn.execute(sql, params)
    out = []
    for row in cur.fetchall():
        (asset, product_type, strategy, price_band, stc_band, stage,
         n, n_yes, n_no, sum_cf_cents, sum_cal_prob, n_cal_obs) = row
        cohort_key = (asset, product_type, strategy, int(price_band), int(stc_band), stage)
        out.append((
            cohort_key,
            int(n),
            int(n_yes),
            int(n_no),
            int(sum_cf_cents) if sum_cf_cents is not None else 0,
            float(sum_cal_prob) if sum_cal_prob is not None else None,
            int(n_cal_obs) if n_cal_obs is not None else 0,
        ))
    return out


def _fetch_prior_cohort_row(
    conn: sqlite3.Connection, *, cohort_date: str, cohort_key: Tuple,
) -> Optional[Dict[str, Any]]:
    """Return the most-recent prior `cohort_attribution_daily` row strictly
    before `cohort_date`, or None if this is a brand-new cohort.
    """
    (asset, product_type, strategy, price_band_5c, stc_band_60s, stage) = cohort_key
    row = conn.execute(
        """
        SELECT cohort_date, alert_state, last_alert_time
        FROM cohort_attribution_daily
        WHERE asset = ? AND product_type = ? AND strategy = ?
          AND price_band_5c = ? AND stc_band_60s = ?
          AND cell_block_stage = ?
          AND cohort_date < ?
        ORDER BY cohort_date DESC
        LIMIT 1
        """,
        (asset, product_type, strategy, price_band_5c, stc_band_60s, stage, cohort_date),
    ).fetchone()
    if row is None:
        return None
    return {
        "cohort_date": row[0],
        "alert_state": row[1],
        "last_alert_time": row[2],
    }


def _compute_persistence_days(
    conn: sqlite3.Connection,
    *,
    cohort_date: str,
    cohort_key: Tuple,
    abs_threshold: float,
) -> int:
    """Count consecutive prior aggregation days (immediately preceding
    `cohort_date`) where the cohort's `abs(cal_gap_30d)` exceeded
    `abs_threshold`. Used by P1.3's CALIBRATION trigger.

    Brand-new cohort (no prior rows) → 0.
    """
    (asset, product_type, strategy, price_band_5c, stc_band_60s, stage) = cohort_key
    rows = conn.execute(
        """
        SELECT cohort_date, cal_gap_30d
        FROM cohort_attribution_daily
        WHERE asset = ? AND product_type = ? AND strategy = ?
          AND price_band_5c = ? AND stc_band_60s = ?
          AND cell_block_stage = ?
          AND cohort_date < ?
        ORDER BY cohort_date DESC
        LIMIT ?
        """,
        (asset, product_type, strategy, price_band_5c, stc_band_60s, stage,
         cohort_date, _PERSISTENCE_LOOKBACK_LIMIT),
    ).fetchall()
    persistence = 0
    for _, cal_gap in rows:
        if cal_gap is None or abs(cal_gap) <= abs_threshold:
            break
        persistence += 1
    return persistence
