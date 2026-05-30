"""READ-ONLY live WS probe v2 — pin cmd_id-reuse vs re-subscribe-churn.

Ticket 86ba76adw. Probe v1 proved mid-session `subscribe` streams deltas
(per-ticker AND batched) with CLEAN cmd_ids → the collector bug is orchestration.
This run isolates WHICH orchestration fault, by reproducing the two things the
collector does that the working bot does not:

  M1 @ session_start, cmd_id=100                  [control: must stream deltas]
  M2 @ +10s, cmd_id=100  (REUSED id, fresh ticker) [cmd_id-reuse test]
  M3 @ +20s, cmd_id=200  (fresh id, fresh ticker)  [mid-session control]
  M1 re-subscribe @ +30s, cmd_id=300 (SAME ticker) [churn test: does M1 keep streaming?]

Deltas are bucketed by 5s window so we can see whether M1's stream BREAKS after
the +30s re-subscribe. READ-ONLY (orderbook_delta only, no orders), one conn, ~50s.
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


def fetch(api_key, pk, n=3):
    out = []
    for series in SERIES:
        h = make_rest_headers(api_key, pk, "GET", MARKETS_PATH)
        r = requests.get(REST_BASE + MARKETS_PATH,
                         params={"series_ticker": series, "status": "open", "limit": 200},
                         headers=h, timeout=15)
        r.raise_for_status()
        out.extend(m["ticker"] for m in r.json().get("markets", []))
        if len(out) >= n:
            break
    return out[:n]


def main():
    api_key = os.environ.get("KALSHI_API_KEY") or os.environ["KALSHI_API_KEY_ID"]
    pk = load_private_key(os.environ["KALSHI_PRIVATE_KEY_PATH"])
    tk = fetch(api_key, pk, 3)
    if len(tk) < 3:
        print(f"FAIL: need 3 open markets, got {tk}")
        return 1
    M1, M2, M3 = tk
    print(f"M1(ctrl)={M1}  M2(reuse-id)={M2}  M3(fresh-id)={M3}")
    t0 = time.time()
    # per-ticker delta count per 5s bucket
    buckets = defaultdict(lambda: defaultdict(int))
    snaps = defaultdict(int)

    def on_frame(frame):
        try:
            inner = json.loads(frame.raw)
        except Exception:
            return
        m = inner.get("msg", {}).get("market_ticker", "")
        if m not in (M1, M2, M3):
            return
        if inner.get("type") == "orderbook_snapshot":
            snaps[m] += 1
        elif inner.get("type") == "orderbook_delta":
            buckets[m][int((time.time() - t0) // 5)] += 1

    def sub(tkr, cid):
        ws.send_frame({"id": cid, "cmd": "subscribe",
                       "params": {"channels": ["orderbook_delta"], "market_tickers": [tkr]}})

    ws = WSClient(api_key=api_key, private_key=pk, on_frame=on_frame,
                  on_session_start=lambda: sub(M1, 100), parse_on_demand=True)
    ws.start()
    for _ in range(100):
        if ws.is_connected:
            break
        time.sleep(0.1)
    if not ws.is_connected:
        print("FAIL: no connect"); ws.stop(); return 1
    print(f"[{time.time()-t0:4.1f}s] M1 subscribed @ session_start cmd_id=100")

    time.sleep(10); sub(M2, 100)
    print(f"[{time.time()-t0:4.1f}s] M2 subscribed mid-session cmd_id=100 (REUSED)")
    time.sleep(10); sub(M3, 200)
    print(f"[{time.time()-t0:4.1f}s] M3 subscribed mid-session cmd_id=200 (fresh)")
    time.sleep(10); sub(M1, 300)
    print(f"[{time.time()-t0:4.1f}s] M1 RE-subscribed cmd_id=300 (same ticker / churn)")
    time.sleep(15)
    ws.stop()

    nb = int((time.time() - t0) // 5) + 1
    print(f"\nDeltas per 5s bucket (col header = bucket start sec):")
    print(f"{'ticker':34}{'snaps':6}  " + "".join(f"{b*5:>5}" for b in range(nb)))
    for label, m in (("M1(ctrl/churn)", M1), ("M2(reuse-id)", M2), ("M3(fresh-id)", M3)):
        row = "".join(f"{buckets[m].get(b,0):>5}" for b in range(nb))
        print(f"{m:34}{snaps[m]:6}  {row}")
    print("\nKEY EVENTS: M2 subscribe ~10s, M3 ~20s, M1 re-subscribe ~30s")
    m1_tot = sum(buckets[M1].values()); m2_tot = sum(buckets[M2].values()); m3_tot = sum(buckets[M3].values())
    print(f"TOTAL DELTAS  M1={m1_tot}  M2={m2_tot}  M3={m3_tot}")
    if m2_tot == 0 and m3_tot > 0:
        print("VERDICT: REUSED cmd_id (M2) gets NO deltas while fresh id (M3) does → cmd_id-REUSE is the bug.")
    elif m2_tot > 0:
        print("VERDICT: reused cmd_id (M2) STILL streams → cmd_id reuse is NOT the bug; suspect re-subscribe churn.")
    # churn check on M1: did deltas continue in the last buckets (after +30s re-sub)?
    last2 = sum(buckets[M1].get(b, 0) for b in range(max(0, nb - 3), nb))
    print(f"M1 deltas in last ~15s (after re-subscribe @30s): {last2}  "
          f"({'CONTINUED — re-subscribe did NOT break stream' if last2 > 0 else 'STOPPED — re-subscribe BROKE the stream'})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
