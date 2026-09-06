"""settlement_rti_convergence — final-seconds convergence test vs a REAL
multi-venue synthetic index.

Family: settlement_microstructure / convergence. NOT forecasting — a Jane-Street
fair-value / convergence claim. In the last T seconds of a crypto-15M window we
build a synthetic spot index from the venue top-of-book mids available at
decision time. DEFAULT index = Coinbase spot ticker + reconstructed Kraken L2 (a
REAL two-venue synthetic index — the spec's core ask, vs the single stale
Coinbase quote that killed the spot-info idea). Gemini and Bitstamp are
reconstructable too (RTI_INCLUDE_GEMINI=1 / RTI_INCLUDE_BITSTAMP=1) but their
bronze is 10-50x costlier to decompress+parse over this 1.3-day window for
marginal index benefit, so they are default-OFF. From |index - strike| and the time
remaining we compute a TIGHT diffusion bound on the probability the terminal
spot stays on the current side (p_win). When p_win is essentially locked
(>= 0.97) AND a RELIABLE Kalshi NBBO (honor the refusal) prices the winning
side's ask below the fair-value breakeven (100*p_win - fee), we LIFT the winning
side as a TAKER at the ask. Label by the terminal Kalshi book mid at close.

PnL/contract = 100 * win - entry_ask - fee   (win in {0,1}).
Fees: Kalshi ceil(0.07 * C=1 * P * (1-P)) cents, P = entry_ask/100. Maker rebate
n/a (taker). HEADLINE = mean net PnL per entered contract + ticker-clustered
bootstrap CI (>=1000). EDGE iff the net CI excludes zero on the positive side.

Look-ahead discipline: index is built ONLY from venue frames with
recv_epoch <= decision_epoch; the Kalshi signal-NBBO is reliable_nbbo_at the
SAME decision_epoch; the label book is reconstructed independently at
close_epoch. Strike comes from the DB threshold (set at evaluation), never the
terminal data. HYPE/DOGE/BNB have NO local Coinbase mid AND incomplete venue
constituency for a clean index -> flagged DATA_GAP for the index leg, excluded.

Reuses the tested reconstruction modules (no hand-rolled parsing):
  scripts.research.kalshi_book_reconstruct.{reliable_nbbo_at, book_at, KalshiBook}
  scripts.research.venue_book_reconstruct.{VENUE_SYMBOLS, mid_at, *Book}
  scripts.research.phase1b_real_price_economics.close_epoch_from_ticker

Hunt program: settlement-second RTI convergence (open list). B2 multi-venue
synthetic-RTI corpus now local.
"""

from __future__ import annotations

import glob
import json
import math
import os
import subprocess
import sys
import tempfile
from collections import defaultdict

sys.path.insert(0, "/Users/gabrielkagan/Documents/kalshi-bot")

from scripts.research.kalshi_book_reconstruct import (  # noqa: E402
    book_at,
    reliable_nbbo_at,
)
from scripts.research.venue_book_reconstruct import (  # noqa: E402
    VENUE_SYMBOLS,
    BitstampBook,
    GeminiBook,
    KrakenBook,
    load_venue_frames,
    mid_at,
)
from scripts.research.phase1b_real_price_economics import (  # noqa: E402
    _epoch,
    close_epoch_from_ticker,
)

FRAMES = "/tmp/edge_daily/frames_crypto.jsonl"
SPOT = "/tmp/edge_daily/coinbase_spot.jsonl"
DB = "/tmp/edge_daily/state.db"
VENUE_ROOT = "/tmp/edge_daily/venue_pull"

# Index-eligible assets: need a real multi-venue index. BTC/ETH/SOL/XRP have a
# Coinbase mid AND >=2 venue constituents. HYPE/DOGE/BNB are DATA_GAP.
INDEX_ASSETS = ("BTC", "ETH", "SOL", "XRP")
GAP_ASSETS = ("HYPE", "DOGE", "BNB")

T_SWEEP = (30, 20, 10, 5)  # decision offsets (seconds before close)
P_LOCK = 0.97
FEE_RATE = 0.07
MAX_STALE_S = 5.0  # a venue component older than this is dropped (no stale-quote artifact)
VENUE_BOOK = {"kraken": KrakenBook, "bitstamp": BitstampBook, "gemini": GeminiBook}

# Which reconstructed L2 venues to blend into the synthetic index (Coinbase spot
# is ALWAYS included and is not an L2-reconstruction venue). Kraken is the default
# and the broadest constituent (all 4 of our assets, true incremental deltas that
# parse cheaply). Bitstamp (full top-100 snapshot every message) and Gemini (very
# high l2_updates delta rate) are each ~10-50x costlier to decompress+parse over
# this 1.3-day window for marginal index benefit — Coinbase + Kraken already give
# a REAL two-venue index (the spec's core ask: not a single stale Coinbase quote).
# Opt them in with RTI_INCLUDE_GEMINI=1 / RTI_INCLUDE_BITSTAMP=1.
_INCLUDE_BITSTAMP = os.environ.get("RTI_INCLUDE_BITSTAMP", "0") == "1"
_INCLUDE_GEMINI = os.environ.get("RTI_INCLUDE_GEMINI", "0") == "1"
ACTIVE_VENUES = (["kraken"]
                 + (["gemini"] if _INCLUDE_GEMINI else [])
                 + (["bitstamp"] if _INCLUDE_BITSTAMP else []))

_VENUE_DAYS = (30, 31)
_VENUE_HOURS = range(0, 24)

# Subsample / smoke knobs (env). VENUE_HOUR_LIMIT restricts which venue hours are
# decompressed (for a fast first pass); 0 = all. SMOKE limits tickers loaded.
_VENUE_HOUR_LIMIT = int(os.environ.get("RTI_VENUE_HOUR_LIMIT", "0"))
_SMOKE_TICKERS = int(os.environ.get("RTI_SMOKE_TICKERS", "0"))


def fee_cents(entry_ask_cents: float) -> int:
    """Kalshi taker fee: ceil(0.07 * C=1 * P * (1-P)) cents, P=ask/100."""
    p = entry_ask_cents / 100.0
    return math.ceil(FEE_RATE * 1.0 * p * (1.0 - p))


def asset_of(ticker: str):
    for a in INDEX_ASSETS + GAP_ASSETS:
        if ticker.startswith(f"KX{a}15M-"):
            return a
    return None


# ---------------------------------------------------------------------------
# Loaders
# ---------------------------------------------------------------------------

def load_strikes(db_path: str) -> dict:
    """{ticker: strike} from evaluated_opportunities.threshold (the strike is
    set at evaluation time, not from terminal data -> no look-ahead)."""
    import sqlite3
    c = sqlite3.connect(db_path)
    c.execute("PRAGMA busy_timeout=10000")
    out = {}
    sql = ("SELECT ticker, threshold FROM evaluated_opportunities "
           "WHERE ticker LIKE 'KX%15M-26MAY3%' AND threshold IS NOT NULL")
    for tk, thr in c.execute(sql):
        if asset_of(tk) and tk not in out:
            out[tk] = float(thr)
    c.close()
    return out


def load_kalshi_frames(path: str, want: set) -> dict:
    """{ticker: [(recv_epoch, inner)]} for wanted crypto-15M tickers, sorted."""
    fr = defaultdict(list)
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
            if tk in want:
                fr[tk].append((_epoch(env["_wire_recv_ts"]), inner))
    for tk in fr:
        fr[tk].sort(key=lambda x: x[0])
    return fr


def load_spot(path: str) -> dict:
    """{asset: [(epoch, mid)]} from the pre-normalized Coinbase ticker file
    (already flattened: {ts, product_id, mid, bid, ask, last}), sorted ascending.
    Uses (bid+ask)/2 to mirror the venue-mid construction; falls back to `mid`."""
    prod_asset = {"BTC-USD": "BTC", "ETH-USD": "ETH", "SOL-USD": "SOL",
                  "XRP-USD": "XRP"}
    out = defaultdict(list)
    with open(path) as fh:
        for line in fh:
            if not line.strip():
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            a = prod_asset.get(rec.get("product_id"))
            if a is None:
                continue
            mid = None
            try:
                bb = float(rec["bid"]); ba = float(rec["ask"])
                if bb > 0 and ba > 0 and bb <= ba:
                    mid = (bb + ba) / 2.0
            except (KeyError, TypeError, ValueError):
                mid = None
            if mid is None:
                try:
                    m = float(rec["mid"])
                    if m > 0:
                        mid = m
                except (KeyError, TypeError, ValueError):
                    continue
            if mid is None:
                continue
            ts = rec.get("ts")
            if not ts:
                continue
            out[a].append((_epoch(ts), mid))
    for a in out:
        out[a].sort(key=lambda x: x[0])
    return out


# Venue load WINDOW: all 214 in-scope tickers close in [day30 21Z, day31 17Z]
# (ET 17:00 -> 13:15). The index leg needs venue mids only from ~1h before the
# earliest decision through the last close, so we skip decompressing day30 00-19Z
# (kraken's 14GB / bitstamp's 21GB are the bottleneck). Starting mid-stream is
# safe: Bitstamp is a FULL snapshot every frame (self-complete), Coinbase covers
# the whole window, and the multi-venue MEAN tolerates a venue that hasn't yet
# re-snapshotted. Kraken/Gemini simply contribute None until their first in-window
# snapshot/update rebuilds a clean book, and the index averages over whoever IS
# clean. Set RTI_VENUE_START_DAY/HOUR=0 to load the whole corpus.
_VENUE_START_DAY = int(os.environ.get("RTI_VENUE_START_DAY", "30"))
_VENUE_START_HOUR = int(os.environ.get("RTI_VENUE_START_HOUR", "20"))


def _venue_chunk_paths(venue: str):
    files = []
    for d in _VENUE_DAYS:
        for h in _VENUE_HOURS:
            if d < _VENUE_START_DAY or (d == _VENUE_START_DAY and h < _VENUE_START_HOUR):
                continue
            files += sorted(glob.glob(
                f"{VENUE_ROOT}/{venue}_ws/day={d:02d}/hour={h:02d}/conn=A/*.jsonl.zst"))
    if _VENUE_HOUR_LIMIT > 0:
        files = files[:_VENUE_HOUR_LIMIT]
    return files


def load_venue_all_assets(venue: str, assets):
    """SINGLE streaming pass over one venue's chunks (zstd -dc piped, never a
    multi-GB temp file): returns {asset: [(recv_epoch, inner_frame)]} sorted, for
    every requested constituent asset at once. Mirrors load_venue_frames'
    _extract_frame logic but extracts all asset symbols in the same line scan so
    a 20GB venue stream is decompressed + parsed ONCE, not 4x."""
    from scripts.research.venue_book_reconstruct import _epoch as _vepoch
    sym_for = {a: VENUE_SYMBOLS[venue][a] for a in assets if a in VENUE_SYMBOLS[venue]}
    out = {a: [] for a in sym_for}
    if not sym_for:
        return out
    # reverse map symbol -> asset for O(1) routing
    asset_for_sym = {sym: a for a, sym in sym_for.items()}
    syms = set(sym_for.values())
    expect_source = f"{venue}_ws"
    for fp in _venue_chunk_paths(venue):
        proc = subprocess.Popen(["zstd", "-dc", fp], stdout=subprocess.PIPE)
        for bline in proc.stdout:
            line = bline.decode("utf-8", "replace")
            if not line.strip():
                continue
            try:
                env = json.loads(line)
                src = env.get("_source")
                if src is not None and src != expect_source:
                    continue
                inner = json.loads(env["_raw"])
            except (ValueError, KeyError):
                continue
            ts_iso = env.get("_wire_recv_ts")
            if not ts_iso:
                continue
            ep = None
            # Build a MINIMAL per-asset frame (no full-envelope dict copy — that
            # `{**inner}` copy was the kraken hot-path killer). Each branch emits
            # exactly the keys the matching *Book.apply_frame reads.
            if venue == "kraken":
                t = inner.get("type")
                # group this frame's data entries by symbol in one pass
                by_sym = None
                for entry in inner.get("data", ()):
                    s = entry.get("symbol")
                    if s in syms:
                        if by_sym is None:
                            by_sym = {}
                        by_sym.setdefault(s, []).append(entry)
                if by_sym:
                    ep = _vepoch(ts_iso)
                    for s, entries in by_sym.items():
                        out[asset_for_sym[s]].append((ep, {"type": t, "data": entries}))
            elif venue == "bitstamp":
                ch = inner.get("channel", "")
                if ch.startswith("order_book_"):
                    s = ch[len("order_book_"):]
                    if s in syms:
                        ep = _vepoch(ts_iso)
                        out[asset_for_sym[s]].append((ep, inner))
            elif venue == "gemini":
                if inner.get("type") == "l2_updates":
                    s = inner.get("symbol")
                    if s in syms:
                        ep = _vepoch(ts_iso)
                        out[asset_for_sym[s]].append((ep, inner))
        proc.stdout.close()
        proc.wait()
        # ticket 86bbvrx1t: this loop has NO `break`, so reaching here is a
        # true EOF — a non-zero zstd exit means the venue chunk was TRUNCATED
        # and this asset's frame list is silently short.
        assert_zstd_ok(proc, fp, exhausted=True, require_nonempty=False)
    for a in out:
        out[a].sort(key=lambda x: x[0])
    return out


# ---------------------------------------------------------------------------
# Index + signal
# ---------------------------------------------------------------------------

import bisect
from scripts.research.zstd_stream import assert_zstd_ok  # noqa: E402  (repo root on sys.path above)


def precompute_venue_grid(frames, book_cls, grid_dt=1.0):
    """ONE forward replay of a venue's entire frame stream, emitting a coarse
    (epoch, mid) sample every `grid_dt`s plus the last-applied frame ts. Done
    ONCE per (asset,venue) across the whole corpus -> O(n). Downstream cutoff
    lookups are O(log n) bisect, so the per-ticker work no longer re-replays from
    corpus start. Returns (grid_epochs, grid_mids, grid_last_ts) parallel lists.
    A grid cell records the book mid AS OF that grid time (causal)."""
    book = book_cls()
    g_ep, g_mid, g_last = [], [], []
    if not frames:
        return g_ep, g_mid, g_last
    next_t = frames[0][0]
    last_ts = None
    for ts, inner in frames:
        # before applying this frame, close out any grid cells up to ts
        while next_t <= ts:
            g_ep.append(next_t)
            g_mid.append(book.mid())
            g_last.append(last_ts)
            next_t += grid_dt
        book.apply_frame(inner)
        last_ts = ts
    # final cell at end
    g_ep.append(frames[-1][0])
    g_mid.append(book.mid())
    g_last.append(last_ts)
    return g_ep, g_mid, g_last


def grid_lookup(grid, cutoff):
    """(mid, last_ts) at the grid cell with epoch <= cutoff (None if before start)."""
    g_ep, g_mid, g_last = grid
    if not g_ep:
        return None, None
    i = bisect.bisect_right(g_ep, cutoff) - 1
    if i < 0:
        return None, None
    return g_mid[i], g_last[i]


def spot_lookup(spot_pre, cutoff):
    """Latest Coinbase (epoch, mid) <= cutoff via bisect (None if none).
    spot_pre is a preprocessed (epochs, mids) parallel-list tuple."""
    epochs, mids = spot_pre
    if not epochs:
        return None
    i = bisect.bisect_right(epochs, cutoff) - 1
    if i < 0:
        return None
    return (epochs[i], mids[i])


def synthetic_index(asset, cutoff_epoch, spot_pre, venue_grids):
    """Mean of available NON-STALE venue mids + Coinbase at cutoff, using the
    precomputed per-venue grids + bisected Coinbase series. Returns
    (index, n_venues, comps)."""
    comps = {}
    sp = spot_lookup(spot_pre, cutoff_epoch)
    if sp is not None and (cutoff_epoch - sp[0]) <= MAX_STALE_S:
        comps["coinbase"] = sp[1]
    for venue, grid in venue_grids.items():
        mid, last_ts = grid_lookup(grid, cutoff_epoch)
        if mid is not None and mid > 0 and last_ts is not None and (cutoff_epoch - last_ts) <= MAX_STALE_S:
            comps[venue] = mid
    if not comps:
        return None, 0, {}
    vals = list(comps.values())
    return sum(vals) / len(vals), len(vals), comps


def diffusion_p_win(index, strike, sigma_per_s, t_remaining):
    """Probability terminal spot stays on the CURRENT side of strike under a
    driftless Brownian bound. distance=|index-strike|; sd=sigma*sqrt(t);
    p_win = Phi(distance / sd)."""
    if t_remaining <= 0 or sigma_per_s <= 0:
        return 1.0 if abs(index - strike) > 0 else 0.5
    sd = sigma_per_s * math.sqrt(t_remaining)
    if sd <= 0:
        return 1.0
    z = abs(index - strike) / sd
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def estimate_sigma(history):
    """Per-second price-stdev (absolute USD units) from recent index samples.
    history = [(epoch, index)]; returns sigma_per_s in PRICE units."""
    if len(history) < 5:
        return None
    diffs = []
    for i in range(1, len(history)):
        dt = history[i][0] - history[i - 1][0]
        if dt <= 0:
            continue
        dp = history[i][1] - history[i - 1][1]
        diffs.append((dp * dp) / dt)
    if len(diffs) < 4:
        return None
    var_per_s = sum(diffs) / len(diffs)
    if var_per_s <= 0:
        return None
    return math.sqrt(var_per_s)


def index_history(asset, end_epoch, lookback_s, spot_pre, venue_grids):
    """Sample the synthetic index every ~2s over [end-lookback, end] for sigma.
    Causal (each sample uses only data <= its time)."""
    hist = []
    t = end_epoch - lookback_s
    while t <= end_epoch:
        idx, _n, _c = synthetic_index(asset, t, spot_pre, venue_grids)
        if idx is not None:
            hist.append((t, idx))
        t += 2.0
    return hist


# ---------------------------------------------------------------------------
# Bootstrap
# ---------------------------------------------------------------------------

def cluster_bootstrap_ci(pnls_by_ticker, n_boot=2000, seed=12345):
    """Ticker-clustered bootstrap of mean net PnL per contract. Resample tickers
    (the independent unit) with replacement, pool their contract-level PnLs, take
    the mean. Returns (point, lo, hi) at 95%."""
    import random
    rng = random.Random(seed)
    tickers = list(pnls_by_ticker.keys())
    if not tickers:
        return None, None, None
    all_pnls = [p for ps in pnls_by_ticker.values() for p in ps]
    point = sum(all_pnls) / len(all_pnls)
    means = []
    for _ in range(n_boot):
        pool = []
        for _ in range(len(tickers)):
            tk = tickers[rng.randrange(len(tickers))]
            pool.extend(pnls_by_ticker[tk])
        if pool:
            means.append(sum(pool) / len(pool))
    if not means:
        return point, None, None
    means.sort()
    lo = means[int(0.025 * len(means))]
    hi = means[min(int(0.975 * len(means)), len(means) - 1)]
    return point, lo, hi


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    strikes = load_strikes(DB)
    # index leg needs Coinbase; restrict to tickers whose CLOSE is in the spot
    # window (spot starts 2026-05-30T21:00Z). Venues cover earlier (day30 hr00).
    from datetime import datetime, timezone
    spot_start = datetime(2026, 5, 30, 21, 0, tzinfo=timezone.utc).timestamp()
    strikes = {tk: s for tk, s in strikes.items()
               if asset_of(tk) in INDEX_ASSETS
               and close_epoch_from_ticker(tk) >= spot_start}
    if _SMOKE_TICKERS > 0:
        # deterministic subsample: LAST N per asset (latest tickers, mid-window)
        keep = {}
        per = defaultdict(int)
        for tk in sorted(strikes, reverse=True):
            a = asset_of(tk)
            if per[a] < _SMOKE_TICKERS:
                keep[tk] = strikes[tk]
                per[a] += 1
        strikes = keep
    want = set(strikes)
    print(f"[load] {len(strikes)} tickers w/ strike; {len(want)} index-eligible",
          file=sys.stderr)

    kframes = load_kalshi_frames(FRAMES, want)
    print(f"[load] kalshi frames for {len(kframes)} tickers", file=sys.stderr)

    spot_raw = load_spot(SPOT)
    # precompute (epochs, mids) tuples for O(log n) bisect lookup
    spot_pre = {a: ([s[0] for s in v], [s[1] for s in v]) for a, v in spot_raw.items()}
    print(f"[load] spot assets: { {a: len(v) for a, v in spot_raw.items()} }",
          file=sys.stderr)

    # SINGLE pass per venue (zstd streamed), then ONE full replay -> coarse grid
    # per (asset,venue). After this, every cutoff lookup is O(log n) bisect.
    grids_by_asset = {a: {} for a in INDEX_ASSETS}
    for venue in ACTIVE_VENUES:
        per_asset = load_venue_all_assets(venue, INDEX_ASSETS)
        for a, fr in per_asset.items():
            grids_by_asset[a][venue] = precompute_venue_grid(fr, VENUE_BOOK[venue])
        print(f"[load] {venue}: "
              f"{ {a: len(per_asset[a]) for a in per_asset} } frames; gridded",
              file=sys.stderr)
        del per_asset

    results = {}
    for T in T_SWEEP:
        pnls_by_ticker = defaultdict(list)
        n_locked = n_traded = n_no_gap = n_disagree = 0
        n_no_index = n_no_nbbo = n_eval = 0

        for tk, strike in strikes.items():
            a = asset_of(tk)
            if a not in INDEX_ASSETS:
                continue
            frames = kframes.get(tk)
            if not frames:
                continue
            close_e = close_epoch_from_ticker(tk)
            dec_e = close_e - T
            vgrids = grids_by_asset[a]
            sp_pre = spot_pre.get(a, ([], []))

            idx, nv, comps = synthetic_index(a, dec_e, sp_pre, vgrids)
            if idx is None:
                n_no_index += 1
                continue
            n_eval += 1

            hist = index_history(a, dec_e, 90.0, sp_pre, vgrids)
            sigma = estimate_sigma(hist)
            if sigma is None:
                continue

            lbl_book, _ = book_at(frames, close_e)
            yb = lbl_book.best_yes_bid_cents()
            ya = lbl_book.best_yes_ask_cents()
            if yb is None or ya is None:
                continue
            term_mid = (yb + ya) / 2.0
            terminal_yes = term_mid >= 50.0

            idx_yes = idx > strike
            p_win = diffusion_p_win(idx, strike, sigma, T)
            if p_win < P_LOCK:
                continue
            n_locked += 1
            if idx_yes != terminal_yes:
                n_disagree += 1

            sb, sa = reliable_nbbo_at(frames, dec_e)
            if sb is None or sa is None:
                n_no_nbbo += 1
                continue

            win_ask = sa if idx_yes else (100.0 - sb)
            if win_ask is None or win_ask <= 0 or win_ask >= 100:
                continue

            f = fee_cents(win_ask)
            breakeven = 100.0 * p_win - f
            if win_ask >= breakeven:
                n_no_gap += 1
                continue

            n_traded += 1
            if idx_yes:
                win = 1.0 if terminal_yes else 0.0
            else:
                win = 1.0 if (not terminal_yes) else 0.0
            pnl = 100.0 * win - win_ask - f
            pnls_by_ticker[tk].append(pnl)

        point, lo, hi = cluster_bootstrap_ci(pnls_by_ticker)
        n_contracts = sum(len(v) for v in pnls_by_ticker.values())
        results[T] = dict(
            n_eval=n_eval, n_locked=n_locked, n_traded=n_traded,
            n_no_gap=n_no_gap, n_disagree=n_disagree, n_no_index=n_no_index,
            n_no_nbbo=n_no_nbbo, n_contracts=n_contracts,
            n_tickers=len(pnls_by_ticker), point=point, lo=lo, hi=hi,
        )

    print("\n==== settlement_rti_convergence ====")
    for T in T_SWEEP:
        r = results[T]
        print(f"\nT-{T}s: eval={r['n_eval']} locked(p>={P_LOCK})={r['n_locked']} "
              f"traded={r['n_traded']} contracts={r['n_contracts']} "
              f"tickers={r['n_tickers']}")
        print(f"   no_index={r['n_no_index']} no_nbbo={r['n_no_nbbo']} "
              f"no_gap(book-fair)={r['n_no_gap']} "
              f"disagree(locked-but-flip)={r['n_disagree']}")
        if r["point"] is not None and r["lo"] is not None:
            print(f"   mean net PnL/contract = {r['point']:.3f}c  "
                  f"95% CI [{r['lo']:.3f}, {r['hi']:.3f}]")
        else:
            print("   no trades")

    # HEADLINE = the T with the most traded contracts (deepest sample); ties ->
    # smallest T (tightest lock). Verdict: EDGE iff its net CI excludes zero > 0.
    import json as _json
    traded = {T: results[T] for T in T_SWEEP if results[T]["n_contracts"] > 0}
    if traded:
        headline_T = max(traded, key=lambda T: traded[T]["n_contracts"])
        hr = results[headline_T]
        print("\n[JSON] " + _json.dumps({"headline_T": headline_T, **hr}))
    else:
        print("\n[JSON] " + _json.dumps({"headline_T": None, "n_contracts": 0}))

    return results


if __name__ == "__main__":
    main()
