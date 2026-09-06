"""liquidity_vacuum_dimes — "Picking up dimes after explosions" (passive vacuum fills).

HYPOTHESIS (pre-registered)
---------------------------
Fading sweeps as TAKER died in the May-31 hunt (sweeps continue; adverse
selection). The untested INVERSION: park PASSIVE resting bids far below the
reliable mid (and offers far above) and wait. They fill ONLY when a sweep /
fat-finger blows through a thin book — we get filled at OUR price during a
liquidity vacuum, sellers-in-a-hurry pay us, and price tends to revert toward
pre-sweep fair. The counterparty is URGENCY, not information. Key difference
from the dead taker-fade: we never pay the spread and we set the price.

WHAT WE MEASURE (pre-registered)
--------------------------------
1. VACUUM CENSUS — how often does a print occur >= D cents through the last
   reliable mid (D grid: 5, 10, 15, 20c)? Per asset, per time-to-close bucket
   (>10min, 5-10min, 2-5min, <2min). These are the candidate fills.
2. REVERSION CHECK — after such a print, where is the reliable mid 30s / 60s /
   120s later, relative to the PRINT price? Mean + day-bootstrap CI of
   (mid_after - print_price) in cents, SIGNED so positive = profit for the
   resting order (bid-side fill: mid_after - print; ask-side: print - mid_after).
   Compared to settlement value too (same signing).
3. SIM — strategy: at all times maintain a resting YES bid at
   (last_reliable_mid - D) and a resting YES offer at (last_reliable_mid + D);
   refresh when the reliable mid moves > 2c from the last QUOTED mid, with a 1s
   reaction latency (we are slow — modeled: new levels become effective at
   trigger_ts + 1s). Levels are integer cents, rounded AWAY from mid
   (bid floor, offer ceil — conservative). Fill model: an actual printed trade
   at a price at-or-through our resting level fills 1 contract at OUR level;
   a side that fills is CONSUMED until the next refresh becomes effective (so
   one sweep can't "fill" us 15 times on the same quote). Two exits evaluated
   separately: (a) HOLD to settlement, net of the entry fee; (b) EXIT at the
   first reliable mid >= 60s after the fill (taker exit, fee both ways; falls
   back to hold-to-settle when no reliable mid exists before close — flagged).
   Per (asset x D x time-bucket): n_fills, mean net PnL/ct, DAY-bootstrap CI
   (resample days). KILL CRITERION: survives iff CI-lo > 0 AND n_fills >= 30.
4. TOXICITY SPLIT — split fills by whether the window subsequently settled
   AGAINST the fill side (long-YES fill + settle no; short-YES fill + settle
   yes) — conditional means show whether deep fills are systematically informed
   (the way this idea dies).

LOOK-AHEAD DISCIPLINE
---------------------
- Everything keyed on `_wire_recv_ts` (arrival clock; frames + trades share it).
- The resting level active at a print time comes ONLY from mids observed
  strictly earlier, shifted +1s latency. Census mid = last reliable mid with
  frame-arrival STRICTLY before the print arrival.
- Reliable NBBO = snapshot-anchored, never-crossed reconstruction
  (`reliable_nbbo_timeline`); unreliable stretches emit nothing and we quote
  off the last reliable mid (as a real slow quoter would).
- Day-bootstrap CI ONLY (resample days; n_days < 2 => verdict INSUFF-DAYS,
  never a survive). Net of fees via the canonical amortized fee curve.
- Per-day n reported so thin cells are visible.

CAVEATS (honest, pre-registered)
--------------------------------
- Exit (b) assumes we can exit AT the reliable mid (midpoint assumption,
  optimistic by ~half-spread); hold-to-settle (a) has no such assumption.
- No queue model: we assume our resting order is present and unfilled-by-attrition
  at the print. At levels 5-20c off-mid the queue is usually empty, but a real
  order may occasionally be beaten to the fill. Fill counts are an UPPER bound.
- Windows closing within ~15min of the UTC day partition boundary may have
  partial frames/trades (day files are processed independently; first-day-wins
  dedup). Affects ~1-2% of windows; noted, not corrected.

Corpus contract (~/kalshi-research-data/fairvalue): frames/day=*.jsonl[.zst]
bronze orderbook envelopes; trades/day=*.jsonl.zst trade prints
(yes_price_dollars string-dollars, count_fp, ts_ms); lifecycle/ -> determined
outcomes via `load_determined`.

Usage:
  python3 -m scripts.research.stepfour.liquidity_vacuum_dimes \
      --corpus ~/kalshi-research-data/fairvalue [--days day=2026-05-30[,day=...]]

Siblings: scripts/research/algo_zoo/taker_sweep_vwap_reversion.py (the dead
taker version), scripts/research/path_alpha_probe.py (streaming pattern).
"""
from __future__ import annotations

import argparse
import glob
import json
import math
import os
import random
import re
import subprocess
import sys
from bisect import bisect_left
from collections import defaultdict
from datetime import date, timedelta

sys.path.insert(0, "/Users/gabrielkagan/Documents/kalshi-bot")

from scripts.research.early_exit_backtest import reliable_nbbo_timeline  # noqa: E402
from scripts.research.phase1b_real_price_economics import (  # noqa: E402
    _epoch,
    _is_crypto_15m,
    close_epoch_from_ticker,
    load_determined,
)
from scripts.research.settlement_convergence_p1a import (  # noqa: E402
    kalshi_fee_per_contract_cents,
)
from scripts.research.zstd_stream import checked_stream_lines  # noqa: E402  (repo root added to sys.path above)

ASSETS = ("BTC", "ETH", "SOL", "XRP", "HYPE", "DOGE", "BNB", "ADA", "BCH")
D_GRID = (5, 10, 15, 20)            # cents through the last reliable mid
REFRESH_C = 2.0                      # re-quote when mid moved > 2c from quoted mid
LATENCY_S = 1.0                      # our reaction latency (slow by design)
HORIZONS_S = (30, 60, 120)           # reversion-check horizons
EXIT_H_S = 60.0                      # sim exit (b) horizon
MIN_FILLS_SURVIVE = 30               # kill criterion floor
FLUSH_EVERY = 50000
CLOSE_MARGIN_S = 180.0

# time-to-close buckets (label, lo_s_inclusive, hi_s_exclusive)
TT_BUCKETS = (
    ("<2min", 0.0, 120.0),
    ("2-5min", 120.0, 300.0),
    ("5-10min", 300.0, 600.0),
    (">10min", 600.0, float("inf")),
)
BUCKET_ORDER = (">10min", "5-10min", "2-5min", "<2min")

_DAY_RE = re.compile(r"(\d{4}-\d{2}-\d{2})")


def _bucket(ttc_s: float) -> str:
    for lab, lo, hi in TT_BUCKETS:
        if lo <= ttc_s < hi:
            return lab
    return ">10min"


# ---------------------------------------------------------------------------
# streaming IO (memory-bounded — NEVER load a whole day)
# ---------------------------------------------------------------------------


def _stream_lines(path: str):
    """Yield lines from a .jsonl or .jsonl.zst WITHOUT materializing the file
    (zstd subprocess pipe for .zst; plain iteration otherwise)."""
    # Delegates to the shared CHECKED reader (ticket 86bbvrx1t) — the previous
    # body discarded zstd's exit code, silently returning a truncated day.
    yield from checked_stream_lines(path, require_nonempty=False,
                                    skip_blank=False)


def load_day_trades(path: str, determined: dict) -> dict:
    """{ticker: [(recv_epoch, yes_price_cents), ...]} sorted, for determined
    crypto-15M tickers only. Trades day files are small (<50MB zst) — the
    filtered in-memory product is tiny."""
    out: dict[str, list] = defaultdict(list)
    n_lines = 0
    for line in _stream_lines(path):
        if not line.strip():
            continue
        n_lines += 1
        try:
            env = json.loads(line)
            msg = json.loads(env["_raw"])["msg"]
            tk = msg["market_ticker"]
        except (ValueError, KeyError):
            continue
        if tk not in determined:
            continue
        try:
            recv = _epoch(env["_wire_recv_ts"])
            yc = float(msg["yes_price_dollars"]) * 100.0
        except (ValueError, KeyError, TypeError):
            continue
        if not (0.0 < yc < 100.0):
            continue
        out[tk].append((recv, yc))
    for tk in out:
        out[tk].sort(key=lambda x: x[0])
    return out


def stream_day_frames(path: str, determined: dict, skip: set, on_window) -> tuple:
    """Stream one day's bronze orderbook file in arrival order, buffering raw
    frames ONLY for currently-open determined windows (path_alpha_probe
    stream_windows pattern, .zst-capable). When the stream clock passes a
    window's det_ts + margin, build its reliable timeline, hand
    (ticker, det_row, timeline) to on_window, free the buffer. Returns
    (n_lines, n_finalized)."""
    buffers: dict[str, list] = defaultdict(list)
    done: set = set()
    n_lines = n_final = 0
    stream_ts = 0.0

    def _finalize(tk: str) -> None:
        nonlocal n_final
        buf = buffers[tk]
        buf.sort(key=lambda x: x[0])
        tl = reliable_nbbo_timeline(buf)
        on_window(tk, determined[tk], tl)
        n_final += 1

    for line in _stream_lines(path):
        if not line.strip():
            continue
        n_lines += 1
        try:
            env = json.loads(line)
            inner = json.loads(env["_raw"])
            tk = inner["msg"]["market_ticker"]
        except (ValueError, KeyError):
            continue
        if tk in done or tk in skip or tk not in determined:
            continue
        ts = _epoch(env["_wire_recv_ts"])
        stream_ts = ts if ts > stream_ts else stream_ts
        buffers[tk].append((ts, inner))
        if n_lines % FLUSH_EVERY == 0:
            cut = stream_ts - CLOSE_MARGIN_S
            for t in [t for t in list(buffers)
                      if float(determined[t]["det_ts"]) < cut]:
                _finalize(t)
                done.add(t)
                del buffers[t]
    for t in list(buffers):
        _finalize(t)
        done.add(t)
        del buffers[t]
    return n_lines, n_final


def load_determined_for_days(lifecycle_dir: str, days: list, assets) -> dict:
    """load_determined restricted to the requested day partitions (+1 day for
    midnight straddlers). Falls back to the whole tree if the per-day dirs are
    absent. Strike linkage is irrelevant here (unused)."""
    want: set = set()
    for d in days:
        dt = date.fromisoformat(d)
        for dd in (dt, dt + timedelta(days=1)):
            want.add((f"{dd.year:04d}", f"{dd.month:02d}", f"{dd.day:02d}"))
    day_dirs = []
    for y, m, dd in sorted(want):
        p = os.path.join(lifecycle_dir, f"year={y}", f"month={m}", f"day={dd}")
        if os.path.isdir(p):
            day_dirs.append(p)
    if not day_dirs:
        return load_determined(lifecycle_dir, assets)
    merged: dict = {}
    for p in day_dirs:
        merged.update(load_determined(p, assets))
    return merged


# ---------------------------------------------------------------------------
# per-window evaluation (census + reversion + sim + toxicity)
# ---------------------------------------------------------------------------


def _quote_schedule(mids: list, close_ts: float) -> list:
    """[(effective_ts, quoted_mid)] — re-quote when the reliable mid moves
    > REFRESH_C from the last QUOTED mid; the new quote turns on LATENCY_S
    later. Past-only by construction."""
    sched: list = []
    qmid = None
    for ts, mid in mids:
        if ts >= close_ts:
            break
        if qmid is None or abs(mid - qmid) > REFRESH_C:
            qmid = mid
            sched.append((ts + LATENCY_S, mid))
    return sched


def evaluate_window(day: str, tk: str, d: dict, tl: list, trades: list,
                    coll: dict) -> None:
    """Run all four pre-registered measurements on one settled window."""
    asset = d["asset"]
    try:
        close_ts = close_epoch_from_ticker(tk)
    except (ValueError, IndexError):
        close_ts = float(d["det_ts"])
    settle = 100.0 if d["result"] == "yes" else 0.0

    mids = [(ts, (b + a) / 2.0) for ts, b, a in tl
            if b is not None and a is not None]
    coll["n_windows"] += 1
    if not mids or not trades:
        coll["n_prints_unusable_window"] += len(trades)
        return
    mts = [m[0] for m in mids]

    def _mid_before(t: float):
        i = bisect_left(mts, t) - 1          # strictly earlier arrival
        return mids[i][1] if i >= 0 else None

    def _mid_at_or_after(t: float):
        i = bisect_left(mts, t)
        if i < len(mids) and mids[i][0] <= close_ts:
            return mids[i][1]
        return None

    # --- 1+2: census + reversion -------------------------------------------
    for rts, p in trades:
        ttc = close_ts - rts
        if ttc <= 0:
            continue
        mid = _mid_before(rts)
        if mid is None:
            coll["n_prints_no_mid"] += 1
            continue
        bkt = _bucket(ttc)
        coll["prints"][(asset, bkt)] += 1
        coll["prints_by_day"][day] += 1
        depth = p - mid
        for D in D_GRID:
            if depth <= -D:
                side = "bid"
            elif depth >= D:
                side = "ask"
            else:
                continue
            coll["census"][(asset, bkt, D)] += 1
            sgn = 1.0 if side == "bid" else -1.0
            for h in HORIZONS_S:
                m_h = _mid_at_or_after(rts + h)
                if m_h is not None:
                    coll["rev"][(D, h)].append((day, sgn * (m_h - p)))
            coll["rev_settle"][D].append((day, sgn * (settle - p)))

    # --- 3+4: sim ------------------------------------------------------------
    sched = _quote_schedule(mids, close_ts)
    if not sched:
        return
    eff_ts = [s[0] for s in sched]
    for D in D_GRID:
        si = -1
        bid_used = ask_used = False
        for rts, p in trades:
            ttc = close_ts - rts
            if ttc <= 0:
                break
            nsi = bisect_left(eff_ts, rts + 1e-9) - 1  # active quote: eff <= rts
            if nsi > si:
                si = nsi
                bid_used = ask_used = False
            if si < 0:
                continue
            qm = sched[si][1]
            bid_lvl = math.floor(qm - D)
            ask_lvl = math.ceil(qm + D)
            fills = []
            if not bid_used and bid_lvl >= 1 and p <= bid_lvl:
                fills.append(("bid", float(bid_lvl)))
                bid_used = True
            if not ask_used and ask_lvl <= 99 and p >= ask_lvl:
                fills.append(("ask", float(ask_lvl)))
                ask_used = True
            for side, L in fills:
                fee_in = kalshi_fee_per_contract_cents(L)
                if side == "bid":          # long YES at L
                    hold = (settle - L) - fee_in
                    against = settle == 0.0
                else:                       # short YES at L (sold YES)
                    hold = (L - settle) - fee_in
                    against = settle == 100.0
                m_x = _mid_at_or_after(rts + EXIT_H_S)
                if m_x is not None:
                    fee_out = kalshi_fee_per_contract_cents(m_x)
                    gross = (m_x - L) if side == "bid" else (L - m_x)
                    exit60 = gross - fee_in - fee_out
                    fb = False
                else:
                    exit60, fb = hold, True
                coll["fills"].append({
                    "day": day, "asset": asset, "D": D, "bucket": _bucket(ttc),
                    "side": side, "lvl": L, "hold": hold, "exit60": exit60,
                    "exit_fallback": fb, "against": against,
                })


# ---------------------------------------------------------------------------
# day-bootstrap CI
# ---------------------------------------------------------------------------


def day_bootstrap_ci(pairs: list, n_boot: int = 1000, alpha: float = 0.05,
                     seed: int = 12345) -> tuple:
    """Percentile bootstrap CI for the mean, resampling DAYS (the pre-registered
    unit). pairs = [(day, value)]. Returns (lo, hi, n_days); lo/hi are NaN when
    n_days < 2 (a 1-day CI is degenerate — never trust it)."""
    by_day: dict = defaultdict(list)
    for dy, v in pairs:
        by_day[dy].append(v)
    days = sorted(by_day)
    nd = len(days)
    if nd < 2:
        return (float("nan"), float("nan"), nd)
    # per-day (sum, count) precomputed ONCE — each bootstrap draw then combines
    # day aggregates, identical semantics (and RNG path) to summing raw values
    sums = {dy: sum(by_day[dy]) for dy in days}
    cnts = {dy: float(len(by_day[dy])) for dy in days}
    rng = random.Random(seed)
    means = []
    for _ in range(n_boot):
        tot = cnt = 0.0
        for _ in range(nd):
            dy = days[rng.randrange(nd)]
            tot += sums[dy]
            cnt += cnts[dy]
        means.append(tot / cnt)
    means.sort()
    lo = means[int((alpha / 2) * n_boot)]
    hi = means[min(n_boot - 1, int((1 - alpha / 2) * n_boot))]
    return (lo, hi, nd)


def _mean(xs) -> float:
    xs = list(xs)
    return sum(xs) / len(xs) if xs else float("nan")


def _ci_str(lo: float, hi: float) -> str:
    if math.isnan(lo):
        return "[insuff-days]"
    return f"[{lo:+.2f},{hi:+.2f}]"


# ---------------------------------------------------------------------------
# reporting
# ---------------------------------------------------------------------------


def report(coll: dict) -> None:
    print(f"\nwindows evaluated (frames+determined): {coll['n_windows']}   "
          f"prints with no prior reliable mid: {coll['n_prints_no_mid']}   "
          f"prints in unusable windows (no mids): {coll['n_prints_unusable_window']}\n"
          f"(remaining loaded-vs-classified gap = prints on determined tickers "
          f"with NO frames in the day file)")

    # --- per-day n (thin cells visible) -------------------------------------
    fills_by_day_D: dict = defaultdict(int)
    for f in coll["fills"]:
        fills_by_day_D[(f["day"], f["D"])] += 1
    print("\n=== PER-DAY n (prints with mid; sim fills at each D) ===")
    print(f"{'day':>12}{'prints':>9}" + "".join(f"{'fills@'+str(D):>10}" for D in D_GRID))
    for dy in sorted(coll["prints_by_day"]):
        row = f"{dy:>12}{coll['prints_by_day'][dy]:>9}"
        for D in D_GRID:
            row += f"{fills_by_day_D.get((dy, D), 0):>10}"
        print(row)

    # --- 1. census -----------------------------------------------------------
    print("\n=== 1. VACUUM CENSUS — prints >= D cents through last reliable mid ===")
    print(f"{'asset':>6}{'bucket':>9}{'prints':>9}"
          + "".join(f"{f'>={D}c':>7}" for D in D_GRID))
    assets_seen = sorted({a for a, _ in coll["prints"]})
    for a in assets_seen + ["ALL"]:
        for bkt in BUCKET_ORDER:
            if a == "ALL":
                npr = sum(v for (aa, bb), v in coll["prints"].items() if bb == bkt)
                cnts = [sum(v for (aa, bb, dd), v in coll["census"].items()
                            if bb == bkt and dd == D) for D in D_GRID]
            else:
                npr = coll["prints"].get((a, bkt), 0)
                cnts = [coll["census"].get((a, bkt, D), 0) for D in D_GRID]
            if npr == 0:
                continue
            print(f"{a:>6}{bkt:>9}{npr:>9}"
                  + "".join(f"{c:>7}" for c in cnts))

    # --- 2. reversion --------------------------------------------------------
    print("\n=== 2. REVERSION — signed (profit-for-resting-order) vs PRINT price ===")
    print("   bid-side event: mid_after - print ; ask-side: print - mid_after")
    print(f"{'D':>4}{'h':>6}{'n':>7}{'mean_c':>9}{'dayCI':>18}")
    for D in D_GRID:
        for h in HORIZONS_S:
            pairs = coll["rev"].get((D, h), [])
            if not pairs:
                continue
            lo, hi, nd = day_bootstrap_ci(pairs)
            print(f"{D:>4}{h:>5.0f}s{len(pairs):>7}{_mean(v for _, v in pairs):>+9.2f}"
                  f"{_ci_str(lo, hi):>18}")
        pairs = coll["rev_settle"].get(D, [])
        if pairs:
            lo, hi, nd = day_bootstrap_ci(pairs)
            print(f"{D:>4}{'settl':>6}{len(pairs):>7}"
                  f"{_mean(v for _, v in pairs):>+9.2f}{_ci_str(lo, hi):>18}")

    # --- 3. sim --------------------------------------------------------------
    fills = coll["fills"]
    print(f"\n=== 3. SIM — passive (mid-D / mid+D), {LATENCY_S:.0f}s latency, "
          f"refresh on >{REFRESH_C:.0f}c mid move; net of fees ===")
    print(f"{'D':>4}{'n':>7}{'nd':>4}{'fb%':>5}{'holdEV':>9}{'holdCI':>18}"
          f"{'exitEV':>9}{'exitCI':>18}")
    for D in D_GRID:
        sub = [f for f in fills if f["D"] == D]
        if not sub:
            print(f"{D:>4}{0:>7}   -- no fills")
            continue
        hp = [(f["day"], f["hold"]) for f in sub]
        xp = [(f["day"], f["exit60"]) for f in sub]
        hlo, hhi, nd = day_bootstrap_ci(hp)
        xlo, xhi, _ = day_bootstrap_ci(xp)
        fb = 100.0 * sum(f["exit_fallback"] for f in sub) / len(sub)
        print(f"{D:>4}{len(sub):>7}{nd:>4}{fb:>5.0f}"
              f"{_mean(v for _, v in hp):>+9.2f}{_ci_str(hlo, hhi):>18}"
              f"{_mean(v for _, v in xp):>+9.2f}{_ci_str(xlo, xhi):>18}")

    print(f"\n--- sim cells: (asset x D x bucket), n >= 5 shown; "
          f"KILL bar: CI-lo > 0 AND n >= {MIN_FILLS_SURVIVE} ---")
    print(f"{'asset':>6}{'D':>4}{'bucket':>9}{'n':>6}{'nd':>4}{'holdEV':>9}"
          f"{'holdCI':>18}{'exitEV':>9}{'exitCI':>18}{'verdict':>13}")
    cells: dict = defaultdict(list)
    for f in fills:
        cells[(f["asset"], f["D"], f["bucket"])].append(f)
    survivors = []
    for key in sorted(cells, key=lambda k: (k[0], k[1], BUCKET_ORDER.index(k[2]))):
        sub = cells[key]
        if len(sub) < 5:
            continue
        a, D, bkt = key
        hp = [(f["day"], f["hold"]) for f in sub]
        xp = [(f["day"], f["exit60"]) for f in sub]
        hlo, hhi, nd = day_bootstrap_ci(hp)
        xlo, xhi, _ = day_bootstrap_ci(xp)
        verdict = "thin"
        if len(sub) >= MIN_FILLS_SURVIVE and nd >= 2:
            best_lo = max(hlo, xlo)
            verdict = "SURVIVES" if best_lo > 0 else "killed"
        elif nd < 2:
            verdict = "INSUFF-DAYS"
        if verdict == "SURVIVES":
            survivors.append((key, len(sub), _mean(v for _, v in hp),
                              _mean(v for _, v in xp), hlo, xlo))
        print(f"{a:>6}{D:>4}{bkt:>9}{len(sub):>6}{nd:>4}"
              f"{_mean(v for _, v in hp):>+9.2f}{_ci_str(hlo, hhi):>18}"
              f"{_mean(v for _, v in xp):>+9.2f}{_ci_str(xlo, xhi):>18}{verdict:>13}")

    # --- 4. toxicity ----------------------------------------------------------
    print("\n=== 4. TOXICITY SPLIT — did the window settle AGAINST the fill side? ===")
    print(f"{'D':>4}{'class':>9}{'n':>7}{'hold_mean':>11}{'exit60_mean':>13}"
          f"{'avg_lvl_dist':>14}")
    for D in D_GRID:
        sub = [f for f in fills if f["D"] == D]
        for label, flag in (("AGAINST", True), ("WITH", False)):
            grp = [f for f in sub if f["against"] is flag]
            if not grp:
                continue
            print(f"{D:>4}{label:>9}{len(grp):>7}"
                  f"{_mean(f['hold'] for f in grp):>+11.2f}"
                  f"{_mean(f['exit60'] for f in grp):>+13.2f}"
                  f"{_mean(abs(50.0 - f['lvl']) for f in grp):>14.1f}")

    # --- verdict ---------------------------------------------------------------
    print("\n=== KILL CRITERION (pre-registered): survives iff day-CI-lo > 0 "
          f"AND n_fills >= {MIN_FILLS_SURVIVE} ===")
    nd_all = len({f["day"] for f in fills})
    if nd_all < 5:
        print(f"⚠️  only {nd_all} day(s) of fills — the day-bootstrap is "
              f"under-powered (nd=2 has 3 resample combos); treat any SURVIVES "
              f"as provisional until the full corpus runs. ~5% of cells false-"
              f"survive by chance at the 95% CI (multiple comparisons).")
    if survivors:
        print(f"🟢 {len(survivors)} surviving cell(s) — CANDIDATE, dispatch "
              f"adversarial review before belief:")
        for (a, D, bkt), n, hm, xm, hlo, xlo in survivors:
            print(f"   {a} D={D} {bkt}: n={n} holdEV={hm:+.2f}c (lo {hlo:+.2f}) "
                  f"exitEV={xm:+.2f}c (lo {xlo:+.2f})")
    else:
        print("⚪ NO cell survives. Passive vacuum-fill dimes do not clear the "
              "bar on this corpus (informed flow / insufficient reversion / "
              "insufficient days — see toxicity + per-day n above).")


# ---------------------------------------------------------------------------
# driver
# ---------------------------------------------------------------------------


def _discover_days(corpus: str) -> list:
    """Day labels (YYYY-MM-DD) present in BOTH frames/ and trades/."""
    def days_in(sub: str) -> set:
        out = set()
        for p in glob.glob(os.path.join(corpus, sub, "day=*.jsonl*")):
            m = _DAY_RE.search(os.path.basename(p))
            if m:
                out.add(m.group(1))
        return out
    return sorted(days_in("frames") & days_in("trades"))


def _day_file(corpus: str, sub: str, day: str):
    for ext in (".jsonl.zst", ".jsonl"):
        p = os.path.join(corpus, sub, f"day={day}{ext}")
        if os.path.exists(p):
            return p
    return None


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="liquidity-vacuum passive dimes evaluator")
    ap.add_argument("--corpus", required=True,
                    help="~/kalshi-research-data/fairvalue (frames/ trades/ lifecycle/)")
    ap.add_argument("--days", default=None,
                    help="comma-separated day partitions (e.g. day=2026-05-30); "
                         "default = all days present in BOTH frames/ and trades/")
    args = ap.parse_args(argv)
    corpus = os.path.expanduser(args.corpus)

    if args.days:
        days = [_DAY_RE.search(s).group(1) for s in args.days.split(",")
                if _DAY_RE.search(s)]
    else:
        days = _discover_days(corpus)
    if not days:
        print("no day partitions found", file=sys.stderr)
        return 2

    print(f"days: {days}")
    print("loading determined windows (lifecycle, day-restricted)...", flush=True)
    determined = load_determined_for_days(
        os.path.join(corpus, "lifecycle"), days, ASSETS)
    print(f"  determined crypto-15M windows: {len(determined)}", flush=True)

    coll = {
        "n_windows": 0, "n_prints_no_mid": 0, "n_prints_unusable_window": 0,
        "prints": defaultdict(int), "prints_by_day": defaultdict(int),
        "census": defaultdict(int),
        "rev": defaultdict(list), "rev_settle": defaultdict(list),
        "fills": [],
    }
    seen: set = set()   # first-day-wins dedup for midnight straddlers
    for day in days:
        fpath = _day_file(corpus, "frames", day)
        tpath = _day_file(corpus, "trades", day)
        if not fpath or not tpath:
            print(f"  {day}: missing frames or trades file — skipped")
            continue
        print(f"  {day}: loading trades...", flush=True)
        trades_by_tk = load_day_trades(tpath, determined)
        n_tr = sum(len(v) for v in trades_by_tk.values())
        print(f"  {day}: {len(trades_by_tk)} tickers / {n_tr} prints; "
              f"streaming frames...", flush=True)

        def on_window(tk, d, tl, _day=day, _tr=trades_by_tk):
            evaluate_window(_day, tk, d, tl, _tr.get(tk, []), coll)

        n_lines, n_final = stream_day_frames(fpath, determined, seen, on_window)
        # First-day-wins dedup: every determined ticker whose det_ts falls in
        # this day is marked seen so a later day file never re-evaluates it
        # (windows straddling midnight get partial frames — see CAVEATS).
        d0 = date.fromisoformat(day)
        lo_e = _epoch(f"{d0.isoformat()}T00:00:00Z")
        hi_e = lo_e + 86400.0
        for tk, dd in determined.items():
            if lo_e <= float(dd["det_ts"]) < hi_e + CLOSE_MARGIN_S:
                seen.add(tk)
        print(f"  {day}: {n_lines} frame lines, {n_final} windows finalized",
              flush=True)

    report(coll)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
