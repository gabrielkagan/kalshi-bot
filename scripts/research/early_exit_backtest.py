"""Early-exit (take-profit) backtest on crypto-15M bronze — is there edge in
buying then EXITING before close, vs the bot's historical buy-and-hold-to-close?

THEORY UNDER TEST: "buy at E (e.g. 50c), sell at X (e.g. 70c) before close." The
bot today holds every 15M position to binary settlement (0/100). Does an early
take-profit overlay ADD edge?

THE PRIOR WE MUST BEAT (why this isn't free money):
  If the YES mid is a fair martingale (efficient market), the optional-stopping
  theorem makes a first-hit take-profit EV-IDENTICAL to holding — BEFORE fees —
  and strictly WORSE after, because early exit pays a 2nd trading fee (entry +
  exit) while hold-to-close pays only the entry fee (settlement is free). So
  early-exit has real edge ONLY if the post-entry path is MEAN-REVERTING (spikes
  to X fade) by enough to clear the extra fee. Momentum paths make it lose.

STRATEGY (taker-to-taker, the conservative/honest fill model):
  entry  = first reliable tick with yes_ask <= E  -> BUY at the ask (lift offer)
  exit   = first later reliable tick with yes_bid >= X before close -> SELL at bid
  else   = HOLD to settlement (0/100)   [same downside the bot has today]
  baseline = same entry, ALWAYS hold to close.

  delta = early_exit_pnl - hold_pnl. On a TP-triggered window this reduces to
          (exit_px - exit_fee) - settle  -- the entry price cancels, so delta is
          PURELY the marginal value of the exit decision, orthogonal to entry edge.

VERDICT (pre-registered, WWJD): early-exit ADDS edge in an (E,X) cell iff the
bootstrap-CI lower bound of delta (NET of fees) > 0. We also report a ZERO-FEE
delta to separate two failure modes:
  - delta_nofee > 0 but net delta CI includes/below 0  -> path mean-reverts but
    fees eat it (a structural "no" unless fees change).
  - delta_nofee <= 0                                   -> momentum/martingale, no
    path edge exists to capture.

Reconstruction reuses kalshi_book_reconstruct's snapshot-anchored, drift-REFUSING
book (never-crossed NBBO); outcomes/universe from market_lifecycle_v2 `determined`
events (selection-bias-free: every settled window, traded or not).

Usage:
  python3 -m scripts.research.early_exit_backtest \
    --frames-file ~/kalshi-research-data/early_exit/frames_crypto.jsonl \
    --lifecycle-dir ~/kalshi-research-data/early_exit/lifecycle \
    [--entries 30,40,50,60,70] [--exits-ahead 10,20,30] [--min-runway-s 60] \
    [--per-asset] [--min-n 20]

Parent theory: buy-then-early-exit vs hold-to-close in 15M crypto (2026-05-31).
"""
from __future__ import annotations

import argparse
import json
import os
from collections import defaultdict
from typing import List, Optional, Sequence, Tuple

from scripts.research import kalshi_book_reconstruct as kbr
from scripts.research.mm_markout_evaluator import bootstrap_ci, _mean
from scripts.research.phase1b_real_price_economics import (
    _band,
    _epoch,
    _is_crypto_15m,
    kalshi_fee_per_contract_cents,
    load_determined,
    load_frames_jsonl,
)

DEFAULT_ENTRIES = (30, 40, 50, 60, 70)   # buy when yes_ask <= E (cents)
DEFAULT_EXITS_AHEAD = (10, 20, 30)        # take-profit at E + this (cents)


# ----- reliable NBBO timeline (snapshot-anchored, drift-refusing stream) -----


def reliable_nbbo_timeline(
    frames: Sequence,
    max_deltas_since_snap: int = 100000,
    max_levels_per_side: int = 220,
) -> List[Tuple[float, Optional[float], Optional[float]]]:
    """Replay frames ONCE into a stream of (recv_epoch, yes_bid_c, yes_ask_c) at
    every point where the book is snapshot-anchored AND `is_reliable` (never
    crossed). Same trust semantics as `reliable_nbbo_at`, emitted incrementally so
    a whole window costs one pass. Unanchored / drifted / crossed points are
    SKIPPED (not emitted) — the strategy can only act on prices it could trust."""
    b = kbr.KalshiBook()
    since_snap = 0
    anchored = False
    out: List[Tuple[float, Optional[float], Optional[float]]] = []
    for ts, inner in frames:
        if inner.get("type") == "orderbook_snapshot":
            b = kbr.KalshiBook()
            b.apply_frame(inner)
            since_snap = 0
            anchored = True
        else:
            b.apply_frame(inner)
            since_snap += 1
        if not anchored or since_snap > max_deltas_since_snap:
            continue
        if not b.is_reliable(max_levels_per_side):
            continue
        out.append((ts, b.best_yes_bid_cents(), b.best_yes_ask_cents()))
    return out


# ----- single-window simulation --------------------------------------------


def simulate_window(
    timeline: Sequence, close_ts: float, result: str, E: float, X: float,
    min_runway_s: float = 0.0,
) -> Optional[dict]:
    """Buy at first reliable yes_ask <= E (with >= min_runway_s to close), then
    sell at first later yes_bid >= X before close, else hold to settle. Returns
    None if never entered. result is 'yes'/'no'."""
    settle = 100.0 if result == "yes" else 0.0
    entry_px = entry_ts = None
    for ts, bid, ask in timeline:
        if ts > close_ts:
            break
        if ask is not None and ask <= E and (close_ts - ts) >= min_runway_s:
            entry_px, entry_ts = ask, ts
            break
    if entry_px is None:
        return None

    exit_px = exit_ts = None
    for ts, bid, ask in timeline:
        if ts <= entry_ts:
            continue
        if ts > close_ts:
            break
        if bid is not None and bid >= X:
            exit_px, exit_ts = bid, ts
            break

    fee_e = kalshi_fee_per_contract_cents(entry_px)
    hold_pnl = settle - entry_px - fee_e
    hold_pnl_nf = settle - entry_px
    if exit_px is not None:
        fee_x = kalshi_fee_per_contract_cents(exit_px)
        exit_pnl = exit_px - entry_px - fee_e - fee_x
        exit_pnl_nf = exit_px - entry_px
    else:
        exit_pnl = hold_pnl
        exit_pnl_nf = hold_pnl_nf
    return {
        "entered": True,
        "tp_hit": exit_px is not None,
        "entry_px": entry_px,
        "exit_px": exit_px,
        "runway_s": close_ts - entry_ts,
        "won": result == "yes",
        "hold_pnl": hold_pnl,
        "exit_pnl": exit_pnl,
        "delta": exit_pnl - hold_pnl,            # net of fees
        "delta_nofee": exit_pnl_nf - hold_pnl_nf,
    }


# ----- grid + reporting -----------------------------------------------------


def _new_cell():
    return {"hold": [], "exit": [], "delta": [], "delta_nf": [], "tp": 0,
            "win": 0, "runway": [], "entry_px": []}


def run_grid(windows: dict, frames: dict, entries, exits_ahead,
             min_runway_s: float, per_asset: bool) -> dict:
    """windows: {ticker: {asset,result,det_ts,...}}. Returns {key: cell} where key
    is (E, X) or (asset, E, X) when per_asset."""
    cells = defaultdict(_new_cell)
    for tk, d in windows.items():
        fr = frames.get(tk)
        if not fr:
            continue
        close_ts = float(d["det_ts"])
        tl = reliable_nbbo_timeline(fr)
        if not tl:
            continue
        for E in entries:
            for ahead in exits_ahead:
                X = E + ahead
                if X >= 100:
                    continue
                r = simulate_window(tl, close_ts, d["result"], float(E), float(X),
                                    min_runway_s)
                if r is None:
                    continue
                keys = [(E, X)]
                if per_asset:
                    keys.append((d["asset"], E, X))
                for k in keys:
                    c = cells[k]
                    c["hold"].append(r["hold_pnl"])
                    c["exit"].append(r["exit_pnl"])
                    c["delta"].append(r["delta"])
                    c["delta_nf"].append(r["delta_nofee"])
                    c["runway"].append(r["runway_s"])
                    c["entry_px"].append(r["entry_px"])
                    c["tp"] += int(r["tp_hit"])
                    c["win"] += int(r["won"])
    return cells


def _accumulate(cells, keys, r):
    for k in keys:
        c = cells[k]
        c["hold"].append(r["hold_pnl"])
        c["exit"].append(r["exit_pnl"])
        c["delta"].append(r["delta"])
        c["delta_nf"].append(r["delta_nofee"])
        c["runway"].append(r["runway_s"])
        c["entry_px"].append(r["entry_px"])
        c["tp"] += int(r["tp_hit"])
        c["win"] += int(r["won"])


def _finalize_ticker(buf, d, entries, exits_ahead, min_runway_s, per_asset, cells):
    """Build the reliable timeline for one ticker from its buffered frames, run the
    (E,X) grid, accumulate into cells. Returns True if any (E,X) was entered."""
    buf.sort(key=lambda x: x[0])  # ensure ts-ascending (cross-partition safety)
    tl = reliable_nbbo_timeline(buf)
    if not tl:
        return False
    close_ts = float(d["det_ts"])
    entered_any = False
    for E in entries:
        for ahead in exits_ahead:
            X = E + ahead
            if X >= 100:
                continue
            r = simulate_window(tl, close_ts, d["result"], float(E), float(X),
                                min_runway_s)
            if r is None:
                continue
            entered_any = True
            keys = [(E, X)]
            if per_asset:
                keys.append((d["asset"], E, X))
            _accumulate(cells, keys, r)
    return entered_any


def run_stream(frames_path: str, determined: dict, entries, exits_ahead,
               min_runway_s: float, per_asset: bool,
               flush_every: int = 50000, close_margin_s: float = 180.0) -> Tuple[dict, int, int]:
    """Memory-bounded driver: stream the crypto-15M JSONL in arrival order,
    buffering raw frames ONLY for currently-open windows. A ticker is finalized
    (timeline built, grid run, buffer freed) once the stream clock passes its
    close_ts + margin, so peak RAM ~ the active ~15-30min slice, not the whole
    corpus. Returns (cells, n_covered, n_lines)."""
    cells = defaultdict(_new_cell)
    buffers: dict = defaultdict(list)        # ticker -> [(ts, inner)]
    done: set = set()                         # tickers already finalized
    n_covered = n_lines = 0
    stream_ts = 0.0
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
                cutoff = stream_ts - close_margin_s
                for done_tk in [t for t, dd in
                                ((t, determined[t]) for t in list(buffers))
                                if float(dd["det_ts"]) < cutoff]:
                    if _finalize_ticker(buffers[done_tk], determined[done_tk],
                                        entries, exits_ahead, min_runway_s,
                                        per_asset, cells):
                        n_covered += 1
                    done.add(done_tk)
                    del buffers[done_tk]
    # EOF: finalize whatever is left
    for tk in list(buffers):
        if _finalize_ticker(buffers[tk], determined[tk], entries, exits_ahead,
                            min_runway_s, per_asset, cells):
            n_covered += 1
        del buffers[tk]
    return cells, n_covered, n_lines


def _verdict(delta_lo: float, delta_mean: float, delta_nf_mean: float) -> str:
    if delta_lo > 0:
        return "ADDS-EDGE"
    if delta_nf_mean > 0:
        return "fee-eaten"   # path mean-reverts but the 2nd fee kills it
    return "no-edge"          # momentum / martingale: nothing to capture


def report(cells: dict, min_n: int, per_asset: bool) -> None:
    simple = {k: v for k, v in cells.items() if len(k) == 2}
    print(f"\n{'E':>4}{'X':>4}{'nEnt':>6}{'tp%':>6}{'win%':>6}{'avgEnt':>7}"
          f"{'rwy_s':>7}{'holdEV':>8}{'exitEV':>8}{'dEV':>8}{'dCI':>16}"
          f"{'dEV_nofee':>11}{'VERDICT':>11}")
    for E, X in sorted(simple):
        c = simple[(E, X)]
        n = len(c["delta"])
        if n < min_n:
            continue
        lo, hi = bootstrap_ci(c["delta"])
        dmean = _mean(c["delta"])
        dnf = _mean(c["delta_nf"])
        print(f"{E:>4}{X:>4}{n:>6}{100*c['tp']/n:>6.1f}{100*c['win']/n:>6.1f}"
              f"{_mean(c['entry_px']):>7.1f}{_mean(c['runway']):>7.0f}"
              f"{_mean(c['hold']):>+8.2f}{_mean(c['exit']):>+8.2f}{dmean:>+8.2f}"
              f"{f'[{lo:+.2f},{hi:+.2f}]':>16}{dnf:>+11.2f}{_verdict(lo, dmean, dnf):>11}")

    survivors = []
    for E, X in sorted(simple):
        c = simple[(E, X)]
        if len(c["delta"]) < min_n:
            continue
        lo, _ = bootstrap_ci(c["delta"])
        if lo > 0:
            survivors.append((E, X, _mean(c["delta"]), lo, len(c["delta"])))
    print()
    if survivors:
        print(f"🟢 {len(survivors)} (E,X) cell(s) where early-exit delta CI-lo > 0 "
              f"(net of fees) — CANDIDATE, dispatch adversarial review before belief:")
        for E, X, dm, lo, n in survivors:
            print(f"   E={E} X={X}: deltaEV={dm:+.2f}c CI-lo={lo:+.2f} n={n}")
    else:
        print("⚪ No (E,X) cell has early-exit delta CI-lo > 0 net of fees. Under the "
              "tested grid, early-exit does NOT beat hold-to-close (efficient / "
              "fee-dominated / momentum). See dEV_nofee column for whether the raw "
              "path even mean-reverts before fees.")

    if per_asset:
        print(f"\n--- per-asset (E,X) cells (n>={min_n}) ---")
        print(f"{'asset':>6}{'E':>4}{'X':>4}{'nEnt':>6}{'tp%':>6}{'dEV':>8}"
              f"{'dCI':>16}{'dEV_nofee':>11}{'VERDICT':>11}")
        for key in sorted(k for k in cells if len(k) == 3):
            a, E, X = key
            c = cells[key]
            n = len(c["delta"])
            if n < min_n:
                continue
            lo, hi = bootstrap_ci(c["delta"])
            dmean, dnf = _mean(c["delta"]), _mean(c["delta_nf"])
            print(f"{a:>6}{E:>4}{X:>4}{n:>6}{100*c['tp']/n:>6.1f}{dmean:>+8.2f}"
                  f"{f'[{lo:+.2f},{hi:+.2f}]':>16}{dnf:>+11.2f}"
                  f"{_verdict(lo, dmean, dnf):>11}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--frames-file", required=True,
                    help="crypto-15M-prefiltered bronze JSONL (orderbook_delta)")
    ap.add_argument("--lifecycle-dir", required=True,
                    help="dir of market_lifecycle_v2 .zst (outcomes + close ts)")
    ap.add_argument("--assets", default="BTC,ETH,SOL,XRP,HYPE,DOGE,BNB,ADA,BCH")
    ap.add_argument("--entries", default=",".join(str(x) for x in DEFAULT_ENTRIES))
    ap.add_argument("--exits-ahead", default=",".join(str(x) for x in DEFAULT_EXITS_AHEAD))
    ap.add_argument("--min-runway-s", type=float, default=60.0,
                    help="require >= this many seconds to close at entry (default 60)")
    ap.add_argument("--min-n", type=int, default=20)
    ap.add_argument("--per-asset", action="store_true")
    args = ap.parse_args(argv)

    assets = tuple(a.strip().upper() for a in args.assets.split(","))
    entries = [int(x) for x in args.entries.split(",")]
    exits_ahead = [int(x) for x in args.exits_ahead.split(",")]

    determined = load_determined(os.path.expanduser(args.lifecycle_dir), assets)
    print(f"determined crypto-15M windows: {len(determined)}  | entries={entries} "
          f"exits_ahead={exits_ahead} min_runway={args.min_runway_s:.0f}s  "
          f"(streaming, memory-bounded)")
    cells, n_covered, n_lines = run_stream(
        os.path.expanduser(args.frames_file), determined, entries, exits_ahead,
        args.min_runway_s, args.per_asset)
    print(f"streamed {n_lines} frame lines; covered windows (entered >=1 (E,X)): "
          f"{n_covered}")
    report(cells, args.min_n, args.per_asset)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
