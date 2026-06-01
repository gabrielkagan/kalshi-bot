"""multivenue_consensus_dislocation_fade — Kalshi-vs-cross-venue-consensus maker lean.

THESIS
  The spot-info-edge idea died because a SINGLE stale venue quote produced phantom
  dislocations: on a fresh quote the market wins. A 3-4 venue CONSENSUS mid is far
  harder to make stale simultaneously, so a Kalshi-implied-prob vs consensus-implied
  dislocation is more likely real informational lag than one-venue noise. We avoid the
  spread cost that refuted microprice by executing as a resting MAKER lean (NBBO-1),
  filled HONESTLY only when a real trade print crosses us, settled to the actual Kalshi
  result, net of real price-dependent Kalshi fees.

SIGNAL (per ticker-window, at fixed decision offsets before close)
  1. Build a robust cross-venue CONSENSUS spot for the asset at the decision time:
     depth-... actually TOP-of-book median across {kraken,bitstamp,gemini}(+coinbase
     spot where available) mids (median = robust to one stale/outlier venue). NO
     look-ahead: each venue mid is mid_at(frames, decision_ts).
  2. Map consensus spot -> P(YES) for the 15M above/below threshold via a simple
     diffusion: P(cross) = Phi( (log(S/K)) / (sigma*sqrt(tau)) ) for an above market,
     where sigma = trailing realized vol of the consensus spot (per-sqrt-second),
     tau = seconds to close. (above: YES iff S_close >= K.)
  3. Kalshi-implied P(YES) = reliable NBBO yes-mid / 100.
  4. dislocation = model_p - kalshi_p. If model_p > kalshi_p by a margin (Kalshi too
     CHEAP on YES vs consensus) -> rest a YES maker bid at NBBO yes_bid (==NBBO-1 in
     spread terms; we lean to the bid, never cross). If model_p < kalshi_p (Kalshi too
     RICH on YES / cheap on NO) -> rest a NO maker bid.

FILL + SETTLEMENT (honest)
  HONEST maker fill: the resting bid fills only when a REAL trade print crosses it
  (mm_markout_evaluator.first_yes_bid_fill_ts / first_no_bid_fill_ts). Settlement to the
  ACTUAL Kalshi result from evaluated_opportunities (the real binary outcome; settlement
  is post-close so no look-ahead). Net of ceil(0.07*C*P*(1-P)) Kalshi fee/contract.
  Maker rebate assumed 0 (Kalshi has none).

HEADLINE
  Cluster/block bootstrap CI (clustered by ticker-window = the true independent unit)
  of net settlement markout cents/contract, STRATIFIED by |dislocation| magnitude bin.
  KILL: only |dislocation| bins whose net-cents CI lower bound > 0 survive.

DATA
  VENUE L2 (kraken/bitstamp/gemini .jsonl.zst, day 30-31) via venue_book_reconstruct.
  SPOT (coinbase mid, consensus member where present). FRAMES (kalshi NBBO + terminal).
  TRADES (honest fills). DB (strike + actual settlement). Assets limited to the
  venue-supported set (BTC/ETH/SOL/XRP have >=2 venues + coinbase spot).

Run:
  cd /Users/gabrielkagan/Documents/kalshi-bot
  python3 scripts/research/algo_zoo/multivenue_consensus_dislocation_fade.py
"""
from __future__ import annotations

import glob
import json
import math
import random
import sqlite3
import subprocess
import sys
from collections import defaultdict
from datetime import datetime, timezone
from statistics import median
from typing import Optional

sys.path.insert(0, "/Users/gabrielkagan/Documents/kalshi-bot")

from scripts.research import kalshi_book_reconstruct as kbr  # noqa: E402
from scripts.research import venue_book_reconstruct as vbr  # noqa: E402
from scripts.research.mm_markout_evaluator import (  # noqa: E402
    first_no_bid_fill_ts,
    first_yes_bid_fill_ts,
)
from scripts.research.phase1b_live_shadow import load_trades_by_ticker  # noqa: E402
from scripts.research.phase1b_real_price_economics import (  # noqa: E402
    _is_crypto_15m,
    close_epoch_from_ticker,
)
def kalshi_fee_per_contract_cents(price_cents: float) -> float:
    """Conservative honest Kalshi fee for a SMALL resting maker order: the per-order
    fee is ceil(0.07 * C * P * (1-P)) cents and Kalshi rounds UP to the next cent, so
    a 1-contract resting fill pays at least 1c. We model the harsher per-contract ceil
    (not the amortized large-order rate) because a maker lean rests small size."""
    p = price_cents / 100.0
    return float(math.ceil(7.0 * p * (1.0 - p)))

# ---------------------------------------------------------------------------
# Pre-filtered to the 4 venue-supported assets (grep KX(BTC|ETH|SOL|XRP)15M- of the
# full frames_crypto.jsonl) so the stream pass touches 3.3GB not 4.4GB; falls back to
# the full file if the pre-filtered one is absent.
FRAMES_FILE = ("/tmp/edge_daily/frames_4asset.jsonl"
               if __import__("os").path.exists("/tmp/edge_daily/frames_4asset.jsonl")
               else "/tmp/edge_daily/frames_crypto.jsonl")
TRADES_FILE = "/tmp/edge_daily/trades_crypto.jsonl"
SPOT_FILE = "/tmp/edge_daily/coinbase_spot.jsonl"
DB_FILE = "/tmp/edge_daily/state.db"
VENUE_ROOT = "/tmp/edge_daily/venue_pull"

# Assets with >=2 venue L2 feeds AND coinbase spot (the consensus-robust set).
ASSETS = ("BTC", "ETH", "SOL", "XRP")
DECISION_OFFSETS_S = (60, 120)  # T-Xs before close; settlement forms over final ~60s
DISLOC_MARGIN = 0.0             # min |model_p - kalshi_p| to even consider (bins below)
# |dislocation| magnitude bins (in P-units). Pre-registered; survival is per-bin.
DISLOC_BINS = [(0.02, 0.05, "2-5pp"), (0.05, 0.10, "5-10pp"),
               (0.10, 0.20, "10-20pp"), (0.20, 1.01, "20pp+")]
VOL_TRAIL_S = 600.0   # trailing window for realized-vol estimate of consensus spot
MIN_FILLS_FLOOR = 25  # per-bin; below this -> insufficient, never an edge
N_BOOT = 2000
# Subsample ticker-windows so the 4.4GB frames corpus fits in RAM on this box. A
# random subsample of windows is an unbiased first-pass estimate of the per-bin CIs
# (the spec explicitly sanctions subsampling on the ~1.3-day corpus). 0 = no cap.
MAX_TICKERS = 220
SUBSAMPLE_SEED = 7


def stream_frames_for_tickers(path: str, keep: set) -> dict:
    """Stream the plain-JSONL frames file ONCE, retaining only frames whose
    market_ticker is in `keep`. Memory-bounded: never holds the full 9.77M-row
    corpus, only the chosen subset. Returns {ticker: [(recv_epoch, inner)]} sorted."""
    frames: dict = defaultdict(list)
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
    return frames


# ---------------------------------------------------------------------------
def _epoch(iso: str) -> float:
    s = iso.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


# ---- venue consensus spot --------------------------------------------------
def _venue_book(venue: str):
    return {"kraken": vbr.KrakenBook, "bitstamp": vbr.BitstampBook,
            "gemini": vbr.GeminiBook}[venue]()


def stream_venue_mid_series(venue: str, assets) -> dict:
    """Decompress ONE venue's .zst day/hour tree in TIME ORDER and replay each asset's
    L2 book LIVE, emitting a compact (epoch, mid) sample whenever that asset's frame
    changes the book. MEMORY-BOUNDED: never retains the raw frames — only the resulting
    mid time-series (tens of MB, not the ~15GB the raw-frame retention cost).

    Returns {asset: [(epoch, mid)]} sorted ascending. Reuses
    venue_book_reconstruct._extract_frame for the per-venue/symbol narrowing + the
    venue *Book classes for the reconstruction (do NOT hand-roll book parsing)."""
    sym_to_asset = {}
    for a in assets:
        sym = vbr.VENUE_SYMBOLS.get(venue, {}).get(a)
        if sym is not None:
            sym_to_asset[sym] = a
    if not sym_to_asset:
        return {}
    books = {a: _venue_book(venue) for a in sym_to_asset.values()}
    out: dict = {a: [] for a in sym_to_asset.values()}
    src = f"{venue}_ws"
    # File names are time-ordered (start-ts prefix) so sorted() gives chronological
    # replay; per-conn baseline (snapshot / first full book) is included from file 0.
    files = sorted(glob.glob(f"{VENUE_ROOT}/{src}/day=*/hour=*/conn=*/*.jsonl.zst"))
    for f in files:
        raw = subprocess.run(["zstd", "-dc", f], capture_output=True).stdout.decode()
        for line in raw.splitlines():
            if not line.strip():
                continue
            try:
                env = json.loads(line)
                inner = json.loads(env["_raw"])
            except (ValueError, KeyError):
                continue
            ts = _epoch(env["_wire_recv_ts"])
            for sym, asset in sym_to_asset.items():
                frame = vbr._extract_frame(venue, inner, sym)
                if frame is None:
                    continue
                books[asset].apply_frame(frame)
                m = books[asset].mid()
                if m is not None:
                    out[asset].append((ts, float(m)))
    for a in out:
        out[a].sort(key=lambda x: x[0])
    return out


def build_consensus_from_midseries(asset: str, venue_mids: dict) -> list:
    """Merge per-venue (epoch, mid) series + coinbase spot into a robust cross-venue
    CONSENSUS [(epoch, consensus_mid)] by forward time-merge: carry each source's last
    mid and, at every event, take the MEDIAN across sources that have a current mid.

    `venue_mids` = {venue: [(epoch, mid)]} for THIS asset. Robustness: median across
    >=2 live sources -> a single stale/outlier venue cannot move consensus (the whole
    point vs the single-venue spot-info-edge idea that died on one stale quote)."""
    sources = {v: s for v, s in venue_mids.items() if s}
    cb = load_spot_series(asset)
    if cb:
        sources["coinbase"] = cb
    if len(sources) < 2:
        return []  # cannot form a >=2-source robust consensus
    # Merge all (epoch, src, mid) events in time order.
    events = []
    for src, s in sources.items():
        for ep, mid in s:
            events.append((ep, src, mid))
    events.sort(key=lambda x: x[0])
    last = {}  # src -> last mid
    out = []
    for ep, src, mid in events:
        last[src] = mid
        mids = list(last.values())
        if len(mids) >= 2:
            out.append((ep, float(median(mids))))
    return out


def load_spot_series(asset: str) -> list:
    """[(epoch, mid)] for coinbase <ASSET>-USD from the pre-parsed spot file."""
    pid = f"{asset}-USD"
    out = []
    with open(SPOT_FILE) as fh:
        for line in fh:
            if not line.strip():
                continue
            try:
                d = json.loads(line)
            except ValueError:
                continue
            if d.get("product_id") != pid:
                continue
            mid = d.get("mid")
            if mid is None:
                b, a = d.get("bid"), d.get("ask")
                if b is not None and a is not None:
                    mid = (float(b) + float(a)) / 2.0
            if mid is not None:
                out.append((_epoch(d["ts"]), float(mid)))
    out.sort()
    return out


def consensus_at(series: list, cutoff: float) -> Optional[float]:
    """Latest consensus mid at/before cutoff (no look-ahead). series sorted by epoch.
    Returns None if no consensus point precedes cutoff or the latest point is stale
    (> 30s old at the decision time -> refuse, treat as no signal)."""
    lo, hi = 0, len(series)
    while lo < hi:
        mid = (lo + hi) // 2
        if series[mid][0] <= cutoff:
            lo = mid + 1
        else:
            hi = mid
    if lo == 0:
        return None
    ep, m = series[lo - 1]
    if cutoff - ep > 30.0:  # consensus quote too stale at decision time
        return None
    return m


def _bisect_right_epoch(series: list, cutoff: float) -> int:
    lo, hi = 0, len(series)
    while lo < hi:
        mid = (lo + hi) // 2
        if series[mid][0] <= cutoff:
            lo = mid + 1
        else:
            hi = mid
    return lo


def trailing_vol_per_sqrt_s(series: list, cutoff: float,
                            window_s: float = VOL_TRAIL_S) -> Optional[float]:
    """Per-sqrt-second realized vol of log-returns of the consensus mid over the
    trailing `window_s` ending at cutoff. None if too few points."""
    hi_i = _bisect_right_epoch(series, cutoff)
    lo_i = _bisect_right_epoch(series, cutoff - window_s)
    pts = [(ep, m) for ep, m in series[lo_i:hi_i] if m > 0]
    if len(pts) < 5:
        return None
    # log returns scaled by sqrt(dt) -> per-sqrt-second instantaneous vol, averaged.
    rs = []
    for (e0, m0), (e1, m1) in zip(pts, pts[1:]):
        dt = e1 - e0
        if dt <= 0 or m0 <= 0 or m1 <= 0:
            continue
        rs.append((math.log(m1 / m0)) / math.sqrt(dt))
    if len(rs) < 4:
        return None
    mu = sum(rs) / len(rs)
    var = sum((r - mu) ** 2 for r in rs) / (len(rs) - 1)
    return math.sqrt(var)


def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def model_p_yes_above(spot: float, strike: float, sigma_sqrt_s: float,
                      tau_s: float) -> Optional[float]:
    """Diffusion P(S_close >= K) for an ABOVE market: Phi(ln(S/K)/(sigma*sqrt(tau)))."""
    if spot <= 0 or strike <= 0 or sigma_sqrt_s <= 0 or tau_s <= 0:
        return None
    denom = sigma_sqrt_s * math.sqrt(tau_s)
    if denom <= 0:
        return None
    z = math.log(spot / strike) / denom
    return _norm_cdf(z)


# ---- outcomes / strikes from DB -------------------------------------------
def load_all_db_outcomes(assets) -> dict:
    """{ticker: {result, strike}} for ALL in-window (26MAY30/31) crypto-15M tickers of
    `assets` that have an ACTUAL Kalshi settlement + strike in evaluated_opportunities.
    Settlement is post-close -> no look-ahead at the decision time. This is the label +
    strike source AND the candidate-ticker universe (subsampled in main)."""
    conn = sqlite3.connect(DB_FILE)
    conn.execute("PRAGMA busy_timeout=10000")
    conn.row_factory = sqlite3.Row
    out: dict = {}
    # In-window dates only (the local bronze covers 26MAY30 -> 26MAY31).
    for date_tag in ("26MAY30", "26MAY31"):
        for a in assets:
            sql = ("SELECT DISTINCT ticker, market_result, threshold FROM "
                   "evaluated_opportunities WHERE ticker LIKE ? "
                   "AND market_result IN ('yes','no') AND threshold IS NOT NULL")
            like = f"KX{a}15M-{date_tag}%"
            for r in conn.execute(sql, (like,)):
                out[r["ticker"]] = {"result": r["market_result"],
                                    "strike": float(r["threshold"])}
    conn.close()
    return out


# ---- stats -----------------------------------------------------------------
def cluster_bootstrap_ci(clusters: list, *, n_boot: int = N_BOOT,
                         alpha: float = 0.05, seed: int = 12345):
    """Percentile CI for the mean of per-contract net cents, CLUSTER-resampled by
    ticker-window (the true independent unit; serially-correlated fills inside one
    window are NOT independent). `clusters` = list of lists of per-fill net cents.
    Returns (mean, lo, hi, n_fills)."""
    flat = [x for c in clusters for x in c]
    n_fills = len(flat)
    if not flat or not clusters:
        return (float("nan"), float("nan"), float("nan"), 0)
    grand_mean = sum(flat) / n_fills
    rng = random.Random(seed)
    nc = len(clusters)
    means = []
    for _ in range(n_boot):
        s = 0.0
        cnt = 0
        for _ in range(nc):
            c = clusters[rng.randrange(nc)]
            for v in c:
                s += v
                cnt += 1
        if cnt:
            means.append(s / cnt)
    if not means:
        return (grand_mean, float("nan"), float("nan"), n_fills)
    means.sort()
    lo = means[int((alpha / 2) * len(means))]
    hi = means[min(len(means) - 1, int((1 - alpha / 2) * len(means)))]
    return (grand_mean, lo, hi, n_fills)


def _disloc_bin(d: float) -> Optional[str]:
    a = abs(d)
    for lo, hi, name in DISLOC_BINS:
        if lo <= a < hi:
            return name
    return None


# ---------------------------------------------------------------------------
def main() -> int:
    # The candidate ticker universe = in-window crypto-15M tickers that HAVE a strike
    # + actual settlement in the DB (the label source). Subsample to MAX_TICKERS so the
    # multi-GB frames corpus stays in RAM (random subsample = unbiased first-pass CI).
    print("enumerating candidate tickers from DB (strike+settlement)...", flush=True)
    all_outcomes = load_all_db_outcomes(ASSETS)
    cand = sorted(all_outcomes)
    print(f"  {len(cand)} in-window {ASSETS} tickers with strike+settlement", flush=True)
    if MAX_TICKERS and len(cand) > MAX_TICKERS:
        rng = random.Random(SUBSAMPLE_SEED)
        cand = sorted(rng.sample(cand, MAX_TICKERS))
        print(f"  subsampled to {len(cand)} tickers (seed={SUBSAMPLE_SEED})", flush=True)
    keep = set(cand)
    outcomes = {tk: all_outcomes[tk] for tk in keep}

    print(f"streaming frames for {len(keep)} chosen tickers (memory-bounded)...",
          flush=True)
    frames = stream_frames_for_tickers(FRAMES_FILE, keep)
    print(f"  {len(frames)} tickers have orderbook frames in bronze", flush=True)

    print("loading trades...", flush=True)
    trades = load_trades_by_ticker(TRADES_FILE)
    trades = {tk: tr for tk, tr in trades.items() if tk in keep}  # bound memory

    # Stream each venue tree ONCE, replaying books LIVE into compact per-asset mid
    # series (memory-bounded: NO raw-frame retention).
    venue_mids = {a: {} for a in ASSETS}  # asset -> {venue: [(epoch, mid)]}
    for venue in vbr.VENUES:
        print(f"streaming {venue} venue tree -> live mid series...", flush=True)
        per_asset = stream_venue_mid_series(venue, ASSETS)
        for a, ms in per_asset.items():
            if ms:
                venue_mids[a][venue] = ms
                print(f"  {venue}/{a}: {len(ms)} mid samples", flush=True)

    # Build per-asset robust consensus from the merged per-venue mid series.
    consensus = {}
    for asset in ASSETS:
        print(f"building {asset} cross-venue consensus...", flush=True)
        series = build_consensus_from_midseries(asset, venue_mids.get(asset, {}))
        consensus[asset] = series
        if series:
            print(f"  {asset}: {len(series)} consensus pts "
                  f"({datetime.utcfromtimestamp(series[0][0])} .. "
                  f"{datetime.utcfromtimestamp(series[-1][0])})", flush=True)
        else:
            print(f"  {asset}: NO consensus (missing venue feeds)", flush=True)

    # Decision loop. Per (bin) accumulate per-fill net cents, clustered by ticker-window.
    # cluster key = (ticker, offset) so each independent quote is its own cluster.
    bin_clusters = defaultdict(lambda: defaultdict(list))  # bin -> clusterkey -> [net]
    bin_posted = defaultdict(int)
    bin_filled = defaultdict(int)
    n_eval = 0
    n_no_book = 0
    n_no_consensus = 0
    n_no_vol = 0
    n_signal = 0

    for tk, fr in frames.items():
        if tk not in outcomes or tk not in trades:
            continue
        asset = _is_crypto_15m(tk)
        series = consensus.get(asset)
        if not series:
            continue
        result = outcomes[tk]["result"]
        strike = outcomes[tk]["strike"]
        close = close_epoch_from_ticker(tk)
        tr = trades[tk]
        for off in DECISION_OFFSETS_S:
            dts = close - off
            n_eval += 1
            # Kalshi reliable NBBO at decision time (refuses drifted books).
            ybid, yask = kbr.reliable_nbbo_at(fr, dts)
            if ybid is None or yask is None:
                n_no_book += 1
                continue
            kalshi_p = (ybid + yask) / 2.0 / 100.0
            spot = consensus_at(series, dts)
            if spot is None:
                n_no_consensus += 1
                continue
            sigma = trailing_vol_per_sqrt_s(series, dts)
            if sigma is None:
                n_no_vol += 1
                continue
            model_p = model_p_yes_above(spot, strike, sigma, float(off))
            if model_p is None:
                continue
            disloc = model_p - kalshi_p
            b = _disloc_bin(disloc)
            if b is None or abs(disloc) < DISLOC_MARGIN:
                continue
            n_signal += 1
            # Lean direction: model says YES richer than Kalshi -> rest YES bid.
            if disloc > 0:
                side, price = "yes", float(ybid)
                fill_ts = first_yes_bid_fill_ts(tr, dts, price)
            else:
                no_bid = 100.0 - yask
                side, price = "no", float(no_bid)
                fill_ts = first_no_bid_fill_ts(tr, dts, price)
            if not (0 < price < 100):
                continue
            bin_posted[b] += 1
            if fill_ts is None:
                continue
            bin_filled[b] += 1
            fee = kalshi_fee_per_contract_cents(price)
            settle = (100.0 - price) if side == result else -price
            net = settle - fee
            bin_clusters[b][(tk, off)].append(net)

    print(f"\nevaluated decisions: {n_eval}  no_reliable_book: {n_no_book}  "
          f"no_consensus: {n_no_consensus}  no_vol: {n_no_vol}  signals: {n_signal}",
          flush=True)

    print(f"\n{'bin':>8}{'posted':>8}{'filled':>8}{'fill%':>7}"
          f"{'netMean':>10}{'CI_lo':>9}{'CI_hi':>9}{'VERDICT':>9}")
    results = {}
    survivors = []
    for _, _, name in DISLOC_BINS:
        posted = bin_posted.get(name, 0)
        filled = bin_filled.get(name, 0)
        clusters = list(bin_clusters.get(name, {}).values())
        mean, lo, hi, nf = cluster_bootstrap_ci(clusters)
        fillpct = 100 * filled / posted if posted else 0.0
        survives = (nf >= MIN_FILLS_FLOOR) and (not math.isnan(lo)) and (lo > 0)
        verdict = "SURVIVE" if survives else "kill"
        results[name] = {"posted": posted, "filled": filled, "mean": mean,
                         "lo": lo, "hi": hi, "n_fills": nf}
        lo_s = f"{lo:+.2f}" if not math.isnan(lo) else "—"
        hi_s = f"{hi:+.2f}" if not math.isnan(hi) else "—"
        mn_s = f"{mean:+.2f}" if not math.isnan(mean) else "—"
        print(f"{name:>8}{posted:>8}{filled:>8}{fillpct:>7.1f}"
              f"{mn_s:>10}{lo_s:>9}{hi_s:>9}{verdict:>9}")
        if survives:
            survivors.append((name, mean, lo, hi, nf))

    print()
    if survivors:
        print(f"SURVIVORS ({len(survivors)}): net-cents CI lower bound > 0")
        for name, mean, lo, hi, nf in survivors:
            print(f"  {name}: net={mean:+.2f}c CI=[{lo:+.2f},{hi:+.2f}] n_fills={nf}")
    else:
        print("NO bin survived the gate (CI lower bound <= 0 or insufficient fills).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
