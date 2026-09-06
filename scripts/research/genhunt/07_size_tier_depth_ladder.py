"""genhunt-07: Size-tiered depth ladder — is mid-size impatience uninformed
while whale size is informed? (2026-06-10)

PRE-REGISTRATION (stated BEFORE any data was examined; goalposts frozen):

(a) COUNTERPARTY: mid-size takers (25-199 contracts) whose order size
    structurally exceeds touch depth in thin alt books, forcing a 2-4c walk
    through the book on every trade — a recurring "desperation toll" paid for
    immediacy, hypothesized to carry NO informational content. Whales (200+ ct)
    are hypothesized informed (the established fact "deep prints are informed").

(b) SIGNAL + STRUCTURE UNDER TEST:
    STAGE 1 (measurement, primary deliverable): settlement-direction markout of
    real prints, bucketed by size tier {1-4, 5-24, 25-99, 100-199, 200+} x
    depth-walked (taker's paid price vs prevailing reliable mid, in the taker's
    outcome terms; buckets: at-touch walk<1c, 1c<=walk<2c, deep walk>=2c).
    Two mid variants, BOTH reported:
      (a) frames-anchored mid via reliable_nbbo_timeline on <=3 sealed frames
          days (default 2026-06-03..06-05), prevailing = last reliable
          (bid,ask) with frame_recv_ts <= trade_event_ts and age <= 60s;
      (b) trades-only proxy mid = same-ticker previous print's yes price
          (age <= 60s) on ALL trades days.
    Flag any tier x walk cell where the variants disagree in markout sign.
    Exclusions (both stages): prints with <120s to close (endgame = informed
    flow, reported separately); per-ticker ts_ms sort before processing.
    STAGE 2 (ladder sim, frames days only): maker quotes 5 ct at mid+/-1c and
    20 ct at mid+/-4c, refreshed every 60s from first reliable book tick to
    close-120s; fills = strict price cross by real prints only (taker-yes print
    at p fills our YES asks < p; taker-no print at p fills our YES bids > p),
    fill size min(remaining, print count); deep layer cancelled 1s after any
    >=200-ct print (the campaign pull trigger; the triggering print itself can
    still fill us), re-armed at next refresh; ONE observation per window
    (per-contract PnL of the window's deep-layer fills); hold to settlement.

(c) FEE MATH: maker pays zero fee on Kalshi. The maker-mirror of a taker print
    is the taker's GROSS settlement PnL with sign flipped (the taker's fee goes
    to Kalshi, NOT to us — gotcha 1). All PnL below is GROSS, reported
    explicitly as such, in cents/contract.

(d) KILL CRITERIA (numeric, day-bootstrap, frozen):
    STAGE 1 KILL: monotonicity of informedness in size must HOLD for the ladder
    to live: on variant (b) (all days, primary), day-bootstrap CI lower bound
    of [mean taker markout(200+ deep) - mean taker markout(25-199 deep)] must
    be > 0 (whales strictly more informed than mid-tier) AND the mid-tier deep
    markout itself must have day-bootstrap CI upper bound <= 0 (mid-tier deep
    flow loses gross -> maker-mirror profitable). Either fails -> DEAD
    (publish the sharpened fact "informed at all sizes"). If variant (a)
    disagrees in SIGN with variant (b) on either of the two load-bearing cells
    (mid-tier deep, 200+ deep) -> INCONCLUSIVE instead.
    STAGE 2 KILL: day-bootstrap CI lower bound of deep-layer maker GROSS
    PnL/contract <= +0.5 c/ct (must beat the established 0.2-0.8c touch margin
    to justify depth risk) over available frames days -> DEAD. n reported
    honestly given the <=3-day frames cap.
    OVERALL: EDGE_CANDIDATE requires BOTH stages to survive.

Usage:
  python3 scripts/research/genhunt/07_size_tier_depth_ladder.py \
    [--corpus ~/kalshi-research-data/fairvalue] \
    [--frames-days 2026-06-03,2026-06-04,2026-06-05] [--trades-days all] \
    [--smoke]
"""
from __future__ import annotations

import argparse
import glob
import json
import math
import os
import pickle
import subprocess
import sys
import time
from collections import defaultdict

_REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)

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
from scripts.research.zstd_stream import checked_stream_lines  # noqa: E402  (repo root added to sys.path above)

CR_DEFAULT = os.path.expanduser("~/kalshi-research-data/fairvalue")
ASSETS = ("BTC", "ETH", "SOL", "XRP", "HYPE", "DOGE", "BNB", "ADA", "BCH")

TIERS = (("1-4", 0.0, 5.0), ("5-24", 5.0, 25.0), ("25-99", 25.0, 100.0),
         ("100-199", 100.0, 200.0), ("200+", 200.0, math.inf))
WALKS = ("touch", "1c", "deep>=2c")
ENDGAME_S = 120.0          # exclude prints with < this to close (gotcha 4)
PROXY_MAX_AGE_S = 60.0     # variant (b) prev-print mid max age
MID_MAX_AGE_S = 60.0       # variant (a) reliable-mid max age
SHALLOW_SZ, SHALLOW_OFF = 5, 1.0
DEEP_SZ, DEEP_OFF = 20, 4.0
PULL_TRIGGER_CT = 200.0
PULL_LATENCY_S = 1.0
REFRESH_S = 60.0
RNG = np.random.default_rng(7)


def tier_of(count: float) -> str:
    for name, lo, hi in TIERS:
        if lo <= count < hi:
            return name
    return TIERS[-1][0]


def walk_of(walk_c: float) -> str:
    if walk_c < 1.0:
        return "touch"
    if walk_c < 2.0:
        return "1c"
    return "deep>=2c"


def zst_lines(path: str):
    """Stream lines from a .zst (or plain .jsonl) without buffering the file."""
    # Delegates to the shared CHECKED reader (ticket 86bbvrx1t): the previous
    # body discarded zstd's exit code, so a decompressor that died mid-file
    # ended the loop silently and this function returned a PREFIX of the day.
    yield from checked_stream_lines(path, require_nonempty=False,
                                    skip_blank=False)


def load_determined_cached(lifecycle_dir: str, corpus: str) -> dict:
    files = sorted(glob.glob(f"{lifecycle_dir}/**/*.zst", recursive=True))
    key = (len(files), sum(os.path.getsize(f) for f in files))
    cache = os.path.join(corpus, "_tmp_genhunt_lifecycle.pkl")
    if os.path.exists(cache):
        try:
            with open(cache, "rb") as fh:
                k, det = pickle.load(fh)
            if k == key:
                return det
        except Exception:
            pass
    det = load_determined(lifecycle_dir, set(ASSETS))
    with open(cache, "wb") as fh:
        pickle.dump((key, det), fh)
    return det


def load_trades_day(path: str, determined: dict, funnel: dict) -> dict:
    """{ticker: [(ts, yes_c, taker_side, count)]} per-ticker ts_ms-sorted,
    restricted to settled crypto-15M tickers."""
    out = defaultdict(list)
    for line in zst_lines(path):
        if not line.strip():
            continue
        funnel["trade_lines"] += 1
        try:
            env = json.loads(line)
            msg = json.loads(env["_raw"])["msg"]
            tk = msg["market_ticker"]
        except (ValueError, KeyError):
            funnel["trade_parse_err"] += 1
            continue
        if not _is_crypto_15m(tk):
            funnel["trade_not_crypto15m"] += 1
            continue
        if tk not in determined:
            funnel["trade_no_settlement"] += 1
            continue
        try:
            ts = float(msg["ts_ms"]) / 1000.0
            yes_c = float(msg["yes_price_dollars"]) * 100.0
            cnt = float(msg["count_fp"])
            side = msg["taker_side"]
        except (KeyError, ValueError, TypeError):
            funnel["trade_parse_err"] += 1
            continue
        if side not in ("yes", "no"):
            funnel["trade_parse_err"] += 1
            continue
        out[tk].append((ts, yes_c, side, cnt))
    for tk in out:
        out[tk].sort(key=lambda r: r[0])
    return out


def taker_print_econ(yes_c: float, side: str, result: str):
    """(price paid by taker in its outcome, gross settlement markout c/ct)."""
    if side == "yes":
        pay = yes_c
        settle = 100.0 if result == "yes" else 0.0
    else:
        pay = 100.0 - yes_c
        settle = 100.0 if result == "no" else 0.0
    return pay, settle - pay


# ---------------- STAGE 1 variant (b): trades-only proxy mid ----------------


def stage1_trades_proxy(corpus: str, days, determined: dict):
    rows, endgame_rows = [], []
    funnel = defaultdict(int)
    for day in days:
        path = os.path.join(corpus, "trades", f"day={day}.jsonl.zst")
        if not os.path.exists(path):
            funnel["missing_day_files"] += 1
            continue
        trades = load_trades_day(path, determined, funnel)
        for tk, tr in trades.items():
            close = close_epoch_from_ticker(tk)
            result = determined[tk]["result"]
            prev_ts = prev_yes = None
            for ts, yes_c, side, cnt in tr:
                funnel["prints_settled"] += 1
                this_prev = (prev_ts, prev_yes)
                prev_ts, prev_yes = ts, yes_c  # update BEFORE skips
                if this_prev[0] is None:
                    funnel["no_prev_print"] += 1
                    continue
                if ts - this_prev[0] > PROXY_MAX_AGE_S:
                    funnel["prev_print_stale"] += 1
                    continue
                pay, mark = taker_print_econ(yes_c, side, result)
                prev_pay = this_prev[1] if side == "yes" else 100.0 - this_prev[1]
                walk = pay - prev_pay
                row = (day, tier_of(cnt), walk_of(walk), mark, cnt)
                if close - ts < ENDGAME_S:
                    funnel["endgame_excluded"] += 1
                    endgame_rows.append(row)
                    continue
                funnel["prints_used"] += 1
                rows.append(row)
    cols = ["day", "tier", "walk", "mark", "cnt"]
    return (pd.DataFrame(rows, columns=cols),
            pd.DataFrame(endgame_rows, columns=cols), funnel)


# ------------- STAGE 1 variant (a) + STAGE 2: frames-anchored ---------------


def prevailing_quote(tl, i, ts, max_age):
    """Advance pointer i over timeline tl to the last entry <= ts; return
    (new_i, (bid, ask)) or (new_i, None) if absent/stale/one-sided."""
    while i + 1 < len(tl) and tl[i + 1][0] <= ts:
        i += 1
    if i < 0 or not tl or tl[i][0] > ts or ts - tl[i][0] > max_age:
        return i, None
    _, b, a = tl[i]
    if b is None or a is None:
        return i, None
    return i, (b, a)


def simulate_window_ladder(tl, trades, close, result, funnel):
    """Stage-2 ladder sim for one window. Returns per-layer dict
    {layer: (contracts, gross_pnl_cents)} or None if never quoted."""
    qend = close - ENDGAME_S
    if not tl or tl[0][0] >= qend:
        return None
    settle_yes = 100.0 if result == "yes" else 0.0
    t0 = tl[0][0]
    fills = {"shallow": [0.0, 0.0], "deep": [0.0, 0.0]}  # [contracts, pnl_c]
    i_tl = -1
    j = 0  # trades pointer
    r = t0
    quoted_any = False
    while r < qend:
        r_next = min(r + REFRESH_S, qend)
        i_tl, q = prevailing_quote(tl, i_tl, r, MID_MAX_AGE_S)
        quotes = None
        deep_dead_from = math.inf
        if q is not None:
            mid = (q[0] + q[1]) / 2.0
            quotes = {
                "shallow": {"bid": [max(1.0, math.floor(mid - SHALLOW_OFF)), SHALLOW_SZ],
                            "ask": [min(99.0, math.ceil(mid + SHALLOW_OFF)), SHALLOW_SZ]},
                "deep": {"bid": [max(1.0, math.floor(mid - DEEP_OFF)), DEEP_SZ],
                         "ask": [min(99.0, math.ceil(mid + DEEP_OFF)), DEEP_SZ]},
            }
            quoted_any = True
        while j < len(trades) and trades[j][0] < r_next:
            ts, yes_c, side, cnt = trades[j]
            j += 1
            if ts < r or quotes is None or ts >= qend:
                continue
            deep_live = ts < deep_dead_from
            for layer in ("shallow", "deep"):
                if layer == "deep" and not deep_live:
                    continue
                if side == "yes":   # taker buys YES -> crosses our YES asks
                    px, rem = quotes[layer]["ask"]
                    if yes_c > px and rem > 0:  # strict cross
                        f = min(rem, cnt)
                        quotes[layer]["ask"][1] -= f
                        fills[layer][0] += f
                        fills[layer][1] += f * (px - settle_yes)  # sold YES
                else:               # taker buys NO -> crosses our YES bids
                    px, rem = quotes[layer]["bid"]
                    if yes_c < px and rem > 0:
                        f = min(rem, cnt)
                        quotes[layer]["bid"][1] -= f
                        fills[layer][0] += f
                        fills[layer][1] += f * (settle_yes - px)  # bought YES
            if cnt >= PULL_TRIGGER_CT:
                deep_dead_from = min(deep_dead_from, ts + PULL_LATENCY_S)
                funnel["pull_triggers"] += 1
        r = r_next
    if not quoted_any:
        return None
    return fills


def stage_frames(corpus: str, days, determined: dict, line_cap: int = 0):
    """Single streaming pass per frames day: builds reliable timelines per
    window, then (a) frames-anchored print markouts and (2) ladder sim."""
    v_rows, v_end_rows = [], []        # variant (a) markout rows
    sim_rows = []                      # one row per window per layer
    funnel = defaultdict(int)

    for day in days:
        path = os.path.join(corpus, "frames", f"day={day}.jsonl.zst")
        tpath = os.path.join(corpus, "trades", f"day={day}.jsonl.zst")
        if not (os.path.exists(path) and os.path.exists(tpath)):
            funnel["missing_day_files"] += 1
            continue
        tfun = defaultdict(int)
        trades = load_trades_day(tpath, determined, tfun)
        funnel["trades_loaded"] += sum(len(v) for v in trades.values())

        buffers = defaultdict(list)
        done = set()
        stream_ts = 0.0
        t_start = time.time()

        def finalize(tk):
            buf = buffers.pop(tk)
            buf.sort(key=lambda x: x[0])
            tl = reliable_nbbo_timeline(buf)
            funnel["windows_finalized"] += 1
            if not tl:
                funnel["windows_no_reliable_book"] += 1
            d = determined[tk]
            close = close_epoch_from_ticker(tk)
            tr = trades.get(tk, [])
            # ---- variant (a): frames-anchored markout of real prints ----
            i_tl = -1
            for ts, yes_c, side, cnt in tr:
                funnel["va_prints"] += 1
                i_tl, q = prevailing_quote(tl, i_tl, ts, MID_MAX_AGE_S)
                if q is None:
                    funnel["va_no_reliable_mid"] += 1
                    continue
                mid = (q[0] + q[1]) / 2.0
                pay, mark = taker_print_econ(yes_c, side, d["result"])
                mid_pay = mid if side == "yes" else 100.0 - mid
                walk = pay - mid_pay
                row = (day, tier_of(cnt), walk_of(walk), mark, cnt)
                if close - ts < ENDGAME_S:
                    funnel["va_endgame_excluded"] += 1
                    v_end_rows.append(row)
                else:
                    funnel["va_prints_used"] += 1
                    v_rows.append(row)
            # ---- stage 2: ladder sim ----
            fills = simulate_window_ladder(tl, tr, close, d["result"], funnel)
            if fills is None:
                funnel["windows_never_quoted"] += 1
                return
            funnel["windows_quoted"] += 1
            for layer, (cts, pnl) in fills.items():
                if cts > 0:
                    sim_rows.append((day, tk, layer, cts, pnl, pnl / cts))

        n_lines = 0
        for line in zst_lines(path):
            if not line.strip():
                continue
            n_lines += 1
            if line_cap and n_lines > line_cap:
                break
            try:
                env = json.loads(line)
                inner = json.loads(env["_raw"])
                tk = inner["msg"]["market_ticker"]
            except (ValueError, KeyError):
                continue
            if tk in done or tk not in determined:
                continue
            ts = _epoch(env["_wire_recv_ts"])
            stream_ts = max(stream_ts, ts)
            buffers[tk].append((ts, inner))
            if n_lines % 50000 == 0:
                cut = stream_ts - 180.0
                for t in [t for t in list(buffers)
                          if float(determined[t]["det_ts"]) < cut]:
                    finalize(t)
                    done.add(t)
        for t in list(buffers):
            finalize(t)
            done.add(t)
        funnel["frames_lines"] += n_lines
        print(f"  [frames {day}] {n_lines:,} lines, "
              f"{len(done)} windows, {time.time() - t_start:.0f}s", flush=True)

    cols = ["day", "tier", "walk", "mark", "cnt"]
    sim = pd.DataFrame(sim_rows,
                       columns=["day", "ticker", "layer", "cts", "pnl_c", "pnl_per_ct"])
    return (pd.DataFrame(v_rows, columns=cols),
            pd.DataFrame(v_end_rows, columns=cols), sim, funnel)


# ---------------------------- reporting / verdict ---------------------------


def boot_ci(df, col="mark"):
    if df.empty or df["day"].nunique() < 2:
        m = float(df[col].mean()) if len(df) else float("nan")
        return m, float("nan"), float("nan")
    return day_bootstrap_ci(df, col)


def boot_diff_ci(df_a, df_b, col="mark", n=2000):
    """Day-bootstrap CI of mean(a) - mean(b), resampling the union of days."""
    days = sorted(set(df_a["day"]) | set(df_b["day"]))
    by_a = {d: df_a.loc[df_a["day"] == d, col].values for d in days}
    by_b = {d: df_b.loc[df_b["day"] == d, col].values for d in days}
    diffs = []
    for _ in range(n):
        pick = RNG.choice(days, size=len(days), replace=True)
        va = np.concatenate([by_a[d] for d in pick])
        vb = np.concatenate([by_b[d] for d in pick])
        if len(va) == 0 or len(vb) == 0:
            continue
        diffs.append(va.mean() - vb.mean())
    if not diffs:
        return float("nan"), float("nan"), float("nan")
    lo, hi = np.percentile(diffs, [2.5, 97.5])
    return (float(df_a[col].mean() - df_b[col].mean()), float(lo), float(hi))


def print_grid(df: pd.DataFrame, label: str):
    print(f"\n=== STAGE 1 [{label}]: taker GROSS settlement markout (c/ct), "
          f"maker-mirror = sign-flipped ===")
    print(f"{'tier':>9} {'walk':>9} {'n':>8} {'sum_ct':>10} {'mark':>8} "
          f"{'CI95':>18}")
    for tname, _, _ in TIERS:
        for w in WALKS:
            g = df[(df["tier"] == tname) & (df["walk"] == w)]
            if g.empty:
                print(f"{tname:>9} {w:>9} {0:>8}")
                continue
            m, lo, hi = boot_ci(g)
            ci = f"[{lo:+.2f},{hi:+.2f}]" if not math.isnan(lo) else "[--]"
            print(f"{tname:>9} {w:>9} {len(g):>8,} {g['cnt'].sum():>10,.0f} "
                  f"{m:>+8.2f} {ci:>18}")


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--corpus", default=CR_DEFAULT)
    ap.add_argument("--frames-days", default="2026-06-03,2026-06-04,2026-06-05")
    ap.add_argument("--trades-days", default="all")
    ap.add_argument("--smoke", action="store_true",
                    help="1 frames day, 1 trades day, 400k frame-line cap")
    ap.add_argument("--boot-n", type=int, default=2000)
    a = ap.parse_args(argv)
    corpus = os.path.expanduser(a.corpus)

    sealed = sorted(os.path.basename(p).replace(".done_", "")
                    for p in glob.glob(os.path.join(corpus, ".done_2*")))
    frames_days = [d for d in a.frames_days.split(",") if d in sealed][:3]
    if a.trades_days == "all":
        trades_days = sorted(
            os.path.basename(p)[4:-10]
            for p in glob.glob(os.path.join(corpus, "trades", "day=*.jsonl.zst")))
    else:
        trades_days = a.trades_days.split(",")
    line_cap = 0
    if a.smoke:
        frames_days, trades_days, line_cap = frames_days[:1], trades_days[:1], 400_000

    print(f"[07] sealed markers: {sealed}")
    print(f"[07] frames days (<=3, sealed): {frames_days}   "
          f"trades days: {trades_days}")

    t0 = time.time()
    det = load_determined_cached(os.path.join(corpus, "lifecycle"), corpus)
    print(f"[07] determined windows: {len(det):,} "
          f"({time.time() - t0:.0f}s)", flush=True)

    # ---------------- STAGE 1 (b): trades-only proxy, all days --------------
    vb, vb_end, fb = stage1_trades_proxy(corpus, trades_days, det)
    print("\n[funnel variant-b] " + "  ".join(
        f"{k}={v:,}" for k, v in sorted(fb.items())))
    print_grid(vb, "variant (b) trades-proxy mid, "
               f"{vb['day'].nunique() if len(vb) else 0} days")

    # ------------- STAGE 1 (a) + STAGE 2: frames-anchored, <=3 days ---------
    va, va_end, sim, ff = stage_frames(corpus, frames_days, det, line_cap)
    print("\n[funnel frames] " + "  ".join(
        f"{k}={v:,}" for k, v in sorted(ff.items())))
    print_grid(va, "variant (a) frames-anchored mid, "
               f"{va['day'].nunique() if len(va) else 0} days")

    # endgame, reported separately (gotcha 4)
    eg = pd.concat([vb_end], ignore_index=True)
    if len(eg):
        m = eg["mark"].mean()
        print(f"\n[endgame <120s, variant b, EXCLUDED above] n={len(eg):,} "
              f"taker markout={m:+.2f} c/ct (informed-flow territory)")

    # variant sign-agreement flags
    disagree = []
    for tname, _, _ in TIERS:
        for w in WALKS:
            gb = vb[(vb["tier"] == tname) & (vb["walk"] == w)]
            ga = va[(va["tier"] == tname) & (va["walk"] == w)]
            if len(gb) >= 30 and len(ga) >= 30:
                if np.sign(gb["mark"].mean()) != np.sign(ga["mark"].mean()):
                    disagree.append(f"{tname}/{w}")
    print(f"\n[variant agreement] sign-disagreeing cells (n>=30 both): "
          f"{disagree or 'none'}")

    # ------------------------- STAGE 1 KILL ---------------------------------
    def cell(df, tiers, walk):
        return df[df["tier"].isin(tiers) & (df["walk"] == walk)]

    mid_b = cell(vb, ["25-99", "100-199"], "deep>=2c")
    whale_b = cell(vb, ["200+"], "deep>=2c")
    mid_a = cell(va, ["25-99", "100-199"], "deep>=2c")
    whale_a = cell(va, ["200+"], "deep>=2c")

    print("\n=== STAGE 1 KILL EVAL (pre-registered; variant (b) primary) ===")
    s1_alive = False
    s1_notes = []
    if len(mid_b) < 30 or len(whale_b) < 30:
        s1_notes.append(f"too thin: n_mid_deep={len(mid_b)} "
                        f"n_whale_deep={len(whale_b)}")
        print(f"  {s1_notes[-1]} -> cannot establish monotonicity -> DEAD/GAP")
    else:
        d_m, d_lo, d_hi = boot_diff_ci(whale_b, mid_b, n=a.boot_n)
        m_m, m_lo, m_hi = boot_ci(mid_b)
        print(f"  whale(200+) deep n={len(whale_b)}  mark="
              f"{whale_b['mark'].mean():+.2f}")
        print(f"  mid(25-199) deep n={len(mid_b)}  mark={m_m:+.2f} "
              f"CI[{m_lo:+.2f},{m_hi:+.2f}]")
        print(f"  diff whale-mid = {d_m:+.2f} CI[{d_lo:+.2f},{d_hi:+.2f}] "
              f"(need lo>0)")
        mono = (not math.isnan(d_lo)) and d_lo > 0
        mid_loses = (not math.isnan(m_hi)) and m_hi <= 0
        print(f"  monotonicity(whale>mid informed): "
              f"{'HOLDS' if mono else 'FAILS'};  mid-tier deep loses gross "
              f"(CI hi<=0): {'YES' if mid_loses else 'NO'}")
        s1_alive = mono and mid_loses
        s1_notes.append(f"diff={d_m:+.2f}[{d_lo:+.2f},{d_hi:+.2f}], "
                        f"mid={m_m:+.2f}[{m_lo:+.2f},{m_hi:+.2f}]")
    # variant cross-check on the two load-bearing cells
    s1_inconclusive = False
    for nm, ga, gb in (("mid-deep", mid_a, mid_b), ("whale-deep", whale_a, whale_b)):
        if len(ga) >= 30 and len(gb) >= 30 and \
                np.sign(ga["mark"].mean()) != np.sign(gb["mark"].mean()):
            s1_inconclusive = True
            print(f"  VARIANT DISAGREEMENT on {nm}: (a)="
                  f"{ga['mark'].mean():+.2f} vs (b)={gb['mark'].mean():+.2f} "
                  f"-> Stage 1 INCONCLUSIVE")
    print(f"  STAGE 1: {'INCONCLUSIVE' if s1_inconclusive else ('ALIVE (ladder structure exists)' if s1_alive else 'DEAD (informed at all sizes / no harvestable mid-tier)')}")

    # ------------------------- STAGE 2 KILL ---------------------------------
    print("\n=== STAGE 2: ladder sim, maker GROSS (zero maker fee), "
          "frames days only ===")
    s2_alive = False
    s2_key = "no fills"
    for layer in ("shallow", "deep"):
        g = sim[sim["layer"] == layer]
        if g.empty:
            print(f"  {layer}: 0 windows with fills")
            continue
        m, lo, hi = boot_ci(g, "pnl_per_ct")
        tot_ct, tot_pnl = g["cts"].sum(), g["pnl_c"].sum()
        ci = f"CI95[{lo:+.2f},{hi:+.2f}]" if not math.isnan(lo) else "CI[--]"
        print(f"  {layer:>8}: windows={len(g):,} contracts={tot_ct:,.0f} "
              f"gross/ct(window-mean)={m:+.2f}c {ci} "
              f"pool gross/ct={tot_pnl / tot_ct:+.2f}c "
              f"days={g['day'].nunique()}")
        if layer == "deep":
            s2_alive = (not math.isnan(lo)) and lo > 0.5
            s2_key = (f"deep windows={len(g)}, gross/ct={m:+.2f}c "
                      f"CI[{lo:+.2f},{hi:+.2f}]")
            print(f"  STAGE 2 KILL: deep CI lower bound {lo:+.2f}c "
                  f"{'>' if s2_alive else '<='} +0.50c -> "
                  f"{'SURVIVES' if s2_alive else 'DEAD'}")

    # ------------------------- OVERALL VERDICT ------------------------------
    print("\n=== VERDICT (pre-registered) ===")
    if s1_inconclusive:
        verdict = "INCONCLUSIVE"
    elif s1_alive and s2_alive:
        verdict = "EDGE_CANDIDATE"
    else:
        verdict = "NO_EDGE"
    print(f"  Stage1 alive={s1_alive} ({'; '.join(s1_notes)})")
    print(f"  Stage2 alive={s2_alive} ({s2_key})")
    print(f"  OVERALL: {verdict}")
    print(f"[07] total runtime {time.time() - t0:.0f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
