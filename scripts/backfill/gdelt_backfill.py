#!/usr/bin/env python3
"""Phase H-4a backfill: GDELT news event clusters → evaluated_opportunities.

Pulls article counts and average tone for each row's underlying asset
in the 1-hour window ending at `evaluation_time`. Two new columns:

    gdelt_event_count_1h_pre_decision   INTEGER  (count of articles)
    gdelt_avg_tone_1h_pre_decision      REAL     (mean of GDELT `tone` field)

Source: GDELT 2.0 Doc API (free, no auth, indexed since 2015).
  https://api.gdeltproject.org/api/v2/doc/doc?query=...&mode=ArtList&format=json

Mirrors the G-2/G-4/G-5 idempotency + checkpoint pattern in
`shadow_coverage_backfill.py`:
  - WHERE col IS NULL → re-runs skip already-done rows.
  - Checkpoint at `data/backfill_ckpt/h4a_gdelt.last_id` resumes after Ctrl-C.
  - Per (date, hour, asset) bucket cache amortizes API calls — many
    15M rows share the same hour bucket.
  - Aborts (raises) on ZERO data for any (asset, day) — the alternative
    is silently writing NULL across hundreds of rows, which then look
    "backfilled" forever and never get retried.

Usage:
  python3 scripts/backfill/gdelt_backfill.py --db state.db
  python3 scripts/backfill/gdelt_backfill.py --db state.db --dry-run
  python3 scripts/backfill/gdelt_backfill.py --db state.db --batch-size 50 --sleep-ms 250

Design doc: kb/decisions/phase-h4a-gdelt-may02.md.
Master plan: kb/decisions/shadow-coverage-phase-h-data-recovery-may02.md.

Note: this script is RUNNABLE but does not auto-trigger. Operator must
invoke it explicitly with --db.
"""

import argparse
import datetime
import logging
import os
import sqlite3
import sys
import time as _time_mod
from typing import Callable, Dict, List, Optional, Tuple

# Re-use the checkpoint helpers from the master harness so phase-H scripts
# share one checkpoint convention with phase-G.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from shadow_coverage_backfill import read_checkpoint, write_checkpoint  # noqa: E402

logger = logging.getLogger(__name__)

GDELT_DOC_URL = "https://api.gdeltproject.org/api/v2/doc/doc"

# Asset → GDELT search query. We use double-quoted phrases so the API
# treats them as literal terms (per GDELT Doc API query syntax). Round 3
# fix #2: queries are now SYMMETRIC across the four assets — canonical
# name + $-prefixed ticker (a common convention in crypto news headlines).
# The previous "sol crypto" / "ripple crypto" qualifiers narrowed SOL+XRP
# recall vs BTC+ETH, biasing event counts. We accept the small false-
# positive rate from bare "sol"/"xrp" (mostly resolved by the $-ticker
# alternative widening recall on real crypto coverage).
GDELT_ASSET_QUERIES: Dict[str, str] = {
    "BTC": '("bitcoin" OR "$btc")',
    "ETH": '("ethereum" OR "$eth")',
    "SOL": '("solana" OR "$sol")',
    # Round 4 fix #2 (MEDIUM): restore "ripple" — it is the dominant
    # name used in news headlines for XRP. Round 3 dropped it on
    # symmetry grounds, but the recall regression is bigger than the
    # tiny false-positive cost (BTC/ETH ticker collisions already
    # exist for "ethereum" vs "ETC" etc.). Symmetric recall matters
    # MORE than symmetric query syntax.
    "XRP": '("xrp" OR "$xrp" OR "ripple")',
}

# GDELT's `tone` is roughly [-10, +10]. Sanity bounds for outlier guard.
GDELT_TONE_MIN = -100.0
GDELT_TONE_MAX = 100.0

# Free-tier rate limit: GDELT recommends ≤1 req/sec from a single IP.
# We default to 1100ms → comfortably under that ceiling.
GDELT_DEFAULT_SLEEP_MS = 1100

DEFAULT_LOOKBACK_MIN = 60  # 1-hour window per spec.


class GdeltFetchError(Exception):
    """Raised when ALL retries are exhausted on the GDELT Doc API.

    Distinguishes 'API call failed' from 'API succeeded but window has
    no articles' — the latter is a legitimate `articles=[]`. Aborting on
    failure prevents silently writing NULL across many rows."""


# ── Network ────────────────────────────────────────────────────────────

# Bug 1 fix (2026-05-04): GDELT free-tier IP throttle on 429 persists
# longer than the previous 1+2+4=7s exponential budget. Observed in
# May 4 smoke test — workflow exited on the very first bucket. New
# schedule gives 5+15+30+60+120 = 230s cumulative budget across 5
# attempts, which empirically clears the throttle.
_GDELT_429_BACKOFF_SCHEDULE_S: List[int] = [5, 15, 30, 60, 120]


def fetch_gdelt_articles(
    asset: str,
    start_dt: datetime.datetime,
    end_dt: datetime.datetime,
    *,
    request_fn: Optional[Callable] = None,
    max_retries: int = 5,
) -> List[Dict]:
    """Fetch GDELT articles for `asset` in [start_dt, end_dt].

    Returns the parsed `articles` list (may be empty). Raises
    `GdeltFetchError` on final failure across `max_retries`.

    GDELT timestamps use the format YYYYMMDDHHMMSS (UTC). The Doc API
    returns at most 250 articles per call — adequate for a 1-hour window
    on these 4 crypto queries (typical: 5-50 articles/hour/asset).

    On HTTP 429, sleeps per `_GDELT_429_BACKOFF_SCHEDULE_S` between
    attempts. On other exceptions (network, JSON parse), uses the
    same schedule. Both cases bounded by `max_retries`."""
    if request_fn is None:
        import requests as _requests

        def request_fn(url, params, timeout):
            return _requests.get(url, params=params, timeout=timeout)

    query = GDELT_ASSET_QUERIES.get(asset)
    if query is None:
        return []

    params = {
        "query": query,
        "mode": "ArtList",
        "format": "json",
        "maxrecords": 250,
        "startdatetime": start_dt.strftime("%Y%m%d%H%M%S"),
        "enddatetime": end_dt.strftime("%Y%m%d%H%M%S"),
        "sort": "DateDesc",
    }

    last_err: Optional[str] = None
    for attempt in range(max_retries):
        try:
            resp = request_fn(GDELT_DOC_URL, params, 30)
            status = getattr(resp, "status_code", 0)
            if status == 200:
                # Bug 1.1 (2026-05-03): GDELT free-tier sometimes
                # returns 200 with empty/HTML body (Cloudflare
                # challenge, anti-bot WAF, brief upstream blip).
                # Pre-fix this raised on the very first bucket of
                # the smoke test (workflow 25293299100, SOL-2026-
                # 02-22-04). Treat all malformed-200 cases as
                # transient: retry per the same backoff schedule
                # used for 429.
                # Adversarial-review CRIT-D: initialize `body = None`
                # so the chained checks below cannot hit
                # UnboundLocalError if a future refactor changes the
                # short-circuit ordering.
                body = None
                _malformed_reason: Optional[str] = None
                try:
                    body = resp.json()
                except Exception as e:
                    _malformed_reason = f"non-json body: {e}"
                if _malformed_reason is None and not isinstance(body, dict):
                    _malformed_reason = (
                        f"unexpected body type: {type(body).__name__}"
                    )
                if _malformed_reason is None:
                    arts = body.get("articles") or []
                    if not isinstance(arts, list):
                        _malformed_reason = "articles field not a list"
                    else:
                        return arts
                # Malformed 200 — retry per schedule, same as 429.
                last_err = _malformed_reason
                if attempt < max_retries - 1:
                    _idx = min(
                        attempt, len(_GDELT_429_BACKOFF_SCHEDULE_S) - 1,
                    )
                    _time_mod.sleep(_GDELT_429_BACKOFF_SCHEDULE_S[_idx])
                continue
            if status == 429:
                # Bug 1 (2026-05-04): the previous `2 ** attempt`
                # backoff (1+2+4=7s for 3 attempts) was insufficient
                # against GDELT free-tier IP throttle. New schedule:
                # 5/15/30/60/120s = 230s cumulative across 5 attempts.
                last_err = (
                    f"HTTP 429 (rate limit, attempt {attempt + 1}/"
                    f"{max_retries})"
                )
                # Adversarial-review CRIT-1: skip the sleep on the
                # final attempt — the next iteration won't run, so
                # sleeping is pure budget burn (up to 120s). Without
                # this, an all-429 path wastes 120s right before
                # raising the GdeltFetchError.
                if attempt < max_retries - 1:
                    _idx = min(
                        attempt, len(_GDELT_429_BACKOFF_SCHEDULE_S) - 1,
                    )
                    _time_mod.sleep(_GDELT_429_BACKOFF_SCHEDULE_S[_idx])
                continue
            last_err = f"HTTP {status}"
            break
        except Exception as e:
            last_err = str(e)
            # Same final-attempt skip as the 429 branch.
            if attempt < max_retries - 1:
                _idx = min(
                    attempt, len(_GDELT_429_BACKOFF_SCHEDULE_S) - 1,
                )
                _time_mod.sleep(_GDELT_429_BACKOFF_SCHEDULE_S[_idx])
            continue
    raise GdeltFetchError(
        f"asset={asset} window=[{start_dt.isoformat()}, {end_dt.isoformat()}]: {last_err}"
    )


# ── Aggregation ────────────────────────────────────────────────────────

def summarize_articles(articles: List[Dict]) -> Tuple[int, Optional[float]]:
    """Reduce a GDELT article list → (count, avg_tone).

    Articles missing or with non-parseable `tone` are still counted in
    the article count (matches GDELT's own reporting), but excluded from
    the tone mean. Returns (0, None) on empty input."""
    if not articles:
        return 0, None
    n = len(articles)
    tones: List[float] = []
    for a in articles:
        if not isinstance(a, dict):
            continue
        # GDELT field name for tone in Doc API ArtList JSON varies across
        # response shapes / API versions. Round 3 fix #3: try the known
        # spellings in order. Operator validates against real API later;
        # tests/fixtures/gdelt_artlist_sample.json captures the shape we
        # currently believe is correct.
        raw = a.get("tone")
        if raw is None:
            raw = a.get("documenttone")
        if raw is None:
            raw = a.get("docTone")
        if raw is None:
            continue
        try:
            t = float(raw)
        except (TypeError, ValueError):
            continue
        if t < GDELT_TONE_MIN or t > GDELT_TONE_MAX:
            continue
        tones.append(t)
    if not tones:
        return n, None
    return n, sum(tones) / len(tones)


# ── Backfill driver ────────────────────────────────────────────────────

def _ensure_local_columns(conn: sqlite3.Connection) -> None:
    """Phase H-4a adds two columns to local SQLite. Idempotent
    ALTER TABLE — no-op if already present. Mirrors bot.py's migration
    pattern (try/except OperationalError on "duplicate column")."""
    for col, typ in (
        ("gdelt_event_count_1h_pre_decision", "INTEGER"),
        ("gdelt_avg_tone_1h_pre_decision", "REAL"),
    ):
        try:
            conn.execute(
                f"ALTER TABLE evaluated_opportunities ADD COLUMN {col} {typ}"
            )
        except sqlite3.OperationalError:
            pass
    # Round 4 cross-cutting (MEDIUM): defensively ensure data_provenance
    # column exists so the COALESCE stamp does not fail on a DB where
    # G-6 has not run.
    try:
        conn.execute(
            "ALTER TABLE evaluated_opportunities ADD COLUMN data_provenance TEXT"
        )
    except sqlite3.OperationalError:
        pass
    conn.commit()


def _bucket_key(eval_iso: str, asset: str) -> Optional[Tuple[str, int, str]]:
    """Map (evaluation_time_iso, asset) → cache bucket key.

    Bucket = (date_iso, hour_int, asset) where (date_iso, hour_int)
    identify the eval row's hour. All rows whose evaluation_time falls
    in the SAME hour share one GDELT API call AND the SAME values.

    Round 4 fix #1 (HIGH) re-narrowed the WINDOW (in `_bucket_window`)
    to the strictly-prior hour to eliminate future-of-decision leakage.
    The bucket KEY still names the eval hour (so two evals at 14:01 +
    14:59 share the same bucket); the bucket WINDOW for that key is
    [13:00, 14:00] — the full prior hour.

    Returns None on parse failure (caller should skip the row)."""
    try:
        dt = datetime.datetime.fromisoformat(eval_iso.replace("Z", "+00:00"))
    except Exception:
        return None
    return (dt.date().isoformat(), dt.hour, asset.upper())


def _bucket_window(
    bucket_key: Tuple[str, int, str],
) -> Tuple[datetime.datetime, datetime.datetime]:
    """Return the [hour-1:00, hour:00] UTC window for a bucket key.

    Round 4 fix #1 (HIGH): the window is now the STRICTLY-PRIOR hour
    — no leakage of future-of-decision data. Round 3's fix changed the
    window to [hour:00, hour+1:00] (the hour CONTAINING the eval),
    which leaked up to 59 minutes of post-decision news. For an eval at
    HH:01, the [HH:00, HH+1:00] window included 59 min of post-decision
    articles — a real leakage source for v2 training.

    The new contract: feature semantics are "the full hour BEFORE the
    eval's hour" — lossy at the trailing edge by up to 60 min (an eval
    at HH:01 sees only news from [HH-1:00, HH:00], missing 1 min of
    pre-decision coverage in the eval hour itself), but ZERO leakage.
    v2 sees one consistent definition.

    Tested by `test_bucket_window_excludes_future_data`."""
    date_iso, hour_int, _asset = bucket_key
    y, m, d = (int(x) for x in date_iso.split("-"))
    eval_hour_start = datetime.datetime(
        y, m, d, hour_int, 0, 0, tzinfo=datetime.timezone.utc,
    )
    start = eval_hour_start - datetime.timedelta(hours=1)
    end = eval_hour_start
    return start, end


def backfill_gdelt(
    conn: sqlite3.Connection,
    fetcher: Optional[Callable] = None,
    batch_size: int = 50,
    sleep_ms: int = GDELT_DEFAULT_SLEEP_MS,
    checkpoint_dir: Optional[str] = None,
    dry_run: bool = False,
    only_15m: bool = True,
) -> int:
    """H-4a backfill: per-row GDELT event count + avg tone over a
    1-hour pre-decision window.

    `fetcher(asset, start_dt, end_dt) → List[article_dict]` is injected
    for tests. Production default: `fetch_gdelt_articles`.

    `sleep_ms` clamps to ≥1000 (GDELT free-tier ceiling). Set lower in
    tests via dependency injection of `fetcher`.

    `only_15m`: scope to rows with `product_type='15m'` — these are the
    ones v2 calibrator will train on. Other product_type rows aren't
    affected.

    Returns total rows updated."""
    if fetcher is None:
        fetcher = fetch_gdelt_articles
    # Defense in depth: never let an aggressive caller smash GDELT.
    if sleep_ms < 1000 and fetcher is fetch_gdelt_articles:
        logger.info(
            "h4a: bumping sleep_ms %d → 1000 (GDELT rate-limit floor)",
            sleep_ms,
        )
        sleep_ms = 1000

    _ensure_local_columns(conn)

    # Per-(date,hour,asset) cache. Each entry is (count, avg_tone)
    # OR the sentinel string "__ABORT__" on per-bucket fetch failure
    # — we propagate failure UPWARDS rather than letting it look like a
    # 0-article hour. The abort sentinel lets us detect cross-row
    # propagation in tests.
    cache: Dict[Tuple[str, int, str], Tuple[int, Optional[float]]] = {}

    # Track per-(asset, day) zero-article streaks. If an entire day
    # for an asset returns zero articles across all hour buckets touched,
    # that's almost certainly an API misconfiguration (silent empty),
    # not a real news drought — abort.
    per_day_hits: Dict[Tuple[str, str], int] = {}
    per_day_attempts: Dict[Tuple[str, str], int] = {}

    last_id = (
        read_checkpoint(checkpoint_dir, "h4a_gdelt") if checkpoint_dir else 0
    )
    total = 0

    while True:
        sql = (
            "SELECT id, asset, evaluation_time FROM evaluated_opportunities "
            "WHERE id > ? AND gdelt_event_count_1h_pre_decision IS NULL "
            "AND evaluation_time IS NOT NULL "
            "AND asset IS NOT NULL "
        )
        if only_15m:
            sql += "AND product_type = '15m' "
        sql += "ORDER BY id LIMIT ?"
        rows = conn.execute(sql, (last_id, batch_size)).fetchall()
        if not rows:
            break

        for r in rows:
            asset = (r["asset"] or "").upper()
            if asset not in GDELT_ASSET_QUERIES:
                # Not in our coverage map (e.g., a future-asset row);
                # advance checkpoint but don't write.
                last_id = r["id"]
                continue
            key = _bucket_key(r["evaluation_time"], asset)
            if key is None:
                last_id = r["id"]
                continue
            day_key = (key[0], asset)
            per_day_attempts[day_key] = per_day_attempts.get(day_key, 0) + 1

            if key in cache:
                count, tone = cache[key]
            else:
                # Round 3 fix #1: fetch the full hour window [hour:00,
                # hour+1:00] so every row in this bucket sees IDENTICAL
                # data (was: per-row [eval-60min, eval] which only
                # matched the first row's window).
                start_dt, end_dt = _bucket_window(key)
                try:
                    arts = fetcher(asset, start_dt, end_dt)
                except GdeltFetchError as e:
                    # Per-bucket fetch failure — propagate as abort.
                    raise GdeltFetchError(
                        f"h4a: bucket {key} fetch failed: {e}"
                    )
                count, tone = summarize_articles(arts)
                cache[key] = (count, tone)
                if sleep_ms > 0 and not dry_run:
                    _time_mod.sleep(sleep_ms / 1000.0)

            if count > 0:
                per_day_hits[day_key] = per_day_hits.get(day_key, 0) + 1

            if not dry_run:
                # Round 4 cross-cutting (MEDIUM): stamp data_provenance
                # so v2 trainer can downweight/exclude these rows.
                # COALESCE preserves any pre-existing
                # 'backfill_60s_inputs' / 'backfill_glassnode_daily' /
                # 'live_ws' value; H-4a only sets when NULL.
                conn.execute(
                    "UPDATE evaluated_opportunities SET "
                    "gdelt_event_count_1h_pre_decision = ?, "
                    "gdelt_avg_tone_1h_pre_decision = ?, "
                    "data_provenance = COALESCE(data_provenance, "
                    "'backfill_gdelt_hour_bucket') "
                    "WHERE id = ?",
                    (int(count), tone, r["id"]),
                )
            total += 1
            last_id = r["id"]

        if not dry_run:
            conn.commit()
        if checkpoint_dir:
            write_checkpoint(checkpoint_dir, "h4a_gdelt", last_id)
        if len(rows) < batch_size:
            break

    # Post-pass abort: if any (asset, day) attempted >= 6 buckets but
    # got ZERO hits, treat as silent-empty failure. Threshold of 6
    # avoids false-positives on legitimately quiet hours.
    for day_key, attempts in per_day_attempts.items():
        hits = per_day_hits.get(day_key, 0)
        if attempts >= 6 and hits == 0:
            raise GdeltFetchError(
                f"h4a: zero data for asset={day_key[1]} day={day_key[0]} "
                f"across {attempts} buckets — refusing to leave NULL silently. "
                f"Rerun when GDELT API is healthy."
            )

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
    # stuck loop, DNS hang, or DB lock. The wrapper has its own
    # SIGTERM/SIGHUP handling; this is the in-script defense in depth.
    from _h4_runtime_safety import install_hard_timeout
    install_hard_timeout(label="h4a_gdelt")

    parser = argparse.ArgumentParser(
        description="Phase H-4a — GDELT news event cluster backfill"
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
        "--sleep-ms", type=int, default=GDELT_DEFAULT_SLEEP_MS,
        help=(
            "sleep between API calls (default 1100; clamped to ≥1000 "
            "when using the real GDELT fetcher)"
        ),
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="walk rows + buckets but skip UPDATE",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )
    conn = _connect(args.db)
    n = backfill_gdelt(
        conn,
        batch_size=args.batch_size,
        sleep_ms=args.sleep_ms,
        checkpoint_dir=args.checkpoint_dir,
        dry_run=args.dry_run,
    )
    logger.info("h4a: updated %d rows (dry_run=%s)", n, args.dry_run)
    return 0


if __name__ == "__main__":
    sys.exit(main())
