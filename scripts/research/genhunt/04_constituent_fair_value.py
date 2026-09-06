"""Coinbase-blind constituent fair value: HYPE/BNB primary + majors-basis arm.

PRE-REGISTRATION (stated BEFORE looking at results; merged spec #16 + #9):

(a) COUNTERPARTY / why they pay us
    HYPE/BNB arm: Kalshi quoters on these assets anchor to Binance/Hyperliquid
    (NON-constituents of the CF RTI Kalshi settles to) and to nothing at all
    (our own production bot is spot-blind on HYPE/BNB; 45K HYPE trades/day of
    desperation-premium retail flow). We hold actual L2 for 2-of-3 HYPE RTI
    constituents (Kraken HYPE/USD, Bitstamp hypeusd) and 1-of-2 BNB
    constituents (Kraken BNB/USD), i.e. a better contemporaneous estimate of
    the SETTLEMENT measure's level than the book is quoting.
    Majors arm: Coinbase trades at minutes-scale premium/discount to the
    constituent consensus; Coinbase-anchored quoters/takers (incl. bots
    architected like ours) misprice near-money windows by the level shift.
    Contemporaneous LEVEL correction to the settlement measure — NOT lead-lag.

(b) SIGNAL / ENTRY / EXIT (one observation per window, decision at T = close-300s)
    HYPE/BNB arm: constituent mid via venue_book_reconstruct books
    (HYPE: mean of Kraken + Bitstamp mids, each fresh within <=30s;
     BNB: Kraken mid). rv = stdev of per-5s log-returns of the constituent
    consensus mid over [T-300, T] (mirrors fairvalue_extract._realized_vol).
    p_cons = Phi((cons - strike) / (cons * rv * sqrt(300/5))).
    Book mid at T: frames days -> reliable_nbbo_timeline mid; non-frames days
    -> last-trade proxy (age <= 120s), with proxy-vs-NBBO divergence reported
    on frames days. Gate: |p_cons*100 - mid_c| >= 6c -> rest MAKER on the
    favored side at favored-best+1c (join best when spread is 1c), never at or
    above p_cons (YES) / never at or above (1-p_cons) (NO). On proxy days the
    favored-side best is proxied by the last print. Passive fill ONLY on a
    later REAL print strictly crossing our level, in (T, close]. Exit:
    settlement (lifecycle `determined`).
    Majors arm (BTC/ETH/SOL/XRP/DOGE, frames days only): at T,
    basis = (coinbase_spot - synth3)/coinbase_spot, synth3 = mean of held
    non-Coinbase constituent mids (>=2 venues). Qualify: |basis| > 2bps AND
    |d_sigma_coinbase| < 1 AND reliable NBBO at T. rti_level = mean(coinbase,
    held venue mids) (CF RTI includes Coinbase). p*_rti = Phi(d_sigma_rti)
    with the SAME coinbase rv. TAKER if p* - ask/100 > _fee_frac(ask) + 0.02
    (symmetrically for NO at 100-bid), else MAKER at favored-side best+1c,
    passive fill on strict-cross print. Exit: settlement.

(c) FEES  taker = 7*p*(1-p) cents (kalshi_fee_per_contract_cents at entry);
    maker = ZERO (maker net == maker gross; the mirror of our maker fill is a
    taker who pays the fee to Kalshi, NOT to us).

(d) MECHANISM PRE-CHECKS (before the trading sim is allowed to count)
    HYPE/BNB per asset: Brier(p_cons) must beat Brier(book-mid-implied prob)
    as a settlement predictor at T by >= 0.005, else arm DEAD for that asset.
    Majors: in-sample logistic regression of outcome on d_sigma_rti must beat
    the same regression on d_sigma_coinbase by per-row delta-logloss whose
    day-bootstrap 95% CI excludes 0 (rti strictly better), else arm DEAD.

(e) KILL CRITERIA (numeric, per arm)
    HYPE and BNB SEPARATELY: day-bootstrap net PnL/contract 95% CI lower
    bound <= 0 OR n < 80 filled windows -> DEAD for that asset. One survivor
    = SHIP candidate for that asset alone.
    Majors: n >= 100 qualifying windows required; CI lower bound <= 0 -> DEAD.

HONESTY RULES BAKED IN: no look-ahead (all features from data <= T; close via
close_epoch_from_ticker); fills only on real prints strictly crossing the
level; one obs per window (single offset 300s); day-bootstrap (never row);
funnel printed (incl. reliable_nbbo refusals + crossed-Bitstamp-snapshot
drops); settlement endgame (fill <120s to close) reported separately.

DATA / RUNTIME: venue_pull + trades passes over all lifecycle-covered days
(2026-05-30 partial .. 2026-06-07); Kalshi frames restricted to <=3 SEALED
days (default 06-03..06-05) via zstd -dc pipes; Kraken/Gemini books are
anchored per UTC-day (prev-day 18:00Z tail seeds the replay; Kraken emits only
snapshot-anchored mids, Gemini after >=4h of absolute-set updates — caveat).

Usage:
  python3 scripts/research/genhunt/04_constituent_fair_value.py --smoke   # 1 day
  python3 scripts/research/genhunt/04_constituent_fair_value.py          # full
"""
from __future__ import annotations

import argparse
import bisect
import math
import os
import subprocess
import sys
import time
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor
from datetime import date, datetime, timedelta, timezone

_REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)

try:
    import orjson as _json
    def _loads(s):
        return _json.loads(s)
except ImportError:  # pragma: no cover
    import json as _json
    def _loads(s):
        return _json.loads(s)

import numpy as np
import pandas as pd

from scripts.research import venue_book_reconstruct as vbr
from scripts.research.early_exit_backtest import reliable_nbbo_timeline
from scripts.research.fairvalue_model import day_bootstrap_ci, _fee_frac  # noqa: F401
from scripts.research.phase1b_real_price_economics import (
    _epoch, close_epoch_from_ticker, load_determined,
)
from scripts.research.settlement_convergence_p1a import kalshi_fee_per_contract_cents
from scripts.research.zstd_stream import assert_zstd_ok  # noqa: E402  (repo root on sys.path above)

CR = os.path.expanduser("~/kalshi-research-data/fairvalue")
OFFSET_S = 300.0
GATE_C = 6.0                 # arm-1 divergence gate, cents
BASIS_BPS = 2.0              # majors qualifier, |basis| > 2bps
TAKER_MARGIN = 0.02          # majors taker margin over fee
VENUE_AGE_S = 30.0           # max staleness of a venue mid at T
PROXY_AGE_S = 120.0          # max staleness of last-trade proxy mid
VOL_STEP_S = 5.0
VOL_WINDOW_S = 300.0
GEMINI_WARM_S = 4 * 3600.0   # Gemini has no snapshot marker: trust after 4h of sets
LATE_FILL_S = 120.0          # settlement-endgame separation
ARM1_ASSETS = ("HYPE", "BNB")
MAJORS = ("BTC", "ETH", "SOL", "XRP", "DOGE")
FRAMES_DAYS_DEFAULT = ("2026-06-03", "2026-06-04", "2026-06-05")

# grep needles per (venue, asset) — substring of the raw envelope line
NEEDLE = {
    ("kraken", a): f"{a}/USD" for a in ("BTC", "ETH", "SOL", "XRP", "DOGE", "HYPE", "BNB")
}
NEEDLE.update({("bitstamp", a): f"order_book_{s}" for a, s in vbr.VENUE_SYMBOLS["bitstamp"].items()})
NEEDLE.update({("gemini", a): s for a, s in vbr.VENUE_SYMBOLS["gemini"].items()})

_BOOK_CLS = {"kraken": vbr.KrakenBook, "bitstamp": vbr.BitstampBook, "gemini": vbr.GeminiBook}


def _norm_cdf(z):
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def _day_files(root: str, day_iso: str, min_hour: int = 0) -> list:
    y, m, d = day_iso.split("-")
    base = os.path.join(root, f"year={y}", f"month={m}", f"day={d}")
    out = []
    if not os.path.isdir(base):
        return out
    for hour in sorted(os.listdir(base)):
        if not hour.startswith("hour="):
            continue
        if int(hour.split("=")[1]) < min_hour:
            continue
        hd = os.path.join(base, hour)
        for conn in sorted(os.listdir(hd)):
            cd = os.path.join(hd, conn)
            if os.path.isdir(cd):
                out.extend(os.path.join(cd, f) for f in sorted(os.listdir(cd)) if f.endswith(".zst"))
    # global order by chunk start-timestamp in the basename
    out.sort(key=lambda p: os.path.basename(p))
    return out


def _pipe_lines(files: list, needles: list):
    """zstd -dc <files> | grep -aF -e n1 -e n2 ... , streamed (never materialized)."""
    if not files:
        return
    CHUNK = 200  # keep argv well under ARG_MAX
    for i in range(0, len(files), CHUNK):
        sub = files[i:i + CHUNK]
        zst = subprocess.Popen(["zstd", "-dc"] + sub, stdout=subprocess.PIPE,
                               stderr=subprocess.DEVNULL, bufsize=1 << 20)
        if needles:
            args = ["grep", "-aF"]
            for n in needles:
                args += ["-e", n]
            grep = subprocess.Popen(args, stdin=zst.stdout, stdout=subprocess.PIPE,
                                    bufsize=1 << 20)
            zst.stdout.close()
            src = grep
        else:
            src = zst
        _exhausted = False
        try:
            for raw in src.stdout:
                yield raw
            _exhausted = True
        finally:
            src.stdout.close()
            src.wait()
            zst.wait()
            # ticket 86bbvrx1t: check the DECOMPRESSOR, not `src`. When a grep
            # is spliced on, src.returncode is grep's — and grep exits 1 on
            # "no matches", which is legitimate, while a zstd that died
            # mid-stream is masked entirely. Only a true EOF can be a
            # truncation, hence the flag: an early break kills zstd with
            # SIGPIPE, which is expected.
            assert_zstd_ok(zst, ",".join(sub[:2]) + f" (+{max(0, len(sub) - 2)} more)",
                           exhausted=_exhausted, require_nonempty=False)


# --------------------------------------------------------------------------
# Venue pass: per (venue, day) -> 1s-downsampled mid series per asset
# --------------------------------------------------------------------------

def venue_day_task(venue: str, day_iso: str, assets: tuple, corpus: str) -> dict:
    """Replay one venue's L2 for one UTC day (seeded with the previous day's
    18:00Z+ tail for book anchoring) and emit per-asset (ts, mid) series
    downsampled to <=1 point/second, ONLY for ts inside the primary day.
    Kraken mids emit only snapshot-anchored; Gemini after GEMINI_WARM_S of
    observed absolute-set updates; Bitstamp every (non-crossed) frame."""
    root = os.path.join(corpus, "venue_pull", f"{venue}_ws")
    d = date.fromisoformat(day_iso)
    prev = (d - timedelta(days=1)).isoformat()
    day_start = datetime(d.year, d.month, d.day, tzinfo=timezone.utc).timestamp()
    day_end = day_start + 86400.0
    files = _day_files(root, prev, min_hour=18) + _day_files(root, day_iso)
    symbols = {}
    for a in assets:
        sym = vbr.VENUE_SYMBOLS.get(venue, {}).get(a)
        if sym is not None:
            symbols[sym] = a
    if not symbols:
        return {"venue": venue, "day": day_iso, "series": {}, "counters": {}}
    needles = [NEEDLE[(venue, a)] for a in assets if (venue, a) in NEEDLE]
    books = {a: _BOOK_CLS[venue]() for a in symbols.values()}
    anchored = {a: venue == "bitstamp" for a in symbols.values()}
    first_seen = {}
    series = {a: ([], []) for a in symbols.values()}   # (ts_list, mid_list)
    last_sec = {a: None for a in symbols.values()}
    n_lines = n_crossed = 0

    for raw in _pipe_lines(files, needles):
        n_lines += 1
        try:
            env = _loads(raw)
            inner = _loads(env["_raw"])
        except Exception:
            continue
        # route to one asset
        if venue == "kraken":
            data = inner.get("data")
            if not data:
                continue
            a = symbols.get(data[0].get("symbol"))
            if a is None:
                continue
            if inner.get("type") == "snapshot":
                anchored[a] = True
        elif venue == "bitstamp":
            ch = inner.get("channel", "")
            a = None
            for sym, aa in symbols.items():
                if ch == f"order_book_{sym}":
                    a = aa
                    break
            if a is None:
                continue
        else:  # gemini
            if inner.get("type") != "l2_updates":
                continue
            a = symbols.get(inner.get("symbol"))
            if a is None:
                continue
        ts = _epoch(env["_wire_recv_ts"])
        if venue == "gemini" and not anchored[a]:
            if a not in first_seen:
                first_seen[a] = ts
            if ts - first_seen[a] >= GEMINI_WARM_S:
                anchored[a] = True
        books[a].apply_frame(inner)
        if not anchored[a] or ts < day_start or ts >= day_end:
            continue
        m = books[a].mid()
        if m is None:
            if books[a].is_crossed():
                n_crossed += 1
            continue
        sec = int(ts)
        tsl, ml = series[a]
        if last_sec[a] == sec:
            ml[-1] = m            # keep the LAST mid within the second
        else:
            tsl.append(sec)
            ml.append(m)
            last_sec[a] = sec
    return {"venue": venue, "day": day_iso,
            "series": {a: s for a, s in series.items() if s[0]},
            "counters": {"lines": n_lines, "crossed_skips": n_crossed,
                         "anchored": {a: anchored[a] for a in anchored}}}


# --------------------------------------------------------------------------
# Kalshi frames pass (<=3 sealed days): NBBO (bid, ask) at T per window
# --------------------------------------------------------------------------

def frames_day_task(day_iso: str, corpus: str, det_close: dict) -> dict:
    """Stream one sealed frames day; for every determined window whose decision
    time T = close-300 falls in this day, build the reliable NBBO timeline and
    read (bid, ask) at T. Returns {ticker: (bid_c, ask_c)} + funnel counts.
    det_close: {ticker: close_epoch} restricted to windows decided this day."""
    path = os.path.join(corpus, "frames", f"day={day_iso}.jsonl.zst")
    if not os.path.exists(path):
        return {"day": day_iso, "nbbo": {}, "funnel": {"missing_file": 1}}
    buffers = defaultdict(list)
    done = set()
    out = {}
    funnel = defaultdict(int)
    stream_ts = 0.0
    n_lines = 0

    def _finalize(tk):
        buf = buffers[tk]
        buf.sort(key=lambda x: x[0])
        T = det_close[tk] - OFFSET_S
        tl = reliable_nbbo_timeline([(ts, f) for ts, f in buf if ts <= T])
        if not tl:
            funnel["no_reliable_book"] += 1
            return
        bid = ask = None
        for ts, b, k in tl:
            if b is not None:
                bid = b
            if k is not None:
                ask = k
        if ask is None or not (0 < ask < 100):
            funnel["no_quote_at_T"] += 1
            return
        out[tk] = (bid, ask)
        funnel["nbbo_ok"] += 1

    for raw in _pipe_lines([path], []):
        n_lines += 1
        try:
            env = _loads(raw)
            inner = _loads(env["_raw"])
            tk = inner["msg"]["market_ticker"]
        except Exception:
            continue
        if tk in done or tk not in det_close:
            continue
        ts = _epoch(env["_wire_recv_ts"])
        if ts > stream_ts:
            stream_ts = ts
        buffers[tk].append((ts, inner))
        if n_lines % 100000 == 0:
            cut = stream_ts - 60.0
            for t in [t for t in list(buffers) if det_close[t] - OFFSET_S < cut]:
                _finalize(t)
                done.add(t)
                del buffers[t]
    for t in list(buffers):
        _finalize(t)
    funnel["lines"] = n_lines
    return {"day": day_iso, "nbbo": out, "funnel": dict(funnel)}


# --------------------------------------------------------------------------
# Trades pass: real prints per ticker (fills + proxy mid). Event-time ts.
# --------------------------------------------------------------------------

def trades_day_task(day_iso: str, corpus: str, prefixes: tuple) -> dict:
    path = os.path.join(corpus, "trades", f"day={day_iso}.jsonl.zst")
    if not os.path.exists(path):
        return {}
    out = defaultdict(list)
    needles = [f"KX{a}15M-" for a in prefixes]
    for raw in _pipe_lines([path], needles):
        try:
            env = _loads(raw)
            msg = _loads(env["_raw"])["msg"]
            tk = msg["market_ticker"]
            yc = float(msg["yes_price_dollars"]) * 100.0
            ts = float(msg["ts"])
        except Exception:
            continue
        out[tk].append((ts, yc))
    return dict(out)


# --------------------------------------------------------------------------
# Coinbase spot (majors arm only)
# --------------------------------------------------------------------------

def coinbase_task(days: tuple, corpus: str) -> dict:
    prods = {f"{a}-USD": a for a in MAJORS}
    raw = defaultdict(dict)
    files = []
    for di in days:
        files += _day_files(os.path.join(corpus, "coinbase_ticker"), di)
    for line in _pipe_lines(files, list(prods)):
        try:
            env = _loads(line)
            inner = _loads(env["_raw"])
            a = prods.get(inner.get("product_id"))
            px = inner.get("price")
            if a is None or px is None:
                continue
            raw[a][int(_epoch(env["_wire_recv_ts"]))] = float(px)
        except Exception:
            continue
    out = {}
    for a, d in raw.items():
        secs = sorted(d)
        out[a] = (secs, [d[s] for s in secs])
    return out


# --------------------------------------------------------------------------
# Series lookup helpers
# --------------------------------------------------------------------------

def _series_at(tl, t, max_age):
    if tl is None:
        return None
    secs, vals = tl
    i = bisect.bisect_right(secs, t) - 1
    if i < 0 or t - secs[i] > max_age:
        return None
    return vals[i]


def _rv_from(fn, t):
    """stdev of per-5s log returns of fn(t) over [t-300, t] (mirror of
    fairvalue_extract._realized_vol but over an arbitrary level function)."""
    k = int(VOL_WINDOW_S // VOL_STEP_S)
    samples = []
    for j in range(k + 1):
        s = fn(t - j * VOL_STEP_S)
        if s is None or s <= 0:
            return None
        samples.append(s)
    samples.reverse()
    rets = [math.log(samples[i] / samples[i - 1]) for i in range(1, len(samples))]
    if len(rets) < 10:
        return None
    m = sum(rets) / len(rets)
    var = sum((r - m) ** 2 for r in rets) / (len(rets) - 1)
    return math.sqrt(var)


def _maker_price(side, bid, ask, p_model):
    """Favored-side best + 1c (join best when that would cross), never at/above
    the model fair value. Returns price in cents for the side's own contract
    (YES price for YES side, NO price for NO side), or None if no valid level."""
    if side == "yes":
        cap = p_model * 100.0
        if bid is None:
            return None
        px = bid + 1.0 if (ask is None or bid + 1.0 < ask) else bid
        if px >= cap:
            px = math.floor(cap - 1e-9)
            if bid is not None and px > bid + 1.0:
                px = bid + 1.0
        return px if 1.0 <= px < cap else None
    # NO side: no_bid = 100-ask, no_ask = 100-bid
    cap = (1.0 - p_model) * 100.0
    if ask is None:
        return None
    no_bid = 100.0 - ask
    no_ask = 100.0 - bid if bid is not None else None
    px = no_bid + 1.0 if (no_ask is None or no_bid + 1.0 < no_ask) else no_bid
    if px >= cap:
        px = math.floor(cap - 1e-9)
        if px > no_bid + 1.0:
            px = no_bid + 1.0
    return px if 1.0 <= px < cap else None


def _maker_fill(prints, T, close, side, px):
    """First REAL print in (T, close] STRICTLY crossing our resting level.
    YES bid at px: filled by a print at yes_price < px.
    NO  bid at px (== resting YES offer at 100-px): print at yes_price > 100-px."""
    for ts, yc in prints:
        if ts <= T or ts > close:
            continue
        if side == "yes" and yc < px - 1e-9:
            return ts
        if side == "no" and yc > (100.0 - px) + 1e-9:
            return ts
    return None


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--corpus", default=CR)
    ap.add_argument("--frames-days", default=",".join(FRAMES_DAYS_DEFAULT))
    ap.add_argument("--smoke", action="store_true", help="single sealed day (2026-06-04)")
    ap.add_argument("--workers", type=int, default=9)
    a = ap.parse_args(argv)
    corpus = os.path.expanduser(a.corpus)
    t0 = time.time()

    frames_days = tuple(a.frames_days.split(","))
    if a.smoke:
        frames_days = ("2026-06-04",)
    majors_days = frames_days  # majors arm needs reliable NBBO -> frames days only

    print("[cfv] load_determined ...", flush=True)
    cache = os.path.join(corpus, "_tmp_genhunt04_determined.pkl")
    if os.path.exists(cache):
        import pickle
        with open(cache, "rb") as fh:
            determined = pickle.load(fh)
    else:
        determined = load_determined(os.path.join(corpus, "lifecycle"),
                                     set(ARM1_ASSETS) | set(MAJORS))
        import pickle
        with open(cache, "wb") as fh:
            pickle.dump(determined, fh)
    # decision-day assignment: day = UTC date of T = close-300
    win = {}
    for tk, d in determined.items():
        if d.get("strike") is None:
            continue
        close = close_epoch_from_ticker(tk)
        T = close - OFFSET_S
        day = datetime.fromtimestamp(T, tz=timezone.utc).date().isoformat()
        win[tk] = {**d, "close": close, "T": T, "day": day}
    all_days = sorted({w["day"] for w in win.values()})
    arm1_days = [d for d in all_days if d >= "2026-05-30"]
    if a.smoke:
        arm1_days = ["2026-06-04"]
    print(f"[cfv]   {len(determined)} determined, {len(win)} with strike; "
          f"days {arm1_days[0]}..{arm1_days[-1]} ({len(arm1_days)})", flush=True)

    # ---- dispatch all heavy passes -------------------------------------
    det_close_by_day = defaultdict(dict)
    for tk, w in win.items():
        det_close_by_day[w["day"]][tk] = w["close"]

    futs = {}
    with ProcessPoolExecutor(max_workers=a.workers) as ex:
        for d in arm1_days:
            futs[("kraken", d)] = ex.submit(venue_day_task, "kraken", d, ARM1_ASSETS, corpus)
            futs[("bitstamp", d)] = ex.submit(venue_day_task, "bitstamp", d, ("HYPE",), corpus)
            futs[("trades", d)] = ex.submit(trades_day_task, d, corpus,
                                            tuple(set(ARM1_ASSETS) | set(MAJORS)))
        for d in majors_days:
            futs[("kraken_maj", d)] = ex.submit(venue_day_task, "kraken", d, MAJORS, corpus)
            futs[("bitstamp_maj", d)] = ex.submit(venue_day_task, "bitstamp", d, MAJORS, corpus)
            futs[("gemini_maj", d)] = ex.submit(venue_day_task, "gemini", d, MAJORS, corpus)
            futs[("frames", d)] = ex.submit(frames_day_task, d, corpus, det_close_by_day[d])
        futs[("coinbase", "*")] = ex.submit(coinbase_task, majors_days, corpus)

        res = {}
        for k, f in futs.items():
            res[k] = f.result()
            print(f"[cfv]   done {k}  (+{time.time()-t0:.0f}s)", flush=True)

    # merge venue series: (venue, asset, day) -> series; lookups stay in-day
    vser = {}
    crossed_bitstamp = 0
    for (tag, d), r in res.items():
        if tag in ("kraken", "bitstamp", "kraken_maj", "bitstamp_maj", "gemini_maj"):
            venue = r["venue"]
            if venue == "bitstamp":
                crossed_bitstamp += r["counters"].get("crossed_skips", 0)
            for asset, s in r["series"].items():
                vser[(venue, asset, d)] = s
    trades = defaultdict(list)
    for d in arm1_days:
        for tk, pr in res.get(("trades", d), {}).items():
            trades[tk].extend(pr)   # a window's prints can span a UTC midnight
    trades = dict(trades)
    nbbo = {}
    frames_funnel = defaultdict(int)
    for d in majors_days:
        r = res.get(("frames", d), {})
        nbbo.update(r.get("nbbo", {}))
        for k, v in r.get("funnel", {}).items():
            frames_funnel[k] += v
    cb_spot = res.get(("coinbase", "*"), {})
    print(f"[cfv] venue series merged: {len(vser)} (venue,asset,day) keys; "
          f"trades tickers={len(trades)}; nbbo windows={len(nbbo)}", flush=True)

    def venue_mid(venue, asset, day, t):
        return _series_at(vser.get((venue, asset, day)), t, VENUE_AGE_S)

    def cons_mid_fn(asset, day):
        if asset == "HYPE":
            def f(t):
                ms = [m for m in (venue_mid("kraken", "HYPE", day, t),
                                  venue_mid("bitstamp", "HYPE", day, t)) if m is not None]
                return sum(ms) / len(ms) if ms else None
        else:  # BNB: Kraken only
            def f(t):
                return venue_mid("kraken", "BNB", day, t)
        return f

    def proxy_mid(tk, t):
        pr = trades.get(tk)
        if not pr:
            return None
        i = bisect.bisect_right(pr, (t, float("inf"))) - 1
        if i < 0 or t - pr[i][0] > PROXY_AGE_S:
            return None
        return pr[i][1]

    for tk in trades:
        trades[tk].sort()

    # =====================================================================
    # ARM 1: HYPE / BNB constituent fair value (all lifecycle days)
    # =====================================================================
    print("\n================ ARM 1: HYPE/BNB constituent fair value ================")
    arm1_rows = []
    brier_rows = []
    fun = {as_: defaultdict(int) for as_ in ARM1_ASSETS}
    diverg = []
    for tk, w in sorted(win.items()):
        asset = w["asset"]
        if asset not in ARM1_ASSETS or w["day"] not in arm1_days:
            continue
        F = fun[asset]
        F["windows"] += 1
        day, T, close = w["day"], w["T"], w["close"]
        cf = cons_mid_fn(asset, day)
        cons = cf(T)
        if cons is None:
            F["no_venue_mid"] += 1
            continue
        rv = _rv_from(cf, T)
        if rv is None or rv <= 0:
            F["no_rv"] += 1
            continue
        sigma = cons * rv * math.sqrt(OFFSET_S / VOL_STEP_S)
        z = (cons - w["strike"]) / sigma if sigma > 0 else 0.0
        p_cons = _norm_cdf(z)
        # book mid at T: NBBO on frames days, last-trade proxy elsewhere
        is_frames = day in frames_days
        bid = ask = None
        if is_frames:
            if tk in nbbo:
                bid, ask = nbbo[tk]
                mid_c = (bid + ask) / 2.0 if bid is not None else float(ask)
            else:
                F["no_reliable_nbbo"] += 1
                continue
            pm = proxy_mid(tk, T)
            if pm is not None:
                diverg.append(abs(pm - mid_c))
        else:
            pm = proxy_mid(tk, T)
            if pm is None:
                F["no_proxy_mid"] += 1
                continue
            mid_c = pm
        F["priced"] += 1
        won = int(w["result"] == "yes")
        brier_rows.append((asset, day, p_cons, mid_c / 100.0, won))
        edge_c = p_cons * 100.0 - mid_c
        if abs(edge_c) < GATE_C:
            F["gate_fail"] += 1
            continue
        side = "yes" if edge_c > 0 else "no"
        if is_frames:
            px = _maker_price(side, bid, ask, p_cons)
        else:
            # proxy day: favored-side best proxied by the last print
            if side == "yes":
                px = min(mid_c, math.floor(p_cons * 100.0 - 1e-9))
                px = px if 1.0 <= px < p_cons * 100.0 else None
            else:
                px = min(100.0 - mid_c, math.floor((1.0 - p_cons) * 100.0 - 1e-9))
                px = px if 1.0 <= px < (1.0 - p_cons) * 100.0 else None
        if px is None:
            F["no_maker_level"] += 1
            continue
        F["rested"] += 1
        fts = _maker_fill(trades.get(tk, []), T, close, side, px)
        if fts is None:
            F["unfilled"] += 1
            continue
        F["filled"] += 1
        late = (close - fts) < LATE_FILL_S
        if late:
            F["filled_late"] += 1
        if side == "yes":
            pnl = (100.0 - px) if won else -px
        else:
            pnl = (100.0 - px) if not won else -px
        arm1_rows.append({"asset": asset, "day": day, "ticker": tk, "side": side,
                          "px": px, "p_cons": p_cons, "mid_c": mid_c, "late": late,
                          "is_frames": int(is_frames), "won": won, "pnl_c": pnl})

    bdf = pd.DataFrame(brier_rows, columns=["asset", "day", "p_cons", "p_mid", "won"])
    adf = pd.DataFrame(arm1_rows)
    if diverg:
        print(f"  proxy-vs-NBBO divergence (frames days, HYPE/BNB): n={len(diverg)} "
              f"mean={np.mean(diverg):.2f}c  p90={np.percentile(diverg, 90):.2f}c")
    arm1_verdicts = {}
    key1 = {}
    for asset in ARM1_ASSETS:
        F = fun[asset]
        print(f"\n  --- {asset} ---")
        print("  FUNNEL: " + "  ".join(f"{k}={F[k]}" for k in
              ("windows", "no_venue_mid", "no_rv", "no_reliable_nbbo", "no_proxy_mid",
               "priced", "gate_fail", "no_maker_level", "rested", "unfilled",
               "filled", "filled_late")))
        b = bdf[bdf.asset == asset]
        if len(b) < 30:
            print(f"  PRE-CHECK: only {len(b)} priced windows -> DATA_GAP")
            arm1_verdicts[asset] = "DATA_GAP"
            continue
        br_cons = float(np.mean((b.p_cons - b.won) ** 2))
        br_mid = float(np.mean((b.p_mid - b.won) ** 2))
        impr = br_mid - br_cons
        ok_pre = impr >= 0.005
        print(f"  PRE-CHECK Brier@T-300: p_cons={br_cons:.5f}  mid={br_mid:.5f}  "
              f"improvement={impr:+.5f} (need >= +0.005) -> {'PASS' if ok_pre else 'FAIL'}  (n={len(b)})")
        sel = adf[adf.asset == asset] if len(adf) else adf
        n_fill = len(sel)
        if n_fill == 0:
            print(f"  TRADES: 0 filled windows -> DEAD (n < 80)")
            arm1_verdicts[asset] = "DEAD"
            key1[asset] = f"n=0, pre-check impr={impr:+.4f}"
            continue
        mean, lo, hi = day_bootstrap_ci(sel, "pnl_c")
        wr = sel.won.where(sel.side == "yes", 1 - sel.won).mean()
        print(f"  TRADES: n={n_fill} filled ({int(sel.late.sum())} late<120s, "
              f"{int(sel.is_frames.sum())} on frames days)  win%={100*wr:.1f}  "
              f"net PnL/ct={mean:+.2f}c  dayCI95=[{lo:+.2f},{hi:+.2f}]c "
              f"({sel.day.nunique()} days)")
        if not sel[sel.late == 0].empty:
            m2, l2, h2 = day_bootstrap_ci(sel[sel.late == 0], "pnl_c")
            print(f"          excl-late subset: n={len(sel[sel.late==0])} "
                  f"net={m2:+.2f}c CI=[{l2:+.2f},{h2:+.2f}]c")
        dead = (not ok_pre) or n_fill < 80 or lo <= 0
        arm1_verdicts[asset] = "DEAD" if dead else "SHIP_CANDIDATE"
        key1[asset] = (f"n={n_fill}, net={mean:+.2f}c/ct, CI=[{lo:+.2f},{hi:+.2f}], "
                       f"pre-check impr={impr:+.4f}")
        why = []
        if not ok_pre:
            why.append("pre-check FAIL")
        if n_fill < 80:
            why.append(f"n={n_fill}<80")
        if lo <= 0:
            why.append("CI LB<=0")
        print(f"  VERDICT[{asset}]: {arm1_verdicts[asset]}" +
              (f"  ({', '.join(why)})" if why else ""))

    # =====================================================================
    # ARM 2: majors RTI-basis correction (frames days only)
    # =====================================================================
    print("\n================ ARM 2: majors BRTI-vs-Coinbase basis ================")
    F2 = defaultdict(int)
    feat_rows = []
    m_rows = []
    for tk, w in sorted(win.items()):
        asset = w["asset"]
        if asset not in MAJORS or w["day"] not in majors_days:
            continue
        F2["windows"] += 1
        day, T, close = w["day"], w["T"], w["close"]
        cb = _series_at(cb_spot.get(asset), T, 60.0)
        if cb is None or cb <= 0:
            F2["no_coinbase"] += 1
            continue
        rv = _rv_from(lambda t: _series_at(cb_spot.get(asset), t, 60.0), T)
        if rv is None or rv <= 0:
            F2["no_rv"] += 1
            continue
        vms = [m for m in (venue_mid(v, asset, day, T) for v in vbr.VENUES
                           if asset in vbr.VENUE_SYMBOLS[v]) if m is not None]
        if len(vms) < 2:
            F2["lt2_venues"] += 1
            continue
        synth3 = sum(vms) / len(vms)
        basis = (cb - synth3) / cb
        rti = (cb + sum(vms)) / (1 + len(vms))
        sig_cb = cb * rv * math.sqrt(OFFSET_S / VOL_STEP_S)
        z_cb = (cb - w["strike"]) / sig_cb
        sig_r = rti * rv * math.sqrt(OFFSET_S / VOL_STEP_S)
        z_rti = (rti - w["strike"]) / sig_r
        won = int(w["result"] == "yes")
        feat_rows.append((day, z_cb, z_rti, won))
        if tk not in nbbo:
            F2["no_reliable_nbbo"] += 1
            continue
        bid, ask = nbbo[tk]
        if abs(basis) * 1e4 <= BASIS_BPS:
            F2["basis_small"] += 1
            continue
        if abs(z_cb) >= 1.0:
            F2["not_near_money"] += 1
            continue
        F2["qualified"] += 1
        p_star = _norm_cdf(z_rti)
        # taker first (either side), else maker on favored side
        traded = None
        fee_y = _fee_frac(np.array([ask]))[0]
        if p_star - ask / 100.0 > fee_y + TAKER_MARGIN:
            pnl = ((100.0 - ask) if won else -ask) - kalshi_fee_per_contract_cents(ask)
            traded = ("yes", "taker", ask, pnl)
        elif bid is not None:
            no_ask = 100.0 - bid
            fee_n = _fee_frac(np.array([no_ask]))[0]
            if (1.0 - p_star) - no_ask / 100.0 > fee_n + TAKER_MARGIN:
                pnl = ((100.0 - no_ask) if not won else -no_ask) \
                    - kalshi_fee_per_contract_cents(no_ask)
                traded = ("no", "taker", no_ask, pnl)
        if traded is None:
            mid_c = (bid + ask) / 2.0 if bid is not None else float(ask)
            side = "yes" if p_star * 100.0 > mid_c else "no"
            px = _maker_price(side, bid, ask, p_star)
            if px is None:
                F2["no_maker_level"] += 1
                continue
            fts = _maker_fill(trades.get(tk, []), T, close, side, px)
            if fts is None:
                F2["maker_unfilled"] += 1
                continue
            if side == "yes":
                pnl = (100.0 - px) if won else -px
            else:
                pnl = (100.0 - px) if not won else -px
            traded = (side, "maker", px, pnl)
            if (close - fts) < LATE_FILL_S:
                F2["maker_filled_late"] += 1
        side, kind, px, pnl = traded
        F2[f"trade_{kind}"] += 1
        m_rows.append({"asset": asset, "day": day, "ticker": tk, "side": side,
                       "kind": kind, "px": px, "won": won, "pnl_c": pnl,
                       "basis_bps": basis * 1e4})
    print("  FUNNEL: " + "  ".join(f"{k}={F2[k]}" for k in
          ("windows", "no_coinbase", "no_rv", "lt2_venues", "no_reliable_nbbo",
           "basis_small", "not_near_money", "qualified", "trade_taker",
           "no_maker_level", "maker_unfilled", "maker_filled_late", "trade_maker")))
    print(f"  frames funnel: {dict(frames_funnel)}")
    print(f"  crossed-Bitstamp-snapshot tick drops (known 0.2% artifact): {crossed_bitstamp}")

    majors_verdict = "DEAD"
    key2 = "no data"
    fdf = pd.DataFrame(feat_rows, columns=["day", "z_cb", "z_rti", "won"])
    if len(fdf) >= 50 and fdf.won.nunique() > 1:
        from sklearn.linear_model import LogisticRegression
        eps = 1e-12
        ll = {}
        for c in ("z_cb", "z_rti"):
            m = LogisticRegression(max_iter=1000).fit(fdf[[c]].values, fdf.won.values)
            p = np.clip(m.predict_proba(fdf[[c]].values)[:, 1], eps, 1 - eps)
            ll[c] = -(fdf.won.values * np.log(p) + (1 - fdf.won.values) * np.log(1 - p))
        fdf["dll"] = ll["z_cb"] - ll["z_rti"]   # >0 -> rti better
        mean, lo, hi = day_bootstrap_ci(fdf, "dll")
        pre_ok = lo > 0
        print(f"  PRE-CHECK delta-logloss (cb - rti, >0 favors RTI): n={len(fdf)} "
              f"mean={mean:+.5f} dayCI95=[{lo:+.5f},{hi:+.5f}] "
              f"({fdf.day.nunique()} days) -> {'PASS' if pre_ok else 'FAIL'}")
    else:
        pre_ok = False
        print(f"  PRE-CHECK: insufficient feature rows ({len(fdf)}) -> cannot run")
    mdf = pd.DataFrame(m_rows)
    n_qual = F2["qualified"]
    if len(mdf):
        mean, lo, hi = day_bootstrap_ci(mdf, "pnl_c")
        print(f"  TRADES: n={len(mdf)} (qualified={n_qual})  "
              f"net PnL/ct={mean:+.2f}c  dayCI95=[{lo:+.2f},{hi:+.2f}]c  "
              f"({mdf.day.nunique()} days; taker={int((mdf.kind=='taker').sum())} "
              f"maker={int((mdf.kind=='maker').sum())})")
        for asset in MAJORS:
            s = mdf[mdf.asset == asset]
            if len(s):
                m3, l3, h3 = day_bootstrap_ci(s, "pnl_c")
                print(f"    {asset}: n={len(s)} net={m3:+.2f}c CI=[{l3:+.2f},{h3:+.2f}]c")
        dead = (not pre_ok) or n_qual < 100 or lo <= 0
        majors_verdict = "DEAD" if dead else "SHIP_CANDIDATE"
        key2 = (f"n_trades={len(mdf)} (n_qual={n_qual}), net={mean:+.2f}c/ct, "
                f"CI=[{lo:+.2f},{hi:+.2f}], pre-check={'PASS' if pre_ok else 'FAIL'}")
        why = []
        if not pre_ok:
            why.append("pre-check FAIL")
        if n_qual < 100:
            why.append(f"n_qual={n_qual}<100")
        if lo <= 0:
            why.append("CI LB<=0")
        print(f"  VERDICT[majors]: {majors_verdict}" + (f"  ({', '.join(why)})" if why else ""))
    else:
        key2 = f"0 trades (qualified={n_qual}), pre-check={'PASS' if pre_ok else 'FAIL'}"
        print(f"  VERDICT[majors]: DEAD (no trades)")

    print("\n================ OVERALL ================")
    for asset in ARM1_ASSETS:
        print(f"  {asset}: {arm1_verdicts.get(asset, 'DATA_GAP')}  [{key1.get(asset, '-')}]")
    print(f"  MAJORS: {majors_verdict}  [{key2}]")
    print(f"  wall: {time.time()-t0:.0f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
