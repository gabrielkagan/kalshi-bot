# PROVENANCE NOTE (Bit T-1 R2-MN2): committed as the auditable record of
# the validated construction + numbers cited by bot/longshot.py /
# bot/constants.py (+4.58c/ct fillable-only, CI [+2.82, +6.26], 12/12
# days), NOT as a runnable CI artifact. Running it requires UNTRACKED
# research-corpus siblings that exist only on the research machine — a
# clean clone fails at import on scripts/research/nbbo_cache.py +
# scripts/research/fairvalue_model.py — plus the local bronze corpus.
"""GENHUNT #02b — Arm B (deep-OTM longshot selling) FILLABLE-ONLY validation re-run.

Pre-registered clean re-run of the 2026-06-11 GENHUNT round's strongest output
(GENHUNT_REPORT.md §2, #02 Arm B), fixing the three refutation gaps that killed
the original run: (1) stale whole-corpus lifecycle pickle cache -> settlements
are rebuilt FRESH every run, never cached; (2) no book-anchored fill model ->
the headline is now FILLABLE-ONLY against the precomputed reliable-NBBO cache;
(3) `(id(tl), t//5)` rv memo-cache made results call-order-dependent -> rv is
now a pure function of (timeline, t), no cache, bit-stable across runs.

PRE-REGISTRATION (fixed 2026-06-11 BEFORE any number was computed — do not edit
after results):

  STRATEGY (identical to #02 Arm B): maker SELLS the deep-OTM side s of a
  crypto-15M window (rests an ask on s / equivalently bids the opposite side)
  at taker-print prices in [4,15]c, print ts in [close-720s, close-180s]
  (T-12..T-3 min, non-endgame), CONDITIONED on p_s(print_ts) <= price_c/200
  (self-computed normal-model probability at most half the ask). Hold to
  settlement. Maker fee on Kalshi = 0; PnL/ct = price_c - 100*(s settles ITM).
  Exclusions: dust prints (count_fp <= 0.0101) and the 225-size campaign class.
  Universe: BTC/ETH/SOL/XRP/DOGE/ADA/BCH (Coinbase spot) + HYPE (Bitstamp L2
  mid); BNB excluded (no replayable spot source — same honest subset as #02).

  FILLABLE-ONLY (the gap that killed the original run): a conditioned taker
  print counts as a maker fill ONLY if print_price >= prevailing reliable
  same-side ask at print time (yes-side ask = yes_ask; no-side ask =
  100 - yes_bid), where "prevailing" = last point at-or-before the print ts in
  the precomputed reliable-NBBO timeline (`scripts.research.nbbo_cache`,
  snapshot-anchored / drift-refusing / never-crossed). No book point at-or-
  before the print, or an empty side, -> NOT fillable.

  PRIMARY STATISTIC: day-bootstrap (days resampled, never rows; seeded RNG) 95%
  CI of net PnL/ct over PER-WINDOW rows (one row per window: ct-weighted mean
  of its fills), fillable-only conditioned set. Contract-weighted day-bootstrap
  CI is reported as a COMPANION (the BTC per-window/ct-weighted sign divergence
  from the original run must stay visible), it is NOT the verdict stat.

  PROMOTION CRITERION (all three legs, else NOT PROMOTED):
    leg 1: fillable-only PRIMARY day-bootstrap CI lower bound > 0 across all
           sealed frames days;
    leg 2: n_days >= 10 (distinct UTC close-dates contributing fillable
           conditioned windows);
    leg 3: conditioning gap >= +1.0 c/ct on the fillable subset (conditioned
           per-window pooled mean minus unconditioned same-band fillable
           per-window pooled mean).

  SENSITIVITY (reported, not a verdict leg): 10s cancel-latency pickoff — the
  maker's quote-up decision uses the signal as of (print_ts - 10s), so prints
  arriving within 10s after the signal lapsed still fill (pickoffs eaten) and
  prints within 10s of signal birth are missed. Original refuter figure for
  reference: -0.5 c/ct.

  HONESTY RAILS: settlements via `load_determined` on the full lifecycle dir,
  FRESH each run (no pickle cache of any kind for settlements); per-day
  evaluated-window funnel printed so silent corpus truncation is log-visible;
  spot/tape caches (expensive bronze scans, pure per-day transforms) are
  DAY-KEYED with `.done` markers, never mtime-trusted; midnight-spillover
  windows (close == 00:00 UTC) pull book + spot + tape from the previous UTC
  day's partitions instead of silently losing them; rv has no memo-cache —
  bit-stable output verified by running twice on one day and diffing stdout.

Usage:
  python3 scripts/research/genhunt/02b_longshot_fillable_validation.py \
      [--days 2026-05-31] [--no-day-cache]
  (default days = every frames_nbbo-cached day that is also a sealed trades day)
"""
from __future__ import annotations

import argparse
import bisect
import json
import math
import os
import pickle
import subprocess
import sys
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone

_REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)

import numpy as np
import pandas as pd

from scripts.research import nbbo_cache
from scripts.research import venue_book_reconstruct as vbr
from scripts.research.fairvalue_model import day_bootstrap_ci
from scripts.research.phase1b_real_price_economics import (
    _epoch, _is_crypto_15m, close_epoch_from_ticker, load_determined,
)

CR = os.path.expanduser("~/kalshi-research-data/fairvalue")
CACHE = os.path.join(CR, "_tmp_genhunt02b")
ASSETS = ("BTC", "ETH", "SOL", "XRP", "HYPE", "DOGE", "BNB", "ADA", "BCH")
CB_PRODUCT = {"BTC": "BTC-USD", "ETH": "ETH-USD", "SOL": "SOL-USD",
              "XRP": "XRP-USD", "DOGE": "DOGE-USD", "BCH": "BCH-USD", "ADA": "ADA-USD"}
UNIVERSE = ("BTC", "ETH", "SOL", "XRP", "DOGE", "ADA", "BCH", "HYPE")  # BNB excluded

VOL_WIN_S, VOL_STEP_S = 300.0, 5.0
STALE_S = 30.0
ARMB_START_OFF_S = 720.0   # T-12 min
ARMB_END_OFF_S = 180.0     # T-3 min
DUST_MAX = 0.0101
CAMPAIGN_SIZE = 225.0
ARMB_LO, ARMB_HI = 4, 15
PARTICIPATION = 0.10
LATENCY_S = 10.0


def log(msg):
    print(f"[02b] {msg}", file=sys.stderr, flush=True)


def _norm_cdf(z):
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


# ------------------------------------------------------------ bronze loaders
# (adapted from genhunt/02_longshot_tick_floor.py, restructured to PER-DAY)

def _pipe(cmd):
    proc = subprocess.Popen(cmd, shell=True, stdout=subprocess.PIPE, bufsize=1 << 22)
    try:
        for raw in proc.stdout:
            yield raw
    finally:
        proc.stdout.close()
        proc.wait()


def _day_dir(root, day):
    y, m, dd = day.split("-")
    p = os.path.join(root, f"year={y}", f"month={m}", f"day={dd}")
    return p if os.path.isdir(p) else None


def load_spot_day(day):
    """{asset: (ts_list, px_list)} from Coinbase ticker bronze for ONE day."""
    d = _day_dir(os.path.join(CR, "coinbase_ticker"), day)
    if not d:
        return {}
    cmd = f"find '{d}' -name '*.zst' -print0 | xargs -0 zstd -dc --"
    want = {p.encode(): a for a, p in CB_PRODUCT.items()}
    pid_pat = b'product_id\\":\\"'
    tick_pat = b'type\\":\\"ticker'
    seen = set()
    pts = defaultdict(list)
    n = kept = 0
    for line in _pipe(cmd):
        n += 1
        if tick_pat not in line:
            continue
        i = line.find(pid_pat)
        if i < 0:
            continue
        j = line.find(b'\\"', i + len(pid_pat))
        a = want.get(line[i + len(pid_pat):j])
        if a is None:
            continue
        key = (line[i + len(pid_pat):j], line[18:37])  # (product, ISO-second)
        if key in seen:
            continue
        seen.add(key)
        try:
            env = json.loads(line)
            inner = json.loads(env["_raw"])
            px = inner.get("price")
            if px is None:
                continue
            pts[a].append((_epoch(env["_wire_recv_ts"]), float(px)))
            kept += 1
        except (ValueError, KeyError):
            continue
    out = {a: sorted(v) for a, v in pts.items()}
    log(f"spot[{day}]: scanned {n:,} cb lines, kept {kept:,}")
    return out  # {asset: [(ts, px)] sorted}


def load_hype_day(day, every=8):
    """HYPE Bitstamp L2 full-snapshot mid points for ONE day."""
    d = _day_dir(os.path.join(CR, "venue_pull", "bitstamp_ws"), day)
    if not d:
        return []
    cmd = (f"find '{d}' -name '*.zst' -print0 | xargs -0 zstd -dc -- "
           "| LC_ALL=C grep hypeusd")
    pts = []
    n = 0
    for line in _pipe(cmd):
        n += 1
        if n % every:
            continue
        try:
            env = json.loads(line)
            inner = json.loads(env["_raw"])
            if inner.get("channel") != "order_book_hypeusd":
                continue
            book = vbr.BitstampBook()
            book.apply_frame(inner)
            m = book.mid()
            if m:
                pts.append((_epoch(env["_wire_recv_ts"]), m))
        except (ValueError, KeyError):
            continue
    pts.sort()
    log(f"hype[{day}]: {n:,} hypeusd frames, kept {len(pts):,} mids")
    return pts


def load_tape_day(day):
    """{ticker: [(ts, yes_c, no_c, side, ct)]} for prints with a side <= 15c."""
    path = os.path.join(CR, "trades", f"day={day}.jsonl.zst")
    if not os.path.exists(path):
        return {}
    needles = (b'yes_price_dollars\\":\\"0.0', b'yes_price_dollars\\":\\"0.1',
               b'yes_price_dollars\\":\\"0.8', b'yes_price_dollars\\":\\"0.9')
    tape = defaultdict(list)
    n = kept = 0
    for line in _pipe(f"zstd -dc '{path}'"):
        n += 1
        if not any(nd in line for nd in needles):
            continue
        try:
            inner = json.loads(json.loads(line)["_raw"])
            msg = inner["msg"]
            tk = msg["market_ticker"]
            if _is_crypto_15m(tk) is None:
                continue
            yes_c = int(round(float(msg["yes_price_dollars"]) * 100))
            no_c = int(round(float(msg["no_price_dollars"]) * 100))
            side = msg["taker_side"]
            ct = float(msg["count_fp"])
            ts = float(msg["ts"])
        except (ValueError, KeyError, TypeError):
            continue
        if min(yes_c, no_c) > ARMB_HI:
            continue
        kept += 1
        tape[tk].append((ts, yes_c, no_c, side, ct))
    log(f"tape[{day}]: {n:,} lines, kept {kept:,} across {len(tape):,} tickers")
    return dict(tape)


def day_cached(name, day, fn, enabled=True):
    """Day-keyed pickle cache with .done marker (NEVER mtime). Atomic write."""
    if not enabled:
        return fn()
    os.makedirs(CACHE, exist_ok=True)
    p = os.path.join(CACHE, f"{name}_{day}.pkl")
    done = os.path.join(CACHE, f".done_{name}_{day}")
    if os.path.exists(done) and os.path.exists(p):
        with open(p, "rb") as fh:
            return pickle.load(fh)
    v = fn()
    tmp = f"{p}.tmp.{os.getpid()}"
    with open(tmp, "wb") as fh:
        pickle.dump(v, fh, protocol=4)
    os.replace(tmp, p)
    with open(done, "w") as fh:
        fh.write(time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
    return v


# ------------------------------------------------------------------- signal
# rv is a PURE function of (timeline, t): NO memo-cache. The #02 cache keyed
# (id(tl), t//5) but the value depended on exact t -> first caller in a 5s
# bucket pinned the value for everyone, making results call-order-dependent
# (the bit-stability refutation). Pure recompute is bit-stable by construction.

def _at(secs, px, t):
    i = bisect.bisect_right(secs, t) - 1
    if i < 0 or (t - secs[i]) > STALE_S:
        return None
    return px[i]


def _rv_pure(secs, px, t):
    samples = []
    k = int(VOL_WIN_S // VOL_STEP_S)
    for j in range(k + 1):
        tt = t - j * VOL_STEP_S
        i = bisect.bisect_right(secs, tt) - 1
        if i < 0 or (tt - secs[i]) > STALE_S:
            return None
        samples.append(px[i])
    samples.reverse()
    rets = [math.log(samples[i] / samples[i - 1]) for i in range(1, len(samples))
            if samples[i - 1] > 0]
    if len(rets) < 10:
        return None
    m = sum(rets) / len(rets)
    var = sum((r - m) ** 2 for r in rets) / (len(rets) - 1)
    return math.sqrt(var)


def zscore(tl, t, strike, close):
    secs, px = tl
    s = _at(secs, px, t)
    if s is None or s <= 0:
        return None
    rv = _rv_pure(secs, px, t)
    if rv is None or rv <= 0:
        return None
    tau = close - t
    if tau <= 0:
        return None
    sig = s * rv * math.sqrt(tau / VOL_STEP_S)
    if sig <= 0:
        return None
    return (s - strike) / sig


class BookLookup:
    """Prevailing reliable NBBO at-or-before t from a cached timeline."""

    def __init__(self, timeline):
        self.ts = [p[0] for p in timeline]
        self.bid = [p[1] for p in timeline]
        self.ask = [p[2] for p in timeline]

    def side_ask(self, t, side):
        i = bisect.bisect_right(self.ts, t) - 1
        if i < 0:
            return None
        if side == "yes":
            return self.ask[i]
        b = self.bid[i]
        return None if b is None else 100.0 - b


# ---------------------------------------------------------------- statistics

def weighted_day_ci(df, n=2000, seed=7):
    """Day-bootstrap CI of contract-weighted pooled PnL/ct (COMPANION stat)."""
    rng = np.random.default_rng(seed)
    days = df["day"].unique()
    g = {d: (df.loc[df["day"] == d, "wpnl"].sum(), df.loc[df["day"] == d, "w"].sum())
         for d in days}
    means = []
    for _ in range(n):
        pick = rng.choice(days, size=len(days), replace=True)
        num = sum(g[d][0] for d in pick)
        den = sum(g[d][1] for d in pick)
        if den > 0:
            means.append(num / den)
    lo, hi = np.percentile(means, [2.5, 97.5])
    pooled = df["wpnl"].sum() / max(df["w"].sum(), 1e-9)
    return pooled, float(lo), float(hi)


def paired_gap_ci(dfc, dfu, n=2000, seed=11):
    """Day-bootstrap CI of (cond per-window mean - uncond per-window mean)."""
    rng = np.random.default_rng(seed)
    days = sorted(set(dfc["day"]) | set(dfu["day"]))
    gc = {d: dfc.loc[dfc["day"] == d, "pnl_per_ct"].values.astype(float) for d in days}
    gu = {d: dfu.loc[dfu["day"] == d, "pnl_per_ct"].values.astype(float) for d in days}
    gaps = []
    for _ in range(n):
        pick = rng.choice(days, size=len(days), replace=True)
        c = np.concatenate([gc[d] for d in pick])
        u = np.concatenate([gu[d] for d in pick])
        if len(c) and len(u):
            gaps.append(c.mean() - u.mean())
    lo, hi = np.percentile(gaps, [2.5, 97.5])
    return float(lo), float(hi)


# --------------------------------------------------------------- evaluation

def _merge_spot(parts):
    """parts: list of {asset: [(ts, px)]} -> {asset: (secs, px)} sorted."""
    acc = defaultdict(list)
    for part in parts:
        for a, v in part.items():
            acc[a].extend(v)
    out = {}
    for a, v in acc.items():
        v.sort()
        out[a] = ([t for t, _ in v], [p for _, p in v])
    return out


def evaluate_day(dkey, windows, spot, tape, books, fun):
    """windows: {ticker: determined-dict} with close-date == dkey.
    Returns per-window row lists: cond / uncond / latency (all FILLABLE-only)
    plus a non-fillable conditioned reference set."""
    rows = {"cond": [], "uncond": [], "lat": [], "cond_all": []}
    for tk in sorted(windows):
        d = windows[tk]
        a = d["asset"]
        fun["windows_settled"] += 1
        if a not in UNIVERSE:
            fun["drop_asset_excluded(BNB)"] += 1
            continue
        if d.get("strike") is None:
            fun["drop_no_strike"] += 1
            continue
        tl = spot.get(a)
        if tl is None:
            fun["drop_no_spot_source"] += 1
            continue
        close = close_epoch_from_ticker(tk)
        strike, result = float(d["strike"]), d["result"]
        fun["windows_in_scope"] += 1
        book = books.get(tk)
        cond, uncond, lat, cond_all = [], [], [], []
        win_conditioned = False
        for ts, yc, nc, side, ct in tape.get(tk, ()):
            if not (close - ARMB_START_OFF_S) <= ts <= (close - ARMB_END_OFF_S):
                continue
            px = yc if side == "yes" else nc
            if not (ARMB_LO <= px <= ARMB_HI):
                continue
            if ct <= DUST_MAX or ct == CAMPAIGN_SIZE:
                fun["prints_excl_dust_or_225"] += 1
                continue
            fun["prints_band"] += 1
            zb = zscore(tl, ts, strike, close)
            if zb is None:
                fun["prints_no_signal"] += 1
                continue
            fun["prints_signal_ok"] += 1
            p_side = _norm_cdf(zb) if side == "yes" else 1.0 - _norm_cdf(zb)
            conditioned = p_side <= px / 200.0
            pnl = px - (100.0 if result == side else 0.0)
            if conditioned:
                fun["prints_conditioned"] += 1
                win_conditioned = True
                cond_all.append((ct, pnl))
            side_ask = book.side_ask(ts, side) if book is not None else None
            if side_ask is None:
                fun["prints_no_book"] += 1
                continue
            fun["prints_book_avail"] += 1
            if px < side_ask:
                fun["prints_inside_ask(unfillable)"] += 1
                continue
            fun["prints_fillable"] += 1
            uncond.append((ct, pnl))
            if conditioned:
                fun["fills"] += 1
                cond.append((ct, pnl))
            # sensitivity: quote decision lagged LATENCY_S
            zl = zscore(tl, ts - LATENCY_S, strike, close)
            if zl is not None:
                pl = _norm_cdf(zl) if side == "yes" else 1.0 - _norm_cdf(zl)
                if pl <= px / 200.0:
                    fun["fills_latency10"] += 1
                    lat.append((ct, pnl))
        if win_conditioned:
            fun["windows_conditioned"] += 1
        for key, bucket in (("cond", cond), ("uncond", uncond),
                            ("lat", lat), ("cond_all", cond_all)):
            if not bucket:
                continue
            cs = sum(c for c, _ in bucket)
            ps = sum(c * p for c, p in bucket)
            rows[key].append({"day": dkey, "asset": a, "ticker": tk,
                              "n_prints": len(bucket),
                              "pnl_per_ct": ps / cs,
                              "w": PARTICIPATION * cs,
                              "wpnl": PARTICIPATION * ps})
    return rows


# -------------------------------------------------------------------- main

def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", default=None,
                    help="comma list; default = all frames_nbbo-cached sealed days")
    ap.add_argument("--no-day-cache", action="store_true",
                    help="bypass the day-keyed spot/hype/tape caches")
    a = ap.parse_args(argv)
    t0 = time.time()

    frames_days = nbbo_cache.available_days(CR)
    sealed = {f[len(".done_"):] for f in os.listdir(CR)
              if f.startswith(".done_2026")
              and os.path.exists(os.path.join(CR, "trades", f"day={f[len('.done_'):]}.jsonl.zst"))}
    days = sorted(set(frames_days) & sealed)
    if a.days:
        days = [d for d in a.days.split(",") if d]
    print(f"frames_nbbo days available: {frames_days}")
    print(f"evaluation days (frames ∩ sealed trades): {days}")

    # settlements: FRESH every run — the stale whole-corpus pickle is the exact
    # failure mode that killed the original run. Never cached here.
    determined = load_determined(os.path.join(CR, "lifecycle"), ASSETS)
    log(f"lifecycle fresh load: {len(determined)} determined windows "
        f"({time.time() - t0:.0f}s)")
    by_day = defaultdict(dict)
    for tk, d in determined.items():
        close = close_epoch_from_ticker(tk)
        dk = datetime.fromtimestamp(close, tz=timezone.utc).date().isoformat()
        by_day[dk][tk] = d
    print("\n-------- settled-window corpus by close-date (truncation guard) --------")
    for dk in sorted(by_day):
        flag = "  <-- evaluated" if dk in days else ""
        print(f"  {dk}: {len(by_day[dk]):>5,} determined windows{flag}")

    cache_on = not a.no_day_cache

    def prev_day(d):
        return (datetime.strptime(d, "%Y-%m-%d") - timedelta(days=1)).date().isoformat()

    all_rows = {"cond": [], "uncond": [], "lat": [], "cond_all": []}
    day_funnels = {}
    for d in days:
        fun = defaultdict(int)
        windows = by_day.get(d, {})
        pd_ = prev_day(d)
        have_prev = pd_ in sealed
        # spot + tape: merge previous UTC day (midnight-spillover windows need
        # trailing-300s vol and prints that live in the previous day partition)
        spot_parts = []
        if have_prev:
            spot_parts.append(day_cached("spot", pd_, lambda p=pd_: load_spot_day(p), cache_on))
        spot_parts.append(day_cached("spot", d, lambda p=d: load_spot_day(p), cache_on))
        spot = _merge_spot(spot_parts)
        hype_pts = []
        if have_prev:
            hype_pts.extend(day_cached("hype", pd_, lambda p=pd_: load_hype_day(p), cache_on))
        hype_pts.extend(day_cached("hype", d, lambda p=d: load_hype_day(p), cache_on))
        hype_pts.sort()
        if hype_pts:
            spot["HYPE"] = ([t for t, _ in hype_pts], [p for _, p in hype_pts])
        tape = defaultdict(list)
        parts = ([day_cached("tape", pd_, lambda p=pd_: load_tape_day(p), cache_on)]
                 if have_prev else [])
        parts.append(day_cached("tape", d, lambda p=d: load_tape_day(p), cache_on))
        for part in parts:
            for tk, v in part.items():
                tape[tk].extend(v)
        for tk in tape:
            tape[tk].sort()
        # books: this day's NBBO cache; midnight windows (close < open+900 of
        # the UTC day) have their frames (incl. the anchoring snapshot) in the
        # PREVIOUS day's cache — prepend that segment.
        day_start = datetime.strptime(d, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp()
        cur = nbbo_cache.load_day(CR, d)
        books = {}
        for tk in windows:
            seg = cur.get(tk)
            if seg:
                books[tk] = seg
        del cur
        prev_needed = {tk for tk in windows
                       if close_epoch_from_ticker(tk) < day_start + 900.0}
        if prev_needed and pd_ in frames_days:
            prevb = nbbo_cache.load_day(CR, pd_)
            for tk in prev_needed:
                seg = prevb.get(tk)
                if seg:
                    books[tk] = seg + books.get(tk, [])
                    fun["midnight_book_from_prev_day"] += 1
            del prevb
        books = {tk: BookLookup(tl) for tk, tl in books.items()}
        log(f"[{d}] inputs ready: windows={len(windows)} books={len(books)} "
            f"({time.time() - t0:.0f}s)")

        rows = evaluate_day(d, windows, spot, tape, books, fun)
        for k in all_rows:
            all_rows[k].extend(rows[k])
        day_funnels[d] = fun
        log(f"[{d}] evaluated: cond_windows={len(rows['cond'])} "
            f"fills={fun['fills']} ({time.time() - t0:.0f}s)")

    # ------------------------------------------------ per-day funnel printout
    print("\n================ PER-DAY FUNNEL (fillable-only Arm B) ================")
    hdr = ("day", "settled", "in_scope", "win_cond", "prints_band", "signal_ok",
           "book_avail", "fillable", "cond", "fills", "lat10", "ct@10%")
    print("  " + "  ".join(f"{h:>11}" for h in hdr))
    for d in days:
        f = day_funnels[d]
        ct = sum(r["w"] for r in all_rows["cond"] if r["day"] == d)
        print("  " + "  ".join(f"{v:>11}" for v in (
            d, f["windows_settled"], f["windows_in_scope"], f["windows_conditioned"],
            f["prints_band"], f["prints_signal_ok"], f["prints_book_avail"],
            f["prints_fillable"], f["prints_conditioned"], f["fills"],
            f["fills_latency10"], f"{ct:,.0f}")))
    tot = defaultdict(int)
    for f in day_funnels.values():
        for k, v in f.items():
            tot[k] += v
    print("\n  totals: " + " | ".join(f"{k}={v:,}" for k, v in sorted(tot.items())))

    # ------------------------------------------------------------- headline
    print("\n================ ARM B FILLABLE-ONLY (PRIMARY) ================")
    dfc = pd.DataFrame(all_rows["cond"])
    dfu = pd.DataFrame(all_rows["uncond"])
    verdict_ok = False
    lo = mean = gap = float("nan")
    ndays = 0
    if len(dfc) and len(dfu):
        mean, lo, hi = day_bootstrap_ci(dfc, "pnl_per_ct")
        wmean, wlo, whi = weighted_day_ci(dfc)
        umean, ulo, uhi = day_bootstrap_ci(dfu, "pnl_per_ct")
        uw, uwlo, uwhi = weighted_day_ci(dfu)
        ndays = dfc["day"].nunique()
        gap = mean - umean
        wgap = wmean - uw
        glo, ghi = paired_gap_ci(dfc, dfu)
        print(f"conditioned fillable: windows={len(dfc):,} fills={int(dfc['n_prints'].sum()):,} "
              f"days={ndays} ct@10%={dfc['w'].sum():,.0f}")
        print(f"  PRIMARY per-window day-bootstrap PnL/ct: {mean:+.3f}c "
              f"CI95=[{lo:+.3f}, {hi:+.3f}]")
        print(f"  companion ct-weighted day-bootstrap:     {wmean:+.3f}c "
              f"CI95=[{wlo:+.3f}, {whi:+.3f}]")
        print(f"unconditioned same-band fillable benchmark: windows={len(dfu):,} "
              f"per-window {umean:+.3f}c CI95=[{ulo:+.3f}, {uhi:+.3f}]  "
              f"ct-weighted {uw:+.3f}c CI95=[{uwlo:+.3f}, {uwhi:+.3f}]")
        print(f"conditioning gap (fillable, per-window) = {gap:+.3f}c "
              f"(paired day-bootstrap CI [{glo:+.3f}, {ghi:+.3f}]; ct-weighted gap "
              f"{wgap:+.3f}c; bar >= +1.0)")
        dfa = pd.DataFrame(all_rows["cond_all"])
        am, al, ah = day_bootstrap_ci(dfa, "pnl_per_ct")
        print(f"[reference] conditioned ALL prints (no fill model, the old #02 "
              f"headline basis): windows={len(dfa):,} per-window {am:+.3f}c "
              f"CI95=[{al:+.3f}, {ah:+.3f}]")

        # --------------------------------------------------------- per-asset
        print("\n---- per-asset (conditioned fillable) ----")
        print(f"  {'asset':>6} {'windows':>8} {'fills':>7} {'ct@10%':>10} "
              f"{'ctw_pnl/ct':>11} {'perwin_mean':>12}")
        for asset, g in dfc.groupby("asset"):
            ctw = g["wpnl"].sum() / max(g["w"].sum(), 1e-9)
            print(f"  {asset:>6} {len(g):>8,} {int(g['n_prints'].sum()):>7,} "
                  f"{g['w'].sum():>10,.0f} {ctw:>+11.3f} {g['pnl_per_ct'].mean():>+12.3f}")

        # ------------------------------------------------------- sensitivity
        print("\n---- sensitivity: 10s cancel-latency pickoff ----")
        dfl = pd.DataFrame(all_rows["lat"])
        if len(dfl):
            lm, ll, lh = day_bootstrap_ci(dfl, "pnl_per_ct")
            lw, lwlo, lwhi = weighted_day_ci(dfl)
            print(f"  latency-conditioned fillable: windows={len(dfl):,} "
                  f"per-window {lm:+.3f}c CI95=[{ll:+.3f}, {lh:+.3f}] "
                  f"(delta vs headline {lm - mean:+.3f}c)")
            print(f"  ct-weighted {lw:+.3f}c CI95=[{lwlo:+.3f}, {lwhi:+.3f}] "
                  f"(delta {lw - wmean:+.3f}c; original refuter flat figure: -0.5c/ct)")
        else:
            print("  no latency-arm rows")

        # ----------------------------------------------------------- verdict
        leg1 = lo > 0
        leg2 = ndays >= 10
        leg3 = gap >= 1.0
        verdict_ok = leg1 and leg2 and leg3
        print("\n================ PRE-REGISTERED VERDICT ================")
        print(f"leg1 fillable-only PRIMARY day-bootstrap CI LB > 0: "
              f"{'PASS' if leg1 else 'FAIL'} (LB={lo:+.3f}c)")
        print(f"leg2 n_days >= 10:                                  "
              f"{'PASS' if leg2 else 'FAIL'} (n_days={ndays})")
        print(f"leg3 conditioning gap >= +1.0c (fillable):          "
              f"{'PASS' if leg3 else 'FAIL'} (gap={gap:+.3f}c)")
    else:
        print("no fillable conditioned rows — cannot test")
        print("\n================ PRE-REGISTERED VERDICT ================")
        print("leg1/leg2/leg3: FAIL (no data)")
    print(f"VERDICT: {'PROMOTED' if verdict_ok else 'NOT PROMOTED'} — "
          f"fillable-only {mean:+.3f}c/ct CI_LB={lo:+.3f}c, n_days={ndays}, "
          f"gap={gap:+.3f}c")
    log(f"total runtime {time.time() - t0:.0f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
