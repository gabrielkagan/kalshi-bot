#!/usr/bin/env python3
"""
Deterministic Auditor — Automated health checks for the Kalshi trading bot.

Runs hourly via cron. Queries state.db (read-only), sends alerts to Telegram.
No external AI APIs. No writes to state.db.

Usage:
    python3 bot/ai/auditor.py              # Run all checks
    python3 bot/ai/auditor.py --verbose    # Print all check results to stdout
    python3 bot/ai/auditor.py --check performance  # Run only one category
    python3 bot/ai/auditor.py --test-telegram      # Send a test message

Adding new checks:
    1. Write a function: def check_something(db, verbose) -> list[str]
    2. Add it to CHECKS list below with its category name
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sqlite3
import sys
import time
import urllib.request
import urllib.error
from datetime import datetime, timedelta, timezone
from pathlib import Path

log = logging.getLogger("auditor")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
# Bit 10.3 (2026-05-12): relocated from repo root → bot/ai/.
# `Path(__file__).resolve().parent.parent.parent` walks ai/ → bot/ → repo/.
# DO NOT shorten the chain — it would re-anchor data files under bot/ai/.
SCRIPT_DIR = Path(__file__).resolve().parent.parent.parent
STATE_DB_PATH = SCRIPT_DIR / "state.db"
AUDITOR_DB_PATH = SCRIPT_DIR / "auditor_state.db"
ENV_PATH = SCRIPT_DIR / ".env"
SCAN_JOURNAL_PATH = SCRIPT_DIR / "scan_journal.jsonl"
OPP_JOURNAL_PATH = SCRIPT_DIR / "opportunity_journal.jsonl"

# ---------------------------------------------------------------------------
# Alert deduplication window
# ---------------------------------------------------------------------------
DEDUP_HOURS = 24

# ---------------------------------------------------------------------------
# Max Telegram messages per run (batch overflow into one summary)
# ---------------------------------------------------------------------------
MAX_TG_MESSAGES = 5

# ---------------------------------------------------------------------------
# Expected table schemas (column counts for drift detection)
# ---------------------------------------------------------------------------
EXPECTED_COLUMN_COUNTS = {
    "settled_trades": 24,       # +2: strategy_group, is_stacked (stacking migration Apr 1 2026)
    "evaluated_opportunities": 80,
    "rejected_opportunities": 36,  # +7: sigma_winsorize, hour_sin, hour_cos, prob_breakeven_gap, vol_regime, data_provenance, orderbook_levels_json (Sprint B Bit B.1a, 2026-05-12)
    "positions": 26,            # +3: strategy_group, is_stacked, accumulated_fee_cents (Apr 6 2026)
    "pending_orders": 12,
}

# ---------------------------------------------------------------------------
# .env parser (cron doesn't source .env via start.sh)
# ---------------------------------------------------------------------------

def load_dotenv(path: Path) -> None:
    """Parse a .env file and set variables into os.environ."""
    if not path.exists():
        return
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip()
            # Strip surrounding quotes
            if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
                value = value[1:-1]
            if key not in os.environ:  # don't override existing env vars
                os.environ[key] = value


# ---------------------------------------------------------------------------
# Telegram
# ---------------------------------------------------------------------------

def send_telegram(message: str, token: str, chat_id: str) -> bool:
    """Send a Telegram message. Returns True on success."""
    if not token or not chat_id:
        log.warning("Telegram credentials not configured")
        return False
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    # Escape underscores so Telegram doesn't consume them as italic
    escaped = message.replace("_", "\\_")
    for attempt_msg in (escaped, message):
        body = {
            "chat_id": chat_id,
            "text": attempt_msg[:4096],
            "parse_mode": "Markdown",
        }
        payload = json.dumps(body).encode()
        req = urllib.request.Request(
            url, data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.status == 200
        except urllib.error.HTTPError as e:
            if e.code == 400 and attempt_msg == escaped:
                log.info("Escaped Markdown failed, retrying with original")
                continue
            log.warning("Telegram send failed: %s", e)
            return False
        except Exception as e:
            log.warning("Telegram send failed: %s", e)
            return False
    return False


# ---------------------------------------------------------------------------
# Auditor state DB (deduplication)
# ---------------------------------------------------------------------------

def get_auditor_db() -> sqlite3.Connection:
    """Open auditor_state.db with busy timeout."""
    conn = sqlite3.connect(str(AUDITOR_DB_PATH), timeout=10)
    conn.execute("PRAGMA busy_timeout=10000")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS alert_history (
            alert_type TEXT,
            alert_key TEXT,
            first_seen TEXT,
            last_alerted TEXT,
            times_seen INTEGER DEFAULT 1,
            resolved INTEGER DEFAULT 0,
            PRIMARY KEY (alert_type, alert_key)
        )
    """)
    conn.commit()
    return conn


def should_alert(auditor_db: sqlite3.Connection, alert_type: str, alert_key: str) -> bool:
    """Check if this alert should fire (not alerted in last DEDUP_HOURS)."""
    now = datetime.now(timezone.utc).isoformat()
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=DEDUP_HOURS)).isoformat()
    row = auditor_db.execute(
        "SELECT last_alerted FROM alert_history WHERE alert_type=? AND alert_key=?",
        (alert_type, alert_key),
    ).fetchone()
    if row is None:
        auditor_db.execute(
            "INSERT INTO alert_history (alert_type, alert_key, first_seen, last_alerted) VALUES (?,?,?,?)",
            (alert_type, alert_key, now, now),
        )
        auditor_db.commit()
        return True
    if row[0] < cutoff:
        auditor_db.execute(
            "UPDATE alert_history SET last_alerted=?, times_seen=times_seen+1, resolved=0 WHERE alert_type=? AND alert_key=?",
            (now, alert_type, alert_key),
        )
        auditor_db.commit()
        return True
    # Already alerted within window — increment counter but don't re-alert
    auditor_db.execute(
        "UPDATE alert_history SET times_seen=times_seen+1 WHERE alert_type=? AND alert_key=?",
        (alert_type, alert_key),
    )
    auditor_db.commit()
    return False


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

def get_state_db() -> sqlite3.Connection | None:
    """Open state.db read-only. Returns None if missing."""
    if not STATE_DB_PATH.exists():
        log.error("state.db not found at %s", STATE_DB_PATH)
        return None
    conn = sqlite3.connect(f"file:{STATE_DB_PATH}?mode=ro", uri=True, timeout=10)
    conn.execute("PRAGMA busy_timeout=10000")
    conn.row_factory = sqlite3.Row
    return conn


def table_exists(db: sqlite3.Connection, table: str) -> bool:
    row = db.execute(
        "SELECT count(*) FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    return row[0] > 0


def get_columns(db: sqlite3.Connection, table: str) -> list[str]:
    rows = db.execute(f"PRAGMA table_info({table})").fetchall()
    return [r["name"] for r in rows]


def has_column(db: sqlite3.Connection, table: str, col: str) -> bool:
    return col in get_columns(db, table)


# ---------------------------------------------------------------------------
# CHECK FUNCTIONS
# Each returns a list of (alert_type, alert_key, message) tuples.
# Empty list = all good.
# ---------------------------------------------------------------------------

# ── Category 1: Trade Distribution ──────────────────────────────────────────

def check_missing_sides(db: sqlite3.Connection, verbose: bool) -> list[tuple[str, str, str]]:
    """1a. Check for missing YES/NO side distribution."""
    alerts = []
    if not table_exists(db, "settled_trades") or not has_column(db, "settled_trades", "side"):
        if verbose:
            print("  SKIP: settled_trades or side column missing")
        return alerts

    cutoff = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
    rows = db.execute(
        "SELECT side, count(*) as cnt FROM settled_trades WHERE settled_at >= ? GROUP BY side",
        (cutoff,),
    ).fetchall()
    side_counts = {r["side"]: r["cnt"] for r in rows}
    total = sum(side_counts.values())

    if verbose:
        print(f"  Side distribution (7d): {dict(side_counts)}, total={total}")

    # NOTE: NO-side is shadow-only by design — the bot only trades YES live.
    # Only alert if NO-side trades appear unexpectedly (would indicate a bug).
    all_no = db.execute(
        "SELECT count(*) as cnt FROM settled_trades WHERE side='no'"
    ).fetchone()["cnt"]
    if all_no > 0:
        alerts.append((
            "unexpected_no_side",
            "no_side_live",
            "🔍 *AUDITOR ALERT: Unexpected NO-Side Trade*\n\n"
            f"Found {all_no} NO-side settled trades. The bot should only trade YES live.\n\n"
            "Suggested action: Check if NO-side was accidentally promoted from shadow.",
        ))
    return alerts


def check_missing_assets(db: sqlite3.Connection, verbose: bool) -> list[tuple[str, str, str]]:
    """1b. Check for assets with zero evaluated opportunities in 48h."""
    alerts = []
    if not table_exists(db, "evaluated_opportunities"):
        return alerts

    active_assets = ["BTC", "ETH", "SOL", "XRP", "HYPE", "DOGE"]
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=48)).isoformat()
    rows = db.execute(
        "SELECT asset, count(*) as cnt FROM evaluated_opportunities "
        "WHERE evaluation_time >= ? AND product_type = '15m' GROUP BY asset",
        (cutoff,),
    ).fetchall()
    asset_counts = {r["asset"]: r["cnt"] for r in rows}
    has_activity = any(v > 0 for v in asset_counts.values())

    if verbose:
        print(f"  Asset activity (48h, 15m): {dict(asset_counts)}")

    if has_activity:
        for asset in active_assets:
            if asset_counts.get(asset, 0) == 0:
                alerts.append((
                    "missing_asset",
                    f"no_eval_{asset}",
                    f"🔍 *AUDITOR ALERT: Missing Asset*\n\n"
                    f"{asset} has zero evaluated opportunities in 48h while other assets are active.\n\n"
                    f"Active assets: {asset_counts}\n\n"
                    f"Suggested action: Check if {asset} markets are being discovered.",
                ))
    return alerts


def check_missing_product_types(db: sqlite3.Connection, verbose: bool) -> list[tuple[str, str, str]]:
    """1c. Check that enabled product types are producing entries."""
    alerts = []
    if not table_exists(db, "evaluated_opportunities"):
        return alerts

    cutoff = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
    rows = db.execute(
        "SELECT product_type, count(*) as cnt FROM evaluated_opportunities "
        "WHERE evaluation_time >= ? GROUP BY product_type",
        (cutoff,),
    ).fetchall()
    pt_counts = {r["product_type"]: r["cnt"] for r in rows}

    if verbose:
        print(f"  Product type activity (24h): {dict(pt_counts)}")

    # 15m should always have activity (it's the main product)
    if pt_counts.get("15m", 0) == 0:
        alerts.append((
            "missing_product_type",
            "15m_zero",
            "🔍 *AUDITOR ALERT: Missing Product Type*\n\n"
            "15m product type has zero evaluated opportunities in 24h.\n\n"
            "Suggested action: Check if 15m market discovery is working.",
        ))
    return alerts


# ── Category 2: Data Integrity ──────────────────────────────────────────────

def check_null_columns(db: sqlite3.Connection, verbose: bool) -> list[tuple[str, str, str]]:
    """2a. Check for critical columns that are 100% NULL in recent data."""
    alerts = []
    if not table_exists(db, "evaluated_opportunities"):
        return alerts

    cutoff = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
    total = db.execute(
        "SELECT count(*) as cnt FROM evaluated_opportunities WHERE evaluation_time >= ?",
        (cutoff,),
    ).fetchone()["cnt"]

    if total == 0:
        if verbose:
            print("  SKIP: No evaluated_opportunities in 24h")
        return alerts

    # Columns that should NOT be 100% null
    critical_cols = [
        "calibrated_prob", "edge", "asset", "product_type", "filter_stage",
        "market_price", "seconds_to_close", "spot_price", "volatility",
    ]
    existing_cols = get_columns(db, "evaluated_opportunities")

    for col in critical_cols:
        if col not in existing_cols:
            continue
        null_count = db.execute(
            f"SELECT count(*) as cnt FROM evaluated_opportunities "
            f"WHERE evaluation_time >= ? AND {col} IS NULL",
            (cutoff,),
        ).fetchone()["cnt"]
        if null_count == total:
            alerts.append((
                "null_column",
                f"eval_{col}",
                f"🔍 *AUDITOR ALERT: NULL Column*\n\n"
                f"Column `{col}` is 100% NULL in last 24h evaluated\\_opportunities ({total} rows).\n\n"
                f"Suggested action: Check if the column is being populated in bot.py inserts.",
            ))
            if verbose:
                print(f"  ALERT: {col} is 100% NULL ({total} rows)")
        elif verbose:
            print(f"  OK: {col} — {total - null_count}/{total} non-null")

    return alerts


def check_pnl_math(db: sqlite3.Connection, verbose: bool) -> list[tuple[str, str, str]]:
    """2b. Verify PnL math on recent settled trades."""
    alerts = []
    if not table_exists(db, "settled_trades"):
        return alerts

    required = ["ticker", "side", "count", "entry_price_cents", "revenue_cents",
                 "fee_cents", "pnl_cents", "market_result"]
    existing = get_columns(db, "settled_trades")
    if not all(c in existing for c in required):
        if verbose:
            print("  SKIP: settled_trades missing required columns")
        return alerts

    cutoff = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
    rows = db.execute(
        "SELECT ticker, side, count, entry_price_cents, revenue_cents, "
        "fee_cents, pnl_cents, market_result FROM settled_trades WHERE settled_at >= ?",
        (cutoff,),
    ).fetchall()

    if verbose:
        print(f"  Checking PnL math on {len(rows)} trades")

    for r in rows:
        ticker = r["ticker"]
        side = r["side"]
        contracts = r["count"]
        entry = r["entry_price_cents"]
        revenue = r["revenue_cents"]
        fee = r["fee_cents"]
        pnl = r["pnl_cents"]
        result = r["market_result"]

        if any(v is None for v in [side, contracts, entry, revenue, fee, pnl]):
            continue

        # Compute expected: pnl = revenue - (entry_price * contracts for YES, (100-entry)*contracts for NO) - fees
        if side == "yes":
            cost = entry * contracts
        else:
            cost = (100 - entry) * contracts

        expected_pnl = revenue - cost - fee
        deviation = abs(pnl - expected_pnl)

        if deviation > 50:  # > $0.50 deviation
            alerts.append((
                "pnl_math",
                ticker,
                f"🔍 *AUDITOR ALERT: PnL Mismatch*\n\n"
                f"Trade `{ticker}` PnL doesn't match expected calculation.\n"
                f"Side: {side}, Contracts: {contracts}, Entry: {entry}c\n"
                f"Recorded PnL: {pnl}c, Expected: ~{expected_pnl}c (Δ{deviation}c)\n\n"
                f"Suggested action: Check settlement logic for this trade.",
            ))
        elif verbose:
            print(f"  OK: {ticker} pnl={pnl}c expected={expected_pnl}c")

    return alerts


def check_duplicate_trades(db: sqlite3.Connection, verbose: bool) -> list[tuple[str, str, str]]:
    """2c. Check for duplicate event_ticker + side in settled trades."""
    alerts = []
    if not table_exists(db, "settled_trades"):
        return alerts
    if not has_column(db, "settled_trades", "event_ticker") or not has_column(db, "settled_trades", "side"):
        return alerts

    # Only check 15m — hourly legitimately allows multiple positions per window
    rows = db.execute(
        "SELECT event_ticker, side, count(*) as cnt FROM settled_trades "
        "WHERE product_type = '15m' "
        "GROUP BY event_ticker, side HAVING cnt > 1"
    ).fetchall()

    if verbose:
        print(f"  Duplicate check: {len(rows)} duplicate groups found")

    for r in rows:
        alerts.append((
            "duplicate_trade",
            f"{r['event_ticker']}_{r['side']}",
            f"🔍 *AUDITOR ALERT: Duplicate Trades*\n\n"
            f"Event `{r['event_ticker']}` has {r['cnt']} {r['side']}-side trades.\n\n"
            f"Suggested action: Check position accumulation logic.",
        ))
    return alerts


def check_schema_drift(db: sqlite3.Connection, verbose: bool) -> list[tuple[str, str, str]]:
    """2d. Check column counts against expected values."""
    alerts = []
    for table, expected in EXPECTED_COLUMN_COUNTS.items():
        if not table_exists(db, table):
            if verbose:
                print(f"  SKIP: Table {table} doesn't exist")
            continue
        cols = get_columns(db, table)
        actual = len(cols)
        if actual != expected:
            alerts.append((
                "schema_drift",
                f"{table}_{actual}",
                f"🔍 *AUDITOR ALERT: Schema Drift*\n\n"
                f"Table `{table}` has {actual} columns (expected {expected}).\n\n"
                f"Suggested action: Update EXPECTED\\_COLUMN\\_COUNTS in auditor.py if intentional.",
            ))
        elif verbose:
            print(f"  OK: {table} has {actual} columns (expected {expected})")
    return alerts


# ── Category 3: Performance Anomaly ─────────────────────────────────────────

def check_win_rate_deviation(db: sqlite3.Connection, verbose: bool) -> list[tuple[str, str, str]]:
    """3a. Check if rolling 20-trade win rate deviates from historical."""
    alerts = []
    if not table_exists(db, "settled_trades"):
        return alerts
    if not has_column(db, "settled_trades", "product_type"):
        return alerts

    # Historical win rate for 15m
    hist = db.execute(
        "SELECT count(*) as total, "
        "sum(CASE WHEN pnl_cents > 0 THEN 1 ELSE 0 END) as wins "
        "FROM settled_trades WHERE product_type='15m'"
    ).fetchone()

    if hist["total"] < 30:
        if verbose:
            print(f"  SKIP: Only {hist['total']} 15m trades (need 30+)")
        return alerts

    hist_wr = hist["wins"] / hist["total"]

    # Last 20 trades
    recent = db.execute(
        "SELECT pnl_cents FROM settled_trades WHERE product_type='15m' "
        "ORDER BY settled_at DESC LIMIT 20"
    ).fetchall()

    if len(recent) < 20:
        if verbose:
            print(f"  SKIP: Only {len(recent)} recent 15m trades (need 20)")
        return alerts

    recent_wins = sum(1 for r in recent if r["pnl_cents"] > 0)
    recent_wr = recent_wins / len(recent)

    if verbose:
        print(f"  15m WR: historical={hist_wr:.1%} ({hist['wins']}/{hist['total']}), "
              f"rolling-20={recent_wr:.1%} ({recent_wins}/{len(recent)})")

    if hist_wr - recent_wr > 0.15:  # 15pp drop
        alerts.append((
            "wr_deviation",
            "15m_rolling",
            f"🔍 *AUDITOR ALERT: Win Rate Drop*\n\n"
            f"15M rolling win rate dropped to {recent_wr:.0%} (last 20 trades) "
            f"vs historical {hist_wr:.0%}.\n\n"
            f"Suggested action: Investigate recent losses for pattern.",
        ))
    return alerts


def check_consecutive_losses(db: sqlite3.Connection, verbose: bool) -> list[tuple[str, str, str]]:
    """3b. Check for 3+ consecutive 15m losses."""
    alerts = []
    if not table_exists(db, "settled_trades"):
        return alerts

    rows = db.execute(
        "SELECT ticker, pnl_cents, settled_at FROM settled_trades "
        "WHERE product_type='15m' ORDER BY settled_at DESC LIMIT 20"
    ).fetchall()

    streak = 0
    loss_tickers = []
    for r in rows:
        if r["pnl_cents"] < 0:
            streak += 1
            loss_tickers.append(f"`{r['ticker']}` ({r['pnl_cents']}c)")
        else:
            break

    if verbose:
        print(f"  Current loss streak: {streak}")

    if streak >= 3:
        details = ", ".join(loss_tickers[:5])
        alerts.append((
            "consec_losses",
            f"streak_{streak}_{rows[0]['settled_at'][:10]}",
            f"🔍 *AUDITOR ALERT: Consecutive Losses*\n\n"
            f"{streak} consecutive 15M losses detected:\n{details}\n\n"
            f"Suggested action: Check for market regime change or model degradation.",
        ))
    return alerts


def check_unusual_loss(db: sqlite3.Connection, verbose: bool) -> list[tuple[str, str, str]]:
    """3c. Check for unusually large single losses in last 24h."""
    alerts = []
    if not table_exists(db, "settled_trades"):
        return alerts

    # Average loss magnitude
    avg_loss = db.execute(
        "SELECT avg(abs(pnl_cents)) as avg_loss FROM settled_trades "
        "WHERE pnl_cents < 0 AND product_type='15m'"
    ).fetchone()

    if avg_loss["avg_loss"] is None:
        if verbose:
            print("  SKIP: No historical losses to compare against")
        return alerts

    avg = avg_loss["avg_loss"]
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
    recent_losses = db.execute(
        "SELECT ticker, pnl_cents, entry_price_cents FROM settled_trades "
        "WHERE pnl_cents < 0 AND settled_at >= ? AND product_type='15m'",
        (cutoff,),
    ).fetchall()

    if verbose:
        print(f"  Avg loss magnitude: {avg:.0f}c, recent losses: {len(recent_losses)}")

    for r in recent_losses:
        loss_mag = abs(r["pnl_cents"])
        if loss_mag > 2 * avg:
            alerts.append((
                "unusual_loss",
                r["ticker"],
                f"🔍 *AUDITOR ALERT: Large Loss*\n\n"
                f"Unusually large loss: ${loss_mag / 100:.2f} on `{r['ticker']}`\n"
                f"Entry: {r['entry_price_cents']}c, avg loss: ${avg / 100:.2f}\n\n"
                f"Suggested action: Review this trade for anomalies.",
            ))
    return alerts


# ── Category 4: Operational Health ──────────────────────────────────────────

def check_bot_activity(db: sqlite3.Connection, verbose: bool) -> list[tuple[str, str, str]]:
    """4a. Check if bot is producing evaluated opportunities recently."""
    alerts = []
    if not table_exists(db, "evaluated_opportunities"):
        return alerts

    cutoff = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    count = db.execute(
        "SELECT count(*) as cnt FROM evaluated_opportunities WHERE evaluation_time >= ?",
        (cutoff,),
    ).fetchone()["cnt"]

    if verbose:
        print(f"  Evaluated opportunities in last 2h: {count}")

    if count == 0:
        alerts.append((
            "bot_activity",
            "no_evals_2h",
            "🔍 *AUDITOR ALERT: Bot Inactive*\n\n"
            "No evaluated opportunities in last 2 hours — bot may be down or stuck.\n\n"
            "Suggested action: Check systemd status (`systemctl status kalshi-bot`).",
        ))
    return alerts


def check_unsettled_trades(db: sqlite3.Connection, verbose: bool) -> list[tuple[str, str, str]]:
    """4b. Check for trades that should have settled but haven't."""
    alerts = []
    if not table_exists(db, "positions"):
        return alerts

    # Positions with status 'open' — check if they're stale
    rows = db.execute(
        "SELECT ticker, asset, side, count, avg_price_cents, status FROM positions "
        "WHERE status = 'open'"
    ).fetchall()

    if verbose:
        print(f"  Open positions: {len(rows)}")

    # We can't easily determine window close time from just the positions table,
    # but we can flag positions that have been open for a suspiciously long time
    # by checking if there are no recent evaluated_opportunities for that ticker
    for r in rows:
        ticker = r["ticker"]
        # Check if any recent activity for this ticker
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
        recent = db.execute(
            "SELECT count(*) as cnt FROM evaluated_opportunities "
            "WHERE ticker = ? AND evaluation_time >= ?",
            (ticker, cutoff),
        ).fetchone()["cnt"]

        if recent == 0:
            # No recent activity — might be unsettled
            # Only alert if the ticker looks like it should have expired
            # (15M tickers have timestamps embedded)
            alerts.append((
                "unsettled",
                ticker,
                f"🔍 *AUDITOR ALERT: Possibly Unsettled*\n\n"
                f"Position `{ticker}` ({r['asset']}, {r['side']}, {r['count']} contracts) "
                f"is open with no recent scan activity.\n\n"
                f"Suggested action: Check if this market has settled.",
            ))
    return alerts


def check_shadow_activity(db: sqlite3.Connection, verbose: bool) -> list[tuple[str, str, str]]:
    """4c. Check that shadow strategies are producing entries."""
    alerts = []
    if not table_exists(db, "evaluated_opportunities"):
        return alerts

    cutoff = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()

    # Check for shadow-related filter stages
    shadow_stages = db.execute(
        "SELECT filter_stage, count(*) as cnt FROM evaluated_opportunities "
        "WHERE evaluation_time >= ? AND filter_stage LIKE '%shadow%' "
        "GROUP BY filter_stage",
        (cutoff,),
    ).fetchall()

    shadow_counts = {r["filter_stage"]: r["cnt"] for r in shadow_stages}

    if verbose:
        print(f"  Shadow activity (24h): {dict(shadow_counts)}")

    # Check observation trades (hourly, spx, weather, sports)
    obs_types = ["hourly", "spx_hourly", "weather", "sports"]
    for pt in obs_types:
        count = db.execute(
            "SELECT count(*) as cnt FROM evaluated_opportunities "
            "WHERE evaluation_time >= ? AND product_type = ?",
            (cutoff, pt),
        ).fetchone()["cnt"]
        if verbose:
            print(f"  {pt} entries (24h): {count}")

    return alerts


def check_db_size(db: sqlite3.Connection, verbose: bool) -> list[tuple[str, str, str]]:
    """4d. Check database and journal file sizes."""
    alerts = []

    if STATE_DB_PATH.exists():
        size_mb = STATE_DB_PATH.stat().st_size / (1024 * 1024)
        if verbose:
            print(f"  state.db size: {size_mb:.1f} MB")
        if size_mb > 500:
            alerts.append((
                "db_size",
                "state_db_large",
                f"🔍 *AUDITOR ALERT: Large Database*\n\n"
                f"state.db is {size_mb:.0f} MB. Consider running VACUUM or archiving old data.\n\n"
                f"Suggested action: Check if evaluated\\_opportunities is growing unbounded.",
            ))

    if SCAN_JOURNAL_PATH.exists():
        size_mb = SCAN_JOURNAL_PATH.stat().st_size / (1024 * 1024)
        if verbose:
            print(f"  scan_journal.jsonl size: {size_mb:.1f} MB")
        if size_mb > 1500:  # ~330MB/day, rotated at 4AM UTC, can reach ~1.2GB before rotation
            alerts.append((
                "journal_size",
                "scan_journal_large",
                f"🔍 *AUDITOR ALERT: Large Journal*\n\n"
                f"scan\\_journal.jsonl is {size_mb:.0f} MB (expected ~330 MB/day).\n\n"
                f"Suggested action: Check if journal rotation cron is running.",
            ))

    return alerts


def check_rejection_spike(db: sqlite3.Connection, verbose: bool) -> list[tuple[str, str, str]]:
    """4e. Check for rejection rate spikes in the last hour vs 24h average."""
    alerts = []
    if not table_exists(db, "evaluated_opportunities"):
        return alerts

    now_utc = datetime.now(timezone.utc)
    cutoff_1h = (now_utc - timedelta(hours=1)).isoformat()
    cutoff_24h = (now_utc - timedelta(hours=24)).isoformat()

    # Last hour
    hour_total = db.execute(
        "SELECT count(*) as cnt FROM evaluated_opportunities WHERE evaluation_time >= ?",
        (cutoff_1h,),
    ).fetchone()["cnt"]
    hour_rejections = db.execute(
        "SELECT count(*) as cnt FROM evaluated_opportunities "
        "WHERE evaluation_time >= ? AND filter_stage NOT IN ('candidate', 'observation_trade')",
        (cutoff_1h,),
    ).fetchone()["cnt"]

    # 24h average (per hour)
    day_total = db.execute(
        "SELECT count(*) as cnt FROM evaluated_opportunities WHERE evaluation_time >= ?",
        (cutoff_24h,),
    ).fetchone()["cnt"]
    day_rejections = db.execute(
        "SELECT count(*) as cnt FROM evaluated_opportunities "
        "WHERE evaluation_time >= ? AND filter_stage NOT IN ('candidate', 'observation_trade')",
        (cutoff_24h,),
    ).fetchone()["cnt"]

    if day_total == 0 or hour_total == 0:
        if verbose:
            print("  SKIP: Not enough data for rejection rate analysis")
        return alerts

    hour_rate = hour_rejections / hour_total if hour_total > 0 else 0
    day_rate = day_rejections / day_total if day_total > 0 else 0

    if verbose:
        print(f"  Rejection rate: last hour={hour_rate:.1%} ({hour_rejections}/{hour_total}), "
              f"24h avg={day_rate:.1%} ({day_rejections}/{day_total})")

    # Only alert if day rate is meaningful and hour rate is 2x+
    if day_rate > 0 and hour_rate > 2 * day_rate and hour_total >= 10:
        alerts.append((
            "rejection_spike",
            f"spike_{now_utc.strftime('%Y%m%d_%H')}",
            f"🔍 *AUDITOR ALERT: Rejection Spike*\n\n"
            f"Rejection rate spiked to {hour_rate:.0%} in last hour (24h avg: {day_rate:.0%}).\n"
            f"Last hour: {hour_rejections}/{hour_total}, 24h: {day_rejections}/{day_total}\n\n"
            f"Suggested action: Check if market conditions changed or a filter is too aggressive.",
        ))
    return alerts


# ── Category 5: Calibration Sanity ──────────────────────────────────────────

def check_model_vs_reality(db: sqlite3.Connection, verbose: bool) -> list[tuple[str, str, str]]:
    """5a. Compare predicted probability vs actual win rate."""
    alerts = []
    if not table_exists(db, "settled_trades"):
        return alerts
    if not table_exists(db, "evaluated_opportunities"):
        return alerts

    # Get settled candidate trades with their predicted probabilities
    cutoff = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()

    # Join settled_trades with evaluated_opportunities on ticker to get predictions
    rows = db.execute(
        "SELECT st.ticker, st.pnl_cents, eo.calibrated_prob "
        "FROM settled_trades st "
        "JOIN evaluated_opportunities eo ON st.ticker = eo.ticker "
        "WHERE st.settled_at >= ? AND st.product_type = '15m' "
        "AND eo.filter_stage IN ('candidate', 'observation_trade') "
        "AND eo.calibrated_prob IS NOT NULL",
        (cutoff,),
    ).fetchall()

    if len(rows) < 20:
        if verbose:
            print(f"  SKIP: Only {len(rows)} matched trades (need 20+)")
        return alerts

    avg_pred = sum(r["calibrated_prob"] for r in rows) / len(rows)
    wins = sum(1 for r in rows if r["pnl_cents"] > 0)
    actual_wr = wins / len(rows)

    if verbose:
        print(f"  Model vs reality (7d, n={len(rows)}): avg predicted={avg_pred:.1%}, "
              f"actual WR={actual_wr:.1%}, gap={avg_pred - actual_wr:+.1%}")

    if abs(avg_pred - actual_wr) > 0.05:  # 5pp divergence
        direction = "overconfident" if avg_pred > actual_wr else "underconfident"
        alerts.append((
            "model_divergence",
            f"15m_{direction}",
            f"🔍 *AUDITOR ALERT: Model Divergence*\n\n"
            f"15M model appears {direction} (7d, n={len(rows)}):\n"
            f"Avg predicted: {avg_pred:.1%}, Actual WR: {actual_wr:.1%} "
            f"(gap: {abs(avg_pred - actual_wr):.1%})\n\n"
            f"Suggested action: Review calibration engine performance.",
        ))
    return alerts


def check_price_zone_gaps(db: sqlite3.Connection, verbose: bool) -> list[tuple[str, str, str]]:
    """5b. Check for dead price zones in evaluated opportunities."""
    alerts = []
    if not table_exists(db, "evaluated_opportunities"):
        return alerts

    cutoff = (datetime.now(timezone.utc) - timedelta(hours=48)).isoformat()
    rows = db.execute(
        "SELECT market_price FROM evaluated_opportunities "
        "WHERE evaluation_time >= ? AND product_type = '15m' AND market_price IS NOT NULL",
        (cutoff,),
    ).fetchall()

    if len(rows) < 50:
        if verbose:
            print(f"  SKIP: Only {len(rows)} 15m evals (need 50+)")
        return alerts

    # Bucket prices
    buckets = {
        "86-88": (86, 88),
        "88-90": (88, 90),
        "90-92": (90, 92),
        "92-94": (92, 94),
        "94-96": (94, 96),
        "96-99": (96, 99),
    }
    bucket_counts = {}
    for name, (lo, hi) in buckets.items():
        count = sum(1 for r in rows if lo <= r["market_price"] <= hi)
        bucket_counts[name] = count

    if verbose:
        print(f"  Price zone distribution (48h): {bucket_counts}")

    # Check for gaps — a bucket with 0 when adjacent buckets have entries
    bucket_names = list(buckets.keys())
    for i, name in enumerate(bucket_names):
        if bucket_counts[name] == 0:
            # Check if adjacent buckets have entries
            has_adjacent = False
            if i > 0 and bucket_counts[bucket_names[i - 1]] > 0:
                has_adjacent = True
            if i < len(bucket_names) - 1 and bucket_counts[bucket_names[i + 1]] > 0:
                has_adjacent = True
            if has_adjacent:
                alerts.append((
                    "price_zone_gap",
                    f"gap_{name}",
                    f"🔍 *AUDITOR ALERT: Price Zone Gap*\n\n"
                    f"No opportunities evaluated in price zone {name}c in 48h "
                    f"while adjacent zones are active.\n"
                    f"Distribution: {bucket_counts}\n\n"
                    f"Suggested action: Check if a filter is blocking this zone.",
                ))
    return alerts


def check_edge_trend(db: sqlite3.Connection, verbose: bool) -> list[tuple[str, str, str]]:
    """5c. Check if average edge is declining over the last 7 days."""
    alerts = []
    if not table_exists(db, "evaluated_opportunities"):
        return alerts
    if not has_column(db, "evaluated_opportunities", "edge"):
        return alerts

    now_utc = datetime.now(timezone.utc)
    cutoff_7d = (now_utc - timedelta(days=7)).isoformat()
    cutoff_3_5d = (now_utc - timedelta(days=3.5)).isoformat()

    # First half of week
    first_half = db.execute(
        "SELECT avg(edge) as avg_edge, count(*) as cnt FROM evaluated_opportunities "
        "WHERE evaluation_time >= ? AND evaluation_time < ? "
        "AND filter_stage IN ('candidate', 'observation_trade') "
        "AND product_type = '15m' AND edge IS NOT NULL",
        (cutoff_7d, cutoff_3_5d),
    ).fetchone()

    # Second half of week
    second_half = db.execute(
        "SELECT avg(edge) as avg_edge, count(*) as cnt FROM evaluated_opportunities "
        "WHERE evaluation_time >= ? "
        "AND filter_stage IN ('candidate', 'observation_trade') "
        "AND product_type = '15m' AND edge IS NOT NULL",
        (cutoff_3_5d,),
    ).fetchone()

    if first_half["cnt"] < 10 or second_half["cnt"] < 10:
        if verbose:
            print(f"  SKIP: Not enough data for edge trend "
                  f"(first={first_half['cnt']}, second={second_half['cnt']})")
        return alerts

    if first_half["avg_edge"] is None or second_half["avg_edge"] is None:
        return alerts

    edge_1 = first_half["avg_edge"]
    edge_2 = second_half["avg_edge"]

    if verbose:
        print(f"  Edge trend: first half={edge_1:.2%} (n={first_half['cnt']}), "
              f"second half={edge_2:.2%} (n={second_half['cnt']})")

    if edge_1 > 0 and (edge_1 - edge_2) / edge_1 > 0.30:  # 30% decline
        alerts.append((
            "edge_decline",
            f"decline_{now_utc.strftime('%Y%m%d')}",
            f"🔍 *AUDITOR ALERT: Edge Declining*\n\n"
            f"Average edge declining: {edge_1:.2%} (first half of week) → "
            f"{edge_2:.2%} (second half).\n"
            f"Drop: {(edge_1 - edge_2) / edge_1:.0%}\n\n"
            f"Suggested action: Check if market efficiency is increasing or model is stale.",
        ))
    return alerts


def check_hourly_live_health(db, verbose):
    """Hourly live: WR, constraint violations, PnL monitoring.

    Constraints split by side (YES/NO asymmetry, commit ca89d7b 2026-04-15):
    - YES-side: BTC+ETH only, entry 50-59c, count=25 (HOURLY_FIXED_CONTRACTS).
      Excluded: SOL (marginal), XRP (42.2% YES WR = toxic).
    - NO-side: all 4 assets eligible (HOURLY_NO_EXCLUDED_ASSETS=set()),
      entry 40-54c, count=1 (HOURLY_NO_FIXED_CONTRACTS verification).
      All assets show positive model edge on NO-side per Apr 15 audit.
    """
    alerts = []
    try:
        # Check for constraint violations in last 24h
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
        violations = db.execute(
            "SELECT ticker, asset, side, entry_price_cents, count "
            "FROM settled_trades WHERE product_type='hourly' AND settled_at > ? "
            "AND ("
            # YES-side constraints
            "  (side='yes' AND ("
            "    asset NOT IN ('BTC','ETH') OR "
            "    entry_price_cents < 50 OR entry_price_cents > 59 OR "
            "    count != 25"
            "  ))"
            "  OR "
            # NO-side constraints (all 4 assets, 40-54c, count=1)
            "  (side='no' AND ("
            "    asset NOT IN ('BTC','ETH','SOL','XRP') OR "
            "    entry_price_cents < 40 OR entry_price_cents > 54 OR "
            "    count != 1"
            "  ))"
            ")",
            (cutoff,),
        ).fetchall()
        if violations:
            alerts.append((
                "hourly_constraint_violation",
                "hourly_violation_24h",
                f"🚨 *AUDITOR ALERT: Hourly Constraint Violation*\n\n"
                f"{len(violations)} trades violating constraints "
                f"(YES: BTC/ETH 50-59c ct=25; NO: any asset 40-54c ct=1).\n"
                f"First: {dict(violations[0]) if violations else 'N/A'}",
            ))
        # Rolling 20-trade WR
        recent = db.execute(
            "SELECT pnl_cents FROM settled_trades "
            "WHERE product_type='hourly' ORDER BY settled_at DESC LIMIT 20"
        ).fetchall()
        if len(recent) >= 10:
            wins = sum(1 for r in recent if r["pnl_cents"] > 0)
            wr = wins / len(recent)
            if verbose:
                print(f"  Hourly live: {wins}/{len(recent)} = {wr:.1%} WR (last {len(recent)} trades)")
            if wr < 0.50:
                alerts.append((
                    "hourly_low_wr",
                    f"hourly_wr_{len(recent)}",
                    f"⚠️ *AUDITOR ALERT: Hourly WR Below 50%*\n\n"
                    f"Rolling {len(recent)}-trade WR: {wr:.1%} ({wins}W/{len(recent)-wins}L).\n"
                    f"Sub-60c breakeven is ~46.5%. Consider pausing if trend continues.",
                ))
    except Exception as e:
        if verbose:
            print(f"  Hourly live check failed: {e}")
    return alerts


# ── Category 7: Sizing Sanity ─────────────────────────────────────────────

def check_drawdown_scaler_health(db: sqlite3.Connection, verbose: bool) -> list[tuple[str, str, str]]:
    """7a. Check if drawdown scaler is compressed (undersizing trades).

    Queries the most recent drawdown_scaler from ANY evaluation (not just
    candidates) to avoid false positives during quiet periods when no
    candidates are generated. Includes a 15-minute freshness gate to ignore
    stale pre-restart data. (Learned: false positive on first run after
    deploy — auditor read pre-restart candidate with ds=0.25. Mar 29 2026.)
    """
    alerts = []
    if not table_exists(db, "evaluated_opportunities"):
        return alerts

    # Query ANY evaluation with non-null ds (not just candidates).
    # Candidates are sparse on quiet weekends; non-candidate evals (shadows,
    # rejections) also record ds and are much more frequent.
    row = db.execute(
        "SELECT drawdown_scaler, available_balance_cents, evaluation_time "
        "FROM evaluated_opportunities "
        "WHERE drawdown_scaler IS NOT NULL "
        "  AND evaluation_time > datetime('now', '-2 hours') "
        "ORDER BY evaluation_time DESC LIMIT 1"
    ).fetchone()

    if row is None:
        if verbose:
            print("  No recent evaluations with drawdown_scaler in last 2h")
        return alerts

    ds = row["drawdown_scaler"]
    bal = row["available_balance_cents"]
    ts = row["evaluation_time"]

    if verbose:
        print(f"  Most recent ds={ds:.2f} bal=${bal / 100:.2f} at {ts[:19]}")

    # Freshness gate: ignore data older than 15 minutes. After a restart,
    # the warmup takes ~50s (5 readings × 10s). A 15-min gate ensures we
    # only alert on post-warmup data from the current session.
    try:
        eval_dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        age_minutes = (datetime.now(timezone.utc) - eval_dt).total_seconds() / 60
    except (ValueError, TypeError):
        age_minutes = 0

    if age_minutes > 15:
        if verbose:
            print(f"  ds data is {age_minutes:.0f}m old — skipping (may be pre-restart)")
        return alerts

    if ds < 0.50:
        alerts.append((
            "sizing",
            f"ds_critical_{int(ds * 100)}",
            f"\U0001f6a8 *AUDITOR ALERT: Drawdown Scaler Critical*\n\n"
            f"ds={ds:.2f} — main pipeline **quarter-sizing or worse**.\n"
            f"Balance: ${bal / 100:.2f}.\n"
            f"Likely cause: HWM inflated by position exposure.\n\n"
            f"Suggested action: Check HWM vs cash balance. "
            f"Consider `OVERRIDE_HWM` env var if HWM is stale.",
        ))
    elif ds < 0.90:
        alerts.append((
            "sizing",
            f"ds_warning_{int(ds * 100)}",
            f"\u26a0\ufe0f *AUDITOR ALERT: Drawdown Scaler Compressed*\n\n"
            f"ds={ds:.2f} — main pipeline undersizing.\n"
            f"Balance: ${bal / 100:.2f}.\n\n"
            f"Suggested action: Monitor — may resolve naturally or indicate HWM issue.",
        ))

    return alerts


# ── Category 8: SPX Pipeline Health ──────────────────────────────────────────

def check_spx_pipeline_health(db: sqlite3.Connection, verbose: bool) -> list[tuple[str, str, str]]:
    """8a. Check SPX HAR-RV shadow is producing signals during market hours."""
    alerts = []

    # Only alert Mon-Fri during/after US market hours (14:30-21:00 UTC = 9:30-16:00 ET)
    now_utc = datetime.now(timezone.utc)
    if now_utc.weekday() >= 5:  # Sat/Sun — no SPX markets
        if verbose:
            print("  SPX pipeline: weekend — skipping")
        return alerts

    # Check HAR-RV shadow signals in the last 4 hours
    if not table_exists(db, "spx_harrv_shadow_signals"):
        if verbose:
            print("  SPX pipeline: spx_harrv_shadow_signals table not found")
        return alerts

    cutoff = (now_utc - timedelta(hours=4)).isoformat()
    row = db.execute(
        "SELECT COUNT(*) as cnt, MAX(evaluation_time) as latest "
        "FROM spx_harrv_shadow_signals WHERE evaluation_time >= ?",
        (cutoff,),
    ).fetchone()
    cnt = row["cnt"]
    latest = row["latest"]

    if verbose:
        print(f"  SPX HAR-RV signals (4h): {cnt}, latest: {latest or 'none'}")

    # Check Finnhub health via price freshness
    # If no signals in 4h during a weekday, the price feed is likely down
    if cnt == 0 and 14 <= now_utc.hour <= 21:
        alerts.append((
            "spx_pipeline_stale",
            "spx_harrv_no_signals_4h",
            "⚠️ *AUDITOR ALERT: SPX HAR-RV Pipeline Stale*\n\n"
            "0 HAR-RV shadow signals in the last 4 hours during market hours.\n"
            "Likely cause: Finnhub WebSocket disconnected or SPX engine not running.\n"
            "Check: `systemctl status kalshi-bot` and Finnhub WS logs.",
        ))

    # Check for zero-return dominance (the bug we're fixing)
    if cnt > 0:
        zero_rv = db.execute(
            "SELECT COUNT(*) as cnt FROM spx_harrv_shadow_signals "
            "WHERE evaluation_time >= ? AND (rv_1h IS NULL OR rv_1h = 0)",
            (cutoff,),
        ).fetchone()["cnt"]
        if zero_rv > 0 and zero_rv == cnt:
            alerts.append((
                "spx_harrv_zero_rv",
                "spx_harrv_all_zero_rv_4h",
                "⚠️ *AUDITOR ALERT: SPX HAR-RV All Zero RV*\n\n"
                f"All {cnt} signals in the last 4h have rv_1h=0 or NULL.\n"
                "Likely cause: Finnhub WS delivering identical prices (zero returns).\n"
                "The zero-return filter should prevent this — check bot/shadows/spx_harrv_shadow.py.",
            ))

    return alerts


def check_stacking_health(db: sqlite3.Connection, verbose: bool) -> list[tuple[str, str, str]]:
    """Check stacking performance and alert thresholds."""
    alerts = []
    try:
        row = db.execute(
            "SELECT COUNT(*) as n, COALESCE(SUM(pnl_cents - COALESCE(fee_cents, 0)), 0) as pnl "
            "FROM settled_trades WHERE is_stacked = 1"
        ).fetchone()
        n = row["n"]
        pnl = row["pnl"]
        if verbose:
            print(f"  Stacking: {n} trades, PnL ${pnl/100:.2f}")
        if pnl < -2500:  # -$25 alert (NET, post-fee — R-p7-deploy-r9 fee-fix). Originally calibrated when pnl was gross; threshold may need widening if stacking volume grows + fees compound.
            alerts.append((
                "stacking_pnl_alert",
                f"stacking_pnl_{n}",
                f"\u26a0\ufe0f *AUDITOR ALERT: Stacking PnL Warning*\n\n"
                f"Cumulative stacking PnL: ${pnl/100:.2f} on {n} trades.\n"
                f"Alert threshold: -$25.00",
            ))
        if pnl < -7500:  # -$75 kill recommendation (NET, post-fee).
            alerts.append((
                "stacking_kill_recommendation",
                f"stacking_kill_{n}",
                f"\U0001f6a8 *AUDITOR ALERT: Stacking Kill Recommended*\n\n"
                f"Cumulative stacking PnL: ${pnl/100:.2f} on {n} trades.\n"
                f"Recommendation: Set STACKING_ENABLED=0 in VPS .env",
            ))
        if n == 50:
            alerts.append((
                "stacking_review",
                "stacking_50_review",
                f"\U0001f4ca *Stacking Milestone: 50 Trades*\n\n"
                f"50 stacked trades settled. PnL: ${pnl/100:.2f}.\n"
                f"Manual review recommended before scaling.",
            ))
    except Exception as e:
        if verbose:
            print(f"  Stacking health check failed: {e}")
    return alerts


# ---------------------------------------------------------------------------
# Check registry
# ---------------------------------------------------------------------------

CHECKS = [
    # Category 1: Trade Distribution
    ("trade_distribution", check_missing_sides),
    ("trade_distribution", check_missing_assets),
    ("trade_distribution", check_missing_product_types),
    # Category 2: Data Integrity
    ("data_integrity", check_null_columns),
    ("data_integrity", check_pnl_math),
    ("data_integrity", check_duplicate_trades),
    ("data_integrity", check_schema_drift),
    # Category 3: Performance Anomaly
    ("performance", check_win_rate_deviation),
    ("performance", check_consecutive_losses),
    ("performance", check_unusual_loss),
    # Category 4: Operational Health
    ("operational", check_bot_activity),
    ("operational", check_unsettled_trades),
    ("operational", check_shadow_activity),
    ("operational", check_db_size),
    ("operational", check_rejection_spike),
    # Category 5: Calibration Sanity
    ("calibration", check_model_vs_reality),
    ("calibration", check_price_zone_gaps),
    ("calibration", check_edge_trend),
    # Category 6: Hourly Live
    ("hourly", check_hourly_live_health),
    # Category 7: Sizing Sanity
    ("sizing", check_drawdown_scaler_health),
    # Category 8: SPX Pipeline
    ("spx", check_spx_pipeline_health),
    # Category 9: Stacking
    ("stacking", check_stacking_health),
    # --- Add new checks here ---
    # ("category", check_function),
    # Future: Plug in Claude API analysis (Layer 2)
    # Future: Auto-fix proposal generation
]


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def run_checks(
    category_filter: str | None = None,
    verbose: bool = False,
    test_telegram: bool = False,
) -> int:
    """Run all checks, deduplicate, and send alerts. Returns number of alerts sent."""
    load_dotenv(ENV_PATH)

    tg_token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    tg_chat = os.environ.get("TELEGRAM_CHAT_ID", "")

    if test_telegram:
        ok = send_telegram(
            "🔍 *AUDITOR TEST*\n\nTelegram integration is working.",
            tg_token, tg_chat,
        )
        print(f"Telegram test: {'SUCCESS' if ok else 'FAILED'}")
        return 1 if ok else 0

    # Open databases
    state_db = get_state_db()
    if state_db is None:
        log.error("Cannot proceed without state.db")
        return 0

    auditor_db = get_auditor_db()
    now_utc = datetime.now(timezone.utc)

    # Collect all alerts
    all_alerts: list[tuple[str, str, str]] = []  # (type, key, message)
    checks_run = 0
    checks_passed = 0

    for category, check_fn in CHECKS:
        if category_filter and category != category_filter:
            continue
        check_name = check_fn.__name__
        if verbose:
            print(f"\n[{category}] {check_name}:")
        try:
            results = check_fn(state_db, verbose)
            checks_run += 1
            if not results:
                checks_passed += 1
            else:
                all_alerts.extend(results)
        except Exception as e:
            log.warning("Check %s failed: %s", check_name, e, exc_info=True)
            checks_run += 1

    state_db.close()

    # Deduplicate alerts
    new_alerts = []
    for alert_type, alert_key, message in all_alerts:
        if should_alert(auditor_db, alert_type, alert_key):
            new_alerts.append(message)

    if verbose:
        print(f"\n{'=' * 60}")
        print(f"Checks run: {checks_run}, passed: {checks_passed}, "
              f"total alerts: {len(all_alerts)}, new (deduplicated): {len(new_alerts)}")

    # Send alerts to Telegram (max MAX_TG_MESSAGES)
    sent = 0
    if new_alerts:
        if len(new_alerts) <= MAX_TG_MESSAGES:
            for msg in new_alerts:
                if send_telegram(msg, tg_token, tg_chat):
                    sent += 1
                    time.sleep(0.5)  # Avoid Telegram rate limits
        else:
            # Batch: send first (MAX_TG_MESSAGES - 1) individually, summarize the rest
            for msg in new_alerts[: MAX_TG_MESSAGES - 1]:
                if send_telegram(msg, tg_token, tg_chat):
                    sent += 1
                    time.sleep(0.5)
            remaining = len(new_alerts) - (MAX_TG_MESSAGES - 1)
            summary = (
                f"🔍 *AUDITOR: +{remaining} more alerts*\n\n"
                f"Run `python3 bot/ai/auditor.py --verbose` to see all {len(new_alerts)} alerts."
            )
            if send_telegram(summary, tg_token, tg_chat):
                sent += 1

    # Daily all-clear (only at midnight UTC hour)
    elif now_utc.hour == 0 and checks_run > 0:
        send_telegram(
            f"✅ Auditor: All checks passed ({checks_run} checks, "
            f"{now_utc.strftime('%H:%M')} UTC)",
            tg_token, tg_chat,
        )
        sent = 1

    auditor_db.close()

    if verbose and sent > 0:
        print(f"Sent {sent} Telegram messages")

    return sent


def main():
    parser = argparse.ArgumentParser(description="Kalshi Bot Deterministic Auditor")
    parser.add_argument("--verbose", "-v", action="store_true", help="Print all check results")
    parser.add_argument("--check", type=str, help="Run only one category: trade_distribution, data_integrity, performance, operational, calibration")
    parser.add_argument("--test-telegram", action="store_true", help="Send a test Telegram message")
    args = parser.parse_args()

    run_checks(
        category_filter=args.check,
        verbose=args.verbose,
        test_telegram=args.test_telegram,
    )


if __name__ == "__main__":
    main()
