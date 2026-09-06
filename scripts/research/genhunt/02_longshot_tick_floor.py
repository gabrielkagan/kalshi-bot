"""Longshot Tick-Floor Premium Harvest (merged: + Lottery-Premium Harvest).

PRE-REGISTRATION (stated BEFORE any data was scored — do not edit after results):

(a) COUNTERPARTY: small-lot lottery buyers (cumulative-prospect-theory
    overweighting of tiny probabilities). A fresh 15-minute window every 15min
    x 9 assets gives them a renewable stream of "lottery tickets"; the 1c tick
    floor means anything truly worth <0.5c still costs them 1c, and the fee
    7*p*(1-p) is ~0.07c at p=0.01 so extremes are nearly free for them to BUY
    taker. They pay us by lifting our passively-resting tail asks.

(b) SIGNAL / ENTRY / EXIT (two arms, one harness):
    Self-computed distance signal (NO use of the Kalshi price as the signal):
      z(t) = (spot(t) - strike) / (spot(t) * rv_5s(t) * sqrt(tau/5)),
      tau = close - t; rv_5s = stdev of trailing per-5s log returns over 300s
      (identical formula to scripts.research.fairvalue_extract). p_yes = Phi(z).
      Spot source: Coinbase ticker for BTC/ETH/SOL/XRP/DOGE/ADA/BCH; Bitstamp
      L2 full-snapshot mid (venue_book_reconstruct.BitstampBook) for HYPE.
      BNB: only Kraken constitutes it and the Kraken L2 stream (20GB zst,
      ~500M frames) cannot be replayed inside the runtime budget -> BNB is
      EXCLUDED from this run (documented honest subset, not a goalpost move).
    ARM A (tick-floor): the gate is dist_sigma > 3 on the doomed side
      (z > +3 -> NO doomed; z < -3 -> YES doomed), evaluated CONTINUOUSLY:
      arrival = first time the gate fires (detected on a 15s grid over
      [close-870s, close-120s], strictly-past data only), and a doomed-side
      1c/2c taker print is mirrored iff ts > arrival AND the gate STILL holds
      at the print's own timestamp. Endgame prints (close-120s, close] are
      reported SEPARATELY, never in the headline. Hold to settlement, no exit.
      AMENDMENT (pre-results, documented): the first implementation pinned a
      single arrival at T-12min; the smoke day showed 0 qualifying windows
      because Kalshi sets the single strike ~ATM at window open, so >3-sigma
      distance only develops mid-window. The fixed arrival was an
      implementation choice not in the build spec (whose T-12..T-3 signal
      window belongs to Arm B); continuous gating is the faithful reading of
      "1-2c asks where self-computed dist_sigma > 3". Amended BEFORE any Arm A
      PnL existed (the arm had zero trades under the old reading).
    ARM B (deep-OTM): mirror taker BUY prints of the OTM side s at price in
      [4,15]c, ts in [close-720s, close-180s], CONDITIONED on
      p_s(print_ts) <= price_c/200 (i.e. p_normal <= ask/2). Unconditioned
      benchmark = same prints without the p-condition. Hold to settlement.
    EXCLUSIONS (both arms): 0.01-dust prints (count_fp <= 0.0101) and the
      225-size campaign class (count_fp == 225) — known-informed cheap flow,
      in flight as COPY signals elsewhere; exclude, don't fight.

(c) FEE MATH: maker pays ZERO fee on Kalshi. Mirror PnL per contract =
      price_c - 100*(sold side settles ITM)  == taker GROSS negated
    (gotcha 1: never negate taker NET — the taker's fee goes to Kalshi, not us).

(d) FILL REALISM: strict cross only — we count only real prints at our level
    strictly after arrival. Queue-honest: depth at level is unavailable in the
    trades-only pass, so mirrored size is capped at 10% participation of the
    qualifying printed volume per window. Frames pass (<=3 sealed days,
    --frames-days) anchors ask-level realism only: at each mirrored print, was
    there a reliable (snapshot-anchored, uncrossed) book whose prevailing ask
    on that side was <= the print price (i.e. an order resting at the print
    price was at-or-behind the NBBO, not a fantasy level)?

(e) KILL CRITERIA (numeric, per arm, independent verdicts):
    ARM A — DEAD if day-bootstrap (day_bootstrap_ci, days never rows) 95% CI
      lower bound of net PnL/ct (per-window obs, one row per window) <= 0;
      DEAD if realized ITM settle rate of qualifying windows >= 0.8%;
      CAPACITY-DEAD if mirrored volume under the 10% cap < 10,000 contracts
      over 12 days. DATA NOTE pre-registered: only ~7.1 effective sealed days
      exist (05-30 partial 21:13Z, markers end 06-06); the capacity leg is
      therefore ALSO reported against the prorated bar
      10,000 * effective_days/12, and the day shortfall is a stated caveat.
      The 3c bucket is NOT traded and NOT extended (per-ticker cluster check
      required first; out of scope here).
    ARM B — ALIVE only if CI LB > 0 AND >= 10 distinct days AND >= 200
      conditioned prints AND conditioned EV beats the UNCONDITIONED same-band
      maker EV by >= 1.0 c/ct. Any leg fails -> arm dead. Pre-registered:
      with only ~8 sealed days the >=10-day leg CANNOT pass, so the best Arm B
      can do in this run is INCONCLUSIVE (alive-so-far) if every other leg
      passes; if CI LB <= 0 or the EV-gap leg fails it is DEAD outright.

(f) HONESTY RAILS: one observation per window per arm (prints aggregated to a
    single window row); no look-ahead (signal uses strictly-past spot; close
    from close_epoch_from_ticker); spot staleness guard 30s; rv must be > 0;
    full usable-data funnel printed.

Usage:
  python3 scripts/research/genhunt/02_longshot_tick_floor.py \
      [--days 2026-06-04] [--frames-days 2026-06-04] [--no-cache]
"""
from __future__ import annotations

import argparse
import json
import math
import os
import pickle
import subprocess
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone

_REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)

import numpy as np
import pandas as pd

from scripts.research import venue_book_reconstruct as vbr
from scripts.research.early_exit_backtest import reliable_nbbo_timeline
from scripts.research.fairvalue_model import day_bootstrap_ci
from scripts.research.phase1b_real_price_economics import (
    _epoch, _is_crypto_15m, close_epoch_from_ticker, load_determined,
)
from scripts.research.zstd_stream import assert_zstd_ok  # noqa: E402  (repo root on sys.path above)

CR = os.path.expanduser("~/kalshi-research-data/fairvalue")
CACHE = os.path.join(CR, "_tmp_genhunt02")
ASSETS = ("BTC", "ETH", "SOL", "XRP", "HYPE", "DOGE", "BNB", "ADA", "BCH")
CB_PRODUCT = {"BTC": "BTC-USD", "ETH": "ETH-USD", "SOL": "SOL-USD",
              "XRP": "XRP-USD", "DOGE": "DOGE-USD", "BCH": "BCH-USD", "ADA": "ADA-USD"}
UNIVERSE = ("BTC", "ETH", "SOL", "XRP", "DOGE", "ADA", "BCH", "HYPE")  # BNB excluded (see header)

VOL_WIN_S, VOL_STEP_S = 300.0, 5.0
STALE_S = 30.0          # max spot staleness at a decision point
Z_GATE = 3.0            # Arm A dist_sigma gate
ARRIVAL_OFF_S = 720.0   # T-12min arrival (both arms' signal window start)
ENDGAME_S = 120.0       # endgame boundary (Arm A reported separately)
ARMB_END_OFF_S = 180.0  # Arm B signal window end (T-3min)
PARTICIPATION = 0.10
DUST_MAX = 0.0101
CAMPAIGN_SIZE = 225.0
ARMA_PRICES = (1, 2)
ARMB_LO, ARMB_HI = 4, 15
CAP_RAW = 10_000.0


def log(msg):
    print(f"[lottery] {msg}", flush=True)


def _norm_cdf(z):
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


# ---------------------------------------------------------------- data loads

def sealed_days():
    days = []
    for f in sorted(os.listdir(CR)):
        if f.startswith(".done_2026"):
            d = f[len(".done_"):]
            if os.path.exists(os.path.join(CR, "trades", f"day={d}.jsonl.zst")):
                days.append(d)
    return days


def _day_dirs(root, days):
    out = []
    for d in days:
        y, m, dd = d.split("-")
        p = os.path.join(root, f"year={y}", f"month={m}", f"day={dd}")
        if os.path.isdir(p):
            out.append(p)
    return out


def _pipe(cmd):
    """Stream stdout lines (bytes) of a shell pipeline, checked.

    Ticket 86bbvrx1t. Two things are required for a shell pipeline that a
    plain `zstd -dc` call does not need:

    1. ``pipefail`` + an explicit bash. Without it the pipeline's status is
       the status of the LAST command (grep/xargs), so a zstd that dies
       mid-stream is MASKED and the caller silently receives a prefix.
    2. The `exhausted` flag, so an early ``break`` by the caller — which
       kills the pipeline with SIGPIPE, legitimately non-zero — is not
       mistaken for a truncation.
    """
    proc = subprocess.Popen("set -o pipefail; " + cmd, shell=True,
                            executable="/bin/bash",
                            stdout=subprocess.PIPE, bufsize=1 << 22)
    _exhausted = False
    try:
        for raw in proc.stdout:
            yield raw
        _exhausted = True
    finally:
        proc.stdout.close()
        proc.wait()
        assert_zstd_ok(proc, cmd, exhausted=_exhausted, require_nonempty=False)


def load_spot_fast(days):
    """{asset: (ts_list, px_list)} from Coinbase ticker bronze, ~1 obs/(pid,sec).
    String-prefilter before json (840MB zst corpus; full parse would blow budget)."""
    dirs = _day_dirs(os.path.join(CR, "coinbase_ticker"), days)
    if not dirs:
        return {}
    cmd = ("find " + " ".join(f"'{d}'" for d in dirs) +
           " -name '*.zst' -print0 | xargs -0 zstd -dc --")
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
        pid = line[i + len(pid_pat):j]
        a = want.get(pid)
        if a is None:
            continue
        key = (pid, line[18:37])  # (product, ISO-second) downsample
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
    out = {}
    for a, v in pts.items():
        v.sort()
        out[a] = ([t for t, _ in v], [p for _, p in v])
    log(f"spot: scanned {n:,} cb lines, kept {kept:,} -> " +
        " ".join(f"{a}:{len(out[a][0]):,}" for a in sorted(out)))
    return out


def load_hype_bitstamp(days, every=8):
    """HYPE synthetic mid timeline from Bitstamp full-snapshot L2 (the one HYPE
    venue with self-complete frames; Kraken replay is out of budget). Keeps
    every Nth hypeusd frame (~1 per 1.8s raw cadence ~4.5/s)."""
    dirs = _day_dirs(os.path.join(CR, "venue_pull", "bitstamp_ws"), days)
    if not dirs:
        return None
    cmd = ("find " + " ".join(f"'{d}'" for d in dirs) +
           " -name '*.zst' -print0 | xargs -0 zstd -dc -- | LC_ALL=C grep hypeusd")
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
    log(f"HYPE bitstamp: {n:,} hypeusd frames, kept {len(pts):,} mids")
    return ([t for t, _ in pts], [p for _, p in pts]) if pts else None


def load_trades(days):
    """{ticker: [(ts, yes_c, no_c, taker_side, count)]} for prints with either
    side <= 15c (everything both arms can use), plus tape-wide context counters."""
    needles = (b'yes_price_dollars\\":\\"0.0', b'yes_price_dollars\\":\\"0.1',
               b'yes_price_dollars\\":\\"0.8', b'yes_price_dollars\\":\\"0.9')
    tape = defaultdict(list)
    ctx = {"lines": 0, "kept": 0, "le2_taker_ct": 0.0, "le2_dust_ct": 0.0,
           "le2_225_ct": 0.0, "tmin": {}, "tmax": {}}
    for d in days:
        path = os.path.join(CR, "trades", f"day={d}.jsonl.zst")
        for line in _pipe(f"zstd -dc '{path}'"):
            ctx["lines"] += 1
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
            ctx["kept"] += 1
            ctx["tmin"][d] = min(ctx["tmin"].get(d, ts), ts)
            ctx["tmax"][d] = max(ctx["tmax"].get(d, ts), ts)
            px_taken = yes_c if side == "yes" else no_c
            if px_taken <= 2:
                ctx["le2_taker_ct"] += ct
                if ct <= DUST_MAX:
                    ctx["le2_dust_ct"] += ct
                if ct == CAMPAIGN_SIZE:
                    ctx["le2_225_ct"] += ct
            tape[tk].append((ts, yes_c, no_c, side, ct))
    for tk in tape:
        tape[tk].sort()  # gotcha: tape is arrival-ordered, sort per ticker
    log(f"trades: {ctx['lines']:,} lines, kept {ctx['kept']:,} (<=15c on a side) "
        f"across {len(tape):,} tickers; tape-wide <=2c taker volume "
        f"{ctx['le2_taker_ct']:,.0f} ct (dust {ctx['le2_dust_ct']:,.2f}, "
        f"225-class {ctx['le2_225_ct']:,.0f})")
    return tape, ctx


# ------------------------------------------------------------------- signal

def _at(tl, t):
    """last (ts, px) at-or-before t with staleness guard; None if stale/absent."""
    import bisect
    secs, px = tl
    i = bisect.bisect_right(secs, t) - 1
    if i < 0 or (t - secs[i]) > STALE_S:
        return None
    return px[i]


def _rv(tl, t, _cache={}):
    key = (id(tl), int(t // VOL_STEP_S))
    if key in _cache:
        return _cache[key]
    samples = []
    k = int(VOL_WIN_S // VOL_STEP_S)
    import bisect
    secs, px = tl
    ok = True
    for j in range(k + 1):
        tt = t - j * VOL_STEP_S
        i = bisect.bisect_right(secs, tt) - 1
        if i < 0 or (tt - secs[i]) > STALE_S:
            ok = False
            break
        samples.append(px[i])
    rv = None
    if ok:
        samples.reverse()
        rets = [math.log(samples[i] / samples[i - 1]) for i in range(1, len(samples))
                if samples[i - 1] > 0]
        if len(rets) >= 10:
            m = sum(rets) / len(rets)
            var = sum((r - m) ** 2 for r in rets) / (len(rets) - 1)
            rv = math.sqrt(var)
    if len(_cache) > 2_000_000:
        _cache.clear()
    _cache[key] = rv
    return rv


def zscore(tl, t, strike, close):
    """(z, spot) at time t, or (None, reason)."""
    s = _at(tl, t)
    if s is None or s <= 0:
        return None, "spot_stale"
    rv = _rv(tl, t)
    if rv is None or rv <= 0:
        return None, "rv_unavailable"
    tau = close - t
    if tau <= 0:
        return None, "past_close"
    sig = s * rv * math.sqrt(tau / VOL_STEP_S)
    if sig <= 0:
        return None, "rv_unavailable"
    return (s - strike) / sig, s


# --------------------------------------------------------------- evaluation

def weighted_day_ci(df, n=2000, seed=7):
    """Day-bootstrap CI of contract-weighted pooled PnL/ct (companion stat;
    the PRIMARY kill stat is day_bootstrap_ci on per-window rows)."""
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


def evaluate(determined, spot, tape, days):
    dayset = set(days)
    fun = defaultdict(int)
    armA_rows, armA_end_rows, armB_rows, armB_un_rows = [], [], [], []
    armA_qualified = []          # (ticker, asset, doomed, itm) for ITM-rate kill
    armB_cond_prints = 0
    per_price = defaultdict(lambda: [0.0, 0.0])   # price -> [ct, wpnl]
    per_asset = defaultdict(lambda: [0, 0.0, 0.0])  # asset -> [windows, ct, wpnl]

    for tk, d in sorted(determined.items()):
        a = d["asset"]
        fun["windows_determined"] += 1
        if a not in UNIVERSE:
            fun["drop_asset_excluded(BNB)"] += 1
            continue
        if d.get("strike") is None:
            fun["drop_no_strike"] += 1
            continue
        close = close_epoch_from_ticker(tk)
        dkey = datetime.fromtimestamp(close, tz=timezone.utc).date().isoformat()
        if dkey not in dayset:
            fun["drop_outside_sealed_days"] += 1
            continue
        tl = spot.get(a)
        if tl is None:
            fun["drop_no_spot_source"] += 1
            continue
        strike, result = float(d["strike"]), d["result"]
        fun["windows_in_scope"] += 1

        # ---------------- ARM A ----------------
        # continuous gate: arrival = first 15s-grid time |z|>3 in the
        # non-endgame body; prints mirrored only if gate ALSO holds at print ts
        doomed, t_arrival = None, None
        any_signal = False
        g = close - 870.0
        while g <= close - ENDGAME_S:
            z, _s = zscore(tl, g, strike, close)
            if z is not None:
                any_signal = True
                if abs(z) > Z_GATE:
                    doomed, t_arrival = ("yes" if z < 0 else "no"), g
                    break
            g += 15.0
        if doomed is None:
            fun["armA_gate_never_fired" if any_signal else "armA_drop_no_signal"] += 1
        else:
            itm = (result == doomed)
            armA_qualified.append((tk, a, dkey, doomed, itm))
            head, end = [], []
            for ts, yc, nc, side, ct in tape.get(tk, ()):
                if side != doomed:
                    continue
                px = yc if doomed == "yes" else nc
                if px not in ARMA_PRICES:
                    continue
                if ct <= DUST_MAX:
                    fun["armA_excl_dust_prints"] += 1
                    continue
                if ct == CAMPAIGN_SIZE:
                    fun["armA_excl_225_prints"] += 1
                    continue
                if not (t_arrival < ts <= close):
                    continue
                zp, _r = zscore(tl, ts, strike, close)
                gate_holds = (zp is not None and abs(zp) > Z_GATE
                              and ("yes" if zp < 0 else "no") == doomed)
                if not gate_holds:
                    fun["armA_print_gate_lapsed"] += 1
                    continue
                if ts <= (close - ENDGAME_S):
                    head.append((px, ct))
                else:
                    end.append((px, ct))
            for bucket, rows in ((head, armA_rows), (end, armA_end_rows)):
                if not bucket:
                    continue
                ctsum = sum(c for _, c in bucket)
                pnl = sum(c * (p - (100.0 if itm else 0.0)) for p, c in bucket)
                rows.append({"day": dkey, "asset": a, "ticker": tk,
                             "pnl_per_ct": pnl / ctsum,
                             "w": PARTICIPATION * ctsum,
                             "wpnl": PARTICIPATION * pnl})
                if rows is armA_rows:
                    per_asset[a][0] += 1
                    per_asset[a][1] += PARTICIPATION * ctsum
                    per_asset[a][2] += PARTICIPATION * pnl
                    for p, c in bucket:
                        per_price[p][0] += PARTICIPATION * c
                        per_price[p][1] += PARTICIPATION * c * (p - (100.0 if itm else 0.0))

        # ---------------- ARM B ----------------
        cond, uncond = [], []
        for ts, yc, nc, side, ct in tape.get(tk, ()):
            if not (close - ARRIVAL_OFF_S) <= ts <= (close - ARMB_END_OFF_S):
                continue
            px = yc if side == "yes" else nc
            if not (ARMB_LO <= px <= ARMB_HI):
                continue
            if ct <= DUST_MAX or ct == CAMPAIGN_SIZE:
                fun["armB_excl_dust_or_225"] += 1
                continue
            zb, _r = zscore(tl, ts, strike, close)
            if zb is None:
                fun["armB_drop_print_no_signal"] += 1
                continue
            p_side = _norm_cdf(zb) if side == "yes" else 1.0 - _norm_cdf(zb)
            pnl = px - (100.0 if result == side else 0.0)
            uncond.append((ct, pnl))
            if p_side <= px / 200.0:
                cond.append((ct, pnl))
        if uncond:
            cs = sum(c for c, _ in uncond)
            ps = sum(c * p for c, p in uncond)
            armB_un_rows.append({"day": dkey, "asset": a, "pnl_per_ct": ps / cs,
                                 "w": PARTICIPATION * cs, "wpnl": PARTICIPATION * ps})
        if cond:
            cs = sum(c for c, _ in cond)
            ps = sum(c * p for c, p in cond)
            armB_cond_prints += len(cond)
            armB_rows.append({"day": dkey, "asset": a, "pnl_per_ct": ps / cs,
                              "w": PARTICIPATION * cs, "wpnl": PARTICIPATION * ps})

    return (fun, armA_rows, armA_end_rows, armA_qualified,
            armB_rows, armB_un_rows, armB_cond_prints, per_price, per_asset)


# ------------------------------------------------------- frames anchor pass

def frames_anchor(frames_days, targets):
    """Ask-level realism: at each mirrored print, did a reliable book exist and
    was its prevailing same-side ask <= print price? targets:
    {ticker: (close, [(ts, side, px_c), ...])} restricted to frames_days."""
    res = {"prints": 0, "book_avail": 0, "at_or_inside": 0, "asks": []}
    by_day = defaultdict(dict)
    for tk, v in targets.items():
        close = v[0]
        dkey = datetime.fromtimestamp(close, tz=timezone.utc).date().isoformat()
        if dkey in frames_days:
            by_day[dkey][tk] = v
    for d, tks in by_day.items():
        path = os.path.join(CR, "frames", f"day={d}.jsonl.zst")
        if not os.path.exists(path):
            log(f"frames anchor: missing {path}, skipping {d}")
            continue
        names = {tk.encode() for tk in tks}
        bufs = defaultdict(list)
        tk_pat = b'market_ticker\\":\\"'
        for line in _pipe(f"zstd -dc '{path}'"):
            i = line.find(tk_pat)
            if i < 0:
                continue
            j = line.find(b'\\"', i + len(tk_pat))
            if line[i + len(tk_pat):j] not in names:
                continue
            try:
                env = json.loads(line)
                inner = json.loads(env["_raw"])
                tk = inner["msg"]["market_ticker"]
            except (ValueError, KeyError):
                continue
            if tk not in tks:
                continue
            ts = _epoch(env["_wire_recv_ts"])
            close = tks[tk][0]
            if close - 900 <= ts <= close:
                bufs[tk].append((ts, inner))
        for tk, (close, prints) in tks.items():
            tl = reliable_nbbo_timeline(sorted(bufs.get(tk, []), key=lambda x: x[0]))
            for pts, side, px in prints:
                res["prints"] += 1
                bid = ask = None
                for fts, b, k in tl:
                    if fts > pts:
                        break
                    bid, ask = b, k
                side_ask = ask if side == "yes" else (100.0 - bid if bid is not None else None)
                if side_ask is not None:
                    res["book_avail"] += 1
                    res["asks"].append(side_ask)
                    if px >= side_ask:
                        res["at_or_inside"] += 1
    return res


# -------------------------------------------------------------------- main

def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", default=None, help="comma list; default = all sealed days")
    ap.add_argument("--frames-days", default="", help="comma list (<=3) for ask anchoring")
    ap.add_argument("--no-cache", action="store_true")
    a = ap.parse_args(argv)

    t0 = time.time()
    days = a.days.split(",") if a.days else sealed_days()
    fdays = [d for d in a.frames_days.split(",") if d][:3]
    log(f"sealed days used: {days}")

    os.makedirs(CACHE, exist_ok=True)
    def cached(name, fn, day_keyed=True):
        sfx = f"_{days[0]}_{days[-1]}_{len(days)}" if day_keyed else "_all"
        p = os.path.join(CACHE, f"{name}{sfx}.pkl")
        if not a.no_cache and os.path.exists(p):
            with open(p, "rb") as fh:
                return pickle.load(fh)
        v = fn()
        with open(p, "wb") as fh:
            pickle.dump(v, fh)
        return v

    determined = cached("determined",
                        lambda: load_determined(os.path.join(CR, "lifecycle"), ASSETS),
                        day_keyed=False)  # lifecycle scan is whole-dir regardless of --days
    log(f"lifecycle: {len(determined)} determined windows ({time.time()-t0:.0f}s)")
    spot = cached("spot", lambda: load_spot_fast(days))
    hype = cached("hype", lambda: load_hype_bitstamp(days))
    if hype:
        spot["HYPE"] = hype
    log(f"signal sources ready ({time.time()-t0:.0f}s): {sorted(spot)}")
    tape, ctx = cached("tape", lambda: load_trades(days))

    eff_days = sum(min(1.0, (ctx["tmax"][d] - ctx["tmin"][d]) / 86400.0)
                   for d in days if d in ctx["tmin"])
    cap_prorated = CAP_RAW * eff_days / 12.0

    (fun, armA, armA_end, armA_q, armB, armB_un, nBprints,
     per_price, per_asset) = evaluate(determined, spot, tape, days)
    log(f"evaluation done ({time.time()-t0:.0f}s)")

    print("\n================ USABLE-DATA FUNNEL ================")
    for k in sorted(fun):
        print(f"  {k:42s} {fun[k]:,}")
    print(f"  effective_days_covered                     {eff_days:.2f} (of 12 pre-registered)")

    # ---------------- ARM A verdict ----------------
    print("\n================ ARM A (tick-floor 1-2c, |z|>3, non-endgame) ================")
    nq = len(armA_q)
    n_itm = sum(1 for *_x, itm in armA_q if itm)
    itm_rate = (n_itm / nq) if nq else float("nan")
    vol = sum(r["w"] for r in armA)
    print(f"qualified windows={nq:,}  doomed-side ITM={n_itm} ({100*itm_rate:.3f}%)  "
          f"windows with mirrored prints={len(armA):,}")
    armA_verdict = "DATA_GAP"
    if armA:
        df = pd.DataFrame(armA)
        mean, lo, hi = day_bootstrap_ci(df, "pnl_per_ct")
        wmean, wlo, whi = weighted_day_ci(df)
        print(f"PRIMARY net PnL/ct (per-window rows, day-bootstrap): mean={mean:+.3f}c  "
              f"CI95=[{lo:+.3f}, {hi:+.3f}]  days={df['day'].nunique()}")
        print(f"companion contract-weighted PnL/ct: {wmean:+.3f}c CI95=[{wlo:+.3f}, {whi:+.3f}]")
        print(f"mirrored volume @10% participation = {vol:,.0f} ct "
              f"(raw bar {CAP_RAW:,.0f}/12d; prorated bar {cap_prorated:,.0f}/{eff_days:.2f}d)")
        for p in sorted(per_price):
            c, wp = per_price[p]
            print(f"  {p}c bucket: {c:,.0f} ct mirrored, PnL/ct={wp/max(c,1e-9):+.3f}c")
        print("  per-asset:", {k: f"n={v[0]},ct={v[1]:,.0f},pnl/ct={v[2]/max(v[1],1e-9):+.2f}c"
                               for k, v in sorted(per_asset.items())})
        kill_ci = lo <= 0
        kill_itm = nq > 0 and itm_rate >= 0.008
        kill_cap_raw = vol < CAP_RAW
        kill_cap_pro = vol < cap_prorated
        print(f"KILL CHECK: CI_LB<=0 -> {'DEAD' if kill_ci else 'pass'} | "
              f"ITM>=0.8% -> {'DEAD' if kill_itm else 'pass'} | "
              f"capacity raw -> {'CAPACITY-DEAD' if kill_cap_raw else 'pass'} "
              f"(prorated -> {'CAPACITY-DEAD' if kill_cap_pro else 'pass'})")
        if kill_ci or kill_itm:
            armA_verdict = "DEAD"
        elif kill_cap_pro:
            armA_verdict = "CAPACITY-DEAD"
        elif kill_cap_raw:
            armA_verdict = "ALIVE (prorated capacity; raw 12d bar unmet — day shortfall)"
        else:
            armA_verdict = "ALIVE"
    else:
        print("no mirrored Arm A prints — cannot test")
    print(f"ARM A VERDICT: {armA_verdict}")
    if armA_end:
        dfe = pd.DataFrame(armA_end)
        em, el, eh = day_bootstrap_ci(dfe, "pnl_per_ct")
        print(f"[separate, NOT headline] endgame <120s: windows={len(dfe)} "
              f"ct={dfe['w'].sum():,.0f} PnL/ct mean={em:+.3f}c CI=[{el:+.3f},{eh:+.3f}]")

    # ---------------- ARM B verdict ----------------
    print("\n================ ARM B (deep-OTM 4-15c, p_normal<=ask/2, T-12..T-3) ================")
    armB_verdict = "DATA_GAP"
    if armB:
        df = pd.DataFrame(armB)
        dfu = pd.DataFrame(armB_un)
        mean, lo, hi = day_bootstrap_ci(df, "pnl_per_ct")
        umean, ulo, uhi = day_bootstrap_ci(dfu, "pnl_per_ct")
        ndays = df["day"].nunique()
        gap = mean - umean
        print(f"conditioned: windows={len(df):,} prints={nBprints:,} days={ndays} "
              f"PnL/ct={mean:+.3f}c CI95=[{lo:+.3f}, {hi:+.3f}]")
        print(f"unconditioned same-band benchmark: windows={len(dfu):,} "
              f"PnL/ct={umean:+.3f}c CI95=[{ulo:+.3f}, {uhi:+.3f}]")
        print(f"conditioning gap = {gap:+.3f}c/ct (bar >= +1.0)")
        legs = {"CI_LB>0": lo > 0, ">=10 days": ndays >= 10,
                ">=200 cond prints": nBprints >= 200, "gap>=1.0c": gap >= 1.0}
        print("KILL CHECK:", " | ".join(f"{k} -> {'pass' if v else 'FAIL'}" for k, v in legs.items()))
        if not legs["CI_LB>0"] or not legs["gap>=1.0c"] or not legs[">=200 cond prints"]:
            armB_verdict = "DEAD"
        elif not legs[">=10 days"]:
            armB_verdict = "INCONCLUSIVE (all behavioral legs pass; >=10-day leg data-gated)"
        else:
            armB_verdict = "ALIVE"
    else:
        print("no conditioned Arm B prints — cannot test")
    print(f"ARM B VERDICT: {armB_verdict}")

    # ---------------- frames anchoring ----------------
    if fdays:
        targets = {}
        # rebuild per-ticker mirrored Arm A print lists for the anchor days
        tks = {r["ticker"] for r in armA if r["day"] in fdays}
        for tk in tks:
            close = close_epoch_from_ticker(tk)
            d = None
            prints = []
            for ts, yc, nc, side, ct in tape.get(tk, ()):
                px = yc if side == "yes" else nc
                if px in ARMA_PRICES and (close - ARRIVAL_OFF_S) < ts <= (close - ENDGAME_S) \
                        and ct > DUST_MAX and ct != CAMPAIGN_SIZE:
                    prints.append((ts, side, px))
            if prints:
                targets[tk] = (close, prints)
        log(f"frames anchor pass on {fdays}: {len(targets)} tickers")
        res = frames_anchor(set(fdays), targets)
        if res["prints"]:
            pa = 100.0 * res["book_avail"] / res["prints"]
            pi = (100.0 * res["at_or_inside"] / res["book_avail"]) if res["book_avail"] else float("nan")
            med = float(np.median(res["asks"])) if res["asks"] else float("nan")
            print(f"\n[frames anchor] Arm A mirrored prints checked={res['prints']:,}; "
                  f"reliable book available={pa:.1f}%; print >= prevailing ask "
                  f"(restable level)={pi:.1f}%; median prevailing ask={med:.1f}c")
            print("[frames anchor] queue-position depth NOT modeled here; the 10% "
                  "participation cap is the queue-honesty device (pre-registered).")
        else:
            print("\n[frames anchor] no mirrored prints on requested frames days")

    print(f"\n[lottery] total runtime {time.time()-t0:.0f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
