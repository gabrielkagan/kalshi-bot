"""Terminal TWAP-Lock Repricing — pre-registered VALIDATION run (GENHUNT #01b).

Validates the GENHUNT round's #01 INCONCLUSIVE candidate (lock arm n=53,
+18.85 c/ct, CI [+16.80, +20.75], fresh-quote subset +14.92c — highest point
estimate of the round) against corrected, pre-registered promotion criteria.
The original registration's flaws (frequency gate measured on a different
side/threshold definition than the entry rule; degraded 2-venue index; no
tape validation) are fixed here — entry rule, decision grid, thresholds, fee
model and the TWAP variance math stay byte-identical to 01_twap_lock.py
(remaining_weight_sq_sum is imported from it, not copied).

PRE-REGISTERED PROMOTION CRITERIA (fixed BEFORE this run; all three must pass):
  (1) FREQUENCY >= 0.75 lock-entry events/day measured on the SAME
      side/threshold definition as the entry rule (both sides, p_lock > 0.95,
      tradeable quote, edge > fee + 3c) across all 12 days.
      [context also reported: raw lock-STATE windows/day]
  (2) PNL: day-bootstrap CI lower bound > 0 with n >= 50 fills across
      >= 8 distinct days.
  (3) PRINT CROSS-CHECK (decisive falsifier): every simulated taker entry
      must be tape-validated — EITHER a real print for that ticker in
      CR/trades within +-30s of entry whose yes-price is within 2c of the
      book side we lift (ask for YES entries; bid for NO entries, since
      no_price = 100 - yes_price), OR the cached reliable NBBO holding
      at-or-below our assumed entry for >= 5 continuous seconds spanning the
      entry. Pass fraction < 70% -> stale-book artifact -> NOT PROMOTED
      regardless of PnL.

INDEX (best honest available; upgrade from the original 2-venue subset):
  Equal-weight mean of FRESH (<=30s) per-venue inputs at each decision time:
    Coinbase ticker last + Kraken L2 mid + Bitstamp order_book mid +
    Gemini l2 mid (book semantics from venue_book_reconstruct; never-crossed
    mids only). Per-asset venue sets = venues that actually carry the asset
    in bronze (measured and printed per day). Expected coverage:
      BTC/ETH/SOL 4-venue; XRP 3 (Gemini doesn't list XRP); DOGE 2
      (Coinbase+Gemini — Kraken bronze carries NO DOGE frames, verified
      2026-06-11); HYPE 2 (Kraken+Bitstamp); BNB 1 (Kraken only —
      single-venue index, flagged in the per-asset breakdown).
  COMPUTE NOTE: full 4-venue replay across all 12 days proved affordable
  (orjson double-parse ~1M lines/s measured), so NO asset/day subset was
  taken — the pre-authorized fallback subset (BTC+ETH 4-venue, alts
  Coinbase+Bitstamp) was NOT needed.

DECLARED DEVIATIONS / HONESTY NOTES:
  * Kraken/Gemini day partitions usually lack a session-start snapshot (the
    venue conn persists across UTC days; e.g. 2026-06-03's first Kraken
    snapshot frame appears 06:38Z). Books are therefore WARM-STARTED from
    absolute-set updates at day start (top-of-book converges in seconds on
    these active books; both venues SET absolute sizes incl. removals);
    snapshots re-anchor whenever they appear; crossed books emit no mid.
  * Venue mids are emitted only inside [Q-420s, Q] per quarter-hour Q (420s
    = 90s decision horizon + 300s vol lookback + slack).
  * NO LOOK-AHEAD: every index/vol read uses 1-second buckets STRICTLY
    before the read time (bisect_left), i.e. only data with ts < t.
  * Vol input: Coinbase full-day 1s timeline where the asset is listed on
    Coinbase, else Kraken in-region mids (HYPE, BNB).
  * Settlements: load_determined re-run fresh over the lifecycle day
    partitions (run-days +-1 day) — no pickle/mtime caches anywhere.
  * Kalshi NBBO comes from the day-keyed frames_nbbo cache
    (scripts.research.nbbo_cache; .done-marker sealed, never mtime).

Usage:
  python3 scripts/research/genhunt/01b_twap_lock_validation.py \
      [--days 2026-05-30,...,2026-06-10] [--workers 5]
"""
from __future__ import annotations

import argparse
import bisect
import glob
import importlib
import math
import os
import re
import subprocess
import sys
import time
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor
from datetime import date, datetime, timedelta, timezone

import orjson

_REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)

from scripts.research import nbbo_cache  # noqa: E402
from scripts.research.fairvalue_model import _fee_frac, day_bootstrap_ci  # noqa: E402
from scripts.research.phase1b_real_price_economics import (  # noqa: E402
    close_epoch_from_ticker, load_determined,
)
from scripts.research.settlement_convergence_p1a import (  # noqa: E402
    kalshi_fee_per_contract_cents,
)
from scripts.research.venue_book_reconstruct import _Book  # noqa: E402
from scripts.research.zstd_stream import assert_zstd_ok  # noqa: E402  (repo root on sys.path above)

# lock-step with the original evaluator: variance math + norm cdf are IMPORTED
_mod01 = importlib.import_module("scripts.research.genhunt.01_twap_lock")
remaining_weight_sq_sum = _mod01.remaining_weight_sq_sum
_norm_cdf = _mod01._norm_cdf

CR = os.path.expanduser("~/kalshi-research-data/fairvalue")
DEFAULT_DAYS = ("2026-05-30,2026-05-31,2026-06-01,2026-06-02,2026-06-03,"
                "2026-06-04,2026-06-05,2026-06-06,2026-06-07,2026-06-08,"
                "2026-06-09,2026-06-10")
TRACKED = ("BTC", "ETH", "SOL", "XRP", "DOGE", "HYPE", "BNB")

# entry-rule constants — IDENTICAL to 01_twap_lock.py (no retune)
N_PRINTS = 60
DEC_FROM, DEC_TO, DEC_STEP = 90, 10, 5
STALE_S = 30.0
MIN_PARTIAL_COVER = 0.9
EDGE_OVER_FEE = 0.03
LOCK_LO, LOCK_HI = 0.05, 0.95

REGION_S = 420            # venue-mid emission region before each quarter-hour
VOL_WINDOW_S = 300.0      # mirrors fairvalue_extract VOL_WINDOW_S
VOL_STEP_S = 5.0
PRINT_WINDOW_S = 30.0     # criterion 3(a): print within +-30s of entry
PRINT_TOL_C = 2.0         # ... at a yes-price within 2c of the lifted side
SPAN_MIN_S = 5.0          # criterion 3(b): quote persistence >= 5s
FREQ_FLOOR_PER_DAY = 0.75
MIN_FILLS, MIN_FILL_DAYS = 50, 8
XCHECK_FLOOR = 0.70

# Coinbase product ids (None -> not listed; HYPE/BNB)
CB_PRODUCT = {"BTC": "BTC-USD", "ETH": "ETH-USD", "SOL": "SOL-USD",
              "XRP": "XRP-USD", "DOGE": "DOGE-USD"}
KRAKEN_SYM = {"BTC": "BTC/USD", "ETH": "ETH/USD", "SOL": "SOL/USD",
              "XRP": "XRP/USD", "DOGE": "DOGE/USD", "HYPE": "HYPE/USD",
              "BNB": "BNB/USD"}   # DOGE expected absent in bronze; measured
BITSTAMP_SYM = {"BTC": "btcusd", "ETH": "ethusd", "SOL": "solusd",
                "XRP": "xrpusd", "HYPE": "hypeusd"}
GEMINI_SYM = {"BTC": "BTCUSD", "ETH": "ETHUSD", "SOL": "SOLUSD",
              "DOGE": "DOGEUSD"}


# ------------------------------------------------------------ ts helpers ----

class _TsCache(dict):
    """Second-resolution ISO prefix -> epoch float (one strptime per second)."""

    def epoch(self, iso_sec: str) -> float:
        v = self.get(iso_sec)
        if v is None:
            v = datetime.strptime(iso_sec, "%Y-%m-%dT%H:%M:%S").replace(
                tzinfo=timezone.utc).timestamp()
            self[iso_sec] = v
        return v


def _in_region(sec: float) -> bool:
    return sec % 900.0 >= 900.0 - REGION_S


def _venue_files(venue: str, day: str) -> list:
    y, m, d = day.split("-")
    root = f"{CR}/venue_pull/{venue}/year={y}/month={m}/day={d}"
    return sorted(glob.glob(f"{root}/**/*.zst", recursive=True))


# ------------------------------------------------------------ venue loaders --

def kraken_day_mids(day: str) -> dict:
    """{asset: {sec: mid}} via full-day warm-start L2 replay, emitted only
    in-region, <=1 mid per asset-second (state at the END of each second)."""
    sym2a = {s: a for a, s in KRAKEN_SYM.items()}
    books = {a: _Book() for a in KRAKEN_SYM}
    out = {a: {} for a in KRAKEN_SYM}
    pend = dict.fromkeys(KRAKEN_SYM)     # asset -> pending in-region sec
    tsc = _TsCache()
    n_lines = n_snap = 0
    for f in _venue_files("kraken_ws", day):
        proc = subprocess.Popen(["zstd", "-dc", f], stdout=subprocess.PIPE,
                                bufsize=1 << 22)
        for line in proc.stdout:
            n_lines += 1
            try:
                env = orjson.loads(line)
                inner = orjson.loads(env["_raw"])
            except (ValueError, KeyError):
                continue
            if inner.get("channel") != "book":
                continue
            typ = inner.get("type")
            sec = int(tsc.epoch(env["_wire_recv_ts"][:19]))
            for entry in inner.get("data", ()):
                a = sym2a.get(entry.get("symbol"))
                if a is None:
                    continue
                b = books[a]
                p = pend[a]
                if p is not None and sec > p:        # flush end-of-second state
                    mid = b.mid()
                    if mid is not None:
                        out[a][p] = mid
                    pend[a] = None
                if typ == "snapshot":
                    n_snap += 1
                    b.apply_snapshot(entry.get("bids", ()), entry.get("asks", ()))
                elif typ == "update":
                    for lv in entry.get("bids", ()):
                        b.apply_delta("bid", lv["price"], lv["qty"])
                    for lv in entry.get("asks", ()):
                        b.apply_delta("ask", lv["price"], lv["qty"])
                else:
                    continue
                if _in_region(sec):
                    pend[a] = sec
        proc.stdout.close()
        proc.wait()
        # ticket 86bbvrx1t: no break in the loop above, so reaching
        # here is a true EOF — a non-zero zstd exit means the day
        # was TRUNCATED and this pass silently under-counted.
        assert_zstd_ok(proc, f, exhausted=True, require_nonempty=False)
    for a, p in pend.items():
        if p is not None:
            mid = books[a].mid()
            if mid is not None:
                out[a][p] = mid
    print(f"  [kraken {day}] lines={n_lines:,} snapshots={n_snap} mids: "
          + ", ".join(f"{a}={len(v)}" for a, v in sorted(out.items()) if v))
    return {a: v for a, v in out.items() if v}


def gemini_day_mids(day: str) -> dict:
    """{asset: {sec: mid}} — gemini l2_updates absolute-set replay (the first
    l2_updates per symbol after session start IS the snapshot)."""
    sym2a = {s: a for a, s in GEMINI_SYM.items()}
    books = {a: _Book() for a in GEMINI_SYM}
    out = {a: {} for a in GEMINI_SYM}
    pend = dict.fromkeys(GEMINI_SYM)
    tsc = _TsCache()
    n_lines = 0
    for f in _venue_files("gemini_ws", day):
        proc = subprocess.Popen(["zstd", "-dc", f], stdout=subprocess.PIPE,
                                bufsize=1 << 22)
        for line in proc.stdout:
            n_lines += 1
            if b"l2_updates" not in line:
                continue
            try:
                env = orjson.loads(line)
                inner = orjson.loads(env["_raw"])
            except (ValueError, KeyError):
                continue
            if inner.get("type") != "l2_updates":
                continue
            a = sym2a.get(inner.get("symbol"))
            if a is None:
                continue
            sec = int(tsc.epoch(env["_wire_recv_ts"][:19]))
            b = books[a]
            p = pend[a]
            if p is not None and sec > p:
                mid = b.mid()
                if mid is not None:
                    out[a][p] = mid
                pend[a] = None
            for ch in inner.get("changes", ()):
                if len(ch) >= 3:
                    b.apply_delta(ch[0], ch[1], ch[2])
            if _in_region(sec):
                pend[a] = sec
        proc.stdout.close()
        proc.wait()
        # ticket 86bbvrx1t: no break in the loop above, so reaching
        # here is a true EOF — a non-zero zstd exit means the day
        # was TRUNCATED and this pass silently under-counted.
        assert_zstd_ok(proc, f, exhausted=True, require_nonempty=False)
    for a, p in pend.items():
        if p is not None:
            mid = books[a].mid()
            if mid is not None:
                out[a][p] = mid
    print(f"  [gemini {day}] lines={n_lines:,} mids: "
          + ", ".join(f"{a}={len(v)}" for a, v in sorted(out.items()) if v))
    return {a: v for a, v in out.items() if v}


def bitstamp_day_mids(day: str) -> dict:
    """{asset: {sec: mid}} — every bitstamp order_book frame is a complete
    top-100 book, so out-of-region frames are skipped exactly. Crossed books
    (known dust artifact) emit no mid."""
    chan2a = {f"order_book_{s}": a for a, s in BITSTAMP_SYM.items()}
    out = {a: {} for a in BITSTAMP_SYM}
    tsc = _TsCache()
    n_kept = 0
    for f in _venue_files("bitstamp_ws", day):
        span = _mod01._chunk_span(f)
        if span is not None:
            s0, e0 = span
            q1 = math.ceil(s0 / 900.0) * 900.0
            if q1 > e0 + REGION_S:
                continue
        proc = subprocess.Popen(["zstd", "-dc", f], stdout=subprocess.PIPE,
                                bufsize=1 << 22)
        for line in proc.stdout:
            if line[:18] != b'{"_wire_recv_ts":"':
                continue
            try:
                sec = int(tsc.epoch(line[18:37].decode()))
            except ValueError:
                continue
            if not _in_region(sec):
                continue
            try:
                env = orjson.loads(line)
                inner = orjson.loads(env["_raw"])
            except (ValueError, KeyError):
                continue
            a = chan2a.get(inner.get("channel", ""))
            if a is None:
                continue
            data = inner.get("data") or {}
            bids, asks = data.get("bids") or (), data.get("asks") or ()
            if not bids or not asks:
                continue
            bb, ba = float(bids[0][0]), float(asks[0][0])
            if bb <= 0 or ba <= 0 or bb >= ba:
                continue
            out[a][sec] = (bb + ba) / 2.0
            n_kept += 1
        proc.stdout.close()
        proc.wait()
        # ticket 86bbvrx1t: no break in the loop above, so reaching
        # here is a true EOF — a non-zero zstd exit means the day
        # was TRUNCATED and this pass silently under-counted.
        assert_zstd_ok(proc, f, exhausted=True, require_nonempty=False)
    print(f"  [bitstamp {day}] in-region mids={n_kept:,}: "
          + ", ".join(f"{a}={len(v)}" for a, v in sorted(out.items()) if v))
    return {a: v for a, v in out.items() if v}


def coinbase_day_spot(day: str) -> dict:
    """{asset: {sec: last price}} full-day (vol source needs full day)."""
    y, m, d = day.split("-")
    want = {p: a for a, p in CB_PRODUCT.items()}
    out = {a: {} for a in CB_PRODUCT}
    tsc = _TsCache()
    files = sorted(glob.glob(
        f"{CR}/coinbase_ticker/year={y}/month={m}/day={d}/**/*.zst",
        recursive=True))
    for f in files:
        proc = subprocess.Popen(["zstd", "-dc", f], stdout=subprocess.PIPE,
                                bufsize=1 << 22)
        for line in proc.stdout:
            try:
                env = orjson.loads(line)
                inner = orjson.loads(env["_raw"])
            except (ValueError, KeyError):
                continue
            a = want.get(inner.get("product_id"))
            if a is None:
                continue
            px = inner.get("price")
            if px is None:
                continue
            try:
                sec = int(tsc.epoch(env["_wire_recv_ts"][:19]))
                out[a][sec] = float(px)
            except (ValueError, TypeError):
                continue
        proc.stdout.close()
        proc.wait()
        # ticket 86bbvrx1t: no break in the loop above, so reaching
        # here is a true EOF — a non-zero zstd exit means the day
        # was TRUNCATED and this pass silently under-counted.
        assert_zstd_ok(proc, f, exhausted=True, require_nonempty=False)
    print(f"  [coinbase {day}] spot secs: "
          + ", ".join(f"{a}={len(v)}" for a, v in sorted(out.items()) if v))
    return {a: v for a, v in out.items() if v}


# ------------------------------------------------------------ index ---------

def _arrays(d: dict):
    secs = sorted(d)
    return secs, [d[s] for s in secs]


def _last_strict(secs, px, t):
    """Last value in a 1s-bucket timeline with bucket key STRICTLY < t and
    within STALE_S. (bucket key s holds data with ts in [s, s+1) < s+1 <= t)."""
    i = bisect.bisect_left(secs, t) - 1
    if i >= 0 and t - secs[i] <= STALE_S:
        return px[i]
    return None


class MultiVenueIndex:
    """Equal-weight mean of fresh per-venue inputs; strictly-before reads."""

    def __init__(self, venue_mids: dict):
        # venue_mids: {venue_name: {asset: {sec: px}}}
        self.tls = defaultdict(list)            # asset -> [(secs, px)]
        self.coverage = defaultdict(list)       # asset -> [venue names]
        for venue, per_asset in venue_mids.items():
            for a, d in per_asset.items():
                if d:
                    self.tls[a].append(_arrays(d))
                    self.coverage[a].append(venue)
        # vol timeline: coinbase full-day if present, else kraken region mids
        self.vol_tl = {}
        for a in self.tls:
            src = (venue_mids.get("coinbase", {}).get(a)
                   or venue_mids.get("kraken", {}).get(a))
            if src:
                self.vol_tl[a] = _arrays(src)

    def at(self, asset: str, t: float):
        vals = []
        for secs, px in self.tls.get(asset, ()):
            v = _last_strict(secs, px, t)
            if v is not None:
                vals.append(v)
        if not vals:
            return None, 0
        return sum(vals) / len(vals), len(vals)

    def realized_vol(self, asset: str, t: float):
        """stdev of per-5s log returns over [t-300s, t), strictly-before reads."""
        tl = self.vol_tl.get(asset)
        if tl is None:
            return None
        secs, px = tl
        k = int(VOL_WINDOW_S // VOL_STEP_S)
        samples = []
        for j in range(k + 1):
            v = _last_strict(secs, px, t - j * VOL_STEP_S)
            if v is None or v <= 0:
                return None
            samples.append(v)
        samples.reverse()
        rets = [math.log(samples[i] / samples[i - 1])
                for i in range(1, len(samples))]
        if len(rets) < 10:
            return None
        mu = sum(rets) / len(rets)
        var = sum((r - mu) ** 2 for r in rets) / (len(rets) - 1)
        return math.sqrt(var)


# ------------------------------------------------------------ evaluator -----

def _quote_span(tl, ai: int, side: str, px_ask: float, px_bid: float,
                close: float) -> float:
    """Length (s) of the contiguous run of NBBO points around anchor index
    `ai` during which the lifted side stayed at-or-better than our assumed
    entry (yes: ask <= entry ask; no: bid >= entry bid). Run end caps at
    close; the run necessarily spans the decision time (the anchor is the
    last point <= t, so the next point is > t)."""
    if side == "yes":
        def ok(p):
            return p[2] is not None and p[2] <= px_ask
    else:
        def ok(p):
            return p[1] is not None and p[1] >= px_bid
    j = ai
    while j - 1 >= 0 and ok(tl[j - 1]):
        j -= 1
    start = tl[j][0]
    k = ai + 1
    while k < len(tl) and ok(tl[k]):
        k += 1
    end = tl[k][0] if k < len(tl) else close
    return min(end, close) - start


def evaluate_window(tk: str, det: dict, tl: list, idx: MultiVenueIndex,
                    fun: dict, day: str, trades: list, lockwins: list,
                    sanity: list):
    asset = det["asset"]
    close = int(round(close_epoch_from_ticker(tk)))
    strike = det.get("strike")
    won = 1.0 if det["result"] == "yes" else 0.0
    if strike is None:
        fun["no_strike"] = fun.get("no_strike", 0) + 1
        return
    if not tl:
        fun["no_reliable_book"] = fun.get("no_reliable_book", 0) + 1
        return

    ti = 0
    first_print = close - N_PRINTS
    locked_yes = locked_no = lock_done = False
    any_decision = any_rti = False

    for t in range(close - DEC_FROM, close - DEC_TO + 1, DEC_STEP):
        while ti < len(tl) and tl[ti][0] <= t:
            ti += 1
        bid = ask = q_age = None
        if ti > 0 and t - tl[ti - 1][0] <= STALE_S:
            bid, ask = tl[ti - 1][1], tl[ti - 1][2]
            q_age = t - tl[ti - 1][0]

        rti, n_in = idx.at(asset, t)
        if rti is None:
            continue
        any_rti = True
        rv5 = idx.realized_vol(asset, t)
        if rv5 is None or rv5 <= 0:
            fun["dp_no_vol"] = fun.get("dp_no_vol", 0) + 1
            continue
        need = [s for s in range(first_print, close) if s <= t]
        realized = []
        for s in need:
            v, _ = idx.at(asset, s)
            if v is not None:
                realized.append(v)
        if need and len(realized) < MIN_PARTIAL_COVER * len(need):
            fun["dp_partial_gap"] = fun.get("dp_partial_gap", 0) + 1
            continue
        k = len(realized)
        e_avg = (sum(realized) + (N_PRINTS - k) * rti) / float(N_PRINTS)
        sigma_1s = rti * rv5 / math.sqrt(5.0)
        var = sigma_1s * sigma_1s * remaining_weight_sq_sum(t, close)
        sd = math.sqrt(var) if var > 0 else 0.0
        p_twap = (_norm_cdf((e_avg - strike) / sd) if sd > 0
                  else (1.0 if e_avg > strike else 0.0))
        any_decision = True

        # lock-STATE bookkeeping (entry threshold 0.95, both sides) + sanity
        if p_twap > LOCK_HI and not locked_yes:
            locked_yes = True
            lockwins.append((day, tk))
            sanity.append((day, asset, "yes", won))
        if p_twap < LOCK_LO and not locked_no:
            locked_no = True
            lockwins.append((day, tk))
            sanity.append((day, asset, "no", 1.0 - won))

        # entry rule — byte-identical economics to the original lock arm
        if not lock_done:
            if p_twap > LOCK_HI and ask is not None and 0 < ask < 100:
                edge = p_twap - ask / 100.0
                if edge > float(_fee_frac(ask)) + EDGE_OVER_FEE:
                    fee = kalshi_fee_per_contract_cents(ask)
                    pnl = (100.0 - ask - fee) if won else (-ask - fee)
                    span = _quote_span(tl, ti - 1, "yes", ask, bid, close)
                    trades.append(dict(
                        day=day, ticker=tk, asset=asset, side="yes",
                        t_entry=t, t_to_close=close - t, px=float(ask),
                        book_px=float(ask), p_twap=p_twap, n_in=n_in,
                        pnl=pnl, q_age=q_age, span_s=span,
                        span_pass=span >= SPAN_MIN_S))
                    lock_done = True
            elif p_twap < LOCK_LO and bid is not None and 0 < bid < 100:
                c = 100.0 - bid
                edge = (1.0 - p_twap) - c / 100.0
                if edge > float(_fee_frac(c)) + EDGE_OVER_FEE:
                    fee = kalshi_fee_per_contract_cents(c)
                    pnl = (100.0 - c - fee) if not won else (-c - fee)
                    span = _quote_span(tl, ti - 1, "no", ask, bid, close)
                    trades.append(dict(
                        day=day, ticker=tk, asset=asset, side="no",
                        t_entry=t, t_to_close=close - t, px=c,
                        book_px=float(bid), p_twap=p_twap, n_in=n_in,
                        pnl=pnl, q_age=q_age, span_s=span,
                        span_pass=span >= SPAN_MIN_S))
                    lock_done = True

    if not any_rti:
        fun["no_rti_coverage"] = fun.get("no_rti_coverage", 0) + 1
    elif not any_decision:
        fun["no_valid_decision_pt"] = fun.get("no_valid_decision_pt", 0) + 1
    else:
        fun["evaluated"] = fun.get("evaluated", 0) + 1


# ------------------------------------------------------------ tape check ----

def _scan_trades_file(path: str, want: dict, results: dict, stop_after: float):
    """Collect tape prints near entries. want: {ticker_bytes_str: [(i_global,
    t_entry, book_px)]}. results[i_global] = [n_near, matched]."""
    if not os.path.exists(path):
        return
    pat = re.compile(b"|".join(re.escape(tk.encode()) for tk in want))
    tsc = _TsCache()
    proc = subprocess.Popen(["zstd", "-dc", path], stdout=subprocess.PIPE,
                            bufsize=1 << 22)
    n = 0
    try:
        for line in proc.stdout:
            n += 1
            if stop_after and n % 20000 == 0:
                try:
                    if tsc.epoch(line[18:37].decode()) > stop_after:
                        break
                except ValueError:
                    pass
            if not pat.search(line):
                continue
            try:
                env = orjson.loads(line)
                msg = orjson.loads(env["_raw"]).get("msg", {})
            except (ValueError, KeyError):
                continue
            tk = msg.get("market_ticker")
            ents = want.get(tk)
            if not ents:
                continue
            ts_ms = msg.get("ts_ms")
            pts = (ts_ms / 1000.0 if ts_ms is not None
                   else float(msg.get("ts", 0)))
            try:
                yes_c = float(msg.get("yes_price_dollars")) * 100.0
            except (TypeError, ValueError):
                continue
            for i, te, bpx in ents:
                if abs(pts - te) <= PRINT_WINDOW_S:
                    results[i][0] += 1
                    if abs(yes_c - bpx) <= PRINT_TOL_C:
                        results[i][1] = True
    finally:
        proc.stdout.close()
        # ticket 86bbvrx1t: DELIBERATELY NOT checked. This site kills the
        # decompressor on purpose once it has what it needs, so a non-zero
        # exit is expected and asserting here would raise on every clean run.
        proc.kill()
        proc.wait()


def print_cross_check(day: str, trades: list):
    """Fill print_n_near / print_pass on each entry row (criterion 3a)."""
    if not trades:
        return
    want = defaultdict(list)
    for i, tr in enumerate(trades):
        want[tr["ticker"]].append((i, float(tr["t_entry"]), tr["book_px"]))
    results = {i: [0, False] for i in range(len(trades))}
    _scan_trades_file(f"{CR}/trades/day={day}.jsonl.zst", dict(want),
                      results, stop_after=0.0)
    # midnight spill: entries within 30s of next day's tape
    day_end = datetime.fromisoformat(day + "T00:00:00+00:00").timestamp() + 86400
    if any(t["t_entry"] + PRINT_WINDOW_S > day_end for t in trades):
        nxt = (date.fromisoformat(day) + timedelta(days=1)).isoformat()
        _scan_trades_file(f"{CR}/trades/day={nxt}.jsonl.zst", dict(want),
                          results, stop_after=day_end + 120.0)
    for i, tr in enumerate(trades):
        tr["print_n_near"], tr["print_pass"] = results[i]
        tr["xcheck_pass"] = bool(tr["print_pass"] or tr["span_pass"])


# ------------------------------------------------------------ per-day -------

def process_day(day: str, det_day: dict):
    t0 = time.time()
    fun: dict = {}
    trades: list = []
    lockwins: list = []
    sanity: list = []

    venue_mids = {
        "coinbase": coinbase_day_spot(day),
        "kraken": kraken_day_mids(day),
        "bitstamp": bitstamp_day_mids(day),
        "gemini": gemini_day_mids(day),
    }
    idx = MultiVenueIndex(venue_mids)
    coverage = {a: sorted(v) for a, v in idx.coverage.items()}

    book = nbbo_cache.load_day(CR, day)
    pruned = {}
    for tk in det_day:
        tl = book.get(tk)
        if not tl:
            continue
        close = close_epoch_from_ticker(tk)
        lo = bisect.bisect_left(tl, (close - 160.0,))
        hi = bisect.bisect_right(tl, (close + 0.001,))
        pruned[tk] = tl[lo:hi]
    del book

    for tk, det in sorted(det_day.items()):
        evaluate_window(tk, det, pruned.get(tk, []), idx, fun, day,
                        trades, lockwins, sanity)
    print_cross_check(day, trades)
    print(f"  [{day}] evaluated={fun.get('evaluated', 0)} "
          f"lock_state_windows={len(set(w[1] for w in lockwins))} "
          f"fills={len(trades)} ({time.time()-t0:.0f}s)")
    return dict(fun=fun, trades=trades, lockwins=lockwins, sanity=sanity,
                coverage=coverage)


# ------------------------------------------------------------ main ----------

def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", default=DEFAULT_DAYS)
    ap.add_argument("--workers", type=int, default=5)
    a = ap.parse_args(argv)
    days = a.days.split(",")
    n_days = len(days)

    avail = set(nbbo_cache.available_days(CR))
    missing = [d for d in days if d not in avail]
    if missing:
        print(f"FATAL: frames_nbbo cache not sealed for {missing}")
        return 2
    for d in days:
        if not os.path.exists(f"{CR}/.done_{d}"):
            print(f"FATAL: corpus day {d} has no .done marker — not sealed.")
            return 2

    t0 = time.time()
    print(f"=== GENHUNT #01b TWAP-lock VALIDATION — days={n_days} "
          f"({days[0]}..{days[-1]}) ===")

    # fresh settlements — lifecycle day partitions for run-days +- 1 day
    want_dirs = []
    seen_days = set()
    for d in days:
        dt = date.fromisoformat(d)
        for dd in (dt - timedelta(days=1), dt, dt + timedelta(days=1)):
            if dd in seen_days:
                continue
            seen_days.add(dd)
            p = (f"{CR}/lifecycle/year={dd.year:04d}/month={dd.month:02d}/"
                 f"day={dd.day:02d}")
            if os.path.isdir(p):
                want_dirs.append(p)
    determined_all: dict = {}
    for p in want_dirs:
        determined_all.update(load_determined(p, TRACKED))
    print(f"  [lifecycle] FRESH load_determined over {len(want_dirs)} day dirs: "
          f"{len(determined_all):,} windows ({time.time()-t0:.0f}s)")

    det_by_day = {d: {} for d in days}
    for tk, det in determined_all.items():
        c = close_epoch_from_ticker(tk)
        for d in days:
            d0 = datetime.fromisoformat(d + "T00:00:00+00:00").timestamp()
            if d0 < c <= d0 + 86400:
                det_by_day[d][tk] = det
                break

    fun: dict = {}
    trades: list = []
    lockwins: list = []
    sanity: list = []
    coverage_all: dict = {}
    with ProcessPoolExecutor(max_workers=min(a.workers, n_days)) as ex:
        futs = {d: ex.submit(process_day, d, det_by_day[d]) for d in days}
        for d in days:
            r = futs[d].result()
            for k, v in r["fun"].items():
                fun[k] = fun.get(k, 0) + v
            trades.extend(r["trades"])
            lockwins.extend(r["lockwins"])
            sanity.extend(r["sanity"])
            for asset, vs in r["coverage"].items():
                coverage_all.setdefault(asset, set()).update(vs)

    import pandas as pd

    print("\n=== PER-ASSET VENUE COVERAGE (measured) ===")
    for asset in TRACKED:
        vs = sorted(coverage_all.get(asset, ()))
        flag = "  [SINGLE-VENUE — flagged]" if len(vs) == 1 else ""
        print(f"  {asset:5s} {len(vs)} venue(s): {', '.join(vs) or 'NONE'}{flag}")

    print("\n=== USABLE-DATA FUNNEL (12-day) ===")
    n_det = sum(len(v) for v in det_by_day.values())
    print(f"  determined_in_days     {n_det}")
    for k in ("no_strike", "no_reliable_book", "no_rti_coverage",
              "no_valid_decision_pt", "evaluated", "dp_no_vol",
              "dp_partial_gap"):
        print(f"  {k:22s} {fun.get(k, 0)}")

    # ---- sanity (context, not a registered criterion this round) ----
    print("\n=== SANITY (context): locked-state (p>0.95) realized settle rate ===")
    if sanity:
        sr = sum(s[3] for s in sanity) / len(sanity)
        print(f"  locked windows n={len(sanity)}  settle rate={sr:.4f}")

    # ---- CRITERION 1: frequency ----
    lock_state_windows = len(set((w[0], w[1]) for w in lockwins))
    n_fills = len(trades)
    freq_fills = n_fills / n_days
    freq_state = lock_state_windows / n_days
    c1 = freq_fills >= FREQ_FLOOR_PER_DAY
    print("\n=== CRITERION 1 — FREQUENCY (entry-rule definition) ===")
    print(f"  lock-entry fills: n={n_fills} over {n_days} days -> "
          f"{freq_fills:.2f}/day (floor {FREQ_FLOOR_PER_DAY}) -> "
          f"{'PASS' if c1 else 'FAIL'}")
    print(f"  [context] lock-STATE windows (p_lock>0.95 either side, no book "
          f"gate): {lock_state_windows} -> {freq_state:.2f}/day")

    # ---- CRITERION 2: PnL ----
    c2 = False
    ci = (float("nan"),) * 3
    df = pd.DataFrame(trades) if trades else pd.DataFrame()
    if len(df):
        out_csv = "/tmp/genhunt01b_trades.csv"
        df.to_csv(out_csv, index=False)
        n_fill_days = df["day"].nunique()
        mean, lo, hi = day_bootstrap_ci(df, "pnl")
        ci = (mean, lo, hi)
        wr = (df["pnl"] > 0).mean()
        c2 = (n_fills >= MIN_FILLS and n_fill_days >= MIN_FILL_DAYS and lo > 0)
        print("\n=== CRITERION 2 — PNL (taker, hold to settle, net of 7p(1-p)) ===")
        print(f"  n={n_fills} fills across {n_fill_days} distinct days "
              f"(need >= {MIN_FILLS} across >= {MIN_FILL_DAYS})")
        print(f"  mean net={mean:+.2f}c/ct  total={df['pnl'].sum():+.0f}c  "
              f"WR={100*wr:.1f}%  day-bootstrap CI=[{lo:+.2f}, {hi:+.2f}]c")
        print(f"  -> {'PASS' if c2 else 'FAIL'}   [dump: {out_csv}]")
        fresh = df[df["q_age"] <= 5.0]
        if len(fresh) and fresh["day"].nunique() > 1:
            mf, lf, hf = day_bootstrap_ci(fresh, "pnl")
            print(f"  fresh-quote (<=5s) subset: n={len(fresh)} "
                  f"mean={mf:+.2f}c CI=[{lf:+.2f}, {hf:+.2f}]c")
    else:
        print("\n=== CRITERION 2 — PNL ===\n  no fills -> FAIL")

    # ---- CRITERION 3: print cross-check ----
    c3 = False
    if len(df):
        pa = df["print_pass"].mean()
        pb = df["span_pass"].mean()
        pe = df["xcheck_pass"].mean()
        c3 = pe >= XCHECK_FLOOR
        print("\n=== CRITERION 3 — PRINT CROSS-CHECK (decisive falsifier) ===")
        print(f"  (a) real print +-{PRINT_WINDOW_S:.0f}s within "
              f"{PRINT_TOL_C:.0f}c of lifted side: {100*pa:.1f}%")
        print(f"  (b) NBBO at-or-better >= {SPAN_MIN_S:.0f}s spanning entry:   "
              f"{100*pb:.1f}%")
        print(f"  EITHER (pass fraction): {100*pe:.1f}%  (floor "
              f"{100*XCHECK_FLOOR:.0f}%) -> {'PASS' if c3 else 'FAIL'}")
        print(f"  median prints within +-30s of entry: "
              f"{df['print_n_near'].median():.0f}")

    # ---- concentration profile ----
    if len(df):
        print("\n=== CONCENTRATION (stale-book risk profile) ===")
        print("  by asset:")
        print(df.groupby("asset").agg(
            n=("pnl", "size"), mean=("pnl", "mean"), sum=("pnl", "sum"),
            xchk=("xcheck_pass", "mean")).round(2).to_string())
        df["px_band"] = (df["px"] // 10 * 10).astype(int)
        print("  by entry px decile x side:")
        print(df.groupby(["side", "px_band"]).agg(
            n=("pnl", "size"), mean=("pnl", "mean"), sum=("pnl", "sum"),
            xchk=("xcheck_pass", "mean")).round(2).to_string())
        mid_no = df[(df["side"] == "no") & (df["px"] >= 20) & (df["px"] < 70)]
        tot = df["pnl"].sum()
        print(f"  mid-price NO entries (20-70c, the flagged stale-book cell): "
              f"n={len(mid_no)} sum={mid_no['pnl'].sum():+.0f}c "
              f"({100*mid_no['pnl'].sum()/tot if tot else 0:.0f}% of total) "
              f"xcheck={100*mid_no['xcheck_pass'].mean() if len(mid_no) else float('nan'):.0f}%")
        xc = df[df["xcheck_pass"]]
        if len(xc) and xc["day"].nunique() > 1:
            mx, lx, hx = day_bootstrap_ci(xc, "pnl")
            print(f"  cross-check-PASSING subset only: n={len(xc)} "
                  f"mean={mx:+.2f}c CI=[{lx:+.2f}, {hx:+.2f}]c")

    # ---- verdict ----
    print("\n=== VERDICT (pre-registered; all three must pass) ===")
    print(f"  C1 frequency {freq_fills:.2f}/day >= {FREQ_FLOOR_PER_DAY}: "
          f"{'PASS' if c1 else 'FAIL'}")
    print(f"  C2 PnL n={n_fills}, days={df['day'].nunique() if len(df) else 0}, "
          f"CI=[{ci[1]:+.2f}, {ci[2]:+.2f}]c LB>0: {'PASS' if c2 else 'FAIL'}")
    print(f"  C3 print cross-check "
          f"{100*df['xcheck_pass'].mean() if len(df) else 0:.1f}% >= 70%: "
          f"{'PASS' if c3 else 'FAIL'}")
    print(f"\n  {'PROMOTED' if (c1 and c2 and c3) else 'NOT PROMOTED'}")
    print(f"  runtime {time.time()-t0:.0f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
