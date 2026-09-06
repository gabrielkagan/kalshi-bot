"""venue_l2_imbalance_lead_kalshi_taker — multi-venue L2 depth-imbalance lead-lag.

FAMILY: multi-venue lead-lag / cross-feed microstructure.

MECHANISM
---------
At sampled decision times t in [window_open+60s, close-60s] every 30s, for
BTC/ETH (deepest venue books; majors only — HYPE/DOGE/BNB have no/partial venue
feed):
  1. Reconstruct each spot venue's L2 book (kraken / bitstamp / gemini) via the
     tested `venue_book_reconstruct` module at t (NO look-ahead: only frames with
     recv_epoch <= t).
  2. Compute a per-venue top-N depth imbalance
        DI_v = (bidvol - askvol) / (bidvol + askvol)
     over the top-N price levels, then a depth-weighted consensus DI across the
     venues that constitute the asset.
  3. SIGNAL fires when |consensus DI| exceeds the 90th-pct magnitude (computed
     per asset over the sampled grid) AND the Kalshi reliable book mid has NOT
     yet moved: |mid(t) - mid(t-10s)| <= 1c (book is stale to the venue impulse).
  4. We TAKE the Kalshi side implied by DI sign (DI>0 => bullish spot => buy YES;
     DI<0 => buy NO) by CROSSING the reconstructed reliable ask. Skip if no ask.
  5. Pay the Kalshi taker fee. Settle to the terminal 15M above/below outcome
     (DB ground truth from evaluated_opportunities; fallback terminal book mid).

HEADLINE: per-contract net cents, block bootstrap clustered by (ticker, hour).

HONESTY
-------
- Fee = ceil(0.07 * C * P * (1-P)) cents/contract on taker entry, C=1, P=ask/100.
  Maker rebate = 0 (this is a pure taker strategy).
- Fill: cross to the EXISTING reliable reconstructed ask only (skip if none /
  if the book is unreliable / refused). No phantom fills.
- Label: DB evaluated_opportunities (ground truth) primary; terminal reliable
  book mid fallback. NO look-ahead — signal book built strictly <= t.
- reliable_nbbo_at REFUSES drifted books; we honor the refusal everywhere.

This is the L2-DEPTH variant of cross_venue_lead_lag (listed NOT-YET-MEASURED /
OPEN). It conditions on a FRESH multi-venue DEPTH signal vs a STALE Kalshi book
and only fires on the lag.
"""
from __future__ import annotations

import json
import math
import os
import random
import sqlite3
import subprocess
import sys
from collections import defaultdict
from datetime import datetime, timezone

sys.path.insert(0, "/Users/gabrielkagan/Documents/kalshi-bot")

from scripts.research.kalshi_book_reconstruct import reliable_nbbo_at  # noqa: E402
from scripts.research.phase1b_real_price_economics import (  # noqa: E402
    close_epoch_from_ticker,
    load_frames_jsonl,
)
from scripts.research.venue_book_reconstruct import (  # noqa: E402
    VENUE_SYMBOLS,
    BitstampBook,
    GeminiBook,
    KrakenBook,
    _epoch as venue_epoch,
    _extract_frame,
    parse_envelope as venue_parse_envelope,
)
from scripts.research.zstd_stream import assert_zstd_ok  # noqa: E402  (repo root on sys.path above)

# ----- config ---------------------------------------------------------------
FRAMES_BTC_ETH = "/tmp/edge_daily/frames_btc_eth.jsonl"
FRAMES_ALL = "/tmp/edge_daily/frames_crypto.jsonl"
VENUE_ROOT = "/tmp/edge_daily/venue_pull"
DB_PATH = "/tmp/edge_daily/state.db"

ASSETS = ("BTC", "ETH")  # subsample: deepest venue books

# CORPUS REALITY (verified 2026-05-31 on the local pull): the venue collector
# started MID-SESSION, so Kraken and Gemini archive ONLY incremental absolute-set
# `update` frames with NO snapshot baseline anywhere in the 31h window. Replaying
# them accumulates stale levels and the book CROSSES — unreliable, cannot be used
# without look-ahead/fabrication. Bitstamp ships a FULL top-100 both-sides
# snapshot on EVERY frame (self-complete), so it is the ONLY reconstructable
# venue here. We therefore run a Bitstamp-only depth-imbalance signal and flag
# the multi-venue degradation explicitly (see RELIABLE_VENUES / lookahead_risks).
VENUE_DIRS = {"kraken": "kraken_ws", "bitstamp": "bitstamp_ws", "gemini": "gemini_ws"}
BOOK_CLASSES = {"kraken": KrakenBook, "bitstamp": BitstampBook, "gemini": GeminiBook}
# Only venues with a usable baseline in THIS corpus. Bitstamp self-completes.
RELIABLE_VENUES = ("bitstamp",)

SAMPLE_EVERY_S = 30.0
WINDOW_OPEN_PAD_S = 60.0   # start at window_open + 60s
WINDOW_CLOSE_PAD_S = 60.0  # stop at close - 60s
STALE_LOOKBACK_S = 10.0    # Kalshi mid t vs t-10s
STALE_MAX_DELTA_C = 1.0    # require |mid delta| <= 1c
TOP_N_LEVELS = 10          # top-N depth levels for DI
DI_PCT = 90.0              # fire when |DI| exceeds this percentile magnitude
WINDOW_LEN_S = 900.0       # 15M
N_BOOTSTRAP = 2000
SEED = 7


def _asset_of(ticker: str) -> str:
    # KXBTC15M-... -> BTC
    core = ticker.split("15M")[0]
    return core.replace("KX", "")


def _ticker_strike(ticker: str):
    """evaluated_opportunities.threshold is the strike. Return None here; we use
    the DB threshold for labelling (already keyed by full ticker)."""
    return None


# ----- venue L2 loading -----------------------------------------------------
def _hour_lines(hdir: str):
    """Stream decompressed lines for ALL .jsonl.zst chunks in an hour dir in a
    single `zstd -dc` invocation (far fewer subprocess spawns than per-file)."""
    files = sorted(os.path.join(hdir, f) for f in os.listdir(hdir)
                   if f.endswith(".jsonl.zst"))
    if not files:
        return
    p = subprocess.Popen(["zstd", "-dcq", *files], stdout=subprocess.PIPE, text=True)
    assert p.stdout is not None
    _exhausted = False
    try:
        for line in p.stdout:
            yield line
        _exhausted = True
    finally:
        # ticket 86bbvrx1t: a decompressor that dies mid-file just ENDS the
        # loop; without this the caller silently receives a PREFIX of the hour
        # and reports success. Multi-file `zstd -dcq *files`, so the shared
        # checked_stream_lines (single path) does not fit — assert directly.
        p.wait()
        assert_zstd_ok(p, str(hdir), exhausted=_exhausted, require_nonempty=False)


def _line_substr(venue: str, symbol: str) -> str:
    """Cheap raw-line prefilter substring (avoids json.loads on non-matching
    lines). bitstamp embeds `order_book_<symbol>`; kraken/gemini embed the
    symbol verbatim in the escaped _raw."""
    if venue == "bitstamp":
        return f"order_book_{symbol}"
    return symbol  # kraken "BTC/USD" / gemini "BTCUSD" appear verbatim


def load_venue_frames_for_asset(venue: str, asset: str, day_hours):
    """Load (recv_epoch, inner) frames for ONE venue+asset across the local
    .jsonl.zst chunk tree, sorted by recv_epoch. Honors VENUE_SYMBOLS (KeyError
    if the venue doesn't constitute the asset -> caller skips)."""
    if asset not in VENUE_SYMBOLS[venue]:
        return None
    symbol = VENUE_SYMBOLS[venue][asset]
    substr = _line_substr(venue, symbol)
    vdir = os.path.join(VENUE_ROOT, VENUE_DIRS[venue])
    out = []
    for day, hour in day_hours:
        hdir = os.path.join(vdir, f"day={day:02d}", f"hour={hour:02d}", "conn=A")
        if not os.path.isdir(hdir):
            continue
        for line in _hour_lines(hdir):
            if substr not in line:  # cheap reject before any json parsing
                continue
            try:
                env = json.loads(line)
            except ValueError:
                continue
            src = env.get("_source")
            if src is not None and src != f"{venue}_ws":
                continue
            try:
                ts, inner = venue_parse_envelope(env)
            except (ValueError, KeyError):
                continue
            frame = _extract_frame(venue, inner, symbol)
            if frame is None:
                continue
            try:
                out.append((venue_epoch(ts), frame))
            except Exception:
                continue
    out.sort(key=lambda x: x[0])
    return out


def _topn_di_from_levels(bids, asks, top_n: int):
    """DI over top-N levels from raw [price, qty] level lists (bitstamp full
    snapshot). Returns (di, depth) or (None, 0.0) if empty/crossed."""
    # parse and keep best top_n per side
    bp = []
    for lv in bids:
        try:
            p = float(lv[0]); q = float(lv[1])
        except (ValueError, IndexError, TypeError):
            continue
        if q > 0:
            bp.append((p, q))
    ap = []
    for lv in asks:
        try:
            p = float(lv[0]); q = float(lv[1])
        except (ValueError, IndexError, TypeError):
            continue
        if q > 0:
            ap.append((p, q))
    if not bp or not ap:
        return None, 0.0
    bp.sort(key=lambda x: -x[0])
    ap.sort(key=lambda x: x[0])
    if bp[0][0] > ap[0][0]:  # crossed
        return None, 0.0
    bidvol = sum(q for _, q in bp[:top_n])
    askvol = sum(q for _, q in ap[:top_n])
    tot = bidvol + askvol
    if tot <= 0:
        return None, 0.0
    return (bidvol - askvol) / tot, tot


def load_bitstamp_di_series(asset: str, day_hours, top_n: int):
    """STREAMING: read bitstamp (full-snapshot-per-frame) chunks for `asset`,
    reduce each frame to (recv_epoch, di, depth) on the fly (NEVER materialize
    the 100-deep ladders -> ~100x less memory), return sorted by epoch.

    Only valid for bitstamp, whose every `order_book` frame is a complete
    top-100 both-sides snapshot (self-anchoring; no look-ahead, no baseline
    needed). Kraken/Gemini are excluded (no snapshot in this corpus)."""
    symbol = VENUE_SYMBOLS["bitstamp"][asset]  # KeyError if unsupported
    substr = f"order_book_{symbol}"
    vdir = os.path.join(VENUE_ROOT, VENUE_DIRS["bitstamp"])
    out = []
    for day, hour in day_hours:
        hdir = os.path.join(vdir, f"day={day:02d}", f"hour={hour:02d}", "conn=A")
        if not os.path.isdir(hdir):
            continue
        for line in _hour_lines(hdir):
            if substr not in line:
                continue
            try:
                env = json.loads(line)
                inner = json.loads(env["_raw"])
            except (ValueError, KeyError):
                continue
            if inner.get("channel") != substr:
                continue
            data = inner.get("data") or {}
            di, depth = _topn_di_from_levels(
                data.get("bids", []), data.get("asks", []), top_n)
            if di is None:
                continue
            try:
                ep = venue_epoch(env["_wire_recv_ts"])
            except Exception:
                continue
            out.append((ep, di, depth))
    out.sort(key=lambda x: x[0])
    return out


def di_series_at_times(di_series, decision_times):
    """Map each decision time to the most-recent (di, depth) at/before it via a
    single merge walk. di_series = sorted [(epoch, di, depth)]; decision_times
    sorted ascending. NO look-ahead. Returns {t: (di, depth)} (absent if no
    venue frame precedes t)."""
    out = {}
    i = 0
    n = len(di_series)
    last = None
    for t in decision_times:
        while i < n and di_series[i][0] <= t:
            last = (di_series[i][1], di_series[i][2])
            i += 1
        if last is not None:
            out[t] = last
    return out


def _book_di(book, top_n: int):
    """Top-N depth imbalance of a live (already-replayed) venue book.
    Returns (DI, total_depth) or (None, 0.0) if empty/crossed."""
    if book.is_crossed():
        return None, 0.0
    bid_ticks = sorted(book.bids, reverse=True)[:top_n]
    ask_ticks = sorted(book.asks)[:top_n]
    bidvol = sum(book.bids[t] for t in bid_ticks)
    askvol = sum(book.asks[t] for t in ask_ticks)
    tot = bidvol + askvol
    if tot <= 0:
        return None, 0.0
    return (bidvol - askvol) / tot, tot


def di_at_decision_times(frames, decision_times, book_cls, top_n: int):
    """SINGLE forward pass: replay sorted `frames` once, maintaining a live
    `book`, and snapshot (DI, depth) at each pre-sorted `decision_times` cutoff
    (NO look-ahead — only frames with recv_epoch <= t are applied before
    snapshotting at t). Returns {t: (DI, depth)}.

    Bitstamp self-completes (every frame is a full top-100 snapshot), so the
    book is valid from the first applied frame at/before each cutoff. Kraken /
    Gemini are excluded upstream (RELIABLE_VENUES) because the local corpus has
    NO snapshot baseline for them — replaying their pure-incremental streams
    crosses the book."""
    out = {}
    fi = 0
    nf = len(frames)
    book = book_cls()
    applied_any = False
    for t in decision_times:
        while fi < nf and frames[fi][0] <= t:
            book.apply_frame(frames[fi][1])
            applied_any = True
            fi += 1
        if not applied_any:
            out[t] = (None, 0.0)
        else:
            out[t] = _book_di(book, top_n)
    return out


# ----- label ----------------------------------------------------------------
def load_db_outcomes(tickers):
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=10000")
    out = {}
    if not tickers:
        return out
    tl = list(tickers)
    for i in range(0, len(tl), 400):
        chunk = tl[i:i + 400]
        q = ",".join("?" * len(chunk))
        sql = (f"SELECT DISTINCT ticker, market_result FROM evaluated_opportunities "
               f"WHERE ticker IN ({q}) AND market_result IN ('yes','no')")
        for r in conn.execute(sql, tuple(chunk)):
            out[r["ticker"]] = r["market_result"]
    conn.close()
    return out


def terminal_outcome_from_book(frames, close_epoch: float):
    """Fallback label: reliable NBBO just before close. If yes mid >= 50 -> yes."""
    yb, ya = reliable_nbbo_at(frames, close_epoch - 2.0)
    if yb is None or ya is None:
        return None
    mid = (yb + ya) / 2.0
    if mid >= 55:
        return "yes"
    if mid <= 45:
        return "no"
    return None  # too ambiguous to label


# ----- fees -----------------------------------------------------------------
def kalshi_fee_cents(price_cents: float) -> int:
    p = price_cents / 100.0
    return math.ceil(0.07 * 1.0 * p * (1.0 - p) * 100.0) / 100.0 if False else \
        math.ceil(0.07 * 1.0 * p * (1.0 - p) * 100.0)


def taker_net_cents(ask_cents: float, side: str, result: str) -> float:
    """Buy `side` at ask_cents (taker), pay fee, hold to settlement.
    side in {'yes','no'}; result in {'yes','no'}. Payoff 100 if side wins else 0."""
    fee = kalshi_fee_cents(ask_cents)
    won = (side == result)
    payoff = 100.0 if won else 0.0
    return payoff - ask_cents - fee


# ----- bootstrap ------------------------------------------------------------
def block_bootstrap_ci(values_by_cluster, n_boot=N_BOOTSTRAP, seed=SEED):
    """Cluster bootstrap: resample clusters (ticker,hour) with replacement,
    pool their trades, take the mean. Returns (point, lo, hi, n_trades)."""
    rng = random.Random(seed)
    clusters = list(values_by_cluster.values())
    all_vals = [v for c in clusters for v in c]
    n = len(all_vals)
    if n == 0:
        return float("nan"), float("nan"), float("nan"), 0
    point = sum(all_vals) / n
    if len(clusters) < 2:
        return point, float("nan"), float("nan"), n
    means = []
    k = len(clusters)
    for _ in range(n_boot):
        pool = []
        for _ in range(k):
            pool.extend(clusters[rng.randrange(k)])
        if pool:
            means.append(sum(pool) / len(pool))
    means.sort()
    lo = means[int(0.025 * len(means))]
    hi = means[int(0.975 * len(means))]
    return point, lo, hi, n


# ----- main -----------------------------------------------------------------
def main():
    t0 = datetime.now()
    print("[load] kalshi BTC/ETH frames ...", flush=True)
    frames_path = FRAMES_BTC_ETH if os.path.exists(FRAMES_BTC_ETH) else FRAMES_ALL
    all_frames = load_frames_jsonl(frames_path)  # {ticker: [(epoch, inner)]}
    # keep only BTC/ETH tickers
    kalshi_frames = {tk: fr for tk, fr in all_frames.items()
                     if _asset_of(tk) in ASSETS}
    print(f"[load] {len(kalshi_frames)} BTC/ETH tickers", flush=True)

    # window time bounds per ticker
    tickers = sorted(kalshi_frames)
    db_out = load_db_outcomes(set(tickers))
    print(f"[load] DB outcomes for {len(db_out)} / {len(tickers)} tickers", flush=True)

    # Determine which (day,hour) chunks we need for venue load: union of all
    # decision-time windows. Corpus is 05-30T10Z -> 05-31T17Z.
    day_hours = []
    for day in (30, 31):
        for hour in range(0, 24):
            if day == 31 and hour > 17:
                continue
            day_hours.append((day, hour))

    # Preload venue frames per asset (heavy; do once per asset).
    results = []  # (cluster_key, net_cents)
    diag = defaultdict(int)
    di_grid = defaultdict(list)  # asset -> list of |DI| for percentile

    for asset in ASSETS:
        print(f"\n[venue] loading bitstamp L2 DI series for {asset} ...", flush=True)
        # Only bitstamp is reconstructable in this corpus (see RELIABLE_VENUES).
        # Stream-reduce each full-snapshot frame to (epoch, di, depth).
        if asset not in VENUE_SYMBOLS["bitstamp"]:
            print(f"  no bitstamp feed for {asset} -> DATA_GAP", flush=True)
            continue
        di_series = load_bitstamp_di_series(asset, day_hours, TOP_N_LEVELS)
        if not di_series:
            print(f"  NO bitstamp DI for {asset} -> DATA_GAP", flush=True)
            continue
        print(f"  bitstamp: {len(di_series)} DI samples "
              f"[{datetime.fromtimestamp(di_series[0][0], timezone.utc):%m-%dT%H:%MZ}.."
              f"{datetime.fromtimestamp(di_series[-1][0], timezone.utc):%m-%dT%H:%MZ}]",
              flush=True)

        asset_tickers = [tk for tk in tickers if _asset_of(tk) == asset]

        # Build the full decision-time grid for the asset: per ticker, every 30s
        # in [open+60s, close-60s]. Tag each with its ticker.
        tk_decisions = []  # (ticker, t)
        for tk in asset_tickers:
            try:
                close_ep = close_epoch_from_ticker(tk)
            except Exception:
                continue
            open_ep = close_ep - WINDOW_LEN_S
            t = open_ep + WINDOW_OPEN_PAD_S
            t_end = close_ep - WINDOW_CLOSE_PAD_S
            while t <= t_end:
                tk_decisions.append((tk, t))
                t += SAMPLE_EVERY_S
        uniq_times = sorted({t for _, t in tk_decisions})

        # PASS 1: consensus DI per decision time (single venue here -> the
        # bitstamp DI at/before t via one merge walk; NO look-ahead).
        di_by_t = di_series_at_times(di_series, uniq_times)  # {t: (di, depth)}
        cons_di_at = {t: v[0] for t, v in di_by_t.items()}

        # Now build cached decision points (DI + reliable Kalshi book staleness).
        cached = []  # (ticker, t, di, yb, ya, mid_now, mid_prev)
        for tk, t in tk_decisions:
            cons_di = cons_di_at.get(t)
            if cons_di is None:
                diag["no_venue_book"] += 1
                continue
            di_grid[asset].append(abs(cons_di))
            fr = kalshi_frames[tk]
            yb_now, ya_now = reliable_nbbo_at(fr, t)
            if yb_now is None or ya_now is None:
                diag["kalshi_unreliable_now"] += 1
                continue
            yb_prev, ya_prev = reliable_nbbo_at(fr, t - STALE_LOOKBACK_S)
            if yb_prev is None or ya_prev is None:
                diag["kalshi_unreliable_prev"] += 1
                continue
            mid_now = (yb_now + ya_now) / 2.0
            mid_prev = (yb_prev + ya_prev) / 2.0
            cached.append((tk, t, cons_di, yb_now, ya_now, mid_now, mid_prev))

        if not di_grid[asset]:
            print(f"  {asset}: no DI samples", flush=True)
            continue
        grid = sorted(di_grid[asset])
        thresh = grid[min(len(grid) - 1, int(DI_PCT / 100.0 * len(grid)))]
        print(f"  {asset}: {len(cached)} decision points w/ reliable kalshi book; "
              f"|DI| p{DI_PCT:.0f} threshold = {thresh:.4f} "
              f"(over {len(grid)} venue-book samples)", flush=True)

        # PASS 2: apply signal + stale gate + take + settle.
        for (tk, t, cons_di, yb_now, ya_now, mid_now, mid_prev) in cached:
            if abs(cons_di) < thresh:
                continue
            diag["di_fired"] += 1
            # stale gate: kalshi mid not yet moved
            if abs(mid_now - mid_prev) > STALE_MAX_DELTA_C:
                diag["kalshi_already_moved"] += 1
                continue
            diag["stale_passed"] += 1
            # side implied by DI sign
            side = "yes" if cons_di > 0 else "no"
            # cross to reconstructed reliable ask for that side
            if side == "yes":
                take_ask = ya_now  # yes ask
            else:
                take_ask = 100.0 - yb_now  # no ask = 100 - yes bid
            if take_ask is None or take_ask <= 0 or take_ask >= 100:
                diag["no_ask_to_cross"] += 1
                continue
            # label
            result = db_out.get(tk)
            label_src = "db"
            if result is None:
                close_ep = close_epoch_from_ticker(tk)
                result = terminal_outcome_from_book(kalshi_frames[tk], close_ep)
                label_src = "book"
            if result is None:
                diag["no_label"] += 1
                continue
            net = taker_net_cents(take_ask, side, result)
            diag["traded"] += 1
            diag[f"label_{label_src}"] += 1
            # cluster by (ticker, hour-of-decision)
            hour = int(t // 3600)
            results.append(((tk, hour), net))

    # aggregate
    by_cluster = defaultdict(list)
    for ck, net in results:
        by_cluster[ck].append(net)

    point, lo, hi, n = block_bootstrap_ci(by_cluster)
    n_clusters = len(by_cluster)

    print("\n===== DIAGNOSTICS =====")
    for k in sorted(diag):
        print(f"  {k}: {diag[k]}")
    print(f"\n===== RESULT =====")
    print(f"n_trades            = {n}")
    print(f"n_clusters          = {n_clusters} (ticker,hour)")
    if n:
        wins = sum(1 for _, v in results if v > 0)
        print(f"win_rate            = {wins/n:.3f}")
    print(f"mean_net_cents      = {point:.3f}")
    print(f"95% CI              = [{lo:.3f}, {hi:.3f}]")
    print(f"elapsed             = {(datetime.now()-t0).total_seconds():.0f}s")

    return {
        "n": n, "n_clusters": n_clusters,
        "point": point, "lo": lo, "hi": hi,
        "diag": dict(diag),
    }


if __name__ == "__main__":
    main()
