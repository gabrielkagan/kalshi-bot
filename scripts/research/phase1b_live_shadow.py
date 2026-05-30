"""Phase 1b — LIVE-SHADOW harness (artifact-proof order-book edge test).

Runs an order-book strategy on RELIABLE reconstructed books (`reliable_nbbo_at` —
refuses drifted books) with REAL fills (would my resting order have been hit, per
the trades that actually printed?) and REAL outcomes. Built to run forward on the
now-clean 99.9% feed, where reliable books are plentiful (on the corrupt 25%-era
bronze it will simply skip ~all windows — by design).

Strategy shadowed (v1): passive two-sided maker — rest a YES bid at the best yes
bid and a NO bid at the best no bid, at T-Xs. Fill ONLY if a real trade reaches
the level afterward; PnL at the fill price held to settlement. This is the
make-the-spread idea, but with REAL adverse selection (you fill when the market
comes to you) on REAL data — the test that the corrupted backtests couldn't do.

Usage (on clean forward bronze):
  python3 scripts/research/phase1b_live_shadow.py \
    --frames-file /tmp/ob_clean.jsonl --trades-file /tmp/tr_clean.jsonl --outcomes-db /tmp/state.db

Parent: kb/decisions/settlement-convergence-worklist.md (live-shadow)
"""

from __future__ import annotations

import argparse
from collections import defaultdict

from scripts.research import kalshi_book_reconstruct as kbr
from scripts.research.phase1b_real_price_economics import (
    _is_crypto_15m, close_epoch_from_ticker, kalshi_fee_per_contract_cents,
    load_frames_jsonl, load_outcomes_db,
)
from scripts.research.phase1b_retail_flow import parse_trade

DECISION_OFFSETS_S = (30, 60)


# ----- real-trade fill model (TDD-pinned) ---------------------------------


def would_yes_bid_fill(trades, post_ts: float, bid_cents: float) -> bool:
    """My resting YES bid fills iff a real YES-SELL (taker_side='no') prints at
    yes_price <= my bid AFTER I post (someone sold YES down to my level)."""
    return any(ts > post_ts and side == "no" and yp <= bid_cents
               for ts, yp, side in trades)


def would_no_bid_fill(trades, post_ts: float, no_bid_cents: float) -> bool:
    """My resting NO bid (= offer to sell YES at 100-no_bid) fills iff a real
    YES-BUY (taker_side='yes') prints at yes_price >= 100-no_bid after I post."""
    thresh = 100.0 - no_bid_cents
    return any(ts > post_ts and side == "yes" and yp >= thresh
               for ts, yp, side in trades)


def maker_pnl_cents(fill_price: float, side: str, result: str) -> float:
    """Realized PnL of a maker fill held to settlement (maker pays no taker fee
    on the resting side)."""
    return (100.0 - fill_price) if side == result else -float(fill_price)


# ----- harness ------------------------------------------------------------


def load_trades_by_ticker(path: str) -> dict:
    out = defaultdict(list)
    with open(path) as fh:
        for line in fh:
            if not line.strip():
                continue
            t = parse_trade(line)
            if t:
                out[t["ticker"]].append((t["ts"], t["yes_c"], t["taker_side"]))
    for tk in out:
        out[tk].sort()
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--frames-file", required=True)
    ap.add_argument("--trades-file", required=True)
    ap.add_argument("--outcomes-db", required=True)
    args = ap.parse_args(argv)

    frames = load_frames_jsonl(args.frames_file)
    trades = load_trades_by_ticker(args.trades_file)
    outcomes = load_outcomes_db(args.outcomes_db, set(frames))
    print(f"windows: {len(frames)}  with trades: {sum(1 for tk in frames if tk in trades)}  "
          f"with outcome: {sum(1 for tk in frames if tk in outcomes)}")

    agg = defaultdict(lambda: {"reliable": 0, "posted": 0, "filled": 0,
                               "pnl": 0.0, "win": 0})
    skipped_unreliable = 0
    for tk, fr in frames.items():
        if tk not in outcomes or tk not in trades:
            continue
        result = outcomes[tk]["result"]
        close = close_epoch_from_ticker(tk)
        tr = trades[tk]
        for off in DECISION_OFFSETS_S:
            cutoff = close - off
            bid, ask = kbr.reliable_nbbo_at(fr, cutoff)  # None,None if book not reliable
            a = agg[off]
            if bid is None or ask is None:
                skipped_unreliable += 1
                continue
            a["reliable"] += 1
            no_bid = 100.0 - ask  # best NO bid
            # post both sides at the (reliable) best bids
            for side, price, fill in (("yes", bid, would_yes_bid_fill(tr, cutoff, bid)),
                                      ("no", no_bid, would_no_bid_fill(tr, cutoff, no_bid))):
                if not (0 < price < 100):
                    continue
                a["posted"] += 1
                if fill:
                    a["filled"] += 1
                    won = (side == result)
                    a["win"] += int(won)
                    a["pnl"] += maker_pnl_cents(price, side, result) - kalshi_fee_per_contract_cents(price)

    print(f"skipped (book not reliable at decision): {skipped_unreliable}")
    print(f"\n{'T-Xs':>5}{'reliable_bk':>12}{'posted':>8}{'filled':>8}{'fill%':>7}{'winF%':>7}{'netEV/fill':>12}{'totalPnL$':>11}")
    for off in DECISION_OFFSETS_S:
        a = agg[off]
        if not a["posted"]:
            print(f"{off:>5}{a['reliable']:>12}{0:>8}  (no reliable books on this data — needs clean forward feed)")
            continue
        fl = a["filled"] or 1
        print(f"{off:>5}{a['reliable']:>12}{a['posted']:>8}{a['filled']:>8}"
              f"{100*a['filled']/a['posted']:>7.1f}{100*a['win']/fl:>7.1f}"
              f"{a['pnl']/fl:>+12.2f}{a['pnl']/100:>+11.2f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
