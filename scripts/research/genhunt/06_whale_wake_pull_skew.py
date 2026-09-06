"""Whale-wake same-side passive provision + pull-and-skew arm (genhunt #06).

MERGED mechanism (flow #19 + #23): same trigger, two maker arms.

PRE-REGISTRATION (stated before any data was read; goalposts frozen):

HYPOTHESIS: after an informed >=200-ct sweep in direction D in a Kalshi
15M-crypto window, the mid bounces against D as mean-reversion faders sell
back, then settlement follows the sweep.

COUNTERPARTY / WHY THEY PAY US:
  Arm W (wake-bid): bounce-sellers — mean-reversion retail fading the sweep,
    the one flow stream previously established to be on the WRONG side. They
    sell the informed direction back to us; we inherit it fee-free as a maker.
  Arm P (pull-and-skew): the time-boxed whale itself (target quantity must
    fill before the window closes — impatience is contractual) plus trailing
    copy-traders, who pay a desperation premium of +4c over pre-print mid.

SIGNAL / ENTRY / EXIT:
  TRIGGER: FIRST print with count_fp >= 200 in a window, with >=120s to close
    (close via close_epoch_from_ticker). Per-ticker ts_ms-sorted tape.
    Endgame (<120s-to-close) first-200ct prints reported separately (gotcha 4),
    never traded.
  STAGE 1 (cheap, trades-only, ALL days, run FIRST):
    P(>=1 same-direction >=100-ct print within 120s | first >=200-ct print).
    If < 0.45 -> Arm P DEAD (no continuation to skew into). Arm W proceeds
    regardless.
  STAGE 2 (frames, <=3 sealed days, default 2026-06-03..06-05):
    Arm W: passive bids on the whale's side at post-sweep reliable-NBBO mid
      -1c and -2c (side-price space), working 90s, fill ONLY on STRICT cross
      by a later real print at a side price strictly below our bid (last in
      queue — a print AT our price does NOT fill us); cancel both levels on
      any opposite-direction >=200-ct print; hold to settlement. ONE
      observation per window (per-contract PnL averaged over filled levels).
    Arm P: on the trigger, the maker PULLS its same-side resting quote
      (modeled as resting at the pre-print best ask on the whale's side, in
      side-price space) and re-quotes at pre-print mid +4c in the whale's
      direction, working 120s. BASELINE comparator simulated in parallel on
      the IDENTICAL campaign: the same passive quote left at the pre-print
      best ask, working 120s. Both fill on a later real print at a side
      price >= the quote (symmetric rule for both arms of the comparison —
      a sweep takes the whole level, and the kill metric is the DIFFERENCE,
      so the rule cancels). Markout = hold-to-settlement, per contract.
      Edge = skew-fill markout minus baseline-passive markout (unfilled
      quote contributes 0).

FEE MATH: maker pays ZERO Kalshi fee (gotcha 1 — the taker's 7*p*(1-p)c goes
  to Kalshi; a maker's mirror of a taker print is GROSS, not net). All PnL
  here is maker-side hold-to-settlement: win -> 100 - price, lose -> -price
  (long side D); short side D: win(D) -> price - 100, lose(D) -> +price.
  No fee term anywhere, by construction.

KILL CRITERIA (numeric, pre-registered):
  Arm W DEAD iff: day-bootstrap (day_bootstrap_ci, resample DAYS never rows)
    CI lower bound of per-contract settlement PnL on strict-cross wake fills
    <= 0, OR n < 30 filled wake-windows, OR fill-rate < 10% of wake events.
  Arm P DEAD iff: stage-1 continuation P < 0.45, OR day-bootstrap CI lower
    bound of (skew markout - baseline markout) per contract <= 0, OR
    n < 30 campaigns pooled. Per-asset breakdown reported if n allows.

DATA: CR/trades (all days, 05-30 partial), CR/frames (<=3 sealed days, via
  `zstd -dc` subprocess pipe + `grep -F` ticker prefilter — never buffered),
  CR/lifecycle via phase1b load_determined. Day exists iff CR/.done_<day>.
NO LOOK-AHEAD: pre-print NBBO = last reliable point STRICTLY before trigger;
  post-sweep NBBO = first reliable point STRICTLY after trigger (<=30s);
  fills only from prints strictly after quote placement.
FUNNEL: every drop stage printed (gotcha 6 — reliable_nbbo refuses
  unanchored/crossed books; filter bias must be visible).

Usage:
  python3 -m scripts.research.genhunt.06_whale_wake_pull_skew \
      [--corpus ~/kalshi-research-data/fairvalue] \
      [--stage2-days 2026-06-03,2026-06-04,2026-06-05] [--smoke]
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import re
import subprocess
import tempfile
from collections import defaultdict
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

from scripts.research.early_exit_backtest import reliable_nbbo_timeline
from scripts.research.fairvalue_model import day_bootstrap_ci
from scripts.research.phase1b_real_price_economics import (
    _epoch,
    _is_crypto_15m,
    close_epoch_from_ticker,
    load_determined,
)
from scripts.research.zstd_stream import assert_zstd_ok  # noqa: E402  (repo root on sys.path above)

ASSETS = ("BTC", "ETH", "SOL", "XRP", "HYPE", "DOGE", "BNB", "ADA", "BCH")

WHALE_CT = 200.0          # trigger size (contracts, count_fp)
CONT_CT = 100.0           # stage-1 continuation size
CONT_WINDOW_S = 120.0     # stage-1 continuation lookout
MIN_TO_CLOSE_S = 120.0    # trigger eligibility
ARM_P_GATE = 0.45         # stage-1 continuation gate for Arm P
WAKE_WORK_S = 90.0        # Arm W working time
WAKE_OFFSETS = (1.0, 2.0)  # cents below post-sweep side mid
SKEW_WORK_S = 120.0       # Arm P working time
SKEW_C = 4.0              # cents above pre-print side mid
POST_NBBO_MAX_S = 30.0    # post-sweep reliable point must appear within this
MIN_N = 30                # pre-registered n floor (both arms)
MIN_FILL_RATE = 0.10      # Arm W capacity floor

_TICKER_RE = re.compile(r'market_ticker\\":\\"(KX[A-Z]+15M-[A-Z0-9-]+)')
# fast field extraction from the ESCAPED inner _raw (avoids double json.loads
# on tens of millions of trade lines; sample line verified 2026-06-10):
#   ...\"market_ticker\":\"KXDOGE15M-26JUN022100-00\",\"yes_price_dollars\":
#   \"0.0790\",...\"count_fp\":\"51.68\",\"taker_side\":\"yes\",...
#   \"ts_ms\":1780448191642...
_COUNT_RE = re.compile(r'count_fp\\":\\"([0-9.]+)')
_SIDE_RE = re.compile(r'\\"taker_side\\":\\"(yes|no)')
_TSMS_RE = re.compile(r'ts_ms\\":(\d+)')
_YESPX_RE = re.compile(r'yes_price_dollars\\":\\"([0-9.]+)')


# ----------------------------------------------------------------------------
# streaming I/O
# ----------------------------------------------------------------------------

def _filter_cmd(grep_file: str) -> List[str]:
    """Multi-pattern fixed-string prefilter. BSD grep -F -f is pathologically
    slow with hundreds of patterns (>25 min per frames day, measured); ripgrep
    is Aho-Corasick. Fall back to LC_ALL=C grep if rg is absent."""
    from shutil import which
    if which("rg"):
        return ["rg", "--no-line-number", "-F", "-f", grep_file]
    return ["grep", "-F", "-f", grep_file]


def _stream_zst(path: str, grep_file: Optional[str] = None):
    """Stream lines of a .jsonl.zst via `zstd -dc` pipe (never buffered).
    Optional fixed-string prefilter keeps Python off cold lines."""
    z = subprocess.Popen(["zstd", "-dc", path], stdout=subprocess.PIPE)
    if grep_file:
        env = dict(os.environ, LC_ALL="C")
        g = subprocess.Popen(_filter_cmd(grep_file),
                             stdin=z.stdout, stdout=subprocess.PIPE, env=env)
        z.stdout.close()
        src = g
    else:
        g = None
        src = z
    _exhausted = False
    try:
        for raw in src.stdout:
            if raw.strip():
                yield raw.decode("utf-8", errors="replace")
        _exhausted = True
    finally:
        src.stdout.close()
        if g is not None:
            g.wait()
        z.wait()
        # ticket 86bbvrx1t: check `z` (the decompressor), never `src`. With a
        # grep spliced on, src.returncode is grep's, and grep legitimately
        # exits 1 on no-matches while a truncated zstd is masked entirely.
        # The flag keeps a caller's early break (SIGPIPE) from reading as a
        # truncation.
        assert_zstd_ok(z, path, exhausted=_exhausted, require_nonempty=False)


def _sealed_days(corpus: str) -> List[str]:
    return sorted(m.split(".done_")[1] for m in glob.glob(f"{corpus}/.done_*")
                  if re.search(r"\.done_\d{4}-\d{2}-\d{2}$", m))


def _trade_days(corpus: str) -> List[str]:
    return sorted(re.search(r"day=(\d{4}-\d{2}-\d{2})", f).group(1)
                  for f in glob.glob(f"{corpus}/trades/day=*.jsonl.zst"))


# ----------------------------------------------------------------------------
# side-price space: D = whale taker_side; side price = price of side D
# ----------------------------------------------------------------------------

def _side_px(yes_px_c: float, d: str) -> float:
    return yes_px_c if d == "yes" else 100.0 - yes_px_c


def _settle_long_pnl(price_c: float, d: str, result: str) -> float:
    """Maker LONG side D at price_c (cents), held to settlement, zero fee."""
    return (100.0 - price_c) if result == d else -price_c


def _settle_short_pnl(price_c: float, d: str, result: str) -> float:
    """Maker SHORT side D at price_c (sold to the whale/copiers), zero fee."""
    return (price_c - 100.0) if result == d else price_c


# ----------------------------------------------------------------------------
# trades passes
# ----------------------------------------------------------------------------

def load_big_prints(corpus: str, day: str) -> Dict[str, List[Tuple[float, float, str]]]:
    """Pass 1: per ticker, all prints with count_fp >= CONT_CT, as
    (ts_s, count, taker_side), ts-sorted. Enough for trigger + continuation."""
    out: Dict[str, List[Tuple[float, float, str]]] = defaultdict(list)
    path = f"{corpus}/trades/day={day}.jsonl.zst"
    for line in _stream_zst(path):
        try:
            inner = json.loads(json.loads(line)["_raw"])
        except (json.JSONDecodeError, KeyError):
            continue
        msg = inner.get("msg", {})
        tk = msg.get("market_ticker", "")
        if not _is_crypto_15m(tk):
            continue
        cnt = float(msg.get("count_fp", 0) or 0)
        if cnt < CONT_CT:
            continue
        side = msg.get("taker_side", "")
        if side not in ("yes", "no"):
            continue
        out[tk].append((float(msg["ts_ms"]) / 1000.0, cnt, side))
    for tk in out:
        out[tk].sort()
    return out


def load_all_prints(corpus: str, day: str, tickers: set) -> Dict[str, List[Tuple[float, float, float, str]]]:
    """Pass 2 (stage 2 only): ALL prints for triggered tickers, as
    (ts_s, yes_px_c, count, taker_side), ts-sorted (tape NOT globally sorted)."""
    out: Dict[str, List[Tuple[float, float, float, str]]] = defaultdict(list)
    path = f"{corpus}/trades/day={day}.jsonl.zst"
    for line in _stream_zst(path):
        try:
            inner = json.loads(json.loads(line)["_raw"])
        except (json.JSONDecodeError, KeyError):
            continue
        msg = inner.get("msg", {})
        tk = msg.get("market_ticker", "")
        if tk not in tickers:
            continue
        side = msg.get("taker_side", "")
        if side not in ("yes", "no"):
            continue
        out[tk].append((float(msg["ts_ms"]) / 1000.0,
                        float(msg["yes_price_dollars"]) * 100.0,
                        float(msg.get("count_fp", 0) or 0), side))
    for tk in out:
        out[tk].sort()
    return out


def find_triggers(big: Dict[str, List[Tuple[float, float, str]]]):
    """Per ticker: first >=WHALE_CT print. Returns
    (eligible: {tk: (ts, count, side)}, n_any200, n_endgame, n_continued)."""
    eligible: Dict[str, Tuple[float, float, str]] = {}
    n_any200 = n_endgame = n_cont = 0
    for tk, prints in big.items():
        trig = next(((ts, c, s) for ts, c, s in prints if c >= WHALE_CT), None)
        if trig is None:
            continue
        n_any200 += 1
        ts0, _, d = trig
        if close_epoch_from_ticker(tk) - ts0 < MIN_TO_CLOSE_S:
            n_endgame += 1
            continue
        eligible[tk] = trig
        if any(ts0 < ts <= ts0 + CONT_WINDOW_S and c >= CONT_CT and s == d
               for ts, c, s in prints):
            n_cont += 1
    return eligible, n_any200, n_endgame, n_cont


# ----------------------------------------------------------------------------
# frames pass (stage 2)
# ----------------------------------------------------------------------------

def load_frames_for(corpus: str, day: str, want: Dict[str, float]):
    """Stream one frames day; buffer (epoch, inner) ONLY for tickers in `want`
    and only up to trigger_ts + POST_NBBO_MAX_S + 5 (we never need the book
    after the post-sweep point). grep -F prefilter keeps it fast."""
    buf: Dict[str, List[Tuple[float, dict]]] = defaultdict(list)
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as tf:
        tf.write("\n".join(sorted(want)) + "\n")
        gf = tf.name
    try:
        path = f"{corpus}/frames/day={day}.jsonl.zst"
        for line in _stream_zst(path, grep_file=gf):
            m = _TICKER_RE.search(line)
            if not m or m.group(1) not in want:
                continue
            tk = m.group(1)
            try:
                env = json.loads(line)
                ts = _epoch(env["_wire_recv_ts"])
            except (json.JSONDecodeError, KeyError, ValueError):
                continue
            if ts > want[tk] + POST_NBBO_MAX_S + 5.0:
                continue
            buf[tk].append((ts, json.loads(env["_raw"])))
    finally:
        os.unlink(gf)
    for tk in buf:
        buf[tk].sort(key=lambda x: x[0])
    return buf


# ----------------------------------------------------------------------------
# stage-2 simulation
# ----------------------------------------------------------------------------

def simulate_day(corpus: str, day: str, det: dict, funnel: dict,
                 wake_rows: list, skew_rows: list):
    big = load_big_prints(corpus, day)
    funnel["windows_with_100ct_print"] += len(big)
    eligible, n200, n_end, _ = find_triggers(big)
    funnel["windows_with_200ct_print"] += n200
    funnel["trigger_endgame_lt120s"] += n_end
    funnel["campaigns_ge120s"] += len(eligible)

    eligible = {tk: tr for tk, tr in eligible.items() if tk in det}
    funnel["with_settlement"] += len(eligible)
    if not eligible:
        return

    prints = load_all_prints(corpus, day, set(eligible))
    frames = load_frames_for(corpus, day, {tk: tr[0] for tk, tr in eligible.items()})

    for tk, (trig_ts, _trig_ct, d) in sorted(eligible.items()):
        result = det[tk]["result"]
        asset = _is_crypto_15m(tk)
        tape = prints.get(tk, [])
        tl = reliable_nbbo_timeline(frames.get(tk, []))
        # pre-print NBBO: last reliable two-sided point STRICTLY before trigger
        pre = next(((ts, b, a) for ts, b, a in reversed(tl)
                    if ts < trig_ts and b is not None and a is not None), None)
        # post-sweep NBBO: first reliable two-sided point STRICTLY after trigger
        post = next(((ts, b, a) for ts, b, a in tl
                     if trig_ts < ts <= trig_ts + POST_NBBO_MAX_S
                     and b is not None and a is not None), None)

        # ---- Arm W: wake bids at post-sweep side mid -1c / -2c, 90s --------
        if post is None:
            funnel["w_no_post_nbbo"] += 1
        else:
            funnel["w_wake_events"] += 1
            post_ts, yb, ya = post
            side_mid = (_side_px(yb, d) + _side_px(ya, d)) / 2.0
            levels = [side_mid - off for off in WAKE_OFFSETS
                      if 1.0 <= side_mid - off <= 99.0]
            fills: List[float] = []
            live = list(levels)
            for ts, ypx, cnt, s in tape:
                if ts <= post_ts or not live:
                    continue
                if ts > post_ts + WAKE_WORK_S:
                    break
                if cnt >= WHALE_CT and s != d:
                    break  # opposite-direction whale -> cancel remaining
                spx = _side_px(ypx, d)
                for lv in list(live):
                    if spx < lv:  # STRICT cross — print AT our price never fills
                        fills.append(lv)
                        live.remove(lv)
            if fills:
                pnl = float(np.mean([_settle_long_pnl(f, d, result) for f in fills]))
                wake_rows.append({"day": day, "asset": asset, "ticker": tk,
                                  "pnl": pnl, "n_levels": len(fills)})
                funnel["w_filled_windows"] += 1

        # ---- Arm P: pull-and-skew vs baseline passive, 120s ----------------
        if pre is None:
            funnel["p_no_pre_nbbo"] += 1
            continue
        funnel["p_campaigns"] += 1
        _, pyb, pya = pre
        side_bid = _side_px(pya, d) if d == "no" else _side_px(pyb, d)
        side_ask = _side_px(pyb, d) if d == "no" else _side_px(pya, d)
        side_bid, side_ask = min(side_bid, side_ask), max(side_bid, side_ask)
        pre_mid = (side_bid + side_ask) / 2.0
        base_q = min(99.0, max(1.0, side_ask))          # original resting ask
        skew_q = min(99.0, max(1.0, pre_mid + SKEW_C))  # desperation re-quote
        base_fill = skew_fill = None
        for ts, ypx, cnt, s in tape:
            if ts <= trig_ts:
                continue  # strictly after the trigger print (no look-back)
            if ts > trig_ts + SKEW_WORK_S:
                break
            spx = _side_px(ypx, d)
            if base_fill is None and spx >= base_q:
                base_fill = base_q
            if skew_fill is None and spx >= skew_q:
                skew_fill = skew_q
            if base_fill is not None and skew_fill is not None:
                break
        base_mo = _settle_short_pnl(base_fill, d, result) if base_fill is not None else 0.0
        skew_mo = _settle_short_pnl(skew_fill, d, result) if skew_fill is not None else 0.0
        skew_rows.append({"day": day, "asset": asset, "ticker": tk,
                          "improve": skew_mo - base_mo,
                          "base_filled": int(base_fill is not None),
                          "skew_filled": int(skew_fill is not None),
                          "base_mo": base_mo, "skew_mo": skew_mo})


# ----------------------------------------------------------------------------
# main
# ----------------------------------------------------------------------------

def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--corpus", default=os.path.expanduser("~/kalshi-research-data/fairvalue"))
    ap.add_argument("--stage2-days", default="2026-06-03,2026-06-04,2026-06-05")
    ap.add_argument("--smoke", action="store_true",
                    help="stage 1 + stage 2 on the FIRST stage-2 day only")
    # ---- parallel-execution plumbing ONLY (identical computation; rows from
    # per-day worker processes are pooled by pool_main with the IDENTICAL
    # pre-registered criteria — added to fit the runtime budget, not to move
    # any goalpost) ----
    ap.add_argument("--stage1-days", default="",
                    help="CSV override of stage-1 trade days (default: all)")
    ap.add_argument("--skip-stage1", action="store_true",
                    help="worker mode: skip stage 1 (gate applied at pooling)")
    ap.add_argument("--rows-out", default="",
                    help="prefix: dump wake/skew rows + stage1 counts + funnel")
    args = ap.parse_args(argv)
    cr = args.corpus

    sealed = set(_sealed_days(cr))
    s2_days = [d for d in args.stage2_days.split(",") if d]
    for d in s2_days:
        if d not in sealed:
            raise SystemExit(f"stage-2 day {d} has no .done_ marker — not sealed")
    if args.smoke:
        s2_days = s2_days[:1]
    if args.stage1_days:
        s1_days = [d for d in args.stage1_days.split(",") if d]
    else:
        s1_days = _trade_days(cr) if not args.smoke else s2_days

    print(f"[06] corpus={cr}")
    if s1_days:
        print(f"[06] stage-1 trade days ({len(s1_days)}): {s1_days[0]}..{s1_days[-1]}"
              f"  (05-30 partial)")
    print(f"[06] stage-2 frame days: {s2_days}")

    # ---------------- STAGE 1: continuation gate (trades only) -------------
    s1_trig = s1_cont = s1_end = 0
    per_day = {}
    if not args.skip_stage1:
        for day in s1_days:
            big = load_big_prints(cr, day)
            elig, _n200, n_end, n_cont = find_triggers(big)
            s1_trig += len(elig)
            s1_cont += n_cont
            s1_end += n_end
            per_day[day] = (len(elig), n_cont)
    p_cont = s1_cont / s1_trig if s1_trig else float("nan")
    print("\n=== STAGE 1: P(same-dir >=100ct within 120s | first >=200ct print) ===")
    for day, (nt, nc) in sorted(per_day.items()):
        print(f"  {day}: triggers={nt:4d} continued={nc:4d} "
              f"p={nc / nt if nt else float('nan'):.3f}")
    print(f"  POOLED: triggers={s1_trig} continued={s1_cont} P={p_cont:.4f} "
          f"(endgame <120s-to-close, excluded: {s1_end})")
    arm_p_gate_pass = p_cont >= ARM_P_GATE
    print(f"  Arm P gate (P >= {ARM_P_GATE}): {'PASS' if arm_p_gate_pass else 'FAIL -> Arm P DEAD'}")

    # ---------------- STAGE 2: frames sim -----------------------------------
    det = load_determined(f"{cr}/lifecycle", ASSETS)
    print(f"\n[06] settlements loaded: {len(det)} determined windows")

    funnel = defaultdict(int)
    wake_rows: list = []
    skew_rows: list = []
    for day in s2_days:
        print(f"[06] simulating {day} ...", flush=True)
        simulate_day(cr, day, det, funnel, wake_rows, skew_rows)

    if args.rows_out:
        pd.DataFrame(wake_rows).to_csv(f"{args.rows_out}_wake.csv", index=False)
        pd.DataFrame(skew_rows).to_csv(f"{args.rows_out}_skew.csv", index=False)
        with open(f"{args.rows_out}_meta.json", "w") as fh:
            json.dump({"stage1_per_day": per_day, "stage1_endgame": s1_end,
                       "funnel": dict(funnel)}, fh)
        print(f"[06] rows dumped to {args.rows_out}_*.{{csv,json}}")

    print("\n=== STAGE 2 FUNNEL (gotcha 6 — filter bias visible) ===")
    order = ["windows_with_100ct_print", "windows_with_200ct_print",
             "trigger_endgame_lt120s", "campaigns_ge120s", "with_settlement",
             "w_no_post_nbbo", "w_wake_events", "w_filled_windows",
             "p_no_pre_nbbo", "p_campaigns"]
    for k in order:
        print(f"  {k:32s} {funnel[k]}")

    # ---------------- Arm W verdict -----------------------------------------
    print("\n=== ARM W (wake-bid, same-side passive) ===")
    n_wake = funnel["w_wake_events"]
    nW = len(wake_rows)
    fill_rate = nW / n_wake if n_wake else float("nan")
    armW = "DEAD"
    if nW == 0:
        print(f"  n_filled=0 of {n_wake} wake events -> DEAD (no fills)")
    else:
        dfW = pd.DataFrame(wake_rows)
        mean, lo, hi = day_bootstrap_ci(dfW, "pnl")
        print(f"  filled windows n={nW} / wake events {n_wake} "
              f"(fill rate {fill_rate:.1%})")
        print(f"  per-ct settlement PnL: mean={mean:+.2f}c  "
              f"day-bootstrap 95% CI=[{lo:+.2f}, {hi:+.2f}]c  (fee-free maker)")
        for a, g in dfW.groupby("asset"):
            print(f"    {a:5s} n={len(g):3d} mean={g['pnl'].mean():+.2f}c")
        if lo > 0 and nW >= MIN_N and fill_rate >= MIN_FILL_RATE:
            armW = "SURVIVES"
        kill = []
        if lo <= 0:
            kill.append("CI_lo<=0")
        if nW < MIN_N:
            kill.append(f"n<{MIN_N}")
        if fill_rate < MIN_FILL_RATE:
            kill.append("fill_rate<10%")
        print(f"  Arm W verdict: {armW}" + (f"  (killed by: {', '.join(kill)})" if kill else ""))

    # ---------------- Arm P verdict -----------------------------------------
    print("\n=== ARM P (pull-and-skew vs baseline passive) ===")
    nP = len(skew_rows)
    armP = "DEAD"
    if not arm_p_gate_pass:
        print(f"  stage-1 gate FAILED (P={p_cont:.3f} < {ARM_P_GATE}) -> Arm P DEAD "
              f"(sim below reported as supplementary only)")
    if nP == 0:
        print("  n=0 campaigns with pre-print NBBO -> no comparison possible")
    else:
        dfP = pd.DataFrame(skew_rows)
        mean, lo, hi = day_bootstrap_ci(dfP, "improve")
        print(f"  campaigns n={nP}  base fills={int(dfP.base_filled.sum())} "
              f"skew fills={int(dfP.skew_filled.sum())}")
        print(f"  baseline markout mean={dfP.base_mo.mean():+.2f}c  "
              f"skew markout mean={dfP.skew_mo.mean():+.2f}c")
        print(f"  improvement (skew-base) per ct: mean={mean:+.2f}c  "
              f"day-bootstrap 95% CI=[{lo:+.2f}, {hi:+.2f}]c")
        for a, g in dfP.groupby("asset"):
            if len(g) >= 10:
                print(f"    {a:5s} n={len(g):3d} improve={g['improve'].mean():+.2f}c")
        if arm_p_gate_pass and lo > 0 and nP >= MIN_N:
            armP = "SURVIVES"
        kill = []
        if not arm_p_gate_pass:
            kill.append(f"stage1_P<{ARM_P_GATE}")
        if lo <= 0:
            kill.append("CI_lo<=0")
        if nP < MIN_N:
            kill.append(f"n<{MIN_N}")
        print(f"  Arm P verdict: {armP}" + (f"  (killed by: {', '.join(kill)})" if kill else ""))

    print("\n=== VERDICT (pre-registered) ===")
    print(f"  Arm W: {armW}   Arm P: {armP}")
    overall = "EDGE_CANDIDATE" if "SURVIVES" in (armW, armP) else "NO_EDGE"
    print(f"  OVERALL: {overall}")
    return overall


def pool_main(argv=None):
    """Pool per-day worker dumps (parallel execution of the SAME computation)
    and print the verdict against the IDENTICAL pre-registered criteria."""
    ap = argparse.ArgumentParser()
    ap.add_argument("--prefixes", required=True,
                    help="CSV of --rows-out prefixes (stage-1 + stage-2 workers)")
    args = ap.parse_args(argv)
    per_day: Dict[str, Tuple[int, int]] = {}
    s1_end = 0
    funnel = defaultdict(int)
    wake_frames, skew_frames = [], []
    for pref in args.prefixes.split(","):
        with open(f"{pref}_meta.json") as fh:
            meta = json.load(fh)
        for d, (nt, nc) in meta["stage1_per_day"].items():
            per_day[d] = (nt, nc)
        s1_end += meta["stage1_endgame"]
        for k, v in meta["funnel"].items():
            funnel[k] += v
        for arm, acc in (("wake", wake_frames), ("skew", skew_frames)):
            p = f"{pref}_{arm}.csv"
            if os.path.exists(p) and os.path.getsize(p) > 1:
                try:
                    acc.append(pd.read_csv(p))
                except pd.errors.EmptyDataError:
                    pass

    s1_trig = sum(nt for nt, _ in per_day.values())
    s1_cont = sum(nc for _, nc in per_day.values())
    p_cont = s1_cont / s1_trig if s1_trig else float("nan")
    print("=== STAGE 1 (pooled): P(same-dir >=100ct within 120s | first >=200ct) ===")
    for day, (nt, nc) in sorted(per_day.items()):
        print(f"  {day}: triggers={nt:4d} continued={nc:4d} "
              f"p={nc / nt if nt else float('nan'):.3f}")
    print(f"  POOLED: triggers={s1_trig} continued={s1_cont} P={p_cont:.4f} "
          f"(endgame <120s-to-close, excluded: {s1_end})")
    arm_p_gate_pass = p_cont >= ARM_P_GATE
    print(f"  Arm P gate (P >= {ARM_P_GATE}): "
          f"{'PASS' if arm_p_gate_pass else 'FAIL -> Arm P DEAD'}")

    print("\n=== STAGE 2 FUNNEL (pooled; gotcha 6) ===")
    order = ["windows_with_100ct_print", "windows_with_200ct_print",
             "trigger_endgame_lt120s", "campaigns_ge120s", "with_settlement",
             "w_no_post_nbbo", "w_wake_events", "w_filled_windows",
             "p_no_pre_nbbo", "p_campaigns"]
    for k in order:
        print(f"  {k:32s} {funnel[k]}")

    print("\n=== ARM W (wake-bid, same-side passive) ===")
    n_wake = funnel["w_wake_events"]
    armW = "DEAD"
    nW = 0
    if wake_frames:
        dfW = pd.concat(wake_frames, ignore_index=True)
        nW = len(dfW)
    fill_rate = nW / n_wake if n_wake else float("nan")
    if nW == 0:
        print(f"  n_filled=0 of {n_wake} wake events -> DEAD (no fills)")
    else:
        mean, lo, hi = day_bootstrap_ci(dfW, "pnl")
        print(f"  filled windows n={nW} / wake events {n_wake} "
              f"(fill rate {fill_rate:.1%})")
        print(f"  per-ct settlement PnL: mean={mean:+.2f}c  "
              f"day-bootstrap 95% CI=[{lo:+.2f}, {hi:+.2f}]c  (fee-free maker)")
        for a, g in dfW.groupby("asset"):
            print(f"    {a:5s} n={len(g):3d} mean={g['pnl'].mean():+.2f}c")
        if lo > 0 and nW >= MIN_N and fill_rate >= MIN_FILL_RATE:
            armW = "SURVIVES"
        kill = []
        if lo <= 0:
            kill.append("CI_lo<=0")
        if nW < MIN_N:
            kill.append(f"n<{MIN_N}")
        if fill_rate < MIN_FILL_RATE:
            kill.append("fill_rate<10%")
        print(f"  Arm W verdict: {armW}"
              + (f"  (killed by: {', '.join(kill)})" if kill else ""))

    print("\n=== ARM P (pull-and-skew vs baseline passive) ===")
    armP = "DEAD"
    nP = 0
    if skew_frames:
        dfP = pd.concat(skew_frames, ignore_index=True)
        nP = len(dfP)
    if not arm_p_gate_pass:
        print(f"  stage-1 gate FAILED (P={p_cont:.3f} < {ARM_P_GATE}) -> Arm P DEAD")
    if nP == 0:
        print("  n=0 campaigns with pre-print NBBO -> no comparison possible")
    else:
        mean, lo, hi = day_bootstrap_ci(dfP, "improve")
        print(f"  campaigns n={nP}  base fills={int(dfP.base_filled.sum())} "
              f"skew fills={int(dfP.skew_filled.sum())}")
        print(f"  baseline markout mean={dfP.base_mo.mean():+.2f}c  "
              f"skew markout mean={dfP.skew_mo.mean():+.2f}c")
        print(f"  improvement (skew-base) per ct: mean={mean:+.2f}c  "
              f"day-bootstrap 95% CI=[{lo:+.2f}, {hi:+.2f}]c")
        for a, g in dfP.groupby("asset"):
            if len(g) >= 10:
                print(f"    {a:5s} n={len(g):3d} improve={g['improve'].mean():+.2f}c")
        if arm_p_gate_pass and lo > 0 and nP >= MIN_N:
            armP = "SURVIVES"
        kill = []
        if not arm_p_gate_pass:
            kill.append(f"stage1_P<{ARM_P_GATE}")
        if lo <= 0:
            kill.append("CI_lo<=0")
        if nP < MIN_N:
            kill.append(f"n<{MIN_N}")
        print(f"  Arm P verdict: {armP}"
              + (f"  (killed by: {', '.join(kill)})" if kill else ""))

    print("\n=== VERDICT (pre-registered) ===")
    print(f"  Arm W: {armW}   Arm P: {armP}")
    overall = "EDGE_CANDIDATE" if "SURVIVES" in (armW, armP) else "NO_EDGE"
    print(f"  OVERALL: {overall}")
    return overall


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "pool":
        pool_main(sys.argv[2:])
    else:
        main()
