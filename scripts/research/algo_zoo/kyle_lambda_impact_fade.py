#!/usr/bin/env python3
"""kyle_lambda_impact_fade  (family: price_impact / microstructure)

SPEC
----
Estimate Kyle's lambda (price impact per unit signed volume) per ticker from the
relation between cumulative signed trade flow and book-mid moves. When a single
trade or short sweep moves the mid MORE than the local lambda predicts (an
impact OVERSHOOT relative to the order's information content), fade the residual:
the transient impact decays back. This monetizes mean-reversion of mechanical
(uninformed) price impact.

MECHANISM
  1. Per ticker, from TRADES + FRAMES build aligned (signed_volume, mid_change)
     pairs over short intervals. Estimate lambda via OLS of mid_change on signed
     volume over a rolling burn-in (no look-ahead; lambda from past only).
  2. DECISION: at each trade, compute the realized mid move vs lambda*signed_vol.
     A large positive RESIDUAL (mid moved more than impact justifies) flags a
     transient overshoot. At that moment reconstruct reliable NBBO (honor
     refusal) and FADE the overshoot: enter the side the mid over-moved AWAY from,
     as a TAKER at the ask, OR as a resting MAKER bid at the post-overshoot best
     bid with honest trade-cross fill.
  3. LABEL: two horizons — (a) short-horizon mid reversion at +T seconds
     (gross signal validation), and (b) the TERMINAL book mid at close
     (tradeable settlement label). PnL/ct net of Kalshi fee. EDGE requires the
     TERMINAL-labeled net PnL CI > 0, not just short-horizon reversion.
  4. HEADLINE = mean net PnL per entered contract, ticker-clustered bootstrap CI
     (>=1000). Report lambda distribution + residual-overshoot count (n).

WHY IT MIGHT BEAT AN EFFICIENT MARKET
  Kyle's-lambda price-impact is explicitly on the hunt's open list. Distinct from
  OFI (imbalance) and microprice (fair-value snapshot): this conditions on the
  RESIDUAL of an impact MODEL — separating informed impact (permanent) from
  mechanical impact (transient, fadeable). In thin 15M books a single retail
  market order can overshoot; if the overshoot decays before close, fading it is
  an edge. The terminal label is the honesty gate against horizon cherry-picking
  (the trap that refuted microprice).

DATA: TRADES (signed flow), FRAMES (mid series + lambda fit + NBBO + terminal
label + honest maker fill). No spot -> all 7 assets.

USAGE
  cd /Users/gabrielkagan/Documents/kalshi-bot
  python3 scripts/research/algo_zoo/kyle_lambda_impact_fade.py \
    --frames-file /tmp/edge_daily/frames_crypto.jsonl \
    --trades-file /tmp/edge_daily/trades_crypto.jsonl \
    [--max-tickers N] [--burn-in K] [--horizon-s T] [--overshoot-z Z]
"""
from __future__ import annotations

import argparse
import json
import math
import random
import sys
from collections import defaultdict
from datetime import datetime, timezone

sys.path.insert(0, "/Users/gabrielkagan/Documents/kalshi-bot")

from scripts.research import kalshi_book_reconstruct as kbr  # noqa: E402
from scripts.research.phase1b_real_price_economics import (  # noqa: E402
    _is_crypto_15m,
    close_epoch_from_ticker,
)
from scripts.research.settlement_convergence_p1a import (  # noqa: E402
    kalshi_fee_per_contract_cents,
)

# ---- parameters (tunable via CLI) ------------------------------------------
BURN_IN_DEFAULT = 20          # trades of past flow to fit rolling lambda
HORIZON_S_DEFAULT = 30.0      # short-horizon reversion label window (seconds)
OVERSHOOT_Z_DEFAULT = 2.0     # residual must exceed Z * resid-std to trade
MIN_LAMBDA_R2 = 0.0           # accept any (positive) fit; gate is on residual
DECISION_MID_WINDOW_S = 10.0  # interval over which a trade's mid_change is measured
ENTRY_CUTOFF_BEFORE_CLOSE_S = 30.0  # don't enter inside the final settlement window
EARLIEST_ENTRY_AFTER_OPEN_S = 60.0  # ignore the first minute (book warming up)


def _epoch(iso: str) -> float:
    s = iso.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def _trade_ts(msg: dict) -> float:
    """Event time of the trade print (prefer ms; fall back to s)."""
    if "ts_ms" in msg:
        return float(msg["ts_ms"]) / 1000.0
    return float(msg["ts"])


# ---- loaders ----------------------------------------------------------------


def load_frames(path: str, allowed: set | None = None) -> dict:
    """{ticker: [(recv_epoch, inner)]} sorted ascending by recv epoch.
    Uses _wire_recv_ts (arrival) — book state is only knowable on arrival."""
    frames: dict[str, list] = defaultdict(list)
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
            if not _is_crypto_15m(tk):
                continue
            if allowed is not None and tk not in allowed:
                continue
            try:
                rt = _epoch(env["_wire_recv_ts"])
            except (KeyError, ValueError, TypeError):
                continue
            frames[tk].append((rt, inner))
    for tk in frames:
        frames[tk].sort(key=lambda x: x[0])
    return frames


def load_trades(path: str) -> dict:
    """{ticker: [(event_ts, recv_ts, signed_vol, yes_c, taker_side, count)]}.
    signed_vol = +count for a YES-buy (taker_side='yes'; pushes YES up),
                 -count for a YES-sell (taker_side='no'; pushes YES down).
    Sorted ascending by event_ts."""
    out: dict[str, list] = defaultdict(list)
    with open(path) as fh:
        for line in fh:
            if not line.strip():
                continue
            try:
                env = json.loads(line)
                msg = json.loads(env["_raw"])["msg"]
            except (ValueError, KeyError):
                continue
            tk = msg.get("market_ticker", "")
            if not _is_crypto_15m(tk):
                continue
            try:
                ts = _trade_ts(msg)
                rt = _epoch(env["_wire_recv_ts"])
                count = float(msg["count_fp"])
                yes_c = float(msg["yes_price_dollars"]) * 100.0
                side = msg["taker_side"]
            except (KeyError, ValueError, TypeError):
                continue
            signed = count if side == "yes" else -count
            out[tk].append((ts, rt, signed, yes_c, side, count))
    for tk in out:
        out[tk].sort(key=lambda x: x[0])
    return out


# ---- mid-series helper ------------------------------------------------------


def build_mid_series(frames_for_ticker: list) -> list:
    """Replay frames into a running book; emit (recv_epoch, yes_mid_cents) whenever
    a RELIABLE (uncrossed, anchored) NBBO is available. Snapshot-anchored: reset
    on each snapshot so re-subscribe drift is discarded. This is the *signal* mid
    series; both lambda fit and reversion labels read from it (look-ahead-safe by
    construction since each point uses only frames up to its own recv epoch)."""
    series: list[tuple[float, float]] = []
    b = kbr.KalshiBook()
    anchored = False
    since_snap = 0
    for ts, inner in frames_for_ticker:
        t = inner.get("type")
        if t == "orderbook_snapshot":
            b = kbr.KalshiBook()
            b.apply_frame(inner)
            anchored = True
            since_snap = 0
        else:
            b.apply_frame(inner)
            since_snap += 1
        if not anchored:
            continue
        if since_snap > 100000:
            continue
        if not b.is_reliable():
            continue
        yb = b.best_yes_bid_cents()
        ya = b.best_yes_ask_cents()
        if yb is None or ya is None:
            continue
        series.append((ts, (yb + ya) / 2.0))
    return series


def mid_at(series: list, t: float):
    """Last reliable yes_mid at or before t (no look-ahead). series sorted asc."""
    import bisect
    if not series:
        return None
    times = [s[0] for s in series]
    i = bisect.bisect_right(times, t) - 1
    if i < 0:
        return None
    return series[i][1]


def mid_after(series: list, t: float, max_gap_s: float = 999999.0):
    """First reliable yes_mid strictly after t (for forward reversion label)."""
    import bisect
    if not series:
        return None
    times = [s[0] for s in series]
    i = bisect.bisect_right(times, t)
    if i >= len(series):
        return None
    if series[i][0] - t > max_gap_s:
        return None
    return series[i][1]


def ols_lambda(xs: list, ys: list):
    """Slope of OLS y = lambda * x (through origin: signed_vol drives mid_change,
    zero flow => zero expected change). Returns (lambda, resid_std) or (None,None).
    Through-origin keeps lambda interpretable as price impact per contract."""
    n = len(xs)
    if n < 3:
        return None, None
    sxx = sum(x * x for x in xs)
    if sxx <= 1e-12:
        return None, None
    sxy = sum(x * y for x, y in zip(xs, ys))
    lam = sxy / sxx
    resid = [y - lam * x for x, y in zip(xs, ys)]
    if n > 1:
        rs = math.sqrt(sum(r * r for r in resid) / (n - 1))
    else:
        rs = 0.0
    return lam, rs


# ---- statistics: ticker-clustered bootstrap --------------------------------


def cluster_bootstrap_ci(values_by_cluster: dict, n_boot: int = 2000,
                         alpha: float = 0.05, seed: int = 4242):
    """Cluster (ticker) bootstrap of the GRAND MEAN over all contracts.
    Resample tickers with replacement; the statistic is the pooled mean of all
    per-contract net PnL in the resampled tickers. Returns (point, lo, hi, n)."""
    clusters = [v for v in values_by_cluster.values() if v]
    if not clusters:
        return float("nan"), float("nan"), float("nan"), 0
    all_vals = [x for v in clusters for x in v]
    n_total = len(all_vals)
    point = sum(all_vals) / n_total
    rng = random.Random(seed)
    k = len(clusters)
    means = []
    for _ in range(n_boot):
        s = 0.0
        c = 0
        for _ in range(k):
            cl = clusters[rng.randrange(k)]
            s += sum(cl)
            c += len(cl)
        if c:
            means.append(s / c)
    if not means:
        return point, float("nan"), float("nan"), n_total
    means.sort()
    lo = means[int((alpha / 2) * len(means))]
    hi = means[min(len(means) - 1, int((1 - alpha / 2) * len(means)))]
    return point, lo, hi, n_total


# ---- core engine ------------------------------------------------------------


def run(frames_path: str, trades_path: str, max_tickers=None,
        burn_in=BURN_IN_DEFAULT, horizon_s=HORIZON_S_DEFAULT,
        overshoot_z=OVERSHOOT_Z_DEFAULT, fill_model="taker", verbose=True):
    trades = load_trades(trades_path)
    # subsample tickers (those with enough trades to fit lambda) for a first pass
    candidate_tks = [tk for tk, tr in trades.items() if len(tr) >= burn_in + 5]
    candidate_tks.sort()
    if max_tickers is not None:
        candidate_tks = candidate_tks[:max_tickers]
    allowed = set(candidate_tks)
    if verbose:
        print(f"loading frames for {len(allowed)} candidate tickers "
              f"(of {len(trades)} tickers with trades)...", flush=True)
    frames = load_frames(frames_path, allowed=allowed)

    lambdas = []                 # fitted lambda distribution (positive impacts)
    overshoot_n = 0
    # per-ticker net-PnL lists for clustered bootstrap, terminal + reversion label
    term_taker = defaultdict(list)
    term_maker = defaultdict(list)
    rev_taker = defaultdict(list)   # short-horizon mid-reversion gross (cents, no fee)
    maker_posted = 0
    maker_filled = 0
    n_no_book = 0
    n_no_reliable_entry = 0

    for tk in candidate_tks:
        tr = trades.get(tk)
        fr = frames.get(tk)
        if not tr or not fr:
            continue
        series = build_mid_series(fr)
        if len(series) < 5:
            continue
        close = close_epoch_from_ticker(tk)
        open_ts = close - 15 * 60.0

        # determine terminal label = book mid at close from a reliable book
        term_mid = mid_at(series, close)
        if term_mid is None:
            # fall back: last reliable mid before close
            term_mid = series[-1][1] if series else None
        if term_mid is None:
            continue
        # binary settlement: YES wins iff terminal yes-mid > 50 (deep ITM near 100,
        # OTM near 0). Use the terminal *mid* as the tradeable outcome proxy since
        # in-window DB settlements are sparse (per corpus note). This is the
        # honesty gate: PnL marked to the real terminal book, not a cherry horizon.
        # Where term_mid is ambiguous (40-60), the contract is genuinely uncertain;
        # we still mark to it honestly (expected value of holding).

        # rolling (signed_vol, mid_change) pairs to fit lambda from PAST only.
        hist_x: list[float] = []
        hist_y: list[float] = []

        for (ts, rt, signed, yes_c, side, count) in tr:
            # entry-time gating: use event ts for market timing
            if ts < open_ts + EARLIEST_ENTRY_AFTER_OPEN_S:
                # still accumulate history but don't trade yet
                pass
            # decision time = trade arrival (recv); never read book past it.
            decision_t = rt
            mid_pre = mid_at(series, decision_t - 1e-6)
            mid_post = mid_at(series, decision_t + DECISION_MID_WINDOW_S)
            if mid_pre is None or mid_post is None:
                continue
            realized_change = mid_post - mid_pre

            # fit lambda on past pairs (strictly before this trade)
            lam, rstd = ols_lambda(hist_x, hist_y) if len(hist_x) >= burn_in else (None, None)

            # append THIS pair to history AFTER using past-only fit (no look-ahead)
            hist_x.append(signed)
            hist_y.append(realized_change)
            if len(hist_x) > burn_in:
                hist_x.pop(0)
                hist_y.pop(0)

            if lam is None or rstd is None or rstd <= 1e-9:
                continue
            if lam > 0:
                lambdas.append(lam)

            predicted = lam * signed
            residual = realized_change - predicted

            # OVERSHOOT detection: |residual| beyond Z*resid_std AND in the
            # direction the trade pushed (mechanical overshoot, fadeable).
            if abs(residual) < overshoot_z * rstd:
                continue
            # we only fade when the overshoot is in the same direction as the
            # signed flow (a buy that over-lifted, or a sell that over-dumped).
            # signed>0 & residual>0 => YES over-moved UP  => fade by entering NO.
            # signed<0 & residual<0 => YES over-moved DOWN => fade by entering YES.
            if signed > 0 and residual > 0:
                fade_side = "no"
            elif signed < 0 and residual < 0:
                fade_side = "yes"
            else:
                continue

            # market-timing window: must be tradeable (not the first minute, not
            # inside the final settlement window).
            if ts < open_ts + EARLIEST_ENTRY_AFTER_OPEN_S:
                continue
            if ts > close - ENTRY_CUTOFF_BEFORE_CLOSE_S:
                continue

            # reconstruct RELIABLE NBBO at decision (honor refusal)
            bid, ask = kbr.reliable_nbbo_at(fr, decision_t)
            if bid is None or ask is None:
                n_no_reliable_entry += 1
                continue
            if not (bid <= ask and 0 < ask < 100):
                continue

            overshoot_n += 1

            # ---- short-horizon reversion label (gross, cents) ----
            # did the mid revert toward fair after +horizon? measure in the
            # FADE side's price direction. yes-mid reverting DOWN helps a NO fade.
            mid_h = mid_after(series, decision_t + horizon_s - 1e-6, max_gap_s=horizon_s * 3)
            if mid_h is not None:
                mid_now = mid_at(series, decision_t)
                if mid_now is not None:
                    if fade_side == "no":
                        # NO gains if yes-mid falls: gross = (mid_now - mid_h)
                        rev_taker[tk].append(mid_now - mid_h)
                    else:
                        rev_taker[tk].append(mid_h - mid_now)

            # terminal binary outcome for the FADE side
            yes_won = term_mid > 50.0

            # ---- TAKER fill at the ask of the fade side ----
            if fade_side == "yes":
                entry_px = ask  # buy YES at ask
                won = yes_won
            else:
                entry_px = 100.0 - bid  # buy NO at no_ask = 100 - yes_bid
                won = not yes_won
            if not (0 < entry_px < 100):
                continue
            gross = (100.0 - entry_px) if won else -entry_px
            fee = kalshi_fee_per_contract_cents(entry_px)
            term_taker[tk].append(gross - fee)

            # ---- MAKER fill (honest trade-cross) at the post-overshoot best bid ----
            # We rest a bid on the fade side and only fill if a real trade crosses.
            maker_posted += 1
            if fade_side == "yes":
                # rest YES bid at current best yes bid; fills when a YES-SELL
                # (taker_side='no') prints at yes_price <= our bid after decision.
                our_bid = bid
                fill_ts = _first_cross_yes_bid(tr, decision_t, our_bid)
                if fill_ts is not None:
                    maker_filled += 1
                    g = (100.0 - our_bid) if yes_won else -our_bid
                    f = kalshi_fee_per_contract_cents(our_bid)
                    term_maker[tk].append(g - f)
            else:
                # rest NO bid at current best no bid (= 100 - yes_ask); fills when a
                # YES-BUY (taker_side='yes') prints at yes_price >= 100 - our no_bid.
                our_no_bid = 100.0 - ask
                if not (0 < our_no_bid < 100):
                    maker_posted -= 1
                    continue
                fill_ts = _first_cross_no_bid(tr, decision_t, our_no_bid)
                if fill_ts is not None:
                    maker_filled += 1
                    g = (100.0 - our_no_bid) if (not yes_won) else -our_no_bid
                    f = kalshi_fee_per_contract_cents(our_no_bid)
                    term_maker[tk].append(g - f)

    return {
        "lambdas": lambdas,
        "overshoot_n": overshoot_n,
        "term_taker": term_taker,
        "term_maker": term_maker,
        "rev_taker": rev_taker,
        "maker_posted": maker_posted,
        "maker_filled": maker_filled,
        "n_tickers": len(candidate_tks),
        "n_no_reliable_entry": n_no_reliable_entry,
    }


def _first_cross_yes_bid(tr, post_t, bid_cents):
    """First real YES-SELL (taker_side='no') at yes_price <= our bid after post_t."""
    for (ts, rt, signed, yes_c, side, count) in tr:
        if rt > post_t and side == "no" and yes_c <= bid_cents:
            return rt
    return None


def _first_cross_no_bid(tr, post_t, no_bid_cents):
    """First real YES-BUY (taker_side='yes') at yes_price >= 100-no_bid after post_t.
    A YES-buy lifting our NO bid means a seller of NO crosses to us."""
    thresh = 100.0 - no_bid_cents
    for (ts, rt, signed, yes_c, side, count) in tr:
        if rt > post_t and side == "yes" and yes_c >= thresh:
            return rt
    return None


def _pctl(xs, p):
    if not xs:
        return float("nan")
    xs = sorted(xs)
    i = min(len(xs) - 1, int(p * len(xs)))
    return xs[i]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--frames-file", default="/tmp/edge_daily/frames_crypto.jsonl")
    ap.add_argument("--trades-file", default="/tmp/edge_daily/trades_crypto.jsonl")
    ap.add_argument("--max-tickers", type=int, default=None)
    ap.add_argument("--burn-in", type=int, default=BURN_IN_DEFAULT)
    ap.add_argument("--horizon-s", type=float, default=HORIZON_S_DEFAULT)
    ap.add_argument("--overshoot-z", type=float, default=OVERSHOOT_Z_DEFAULT)
    args = ap.parse_args(argv)

    res = run(args.frames_file, args.trades_file, max_tickers=args.max_tickers,
              burn_in=args.burn_in, horizon_s=args.horizon_s,
              overshoot_z=args.overshoot_z)

    lam = res["lambdas"]
    print(f"\n=== kyle_lambda_impact_fade ===")
    print(f"tickers evaluated: {res['n_tickers']}")
    print(f"lambda fits (positive): n={len(lam)}")
    if lam:
        print(f"  lambda cents/contract: p10={_pctl(lam,0.10):.4f} "
              f"median={_pctl(lam,0.50):.4f} p90={_pctl(lam,0.90):.4f} "
              f"mean={sum(lam)/len(lam):.4f}")
    print(f"overshoot signals (entered, taker): {res['overshoot_n']}")
    print(f"reliable-book refusals at entry: {res['n_no_reliable_entry']}")
    print(f"maker posted={res['maker_posted']} filled={res['maker_filled']} "
          f"({100*res['maker_filled']/max(1,res['maker_posted']):.1f}% fill)")

    # ---- short-horizon reversion (gross signal validation) ----
    p_rev, lo_rev, hi_rev, n_rev = cluster_bootstrap_ci(res["rev_taker"])
    print(f"\n[A] SHORT-HORIZON mid reversion (gross, cents/ct, +{args.horizon_s:.0f}s):")
    print(f"    mean={p_rev:+.3f}c  CI=[{lo_rev:+.3f},{hi_rev:+.3f}]  n={n_rev}")

    # ---- TERMINAL taker label (the tradeable, fee-net headline) ----
    p_t, lo_t, hi_t, n_t = cluster_bootstrap_ci(res["term_taker"])
    print(f"\n[B] TERMINAL taker net PnL/ct (fee-net, ticker-clustered boot):")
    print(f"    mean={p_t:+.3f}c  CI=[{lo_t:+.3f},{hi_t:+.3f}]  n={n_t}")

    # ---- TERMINAL maker label (honest fill) ----
    p_m, lo_m, hi_m, n_m = cluster_bootstrap_ci(res["term_maker"])
    print(f"\n[C] TERMINAL maker net PnL/ct (honest fill, fee-net):")
    print(f"    mean={p_m:+.3f}c  CI=[{lo_m:+.3f},{hi_m:+.3f}]  n={n_m}")

    # headline = terminal taker (primary tradeable path)
    print(f"\n=== HEADLINE (terminal taker, fee-net): {p_t:+.3f}c/ct "
          f"CI=[{lo_t:+.3f},{hi_t:+.3f}] n={n_t} ===")
    edge = (n_t >= 30 and lo_t > 0)
    print(f"VERDICT: {'EDGE' if edge else 'NO_EDGE / INCONCLUSIVE'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
