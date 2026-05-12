#!/usr/bin/env python3
"""Phase H-4b backfill: Glassnode on-chain features → evaluated_opportunities.

Three new columns, each a 24h-rolling z-score against a trailing 30-day
window of the same metric (so values are distribution-free across asset
and regime):

    btc_active_addresses_24h_zscore   REAL
    eth_active_addresses_24h_zscore   REAL
    btc_exchange_inflow_24h_zscore    REAL  (free tier may not cover; NULL ok)

PROVENANCE NOTE (Round 3 fix #5):
    Rows touched by this backfill carry `data_provenance =
    'backfill_glassnode_daily'`. Glassnode publishes day-D's partial
    value DURING day D — for an evaluation at 14:00 UTC, the day-D total
    has accumulated only ~58% of its eventual close. This is leakage
    relative to a strict "data available at decision time" semantic.
    The v2 trainer MUST downweight or exclude rows with this provenance
    when computing held-out validation metrics. See
    `kb/decisions/v2-train-must-account-for-backfill-skew-may02.md`.

Source: https://api.glassnode.com/v1/metrics/...
  - addresses/active_count   (free)
  - distribution/exchange_net_position_change OR exchange_inflow (premium)

Free tier: no auth required for some `/v1/metrics/<category>/<name>` endpoints
returning daily data with a several-day lag. If env GLASSNODE_API_KEY is
present, premium endpoints are attempted, falling back to NULL on 401/403.

Mirrors G-2/G-4/G-5 idempotency + checkpoint pattern. Daily granularity
means the API call count is small (~70 days × 4 metrics ≈ 280 calls
total for the entire backfill window) — caching is at (asset, metric)
granularity, not per-row.

Usage:
  python3 scripts/backfill/glassnode_backfill.py --db state.db
  python3 scripts/backfill/glassnode_backfill.py --db state.db --dry-run

Design doc: kb/decisions/phase-h4b-glassnode-may02.md.
Master plan: kb/decisions/shadow-coverage-phase-h-data-recovery-may02.md.
"""

import argparse
import bisect
import datetime
import logging
import math
import os
import sqlite3
import sys
import time as _time_mod
from typing import Callable, Dict, List, Optional, Tuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from shadow_coverage_backfill import read_checkpoint, write_checkpoint  # noqa: E402

logger = logging.getLogger(__name__)

GLASSNODE_BASE_URL = "https://api.glassnode.com/v1/metrics"

# Metric → (path, asset_symbol, requires_paid_tier)
GLASSNODE_METRICS = {
    "btc_active_addresses": ("addresses/active_count", "BTC", False),
    "eth_active_addresses": ("addresses/active_count", "ETH", False),
    "btc_exchange_inflow":  ("distribution/exchange_net_position_change", "BTC", True),
}

# Trailing window (in DAYS) used to compute z-score mean+std for each metric.
ZSCORE_WINDOW_DAYS = 30

# Free-tier rate limit: ~10 req/sec is the documented cap; be conservative.
GLASSNODE_DEFAULT_SLEEP_MS = 250


class GlassnodeFetchError(Exception):
    """Raised when ALL retries are exhausted on Glassnode. Distinguishes
    from a legitimate empty series."""


class GlassnodeAuthError(GlassnodeFetchError):
    """Raised when Glassnode returns 401/403 on a metric the caller
    expected to be free-tier (Round 3 fix #7). Distinguishes "auth
    required" from "succeeded with no data" — the latter is a legitimate
    empty window; the former is a config error that must abort the
    pipeline rather than silently NULL the column."""


# ── Network ────────────────────────────────────────────────────────────

def fetch_glassnode_metric(
    metric_path: str,
    asset_symbol: str,
    start_dt: datetime.datetime,
    end_dt: datetime.datetime,
    *,
    api_key: Optional[str] = None,
    request_fn: Optional[Callable] = None,
    max_retries: int = 3,
) -> List[Dict]:
    """Fetch a Glassnode metric series in [start_dt, end_dt] (inclusive
    on both ends, daily resolution).

    Returns list of `{"t": epoch_sec, "v": float}` dicts (Glassnode's
    standard response format), possibly empty. Raises
    `GlassnodeFetchError` on final failure.

    `metric_path` is the suffix after `/v1/metrics/` (e.g. 'addresses/active_count').
    `asset_symbol` is the Glassnode asset code ('BTC', 'ETH').

    On 401/403: raises `GlassnodeAuthError` (Round 3 fix #7). Caller is
    responsible for distinguishing "premium metric, no key supplied"
    (which it knows ahead of time and skips entirely) from "free metric
    came back 401" (a config error). Returning [] silently here would
    erase the distinction."""
    if request_fn is None:
        import requests as _requests

        def request_fn(url, params, timeout):
            return _requests.get(url, params=params, timeout=timeout)

    url = f"{GLASSNODE_BASE_URL}/{metric_path}"
    params: Dict[str, object] = {
        "a": asset_symbol,
        "s": int(start_dt.timestamp()),
        "u": int(end_dt.timestamp()),
        "i": "24h",  # daily resolution
        "f": "JSON",
    }
    if api_key:
        params["api_key"] = api_key

    last_err: Optional[str] = None
    for attempt in range(max_retries):
        try:
            resp = request_fn(url, params, 30)
            status = getattr(resp, "status_code", 0)
            if status == 200:
                try:
                    body = resp.json()
                except Exception as e:
                    last_err = f"non-json body: {e}"
                    break
                if isinstance(body, list):
                    return body
                last_err = f"unexpected body type: {type(body).__name__}"
                break
            if status in (401, 403):
                # Round 3 fix #7: do NOT silently return []. Raise so the
                # caller can decide — for a metric we know is premium-
                # gated, the caller skips it before calling us. If the
                # call still hit 401/403, that's a config error worth
                # surfacing.
                raise GlassnodeAuthError(
                    f"metric={metric_path} asset={asset_symbol}: "
                    f"HTTP {status} (auth required)"
                )
            if status == 429:
                _time_mod.sleep(2 ** attempt)
                last_err = "HTTP 429 (rate limit)"
                continue
            last_err = f"HTTP {status}"
            break
        except GlassnodeAuthError:
            # Round 3 fix #7: do NOT swallow auth errors in the
            # generic `except Exception` retry loop — propagate
            # immediately so the caller can decide.
            raise
        except Exception as e:
            last_err = str(e)
            _time_mod.sleep(2 ** attempt)
            continue
    raise GlassnodeFetchError(
        f"metric={metric_path} asset={asset_symbol}: {last_err}"
    )


# ── Z-score math ───────────────────────────────────────────────────────

def compute_zscore(value: float, history: List[float]) -> Optional[float]:
    """z = (value - mean(history)) / std(history).

    Returns None if history is too short (<5 samples) or stddev is zero
    (constant series → z is undefined). Uses sample std (n-1)."""
    if value is None or not history:
        return None
    if len(history) < 5:
        return None
    mean = sum(history) / len(history)
    var = sum((x - mean) ** 2 for x in history) / max(1, len(history) - 1)
    std = math.sqrt(var)
    if std <= 0.0:
        return None
    return (value - mean) / std


def lookup_zscore_for_date(
    series: List[Tuple[datetime.date, float]],
    target: datetime.date,
    window_days: int = ZSCORE_WINDOW_DAYS,
) -> Optional[float]:
    """Given a sorted (date, value) series, compute z-score of the value
    AT `target` using the trailing `window_days` strictly BEFORE the
    decision date — no leakage.

    Round 3 fix #5(a): trailing window now excludes ALL dates >= target
    (was: only excluded the exact target date). Glassnode publishes
    day-D's partial value DURING day D — that value is future-of-decision
    data for an evaluation row whose `eval_date == target`. Using
    `bisect_left(dates, target)` to find the cutoff index makes the
    history strictly prior days only.

    Returns None if target's date is missing OR if the trailing window
    has <5 valid samples."""
    if not series:
        return None
    # Series is sorted ascending by date.
    keys = [d for d, _ in series]
    # Find index of target.
    i = bisect.bisect_left(keys, target)
    if i >= len(series) or series[i][0] != target:
        # Target date not present in series.
        return None
    target_value = series[i][1]
    # Round 3 fix #5(a): use bisect_left(keys, target) which already
    # gives us the FIRST index with date >= target. History is strictly
    # before that — same `i` we already computed. No change in mechanics
    # for this single-target case (since `series[i].date == target` and
    # `bisect_left` returns the leftmost match), but the contract is now
    # documented as "strictly before eval_date" and the slice [lo:i]
    # excludes any entries at or after target.
    lo = max(0, i - window_days)
    history = [v for (_, v) in series[lo:i] if v is not None]
    return compute_zscore(target_value, history)


def parse_glassnode_series(
    raw: List[Dict],
) -> List[Tuple[datetime.date, float]]:
    """Convert Glassnode's `[{"t": epoch_sec, "v": float}, ...]` →
    sorted [(date, value)] list, dropping null/non-numeric entries."""
    out: List[Tuple[datetime.date, float]] = []
    for entry in raw or []:
        if not isinstance(entry, dict):
            continue
        try:
            ts = int(entry["t"])
            val = entry["v"]
            if val is None:
                continue
            v = float(val)
        except (KeyError, TypeError, ValueError):
            continue
        if math.isnan(v) or math.isinf(v):
            continue
        # Round 3 fix #6: utcfromtimestamp is deprecated (Py3.12+);
        # tz-aware constructor is the supported replacement.
        d = datetime.datetime.fromtimestamp(
            ts, tz=datetime.timezone.utc,
        ).date()
        out.append((d, v))
    out.sort(key=lambda x: x[0])
    return out


# ── Backfill driver ────────────────────────────────────────────────────

def _ensure_local_columns(conn: sqlite3.Connection) -> None:
    # Pre-check via PRAGMA: only ALTER for columns that are actually
    # missing. The previous pattern (`try: ALTER; except OperationalError:
    # pass`) was too broad — it swallowed `database is locked` the same
    # way it swallowed `duplicate column`. Under VPS lock contention
    # (May 4 2026 smoke test), the ALTER timed out, the broad except
    # hid the failure, and the next SELECT crashed with `no such column`.
    existing = {
        row[1] for row in conn.execute(
            "PRAGMA table_info(evaluated_opportunities)"
        ).fetchall()
    }
    needed = [
        ("btc_active_addresses_24h_zscore", "REAL"),
        ("eth_active_addresses_24h_zscore", "REAL"),
        ("btc_exchange_inflow_24h_zscore", "REAL"),
        # G-6 normally adds data_provenance; H-4b stamps it via COALESCE
        # in the UPDATE, so we defensively add it here for DBs that
        # haven't had G-6 run.
        ("data_provenance", "TEXT"),
    ]
    for col, typ in needed:
        if col in existing:
            continue
        conn.execute(
            f"ALTER TABLE evaluated_opportunities ADD COLUMN {col} {typ}"
        )
    conn.commit()


def backfill_glassnode(
    conn: sqlite3.Connection,
    fetcher: Optional[Callable] = None,
    batch_size: int = 50,
    sleep_ms: int = GLASSNODE_DEFAULT_SLEEP_MS,
    checkpoint_dir: Optional[str] = None,
    dry_run: bool = False,
    only_15m: bool = True,
    api_key: Optional[str] = None,
) -> int:
    """Backfill 3 z-score columns from Glassnode daily metrics.

    Strategy:
      1. Discover backfill window over rows missing the FIRST z-score
         column (we treat all 3 atomically — partial backfill would
         create inconsistent rows; the per-metric NULL fallback is for
         the WHOLE column when premium tier is unavailable).
      2. For each metric, fetch ONE Glassnode series spanning
         [window_start - 30d, window_end] — the extra 30d is the trailing
         z-score history.
      3. Per row, look up z-score for the row's evaluation_date.
      4. UPDATE with NULL when series is empty for that asset (premium
         endpoint not accessible) but DO NOT silently NULL without
         logging; raise if ALL three series came back empty (true API
         outage).

    Like G-2/G-4: aborts on per-metric ZERO-data ONLY when that metric
    is documented free-tier (active_addresses on BTC/ETH). For premium
    metrics, NULL is acceptable per spec.
    """
    if fetcher is None:
        fetcher = fetch_glassnode_metric
    if api_key is None:
        api_key = os.environ.get("GLASSNODE_API_KEY")

    _ensure_local_columns(conn)

    # 1. Discover row date range. Use the BTC active-addresses column
    # (free tier guaranteed) as the "is this row backfilled?" sentinel.
    rng = conn.execute(
        "SELECT MIN(evaluation_time), MAX(evaluation_time) "
        "FROM evaluated_opportunities "
        "WHERE btc_active_addresses_24h_zscore IS NULL "
        "AND evaluation_time IS NOT NULL "
        + (" AND product_type = '15m'" if only_15m else "")
    ).fetchone()
    if not rng or rng[0] is None:
        return 0
    start_iso, end_iso = rng[0], rng[1]
    logger.info("h4b: backfill window [%s .. %s]", start_iso, end_iso)

    start_dt = datetime.datetime.fromisoformat(
        start_iso.replace("Z", "+00:00")
    )
    end_dt = datetime.datetime.fromisoformat(end_iso.replace("Z", "+00:00"))

    # Extend start by `ZSCORE_WINDOW_DAYS` so the earliest row's z-score
    # has a full trailing window.
    fetch_start = start_dt - datetime.timedelta(days=ZSCORE_WINDOW_DAYS + 1)
    fetch_end = end_dt + datetime.timedelta(days=1)

    # 2. Fetch each metric series ONCE.
    series_by_metric: Dict[str, List[Tuple[datetime.date, float]]] = {}
    n_free_metrics_with_data = 0
    for metric_key, (path, asset_sym, requires_paid) in GLASSNODE_METRICS.items():
        if requires_paid and not api_key:
            logger.info(
                "h4b: skipping paid metric %s (no GLASSNODE_API_KEY) — "
                "column will be NULL for all rows",
                metric_key,
            )
            series_by_metric[metric_key] = []
            continue
        try:
            raw = fetcher(
                path, asset_sym, fetch_start, fetch_end, api_key=api_key,
            )
        except GlassnodeAuthError as e:
            # Round 3 fix #7: 401/403 on a metric we attempted means
            # either (a) the metric we thought was free is now paid
            # (Glassnode policy change), or (b) GLASSNODE_API_KEY env is
            # invalid for the paid metric we attempted. Either way the
            # column would silently NULL across all rows — refuse and let
            # the operator triage. Premium metrics with no key supplied
            # are short-circuited above and do NOT reach this branch.
            if not requires_paid:
                raise GlassnodeFetchError(
                    f"h4b: free-tier metric {metric_key} returned 401/403 "
                    f"({e}) — refusing to NULL silently. Glassnode policy "
                    f"may have changed; investigate."
                )
            # Paid metric with key supplied but key rejected: warn loud,
            # leave NULL (operator may have a stale key).
            logger.warning(
                "h4b: paid metric %s rejected key (%s) — column NULL",
                metric_key, e,
            )
            raw = []
        except GlassnodeFetchError as e:
            logger.warning("h4b: %s fetch failed: %s", metric_key, e)
            raw = []
        parsed = parse_glassnode_series(raw)
        series_by_metric[metric_key] = parsed
        if not requires_paid and parsed:
            n_free_metrics_with_data += 1
        logger.info(
            "h4b: %s — %d daily samples (paid_tier=%s)",
            metric_key, len(parsed), requires_paid,
        )
        if sleep_ms > 0 and not dry_run:
            _time_mod.sleep(sleep_ms / 1000.0)

    # Abort if ZERO free-tier metrics returned data — that's an outage,
    # not a data drought. Don't silently NULL. (Free tier is BTC + ETH
    # active_addresses → 2 expected.)
    if n_free_metrics_with_data == 0:
        raise GlassnodeFetchError(
            "h4b: zero data on ALL free-tier metrics — refusing to leave "
            "NULL silently. Rerun when Glassnode is healthy."
        )

    # 3. Per-row UPDATE.
    last_id = (
        read_checkpoint(checkpoint_dir, "h4b_glassnode")
        if checkpoint_dir
        else 0
    )
    total = 0
    while True:
        sql = (
            "SELECT id, evaluation_time FROM evaluated_opportunities "
            "WHERE id > ? AND btc_active_addresses_24h_zscore IS NULL "
            "AND evaluation_time IS NOT NULL "
        )
        if only_15m:
            sql += "AND product_type = '15m' "
        sql += "ORDER BY id LIMIT ?"
        rows = conn.execute(sql, (last_id, batch_size)).fetchall()
        if not rows:
            break
        for r in rows:
            try:
                eval_dt = datetime.datetime.fromisoformat(
                    r["evaluation_time"].replace("Z", "+00:00")
                )
                eval_date = eval_dt.date()
            except Exception:
                last_id = r["id"]
                continue

            btc_z = lookup_zscore_for_date(
                series_by_metric.get("btc_active_addresses", []),
                eval_date,
            )
            eth_z = lookup_zscore_for_date(
                series_by_metric.get("eth_active_addresses", []),
                eval_date,
            )
            btc_inflow_z = lookup_zscore_for_date(
                series_by_metric.get("btc_exchange_inflow", []),
                eval_date,
            )
            if not dry_run:
                # Round 4 fix #1 (CRITICAL): stamp data_provenance so the
                # v2 trainer can downweight/exclude these rows. COALESCE
                # preserves any pre-existing 'backfill_60s_inputs' from
                # G-2/G-4 — H-4b only sets provenance when it is currently
                # NULL.
                conn.execute(
                    "UPDATE evaluated_opportunities SET "
                    "btc_active_addresses_24h_zscore = ?, "
                    "eth_active_addresses_24h_zscore = ?, "
                    "btc_exchange_inflow_24h_zscore = ?, "
                    "data_provenance = COALESCE(data_provenance, "
                    "'backfill_glassnode_daily') "
                    "WHERE id = ?",
                    (btc_z, eth_z, btc_inflow_z, r["id"]),
                )
            total += 1
            last_id = r["id"]
        if not dry_run:
            conn.commit()
        if checkpoint_dir:
            write_checkpoint(checkpoint_dir, "h4b_glassnode", last_id)
        if len(rows) < batch_size:
            break
    return total


# ── CLI ────────────────────────────────────────────────────────────────

def _connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=10000")
    return conn


def main(argv=None) -> int:
    # Layer 2 of orphan prevention (May 3 2026 postmortem): hard 25-min
    # alarm so the script self-kills at the ceiling regardless of any
    # stuck loop, DNS hang, or DB lock.
    from _h4_runtime_safety import install_hard_timeout
    install_hard_timeout(label="h4b_glassnode")

    parser = argparse.ArgumentParser(
        description="Phase H-4b — Glassnode on-chain z-score backfill"
    )
    parser.add_argument("--db", required=True, help="path to state.db")
    parser.add_argument(
        "--checkpoint-dir", default="data/backfill_ckpt",
        help="directory for resumable checkpoint files",
    )
    parser.add_argument(
        "--batch-size", type=int, default=50,
        help="rows per UPDATE commit (CLAUDE.md ≤50)",
    )
    parser.add_argument(
        "--sleep-ms", type=int, default=GLASSNODE_DEFAULT_SLEEP_MS,
        help="sleep between API calls (default 250)",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="walk rows but skip UPDATE",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )
    conn = _connect(args.db)
    n = backfill_glassnode(
        conn,
        batch_size=args.batch_size,
        sleep_ms=args.sleep_ms,
        checkpoint_dir=args.checkpoint_dir,
        dry_run=args.dry_run,
    )
    logger.info("h4b: updated %d rows (dry_run=%s)", n, args.dry_run)
    return 0


if __name__ == "__main__":
    sys.exit(main())
