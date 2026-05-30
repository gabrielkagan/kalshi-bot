"""Phase 1b — RETAIL-FLOW edge test: are small aggressive takers systematically -EV?

The reframe (operator, 2026-05-30): the market PRICE is set by MMs/quants and is
efficient — you can't out-forecast it (proven 6 ways). But the FLOW isn't
efficient. The edge is being on the other side of dumb retail orders. This tests
the most direct version: do SMALL (retail) aggressive trades LOSE money at the
prices they pay? If yes, the maker on the other side collects it — and the edge
exists (capture mechanics are the next question).

For each crypto-15M trade (`kalshi_ws/trade` bronze): price + size (`count_fp`) +
aggressor (`taker_side`). Realized taker PnL = the taker bought their side at the
trade price and held to settlement. Aggregate by SIZE (retail = small) × price ×
time-to-close. Taker PnL < 0  ==  maker (you) PnL > 0.

Usage:
  python3 scripts/research/phase1b_retail_flow.py --trades-file /tmp/trades15m.jsonl --outcomes-db /tmp/state.db

Parent: kb/decisions/settlement-convergence-worklist.md (retail-flow corner)
"""

from __future__ import annotations

import argparse
import json
from collections import defaultdict

from scripts.research.phase1b_real_price_economics import (
    ASSETS, _is_crypto_15m, close_epoch_from_ticker, load_outcomes_db,
)


def taker_pnl_cents(yes_c: float, no_c: float, taker_side: str, result: str) -> float:
    """Realized PnL (cents/contract) of the AGGRESSOR: they bought their side at
    the trade price and held to settlement. Maker PnL = -this."""
    if taker_side == "yes":
        return (100.0 - yes_c) if result == "yes" else -float(yes_c)
    return (100.0 - no_c) if result == "no" else -float(no_c)


def parse_trade(line: str):
    try:
        env = json.loads(line)
        msg = json.loads(env["_raw"])["msg"]
    except (ValueError, KeyError):
        return None
    tk = msg.get("market_ticker", "")
    a = _is_crypto_15m(tk)
    if not a:
        return None
    try:
        return {"ticker": tk, "asset": a, "taker_side": msg["taker_side"],
                "yes_c": float(msg["yes_price_dollars"]) * 100,
                "no_c": float(msg["no_price_dollars"]) * 100,
                "count": float(msg["count_fp"]), "ts": float(msg["ts"])}
    except (KeyError, ValueError):
        return None


def load_trades(path: str) -> list:
    out = []
    with open(path) as fh:
        for line in fh:
            if not line.strip():
                continue
            t = parse_trade(line)
            if t:
                out.append(t)
    return out


SBKT = [(0, 2, "1"), (2, 6, "2-5"), (6, 21, "6-20"), (21, 101, "21-100"), (101, 1e18, "100+")]
TBKT = [(0, 60, "<=60s"), (60, 300, "1-5min"), (300, 1e18, ">5min")]
PBKT = [(0, 10, "1-9c"), (10, 30, "10-29c"), (30, 50, "30-49c"),
        (50, 70, "50-69c"), (70, 90, "70-89c"), (90, 100, "90-99c")]


def _report(title, agg, keys):
    print(f"\n  {title}")
    print(f"    {'bucket':>10}{'trades':>8}{'contracts':>11}{'takerEV/ct':>12}{'taker$':>12}{'win%':>7}")
    for _, _, l in keys:
        a = agg.get(l)
        if not a or not a["n"]:
            continue
        print(f"    {l:>10}{a['n']:>8}{a['ct']:>11.0f}{a['pnl']/a['ct']:>+12.2f}"
              f"{a['pnl']/100:>+11.2f}${100*a['win']/a['n']:>7.1f}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--trades-file", required=True)
    ap.add_argument("--outcomes-db", required=True)
    args = ap.parse_args(argv)

    trades = load_trades(args.trades_file)
    outcomes = load_outcomes_db(args.outcomes_db, {t["ticker"] for t in trades})
    print(f"trades: {len(trades)}  with outcome: "
          f"{sum(1 for t in trades if t['ticker'] in outcomes)}  "
          f"tickers w/outcome: {len(outcomes)}")

    def _mk():
        return defaultdict(lambda: {"n": 0, "ct": 0.0, "pnl": 0.0, "win": 0})
    by_size, by_ttc, by_price = _mk(), _mk(), _mk()
    by_side = _mk()            # R1-C1 control: is the band pattern just direction?
    by_price_side = _mk()
    n_yes_outcome = n_win_outcome = 0
    # retail proxy = small (<=5 contracts); also split retail by price/ttc
    retail_by_price, retail_by_ttc = _mk(), _mk()
    tot = {"n": 0, "ct": 0.0, "pnl": 0.0}
    for t in trades:
        res = outcomes.get(t["ticker"])
        if res is None:
            continue
        r = res["result"]
        pnl = taker_pnl_cents(t["yes_c"], t["no_c"], t["taker_side"], r)
        ct = t["count"]
        won = pnl > 0
        price = t["yes_c"] if t["taker_side"] == "yes" else t["no_c"]
        ttc = close_epoch_from_ticker(t["ticker"]) - t["ts"]
        tot["n"] += 1; tot["ct"] += ct; tot["pnl"] += pnl * ct
        n_yes_outcome += int(r == "yes")
        s = by_side[t["taker_side"]]
        s["n"] += 1; s["ct"] += ct; s["pnl"] += pnl * ct; s["win"] += int(won)
        pband = next((l for lo, hi, l in PBKT if lo <= price < hi), None)
        if pband:
            ps = by_price_side[f"{pband}/{t['taker_side']}"]
            ps["n"] += 1; ps["ct"] += ct; ps["pnl"] += pnl * ct; ps["win"] += int(won)
        for agg, bkts, val in ((by_size, SBKT, ct), (by_ttc, TBKT, ttc), (by_price, PBKT, price)):
            lbl = next((l for lo, hi, l in bkts if lo <= val < hi), None)
            if lbl is None:
                continue
            a = agg[lbl]
            a["n"] += 1; a["ct"] += ct; a["pnl"] += pnl * ct; a["win"] += int(won)
        if ct <= 5:  # RETAIL slice
            for agg, bkts, val in ((retail_by_price, PBKT, price), (retail_by_ttc, TBKT, ttc)):
                lbl = next((l for lo, hi, l in bkts if lo <= val < hi), None)
                if lbl is None:
                    continue
                a = agg[lbl]
                a["n"] += 1; a["ct"] += ct; a["pnl"] += pnl * ct; a["win"] += int(won)

    if tot["ct"]:
        print(f"\n  ALL taker flow: {tot['n']} trades, {tot['ct']:.0f} contracts, "
              f"takerEV={tot['pnl']/tot['ct']:+.2f}c/ct, total taker$={tot['pnl']/100:+.2f}$ "
              f"(taker LOSS = maker EDGE)")
    if tot["n"]:
        print(f"\n  DIRECTION CONTROL (R1-C1): outcome was YES in {100*n_yes_outcome/tot['n']:.1f}% "
              f"of trades' windows. If the band pattern is just this drift, yes-takers win "
              f"every band + no-takers lose every band:")
        for side in ("yes", "no"):
            s = by_side.get(side)
            if s and s["ct"]:
                print(f"    {side}-taker overall: {s['n']} trades  EV={s['pnl']/s['ct']:+.2f}c/ct")
        print(f"    {'band/side':>14}{'trades':>8}{'EV/ct':>9}")
        for _, _, pb in PBKT:
            for side in ("yes", "no"):
                a = by_price_side.get(f"{pb}/{side}")
                if a and a["ct"]:
                    print(f"    {pb+'/'+side:>14}{a['n']:>8}{a['pnl']/a['ct']:>+9.2f}")
    _report("TAKER EV by SIZE (retail=small; -EV at small = the edge):", by_size, SBKT)
    _report("TAKER EV by TIME-TO-CLOSE:", by_ttc, TBKT)
    _report("TAKER EV by PRICE band:", by_price, PBKT)
    _report("RETAIL (<=5ct) by PRICE:", retail_by_price, PBKT)
    _report("RETAIL (<=5ct) by TIME-TO-CLOSE:", retail_by_ttc, TBKT)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
