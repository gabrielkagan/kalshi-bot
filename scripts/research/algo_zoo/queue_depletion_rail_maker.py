"""queue_depletion_rail_maker — mid-life QUEUE-AHEAD-depletion rail maker.

Family: queue-position / rail microstructure. DISTINCT from terminal_rail_queue_rebate:
this is *mid-life* queue-AHEAD-depletion timing on the 1c / 99c rails, NOT terminal
rebate harvest at expiry.

THESIS (why a rail might survive where the 60-89 middle died):
  - Rail fees are near-zero: fee = ceil(0.07 * C * P * (1-P)); at P->{0.01, 0.99}
    the per-contract fee floor (large-order, fractional) is ~0.07c and the 1-ct
    rounding minimum is 1c. The MIDDLE that killed maker-mirage was 50-69c (fee
    peaks at ~1.75c) with 2.6% fill. The rail is the OPPOSITE fee regime.
  - A maker resting at the 99c rail on the FAVORITE side collects a 1c spread with
    ~99% settlement-win probability, IF it fills before close.

MECHANISM:
  - On the 1c and 99c rails, reconstruct full book depth over time (book_at gives
    per-level resting size). Track queue-ahead = resting size AT the rail level at
    the moment we'd post (T-OFFSET before close).
  - Decompose the depletion of that queue between post-time and close into
    TRADE-driven (real taker prints hitting the level, from TRADES count_fp at the
    rail price) vs CANCEL-driven (level-size deltas in FRAMES that are NOT explained
    by trades). Trade-driven depletion = benign (real takers consuming queue ahead,
    pulling our order toward the front). Cancel-driven = toxic (queue fleeing — the
    informed are leaving, our fill is adverse).
  - HONEST fill: a resting rail maker fills ONLY when a real trade print crosses it
    (mm_markout_evaluator.first_{yes,no}_bid_fill_ts). A fill we got is usually a
    fill we regret -> we settle it against the TERMINAL BOOK outcome.

LABEL (no DB look-ahead in-window): the 15M above/below outcome is derived from the
TERMINAL reconstructed book mid at/just before close_epoch_from_ticker(ticker).
result='yes' iff terminal yes-mid >= 50. Books that fail is_reliable at terminal are
dropped (can't honestly settle them).

HEADLINE: per-rail, per-(entry queue-depth quartile) bootstrap-CI of settlement
markout cents/ct NET OF FEES, clustered by ticker (the true independent unit).
Maker rebate = 0 by default; a sensitivity row at a hypothetical rebate is printed.

KILL: a (rail, side, depletion-regime, queue-quartile) cell SURVIVES only if its
clustered-bootstrap CI lower bound of net settlement markout > 0 AND n_fills >= floor.
A CI straddling zero = NO_EDGE. Negative net = NO_EDGE. Missing inputs = DATA_GAP.

Run (from repo root):
  cd /Users/gabrielkagan/Documents/kalshi-bot
  python3 scripts/research/algo_zoo/queue_depletion_rail_maker.py
"""
from __future__ import annotations

import json
import math
import random
import sys
from collections import defaultdict
from typing import Optional

sys.path.insert(0, "/Users/gabrielkagan/Documents/kalshi-bot")

from scripts.research import kalshi_book_reconstruct as kbr  # noqa: E402
from scripts.research.phase1b_real_price_economics import (  # noqa: E402
    _is_crypto_15m,
    close_epoch_from_ticker,
    load_frames_jsonl,
)
from scripts.research.phase1b_live_shadow import load_trades_by_ticker  # noqa: E402
from scripts.research.mm_markout_evaluator import (  # noqa: E402
    first_no_bid_fill_ts,
    first_yes_bid_fill_ts,
)
from scripts.research.settlement_convergence_p1a import (  # noqa: E402
    kalshi_fee_per_contract_cents,
)

FRAMES_FILE = "/tmp/edge_daily/frames_crypto.jsonl"
TRADES_FILE = "/tmp/edge_daily/trades_crypto.jsonl"

# Rails (the only levels this algo touches). Price in cents.
RAILS = (1, 99)
# When we'd post the resting maker, in seconds before close. Mid-life (not terminal).
POST_OFFSETS_S = (120, 60)
MIN_FILLS_FLOOR = 30          # per cell; below this is INCONCLUSIVE, never EDGE
N_BOOT = 2000                 # >= 1000 required
REBATE_SENSITIVITY_C = 0.25   # hypothetical maker rebate (cents/ct) for the sensitivity row
SUBSAMPLE_TICKERS = None      # set to int to subsample; None = use all reconstructable tickers


def _rail_resting_size(book: kbr.KalshiBook, side: str, price_cents: int) -> float:
    """Contracts resting at the rail level on `side` (yes/no), in the book's own
    side-units. tick = price_cents * 100 (the reconstruct module keys ticks of $1e-4)."""
    t = price_cents * 100
    d = book.yes if side == "yes" else book.no
    return float(d.get(t, 0.0))


def _trade_volume_at_rail(trades_raw, side: str, price_cents: int,
                          t0: float, t1: float) -> float:
    """Sum of trade-print contracts that consumed our rail level in (t0, t1].

    A maker resting a NO bid at no=price wants YES-BUYS at yes_price = 100-price to
    lift it; queue ahead is depleted by those same prints. A maker resting a YES bid
    at yes=price wants YES-SELLS (taker_side='no') at yes<=price. We attribute trade
    volume at the matching aggressor + price to queue depletion.
    trades_raw: list of (ts, yes_c, taker_side)."""
    vol = 0.0
    if side == "no":
        thresh = 100.0 - price_cents       # YES-BUY at >= thresh lifts our NO bid
        for ts, yp, tside, ct in trades_raw:
            if t0 < ts <= t1 and tside == "yes" and yp >= thresh:
                vol += ct
    else:  # yes-bid
        for ts, yp, tside, ct in trades_raw:
            if t0 < ts <= t1 and tside == "no" and yp <= price_cents:
                vol += ct
    return vol


def _load_trades_with_count(path: str) -> dict:
    """{ticker: [(ts, yes_c, taker_side, count)]} sorted — count needed for depletion."""
    from scripts.research.phase1b_retail_flow import parse_trade
    out = defaultdict(list)
    with open(path) as fh:
        for line in fh:
            if not line.strip():
                continue
            t = parse_trade(line)
            if t:
                out[t["ticker"]].append((t["ts"], t["yes_c"], t["taker_side"], t["count"]))
    for tk in out:
        out[tk].sort()
    return out


def _terminal_result(frames) -> Optional[str]:
    """Derive the 15M above/below outcome from the TERMINAL reconstructed book.
    Uses reliable_nbbo_at at the last applied frame epoch; refuses unreliable books.
    result='yes' iff terminal yes-mid >= 50."""
    if not frames:
        return None
    last_ts = frames[-1][0]
    bid, ask = kbr.reliable_nbbo_at(frames, last_ts)
    if bid is None or ask is None:
        return None
    ymid = (bid + ask) / 2.0
    # near-rail terminal books should be decisive; require a clear side
    if 49.0 <= ymid <= 51.0:
        return None  # ambiguous terminal mark -> can't honestly settle
    return "yes" if ymid >= 50.0 else "no"


def settlement_markout_cents(fill_price: float, side: str, result: str) -> float:
    """Markout to the binary outcome for the FILLED side: win -> 100-fill, lose -> -fill."""
    return (100.0 - fill_price) if side == result else -float(fill_price)


def _quartile_label(v: float, edges) -> str:
    """edges = (q1, q2, q3). Returns Q1..Q4 label by entry queue depth."""
    if v <= edges[0]:
        return "Q1"
    if v <= edges[1]:
        return "Q2"
    if v <= edges[2]:
        return "Q3"
    return "Q4"


def clustered_bootstrap_ci(samples, *, n_boot=N_BOOT, alpha=0.05, seed=12345):
    """Block/cluster bootstrap on the MEAN, clustered by ticker (the true independent
    unit — fills within one window are serially correlated). `samples` is a list of
    (cluster_id, value). Resamples CLUSTERS with replacement, pools their values.
    Returns (lo, hi, mean)."""
    if not samples:
        return (float("nan"), float("nan"), float("nan"))
    by_cluster = defaultdict(list)
    for cid, v in samples:
        by_cluster[cid].append(v)
    clusters = list(by_cluster.values())
    flat = [v for _, v in samples]
    mean = sum(flat) / len(flat)
    rng = random.Random(seed)
    n_cl = len(clusters)
    means = []
    for _ in range(n_boot):
        pool = []
        for _ in range(n_cl):
            pool.extend(clusters[rng.randrange(n_cl)])
        if pool:
            means.append(sum(pool) / len(pool))
    if not means:
        return (float("nan"), float("nan"), mean)
    means.sort()
    lo = means[int((alpha / 2) * len(means))]
    hi = means[min(len(means) - 1, int((1 - alpha / 2) * len(means)))]
    return (lo, hi, mean)


def main() -> int:
    print("loading frames (this is a 4.4GB file; one pass)...", flush=True)
    frames = load_frames_jsonl(FRAMES_FILE)
    print(f"  crypto-15M tickers in frames: {len(frames)}", flush=True)
    trades = _load_trades_with_count(TRADES_FILE)
    # honest-fill primitives want (ts, yes_c, taker_side)
    trades_fill = {tk: [(ts, yp, ts2) for (ts, yp, ts2, _) in v]
                   for tk, v in trades.items()}
    print(f"  tickers with trades: {len(trades)}", flush=True)

    tickers = sorted(frames)
    if SUBSAMPLE_TICKERS:
        rng = random.Random(7)
        tickers = sorted(rng.sample(tickers, min(SUBSAMPLE_TICKERS, len(tickers))))
        print(f"  subsampled to {len(tickers)} tickers", flush=True)

    # raw fill records: keyed (rail, side, regime) -> list of (ticker, queue_depth, net_markout)
    fills = defaultdict(list)
    # also keep depths for quartile edges per (rail, side)
    depths = defaultdict(list)
    posted_n = defaultdict(int)
    n_windows_settled = 0
    n_windows_dropped = 0

    # PASS 1: settle each window from terminal book, collect posted/fill records
    # store intermediate so we don't recompute books twice
    records = []  # (rail, side, queue_depth, fill_price, fill_ts, result, ticker, regime)
    for ti, tk in enumerate(tickers):
        fr = frames[tk]
        result = _terminal_result(fr)
        if result is None:
            n_windows_dropped += 1
            continue
        n_windows_settled += 1
        close = close_epoch_from_ticker(tk)
        tr_fill = trades_fill.get(tk, [])
        tr_cnt = trades.get(tk, [])

        for off in POST_OFFSETS_S:
            post_ts = close - off
            book, last_applied = kbr.book_at(fr, post_ts)
            if last_applied is None or not book.is_reliable():
                continue
            for rail in RAILS:
                for side in ("yes", "no"):
                    qdepth = _rail_resting_size(book, side, rail)
                    if qdepth <= 0:
                        continue  # no queue to rest behind -> we'd be alone / no level
                    posted_n[(rail, side)] += 1
                    depths[(rail, side)].append(qdepth)

                    # honest fill: resting our maker at the rail behind the queue
                    if side == "no":
                        fill_ts = first_no_bid_fill_ts(tr_fill, post_ts, float(rail))
                    else:
                        fill_ts = first_yes_bid_fill_ts(tr_fill, post_ts, float(rail))
                    if fill_ts is None or fill_ts > close:
                        continue  # never filled before close

                    # QUEUE-AHEAD GATE: did genuine takers consume enough queue to
                    # reach us? Trade volume at the rail between post and our fill
                    # must be >= queue ahead (we sit BEHIND the resting depth).
                    trade_vol = _trade_volume_at_rail(tr_cnt, side, rail, post_ts, fill_ts)
                    if trade_vol < qdepth:
                        # the print that crossed us did not clear the modeled queue
                        # ahead -> in reality our order would NOT yet be at the front.
                        # Honor the queue model: no fill.
                        continue

                    # depletion regime: of the total queue-ahead depletion between
                    # post and fill, how much was trade-driven vs cancel-driven?
                    # cancel-driven = (book size drop) - (trade volume). We approximate
                    # net level change via the book at fill_ts.
                    book_fill, _ = kbr.book_at(fr, fill_ts)
                    qdepth_fill = _rail_resting_size(book_fill, side, rail)
                    consumed = max(0.0, qdepth - qdepth_fill)
                    trade_share = (trade_vol / consumed) if consumed > 0 else 1.0
                    regime = "trade_driven" if trade_share >= 0.5 else "cancel_driven"

                    records.append((rail, side, qdepth, float(rail), fill_ts,
                                    result, tk, regime))
        if (ti + 1) % 100 == 0:
            print(f"  ...{ti+1}/{len(tickers)} tickers processed", flush=True)

    print(f"\nwindows settled from terminal book: {n_windows_settled}  "
          f"dropped (unreliable/ambiguous terminal): {n_windows_dropped}", flush=True)

    # quartile edges per (rail, side)
    edges = {}
    for key, ds in depths.items():
        ds_sorted = sorted(ds)
        n = len(ds_sorted)
        if n >= 4:
            edges[key] = (ds_sorted[n // 4], ds_sorted[n // 2], ds_sorted[3 * n // 4])
        else:
            edges[key] = (math.inf, math.inf, math.inf)

    # assemble cells: (rail, side, regime, quartile)
    cells = defaultdict(list)        # -> [(ticker, net_settle_markout)]
    cells_rebate = defaultdict(list)
    for rail, side, qdepth, fill_price, fill_ts, result, tk, regime in records:
        fee = kalshi_fee_per_contract_cents(fill_price)
        fee_1ct = max(1.0, math.ceil(fee))   # 1-contract rounding floor (honest worst case)
        gross = settlement_markout_cents(fill_price, side, result)
        net = gross - fee_1ct
        ql = _quartile_label(qdepth, edges[(rail, side)])
        key = (rail, side, regime, ql)
        cells[key].append((tk, net))
        cells_rebate[key].append((tk, net + REBATE_SENSITIVITY_C))

    # also a coarser cell: (rail, side, regime) pooled across quartiles
    cells_pooled = defaultdict(list)
    for (rail, side, regime, ql), recs in cells.items():
        cells_pooled[(rail, side, regime)].extend(recs)

    # ---- report ----
    print(f"\n{'rail':>5}{'side':>5}{'posted':>8}{'fills':>7}", flush=True)
    for key in sorted(posted_n):
        rail, side = key
        nf = sum(len(v) for (r, s, _, _), v in cells.items() if r == rail and s == side)
        print(f"{rail:>5}{side:>5}{posted_n[key]:>8}{nf:>7}", flush=True)

    print(f"\n=== POOLED cells (rail, side, regime) — net settlement markout c/ct ===")
    print(f"{'rail':>5}{'side':>6}{'regime':>14}{'nfill':>7}{'mean':>8}"
          f"{'ci_lo':>8}{'ci_hi':>8}{'VERDICT':>9}")
    pooled_rows = []
    for key in sorted(cells_pooled):
        rail, side, regime = key
        recs = cells_pooled[key]
        lo, hi, mean = clustered_bootstrap_ci(recs)
        n = len(recs)
        verdict = "SURVIVE" if (n >= MIN_FILLS_FLOOR and lo > 0) else "kill"
        pooled_rows.append((key, n, mean, lo, hi, verdict))
        print(f"{rail:>5}{side:>6}{regime:>14}{n:>7}{mean:>+8.2f}"
              f"{lo:>+8.2f}{hi:>+8.2f}{verdict:>9}", flush=True)

    print(f"\n=== QUARTILE cells (rail, side, regime, qDepth-quartile) ===")
    print(f"{'rail':>5}{'side':>6}{'regime':>14}{'Q':>4}{'nfill':>7}{'mean':>8}"
          f"{'ci_lo':>8}{'ci_hi':>8}{'VERDICT':>9}")
    survivors = []
    best = None  # (lo, dict) for headline
    for key in sorted(cells):
        rail, side, regime, ql = key
        recs = cells[key]
        n = len(recs)
        lo, hi, mean = clustered_bootstrap_ci(recs)
        survive = (n >= MIN_FILLS_FLOOR and lo > 0)
        verdict = "SURVIVE" if survive else "kill"
        print(f"{rail:>5}{side:>6}{regime:>14}{ql:>4}{n:>7}{mean:>+8.2f}"
              f"{lo:>+8.2f}{hi:>+8.2f}{verdict:>9}", flush=True)
        if survive:
            # rebate sensitivity
            rlo, rhi, rmean = clustered_bootstrap_ci(cells_rebate[key])
            survivors.append({
                "cell": f"rail{rail}_{side}_{regime}_{ql}",
                "n": n, "mean": mean, "ci": (lo, hi),
                "rebate_mean": rmean, "rebate_ci": (rlo, rhi),
            })
        # track best lower-bound among cells meeting the floor (headline candidate)
        if n >= MIN_FILLS_FLOOR and (best is None or lo > best[0]):
            best = (lo, {"cell": (rail, side, regime, ql), "n": n,
                         "mean": mean, "ci": (lo, hi)})

    print("\n=== REBATE SENSITIVITY (hypothetical +%.2fc maker rebate) ==="
          % REBATE_SENSITIVITY_C)
    if survivors:
        for s in survivors:
            print(f"  {s['cell']}: base mean={s['mean']:+.2f} CI={tuple(round(x,2) for x in s['ci'])}"
                  f" | +rebate mean={s['rebate_mean']:+.2f} CI={tuple(round(x,2) for x in s['rebate_ci'])}")
    else:
        print("  (no base survivors; rebate sensitivity moot for verdict)")

    # ---- headline / verdict assembly ----
    print("\n" + "=" * 60)
    if not records:
        print("VERDICT: DATA_GAP — no rail maker fills reconstructable.")
        return _emit("DATA_GAP", None, 0, "no fills")

    if survivors:
        # pick survivor with highest CI lower bound
        top = max(survivors, key=lambda s: s["ci"][0])
        print(f"VERDICT: EDGE — cell {top['cell']} clears fees, "
              f"CI lower bound {top['ci'][0]:+.2f}c > 0, n={top['n']}")
        return _emit("EDGE", top, top["n"], top["cell"])

    # no survivor. Report the best (highest-lower-bound) cell that met the floor.
    if best is not None:
        bl, bd = best
        print(f"VERDICT: NO_EDGE — best cell {bd['cell']} net mean {bd['mean']:+.2f}c "
              f"CI=({bd['ci'][0]:+.2f},{bd['ci'][1]:+.2f}); CI does not exclude zero "
              f"on the favorable side / is negative. Rails do not survive net of "
              f"fees+honest fills on this corpus.")
        return _emit("NO_EDGE", {"cell": str(bd['cell']), "n": bd["n"],
                                 "mean": bd["mean"], "ci": bd["ci"]}, bd["n"], str(bd["cell"]))

    print("VERDICT: INCONCLUSIVE — no cell met the min-fills floor "
          f"({MIN_FILLS_FLOOR}); ~1.3-day corpus too thin for the rails.")
    total_fills = sum(len(v) for v in cells.values())
    return _emit("INCONCLUSIVE", None, total_fills, "no cell met floor")


_RESULT = {}


def _emit(verdict, cell, n, label):
    _RESULT.update({"verdict": verdict, "cell": cell, "n": n, "label": label})
    return 0


if __name__ == "__main__":
    rc = main()
    print("\nRESULT_JSON:", json.dumps(_RESULT, default=str))
    raise SystemExit(rc)
