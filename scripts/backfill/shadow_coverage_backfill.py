#!/usr/bin/env python3
"""Shadow-coverage backfill harness.

Backfills Phase B columns on historical `evaluated_opportunities` rows
that pre-date the live-capture path. Phase G covers what is derivable
from existing DB state OR public APIs:

  G-1 (this commit, SQL-only — no API):
    - `final_spot_price` = `spot_price` (same value)
    - `maker_price_cents` + `maker_depth_at_post` from yes_bid_cents +
      market_price + (optionally) orderbook_levels_json
    - `recent_n_outcome_streak` from settled_trades up to row.evaluation_time
    - `time_since_last_fill_s` from MAX over positions.opened_at +
      settled_trades.settled_at, both with timestamp <= row.evaluation_time

  G-2/G-3/G-4/G-5: separate commits — Coinbase candles, cal_mlp annotation,
  path metrics, OKX/Deribit funding.

Design:
  - Each phase is an idempotent, batched, resumable function.
  - WHERE clauses use `WHERE <col> IS NULL` so re-runs skip already-done rows.
  - Checkpoint files in `--checkpoint-dir` (default `data/backfill_ckpt/`)
    persist last-processed `id` per phase so a long-running backfill
    crashing mid-stream resumes cleanly.
  - Default 50 rows/batch + 50ms inter-batch sleep — safe to run against
    the live DB (CLAUDE.md "≤50 rows per commit" rule). Override via
    `--batch-size` / `--sleep-ms` if running offline.

Usage:
  python3 scripts/backfill/shadow_coverage_backfill.py --phase final_spot --db state.db
  python3 scripts/backfill/shadow_coverage_backfill.py --phase maker --db state.db
  python3 scripts/backfill/shadow_coverage_backfill.py --phase streak --db state.db
  python3 scripts/backfill/shadow_coverage_backfill.py --phase tslf --db state.db
  python3 scripts/backfill/shadow_coverage_backfill.py --phase all --db state.db

Master plan: kb/decisions/shadow-coverage-expansion-may01.md.
"""

import argparse
import bisect
import datetime
import json
import logging
import os
import sqlite3
import sys
import time as _time_mod
from typing import Callable, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


# ── Checkpoint helpers ─────────────────────────────────────────────────

def read_checkpoint(checkpoint_dir: str, phase: str) -> int:
    """Return last-processed row id for `phase`, or 0 if no checkpoint."""
    path = os.path.join(checkpoint_dir, f"{phase}.last_id")
    if not os.path.exists(path):
        return 0
    try:
        with open(path) as f:
            return int((f.read() or "0").strip())
    except Exception:
        return 0


def write_checkpoint(checkpoint_dir: str, phase: str, last_id: int) -> None:
    os.makedirs(checkpoint_dir, exist_ok=True)
    path = os.path.join(checkpoint_dir, f"{phase}.last_id")
    with open(path, "w") as f:
        f.write(str(last_id))


# ── Phase G-1a: final_spot_price ───────────────────────────────────────

def backfill_final_spot_price(
    conn: sqlite3.Connection,
    batch_size: int = 50,
    sleep_ms: int = 0,
    checkpoint_dir: Optional[str] = None,
) -> int:
    """Copy `spot_price` → `final_spot_price` on rows where final is NULL
    and spot is not. Batched per CLAUDE.md "≤50 rows per commit" to
    avoid lock contention with the live bot. Returns total count."""
    import time as _time
    last_id = read_checkpoint(checkpoint_dir, "g1_final_spot") if checkpoint_dir else 0
    total = 0
    while True:
        rows = conn.execute(
            "SELECT id FROM evaluated_opportunities "
            "WHERE id > ? AND final_spot_price IS NULL AND spot_price IS NOT NULL "
            "ORDER BY id LIMIT ?",
            (last_id, batch_size),
        ).fetchall()
        if not rows:
            break
        ids = [r["id"] for r in rows]
        # Range UPDATE — bounded to this batch's id range. id is INTEGER
        # PRIMARY KEY so range scans are O(batch_size).
        placeholders = ",".join("?" * len(ids))
        cur = conn.execute(
            f"UPDATE evaluated_opportunities "
            f"SET final_spot_price = spot_price "
            f"WHERE id IN ({placeholders})",
            ids,
        )
        conn.commit()
        total += cur.rowcount
        last_id = ids[-1]
        if checkpoint_dir:
            write_checkpoint(checkpoint_dir, "g1_final_spot", last_id)
        if sleep_ms > 0:
            _time.sleep(sleep_ms / 1000.0)
        if len(rows) < batch_size:
            break
    return total


# ── Phase G-1b: maker counterfactual ───────────────────────────────────

def _parse_depth_at_price(ladder_json: Optional[str], price_cents: int) -> Optional[int]:
    """Sum of yes-bid qty at `price_cents` in the ladder, 0 if level absent.
    Returns None if ladder is missing or unparseable — distinguishes
    "no data" from "data shows level absent" (Phase G-1 round 1 MEDIUM
    fix; was previously conflating both as 0).

    Differs from `OpportunityScanner._compute_maker_counterfactual` in
    bot.py which always returns 0 on parse-failure: that's safer at scan
    time (live row has the bid+ask anyway, so 0 means "no ladder snapshot
    at this tick"); the backfill operates on historical rows where parse-
    failure could mask a Kalshi schema-drift incident, so NULL is honest.
    """
    if not ladder_json:
        return None
    try:
        ladder = json.loads(ladder_json)
    except Exception:
        return None
    depth = 0
    for entry in (ladder.get("yes_bids") or []):
        if isinstance(entry, (list, tuple)) and len(entry) >= 2:
            try:
                if int(entry[0]) == price_cents:
                    depth += int(entry[1])
            except (TypeError, ValueError):
                continue
    return depth


def ensure_indexes(conn: sqlite3.Connection) -> None:
    """Phase G-1 round 1 CRITICAL fix: without these indexes the streak
    + tslf per-row queries do full-table scans on settled_trades. With
    81K + 60K backfill rows, that's catastrophic O(N²).

    Phase G-1 round 2 HIGH fix: only builds INDEXES THAT DON'T ALREADY
    EXIST (checks sqlite_master first), so re-runs are no-op. The
    initial build, however, holds the writer for 1-2s on settled_trades
    (~30K rows) and ~1s on positions (small) — the live bot's 10s
    busy_timeout absorbs this comfortably. We do NOT add an index on
    evaluated_opportunities (142K rows would block writers ~30-60s).

    Recommended operator window: any time except the 5s after a 15M
    settlement burst. Idempotent."""
    existing = {
        r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index'"
        ).fetchall()
    }
    plan = [
        ("idx_st_settled_at", "settled_trades(settled_at)"),
        ("idx_pos_opened_at", "positions(opened_at)"),
    ]
    for name, target in plan:
        if name in existing:
            logger.info("index %s already present — skipping", name)
            continue
        logger.info("creating index %s ON %s (may hold writer briefly)", name, target)
        conn.execute(f"CREATE INDEX IF NOT EXISTS {name} ON {target}")
    conn.commit()


def backfill_maker_counterfactual(
    conn: sqlite3.Connection,
    batch_size: int = 50,
    sleep_ms: int = 0,
    checkpoint_dir: Optional[str] = None,
) -> int:
    """Compute maker_price_cents + maker_depth_at_post per row from
    yes_bid_cents + market_price + orderbook_levels_json. Skips rows
    that already have a maker_price_cents value (live-captured)."""
    import time as _time
    last_id = read_checkpoint(checkpoint_dir, "g1_maker") if checkpoint_dir else 0
    total = 0
    while True:
        rows = conn.execute(
            "SELECT id, yes_bid_cents, market_price, orderbook_levels_json "
            "FROM evaluated_opportunities "
            "WHERE id > ? AND maker_price_cents IS NULL "
            "AND maker_depth_at_post IS NULL "
            "AND yes_bid_cents IS NOT NULL "
            "AND market_price IS NOT NULL "
            "ORDER BY id LIMIT ?",
            (last_id, batch_size),
        ).fetchall()
        if not rows:
            break
        for r in rows:
            bid = r["yes_bid_cents"]
            ask = r["market_price"]
            if bid is None or ask is None:
                continue
            maker_price = bid + 1
            if maker_price >= ask:
                # Tight spread — leave NULLs but mark row as processed.
                # We can't write NULL via the WHERE-NULL filter (it's
                # already NULL); the checkpoint advances regardless.
                conn.execute(
                    "UPDATE evaluated_opportunities "
                    "SET maker_price_cents = NULL, maker_depth_at_post = NULL "
                    "WHERE id = ?", (r["id"],),
                )
            else:
                depth = _parse_depth_at_price(r["orderbook_levels_json"], maker_price)
                # depth is None if ladder unparseable (preserves NULL — see
                # _parse_depth_at_price docstring).
                conn.execute(
                    "UPDATE evaluated_opportunities "
                    "SET maker_price_cents = ?, maker_depth_at_post = ? "
                    "WHERE id = ?",
                    (maker_price, depth, r["id"]),
                )
            total += 1
            last_id = r["id"]
        conn.commit()
        if checkpoint_dir:
            write_checkpoint(checkpoint_dir, "g1_maker", last_id)
        if sleep_ms > 0:
            _time.sleep(sleep_ms / 1000.0)
        if len(rows) < batch_size:
            break
    return total


# ── Phase G-1c: recent_n_outcome_streak ────────────────────────────────

def backfill_recent_streak(
    conn: sqlite3.Connection,
    batch_size: int = 50,
    sleep_ms: int = 0,
    checkpoint_dir: Optional[str] = None,
) -> int:
    """Compute recent_n_outcome_streak per row using settled_trades with
    settled_at <= row.evaluation_time. Pushes (net pnl == 0) break the
    streak (treated neutrally). Mirrors `_compute_bot_state_features`
    streak logic in bot.py.

    Caller MUST run `ensure_indexes(conn)` first — without
    idx_st_settled_at, this is a full-table scan per row × 81K rows
    (Phase G-1 round 1 CRITICAL fix)."""
    import time as _time
    last_id = read_checkpoint(checkpoint_dir, "g1_streak") if checkpoint_dir else 0
    total = 0
    while True:
        rows = conn.execute(
            "SELECT id, evaluation_time FROM evaluated_opportunities "
            "WHERE id > ? AND recent_n_outcome_streak IS NULL "
            "ORDER BY id LIMIT ?",
            (last_id, batch_size),
        ).fetchall()
        if not rows:
            break
        for r in rows:
            eval_ts = r["evaluation_time"]
            streak = 0
            try:
                # Last 50 settlements at or before this row's eval time.
                streak_rows = conn.execute(
                    "SELECT pnl_cents - COALESCE(fee_cents, 0) AS net_pnl "
                    "FROM settled_trades "
                    "WHERE settled_at <= ? "
                    "ORDER BY settled_at DESC LIMIT 50",
                    (eval_ts,),
                ).fetchall()
                if streak_rows:
                    first_net = streak_rows[0][0] or 0
                    if first_net != 0:
                        sign = 1 if first_net > 0 else -1
                        for sr in streak_rows:
                            net = sr[0] or 0
                            if (sign > 0 and net > 0) or (sign < 0 and net < 0):
                                streak += sign
                            else:
                                break
            except Exception:
                streak = 0
            conn.execute(
                "UPDATE evaluated_opportunities "
                "SET recent_n_outcome_streak = ? WHERE id = ?",
                (streak, r["id"]),
            )
            total += 1
            last_id = r["id"]
        conn.commit()
        if checkpoint_dir:
            write_checkpoint(checkpoint_dir, "g1_streak", last_id)
        if sleep_ms > 0:
            _time.sleep(sleep_ms / 1000.0)
        if len(rows) < batch_size:
            break
    return total


# ── Phase G-1d: time_since_last_fill_s ─────────────────────────────────

def backfill_tslf(
    conn: sqlite3.Connection,
    batch_size: int = 50,
    sleep_ms: int = 0,
    checkpoint_dir: Optional[str] = None,
) -> int:
    """Compute time_since_last_fill_s per row using MAX over positions.opened_at
    + settled_trades.settled_at, both with timestamp <= row.evaluation_time.
    Mirrors `_compute_bot_state_features` tslf logic in bot.py.

    Caller MUST run `ensure_indexes(conn)` first — Phase G-1 round 1
    CRITICAL fix."""
    import time as _time
    last_id = read_checkpoint(checkpoint_dir, "g1_tslf") if checkpoint_dir else 0
    total = 0
    while True:
        rows = conn.execute(
            "SELECT id, evaluation_time FROM evaluated_opportunities "
            "WHERE id > ? AND time_since_last_fill_s IS NULL "
            "ORDER BY id LIMIT ?",
            (last_id, batch_size),
        ).fetchall()
        if not rows:
            break
        for r in rows:
            eval_ts = r["evaluation_time"]
            try:
                tslf_row = conn.execute(
                    "SELECT (julianday(?) - julianday(MAX(t))) * 86400.0 FROM ("
                    "  SELECT MAX(opened_at) AS t FROM positions WHERE opened_at <= ?"
                    "  UNION ALL"
                    "  SELECT MAX(settled_at) AS t FROM settled_trades WHERE settled_at <= ?"
                    ") WHERE t IS NOT NULL",
                    (eval_ts, eval_ts, eval_ts),
                ).fetchone()
                tslf = None
                if tslf_row and tslf_row[0] is not None:
                    tslf = max(0.0, float(tslf_row[0]))
            except Exception:
                tslf = None
            conn.execute(
                "UPDATE evaluated_opportunities "
                "SET time_since_last_fill_s = ? WHERE id = ?",
                (tslf, r["id"]),
            )
            total += 1
            last_id = r["id"]
        conn.commit()
        if checkpoint_dir:
            write_checkpoint(checkpoint_dir, "g1_tslf", last_id)
        if sleep_ms > 0:
            _time.sleep(sleep_ms / 1000.0)
        if len(rows) < batch_size:
            break
    return total


# ── Phase G-2: Coinbase candles → cross-asset spot at decision ────────

# Map asset → Coinbase product id. ASSETS comes from config but we hardcode
# here to avoid importing bot.py (which loads heavy deps).
COINBASE_PRODUCTS = {
    "BTC": "BTC-USD",
    "ETH": "ETH-USD",
    "SOL": "SOL-USD",
    "XRP": "XRP-USD",
    "DOGE": "DOGE-USD",
    "HYPE": "HYPE-USD",
    "BNB": "BNB-USD",       # T1 onboarding 2026-05-17 (86b9zmj0c) — mirror of bot.constants.COINBASE_PRODUCTS
}

COINBASE_CANDLES_URL = "https://api.exchange.coinbase.com/products/{product_id}/candles"
# Coinbase returns max 300 candles per request. At 1-min granularity that's 5 hours.
COINBASE_MAX_CANDLES_PER_REQUEST = 300
COINBASE_GRANULARITY_SEC = 60  # 1-minute candles


class CoinbaseFetchError(Exception):
    """Raised by fetch_coinbase_candles when ALL retries are exhausted.
    Distinguishes "API failed" from "API succeeded but window has no
    candles" — the latter is a legitimate empty list."""


def fetch_coinbase_candles(
    asset: str,
    start_iso: str,
    end_iso: str,
    *,
    request_fn: Optional[Callable] = None,
    max_retries: int = 3,
) -> List[List]:
    """Fetch 1-minute OHLCV candles for `asset` between [start_iso, end_iso].

    Coinbase returns `[time, low, high, open, close, volume]` per candle.
    Caps at 300 per request — caller paginates.

    Phase G-2 round 1 HIGH fix: retries on 429 (rate limit) with
    exponential backoff (1s, 2s, 4s) up to `max_retries`. Raises
    `CoinbaseFetchError` on final exhaustion or repeated non-200 — caller
    can catch + log + count failures. Distinguishes "API failed" from
    "successful empty response" (the latter is `[]`).

    `request_fn` is the HTTP requestor (defaults to `requests.get`);
    injected for tests."""
    if request_fn is None:
        import requests as _requests
        def request_fn(url, params, timeout):
            return _requests.get(url, params=params, timeout=timeout)
    product_id = COINBASE_PRODUCTS.get(asset)
    if product_id is None:
        return []
    url = COINBASE_CANDLES_URL.format(product_id=product_id)
    params = {
        "start": start_iso, "end": end_iso,
        "granularity": COINBASE_GRANULARITY_SEC,
    }
    last_err = None
    for attempt in range(max_retries):
        try:
            resp = request_fn(url, params, 30)
            status = getattr(resp, "status_code", 0)
            if status == 200:
                return resp.json()
            if status == 429:
                # Rate limited — exponential backoff.
                _time_mod.sleep(2 ** attempt)
                last_err = f"HTTP 429 (rate limit)"
                continue
            # Other non-200: count as a failure but don't infinitely retry.
            last_err = f"HTTP {status}"
            break
        except Exception as e:
            last_err = str(e)
            _time_mod.sleep(2 ** attempt)
            continue
    raise CoinbaseFetchError(
        f"asset={asset} window=[{start_iso}, {end_iso}]: {last_err}"
    )


def build_candle_lookup(candles: List) -> Dict[int, float]:
    """Convert raw Coinbase candle list → {epoch_minute: close_price}.

    Skips malformed entries silently. Coinbase's candle format:
      [time_epoch_sec, low, high, open, close, volume]
    `close` (index 4) is the spot at the END of the minute — the value
    most representative of "the price at minute M:M."
    """
    lookup: Dict[int, float] = {}
    for c in candles:
        if not isinstance(c, (list, tuple)) or len(c) < 5:
            continue
        try:
            ts = int(c[0])
            close = float(c[4])
        except (TypeError, ValueError):
            continue
        # Prefer first-seen on duplicate minutes (Phase G-2 round 1 LOW fix).
        # Coinbase pagination boundaries can re-return the same minute;
        # first-seen avoids cache-race nondeterminism.
        key = ts // 60
        if key not in lookup:
            lookup[key] = close
    return lookup


def lookup_xasset_spots_for_row(
    eval_time_iso: str,
    lookups: Dict[str, Dict[int, float]],
    fallback_minutes: int = 3,
) -> Dict[str, Optional[float]]:
    """Resolve cross-asset spots for a given evaluation_time.

    Returns dict with one `<asset_lower>_spot_at_decision` key per
    `COINBASE_PRODUCTS` entry — 7 keys post-T1 (btc/eth/sol/xrp/hype/doge/bnb).
    If exact minute is missing for an asset, falls back to the nearest
    minute within ±`fallback_minutes`. Beyond that, returns None for that
    asset (staler is dishonest for a minute-grade feature)."""
    try:
        dt = datetime.datetime.fromisoformat(eval_time_iso.replace("Z", "+00:00"))
        epoch_min = int(dt.timestamp()) // 60
    except Exception:
        return {f"{a.lower()}_spot_at_decision": None for a in lookups}

    out: Dict[str, Optional[float]] = {}
    for asset, lookup in lookups.items():
        key = f"{asset.lower()}_spot_at_decision"
        # Exact minute first.
        v = lookup.get(epoch_min)
        if v is not None:
            out[key] = v
            continue
        # ±fallback_minutes search.
        v_found = None
        for delta in range(1, fallback_minutes + 1):
            v_pre = lookup.get(epoch_min - delta)
            v_post = lookup.get(epoch_min + delta)
            if v_pre is not None:
                v_found = v_pre
                break
            if v_post is not None:
                v_found = v_post
                break
        out[key] = v_found
    return out


def backfill_xasset_spots(
    conn: sqlite3.Connection,
    fetcher: Optional[Callable] = None,
    batch_size: int = 50,
    sleep_ms: int = 200,
    checkpoint_dir: Optional[str] = None,
) -> int:
    """Phase G-2 backfill: per-row cross-asset spots from Coinbase candles.

    `fetcher(asset, start_iso, end_iso) → List[candle]` is injected for
    tests. Production default: `fetch_coinbase_candles`.

    Default `sleep_ms=200` (Phase G-2 round 1 MEDIUM fix): Coinbase
    Exchange public API rate-limits at ~10 req/sec per IP. 200ms gives
    5 req/sec headroom — well under the limit. The existing 50ms G-1
    default is too aggressive when the same parameter ALSO paces API calls.

    Strategy:
      1. Find earliest/latest evaluation_time over rows missing xasset spots.
      2. For each asset, paginate Coinbase candles across the range and
         build a minute-keyed lookup.
      3. For each row missing spots, look up each asset's close at the
         row's evaluation_time minute (±3 min fallback). UPDATE.
    """
    if fetcher is None:
        fetcher = fetch_coinbase_candles

    # Phase G-2 round 2 MEDIUM fix: clamp sleep_ms at the function
    # boundary so the CLI default of 50ms (tuned for the SQL phases)
    # cannot accidentally smash Coinbase's 10 req/sec ceiling. 200ms
    # gives 5 req/sec headroom. Function-level defense regardless of caller.
    if sleep_ms < 200:
        logger.info(
            "g2: bumping sleep_ms %d → 200 (Coinbase rate-limit floor)",
            sleep_ms,
        )
        sleep_ms = 200

    # 1. Discover historical date range.
    # Bit 2 (2026-05-11, ClickUp 86b9vrjf2): predicate widened from
    # `btc_spot_at_decision IS NULL` to also catch rows where btc IS
    # populated but hype/doge IS NULL — the exact cohort that
    # accumulated since T1 5dca85a, where the producer at
    # bot/scanner/__init__.py:990 emitted 6 keys but the pre-Bit-2
    # consumer at bot/state.py:1833-1840 silently dropped hype/doge.
    # Without this widening the post-Bit-2 backfill runbook would skip
    # the rows the Bit is designed to fix (R1 adversarial review M1).
    rng = conn.execute(
        "SELECT MIN(evaluation_time), MAX(evaluation_time) "
        "FROM evaluated_opportunities "
        "WHERE (btc_spot_at_decision IS NULL "
        "       OR hype_spot_at_decision IS NULL "
        "       OR doge_spot_at_decision IS NULL) "
        "AND evaluation_time IS NOT NULL"
    ).fetchone()
    if not rng or rng[0] is None:
        return 0
    start_iso, end_iso = rng[0], rng[1]
    logger.info("g2: backfilling xasset spots over [%s .. %s]", start_iso, end_iso)

    # 2. Fetch + build per-asset lookups. Coinbase caps at 300 candles
    # per request → paginate by 299-minute chunks (Phase G-2 round 1
    # LOW fix: 5-hour window with inclusive boundaries can return 301
    # candles, exceeding Coinbase's 300 cap and silently dropping the
    # last minute).
    lookups: Dict[str, Dict[int, float]] = {}
    for asset in COINBASE_PRODUCTS:
        all_candles: List = []
        n_chunks_ok = 0
        n_chunks_failed = 0
        cur_start = datetime.datetime.fromisoformat(start_iso.replace("Z", "+00:00"))
        end_dt = datetime.datetime.fromisoformat(end_iso.replace("Z", "+00:00"))
        # Extend end by 1 minute so a single-row range (start == end) still
        # fetches one candle. Coinbase's [start, end] is inclusive on the
        # closest-to-now side; +1min covers the row's exact minute.
        end_dt = end_dt + datetime.timedelta(minutes=1)
        chunk = datetime.timedelta(minutes=299)  # safely under Coinbase's 300 cap
        while cur_start < end_dt:
            cur_end = min(cur_start + chunk, end_dt)
            chunk_start_iso = cur_start.isoformat().replace("+00:00", "Z")
            chunk_end_iso = cur_end.isoformat().replace("+00:00", "Z")
            try:
                page = fetcher(asset, chunk_start_iso, chunk_end_iso)
                all_candles.extend(page or [])
                n_chunks_ok += 1
            except CoinbaseFetchError as e:
                n_chunks_failed += 1
                logger.warning("g2: chunk failed asset=%s: %s", asset, e)
            cur_start = cur_end
            if sleep_ms > 0:
                _time_mod.sleep(sleep_ms / 1000.0)
        lookups[asset] = build_candle_lookup(all_candles)
        logger.info(
            "g2: %s — %d/%d chunks OK, %d candles, %d unique minutes",
            asset, n_chunks_ok, n_chunks_ok + n_chunks_failed,
            len(all_candles), len(lookups[asset]),
        )
        # Phase G-2 round 1 HIGH fix: abort if API failed completely for
        # any asset — silently writing all-NULL for that asset is worse
        # than not writing at all (the rows would look "backfilled").
        if len(lookups[asset]) == 0:
            raise CoinbaseFetchError(
                f"g2: asset={asset} produced ZERO candles — refusing to write "
                f"all-NULL spots. Re-run when Coinbase API is healthy."
            )

    # 3. Per-row UPDATE.
    last_id = read_checkpoint(checkpoint_dir, "g2_xasset") if checkpoint_dir else 0
    total = 0
    while True:
        rows = conn.execute(
            # Bit 2 (2026-05-11): predicate matches the discovery range
            # widening above — catches the post-T1 cohort where btc was
            # populated but hype/doge stayed NULL due to the pre-Bit-2
            # silent-drop bug.
            "SELECT id, evaluation_time FROM evaluated_opportunities "
            "WHERE id > ? AND (btc_spot_at_decision IS NULL "
            "                  OR hype_spot_at_decision IS NULL "
            "                  OR doge_spot_at_decision IS NULL) "
            "AND evaluation_time IS NOT NULL "
            "ORDER BY id LIMIT ?",
            (last_id, batch_size),
        ).fetchall()
        if not rows:
            break
        for r in rows:
            spots = lookup_xasset_spots_for_row(r["evaluation_time"], lookups)
            conn.execute(
                "UPDATE evaluated_opportunities SET "
                "btc_spot_at_decision = ?, eth_spot_at_decision = ?, "
                "sol_spot_at_decision = ?, xrp_spot_at_decision = ?, "
                # Bit 2 / T1 cross-asset expansion (2026-05-11): UPDATE
                # extended to 6 columns. lookup_xasset_spots_for_row
                # iterates COINBASE_PRODUCTS keys, which already includes
                # HYPE/DOGE since T1.5 (bf8b9a3, 2026-05-10) — so `spots`
                # already has the keys, they just weren't being written.
                "hype_spot_at_decision = ?, doge_spot_at_decision = ? "
                "WHERE id = ?",
                (spots["btc_spot_at_decision"], spots["eth_spot_at_decision"],
                 spots["sol_spot_at_decision"], spots["xrp_spot_at_decision"],
                 spots["hype_spot_at_decision"], spots["doge_spot_at_decision"],
                 r["id"]),
            )
            total += 1
            last_id = r["id"]
        conn.commit()
        if checkpoint_dir:
            write_checkpoint(checkpoint_dir, "g2_xasset", last_id)
        if sleep_ms > 0:
            _time_mod.sleep(sleep_ms / 1000.0)
        if len(rows) < batch_size:
            break
    return total


# ── Phase G-4: Path metrics from per-row Coinbase candle samples ──────

def compute_path_metrics_for_row(
    asset_lookup: Dict[int, float],
    eval_epoch_min: int,
    seconds_to_close: float,
    threshold: float,
) -> Dict[str, Optional[float]]:
    """Compute the 4 path-metric fields from per-row asset candle data.

    Window definition: a 15M market opens 900s before settlement.
    Eval is at `evaluation_time` with `seconds_to_close` until settlement.
    Therefore the OBSERVATION window seen so far at eval time is:
      [eval - (900 - seconds_to_close), eval] = [eval - observed_secs, eval]
    where `observed_secs = 900 - seconds_to_close` (clamped ≥ 0).

    Returns dict with the 4 fields. Returns all-None if threshold is 0
    (div-by-zero protection) OR if no candles fall in the window.

    Match Phase F live-capture semantics:
      - max_excursion = signed price (positive if max-above wins by |val|;
        else negative). Tie favors above.
      - knockout_time_relative = (eval - last_crossing) / (eval - window_open)
        bounded [0, 1]; 1.0 if no crossings.

    NOTE: 1-min candle granularity → time_above/below precision is ±60s.
    Acceptable for retrospective research (Phase F live captures the same
    granularity through the scan loop's tick cadence).
    """
    out = {
        "time_above_strike_seconds": None,
        "time_below_strike_seconds": None,
        "max_excursion_from_strike": None,
        "knockout_time_relative": None,
    }
    # Phase G-4 round 1 L2: defense-in-depth — negative thresholds are
    # impossible for a real strike price but cheap to guard.
    if threshold is None or threshold <= 0:
        return out
    # Phase G-4 round 3 MEDIUM fix: clamp observed_secs into [0, 900].
    # max() floors negative-stc cases (post-settlement straggler row,
    # clock skew); min() caps if seconds_to_close < 0 — without the cap
    # observed_secs could exceed 900, walking more candles than the
    # 15M window has, violating time_above + time_below <= 900s and
    # creating train/serve skew with Phase F live capture.
    observed_secs = max(0.0, min(900.0, 900.0 - float(seconds_to_close or 0.0)))
    if observed_secs <= 0.0:
        return out
    observed_min = int(observed_secs // 60)
    window_start_min = eval_epoch_min - observed_min
    # Walk candles oldest-to-newest in the window.
    # Phase G-4 round 2 fix (R1 H2 fix went the WRONG direction):
    # Coinbase candle `time` field is the BUCKET START, so the candle
    # at minute M represents [M, M+1) of price-time. For an observation
    # window ending at eval_epoch_min, the candle at eval_epoch_min
    # covers a period ENTIRELY POST-eval — it's leakage. Correct range
    # is [window_start_min, eval_epoch_min) — exactly observed_min
    # candles, all strictly pre-eval. Pre-fix would have polluted path
    # metrics with up to 60s of post-eval price action (and ghost
    # crossings at settlement-edge whipsaws), creating train/serve skew
    # vs. Phase F live capture which is bounded by `last_ts <= now`.
    samples: List[Tuple[int, float]] = []
    for m in range(window_start_min, eval_epoch_min):
        v = asset_lookup.get(m)
        if v is not None:
            samples.append((m, v))
    if not samples:
        return out

    time_above = 0.0
    time_below = 0.0
    max_above_excursion = 0.0  # >= 0; price units
    max_below_excursion = 0.0  # >= 0 magnitude (will negate at output)
    crossings: List[int] = []  # epoch_min of crossings
    prev_above: Optional[bool] = None
    for m, close in samples:
        # Phase G-4 round 2 MEDIUM fix: match live `_update_window_state`
        # at-strike semantics — bot.py uses `spot >= threshold` (inclusive).
        is_above = close >= threshold
        diff = close - threshold
        if is_above:
            time_above += 60.0
            if diff > max_above_excursion:
                max_above_excursion = diff
        else:
            time_below += 60.0
            if -diff > max_below_excursion:
                max_below_excursion = -diff
        if prev_above is not None and is_above != prev_above:
            crossings.append(m)
        prev_above = is_above

    # max_excursion: signed; max-magnitude wins; tie favors above.
    if max_above_excursion >= max_below_excursion:
        max_excursion = max_above_excursion
    else:
        max_excursion = -max_below_excursion

    # knockout_time_relative: (eval - last_crossing) / (eval - window_open).
    if crossings:
        last_crossing_min = crossings[-1]
        decided_min = eval_epoch_min - last_crossing_min
        knockout = max(0.0, min(1.0, decided_min / observed_min)) if observed_min > 0 else 1.0
    else:
        knockout = 1.0

    out["time_above_strike_seconds"] = time_above
    out["time_below_strike_seconds"] = time_below
    out["max_excursion_from_strike"] = max_excursion
    out["knockout_time_relative"] = knockout
    return out


def backfill_path_metrics(
    conn: sqlite3.Connection,
    fetcher: Optional[Callable] = None,
    batch_size: int = 50,
    sleep_ms: int = 200,
    checkpoint_dir: Optional[str] = None,
) -> int:
    """Phase G-4 backfill: per-row path metrics from Coinbase candles.

    Reuses the G-2 fetch + lookup pattern. SELECTs rows missing
    time_above_strike_seconds (Phase F's primary populated field —
    rows missing it are pre-Phase-F historical). Pulls candles for
    the FULL date range across all 4 assets ONCE (avoids per-row API
    calls). Per-row computation in Python.

    Like G-2, clamps `sleep_ms = max(sleep_ms, 200)` for the API
    pacing portion."""
    if fetcher is None:
        fetcher = fetch_coinbase_candles
    if sleep_ms < 200:
        logger.info(
            "g4: bumping sleep_ms %d → 200 (Coinbase rate-limit floor)",
            sleep_ms,
        )
        sleep_ms = 200

    # 1. Discover historical date range.
    rng = conn.execute(
        "SELECT MIN(evaluation_time), MAX(evaluation_time) "
        "FROM evaluated_opportunities "
        "WHERE time_above_strike_seconds IS NULL "
        "AND product_type = '15m' "
        "AND threshold IS NOT NULL "
        "AND seconds_to_close IS NOT NULL "
        "AND evaluation_time IS NOT NULL"
    ).fetchone()
    if not rng or rng[0] is None:
        return 0
    start_iso, end_iso = rng[0], rng[1]
    logger.info("g4: backfilling path metrics over [%s .. %s]", start_iso, end_iso)

    # 2. Fetch candles for the full range (extend start by 15min to cover
    # the earliest row's full window-open).
    extended_start = (
        datetime.datetime.fromisoformat(start_iso.replace("Z", "+00:00"))
        - datetime.timedelta(minutes=16)
    )
    extended_start_iso = extended_start.isoformat().replace("+00:00", "Z")
    end_dt = datetime.datetime.fromisoformat(end_iso.replace("Z", "+00:00")) + datetime.timedelta(minutes=1)

    lookups: Dict[str, Dict[int, float]] = {}
    chunk = datetime.timedelta(minutes=299)
    for asset in COINBASE_PRODUCTS:
        all_candles: List = []
        n_chunks_ok = 0
        n_chunks_failed = 0
        cur_start = extended_start
        while cur_start < end_dt:
            cur_end = min(cur_start + chunk, end_dt)
            chunk_start_iso = cur_start.isoformat().replace("+00:00", "Z")
            chunk_end_iso = cur_end.isoformat().replace("+00:00", "Z")
            try:
                page = fetcher(asset, chunk_start_iso, chunk_end_iso)
                all_candles.extend(page or [])
                n_chunks_ok += 1
            except CoinbaseFetchError as e:
                n_chunks_failed += 1
                logger.warning("g4: chunk failed asset=%s: %s", asset, e)
            cur_start = cur_end
            if sleep_ms > 0:
                _time_mod.sleep(sleep_ms / 1000.0)
        lookups[asset] = build_candle_lookup(all_candles)
        logger.info(
            "g4: %s — %d/%d chunks OK, %d candles, %d unique minutes",
            asset, n_chunks_ok, n_chunks_ok + n_chunks_failed,
            len(all_candles), len(lookups[asset]),
        )
        # Phase G-4 round 1 H1 fix: mirror G-2's per-asset zero-candle
        # abort. Without this, a complete API failure for one asset would
        # silently write all-NULL path metrics to every row of that asset
        # AND increment `total` — operator gets misleading "n=X updated"
        # while the rows are unusable. WORSE than G-2 because backfill
        # marks rows as "processed" via the UPDATE, so re-runs won't retry.
        if len(lookups[asset]) == 0:
            raise CoinbaseFetchError(
                f"g4: asset={asset} produced ZERO candles — refusing to "
                f"write all-NULL path metrics. Re-run when API healthy."
            )

    # 3. Per-row UPDATE.
    last_id = read_checkpoint(checkpoint_dir, "g4_path") if checkpoint_dir else 0
    total = 0
    while True:
        rows = conn.execute(
            "SELECT id, asset, evaluation_time, threshold, seconds_to_close "
            "FROM evaluated_opportunities "
            "WHERE id > ? AND time_above_strike_seconds IS NULL "
            "AND product_type = '15m' "
            "AND threshold IS NOT NULL "
            "AND seconds_to_close IS NOT NULL "
            "AND evaluation_time IS NOT NULL "
            "ORDER BY id LIMIT ?",
            (last_id, batch_size),
        ).fetchall()
        if not rows:
            break
        for r in rows:
            asset = r["asset"]
            asset_lookup = lookups.get(asset, {})
            try:
                eval_dt = datetime.datetime.fromisoformat(
                    r["evaluation_time"].replace("Z", "+00:00"))
                eval_epoch_min = int(eval_dt.timestamp()) // 60
            except Exception:
                eval_epoch_min = None
            if eval_epoch_min is None:
                last_id = r["id"]
                continue
            metrics = compute_path_metrics_for_row(
                asset_lookup=asset_lookup,
                eval_epoch_min=eval_epoch_min,
                seconds_to_close=float(r["seconds_to_close"] or 0.0),
                threshold=float(r["threshold"] or 0.0),
            )
            conn.execute(
                "UPDATE evaluated_opportunities SET "
                "time_above_strike_seconds = ?, time_below_strike_seconds = ?, "
                "max_excursion_from_strike = ?, knockout_time_relative = ? "
                "WHERE id = ?",
                (metrics["time_above_strike_seconds"],
                 metrics["time_below_strike_seconds"],
                 metrics["max_excursion_from_strike"],
                 metrics["knockout_time_relative"],
                 r["id"]),
            )
            total += 1
            last_id = r["id"]
        conn.commit()
        if checkpoint_dir:
            write_checkpoint(checkpoint_dir, "g4_path", last_id)
        # Phase G-4 round 1 M1 fix: yield DB lock to live writers
        # between batches (mirror G-2 pattern; was missing here).
        if sleep_ms > 0:
            _time_mod.sleep(sleep_ms / 1000.0)
        if len(rows) < batch_size:
            break
    return total


# ── Phase G-5: OKX + Deribit perp funding rate backfill ────────────────

# OKX uses USDT-quoted perps for these assets.
# T1.5 (2026-05-10, ticket 86b9vre9p): DOGE + HYPE added. Both verified live
# via /api/v5/public/funding-rate-history. T1.5 (2026-05-17, ticket
# 86b9zmj15): BNB added (verified live via same endpoint).
# Must stay lock-step with the live poller's FUNDING_SYMBOLS (regression
# tests: TestBackfillOkxFundingInstruments in
# tests/integration/test_doge_hype_onboarding_t1_5.py +
# tests/integration/test_bnb_onboarding_t1_5.py) — divergence biases T3 training.
OKX_FUNDING_INSTRUMENTS = {
    "BTC": "BTC-USDT-SWAP",
    "ETH": "ETH-USDT-SWAP",
    "SOL": "SOL-USDT-SWAP",
    "XRP": "XRP-USDT-SWAP",
    "DOGE": "DOGE-USDT-SWAP",
    "HYPE": "HYPE-USDT-SWAP",
    "BNB": "BNB-USDT-SWAP",
}
# Deribit uses USD-quoted PERPETUAL for BTC/ETH; SOL/XRP/DOGE are USDC-quoted.
# T1.5: DOGE added (verified live via /public/get_funding_rate_history).
# HYPE STAYS ABSENT — Deribit does not list a HYPE perpetual (verified via
# /public/get_instruments?currency=HYPE&kind=future returning empty).
# BNB STAYS ABSENT (T1.5 2026-05-17, ticket 86b9zmj15) — Deribit does not
# list a BNB perpetual either (verified via /public/get_instruments?
# currency=BNB&kind=future returning empty). Same documented-gap pattern
# as HYPE. Consumer code uses .get(asset)/iteration so key-absence is safe.
DERIBIT_FUNDING_INSTRUMENTS = {
    "BTC": "BTC-PERPETUAL",
    "ETH": "ETH-PERPETUAL",
    "SOL": "SOL_USDC-PERPETUAL",
    "XRP": "XRP_USDC-PERPETUAL",
    "DOGE": "DOGE_USDC-PERPETUAL",
}
OKX_FUNDING_URL = "https://www.okx.com/api/v5/public/funding-rate-history"
DERIBIT_FUNDING_URL = "https://www.deribit.com/api/v2/public/get_funding_rate_history"


def parse_okx_funding_response(resp: Dict) -> List[Tuple[int, float]]:
    """OKX returns reverse-chronological JSON. Parse + sort ascending
    by fundingTime. Skips malformed entries."""
    if not isinstance(resp, dict):
        return []
    rows = resp.get("data") or []
    out: List[Tuple[int, float]] = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        try:
            ts = int(r["fundingTime"])
            rate = float(r["fundingRate"])
        except (KeyError, TypeError, ValueError):
            continue
        out.append((ts, rate))
    out.sort(key=lambda x: x[0])
    return out


def parse_deribit_funding_response(resp: Dict) -> List[Tuple[int, float]]:
    """Deribit returns chronological. Parse + (re)sort defensively."""
    if not isinstance(resp, dict):
        return []
    rows = resp.get("result") or []
    out: List[Tuple[int, float]] = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        try:
            ts = int(r["timestamp"])
            rate = float(r["interest_8h"])
        except (KeyError, TypeError, ValueError):
            continue
        out.append((ts, rate))
    out.sort(key=lambda x: x[0])
    return out


def lookup_funding_rate_at_or_before(
    rates: List[Tuple[int, float]], eval_ms: int,
) -> Optional[float]:
    """Return the rate of the most-recent entry with funding_time <= eval_ms.
    None if rates is empty or all entries are after eval. O(log n) bisect."""
    if not rates:
        return None
    # bisect_right returns insertion point such that rates[i-1] <= eval_ms.
    keys = [r[0] for r in rates]
    i = bisect.bisect_right(keys, eval_ms)
    if i == 0:
        return None
    return rates[i - 1][1]


def fetch_okx_funding(
    asset: str,
    start_ms: int,
    end_ms: int,
    *,
    request_fn: Optional[Callable] = None,
    max_retries: int = 3,
    page_sleep_ms: int = 200,
) -> Dict:
    """Fetch ALL OKX funding history in [start_ms, end_ms]. OKX caps at
    100 entries per request (newest first). For a 90-day backfill at
    8h cadence ≈ 270 entries/asset, that's 3 paginated requests.

    Phase G-5 round 1 C1 fix: pagination loop. Pre-fix sent ONE request
    with limit=100 → silently dropped 170 entries per asset for the
    older 60 days of the backfill range. Pagination uses `after` cursor
    set to the oldest fundingTime from the previous page to fetch the
    next-older batch.

    Phase G-5 round 1 C4 fix: OKX returns HTTP 200 with `code != "0"`
    on application-layer failures (rate limit on certain endpoints, bad
    inst, etc.). We detect + log + return empty `data` so caller can't
    silently treat a bad response as 'no funding entries'.
    """
    if request_fn is None:
        import requests as _requests
        def request_fn(url, params, timeout):
            return _requests.get(url, params=params, timeout=timeout)
    inst = OKX_FUNDING_INSTRUMENTS.get(asset)
    if inst is None:
        return {"data": []}

    # `after` cursor — start at end_ms (means: "give me entries with
    # fundingTime < end_ms"). Each subsequent page narrows it.
    after = end_ms
    all_entries: List[Dict] = []
    while True:
        params = {"instId": inst, "limit": 100,
                  "before": str(start_ms), "after": str(after)}
        last_err = None
        page = None
        for attempt in range(max_retries):
            try:
                resp = request_fn(OKX_FUNDING_URL, params, 30)
                status = getattr(resp, "status_code", 0)
                if status == 200:
                    page = resp.json()
                    break
                if status == 429:
                    _time_mod.sleep(2 ** attempt)
                    last_err = "HTTP 429"
                    continue
                last_err = f"HTTP {status}"
                break
            except Exception as e:
                last_err = str(e)
                _time_mod.sleep(2 ** attempt)
        if page is None:
            logger.warning("g5: OKX %s page fetch failed: %s",
                           asset, last_err)
            break
        # Phase G-5 round 1 C4: detect application-layer failures.
        if str(page.get("code", "0")) != "0":
            logger.warning(
                "g5: OKX %s app-layer error: code=%s msg=%s",
                asset, page.get("code"), page.get("msg"),
            )
            break
        entries = page.get("data") or []
        if not entries:
            break
        all_entries.extend(entries)
        # Advance cursor to the oldest fundingTime from this page
        # (entries are newest-first within a page).
        try:
            oldest_ts = min(int(e["fundingTime"]) for e in entries
                            if "fundingTime" in e)
        except (ValueError, TypeError):
            break
        if oldest_ts <= start_ms:
            break
        # Phase G-5 round 2 M1 fix: pagination stall guard. If `after`
        # didn't advance (API ignored cursor / returned same page), break
        # with warning rather than infinite-loop.
        if oldest_ts >= after:
            logger.warning(
                "g5: OKX %s pagination stalled at %d (no cursor advance) — "
                "stopping fetch", asset, after,
            )
            break
        after = oldest_ts
        if page_sleep_ms > 0:
            _time_mod.sleep(page_sleep_ms / 1000.0)
    return {"data": all_entries}


def fetch_deribit_funding(
    asset: str,
    start_ms: int,
    end_ms: int,
    *,
    request_fn: Optional[Callable] = None,
    max_retries: int = 3,
) -> Dict:
    """Fetch Deribit funding history for [start_ms, end_ms]. Deribit's
    endpoint accepts a time range and returns all entries in it."""
    if request_fn is None:
        import requests as _requests
        def request_fn(url, params, timeout):
            return _requests.get(url, params=params, timeout=timeout)
    inst = DERIBIT_FUNDING_INSTRUMENTS.get(asset)
    if inst is None:
        return {"result": []}
    params = {
        "instrument_name": inst,
        "start_timestamp": start_ms, "end_timestamp": end_ms,
    }
    last_err = None
    for attempt in range(max_retries):
        try:
            resp = request_fn(DERIBIT_FUNDING_URL, params, 30)
            status = getattr(resp, "status_code", 0)
            if status == 200:
                return resp.json()
            if status == 429:
                _time_mod.sleep(2 ** attempt)
                last_err = "HTTP 429"
                continue
            last_err = f"HTTP {status}"
            break
        except Exception as e:
            last_err = str(e)
            _time_mod.sleep(2 ** attempt)
    logger.warning("g5: Deribit %s fetch failed: %s", asset, last_err)
    return {"result": []}


def backfill_funding_rates(
    conn: sqlite3.Connection,
    okx_fetcher: Optional[Callable] = None,
    deribit_fetcher: Optional[Callable] = None,
    batch_size: int = 50,
    sleep_ms: int = 200,
    checkpoint_dir: Optional[str] = None,
) -> int:
    """Phase G-5 backfill: per-row OKX + Deribit funding rates from
    public funding-rate-history endpoints.

    Strategy:
      1. Discover historical date range over rows missing okx_funding_*.
      2. Per asset per exchange, fetch all funding entries in range,
         build sorted (timestamp_ms, rate) list.
      3. Per row, lookup most-recent rate ≤ row.evaluation_time on each
         exchange. UPDATE.

    Unlike G-2/G-4, does NOT abort if an exchange returns empty for an
    asset — funding can legitimately be unavailable for some pairs (e.g.,
    Deribit USDC perps for SOL/XRP have shorter history). NULL is
    semantically 'no data' rather than 'computation failed'.
    """
    if okx_fetcher is None:
        okx_fetcher = fetch_okx_funding
    if deribit_fetcher is None:
        deribit_fetcher = fetch_deribit_funding

    # 1. Date range. Phase G-5 round 1 C2 fix: pre-filter is OR (was AND)
    # so rows where only ONE column is populated still get the missing
    # one filled in. Pre-fix: a partial-run (OKX failed mid-backfill,
    # Deribit succeeded) would leave OKX permanently NULL because the
    # row's deribit IS NOT NULL excluded it from re-runs.
    rng = conn.execute(
        "SELECT MIN(evaluation_time), MAX(evaluation_time) "
        "FROM evaluated_opportunities "
        "WHERE (okx_funding_rate_at_decision IS NULL "
        "       OR deribit_funding_rate_at_decision IS NULL) "
        "AND evaluation_time IS NOT NULL"
    ).fetchone()
    if not rng or rng[0] is None:
        return 0
    start_iso, end_iso = rng[0], rng[1]
    logger.info("g5: backfilling funding rates over [%s .. %s]",
                start_iso, end_iso)
    start_dt = datetime.datetime.fromisoformat(start_iso.replace("Z", "+00:00"))
    end_dt = datetime.datetime.fromisoformat(end_iso.replace("Z", "+00:00"))
    start_ms = int(start_dt.timestamp() * 1000)
    end_ms = int(end_dt.timestamp() * 1000)

    # 2. Per (exchange, asset) fetch + parse.
    okx_rates: Dict[str, List[Tuple[int, float]]] = {}
    deribit_rates: Dict[str, List[Tuple[int, float]]] = {}
    for asset in OKX_FUNDING_INSTRUMENTS:
        okx_rates[asset] = parse_okx_funding_response(
            okx_fetcher(asset, start_ms, end_ms))
        logger.info("g5: OKX %s — %d funding entries", asset, len(okx_rates[asset]))
        if sleep_ms > 0:
            _time_mod.sleep(sleep_ms / 1000.0)
    for asset in DERIBIT_FUNDING_INSTRUMENTS:
        deribit_rates[asset] = parse_deribit_funding_response(
            deribit_fetcher(asset, start_ms, end_ms))
        logger.info("g5: Deribit %s — %d funding entries",
                    asset, len(deribit_rates[asset]))
        if sleep_ms > 0:
            _time_mod.sleep(sleep_ms / 1000.0)

    # 3. Per-row UPDATE.
    last_id = read_checkpoint(checkpoint_dir, "g5_funding") if checkpoint_dir else 0
    total = 0
    while True:
        rows = conn.execute(
            "SELECT id, asset, evaluation_time, "
            "okx_funding_rate_at_decision, deribit_funding_rate_at_decision "
            "FROM evaluated_opportunities "
            "WHERE id > ? AND (okx_funding_rate_at_decision IS NULL "
            "                  OR deribit_funding_rate_at_decision IS NULL) "
            "AND evaluation_time IS NOT NULL "
            "ORDER BY id LIMIT ?",
            (last_id, batch_size),
        ).fetchall()
        if not rows:
            break
        for r in rows:
            asset = r["asset"]
            try:
                eval_dt = datetime.datetime.fromisoformat(
                    r["evaluation_time"].replace("Z", "+00:00"))
                eval_ms = int(eval_dt.timestamp() * 1000)
            except Exception:
                last_id = r["id"]
                continue
            # Phase G-5 round 1 C2 fix: only write a column if the row's
            # current value is NULL — preserves any previously-populated
            # value (live capture or prior backfill). Pre-fix
            # unconditionally overwrote both columns on every UPDATE,
            # which would clobber non-NULL existing values.
            okx_rate = (lookup_funding_rate_at_or_before(
                            okx_rates.get(asset, []), eval_ms)
                        if r["okx_funding_rate_at_decision"] is None
                        else r["okx_funding_rate_at_decision"])
            deribit_rate = (lookup_funding_rate_at_or_before(
                                deribit_rates.get(asset, []), eval_ms)
                            if r["deribit_funding_rate_at_decision"] is None
                            else r["deribit_funding_rate_at_decision"])
            conn.execute(
                "UPDATE evaluated_opportunities SET "
                "okx_funding_rate_at_decision = ?, "
                "deribit_funding_rate_at_decision = ? "
                "WHERE id = ?",
                (okx_rate, deribit_rate, r["id"]),
            )
            total += 1
            last_id = r["id"]
        conn.commit()
        if checkpoint_dir:
            write_checkpoint(checkpoint_dir, "g5_funding", last_id)
        if sleep_ms > 0:
            _time_mod.sleep(sleep_ms / 1000.0)
        if len(rows) < batch_size:
            break
    return total


# ── Driver ─────────────────────────────────────────────────────────────

def _connect(db_path: str) -> sqlite3.Connection:
    """Open the DB with the same PRAGMAs production uses."""
    conn = sqlite3.connect(db_path, timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=10000")
    return conn


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Shadow-coverage backfill harness")
    parser.add_argument("--db", required=True, help="path to state.db")
    parser.add_argument(
        "--phase", required=True,
        choices=["final_spot", "maker", "streak", "tslf", "xasset",
                 "pathmetrics", "funding", "all"],
        help="which backfill to run",
    )
    parser.add_argument(
        "--checkpoint-dir", default="data/backfill_ckpt",
        help="directory for resumable checkpoint files",
    )
    parser.add_argument(
        "--batch-size", type=int, default=50,
        help="rows per UPDATE commit (CLAUDE.md ≤50 when DB is shared with live bot)",
    )
    parser.add_argument(
        "--sleep-ms", type=int, default=50,
        help=("sleep between batches to yield DB lock to live writers "
              "(default 50ms; xasset phase clamps to ≥200ms because "
              "Coinbase Exchange API rate-limits at ~10 req/sec)"),
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )
    conn = _connect(args.db)

    # Phase G-1 round 1 CRITICAL: per-row queries on settled_at / opened_at
    # would do full-table scans without these indexes — catastrophic at
    # 81K + 60K backfill rows. Idempotent.
    logger.info("ensuring backfill indexes")
    ensure_indexes(conn)

    kwargs = {
        "batch_size": args.batch_size,
        "sleep_ms": args.sleep_ms,
        "checkpoint_dir": args.checkpoint_dir,
    }
    phases = (["final_spot", "maker", "streak", "tslf", "xasset",
               "pathmetrics", "funding"]
              if args.phase == "all" else [args.phase])
    for p in phases:
        logger.info("starting phase %s (batch_size=%d sleep_ms=%d)",
                    p, args.batch_size, args.sleep_ms)
        if p == "final_spot":
            n = backfill_final_spot_price(conn, **kwargs)
        elif p == "maker":
            n = backfill_maker_counterfactual(conn, **kwargs)
        elif p == "streak":
            n = backfill_recent_streak(conn, **kwargs)
        elif p == "tslf":
            n = backfill_tslf(conn, **kwargs)
        elif p == "xasset":
            n = backfill_xasset_spots(conn, **kwargs)
        elif p == "pathmetrics":
            n = backfill_path_metrics(conn, **kwargs)
        elif p == "funding":
            n = backfill_funding_rates(conn, **kwargs)
        else:
            raise ValueError(f"unknown phase: {p}")
        logger.info("phase %s: updated %d rows", p, n)
    return 0


if __name__ == "__main__":
    sys.exit(main())
