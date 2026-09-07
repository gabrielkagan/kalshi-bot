#!/usr/bin/env python3
"""btc_spot_jump_alt_quote_pull — defensive MAKER-pull on a BTC-spot jump.

FAMILY: cross-asset event-driven liquidity provision (defensive inversion of
btc_beta_alt_fade — that tried to MONETIZE the BTC lead as a directional taker
and failed; this AVOIDS adverse selection as a maker).

HYPOTHESIS
----------
A resting alt (ETH/SOL/XRP) 15M maker bid is profitable in calm times but gets
PICKED OFF in the ~3s after a BTC spot jump propagates to the alt. If post-jump
fills are systematically toxic (negative settlement markout) while out-of-window
fills are positive, the tradeable rule is: quote alt makers continuously but
CANCEL for 3s after each BTC jump. Net edge = markout of RETAINED out-of-window
fills minus the spread forgone on cancelled windows.

MECHANICS (honest, no look-ahead, fee-inclusive)
-------------------------------------------------
- BTC jump signal: rolling 10s BTC spot-mid return |r| > k*sigma, sigma = stdev
  of 10s returns over trailing 5min. Signal is built ONLY from spot ticks at or
  before the jump time -> no look-ahead.
- Alt maker quote: at sampled quote times across each alt 15M ticker's life, we
  post a YES maker bid at NBBO-1 (best_yes_bid - 1c) using the snapshot-anchored
  reliable book. A fill happens ONLY when a real trade print crosses it
  (first_yes_bid_fill_ts).
- Each fill is tagged in-window (fill_ts within 3s AFTER any BTC jump) or
  out-of-window.
- Settlement markout: outcome derived from the TERMINAL reliable book mid vs
  strike at close (DB in-window outcomes are sparse). markout = (100-fill) if
  YES wins else -fill, minus fees on BOTH legs (entry maker fee + settlement).
- Pull-rule PnL per (ticker,quote-sample): if the fill is in-window we CANCEL
  (forgo it -> 0 contribution and 0 fee); else we keep it (markout - fees).
  Always-on PnL: keep every fill.
  uplift = pull_rule_pnl - always_on_pnl  (== -sum of in-window fill markouts).
- Headline metric: cents/contract uplift of pull-rule vs always-on, bootstrap-CI
  clustered by ticker (the true independent unit). EDGE requires CI lower bound
  > 0 AND clears fee, i.e. the in-window fills must be genuinely toxic by enough
  that avoiding them is net positive after we also lose their (possibly positive)
  spread.

KILL: uplift CI lower bound must clear fees AND be > 0. Anything weaker is
NO_EDGE / INCONCLUSIVE.

DATA_GAP: HYPE/DOGE/BNB have no local coinbase spot -> ETH/SOL/XRP only. Spot
starts 05-30T21Z so the first ~11h of frames have no BTC signal -> n limited.
"""
from __future__ import annotations

import json
import math
import random
import sys
from bisect import bisect_right
from collections import defaultdict
from datetime import datetime, timezone

sys.path.insert(0, "/Users/gabrielkagan/Documents/kalshi-bot")

from scripts.research import kalshi_book_reconstruct as kbr
from scripts.research.phase1b_real_price_economics import (
    _is_crypto_15m,
    close_epoch_from_ticker,
    kalshi_fee_per_contract_cents,
)
from scripts.research.mm_markout_evaluator import (
    first_yes_bid_fill_ts,
    settlement_markout_cents,
    yes_mid,
)

SPOT_FILE = "/tmp/edge_daily/coinbase_spot.jsonl"
TRADES_FILE = "/tmp/edge_daily/trades_crypto.jsonl"
FRAMES_FILE = "/tmp/edge_daily/frames_crypto.jsonl"

ALT_ASSETS = ("ETH", "SOL", "XRP")
JUMP_K = 4.0                 # jump threshold = k * trailing-5min sigma of 10s returns
JUMP_RETURN_WINDOW = 10.0    # seconds for the rolling return
JUMP_SIGMA_WINDOW = 300.0    # trailing window for sigma (5 min)
POST_JUMP_WINDOW = 3.0       # seconds: the toxic-fill window after a jump
QUOTE_SAMPLE_STRIDE = 15.0   # post a fresh maker quote every N sec across ticker life
QUOTE_LEAD_MIN = 30.0        # quote only between [close-15min, close-QUOTE_TAIL]
QUOTE_TAIL = 20.0            # stop quoting last 20s (settlement avg window noise)
MAKER_REBATE_CENTS = 0.0     # state assumption: no maker rebate on Kalshi
N_BOOT = 2000


def _epoch(iso: str) -> float:
    return datetime.strptime(iso, "%Y-%m-%dT%H:%M:%S.%fZ").replace(
        tzinfo=timezone.utc
    ).timestamp()


# --------------------------------------------------------------------------
# BTC jump detection (no look-ahead — signal built only from past spot ticks)
# --------------------------------------------------------------------------
def load_btc_spot():
    """Sorted (ts, mid) for BTC-USD."""
    pts = []
    with open(SPOT_FILE) as fh:
        for line in fh:
            if not line.strip():
                continue
            try:
                d = json.loads(line)
            except ValueError:
                continue
            if d.get("product_id") != "BTC-USD":
                continue
            mid = d.get("mid")
            if mid is None:
                continue
            pts.append((_epoch(d["ts"]), float(mid)))
    pts.sort(key=lambda x: x[0])
    return pts


def detect_btc_jumps(spot):
    """Return sorted list of jump timestamps. At each tick t, compute the 10s
    return r(t) = mid(t)/mid(t-10s) - 1. sigma(t) = stdev of the r-series over
    the trailing 5min. Jump if |r(t)| > k*sigma(t). Uses only data <= t."""
    ts = [p[0] for p in spot]
    mid = [p[1] for p in spot]
    n = len(spot)
    # 10s returns at each tick (price now vs price ~10s ago)
    rets = []  # (t, r)
    j = 0
    for i in range(n):
        t = ts[i]
        # advance j to the last tick <= t - JUMP_RETURN_WINDOW
        target = t - JUMP_RETURN_WINDOW
        while j + 1 < n and ts[j + 1] <= target:
            j += 1
        if ts[j] > target:
            continue  # no tick old enough yet
        m0 = mid[j]
        if m0 <= 0:
            continue
        rets.append((t, mid[i] / m0 - 1.0))

    rt = [r[0] for r in rets]
    rv = [r[1] for r in rets]
    jumps = []
    lo = 0
    # trailing-sigma over the return series
    for i in range(len(rets)):
        t = rt[i]
        win_start = t - JUMP_SIGMA_WINDOW
        while rt[lo] < win_start:
            lo += 1
        sample = rv[lo:i]  # strictly past returns (exclude current to avoid self-inflation)
        if len(sample) < 30:
            continue
        mean = sum(sample) / len(sample)
        var = sum((x - mean) ** 2 for x in sample) / (len(sample) - 1)
        sigma = math.sqrt(var)
        if sigma <= 0:
            continue
        if abs(rv[i]) > JUMP_K * sigma:
            jumps.append(t)
    # collapse jumps within POST_JUMP_WINDOW into single events (an event, not ticks)
    collapsed = []
    for t in jumps:
        if not collapsed or t - collapsed[-1] > POST_JUMP_WINDOW:
            collapsed.append(t)
    return collapsed


def in_post_jump_window(fill_ts, jump_ts_sorted):
    """True if fill_ts is within (jump, jump+POST_JUMP_WINDOW] for some jump."""
    if not jump_ts_sorted:
        return False
    idx = bisect_right(jump_ts_sorted, fill_ts) - 1
    if idx < 0:
        return False
    jt = jump_ts_sorted[idx]
    return jt < fill_ts <= jt + POST_JUMP_WINDOW


# --------------------------------------------------------------------------
# Alt frames + trades
# --------------------------------------------------------------------------
def load_alt_frames():
    frames = defaultdict(list)
    with open(FRAMES_FILE) as fh:
        for line in fh:
            if not line.strip():
                continue
            try:
                env = json.loads(line)
                inner = json.loads(env["_raw"])
            except (ValueError, KeyError):
                continue
            tk = inner.get("msg", {}).get("market_ticker", "")
            a = _is_crypto_15m(tk)
            if a in ALT_ASSETS:
                frames[tk].append((_epoch(env["_wire_recv_ts"]), inner))
    for tk in frames:
        frames[tk].sort(key=lambda x: x[0])
    return frames


def load_alt_trades():
    """{ticker: sorted [(ts, yes_c, taker_side)]} for ETH/SOL/XRP."""
    trades = defaultdict(list)
    with open(TRADES_FILE) as fh:
        for line in fh:
            if not line.strip():
                continue
            try:
                env = json.loads(line)
                msg = json.loads(env["_raw"])["msg"]
            except (ValueError, KeyError):
                continue
            tk = msg.get("market_ticker", "")
            a = _is_crypto_15m(tk)
            if a not in ALT_ASSETS:
                continue
            try:
                trades[tk].append(
                    (float(msg["ts"]), float(msg["yes_price_dollars"]) * 100, msg["taker_side"])
                )
            except (KeyError, ValueError):
                continue
    for tk in trades:
        trades[tk].sort(key=lambda x: x[0])
    return trades


def terminal_outcome(frames_tk, close_epoch):
    """Derive YES/NO outcome from the terminal reliable book mid vs the implied
    50c line. We use the binary settlement of the MARKET itself: at close, the
    reliable NBBO mid > 50 => YES likely settles in (book is the market's belief
    at close, which converges to the realized outcome in the final seconds).
    Returns 'yes'/'no' or None if the terminal book is unreliable (refuse)."""
    bid, ask = kbr.reliable_nbbo_at(frames_tk, close_epoch)
    if bid is None or ask is None:
        return None
    mid = yes_mid(bid, ask)
    # Final-seconds book mid is the market's near-certain belief; >50 => YES.
    if mid >= 95:
        return "yes"
    if mid <= 5:
        return "no"
    return None  # ambiguous terminal book -> refuse (no confident label)


# --------------------------------------------------------------------------
# Simulation
# --------------------------------------------------------------------------
def simulate(frames, trades, jumps):
    """Per alt ticker, post NBBO-1 YES maker quotes on a stride; honest fills via
    real trade prints; tag in/out window; settlement markout net of fees.
    Returns per-ticker aggregates for clustered bootstrap."""
    per_ticker = {}  # ticker -> dict(in_markouts, out_markouts, fee accounting)
    total_fills = 0
    total_in = 0

    for tk, frs in frames.items():
        tk_trades = trades.get(tk)
        if not tk_trades:
            continue
        try:
            close_ep = close_epoch_from_ticker(tk)
        except Exception:
            continue
        outcome = terminal_outcome(frs, close_ep)
        if outcome is None:
            continue

        quote_start = close_ep - 15 * 60 + QUOTE_LEAD_MIN
        quote_end = close_ep - QUOTE_TAIL
        if quote_end <= quote_start:
            continue

        in_marks = []
        out_marks = []
        seen_fill_ts = set()  # de-dup: same fill reached by multiple quote posts

        t = quote_start
        while t <= quote_end:
            bid, ask = kbr.reliable_nbbo_at(frs, t)
            if bid is None or ask is None:
                t += QUOTE_SAMPLE_STRIDE
                continue
            our_bid = bid - 1.0  # NBBO-1 maker bid
            if our_bid < 1:
                t += QUOTE_SAMPLE_STRIDE
                continue
            fill_ts = first_yes_bid_fill_ts(tk_trades, t, our_bid)
            if fill_ts is None or fill_ts > quote_end + 60:
                t += QUOTE_SAMPLE_STRIDE
                continue
            key = round(fill_ts, 3)
            if key in seen_fill_ts:
                t += QUOTE_SAMPLE_STRIDE
                continue
            seen_fill_ts.add(key)

            fill_price = our_bid  # we are the resting maker; we get our limit
            entry_fee = kalshi_fee_per_contract_cents(fill_price) - MAKER_REBATE_CENTS
            settle_fee = 0.0  # binary settlement has no exit fee; entry fee already counted
            mk = settlement_markout_cents(fill_price, "yes", outcome) - entry_fee - settle_fee

            total_fills += 1
            if in_post_jump_window(fill_ts, jumps):
                in_marks.append(mk)
                total_in += 1
            else:
                out_marks.append(mk)
            t += QUOTE_SAMPLE_STRIDE

        if in_marks or out_marks:
            per_ticker[tk] = {
                "in_marks": in_marks,
                "out_marks": out_marks,
            }
    return per_ticker, total_fills, total_in


# --------------------------------------------------------------------------
# Clustered bootstrap on the uplift
# --------------------------------------------------------------------------
def cluster_bootstrap_uplift(per_ticker, n_boot=N_BOOT, seed=12345):
    """uplift per fill = pull_rule - always_on.
    always_on keeps every fill: contribution = markout.
    pull_rule cancels in-window fills: contribution = markout if out-of-window
    else 0.
    So per-fill uplift = 0 (out-of-window, both keep) OR -markout (in-window,
    pull cancels it). Average uplift per fill = -(sum in-window markouts)/(#fills).

    We cluster by TICKER. Each ticker contributes (sum_uplift, n_fills). The
    statistic is the pooled mean uplift per fill = sum(sum_uplift)/sum(n_fills).
    Bootstrap resamples tickers with replacement."""
    clusters = []
    for tk, d in per_ticker.items():
        n_fills = len(d["in_marks"]) + len(d["out_marks"])
        if n_fills == 0:
            continue
        sum_uplift = -sum(d["in_marks"])  # out-of-window contributes 0
        clusters.append((sum_uplift, n_fills))
    if not clusters:
        return None

    def stat(sample):
        su = sum(c[0] for c in sample)
        nf = sum(c[1] for c in sample)
        return su / nf if nf else float("nan")

    point = stat(clusters)
    rng = random.Random(seed)
    K = len(clusters)
    means = []
    for _ in range(n_boot):
        sample = [clusters[rng.randrange(K)] for _ in range(K)]
        m = stat(sample)
        if not math.isnan(m):
            means.append(m)
    means.sort()
    lo = means[int(0.025 * len(means))]
    hi = means[min(len(means) - 1, int(0.975 * len(means)))]
    return point, lo, hi, len(clusters)


def main():
    print("loading BTC spot...", flush=True)
    spot = load_btc_spot()
    print(f"  BTC spot ticks: {len(spot)}", flush=True)
    jumps = detect_btc_jumps(spot)
    print(f"  BTC jump events (k={JUMP_K}): {len(jumps)}", flush=True)
    if jumps:
        span_h = (spot[-1][0] - spot[0][0]) / 3600
        print(f"  spot span: {span_h:.1f}h -> {len(jumps)/max(span_h,1e-9):.1f} jumps/h", flush=True)

    print("loading alt frames (ETH/SOL/XRP)...", flush=True)
    frames = load_alt_frames()
    print(f"  alt tickers with frames: {len(frames)}", flush=True)
    print("loading alt trades...", flush=True)
    trades = load_alt_trades()
    print(f"  alt tickers with trades: {len(trades)}", flush=True)

    # Restrict alt tickers to those whose quoting window overlaps the spot-covered
    # period (else no jump signal exists -> meaningless in/out split).
    spot_lo, spot_hi = spot[0][0], spot[-1][0]
    keep = {}
    for tk, frs in frames.items():
        try:
            ce = close_epoch_from_ticker(tk)
        except Exception:
            continue
        win_lo = ce - 15 * 60
        if win_lo >= spot_lo and ce <= spot_hi + 120:
            keep[tk] = frs
    print(f"  alt tickers within spot-covered window: {len(keep)}", flush=True)

    per_ticker, total_fills, total_in = simulate(keep, trades, jumps)
    print(f"  tickers with fills: {len(per_ticker)}", flush=True)
    print(f"  total honest fills: {total_fills}  (in-jump-window: {total_in})", flush=True)

    all_in = [m for d in per_ticker.values() for m in d["in_marks"]]
    all_out = [m for d in per_ticker.values() for m in d["out_marks"]]

    def mean(xs):
        return sum(xs) / len(xs) if xs else float("nan")

    print(f"\n  mean settlement markout (net fees), in-window  : {mean(all_in):+.3f}c  n={len(all_in)}", flush=True)
    print(f"  mean settlement markout (net fees), out-window : {mean(all_out):+.3f}c  n={len(all_out)}", flush=True)

    res = cluster_bootstrap_uplift(per_ticker)
    if res is None:
        print("\nNO USABLE CLUSTERS -> INCONCLUSIVE", flush=True)
        return {
            "verdict": "DATA_GAP",
            "point": float("nan"),
            "lo": float("nan"),
            "hi": float("nan"),
            "n_clusters": 0,
            "n_fills": total_fills,
            "n_in": total_in,
        }
    point, lo, hi, K = res
    print(f"\n  UPLIFT (pull-rule - always-on), cents/contract per fill:", flush=True)
    print(f"    point = {point:+.4f}c   95% CI [{lo:+.4f}, {hi:+.4f}]   clusters(tickers)={K}", flush=True)

    fee_floor = 0.0  # uplift is already fee-net; EDGE requires lo > 0
    if lo > fee_floor and point > 0:
        verdict = "EDGE"
    elif math.isnan(lo) or K < 5 or total_in < 20:
        verdict = "INCONCLUSIVE"
    else:
        verdict = "NO_EDGE"
    print(f"\n  VERDICT: {verdict}", flush=True)
    return {
        "verdict": verdict,
        "point": point,
        "lo": lo,
        "hi": hi,
        "n_clusters": K,
        "n_fills": total_fills,
        "n_in": total_in,
        "mean_in": mean(all_in),
        "mean_out": mean(all_out),
        "n_jumps": len(jumps),
    }


if __name__ == "__main__":
    out = main()
    print("\nRESULT_JSON:", json.dumps(out), flush=True)
