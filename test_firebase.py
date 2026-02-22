#!/usr/bin/env python3
"""Integration test for FirebasePusher — runs for 30s against real Firebase."""

import os
import sys
import time
import json
import sqlite3
import logging
import threading
from types import SimpleNamespace

# Load .env manually
env_path = os.path.join(os.path.dirname(__file__), ".env")
if os.path.exists(env_path):
    with open(env_path) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, val = line.split("=", 1)
                os.environ.setdefault(key.strip(), val.strip())

import requests
from firebase_push import FirebasePusher

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)

FIREBASE_DB_URL = os.environ.get("FIREBASE_DB_URL", "").rstrip("/")
if not FIREBASE_DB_URL:
    print("FIREBASE_DB_URL not set in .env — cannot test")
    sys.exit(1)

REQUIRED_FIELDS = [
    "timestamp", "uptime_seconds", "current_balance", "active_positions",
    "recent_trades", "win_count", "loss_count", "win_rate", "daily_pnl_cents",
    "current_volatility", "seconds_to_next_close", "bot_status",
    "last_error_message",
]

# Firebase drops null values and empty arrays — these fields may be absent on read-back
FIREBASE_DROPPABLE = {"active_positions"}


def make_mock_state():
    """Create a minimal SQLite DB matching bot's schema."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE positions (
            ticker TEXT PRIMARY KEY, event_ticker TEXT, asset TEXT,
            side TEXT, count INTEGER, avg_price_cents INTEGER,
            total_cost_cents INTEGER, status TEXT, updated_at TEXT
        );
        CREATE TABLE settled_trades (
            ticker TEXT PRIMARY KEY, event_ticker TEXT, asset TEXT,
            market_result TEXT, side TEXT, count INTEGER,
            entry_price_cents INTEGER, revenue_cents INTEGER,
            fee_cents INTEGER, pnl_cents INTEGER, settled_at TEXT
        );
    """)
    # Insert sample settled trades
    conn.execute("""
        INSERT INTO settled_trades VALUES
        ('KXBTC15M-26FEB21-T12', 'KXBTC15M-26FEB21', 'BTC', 'yes', 'yes',
         3, 88, 300, 2, 36, '2026-02-21T10:00:00Z')
    """)
    conn.execute("""
        INSERT INTO settled_trades VALUES
        ('KXETH15M-26FEB21-T08', 'KXETH15M-26FEB21', 'ETH', 'no', 'yes',
         2, 90, 0, 2, -180, '2026-02-21T11:00:00Z')
    """)
    conn.commit()

    state = SimpleNamespace()
    state.conn = conn
    state.get_open_positions = lambda asset=None: []
    return state


def make_mock_mainloop():
    """Build a fake MainLoop with all attributes FirebasePusher reads."""
    ml = SimpleNamespace()
    ml._start_time = time.time()
    ml._last_error = None
    ml._last_error_time = 0.0
    ml._active_windows = [
        {"seconds_to_close": 245.3},
        {"seconds_to_close": 545.7},
    ]

    ml.client = SimpleNamespace()
    ml.client.get_balance = lambda: {"balance": 19850}  # $198.50

    ml.state = make_mock_state()

    ml.vol = SimpleNamespace()
    ml.vol._cache = {
        "BTC": {"blended_rv": 0.00042, "regime": "normal", "rv_1min": 0.0005,
                "rv_5min": 0.0004, "rv_15min": 0.0003, "num_returns": 50,
                "jump_seconds_remaining": 0},
        "ETH": {"blended_rv": 0.00055, "regime": "elevated", "rv_1min": 0.0007,
                "rv_5min": 0.0005, "rv_15min": 0.0004, "num_returns": 48,
                "jump_seconds_remaining": 12.3},
        "SOL": None,
        "XRP": None,
    }

    ml.executor = SimpleNamespace()
    ml.executor.has_active_order = False

    return ml


def verify_snapshot_from_firebase() -> dict:
    """GET the snapshot back from Firebase and return it."""
    url = f"{FIREBASE_DB_URL}/bot_status.json"
    resp = requests.get(url, timeout=5)
    resp.raise_for_status()
    return resp.json()


def main():
    ml = make_mock_mainloop()
    pusher = FirebasePusher(ml)

    # ── Test 1: Verify snapshot fields locally ────────────────────────
    logging.info("TEST 1: Verify _build_snapshot() has all required fields")
    snap = pusher._build_snapshot()
    missing = [f for f in REQUIRED_FIELDS if f not in snap]
    if missing:
        logging.error(f"FAIL — Missing fields: {missing}")
        sys.exit(1)
    logging.info(f"PASS — All {len(REQUIRED_FIELDS)} required fields present")
    logging.info(f"  Snapshot: {json.dumps(snap, indent=2, default=str)}")

    # ── Test 2: Verify specific field values ──────────────────────────
    logging.info("TEST 2: Verify field values")
    assert snap["current_balance"] == 198.50, f"Balance wrong: {snap['current_balance']}"
    assert snap["win_count"] == 1, f"Win count wrong: {snap['win_count']}"
    assert snap["loss_count"] == 1, f"Loss count wrong: {snap['loss_count']}"
    assert snap["win_rate"] == 0.5, f"Win rate wrong: {snap['win_rate']}"
    assert snap["bot_status"] == "SCANNING", f"Status wrong: {snap['bot_status']}"
    assert snap["seconds_to_next_close"] == 245.3, f"Seconds wrong: {snap['seconds_to_next_close']}"
    assert len(snap["recent_trades"]) == 2, f"Trade count wrong: {len(snap['recent_trades'])}"
    assert snap["current_volatility"]["BTC"]["blended_rv"] == 0.00042
    assert snap["current_volatility"]["BTC"]["regime"] == "normal"
    assert snap["current_volatility"]["ETH"]["regime"] == "elevated"
    assert snap["current_volatility"]["SOL"] is None
    assert snap["last_error_message"] == ""
    assert snap["uptime_seconds"] >= 0
    logging.info("PASS — All field values correct")

    # ── Test 3: Push to Firebase and read back ────────────────────────
    logging.info("TEST 3: Push to Firebase and verify round-trip")
    pusher._push(snap)
    fb_data = verify_snapshot_from_firebase()
    # Firebase drops empty arrays and null values — only check non-droppable fields
    fb_missing = [f for f in REQUIRED_FIELDS if f not in fb_data and f not in FIREBASE_DROPPABLE]
    if fb_missing:
        logging.error(f"FAIL — Firebase missing fields: {fb_missing}")
        sys.exit(1)
    assert fb_data["current_balance"] == 198.50
    assert fb_data["bot_status"] == "SCANNING"
    assert fb_data["win_count"] == 1
    assert fb_data["loss_count"] == 1
    assert fb_data["seconds_to_next_close"] == 245.3
    logging.info("PASS — Firebase round-trip verified")
    logging.info(f"  Firebase data: {json.dumps(fb_data, indent=2, default=str)}")

    # ── Test 4: Run daemon for 30s, count pushes ─────────────────────
    logging.info("TEST 4: Running pusher daemon for 30 seconds...")
    push_count = 0
    observed_statuses = []
    original_push = pusher._push

    def counting_push(snapshot):
        nonlocal push_count
        original_push(snapshot)
        push_count += 1
        observed_statuses.append(snapshot["bot_status"])
        logging.info(f"  Push #{push_count} succeeded — status={snapshot['bot_status']}")

    pusher._push = counting_push

    # Mutate state mid-run to test different statuses
    # Timing: pushes at ~0s, ~10s, ~20s, ~30s. Mutate at 5s and 15s to land in windows.
    def mutate_state():
        time.sleep(5)
        logging.info("  >> Simulating error state")
        ml._last_error = "Connection timeout to Kalshi API"
        ml._last_error_time = time.time()
        time.sleep(10)
        logging.info("  >> Simulating trading state")
        ml._last_error = None
        ml.executor.has_active_order = True

    mutator = threading.Thread(target=mutate_state, daemon=True)
    mutator.start()

    pusher.start()
    time.sleep(30)
    pusher.stop()

    logging.info(f"  Daemon pushed {push_count} times in 30s")
    logging.info(f"  Observed statuses: {observed_statuses}")
    if push_count < 2:
        logging.error(f"FAIL — Expected at least 2 pushes, got {push_count}")
        sys.exit(1)
    logging.info("PASS — Daemon ran without crashes")

    # ── Test 5: Final Firebase read — verify last state ───────────────
    logging.info("TEST 5: Verify final Firebase state")
    final = verify_snapshot_from_firebase()
    logging.info(f"  Final bot_status: {final['bot_status']}")
    logging.info(f"  Final snapshot: {json.dumps(final, indent=2, default=str)}")
    # After mutations: executor.has_active_order=True, error cleared → TRADING
    assert final["bot_status"] == "TRADING", f"Expected TRADING, got {final['bot_status']}"
    logging.info("PASS — Final state correct (TRADING)")

    # ── Test 6: Graceful stop ─────────────────────────────────────────
    logging.info("TEST 6: Verify clean shutdown")
    assert not pusher._thread.is_alive(), "Thread still alive after stop()"
    logging.info("PASS — Thread stopped cleanly")

    logging.info("")
    logging.info("=" * 60)
    logging.info("ALL 6 TESTS PASSED")
    logging.info("=" * 60)

    # Cleanup: delete test data from Firebase
    requests.delete(f"{FIREBASE_DB_URL}/bot_status.json", timeout=5)
    logging.info("Cleaned up Firebase test data")


if __name__ == "__main__":
    main()
