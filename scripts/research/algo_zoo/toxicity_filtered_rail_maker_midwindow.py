"""toxicity_filtered_rail_maker_midwindow — rail maker with OOS adverse-selection gate.

FAMILY: rail maker with out-of-sample adverse-selection gate.

THESIS
------
Rest a maker bid at the RAILS (YES bid 2-5c, or NO bid 2-5c == YES ask 95-98c)
MID-WINDOW (NOT terminal-second). The middle maker-mirage died (2.6% fill,
-79c on fills) — but rails are economically different: tiny fee (P(1-P)->0),
asymmetric near-resolved payoff, wide spread, largely untested mid-window. The
NOVEL ingredient is an out-of-sample (OOS) toxicity gate: only quote contexts
where prior fills were NOT adverse-selected.

  tox(ctx) = rolling prior mean of post-fill SETTLEMENT markout for
             ctx = (asset, time-to-close bucket, rail-side), computed ONLINE
             over fills whose decision-time precedes the current decision
             (ts <= decision -> a real gate, not hindsight).
  POST only when tox(ctx) <= tox_max  (i.e. NOT historically toxic).

Distinct from terminal_rail_queue_rebate (terminal-second/rebate-driven) and
queue_depletion_rail_maker (depletion-triggered): this is mid-window + an OOS gate.

HONEST FILL MODEL
-----------------
A resting maker bid fills ONLY when a real trade print crosses it
(scripts.research.mm_markout_evaluator.first_yes_bid_fill_ts / first_no_bid_fill_ts).
Unfilled = 0 PnL / 0 cost (excluded from the fill-mean; counted in fill-rate
denominator). "A fill you got is a fill you regret" — adverse selection is the
whole game, which is exactly what the OOS gate attacks.

OUTCOME / PnL
-------------
On a fill, mark to settlement via the TERMINAL BOOK at close (rails resolve fast):
reliable book mid at close_epoch -> YES wins iff mid >= 50. PnL/contract =
  settle(0 or 100) - entry - fee + rebate
  fee = ceil(0.07 * P * (1-P)) cents  (~0 at the rails)
  rebate default 0 (sensitivity reported at a stated maker rebate).

NO LOOK-AHEAD
-------------
Signal-book and label-book are independent. Posting uses reliable_nbbo_at(post_ts)
(ts <= post). Toxicity uses only fills from strictly-earlier-closing windows
(windows processed in chronological order of close; a window's own fills are
folded into the gate ONLY after that window closes). Terminal-book outcome uses
reliable_nbbo_at(close).

CI
--
Cluster bootstrap by ticker (the independent unit), >= 1000 resamples, on the
fill-mean net PnL AND the fill-rate. A CI straddling zero is NOT an edge.

Usage:
  cd /Users/gabrielkagan/Documents/kalshi-bot
  python3 scripts/research/algo_zoo/toxicity_filtered_rail_maker_midwindow.py \
    --frames-file /tmp/edge_daily/frames_crypto.jsonl \
    --trades-file /tmp/edge_daily/trades_crypto.jsonl

Parent hunt: kb/decisions/settlement-convergence-worklist.md (algo zoo)
"""
from __future__ import annotations

import argparse
import math
import random
import sys
from collections import defaultdict
from typing import Optional, Sequence, Tuple

sys.path.insert(0, "/Users/gabrielkagan/Documents/kalshi-bot")

from scripts.research import kalshi_book_reconstruct as kbr  # noqa: E402
from scripts.research.mm_markout_evaluator import (  # noqa: E402
    first_no_bid_fill_ts,
    first_yes_bid_fill_ts,
)
from scripts.research.phase1b_live_shadow import load_trades_by_ticker  # noqa: E402
from scripts.research.phase1b_real_price_economics import (  # noqa: E402
    _is_crypto_15m,
    close_epoch_from_ticker,
    load_frames_jsonl,
)


def load_frames_compact_pickle(path: str) -> dict:
    """Fast loader: a compact pickle (built by /tmp/build_frames_cache.py) of
    {ticker: [compact_record]} -> reconstruct the {ticker: [(epoch, inner_dict)]}
    shape that kalshi_book_reconstruct expects. Compact records:
      ('s', yes_levels, no_levels)  -> orderbook_snapshot inner
      ('d', side, price_str, delta_str) -> orderbook_delta inner
    The (epoch, ...) is the first tuple element. Byte-for-byte equivalent to
    load_frames_jsonl on the same source (same fields kbr reads), just ~10x faster
    to parse because it skips per-frame inner-dict json.loads on every run."""
    import pickle
    with open(path, "rb") as fh:
        raw = pickle.load(fh)
    out: dict = {}
    for tk, recs in raw.items():
        lst = []
        for r in recs:
            ep = r[0]
            if r[1] == "s":
                inner = {"type": "orderbook_snapshot",
                         "msg": {"yes_dollars_fp": r[2], "no_dollars_fp": r[3]}}
            else:
                inner = {"type": "orderbook_delta",
                         "msg": {"side": r[2], "price_dollars": r[3], "delta_fp": r[4]}}
            lst.append((ep, inner))
        out[tk] = lst  # already sorted by the cache builder
    return out

# --- parameters -------------------------------------------------------------
# MID-WINDOW post offsets (seconds before close). A 15M window is 900s; these are
# squarely mid-window, NOT terminal-second (avoid the terminal_rail_queue_rebate
# overlap). Several offsets per window so a context accrues fills.
POST_OFFSETS_S = (600, 450, 300, 180)
# Rail band: a maker bid is a "rail" only if its price is in [2, 5]c (cheap rail).
# YES-rail   = best YES bid in [2,5]c.
# NO-rail    = best NO  bid in [2,5]c  (== YES ask in [95,98]c).
RAIL_LO, RAIL_HI = 2.0, 5.0
TOX_MAX_DEFAULT = 0.0   # post only where prior mean settlement markout <= this
REBATE_DEFAULT = 0.0    # maker rebate cents/contract


def _ttc_bucket(off: float) -> str:
    if off <= 240:
        return "t<=4m"
    if off <= 420:
        return "t4-7m"
    return "t7-10m"


def kalshi_fee_ceil_cents(price_cents: float) -> float:
    """ceil(0.07 * P * (1-P)) cents/contract  (per-contract, rounded up per spec)."""
    p = price_cents / 100.0
    return math.ceil(7.0 * p * (1.0 - p))


def settle_pnl_cents(entry: float, side: str, result: str, fee: float,
                     rebate: float) -> float:
    """Settlement PnL of a maker buy at `entry` on `side`, terminal outcome `result`."""
    gross = (100.0 - entry) if side == result else -float(entry)
    return gross - fee + rebate


# How far back from close (seconds) we will accept the last RELIABLE book as the
# terminal-settlement proxy. Rails resolve fast and the book is frequently
# crossed/drifted at the exact close tick, so an at-close-only label discards ~85%
# of the corpus. Walking back to the last reliable book within this window is NOT
# look-ahead: every decision + fill happens strictly before close, and the label
# never uses any frame later than the cutoff it is evaluated at.
TERMINAL_LOOKBACK_S = (0, 1, 2, 3, 5, 8, 12, 20, 30)


def terminal_outcome(frames, close_epoch: float) -> Optional[str]:
    """TERMINAL BOOK outcome at close: the LAST reliable book mid within the final
    TERMINAL_LOOKBACK_S seconds -> 'yes' iff mid >= 50. Returns None if no reliable
    book exists in that terminal window (refuse — never fabricate)."""
    for back in TERMINAL_LOOKBACK_S:
        bid, ask = kbr.reliable_nbbo_at(frames, close_epoch - back)
        if bid is not None and ask is not None:
            mid = (bid + ask) / 2.0
            return "yes" if mid >= 50.0 else "no"
    return None


# --- statistics: cluster bootstrap by ticker --------------------------------


def cluster_bootstrap_ci(
    clusters: Sequence[Sequence[float]],
    *, n_boot: int = 2000, alpha: float = 0.05, seed: int = 7,
) -> Tuple[float, float, float]:
    """Cluster (by ticker) percentile-bootstrap CI for the pooled mean. Each
    cluster is a list of per-fill values for one ticker; resample CLUSTERS with
    replacement (the ticker/window is the independent unit). Returns
    (point_mean, lo, hi). NaN if no observations."""
    clusters = [c for c in clusters if c]
    flat = [x for c in clusters for x in c]
    if not flat:
        return (float("nan"), float("nan"), float("nan"))
    point = sum(flat) / len(flat)
    rng = random.Random(seed)
    n = len(clusters)
    means = []
    for _ in range(n_boot):
        s = 0.0
        cnt = 0
        for _ in range(n):
            c = clusters[rng.randrange(n)]
            s += sum(c)
            cnt += len(c)
        means.append(s / cnt if cnt else float("nan"))
    means = [m for m in means if not math.isnan(m)]
    means.sort()
    lo = means[int((alpha / 2) * len(means))]
    hi = means[min(len(means) - 1, int((1 - alpha / 2) * len(means)))]
    return (point, lo, hi)


def cluster_bootstrap_rate_ci(
    cluster_counts: Sequence[Tuple[int, int]],
    *, n_boot: int = 2000, alpha: float = 0.05, seed: int = 11,
) -> Tuple[float, float, float]:
    """Cluster-bootstrap CI for a RATE (fills/posts). cluster_counts = list of
    (fills, posts) per ticker. Resample tickers with replacement."""
    cluster_counts = [c for c in cluster_counts if c[1] > 0]
    if not cluster_counts:
        return (float("nan"), float("nan"), float("nan"))
    tot_f = sum(f for f, _ in cluster_counts)
    tot_p = sum(p for _, p in cluster_counts)
    point = tot_f / tot_p
    rng = random.Random(seed)
    n = len(cluster_counts)
    rates = []
    for _ in range(n_boot):
        f = p = 0
        for _ in range(n):
            cf, cp = cluster_counts[rng.randrange(n)]
            f += cf
            p += cp
        rates.append(f / p if p else float("nan"))
    rates = [r for r in rates if not math.isnan(r)]
    rates.sort()
    lo = rates[int((alpha / 2) * len(rates))]
    hi = rates[min(len(rates) - 1, int((1 - alpha / 2) * len(rates)))]
    return (point, lo, hi)


# --- core evaluation --------------------------------------------------------


def extract_posts(frames, trades) -> list:
    """HEAVY pass (run ONCE): reconstruct every mid-window rail post and its honest
    fill + terminal outcome. Returns a chronologically-ordered list of post records:
      {close, ticker, asset, ttc, side, price, fee, filled, result}
    The rebate-0 settlement markout + the toxicity gate are applied as a CHEAP
    post-pass on this list (rebate only shifts every fill PnL by +rebate; the gate
    is a threshold), so we never re-run reconstruction for sensitivity sweeps.

    For each ticker we replay the book ONCE to capture the NBBO at every post
    offset (book_at with progressive cutoffs reuses the running book), and ONCE
    for the terminal outcome — instead of re-replaying per (offset, evaluate)."""
    posts = []
    for tk, fr in frames.items():
        if _is_crypto_15m(tk) is None or tk not in trades:
            continue
        close = close_epoch_from_ticker(tk)
        result = terminal_outcome(fr, close)
        if result is None:
            continue  # no reliable terminal book -> no honest label, drop window
        asset = _is_crypto_15m(tk)
        tr = trades[tk]
        for off in POST_OFFSETS_S:
            post_ts = close - off
            bid, ask = kbr.reliable_nbbo_at(fr, post_ts)
            if bid is None or ask is None:
                continue  # GUARD: refuse drifted/unanchored book at decision time
            no_bid = 100.0 - ask
            ttc = _ttc_bucket(off)
            for side, price in (("yes", bid), ("no", no_bid)):
                if not (RAIL_LO <= price <= RAIL_HI):
                    continue  # RAIL filter
                fill_ts = (first_yes_bid_fill_ts(tr, post_ts, price) if side == "yes"
                           else first_no_bid_fill_ts(tr, post_ts, price))
                posts.append({
                    "close": close, "ticker": tk, "asset": asset, "ttc": ttc,
                    "side": side, "price": price,
                    "fee": kalshi_fee_ceil_cents(price),
                    "filled": fill_ts is not None, "result": result,
                })
    posts.sort(key=lambda p: p["close"])
    return posts


def evaluate_from_posts(posts, *, tox_max: float = TOX_MAX_DEFAULT,
                        rebate: float = REBATE_DEFAULT, verbose: bool = True) -> dict:
    """CHEAP pass over precomputed posts: apply the OOS toxicity gate (chronological,
    prior-fill-only) + rebate, then cluster-bootstrap. Two arms (ungated/gated)."""
    # Toxicity state built on the rebate-0 settlement markout (the adverse-selection
    # signal is the binary outcome, independent of rebate — rebate is a pure +shift,
    # so the gate verdict is rebate-invariant by construction).
    tox_state: dict = defaultdict(lambda: [0.0, 0])

    def tox(ctx):
        s, n = tox_state[ctx]
        return (s / n) if n > 0 else None

    arms = {
        "ungated": {"fills": defaultdict(list), "counts": defaultdict(lambda: [0, 0])},
        "gated": {"fills": defaultdict(list), "counts": defaultdict(lambda: [0, 0])},
    }
    n_cold_start_admit = 0
    per_side_fills = defaultdict(list)

    # Group posts by window (same close) so a window's own fills only enter the
    # toxicity state AFTER the whole window is processed (no within-window leak).
    from itertools import groupby
    for _close, grp in groupby(posts, key=lambda p: (p["close"], p["ticker"])):
        window_fills = []
        for p in grp:
            tk = p["ticker"]
            ctx = (p["asset"], p["ttc"], p["side"])
            filled = p["filled"]
            net0 = (settle_pnl_cents(p["price"], p["side"], p["result"], p["fee"], 0.0)
                    if filled else None)
            net = (net0 + rebate) if filled else None

            arms["ungated"]["counts"][tk][1] += 1
            if filled:
                arms["ungated"]["counts"][tk][0] += 1
                arms["ungated"]["fills"][tk].append(net)

            tv = tox(ctx)
            admit = (tv is None) or (tv <= tox_max)
            if tv is None:
                n_cold_start_admit += 1
            if admit:
                arms["gated"]["counts"][tk][1] += 1
                if filled:
                    arms["gated"]["counts"][tk][0] += 1
                    arms["gated"]["fills"][tk].append(net)

            if filled:
                window_fills.append((ctx, net0))  # tox on rebate-0 markout
                per_side_fills[p["side"]].append(net)
        for ctx, mk in window_fills:
            tox_state[ctx][0] += mk
            tox_state[ctx][1] += 1

    out = {"n_rails_seen": len(posts), "n_cold_start_admit": n_cold_start_admit,
           "n_windows": len({(p["close"], p["ticker"]) for p in posts}),
           "arms": {}}
    for name, a in arms.items():
        fill_clusters = list(a["fills"].values())
        count_clusters = list(a["counts"].values())
        n_fills = sum(len(c) for c in fill_clusters)
        n_posts = sum(pp for _, pp in count_clusters)
        mean, lo, hi = cluster_bootstrap_ci(fill_clusters)
        frate, frlo, frhi = cluster_bootstrap_rate_ci(count_clusters)
        out["arms"][name] = {
            "n_posts": n_posts, "n_fills": n_fills,
            "n_tickers_with_fill": len([c for c in fill_clusters if c]),
            "fill_mean_net": mean, "fill_mean_ci": (lo, hi),
            "fill_rate": frate, "fill_rate_ci": (frlo, frhi),
        }
    g = out["arms"]["gated"]
    u = out["arms"]["ungated"]
    out["gated_minus_ungated_fillmean"] = (
        g["fill_mean_net"] - u["fill_mean_net"]
        if not (math.isnan(g["fill_mean_net"]) or math.isnan(u["fill_mean_net"]))
        else float("nan"))
    out["per_side_fill_mean"] = {
        s: (sum(v) / len(v) if v else float("nan"), len(v))
        for s, v in per_side_fills.items()
    }
    if verbose:
        _print(out, tox_max, rebate)
    return out


def evaluate(frames, trades, *, tox_max: float = TOX_MAX_DEFAULT,
             rebate: float = REBATE_DEFAULT, verbose: bool = True) -> dict:
    """Convenience: heavy extract + cheap evaluate in one call (kept for API
    compatibility / single-shot callers)."""
    posts = extract_posts(frames, trades)
    return evaluate_from_posts(posts, tox_max=tox_max, rebate=rebate, verbose=verbose)


def _print(out: dict, tox_max: float, rebate: float) -> None:
    print(f"\n=== toxicity_filtered_rail_maker_midwindow ===")
    print(f"windows (terminal-book outcome OK): {out['n_windows']}  "
          f"rail-posts seen: {out['n_rails_seen']}  "
          f"cold-start admits: {out['n_cold_start_admit']}")
    print(f"tox_max={tox_max}c  rebate={rebate}c\n")
    print(f"{'arm':>9}{'posts':>8}{'fills':>7}{'fill%':>8}{'fillCI%':>16}"
          f"{'meanNet':>9}{'meanNetCI':>18}{'tks':>5}")
    for name in ("ungated", "gated"):
        a = out["arms"][name]
        lo, hi = a["fill_mean_ci"]
        flo, fhi = a["fill_rate_ci"]
        ci = (f"[{lo:+.2f},{hi:+.2f}]" if not math.isnan(lo) else "—")
        fci = (f"[{100*flo:.2f},{100*fhi:.2f}]" if not math.isnan(flo) else "—")
        mn = (f"{a['fill_mean_net']:+.2f}" if not math.isnan(a['fill_mean_net'])
              else "nan")
        print(f"{name:>9}{a['n_posts']:>8}{a['n_fills']:>7}"
              f"{100*a['fill_rate']:>7.2f}%{fci:>16}{mn:>9}{ci:>18}"
              f"{a['n_tickers_with_fill']:>5}")
    print(f"\ngated - ungated fill-mean delta: "
          f"{out['gated_minus_ungated_fillmean']:+.2f}c")
    print("per-rail-side fill-mean (ungated diag):")
    for s, (m, n) in sorted(out["per_side_fill_mean"].items()):
        print(f"  {s:>4}: {m:+.2f}c  n={n}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--frames-file", required=True)
    ap.add_argument("--frames-pickle", default=None,
                    help="compact pickle cache (fast path; built by "
                         "/tmp/build_frames_cache.py). If set + exists, used "
                         "instead of parsing the JSONL.")
    ap.add_argument("--trades-file", required=True)
    ap.add_argument("--tox-max", type=float, default=TOX_MAX_DEFAULT)
    ap.add_argument("--rebate", type=float, default=REBATE_DEFAULT,
                    help="maker rebate cents/contract (sensitivity)")
    ap.add_argument("--max-tickers", type=int, default=0,
                    help="subsample first N (by load order) tickers; 0=all")
    args = ap.parse_args(argv)

    import os
    if args.frames_pickle and os.path.exists(args.frames_pickle):
        print(f"loading frames (compact pickle) {args.frames_pickle} ...", flush=True)
        frames = load_frames_compact_pickle(args.frames_pickle)
    else:
        print(f"loading frames {args.frames_file} ...", flush=True)
        frames = load_frames_jsonl(args.frames_file)
    print(f"  {len(frames)} crypto-15M tickers in frames", flush=True)
    if args.max_tickers:
        keep = set(list(frames)[: args.max_tickers])
        frames = {k: v for k, v in frames.items() if k in keep}
        print(f"  subsampled to {len(frames)} tickers", flush=True)
    print(f"loading trades {args.trades_file} ...", flush=True)
    trades = load_trades_by_ticker(args.trades_file)
    print(f"  {len(trades)} tickers with trades", flush=True)

    print("extracting rail posts (heavy reconstruction, once) ...", flush=True)
    posts = extract_posts(frames, trades)
    print(f"  {len(posts)} rail posts extracted from "
          f"{len({(p['close'], p['ticker']) for p in posts})} windows", flush=True)

    out = evaluate_from_posts(posts, tox_max=args.tox_max, rebate=args.rebate)

    print("\n--- rebate sensitivity (gated arm fill-mean net; rebate = pure +shift) ---")
    for rb in (0.0, 0.25, 0.5, 1.0):
        o = evaluate_from_posts(posts, tox_max=args.tox_max, rebate=rb, verbose=False)
        a = o["arms"]["gated"]
        lo, hi = a["fill_mean_ci"]
        print(f"  rebate={rb:>4}c -> gated mean={a['fill_mean_net']:+.2f}c "
              f"CI=[{lo:+.2f},{hi:+.2f}] n_fills={a['n_fills']}")

    # tox_max sensitivity (does a stricter/looser gate help?)
    print("\n--- tox_max sensitivity (gated arm, rebate=0) ---")
    for tm in (-5.0, -2.0, 0.0, 2.0, 5.0):
        o = evaluate_from_posts(posts, tox_max=tm, rebate=0.0, verbose=False)
        a = o["arms"]["gated"]
        lo, hi = a["fill_mean_ci"]
        print(f"  tox_max={tm:>5}c -> gated mean={a['fill_mean_net']:+.2f}c "
              f"CI=[{lo:+.2f},{hi:+.2f}] n_fills={a['n_fills']} posts={a['n_posts']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
