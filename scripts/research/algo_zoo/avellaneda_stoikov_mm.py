"""Avellaneda-Stoikov optimal market-making on the reconstructed Kalshi binary book.

WWJD frame: figure out a fair value (the reconstructed YES-mid), quote a spread
around it, and SKEW the quotes against inventory and time-to-expiry so we don't
accumulate one-sided risk into settlement.

The A-S model (Avellaneda & Stoikov 2008), adapted to a binary 15M window:

    reservation price  r(t) = s(t) - q * gamma * sigma^2 * (T - t)
    optimal half-spread delta(t) = (gamma * sigma^2 * (T - t)) / 2
                                   + (1/gamma) * ln(1 + gamma / kappa)
    quotes:  YES bid = r - delta   ,   YES ask = r + delta
             (a YES ask is a resting NO bid at 100 - (r + delta))

where
    s(t)    = reconstructed YES-mid in CENTS  (our fair value proxy)
    q       = current signed inventory in YES contracts (+long YES / -short YES)
    sigma   = per-(unit-time) volatility of the binary mid, estimated CAUSALLY
              from frames at/before t only (cents per sqrt(unit-time))
    T - t   = time to window close, in MINUTES (the window's natural clock)
    gamma   = inventory risk aversion  (assumption, swept)
    kappa   = order-arrival decay / book-density  (assumption, stated)

HONEST FILL MODEL (the only honest maker cross signal in this corpus):
  a resting YES bid at price b fills only when a REAL trade print crosses it
  downward (taker_side='no' = a seller, yes_price <= b). A resting YES ask (NO
  bid) at price a fills only when a real BUY crosses up (taker_side='yes',
  yes_price >= a). We reuse the tested fill-time primitives in
  scripts.research.mm_markout_evaluator. A maker fills AT ITS OWN QUOTED PRICE
  (the print only tells us the cross happened; the maker's price is the limit).

We quote on a discrete time grid through the final minutes of each window. Each
grid step: reconstruct a RELIABLE book (reliable_nbbo_at refuses drifted books),
compute fair value + A-S quotes given current inventory, see which side(s) a real
print crosses before the next grid step, update inventory, and continue. At close,
any residual inventory is marked to the binary settlement. Realized cash = entry
cashflows from fills (maker buys cost price, maker sells receive price) + the
settlement value of the terminal inventory, minus per-fill Kalshi fees.

HEADLINE METRIC: per-contract settlement-markout NET of fees (cents/contract),
bootstrap-CI'd over the per-window realized A-S PnL stream (each window is one
i.i.d.-ish sample of running the strategy end-to-end).

FEES: Kalshi trading fee = 7 * P * (1-P) cents/contract (the large-order rate;
kalshi_fee_per_contract_cents). Charged on BOTH the entry fill and the
settlement-side exit of any inventory that is closed by a fill. We apply it once
per fill (entry) and once on the settlement leg of residual inventory is NOT a
real fill so it pays no exit fee (you just hold to expiry, no order). MAKER REBATE
ASSUMPTION: Kalshi pays NO maker rebate on these markets -> rebate = 0. Stated.

Look-ahead discipline: sigma and every quote come from frames with recv_ts <= the
grid time (reliable_nbbo_at enforces the cutoff). Fills are detected from prints
STRICTLY AFTER the post time. Settlement label comes from evaluated_opportunities
market_result (realized outcome), used only at terminal marking.

Usage:
  cd /Users/gabrielkagan/Documents/kalshi-bot
  python3 scripts/research/algo_zoo/avellaneda_stoikov_mm.py \
    --frames-file /tmp/edge_daily/frames_crypto.jsonl \
    --trades-file /tmp/edge_daily/trades_crypto.jsonl \
    --outcomes-db /tmp/edge_daily/state.db
"""
from __future__ import annotations

import argparse
import math
import random
import sys
from collections import defaultdict
from typing import List, Optional, Sequence, Tuple

sys.path.insert(0, "/Users/gabrielkagan/Documents/kalshi-bot")

from scripts.research import kalshi_book_reconstruct as kbr
from scripts.research.mm_markout_evaluator import (
    bootstrap_ci,
    first_no_bid_fill_ts,
    first_yes_bid_fill_ts,
    kalshi_fee_per_contract_cents,
    yes_mid,
)
from scripts.research.phase1b_live_shadow import load_trades_by_ticker
from scripts.research.phase1b_real_price_economics import (
    close_epoch_from_ticker,
    load_frames_jsonl,
    load_outcomes_db,
)

# ---- A-S model parameters (assumptions; gamma swept) -----------------------
# gamma: inventory risk aversion. Swept across a small grid; headline = the
# middle value. Larger gamma => wider spread + harder inventory skew.
GAMMA_GRID = (0.05, 0.1, 0.2)
GAMMA_HEADLINE = 0.1
# kappa: order-arrival intensity decay (book density). Higher kappa => tighter
# base spread. On a deep ~1-cent-tick Kalshi 15M book, arrival is dense; we set
# kappa=1.5 as a stated assumption (the (1/gamma)ln(1+gamma/kappa) term is then a
# modest few-cent base spread, in the realistic Kalshi maker-spread regime).
KAPPA = 1.5
# Max signed inventory we will hold (contracts). A-S skew already discourages
# runaway inventory; this is a hard risk cap (we stop quoting the side that would
# grow |q| beyond this).
MAX_INVENTORY = 5
# We quote 1 contract per side per grid step (a maker resting 1-lot).
QUOTE_SIZE = 1.0

# Quote grid: seconds-before-close at which we (re)quote. The binary settlement
# value forms over the final minute; we run the maker through the last ~5 minutes.
# Coarser than real life (real MM requotes continuously) -> see lookahead_risks.
GRID_OFFSETS_S = (300, 270, 240, 210, 180, 150, 120, 90, 60, 30)

# sigma estimation lookback (seconds before the quote time) for the per-minute
# mid volatility. Causal: only frames in [t - LOOKBACK, t] are used.
SIGMA_LOOKBACK_S = 180
SIGMA_SAMPLE_STEP_S = 15  # sample the reliable mid every 15s within the lookback
SIGMA_FLOOR = 0.5  # cents/sqrt(min) floor so a flat lookback doesn't zero spread

MIN_WINDOWS_FLOOR = 20  # below this, the headline CI is not trustworthy


def reliable_nbbo_multi(frames, query_ts: Sequence[float]) -> dict:
    """Single forward pass that returns reliable NBBO at EACH query timestamp.

    Equivalent to calling kbr.reliable_nbbo_at(frames, t) for every t in
    query_ts, but in ONE ordered sweep instead of N full replays (the naive
    approach re-replays the whole window per query -> O(N*F); this is O(F + N)).
    Honors the same snapshot-anchored, drift-refusing semantics: the book is
    reset on each snapshot, and a query returns (None, None) if the book is not
    anchored or fails is_reliable at that point.

    Returns {t: (yes_bid_cents, yes_ask_cents)} for each requested t."""
    qs = sorted(set(query_ts))
    out: dict = {}
    b = kbr.KalshiBook()
    anchored = False
    qi = 0
    nq = len(qs)
    for ts, inner in frames:
        # emit for every query timestamp strictly before this frame's ts
        while qi < nq and qs[qi] < ts:
            t = qs[qi]
            if anchored and b.is_reliable():
                out[t] = (b.best_yes_bid_cents(), b.best_yes_ask_cents())
            else:
                out[t] = (None, None)
            qi += 1
        if inner.get("type") == "orderbook_snapshot":
            b = kbr.KalshiBook()
            b.apply_frame(inner)
            anchored = True
        else:
            b.apply_frame(inner)
    # any remaining queries are at/after the last frame -> evaluate final book
    while qi < nq:
        t = qs[qi]
        if anchored and b.is_reliable():
            out[t] = (b.best_yes_bid_cents(), b.best_yes_ask_cents())
        else:
            out[t] = (None, None)
        qi += 1
    return out


def sigma_from_mids(mids: List[float]) -> Optional[float]:
    """Per-MINUTE volatility (cents) from a causal sequence of reliable YES-mids
    sampled every SIGMA_SAMPLE_STEP_S seconds. None if too few samples."""
    if len(mids) < 4:
        return None
    diffs = [mids[i + 1] - mids[i] for i in range(len(mids) - 1)]
    if len(diffs) < 3:
        return None
    mean = sum(diffs) / len(diffs)
    var = sum((d - mean) ** 2 for d in diffs) / (len(diffs) - 1)
    sd_per_sample = math.sqrt(var)
    sigma_per_min = sd_per_sample * math.sqrt(60.0 / SIGMA_SAMPLE_STEP_S)
    return max(sigma_per_min, SIGMA_FLOOR)


def as_quotes(
    mid: float, q: float, sigma_per_min: float, t_minus_close_min: float,
    gamma: float, kappa: float,
) -> Tuple[float, float, float, float]:
    """Avellaneda-Stoikov reservation price + optimal half-spread + the two quotes
    (all in cents). Returns (reservation, half_spread, yes_bid, yes_ask)."""
    tau = max(t_minus_close_min, 0.0)
    var = sigma_per_min ** 2
    reservation = mid - q * gamma * var * tau
    half_spread = 0.5 * gamma * var * tau + (1.0 / gamma) * math.log1p(gamma / kappa)
    yes_bid = reservation - half_spread
    yes_ask = reservation + half_spread
    return reservation, half_spread, yes_bid, yes_ask


def precompute_window(frames, close: float) -> Optional[List[dict]]:
    """GAMMA-INDEPENDENT per-window precompute (done ONCE, reused across the
    gamma grid). Returns a list of per-grid-step dicts, each:
        {off, post_ts, live_until, mid, sigma}
    using a SINGLE reliable-NBBO forward pass over the window's frames. Steps
    with no reliable book at post time are dropped. None if no step survives."""
    grid = sorted(GRID_OFFSETS_S, reverse=True)  # furthest-from-close first

    # collect every timestamp we need a reliable mid for: each grid post time +
    # its sigma-lookback samples.
    query_ts: set = set()
    sigma_sample_ts: dict = {}  # post_ts -> [sample ts ...]
    n_sig = SIGMA_LOOKBACK_S // SIGMA_SAMPLE_STEP_S
    for off in grid:
        post_ts = close - off
        query_ts.add(post_ts)
        samples = [post_ts - SIGMA_LOOKBACK_S + i * SIGMA_SAMPLE_STEP_S
                   for i in range(n_sig + 1)]
        sigma_sample_ts[post_ts] = samples
        query_ts.update(samples)

    nbbo = reliable_nbbo_multi(frames, query_ts)

    steps: List[dict] = []
    for idx, off in enumerate(grid):
        post_ts = close - off
        bid_px, ask_px = nbbo.get(post_ts, (None, None))
        if bid_px is None or ask_px is None:
            continue  # GUARD: no reliable book at quote time -> skip step
        mid = yes_mid(bid_px, ask_px)
        sample_mids = []
        for s in sigma_sample_ts[post_ts]:
            sb, sa = nbbo.get(s, (None, None))
            if sb is not None and sa is not None:
                sample_mids.append(yes_mid(sb, sa))
        sigma = sigma_from_mids(sample_mids)
        if sigma is None:
            sigma = SIGMA_FLOOR
        next_off = grid[idx + 1] if idx + 1 < len(grid) else 0
        steps.append({
            "off": off, "post_ts": post_ts, "live_until": close - next_off,
            "mid": mid, "sigma": sigma,
        })
    return steps or None


def run_window(steps: List[dict], trades, result: str, gamma: float,
               kappa: float) -> dict:
    """Simulate the A-S maker through one window from PRECOMPUTED (gamma-free)
    grid steps. Cheap: pure arithmetic + the (gamma-dependent) trade-cross scan.

    Cash convention (cents, per the YES side):
      - a maker BUY of YES at price b: pay b, inventory q += size, cash -= b
      - a maker SELL of YES at price a: receive a, inventory q -= size, cash += a
        (a YES ask filled = we sold YES = a resting NO bid lifted)
      - each fill pays a Kalshi fee = 7*P*(1-P) on its own price.
      - terminal inventory q marked to settlement: long YES contract -> 100 if
        result=='yes' else 0; short YES (q<0) is the mirror. No exit fee (holding
        to expiry is not an order).
    """
    q = 0.0
    cash = 0.0
    fee_paid = 0.0
    n_fills = 0
    n_posts = 0

    for st in steps:
        post_ts = st["post_ts"]
        live_until = st["live_until"]
        t_min = st["off"] / 60.0
        _, _, as_bid, as_ask = as_quotes(st["mid"], q, st["sigma"], t_min, gamma, kappa)

        # snap to whole cents in the valid 1..99 grid (Kalshi trades whole cents)
        as_bid = max(1.0, min(99.0, round(as_bid)))
        as_ask = max(1.0, min(99.0, round(as_ask)))
        if as_ask <= as_bid:
            continue  # never quote a crossed/locked book

        # YES-bid (we BUY yes) -- only if it won't push q above the cap
        if q < MAX_INVENTORY:
            n_posts += 1
            fts = first_yes_bid_fill_ts(trades, post_ts, as_bid)
            if fts is not None and fts <= live_until:
                cash -= as_bid * QUOTE_SIZE
                fee_paid += kalshi_fee_per_contract_cents(as_bid) * QUOTE_SIZE
                q += QUOTE_SIZE
                n_fills += 1

        # YES-ask (we SELL yes / our resting NO bid gets lifted) -- only if q can drop
        if q > -MAX_INVENTORY:
            n_posts += 1
            no_bid = 100.0 - as_ask
            fts = first_no_bid_fill_ts(trades, post_ts, no_bid)
            if fts is not None and fts <= live_until:
                cash += as_ask * QUOTE_SIZE
                fee_paid += kalshi_fee_per_contract_cents(as_ask) * QUOTE_SIZE
                q -= QUOTE_SIZE
                n_fills += 1

    settle_val = 100.0 if result == "yes" else 0.0
    terminal = q * settle_val
    realized = cash + terminal - fee_paid

    return {
        "realized_cents": realized,
        "n_fills": n_fills,
        "n_posts": n_posts,
        "terminal_inventory": q,
        "fee_paid": fee_paid,
    }


def build_window_cache(frames, trades, outcomes) -> dict:
    """One-time GAMMA-INDEPENDENT precompute over every usable window. Returns
    {ticker: (steps, result)} so the gamma sweep replays cheap arithmetic only.
    This is where the expensive single-pass reliable-NBBO replay happens."""
    cache: dict = {}
    for tk, fr in frames.items():
        if tk not in outcomes or tk not in trades:
            continue
        close = close_epoch_from_ticker(tk)
        steps = precompute_window(fr, close)
        if steps is None:
            continue
        cache[tk] = (steps, outcomes[tk]["result"])
    return cache


def evaluate(cache, trades, gamma: float, kappa: float) -> dict:
    """Run A-S over every cached window. Headline metric = per-CONTRACT
    settlement-markout net of fees (total realized cents / total contracts
    filled). The bootstrap resamples at the per-contract level."""
    per_window_realized: List[float] = []      # realized cents per window
    per_window_fills: List[int] = []
    per_contract_markouts: List[float] = []    # one entry per FILLED contract
    total_fills = 0
    total_realized = 0.0
    n_windows = 0

    for tk, (steps, result) in cache.items():
        out = run_window(steps, trades[tk], result, gamma, kappa)
        n_windows += 1
        per_window_realized.append(out["realized_cents"])
        per_window_fills.append(out["n_fills"])
        total_fills += out["n_fills"]
        total_realized += out["realized_cents"]
        if out["n_fills"] > 0:
            # per-contract markout for this window's fills (uniform attribution)
            pc = out["realized_cents"] / out["n_fills"]
            per_contract_markouts.extend([pc] * out["n_fills"])

    headline_per_contract = (total_realized / total_fills) if total_fills else float("nan")

    # Bootstrap the PER-CONTRACT settlement markout. Resample at the per-contract
    # level (each filled contract is the unit of the headline metric).
    if per_contract_markouts:
        lo, hi = bootstrap_ci(per_contract_markouts, n_boot=2000)
    else:
        lo, hi = (float("nan"), float("nan"))

    return {
        "n_windows": n_windows,
        "total_fills": total_fills,
        "total_realized_cents": total_realized,
        "headline_per_contract_cents": headline_per_contract,
        "ci": (lo, hi),
        "per_contract_markouts": per_contract_markouts,
        "per_window_realized": per_window_realized,
        "mean_fills_per_window": (sum(per_window_fills) / n_windows) if n_windows else 0.0,
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--frames-file", default="/tmp/edge_daily/frames_crypto.jsonl")
    ap.add_argument("--trades-file", default="/tmp/edge_daily/trades_crypto.jsonl")
    ap.add_argument("--outcomes-db", default="/tmp/edge_daily/state.db")
    ap.add_argument("--max-windows", type=int, default=0,
                    help="subsample N windows (0 = all) for a faster first pass")
    args = ap.parse_args(argv)

    print("loading frames...", flush=True)
    frames = load_frames_jsonl(args.frames_file)
    print(f"  {len(frames)} crypto-15M windows", flush=True)
    print("loading trades...", flush=True)
    trades = load_trades_by_ticker(args.trades_file)
    print("loading outcomes...", flush=True)
    outcomes = load_outcomes_db(args.outcomes_db, set(frames))

    usable = [tk for tk in frames if tk in outcomes and tk in trades]
    print(f"windows: {len(frames)}  with trades: {sum(1 for t in frames if t in trades)}  "
          f"with outcome: {sum(1 for t in frames if t in outcomes)}  "
          f"usable: {len(usable)}", flush=True)

    if args.max_windows and len(usable) > args.max_windows:
        rng = random.Random(12345)
        usable_set = set(rng.sample(usable, args.max_windows))
        frames = {tk: fr for tk, fr in frames.items() if tk in usable_set}
        print(f"  subsampled to {len(usable_set)} windows", flush=True)

    print("precomputing per-window grid (single NBBO pass each)...", flush=True)
    cache = build_window_cache(frames, trades, outcomes)
    print(f"  {len(cache)} windows with a reliable book to quote against",
          flush=True)
    del frames  # free the heavy frame lists before the gamma sweep

    print(f"\n{'gamma':>7}{'windows':>9}{'fills':>8}{'totPnL$':>10}"
          f"{'perContract':>13}{'CI(cents)':>20}", flush=True)
    headline = None
    for gamma in GAMMA_GRID:
        res = evaluate(cache, trades, gamma, KAPPA)
        lo, hi = res["ci"]
        ci = f"[{lo:+.2f},{hi:+.2f}]" if res["total_fills"] else "—"
        print(f"{gamma:>7.2f}{res['n_windows']:>9}{res['total_fills']:>8}"
              f"{res['total_realized_cents']/100.0:>10.2f}"
              f"{res['headline_per_contract_cents']:>+13.3f}{ci:>20}", flush=True)
        if gamma == GAMMA_HEADLINE:
            headline = res

    print("\n--- HEADLINE (gamma={}) ---".format(GAMMA_HEADLINE), flush=True)
    if headline:
        lo, hi = headline["ci"]
        print(f"windows simulated      : {headline['n_windows']}")
        print(f"total contract fills   : {headline['total_fills']}")
        print(f"mean fills/window      : {headline['mean_fills_per_window']:.2f}")
        print(f"total realized PnL     : ${headline['total_realized_cents']/100.0:.2f}")
        print(f"per-contract markout   : {headline['headline_per_contract_cents']:+.3f} c "
              f"(net of fees)")
        print(f"bootstrap 95% CI       : [{lo:+.3f}, {hi:+.3f}] c/contract")
        # Verdict logic:
        #   EDGE        -> CI strictly above 0 (profitable maker, fees in) + enough n
        #   NO_EDGE     -> CI does NOT include a profit we'd act on, with enough n:
        #                  either entirely below 0 (we get picked off) OR straddling 0
        #   DATA_GAP    -> no fills at all
        #   INCONCLUSIVE-> some fills but n below the trust floor
        enough = (headline['total_fills'] >= 30
                  and headline['n_windows'] >= MIN_WINDOWS_FLOOR)
        if not headline['total_fills']:
            verdict = "DATA_GAP"
        elif not enough:
            verdict = "INCONCLUSIVE"
        elif lo > 0:
            verdict = "EDGE"
        else:
            # hi < 0 (entirely below zero) or lo <= 0 <= hi (straddles) -> no edge
            verdict = "NO_EDGE"
        print(f"VERDICT                : {verdict}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
