#!/usr/bin/env python3
"""Phase H-4c backfill: CryptoCompare news sentiment → evaluated_opportunities.

One new column:

    news_sentiment_score_1h_pre_decision   REAL  (mean of mapped sentiment,
                                                  range [-1.0, +1.0]; NULL when
                                                  no articles in 1h window)

Source: https://min-api.cryptocompare.com/data/v2/news/?lang=EN&categories=...
Free tier: 250K calls/month — well above what the backfill needs.
No API key required for news endpoint.

Sentiment mapping (CryptoCompare returns categorical labels):
    POSITIVE → +1.0
    NEUTRAL  →  0.0
    NEGATIVE → -1.0

Average is in [-1.0, +1.0]. NULL is the honest answer when zero articles
are tagged for the asset in the 1h window — DO NOT synthesize 0.0
(which would conflate "no news" with "balanced news").

Mirrors G-2/G-4/G-5 idempotency + checkpoint pattern.

Usage:
  python3 scripts/backfill/cryptocompare_news_backfill.py --db state.db
  python3 scripts/backfill/cryptocompare_news_backfill.py --db state.db --dry-run

Design doc: kb/decisions/phase-h4c-cryptocompare-may02.md.
Master plan: kb/decisions/shadow-coverage-phase-h-data-recovery-may02.md.
"""

import argparse
import datetime
import logging
import os
import sqlite3
import sys
import time as _time_mod
from typing import Callable, Dict, List, Optional, Tuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from shadow_coverage_backfill import read_checkpoint, write_checkpoint  # noqa: E402

logger = logging.getLogger(__name__)

CRYPTOCOMPARE_NEWS_URL = "https://min-api.cryptocompare.com/data/v2/news/"

# Asset → CryptoCompare news category. CC uses uppercase tags per coin.
CRYPTOCOMPARE_ASSET_CATEGORIES: Dict[str, str] = {
    "BTC": "BTC",
    "ETH": "ETH",
    "SOL": "SOL",
    "XRP": "XRP",
}

# Sentiment label → numeric mapping. CC returns label as string; we
# normalize to upper and trim. Unrecognized labels → contribute 0
# articles (caller decides NULL vs 0 mean).
SENTIMENT_MAP = {
    "POSITIVE": 1.0,
    "NEUTRAL": 0.0,
    "NEGATIVE": -1.0,
}

# Free-tier rate limit: 50 calls/sec is the documented ceiling for
# unauthenticated min-api calls. We keep a comfortable buffer.
CRYPTOCOMPARE_DEFAULT_SLEEP_MS = 200

DEFAULT_LOOKBACK_MIN = 60

# Round 3 fix #12: CryptoCompare relabels article sentiment for a window
# after publication (typically minutes-hours). Backfilling rows whose
# evaluation_time is within this relabel window means we cache the
# sentiment BEFORE it stabilizes — and then the cache is "done" and we
# never re-pull.
#
# Round 4 fix #2 (MEDIUM): default bumped 2 → 24 hours. The 2h default
# was unverified — typical news label settle windows run several hours
# to a day, and a too-short cutoff would silently freeze unstable
# labels in the DB. 24h is a conservative ceiling that matches typical
# news-label maturity. Operator can override via --min-eval-age-hours
# if they have evidence the relabel window is shorter for their data.
DEFAULT_MIN_EVAL_AGE_HOURS = 24


class CryptoCompareFetchError(Exception):
    """Raised when ALL retries are exhausted on the CryptoCompare news API.

    Distinguishes 'API call failed' from 'API succeeded but window has
    no articles' — the latter is a legitimate `Data=[]`."""


# ── Network ────────────────────────────────────────────────────────────

def fetch_cryptocompare_news(
    asset: str,
    end_dt: datetime.datetime,
    *,
    request_fn: Optional[Callable] = None,
    max_retries: int = 3,
) -> List[Dict]:
    """Fetch a page of CryptoCompare news for `asset`, with `lTs=end_dt`
    (the API's "before this timestamp" cursor — returns up to 50
    articles older than `lTs`, newest first).

    The 1-hour window filter is applied by the caller (we may need to
    paginate further back if the first page doesn't reach the start of
    the window). Returns the raw list of article dicts (possibly empty).

    Raises `CryptoCompareFetchError` on final failure across `max_retries`."""
    if request_fn is None:
        import requests as _requests

        def request_fn(url, params, timeout):
            return _requests.get(url, params=params, timeout=timeout)

    cat = CRYPTOCOMPARE_ASSET_CATEGORIES.get(asset)
    if cat is None:
        return []

    params = {
        "lang": "EN",
        "categories": cat,
        "lTs": int(end_dt.timestamp()),
    }
    # CryptoCompare (now CoinDesk Indices) requires an API key on the news
    # endpoint as of late 2024 — without it the API returns "You need a
    # valid auth key or api key to access this endpoint". The key is read
    # from CRYPTOCOMPARE_API_KEY env var (lives in the workflow secret /
    # VPS .env). If unset we still call without it so the original
    # auth-error surfaces clearly in the wrapper's Telegram alert.
    api_key = os.environ.get("CRYPTOCOMPARE_API_KEY", "").strip()
    if api_key:
        params["api_key"] = api_key
    last_err: Optional[str] = None
    for attempt in range(max_retries):
        try:
            resp = request_fn(CRYPTOCOMPARE_NEWS_URL, params, 30)
            status = getattr(resp, "status_code", 0)
            if status == 200:
                try:
                    body = resp.json()
                except Exception as e:
                    last_err = f"non-json body: {e}"
                    break
                if not isinstance(body, dict):
                    last_err = f"unexpected body type: {type(body).__name__}"
                    break
                # CC's success response: {"Type": 100, "Data": [...]}.
                # Failure: {"Response": "Error", "Message": "..."}.
                if str(body.get("Response", "")).lower() == "error":
                    last_err = (
                        "CC error: " + str(body.get("Message", ""))
                    )
                    break
                data = body.get("Data") or []
                if not isinstance(data, list):
                    last_err = "Data field not a list"
                    break
                return data
            if status == 429:
                _time_mod.sleep(2 ** attempt)
                last_err = "HTTP 429 (rate limit)"
                continue
            last_err = f"HTTP {status}"
            break
        except Exception as e:
            last_err = str(e)
            _time_mod.sleep(2 ** attempt)
            continue
    raise CryptoCompareFetchError(
        f"asset={asset} end={end_dt.isoformat()}: {last_err}"
    )


# ── Aggregation ────────────────────────────────────────────────────────

def filter_articles_to_window(
    articles: List[Dict],
    start_dt: datetime.datetime,
    end_dt: datetime.datetime,
) -> List[Dict]:
    """Keep only articles with `published_on` in [start_dt, end_dt].

    `published_on` is a Unix epoch second (CC's standard field name).
    Articles missing the field are dropped."""
    start_ts = int(start_dt.timestamp())
    end_ts = int(end_dt.timestamp())
    out: List[Dict] = []
    for a in articles or []:
        if not isinstance(a, dict):
            continue
        try:
            pub = int(a.get("published_on") or 0)
        except (TypeError, ValueError):
            continue
        if start_ts <= pub <= end_ts:
            out.append(a)
    return out


def summarize_sentiment(articles: List[Dict]) -> Tuple[int, Optional[float]]:
    """Reduce articles → (count_with_sentiment, mean_sentiment).

    Maps each article's `sentiment` field via SENTIMENT_MAP.
    Articles without a recognized label do NOT contribute to either the
    count or the mean (silent — we want the score to reflect signal,
    not noise from unmapped labels). Returns (0, None) on empty input
    OR when no articles had a recognized label."""
    if not articles:
        return 0, None
    scores: List[float] = []
    for a in articles:
        if not isinstance(a, dict):
            continue
        label = str(a.get("sentiment") or "").strip().upper()
        if label in SENTIMENT_MAP:
            scores.append(SENTIMENT_MAP[label])
    if not scores:
        return 0, None
    return len(scores), sum(scores) / len(scores)


# ── Backfill driver ────────────────────────────────────────────────────

def _ensure_local_columns(conn: sqlite3.Connection) -> None:
    # Pre-check via PRAGMA (see h4-backfill-bugs-may04.md). The previous
    # `try: ALTER; except OperationalError: pass` pattern swallowed
    # `database is locked` the same way it swallowed `duplicate column`,
    # which under VPS lock contention left the column missing and the
    # next SELECT crashed with `no such column`.
    existing = {
        row[1] for row in conn.execute(
            "PRAGMA table_info(evaluated_opportunities)"
        ).fetchall()
    }
    needed = [
        ("news_sentiment_score_1h_pre_decision", "REAL"),
        # G-6 normally adds data_provenance; H-4c stamps it via COALESCE.
        ("data_provenance", "TEXT"),
    ]
    for col, typ in needed:
        if col in existing:
            continue
        conn.execute(
            f"ALTER TABLE evaluated_opportunities ADD COLUMN {col} {typ}"
        )
    conn.commit()


def _bucket_key(eval_iso: str, asset: str) -> Optional[Tuple[str, int, str]]:
    """(date_iso, hour_int, asset_upper) bucket — same convention as H-4a.

    Round 4 fix #1 (HIGH) re-narrowed the WINDOW to the strictly-prior
    hour to eliminate future-of-decision leakage. The bucket KEY still
    names the eval hour (so two evals at 14:01 and 14:59 share a
    bucket); the window for that key is [13:00, 14:00]."""
    try:
        dt = datetime.datetime.fromisoformat(eval_iso.replace("Z", "+00:00"))
    except Exception:
        return None
    return (dt.date().isoformat(), dt.hour, asset.upper())


def _bucket_window(
    bucket_key: Tuple[str, int, str],
) -> Tuple[datetime.datetime, datetime.datetime]:
    """[hour-1:00, hour:00] UTC window for a bucket key.

    Round 4 fix #1 (HIGH): the window is now the STRICTLY-PRIOR hour —
    no leakage of future-of-decision data. Round 3's [hour:00,
    hour+1:00] window included up to 59 minutes of post-decision news
    (an eval at HH:01 saw the entire HH:01-HH+1:00 future). The new
    contract: feature semantics are "the full hour BEFORE the eval's
    hour" — lossy at the trailing edge by up to 60 min, but ZERO
    leakage. Mirrors H-4a `_bucket_window`.

    Tested by `test_bucket_window_excludes_future_data`."""
    date_iso, hour_int, _asset = bucket_key
    y, m, d = (int(x) for x in date_iso.split("-"))
    eval_hour_start = datetime.datetime(
        y, m, d, hour_int, 0, 0, tzinfo=datetime.timezone.utc,
    )
    start = eval_hour_start - datetime.timedelta(hours=1)
    end = eval_hour_start
    return start, end


def backfill_cryptocompare(
    conn: sqlite3.Connection,
    fetcher: Optional[Callable] = None,
    batch_size: int = 50,
    sleep_ms: int = CRYPTOCOMPARE_DEFAULT_SLEEP_MS,
    checkpoint_dir: Optional[str] = None,
    dry_run: bool = False,
    only_15m: bool = True,
    min_eval_age_hours: float = DEFAULT_MIN_EVAL_AGE_HOURS,
    now_fn: Optional[Callable[[], datetime.datetime]] = None,
) -> int:
    """H-4c backfill: per-row CryptoCompare news sentiment over 1h
    pre-decision window.

    `fetcher(asset, end_dt) → List[article_dict]` injected for tests.
    Production default: `fetch_cryptocompare_news`.

    Per-(date,hour,asset) bucket cache amortizes the API call cost.
    Aborts on per-(asset, day) zero-data spans (≥6 buckets attempted,
    0 hits) — same pattern as H-4a.

    Round 3 fix #12: rows whose `evaluation_time` is within
    `min_eval_age_hours` of NOW are SKIPPED (not aborted, not stamped) —
    CryptoCompare relabels article sentiment after publication; reading
    the unstable label and caching it would freeze a wrong value
    forever. Operator re-runs after the relabel window closes.
    `now_fn` is injected for tests."""
    if fetcher is None:
        fetcher = fetch_cryptocompare_news
    if now_fn is None:
        now_fn = lambda: datetime.datetime.now(datetime.timezone.utc)

    _ensure_local_columns(conn)
    cutoff_dt = now_fn() - datetime.timedelta(hours=min_eval_age_hours)

    cache: Dict[Tuple[str, int, str], Optional[float]] = {}
    per_day_attempts: Dict[Tuple[str, str], int] = {}
    per_day_hits: Dict[Tuple[str, str], int] = {}

    last_id = (
        read_checkpoint(checkpoint_dir, "h4c_cc_news") if checkpoint_dir else 0
    )
    total = 0

    while True:
        sql = (
            "SELECT id, asset, evaluation_time FROM evaluated_opportunities "
            "WHERE id > ? AND news_sentiment_score_1h_pre_decision IS NULL "
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
            if asset not in CRYPTOCOMPARE_ASSET_CATEGORIES:
                last_id = r["id"]
                continue
            # Round 3 fix #12: skip rows still inside the CC relabel
            # window. Do NOT advance `last_id` past them — we want the
            # next run (after the relabel window closes) to see them.
            try:
                row_eval_dt = datetime.datetime.fromisoformat(
                    r["evaluation_time"].replace("Z", "+00:00")
                )
            except Exception:
                last_id = r["id"]
                continue
            if row_eval_dt > cutoff_dt:
                # Skip individual row WITHOUT advancing checkpoint past
                # it — subsequent runs (after the relabel window closes)
                # will re-attempt. The id-ordered query naturally re-
                # discovers it because the column is still NULL.
                logger.info(
                    "h4c: row id=%s eval_time=%s within %sh relabel "
                    "window — skipping (will retry on next run)",
                    r["id"], r["evaluation_time"], min_eval_age_hours,
                )
                # Stop the batch: rows are scanned id-asc and ids
                # generally correlate with eval_time-asc; bailing avoids
                # leaving a "checkpoint hole" where later-id older rows
                # get processed and the in-window row never gets retried
                # by virtue of the checkpoint advancing past it.
                if not dry_run:
                    conn.commit()
                return total
            key = _bucket_key(r["evaluation_time"], asset)
            if key is None:
                last_id = r["id"]
                continue
            day_key = (key[0], asset)
            per_day_attempts[day_key] = per_day_attempts.get(day_key, 0) + 1

            if key in cache:
                score = cache[key]
            else:
                # Round 3 fix #10: use bucket window [hour:00, hour+1:00]
                # so all rows in the bucket get IDENTICAL sentiment.
                start_dt, end_dt = _bucket_window(key)
                try:
                    raw = fetcher(asset, end_dt)
                except CryptoCompareFetchError as e:
                    raise CryptoCompareFetchError(
                        f"h4c: bucket {key} fetch failed: {e}"
                    )
                in_window = filter_articles_to_window(raw, start_dt, end_dt)
                # Round 3 fix #13: CC `lTs` returns up to 50 articles
                # older than the cursor. If we got 50 AND all 50 are in
                # the window, the next-older article is also potentially
                # in-window — we'd be silently truncating. Warn loudly so
                # operators can decide whether to add pagination.
                if len(raw) >= 50 and len(in_window) >= 50:
                    logger.warning(
                        "h4c: bucket %s returned %d articles (CC page "
                        "limit 50, all in-window) — hour may be truncated",
                        key, len(in_window),
                    )
                _, score = summarize_sentiment(in_window)
                cache[key] = score
                if sleep_ms > 0 and not dry_run:
                    _time_mod.sleep(sleep_ms / 1000.0)

            if score is not None:
                per_day_hits[day_key] = per_day_hits.get(day_key, 0) + 1

            if not dry_run:
                # Round 4 cross-cutting (MEDIUM): stamp data_provenance
                # so v2 trainer can downweight/exclude these rows.
                # COALESCE preserves any pre-existing provenance value.
                conn.execute(
                    "UPDATE evaluated_opportunities SET "
                    "news_sentiment_score_1h_pre_decision = ?, "
                    "data_provenance = COALESCE(data_provenance, "
                    "'backfill_cc_hour_bucket') "
                    "WHERE id = ?",
                    (score, r["id"]),
                )
            total += 1
            last_id = r["id"]
        if not dry_run:
            conn.commit()
        if checkpoint_dir:
            write_checkpoint(checkpoint_dir, "h4c_cc_news", last_id)
        if len(rows) < batch_size:
            break

    # Post-pass abort: same pattern as H-4a.
    for day_key, attempts in per_day_attempts.items():
        hits = per_day_hits.get(day_key, 0)
        if attempts >= 6 and hits == 0:
            raise CryptoCompareFetchError(
                f"h4c: zero data for asset={day_key[1]} day={day_key[0]} "
                f"across {attempts} buckets — refusing to leave NULL silently. "
                f"Rerun when CryptoCompare API is healthy."
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
    # alarm so this script self-kills at the ceiling. CRITICAL for this
    # script specifically — the May 3 incident's 2h42m orphan was
    # exactly an instance of THIS script. The wrapper's signal handling
    # would have reaped earlier if SIGTERM had reached us; this alarm
    # is the in-script defense regardless of upstream signals.
    from _h4_runtime_safety import install_hard_timeout
    install_hard_timeout(label="h4c_cc_news")

    parser = argparse.ArgumentParser(
        description="Phase H-4c — CryptoCompare news sentiment backfill"
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
        "--sleep-ms", type=int, default=CRYPTOCOMPARE_DEFAULT_SLEEP_MS,
        help="sleep between API calls (default 200)",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="walk rows but skip UPDATE",
    )
    parser.add_argument(
        "--min-eval-age-hours", type=float,
        default=DEFAULT_MIN_EVAL_AGE_HOURS,
        help=(
            "refuse to process rows whose evaluation_time is within the "
            "last N hours (CryptoCompare relabel window; default 24 — "
            "Round 4 fix #2 bumped from 2 → 24 because typical news "
            "label settle windows are several hours, and a too-short "
            "cutoff would freeze unstable labels)"
        ),
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )
    conn = _connect(args.db)
    n = backfill_cryptocompare(
        conn,
        batch_size=args.batch_size,
        sleep_ms=args.sleep_ms,
        checkpoint_dir=args.checkpoint_dir,
        dry_run=args.dry_run,
        min_eval_age_hours=args.min_eval_age_hours,
    )
    logger.info("h4c: updated %d rows (dry_run=%s)", n, args.dry_run)
    return 0


if __name__ == "__main__":
    sys.exit(main())
