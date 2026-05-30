"""READ-ONLY live WS probe — isolate the delta-less incremental-subscribe bug.

Ticket 86ba76adw. The sub-hourly incremental subscribe (#152/#153) captures
SNAPSHOT-ONLY bronze (0 deltas) for crypto-15M windows. Boot/session_start
subscribes stream deltas fine. This probe reproduces the scenario on a single
live WS session to isolate which mechanism is at fault:

  Group A — subscribed at session_start, one `subscribe` per ticker (bot-style).  [control]
  Group B — subscribed MID-SESSION (+12s), one `subscribe` per ticker (bot-style).
  Group C — subscribed MID-SESSION (+24s), BATCHED one frame many tickers (collector-style).

Verdict:
  - A streams deltas, B does NOT  -> mid-session `subscribe` itself is broken (Kalshi semantics).
  - A & B stream, C does NOT      -> the BATCHED mid-session subscribe is the bug.
  - A & B & C all stream deltas    -> mid-session subscribe is fine; the collector bug is
                                      cmd_id-RESET/reuse or re-subscribe churn (orchestration).

READ-ONLY: subscribes to orderbook_delta only; places NO orders. One WS conn, ~55s.
Run on the VPS (creds live there; private key never leaves the box):
  cd ~/kalshi-bot-repo && set -a && . .env && set +a && \
    KALSHI_API_KEY_ID=$KALSHI_API_KEY_ID KALSHI_PRIVATE_KEY_PATH=$KALSHI_PRIVATE_KEY_PATH \
    python3 scripts/research/incremental_subscribe_probe.py
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


SERIES = ("KXBTC15M", "KXETH15M", "KXSOL15M", "KXXRP15M",
          "KXHYPE15M", "KXDOGE15M", "KXBNB15M")


def fetch_active_crypto15m(api_key, pk, n=9):
    """Return up to n open crypto-15M market tickers across all 7 series,
    most-time-to-close first (so they don't settle mid-probe)."""
    out = []
    for series in SERIES:
        headers = make_rest_headers(api_key, pk, "GET", MARKETS_PATH)
        r = requests.get(REST_BASE + MARKETS_PATH,
                         params={"series_ticker": series, "status": "open", "limit": 200},
                         headers=headers, timeout=15)
        r.raise_for_status()
        out.extend(r.json().get("markets", []))
    # Most time-to-close first (close_time may be ISO or epoch; fall back to volume).
    def keyfn(m):
        return m.get("close_time") or m.get("expiration_time") or ""
    out.sort(key=keyfn, reverse=True)
    return [m["ticker"] for m in out[:n]]


def main():
    api_key = os.environ.get("KALSHI_API_KEY") or os.environ["KALSHI_API_KEY_ID"]
    pk = load_private_key(os.environ["KALSHI_PRIVATE_KEY_PATH"])

    tickers = fetch_active_crypto15m(api_key, pk, n=9)
    if len(tickers) < 3:
        print(f"FAIL: only {len(tickers)} open BTC15M markets; need >=3. {tickers}")
        return 1
    # split into 3 groups (A control / B mid one-per / C mid batched)
    grpA = tickers[0:3]
    grpB = tickers[3:6]
    grpC = tickers[6:9]
    group_of = {}
    for t in grpA: group_of[t] = "A"
    for t in grpB: group_of[t] = "B"
    for t in grpC: group_of[t] = "C"
    print(f"A (session_start, per-ticker): {grpA}")
    print(f"B (mid +12s, per-ticker)     : {grpB}")
    print(f"C (mid +24s, BATCHED)        : {grpC}")

    t0 = time.time()
    # per-ticker: {snapshot, delta} counts + first-delta offset
    tally = defaultdict(lambda: {"snapshot": 0, "delta": 0, "first_delta_s": None})

    def on_frame(frame):
        try:
            inner = json.loads(frame.raw)
        except Exception:
            return
        typ = inner.get("type")
        tk = inner.get("msg", {}).get("market_ticker", "")
        if tk not in group_of:
            return
        if typ == "orderbook_snapshot":
            tally[tk]["snapshot"] += 1
        elif typ == "orderbook_delta":
            tally[tk]["delta"] += 1
            if tally[tk]["first_delta_s"] is None:
                tally[tk]["first_delta_s"] = round(time.time() - t0, 1)

    def sub_one(ws, tk, cmd_id):
        ws.send_frame({"id": cmd_id, "cmd": "subscribe",
                       "params": {"channels": ["orderbook_delta"], "market_tickers": [tk]}})

    def on_session_start():
        # Group A: one subscribe per ticker at session_start (bot-style control).
        for i, tk in enumerate(grpA):
            sub_one(ws, tk, 11 + i)
        print(f"[{time.time()-t0:4.1f}s] session_start: subscribed A {grpA}")

    ws = WSClient(api_key=api_key, private_key=pk, on_frame=on_frame,
                  on_session_start=on_session_start, parse_on_demand=True)
    ws.start()

    # wait for connect
    for _ in range(100):
        if ws.is_connected:
            break
        time.sleep(0.1)
    if not ws.is_connected:
        print("FAIL: WS did not connect in 10s (conn limit? auth?)")
        ws.stop()
        return 1

    time.sleep(12)
    for i, tk in enumerate(grpB):  # B: mid-session, one subscribe per ticker
        sub_one(ws, tk, 21 + i)
    print(f"[{time.time()-t0:4.1f}s] mid-session: subscribed B per-ticker {grpB}")

    time.sleep(12)  # T+24
    ws.send_frame({"id": 31, "cmd": "subscribe",  # C: mid-session, BATCHED (collector-style)
                   "params": {"channels": ["orderbook_delta"], "market_tickers": grpC}})
    print(f"[{time.time()-t0:4.1f}s] mid-session: subscribed C BATCHED {grpC}")

    time.sleep(25)  # observe deltas for all groups
    ws.stop()

    print(f"\n{'group':6}{'ticker':34}{'snap':6}{'delta':7}{'firstDelta':12}")
    for grp, tks in (("A", grpA), ("B", grpB), ("C", grpC)):
        for tk in tks:
            t = tally[tk]
            fd = f"{t['first_delta_s']}s" if t["first_delta_s"] is not None else "-"
            print(f"{grp:6}{tk:34}{t['snapshot']:6}{t['delta']:7}{fd:>12}")
    # verdict
    def grp_deltas(tks): return sum(tally[t]["delta"] for t in tks)
    a, b, c = grp_deltas(grpA), grp_deltas(grpB), grp_deltas(grpC)
    print(f"\nTOTAL DELTAS  A={a}  B={b}  C={c}")
    if a == 0:
        print("INCONCLUSIVE: control group A got 0 deltas (markets inactive / probe issue).")
    elif b == 0:
        print("VERDICT: mid-session `subscribe` itself yields NO deltas (Kalshi semantics) — even per-ticker.")
    elif c == 0:
        print("VERDICT: per-ticker mid-session works (B), but BATCHED mid-session (C) yields NO deltas — the collector's batched add is the bug.")
    else:
        print("VERDICT: A, B, C all stream deltas — mid-session subscribe is fine; the collector bug is cmd_id-RESET/reuse or re-subscribe churn (orchestration).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
