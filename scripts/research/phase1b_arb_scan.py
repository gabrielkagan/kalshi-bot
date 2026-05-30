"""Phase 1b — within-book ARBITRAGE scan (direction-neutral, no forecast).

The prior 6 "edges" all died from forecasting-efficiency or directional drift.
This hunts a structurally different thing: a RISK-FREE arb that needs no
prediction. On a coherent book, if best_yes_bid + best_no_bid > 100, you SELL
YES at the yes-bid AND SELL NO at the no-bid, collect >100 for a contract that
pays exactly 100 -> locked profit = (yes_bid + no_bid - 100) - 2 taker fees.
Thin/retail books can leave such crossed-pair orders un-arbed. We scan every
frame of every window's reconstructed book for it, and report magnitude, depth,
persistence (still there next frame), and net-of-fees profit.

Usage:
  python3 scripts/research/phase1b_arb_scan.py --frames-file /tmp/crypto15m.jsonl

Parent: kb/decisions/settlement-convergence-worklist.md (arbitrage corner)
"""

from __future__ import annotations

import argparse
from collections import defaultdict

from scripts.research import kalshi_book_reconstruct as kbr
from scripts.research.phase1b_real_price_economics import (
    _is_crypto_15m, kalshi_fee_per_contract_cents, load_frames_jsonl,
)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--frames-file", required=True)
    ap.add_argument("--min-net", type=float, default=0.0,
                    help="min net-of-fee arb cents to count (default 0)")
    args = ap.parse_args(argv)

    frames = load_frames_jsonl(args.frames_file)
    print(f"crypto-15M windows: {len(frames)}")

    n_frames = 0
    arb_windows = set()
    n_arb_moments = 0          # frames where overround>100 net>min
    persist_moments = 0        # arb still present at the NEXT frame
    by_mag = defaultdict(int)  # net-cents bucket -> count
    by_asset = defaultdict(lambda: {"win": 0, "moments": 0})
    total_locked = 0.0         # sum over arb-moments of net*min_depth (one-shot $)
    depth_sum = 0.0
    examples = []

    for tk, fr in frames.items():
        asset = _is_crypto_15m(tk)
        b = kbr.KalshiBook()
        prev_arb = False
        for ts, inner in fr:
            b.apply_frame(inner)
            n_frames += 1
            yb = b.best_yes_bid_cents()
            nb = b.best_no_bid_cents()
            if yb is None or nb is None:
                prev_arb = False
                continue
            over = yb + nb
            if over <= 100.0:
                prev_arb = False
                continue
            gross = over - 100.0
            net = gross - kalshi_fee_per_contract_cents(yb) - kalshi_fee_per_contract_cents(nb)
            if net <= args.min_net:
                prev_arb = False
                continue
            depth = min(b.best_yes_bid_depth() or 0.0, b.best_no_bid_depth() or 0.0)
            n_arb_moments += 1
            if prev_arb:
                persist_moments += 1
            arb_windows.add(tk)
            by_mag[min(int(net), 20)] += 1
            by_asset[asset]["moments"] += 1
            total_locked += net * depth
            depth_sum += depth
            if len(examples) < 8 and net >= 2:
                examples.append((tk, round(yb, 1), round(nb, 1), round(net, 2), depth))
            prev_arb = True

    print(f"frames scanned: {n_frames}")
    print(f"ARB moments (yes_bid+no_bid>100, net-of-fee>{args.min_net}c): {n_arb_moments}  "
          f"in {len(arb_windows)} distinct windows ({100*len(arb_windows)/(len(frames) or 1):.1f}% of windows)")
    if n_arb_moments:
        print(f"  persistence (arb still present next frame): {100*persist_moments/n_arb_moments:.1f}%")
        print(f"  avg arb depth (min of two sides): {depth_sum/n_arb_moments:.1f} contracts")
        print(f"  one-shot locked $ (sum net*depth, ALL moments, optimistic): {total_locked/100:+.2f}$")
        print("  net-cents distribution (per-contract, after 2 fees):")
        for c in sorted(by_mag):
            print(f"    {c:>3}c+ : {by_mag[c]}")
        print("  per-asset arb moments:", {a: by_asset[a]["moments"] for a in sorted(by_asset)})
        print("  examples (ticker, yes_bid, no_bid, net_c, depth):")
        for e in examples:
            print("   ", e)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
