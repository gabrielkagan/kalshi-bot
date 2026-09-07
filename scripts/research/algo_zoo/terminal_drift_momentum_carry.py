"""terminal_drift_momentum_carry — terminal-window momentum/carry confirmation (directional TAKER).

MECHANISM (a CONFIRMATION trade, NOT a forecast):
  In the final FINAL_WINDOW_S seconds before close, take a directional TAKER
  position ONLY when THREE independent signals AGREE:
    (1) FLOW: signed Kalshi trade-flow over the trailing window (taker buy-vs-sell
        print imbalance on YES) points one direction;
    (2) SPOT: coinbase spot drift over the same trailing window points the SAME
        direction (spot rising favors YES=above, falling favors NO);
    (3) CARRY: the contract is still MID-priced (not at a rail) so price-to-rail
        (payout left) exceeds the taker fee + spread by a margin.
  When flow AND spot drift AND carry-room all align, we cross the spread as a
  TAKER (worst-case fee) in the agreed direction and hold to settlement.

FILL MODEL: TAKER cross at the reliable book ask (buy YES) / NO-ask=100-bid (buy
  NO) as of decision time. Full Kalshi taker fee ceil(0.07*C*P*(1-P)). NO maker
  assumption (the conservative model; maker rebate = 0).

LABEL: settlement outcome derived from the TERMINAL BOOK mid at close (reliable
  book mid >= 50 => YES settled). DB cross-check (evaluated_opportunities) where
  the ticker is present.

NO LOOK-AHEAD: the signal book + flow + spot use only data with ts <= decision
  epoch. The label book is reconstructed independently at the terminal cutoff.
  reliable_nbbo_at refuses drifted books -> no trade (refusal honored).

CI: block bootstrap clustered by TICKER (the window is the independent unit).
EDGE only if net-of-fee taker PnL-per-trade CI lower bound > 0.

Corpus: ~31h local bronze (2026-05-30T10Z -> 05-31T17Z), crypto-15M.
Spot only BTC/ETH/SOL/XRP and only from 05-30T21:00Z onward.
"""
from __future__ import annotations

import bisect
import json
import math
import sys
from collections import defaultdict
from datetime import datetime, timezone

sys.path.insert(0, "/Users/gabrielkagan/Documents/kalshi-bot")

import numpy as np

from scripts.research.kalshi_book_reconstruct import reliable_nbbo_at
from scripts.research.phase1b_real_price_economics import (
    close_epoch_from_ticker,
    load_outcomes_db,
)

FRAMES_PATH = "/tmp/edge_daily/frames_crypto.jsonl"
TRADES_PATH = "/tmp/edge_daily/trades_crypto.jsonl"
SPOT_PATH = "/tmp/edge_daily/coinbase_spot.jsonl"
DB_PATH = "/tmp/edge_daily/state.db"

# Spot only for these 4 assets (post-21:00Z).
SPOT_ASSETS = {"BTC": "BTC-USD", "ETH": "ETH-USD", "SOL": "SOL-USD", "XRP": "XRP-USD"}

# --- decision geometry (within the 2-5 min terminal window) ------------------
DECISION_OFFSET_S = 240.0   # decide at close - 240s (4 min before close)
TRAIL_S = 120.0             # trailing window for flow + spot drift (2 min)

# --- triple-gate thresholds (baseline) ---------------------------------------
FLOW_MIN_IMBALANCE = 0.20   # |signed-flow| / total-flow >= this
FLOW_MIN_CONTRACTS = 5.0    # min volume in the trailing window
SPOT_DRIFT_MIN_BPS = 2.0    # |spot drift| over trailing window, bps
CARRY_MID_LO = 25.0         # decision-time YES-mid must be in [LO, HI]
CARRY_MID_HI = 75.0
CARRY_MIN_NET_ROOM_C = 3.0  # (room_to_rail - fee - spread) >= this many cents

N_BOOT = 2000
RNG = np.random.default_rng(20260531)


def _epoch_iso(s: str) -> float:
    s = s.strip()
    if s.endswith("Z"):
        s = s[:-1]
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        dt = datetime.strptime(s[:26], "%Y-%m-%dT%H:%M:%S.%f")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def taker_fee_cents(price_cents: float, count: float = 1.0) -> float:
    """Kalshi taker fee = ceil(0.07 * C * P * (1-P)) cents, P in dollars."""
    p = max(0.0, min(1.0, price_cents / 100.0))
    return math.ceil(0.07 * count * p * (1.0 - p))


def asset_of(ticker: str):
    for a in SPOT_ASSETS:
        if ticker.startswith(f"KX{a}15M-"):
            return a
    return None


# Substrings that identify a 4-asset crypto-15M frame line cheaply (the ticker
# lives inside the escaped _raw, so we match the escaped form).
_ASSET_TAGS = tuple(f'KX{a}15M-' for a in SPOT_ASSETS)

# We only need frames in a window around each ticker's close: the decision at
# close-DECISION_OFFSET_S and the terminal label at close. reliable_nbbo_at
# needs a snapshot anchor before the cutoff; snapshots recur ~per-second on
# active 15M books, so a generous lookback guarantees one. Retain only this
# window to keep the in-memory frame dict tiny (the full 9.77M-row dict OOMs an
# 8GB box).
_FRAME_KEEP_BEFORE_S = 600.0   # keep frames from close-600s ...
_FRAME_KEEP_AFTER_S = 10.0     # ... to close+10s


def load_frames_windowed(path, spot_start_epoch):
    """Streaming, memory-bounded loader. Keeps frames ONLY for 4-asset
    crypto-15M tickers whose close is in the spot-covered window, and ONLY the
    frames within [close-600s, close+10s]. Returns {ticker: sorted[(epoch,inner)]}.
    """
    frames = defaultdict(list)
    close_cache = {}
    kept = scanned = 0
    with open(path) as f:
        for line in f:
            scanned += 1
            # cheap asset prefilter on the raw line
            hit = False
            for tag in _ASSET_TAGS:
                if tag in line:
                    hit = True
                    break
            if not hit:
                continue
            try:
                env = json.loads(line)
                inner = json.loads(env["_raw"])
            except (ValueError, KeyError):
                continue
            tk = inner.get("msg", {}).get("market_ticker", "")
            if asset_of(tk) is None:
                continue
            ce = close_cache.get(tk)
            if ce is None:
                try:
                    ce = close_epoch_from_ticker(tk)
                except Exception:
                    ce = -1.0
                close_cache[tk] = ce
            if ce < 0:
                continue
            # only spot-covered windows: decision epoch must be after spot_start
            if (ce - DECISION_OFFSET_S - TRAIL_S) < spot_start_epoch:
                continue
            try:
                rt = env["_wire_recv_ts"]
                recv = _epoch_iso(rt)
            except (KeyError, ValueError):
                continue
            if recv < ce - _FRAME_KEEP_BEFORE_S or recv > ce + _FRAME_KEEP_AFTER_S:
                continue
            frames[tk].append((recv, inner))
            kept += 1
    for tk in frames:
        frames[tk].sort(key=lambda x: x[0])
    print(f"  scanned {scanned} lines, kept {kept} frames across "
          f"{len(frames)} tickers", flush=True)
    return frames


def load_spot():
    """{asset: sorted [(epoch, mid)]}. Flat-JSON coinbase file (mid/bid/ask)."""
    by_asset = defaultdict(list)
    prod2asset = {v: k for k, v in SPOT_ASSETS.items()}
    with open(SPOT_PATH) as f:
        for line in f:
            if not line.strip():
                continue
            try:
                d = json.loads(line)
            except ValueError:
                continue
            a = prod2asset.get(d.get("product_id"))
            if a is None:
                continue
            mid = d.get("mid")
            if mid is None:
                b, k = d.get("bid"), d.get("ask")
                if b is None or k is None:
                    continue
                mid = (float(b) + float(k)) / 2.0
            try:
                by_asset[a].append((_epoch_iso(d["ts"]), float(mid)))
            except (KeyError, ValueError):
                continue
    for a in by_asset:
        by_asset[a].sort(key=lambda x: x[0])
    return by_asset


def load_trades_for_flow():
    """{ticker: sorted [(ts, signed)]}. signed = +count (taker bought YES) or
    -count (taker bought NO = sold YES). Signed flow imbalance."""
    by_ticker = defaultdict(list)
    with open(TRADES_PATH) as f:
        for line in f:
            if not line.strip():
                continue
            try:
                env = json.loads(line)
                msg = json.loads(env["_raw"])["msg"]
            except (ValueError, KeyError):
                continue
            tk = msg.get("market_ticker", "")
            if "15M-" not in tk:
                continue
            try:
                ts = float(msg["ts"])
                cnt = float(msg["count_fp"])
                side = msg["taker_side"]
            except (KeyError, ValueError):
                continue
            by_ticker[tk].append((ts, cnt if side == "yes" else -cnt))
    for tk in by_ticker:
        by_ticker[tk].sort(key=lambda x: x[0])
    return by_ticker


def spot_at(series, t):
    """Last spot mid at or before t (no look-ahead)."""
    if not series:
        return None
    epochs = [e for e, _ in series]
    i = bisect.bisect_right(epochs, t) - 1
    return series[i][1] if i >= 0 else None


def run(decision_offset=DECISION_OFFSET_S, trail_s=TRAIL_S,
        flow_imb=FLOW_MIN_IMBALANCE, flow_min=FLOW_MIN_CONTRACTS,
        spot_min_bps=SPOT_DRIFT_MIN_BPS, mid_lo=CARRY_MID_LO, mid_hi=CARRY_MID_HI,
        min_net_room=CARRY_MIN_NET_ROOM_C, frames=None, flow=None, spot=None,
        db_out=None, verbose=True):
    cand = [(tk, asset_of(tk)) for tk in frames if asset_of(tk) is not None]

    trades = []
    n_eval = n_no_spot = n_flow_fail = n_spot_fail = 0
    n_no_agree = n_carry_fail = n_label_fail = 0

    for tk, a in cand:
        close_ep = close_epoch_from_ticker(tk)
        decide_ep = close_ep - decision_offset
        trail_lo = decide_ep - trail_s
        fr = frames[tk]

        bid_c, ask_c = reliable_nbbo_at(fr, decide_ep)
        if bid_c is None or ask_c is None:
            continue
        n_eval += 1
        mid_c = (bid_c + ask_c) / 2.0
        spread_c = ask_c - bid_c
        if spread_c < 0:
            continue

        ser = spot.get(a, [])
        s_now = spot_at(ser, decide_ep)
        s_then = spot_at(ser, trail_lo)
        if s_now is None or s_then is None or s_then <= 0:
            n_no_spot += 1
            continue
        drift_bps = (s_now - s_then) / s_then * 1e4
        spot_dir = 0
        if drift_bps >= spot_min_bps:
            spot_dir = +1
        elif drift_bps <= -spot_min_bps:
            spot_dir = -1

        net = tot = 0.0
        for ts, signed in flow.get(tk, []):
            if trail_lo <= ts <= decide_ep:
                net += signed
                tot += abs(signed)
        flow_dir = 0
        if tot >= flow_min and abs(net) / tot >= flow_imb:
            flow_dir = +1 if net > 0 else -1

        if flow_dir == 0:
            n_flow_fail += 1
            continue
        if spot_dir == 0:
            n_spot_fail += 1
            continue
        if flow_dir != spot_dir:
            n_no_agree += 1
            continue
        direction = flow_dir  # +1 buy YES, -1 buy NO

        if direction > 0:
            entry_c = ask_c
        else:
            entry_c = 100.0 - bid_c  # NO-ask
        room_to_rail = 100.0 - entry_c
        if not (mid_lo <= mid_c <= mid_hi):
            n_carry_fail += 1
            continue
        fee = taker_fee_cents(entry_c, 1.0)
        if (room_to_rail - fee - spread_c) < min_net_room:
            n_carry_fail += 1
            continue

        lb_bid, lb_ask = reliable_nbbo_at(fr, close_ep)
        if lb_bid is None or lb_ask is None:
            n_label_fail += 1
            continue
        term_mid = (lb_bid + lb_ask) / 2.0
        settled_yes = term_mid >= 50.0

        db = db_out.get(tk) if db_out else None
        db_yes = (db["result"] == "yes") if db else None

        win = settled_yes if direction > 0 else (not settled_yes)
        pnl = (100.0 if win else 0.0) - entry_c - fee

        trades.append({
            "ticker": tk, "asset": a, "direction": direction,
            "entry_c": entry_c, "fee": fee, "spread": spread_c,
            "drift_bps": drift_bps, "term_mid": term_mid,
            "settled_yes": settled_yes, "win": win, "pnl": pnl, "db_yes": db_yes,
        })

    if verbose:
        print("\n=== FUNNEL ===")
        print(f"  candidate windows (4-asset):  {len(cand)}")
        print(f"  reliable book @ decision:     {n_eval}")
        print(f"  dropped no spot:              {n_no_spot}")
        print(f"  dropped flow gate:            {n_flow_fail}")
        print(f"  dropped spot gate:            {n_spot_fail}")
        print(f"  dropped flow/spot disagree:   {n_no_agree}")
        print(f"  dropped carry:                {n_carry_fail}")
        print(f"  dropped label book:           {n_label_fail}")
        print(f"  CONFIRMED TRADES:             {len(trades)}")
    return trades


def bootstrap_ci(trades):
    by_tk = defaultdict(list)
    for t in trades:
        by_tk[t["ticker"]].append(t["pnl"])
    clusters = [np.array(v) for v in by_tk.values()]
    n_cl = len(clusters)
    boot = np.empty(N_BOOT)
    for b in range(N_BOOT):
        idx = RNG.integers(0, n_cl, n_cl)
        boot[b] = np.concatenate([clusters[i] for i in idx]).mean()
    return float(np.percentile(boot, 2.5)), float(np.percentile(boot, 97.5)), n_cl


def main():
    print("Loading spot...", flush=True)
    spot = load_spot()
    print(f"  spot: { {a: len(v) for a, v in spot.items()} }", flush=True)
    spot_start = min((ser[0][0] for ser in spot.values() if ser), default=0.0)
    from datetime import datetime as _dt
    print(f"  spot_start={_dt.fromtimestamp(spot_start, tz=timezone.utc)}", flush=True)

    print("Loading trades (flow)...", flush=True)
    flow = load_trades_for_flow()
    print(f"  {len(flow)} tickers with trades", flush=True)

    print("Loading frames (windowed, mem-bounded)...", flush=True)
    frames = load_frames_windowed(FRAMES_PATH, spot_start)
    print(f"  {len(frames)} tickers with frames", flush=True)

    cand_tk = {tk for tk in frames if asset_of(tk) is not None}
    db_out = load_outcomes_db(DB_PATH, cand_tk)
    print(f"  DB outcomes for {len(db_out)} of {len(cand_tk)} candidates", flush=True)

    trades = run(frames=frames, flow=flow, spot=spot, db_out=db_out)

    if len(trades) < 2:
        print("\nToo few trades -> INCONCLUSIVE")
        return

    pnls = np.array([t["pnl"] for t in trades])
    wins = np.array([1.0 if t["win"] else 0.0 for t in trades])
    lo, hi, n_cl = bootstrap_ci(trades)
    print(f"\n  mean PnL/trade (net fee, cents): {pnls.mean():.3f}")
    print(f"  win rate:                        {wins.mean():.3f}")
    print(f"  total PnL (cents):               {pnls.sum():.1f}")
    print(f"  clusters (tickers):              {n_cl}")
    print(f"  Bootstrap 95% CI: [{lo:.3f}, {hi:.3f}] cents/trade")

    chk = [(t["settled_yes"], t["db_yes"]) for t in trades if t["db_yes"] is not None]
    if chk:
        agree = sum(1 for s, d in chk if s == d) / len(chk)
        print(f"  DB label agreement: {agree:.3f} (n={len(chk)})")

    verdict = "EDGE" if lo > 0 else "NO_EDGE"
    if len(trades) < 30 or n_cl < 10:
        verdict = "INCONCLUSIVE"
    print(f"\n  VERDICT: {verdict}")

    print("\n  per-asset:")
    for a in sorted(set(t["asset"] for t in trades)):
        ap = [t["pnl"] for t in trades if t["asset"] == a]
        print(f"    {a}: n={len(ap)} mean={np.mean(ap):.2f}")

    # Sensitivity sweep over a couple of looser/tighter gates (informational).
    print("\n=== SENSITIVITY (looser gates) ===")
    for off in (300.0, 180.0):
        for imb in (0.20, 0.40):
            for db_bps in (2.0, 5.0):
                tr = run(decision_offset=off, flow_imb=imb, spot_min_bps=db_bps,
                         frames=frames, flow=flow, spot=spot, db_out=db_out,
                         verbose=False)
                if len(tr) >= 5:
                    p = np.array([t["pnl"] for t in tr])
                    l, h, nc = bootstrap_ci(tr)
                    print(f"  off={off:.0f} imb={imb} bps={db_bps}: "
                          f"n={len(tr)} cl={nc} mean={p.mean():.2f} CI=[{l:.2f},{h:.2f}]")
                else:
                    print(f"  off={off:.0f} imb={imb} bps={db_bps}: n={len(tr)} (too few)")


if __name__ == "__main__":
    main()
