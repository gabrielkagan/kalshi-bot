"""Path-alpha probe — a reusable battery for hunting intra-window edge in a clean
bronze corpus. ONE memory-bounded streaming pass reconstructs each window's
reliable NBBO timeline and feeds it to several independent LENSES, each of which
asks a different "is there edge here?" question. Built from the 2026-05-31
early-exit investigation (which found the 15M crypto market efficient at the taker
level); kept so we can re-run the same rigorous battery as clean data accrues or
on a new asset/venue/window WITHOUT re-deriving the methodology.

LENSES (each is a known way a naive "I see a pattern" belief turns out false):
  coverage  — classify EVERY determined window: no-frames / no-reliable-book /
              never-touched-price / usable. Guards against the reliability filter
              silently dropping the volatile regime where edge would live.
  calib     — point-in-time (NO look-ahead) yes_ask bucket -> actual settleYES%.
              Calibrated => efficient. Systematic gap => mispricing a buy+HOLD
              could capture. (Use a FIXED offset; "min over the path" is look-ahead.)
  bandev    — buy YES @ ask & HOLD to settle at ONE decision offset, ONE obs per
              window, net-of-fee mean PnL per band with BOOTSTRAP CI. The CI is
              what kills pseudo-replication false positives (e.g. the same windows
              sampled at several offsets looking like independent confirmation).
  momentum  — forward 60s mid-move conditioned on prior 60s mid-move. Tests whether
              intra-window drift is TRADEABLE (autocorrelation > round-trip cost) or
              just an efficient random walk whose 86/13 settle split is an endpoint
              artifact. Compare the signal to the measured spread + fees.
  earlyexit — defers to early_exit_backtest (take-profit vs hold-to-close grid).

WWJD discipline baked in: every economic claim is net-of-fee with a bootstrap CI;
a lens reports "flat" unless the CI clears zero. Reproduce-a-2nd-way before belief.

Usage:
  python3 -m scripts.research.path_alpha_probe \
    --frames-file ~/kalshi-research-data/early_exit/frames_crypto.jsonl \
    --lifecycle-dir ~/kalshi-research-data/early_exit/lifecycle \
    [--lenses coverage,calib,bandev,momentum] [--decision-offset 180] \
    [--calib-offsets 720,360,180,60] [--assets BTC,ETH,...]

Sibling: early_exit_backtest.py (take-profit grid + the streaming primitives reused
here). Pull helper: ~/kalshi-research-data/early_exit/pull.sh (resumable rclone).
"""
from __future__ import annotations

import argparse
import json
import os
from collections import defaultdict

from scripts.research.early_exit_backtest import reliable_nbbo_timeline
from scripts.research.mm_markout_evaluator import bootstrap_ci, _mean
from scripts.research.phase1b_real_price_economics import (
    _epoch, kalshi_fee_per_contract_cents, load_determined,
)

ALL_ASSETS = ("BTC", "ETH", "SOL", "XRP", "HYPE", "DOGE", "BNB", "ADA", "BCH")


# ----- timeline helpers (point reads off a reliable timeline) ---------------


def _ask_at(tl, t):
    a = None
    for ts, b, k in tl:
        if ts > t:
            break
        if k is not None:
            a = k
    return a


def _mid_at(tl, t):
    m = None
    for ts, b, k in tl:
        if ts > t:
            break
        if b is not None and k is not None:
            m = (b + k) / 2.0
    return m


def _band(p):
    return int(p // 10 * 10)


# ----- the memory-bounded streaming driver (the reusable core) --------------


def stream_windows(frames_path, determined, on_window,
                   flush_every=50000, close_margin_s=180.0):
    """Stream a crypto-prefiltered bronze JSONL once, buffering raw frames ONLY for
    currently-open windows; when the stream clock passes a window's close+margin,
    build its reliable timeline and hand (ticker, determined_row, timeline) to
    `on_window`, then free it. Peak RAM ~ the active ~15-30min slice, not the whole
    corpus. Returns (n_lines, n_finalized)."""
    buffers = defaultdict(list)
    done = set()
    n_lines = n_final = 0
    stream_ts = 0.0

    def _finalize(tk):
        nonlocal n_final
        buf = buffers[tk]
        buf.sort(key=lambda x: x[0])
        tl = reliable_nbbo_timeline(buf)
        on_window(tk, determined[tk], tl)
        n_final += 1

    with open(frames_path) as fh:
        for line in fh:
            if not line.strip():
                continue
            n_lines += 1
            try:
                env = json.loads(line)
                inner = json.loads(env["_raw"])
                tk = inner["msg"]["market_ticker"]
            except (ValueError, KeyError):
                continue
            d = determined.get(tk)
            if d is None or tk in done:
                continue
            ts = _epoch(env["_wire_recv_ts"])
            stream_ts = ts if ts > stream_ts else stream_ts
            buffers[tk].append((ts, inner))
            if n_lines % flush_every == 0:
                cut = stream_ts - close_margin_s
                for t in [t for t in list(buffers)
                          if float(determined[t]["det_ts"]) < cut]:
                    _finalize(t)
                    done.add(t)
                    del buffers[t]
    for t in list(buffers):
        _finalize(t)
    return n_lines, n_final


# ----- lenses: each = (collector closure over per-window timeline, reporter) -


def lens_coverage():
    by = defaultdict(lambda: {"n": 0, "yes": 0})

    def collect(tk, d, tl):
        won = int(d["result"] == "yes")
        if not tl:
            cls = "no_reliable_book"
        else:
            asks = [a for _, _, a in tl if a is not None]
            cls = "usable" if (asks and min(asks) < 100) else "never_priced"
        by[cls]["n"] += 1
        by[cls]["yes"] += won

    def report():
        print("\n=== COVERAGE (every determined window classified) ===")
        for c in sorted(by):
            s = by[c]
            print(f"  {c:18s} n={s['n']:4d}  settleYES={100*s['yes']/max(1,s['n']):5.1f}%")
        print("  -> a large/odd-settle 'no_reliable_book' bucket = possible filter bias")
    return collect, report


def lens_calib(offsets):
    cal = defaultdict(lambda: defaultdict(lambda: {"n": 0, "yes": 0, "px": 0.0}))

    def collect(tk, d, tl):
        if not tl:
            return
        close = float(d["det_ts"])
        won = int(d["result"] == "yes")
        for off in offsets:
            a = _ask_at(tl, close - off)
            if a is None or not (0 < a < 100):
                continue
            c = cal[off][_band(a)]
            c["n"] += 1
            c["yes"] += won
            c["px"] += a

    def report():
        print("\n=== POINT-IN-TIME CALIBRATION (yes_ask bucket -> settleYES%) ===")
        print("   buyHoldEV<<0 = overpriced; >>0 = underpriced (efficient if ~0)")
        for off in offsets:
            print(f"  T-{off//60}min:")
            print(f"    {'band':>8}{'n':>5}{'avgAsk':>8}{'settleYES%':>11}{'buyHoldEV':>11}")
            for b in sorted(cal[off]):
                c = cal[off][b]
                if c["n"] < 8:
                    continue
                avg = c["px"] / c["n"]
                sy = 100 * c["yes"] / c["n"]
                print(f"    {b:2d}-{b+9:2d}c{c['n']:>5}{avg:>8.1f}{sy:>11.1f}{sy-avg:>+11.2f}")
    return collect, report


def lens_bandev(offset):
    band = defaultdict(list)

    def collect(tk, d, tl):
        if not tl:
            return
        a = _ask_at(tl, float(d["det_ts"]) - offset)
        if a is None or not (0 < a < 100):
            return
        settle = 100.0 if d["result"] == "yes" else 0.0
        band[_band(a)].append(settle - a - kalshi_fee_per_contract_cents(a))

    def report():
        print(f"\n=== BUY @ ask & HOLD, decision T-{offset//60}min, net-of-fee, 1 obs/window ===")
        print(f"{'band':>8}{'n':>5}{'meanPnL':>9}{'95% CI':>18}{'verdict':>9}")
        for b in sorted(band):
            xs = band[b]
            if len(xs) < 10:
                continue
            lo, hi = bootstrap_ci(xs)
            v = "+EDGE" if lo > 0 else ("-EDGE" if hi < 0 else "flat")
            print(f"{b:2d}-{b+9:2d}c{len(xs):>5}{_mean(xs):>+9.2f}"
                  f"{f'[{lo:+.2f},{hi:+.2f}]':>18}{v:>9}")
        print("  -> CI straddling 0 = no edge; this is what kills pseudo-replication")
    return collect, report


def lens_momentum(offsets):
    fwd = defaultdict(list)
    spreads = []

    def collect(tk, d, tl):
        if not tl:
            return
        close = float(d["det_ts"])
        for t in offsets:
            mp = _mid_at(tl, close - t - 60)
            mn = _mid_at(tl, close - t)
            mf = _mid_at(tl, close - t + 60)
            an = _ask_at(tl, close - t)
            if None in (mp, mn, mf):
                continue
            sign = "up" if mn - mp > 1 else ("down" if mn - mp < -1 else "flat")
            fwd[sign].append(mf - mn)

    def report():
        print("\n=== MOMENTUM TRADEABILITY: forward 60s mid-move | prior 60s move ===")
        print("   tradeable only if |fwd| > round-trip cost (~spread + ~3c fees)")
        print(f"{'prior':>6}{'n':>6}{'fwd60s':>10}{'95% CI':>18}")
        for s in ("up", "flat", "down"):
            xs = fwd.get(s, [])
            if not xs:
                continue
            lo, hi = bootstrap_ci(xs)
            print(f"{s:>6}{len(xs):>6}{_mean(xs):>+10.2f}{f'[{lo:+.2f},{hi:+.2f}]':>18}")
    return collect, report


LENSES = {
    "coverage": lambda a: lens_coverage(),
    "calib": lambda a: lens_calib([int(x) for x in a.calib_offsets.split(",")]),
    "bandev": lambda a: lens_bandev(a.decision_offset),
    "momentum": lambda a: lens_momentum([int(x) for x in a.calib_offsets.split(",")
                                         if 60 <= int(x) <= 700]),
}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--frames-file", required=True)
    ap.add_argument("--lifecycle-dir", required=True)
    ap.add_argument("--assets", default=",".join(ALL_ASSETS))
    ap.add_argument("--lenses", default="coverage,calib,bandev,momentum")
    ap.add_argument("--decision-offset", type=int, default=180,
                    help="seconds before close for the bandev decision (default 180)")
    ap.add_argument("--calib-offsets", default="720,360,180,60")
    args = ap.parse_args(argv)

    assets = tuple(x.strip().upper() for x in args.assets.split(","))
    determined = load_determined(os.path.expanduser(args.lifecycle_dir), assets)
    names = [x.strip() for x in args.lenses.split(",") if x.strip() in LENSES]
    built = [(n, LENSES[n](args)) for n in names]
    print(f"determined windows: {len(determined)}  lenses: {names}  (1 streaming pass)")

    def on_window(tk, d, tl):
        for _, (collect, _r) in built:
            collect(tk, d, tl)

    n_lines, n_final = stream_windows(os.path.expanduser(args.frames_file),
                                      determined, on_window)
    print(f"streamed {n_lines} lines; finalized {n_final} windows")
    for _, (_c, report) in built:
        report()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
