"""order_flow_imbalance — OFI short-horizon mid predictor for Kalshi crypto-15M.

ALGORITHM (family: signal)
--------------------------
From the reconstructed Kalshi book deltas we compute the standard Cont-Kukanov
Order-Flow Imbalance (OFI) on the YES side:

    OFI contribution at each book event =
        + dBid_size  if best bid price unchanged, +best-bid-size if bid price rose,
                     -prev-bid-size if bid price fell
        - dAsk_size  (symmetric on the ask)

Aggregated over a short lookback window, OFI is the net signed pressure on the
top of book. We then regress the NEXT-horizon mid move (5-30s) on OFI and ask:
does OFI predict where the Kalshi mid goes next? Headline = out-of-sample
correlation of (predicted move | realized move), with a tradeability check:
a taker rule that crosses the spread only when |predicted move| > round-trip
taker fee, marked to the realized mid.

HONESTY / LOOK-AHEAD CONTROLS
-----------------------------
- Book reconstructed ONLY from events at/before each decision time (no peeking).
  We honor `KalshiBook.is_reliable` — any decision instant whose book is crossed
  or drift-blown is DROPPED (never trade off a rejected book).
- Train/test split is BY TICKER (disjoint window sets) so the OFI->move
  regression coefficient is fit on tickers never used to score the headline.
- Mid = (yes_bid + yes_ask)/2 in cents. Realized horizon move is the mid `H`
  seconds later, using only book state up to that later time.
- Fees: taker = ceil(0.07 * count * P * (100-P)/100) cents (bot/models.calculate_fee),
  applied to ENTRY. Maker fee = $0 (we do not claim a maker rebate). The taker
  rule is the tradeability test; we mark the exit to mid (best-case exit, no
  exit-spread cost), so the tradeability PnL is an UPPER bound on a real taker
  round trip. Stated explicitly so the verdict stays honest.
- Bootstrap CI (>=2000 resamples) on the headline OOS correlation and on the
  per-signal taker net PnL.

CORPUS: /tmp/edge_daily/frames_crypto.jsonl  (~31h, crypto-15M, kalshi_ws).
"""

from __future__ import annotations

import math
import sys
from collections import defaultdict

sys.path.insert(0, "/Users/gabrielkagan/Documents/kalshi-bot")

import numpy as np

from scripts.research.kalshi_book_reconstruct import KalshiBook
from scripts.research.phase1b_real_price_economics import (
    load_frames_jsonl,
    _is_crypto_15m,
    close_epoch_from_ticker,
)

FRAMES = "/tmp/edge_daily/frames_crypto.jsonl"

# --- knobs ---
OFI_LOOKBACK_S = 10.0     # window over which OFI pressure is accumulated
HORIZON_S = 15.0          # predict mid move this many seconds ahead (5-30s band)
SAMPLE_STEP_S = 5.0       # decision-grid cadence per ticker
MIN_QUOTE_AGE_OK_S = 30.0 # if the freshest quote is older than this, skip instant
TRAIN_FRAC = 0.6          # by-ticker split
MIN_PRICE_C = 3.0         # avoid degenerate 0/1c books
MAX_PRICE_C = 97.0
N_BOOT = 2000
SEED = 7


def taker_fee_cents(price_c: float) -> float:
    """Kalshi taker fee for a 1-contract order at price_c (ceil to whole cent)."""
    p = max(0.0, min(100.0, price_c))
    return math.ceil(0.07 * 1 * p * (100.0 - p) / 100.0)


def _signed_ofi_step(prev_bid, prev_bid_sz, prev_ask, prev_ask_sz,
                     bid, bid_sz, ask, ask_sz):
    """One OFI increment from (prev top of book) -> (new top of book).
    Cont-Kukanov convention on the YES side."""
    e = 0.0
    # bid side
    if prev_bid is None or bid is None:
        pass
    elif bid > prev_bid:
        e += bid_sz
    elif bid < prev_bid:
        e -= prev_bid_sz
    else:
        e += (bid_sz - prev_bid_sz)
    # ask side (selling pressure reduces OFI)
    if prev_ask is None or ask is None:
        pass
    elif ask < prev_ask:
        e -= ask_sz
    elif ask > prev_ask:
        e += prev_ask_sz
    else:
        e -= (ask_sz - prev_ask_sz)
    return e


def build_samples_for_ticker(frames):
    """Replay a ticker's frames. Returns list of dicts:
    {t, mid, ofi (lookback-accumulated), entry_yes_bid, entry_yes_ask}.
    All quantities use ONLY data up to time t (no look-ahead within the build)."""
    b = KalshiBook()
    prev_top = None  # (bid, bid_sz, ask, ask_sz)
    # rolling OFI events: list of (t, ofi_increment)
    ofi_events = []
    # book-state timeline sampled at each event so we can look up mid later
    timeline = []  # (t, bid, ask, reliable)
    last_event_t = None

    for t, inner in frames:
        b.apply_frame(inner)
        last_event_t = t
        bid = b.best_yes_bid_cents()
        ask = b.best_yes_ask_cents()
        bid_sz = b.best_yes_bid_depth() or 0.0
        ask_sz = b.best_yes_ask_depth() or 0.0
        reliable = b.is_reliable()
        if bid is not None and ask is not None and prev_top is not None and reliable:
            pb, pbz, pa, paz = prev_top
            inc = _signed_ofi_step(pb, pbz, pa, paz, bid, bid_sz, ask, ask_sz)
            ofi_events.append((t, inc))
        if bid is not None and ask is not None:
            prev_top = (bid, bid_sz, ask, ask_sz)
        timeline.append((t, bid, ask, reliable))

    if len(timeline) < 5:
        return []

    tl_t = np.array([x[0] for x in timeline])
    # mid lookup helper: latest reliable book at or before query time
    def mid_at(qt):
        idx = np.searchsorted(tl_t, qt, side="right") - 1
        if idx < 0:
            return None, None
        # walk back to the most recent reliable, non-None book
        j = idx
        while j >= 0:
            _, bd, ak, rel = timeline[j]
            if bd is not None and ak is not None and rel:
                age = qt - timeline[j][0]
                if age > MIN_QUOTE_AGE_OK_S:
                    return None, None
                return (bd + ak) / 2.0, (bd, ak)
            j -= 1
        return None, None

    oe_t = np.array([x[0] for x in ofi_events]) if ofi_events else np.array([])
    oe_v = np.array([x[1] for x in ofi_events]) if ofi_events else np.array([])

    samples = []
    if len(timeline) == 0:
        return samples
    t0 = timeline[0][0]
    t_end = timeline[-1][0]
    qt = t0 + OFI_LOOKBACK_S
    while qt + HORIZON_S <= t_end:
        mid0, qb = mid_at(qt)
        if mid0 is None:
            qt += SAMPLE_STEP_S
            continue
        bd, ak = qb
        entry_price = ak  # taker BUY YES crosses to the ask
        if not (MIN_PRICE_C <= entry_price <= MAX_PRICE_C):
            qt += SAMPLE_STEP_S
            continue
        # accumulate OFI over [qt-lookback, qt]
        if len(oe_t):
            lo = np.searchsorted(oe_t, qt - OFI_LOOKBACK_S, side="left")
            hi = np.searchsorted(oe_t, qt, side="right")
            ofi = float(oe_v[lo:hi].sum())
        else:
            ofi = 0.0
        # realized future mid
        midH, _ = mid_at(qt + HORIZON_S)
        if midH is None:
            qt += SAMPLE_STEP_S
            continue
        samples.append({
            "t": qt, "mid": mid0, "ofi": ofi,
            "yes_bid": bd, "yes_ask": ak,
            "future_move": midH - mid0,
        })
        qt += SAMPLE_STEP_S
    return samples


def bootstrap_ci(vals, fn, n=N_BOOT, seed=SEED):
    rng = np.random.default_rng(seed)
    vals = np.asarray(vals)
    if len(vals) < 3:
        return (float("nan"), float("nan"))
    stats = []
    idx = np.arange(len(vals))
    for _ in range(n):
        s = rng.choice(idx, size=len(idx), replace=True)
        stats.append(fn(vals[s]))
    return (float(np.nanpercentile(stats, 2.5)),
            float(np.nanpercentile(stats, 97.5)))


def bootstrap_corr_ci(x, y, n=N_BOOT, seed=SEED):
    rng = np.random.default_rng(seed)
    x = np.asarray(x); y = np.asarray(y)
    idx = np.arange(len(x))
    stats = []
    for _ in range(n):
        s = rng.choice(idx, size=len(idx), replace=True)
        xs, ys = x[s], y[s]
        if xs.std() < 1e-12 or ys.std() < 1e-12:
            continue
        stats.append(float(np.corrcoef(xs, ys)[0, 1]))
    if len(stats) < 10:
        return (float("nan"), float("nan"))
    return (float(np.percentile(stats, 2.5)), float(np.percentile(stats, 97.5)))


def main():
    print("Loading frames (this is a 4.4GB file, parsing crypto-15M)...", flush=True)
    frames_by_ticker = load_frames_jsonl(FRAMES)
    tickers = sorted(frames_by_ticker.keys())
    print(f"crypto-15M tickers: {len(tickers)}", flush=True)

    # by-ticker train/test split
    rng = np.random.default_rng(SEED)
    perm = rng.permutation(len(tickers))
    n_train = int(TRAIN_FRAC * len(tickers))
    train_tk = {tickers[i] for i in perm[:n_train]}
    test_tk = {tickers[i] for i in perm[n_train:]}

    train_samples, test_samples = [], []
    n_proc = 0
    for tk, fr in frames_by_ticker.items():
        s = build_samples_for_ticker(fr)
        if not s:
            continue
        (train_samples if tk in train_tk else test_samples).extend(s)
        n_proc += 1
    print(f"tickers yielding samples: {n_proc}", flush=True)
    print(f"train samples: {len(train_samples)}  test samples: {len(test_samples)}",
          flush=True)

    if len(train_samples) < 50 or len(test_samples) < 50:
        print("INSUFFICIENT samples for a split regression.")
        return {"verdict": "DATA_GAP", "n": len(train_samples) + len(test_samples)}

    Xtr = np.array([s["ofi"] for s in train_samples])
    Ytr = np.array([s["future_move"] for s in train_samples])
    # OLS slope/intercept: move = a + b*ofi
    if Xtr.std() < 1e-12:
        print("OFI has no variance in train — DATA_GAP")
        return {"verdict": "DATA_GAP", "n": len(train_samples)}
    b_slope, a_int = np.polyfit(Xtr, Ytr, 1)
    in_corr = float(np.corrcoef(Xtr, Ytr)[0, 1])
    print(f"\nIN-SAMPLE (train): slope={b_slope:.6e} cents/OFI  intercept={a_int:.4f}c  "
          f"corr(OFI, move)={in_corr:.4f}  n={len(Xtr)}")

    # OUT-OF-SAMPLE scoring
    Xte = np.array([s["ofi"] for s in test_samples])
    Yte = np.array([s["future_move"] for s in test_samples])
    pred = a_int + b_slope * Xte
    oos_corr = float(np.corrcoef(pred, Yte)[0, 1]) if (pred.std() > 1e-12 and Yte.std() > 1e-12) else float("nan")
    oos_ofi_corr = float(np.corrcoef(Xte, Yte)[0, 1]) if (Xte.std() > 1e-12 and Yte.std() > 1e-12) else float("nan")
    print(f"OUT-OF-SAMPLE (test): corr(pred, move)={oos_corr:.4f}  "
          f"corr(OFI, move)={oos_ofi_corr:.4f}  n={len(Xte)}")
    lo, hi = bootstrap_corr_ci(Xte, Yte)
    print(f"  bootstrap 95% CI on OOS corr(OFI, move): [{lo:.4f}, {hi:.4f}]")

    # ---------- TRADEABILITY: taker rule on the TEST set ----------
    # take when |predicted move| > round-trip taker fee (entry + a phantom exit fee floor).
    # We charge ENTRY taker fee only and mark exit to mid (best-case exit), so this
    # net-PnL is an UPPER bound on a real round trip. Direction = sign(pred).
    per_signal_net = []
    n_trades = 0
    for s in test_samples:
        p = a_int + b_slope * s["ofi"]
        entry = s["yes_ask"] if p > 0 else s["yes_bid"]
        if entry is None:
            continue
        fee = taker_fee_cents(entry)
        if abs(p) <= fee:
            continue  # not worth crossing the spread
        # realized signed PnL of taking direction sign(p), marked to mid move,
        # minus the spread we paid on entry (taker crosses half-spread vs mid) and entry fee.
        spread_half = abs(s["yes_ask"] - s["yes_bid"]) / 2.0
        direction = 1.0 if p > 0 else -1.0
        gross = direction * s["future_move"]      # mid-to-mid move in our favor
        net = gross - spread_half - fee           # pay half-spread to enter + taker fee; exit at mid
        per_signal_net.append(net)
        n_trades += 1

    if n_trades >= 30:
        arr = np.array(per_signal_net)
        mean_net = float(arr.mean())
        clo, chi = bootstrap_ci(arr, lambda v: float(np.mean(v)))
        print(f"\nTAKER RULE (|pred|>fee): n_trades={n_trades}  "
              f"mean net PnL/signal={mean_net:+.3f}c  95% CI [{clo:+.3f}, {chi:+.3f}]c")
    else:
        mean_net = float("nan"); clo = chi = float("nan")
        print(f"\nTAKER RULE: only {n_trades} signals fired — too few to bootstrap.")

    # Decide headline = OOS corr(OFI, move) and report the taker net as the
    # tradeability gate. EDGE only if OOS corr CI excludes 0 AND taker net CI
    # excludes 0 on the favorable side.
    edge_corr = (not math.isnan(lo)) and (lo > 0 or hi < 0)
    edge_trade = (n_trades >= 30) and (not math.isnan(clo)) and (clo > 0)
    print(f"\nedge_corr(CI excl 0)={edge_corr}  edge_trade(net CI>0)={edge_trade}")

    return {
        "oos_corr": oos_ofi_corr, "oos_corr_ci": (lo, hi),
        "in_corr": in_corr, "slope": b_slope,
        "n_test": len(Xte), "n_train": len(Xtr),
        "taker_n": n_trades, "taker_mean_net": mean_net,
        "taker_ci": (clo, chi),
        "edge_corr": edge_corr, "edge_trade": edge_trade,
    }


if __name__ == "__main__":
    out = main()
    print("\nRESULT:", out)
