"""taker_flow_toxicity_skew_quoting  (family: informed-flow-gated market making)

Distinct from glosten_milgrom_vpin: that idea GATES/FADES on toxicity. This RIDES
persistent taker pressure as a momentum-of-flow signal — quote a maker bid ONLY on
the side recent taker flow is leaning INTO.

MECHANISM
  For each crypto-15M ticker, stream kalshi trade prints. Maintain a rolling 60s
  signed taker-volume imbalance over PRINT COUNTS:
        I = (yes_taker_ct - no_taker_ct) / total_ct   (window = last 60s)
  At a decision time T late in the window:
    - if I > +thr: post a YES maker bid at (yes_bid_nbbo - 1) tick.
    - if I < -thr: post a NO  maker bid at (no_bid_nbbo  - 1) tick.
    - else: no quote.
  Rationale: follow the informed taker rather than fade it; the move continues in
  our favor so we capture spread WITHOUT adverse selection.

HONEST FILL
  Resting maker order fills ONLY when a real later trade print crosses it
  (mm_markout_evaluator.first_yes_bid_fill_ts / first_no_bid_fill_ts). No fill =
  no trade. A fill is modeled, then marked to settlement.

LABEL (no DB; in-window outcomes sparse)
  Terminal outcome derived from the TERMINAL BOOK: reliable NBBO mid at
  close_epoch_from_ticker. mid > 50 -> YES settles, else NO settles. Books that
  reliable_nbbo_at REFUSES (drift) are dropped — no trade off a rejected book.

METRIC
  Settlement markout in cents/contract NET of kalshi fees on BOTH the fill price
  and the settlement leg (entry + exit fee), maker rebate = 0. Bootstrap CI
  (>=2000 resamples) CLUSTERED by ticker-window (the independent unit). Also a
  30s-markout (picked-off check). Pre-registered kill: cell survives only if
  CI lower bound > 0 net of fees AND mean 30s-markout > 0.

NO LOOK-AHEAD: signal + NBBO computed from data with ts <= T only; fills from
prints strictly after T; label from the terminal book at close.
"""
from __future__ import annotations

import math
import os
import random
import sys
from collections import defaultdict

sys.path.insert(0, "/Users/gabrielkagan/Documents/kalshi-bot")

import json
from collections import defaultdict as _dd

from scripts.research import kalshi_book_reconstruct as kbr
from scripts.research.phase1b_real_price_economics import (
    close_epoch_from_ticker,
)
from scripts.research.phase1b_retail_flow import parse_trade


def _epoch(ts_str):
    """ISO8601 Z -> epoch seconds (mirror of phase1b helper)."""
    from datetime import datetime, timezone
    return datetime.fromisoformat(ts_str.replace("Z", "+00:00")).timestamp()


def load_frames_jsonl_subset(path, keep_tickers):
    """Memory-frugal: stream the 4.4GB frames file once, keep frames ONLY for
    tickers in keep_tickers. Returns ticker -> sorted [(recv_epoch, inner)]."""
    frames = _dd(list)
    keep = set(keep_tickers)
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
            if tk in keep:
                frames[tk].append((_epoch(env["_wire_recv_ts"]), inner))
    for tk in frames:
        frames[tk].sort(key=lambda x: x[0])
    return dict(frames)
from scripts.research.mm_markout_evaluator import (
    first_no_bid_fill_ts,
    first_yes_bid_fill_ts,
)

FRAMES = "/tmp/edge_daily/frames_crypto.jsonl"
TRADES = "/tmp/edge_daily/trades_crypto.jsonl"

# --- strategy params ---
WINDOW_S = 60.0          # rolling taker-imbalance window
THR = float(os.environ.get("THR", "0.30"))  # |I| threshold to quote
MIN_CT = float(os.environ.get("MIN_CT", "3"))  # min prints in window to act
# Sweep decision points across the late window: from EARLY_LEAD_S down to
# LATE_LEAD_S before close, every STEP_S seconds. Each point is a separate quote
# opportunity; bootstrap CLUSTERS by ticker so serial correlation within a window
# is respected (the ticker-window is the true independent unit).
EARLY_LEAD_S = 600.0     # start quoting up to 10 min before close
LATE_LEAD_S = 60.0       # stop quoting 60s before close (need fill room)
STEP_S = 60.0            # decision-point cadence
MARKOUT_30S = 30.0       # picked-off check horizon
MAX_TICKERS = int(os.environ.get("MAX_TICKERS", "200"))  # subsample for RAM/speed


def kalshi_fee_cents(price_cents: float) -> float:
    """ceil(0.07 * C * P * (1-P)) cents/contract, C=1, P in [0,1]."""
    p = max(0.0, min(1.0, price_cents / 100.0))
    return math.ceil(0.07 * 1.0 * p * (1.0 - p) * 100.0) / 1.0  # cents


def _fee(price_cents: float) -> float:
    p = max(0.0, min(1.0, price_cents / 100.0))
    return math.ceil(0.07 * p * (1.0 - p) * 100.0)


def price_band(yes_mid: float) -> str:
    if yes_mid < 10 or yes_mid > 90:
        return "rails"
    if 60 <= yes_mid <= 89:
        return "high60_89"
    return "mid"


def load_trades_grouped():
    """ticker -> list of (event_ts, yes_c, taker_side) sorted by event ts."""
    by_tk = defaultdict(list)
    with open(TRADES) as fh:
        for line in fh:
            if not line.strip():
                continue
            t = parse_trade(line)
            if not t:
                continue
            by_tk[t["ticker"]].append((t["ts"], t["yes_c"], t["taker_side"], t["count"]))
    for tk in by_tk:
        by_tk[tk].sort(key=lambda x: x[0])
    return by_tk


def rolling_imbalance(trades, T):
    """Signed taker count-imbalance over (T-WINDOW_S, T], counts only ts<=T."""
    yes_ct = no_ct = 0.0
    for ts, yp, side, cnt in trades:
        if ts > T:
            break
        if ts <= T - WINDOW_S:
            continue
        if side == "yes":
            yes_ct += 1.0
        else:
            no_ct += 1.0
    total = yes_ct + no_ct
    if total < MIN_CT:
        return None, total
    return (yes_ct - no_ct) / total, total


def terminal_outcome(frames_tk, close_epoch):
    """YES/NO outcome from the LAST reliable book mid at/just before close.
    reliable_nbbo_at refuses drifted books, and the exact-close moment is often
    unreliable, so walk back in 15s steps up to 180s looking for a reliable book.
    The terminal book mid (near 0 or near 100 at expiry) encodes the settled
    direction. Returns None if no reliable book exists in the final window OR the
    mid is too ambiguous (40-60) to call a settlement."""
    for back in (0, 15, 30, 45, 60, 90, 120, 150, 180):
        at = close_epoch - back
        bid, ask = kbr.reliable_nbbo_at(frames_tk, at)
        if bid is None or ask is None:
            continue
        mid = (bid + ask) / 2.0
        # require the terminal book to have RESOLVED toward a rail; a 40-60 mid
        # this close to expiry is a true coin-flip and an unreliable label.
        if back <= 60 and (mid <= 35.0 or mid >= 65.0):
            return "yes" if mid > 50.0 else "no"
        # further from close, demand a stronger rail to trust the early read
        if back > 60 and (mid <= 20.0 or mid >= 80.0):
            return "yes" if mid > 50.0 else "no"
    return None


def evaluate():
    print("loading trades...", flush=True)
    trades_by_tk = load_trades_grouped()
    print(f"  {len(trades_by_tk)} tickers with trades", flush=True)

    # Subsample to the most-traded tickers (the independent units with the most
    # flow signal). This bounds frame memory: loading ALL 626 tickers' frames
    # thrashed the box (state=U swap). Pick top-N by trade count.
    ranked = sorted(trades_by_tk, key=lambda tk: len(trades_by_tk[tk]), reverse=True)
    keep = set(ranked[:MAX_TICKERS])
    print(f"  subsampling to top {len(keep)} tickers by trade count "
          f"(MAX_TICKERS={MAX_TICKERS})", flush=True)

    print("loading frames (subset)...", flush=True)
    frames = load_frames_jsonl_subset(FRAMES, keep)
    print(f"  {len(frames)} tickers loaded with frames", flush=True)

    # results: list of dicts; cluster key = ticker (one decision per ticker-window)
    fills = []          # filled rows
    n_signals = 0       # times we'd have quoted (had a signal + reliable NBBO)
    n_no_signal = 0
    n_nbbo_refused = 0
    n_label_refused = 0

    n_quoted = 0          # quotes posted (signal fired AND book reliable)
    abs_I_seen = []       # diagnostic: |I| at every point where window had MIN_CT prints

    tickers = sorted(set(frames) & keep)
    for tk in tickers:
        f = frames[tk]
        tr = trades_by_tk[tk]
        tr3 = [(ts, yp, s) for ts, yp, s, _ in tr]  # fill-primitive tuple form
        try:
            close_epoch = close_epoch_from_ticker(tk)
        except Exception:
            continue
        if not tr:
            continue

        # terminal label once per ticker (shared across all decision points)
        result = terminal_outcome(f, close_epoch)
        if result is None:
            n_label_refused += 1
            continue

        # sweep decision points from EARLY_LEAD_S -> LATE_LEAD_S before close
        lead = EARLY_LEAD_S
        while lead >= LATE_LEAD_S:
            T = close_epoch - lead
            lead -= STEP_S
            if tr[0][0] > T:
                continue  # no trade history yet at this point

            I, total = rolling_imbalance(tr, T)
            if I is None:
                continue
            abs_I_seen.append(abs(I))
            if abs(I) < THR:
                n_no_signal += 1
                continue

            ybid, yask = kbr.reliable_nbbo_at(f, T)
            if ybid is None or yask is None:
                n_nbbo_refused += 1
                continue
            ymid = (ybid + yask) / 2.0
            n_quoted += 1

            if I > 0:
                side = "yes"
                bid_cents = ybid - 1.0
                if bid_cents < 1:
                    continue
                fill_ts = first_yes_bid_fill_ts(tr3, T, bid_cents)
                fill_price = bid_cents
            else:
                side = "no"
                no_bid_cents = (100.0 - yask) - 1.0
                if no_bid_cents < 1:
                    continue
                fill_ts = first_no_bid_fill_ts(tr3, T, no_bid_cents)
                fill_price = no_bid_cents

            if fill_ts is None:
                continue  # unfilled -> no position
            if fill_ts >= close_epoch:
                continue

            entry_fee = _fee(fill_price)
            if side == result:
                gross = 100.0 - fill_price
            else:
                gross = -fill_price
            # settlement leg fee: P=0 or 1 -> fee 0; so only entry fee bites
            net = gross - entry_fee

            m30 = None
            fb, fa = kbr.reliable_nbbo_at(f, fill_ts + MARKOUT_30S)
            if fb is not None and fa is not None:
                ym30 = (fb + fa) / 2.0
                sidemid30 = ym30 if side == "yes" else (100.0 - ym30)
                m30 = sidemid30 - fill_price

            fills.append({
                "ticker": tk,
                "side": side,
                "fill_price": fill_price,
                "net": net,
                "gross": gross,
                "result": result,
                "band": price_band(ymid),
                "I": I,
                "m30": m30,
            })

    n_signals = n_quoted
    if abs_I_seen:
        abs_I_seen.sort()
        med = abs_I_seen[len(abs_I_seen) // 2]
        p90 = abs_I_seen[int(0.9 * len(abs_I_seen))]
        print(f"  |I| distribution over {len(abs_I_seen)} decision points: "
              f"median={med:.2f} p90={p90:.2f} "
              f"frac>={THR}: {sum(1 for x in abs_I_seen if x>=THR)/len(abs_I_seen):.1%}",
              flush=True)

    return {
        "fills": fills,
        "n_signals": n_signals,
        "n_no_signal": n_no_signal,
        "n_nbbo_refused": n_nbbo_refused,
        "n_label_refused": n_label_refused,
    }


def cluster_bootstrap_ci(rows, key="net", cluster="ticker", n_boot=2000, alpha=0.05, seed=7):
    """Clustered (block) bootstrap: resample CLUSTERS (ticker-windows) with
    replacement, take the mean of all rows in the drawn clusters."""
    if not rows:
        return (float("nan"), float("nan"), float("nan"))
    by_c = defaultdict(list)
    for r in rows:
        by_c[r[cluster]].append(r[key])
    clusters = list(by_c.values())
    rng = random.Random(seed)
    nC = len(clusters)
    point = sum(v for c in clusters for v in c) / sum(len(c) for c in clusters)
    means = []
    for _ in range(n_boot):
        s = 0.0
        n = 0
        for _ in range(nC):
            c = clusters[rng.randrange(nC)]
            s += sum(c)
            n += len(c)
        means.append(s / n if n else 0.0)
    means.sort()
    lo = means[int((alpha / 2) * n_boot)]
    hi = means[min(n_boot - 1, int((1 - alpha / 2) * n_boot))]
    return (point, lo, hi)


def main():
    res = evaluate()
    fills = res["fills"]
    print("\n=== FUNNEL ===")
    print(f"signals (quoted, reliable NBBO + label): {res['n_signals']}")
    print(f"no-signal (|I|<thr or thin):             {res['n_no_signal']}")
    print(f"NBBO refused (drift):                    {res['n_nbbo_refused']}")
    print(f"label refused (terminal book drift):     {res['n_label_refused']}")
    print(f"FILLED:                                  {len(fills)}")
    if res["n_signals"]:
        print(f"fill rate: {len(fills)/res['n_signals']:.1%}")

    if not fills:
        print("\nNO FILLS -> nothing to evaluate.")
        return res, None

    nets = [r["net"] for r in fills]
    point, lo, hi = cluster_bootstrap_ci(fills, "net")
    mean_net = sum(nets) / len(nets)
    m30s = [r["m30"] for r in fills if r["m30"] is not None]
    mean_m30 = sum(m30s) / len(m30s) if m30s else float("nan")

    print("\n=== OVERALL (net cents/contract, fees on both legs, rebate=0) ===")
    print(f"n_fills={len(fills)}  n_clusters={len(set(r['ticker'] for r in fills))}")
    print(f"mean net markout = {mean_net:+.3f}c   95% CI [{lo:+.3f}, {hi:+.3f}]")
    print(f"mean 30s markout = {mean_m30:+.3f}c  (n={len(m30s)})")
    win = sum(1 for r in fills if r["net"] > 0) / len(fills)
    print(f"win rate (net>0) = {win:.1%}")

    print("\n=== BY PRICE BAND ===")
    by_band = defaultdict(list)
    for r in fills:
        by_band[r["band"]].append(r)
    band_results = {}
    for band, rows in sorted(by_band.items()):
        p, blo, bhi = cluster_bootstrap_ci(rows, "net")
        bm30 = [r["m30"] for r in rows if r["m30"] is not None]
        bmean_m30 = sum(bm30) / len(bm30) if bm30 else float("nan")
        band_results[band] = (p, blo, bhi, len(rows), bmean_m30)
        print(f"  {band:10s} n={len(rows):4d} clu={len(set(r['ticker'] for r in rows)):3d} "
              f"net={p:+.3f} CI[{blo:+.3f},{bhi:+.3f}] m30={bmean_m30:+.3f}")

    print("\n=== KILL GATE (CI_lo>0 net AND mean m30>0) ===")
    overall_pass = lo > 0 and (not math.isnan(mean_m30)) and mean_m30 > 0
    print(f"  OVERALL: {'SURVIVE' if overall_pass else 'KILL'}")
    for band, (p, blo, bhi, n, bm30) in band_results.items():
        bp = blo > 0 and (not math.isnan(bm30)) and bm30 > 0
        print(f"  {band}: {'SURVIVE' if bp else 'KILL'}")

    return res, {
        "point": point, "lo": lo, "hi": hi, "n": len(fills),
        "n_clusters": len(set(r["ticker"] for r in fills)),
        "mean_m30": mean_m30, "fill_rate": len(fills) / res["n_signals"] if res["n_signals"] else 0.0,
        "pass": overall_pass,
    }


if __name__ == "__main__":
    main()
