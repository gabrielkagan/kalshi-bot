"""depth_asymmetry_settlement_pin_maker — pin-risk / book-shape terminal anchoring (maker).

MECHANISM (distinct from gamma-skew quoting & taker-fade):
  Near close, the resting DEPTH asymmetry between the best YES bid and the best NO
  bid encodes the crowd's terminal conviction, but the BEST-PRICE often lags depth.
  When best_yes_bid_depth >> best_no_bid_depth (heavy resting demand to be long YES)
  yet the yes price is still cheap (<55c), JOIN the heavy side as a MAKER one tick
  inside, betting the depth wall both protects queue position and signals the
  resolving side. Symmetric on the NO side (heavy no-depth + cheap no).

DECISION: close-240s. Reconstruct a RELIABLE (snapshot-anchored, uncrossed) book.
  DR = yes_bid_depth / (yes_bid_depth + no_bid_depth).
SIGNAL: DR > 0.75 (heavy YES) AND best_yes_bid_cents < 55  -> post a resting YES bid
        at best_yes_bid+1c.   OR   DR < 0.25 (heavy NO) AND best_no_bid_cents < 55
        -> post a resting NO bid at best_no_bid+1c.
FILL: HONEST cross-fill via mm_markout_evaluator primitives (a resting maker fills
  ONLY when a real print crosses it). first_yes_bid_fill_ts / first_no_bid_fill_ts.
LABEL: terminal book outcome = sign of book-mid at close_epoch_from_ticker
  (>50c -> yes, <50c -> no; ambiguous mids near 50 dropped). Derived from the
  TERMINAL BOOK, not the (sparse, just-settling) in-window DB.
FEES: ceil(0.07 * C * P * (1-P)) cents/contract on FILL, rebate=0.
HEADLINE: per-POSTED net cents (free unfilled = 0) AND per-FILLED net cents
  (adverse-selection check). Block bootstrap clustered by ticker. Fill rate + DR dist.

The per-posted-vs-per-filled split is the built-in assassin: if the filled-leg EV
is still negative net of fees, the depth wall did NOT de-toxify the fill and this
dies like the prior maker deaths. If filled-leg EV is positive with a CI that
excludes zero, the wall is informed and this is a real (if narrow) edge.

NON-NEGOTIABLES honored: real fees (ceil form), honest cross-fill, snapshot-anchored
reliable books only (drift refused), no look-ahead (signal book strictly <= decision;
fill prints strictly after post; label book at close), block bootstrap by ticker.
"""
from __future__ import annotations

import hashlib
import json
import math
import sys
from collections import defaultdict
from datetime import datetime, timezone

sys.path.insert(0, "/Users/gabrielkagan/Documents/kalshi-bot")

from scripts.research import kalshi_book_reconstruct as kbr
from scripts.research.phase1b_real_price_economics import (
    _is_crypto_15m,
    close_epoch_from_ticker,
)
from scripts.research.phase1b_live_shadow import load_trades_by_ticker
from scripts.research.mm_markout_evaluator import (
    first_yes_bid_fill_ts,
    first_no_bid_fill_ts,
)

FRAMES = "/tmp/edge_daily/frames_crypto.jsonl"
TRADES = "/tmp/edge_daily/trades_crypto.jsonl"

DECISION_OFFSET_S = 240          # close - 240s decision time
DR_HI = 0.75                     # heavy-YES threshold
DR_LO = 0.25                     # heavy-NO threshold
CHEAP_MAX_C = 55.0               # heavy side must still be cheap (< 55c)
TICK_C = 1.0                     # one tick inside
N_BOOT = 2000

# Memory-frugal streaming: the full 4.4GB / 9.77M-row corpus parsed whole thrashes
# system Python (10+GB RSS). We (a) keep only frames in the relevant time window
# per ticker [close-WINDOW_BACK_S, close+WINDOW_FWD_S] — covers the decision book
# (close-240) AND the terminal label book (close), discarding the early-window
# frames that this terminal-anchoring algo never reads; and (b) subsample tickers
# by a deterministic hash so a first pass fits in RAM. Both are reported.
WINDOW_BACK_S = 900              # retain frames from close-900s (covers a snapshot anchor)
WINDOW_FWD_S = 120               # ... through close+120s (terminal label book)
TICKER_SUBSAMPLE = 1.0           # 1.0 = all tickers; <1 keeps hash(ticker) fraction


def _epoch_iso(iso: str) -> float:
    s = iso.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def _ticker_kept(tk: str, frac: float) -> bool:
    if frac >= 1.0:
        return True
    h = int(hashlib.md5(tk.encode()).hexdigest()[:8], 16) / 0xFFFFFFFF
    return h < frac


def load_frames_windowed(path: str, frac: float) -> dict:
    """Stream the bronze JSONL ONCE, keeping per (subsampled) crypto-15M ticker only
    the frames whose recv-epoch falls in [close-WINDOW_BACK_S, close+WINDOW_FWD_S].
    Returns {ticker: [(recv_epoch, inner)]} sorted ascending. Memory ∝ retained
    frames, not the whole file."""
    frames: dict[str, list] = defaultdict(list)
    close_cache: dict[str, float] = {}
    n_lines = 0
    for line in open(path):
        if not line.strip():
            continue
        n_lines += 1
        try:
            env = json.loads(line)
            inner = json.loads(env["_raw"])
        except (ValueError, KeyError):
            continue
        tk = inner.get("msg", {}).get("market_ticker", "")
        if not _is_crypto_15m(tk) or not _ticker_kept(tk, frac):
            continue
        rts = env.get("_wire_recv_ts")
        if rts is None:
            continue
        ep = _epoch_iso(rts)
        close = close_cache.get(tk)
        if close is None:
            try:
                close = close_epoch_from_ticker(tk)
            except Exception:
                continue
            close_cache[tk] = close
        if ep < close - WINDOW_BACK_S or ep > close + WINDOW_FWD_S:
            continue
        frames[tk].append((ep, inner))
    for tk in frames:
        frames[tk].sort(key=lambda x: x[0])
    print(f"  streamed {n_lines} lines", flush=True)
    return frames


def kalshi_fee_ceil_cents(price_cents: float) -> float:
    """ceil(0.07 * P * (1-P) * 100) cents/contract, P in dollars. The spec's
    conservative per-contract ceil (1-contract rounding) — ruthless on cost."""
    p = price_cents / 100.0
    return math.ceil(7.0 * p * (1.0 - p))


def reliable_book_at(frames, cutoff_epoch, max_deltas_since_snap=100000,
                     max_levels_per_side=220):
    """Snapshot-anchored RELIABLE full book at cutoff (mirrors reliable_nbbo_at's
    refusal logic but returns the KalshiBook so depth accessors are available).
    Returns None if no snapshot anchored, too many deltas piled up, or the book
    fails is_reliable (drifted/crossed). NO look-ahead: only ts <= cutoff applied."""
    b = kbr.KalshiBook()
    since_snap = 0
    anchored = False
    for ts, inner in frames:
        if ts > cutoff_epoch:
            break
        if inner.get("type") == "orderbook_snapshot":
            b = kbr.KalshiBook()
            b.apply_frame(inner)
            since_snap = 0
            anchored = True
        else:
            b.apply_frame(inner)
            since_snap += 1
    if not anchored or since_snap > max_deltas_since_snap:
        return None
    if not b.is_reliable(max_levels_per_side):
        return None
    return b


def terminal_outcome(frames, close_epoch):
    """Label from the TERMINAL BOOK: reliable book at close_epoch, outcome = side
    of the book mid. Returns 'yes' | 'no' | None (no reliable terminal book or
    ambiguous mid within +-1c of 50)."""
    b = reliable_book_at(frames, close_epoch)
    if b is None:
        return None
    yb, ya = b.best_yes_bid_cents(), b.best_yes_ask_cents()
    if yb is None or ya is None:
        return None
    mid = (yb + ya) / 2.0
    if abs(mid - 50.0) <= 1.0:
        return None  # ambiguous — don't fabricate a label
    return "yes" if mid > 50.0 else "no"


def run():
    print("streaming frames (4.4GB, ~9.8M rows; window-trimmed, memory-frugal)...",
          flush=True)
    frames = load_frames_windowed(FRAMES, TICKER_SUBSAMPLE)
    print(f"  crypto-15M tickers retained: {len(frames)} "
          f"(subsample frac={TICKER_SUBSAMPLE}, window=[close-{WINDOW_BACK_S}s,"
          f"close+{WINDOW_FWD_S}s])", flush=True)
    print("loading trades...", flush=True)
    trades = load_trades_by_ticker(TRADES)
    print(f"  tickers with trades: {len(trades)}", flush=True)

    # Per-posting records. Each posting is one (ticker, side) maker order.
    # per-posted PnL: 0 if unfilled (free), settlement net-of-fee if filled.
    # per-filled PnL: settlement net-of-fee (filled subset only).
    posted_records = []   # (ticker, per_posted_pnl_cents)
    filled_records = []   # (ticker, per_filled_pnl_cents)
    dr_values = []
    n_no_terminal = 0
    n_no_signal_book = 0
    n_signal = 0
    n_filled = 0
    n_windows_considered = 0
    side_counts = {"yes": 0, "no": 0}

    for tk, fr in frames.items():
        close = close_epoch_from_ticker(tk)
        decision = close - DECISION_OFFSET_S

        # signal book — strictly <= decision time, reliable only (no look-ahead)
        sb = reliable_book_at(fr, decision)
        if sb is None:
            n_no_signal_book += 1
            continue
        yb = sb.best_yes_bid_cents()
        nb = sb.best_no_bid_cents()
        ybd = sb.best_yes_bid_depth()
        nbd = sb.best_no_bid_depth()
        if None in (yb, nb, ybd, nbd):
            n_no_signal_book += 1
            continue
        denom = ybd + nbd
        if denom <= 0:
            continue
        dr = ybd / denom
        dr_values.append(dr)
        n_windows_considered += 1

        # decide which (if any) heavy side fires
        side = None
        post_price = None
        if dr > DR_HI and yb < CHEAP_MAX_C:
            # heavy YES demand, cheap YES -> post YES bid one tick inside
            side = "yes"
            post_price = yb + TICK_C
        elif dr < DR_LO and nb < CHEAP_MAX_C:
            # heavy NO demand, cheap NO -> post NO bid one tick inside
            side = "no"
            post_price = nb + TICK_C
        if side is None:
            continue
        if not (0.0 < post_price < 100.0):
            continue

        # label from terminal book (independent of signal book)
        outcome = terminal_outcome(fr, close)
        if outcome is None:
            n_no_terminal += 1
            continue

        n_signal += 1
        side_counts[side] += 1

        # honest fill: only if a real print crosses our resting order after post.
        tr = trades.get(tk, [])
        if side == "yes":
            fill_ts = first_yes_bid_fill_ts(tr, decision, post_price)
        else:
            fill_ts = first_no_bid_fill_ts(tr, decision, post_price)

        if fill_ts is None:
            # unfilled -> free; per-posted contributes 0, no filled record
            posted_records.append((tk, 0.0))
            continue

        # filled -> settlement net of fee
        n_filled += 1
        fee = kalshi_fee_ceil_cents(post_price)
        if side == outcome:
            pnl = (100.0 - post_price) - fee
        else:
            pnl = -post_price - fee
        posted_records.append((tk, pnl))
        filled_records.append((tk, pnl))

    # ---- block bootstrap clustered by ticker ------------------------------
    def block_bootstrap_mean_ci(records, n_boot=N_BOOT, seed=20260531):
        """records = list of (ticker, value). Resample TICKERS with replacement
        (the independent unit), pool their values, take the mean. Returns
        (mean, lo, hi, n_values, n_clusters)."""
        import random
        by_tk = defaultdict(list)
        for tk, v in records:
            by_tk[tk].append(v)
        tickers = list(by_tk.keys())
        all_vals = [v for _, v in records]
        n_vals = len(all_vals)
        if n_vals == 0 or len(tickers) == 0:
            return (float("nan"), float("nan"), float("nan"), 0, 0)
        mean = sum(all_vals) / n_vals
        rng = random.Random(seed)
        means = []
        nt = len(tickers)
        for _ in range(n_boot):
            pooled = []
            for _ in range(nt):
                pooled.extend(by_tk[tickers[rng.randrange(nt)]])
            if pooled:
                means.append(sum(pooled) / len(pooled))
        means.sort()
        lo = means[int(0.025 * len(means))]
        hi = means[min(len(means) - 1, int(0.975 * len(means)))]
        return (mean, lo, hi, n_vals, nt)

    pp_mean, pp_lo, pp_hi, pp_n, pp_clusters = block_bootstrap_mean_ci(posted_records)
    pf_mean, pf_lo, pf_hi, pf_n, pf_clusters = block_bootstrap_mean_ci(filled_records)

    fill_rate = (n_filled / n_signal) if n_signal else float("nan")

    # DR distribution
    dr_values.sort()

    def pct(xs, q):
        if not xs:
            return float("nan")
        return xs[min(len(xs) - 1, int(q * len(xs)))]

    print("\n========== depth_asymmetry_settlement_pin_maker ==========")
    print(f"DECISION close-{DECISION_OFFSET_S}s | DR_HI={DR_HI} DR_LO={DR_LO} "
          f"cheap<{CHEAP_MAX_C}c | tick=+{TICK_C}c | fee=ceil(7*P*(1-P))")
    print(f"reliable-signal-book windows considered: {n_windows_considered}")
    print(f"  skipped no/unreliable signal book: {n_no_signal_book}")
    print(f"DR distribution over considered windows: "
          f"n={len(dr_values)} p05={pct(dr_values,0.05):.3f} "
          f"p25={pct(dr_values,0.25):.3f} p50={pct(dr_values,0.50):.3f} "
          f"p75={pct(dr_values,0.75):.3f} p95={pct(dr_values,0.95):.3f}")
    frac_hi = sum(1 for d in dr_values if d > DR_HI) / (len(dr_values) or 1)
    frac_lo = sum(1 for d in dr_values if d < DR_LO) / (len(dr_values) or 1)
    print(f"  frac DR>{DR_HI}: {frac_hi:.3f}  frac DR<{DR_LO}: {frac_lo:.3f}")
    print(f"signals posted (cheap+asymmetric, w/ terminal label): {n_signal} "
          f"(yes={side_counts['yes']} no={side_counts['no']})")
    print(f"  skipped no-terminal-label: {n_no_terminal}")
    print(f"FILLED (honest cross): {n_filled}  fill_rate={fill_rate:.4f}")
    print()
    print(f"PER-POSTED net cents (free unfilled=0): mean={pp_mean:+.3f} "
          f"CI[{pp_lo:+.3f},{pp_hi:+.3f}] n={pp_n} clusters={pp_clusters}")
    print(f"PER-FILLED net cents (adverse-sel check): mean={pf_mean:+.3f} "
          f"CI[{pf_lo:+.3f},{pf_hi:+.3f}] n={pf_n} clusters={pf_clusters}")
    print("==========================================================")

    return {
        "n_signal": n_signal,
        "n_filled": n_filled,
        "fill_rate": fill_rate,
        "per_posted": (pp_mean, pp_lo, pp_hi, pp_n, pp_clusters),
        "per_filled": (pf_mean, pf_lo, pf_hi, pf_n, pf_clusters),
        "dr_n": len(dr_values),
    }


if __name__ == "__main__":
    run()
