"""READ-ONLY live WS probe v3 — test the LOADED-CONNECTION hypothesis.

Ticket 86ba76adw. Probes v1/v2 proved that on a LIGHT connection, a mid-session
`subscribe` (per-ticker, batched, reused cmd_id, re-subscribe) ALL stream deltas.
But production (heavily-loaded 7-conn universe) yields snapshot-only for
incrementally-added crypto windows — persistently (h18 + h19), even for windows
subscribed during their open life, while boot-subscribed esports on the SAME conn
stream millions of deltas.

The only remaining difference is CONNECTION LOAD. This probe reproduces it:
  1. session_start: subscribe a LARGE universe (~N markets, batched 500/frame) —
     mirrors the collector conn's boot subscription.
  2. +15s mid-session: subscribe ONE fresh crypto-15M window (the incremental add).
  3. Observe: does the late crypto window stream DELTAS, or snapshot-only?

If snapshot-only → the loaded-connection saturation IS the bug (mid-session adds on
a heavily-subscribed conn don't get a delta stream). READ-ONLY, one conn, ~50s.
"""
from __future__ import annotations

import json
import os
import time
from collections import defaultdict

import requests

from kalshi_wire.auth import load_private_key, make_rest_headers
from kalshi_wire.ws_client import WSClient

REST_BASE = "https://api.elections.kalshi.com"
MARKETS_PATH = "/trade-api/v2/markets"
SERIES = ("KXBTC15M", "KXETH15M", "KXSOL15M", "KXXRP15M", "KXHYPE15M", "KXDOGE15M", "KXBNB15M")


def fetch_universe(api_key, pk, pages=10):
    """Fetch a big chunk of open markets (any series) to load the conn."""
    out, cursor = [], None
    for _ in range(pages):
        h = make_rest_headers(api_key, pk, "GET", MARKETS_PATH)
        params = {"status": "open", "limit": 1000}
        if cursor:
            params["cursor"] = cursor
        r = requests.get(REST_BASE + MARKETS_PATH, params=params, headers=h, timeout=20)
        r.raise_for_status()
        j = r.json()
        out.extend(m["ticker"] for m in j.get("markets", []))
        cursor = j.get("cursor")
        if not cursor:
            break
    return out


def fetch_one_crypto(api_key, pk):
    for series in SERIES:
        h = make_rest_headers(api_key, pk, "GET", MARKETS_PATH)
        r = requests.get(REST_BASE + MARKETS_PATH,
                         params={"series_ticker": series, "status": "open", "limit": 200},
                         headers=h, timeout=15)
        r.raise_for_status()
        ms = r.json().get("markets", [])
        if ms:
            return ms[0]["ticker"]
    return None


def main():
    api_key = os.environ.get("KALSHI_API_KEY") or os.environ["KALSHI_API_KEY_ID"]
    pk = load_private_key(os.environ["KALSHI_PRIVATE_KEY_PATH"])

    universe = fetch_universe(api_key, pk, pages=10)
    target = fetch_one_crypto(api_key, pk)
    # ensure target isn't already in the universe batch (subscribe it fresh mid-session)
    universe = [t for t in universe if t != target]
    print(f"universe markets to pre-load: {len(universe)}  | mid-session target: {target}")
    if not target:
        print("FAIL: no open crypto-15M window"); return 1

    t0 = time.time()
    buckets = defaultdict(int); snaps = [0]

    def on_frame(frame):
        try:
            inner = json.loads(frame.raw)
        except Exception:
            return
        if inner.get("msg", {}).get("market_ticker", "") != target:
            return
        if inner.get("type") == "orderbook_snapshot":
            snaps[0] += 1
        elif inner.get("type") == "orderbook_delta":
            buckets[int((time.time() - t0) // 5)] += 1

    def on_session_start():
        # Load the conn with the universe, batched 500/frame (collector-style).
        cid = 1
        for i in range(0, len(universe), 500):
            batch = universe[i:i + 500]
            ws.send_frame({"id": cid, "cmd": "subscribe",
                           "params": {"channels": ["orderbook_delta"], "market_tickers": batch}})
            cid += 1
        print(f"[{time.time()-t0:4.1f}s] session_start: pre-loaded {len(universe)} markets in {cid-1} frames")

    ws = WSClient(api_key=api_key, private_key=pk, on_frame=on_frame,
                  on_session_start=on_session_start, parse_on_demand=True)
    ws.start()
    for _ in range(150):
        if ws.is_connected:
            break
        time.sleep(0.1)
    if not ws.is_connected:
        print("FAIL: no connect"); ws.stop(); return 1

    time.sleep(15)  # let the universe subscription settle
    ws.send_frame({"id": 999999, "cmd": "subscribe",
                   "params": {"channels": ["orderbook_delta"], "market_tickers": [target]}})
    print(f"[{time.time()-t0:4.1f}s] mid-session: subscribed crypto target on LOADED conn")
    time.sleep(30)
    ws.stop()

    nb = int((time.time() - t0) // 5) + 1
    print(f"\ntarget={target}")
    print(f"snapshots={snaps[0]}  total_deltas={sum(buckets.values())}")
    print("deltas per 5s bucket:", {b * 5: buckets.get(b, 0) for b in range(nb)})
    if sum(buckets.values()) == 0:
        print("VERDICT: CONFIRMED — mid-session subscribe on a LOADED conn yields SNAPSHOT-ONLY (0 deltas). "
              "Connection saturation is the root cause.")
    else:
        print("VERDICT: target streamed deltas even on a loaded conn → saturation at this load is NOT the cause; "
              "production load is higher OR another factor.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
