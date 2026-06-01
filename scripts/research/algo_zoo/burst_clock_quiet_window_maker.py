"""burst_clock_quiet_window_maker — self-exciting intensity-conditioned maker (regime-gated).

FAMILY: self-exciting intensity-conditioned market making (regime-gated)

MECHANISM (what's genuinely new here vs every tried MM idea):
  Avellaneda / OFI / Glosten quoted UNCONDITIONALLY into toxic flow and got
  adversely selected. Here the EDGE is TIMING, not pricing. Model trade arrivals
  as a self-exciting (Hawkes-lite) process: lambda(t) = exponentially-decayed
  count of prints in the trailing window, computed FROM THE TRADES STREAM using
  only ts <= decision time (no look-ahead). Quote 2-sided maker ONLY when
  lambda(t) is in its LOW decile (quiet, non-toxic) AND the band is the contested
  60-89c MIDDLE. Withdraw entirely during bursts (high lambda = informed flow).
  This is the INVERSE of hawkes_trade_burst_fade (which FADES the burst); here we
  MAKE in the quiet and SIT OUT the burst.

  Headline = per-cell settlement MARKOUT net of fees + markout@30s, stratified by
  (asset, band, side, lambda-decile). Fills are HONEST real-print-cross via the
  mm_markout_evaluator primitives (first_yes/no_bid_fill_ts). Books are RELIABLE
  only (reliable_nbbo_at refuses drifted books). Fees included.

PRE-REGISTERED cell_gate (locked before data, reused from mm_markout_evaluator):
  a cell SURVIVES iff, net of fees:
    bootstrap-CI lb of settlement markout > 0 AND mean markout@30s > 0 AND
    n_fills >= MIN_FILLS_FLOOR.
  EDGE verdict requires: a QUIET-decile MIDDLE-band cell clears the gate where the
  POOLED (intensity-unconditioned) same (asset,band,side) cell did NOT. If the
  quiet cell only mirrors a pooled cell that already cleared, the intensity gate
  added nothing -> not an edge attributable to THIS mechanism.

OUTCOME LABEL: in-window DB outcomes are sparse (windows just settling), so the
  15M above/below outcome is derived from the TERMINAL BOOK (reliable NBBO mid at
  close-2s) — NOT the DB. The label book is built INDEPENDENTLY of the signal book
  (signal at close-offset; label at terminal), no look-ahead leak into quotes.

NON-NEGOTIABLES honored: real fees, honest cross-fill, bootstrap CI clustered by
  ticker (the true independent unit), no look-ahead, fee-net EDGE only.

Run (cd repo root first):
  python3 scripts/research/algo_zoo/burst_clock_quiet_window_maker.py
"""
from __future__ import annotations

import sys
sys.path.insert(0, "/Users/gabrielkagan/Documents/kalshi-bot")

import math
import random
from collections import defaultdict
from typing import List, Optional, Sequence, Tuple

from scripts.research import kalshi_book_reconstruct as kbr
from scripts.research.phase1b_real_price_economics import (
    _band, _is_crypto_15m, close_epoch_from_ticker,
    kalshi_fee_per_contract_cents, load_frames_jsonl,
)
from scripts.research.phase1b_live_shadow import load_trades_by_ticker
from scripts.research.mm_markout_evaluator import (
    yes_mid, side_mid, settlement_markout_cents, markout_cents,
    first_yes_bid_fill_ts, first_no_bid_fill_ts, _mean,
)

FRAMES_FILE = "/tmp/edge_daily/frames_crypto.jsonl"
TRADES_FILE = "/tmp/edge_daily/trades_crypto.jsonl"

# Quote times before close. Settlement value forms over the final ~60-120s; the
# convergence edge lives in the final minute so seconds-latency reconstruction is
# fair (latency is not the binding variable for a settlement-markout maker).
POST_OFFSETS_S = (60, 120)
MARKOUT_HORIZON_S = 30  # the picked-off horizon the gate checks
LABEL_OFFSET_S = 2      # terminal book mark to derive the above/below outcome

# self-exciting intensity clock: lambda(t) = sum over prints p<=t of
# exp(-(t - ts_p)/TAU). TAU is the decay timescale (seconds).
LAMBDA_TAU_S = 90.0
INTENSITY_LOOKBACK_S = 600.0  # only sum prints within this trailing window

MIN_FILLS_FLOOR = 30        # per-cell; below this a cell is "insufficient"
N_BOOT = 2000               # >= 1000 required
SEED = 20260531
MIDDLE_BAND = "60-89"       # the contested middle the thesis targets

# maker rebate sensitivity: default 0; we ALSO report a +0.25c/contract rebate row.
REBATE_SENSITIVITY_C = 0.25


# ---------------------------------------------------------------------------
# intensity clock
# ---------------------------------------------------------------------------

def lambda_at(trades: Sequence[Tuple[float, float, str]], t: float) -> float:
    """Self-exciting trade intensity at time t from the TRADES stream, using ONLY
    prints with ts <= t (no look-ahead). EWMA-decayed count:
        lambda(t) = sum_{p: ts_p <= t, t-ts_p <= lookback} exp(-(t-ts_p)/TAU).
    `trades` is the ticker's list sorted ascending by ts of (ts, yes_c, side)."""
    lo = t - INTENSITY_LOOKBACK_S
    acc = 0.0
    for ts, _yp, _side in trades:
        if ts > t:
            break
        if ts < lo:
            continue
        acc += math.exp(-(t - ts) / LAMBDA_TAU_S)
    return acc


def decile_cutpoints(values: Sequence[float]) -> List[float]:
    """9 cut points partitioning into 10 equal-count buckets over the POOLED set of
    all (ticker,offset) lambda observations — a post-hoc stratification of the SAME
    corpus we score, exactly like mm_markout_evaluator's band stratification."""
    xs = sorted(values)
    n = len(xs)
    if n < 10:
        return []
    return [xs[int(d * n / 10.0)] for d in range(1, 10)]


def decile_of(x: float, cuts: Sequence[float]) -> int:
    """0..9 decile index for x given 9 cut points."""
    d = 0
    for c in cuts:
        if x >= c:
            d += 1
        else:
            break
    return d


# ---------------------------------------------------------------------------
# label book (terminal-derived outcome, independent of signal book)
# ---------------------------------------------------------------------------

def terminal_outcome(frames, close_epoch: float) -> Optional[str]:
    """Derive the 15M above/below outcome from the TERMINAL reliable book mid at
    close - LABEL_OFFSET_S. Returns 'yes' if yes-mid > 50 else 'no', or None if no
    reliable terminal book (we then DROP the window — never fabricate an outcome)."""
    bid, ask = kbr.reliable_nbbo_at(frames, close_epoch - LABEL_OFFSET_S)
    if bid is None or ask is None:
        return None
    return "yes" if yes_mid(bid, ask) > 50.0 else "no"


# ---------------------------------------------------------------------------
# clustered bootstrap (cluster = ticker, the true independent unit)
# ---------------------------------------------------------------------------

def _block_bootstrap_ci(
    clusters: Sequence[Sequence[float]], *, n_boot: int = N_BOOT,
    alpha: float = 0.05, seed: int = SEED,
) -> Tuple[float, float]:
    """Percentile CI for the grand mean, resampling whole TICKER clusters (block
    bootstrap) so serial correlation within a ticker/window doesn't deflate the CI.
    Each cluster is the list of per-fill markouts for one ticker."""
    clusters = [c for c in clusters if c]
    if not clusters:
        return (float("nan"), float("nan"))
    rng = random.Random(seed)
    k = len(clusters)
    means = []
    for _ in range(n_boot):
        tot = 0.0
        cnt = 0
        for _ in range(k):
            c = clusters[rng.randrange(k)]
            tot += sum(c)
            cnt += len(c)
        means.append(tot / cnt if cnt else 0.0)
    means.sort()
    lo = means[int((alpha / 2) * n_boot)]
    hi = means[min(n_boot - 1, int((1 - alpha / 2) * n_boot))]
    return (lo, hi)


# ---------------------------------------------------------------------------
# harness
# ---------------------------------------------------------------------------

def _new_cell():
    return {
        "posted": 0, "filled": 0,
        "settle_by_ticker": defaultdict(list),   # ticker -> [settlement markout net]
        "mk30": [],
        "spread": [],
    }


def evaluate(frames, trades) -> dict:
    # --- pass 1: collect lambda observations to fix decile cut points ----------
    obs = []  # (tk, asset, off, post_ts, lam, close)
    for tk, fr in frames.items():
        if tk not in trades:
            continue
        asset = _is_crypto_15m(tk)
        if not asset:
            continue
        close = close_epoch_from_ticker(tk)
        tr = trades[tk]
        for off in POST_OFFSETS_S:
            post_ts = close - off
            lam = lambda_at(tr, post_ts)
            obs.append((tk, asset, off, post_ts, lam, close))

    lam_values = [o[4] for o in obs]
    cuts = decile_cutpoints(lam_values)

    # --- pass 2: per-(asset,band,side,decile) AND pooled-(asset,band,side) -----
    cells = defaultdict(_new_cell)      # key = (asset, band, side, decile)
    pooled = defaultdict(_new_cell)     # key = (asset, band, side)

    n_windows_scored = 0
    n_no_label = 0
    n_no_signal_book = 0

    for tk, asset, off, post_ts, lam, close in obs:
        fr = frames[tk]
        tr = trades[tk]
        result = terminal_outcome(fr, close)
        if result is None:
            n_no_label += 1
            continue
        bid, ask = kbr.reliable_nbbo_at(fr, post_ts)
        if bid is None or ask is None:
            n_no_signal_book += 1
            continue
        n_windows_scored += 1
        ymid = yes_mid(bid, ask)
        no_bid = 100.0 - ask  # best NO bid
        dec = decile_of(lam, cuts)

        for side, price, fill_ts in (
            ("yes", bid, first_yes_bid_fill_ts(tr, post_ts, bid)),
            ("no", no_bid, first_no_bid_fill_ts(tr, post_ts, no_bid)),
        ):
            if not (0 < price < 100):
                continue
            band = _band(price if side == "yes" else 100 - price)
            for store, key in ((cells, (asset, band, side, dec)),
                               (pooled, (asset, band, side))):
                c = store[key]
                c["posted"] += 1
                if fill_ts is None:
                    continue
                c["filled"] += 1
                fee = kalshi_fee_per_contract_cents(price)
                smid_fill = side_mid(ymid, side)
                c["spread"].append(smid_fill - price)
                c["settle_by_ticker"][tk].append(
                    settlement_markout_cents(price, side, result) - fee)
                at = fill_ts + MARKOUT_HORIZON_S
                if at <= close:
                    b2, a2 = kbr.reliable_nbbo_at(fr, at)
                    if b2 is not None and a2 is not None:
                        smid_fut = side_mid(yes_mid(b2, a2), side)
                        c["mk30"].append(markout_cents(smid_fut, price) - fee)

    return {
        "cells": cells, "pooled": pooled, "cuts": cuts,
        "n_obs": len(obs), "n_windows_scored": n_windows_scored,
        "n_no_label": n_no_label, "n_no_signal_book": n_no_signal_book,
        "lam_values": lam_values,
    }


def _gate(cell, rebate: float = 0.0) -> dict:
    """Pre-registered gate. rebate (c/contract) ADDS to every markout. settle
    markouts stored per ticker -> block bootstrap clustered by ticker."""
    clusters = [[m + rebate for m in lst]
                for lst in cell["settle_by_ticker"].values()]
    flat = [m for c in clusters for m in c]
    n_fills = cell["filled"]
    lo, hi = _block_bootstrap_ci(clusters) if flat else (float("nan"), float("nan"))
    mk30 = [m + rebate for m in cell["mk30"]]
    mk30_mean = _mean(mk30) if mk30 else float("nan")
    fails = []
    if n_fills < MIN_FILLS_FLOOR:
        fails.append("n_fills")
    if not (flat and lo > 0):
        fails.append("settlement_ci_includes_zero")
    if not (mk30 and mk30_mean > 0):
        fails.append("negative_markout_30s")
    return {
        "survives": not fails, "fail_reasons": fails, "n_fills": n_fills,
        "settle_mean": _mean(flat) if flat else float("nan"),
        "settle_ci": (lo, hi), "mk30_mean": mk30_mean, "n_mk30": len(mk30),
    }


def main() -> dict:
    print("loading frames + trades ...", flush=True)
    frames = load_frames_jsonl(FRAMES_FILE)
    trades = load_trades_by_ticker(TRADES_FILE)
    print(f"frames tickers: {len(frames)}  trade tickers: {len(trades)}", flush=True)

    res = evaluate(frames, trades)
    cuts = res["cuts"]
    print(f"\nlambda observations: {res['n_obs']}  windows scored (reliable signal "
          f"book + terminal label): {res['n_windows_scored']}")
    print(f"dropped: {res['n_no_label']} no terminal label, "
          f"{res['n_no_signal_book']} no reliable signal book")
    if cuts:
        print("lambda decile cut points (D1..D9): "
              + ", ".join(f"{c:.3f}" for c in cuts))
        lv = res["lam_values"]
        print(f"lambda min/median/max: {min(lv):.3f} / "
              f"{sorted(lv)[len(lv)//2]:.3f} / {max(lv):.3f}")

    cells, pooled = res["cells"], res["pooled"]

    print("\n=== QUIET-decile (D0) MIDDLE-band (60-89c) cells vs their POOLED twin ===")
    print(f"{'asset':>6}{'side':>5}{'posted':>7}{'fills':>6}{'fill%':>7}"
          f"{'settleNet':>11}{'settleCI':>18}{'mk30':>8}{'n30':>5}{'VERDICT':>9}"
          f"  poolVerdict")

    edge_rows = []
    quiet_summary = []
    for key in sorted(cells):
        asset, band, side, dec = key
        if band != MIDDLE_BAND or dec != 0:
            continue
        c = cells[key]
        if c["posted"] == 0:
            continue
        g = _gate(c)
        pk = (asset, band, side)
        pg = _gate(pooled[pk]) if pk in pooled else None
        fillpct = 100 * c["filled"] / c["posted"]
        lo, hi = g["settle_ci"]
        ci = f"[{lo:+.2f},{hi:+.2f}]" if c["filled"] else "—"
        verdict = "SURVIVE" if g["survives"] else "kill"
        pv = ("SURVIVE" if pg and pg["survives"] else "kill") if pg else "—"
        print(f"{asset:>6}{side:>5}{c['posted']:>7}{c['filled']:>6}{fillpct:>7.1f}"
              f"{g['settle_mean']:>+11.2f}{ci:>18}{g['mk30_mean']:>+8.2f}"
              f"{g['n_mk30']:>5}{verdict:>9}  {pv}")
        quiet_summary.append((key, g, pg))
        if g["survives"] and (pg is None or not pg["survives"]):
            edge_rows.append((key, g, pg))

    print(f"\n=== rebate sensitivity (+{REBATE_SENSITIVITY_C}c maker rebate) quiet middle ===")
    print(f"{'asset':>6}{'side':>5}{'fills':>6}{'settleNet+reb':>14}{'CI':>18}{'VERDICT':>9}")
    rebate_edge = []
    for key in sorted(cells):
        asset, band, side, dec = key
        if band != MIDDLE_BAND or dec != 0:
            continue
        c = cells[key]
        if c["posted"] == 0:
            continue
        g = _gate(c, rebate=REBATE_SENSITIVITY_C)
        pk = (asset, band, side)
        pg = _gate(pooled[pk], rebate=REBATE_SENSITIVITY_C) if pk in pooled else None
        lo, hi = g["settle_ci"]
        ci = f"[{lo:+.2f},{hi:+.2f}]" if c["filled"] else "—"
        verdict = "SURVIVE" if g["survives"] else "kill"
        print(f"{asset:>6}{side:>5}{c['filled']:>6}{g['settle_mean']:>+14.2f}{ci:>18}"
              f"{verdict:>9}")
        if g["survives"] and (pg is None or not pg["survives"]):
            rebate_edge.append((key, g, pg))

    # headline: pooled-across-assets quiet-middle by side (keep ticker clustering)
    print("\n=== headline: pooled-across-assets quiet-middle by side ===")
    side_pool = defaultdict(_new_cell)
    for key, c in cells.items():
        asset, band, side, dec = key
        if band != MIDDLE_BAND or dec != 0:
            continue
        sp = side_pool[side]
        sp["posted"] += c["posted"]
        sp["filled"] += c["filled"]
        sp["spread"].extend(c["spread"])
        sp["mk30"].extend(c["mk30"])
        for tk, lst in c["settle_by_ticker"].items():
            sp["settle_by_ticker"][(asset, tk)].extend(lst)
    # also pooled-side across all deciles (intensity-unconditioned middle), for EDGE attribution
    pooled_side = defaultdict(_new_cell)
    for key, c in pooled.items():
        asset, band, side = key
        if band != MIDDLE_BAND:
            continue
        ps = pooled_side[side]
        ps["posted"] += c["posted"]
        ps["filled"] += c["filled"]
        ps["mk30"].extend(c["mk30"])
        for tk, lst in c["settle_by_ticker"].items():
            ps["settle_by_ticker"][(asset, tk)].extend(lst)

    headline = None
    for side in ("yes", "no"):
        if side not in side_pool:
            continue
        sp = side_pool[side]
        g = _gate(sp)
        pg = _gate(pooled_side[side]) if side in pooled_side else None
        lo, hi = g["settle_ci"]
        pv = ("SURVIVE" if pg and pg["survives"] else "kill") if pg else "—"
        print(f"  {side} QUIET: posted={sp['posted']} fills={sp['filled']} "
              f"settleNet={g['settle_mean']:+.2f}c CI=[{lo:+.2f},{hi:+.2f}] "
              f"mk30={g['mk30_mean']:+.2f}c n30={g['n_mk30']} "
              f"-> {'SURVIVE' if g['survives'] else 'kill'}")
        if pg:
            plo, phi = pg["settle_ci"]
            print(f"       POOLED(all deciles): fills={pg['n_fills']} "
                  f"settleNet={pg['settle_mean']:+.2f}c CI=[{plo:+.2f},{phi:+.2f}] "
                  f"-> {pv}")
        # headline = side with most quiet fills
        if headline is None or sp["filled"] > headline[1]["n_fills"]:
            headline = (side, g, pg)

    print()
    # EDGE attribution at the headline (pooled-across-assets) level too
    headline_edge = None
    if headline is not None:
        side, g, pg = headline
        if g["survives"] and (pg is None or not pg["survives"]):
            headline_edge = headline

    if edge_rows or headline_edge:
        print(f"🟢 mechanism-attributable: quiet-middle cleared the gate where the "
              f"intensity-unconditioned twin did NOT.")
        for key, g, pg in edge_rows:
            print(f"   cell {key}: settleNet={g['settle_mean']:+.2f}c "
                  f"CI={tuple(round(x,2) for x in g['settle_ci'])} "
                  f"mk30={g['mk30_mean']:+.2f}c n={g['n_fills']}")
        if headline_edge:
            s, g, pg = headline_edge
            print(f"   HEADLINE side={s}: settleNet={g['settle_mean']:+.2f}c "
                  f"CI={tuple(round(x,2) for x in g['settle_ci'])} n={g['n_fills']}")
    else:
        print("⚪ No quiet-middle cell (per-asset OR pooled headline) cleared the "
              "pre-registered gate as a mechanism-attributable edge "
              "(quiet-survives-where-pooled-fails).")

    return {
        "res": res, "quiet_summary": quiet_summary, "edge_rows": edge_rows,
        "rebate_edge": rebate_edge, "headline": headline,
        "headline_edge": headline_edge, "side_pool": side_pool,
        "pooled_side": pooled_side,
    }


if __name__ == "__main__":
    main()
