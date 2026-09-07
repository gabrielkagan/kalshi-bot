"""trade_print_absence_decay_maker — quote into SILENCE, not activity.

Family: inverse-Hawkes trade-intensity / maker fill-selection economics.

THESIS
------
The "maker-mirage in the 60-89c middle" kill (2.6% fill, -79c on fills) aggregated
two very different fill populations:
  - CLUSTERED fills: a sharp taker sweeping the book in a burst of prints — we get
    picked off (adverse selection). These carry the -79c.
  - ISOLATED fills:  a lone print lands in a QUIET book (no recent trade). Plausibly
    uninformed liquidity-demand; the patient maker captures spread + convergence.
If the toxicity is carried by the clustered minority, SELECTING isolated fills (via
an honest fill ledger + inter-trade gap) recovers the canonical patient-liquidity
edge. This is the FIRST time the honest fill primitives let us fill-select.

DESIGN (causal, no look-ahead)
------------------------------
For each crypto-15M ticker, at a grid of post times every GRID_S seconds before
close, in the MIDDLE (60-89c) + rails (1-39, 90-99) bands:
  1. QUOTE: reliable best bid for YES and NO at post_ts (reliable_nbbo_at; refuses
     drifted books). Post a resting bid there.
  2. HONEST FILL: first_yes_bid_fill_ts / first_no_bid_fill_ts — a fill happens only
     when a REAL print crosses our resting bid (mm_markout_evaluator primitives).
  3. SILENCE FEATURE (causal): gap = fill_ts - (last trade-print ts on this ticker
     STRICTLY BEFORE fill_ts). gap>=G -> ISOLATED; gap<G -> CLUSTERED. Pure PRE-fill
     gap — the decision gates on this only. A post-fill cluster count is observed as
     a diagnostic, NOT a gate (no peeking).
  4. MARKOUT: @30s (reliable side-mid at fill+30, fee-net) and at SETTLEMENT
     (settlement_markout_cents, fee-inclusive). Settlement label from the TERMINAL
     reliable book mid at close (in-window DB outcomes are sparse/just-settling).
  5. KEEP ISOLATED fills (the candidate arm); CLUSTERED is the control arm.

GATE (pre-registered, per side, per band)
-----------------------------------------
  EDGE iff, net of fees:
    block-bootstrap CI-lower of ISOLATED settlement markout > 0  AND
    mean ISOLATED settlement markout > mean CLUSTERED settlement markout.
  Bootstrap is CLUSTERED BY TICKER (the true independent unit). YES-maker and
  NO-maker reported SEPARATELY.

Fees: ceil(0.07*C*P*(1-P)) cents/contract (Kalshi rounds the per-order fee up to
the cent; a 1-contract maker order pays the ceil). Maker rebate assumed 0.

DATA: frames_crypto.jsonl + trades_crypto.jsonl + terminal-book label. No spot.

Usage:
  cd /Users/gabrielkagan/Documents/kalshi-bot
  python3 scripts/research/algo_zoo/trade_print_absence_decay_maker.py \
    --frames-file /tmp/edge_daily/frames_crypto.jsonl \
    --trades-file /tmp/edge_daily/trades_crypto.jsonl
"""
from __future__ import annotations

import argparse
import math
import random
import sys
from collections import defaultdict
from typing import Dict, List, Optional, Sequence, Tuple

sys.path.insert(0, "/Users/gabrielkagan/Documents/kalshi-bot")

from scripts.research import kalshi_book_reconstruct as kbr  # noqa: E402
from scripts.research.phase1b_real_price_economics import (  # noqa: E402
    _band,
    close_epoch_from_ticker,
    load_frames_jsonl,
)
from scripts.research.phase1b_live_shadow import load_trades_by_ticker  # noqa: E402
from scripts.research.mm_markout_evaluator import (  # noqa: E402
    first_no_bid_fill_ts,
    first_yes_bid_fill_ts,
    settlement_markout_cents,
    side_mid,
    yes_mid,
)

# ---- knobs -----------------------------------------------------------------
GRID_S = 30.0                # post a resting bid every 30s before close
QUOTE_START_BEFORE = 600.0   # start quoting at T-600s (last 10 min)
QUOTE_END_BEFORE = 30.0      # stop at T-30s (need 30s markout room before close)
G_SECONDS = 15.0             # isolation threshold: gap >= G -> ISOLATED
MARKOUT_H = 30.0             # 30s markout horizon
TARGET_BANDS = ("60-89", "90-99", "1-39")  # middle + the two rails
MIN_FILLS = 30               # per (side,band) cell floor below which -> insufficient
N_BOOT = 2000
FEE_REBATE = 0.0             # maker rebate assumption (Kalshi: none)


def fee_ceil_cents(price_cents: float) -> float:
    """ceil(0.07*C*P*(1-P)) for C=1 contract, in cents. Kalshi rounds the
    per-ORDER fee up to the next cent, so the honest per-fill (1-contract) fee is
    the CEIL of the rate."""
    p = price_cents / 100.0
    return math.ceil(7.0 * p * (1.0 - p)) - FEE_REBATE


def _prev_print_ts(trades: Sequence[Tuple[float, float, str]], fill_ts: float) -> Optional[float]:
    """Causal: ts of the last trade print STRICTLY BEFORE fill_ts on this ticker.
    trades is sorted ascending [(ts, yes_c, taker_side)]. The fill print itself is
    AT fill_ts; the inter-arrival gap to the PREVIOUS print uses < (strict)."""
    prev = None
    for ts, _yp, _side in trades:
        if ts < fill_ts:
            prev = ts
        else:
            break
    return prev


def _post_count_after(trades: Sequence[Tuple[float, float, str]], fill_ts: float,
                      window_s: float) -> int:
    """DIAGNOSTIC ONLY (not a gate): number of prints in (fill_ts, fill_ts+window]."""
    return sum(1 for ts, _yp, _side in trades if fill_ts < ts <= fill_ts + window_s)


LABEL_LOOKBACK_S = 120.0   # how far back from close to find the LAST reliable book
LABEL_STEP_S = 5.0


def _terminal_label(frames: Sequence, close_epoch: float) -> Optional[str]:
    """Settlement label from the TERMINAL reliable book mid: YES if the LAST
    reliable yes-mid at/before close is >= 50, else NO. The book frequently goes
    stale/empty in the final second, so reliable_nbbo_at(close) alone refuses ~93%
    of windows; we step BACKWARD from close (strictly causal, <= close) up to
    LABEL_LOOKBACK_S to find the most recent reliable book. Returns None if NO
    reliable book exists in the final LABEL_LOOKBACK_S (refuse to fabricate a label).
    This is a robust proxy for the binary outcome; in-window DB outcomes are sparse
    / just-settling. A book pinned near 50 at close is genuinely undetermined — but
    those are a tiny minority of 15M windows that resolve decisively by close."""
    t = close_epoch
    floor = close_epoch - LABEL_LOOKBACK_S
    while t >= floor:
        bid, ask = kbr.reliable_nbbo_at(frames, t)
        if bid is not None and ask is not None:
            return "yes" if yes_mid(bid, ask) >= 50.0 else "no"
        t -= LABEL_STEP_S
    return None


def _reliable_side_mid(frames, at_ts: float, side: str) -> Optional[float]:
    bid, ask = kbr.reliable_nbbo_at(frames, at_ts)
    if bid is None or ask is None:
        return None
    return side_mid(yes_mid(bid, ask), side)


def block_bootstrap_ci(
    by_ticker: Dict[str, List[float]], *, n_boot: int = N_BOOT, alpha: float = 0.05,
    seed: int = 4242,
) -> Tuple[float, float, float, int]:
    """Cluster (block) bootstrap of the per-FILL mean, resampling TICKERS with
    replacement (the independent unit; fills on one ticker are serially correlated).
    Returns (lo, hi, point_mean, n_fills)."""
    tickers = [tk for tk, v in by_ticker.items() if v]
    all_vals = [x for tk in tickers for x in by_ticker[tk]]
    n_fills = len(all_vals)
    if n_fills == 0 or not tickers:
        return (float("nan"), float("nan"), float("nan"), 0)
    point = sum(all_vals) / n_fills
    rng = random.Random(seed)
    n_tk = len(tickers)
    means = []
    for _ in range(n_boot):
        s = 0.0
        cnt = 0
        for _ in range(n_tk):
            vv = by_ticker[tickers[rng.randrange(n_tk)]]
            for x in vv:
                s += x
                cnt += 1
        if cnt:
            means.append(s / cnt)
    if not means:
        return (float("nan"), float("nan"), point, n_fills)
    means.sort()
    lo = means[int((alpha / 2) * len(means))]
    hi = means[min(len(means) - 1, int((1 - alpha / 2) * len(means)))]
    return (lo, hi, point, n_fills)


def _mean(xs: Sequence[float]) -> float:
    xs = list(xs)
    return sum(xs) / len(xs) if xs else float("nan")


def evaluate(frames, trades, *, g_seconds=G_SECONDS) -> dict:
    """Returns {cells: {(side,band): stats}, n_windows_used, n_no_label}."""
    cells = defaultdict(lambda: {
        "posted": 0, "filled": 0,
        "iso_settle_by_tk": defaultdict(list),
        "clu_settle_by_tk": defaultdict(list),
        "iso_mk30": [], "clu_mk30": [],
        "iso_gap": [], "clu_gap": [],
        "iso_postcount": [], "clu_postcount": [],
        "iso_fillpx": [], "clu_fillpx": [],
    })

    n_windows_used = 0
    n_no_label = 0
    for tk, fr in frames.items():
        if tk not in trades:
            continue
        close = close_epoch_from_ticker(tk)
        result = _terminal_label(fr, close)
        if result is None:
            n_no_label += 1
            continue
        n_windows_used += 1
        tr = trades[tk]

        off = QUOTE_START_BEFORE
        while off >= QUOTE_END_BEFORE:
            post_ts = close - off
            off -= GRID_S
            bid, ask = kbr.reliable_nbbo_at(fr, post_ts)
            if bid is None or ask is None:
                continue  # GUARD: no reliable book at quote time
            ymid = yes_mid(bid, ask)
            no_bid = 100.0 - ask  # best NO bid

            for side, price, fill_ts in (
                ("yes", bid, first_yes_bid_fill_ts(tr, post_ts, bid)),
                ("no", no_bid, first_no_bid_fill_ts(tr, post_ts, no_bid)),
            ):
                if not (0 < price < 100):
                    continue
                band = _band(price)  # band keyed on the FILLED side's own price
                if band not in TARGET_BANDS:
                    continue
                cell = cells[(side, band)]
                cell["posted"] += 1
                if fill_ts is None or fill_ts >= close:
                    continue
                cell["filled"] += 1

                fee = fee_ceil_cents(price)
                settle = settlement_markout_cents(price, side, result) - fee

                # SILENCE feature (causal, PRE-fill only)
                prev = _prev_print_ts(tr, fill_ts)
                gap = (fill_ts - prev) if prev is not None else float("inf")
                isolated = gap >= g_seconds

                # 30s markout (fee-net), reliable mid, capped before close
                mk30 = None
                at = fill_ts + MARKOUT_H
                if at <= close:
                    smid_fut = _reliable_side_mid(fr, at, side)
                    if smid_fut is not None:
                        mk30 = (smid_fut - price) - fee

                # diagnostic post-fill cluster count (NOT a gate)
                postcount = _post_count_after(tr, fill_ts, g_seconds)

                if isolated:
                    cell["iso_settle_by_tk"][tk].append(settle)
                    cell["iso_gap"].append(gap if gap != float("inf") else 9999.0)
                    cell["iso_postcount"].append(postcount)
                    cell["iso_fillpx"].append(price)
                    if mk30 is not None:
                        cell["iso_mk30"].append(mk30)
                else:
                    cell["clu_settle_by_tk"][tk].append(settle)
                    cell["clu_gap"].append(gap)
                    cell["clu_postcount"].append(postcount)
                    cell["clu_fillpx"].append(price)
                    if mk30 is not None:
                        cell["clu_mk30"].append(mk30)

    return {"cells": cells, "n_windows_used": n_windows_used, "n_no_label": n_no_label}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--frames-file", required=True)
    ap.add_argument("--trades-file", required=True)
    ap.add_argument("--outcomes-db", required=False, default=None,
                    help="state.db (optional; label primarily from terminal book)")
    ap.add_argument("--g-seconds", type=float, default=G_SECONDS)
    args = ap.parse_args(argv)

    print("loading frames...", flush=True)
    frames = load_frames_jsonl(args.frames_file)
    print(f"  crypto-15M tickers in frames: {len(frames)}", flush=True)
    print("loading trades...", flush=True)
    trades = load_trades_by_ticker(args.trades_file)
    print(f"  tickers with trades: {len(trades)}", flush=True)

    res = evaluate(frames, trades, g_seconds=args.g_seconds)
    cells = res["cells"]
    print(f"windows used (reliable terminal label + trades): {res['n_windows_used']}  "
          f"(no-label-skipped: {res['n_no_label']})")
    print(f"G (isolation threshold) = {args.g_seconds:.0f}s\n")

    print(f"{'side':>4}{'band':>7}{'posted':>8}{'filled':>8}{'fill%':>7}"
          f"{'isoN':>6}{'cluN':>6}{'isoSettle':>11}{'isoCI':>18}"
          f"{'cluSettle':>11}{'isoMk30':>9}{'cluMk30':>9}{'VERDICT':>9}")

    results = {}
    for (side, band) in sorted(cells):
        c = cells[(side, band)]
        if not c["posted"]:
            continue
        iso_lo, iso_hi, iso_point, iso_n = block_bootstrap_ci(c["iso_settle_by_tk"])
        clu_all = [x for v in c["clu_settle_by_tk"].values() for x in v]
        clu_mean = _mean(clu_all) if clu_all else float("nan")
        clu_n = len(clu_all)
        fillpct = 100.0 * c["filled"] / c["posted"]

        gate_ci_pos = (iso_n >= MIN_FILLS) and (not math.isnan(iso_lo)) and (iso_lo > 0)
        gate_sep = (not math.isnan(clu_mean)) and (iso_point > clu_mean)
        survives = gate_ci_pos and gate_sep
        if iso_n < MIN_FILLS:
            verdict = "insuff"
        elif survives:
            verdict = "EDGE"
        else:
            verdict = "kill"

        ci_str = (f"[{iso_lo:+.1f},{iso_hi:+.1f}]" if iso_n else "—")
        print(f"{side:>4}{band:>7}{c['posted']:>8}{c['filled']:>8}{fillpct:>7.2f}"
              f"{iso_n:>6}{clu_n:>6}{iso_point:>+11.2f}{ci_str:>18}"
              f"{clu_mean:>+11.2f}{_mean(c['iso_mk30']):>+9.2f}{_mean(c['clu_mk30']):>+9.2f}"
              f"{verdict:>9}")

        results[(side, band)] = {
            "iso_point": iso_point, "iso_lo": iso_lo, "iso_hi": iso_hi,
            "iso_n": iso_n, "clu_mean": clu_mean, "clu_n": clu_n,
            "iso_mk30": _mean(c["iso_mk30"]), "clu_mk30": _mean(c["clu_mk30"]),
            "verdict": verdict,
            "iso_gap_med": (sorted(c["iso_gap"])[len(c["iso_gap"]) // 2] if c["iso_gap"] else float("nan")),
            "iso_postcount_mean": _mean(c["iso_postcount"]),
            "clu_postcount_mean": _mean(c["clu_postcount"]),
            "iso_fillpx_mean": _mean(c["iso_fillpx"]),
        }

    print("\nDIAGNOSTIC (post-fill cluster counts — NOT gated; checks the no-peek "
          "isolated arm isn't accidentally toxic):")
    print(f"{'side':>4}{'band':>7}{'isoGapMed':>11}{'isoPostCt':>11}{'cluPostCt':>11}{'isoFillPx':>11}")
    for (side, band) in sorted(results):
        r = results[(side, band)]
        if r["iso_n"] == 0:
            continue
        print(f"{side:>4}{band:>7}{r['iso_gap_med']:>11.1f}{r['iso_postcount_mean']:>11.2f}"
              f"{r['clu_postcount_mean']:>11.2f}{r['iso_fillpx_mean']:>11.1f}")

    survivors = {k: v for k, v in results.items() if v["verdict"] == "EDGE"}
    print()
    if survivors:
        print(f"EDGE cells: {sorted(survivors)}")
    else:
        print("No cell cleared the gate (CI-lower>0 AND iso>clu).")

    main.RESULTS = results  # type: ignore[attr-defined]
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
