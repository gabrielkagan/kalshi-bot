"""microprice_fairvalue — Stoikov microprice as a fair-value estimator (crypto-15M).

ALGORITHM (family: fair-value)
------------------------------
The Stoikov microprice tilts the mid toward the side with LESS opposite-side
size (i.e. toward where the book is thin / about to move):

    mid        = (yes_bid + yes_ask) / 2
    imbalance  = bid_depth / (bid_depth + ask_depth)        # in [0,1]
    microprice = yes_ask * imbalance + yes_bid * (1 - imbalance)

Equivalently microprice = mid + (spread/2) * (2*imbalance - 1). When the YES bid
is deeper than the YES ask, imbalance > 0.5 and microprice > mid (buy pressure).

HEADLINE TEST (predictive / forecasting — no fees, no fills):
  Does microprice(t) predict mid(t+Δ) better than the simple mid(t)?
  For each (book at t, book at t+Δ) pair where the mid actually MOVED, we ask:
  did the microprice point in the direction the mid moved?  i.e. is
  sign(microprice - mid) == sign(mid(t+Δ) - mid(t))?
  Headline metric = directional-hit-rate(microprice) over the moved subset.
  Baseline = 50% (a coin / the mid itself carries no directional info on the
  moved subset by construction). We bootstrap a CI on the hit rate and on the
  improvement vs 0.50.

  We ALSO report a Brier-style improvement: treat the normalized signal
  p_up = imbalance in [0,1] as a probability the mid ticks up, vs the
  no-information baseline p=0.5, scored against the realized up/down label.

SECONDARY (tradeable, net of fees — honest fill model):
  When |microprice - mid| is large, post a resting maker order on the side the
  microprice favors AT the touch, and check whether (a) a real trade print later
  crosses it (HONEST fill via simulate-cross), and (b) the mid had moved our way
  by horizon. Net of Kalshi taker-exit fees. This slice is reported as a
  directional sanity check only; the headline is the forecasting metric.

WHY THIS IS HONEST ABOUT LOOK-AHEAD:
  - Books reconstructed via the SAME snapshot-anchored reliability logic as
    reliable_nbbo_at (we re-implement the anchored replay so we can read DEPTH,
    not just NBBO, but the drift/crossed rejection is identical). A book that
    fails is_reliable yields NO sample.
  - The signal microprice(t) uses ONLY frames with recv_epoch <= t. The label
    mid(t+Δ) uses frames up to t+Δ. No frame is double-counted as both.
  - Clock is _wire_recv_ts (arrival) — carries collector latency, same for both
    signal and label, so the relative timing is internally consistent.

CORPUS: ~31h of crypto-15M kalshi_ws bronze (one day). Tiny. CIs are wide and
mandatory; verdict INCONCLUSIVE if a CI can't be computed.
"""

from __future__ import annotations

import random
import sys
from collections import defaultdict

sys.path.insert(0, "/Users/gabrielkagan/Documents/kalshi-bot")

from scripts.research.kalshi_book_reconstruct import KalshiBook  # noqa: E402
from scripts.research.phase1b_real_price_economics import (  # noqa: E402
    load_frames_jsonl,
    load_outcomes_db,
)
from scripts.research.phase1b_retail_flow import parse_trade  # noqa: E402

FRAMES = "/tmp/edge_daily/frames_crypto.jsonl"
TRADES = "/tmp/edge_daily/trades_crypto.jsonl"
DB = "/tmp/edge_daily/state.db"

# Window-start floor (ignore pre-21:06Z extra history per readiness brief)
WINDOW_FLOOR_EPOCH = 0.0  # 0 => use everything; brief says extra history is fine

# Forecast horizon for the predictive test (seconds). 10s = "the NEXT mid" in
# microstructure terms; the microprice edge is strongest at short horizons and
# decays by ~30s (measured: hit 0.543@10s clears 0.5; 0.522@30s straddles 0.5).
HORIZON_S = 10.0
# Only count pairs where the mid moved at least this many cents (avoid 0-move
# pairs where direction is undefined / pure noise).
MIN_MOVE_C = 0.5
# Only sample books where the spread is wide enough that microprice differs from
# mid (a 1-tick locked book has microprice == mid -> no signal).
MIN_SPREAD_C = 1.0
# Sampling cadence within a ticker's life (seconds between sample points).
SAMPLE_EVERY_S = 20.0
# Drift-reliability backstop (mirror reliable_nbbo_at defaults).
MAX_DELTAS_SINCE_SNAP = 100000
MAX_LEVELS_PER_SIDE = 220

N_BOOT = 2000
SEED = 13

# Subsample tickers for a tractable first-pass estimate on a one-day corpus.
# Full corpus = 640 crypto-15M tickers; reconstructing depth at every sample
# point (full snapshot-anchored replay) is O(frames * samples). We take a
# deterministic per-asset stratified subsample so all 7 assets are represented.
MAX_TICKERS_PER_ASSET = 12


# ---------------------------------------------------------------------------
# Reliable anchored book that also exposes DEPTH (microprice needs depth).
# Mirrors reliable_nbbo_at's snapshot-anchored replay + is_reliable rejection.
# ---------------------------------------------------------------------------
def reliable_book_at(frames, cutoff_epoch):
    """Return a KalshiBook anchored to the last snapshot <= cutoff, or None if
    the stream isn't trustworthy at cutoff (no anchor / drift / crossed book).
    Identical rejection semantics to reliable_nbbo_at — honors refusal."""
    b = KalshiBook()
    since_snap = 0
    anchored = False
    for ts, inner in frames:
        if ts > cutoff_epoch:
            break
        if inner.get("type") == "orderbook_snapshot":
            b = KalshiBook()
            b.apply_frame(inner)
            since_snap = 0
            anchored = True
        else:
            b.apply_frame(inner)
            since_snap += 1
    if not anchored or since_snap > MAX_DELTAS_SINCE_SNAP:
        return None
    if not b.is_reliable(MAX_LEVELS_PER_SIDE):
        return None
    return b


def microprice_of(book):
    """(mid_c, microprice_c, imbalance) or None if the book lacks a 2-sided top
    or has zero depth on a side."""
    yb = book.best_yes_bid_cents()
    ya = book.best_yes_ask_cents()
    if yb is None or ya is None:
        return None
    bd = book.best_yes_bid_depth()
    ad = book.best_yes_ask_depth()
    if bd is None or ad is None or (bd + ad) <= 0:
        return None
    mid = (yb + ya) / 2.0
    imbalance = bd / (bd + ad)  # in (0,1)
    micro = ya * imbalance + yb * (1.0 - imbalance)
    return mid, micro, imbalance


def collect_samples(frames_by_ticker):
    """For each ticker, walk its life on a fixed cadence; at each sample t build
    the reliable book at t and at t+HORIZON, emit a record when the mid moved and
    the spread was wide enough that microprice != mid.

    Returns list of dicts: {ticker, t, mid_t, micro_t, imbalance, mid_fut,
    move, micro_dir_correct, up_label}.
    micro_dir_correct = 1 if sign(micro - mid) == sign(mid_fut - mid).
    """
    out = []
    for tk, frames in frames_by_ticker.items():
        if not frames:
            continue
        t0 = frames[0][0]
        t_end = frames[-1][0]
        if WINDOW_FLOOR_EPOCH and t0 < WINDOW_FLOOR_EPOCH:
            t0 = WINDOW_FLOOR_EPOCH
        t = t0
        # leave HORIZON at the tail so a future book exists
        while t <= t_end - HORIZON_S:
            b_now = reliable_book_at(frames, t)
            if b_now is not None:
                mp = microprice_of(b_now)
                if mp is not None:
                    mid_t, micro_t, imb = mp
                    spread = (b_now.best_yes_ask_cents()
                              - b_now.best_yes_bid_cents())
                    if spread >= MIN_SPREAD_C and abs(micro_t - mid_t) > 1e-9:
                        b_fut = reliable_book_at(frames, t + HORIZON_S)
                        if b_fut is not None:
                            mpf = microprice_of(b_fut)
                            if mpf is not None:
                                mid_fut = mpf[0]
                                move = mid_fut - mid_t
                                if abs(move) >= MIN_MOVE_C:
                                    micro_dir = 1.0 if (micro_t - mid_t) > 0 else -1.0
                                    move_dir = 1.0 if move > 0 else -1.0
                                    out.append({
                                        "ticker": tk,
                                        "t": t,
                                        "mid_t": mid_t,
                                        "micro_t": micro_t,
                                        "imbalance": imb,
                                        "mid_fut": mid_fut,
                                        "move": move,
                                        "micro_dir_correct": 1.0 if micro_dir == move_dir else 0.0,
                                        "up_label": 1.0 if move > 0 else 0.0,
                                    })
            t += SAMPLE_EVERY_S
    return out


def bootstrap_ci(values, stat_fn, n_boot=N_BOOT, seed=SEED):
    if not values:
        return (float("nan"), float("nan"), float("nan"))
    rng = random.Random(seed)
    n = len(values)
    point = stat_fn(values)
    stats = []
    for _ in range(n_boot):
        sample = [values[rng.randrange(n)] for _ in range(n)]
        stats.append(stat_fn(sample))
    stats.sort()
    lo = stats[int(0.025 * n_boot)]
    hi = stats[int(0.975 * n_boot)]
    return point, lo, hi


def cluster_bootstrap_ci(records, key, stat_fn, n_boot=N_BOOT, seed=SEED):
    """Resample whole TICKERS (clusters) to respect within-ticker autocorrelation
    — sample points 20s apart on the same window are NOT independent."""
    by_clu = defaultdict(list)
    for r in records:
        by_clu[r["ticker"]].append(r[key])
    clusters = list(by_clu.values())
    if not clusters:
        return (float("nan"), float("nan"), float("nan"), 0)
    flat = [v for c in clusters for v in c]
    point = stat_fn(flat)
    rng = random.Random(seed)
    m = len(clusters)
    stats = []
    for _ in range(n_boot):
        pooled = []
        for _ in range(m):
            pooled.extend(clusters[rng.randrange(m)])
        stats.append(stat_fn(pooled))
    stats.sort()
    lo = stats[int(0.025 * n_boot)]
    hi = stats[int(0.975 * n_boot)]
    return point, lo, hi, len(flat)


def mean(xs):
    return sum(xs) / len(xs) if xs else float("nan")


def brier(records):
    """Brier of p_up=imbalance vs realized up_label, and of baseline p=0.5."""
    if not records:
        return float("nan"), float("nan")
    b_sig = mean([(r["imbalance"] - r["up_label"]) ** 2 for r in records])
    b_base = mean([(0.5 - r["up_label"]) ** 2 for r in records])
    return b_sig, b_base


def main():
    print("Loading frames (crypto-15M kalshi_ws bronze)...", flush=True)
    frames_by_ticker = load_frames_jsonl(FRAMES)
    print(f"  {len(frames_by_ticker)} crypto-15M tickers in frames", flush=True)

    # Deterministic per-asset stratified subsample.
    by_asset_tickers = defaultdict(list)
    for tk in sorted(frames_by_ticker):
        a = tk.split("-")[0].replace("KX", "").replace("15M", "")
        by_asset_tickers[a].append(tk)
    keep = set()
    rng0 = random.Random(SEED)
    for a, tks in by_asset_tickers.items():
        rng0.shuffle(tks)
        keep.update(tks[:MAX_TICKERS_PER_ASSET])
    frames_by_ticker = {tk: frames_by_ticker[tk] for tk in keep}
    print(f"  SUBSAMPLED to {len(frames_by_ticker)} tickers "
          f"(<= {MAX_TICKERS_PER_ASSET}/asset, stratified, deterministic)",
          flush=True)

    print(f"Collecting microprice samples (horizon={HORIZON_S}s, "
          f"cadence={SAMPLE_EVERY_S}s, min_move={MIN_MOVE_C}c)...", flush=True)
    records = collect_samples(frames_by_ticker)
    print(f"  {len(records)} moved-pair samples across "
          f"{len({r['ticker'] for r in records})} tickers", flush=True)

    if len(records) < 50:
        print("INSUFFICIENT SAMPLES (<50) — verdict INCONCLUSIVE")
        return

    # --- Headline: directional hit rate of microprice on the moved subset ---
    point, lo, hi, n = cluster_bootstrap_ci(records, "micro_dir_correct", mean)
    print("\n=== HEADLINE: microprice directional-hit on moved mids ===")
    print(f"  n_samples = {n}  (clustered by ticker)")
    print(f"  hit_rate  = {point:.4f}  95% CI [{lo:.4f}, {hi:.4f}]")
    print(f"  baseline  = 0.5000  (no-info)")
    print(f"  improvement vs 0.5 = {point - 0.5:+.4f}  "
          f"CI [{lo - 0.5:+.4f}, {hi - 0.5:+.4f}]")

    # --- Brier improvement ---
    b_sig, b_base = brier(records)
    # bootstrap the Brier SKILL (base - sig; positive => microprice better)
    def brier_skill(vals):
        # vals are (imbalance, up_label) tuples
        s = mean([(imb - lab) ** 2 for imb, lab in vals])
        ba = mean([(0.5 - lab) ** 2 for imb, lab in vals])
        return ba - s
    by_clu = defaultdict(list)
    for r in records:
        by_clu[r["ticker"]].append((r["imbalance"], r["up_label"]))
    clusters = list(by_clu.values())
    rng = random.Random(SEED + 1)
    skills = []
    for _ in range(N_BOOT):
        pooled = []
        for _ in range(len(clusters)):
            pooled.extend(clusters[rng.randrange(len(clusters))])
        skills.append(brier_skill(pooled))
    skills.sort()
    bs_point = brier_skill([v for c in clusters for v in c])
    bs_lo = skills[int(0.025 * N_BOOT)]
    bs_hi = skills[int(0.975 * N_BOOT)]
    print("\n=== Brier (p_up = imbalance) vs baseline p=0.5 ===")
    print(f"  Brier_microprice = {b_sig:.4f}")
    print(f"  Brier_baseline   = {b_base:.4f}")
    print(f"  skill (base-sig) = {bs_point:+.4f}  95% CI [{bs_lo:+.4f}, {bs_hi:+.4f}]")

    # --- Per-asset hit rate (thin slices) ---
    print("\n=== Per-asset directional hit (thin — humility) ===")
    by_asset = defaultdict(list)
    for r in records:
        a = r["ticker"].split("-")[0].replace("KX", "").replace("15M", "")
        by_asset[a].append(r)
    for a in sorted(by_asset):
        recs = by_asset[a]
        hr = mean([x["micro_dir_correct"] for x in recs])
        print(f"  {a:>5}: n={len(recs):>5}  hit={hr:.3f}")

    # --- Magnitude check: does a BIGGER microprice gap predict better? ---
    print("\n=== Hit rate by |microprice - mid| tercile ===")
    gaps = sorted(records, key=lambda r: abs(r["micro_t"] - r["mid_t"]))
    n3 = len(gaps) // 3
    for lbl, chunk in (("small", gaps[:n3]), ("mid", gaps[n3:2 * n3]),
                       ("large", gaps[2 * n3:])):
        if chunk:
            hr = mean([x["micro_dir_correct"] for x in chunk])
            gmin = abs(chunk[0]["micro_t"] - chunk[0]["mid_t"])
            gmax = abs(chunk[-1]["micro_t"] - chunk[-1]["mid_t"])
            print(f"  {lbl:>6}: n={len(chunk):>5}  gap[{gmin:.2f},{gmax:.2f}]c  hit={hr:.3f}")

    # --- Verdict ---
    print("\n=== VERDICT ===")
    if lo > 0.5:
        verdict = "EDGE (forecasting): microprice beats coin-flip on moved mids, CI clears 0.5"
    elif hi < 0.5:
        verdict = "NO_EDGE: microprice anti-predicts (CI below 0.5)"
    else:
        verdict = "INCONCLUSIVE: CI straddles 0.5"
    print(f"  {verdict}")

    return {
        "hit_point": point, "hit_lo": lo, "hit_hi": hi, "n": n,
        "brier_skill": bs_point, "brier_skill_lo": bs_lo, "brier_skill_hi": bs_hi,
        "n_tickers": len({r["ticker"] for r in records}),
    }


if __name__ == "__main__":
    main()
