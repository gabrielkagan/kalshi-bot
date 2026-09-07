"""trade_arrival_intensity_settlement_clock — point-process / settlement-clock MM.

FAMILY: point-process / settlement-second timing.
  DISTINCT from hawkes_trade_burst_fade (which FADED bursts directionally and
  failed). This algo TIMES MAKER ENTRY by the trade-arrival-intensity clock — a
  liquidity clock, not a fade.

HYPOTHESIS (the untested OPPOSITE of the failed fade):
  As a 15M market nears close, trade-print arrival intensity lambda(t) typically
  RISES (settlement attention). The conjecture: in the HIGH-intensity final window
  the inbound taker flow is two-sided LIQUIDITY/NOISE churn (retail settling
  positions), so a resting maker gets filled by UNINFORMED flow rather than picked
  off — the classic "provide liquidity when noise demands it" edge. In the
  LOW-intensity mid-life, the only taker to hit your resting quote is INFORMED ->
  adverse selection. So: condition maker entry on lambda(t)-quartile x
  seconds_to_close and test whether any cell captures spread net of fees+fills.

MECHANICS (all reused from the tested reconstruction; NO hand-rolled book parse):
  - lambda(t): count of real trade prints in a [post_ts - LAMBDA_WIN, post_ts]
    window, /sec. NO look-ahead (window ends at decision time). Quartile-binned
    across all observations -> Q1(low)..Q4(high).
  - Quote book: reliable_nbbo_at(post_ts) — snapshot-anchored, refuses drifted
    books. Post a resting YES bid at best_yes_bid and a resting NO bid at
    100 - best_yes_ask (NBBO-1 maker; we sit AT the inside, the honest "best maker"
    upper bound).
  - HONEST fill: order fills ONLY when a real print crosses it
    (first_yes_bid_fill_ts / first_no_bid_fill_ts from mm_markout_evaluator).
  - Settlement: terminal reliable book mid at close (>50 -> YES wins). DB outcomes
    are sparse in-window, so we LABEL from the terminal book (built independently
    of the signal book — different cutoff, no look-ahead into the decision).
  - Fees: kalshi_fee_per_contract_cents on the entry fill (settlement leg is free
    on Kalshi binaries). Maker rebate assumed 0.
  - 30s markout: reliable side-mid at fill+30s minus fill price, net of fee.

KILL (pre-registered): a (quartile x stc) cell SURVIVES iff, net of fees:
  (1) n_fills >= MIN_FILLS, AND
  (2) bootstrap-CI (ticker-clustered, >=1000 resamples) lower bound of settlement
      markout > 0, AND
  (3) mean 30s-markout > 0 (not getting picked off).
  Fill rate reported so a high-intensity-but-unfilled cell -> NO_EDGE, not EDGE.

Usage:
  cd /Users/gabrielkagan/Documents/kalshi-bot
  python3 scripts/research/algo_zoo/trade_arrival_intensity_settlement_clock.py \
      --frames-file /tmp/edge_daily/frames_crypto.jsonl \
      --trades-file /tmp/edge_daily/trades_crypto.jsonl
"""
from __future__ import annotations

import argparse
import random
import sys
from collections import defaultdict
from typing import Dict, List, Optional, Sequence, Tuple

sys.path.insert(0, "/Users/gabrielkagan/Documents/kalshi-bot")

from scripts.research import kalshi_book_reconstruct as kbr  # noqa: E402
from scripts.research.phase1b_real_price_economics import (  # noqa: E402
    _is_crypto_15m,
    close_epoch_from_ticker,
    load_frames_jsonl,
)
from scripts.research.phase1b_live_shadow import load_trades_by_ticker  # noqa: E402
from scripts.research.settlement_convergence_p1a import (  # noqa: E402
    kalshi_fee_per_contract_cents,
)
from scripts.research.mm_markout_evaluator import (  # noqa: E402
    first_no_bid_fill_ts,
    first_yes_bid_fill_ts,
    side_mid,
    yes_mid,
)

# --- knobs -----------------------------------------------------------------
POST_OFFSETS_S = (60, 120)        # seconds_to_close at maker entry (the "clock")
LAMBDA_WIN_S = 60.0               # lookback for the arrival-intensity estimate
MARKOUT_H_S = 30                  # post-fill markout horizon
MIN_FILLS = 30                    # per-cell floor; below -> insufficient, never EDGE
N_BOOT = 2000
MAKER_REBATE_CENTS = 0.0          # stated assumption: no maker rebate


def settlement_markout_cents(fill_price: float, side: str, won_side: str) -> float:
    """Markout to the binary outcome: side wins -> 100 - fill; loses -> -fill."""
    return (100.0 - fill_price) if side == won_side else -float(fill_price)


def _mean(xs: Sequence[float]) -> float:
    xs = list(xs)
    return sum(xs) / len(xs) if xs else float("nan")


def cluster_bootstrap_ci(
    clusters: Sequence[Sequence[float]], *, n_boot: int = N_BOOT,
    alpha: float = 0.05, seed: int = 12345,
) -> Tuple[float, float]:
    """Percentile bootstrap of the GRAND MEAN, resampling at the CLUSTER
    (ticker-window) level — the true independent unit. Each resample draws
    len(clusters) clusters with replacement, pools their observations, takes the
    mean. Deterministic (fixed seed)."""
    clusters = [list(c) for c in clusters if c]
    if not clusters:
        return (float("nan"), float("nan"))
    rng = random.Random(seed)
    n = len(clusters)
    means: List[float] = []
    for _ in range(n_boot):
        s = 0.0
        cnt = 0
        for _ in range(n):
            c = clusters[rng.randrange(n)]
            for v in c:
                s += v
                cnt += 1
        if cnt:
            means.append(s / cnt)
    if not means:
        return (float("nan"), float("nan"))
    means.sort()
    lo = means[int((alpha / 2) * len(means))]
    hi = means[min(len(means) - 1, int((1 - alpha / 2) * len(means)))]
    return (lo, hi)


def terminal_outcome(frames: Sequence, close_epoch: float) -> Optional[str]:
    """Label the 15M above/below outcome from the TERMINAL reliable book mid at
    close. Built from a cutoff (close) strictly LATER than every decision time, so
    it never leaks into the signal/quote book. Refuses a drifted terminal book."""
    bid, ask = kbr.reliable_nbbo_at(frames, close_epoch)
    if bid is None or ask is None:
        return None
    m = yes_mid(bid, ask)
    if abs(m - 50.0) < 1e-9:
        return None  # genuine toss-up at close -> unlabelable, drop
    return "yes" if m > 50.0 else "no"


def lambda_per_sec(trades: Sequence[Tuple[float, float, str]],
                   post_ts: float) -> Optional[float]:
    """Arrival intensity (prints/sec) over [post_ts - LAMBDA_WIN_S, post_ts].
    NO look-ahead: only prints with ts <= post_ts counted. None if no prints in
    the window at all (can't estimate intensity)."""
    lo = post_ts - LAMBDA_WIN_S
    cnt = sum(1 for ts, _yp, _sd in trades if lo <= ts <= post_ts)
    if cnt == 0:
        return None
    return cnt / LAMBDA_WIN_S


def _quartile_edges(values: Sequence[float]) -> Tuple[float, float, float]:
    xs = sorted(values)
    n = len(xs)
    if n < 4:
        return (float("inf"), float("inf"), float("inf"))
    return (xs[n // 4], xs[n // 2], xs[(3 * n) // 4])


def _qbin(v: float, edges: Tuple[float, float, float]) -> int:
    q1, q2, q3 = edges
    if v <= q1:
        return 1
    if v <= q2:
        return 2
    if v <= q3:
        return 3
    return 4


def evaluate(frames: Dict[str, list], trades: Dict[str, list]) -> dict:
    # ---- Pass 1: gather per-window observations (no binning yet) ----------
    # obs: list of dicts with everything needed; quartile assigned in pass 2 once
    # we know the lambda distribution across the whole corpus.
    obs: List[dict] = []
    n_windows = n_labeled = 0
    skip_no_trades = skip_no_label = skip_no_book = skip_no_lambda = 0

    for tk, fr in frames.items():
        if _is_crypto_15m(tk) is None:
            continue
        tr = trades.get(tk)
        if not tr:
            skip_no_trades += 1
            continue
        n_windows += 1
        close = close_epoch_from_ticker(tk)
        won = terminal_outcome(fr, close)
        if won is None:
            skip_no_label += 1
            continue
        n_labeled += 1
        for off in POST_OFFSETS_S:
            post_ts = close - off
            lam = lambda_per_sec(tr, post_ts)
            if lam is None:
                skip_no_lambda += 1
                continue
            bid, ask = kbr.reliable_nbbo_at(fr, post_ts)
            if bid is None or ask is None:
                skip_no_book += 1
                continue
            ymid = yes_mid(bid, ask)
            no_bid = 100.0 - ask  # best NO bid = where a NO maker rests
            # post BOTH legs (yes-maker at bid, no-maker at no_bid)
            for side, price, fill_ts in (
                ("yes", bid, first_yes_bid_fill_ts(tr, post_ts, bid)),
                ("no", no_bid, first_no_bid_fill_ts(tr, post_ts, no_bid)),
            ):
                if not (0 < price < 100):
                    continue
                rec = {
                    "tk": tk, "off": off, "side": side, "lam": lam,
                    "price": price, "posted": True, "filled": False,
                    "settle_net": None, "mk30_net": None,
                }
                if fill_ts is not None:
                    rec["filled"] = True
                    fee = kalshi_fee_per_contract_cents(price) - MAKER_REBATE_CENTS
                    rec["settle_net"] = (
                        settlement_markout_cents(price, side, won) - fee
                    )
                    at = fill_ts + MARKOUT_H_S
                    if at <= close:
                        b2, a2 = kbr.reliable_nbbo_at(fr, at)
                        if b2 is not None and a2 is not None:
                            smid_fut = side_mid(yes_mid(b2, a2), side)
                            rec["mk30_net"] = (smid_fut - price) - fee
                obs.append(rec)

    # ---- Pass 2: quartile-bin lambda (per offset, so the clock is fair) ----
    # Quartiles computed PER offset because intensity scale differs by stc.
    edges_by_off: Dict[int, Tuple[float, float, float]] = {}
    for off in POST_OFFSETS_S:
        lams = [r["lam"] for r in obs if r["off"] == off]
        edges_by_off[off] = _quartile_edges(lams)

    # ---- Pass 3: aggregate into (quartile x stc) cells --------------------
    cells: Dict[Tuple[int, int, str], dict] = defaultdict(lambda: {
        "posted": 0, "filled": 0,
        "settle_by_tk": defaultdict(list),  # ticker-clustered
        "mk30": [],
    })
    for r in obs:
        q = _qbin(r["lam"], edges_by_off[r["off"]])
        key = (q, r["off"], r["side"])
        c = cells[key]
        c["posted"] += 1
        if not r["filled"]:
            continue
        c["filled"] += 1
        if r["settle_net"] is not None:
            c["settle_by_tk"][r["tk"]].append(r["settle_net"])
        if r["mk30_net"] is not None:
            c["mk30"].append(r["mk30_net"])

    # ---- gate ----
    results = []
    survivors = []
    for key in sorted(cells):
        q, off, side = key
        c = cells[key]
        clusters = list(c["settle_by_tk"].values())
        flat = [v for cl in clusters for v in cl]
        n_fills = len(flat)
        n_tickers = len(clusters)
        settle_mean = _mean(flat) if flat else float("nan")
        lo, hi = cluster_bootstrap_ci(clusters) if clusters else (float("nan"), float("nan"))
        mk30_mean = _mean(c["mk30"]) if c["mk30"] else float("nan")
        fills_ok = n_fills >= MIN_FILLS
        ci_ok = bool(flat) and lo > 0
        mk_ok = bool(c["mk30"]) and mk30_mean > 0
        survives = fills_ok and ci_ok and mk_ok
        fails = []
        if not fills_ok:
            fails.append(f"n_fills<{MIN_FILLS}")
        if not ci_ok:
            fails.append("settle_ci<=0")
        if not mk_ok:
            fails.append("mk30<=0")
        rec = {
            "cell": (f"Q{q}", f"stc{off}", side),
            "posted": c["posted"], "filled": c["filled"],
            "fill_pct": (100.0 * c["filled"] / c["posted"]) if c["posted"] else 0.0,
            "n_fills": n_fills, "n_tickers": n_tickers,
            "settle_mean_net": settle_mean, "settle_ci": (lo, hi),
            "mk30_mean_net": mk30_mean, "survives": survives, "fails": fails,
        }
        results.append(rec)
        if survives:
            survivors.append(rec)

    return {
        "results": results, "survivors": survivors,
        "diag": {
            "n_windows": n_windows, "n_labeled": n_labeled,
            "skip_no_trades": skip_no_trades, "skip_no_label": skip_no_label,
            "skip_no_book": skip_no_book, "skip_no_lambda": skip_no_lambda,
            "edges_by_off": {k: v for k, v in edges_by_off.items()},
        },
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--frames-file", required=True)
    ap.add_argument("--trades-file", required=True)
    ap.add_argument("--max-tickers", type=int, default=0,
                    help="subsample first N tickers (0=all)")
    args = ap.parse_args(argv)

    print("loading frames...", flush=True)
    frames = load_frames_jsonl(args.frames_file)
    print(f"  crypto-15M tickers in frames: {len(frames)}", flush=True)
    print("loading trades...", flush=True)
    trades = load_trades_by_ticker(args.trades_file)
    print(f"  tickers with trades: {len(trades)}", flush=True)

    if args.max_tickers and len(frames) > args.max_tickers:
        keep = sorted(frames)[: args.max_tickers]
        frames = {k: frames[k] for k in keep}
        print(f"  SUBSAMPLED to {len(frames)} tickers", flush=True)

    res = evaluate(frames, trades)
    d = res["diag"]
    print(f"\nwindows(with trades)={d['n_windows']} labeled(terminal book)={d['n_labeled']} "
          f"| skips: no_trades={d['skip_no_trades']} no_label={d['skip_no_label']} "
          f"no_book={d['skip_no_book']} no_lambda={d['skip_no_lambda']}")
    print("lambda quartile edges (prints/sec) by stc-offset:")
    for off, e in d["edges_by_off"].items():
        print(f"  stc{off}: Q1<={e[0]:.4f}  Q2<={e[1]:.4f}  Q3<={e[2]:.4f}")

    print(f"\n{'cell':>22}{'posted':>8}{'fill%':>7}{'fills':>6}{'tk':>4}"
          f"{'settleNet':>11}{'settleCI':>18}{'mk30':>8}{'VERDICT':>9}")
    for r in res["results"]:
        if not r["posted"]:
            continue
        lo, hi = r["settle_ci"]
        ci = f"[{lo:+.2f},{hi:+.2f}]" if r["n_fills"] else "—"
        sm = f"{r['settle_mean_net']:+.2f}" if r["n_fills"] else "—"
        mk = f"{r['mk30_mean_net']:+.2f}" if r["n_fills"] else "—"
        v = "SURVIVE" if r["survives"] else "kill"
        cell = "/".join(r["cell"])
        print(f"{cell:>22}{r['posted']:>8}{r['fill_pct']:>7.1f}{r['n_fills']:>6}"
              f"{r['n_tickers']:>4}{sm:>11}{ci:>18}{mk:>8}{v:>9}")

    print()
    if res["survivors"]:
        print(f"GREEN: {len(res['survivors'])} cell(s) cleared the pre-registered gate:")
        for s in res["survivors"]:
            lo, hi = s["settle_ci"]
            print(f"   {'/'.join(s['cell'])}: settleNet={s['settle_mean_net']:+.2f}c "
                  f"CI=[{lo:+.2f},{hi:+.2f}] mk30={s['mk30_mean_net']:+.2f}c "
                  f"n_fills={s['n_fills']} n_tk={s['n_tickers']}")
    else:
        print("NO cell cleared the gate (efficient / picked-off / insufficient fills).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
