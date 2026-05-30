"""Phase 1b — INFORMATION-edge test: does live spot at T-15s beat Kalshi's price?

The original thesis, finally tested on real data. Every prior test was EXECUTION
(buy at the market's price). This asks the different question: does a live-spot
signal at the decision time RANK the settlement outcome better than Kalshi's own
implied probability? If spot leads the laggy CF RTI (the documented 8-14s lag),
spot at T-15s should out-predict the Kalshi price → an information edge (bet only
when our signal materially disagrees with the market).

Compares, per covered crypto-15M window at T-Xs (no look-ahead):
  - p_mkt  = Kalshi coherent NBBO mid / 100 (the market's implied P(yes)).
  - spot   = Coinbase last price at-or-before T-Xs (a ~2-3bps proxy for the
             multi-venue RTI; refine with all 4 venues only if this shows signal).
  - margin = (spot - strike) / strike  (our spot-implied direction/strength).
Metrics: AUC(p_mkt) vs AUC(margin) against the outcome; head-to-head accuracy on
windows where the two DISAGREE on side; market Brier for reference.

Usage:
  python3 scripts/research/phase1b_information_edge.py \
    --frames-file /tmp/crypto15m.jsonl --spot-file /tmp/coinbase_spot.jsonl \
    --outcomes-db /tmp/state.db

Parent: kb/decisions/settlement-convergence-worklist.md (information-edge corner)
"""

from __future__ import annotations

import argparse
from collections import defaultdict
from typing import Optional

from scripts.research import kalshi_book_reconstruct as kbr
from scripts.research.phase1b_real_price_economics import (
    ASSETS, _epoch, _is_crypto_15m, close_epoch_from_ticker,
    kalshi_fee_per_contract_cents, load_frames_jsonl, load_outcomes_db,
    realized_pnl_cents,
)

DECISION_OFFSETS_S = (15, 30, 60)
# Kalshi-quote age cap. DEFAULT OFF (1e9): a "stale" quote is a RESTING limit
# order that may still be fillable — so instead of discarding it, we record its
# DEPTH + age and ask whether the mispriced order is actually pick-off-able.
import os
MAX_KALSHI_STALENESS_S = float(os.environ.get("MAX_KALSHI_STALENESS_S", "1e9"))


# ----- methodology-critical helpers (TDD-pinned) --------------------------


def spot_at_or_before(series, t: float, max_staleness_s: Optional[float] = None):
    """series = ascending [(epoch, price)]; return the most recent price at/before
    t (NO look-ahead). None if t precedes the series or the nearest tick is older
    than max_staleness_s."""
    lo, hi = 0, len(series)
    while lo < hi:
        mid = (lo + hi) // 2
        if series[mid][0] <= t:
            lo = mid + 1
        else:
            hi = mid
    i = lo - 1
    if i < 0:
        return None
    e, p = series[i]
    if max_staleness_s is not None and (t - e) > max_staleness_s:
        return None
    return p


def auc(scores, labels) -> float:
    """Mann-Whitney AUC with mid-rank ties. O(n^2) — fine for ~hundreds."""
    pos = [s for s, l in zip(scores, labels) if l == 1]
    neg = [s for s, l in zip(scores, labels) if l == 0]
    if not pos or not neg:
        return float("nan")
    wins = 0.0
    for p in pos:
        for n in neg:
            if p > n:
                wins += 1.0
            elif p == n:
                wins += 0.5
    return wins / (len(pos) * len(neg))


def brier(probs, labels) -> float:
    return sum((p - l) ** 2 for p, l in zip(probs, labels)) / len(probs)


# ----- spot loader (fast string parse + 1s downsample) --------------------

_T = '"_wire_recv_ts":"'
_P = 'product_id\\":\\"'
_PR = 'price\\":\\"'


def load_spot(path: str, assets) -> dict:
    """{asset: ascending [(epoch_sec, last_price)]} from the grep-prefiltered
    coinbase ticker JSONL, downsampled to 1 tick/sec (last wins). String-parsed
    (no json.loads) for speed over ~10M lines."""
    buf: dict[str, dict] = {a: {} for a in assets}
    with open(path) as fh:
        for line in fh:
            try:
                i = line.index(_T) + len(_T)
                ts = line[i:line.index('"', i)]
                j = line.index(_P) + len(_P)
                prod = line[j:line.index("-USD", j)]
                if prod not in buf:
                    continue
                k = line.index(_PR, j) + len(_PR)
                price = float(line[k:line.index('\\"', k)])
            except (ValueError, IndexError):
                continue
            buf[prod][int(_epoch(ts))] = price
    return {a: sorted(d.items()) for a, d in buf.items()}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--frames-file", required=True, help="kalshi crypto-15M frames JSONL")
    ap.add_argument("--spot-file", required=True, help="coinbase ticker JSONL")
    ap.add_argument("--outcomes-db", required=True)
    ap.add_argument("--assets", default=",".join(ASSETS))
    args = ap.parse_args(argv)
    assets = tuple(a.strip().upper() for a in args.assets.split(","))

    frames = load_frames_jsonl(args.frames_file)
    outcomes = load_outcomes_db(args.outcomes_db, set(frames))
    windows = {tk: {"asset": _is_crypto_15m(tk), "result": o["result"],
                    "strike": o["strike"], "det_ts": close_epoch_from_ticker(tk)}
               for tk, o in outcomes.items() if _is_crypto_15m(tk) in assets}
    spot = load_spot(args.spot_file, assets)
    print(f"windows: {len(windows)}  spot assets: "
          f"{ {a: len(spot[a]) for a in assets} }")

    for off in DECISION_OFFSETS_S:
        rows = []  # (asset, p_mkt, margin, outcome)
        for tk, d in windows.items():
            fr = frames.get(tk)
            if not fr:
                continue
            cutoff = d["det_ts"] - off
            ts_le = [ts for ts, inner in fr if ts <= cutoff]
            has_snap = any(inner.get("type") == "orderbook_snapshot"
                           for ts, inner in fr if ts <= cutoff)
            if not ts_le or not has_snap:
                continue
            # STALE-QUOTE GUARD: if the Kalshi book hasn't updated near the
            # decision, its quote is stale and "spot disagrees" is an artifact
            # (you couldn't fill at a stale price either). Require a fresh quote.
            age = cutoff - max(ts_le)
            if age > MAX_KALSHI_STALENESS_S:
                continue
            book, _ = kbr.book_at(fr, cutoff)
            bid, ask = book.best_yes_bid_cents(), book.best_yes_ask_cents()
            if bid is None or ask is None or not (bid <= ask):
                continue
            ask_depth = book.best_yes_ask_depth() or 0.0   # contracts to BUY YES
            bid_depth = book.best_yes_bid_depth() or 0.0   # contracts to BUY NO (sell YES)
            # persistence: re-quote 1s later — would the order still be liftable?
            book1, _ = kbr.book_at(fr, cutoff + 1.0)
            ask_1s = book1.best_yes_ask_cents()
            bid_1s = book1.best_yes_bid_cents()
            sp = spot_at_or_before(spot.get(d["asset"], []), cutoff, max_staleness_s=60)
            if sp is None or not d["strike"]:
                continue
            p_mkt = (bid + ask) / 200.0
            margin = (sp - d["strike"]) / d["strike"]
            rows.append((d["asset"], p_mkt, margin, int(d["result"] == "yes"),
                         bid, ask, ask_depth, bid_depth, age, ask_1s, bid_1s))

        if len(rows) < 20:
            print(f"\nT-{off}s: only {len(rows)} rows — skip")
            continue
        labels = [r[3] for r in rows]
        pm = [r[1] for r in rows]
        sm = [r[2] for r in rows]
        a_mkt, a_spot = auc(pm, labels), auc(sm, labels)
        print(f"\n=== T-{off}s ===  n={len(rows)}  base yes-rate={sum(labels)/len(labels):.3f}")
        print(f"  AUC  market-price = {a_mkt:.4f}   spot-margin = {a_spot:.4f}   "
              f"({'spot BEATS market' if a_spot > a_mkt + 0.005 else 'no better'})")
        print(f"  market Brier = {brier(pm, labels):.4f}")
        # R1 fix: the "edge" was in frozen-book ghosts (76% had zero feed updates
        # in the final ~2min) + near-strike coin-flips. Re-measure on the TRADEABLE
        # subset only: FRESH quote (book actively updating, age<=15s) + REAL signal
        # (|margin|>=5bps). If nothing survives there, the edge was the artifact.
        FRESH_S, MARGIN_BPS = 15.0, 5.0

        def _subset(rows, fresh, marg):
            mr = sr = ntr = win = 0
            pnl = pxs = 0.0
            for asset, p_mkt, margin, y, bid, ask, ad, bd, age, a1, b1 in rows:
                if (p_mkt > 0.5) == (margin > 0):
                    continue
                if fresh and age > FRESH_S:
                    continue
                if marg and abs(margin) * 1e4 < MARGIN_BPS:
                    continue
                mr += int(int(p_mkt > 0.5) == y)
                sr += int(int(margin > 0) == y)
                price, won = (ask, y == 1) if margin > 0 else (100 - bid, y == 0)
                if not (0 < price < 100):
                    continue
                pnl += realized_pnl_cents(price, won) - kalshi_fee_per_contract_cents(price)
                pxs += price; ntr += 1; win += int(won)
            return mr, sr, ntr, win, pnl, pxs

        print("  TRADEABLE-SUBSET re-measurement (R1: drop frozen ghosts + near-strike coin-flips):")
        print(f"    {'subset':>24}{'disag':>7}{'spotR':>7}{'mktR':>6}{'trades':>8}{'win%':>7}{'avgPx':>7}{'netEV':>8}")
        for name, fresh, marg in [("ALL", False, False),
                                  ("FRESH (age<=15s)", True, False),
                                  ("FRESH + |margin|>=5bps", True, True)]:
            mr, sr, ntr, win, pnl, pxs = _subset(rows, fresh, marg)
            wr = f"{100*win/ntr:.1f}" if ntr else "-"
            px = f"{pxs/ntr:.1f}" if ntr else "-"
            ev = f"{pnl/ntr:+.2f}" if ntr else "-"
            print(f"    {name:>24}{mr+sr:>7}{sr:>7}{mr:>6}{ntr:>8}{wr:>7}{px:>7}{ev:>8}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
