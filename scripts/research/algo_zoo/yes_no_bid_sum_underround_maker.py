#!/usr/bin/env python3
"""yes_no_bid_sum_underround_maker — rail / queue-economics / structural-mispricing.

MECHANISM
    Kalshi YES and NO are complementary; best_yes_bid + best_no_bid should sum to
    <100c (the maker spread). When that sum s is UNUSUALLY LOW (deep under-round,
    s < 30c) the book is thin/uncertain and few competing makers crowd the queue.
    A maker who joins the bid at +1 tick can, IF the fill isn't purely adverse,
    capture structural value. We test the ASYMMETRIC version: at decision =
    close-300s, on every thin-book ticker (s < 30c), post a single resting bid at
    best_<side>_bid + 1 on the side whose IMPLIED PROB (historical terminal win-rate
    at that bid-price tier) exceeds its bid price by the widest margin.

FILL  — honest. The resting bid fills ONLY when a real trade print crosses it
    (mm_markout_evaluator.first_yes_bid_fill_ts / first_no_bid_fill_ts, post_ts =
    decision). Unfilled orders cost 0 and pnl 0.

LABEL — terminal book mid (reliable_nbbo_at at close_epoch). YES wins iff terminal
    yes-mid > 50c. Built from a book independent of the signal book (no look-ahead;
    label uses frames up to close, signal uses frames up to decision only).

FEES  — ceil(0.07 * C * P * (1-P)) cents/contract on FILL (C=1; the ceil gives the
    Kalshi 1-contract ~1c minimum). P = entry/100. Maker rebate = 0 (stated).

HEADLINE — per-POSTED-order net cents (includes free unfilled) AND per-FILLED net
    cents (adverse-selection check). Block bootstrap clustered by ticker.

NON-NEGOTIABLES honored: reliable books only (reliable_nbbo_at REFUSES drift),
honest cross fills, real fees, no look-ahead, block-bootstrap CI, terminal-book
label (in-window DB outcomes are sparse). ~1.3-day corpus => wide CI, humility.
"""
import math
import os
import random
import sys
from collections import defaultdict

sys.path.insert(0, "/Users/gabrielkagan/Documents/kalshi-bot")

from scripts.research import kalshi_book_reconstruct as kbr
from scripts.research.mm_markout_evaluator import (
    first_no_bid_fill_ts,
    first_yes_bid_fill_ts,
)
from scripts.research.phase1b_live_shadow import load_trades_by_ticker
from scripts.research.phase1b_real_price_economics import (
    close_epoch_from_ticker,
    load_frames_jsonl,
)

FRAMES = "/tmp/edge_daily/frames_crypto.jsonl"
TRADES = "/tmp/edge_daily/trades_crypto.jsonl"

DECISION_OFFSET_S = 300.0   # decision = close - 300s (5 min out)
UNDERROUND_THRESH = 30.0    # s = yes_bid + no_bid < 30c => thin / deep under-round
TICK = 1.0                  # +1 tick join
N_BOOT = 2000
random.seed(7)


def fee_cents(price_cents: float) -> float:
    """ceil(0.07 * C * P * (1-P)) cents/contract, C=1; ceil => 1-contract minimum."""
    p = price_cents / 100.0
    return math.ceil(7.0 * p * (1.0 - p))


def terminal_outcome(frames_tk, close_epoch):
    """YES wins iff reliable terminal yes-mid > 50c at close. Returns 'yes'/'no'
    or None if no reliable terminal book (refuse to label off a drifted book)."""
    bid, ask = kbr.reliable_nbbo_at(frames_tk, close_epoch)
    if bid is None or ask is None:
        return None
    ymid = (bid + ask) / 2.0
    return "yes" if ymid > 50.0 else "no"


def price_tier(p):
    """Bid-price tier for the implied-prob (win-rate) lookup table."""
    return int(p // 5) * 5  # 5c-wide tiers


def main():
    for path in (FRAMES, TRADES):
        if not os.path.exists(path):
            print(f"DATA_GAP: missing {path}")
            return
    print("loading frames ...", flush=True)
    frames = load_frames_jsonl(FRAMES)
    print(f"  {len(frames)} tickers", flush=True)
    print("loading trades ...", flush=True)
    trades = load_trades_by_ticker(TRADES)
    print(f"  {len(trades)} tickers with trades", flush=True)

    # --- terminal outcomes (label book, independent of signal book) -----------
    outcomes = {}
    for tk, fr in frames.items():
        close = close_epoch_from_ticker(tk)
        o = terminal_outcome(fr, close)
        if o is not None:
            outcomes[tk] = o
    print(f"  {len(outcomes)} tickers with reliable terminal outcome", flush=True)

    # --- PASS 1: collect every thin-book candidate (decision-time book) --------
    # cand = (ticker, asset, side, bid_price, joined_bid, s_sum, win_rate_at_tier)
    # Build the win-rate-at-bid-tier table FIRST from terminal outcomes over the
    # decision-time best-bid on BOTH sides across the whole corpus, then re-walk to
    # pick the side. (Global in-regime table; ~1.3d single regime — noted in
    # lookahead_risks: this is a mild in-sample use of the table.)
    raw = []   # (tk, asset, side, bid_price, s_sum)
    n_reliable_decision = 0
    s_hist = []  # diagnostic: s = yes_bid + no_bid over ALL reliable decision books
    for tk, fr in frames.items():
        if tk not in outcomes:
            continue
        close = close_epoch_from_ticker(tk)
        decision = close - DECISION_OFFSET_S
        yb, ya = kbr.reliable_nbbo_at(fr, decision)
        if yb is None or ya is None:
            continue  # GUARD: no reliable book at decision -> skip
        no_bid = 100.0 - ya
        s = yb + no_bid
        n_reliable_decision += 1
        s_hist.append(s)
        if not (s < UNDERROUND_THRESH):
            continue
        if not (0 < yb < 100):
            continue
        if not (0 < no_bid < 100):
            continue
        asset = tk.split("-")[0].replace("KX", "").replace("15M", "")
        raw.append((tk, asset, yb, no_bid, s))

    # win-rate-at-bid-tier table (terminal outcome of the JOINED side at that tier)
    # tier-key = (side, price_tier(bid)); value = (#wins, #n)
    tier_stats = defaultdict(lambda: [0, 0])
    for tk, asset, yb, no_bid, s in raw:
        res = outcomes[tk]
        for side, bidp in (("yes", yb), ("no", no_bid)):
            won = (res == side)
            ts = tier_stats[(side, price_tier(bidp))]
            ts[0] += int(won)
            ts[1] += 1

    def win_rate(side, bidp):
        w, n = tier_stats[(side, price_tier(bidp))]
        return (w / n) if n > 0 else None

    # --- PASS 2: per ticker, choose the side with widest (implied_prob*100 - bid)
    posted = []   # one dict per posted order
    for tk, asset, yb, no_bid, s in raw:
        close = close_epoch_from_ticker(tk)
        decision = close - DECISION_OFFSET_S
        res = outcomes[tk]
        best = None  # (margin, side, bidp, joined)
        for side, bidp in (("yes", yb), ("no", no_bid)):
            wr = win_rate(side, bidp)
            if wr is None:
                continue
            joined = bidp + TICK
            if not (0 < joined < 100):
                continue
            margin = wr * 100.0 - joined  # implied prob (c) minus what we pay
            if best is None or margin > best[0]:
                best = (margin, side, bidp, joined)
        if best is None:
            continue
        _, side, bidp, joined = best
        tr = trades.get(tk, [])
        if side == "yes":
            fill_ts = first_yes_bid_fill_ts(tr, decision, joined)
        else:
            fill_ts = first_no_bid_fill_ts(tr, decision, joined)
        filled = fill_ts is not None
        if filled:
            won = (res == side)
            f = fee_cents(joined)
            net = (100.0 - joined if won else -joined) - f
        else:
            net = 0.0
            won = None
            f = 0.0
        posted.append({
            "ticker": tk, "asset": asset, "side": side, "bid": bidp,
            "joined": joined, "s": s, "filled": filled, "won": won,
            "fee": f, "net": net,
        })

    # diagnostic on the s-distribution among reliable decision books
    print(f"\n[diag] reliable decision books: {n_reliable_decision}")
    if s_hist:
        s_hist.sort()
        n = len(s_hist)
        def pct(p):
            return s_hist[min(n - 1, int(p * n))]
        print(f"[diag] s = yes_bid+no_bid (the maker spread complement) over "
              f"reliable books:")
        print(f"       min={s_hist[0]:.0f}c  p05={pct(.05):.0f}c  p25={pct(.25):.0f}c "
              f" median={pct(.50):.0f}c  p75={pct(.75):.0f}c  max={s_hist[-1]:.0f}c")
        print(f"[diag] count with s<30c: {sum(1 for x in s_hist if x < 30)}  "
              f"s<50c: {sum(1 for x in s_hist if x < 50)}  "
              f"s<70c: {sum(1 for x in s_hist if x < 70)}")

    if not posted:
        print("\nNO posted orders (no thin-book under-round candidates).")
        _emit(None)
        return

    n_posted = len(posted)
    n_filled = sum(1 for p in posted if p["filled"])
    fill_rate = n_filled / n_posted if n_posted else 0.0

    per_posted_net = [p["net"] for p in posted]
    per_filled_net = [p["net"] for p in posted if p["filled"]]

    mean_posted = sum(per_posted_net) / n_posted
    mean_filled = (sum(per_filled_net) / n_filled) if n_filled else 0.0

    # --- block bootstrap clustered by ticker (the true independent unit) ------
    by_tk = defaultdict(list)
    for p in posted:
        by_tk[p["ticker"]].append(p)
    tks = list(by_tk)

    def boot_ci(metric):
        """metric(list_of_order_dicts) -> float over a ticker-resampled draw."""
        vals = []
        for _ in range(N_BOOT):
            draw = []
            for _ in range(len(tks)):
                draw.extend(by_tk[random.choice(tks)])
            v = metric(draw)
            if v is not None:
                vals.append(v)
        if not vals:
            return (float("nan"), float("nan"))
        vals.sort()
        lo = vals[int(0.025 * len(vals))]
        hi = vals[int(0.975 * len(vals))]
        return (lo, hi)

    def m_posted(draw):
        return sum(o["net"] for o in draw) / len(draw) if draw else None

    def m_filled(draw):
        f = [o["net"] for o in draw if o["filled"]]
        return (sum(f) / len(f)) if f else None

    ci_posted = boot_ci(m_posted)
    ci_filled = boot_ci(m_filled)

    # --- report ---------------------------------------------------------------
    print("\n" + "=" * 70)
    print("yes_no_bid_sum_underround_maker — RESULTS")
    print("=" * 70)
    print(f"posted orders (thin-book s<{UNDERROUND_THRESH:.0f}c): {n_posted}")
    print(f"filled: {n_filled}  fill rate: {fill_rate*100:.1f}%")
    print(f"\nPER-POSTED net (incl. free unfilled): {mean_posted:+.3f}c  "
          f"CI95 [{ci_posted[0]:+.3f}, {ci_posted[1]:+.3f}]")
    print(f"PER-FILLED net (adverse-selection):  {mean_filled:+.3f}c  "
          f"CI95 [{ci_filled[0]:+.3f}, {ci_filled[1]:+.3f}]  (n_fills={n_filled})")

    # asset / side breakdown
    print("\nby asset:")
    ba = defaultdict(lambda: [0, 0, 0.0])
    for p in posted:
        a = ba[p["asset"]]
        a[0] += 1
        a[1] += int(p["filled"])
        a[2] += p["net"]
    for a in sorted(ba):
        n, nf, tot = ba[a]
        print(f"  {a:>5}: posted={n:>4} filled={nf:>4} "
              f"per-posted={tot/n:+.2f}c")
    print("\nby side:")
    bs = defaultdict(lambda: [0, 0, 0.0])
    for p in posted:
        s = bs[p["side"]]
        s[0] += 1
        s[1] += int(p["filled"])
        s[2] += p["net"]
    for sd in sorted(bs):
        n, nf, tot = bs[sd]
        print(f"  {sd:>4}: posted={n:>4} filled={nf:>4} per-posted={tot/n:+.2f}c")

    # win rate among filled
    if n_filled:
        wins = sum(1 for p in posted if p["filled"] and p["won"])
        print(f"\nfilled win rate: {wins}/{n_filled} = {wins/n_filled*100:.1f}%")
        avg_entry = sum(p["joined"] for p in posted if p["filled"]) / n_filled
        avg_fee = sum(p["fee"] for p in posted if p["filled"]) / n_filled
        print(f"avg entry (joined bid): {avg_entry:.1f}c  avg fee: {avg_fee:.2f}c")

    # --- verdict: EDGE requires the HEADLINE (per-posted) CI to exclude 0 > 0 --
    # (per-posted is the deployable metric: it includes the free unfilled orders.)
    _emit({
        "n_posted": n_posted, "n_filled": n_filled,
        "mean_posted": mean_posted, "ci_posted": ci_posted,
        "mean_filled": mean_filled, "ci_filled": ci_filled,
    })


def _emit(res):
    print("\n--- MACHINE ---")
    if res is None:
        print("VERDICT NO_EDGE n_posted=0")
        return
    lo, hi = res["ci_posted"]
    if lo > 0:
        v = "EDGE"
    elif hi < 0 or (lo < 0 < hi):
        v = "NO_EDGE"
    else:
        v = "INCONCLUSIVE"
    print(f"VERDICT {v}")
    print(f"point_posted {res['mean_posted']:.4f} ci [{lo:.4f},{hi:.4f}]")
    print(f"point_filled {res['mean_filled']:.4f} "
          f"ci [{res['ci_filled'][0]:.4f},{res['ci_filled'][1]:.4f}]")
    print(f"n_posted {res['n_posted']} n_filled {res['n_filled']}")


if __name__ == "__main__":
    main()
