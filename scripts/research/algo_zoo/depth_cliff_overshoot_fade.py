"""depth_cliff_overshoot_fade — liquidity-vacuum / depth-cliff reversion (TAKER).

ALGORITHM (family: liquidity-vacuum / depth-cliff reversion)
------------------------------------------------------------
A "depth cliff" is a discrete liquidity-vacuum event: an aggressive taker print
that (a) consumes >= F of the best-level depth it hit AND (b) moves the reliable
mid >= G cents in ONE step, leaving the NEW best level on the consumed side thin
(depth <= THIN_CAP). The thesis: a thin-Kalshi-book level got emptied; if the
cliff is *liquidity*-driven (not information), the next refill OVERSHOOTS back, so
we FADE the gap — enter a TAKER opposite the cliff move at the now-rich post-cliff
quote, and exit at the reliable mid H seconds later.

Direction:
  - A YES taker (taker_book_side='ask' / taker_side='yes') lifts the ask -> mid
    moves UP. We fade DOWN: BUY NO at the post-cliff NO ask, exit at mid.
  - A NO taker (taker_book_side='bid' / taker_side='no') hits the bid -> mid moves
    DOWN. We fade UP: BUY YES at the post-cliff YES ask, exit at mid.
  Both ENTER as a taker (cross the spread) and EXIT by marking to the reliable mid
  H s later (best-case exit, no exit half-spread -> PnL is an UPPER bound on a real
  taker round trip; stated explicitly so the verdict stays honest).

HONESTY / LOOK-AHEAD CONTROLS
-----------------------------
- Signal book built ONLY from frames with recv_epoch <= trade.ts (no peeking).
  Cliff detection compares reliable mid just BEFORE the print to reliable mid
  just AT/AFTER it (each from frames <= the respective time).
- Entry quote = reliable book at entry time (trade.ts + ENTRY_EPS), frames <= it.
- Label / exit mid = reliable_nbbo_at(frames, entry+H) — INDEPENDENT book read at
  entry+H using only frames <= entry+H. Both honor reliable_nbbo_at's REFUSAL
  (drifted/crossed -> drop the sample, never trade off a rejected book).
- Cliffs that DON'T repair are counted as REALIZED LOSSES (we entered a real
  taker; the mid simply didn't come back). The headline mean is NOT conditioned
  on repair.
- Fees: ceil(0.07*1*P*(100-P)/100) cents on BOTH taker legs. Rebate = 0. A cliff
  is only EDGE if repair exceeds 2 half-spreads + 2 fees.
- Block/cluster bootstrap CI (>=2000 resamples) CLUSTERED BY TICKER.

CORPUS: /tmp/edge_daily/frames_crypto.jsonl (~31h, crypto-15M, kalshi_ws) +
        /tmp/edge_daily/trades_crypto.jsonl. No spot needed. All 7 assets.
"""

from __future__ import annotations

import json
import math
import os
import sys
from collections import defaultdict

sys.path.insert(0, "/Users/gabrielkagan/Documents/kalshi-bot")

import numpy as np

from scripts.research.kalshi_book_reconstruct import KalshiBook, reliable_nbbo_at
from scripts.research.phase1b_real_price_economics import (
    _epoch,
    _is_crypto_15m,
    close_epoch_from_ticker,
)

FRAMES = "/tmp/edge_daily/frames_crypto.jsonl"
TRADES = "/tmp/edge_daily/trades_crypto.jsonl"

# --- cliff-detection knobs ---
F_DEPTH_CONSUMED = 0.90    # print must consume >= 90% of best-level depth it hit
G_MID_MOVE_C = 2.0         # reliable mid must move >= G cents in one print-step
THIN_CAP = 50.0            # new best level (consumed side) must be <= THIN_CAP deep
H_LIST = (5.0, 15.0)       # exit horizons (s)
ENTRY_EPS_S = 0.5          # settle epsilon after the print before reading entry book
MIN_PRICE_C = 3.0
MAX_PRICE_C = 97.0
CLOSE_GUARD_S = 20.0       # don't enter within this many s of the 15M close
N_BOOT = 2000
SEED = 7

# Memory bound: the full 4.4GB frames file thrashes RAM if loaded whole. We
# subsample to the MAX_TICKERS busiest-by-print tickers (most cliff candidates)
# and stream the frames file ONCE, keeping only those tickers' frames. Override
# via env DCOF_MAX_TICKERS (0 = all tickers, memory permitting).
MAX_TICKERS = int(os.environ.get("DCOF_MAX_TICKERS", "120"))


def load_frames_for_tickers(path, target):
    """Stream the frames JSONL ONCE, keeping only frames whose market_ticker is in
    `target`. Returns {ticker: [(recv_epoch, inner)] sorted}. Memory-bounded to
    the target tickers, avoiding the full-file in-RAM thrash."""
    frames = defaultdict(list)
    with open(path) as fh:
        for line in fh:
            if not line.strip():
                continue
            try:
                env = json.loads(line)
                inner = json.loads(env["_raw"])
            except (ValueError, KeyError):
                continue
            tk = inner.get("msg", {}).get("market_ticker", "")
            if tk in target:
                frames[tk].append((_epoch(env["_wire_recv_ts"]), inner))
    for tk in frames:
        frames[tk].sort(key=lambda x: x[0])
    return frames


def taker_fee_cents(price_c: float) -> int:
    p = max(0.0, min(100.0, price_c))
    return math.ceil(0.07 * 1 * p * (100.0 - p) / 100.0)


def _reliable_mid_at(frames, cutoff):
    """(mid_c, half_spread_c) from reliable_nbbo_at, or (None, None) if refused."""
    yb, ya = reliable_nbbo_at(frames, cutoff)
    if yb is None or ya is None or ya <= yb:
        return None, None
    return (yb + ya) / 2.0, (ya - yb) / 2.0


def build_timeline(frames):
    """ONE forward pass over a ticker's sorted (ts, inner) frames. After each
    frame, record a reliable-book snapshot:
        (ts, mid_c, half_spread_c, yes_ask_depth, yes_bid_depth)
    or skip the record if the book is currently UNRELIABLE (snapshot-anchored,
    not drift-blown, not crossed) — exactly reliable_nbbo_at's refusal. The
    returned list is ascending in ts; cliff detection binary-searches it instead
    of re-replaying frames per trade (O(frames) total, not O(trades*frames))."""
    out = []
    b = KalshiBook()
    since_snap = 0
    anchored = False
    for ts, inner in frames:
        if inner.get("type") == "orderbook_snapshot":
            b = KalshiBook()
            b.apply_frame(inner)
            since_snap = 0
            anchored = True
        else:
            b.apply_frame(inner)
            since_snap += 1
        if not anchored or since_snap > 100000 or not b.is_reliable():
            continue
        yb = b.best_yes_bid_cents()
        ya = b.best_yes_ask_cents()
        if yb is None or ya is None or ya <= yb:
            continue
        out.append((ts, (yb + ya) / 2.0, (ya - yb) / 2.0,
                    b.best_yes_ask_depth(), b.best_yes_bid_depth(), yb, ya))
    return out


def _state_at(timeline, ts_list, cutoff):
    """Most-recent reliable state with ts <= cutoff, via binary search. Returns
    the timeline tuple or None if no reliable state existed at/before cutoff."""
    import bisect
    i = bisect.bisect_right(ts_list, cutoff) - 1
    if i < 0:
        return None
    return timeline[i]


def load_trades_sorted(path):
    """{ticker: [trade_dict sorted by ts]} for crypto-15M trade prints."""
    out = defaultdict(list)
    with open(path) as fh:
        for line in fh:
            if not line.strip():
                continue
            try:
                env = json.loads(line)
                msg = json.loads(env["_raw"])["msg"]
            except (ValueError, KeyError):
                continue
            tk = msg.get("market_ticker", "")
            if not _is_crypto_15m(tk):
                continue
            try:
                ts = float(msg["ts_ms"]) / 1000.0 if msg.get("ts_ms") else float(msg["ts"])
                out[tk].append({
                    "ts": ts,
                    "count": float(msg["count_fp"]),
                    "taker_side": msg.get("taker_side", ""),
                    "taker_book_side": msg.get("taker_book_side", ""),
                })
            except (KeyError, ValueError):
                continue
    for tk in out:
        out[tk].sort(key=lambda x: x["ts"])
    return out


def detect_and_mark(ticker, timeline, ts_list, trades, H):
    """Yield one mark-out record per detected cliff for horizon H, using the
    precomputed reliable-book timeline (each lookup is the most-recent reliable
    state with ts <= cutoff -> no look-ahead)."""
    close_ep = close_epoch_from_ticker(ticker)
    recs = []
    for tr in trades:
        t_ts = tr["ts"]
        if t_ts > close_ep - CLOSE_GUARD_S:
            continue
        if t_ts + ENTRY_EPS_S + H > close_ep:
            continue

        st_before = _state_at(timeline, ts_list, t_ts - 1e-6)
        if st_before is None:
            continue
        _, mid_before, hs_before, ask_d_b, bid_d_b, _, _ = st_before

        # side the taker HIT. taker_side is the unambiguous signal (verified vs
        # realized price moves: yes-taker lifts the YES ask -> mid UP; no-taker
        # hits the YES bid -> mid DOWN). taker_book_side labels the *maker* book
        # side and is NOT the YES-frame side, so we do NOT branch on it.
        if tr["taker_side"] == "yes":
            consumed_depth = ask_d_b
            cliff_dir = +1  # mid pushed UP -> fade DOWN (buy NO)
        elif tr["taker_side"] == "no":
            consumed_depth = bid_d_b
            cliff_dir = -1  # mid pushed DOWN -> fade UP (buy YES)
        else:
            continue
        if consumed_depth is None or consumed_depth <= 0:
            continue

        st_after = _state_at(timeline, ts_list, t_ts + ENTRY_EPS_S)
        if st_after is None:
            continue
        _, mid_after, hs_after, ask_d_a, bid_d_a, yb_e, ya_e = st_after

        move_c = mid_after - mid_before
        if cliff_dir > 0 and move_c < G_MID_MOVE_C:
            continue
        if cliff_dir < 0 and -move_c < G_MID_MOVE_C:
            continue

        # consumption gate: print must consume >= F of best depth it hit
        if tr["count"] < F_DEPTH_CONSUMED * consumed_depth:
            continue

        # new best level on consumed side must be THIN
        new_depth = ask_d_a if cliff_dir > 0 else bid_d_a
        if new_depth is None or new_depth > THIN_CAP:
            continue

        # ENTRY taker in the FADING direction at the post-cliff ask we buy
        if yb_e is None or ya_e is None or ya_e <= yb_e:
            continue
        if cliff_dir > 0:
            entry_price_c = 100.0 - yb_e   # NO ask
            entry_outcome = "no"
        else:
            entry_price_c = ya_e           # YES ask
            entry_outcome = "yes"
        if not (MIN_PRICE_C <= entry_price_c <= MAX_PRICE_C):
            continue

        # EXIT: reliable mid H s later (independent label state from timeline)
        st_exit = _state_at(timeline, ts_list, t_ts + ENTRY_EPS_S + H)
        if st_exit is None:
            continue
        mid_exit = st_exit[1]
        # require the exit state to be reasonably fresh (within H of target),
        # else we'd be marking to a stale pre-horizon quote
        if (t_ts + ENTRY_EPS_S + H) - st_exit[0] > H:
            continue
        if entry_outcome == "yes":
            exit_value_c = mid_exit
        else:
            exit_value_c = 100.0 - mid_exit

        gross_c = exit_value_c - entry_price_c
        fee_in = taker_fee_cents(entry_price_c)
        fee_out = taker_fee_cents(exit_value_c)
        net_c = gross_c - fee_in - fee_out

        barrier = (hs_before + hs_after) + (fee_in + fee_out)
        if cliff_dir > 0:
            repair_amt = mid_after - mid_exit
        else:
            repair_amt = mid_exit - mid_after
        repaired = repair_amt > barrier

        recs.append({
            "ticker": ticker, "net_c": net_c, "gross_c": gross_c,
            "repaired": repaired, "move_c": abs(move_c), "entry_c": entry_price_c,
        })
    return recs


def cluster_bootstrap_ci(values_by_ticker, n_boot=N_BOOT, seed=SEED):
    """Block bootstrap CLUSTERED BY TICKER on the mean of per-sample net_c."""
    rng = np.random.default_rng(seed)
    tickers = list(values_by_ticker.keys())
    if not tickers:
        return None, None, None
    arrs = [np.asarray(values_by_ticker[t], dtype=float) for t in tickers]
    pooled = np.concatenate(arrs)
    point = float(pooled.mean()) if pooled.size else float("nan")
    n = len(tickers)
    means = np.empty(n_boot)
    for b in range(n_boot):
        idx = rng.integers(0, n, size=n)
        samp = np.concatenate([arrs[i] for i in idx])
        means[b] = samp.mean() if samp.size else np.nan
    means = means[~np.isnan(means)]
    lo, hi = np.percentile(means, [2.5, 97.5])
    return point, float(lo), float(hi)


def main():
    print("loading trades ...", flush=True)
    trades_by_tk = load_trades_sorted(TRADES)
    print(f"  trades: {len(trades_by_tk)} tickers, "
          f"{sum(len(v) for v in trades_by_tk.values())} prints", flush=True)

    # subsample to the busiest-by-print tickers (most cliff candidates) to keep
    # the frames load memory-bounded.
    ranked = sorted(trades_by_tk, key=lambda t: len(trades_by_tk[t]), reverse=True)
    if MAX_TICKERS > 0:
        target = set(ranked[:MAX_TICKERS])
    else:
        target = set(ranked)
    print(f"  subsample: {len(target)} target tickers (DCOF_MAX_TICKERS={MAX_TICKERS})", flush=True)

    print("loading frames for target tickers (streaming) ...", flush=True)
    frames_by_tk = load_frames_for_tickers(FRAMES, target)
    print(f"  frames: {len(frames_by_tk)} tickers, "
          f"{sum(len(v) for v in frames_by_tk.values())} frames", flush=True)

    print("building reliable-book timelines ...", flush=True)
    timelines = {}
    for tk in list(frames_by_tk.keys()):
        if tk not in trades_by_tk:
            frames_by_tk.pop(tk, None)
            continue
        fr = frames_by_tk.pop(tk)  # free raw frames as we go (RAM relief)
        tl = build_timeline(fr)
        if tl:
            timelines[tk] = (tl, [r[0] for r in tl])
    frames_by_tk.clear()
    print(f"  timelines: {len(timelines)} tickers", flush=True)

    results = {}
    for H in H_LIST:
        by_ticker = defaultdict(list)
        all_recs = []
        n_repaired = 0
        for tk, trs in trades_by_tk.items():
            tl_pair = timelines.get(tk)
            if not tl_pair:
                continue
            tl, ts_list = tl_pair
            recs = detect_and_mark(tk, tl, ts_list, trs, H)
            for r in recs:
                by_ticker[tk].append(r["net_c"])
                all_recs.append(r)
                n_repaired += int(r["repaired"])
        n = len(all_recs)
        if n == 0:
            results[H] = {"n": 0}
            print(f"\nH={H}s: NO cliffs detected", flush=True)
            continue
        point, lo, hi = cluster_bootstrap_ci(by_ticker)
        gross = np.array([r["gross_c"] for r in all_recs])
        repaired_nets = np.array([r["net_c"] for r in all_recs if r["repaired"]])
        notrep_nets = np.array([r["net_c"] for r in all_recs if not r["repaired"]])
        results[H] = {
            "n": n, "n_tickers": len(by_ticker),
            "point": point, "lo": lo, "hi": hi,
            "mean_gross": float(gross.mean()),
            "repair_rate": n_repaired / n,
            "mean_repaired": float(repaired_nets.mean()) if repaired_nets.size else float("nan"),
            "mean_notrep": float(notrep_nets.mean()) if notrep_nets.size else float("nan"),
        }
        print(f"\nH={H}s  cliffs n={n}  tickers={len(by_ticker)}", flush=True)
        print(f"  mean net cents/trade = {point:+.3f}  CI95=[{lo:+.3f}, {hi:+.3f}]", flush=True)
        print(f"  mean GROSS cents/trade = {gross.mean():+.3f}", flush=True)
        print(f"  repair rate = {n_repaired/n:.1%}", flush=True)
        if repaired_nets.size:
            print(f"  repaired-split mean net = {repaired_nets.mean():+.3f} (n={repaired_nets.size})", flush=True)
        if notrep_nets.size:
            print(f"  NOT-repaired mean net = {notrep_nets.mean():+.3f} (n={notrep_nets.size})", flush=True)

    best_H = None
    for H in H_LIST:
        r = results[H]
        if r["n"] == 0:
            continue
        if best_H is None or r["point"] > results[best_H]["point"]:
            best_H = H
    print("\n==== HEADLINE ====", flush=True)
    if best_H is None:
        print("DATA_GAP / no cliffs", flush=True)
    else:
        r = results[best_H]
        edge = r["lo"] > 0
        print(f"best horizon H={best_H}s: mean net = {r['point']:+.3f}c "
              f"CI=[{r['lo']:+.3f}, {r['hi']:+.3f}]  -> "
              f"{'EDGE' if edge else 'NO_EDGE'}", flush=True)
    return results


if __name__ == "__main__":
    main()
