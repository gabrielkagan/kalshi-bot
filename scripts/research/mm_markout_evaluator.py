"""Markout-centric market-making evaluator (WWJD MM frame).

Scores 2-sided maker quoting by MARKOUT — realized spread capture net of adverse
selection — instead of naive win-rate/EV. This is the market-maker-native edge
signal and the empirical "can we compete?" test: per (asset × band × side) cell,
negative markout = we get picked off (faster/sharper players are there); positive
markout = the book is soft enough that we're the sharper quoter.

    markout(Δ) = spread_captured + adverse_selection(Δ)
      spread_captured       = side_mid_at_fill − fill_price
      adverse_selection(Δ)  = side_mid(fill+Δ) − side_mid_at_fill
      markout(Δ)            = side_mid(fill+Δ) − fill_price

ENCODED ARTIFACT GUARDS (the 7 lessons, as automatic filters — no run can repeat them):
  - reliable_nbbo_at only (snapshot-anchored; refuses drifted books)  -> reconstruction-drift
  - mid marks also reliable (both quote AND markout use reliable books) -> stale/ghost
  - real-trade fill model (fill only when a real print crosses us)    -> maker mirage
  - YES-maker and NO-maker reported SEPARATELY (never aggregated)      -> directional drift
  - fee-inclusive settlement markout                                  -> cheap-loss/fee-rounding
  - per-band stratification + per-cell min-fills floor                -> small-n flips

PRE-REGISTERED KILL CRITERIA (locked in `cell_gate` before we see data — WWJD):
  a cell SURVIVES only if, net of fees: bootstrap-CI lower bound of settlement
  markout > 0 AND mean markout@30s > 0 (not getting picked off) AND n_fills ≥ floor.

Usage (on clean post-fix bronze, prefiltered to crypto-15M):
  python3 -m scripts.research.mm_markout_evaluator \
    --frames-file /tmp/frames_crypto.jsonl --trades-file /tmp/trades_crypto.jsonl \
    --outcomes-db /tmp/state.db

Parent: kb/decisions/settlement-convergence-worklist.md (markout MM evaluator)
"""
from __future__ import annotations

import argparse
import random
from collections import defaultdict
from typing import List, Optional, Sequence, Tuple

from scripts.research import kalshi_book_reconstruct as kbr
from scripts.research.phase1b_real_price_economics import (
    _band, _is_crypto_15m, close_epoch_from_ticker,
    kalshi_fee_per_contract_cents, load_frames_jsonl, load_outcomes_db,
)
from scripts.research.phase1b_live_shadow import load_trades_by_ticker

# Quote times before close (start of the 60s settlement-averaging window + a wider
# anchor). The convergence edge lives in the final minute; latency is NOT the
# binding variable here (the settlement value forms over ~60s), so seconds-latency
# reconstruction is fair.
POST_OFFSETS_S = (60, 120)
# Markout horizons after a fill (seconds). Settlement is the terminal mark.
MARKOUT_HORIZONS_S = (5, 30)
MIN_FILLS_FLOOR = 30  # per-cell; below this a cell is "insufficient", never an edge


# ----- price-space primitives (side-symmetric) ----------------------------


def yes_mid(yes_bid: float, yes_ask: float) -> float:
    return (yes_bid + yes_ask) / 2.0


def side_mid(yes_mid_val: float, side: str) -> float:
    """Mid in the FILLED side's own price units. NO-mid = 100 − YES-mid."""
    return yes_mid_val if side == "yes" else 100.0 - yes_mid_val


def latency_attribution(as_1s, as_30s, eps: float = 0.5) -> str:
    """Would-faster-help diagnostic from the adverse-selection curve. Classifies
    WHY a cell is getting picked off, so we don't chase a C++/FPGA rewrite that
    wouldn't help (the bot's edge thesis is on the non-latency axis):
      - "clean": ~no adverse selection (|as_30s| ≤ eps) — latency irrelevant.
      - "fast_pickoff": ≥50% of the 30s adverse selection already realized by +1s →
        a fast counterparty hit our stale quote; a faster system could have pulled
        it. LATENCY-FIXABLE (colocation/hot-path — see worklist C++/FPGA note).
      - "slow_drift": the move bled in over the window, not at the instant of fill →
        a fair-value/timing problem, NOT latency. Faster wouldn't help."""
    if as_1s is None or as_30s is None or as_30s >= -eps:
        return "clean"
    frac = as_1s / as_30s  # both negative → fraction of total AS realized by +1s
    return "fast_pickoff" if frac >= 0.5 else "slow_drift"


def markout_cents(side_mid_future: float, fill_price: float) -> float:
    """Total mark-to-mid PnL of a maker fill at `fill_price`, marked to the
    side-mid at fill+Δ. Positive = the mid moved our way (or we captured spread)."""
    return side_mid_future - fill_price


def decompose_markout(
    fill_price: float, side_mid_at_fill: float, side_mid_future: float
) -> dict:
    """Split markout into spread_captured (filled inside the spread) +
    adverse_selection (post-fill mid drift; negative = picked off)."""
    spread_captured = side_mid_at_fill - fill_price
    adverse_selection = side_mid_future - side_mid_at_fill
    return {
        "spread_captured": spread_captured,
        "adverse_selection": adverse_selection,
        "markout": spread_captured + adverse_selection,
    }


def settlement_markout_cents(fill_price: float, side: str, result: str) -> float:
    """Markout to the binary outcome: side wins → 100 − fill; loses → −fill."""
    return (100.0 - fill_price) if side == result else -float(fill_price)


# ----- statistics ----------------------------------------------------------


def bootstrap_ci(
    xs: Sequence[float], *, n_boot: int = 1000, alpha: float = 0.05, seed: int = 12345
) -> Tuple[float, float]:
    """Percentile bootstrap CI for the mean. Deterministic (fixed seed) so the
    gate verdict is reproducible run-to-run on the same data."""
    xs = list(xs)
    if not xs:
        return (float("nan"), float("nan"))
    rng = random.Random(seed)
    n = len(xs)
    means = []
    for _ in range(n_boot):
        s = 0.0
        for _ in range(n):
            s += xs[rng.randrange(n)]
        means.append(s / n)
    means.sort()
    lo = means[int((alpha / 2) * n_boot)]
    hi = means[min(n_boot - 1, int((1 - alpha / 2) * n_boot))]
    return (lo, hi)


def _mean(xs: Sequence[float]) -> float:
    xs = list(xs)
    return sum(xs) / len(xs) if xs else float("nan")


def cell_gate(
    *,
    settlement_markouts: Sequence[float],
    markouts_30s: Sequence[float],
    fee_cents: float,
    n_fills: int,
    min_fills: int = MIN_FILLS_FLOOR,
) -> dict:
    """Pre-registered kill criteria. A cell SURVIVES iff, net of fees:
      1. n_fills ≥ min_fills, AND
      2. bootstrap-CI lower bound of (settlement markout − fee) > 0, AND
      3. mean (markout@30s − fee) > 0 (not getting picked off at the 30s horizon).
    Returns {survives, fail_reasons, ...metrics}."""
    fails: List[str] = []
    if n_fills < min_fills:
        fails.append("n_fills")
    net_settle = [m - fee_cents for m in settlement_markouts]
    lo, hi = bootstrap_ci(net_settle) if net_settle else (float("nan"), float("nan"))
    if not (net_settle and lo > 0):
        fails.append("settlement_ci_includes_zero")
    mk30 = _mean([m - fee_cents for m in markouts_30s]) if markouts_30s else float("nan")
    if not (markouts_30s and mk30 > 0):
        fails.append("negative_markout_30s")
    return {
        "survives": not fails,
        "fail_reasons": fails,
        "n_fills": n_fills,
        "settle_mean_net": _mean(net_settle),
        "settle_ci": (lo, hi),
        "markout_30s_net": mk30,
    }


# ----- fill model with FILL TIME (markout is measured from the fill) -------


def first_yes_bid_fill_ts(trades, post_ts: float, bid_cents: float) -> Optional[float]:
    """ts of the first real YES-SELL (taker_side='no') at yes_price ≤ our bid after
    we post — the moment our resting YES bid would have been hit."""
    best = None
    for ts, yp, side in trades:
        if ts > post_ts and side == "no" and yp <= bid_cents:
            best = ts if best is None else min(best, ts)
    return best


def first_no_bid_fill_ts(trades, post_ts: float, no_bid_cents: float) -> Optional[float]:
    """ts of the first real YES-BUY (taker_side='yes') at yes_price ≥ 100−no_bid
    after we post — our resting NO bid (offer to sell YES) gets lifted."""
    thresh = 100.0 - no_bid_cents
    best = None
    for ts, yp, side in trades:
        if ts > post_ts and side == "yes" and yp >= thresh:
            best = ts if best is None else min(best, ts)
    return best


# ----- harness --------------------------------------------------------------


def _reliable_side_mid(frames, at_ts: float, side: str) -> Optional[float]:
    bid, ask = kbr.reliable_nbbo_at(frames, at_ts)
    if bid is None or ask is None:
        return None
    return side_mid(yes_mid(bid, ask), side)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--frames-file", required=True)
    ap.add_argument("--trades-file", required=True)
    ap.add_argument("--outcomes-db", required=True)
    args = ap.parse_args(argv)

    frames = load_frames_jsonl(args.frames_file)
    trades = load_trades_by_ticker(args.trades_file)
    outcomes = load_outcomes_db(args.outcomes_db, set(frames))
    print(f"windows: {len(frames)}  with trades: {sum(1 for t in frames if t in trades)}  "
          f"with outcome: {sum(1 for t in frames if t in outcomes)}")

    result = evaluate(frames, trades, outcomes)
    cells, survivors = result["cells"], result["survivors"]

    print(f"\n{'band':>7}{'side':>5}{'posted':>7}{'fill%':>7}{'fills':>6}"
          f"{'sprd':>7}{'mk5':>7}{'mk30':>7}{'settleNet':>10}{'settleCI':>16}"
          f"{'latency':>13}{'VERDICT':>9}")
    for key in sorted(cells):
        band, side = key
        c = cells[key]
        if not c["posted"]:
            continue
        fillpct = 100 * c["filled"] / c["posted"]
        g = c["gate"]
        verdict = "SURVIVE" if g["survives"] else "kill"
        lo, hi = g["settle_ci"]
        ci = f"[{lo:+.1f},{hi:+.1f}]" if c["settle"] else "—"
        print(f"{band:>7}{side:>5}{c['posted']:>7}{fillpct:>7.1f}{c['filled']:>6}"
              f"{_mean(c['spread']):>7.1f}{_mean(c['mk5']):>7.1f}{_mean(c['mk30']):>7.1f}"
              f"{_mean(c['settle']):>+10.2f}{ci:>16}{c['attribution']:>13}{verdict:>9}")

    print()
    if survivors:
        print(f"🟢 {len(survivors)} CANDIDATE cell(s) cleared the pre-registered gate "
              f"→ dispatch adversarial review before believing:")
        for s in survivors:
            print(f"   {s['cell']}: settleNet={s['settle_mean_net']:+.2f}c "
                  f"CI={tuple(round(x,1) for x in s['settle_ci'])} "
                  f"mk30={s['markout_30s_net']:+.2f}c n={s['n_fills']} "
                  f"latency={s['attribution']}")
    else:
        print("⚪ No cell cleared the gate (efficient / insufficient fills). "
              "Expected on the liquid majors; the signal to watch is thin markets "
              "+ benign-flow regimes as clean data accrues.")
    return 0


def evaluate(frames, trades, outcomes) -> dict:
    """Run the MM-markout battery over all windows. Returns
    {cells: {(band,side): stats}, survivors: [dict]} — importable by the daily
    runner (no stdout parsing). Encoded guards: reliable books, real fills,
    fee-inclusive, sides kept separate, per-band."""
    cells = defaultdict(lambda: {
        "posted": 0, "filled": 0, "spread": [], "mk1": [], "mk5": [], "mk30": [],
        "as1": [], "as30": [], "settle": []})

    for tk, fr in frames.items():
        if tk not in outcomes or tk not in trades:
            continue
        result = outcomes[tk]["result"]
        close = close_epoch_from_ticker(tk)
        tr = trades[tk]
        for off in POST_OFFSETS_S:
            post_ts = close - off
            bid, ask = kbr.reliable_nbbo_at(fr, post_ts)
            if bid is None or ask is None:
                continue  # GUARD: no reliable book at quote time -> skip
            ymid = yes_mid(bid, ask)
            no_bid = 100.0 - ask  # best NO bid
            for side, price, fill_ts in (
                ("yes", bid, first_yes_bid_fill_ts(tr, post_ts, bid)),
                ("no", no_bid, first_no_bid_fill_ts(tr, post_ts, no_bid)),
            ):
                if not (0 < price < 100):
                    continue
                c = cells[(_band(price if side == "yes" else 100 - price), side)]
                c["posted"] += 1
                if fill_ts is None:
                    continue
                c["filled"] += 1
                smid_fill = side_mid(ymid, side)
                fee = kalshi_fee_per_contract_cents(price)
                c["spread"].append(smid_fill - price)
                c["settle"].append(settlement_markout_cents(price, side, result) - fee)
                # markout + adverse-selection curve at horizons (reliable mid at
                # fill+Δ, capped before close). AS is RAW (mid drift after fill) so
                # latency_attribution can read the +1s-vs-+30s shape.
                for h, mk_key, as_key in (
                        (1, "mk1", "as1"), (5, "mk5", None), (30, "mk30", "as30")):
                    at = fill_ts + h
                    if at > close:
                        continue
                    smid_fut = _reliable_side_mid(fr, at, side)
                    if smid_fut is None:
                        continue
                    c[mk_key].append(markout_cents(smid_fut, price) - fee)
                    if as_key:
                        c[as_key].append(smid_fut - smid_fill)  # raw adverse selection

    survivors = []
    for key in sorted(cells):
        c = cells[key]
        g = cell_gate(settlement_markouts=c["settle"],  # already net of fee
                      markouts_30s=c["mk30"], fee_cents=0.0, n_fills=c["filled"])
        attribution = latency_attribution(_mean(c["as1"]) if c["as1"] else None,
                                           _mean(c["as30"]) if c["as30"] else None)
        c["gate"] = g
        c["attribution"] = attribution
        if g["survives"]:
            survivors.append({
                "cell": key,
                "settle_mean_net": g["settle_mean_net"],
                "settle_ci": g["settle_ci"],
                "markout_30s_net": g["markout_30s_net"],
                "n_fills": g["n_fills"],
                "attribution": attribution,
            })
    return {"cells": cells, "survivors": survivors}


if __name__ == "__main__":
    raise SystemExit(main())
