"""venue_consensus_strike_basis — cross-venue underlying-vs-implied basis (settlement).

Family: cross-venue underlying-vs-implied basis (settlement).

MECHANISM
---------
At decision time T = close - L seconds (L in {120, 60, 30}) for each Kalshi
crypto-15M window, build a robust CONSENSUS underlying:

    consensus = median( mid_at(kraken,T), mid_at(bitstamp,T), mid_at(gemini,T) )

over the reliable venues (>=2 required). Apply an AGREEMENT gate: the max
pairwise venue gap (in bps of consensus) must be < AGREE_TOL_BPS, else the
venues disagree (stale-quote artifact) and we DISCARD the window — this is the
guard that killed the naive "spot out-predicts market" idea.

Compute a fair value for the binary "settles above strike":

    sigma_ps = per-second stdev of the consensus log-returns sampled over a
               trailing SIGMA_WINDOW_S lookback (sampled every SIGMA_STEP_S)
    sigma_total = sigma_ps * sqrt(time_left_seconds)        (relative vol)
    FV       = Phi( (consensus - strike) / (sigma_total * strike) )

(FV is the model probability YES settles above strike. denom == 0 -> skip.)

Enter TAKER toward FV iff venues AGREE AND |FV*100 - kalshi_reliable_mid_cents|
>= EDGE_MIN_CENTS:
    FV*100 > kalshi_mid + edge  -> market too cheap on YES -> BUY YES (taker, k_ask)
    FV*100 < kalshi_mid - edge  -> market too rich on YES  -> BUY NO  (taker, 100-k_bid)

LABEL: derived from the TERMINAL Kalshi reliable book (mid at close_epoch), NOT
the DB (in-window outcomes are sparse). YES settles iff terminal reliable mid > 50.

FILLS: TAKER only (per spec). Cross the Kalshi reliable book at the ask (YES) /
no-ask (NO). No resting-maker variant — the spec is taker. A taker fill is
honest: the ask is real liquidity standing at the decision time.

FEES: ceil(0.07 * C * P * (1-P)) cents/contract, P = entry/100. Maker rebate 0
(N/A — taker only). PnL = (100*win - entry) - fee.

CI: cluster bootstrap by (asset, window-ticker) — the true independent unit.
EDGE only if the net-of-fee mean CI lower bound > 0.

DATA: BTC/ETH/SOL/XRP have >=2 venues. HYPE partial (kraken+bitstamp). DOGE
(kraken+gemini) / BNB (kraken only) sparse -> per-asset n reported; assets that
never reach >=2 reliable venues flagged DATA_GAP.

NO LOOK-AHEAD: consensus + sigma use venue frames with ts<=T; the label book is
the terminal Kalshi book at close (strictly separate). Decision book is read at
the decision cutoff only.

Local corpus only (~31h, 2026-05-30T10Z -> 05-31T17Z). Wide CIs, humility.
"""

from __future__ import annotations

import sys
sys.path.insert(0, "/Users/gabrielkagan/Documents/kalshi-bot")

import glob
import json
import math
import random
import re
import subprocess
from collections import defaultdict
from datetime import datetime, timezone
from statistics import median, pstdev

import scripts.research.kalshi_book_reconstruct as kbr
from scripts.research.phase1b_real_price_economics import (
    close_epoch_from_ticker,
    load_frames_jsonl,
    load_outcomes_db,
)
from scripts.research.venue_book_reconstruct import (
    VENUE_SYMBOLS,
    BitstampBook,
    GeminiBook,
    KrakenBook,
    parse_envelope as venue_parse_envelope,
)

FRAMES = "/tmp/edge_daily/frames_crypto.jsonl"
DB = "/tmp/edge_daily/state.db"
VENUE_ROOT = "/tmp/edge_daily/venue_pull"

# Assets to attempt. Per-asset venue coverage is discovered at load time; any
# asset that never reaches >=2 reliable venues is reported as DATA_GAP.
ASSETS = ["BTC", "ETH", "SOL", "XRP", "HYPE", "DOGE", "BNB"]

VENUE_BOOK = {"kraken": KrakenBook, "bitstamp": BitstampBook, "gemini": GeminiBook}
VENUE_SOURCE = {"kraken": "kraken_ws", "bitstamp": "bitstamp_ws", "gemini": "gemini_ws"}
VENUES = ("kraken", "bitstamp", "gemini")

# Decision offsets (seconds before close) — the spec's L set.
DECISION_OFFSETS = [120.0, 60.0, 30.0]

AGREE_TOL_BPS = 5.0        # max pairwise venue gap (bps of consensus) to trade
EDGE_MIN_CENTS = 3.0       # |FV*100 - kalshi_mid| must clear this (pre-fee gross)
SIGMA_WINDOW_S = 600.0     # lookback window for sigma estimation
SIGMA_STEP_S = 10.0        # sample the consensus mid every 10s for returns

N_BOOT = 2000
RNG = random.Random(20260531)


def _epoch(iso: str) -> float:
    s = iso.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def _fee(price_cents: float) -> float:
    """ceil(0.07 * C * P * (1-P)) cents/contract, C=1, P=price/100."""
    p = price_cents / 100.0
    return math.ceil(0.07 * p * (1.0 - p) * 100.0) / 100.0


def _phi(z: float) -> float:
    """Standard normal CDF."""
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


# ---------------------------------------------------------------------------
# Venue L2 frames for one venue+asset across all local hour-chunks.
# (mirrors the tested loader in venue_microprice_disagree_settlement.py)
# ---------------------------------------------------------------------------
def load_venue_asset_frames(venue: str, asset: str) -> list:
    sym = VENUE_SYMBOLS.get(venue, {}).get(asset)
    if sym is None:
        return []
    src = VENUE_SOURCE[venue]
    pattern = f"{VENUE_ROOT}/{src}/**/*.jsonl.zst"
    out = []
    for f in sorted(glob.glob(pattern, recursive=True)):
        raw = subprocess.run(["zstd", "-dc", f], capture_output=True).stdout.decode(
            "utf-8", "replace"
        )
        for line in raw.splitlines():
            if not line.strip():
                continue
            try:
                ts_iso, inner = venue_parse_envelope(line)
            except (ValueError, KeyError):
                continue
            if venue == "kraken":
                data = [e for e in inner.get("data", []) if e.get("symbol") == sym]
                if not data:
                    continue
                inner = {**inner, "data": data}
            elif venue == "bitstamp":
                if inner.get("channel") != f"order_book_{sym}":
                    continue
            elif venue == "gemini":
                if not (inner.get("type") == "l2_updates"
                        and inner.get("symbol") == sym):
                    continue
            out.append((_epoch(ts_iso), inner))
    out.sort(key=lambda x: x[0])
    return out


def build_venue_mid_series(venue: str, frames: list, step_s: float) -> list:
    """Replay the venue book ONCE forward, sampling the top-of-book mid on a
    fixed `step_s` grid. Returns [(epoch, mid)] sorted ascending. Only emits a
    sample once the book has a baseline (kraken: after a snapshot; gemini: after
    first l2_updates; bitstamp: any frame is a full snapshot).

    O(n) per venue instead of O(n) per decision point. No look-ahead: each grid
    sample uses only frames at/before that grid epoch."""
    if not frames:
        return []
    book = VENUE_BOOK[venue]()
    series: list[tuple[float, float]] = []
    anchored = (venue == "bitstamp")  # bitstamp every frame is a full snapshot
    fi = 0
    n = len(frames)
    last_epoch = frames[-1][0]
    grid_t = frames[0][0]
    while grid_t <= last_epoch + step_s:
        while fi < n and frames[fi][0] <= grid_t:
            _, inner = frames[fi]
            if venue == "kraken" and inner.get("type") == "snapshot":
                anchored = True
            if venue == "gemini" and inner.get("type") == "l2_updates":
                anchored = True
            book.apply_frame(inner)
            fi += 1
        if anchored:
            m = book.mid()
            if m is not None and m > 0:
                series.append((grid_t, m))
        grid_t += step_s
    return series


def series_value_at(series: list, cutoff_epoch: float):
    """Last sampled mid at/before cutoff (no look-ahead). series sorted asc.

    Binary search would be faster, but the per-window venue slices are short
    once we window them; we keep the simple linear scan for clarity and to match
    the tested sibling's posture."""
    val = None
    for ts, m in series:
        if ts > cutoff_epoch:
            break
        val = m
    return val


def consensus_sigma_per_sqrt_sec(venue_series: dict, venues: list,
                                 t_lo: float, t_hi: float):
    """Per-second sigma of the consensus log-returns over [t_lo, t_hi].

    Build the consensus mid at each shared grid epoch in the lookback (median
    over available venues), take consecutive log-returns, return
    stdev(returns) / sqrt(step_s). Uses only data <= t_hi -> no look-ahead.

    Returns None if too few samples for a stable estimate."""
    grid_epochs = set()
    for v in venues:
        for ts, _ in venue_series[v]:
            if t_lo <= ts <= t_hi:
                grid_epochs.add(round(ts, 3))
    if len(grid_epochs) < 3:
        return None
    consensus_path = []
    for ge in sorted(grid_epochs):
        mids = []
        for v in venues:
            m = series_value_at(venue_series[v], ge)
            if m is not None and m > 0:
                mids.append(m)
        if mids:
            consensus_path.append(median(mids))
    if len(consensus_path) < 3:
        return None
    rets = []
    for a, b in zip(consensus_path[:-1], consensus_path[1:]):
        if a > 0 and b > 0:
            rets.append(math.log(b / a))
    if len(rets) < 2:
        return None
    sd = pstdev(rets)
    if sd <= 0:
        return None
    return sd / math.sqrt(SIGMA_STEP_S)


def _is_crypto15m(tk, asset):
    return bool(re.match(rf"KX{asset}15M", tk))


def cluster_bootstrap_mean(per_cluster_vals, n_boot=N_BOOT):
    """Cluster bootstrap by (asset, ticker). Resample CLUSTERS with replacement;
    each cluster contributes all of its trade-net values. Returns
    (point, lo, hi, n_trades)."""
    if not per_cluster_vals:
        return (float("nan"), float("nan"), float("nan"), 0)
    keys = list(per_cluster_vals)
    n_clusters = len(keys)
    flat = [v for k in keys for v in per_cluster_vals[k]]
    point = sum(flat) / len(flat)
    boots = []
    for _ in range(n_boot):
        sampled = []
        for _ in range(n_clusters):
            k = keys[RNG.randrange(n_clusters)]
            sampled.extend(per_cluster_vals[k])
        if sampled:
            boots.append(sum(sampled) / len(sampled))
    boots.sort()
    lo = boots[int(0.025 * len(boots))]
    hi = boots[int(0.975 * len(boots))]
    return point, lo, hi, len(flat)


def run():
    print("Loading Kalshi crypto-15M frames (4.4GB)...", flush=True)
    all_frames = load_frames_jsonl(FRAMES)
    print(f"  loaded {len(all_frames)} tickers", flush=True)

    venue_span_lo = _epoch("2026-05-30T00:00:00Z")
    venue_span_hi = _epoch("2026-05-31T17:59:59Z")

    diag = {
        "n_decisions_total": 0,
        "skip_no_strike": 0,
        "skip_no_kalshi_reliable": 0,
        "skip_no_terminal_label": 0,
        "skip_venue_lt2": 0,
        "skip_agreement_gate": 0,
        "skip_no_sigma": 0,
        "no_edge_gate": 0,
        "signal_fired": 0,
        "venue_reliable_2": 0,
        "venue_reliable_3": 0,
    }

    clusters_net = defaultdict(list)            # (asset,tk) -> [net,...]
    per_asset_clusters = defaultdict(lambda: defaultdict(list))
    asset_coverage = {}                          # asset -> max venues seen
    fired_meta = []

    for asset in ASSETS:
        print(f"[{asset}] loading venue L2...", flush=True)
        vframes = {}
        for venue in VENUES:
            vframes[venue] = load_venue_asset_frames(venue, asset)
            print(f"    {venue}: {len(vframes[venue])} frames", flush=True)
        venues_for_asset = [v for v in VENUES if vframes[v]]
        asset_coverage[asset] = 0
        if len(venues_for_asset) < 2:
            print(f"[{asset}] <2 venues locally -> DATA_GAP", flush=True)
            asset_coverage[asset] = len(venues_for_asset)
            continue

        print(f"[{asset}] building venue mid series...", flush=True)
        venue_series = {}
        for venue in venues_for_asset:
            venue_series[venue] = build_venue_mid_series(
                venue, vframes[venue], SIGMA_STEP_S)
            print(f"    {venue}: {len(venue_series[venue])} grid samples",
                  flush=True)

        tickers = []
        for tk in all_frames:
            if not _is_crypto15m(tk, asset):
                continue
            try:
                ce = close_epoch_from_ticker(tk)
            except Exception:
                continue
            if venue_span_lo <= ce <= venue_span_hi:
                tickers.append((tk, ce))
        tickers.sort(key=lambda x: x[1])
        print(f"[{asset}] candidate windows in span: {len(tickers)}", flush=True)

        outcomes = load_outcomes_db(DB, set(tk for tk, _ in tickers))
        max_venues_seen = 0

        for tk, close_ep in tickers:
            fr = all_frames.get(tk)
            if not fr:
                continue

            strike = None
            if tk in outcomes and outcomes[tk].get("strike") is not None:
                strike = float(outcomes[tk]["strike"])
            if strike is None:
                diag["skip_no_strike"] += 1
                continue

            # TERMINAL label from reliable book at close (NOT the sparse DB).
            t_bid, t_ask = kbr.reliable_nbbo_at(fr, close_ep)
            if t_bid is None or t_ask is None:
                diag["skip_no_terminal_label"] += 1
                continue
            terminal_mid = (t_bid + t_ask) / 2.0
            won_yes = terminal_mid > 50.0

            for offset in DECISION_OFFSETS:
                diag["n_decisions_total"] += 1
                cutoff = close_ep - offset
                time_left = offset

                k_bid, k_ask = kbr.reliable_nbbo_at(fr, cutoff)
                if (k_bid is None or k_ask is None or not (0 < k_ask <= 100)
                        or k_bid > k_ask):
                    diag["skip_no_kalshi_reliable"] += 1
                    continue
                kalshi_mid = (k_bid + k_ask) / 2.0

                venue_mids = {}
                for venue in venues_for_asset:
                    m = series_value_at(venue_series[venue], cutoff)
                    if m is not None and m > 0:
                        venue_mids[venue] = m
                if len(venue_mids) < 2:
                    diag["skip_venue_lt2"] += 1
                    continue
                nv = len(venue_mids)
                max_venues_seen = max(max_venues_seen, nv)
                if nv == 2:
                    diag["venue_reliable_2"] += 1
                else:
                    diag["venue_reliable_3"] += 1

                mids = list(venue_mids.values())
                consensus = median(mids)

                max_gap = max(mids) - min(mids)
                gap_bps = (max_gap / consensus) * 1e4 if consensus > 0 else 1e9
                if gap_bps >= AGREE_TOL_BPS:
                    diag["skip_agreement_gate"] += 1
                    continue

                sigma_ps = consensus_sigma_per_sqrt_sec(
                    venue_series, venues_for_asset,
                    cutoff - SIGMA_WINDOW_S, cutoff)
                if sigma_ps is None:
                    diag["skip_no_sigma"] += 1
                    continue

                sigma_total = sigma_ps * math.sqrt(time_left)
                denom = sigma_total * strike
                if denom <= 0:
                    diag["skip_no_sigma"] += 1
                    continue

                z = (consensus - strike) / denom
                fv_cents = _phi(z) * 100.0

                gap = fv_cents - kalshi_mid
                if abs(gap) < EDGE_MIN_CENTS:
                    diag["no_edge_gate"] += 1
                    continue

                diag["signal_fired"] += 1
                if gap > 0:
                    sig = "yes"
                    entry = k_ask
                    win_side = won_yes
                else:
                    sig = "no"
                    entry = 100.0 - k_bid
                    win_side = (not won_yes)

                if not (0 < entry < 100):
                    continue
                gross = (100.0 - entry) if win_side else (-entry)
                net = gross - _fee(entry)
                ckey = (asset, tk)
                clusters_net[ckey].append(net)
                per_asset_clusters[asset][ckey].append(net)
                fired_meta.append((asset, tk, int(offset), sig,
                                   round(fv_cents, 1), round(kalshi_mid, 1),
                                   win_side, round(net, 2)))

        asset_coverage[asset] = max(asset_coverage.get(asset, 0), max_venues_seen)

        ac = per_asset_clusters[asset]
        if ac:
            p, lo, hi, n = cluster_bootstrap_mean(ac)
            print(f"[{asset}] TAKER fired n_trades={n} clusters={len(ac)} "
                  f"mean={p:.3f}c CI[{lo:.3f},{hi:.3f}]", flush=True)

    print("\n=== DIAG ===", flush=True)
    print(json.dumps(diag, indent=2), flush=True)
    print("\n=== ASSET VENUE COVERAGE (max reliable venues at a decision) ===",
          flush=True)
    print(json.dumps(asset_coverage, indent=2), flush=True)

    result = {"diag": diag, "asset_coverage": asset_coverage}
    if clusters_net:
        p, lo, hi, n = cluster_bootstrap_mean(clusters_net)
        result.update({"point": p, "lo": lo, "hi": hi,
                       "n_trades": n, "n_clusters": len(clusters_net)})
        print(f"\nPOOLED TAKER n_trades={n} clusters={len(clusters_net)} "
              f"mean={p:.4f}c CI[{lo:.4f},{hi:.4f}]", flush=True)
    else:
        print("\nNO trades fired.", flush=True)

    print("\n=== PER-ASSET ===", flush=True)
    for asset in ASSETS:
        ac = per_asset_clusters.get(asset, {})
        cov = asset_coverage.get(asset, 0)
        if not ac:
            print(f"  {asset}: cov_venues={cov} no_trades", flush=True)
            continue
        p, lo, hi, n = cluster_bootstrap_mean(ac)
        print(f"  {asset}: cov_venues={cov} n_trades={n} clusters={len(ac)} "
              f"mean={p:.3f}c CI[{lo:.3f},{hi:.3f}]", flush=True)

    if fired_meta:
        print("\n=== SAMPLE FIRED (first 25) ===", flush=True)
        for m in fired_meta[:25]:
            print(f"  {m}", flush=True)

    return result


if __name__ == "__main__":
    run()
