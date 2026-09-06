#!/usr/bin/env python3
"""00_tbd_strike_frontrun -- TBD-Window Strike Front-Run (stale-TWAP strike at open).

PRE-REGISTRATION (stated before any result was seen)
====================================================
WHO IS THE COUNTERPARTY / WHY DO THEY PAY US:
  Opening template quoters seeding symmetric ~50c quotes BEFORE the strike is
  published, plus directional retail entering while the Kalshi UI still shows
  "Target price: TBD". Each window's strike is the backward 60s BRTI TWAP
  ending at open, published via lifecycle `metadata_updated` ~0.6-12.9s AFTER
  open; for the first 3-60s the book is quoted by participants who don't know
  the strike, and even post-publication the strike's center of mass is ~30s
  stale vs live spot, so d = spot - strike is informative.

SIGNAL:
  open = close_epoch_from_ticker(ticker) - 900.
  strike_hat = 60s TWAP of synthetic RTI (equal-weight Kraken/Bitstamp/Gemini
  top-of-book mids via scripts/research/venue_book_reconstruct.py) ending at
  open. Coinbase spot cross-check for BTC/ETH/SOL/XRP/DOGE.
  VALIDATION: strike_hat vs published floor_strike: report RMSE; if median
  |err| > 4 bps -> FALL BACK to published-strike-after-publication variant
  (decisions only at t >= publish_ts, strike = published floor_strike; still
  exploits the ~30s-stale center of mass).
  At t in [open, open+60]:
    d_sigma = (spot - strike) / (spot * sigma_remaining),
    sigma_remaining = rv_5s * sqrt((close - t) / 5),  p* = Phi(d_sigma),
  spot = synthetic RTI at t (same source as strike_hat so venue basis cancels),
  rv_5s = stdev of per-5s RTI log returns over [t-300, t].

ENTRY (ONE observation per window):
  Take the SINGLE EARLIEST frame where reliable_nbbo_timeline yields a
  reliable NBBO and p* - ask/100 > _fee_frac(ask) + 0.02 -> taker buy YES at
  ask (mirror NO: (1-p*) - no_ask/100 > _fee_frac(no_ask) + 0.02).
  Else if mid within 5c of 50 and |d| > 4 bps at the first reliable frame ->
  maker post one tick inside on the favored side; fill ONLY on a strictly-later
  real print crossing our level (taker on the opposite side, price at/through
  our level, ts > post_ts + 1s clock-skew guard).
EXIT: hold to settlement (lifecycle `determined`).

FEES: taker pays 7*p*(1-p) cents (kalshi_fee_per_contract_cents); maker pays
  zero (the taker's fee goes to Kalshi -- never credit it to ourselves).

PRE-CHECK KILL (run FIRST, cheap):
  Across qualifying windows (|d_open| > 4 bps AND reliable first-60s NBBO), if
  sign(first reliable mid - 50) vs sign(d_open) agreement > 60%, the seam is
  already priced -> DEAD (trading sim reported as informational only).
KILL CRITERION:
  Need n >= 60 qualifying windows over the 3-day frames pass; day-bootstrap
  (day_bootstrap_ci) net PnL/ct 95% CI lower bound <= 0 -> DEAD.
  n < 60 -> INCONCLUSIVE-capacity with the full funnel
  (windows -> |d|>4bps -> reliable-NBBO -> fee-clearing).

DATA: CR=/Users/gabrielkagan/kalshi-research-data/fairvalue; frames = 3 sealed
FULL days (default 2026-06-03..05; .done_<day> markers checked; 05-30 partial,
avoided), streamed via `zstd -dc` subprocess pipes (never _zst_lines on
multi-GB); lifecycle (floor_strike, publish timing, determined); venue_pull
(all days); coinbase_ticker (cross-check only). One streaming pass per frames
day, retaining only first-90s book state per window.
"""
from __future__ import annotations

import argparse
import bisect
import glob
import math
import os
import re
import statistics
import subprocess
import sys
import time
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor
from datetime import datetime, timedelta, timezone

try:
    import orjson as _json
except ImportError:  # pragma: no cover
    import json as _json

_REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)

from scripts.research import venue_book_reconstruct as vbr  # noqa: E402
from scripts.research.early_exit_backtest import reliable_nbbo_timeline  # noqa: E402
from scripts.research.phase1b_real_price_economics import (  # noqa: E402
    _epoch, close_epoch_from_ticker,
)
from scripts.research.settlement_convergence_p1a import (  # noqa: E402
    kalshi_fee_per_contract_cents,
)
from scripts.research.zstd_stream import assert_zstd_ok  # noqa: E402  (repo root on sys.path above)

CR_DEFAULT = "/Users/gabrielkagan/kalshi-research-data/fairvalue"
DAYS_DEFAULT = "2026-06-03,2026-06-04,2026-06-05"

VENUE_ASSETS = sorted({a for m in vbr.VENUE_SYMBOLS.values() for a in m})  # 7
_TICK_RE = re.compile(r'market_ticker\\":\\"(KX([A-Z]+)15M-[A-Z0-9]+-?[A-Z0-9]*)')
_ASSET_RE = re.compile(r"^KX([A-Z]+)15M-")
_FNAME_RE = re.compile(r"(\d{8}T\d{6})Z_to_(\d{8}T\d{6})Z")

GRID = 5.0                 # RTI sampling grid (s) — per-5s, mirrors rv_5s
SEG_PRE = 420.0            # venue replay starts open-420 (120s book warmup)
SEG_POST = 66.0            # venue replay ends open+66 (covers decision window)
GRID_KEEP_FROM = 305.0     # keep grid samples from open-305 (post-warmup)
RV_WIN = 300.0             # rv_5s trailing window (VOL_WINDOW_S in fairvalue_extract)
MIN_RETS = 10              # min 5s log-returns for rv
RTI_STALE_MAX = 10.0       # max staleness of an RTI sample at decision time (s)
FFILL_MAX = 30.0           # max forward-fill of a venue book onto the grid (s)
D_QUAL_BPS = 4.0           # |d| qualification threshold (bps)
EDGE_MIN = 0.02            # required edge over fee (probability units)
ENTRY_WIN = 60.0           # decisions only in [start, open+60]
MAKER_BAND_C = 5.0         # maker leg requires |mid-50| <= 5c
FILL_EPS_S = 1.0           # strictly-later guard for passive fills (clock skew)
TWAP_MIN_SAMPLES = 10      # of 13 grid points in [open-60, open]
STRIKE_VALID_BPS = 4.0     # validation gate on median |strike_hat err|
PRECHECK_AGREE = 0.60      # pre-check kill threshold
N_QUAL_MIN = 60            # capacity gate
ENDGAME_S = 120.0          # report maker fills inside last 120s separately


def _phi(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _fee_frac_c(price_cents: float) -> float:
    """Kalshi fee as a fraction of $1 notional, from a price in cents
    (identical math to scripts.research.fairvalue_model._fee_frac)."""
    p = min(max(price_cents, 1.0), 99.0) / 100.0
    return 0.07 * p * (1.0 - p)


def _fast_epoch(ts: str, cache: dict) -> float:
    """ISO '2026-06-03T08:55:17.116399Z' -> epoch, day-base cached."""
    base = cache.get(ts[:10])
    if base is None:
        base = datetime.fromisoformat(ts[:10]).replace(tzinfo=timezone.utc).timestamp()
        cache[ts[:10]] = base
    frac = float(ts[19:-1]) if len(ts) > 20 else 0.0
    return base + int(ts[11:13]) * 3600 + int(ts[14:16]) * 60 + int(ts[17:19]) + frac


def _line_ts(line: str) -> str:
    """Envelope lines start {"_wire_recv_ts":"<iso>" — slice it out cheaply."""
    end = line.find('"', 18)
    return line[18:end]


def _zstd_pipe(path: str):
    p = subprocess.Popen(["zstd", "-dc", path], stdout=subprocess.PIPE,
                         stderr=subprocess.DEVNULL)
    # ticket 86bbvrx1t: remember the path so _zstd_done can name it in the
    # truncation error. This helper RETURNS the process rather than yielding,
    # so the exit-code check cannot live here — every caller must finish with
    # _zstd_done(p) instead of a bare p.wait().
    p._zstd_path = path  # type: ignore[attr-defined]
    return p


def _zstd_done(p, *, exhausted: bool = True) -> None:
    """Wait on a `_zstd_pipe` process and RAISE if it was truncated.

    Ticket 86bbvrx1t. A bare `p.wait()` discards the exit code, so a
    decompressor that dies mid-file just ends the caller's loop and the run
    reports success on a PREFIX of the day. All four call sites in this module
    read to EOF with no `break`, hence exhausted=True by default; pass
    exhausted=False from any future caller that breaks early, since that kills
    zstd with SIGPIPE legitimately.
    """
    p.wait()
    assert_zstd_ok(p, getattr(p, "_zstd_path", "<zstd pipe>"),
                   exhausted=exhausted, require_nonempty=False)


# --------------------------------------------------------------------------
# lifecycle: determined + published strike + publication timestamp
# --------------------------------------------------------------------------

def load_lifecycle(cr: str, day_globs) -> dict:
    """{ticker: {asset,result,det_ts,strike,pub_ts}} for crypto-15M windows.
    pub_ts = EARLIEST _wire_recv_ts of a metadata_updated carrying floor_strike
    (the strike publication moment per the lifecycle-rules hypothesis)."""
    out: dict[str, dict] = {}
    files = []
    for dg in day_globs:
        files.extend(glob.glob(f"{cr}/lifecycle/{dg}/**/*.zst", recursive=True))
    for f in sorted(files):
        p = _zstd_pipe(f)
        for line in p.stdout:
            if b"15M-" not in line:
                continue
            try:
                env = _json.loads(line)
                inner = _json.loads(env["_raw"])
                msg = inner.get("msg", {})
                tk = msg.get("market_ticker", "")
                m = _ASSET_RE.match(tk)
                if not m:
                    continue
                et = msg.get("event_type")
                if et == "metadata_updated" and msg.get("floor_strike") is not None:
                    ts = _epoch(env["_wire_recv_ts"])
                    d = out.setdefault(tk, {"asset": m.group(1)})
                    if d.get("pub_ts") is None or ts < d["pub_ts"]:
                        d["pub_ts"] = ts
                        d["strike"] = float(msg["floor_strike"])
                elif et == "determined" and msg.get("result") in ("yes", "no"):
                    d = out.setdefault(tk, {"asset": m.group(1)})
                    d["result"] = msg["result"]
                    d["det_ts"] = float(msg["determination_ts"])
            except (ValueError, KeyError, TypeError):
                continue
        _zstd_done(p)
    return out


# --------------------------------------------------------------------------
# venue replay -> synthetic-RTI grid samples per (open, asset)
# --------------------------------------------------------------------------

def _seg_open(ts: float, opens: set):
    """Map epoch -> the window open whose replay segment [O-420, O+66] holds it."""
    m = ts % 900.0
    if m >= 900.0 - SEG_PRE:
        o = ts - m + 900.0
    elif m <= SEG_POST:
        o = ts - m
    else:
        return None
    o = round(o)  # kill any float-modulo dust so set membership is exact
    return o if o in opens else None


def _file_overlaps(t0: float, t1: float, opens: set) -> bool:
    t = t0 - 30.0
    while t <= t1 + 30.0:
        if _seg_open(t, opens) is not None:
            return True
        t += 15.0
    return False


def _warm_start(book_cls):
    """Wrap a vbr book for MID-STREAM (snapshot-less) replay: a new resting
    level evicts stale OPPOSITE-side levels it crosses (in reality those were
    consumed/removed before our segment started). No-op once converged — in an
    anchored stream a quoted bid never crosses the true ask. Convergence
    quality is measured empirically by the strike_hat validation gate."""
    class W(book_cls):
        def apply_delta(self, side, price, qty):
            super().apply_delta(side, price, qty)
            if float(qty) <= 1e-12:
                return
            t = vbr._tick(price)
            if side.lower() in ("bid", "buy", "b"):
                while self.asks:
                    ma = min(self.asks)
                    if ma > t:
                        break
                    del self.asks[ma]
            else:
                while self.bids:
                    mb = max(self.bids)
                    if mb < t:
                        break
                    del self.bids[mb]
    return W


def venue_job(args):
    """One (venue, day) pass. Returns {(open, asset): {grid_epoch: rti_mid}}.

    Books start EMPTY at segment start (open-420); the first 120s are warmup
    (absolute-set L2 feeds converge near the touch quickly) and their grid
    samples are DISCARDED (GRID_KEEP_FROM). Convergence quality is measured
    empirically downstream by the strike_hat-vs-published validation gate."""
    venue, day, opens_list, cr = args
    opens = set(opens_list)
    symmap = vbr.VENUE_SYMBOLS[venue]
    rev = {s: a for a, s in symmap.items()}
    book_cls = {"kraken": _warm_start(vbr.KrakenBook), "bitstamp": vbr.BitstampBook,
                "gemini": _warm_start(vbr.GeminiBook)}[venue]

    d = datetime.fromisoformat(day)
    prev = (d - timedelta(days=1)).date()
    pats = [f"{cr}/venue_pull/{venue}_ws/year={d.year}/month={d.month:02d}/day={d.day:02d}/**/*.zst",
            f"{cr}/venue_pull/{venue}_ws/year={prev.year}/month={prev.month:02d}/day={prev.day:02d}/hour=23/**/*.zst"]
    files = []
    for pat in pats:
        for f in glob.glob(pat, recursive=True):
            m = _FNAME_RE.search(os.path.basename(f))
            if not m:
                continue
            t0 = datetime.strptime(m.group(1), "%Y%m%dT%H%M%S").replace(
                tzinfo=timezone.utc).timestamp()
            t1 = datetime.strptime(m.group(2), "%Y%m%dT%H%M%S").replace(
                tzinfo=timezone.utc).timestamp()
            if _file_overlaps(t0, t1, opens):
                files.append((t0, f))
    files.sort()

    out: dict = defaultdict(dict)
    books: dict = {}
    last_grid: dict = {}
    last_fts: dict = {}
    cur_o = None
    cache: dict = {}

    def emit(a, upto, o):
        """Record grid mids for points strictly before `upto` (state = all
        frames already applied), bounded by forward-fill staleness."""
        g = last_grid[a] + GRID
        lt = last_fts.get(a)
        while g < upto:
            if (lt is not None and g <= lt + FFILL_MAX
                    and g >= o - GRID_KEEP_FROM and g <= o + SEG_POST):
                mid = books[a].mid()
                if mid is not None:
                    out[(o, a)][g] = mid
            last_grid[a] = g
            g += GRID

    def reset(o):
        for a in symmap:
            books[a] = book_cls()
            last_grid[a] = GRID * math.floor((o - SEG_PRE) / GRID)
        last_fts.clear()

    def flush(o):
        for a in symmap:
            emit(a, o + SEG_POST + GRID, o)

    def _route(inner, ts, o):
        if venue == "kraken":
            if inner.get("channel") != "book":
                return
            t = inner.get("type")
            for e in inner.get("data", []):
                asset = rev.get(e.get("symbol"))
                if asset is None:
                    continue
                emit(asset, ts, o)
                if t == "snapshot":
                    books[asset].apply_snapshot(e.get("bids", []), e.get("asks", []))
                elif t == "update":
                    for lv in e.get("bids", []):
                        books[asset].apply_delta("bid", lv["price"], lv["qty"])
                    for lv in e.get("asks", []):
                        books[asset].apply_delta("ask", lv["price"], lv["qty"])
                last_fts[asset] = ts
        elif venue == "bitstamp":
            ch = inner.get("channel", "")
            if not ch.startswith("order_book_"):
                return
            asset = rev.get(ch[len("order_book_"):])
            if asset is None:
                return
            data = inner.get("data") or {}
            if "bids" not in data and "asks" not in data:
                return
            emit(asset, ts, o)
            books[asset].apply_snapshot(data.get("bids", []), data.get("asks", []))
            last_fts[asset] = ts
        else:  # gemini
            if inner.get("type") != "l2_updates":
                return
            asset = rev.get(inner.get("symbol"))
            if asset is None:
                return
            emit(asset, ts, o)
            books[asset].apply_frame(inner)
            last_fts[asset] = ts

    for _t0, f in files:
        p = _zstd_pipe(f)
        for bline in p.stdout:
            line = bline.decode("utf-8", "replace")
            try:
                ts = _fast_epoch(_line_ts(line), cache)
            except (ValueError, IndexError):
                continue
            o = _seg_open(ts, opens)
            if o is None:
                continue
            if o != cur_o:
                if cur_o is not None:
                    flush(cur_o)
                reset(o)
                cur_o = o
            try:
                inner = _json.loads(_json.loads(line)["_raw"])
            except (ValueError, KeyError):
                continue
            try:
                _route(inner, ts, o)
            except (KeyError, TypeError, ValueError):
                continue
        _zstd_done(p)
    if cur_o is not None:
        flush(cur_o)
    return dict(out)


# --------------------------------------------------------------------------
# frames pass -> per-window reliable NBBO timeline (first 90s only retained)
# --------------------------------------------------------------------------

def frames_job(args):
    """One streaming pass over a frames day. Buffers raw frames ONLY while
    ts <= open+90 per window (memory-bounded), finalizes once the stream clock
    passes open+90+150, and returns the reliable-NBBO timeline clipped to
    [open-30, open+70] per ticker (decision data only — exit is settlement)."""
    day, win_opens, cr = args  # win_opens: {ticker: open_epoch}
    path = f"{cr}/frames/day={day}.jsonl.zst"
    res: dict = {}
    buffers: dict = defaultdict(list)
    done: set = set()
    cache: dict = {}
    stream_ts = 0.0
    n_lines = 0

    def finalize(tk):
        buf = buffers.pop(tk)
        buf.sort(key=lambda x: x[0])
        o = win_opens[tk]
        tl = reliable_nbbo_timeline(buf)
        res[tk] = {
            "tl": [p for p in tl if o - 30.0 <= p[0] <= o + 70.0],
            "n_frames": len(buf),
            "snap": any(fr.get("type") == "orderbook_snapshot" for _, fr in buf),
        }
        done.add(tk)

    p = _zstd_pipe(path)
    for bline in p.stdout:
        n_lines += 1
        line = bline.decode("utf-8", "replace")
        m = _TICK_RE.search(line)
        if not m:
            continue
        tk = m.group(1)
        if tk in done or tk not in win_opens:
            continue
        try:
            ts = _fast_epoch(_line_ts(line), cache)
        except (ValueError, IndexError):
            continue
        if ts > stream_ts:
            stream_ts = ts
        o = win_opens[tk]
        if ts <= o + 90.0 and len(buffers[tk]) < 400_000:
            try:
                inner = _json.loads(_json.loads(line)["_raw"])
            except (ValueError, KeyError):
                continue
            buffers[tk].append((ts, inner))
        if n_lines % 100_000 == 0:
            for t in [t for t in buffers if stream_ts > win_opens[t] + 240.0]:
                finalize(t)
    _zstd_done(p)
    for t in list(buffers):
        finalize(t)
    return day, res, n_lines


# --------------------------------------------------------------------------
# trades pass -> real prints for maker-fill adjudication
# --------------------------------------------------------------------------

def load_prints(cr: str, day: str, tickers: set) -> dict:
    """{ticker: [(ts_s, yes_price_c, taker_side)]} sorted per ticker (the raw
    stream is arrival-ordered, NOT time-sorted — gotcha)."""
    out: dict = defaultdict(list)
    if not tickers:
        return out
    p = _zstd_pipe(f"{cr}/trades/day={day}.jsonl.zst")
    for bline in p.stdout:
        line = bline.decode("utf-8", "replace")
        m = _TICK_RE.search(line)
        if not m or m.group(1) not in tickers:
            continue
        try:
            msg = _json.loads(_json.loads(line)["_raw"])["msg"]
            out[msg["market_ticker"]].append(
                (msg["ts_ms"] / 1000.0,
                 round(float(msg["yes_price_dollars"]) * 100.0),
                 msg["taker_side"]))
        except (ValueError, KeyError, TypeError):
            continue
    _zstd_done(p)
    for tk in out:
        out[tk].sort(key=lambda x: x[0])
    return out


# --------------------------------------------------------------------------
# per-window evaluation
# --------------------------------------------------------------------------

class Rti:
    """Combined equal-weight synthetic RTI on the 5s grid for one (open, asset)."""

    def __init__(self, venue_grids: list):
        merged = defaultdict(list)
        for vg in venue_grids:
            for g, mid in vg.items():
                merged[g].append(mid)
        self.gs = sorted(merged)
        self.vals = [sum(merged[g]) / len(merged[g]) for g in self.gs]
        self.nv = [len(merged[g]) for g in self.gs]

    def at(self, t: float):
        i = bisect.bisect_right(self.gs, t) - 1
        if i < 0 or self.gs[i] < t - RTI_STALE_MAX:
            return None
        return self.vals[i]

    def rv5(self, t: float):
        """stdev of log returns between CONSECUTIVE (5s apart) grid samples in
        [t-RV_WIN, t]; None if < MIN_RETS returns."""
        lo = bisect.bisect_left(self.gs, t - RV_WIN)
        hi = bisect.bisect_right(self.gs, t)
        rets = []
        for i in range(lo + 1, hi):
            if self.gs[i] - self.gs[i - 1] == GRID and self.vals[i - 1] > 0:
                rets.append(math.log(self.vals[i] / self.vals[i - 1]))
        if len(rets) < MIN_RETS:
            return None
        return statistics.stdev(rets)

    def twap(self, t0: float, t1: float, min_n: int):
        lo = bisect.bisect_left(self.gs, t0)
        hi = bisect.bisect_right(self.gs, t1)
        vals = self.vals[lo:hi]
        if len(vals) < min_n:
            return None
        return sum(vals) / len(vals)


def decision_points(tl, o):
    """Carried standing book at t=open (from the last reliable point <= open,
    if within 30s) + every reliable point in (open, open+60]."""
    pts = []
    carried = None
    for ts, bid, ask in tl:
        if ts <= o:
            if ts >= o - 30.0 and bid is not None and ask is not None:
                carried = (o, bid, ask)
        elif ts <= o + ENTRY_WIN:
            pts.append((ts, bid, ask))
        else:
            break
    if carried is not None:
        pts.insert(0, carried)
    return [(t, b, a) for (t, b, a) in pts if b is not None and a is not None]


def evaluate_window(w, rti, pts, strike_used, t_start):
    """Apply the pre-registered entry rule. Returns an obs dict or None."""
    o, c = w["open"], w["close"]
    first_valid = None
    for ts, bid, ask in pts:
        if ts < t_start:
            continue
        spot = rti.at(ts)
        if spot is None or spot <= 0:
            continue
        rv = rti.rv5(ts)
        if rv is None or rv <= 0:
            continue
        d = spot - strike_used
        sig = rv * math.sqrt(max(c - ts, 1.0) / GRID)
        p_star = _phi(d / (spot * sig))
        if first_valid is None:
            first_valid = (ts, bid, ask, d, p_star)
        if p_star - ask / 100.0 > _fee_frac_c(ask) + EDGE_MIN:
            return {"kind": "taker", "side": "yes", "px": ask, "ts": ts,
                    "p_star": p_star, "d_bps": d / strike_used * 1e4}
        no_ask = 100.0 - bid
        if (1.0 - p_star) - no_ask / 100.0 > _fee_frac_c(no_ask) + EDGE_MIN:
            return {"kind": "taker", "side": "no", "px": no_ask, "ts": ts,
                    "p_star": p_star, "d_bps": d / strike_used * 1e4}
    if first_valid is None:
        return None
    ts, bid, ask, d, p_star = first_valid
    mid = (bid + ask) / 2.0
    d_bps = d / strike_used * 1e4
    if abs(mid - 50.0) <= MAKER_BAND_C and abs(d_bps) > D_QUAL_BPS and ask - bid >= 2:
        if d > 0:
            return {"kind": "maker", "side": "yes", "px": bid + 1, "ts": ts,
                    "p_star": p_star, "d_bps": d_bps, "yes_level": bid + 1}
        return {"kind": "maker", "side": "no", "px": 100 - ask + 1, "ts": ts,
                "p_star": p_star, "d_bps": d_bps, "yes_level": ask - 1}
    return None


def maker_fill(obs, prints, close_ts):
    """Strictly-later real print at/through our level, opposite-side taker."""
    for ts, px_c, taker in prints:
        if ts <= obs["ts"] + FILL_EPS_S or ts >= close_ts:
            continue
        if obs["side"] == "yes" and taker == "no" and px_c <= obs["yes_level"]:
            return ts
        if obs["side"] == "no" and taker == "yes" and px_c >= obs["yes_level"]:
            return ts
    return None


def pnl_cents(obs, result):
    win = (result == obs["side"])
    gross = (100.0 - obs["px"]) if win else -obs["px"]
    fee = kalshi_fee_per_contract_cents(obs["px"]) if obs["kind"] == "taker" else 0.0
    return gross, gross - fee


# --------------------------------------------------------------------------
# Coinbase cross-check (spec: BTC/ETH/SOL/XRP/DOGE)
# --------------------------------------------------------------------------

def cb_crosscheck(cr: str, days, windows):
    from scripts.research.fairvalue_extract import load_spot, _spot_at
    spot = {}
    for day in days:
        d = datetime.fromisoformat(day)
        sub = f"{cr}/coinbase_ticker/year={d.year}/month={d.month:02d}/day={d.day:02d}"
        if not os.path.isdir(sub):
            continue
        for a, (secs, px) in load_spot(sub).items():
            if a in spot:
                s0, p0 = spot[a]
                s0.extend(secs); p0.extend(px)
            else:
                spot[a] = (list(secs), list(px))
    for a in spot:
        pairs = sorted(zip(*spot[a]))
        spot[a] = ([s for s, _ in pairs], [p for _, p in pairs])
    errs = []
    for w in windows.values():
        tl = spot.get(w["asset"])
        if tl is None or w.get("strike") is None:
            continue
        vals = [v for v in (_spot_at(tl, w["open"] - k * GRID) for k in range(13))
                if v is not None]
        if len(vals) < TWAP_MIN_SAMPLES:
            continue
        errs.append((sum(vals) / len(vals) - w["strike"]) / w["strike"] * 1e4)
    return errs


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------

def _median(xs):
    return statistics.median(xs) if xs else float("nan")


def _rmse(xs):
    return math.sqrt(sum(x * x for x in xs) / len(xs)) if xs else float("nan")


def run(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--cr", default=CR_DEFAULT)
    ap.add_argument("--days", default=DAYS_DEFAULT)
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--skip-cb", action="store_true")
    a = ap.parse_args(argv)
    days = [d.strip() for d in a.days.split(",") if d.strip()]
    t_wall = time.time()

    for d in days:
        if not os.path.exists(f"{a.cr}/.done_{d}"):
            print(f"FATAL: day {d} not sealed (missing {a.cr}/.done_{d})")
            print("VERDICT: DATA_GAP")
            return

    # ---- lifecycle -------------------------------------------------------
    dts = [datetime.fromisoformat(d) for d in days]
    lc_days = sorted({(x.year, x.month, x.day) for x in dts}
                     | {((x + timedelta(days=1)).year, (x + timedelta(days=1)).month,
                         (x + timedelta(days=1)).day) for x in dts})
    day_globs = [f"year={y}/month={m:02d}/day={dd:02d}" for y, m, dd in lc_days]
    print(f"[lc] loading lifecycle ({len(day_globs)} day partitions) ...")
    lc = load_lifecycle(a.cr, day_globs)

    windows: dict = {}
    n_lc = {"total": 0, "no_result": 0, "non_venue_asset": 0, "no_strike_pub": 0}
    day_set = set(days)
    for tk, d in lc.items():
        try:
            c = close_epoch_from_ticker(tk)
        except (ValueError, IndexError):
            continue
        o = c - 900.0
        o_day = datetime.fromtimestamp(o, tz=timezone.utc).strftime("%Y-%m-%d")
        if o_day not in day_set:
            continue
        n_lc["total"] += 1
        if d.get("result") not in ("yes", "no"):
            n_lc["no_result"] += 1
            continue
        if d["asset"] not in VENUE_ASSETS:
            n_lc["non_venue_asset"] += 1   # ADA/BCH: no CFB venue feed on disk
            continue
        if d.get("strike") is None or d.get("pub_ts") is None:
            n_lc["no_strike_pub"] += 1
            continue
        windows[tk] = {"asset": d["asset"], "result": d["result"],
                       "strike": d["strike"], "pub_ts": d["pub_ts"],
                       "open": o, "close": c, "day": o_day}
    print(f"[lc] windows on {days}: total={n_lc['total']} "
          f"no_result={n_lc['no_result']} ada_bch_excluded={n_lc['non_venue_asset']} "
          f"no_published_strike={n_lc['no_strike_pub']} usable={len(windows)}")
    lags = [w["pub_ts"] - w["open"] for w in windows.values()]
    if lags:
        qs = statistics.quantiles(lags, n=10)
        print(f"[lc] strike publish lag vs open (s): median={_median(lags):.1f} "
              f"p10={qs[0]:.1f} p90={qs[8]:.1f} min={min(lags):.1f} max={max(lags):.1f}")

    # ---- venue replay -> synthetic RTI ----------------------------------
    opens_by_day = defaultdict(set)
    for w in windows.values():
        opens_by_day[w["day"]].add(w["open"])
    jobs = [(v, d, sorted(opens_by_day[d]), a.cr)
            for v in vbr.VENUES for d in days]
    print(f"[rti] replaying {len(jobs)} venue-day jobs (workers={a.workers}) ...")
    t0 = time.time()
    venue_grids: dict = defaultdict(list)
    with ProcessPoolExecutor(max_workers=a.workers) as ex:
        for res in ex.map(venue_job, jobs):
            for key, grid in res.items():
                venue_grids[key].append(grid)
    print(f"[rti] done in {time.time() - t0:.0f}s; (open,asset) keys={len(venue_grids)}")

    rtis = {key: Rti(grids) for key, grids in venue_grids.items()}

    # strike_hat + validation
    errs_bps = []
    n_hat = 0
    for tk, w in windows.items():
        rti = rtis.get((w["open"], w["asset"]))
        w["rti"] = rti
        w["strike_hat"] = rti.twap(w["open"] - 60.0, w["open"], TWAP_MIN_SAMPLES) if rti else None
        if w["strike_hat"] is not None:
            n_hat += 1
            errs_bps.append((w["strike_hat"] - w["strike"]) / w["strike"] * 1e4)
    med_abs = _median([abs(e) for e in errs_bps])
    print(f"[val] strike_hat coverage {n_hat}/{len(windows)}; vs published floor_strike: "
          f"n={len(errs_bps)} RMSE={_rmse(errs_bps):.2f}bps median|err|={med_abs:.2f}bps "
          f"median_err={_median(errs_bps):+.2f}bps")
    by_asset = defaultdict(list)
    for tk, w in windows.items():
        if w.get("strike_hat") is not None:
            by_asset[w["asset"]].append(abs((w["strike_hat"] - w["strike"]) / w["strike"] * 1e4))
    for asset in sorted(by_asset):
        v = by_asset[asset]
        print(f"[val]   {asset:5s} n={len(v):4d} median|err|={_median(v):.2f}bps")

    if not a.skip_cb:
        try:
            cb_errs = cb_crosscheck(a.cr, days, windows)
            print(f"[val] coinbase TWAP cross-check vs published strike: n={len(cb_errs)} "
                  f"median|err|={_median([abs(e) for e in cb_errs]):.2f}bps "
                  f"RMSE={_rmse(cb_errs):.2f}bps")
        except Exception as e:  # cross-check is informational only
            print(f"[val] coinbase cross-check failed: {e!r}")

    validation_pass = (med_abs <= STRIKE_VALID_BPS) if errs_bps else False
    variant = "primary_strike_hat" if validation_pass else "fallback_published_strike"
    print(f"[val] validation {'PASS' if validation_pass else 'FAIL'} "
          f"(gate median|err|<= {STRIKE_VALID_BPS}bps) -> variant = {variant}")

    # ---- frames pass ------------------------------------------------------
    print(f"[frames] streaming {len(days)} frames days ...")
    t0 = time.time()
    fjobs = [(d, {tk: w["open"] for tk, w in windows.items() if w["day"] == d}, a.cr)
             for d in days]
    tls: dict = {}
    with ProcessPoolExecutor(max_workers=min(3, a.workers)) as ex:
        for day, res, n_lines in ex.map(frames_job, fjobs):
            tls.update(res)
            print(f"[frames]   {day}: {n_lines} lines, {len(res)} windows finalized")
    print(f"[frames] done in {time.time() - t0:.0f}s")

    # ---- per-window signal + funnel --------------------------------------
    # Funnel + pre-check are defined on the CHOSEN variant's strike reference.
    funnels = {}
    all_results = {}
    for var in ("primary_strike_hat", "fallback_published_strike"):
        fn = {"windows": 0, "strike_ref": 0, "d_open": 0, "qual_d": 0,
              "qual_nbbo": 0, "qual_both": 0}
        agree = disagree = 0
        obs_list = []
        for tk, w in windows.items():
            fn["windows"] += 1
            rti = w.get("rti")
            strike_used = w.get("strike_hat") if var == "primary_strike_hat" else w["strike"]
            t_start = w["open"] if var == "primary_strike_hat" else max(w["open"], w["pub_ts"])
            if strike_used is None or rti is None:
                continue
            fn["strike_ref"] += 1
            spot_o = rti.at(w["open"])
            if spot_o is None:
                continue
            fn["d_open"] += 1
            d_open_bps = (spot_o - strike_used) / strike_used * 1e4
            pts = decision_points(tls.get(tk, {}).get("tl", []), w["open"])
            has_nbbo = bool(pts)
            qual_d = abs(d_open_bps) > D_QUAL_BPS
            if qual_d:
                fn["qual_d"] += 1
            if has_nbbo:
                fn["qual_nbbo"] += 1
            if qual_d and has_nbbo:
                fn["qual_both"] += 1
                first_mid = (pts[0][1] + pts[0][2]) / 2.0
                if first_mid != 50.0:
                    if (first_mid > 50.0) == (d_open_bps > 0):
                        agree += 1
                    else:
                        disagree += 1
            if not has_nbbo:
                continue
            obs = evaluate_window(w, rti, pts, strike_used, t_start)
            if obs is not None:
                obs["ticker"] = tk
                obs["day"] = w["day"]
                obs["d_open_bps"] = d_open_bps
                obs_list.append(obs)
        funnels[var] = (fn, agree, disagree)
        all_results[var] = obs_list

    # ---- maker fills (real prints) ----------------------------------------
    need = defaultdict(set)
    for var, obs_list in all_results.items():
        for obs in obs_list:
            if obs["kind"] == "maker":
                need[windows[obs["ticker"]]["day"]].add(obs["ticker"])
    prints = {}
    for day, tks in need.items():
        prints.update(load_prints(a.cr, day, tks))

    import pandas as pd
    from scripts.research.fairvalue_model import day_bootstrap_ci

    summary = {}
    for var, obs_list in all_results.items():
        fn, agree, disagree = funnels[var]
        rows, n_taker, n_post, n_fill, n_endgame_fill = [], 0, 0, 0, 0
        for obs in obs_list:
            w = windows[obs["ticker"]]
            if obs["kind"] == "maker":
                n_post += 1
                fts = maker_fill(obs, prints.get(obs["ticker"], []), w["close"])
                if fts is None:
                    continue
                n_fill += 1
                if w["close"] - fts <= ENDGAME_S:
                    n_endgame_fill += 1
            else:
                n_taker += 1
            gross, net = pnl_cents(obs, w["result"])
            rows.append({"day": obs["day"], "ticker": obs["ticker"],
                         "kind": obs["kind"], "side": obs["side"], "px": obs["px"],
                         "gross_c": gross, "net_c": net,
                         "d_open_bps": obs["d_open_bps"], "p_star": obs["p_star"]})
        df = pd.DataFrame(rows)
        n_pc = agree + disagree
        agree_rate = agree / n_pc if n_pc else float("nan")
        print(f"\n===== VARIANT {var} =====")
        print(f"[funnel] windows={fn['windows']} -> strike_ref={fn['strike_ref']} "
              f"-> rti@open={fn['d_open']} -> |d_open|>{D_QUAL_BPS}bps={fn['qual_d']} "
              f"-> first-60s reliable NBBO={fn['qual_nbbo']} -> QUALIFYING(both)={fn['qual_both']}")
        print(f"[precheck] sign(first_mid-50) vs sign(d_open): agree={agree} "
              f"disagree={disagree} rate={agree_rate:.3f} (kill if > {PRECHECK_AGREE})")
        print(f"[trades] taker_entries={n_taker} maker_posts={n_post} "
              f"maker_fills={n_fill} (endgame<={ENDGAME_S:.0f}s fills={n_endgame_fill}) "
              f"-> executed obs n={len(df)}")
        ci = None
        if len(df):
            mean, lo, hi = day_bootstrap_ci(df, "net_c")
            g = df["gross_c"].mean()
            print(f"[pnl] n={len(df)} gross/ct={g:+.2f}c net/ct={mean:+.2f}c "
                  f"day-bootstrap 95% CI [{lo:+.2f}, {hi:+.2f}]c "
                  f"days={df['day'].nunique()}")
            for kind in ("taker", "maker"):
                sub = df[df["kind"] == kind]
                if len(sub):
                    print(f"[pnl]   {kind}: n={len(sub)} net/ct={sub['net_c'].mean():+.2f}c "
                          f"win={(sub['net_c'] > 0).mean():.2f}")
            ci = (mean, lo, hi)
        else:
            print("[pnl] no executed observations")
        summary[var] = {"fn": fn, "agree_rate": agree_rate, "n_pc": n_pc,
                        "n_qual": fn["qual_both"], "df": df, "ci": ci,
                        "n_taker": n_taker, "n_post": n_post, "n_fill": n_fill}

    # ---- verdict against the PRE-REGISTERED criterion ---------------------
    s = summary[variant]
    print(f"\n===== VERDICT (pre-registered; variant = {variant}) =====")
    verdict, why = None, ""
    if s["n_qual"] < N_QUAL_MIN:
        verdict = "INCONCLUSIVE"
        why = (f"capacity: only {s['n_qual']} qualifying windows "
               f"(|d_open|>{D_QUAL_BPS}bps AND reliable first-60s NBBO) < {N_QUAL_MIN}")
    elif s["n_pc"] and s["agree_rate"] > PRECHECK_AGREE:
        verdict = "NO_EDGE"
        why = (f"pre-check kill: first-60s mid already agrees with d_open "
               f"{s['agree_rate']:.1%} > {PRECHECK_AGREE:.0%} -> seam priced")
    elif s["ci"] is None:
        verdict = "NO_EDGE"
        why = "zero fee-clearing entries across qualifying windows (cannot show CI lower > 0)"
    elif s["ci"][1] <= 0:
        verdict = "NO_EDGE"
        why = (f"day-bootstrap net PnL/ct CI lower bound {s['ci'][1]:+.2f}c <= 0 "
               f"(mean {s['ci'][0]:+.2f}c, n={len(s['df'])})")
    else:
        verdict = "EDGE_CANDIDATE"
        why = (f"net {s['ci'][0]:+.2f}c/ct, day-bootstrap CI "
               f"[{s['ci'][1]:+.2f}, {s['ci'][2]:+.2f}]c, n={len(s['df'])}, "
               f"n_qual={s['n_qual']}")
    print(f"VERDICT: {verdict} -- {why}")
    print(f"[wall] total {time.time() - t_wall:.0f}s")
    return verdict, why, summary, variant, med_abs


if __name__ == "__main__":
    run()
