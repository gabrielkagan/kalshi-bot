#!/usr/bin/env python3
"""
taker_resolution_clock_drift_fade
=================================
family: settlement_microstructure / terminal_convergence

MECHANISM
---------
In the final ~2 minutes of a Kalshi crypto-15M above/below window the outcome is
often near-determined, yet the book frequently sits mid-range (40-65c) because
makers do not re-price fast enough vs spot. We use the SIGN of recent aggressive
TAKER outcome flow as a FORWARD resolution signal, but ONLY when it contradicts
the stale book mid by a threshold, and we TAKE (cross the spread) into a real
resting ask.

SIGNAL (per ticker, one shot per window)
  - decision_time = close - 30s  (all inputs strictly <= this; NO look-ahead)
  - NTP = sum over trades in [close-120s, decision_time] of
          count_fp * (+1 if taker_outcome_side=='yes' else -1)
    (NOTE: spec window upper bound was close-15s, but that is AFTER the
     decision_time of close-30s and would be look-ahead. We cap the NTP window
     at decision_time. Stated explicitly.)
  - Reconstruct reliable_nbbo_at(decision_time); require is_reliable
    (reliable_nbbo_at returns (None,None) if the book is drifted -> skip).
  - book yes_mid = (yes_bid + yes_ask)/2 must be in [35,65]c
  - |NTP| must exceed the 70th percentile of in-sample |NTP|
  - sign(NTP) points to a side; that side's TAKE PRICE (the ask we pay) must be
    < 60c. (NTP>0 -> take YES at yes_ask; NTP<0 -> take NO at no_ask=100-yes_bid.)
  - require positive depth at the ask we cross (honest fill: a real resting quote
    must exist; if depth==0 -> skip).

LABEL (terminal book, NOT the DB; in-window settlements are sparse)
  - terminal reliable book mid at close_epoch_from_ticker via reliable_nbbo_at
  - book mid > 50c => YES wins, else NO wins.
  - if terminal book is not reliable -> drop the sample (cannot label honestly).

FEES: ceil(0.07 * C * P * (1-P)) cents/contract on ENTRY, P = entry_price/100.
  Settlement pays 100c if our taken side wins, 0 if it loses. Maker rebate = 0
  (we are the taker here; no rebate). Net per contract (C=1):
      net = (100 if win else 0) - entry_price - fee
FILL: honest taker cross to a resting ask that exists at decision_time.

HEADLINE: per-contract net cents, block bootstrap clustered by TICKER (the true
independent unit) with >=1000 resamples.

SUBSAMPLE: BTC/ETH/SOL/XRP only, first ~50 tickers per asset.
"""
import sys, os, json, math, glob
from collections import defaultdict

sys.path.insert(0, '/Users/gabrielkagan/Documents/kalshi-bot')

from scripts.research.kalshi_book_reconstruct import reliable_nbbo_at, book_at
from scripts.research.phase1b_real_price_economics import (
    load_frames_jsonl, close_epoch_from_ticker, _is_crypto_15m, _epoch,
)

import numpy as np

FRAMES_PATH = "/tmp/edge_daily/frames_crypto.jsonl"
TRADES_PATH = "/tmp/edge_daily/trades_crypto.jsonl"

ASSETS = {"BTC", "ETH", "SOL", "XRP"}
MAX_TICKERS_PER_ASSET = 50

MID_LO, MID_HI = 35.0, 65.0
TAKE_PRICE_MAX = 60.0          # only take a side priced < 60c
NTP_PCTILE = 70.0             # |NTP| must exceed this in-sample percentile
NTP_WIN_START = 120.0         # seconds before close
DECISION_LEAD = 30.0         # decision_time = close - 30s
FEE_RATE = 0.07


def fee_cents(entry_price_cents):
    p = entry_price_cents / 100.0
    return math.ceil(FEE_RATE * 1.0 * p * (1.0 - p))


def asset_of(ticker):
    return _is_crypto_15m(ticker)


def main():
    print("loading frames (4.4GB, ~9.8M rows)...", flush=True)
    frames = load_frames_jsonl(FRAMES_PATH)
    print(f"  frames for {len(frames)} crypto-15M tickers", flush=True)

    # group tickers by asset, subsample first N per asset (deterministic order)
    by_asset = defaultdict(list)
    for tk in frames:
        a = asset_of(tk)
        if a in ASSETS:
            by_asset[a].append(tk)
    chosen = set()
    for a in sorted(by_asset):
        for tk in sorted(by_asset[a])[:MAX_TICKERS_PER_ASSET]:
            chosen.add(tk)
    print(f"  chosen tickers: {len(chosen)} across {sorted(by_asset)}", flush=True)

    # load trades, keep only chosen tickers
    print("loading trades...", flush=True)
    trades_by_tk = defaultdict(list)
    with open(TRADES_PATH) as fh:
        for line in fh:
            if not line.strip():
                continue
            try:
                env = json.loads(line)
                msg = json.loads(env["_raw"])["msg"]
            except (ValueError, KeyError):
                continue
            tk = msg.get("market_ticker", "")
            if tk not in chosen:
                continue
            try:
                trades_by_tk[tk].append((
                    float(msg["ts"]),
                    msg["taker_outcome_side"],
                    float(msg["count_fp"]),
                ))
            except (KeyError, ValueError):
                continue
    for tk in trades_by_tk:
        trades_by_tk[tk].sort()
    print(f"  trades for {len(trades_by_tk)} of chosen tickers", flush=True)

    # ---- PASS 1: compute candidate features (no trade decision yet) so we can
    #      derive the in-sample 70th-pct |NTP| threshold from candidates that
    #      pass the structural gates (mid in band, reliable book). ----
    cand = []  # dicts: ticker, asset, ntp, mid, take_side, take_price, depth, win
    skip = defaultdict(int)

    for tk in sorted(chosen):
        fr = frames.get(tk)
        if not fr:
            skip["no_frames"] += 1
            continue
        close = close_epoch_from_ticker(tk)
        decision = close - DECISION_LEAD
        ntp_start = close - NTP_WIN_START

        # NTP over [close-120s, decision_time]  (no look-ahead)
        ntp = 0.0
        ntrades = 0
        for ts, side, cnt in trades_by_tk.get(tk, ()):
            if ts < ntp_start:
                continue
            if ts > decision:
                break
            ntp += cnt * (1.0 if side == "yes" else -1.0)
            ntrades += 1
        if ntrades == 0:
            skip["no_taker_flow"] += 1
            continue

        # decision-time reliable book
        yb, ya = reliable_nbbo_at(fr, decision)
        if yb is None or ya is None:
            skip["decision_book_unreliable"] += 1
            continue
        mid = (yb + ya) / 2.0
        if not (MID_LO <= mid <= MID_HI):
            skip["mid_out_of_band"] += 1
            continue

        # which side does NTP point to, and at what take price?
        if ntp > 0:
            take_side = "yes"
            take_price = ya            # cross to the yes ask
        elif ntp < 0:
            take_side = "no"
            take_price = 100.0 - yb    # no ask = 100 - yes bid
        else:
            skip["ntp_zero"] += 1
            continue

        # depth at the ask we cross (honest fill: must be a real resting quote)
        bk, _ = book_at(fr, decision)
        if take_side == "yes":
            depth = bk.best_yes_ask_depth()
        else:
            depth = bk.best_no_bid_depth()
        if not depth or depth <= 0:
            skip["no_ask_depth"] += 1
            continue

        # terminal label from terminal reliable book mid
        tyb, tya = reliable_nbbo_at(fr, close)
        if tyb is None or tya is None:
            skip["terminal_book_unreliable"] += 1
            continue
        term_mid = (tyb + tya) / 2.0
        winner = "yes" if term_mid > 50.0 else "no"
        win = (take_side == winner)

        cand.append({
            "ticker": tk, "asset": asset_of(tk),
            "ntp": ntp, "abs_ntp": abs(ntp), "mid": mid,
            "take_side": take_side, "take_price": take_price,
            "depth": depth, "win": win,
        })

    print("\nskip reasons:", dict(skip), flush=True)
    print(f"structural candidates (mid-band + reliable + flow + depth + take<...): {len(cand)}", flush=True)

    if not cand:
        return _report(verdict="DATA_GAP", metric=0.0, lo=0.0, hi=0.0, n=0,
                       note="no structural candidates survived gates")

    # in-sample |NTP| 70th pctile threshold (over structural candidates)
    abs_ntps = np.array([c["abs_ntp"] for c in cand])
    thr = float(np.percentile(abs_ntps, NTP_PCTILE))
    print(f"|NTP| 70th pctile threshold = {thr:.2f}", flush=True)

    # apply remaining signal gates: |NTP|>thr AND take_price < 60
    trades = []
    for c in cand:
        if c["abs_ntp"] <= thr:
            continue
        if c["take_price"] >= TAKE_PRICE_MAX:
            continue
        entry = c["take_price"]
        fee = fee_cents(entry)
        net = (100.0 if c["win"] else 0.0) - entry - fee
        trades.append({**c, "entry": entry, "fee": fee, "net": net})

    n = len(trades)
    print(f"\nTRADED signals: {n}", flush=True)
    if n == 0:
        return _report(verdict="DATA_GAP", metric=0.0, lo=0.0, hi=0.0, n=0,
                       note="zero trades after |NTP|>thr & take_price<60 gates")

    nets = np.array([t["net"] for t in trades])
    wins = np.array([1.0 if t["win"] else 0.0 for t in trades])
    point = float(nets.mean())
    wr = float(wins.mean())
    n_tickers = len({t["ticker"] for t in trades})
    print(f"  win rate = {wr:.3f}  mean entry = {nets.size and np.mean([t['entry'] for t in trades]):.2f}c"
          f"  mean fee = {np.mean([t['fee'] for t in trades]):.2f}c", flush=True)
    print(f"  per-contract net = {point:.3f}c  over n={n} signals / {n_tickers} tickers", flush=True)

    # per-asset
    for a in sorted({t["asset"] for t in trades}):
        sub = [t["net"] for t in trades if t["asset"] == a]
        print(f"    {a}: n={len(sub)} mean_net={np.mean(sub):.2f}c wr={np.mean([t['win'] for t in trades if t['asset']==a]):.2f}", flush=True)

    # ---- block bootstrap clustered by ticker ----
    by_tk = defaultdict(list)
    for t in trades:
        by_tk[t["ticker"]].append(t["net"])
    tk_list = list(by_tk.keys())
    tk_arrays = [np.array(by_tk[tk]) for tk in tk_list]
    n_tk = len(tk_list)

    rng = np.random.default_rng(20260531)
    B = 2000
    boot = np.empty(B)
    for b in range(B):
        idx = rng.integers(0, n_tk, size=n_tk)
        pooled = np.concatenate([tk_arrays[i] for i in idx])
        boot[b] = pooled.mean()
    lo = float(np.percentile(boot, 2.5))
    hi = float(np.percentile(boot, 97.5))
    print(f"\nblock bootstrap (B={B}, cluster=ticker, n_clusters={n_tk}):", flush=True)
    print(f"  per-contract net 95% CI = [{lo:.3f}, {hi:.3f}] cents", flush=True)

    if lo > 0:
        verdict = "EDGE"
    elif hi < 0:
        verdict = "NO_EDGE"
    elif n < 30 or n_tk < 8:
        verdict = "INCONCLUSIVE"
    else:
        verdict = "NO_EDGE"   # CI straddles zero, real sample -> not an edge

    return _report(verdict=verdict, metric=point, lo=lo, hi=hi, n=n,
                   note=f"wr={wr:.3f} n_tickers={n_tk} thr={thr:.1f}")


def _report(verdict, metric, lo, hi, n, note):
    out = {
        "verdict": verdict, "point_estimate": metric, "ci_low": lo, "ci_high": hi,
        "n_samples": n, "note": note,
    }
    print("\n=== RESULT ===")
    print(json.dumps(out, indent=2))
    return out


if __name__ == "__main__":
    main()
