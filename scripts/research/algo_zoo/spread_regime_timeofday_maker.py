#!/usr/bin/env python3
"""spread_regime_timeofday_maker — regime-conditioned 2-sided maker (algo_zoo).

FAMILY: regime_conditioning / market_making

THESIS
------
The pooled avellaneda maker verdict is ~-5.3c/ct — net-negative because a resting
bid is, on average, adversely selected. But the pooled number hides heterogeneity:
overnight / low-vol / wide-spread windows on thinner books carry less informed
flow, so a resting bid THERE is less picked off. This algo tests whether a REGIME
GATE (asset x UTC hour x realized-vol tercile x spread-width tercile) selected on a
TRAIN split flips the verdict net-positive OUT OF SAMPLE on a held-out TEST split.

The alpha being tested is the GATE itself, validated train/test to defend against
overfit (the hunt warns about exactly this). EDGE only if the selected-regime maker
is net-positive OUT OF SAMPLE with a ticker-clustered bootstrap CI excluding zero.

MECHANICS (per 15M window)
--------------------------
- decision time = close - 300s.
- reliable NBBO at decision (snapshot-anchored; REFUSES drifted books -> skip).
- regime features:
    * asset (from ticker)
    * UTC hour-of-day of the window close
    * realized-vol tercile: stdev of reliable book-mid sampled across the window
      (signal-book only: samples STRICTLY <= decision time -> NO look-ahead)
    * spread-width tercile: (yes_ask - yes_bid) cents at decision time
  Vol/spread tercile cut-points are computed on the TRAIN split ONLY and frozen,
  then applied to TEST (no test-set leakage into the bucketing).
- maker quotes: post a resting YES bid one tick (1c) INSIDE the spread and a
  resting NO bid one tick inside the spread, both at the decision time.
- HONEST fill: each resting bid fills ONLY if a real trade print crosses it
  strictly after the post (mm_markout_evaluator primitives). A fill you got is a
  fill you usually regret.
- label: terminal book mid at close (reliable book at close); YES wins iff
  terminal yes_mid > 50. Filled contracts settle to the binary outcome. Label book
  is built independently of the signal book (no look-ahead leak).
- net PnL/ct (per filled side) = holding payoff - fee + rebate.
    payoff = 100 - fill_price if side wins else -fill_price
    fee    = ceil(0.07 * 100 * P * (1-P)) cents, P = fill_price/100  (price-dependent)
    rebate = MAKER_REBATE_CENTS (default 0.0).

GATE / SPLIT
------------
- windows sorted by close time; first ~60% = TRAIN, last ~40% = TEST.
- bucket = (asset, utc_hour, vol_tercile, spread_tercile, side).
- a bucket is SELECTED iff, on TRAIN: n_fills >= MIN_FILLS_TRAIN and mean net > 0.
- HEADLINE = OOS mean net PnL/filled-ct over TEST fills whose bucket was selected.
- CI = ticker-clustered block bootstrap (resample tickers w/ replacement), n>=1000.
  Clustering by ticker because fills within a window share the terminal outcome.

KILL RULES honored: real fees (ceil, price-dependent), honest trade-cross fill,
clustered bootstrap CI, no look-ahead (signal vs label books independent),
DATA_GAP if inputs missing, humility on a ~1.3-day corpus (wide CIs / INCONCLUSIVE).
"""
from __future__ import annotations

import math
import random
import sys
from collections import defaultdict

sys.path.insert(0, "/Users/gabrielkagan/Documents/kalshi-bot")

from scripts.research import kalshi_book_reconstruct as kbr
from scripts.research.phase1b_real_price_economics import (
    _is_crypto_15m,
    close_epoch_from_ticker,
    load_frames_jsonl,
)
from scripts.research.phase1b_live_shadow import load_trades_by_ticker
from scripts.research.mm_markout_evaluator import (
    first_yes_bid_fill_ts,
    first_no_bid_fill_ts,
)

FRAMES_FILE = "/tmp/edge_daily/frames_crypto.jsonl"
TRADES_FILE = "/tmp/edge_daily/trades_crypto.jsonl"

DECISION_OFFSET_S = 300       # close - 300s: post the resting maker quotes
TICK_INSIDE_C = 1.0           # post one tick (1c) inside the spread
MAKER_REBATE_CENTS = 0.0      # default no rebate (stated assumption)
TRAIN_FRAC = 0.60             # first 60% of windows by close time -> TRAIN
MIN_FILLS_TRAIN = 8           # per-bucket train floor to be eligible for selection
N_BOOT = 2000                 # ticker-clustered block bootstrap resamples
VOL_SAMPLE_STEP_S = 30        # sample book-mid every 30s for realized-vol estimate


def kalshi_fee_ceil_cents(price_cents: float) -> float:
    """ceil(0.07 * C * P * (1-P)) cents/contract, C=100 (per the hunt spec)."""
    p = price_cents / 100.0
    return math.ceil(0.07 * 100.0 * p * (1.0 - p))


def realized_vol_pre_decision(frames, decision_ts: float) -> float | None:
    """Stdev of reliable book-mid sampled across the window, STRICTLY at or before
    the decision time (signal-book; no look-ahead). None if < 3 reliable samples."""
    if not frames:
        return None
    t0 = frames[0][0]
    mids = []
    t = t0
    while t <= decision_ts:
        bid, ask = kbr.reliable_nbbo_at(frames, t)
        if bid is not None and ask is not None:
            mids.append((bid + ask) / 2.0)
        t += VOL_SAMPLE_STEP_S
    bid, ask = kbr.reliable_nbbo_at(frames, decision_ts)
    if bid is not None and ask is not None:
        mids.append((bid + ask) / 2.0)
    if len(mids) < 3:
        return None
    m = sum(mids) / len(mids)
    var = sum((x - m) ** 2 for x in mids) / (len(mids) - 1)
    return math.sqrt(var)


def terminal_outcome(frames, close_ts: float) -> str | None:
    """YES/NO from the reliable book mid AT close (label-book, built independently).
    YES wins iff terminal yes_mid > 50. None if no reliable book at close."""
    bid, ask = kbr.reliable_nbbo_at(frames, close_ts)
    if bid is None or ask is None:
        return None
    ymid = (bid + ask) / 2.0
    if abs(ymid - 50.0) < 1e-9:
        return None
    return "yes" if ymid > 50.0 else "no"


def _tercile_label(value: float, cuts: tuple[float, float]) -> str:
    lo, hi = cuts
    if value <= lo:
        return "T0"
    if value <= hi:
        return "T1"
    return "T2"


def _terciles(values: list[float]) -> tuple[float, float]:
    """33/66 percentile cut-points (computed on TRAIN only)."""
    s = sorted(values)
    n = len(s)
    if n < 3:
        return (float("inf"), float("inf"))
    return (s[n // 3], s[(2 * n) // 3])


def net_pnl_per_fill(side: str, fill_price: float, outcome: str) -> float:
    """Net PnL of one filled maker contract = holding payoff - fee + rebate."""
    payoff = (100.0 - fill_price) if side == outcome else -float(fill_price)
    fee = kalshi_fee_ceil_cents(fill_price)
    return payoff - fee + MAKER_REBATE_CENTS


def build_fills(frames_by_tk, trades_by_tk):
    """Per window simulate the 2-sided resting maker at decision time; record each
    HONEST fill. Returns (fills, skip_counters, n_windows_with_book)."""
    fills = []
    skip = defaultdict(int)
    n_windows_with_book = 0
    for tk, fr in frames_by_tk.items():
        asset = _is_crypto_15m(tk)
        if asset is None:
            skip["not_crypto15m"] += 1
            continue
        close_ts = close_epoch_from_ticker(tk)
        decision_ts = close_ts - DECISION_OFFSET_S

        bid, ask = kbr.reliable_nbbo_at(fr, decision_ts)
        if bid is None or ask is None:
            skip["no_reliable_book_at_decision"] += 1
            continue
        spread = ask - bid
        if spread < 2.0:
            skip["spread_lt_2tick"] += 1   # need room to post 1 tick inside both sides
            continue

        vol = realized_vol_pre_decision(fr, decision_ts)
        if vol is None:
            skip["no_vol_estimate"] += 1
            continue

        outcome = terminal_outcome(fr, close_ts)
        if outcome is None:
            skip["no_terminal_label"] += 1
            continue

        n_windows_with_book += 1
        utc_hour = int((close_ts // 3600) % 24)
        tr = trades_by_tk.get(tk, [])

        yes_bid_post = bid + TICK_INSIDE_C          # resting YES bid, 1 tick inside
        no_bid_yesprice = ask - TICK_INSIDE_C       # YES ask we'd sell at
        no_bid_post = 100.0 - no_bid_yesprice       # resting NO bid (NO price units)

        if 0 < yes_bid_post < 100:
            if first_yes_bid_fill_ts(tr, decision_ts, yes_bid_post) is not None:
                fills.append({
                    "ticker": tk, "asset": asset, "hour": utc_hour, "side": "yes",
                    "fill_price": yes_bid_post, "vol": vol, "spread": spread,
                    "net": net_pnl_per_fill("yes", yes_bid_post, outcome),
                    "won": (outcome == "yes"), "close_ts": close_ts,
                })
        if 0 < no_bid_post < 100:
            if first_no_bid_fill_ts(tr, decision_ts, no_bid_post) is not None:
                fills.append({
                    "ticker": tk, "asset": asset, "hour": utc_hour, "side": "no",
                    "fill_price": no_bid_post, "vol": vol, "spread": spread,
                    "net": net_pnl_per_fill("no", no_bid_post, outcome),
                    "won": (outcome == "no"), "close_ts": close_ts,
                })
    return fills, skip, n_windows_with_book


def cluster_bootstrap_ci(records, n_boot=N_BOOT, alpha=0.05, seed=12345):
    """Ticker-clustered block bootstrap CI for the mean of record['net'].
    Resamples TICKERS (the independent unit) with replacement. Returns
    (mean, lo, hi, n_fills, n_tickers)."""
    if not records:
        return (float("nan"), float("nan"), float("nan"), 0, 0)
    by_tk = defaultdict(list)
    for r in records:
        by_tk[r["ticker"]].append(r["net"])
    tickers = list(by_tk)
    all_net = [r["net"] for r in records]
    mean = sum(all_net) / len(all_net)
    rng = random.Random(seed)
    n_tk = len(tickers)
    means = []
    for _ in range(n_boot):
        pool = []
        for _ in range(n_tk):
            pool.extend(by_tk[tickers[rng.randrange(n_tk)]])
        if pool:
            means.append(sum(pool) / len(pool))
    means.sort()
    lo = means[int((alpha / 2) * len(means))]
    hi = means[min(len(means) - 1, int((1 - alpha / 2) * len(means)))]
    return (mean, lo, hi, len(all_net), n_tk)


def decide(mean, lo, hi, n_fills, n_tk):
    if n_fills < 30 or n_tk < 10:
        return "INCONCLUSIVE"
    if math.isnan(lo) or math.isnan(hi):
        return "INCONCLUSIVE"
    if lo > 0:
        return "EDGE"
    if hi < 0:
        return "NO_EDGE"
    return "INCONCLUSIVE"  # CI straddles zero on a thin corpus -> humility


def main():
    print("loading frames ...", flush=True)
    frames_by_tk = load_frames_jsonl(FRAMES_FILE)
    print(f"  crypto-15M tickers with frames: {len(frames_by_tk)}", flush=True)
    print("loading trades ...", flush=True)
    trades_by_tk = load_trades_by_ticker(TRADES_FILE)
    print(f"  crypto-15M tickers with trades: {len(trades_by_tk)}", flush=True)

    if not frames_by_tk:
        print("DATA_GAP: no frames loaded")
        return {"verdict": "DATA_GAP", "n_fills": 0}

    print("building honest 2-sided maker fills ...", flush=True)
    fills, skip, n_win = build_fills(frames_by_tk, trades_by_tk)
    print(f"  windows with reliable book + label: {n_win}", flush=True)
    print(f"  total honest fills: {len(fills)}", flush=True)
    print(f"  skip reasons: {dict(skip)}", flush=True)

    if not fills:
        print("DATA_GAP / NO_EDGE: zero honest fills produced")
        return {"verdict": "DATA_GAP", "n_fills": 0, "n_windows": n_win}

    fills.sort(key=lambda r: r["close_ts"])
    closes = sorted({r["close_ts"] for r in fills})
    split_idx = int(len(closes) * TRAIN_FRAC)
    split_ts = closes[split_idx] if split_idx < len(closes) else closes[-1]
    train = [r for r in fills if r["close_ts"] < split_ts]
    test = [r for r in fills if r["close_ts"] >= split_ts]
    print(f"\nsplit @ close_ts={split_ts}: train fills={len(train)} test fills={len(test)}", flush=True)
    if not train or not test:
        print("INCONCLUSIVE: empty train or test split")
        return {"verdict": "INCONCLUSIVE", "n_fills": len(fills)}

    vol_cuts = _terciles([r["vol"] for r in train])
    spr_cuts = _terciles([r["spread"] for r in train])
    print(f"TRAIN vol terciles cut={vol_cuts}  spread terciles cut={spr_cuts}", flush=True)

    def bucket(r):
        return (r["asset"], r["hour"],
                _tercile_label(r["vol"], vol_cuts),
                _tercile_label(r["spread"], spr_cuts),
                r["side"])

    train_by_bucket = defaultdict(list)
    for r in train:
        train_by_bucket[bucket(r)].append(r["net"])
    selected = set()
    for b, nets in train_by_bucket.items():
        if len(nets) >= MIN_FILLS_TRAIN and (sum(nets) / len(nets)) > 0:
            selected.add(b)
    print(f"selected buckets (train-profitable, n>={MIN_FILLS_TRAIN}): "
          f"{len(selected)} of {len(train_by_bucket)}", flush=True)

    test_selected = [r for r in test if bucket(r) in selected]
    print(f"OOS test fills in selected buckets: {len(test_selected)}", flush=True)

    def _mean(rs):
        return (sum(x["net"] for x in rs) / len(rs)) if rs else float("nan")
    train_sel = [r for r in train if bucket(r) in selected]
    print(f"\n[context] pooled ALL fills mean net/ct: {_mean(fills):+.2f}c (n={len(fills)})")
    print(f"[context] pooled TEST mean net/ct:      {_mean(test):+.2f}c (n={len(test)})")
    print(f"[context] TRAIN selected-bucket mean:   {_mean(train_sel):+.2f}c (n={len(train_sel)})")

    mean, lo, hi, n_fills, n_tk = cluster_bootstrap_ci(test_selected)
    print(f"\n=== HEADLINE: OOS selected-regime maker ===")
    print(f"  mean net PnL / filled ct = {mean:+.2f}c   95% CI [{lo:+.2f}, {hi:+.2f}]")
    print(f"  n_fills={n_fills}  n_tickers={n_tk}", flush=True)

    if test_selected:
        wins = sum(1 for r in test_selected if r["won"])
        wr = 100.0 * wins / len(test_selected)
        avg_fill = sum(r["fill_price"] for r in test_selected) / len(test_selected)
        # adverse-selection tax: fair value if filled were unbiased = avg_fill (the
        # entry price implies P(win)=fill/100). Realized win-rate below that = tax.
        implied_wr = avg_fill  # for a YES at p, fair P(win)=p; symmetric NO at p too
        print(f"  test_selected win-rate={wr:.1f}%  avg fill price={avg_fill:.1f}c  "
              f"implied(fair)={implied_wr:.1f}%  adverse-sel tax={implied_wr - wr:+.1f}pp")

    verdict = decide(mean, lo, hi, n_fills, n_tk)
    print(f"\nVERDICT: {verdict}")

    return {
        "verdict": verdict, "mean": mean, "ci_low": lo, "ci_high": hi,
        "n_fills": n_fills, "n_tickers": n_tk,
        "pooled_all_mean": _mean(fills), "pooled_test_mean": _mean(test),
        "n_selected_buckets": len(selected), "n_train_buckets": len(train_by_bucket),
        "skip": dict(skip), "n_windows": n_win,
    }


if __name__ == "__main__":
    result = main()
    print("\nRESULT:", result)
