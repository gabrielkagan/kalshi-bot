#!/usr/bin/env python3
"""External market data poller (P7, R-p7-deploy-r11).

Polls free public APIs every minute to capture three high-signal series
that the cal_mlp v3 K=2 train (June 22) needs but currently doesn't have:

  - Binance perpetuals funding rate (8h cycle, but value time-since-last
    matters at minute resolution)
  - Binance perpetuals open interest (~1m updates)
  - Deribit BTC DVOL implied vol index (~5m updates)

Starting today gives v3 ~50d of history by June 22.

Writes to a new `external_market_data` table keyed by (source, symbol, ts)
for idempotent re-polling. Network errors per-symbol fail-open (don't
poison batch); per-pass errors don't crash the loop.

This runs as a SEPARATE process from bot.py to keep blast radius small.

Usage (one-shot):
    python3 scripts/backfill/external_market_poller.py --db state.db --once

Usage (continuous, systemd-style):
    python3 scripts/backfill/external_market_poller.py --db state.db
        # polls every 60s until SIGTERM

Cron (alternative to continuous):
    * * * * * cd ~/kalshi-bot-repo && python3 scripts/backfill/external_market_poller.py \\
        --db state.db --once >> logs/external_poller.log 2>&1
"""

import argparse
import json
import logging
import os
import signal
import sqlite3
import sys
import time
import urllib.parse
import urllib.request
from typing import Optional


# Endpoint templates (free, public, no auth).
# R2 fix: Binance.com (fapi.binance.com) returns HTTP 451 from US IPs.
# Switched to OKX, which is accessible globally and has comparable
# liquidity on USDT-margined perpetuals for all 4 assets.
OKX_FUNDING_URL = 'https://www.okx.com/api/v5/public/funding-rate?instId={inst}'
OKX_OI_URL = 'https://www.okx.com/api/v5/public/open-interest?instId={inst}'
DERIBIT_INDEX_URL = 'https://www.deribit.com/api/v2/public/get_index_price?index_name={index}'

# OKX uses INST-USDT-SWAP for USDT-margined perpetuals.
# DOGE + HYPE added 2026-05-10 (T1.5, ticket 86b9vre9p) — both verified
# live via /api/v5/public/instruments?instType=SWAP. T3 (cal_mlp training)
# needs okx_funding/okx_oi features for HYPE+DOGE rows at parity with
# BTC/ETH/SOL/XRP; without these, shadow rows would have NULL columns
# the model was trained to expect populated.
FUNDING_SYMBOLS = ['BTC-USDT-SWAP', 'ETH-USDT-SWAP', 'SOL-USDT-SWAP', 'XRP-USDT-SWAP', 'DOGE-USDT-SWAP', 'HYPE-USDT-SWAP']
OI_SYMBOLS = ['BTC-USDT-SWAP', 'ETH-USDT-SWAP', 'SOL-USDT-SWAP', 'XRP-USDT-SWAP', 'DOGE-USDT-SWAP', 'HYPE-USDT-SWAP']
# Deribit DVOL: correct index names are btcdvol_usdc / ethdvol_usdc
# (verified at /public/get_index_price_names). Only BTC + ETH have DVOL.
DVOL_SYMBOLS = ['btcdvol_usdc', 'ethdvol_usdc']

POLL_INTERVAL_SECONDS = 60
HTTP_TIMEOUT_SECONDS = 10


def _http_get_json(url: str, timeout: float = HTTP_TIMEOUT_SECONDS):
    """Fetch URL and parse JSON. Raises on network or parse error.
    Caller catches per-symbol so a single failure doesn't poison the batch."""
    req = urllib.request.Request(url, headers={'User-Agent': 'kalshi-bot-poller/1.0'})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = resp.read().decode('utf-8', errors='replace')
    return json.loads(body)


def init_schema(conn: sqlite3.Connection) -> None:
    """Create external_market_data table if absent. Idempotent."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS external_market_data (
            source TEXT NOT NULL,
            symbol TEXT NOT NULL,
            ts INTEGER NOT NULL,
            value REAL NOT NULL,
            raw_json TEXT,
            PRIMARY KEY (source, symbol, ts)
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_emd_ts ON external_market_data(ts)
    """)
    conn.commit()


def insert_observation(
    conn: sqlite3.Connection,
    source: str, symbol: str, ts: int,
    value: float, raw_json: Optional[str],
) -> None:
    """INSERT OR IGNORE — re-polling the same (source, symbol, ts) is a no-op."""
    conn.execute(
        "INSERT OR IGNORE INTO external_market_data "
        "(source, symbol, ts, value, raw_json) VALUES (?, ?, ?, ?, ?)",
        (source, symbol, ts, value, raw_json),
    )
    conn.commit()


def poll_okx_funding(symbols: list[str]) -> list[tuple[str, str, float, int]]:
    """Poll OKX USDT-margined perpetual funding rate. OKX returns
    {"code":"0","data":[{...,"fundingRate":"...","ts":"...",...}]}.
    Per-symbol failures don't poison the batch."""
    out = []
    for inst in symbols:
        try:
            data = _http_get_json(OKX_FUNDING_URL.format(inst=inst))
        except Exception as e:
            logging.warning('okx_funding %s failed: %s', inst, e)
            continue
        try:
            if data.get('code') != '0':
                logging.warning('okx_funding %s API error: %s', inst, data.get('msg'))
                continue
            row = data['data'][0]
            value = float(row['fundingRate'])
            ts = int(row.get('ts') or (time.time() * 1000))
        except (KeyError, ValueError, TypeError, IndexError) as e:
            logging.warning('okx_funding %s parse failed: %s', inst, e)
            continue
        out.append(('okx_funding', inst, value, ts))
    return out


def poll_okx_open_interest(symbols: list[str]) -> list[tuple[str, str, float, int]]:
    """Poll OKX USDT-margined perpetual open interest.
    Returns OI in CONTRACTS (oi field). For OI in USD, use 'oiUsd'.
    We store the contract count for consistency across symbols.
    """
    out = []
    for inst in symbols:
        try:
            data = _http_get_json(OKX_OI_URL.format(inst=inst))
        except Exception as e:
            logging.warning('okx_oi %s failed: %s', inst, e)
            continue
        try:
            if data.get('code') != '0':
                logging.warning('okx_oi %s API error: %s', inst, data.get('msg'))
                continue
            row = data['data'][0]
            value = float(row['oi'])
            ts = int(row.get('ts') or (time.time() * 1000))
        except (KeyError, ValueError, TypeError, IndexError) as e:
            logging.warning('okx_oi %s parse failed: %s', inst, e)
            continue
        out.append(('okx_oi', inst, value, ts))
    return out


def poll_deribit_dvol(indices: list[str]) -> list[tuple[str, str, float, int]]:
    """Deribit's DVOL index lives at the public price-index endpoint.
    Endpoint returns {result: {index_price: <DVOL>}}."""
    out = []
    for index in indices:
        try:
            data = _http_get_json(DERIBIT_INDEX_URL.format(index=index))
        except Exception as e:
            logging.warning('deribit_dvol %s failed: %s', index, e)
            continue
        try:
            value = float(data['result']['index_price'])
            ts = int(time.time() * 1000)
        except (KeyError, ValueError, TypeError) as e:
            logging.warning('deribit_dvol %s parse failed: %s', index, e)
            continue
        out.append(('deribit_dvol', index, value, ts))
    return out


# R-p7-deploy-r11 R3-M3: track per-source last-success for ops visibility.
# Without this, Deribit's weekly maintenance window silently drops 7+ hours
# of training data with only individual WARNING log lines as evidence.
# Surfacing per-source freshness in the per-pass INFO log lets cron-log
# inspection catch sustained outages without scraping every WARNING.
_LAST_SUCCESS_TS: dict[str, float] = {}
# R4 (HIGH): cron --once mode runs in a fresh process every invocation,
# so _LAST_SUCCESS_TS starts empty and a transient first-pass failure
# would otherwise trigger 'NEVER' alarms on EVERY cron run.
# R5 (HIGH): just tracking process-start time isn't enough — a fresh
# process every cron run means grace ALWAYS suppresses NEVER, hiding
# real outages forever. Persist last-success timestamps to disk and
# load on startup; grace only kicks in when BOTH _LAST_SUCCESS_TS is
# empty AND we're inside the grace window.
# R5 (LOW): grace-period delta uses monotonic clock to ignore NTP step-backs.
# Wall-clock would briefly suppress alarms or fire spuriously on adjustment.
# _LAST_SUCCESS_TS still uses wall-clock time (it's compared with the same
# epoch in stale checks AND written to logs alongside observation timestamps).
_PROCESS_START_TS: float = time.time()
_PROCESS_START_MONO: float = time.monotonic()
_STALE_THRESHOLD_S = 900  # 15 min — Deribit maintenance is typically <10 min
_LAST_SUCCESS_PERSIST_PATH = "state/external_poller_state.json"


def _load_last_success_ts(path: Optional[str] = None) -> None:
    """R5 fix: hydrate _LAST_SUCCESS_TS from disk so cron --once invocations
    inherit history from prior runs. Silent no-op on missing/corrupt
    file — the grace period covers cold-start.

    R6 (HIGH): path defaults to None and resolves to the MODULE attr at
    call time (not function-definition time). This lets tests override
    `_LAST_SUCCESS_PERSIST_PATH` per-test via the autouse fixture without
    leaking state into the repo's `state/` dir.
    """
    global _LAST_SUCCESS_TS
    if path is None:
        path = _LAST_SUCCESS_PERSIST_PATH
    try:
        with open(path, 'r') as f:
            data = __import__('json').load(f)
    except (FileNotFoundError, ValueError, OSError):
        return
    if not isinstance(data, dict):
        return
    for source, ts in data.items():
        try:
            _LAST_SUCCESS_TS[source] = float(ts)
        except (ValueError, TypeError):
            continue


def _persist_last_success_ts(path: Optional[str] = None) -> None:
    """R5 fix: save current _LAST_SUCCESS_TS atomically. Called after
    each successful poll pass. tmp+rename for atomicity; OSError is
    non-fatal (next pass retries).

    R6 (HIGH): path defaults to None → reads module attr at call time
    (see _load_last_success_ts docstring).
    """
    import os as _os
    import json as _json
    if path is None:
        path = _LAST_SUCCESS_PERSIST_PATH
    try:
        parent = _os.path.dirname(path) or '.'
        # R6 (LOW): makedirs(exist_ok=True) is idempotent and thread-safe by
        # OS guarantee. No lock needed even if multiple poll_once calls race.
        _os.makedirs(parent, exist_ok=True)
    except OSError:
        return
    tmp = f"{path}.tmp-{_os.getpid()}"
    try:
        with open(tmp, 'w') as f:
            _json.dump(_LAST_SUCCESS_TS, f)
        _os.replace(tmp, path)
    except OSError as e:
        logging.warning('persist_last_success_ts failed: %s', e)
        try:
            _os.unlink(tmp)
        except OSError:
            pass


def poll_once(conn: sqlite3.Connection) -> int:
    """Run one full poll pass. Returns number of observations written.
    Updates _LAST_SUCCESS_TS per source for freshness tracking."""
    pass_obs: dict[str, list] = {
        'okx_funding': poll_okx_funding(FUNDING_SYMBOLS),
        'okx_oi': poll_okx_open_interest(OI_SYMBOLS),
        'deribit_dvol': poll_deribit_dvol(DVOL_SYMBOLS),
    }
    now_s = time.time()
    written = 0
    any_success = False
    for source, obs_list in pass_obs.items():
        if obs_list:
            _LAST_SUCCESS_TS[source] = now_s
            any_success = True
        for src, symbol, value, ts in obs_list:
            try:
                insert_observation(conn, src, symbol, ts, value, None)
                written += 1
            except sqlite3.OperationalError as e:
                logging.warning('insert failed for %s/%s: %s', src, symbol, e)
    # R5 fix: persist after any successful source so cron-mode invocations
    # share state across runs.
    if any_success:
        _persist_last_success_ts()
    # Per-source staleness summary at WARNING level — visible in routine
    # cron logs without scraping individual WARNING lines.
    # R4 HIGH + R5 LOW: skip the NEVER branch during the grace period after
    # process start (cron --once would otherwise report NEVER on every
    # invocation that catches a transient first-pass failure). Use
    # monotonic clock so NTP step-backs don't briefly disable the check.
    in_grace_period = (time.monotonic() - _PROCESS_START_MONO) < _STALE_THRESHOLD_S
    stale = []
    for source in pass_obs.keys():
        last = _LAST_SUCCESS_TS.get(source)
        if last is None:
            if not in_grace_period:
                stale.append(f'{source}=NEVER')
            # else: process just started, no signal yet
        else:
            age_s = int(now_s - last)
            if age_s > _STALE_THRESHOLD_S:
                stale.append(f'{source}={age_s}s_stale')
    if stale:
        logging.warning('per-source staleness: %s', ', '.join(stale))
    return written


_stop = False


def _signal_handler(_signum, _frame):
    global _stop
    _stop = True


def main(argv: Optional[list] = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--db', default='state.db')
    ap.add_argument('--once', action='store_true',
                    help='poll once and exit (for cron)')
    ap.add_argument('--interval', type=int, default=POLL_INTERVAL_SECONDS,
                    help='seconds between poll passes (continuous mode)')
    ap.add_argument('--verbose', action='store_true')
    args = ap.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format='%(asctime)s %(levelname)s %(message)s',
        stream=sys.stderr,
    )

    if not os.path.exists(args.db):
        logging.error('DB not found: %s', args.db)
        return 2

    conn = sqlite3.connect(args.db, check_same_thread=False)
    conn.execute('PRAGMA journal_mode=WAL')
    conn.execute('PRAGMA busy_timeout=10000')
    init_schema(conn)

    # R5 fix: load any prior cron run's last-success timestamps so the
    # grace period only fires on truly cold start, not on every cron
    # invocation.
    _load_last_success_ts()

    signal.signal(signal.SIGTERM, _signal_handler)
    signal.signal(signal.SIGINT, _signal_handler)

    if args.once:
        n = poll_once(conn)
        logging.info('one-pass complete: %d observations written', n)
        return 0

    while not _stop:
        try:
            n = poll_once(conn)
            logging.info('poll pass: %d observations', n)
        except Exception:
            logging.exception('poll pass crashed (continuing)')
        # Sleep with stop-checking
        for _ in range(args.interval):
            if _stop:
                break
            time.sleep(1)
    logging.info('shutdown')
    return 0


if __name__ == '__main__':
    sys.exit(main())
