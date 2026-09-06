"""Empty-book casino — quote where there is NO competition (crypto-15M bronze).

HYPOTHESIS (pre-registered): "Be the only casino in town." During hours/assets
where NO competitive maker is present (book one-sided, empty, or spread >= THRESH),
impatient retail takers still arrive and pay whatever is quoted. A wide two-sided
quote posted during these maker-absence regimes captures spread WITHOUT predictive
edge — the counterparty is impatience, not information. The May-31 41-mechanism
hunt tested MM in COMPETITIVE books (died of adverse selection + speed); quoting
where there is NO competition is untested.

WHAT WE MEASURE (pre-registered, in this order):
  1. ABSENCE MAP — per (asset, UTC-hour-of-day) cell: fraction of reliable-book
     TIME where spread >= 10c, >= 20c, or the book is missing a side entirely.
     Where/when is the venue unquoted?
  2. PAID-DESPERATION EVIDENCE — join trade prints to the contemporaneous book
     state (last reliable point STRICTLY BEFORE the print — no look-ahead). In
     absence regimes, what did takers pay vs the last reliable two-sided mid?
     Signed taker cost = (fill - mid) for YES-takers, (mid - fill) for NO-takers.
     Distribution in absence vs normal regimes.
  3. SIM — had we quoted two-sided at last_reliable_mid +/- Q (Q in {5,8,12}c,
     clamped to [1,99]) ONLY during absence regimes, with fills modeled
     CONSERVATIVELY (we fill only when a real print STRICTLY crosses our quote —
     we are LAST in queue, a print AT our price does NOT fill us; same real-print
     fill model as scripts/research/mm_markout_evaluator.py), what is the
     hold-to-settlement markout PnL NET of Kalshi fee? Per (asset x Q) cell:
     n_fills, mean net PnL/contract, DAY-bootstrap CI (resample days, never rows).

KILL CRITERION (pre-registered): a cell SURVIVES iff bootstrap CI lower bound of
mean net PnL/contract > 0 AND n_fills >= 30. Anything else is dead/inconclusive.

DISCIPLINE:
  - No look-ahead: quote decisions at trade time t use only reliable book points
    with ts < t (strict). The stale "last reliable mid" IS the strategy's anchor.
  - Honest fills: real prints only, strict crossing (last-in-queue assumption).
  - All PnL net of fee (large-order amortized rate; 1-lot rounding penalty would
    only make a marginal cell worse, never better).
  - Per-day fill counts reported so thin cells are visible.
  - Each crossing print = one 1-contract fill event (continuous re-quote
    assumption); per-contract PnL is unaffected, n is what the CI sees.

DATA (all under --corpus, default layout ~/kalshi-research-data/fairvalue/):
  frames/day=*.jsonl.zst    Kalshi orderbook bronze envelopes (crypto-15M
                            pre-filtered); reconstructed via
                            early_exit_backtest.reliable_nbbo_timeline.
  trades/day=*.jsonl.zst    trade prints (yes_price_dollars, taker_side, ts_ms).
  lifecycle/                settlement labels via
                            phase1b_real_price_economics.load_determined.

Streaming is memory-bounded (path_alpha_probe.stream_windows buffering pattern:
buffer raw frames per OPEN ticker only; finalize + free when the stream clock
passes det_ts + margin). zst files are decompressed via a streaming pipe — whole
days are never resident.

Usage:
  python3 -m scripts.research.stepfour.empty_book_casino \
      --corpus ~/kalshi-research-data/fairvalue \
      [--days day=2026-05-30] [--spread-thresh 10]

Parent theory: maker-absence monopoly quoting in 15M crypto (2026-06-10).
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import random
import re
import subprocess
from collections import defaultdict
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

from scripts.research.early_exit_backtest import reliable_nbbo_timeline
from scripts.research.phase1b_real_price_economics import (
    ASSETS,
    _epoch,
    _is_crypto_15m,
)
from scripts.research.settlement_convergence_p1a import kalshi_fee_per_contract_cents
from scripts.research.zstd_stream import checked_stream_lines  # noqa: E402  (repo root added to sys.path above)

Q_GRID = (5, 8, 12)            # half-spread grid, cents
MAP_THRESHES = (10.0, 20.0)    # absence-map fixed spread thresholds (cents)
SEG_CAP_S = 60.0               # cap a reliable point's state-holding time (gap guard)
CLOSE_MARGIN_S = 180.0         # finalize a window this long after det_ts
FLUSH_EVERY = 50_000           # stream lines between finalize sweeps
BOOT_B = 1000                  # day-bootstrap resamples
MIN_FILLS = 30                 # pre-registered n floor
random.seed(20260610)


# ----- streaming I/O ---------------------------------------------------------


def _stream_lines(path: str):
    """Yield lines from .jsonl or .jsonl.zst WITHOUT materializing the file
    (unlike phase1b's _zst_lines, which buffers the whole decompressed day)."""
    # Delegates to the shared CHECKED reader (ticket 86bbvrx1t) — the previous
    # body threw away zstd's exit code, so a short read looked like a quiet day.
    yield from checked_stream_lines(path, require_nonempty=False,
                                    skip_blank=True)


def _day_of(path: str) -> Optional[str]:
    m = re.search(r"day=(\d{4}-\d{2}-\d{2})", os.path.basename(path))
    return m.group(1) if m else None


def _next_day(day: str) -> str:
    from datetime import timedelta
    d = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    return (d + timedelta(days=1)).strftime("%Y-%m-%d")


def load_trades_day(corpus: str, day: str, determined: dict) -> Dict[str, list]:
    """{ticker: [(epoch, yes_price_cents, taker_side)]} for determined crypto-15M
    tickers only, from trades/day=<day>.jsonl(.zst) if present."""
    out: Dict[str, list] = defaultdict(list)
    cands = [f"{corpus}/trades/day={day}.jsonl.zst", f"{corpus}/trades/day={day}.jsonl"]
    path = next((p for p in cands if os.path.exists(p)), None)
    if path is None:
        return out
    for line in _stream_lines(path):
        try:
            inner = json.loads(json.loads(line)["_raw"])
            msg = inner.get("msg", {})
            tk = msg.get("market_ticker", "")
            if tk not in determined:
                continue
            ts = float(msg["ts_ms"]) / 1000.0
            px = float(msg["yes_price_dollars"]) * 100.0
            side = msg.get("taker_side")
            if side not in ("yes", "no"):
                continue
            out[tk].append((ts, px, side))
        except (ValueError, KeyError, TypeError):
            continue
    return out


# ----- accumulators ----------------------------------------------------------


class AbsenceMap:
    """Time-weighted absence fractions per (asset, utc_hour)."""

    def __init__(self):
        self.cells = defaultdict(lambda: {"total": 0.0, "one_sided": 0.0,
                                          "ge10": 0.0, "ge20": 0.0})

    def add_timeline(self, asset: str, tl: list, close_ts: float):
        for i, (ts, b, a) in enumerate(tl):
            end = tl[i + 1][0] if i + 1 < len(tl) else min(close_ts, ts + SEG_CAP_S)
            dur = min(max(end - ts, 0.0), SEG_CAP_S)
            if dur <= 0:
                continue
            c = self.cells[(asset, datetime.fromtimestamp(ts, tz=timezone.utc).hour)]
            c["total"] += dur
            if b is None or a is None:
                c["one_sided"] += dur
            else:
                spr = a - b
                if spr >= MAP_THRESHES[0]:
                    c["ge10"] += dur
                if spr >= MAP_THRESHES[1]:
                    c["ge20"] += dur

    def report(self):
        print("\n=== 1. ABSENCE MAP (fraction of reliable-book TIME; per asset x UTC hour) ===")
        print(f"{'asset':>6} {'hr':>3} {'rel_min':>8} {'one_sided%':>11} "
              f"{'spr>=10c%':>10} {'spr>=20c%':>10}")
        for (asset, hr) in sorted(self.cells):
            c = self.cells[(asset, hr)]
            t = c["total"]
            print(f"{asset:>6} {hr:>3} {t/60:>8.1f} {100*c['one_sided']/t:>11.1f} "
                  f"{100*c['ge10']/t:>10.1f} {100*c['ge20']/t:>10.1f}")
        if not self.cells:
            print("  (no reliable book time observed)")


class Desperation:
    """Signed taker cost vs last reliable two-sided mid, absence vs normal.
    Histogram at 1c resolution -> exact-ish quantiles without storing rows."""

    def __init__(self):
        self.h = defaultdict(lambda: defaultdict(int))   # group -> bucket -> n
        self.s = defaultdict(lambda: {"n": 0, "sum": 0.0})
        self.no_mid = defaultdict(int)                   # prints with no prior 2-sided mid

    def add(self, group: str, cost: Optional[float]):
        if cost is None:
            self.no_mid[group] += 1
            return
        self.h[group][int(round(cost))] += 1
        st = self.s[group]
        st["n"] += 1
        st["sum"] += cost

    def _quantile(self, group: str, q: float) -> float:
        h = self.h[group]
        n = sum(h.values())
        target = q * n
        run = 0
        for k in sorted(h):
            run += h[k]
            if run >= target:
                return float(k)
        return float("nan")

    def report(self):
        print("\n=== 2. PAID-DESPERATION EVIDENCE (taker cost vs last reliable 2-sided mid, cents) ===")
        print("    cost > 0 = taker paid through the mid (our hypothetical capture)")
        print(f"{'regime':>10} {'n':>8} {'mean':>7} {'p50':>6} {'p90':>6} {'p99':>6} {'no_mid_n':>9}")
        for g in ("absence", "normal", "no_book"):
            st = self.s.get(g)
            nm = self.no_mid.get(g, 0)
            if not st or st["n"] == 0:
                if nm:
                    print(f"{g:>10} {0:>8} {'-':>7} {'-':>6} {'-':>6} {'-':>6} {nm:>9}")
                continue
            print(f"{g:>10} {st['n']:>8} {st['sum']/st['n']:>7.2f} "
                  f"{self._quantile(g, 0.50):>6.0f} {self._quantile(g, 0.90):>6.0f} "
                  f"{self._quantile(g, 0.99):>6.0f} {nm:>9}")


class Sim:
    """Two-sided quoting at last 2-sided mid +/- Q during absence regimes only.
    Fills: real prints STRICTLY crossing the quote. Hold to settlement, net fee."""

    def __init__(self):
        # (asset, Q) -> day -> [net_pnl_cents]
        self.fills = defaultdict(lambda: defaultdict(list))
        self.day_counts = defaultdict(int)    # day -> n_fills (all cells)
        self.win_counts = defaultdict(lambda: defaultdict(int))  # (asset,Q) -> ticker -> n
        self.mid_age_sum = 0.0
        self.mid_age_n = 0

    def try_fill(self, asset: str, result: str, ts: float, px: float, side: str,
                 mid: float, mid_ts: float, ticker: str = "?"):
        day = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")
        anchor = int(round(mid))
        for q in Q_GRID:
            bid = max(1, min(99, anchor - q))
            ask = max(1, min(99, anchor + q))
            if bid >= ask:
                continue
            if side == "yes" and px > ask:
                # taker lifted our YES offer: we SOLD YES at `ask`
                gross = (ask - 100.0) if result == "yes" else float(ask)
            elif side == "no" and px < bid:
                # taker hit our YES bid: we BOUGHT YES at `bid`
                gross = (100.0 - bid) if result == "yes" else -float(bid)
            else:
                continue
            price = ask if side == "yes" else bid
            net = gross - kalshi_fee_per_contract_cents(price)
            self.fills[(asset, q)][day].append(net)
            self.win_counts[(asset, q)][ticker] += 1
            self.day_counts[day] += 1
            self.mid_age_sum += ts - mid_ts
            self.mid_age_n += 1

    @staticmethod
    def _day_bootstrap_ci(by_day: Dict[str, list]) -> Tuple[float, float]:
        days = sorted(by_day)
        means = []
        for _ in range(BOOT_B):
            sample = [v for d in random.choices(days, k=len(days)) for v in by_day[d]]
            if sample:
                means.append(sum(sample) / len(sample))
        means.sort()
        if not means:
            return float("nan"), float("nan")
        lo = means[int(0.025 * (len(means) - 1))]
        hi = means[int(0.975 * (len(means) - 1))]
        return lo, hi

    def report(self):
        print(f"\n=== 3. SIM — quote mid+/-Q during absence only; real-print strict-cross "
              f"fills; settle-markout net of fee ===")
        if self.mid_age_n:
            print(f"    mean mid staleness at fill: {self.mid_age_sum/self.mid_age_n:.1f}s "
                  f"(quotes anchor to the LAST reliable 2-sided mid)")
        print(f"{'asset':>6} {'Q':>3} {'n_fills':>8} {'days':>5} {'n_win':>6} "
              f"{'top_win%':>9} {'mean_net_c':>11} {'ci_lo':>8} {'ci_hi':>8} {'verdict':>9}")
        # per-asset cells + an ALL pool per Q
        pooled = defaultdict(lambda: defaultdict(list))
        pooled_w = defaultdict(lambda: defaultdict(int))
        for (asset, q), by_day in self.fills.items():
            for d, v in by_day.items():
                pooled[q][d].extend(v)
            for w, c in self.win_counts[(asset, q)].items():
                pooled_w[q][w] += c
        rows = sorted(self.fills) + [("ALL", q) for q in Q_GRID if pooled.get(q)]
        any_row = False
        for asset, q in rows:
            by_day = pooled[q] if asset == "ALL" else self.fills[(asset, q)]
            by_win = pooled_w[q] if asset == "ALL" else self.win_counts[(asset, q)]
            vals = [v for vs in by_day.values() for v in vs]
            if not vals:
                continue
            any_row = True
            n = len(vals)
            mean = sum(vals) / n
            lo, hi = self._day_bootstrap_ci(by_day)
            nd = len(by_day)
            nw = len(by_win)
            topw = 100.0 * max(by_win.values()) / n if by_win else float("nan")
            verdict = "SURVIVES" if (lo > 0 and n >= MIN_FILLS) else "dead"
            if nd == 1:
                verdict += "*"   # single-day CI is degenerate — not trustworthy
            print(f"{asset:>6} {q:>3} {n:>8} {nd:>5} {nw:>6} {topw:>9.0f} "
                  f"{mean:>11.2f} {lo:>8.2f} {hi:>8.2f} {verdict:>9}")
        if not any_row:
            print("  (zero fills — nobody crossed a wide quote during absence regimes)")
        print("    * = 1 fill-day only: day-bootstrap CI degenerate, treat as inconclusive")
        print("\n    per-day fill counts (thinness check):")
        for d in sorted(self.day_counts):
            print(f"      {d}: {self.day_counts[d]} fills (across all asset x Q cells)")
        if not self.day_counts:
            print("      (none)")


# ----- per-window evaluation -------------------------------------------------


def process_window(tk: str, d: dict, tl: list, trades: list,
                   spread_thresh: float, amap: AbsenceMap, desp: Desperation,
                   sim: Sim, counters: dict):
    asset, result, det_ts = d["asset"], d["result"], float(d["det_ts"])
    amap.add_timeline(asset, tl, det_ts)
    if not trades:
        return
    counters["windows_with_trades"] += 1
    trades = sorted(t for t in trades if t[0] < det_ts)
    j = 0
    last_state: Optional[Tuple[Optional[float], Optional[float]]] = None
    last_mid: Optional[float] = None
    last_mid_ts = 0.0
    for ts, px, side in trades:
        # advance book pointer: STRICTLY before the print (no look-ahead)
        while j < len(tl) and tl[j][0] < ts:
            _, b, a = tl[j]
            last_state = (b, a)
            if b is not None and a is not None:
                last_mid, last_mid_ts = (b + a) / 2.0, tl[j][0]
            j += 1
        if last_state is None:
            regime = "no_book"
        else:
            b, a = last_state
            if b is None or a is None:
                regime = "absence"           # one-sided / empty side
            elif (a - b) >= spread_thresh:
                regime = "absence"           # wide = no competitive maker
            else:
                regime = "normal"
        counters[f"prints_{regime}"] += 1
        cost = None
        if last_mid is not None:
            cost = (px - last_mid) if side == "yes" else (last_mid - px)
        desp.add(regime, cost)
        if regime == "absence" and last_mid is not None:
            sim.try_fill(asset, result, ts, px, side, last_mid, last_mid_ts, ticker=tk)


# ----- streaming driver (stream_windows pattern, multi-file + zst) ----------


def run(corpus: str, days_pat: Optional[str], spread_thresh: float) -> int:
    from scripts.research.phase1b_real_price_economics import load_determined
    determined = load_determined(f"{corpus}/lifecycle", ASSETS)
    print(f"determined windows (lifecycle, all days): {len(determined)}")

    pat = days_pat if days_pat else "day=*"
    frame_files = sorted(set(glob.glob(f"{corpus}/frames/{pat}*.jsonl.zst")
                             + glob.glob(f"{corpus}/frames/{pat}*.jsonl")))
    if not frame_files:
        print(f"no frames files match {corpus}/frames/{pat}*")
        return 1
    print(f"frames files: {[os.path.basename(f) for f in frame_files]}")

    amap, desp, sim = AbsenceMap(), Desperation(), Sim()
    counters: dict = defaultdict(int)
    trades_by_ticker: Dict[str, list] = {}
    loaded_days: set = set()

    buffers: Dict[str, list] = defaultdict(list)
    done: set = set()
    n_lines = n_final = 0
    stream_ts = 0.0

    def _ensure_trades(day: Optional[str]):
        if day is None:
            return
        for dd in (day, _next_day(day)):
            if dd in loaded_days:
                continue
            loaded_days.add(dd)
            for tk, rows in load_trades_day(corpus, dd, determined).items():
                trades_by_ticker.setdefault(tk, []).extend(rows)

    def _finalize(tk: str):
        nonlocal n_final
        buf = buffers[tk]
        buf.sort(key=lambda x: x[0])
        tl = reliable_nbbo_timeline(buf)
        process_window(tk, determined[tk], tl, trades_by_ticker.pop(tk, []),
                       spread_thresh, amap, desp, sim, counters)
        n_final += 1

    for path in frame_files:
        _ensure_trades(_day_of(path))
        for line in _stream_lines(path):
            n_lines += 1
            try:
                env = json.loads(line)
                inner = json.loads(env["_raw"])
                tk = inner["msg"]["market_ticker"]
            except (ValueError, KeyError):
                continue
            d = determined.get(tk)
            if d is None or tk in done:
                counters["lines_skipped_not_determined"] += d is None
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

    print(f"\nstreamed {n_lines} frame lines; finalized {n_final} windows "
          f"({counters['windows_with_trades']} with trades)")
    print(f"prints by regime: absence={counters['prints_absence']} "
          f"normal={counters['prints_normal']} no_book={counters['prints_no_book']} "
          f"(spread_thresh={spread_thresh:.0f}c)")
    amap.report()
    desp.report()
    sim.report()
    print(f"\nKILL CRITERION: cell survives iff day-bootstrap CI lower bound > 0 "
          f"AND n_fills >= {MIN_FILLS}.")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--corpus", required=True,
                    help="corpus root with frames/ trades/ lifecycle/")
    ap.add_argument("--days", default=None,
                    help="restrict frames glob, e.g. day=2026-05-30")
    ap.add_argument("--spread-thresh", type=float, default=10.0,
                    help="spread (cents) at/above which the book counts as "
                         "maker-absent (default 10)")
    args = ap.parse_args(argv)
    return run(os.path.expanduser(args.corpus), args.days, args.spread_thresh)


if __name__ == "__main__":
    raise SystemExit(main())
