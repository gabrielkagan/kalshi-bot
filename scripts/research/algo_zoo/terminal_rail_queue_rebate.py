#!/usr/bin/env python3
"""terminal_rail_queue_rebate  (family: market_making / queue_economics)

SPEC
----
Target the 1c/99c RAILS, not the refuted 60-89c middle. For each crypto-15M
ticker, at decision time = close_epoch - T (sweep T in {120,90,60,45,30}s),
reconstruct the reliable book (reliable_nbbo_at; honor refusal). Require
'rail-decided' state: YES NBBO bid >= 97c (locked-YES) or YES NBBO ask <= 3c
(locked-NO, i.e. best NO bid >= 97c). Post a resting maker bid on the
book-IMPLIED-WINNER side at that side's best bid. HONEST fill: fills only if a
real TRADES print crosses our resting bid strictly after decision and before
close (first_yes_bid_fill_ts / first_no_bid_fill_ts); unfilled = no trade. Label
each FILLED contract by the TERMINAL reliable book mid at close (yes_mid > 50 =>
YES wins). PnL/ct = 100*win - fill_price - fee + rebate; fee =
ceil(0.07 * C * P * (1-P)) cents at fill price (C=1, order-rounded up). Maker
rebate default 0, swept 0 / 0.25c to expose rebate-dependence.

HEADLINE = mean net PnL per FILLED contract, ticker-clustered block bootstrap CI
(>=1000). EDGE iff CI > 0 net of fees+fills. Reports fill rate (binding
constraint) + adverse-selection tax = implied-winner win-rate among FILLED vs
ALL rail-decided books.

WHY IT MIGHT BEAT AN EFFICIENT MARKET: the hunt found majors efficient + the
middle a maker-mirage, but flagged the 1c/99c rails (queue position + rebate
economics, thin retail competition) as unexplored. A 97c bid that fills (someone
dumps the near-certain winner cheap into close) and settles 100c is +3c gross;
the only loss path is a settlement flip, measured directly by the terminal label.

NON-NEGOTIABLES honored: real Kalshi fees, honest real-trade fill, no look-ahead
(reliable signal-book + reliable label-book, both timestamp-gated), clustered
bootstrap, DATA_GAP if inputs absent. ~1.3-day corpus -> wide CIs, humility.

DATA: FRAMES (book at decision + close), TRADES (honest fill). No spot needed ->
all 7 assets incl HYPE/DOGE/BNB.

Run:
  cd /Users/gabrielkagan/Documents/kalshi-bot
  python3 scripts/research/algo_zoo/terminal_rail_queue_rebate.py
"""
from __future__ import annotations

import math
import os
import random
import sys
from collections import defaultdict
from typing import Dict, List, Optional, Tuple

sys.path.insert(0, "/Users/gabrielkagan/Documents/kalshi-bot")

from scripts.research import kalshi_book_reconstruct as kbr  # noqa: E402
from scripts.research.phase1b_real_price_economics import (  # noqa: E402
    _is_crypto_15m,
    close_epoch_from_ticker,
    load_frames_jsonl,
)
from scripts.research.phase1b_live_shadow import load_trades_by_ticker  # noqa: E402
from scripts.research.mm_markout_evaluator import (  # noqa: E402
    first_yes_bid_fill_ts,
    first_no_bid_fill_ts,
)

FRAMES_FILE = "/tmp/edge_daily/frames_crypto.jsonl"
TRADES_FILE = "/tmp/edge_daily/trades_crypto.jsonl"

DECISION_OFFSETS_S = (120, 90, 60, 45, 30)
RAIL_C = 97.0            # locked-side threshold (YES bid>=97 => locked YES)
REBATES_C = (0.0, 0.25)  # maker rebate sweep (cents/contract)
N_BOOT = 2000
HEADLINE_OFFSET = 60     # canonical T for the structured headline
HEADLINE_REBATE = 0.0    # default-rebate verdict (rebate=0)


def fee_per_contract_cents(price_cents: float) -> float:
    """Kalshi per-CONTRACT fee, order-rounded up to the cent (spec form):
    ceil(0.07 * C * P * (1-P) * 100) with C=1. At the 97c rail this is
    ceil(0.07*0.97*0.03*100)=ceil(0.20)=1c."""
    p = price_cents / 100.0
    return math.ceil(0.07 * 1 * p * (1.0 - p) * 100.0)


def terminal_yes_win(frames, close_epoch: float) -> Optional[bool]:
    """Label from the TERMINAL RELIABLE book at close: yes_mid > 50 => YES wins.
    Returns None if no reliable book at close (refuse -> can't label -> drop)."""
    bid, ask = kbr.reliable_nbbo_at(frames, close_epoch)
    if bid is None or ask is None:
        return None
    ymid = (bid + ask) / 2.0
    if abs(ymid - 50.0) < 1e-9:
        return None  # dead-tie book -> unlabelable
    return ymid > 50.0


def cluster_bootstrap_ci(
    by_ticker: Dict[str, List[float]], *, n_boot: int = N_BOOT,
    alpha: float = 0.05, seed: int = 7,
) -> Tuple[float, float, float]:
    """Block bootstrap CLUSTERED by ticker (the true independent unit). Resample
    tickers with replacement; statistic = mean over all pooled contract-level
    PnLs from the resampled tickers. Returns (point_mean, lo, hi)."""
    tickers = list(by_ticker.keys())
    if not tickers:
        return (float("nan"), float("nan"), float("nan"))
    all_vals = [v for vs in by_ticker.values() for v in vs]
    point = sum(all_vals) / len(all_vals)
    rng = random.Random(seed)
    n = len(tickers)
    means = []
    for _ in range(n_boot):
        tot = 0.0
        cnt = 0
        for _ in range(n):
            vs = by_ticker[tickers[rng.randrange(n)]]
            tot += sum(vs)
            cnt += len(vs)
        if cnt:
            means.append(tot / cnt)
    means.sort()
    lo = means[int((alpha / 2) * len(means))]
    hi = means[min(len(means) - 1, int((1 - alpha / 2) * len(means)))]
    return (point, lo, hi)


def main() -> dict:
    if not os.path.exists(FRAMES_FILE) or not os.path.exists(TRADES_FILE):
        return {"data_gap": "frames or trades file missing locally"}

    print(f"loading frames {FRAMES_FILE} ...", flush=True)
    frames = load_frames_jsonl(FRAMES_FILE)
    print(f"  crypto-15M tickers in frames: {len(frames)}", flush=True)
    print(f"loading trades {TRADES_FILE} ...", flush=True)
    trades = load_trades_by_ticker(TRADES_FILE)
    print(f"  tickers with trades: {len(trades)}", flush=True)

    # Per (offset, rebate): per-ticker list of filled-contract PnLs.
    filled_pnls: Dict[Tuple[int, float], Dict[str, List[float]]] = {
        (off, reb): defaultdict(list)
        for off in DECISION_OFFSETS_S for reb in REBATES_C
    }

    diag = {off: {
        "tickers_seen": 0,
        "no_reliable_book": 0,
        "not_rail": 0,
        "rail_yes": 0,
        "rail_no": 0,
        "posted": 0,
        "filled": 0,
        "no_terminal_label": 0,
        "rail_books": 0,       # rail-decided + labelable (AS-tax denominator)
        "rail_books_win": 0,   # implied winner actually won (ALL rail books)
        "filled_win": 0,       # implied winner won among FILLED
    } for off in DECISION_OFFSETS_S}

    asset_filled = defaultdict(lambda: {"n": 0, "win": 0})

    for tk, fr in frames.items():
        asset = _is_crypto_15m(tk)
        if asset is None:
            continue
        close = close_epoch_from_ticker(tk)
        tr = trades.get(tk, [])
        yes_win = terminal_yes_win(fr, close)  # offset-independent label

        for off in DECISION_OFFSETS_S:
            d = diag[off]
            d["tickers_seen"] += 1
            decision_ts = close - off
            bid, ask = kbr.reliable_nbbo_at(fr, decision_ts)
            if bid is None or ask is None:
                d["no_reliable_book"] += 1
                continue
            no_bid = 100.0 - ask  # best NO bid (NO price units)

            if bid >= RAIL_C:
                side, post_price, implied_yes_winner = "yes", bid, True
            elif no_bid >= RAIL_C:
                side, post_price, implied_yes_winner = "no", no_bid, False
            else:
                d["not_rail"] += 1
                continue

            if not (0 < post_price < 100):
                continue
            if yes_win is None:
                d["no_terminal_label"] += 1
                continue

            d["rail_books"] += 1
            implied_correct = (implied_yes_winner == yes_win)
            if implied_correct:
                d["rail_books_win"] += 1
            d["rail_yes" if side == "yes" else "rail_no"] += 1
            d["posted"] += 1

            # HONEST fill: a real trade must cross our resting bid after decision.
            if side == "yes":
                fill_ts = first_yes_bid_fill_ts(tr, decision_ts, post_price)
            else:
                fill_ts = first_no_bid_fill_ts(tr, decision_ts, post_price)
            if fill_ts is None or fill_ts > close:
                continue

            d["filled"] += 1
            win = implied_correct
            if win:
                d["filled_win"] += 1

            fee = fee_per_contract_cents(post_price)
            gross = (100.0 - post_price) if win else (-post_price)
            for reb in REBATES_C:
                filled_pnls[(off, reb)][tk].append(gross - fee + reb)

            if off == HEADLINE_OFFSET:
                a = asset_filled[asset]
                a["n"] += 1
                a["win"] += int(win)

    # ---- report ----
    print("\n=== per-offset funnel ===", flush=True)
    print(f"{'T-s':>5}{'seen':>7}{'noBook':>8}{'notRail':>8}{'railY':>7}{'railN':>7}"
          f"{'posted':>8}{'filled':>8}{'fill%':>7}{'noLbl':>7}", flush=True)
    for off in DECISION_OFFSETS_S:
        d = diag[off]
        fp = 100 * d["filled"] / d["posted"] if d["posted"] else 0.0
        print(f"{off:>5}{d['tickers_seen']:>7}{d['no_reliable_book']:>8}"
              f"{d['not_rail']:>8}{d['rail_yes']:>7}{d['rail_no']:>7}"
              f"{d['posted']:>8}{d['filled']:>8}{fp:>7.1f}{d['no_terminal_label']:>7}",
              flush=True)

    print("\n=== adverse-selection tax (implied-winner win%: ALL rail books vs FILLED) ===",
          flush=True)
    print(f"{'T-s':>5}{'railBooks':>10}{'allWin%':>9}{'filled':>8}{'fillWin%':>10}{'AStax(pp)':>11}",
          flush=True)
    for off in DECISION_OFFSETS_S:
        d = diag[off]
        allw = 100 * d["rail_books_win"] / d["rail_books"] if d["rail_books"] else float("nan")
        flw = 100 * d["filled_win"] / d["filled"] if d["filled"] else float("nan")
        tax = (allw - flw) if d["rail_books"] and d["filled"] else float("nan")
        print(f"{off:>5}{d['rail_books']:>10}{allw:>9.1f}{d['filled']:>8}{flw:>10.1f}{tax:>11.1f}",
              flush=True)

    print("\n=== HEADLINE: mean net PnL per FILLED contract (clustered bootstrap CI) ===",
          flush=True)
    print(f"{'T-s':>5}{'rebate':>8}{'nFilled':>9}{'nTk':>5}{'meanPnL':>9}{'CIlo':>8}{'CIhi':>8}{'verdict':>9}",
          flush=True)
    results = {}
    for off in DECISION_OFFSETS_S:
        for reb in REBATES_C:
            bt = filled_pnls[(off, reb)]
            n_filled = sum(len(v) for v in bt.values())
            n_tk = len(bt)
            mean, lo, hi = cluster_bootstrap_ci(bt)
            verdict = "EDGE" if (n_filled and lo > 0) else "no"
            results[(off, reb)] = {
                "mean": mean, "lo": lo, "hi": hi,
                "n_filled": n_filled, "n_tk": n_tk,
            }
            mstr = f"{mean:+.2f}" if n_filled else "—"
            lostr = f"{lo:+.2f}" if n_filled else "—"
            histr = f"{hi:+.2f}" if n_filled else "—"
            print(f"{off:>5}{reb:>8.2f}{n_filled:>9}{n_tk:>5}{mstr:>9}{lostr:>8}{histr:>8}{verdict:>9}",
                  flush=True)

    print("\n=== per-asset (T-60s, rebate=0) filled win-rate ===", flush=True)
    for a in sorted(asset_filled):
        x = asset_filled[a]
        wr = 100 * x["win"] / x["n"] if x["n"] else 0.0
        print(f"  {a:>5}  nFilled={x['n']:>4}  win%={wr:5.1f}", flush=True)

    return {"results": results, "diag": diag}


if __name__ == "__main__":
    main()
