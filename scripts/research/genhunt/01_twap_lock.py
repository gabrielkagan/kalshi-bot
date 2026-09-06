"""Terminal TWAP-Lock Repricing (merged: + RTI-basis endgame) — pre-registered evaluator.

PRE-REGISTRATION (stated BEFORE looking at results; spec is the contract):
  WHO PAYS US: spot-vs-strike anchored endgame traders (incl. informed-DIRECTION
    sweepers who price MAGNITUDE with the wrong state variable) and single-venue
    chart watchers. Settlement is the simple average of the 60 one-second BRTI
    prints before close, so from close-120s the remaining variance of the AVERAGE
    shrinks ~(T/60)^3 — far faster than the sqrt-time intuition the counterparty
    quotes off. They keep quoting 90-96c on a side that is already TWAP-locked.
  SIGNAL: at t in [close-90s, close-10s], synthetic 1s RTI ->
    p_twap = P(60s-avg > strike | realized partial average,
               Var = rv_5s-derived per-second var * sum of squared remaining
               increment weights).
  ENTRY (lock arm, headline): taker, when (p_twap>0.95 and buy YES) or
    (p_twap<0.05 and buy NO) AND model-vs-price edge > _fee_frac(entry) + 0.03.
    One trade per window max. 15-85c jump-damped arm reported SEPARATELY.
  EXIT: settlement only.  FEES: taker 7*p*(1-p)c at entry price.
  SUB-CLAIM A (run FIRST): freq of windows with p_twap>0.99 computable at
    close-45s while best YES ask <= 96c, on the 3 frames days; < 1.5/day -> DEAD.
  SANITY KILL: realized settle rate of the locked side (p_twap>0.99 or <0.01)
    < 0.985 -> TWAP/variance model wrong -> DEAD pending model fix (no retune).
  FULL KILL: n>=50 qualifying lock-arm windows on the 3-day frames pass AND
    day-bootstrap net-PnL CI lower bound <= 0 -> DEAD.
  BASIS PREMISE CHECK (does not kill the lock arm): |coinbase_mid -
    consensus_mid| at T-90s > 0.5*strike-distance in >=5% of near-strike windows
    (near-strike := reliable YES NBBO mid in [10c, 90c] at T-90s).

ENDGAME REGIME LABEL: every result below is settlement-endgame (<120s to close),
informed-flow territory by construction (gotcha 4) — reported as such.

HONEST-SUBSET DEVIATIONS (declared up front, runtime budget ~25 min):
  * Synthetic RTI = equal-weight mean of Bitstamp L2 best-mid (stateless full
    snapshots) + Coinbase ticker last — NOT Kraken+Bitstamp+Gemini(+Coinbase).
    Kraken (~128M lines/day) and Gemini (~40M lines/day) need full-day L2 replay
    for snapshot anchoring and are infeasible in-budget; Bitstamp is the one
    venue whose every frame is a complete book, Coinbase ships a ready ticker.
  * Assets: BTC/ETH/SOL/XRP (2-input index) + DOGE (Coinbase-only index,
    excluded from the basis check). HYPE/BNB/ADA/BCH untested (no affordable
    index input). Sub-claim A frequency is therefore an UNDERCOUNT of the full
    cross-asset opportunity; treated in the verdict logic.
  * Non-frames-day trades-mid lock-frequency proxy SKIPPED (needs venue replay
    on 12 days). 3 sealed frames days only.

Usage:
  python3 scripts/research/genhunt/01_twap_lock.py [--days 2026-06-03,2026-06-04,2026-06-05]
      [--smoke-hours H]
"""
from __future__ import annotations

import argparse
import bisect
import glob
import json
import math
import os
import re
import subprocess
import sys
import time
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor
from datetime import date, datetime, timedelta, timezone

_REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)

from scripts.research.early_exit_backtest import reliable_nbbo_timeline  # noqa: E402
from scripts.research.fairvalue_extract import (  # noqa: E402
    _realized_vol, load_spot,
)
from scripts.research.fairvalue_model import _fee_frac, day_bootstrap_ci  # noqa: E402
from scripts.research.phase1b_real_price_economics import (  # noqa: E402
    _epoch, close_epoch_from_ticker, load_determined,
)
from scripts.research.settlement_convergence_p1a import (  # noqa: E402
    kalshi_fee_per_contract_cents,
)
from scripts.research.zstd_stream import run_zstd_checked  # noqa: E402  (repo root on sys.path above)

CR = os.path.expanduser("~/kalshi-research-data/fairvalue")
ALL_ASSETS = ("BTC", "ETH", "SOL", "XRP", "HYPE", "DOGE", "BNB", "ADA", "BCH")
# tracked = assets with an affordable index input (see module docstring)
TRACKED = ("BTC", "ETH", "SOL", "XRP", "DOGE")
BS_SYMBOL = {"BTC": "btcusd", "ETH": "ethusd", "SOL": "solusd", "XRP": "xrpusd"}
TWO_INPUT = tuple(BS_SYMBOL)          # assets with bitstamp + coinbase

N_PRINTS = 60                          # settlement = avg of 60 1s prints before close
DEC_FROM, DEC_TO, DEC_STEP = 90, 10, 5  # decisions at close-90 .. close-10, 5s grid
STALE_S = 30.0                         # max staleness for an index input / NBBO
REGION_S = 200                         # load venue data for [Q-200s, Q] per quarter-hour
MIN_PARTIAL_COVER = 0.9                # realized-print coverage required
EDGE_OVER_FEE = 0.03                   # pre-registered entry threshold
LOCK_LO, LOCK_HI = 0.05, 0.95          # lock-arm gates
JUMP_LO, JUMP_HI = 0.15, 0.85          # jump-damped arm gates
SANITY_P = 0.99                        # locked-state definition for sanity kill

_TICKER_RE = re.compile(rb'KX(?:BTC|ETH|SOL|XRP|DOGE)15M-[^\\"]+')


# ---------------------------------------------------------------- bitstamp ---

def _chunk_span(path: str):
    b = os.path.basename(path)
    try:
        s, rest = b.split("_to_", 1)
        e = rest.split("_seq", 1)[0]
        f = "%Y%m%dT%H%M%SZ"
        return (datetime.strptime(s, f).replace(tzinfo=timezone.utc).timestamp(),
                datetime.strptime(e, f).replace(tzinfo=timezone.utc).timestamp())
    except (ValueError, IndexError):
        return None


def load_bitstamp_region_mids(day: str) -> dict:
    """{asset: sorted [(epoch, mid)]} from full-snapshot frames whose recv ts
    falls inside [Q-REGION_S, Q] for any quarter-hour Q. Stateless per frame
    (every Bitstamp order_book message is a complete top-100 book), so skipping
    out-of-region frames is exact, not an approximation. Crossed books dropped
    (known dust artifact, see venue_book_reconstruct docstring)."""
    y, m, d = day.split("-")
    root = f"{CR}/venue_pull/bitstamp_ws/year={y}/month={m}/day={d}"
    chan2asset = {f'order_book_{sym}': a for a, sym in BS_SYMBOL.items()}
    out: dict = defaultdict(list)
    files = sorted(glob.glob(f"{root}/**/*.zst", recursive=True))
    kept = 0
    for f in files:
        span = _chunk_span(f)
        if span is not None:
            s, e = span
            q1 = math.ceil(s / 900.0) * 900.0
            if q1 > e + REGION_S:
                continue
        kept += 1
        raw = run_zstd_checked(f).encode()  # ticket 86bbvrx1t: exit code was discarded
        for line in raw.splitlines():
            if not line:
                continue
            j = line.find(b'"', 18)
            if line[:18] != b'{"_wire_recv_ts":"' or j < 0:
                continue
            try:
                ts = _epoch(line[18:j].decode())
            except ValueError:
                continue
            if ts % 900.0 < 900.0 - REGION_S:
                continue
            try:
                env = json.loads(line)
                inner = json.loads(env["_raw"])
            except (ValueError, KeyError):
                continue
            a = chan2asset.get(inner.get("channel", ""))
            if a is None:
                continue
            data = inner.get("data", {})
            bids, asks = data.get("bids") or [], data.get("asks") or []
            if not bids or not asks:
                continue
            bb, ba = float(bids[0][0]), float(asks[0][0])
            if bb <= 0 or ba <= 0 or bb >= ba:   # crossed/degenerate -> drop
                continue
            out[a].append((ts, (bb + ba) / 2.0))
    for a in out:
        out[a].sort(key=lambda x: x[0])
    print(f"  [bitstamp {day}] {kept}/{len(files)} chunks in-region, "
          f"mids: " + ", ".join(f"{a}={len(v)}" for a, v in sorted(out.items())))
    return out


# ---------------------------------------------------------------- index ------

class RtiIndex:
    """Equal-weight synthetic index from the available per-asset inputs."""

    def __init__(self, bs_mids: dict, cb_spot: dict):
        self.bs = {a: ([t for t, _ in v], [p for _, p in v])
                   for a, v in bs_mids.items()}
        self.cb = cb_spot      # {asset: (secs, px)} from load_spot

    def _last_fresh(self, secs, px, t):
        i = bisect.bisect_right(secs, t) - 1
        if i >= 0 and t - secs[i] <= STALE_S:
            return px[i]
        return None

    def at(self, asset: str, t: float):
        """(index_value | None, n_inputs) using strictly data with ts <= t."""
        vals = []
        tl = self.bs.get(asset)
        if tl:
            v = self._last_fresh(tl[0], tl[1], t)
            if v is not None:
                vals.append(v)
        tl = self.cb.get(asset)
        if tl:
            v = self._last_fresh(tl[0], tl[1], t)
            if v is not None:
                vals.append(v)
        if not vals:
            return None, 0
        return sum(vals) / len(vals), len(vals)

    def coinbase_at(self, asset: str, t: float):
        tl = self.cb.get(asset)
        return self._last_fresh(tl[0], tl[1], t) if tl else None


def remaining_weight_sq_sum(t: int, close: int) -> float:
    """Sum over future 1s increments u in (t, close-1] of w_u^2, where
    w_u = (#prints at seconds >= u)/60 and prints sit at close-60..close-1.
    Increments before the window start carry full weight 1 (every print
    inherits them) -> the pre-registered ~(T/60)^3 shrinkage inside the window."""
    s = 0.0
    first_print = close - N_PRINTS
    for u in range(t + 1, close):       # increment over (u-1, u]
        n_ge = (close - 1) - max(u, first_print) + 1
        if n_ge <= 0:
            continue
        w = min(1.0, n_ge / float(N_PRINTS))
        s += w * w
    return s


def _norm_cdf(z: float) -> float:
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


# ---------------------------------------------------------------- evaluator --

class Funnel(dict):
    def hit(self, k, n=1):
        self[k] = self.get(k, 0) + n


def evaluate_window(tk, det, frames, idx: RtiIndex, fun: Funnel, day: str,
                    trades_lock: list, trades_jump: list, sanity: list,
                    subclaim: list, basis: list):
    asset = det["asset"]
    close = int(round(close_epoch_from_ticker(tk)))
    strike = det.get("strike")
    result = det["result"]
    if strike is None:
        fun.hit("no_strike")
        return
    frames.sort(key=lambda x: x[0])
    tl = reliable_nbbo_timeline(frames)
    if not tl:
        fun.hit("no_reliable_book")
        return
    cb_tl = idx.cb.get(asset)

    # walk pointers over decisions (ascending)
    ti = 0
    first_print = close - N_PRINTS
    locked_yes = locked_no = False
    lock_done = jump_done = False
    any_decision = False
    any_rti = False
    won = 1.0 if result == "yes" else 0.0

    for t in range(close - DEC_FROM, close - DEC_TO + 1, DEC_STEP):
        # --- reliable NBBO at t (last reliable point <= t, fresh) ---
        while ti < len(tl) and tl[ti][0] <= t:
            ti += 1
        bid = ask = None
        q_age = None
        if ti > 0 and t - tl[ti - 1][0] <= STALE_S:
            bid, ask = tl[ti - 1][1], tl[ti - 1][2]
            q_age = t - tl[ti - 1][0]

        # --- model state ---
        rti, n_in = idx.at(asset, t)
        if rti is None:
            continue
        any_rti = True
        rv5 = _realized_vol(cb_tl, t) if cb_tl else None
        if rv5 is None or rv5 <= 0:
            fun.hit("dp_no_vol")
            continue
        realized = []
        need = [s for s in range(first_print, close) if s <= t]
        for s in need:
            v, _ = idx.at(asset, s)
            if v is not None:
                realized.append(v)
        if need and len(realized) < MIN_PARTIAL_COVER * len(need):
            fun.hit("dp_partial_gap")
            continue
        k = len(realized)
        e_avg = (sum(realized) + (N_PRINTS - k) * rti) / float(N_PRINTS)
        sigma_1s = rti * rv5 / math.sqrt(5.0)          # $ per sqrt-second
        var = sigma_1s * sigma_1s * remaining_weight_sq_sum(t, close)
        sd = math.sqrt(var) if var > 0 else 0.0
        if sd <= 0:
            p_twap = 1.0 if e_avg > strike else 0.0
        else:
            p_twap = _norm_cdf((e_avg - strike) / sd)
        any_decision = True

        # --- basis premise check at T-90s exactly (2-input assets only) ---
        if t == close - DEC_FROM and asset in TWO_INPUT and n_in == 2:
            cbv = idx.coinbase_at(asset, t)
            if (cbv is not None and bid is not None and ask is not None
                    and 10.0 <= (bid + ask) / 2.0 <= 90.0):
                sd_strike = abs(rti - strike)
                basis.append((day, asset, abs(cbv - rti), sd_strike))

        # --- sanity-kill bookkeeping (locked states) ---
        if p_twap > SANITY_P and not locked_yes:
            locked_yes = True
            sanity.append((day, asset, "yes", won))
        if p_twap < 1.0 - SANITY_P and not locked_no:
            locked_no = True
            sanity.append((day, asset, "no", 1.0 - won))

        # --- sub-claim A at close-45s exactly ---
        if t == close - 45:
            if p_twap > 0.99 and ask is not None and 0 < ask <= 96:
                subclaim.append((day, asset, "yes", float(ask)))
            # symmetric NO side reported as context (not the registered stat)
            if p_twap < 0.01 and bid is not None and (100 - bid) <= 96 and bid < 100:
                subclaim.append((day, asset, "no_ctx", float(100 - bid)))

        # --- lock arm entry (headline; one per window) ---
        if not lock_done:
            if p_twap > LOCK_HI and ask is not None and 0 < ask < 100:
                edge = p_twap - ask / 100.0
                if edge > float(_fee_frac(ask)) + EDGE_OVER_FEE:
                    fee = kalshi_fee_per_contract_cents(ask)
                    pnl = (100.0 - ask - fee) if won else (-ask - fee)
                    trades_lock.append(dict(day=day, ticker=tk, asset=asset, side="yes",
                                            t_to_close=close - t, px=float(ask),
                                            p_twap=p_twap, pnl=pnl, q_age=q_age))
                    lock_done = True
            elif p_twap < LOCK_LO and bid is not None and 0 < bid < 100:
                c = 100.0 - bid
                edge = (1.0 - p_twap) - c / 100.0
                if edge > float(_fee_frac(c)) + EDGE_OVER_FEE:
                    fee = kalshi_fee_per_contract_cents(c)
                    pnl = (100.0 - c - fee) if not won else (-c - fee)
                    trades_lock.append(dict(day=day, ticker=tk, asset=asset, side="no",
                                            t_to_close=close - t, px=c,
                                            p_twap=p_twap, pnl=pnl, q_age=q_age))
                    lock_done = True

        # --- jump-damped arm (15-85c band; separate report; one per window) ---
        if not jump_done and JUMP_LO <= p_twap <= JUMP_HI:
            if ask is not None and 0 < ask < 100 and \
                    p_twap - ask / 100.0 > float(_fee_frac(ask)) + EDGE_OVER_FEE:
                fee = kalshi_fee_per_contract_cents(ask)
                pnl = (100.0 - ask - fee) if won else (-ask - fee)
                trades_jump.append(dict(day=day, ticker=tk, asset=asset, side="yes",
                                        px=float(ask), p_twap=p_twap, pnl=pnl))
                jump_done = True
            elif bid is not None and 0 < bid < 100:
                c = 100.0 - bid
                if (1.0 - p_twap) - c / 100.0 > float(_fee_frac(c)) + EDGE_OVER_FEE:
                    fee = kalshi_fee_per_contract_cents(c)
                    pnl = (100.0 - c - fee) if not won else (-c - fee)
                    trades_jump.append(dict(day=day, ticker=tk, asset=asset, side="no",
                                            px=c, p_twap=p_twap, pnl=pnl))
                    jump_done = True

    if not any_rti:
        fun.hit("no_rti_coverage")
    elif not any_decision:
        fun.hit("no_valid_decision_pt")
    else:
        fun.hit("evaluated")


# ---------------------------------------------------------------- streaming --

def stream_frames_day(day: str, determined: dict, idx: RtiIndex, fun: Funnel,
                      sinks: tuple, smoke_hours: float = 0.0):
    """One memory-bounded pass over the day's crypto-prefiltered Kalshi bronze
    (zstd -dc pipe). Buffers raw frames per open tracked window; finalizes when
    the stream clock passes close+60s (pattern: path_alpha_probe.stream_windows)."""
    path = f"{CR}/frames/day={day}.jsonl.zst"
    day_start = datetime.fromisoformat(day + "T00:00:00+00:00").timestamp()
    trades_lock, trades_jump, sanity, subclaim, basis = sinks
    buffers: dict = defaultdict(list)
    done: set = set()
    closes: dict = {}
    n_lines = n_kept = n_final = 0
    stream_ts = 0.0

    def _finalize(tk):
        nonlocal n_final
        evaluate_window(tk, determined[tk], buffers.pop(tk), idx, fun, day,
                        trades_lock, trades_jump, sanity, subclaim, basis)
        n_final += 1
        done.add(tk)

    proc = subprocess.Popen(["zstd", "-dc", path], stdout=subprocess.PIPE,
                            bufsize=1 << 22)
    _zstd_exhausted = False  # ticket 86bbvrx1t: set ONLY on a true EOF
    try:
        for line in proc.stdout:
            n_lines += 1
            mt = _TICKER_RE.search(line)
            tk = mt.group(0).decode() if mt else None
            if n_lines % 50000 == 0:
                jj = line.find(b'"', 18)
                if jj > 18:
                    try:
                        stream_ts = max(stream_ts, _epoch(line[18:jj].decode()))
                    except ValueError:
                        pass
                for t in [t for t in buffers if closes.get(t, 1e18) + 60 < stream_ts]:
                    _finalize(t)
                if smoke_hours and stream_ts > day_start + smoke_hours * 3600:
                    break
            if tk is None or tk in done or tk not in determined:
                continue
            try:
                env = json.loads(line)
                inner = json.loads(env["_raw"])
                ts = _epoch(env["_wire_recv_ts"])
            except (ValueError, KeyError):
                continue
            if tk not in closes:
                closes[tk] = close_epoch_from_ticker(tk)
            buffers[tk].append((ts, inner))
            n_kept += 1
        else:
            # for/else: reached ONLY on a true EOF, never after the
            # smoke_hours break above.
            _zstd_exhausted = True
    finally:
        proc.stdout.close()
        proc.wait()
        # ticket 86bbvrx1t: the loop above CAN break early (smoke_hours), which
        # kills zstd with SIGPIPE legitimately — so only a for/else-confirmed
        # EOF is allowed to be treated as a truncation.
        assert_zstd_ok(proc, path, exhausted=_zstd_exhausted,
                       require_nonempty=False)
    for t in list(buffers):
        _finalize(t)
    print(f"  [frames {day}] lines={n_lines:,} kept={n_kept:,} windows_finalized={n_final}")
    return n_final


# ---------------------------------------------------------------- main -------

def process_day(day: str, det_day: dict, smoke_hours: float):
    """Per-day worker (runs in its own process; days are independent)."""
    fun = Funnel()
    sinks = ([], [], [], [], [])     # trades_lock, trades_jump, sanity, subclaim, basis
    bs = load_bitstamp_region_mids(day)
    y, m, dd = day.split("-")
    cb = load_spot(f"{CR}/coinbase_ticker/year={y}/month={m}/day={dd}")
    print(f"  [coinbase {day}] assets: "
          + ", ".join(f"{k}={len(v[0])}" for k, v in sorted(cb.items())))
    idx = RtiIndex(bs, cb)
    stream_frames_day(day, det_day, idx, fun, sinks, smoke_hours=smoke_hours)
    return dict(fun), sinks


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", default="2026-06-03,2026-06-04,2026-06-05")
    ap.add_argument("--smoke-hours", type=float, default=0.0,
                    help="process only the first H hours of each frames day")
    a = ap.parse_args(argv)
    days = a.days.split(",")
    for d in days:
        if not os.path.exists(f"{CR}/.done_{d}"):
            print(f"FATAL: day {d} has no .done marker — not sealed.")
            return 2

    t0 = time.time()
    print(f"=== Terminal TWAP-Lock Repricing — ENDGAME regime, days={days} ===")
    print("    (deviations: 2-input synthetic index Bitstamp+Coinbase; "
          "assets BTC/ETH/SOL/XRP + DOGE(cb-only); see docstring)")

    # lifecycle (day partitions + 1d for midnight straddlers)
    want_dirs = []
    for d in days:
        dt = date.fromisoformat(d)
        for dd in (dt, dt + timedelta(days=1)):
            p = f"{CR}/lifecycle/year={dd.year:04d}/month={dd.month:02d}/day={dd.day:02d}"
            if os.path.isdir(p) and p not in want_dirs:
                want_dirs.append(p)
    determined_all: dict = {}
    if want_dirs:
        for p in want_dirs:
            determined_all.update(load_determined(p, ALL_ASSETS))
    else:
        determined_all = load_determined(f"{CR}/lifecycle", ALL_ASSETS)
    print(f"  [lifecycle] determined windows loaded: {len(determined_all):,} "
          f"({time.time()-t0:.0f}s)")

    fun = Funnel()
    trades_lock: list = []
    trades_jump: list = []
    sanity: list = []
    subclaim: list = []
    basis: list = []

    det_by_day = {}
    for day in days:
        d0 = datetime.fromisoformat(day + "T00:00:00+00:00").timestamp()
        det_day = {}
        for tk, det in determined_all.items():
            c = close_epoch_from_ticker(tk)
            if d0 < c <= d0 + 86400:
                fun.hit("determined_in_days")
                if det["asset"] in TRACKED:
                    fun.hit("tracked_asset")
                    det_day[tk] = det
        det_by_day[day] = det_day

    with ProcessPoolExecutor(max_workers=min(3, len(days))) as ex:
        futs = {d: ex.submit(process_day, d, det_by_day[d], a.smoke_hours)
                for d in days}
        for d in days:
            f_day, sinks = futs[d].result()
            for k, v in f_day.items():
                fun.hit(k, v)
            trades_lock.extend(sinks[0])
            trades_jump.extend(sinks[1])
            sanity.extend(sinks[2])
            subclaim.extend(sinks[3])
            basis.extend(sinks[4])
            print(f"  [{d}] lock={len(sinks[0])} jump={len(sinks[1])} "
                  f"locked_states={len(sinks[2])} "
                  f"subclaimA={sum(1 for s in sinks[3] if s[2]=='yes')} "
                  f"({time.time()-t0:.0f}s)")

    n_days = len(days)
    print("\n=== USABLE-DATA FUNNEL ===")
    for k in ("determined_in_days", "tracked_asset", "no_strike",
              "no_reliable_book", "no_rti_coverage", "no_valid_decision_pt",
              "evaluated", "dp_no_vol", "dp_partial_gap"):
        print(f"  {k:22s} {fun.get(k, 0)}")

    # ---- SUB-CLAIM A (registered: YES side only) ----
    sc_yes = [s for s in subclaim if s[2] == "yes"]
    sc_no = [s for s in subclaim if s[2] == "no_ctx"]
    rate = len(sc_yes) / n_days
    print(f"\n=== SUB-CLAIM A (close-45s, p_twap>0.99, YES ask<=96c) ===")
    print(f"  n={len(sc_yes)} over {n_days} days -> {rate:.2f}/day "
          f"(kill if < 1.5/day)   [context: symmetric NO side n={len(sc_no)}]")
    subclaim_dead = rate < 1.5

    # ---- SANITY KILL ----
    print("\n=== SANITY KILL (locked-state realized settle rate) ===")
    sanity_ok = None
    if sanity:
        hit = sum(s[3] for s in sanity)
        sr = hit / len(sanity)
        ny = sum(1 for s in sanity if s[2] == "yes")
        print(f"  locked windows n={len(sanity)} (yes-locked={ny}, no-locked={len(sanity)-ny})")
        print(f"  locked-side settle rate = {sr:.4f}  (kill if < 0.985)")
        sanity_ok = sr >= 0.985
    else:
        print("  no locked states observed — sanity untestable")

    # ---- LOCK ARM PnL ----
    print("\n=== LOCK ARM (headline; taker, hold to settle) ===")
    ci = None
    if trades_lock:
        import pandas as pd
        df = pd.DataFrame(trades_lock)
        out_csv = "/tmp/genhunt01_lock_trades.csv"
        df.to_csv(out_csv, index=False)
        print(f"  [dump] lock-arm trades -> {out_csv}")
        mean, lo, hi = day_bootstrap_ci(df, "pnl")
        ci = (mean, lo, hi)
        wr = (df["pnl"] > 0).mean()
        print(f"  n={len(df)}  mean net={mean:+.2f}c/ct  total={df['pnl'].sum():+.0f}c  "
              f"WR={100*wr:.1f}%  day-bootstrap CI=[{lo:+.2f}, {hi:+.2f}]c")
        print(df.groupby("asset")["pnl"].agg(["count", "mean", "sum"]).to_string())
        print("  by entry px decile:")
        df["px_band"] = (df["px"] // 10 * 10).astype(int)
        print(df.groupby("px_band")["pnl"].agg(["count", "mean"]).to_string())
        # fill honesty: how stale was the quote we 'lifted'? (endgame books move)
        print(f"  entry quote age: median={df['q_age'].median():.1f}s "
              f"p90={df['q_age'].quantile(0.9):.1f}s")
        fresh = df[df["q_age"] <= 5.0]
        if len(fresh):
            mf, lf, hf = day_bootstrap_ci(fresh, "pnl")
            print(f"  fresh-quote (<=5s) subset: n={len(fresh)} mean={mf:+.2f}c "
                  f"CI=[{lf:+.2f}, {hf:+.2f}]c")
    else:
        print("  no qualifying lock-arm trades")

    # ---- JUMP-DAMPED ARM (separate) ----
    print("\n=== JUMP-DAMPED ARM (15-85c; reported separately) ===")
    if trades_jump:
        import pandas as pd
        dfj = pd.DataFrame(trades_jump)
        mj, lj, hj = day_bootstrap_ci(dfj, "pnl")
        print(f"  n={len(dfj)}  mean net={mj:+.2f}c/ct  total={dfj['pnl'].sum():+.0f}c  "
              f"day-bootstrap CI=[{lj:+.2f}, {hj:+.2f}]c")
    else:
        print("  no qualifying jump-arm trades")

    # ---- BASIS PREMISE CHECK ----
    print("\n=== BASIS PREMISE CHECK (T-90s, near-strike = NBBO mid 10-90c) ===")
    if basis:
        hits = sum(1 for _, _, b, sd in basis if sd > 0 and b > 0.5 * sd)
        pct = 100.0 * hits / len(basis)
        print(f"  near-strike windows n={len(basis)}  |cb-consensus|>0.5*strike-dist: "
              f"{hits} ({pct:.1f}%)  (sub-arm alive if >=5%)")
        print("  NOTE: consensus here is mean(coinbase, bitstamp) -> basis = |cb-bs|/2 "
              "(degraded vs full 4-venue spec)")
    else:
        print("  no near-strike two-input windows at T-90s")

    # ---- VERDICT vs pre-registered criteria ----
    print("\n=== VERDICT (pre-registered) ===")
    if subclaim_dead:
        print(f"  SUB-CLAIM A FAIL: {rate:.2f}/day < 1.5/day -> opportunity too thin -> DEAD")
    if sanity_ok is False:
        print("  SANITY KILL: locked settle rate < 0.985 -> TWAP/variance model wrong "
              "-> DEAD pending model fix (no in-sample retune)")
    if ci is not None:
        n = len(trades_lock)
        if n >= 50 and ci[1] <= 0:
            print(f"  FULL KILL: n={n}>=50 and CI lower {ci[1]:+.2f}c <= 0 -> DEAD")
        elif n >= 50 and ci[1] > 0 and not subclaim_dead and sanity_ok:
            print(f"  PASS: n={n}>=50, CI lower {ci[1]:+.2f}c > 0 -> EDGE CANDIDATE")
        elif n < 50:
            print(f"  n={n} < 50 qualifying windows -> below registered n floor")
    print(f"\n  runtime {time.time()-t0:.0f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
