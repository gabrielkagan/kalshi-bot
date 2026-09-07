#!/usr/bin/env python3
"""convexity_pin_fade_rails — cross-strike static no-arbitrage convexity fade.

FAMILY: cross-strike-arbitrage / static no-arbitrage convexity.

MECHANISM (as specified):
    A 15M crypto event is hypothesized to be a LADDER of strikes (e.g. BTC above
    104000 / 104250 / 104500 ... for the SAME close epoch). The implied P(above)
    must be monotone non-increasing in strike, and the implied PMF between adjacent
    strikes must be non-negative (butterfly = P(K-1) - 2P(K) + P(K+1) >= 0). When
    reliable-NBBO mids violate local convexity, one strike is mispriced relative
    to its neighbours regardless of where spot goes. We would BUY the cheap wing(s)
    and SELL the rich centre as a maker package, fee-net, with all three rungs of
    the butterfly required reliable simultaneously and all legs required to fill
    (honest trade-cross fills), bootstrap-CI clustered by EVENT.

HARD PRE-CHECK (DATA_GAP gate, run FIRST):
    The mechanism is only defined if events have MULTIPLE sibling strikes sharing
    one close epoch. This script's FIRST job is to confirm that structure exists
    in the corpus. If every event is single-strike, there is no convexity relation
    to test and the verdict is DATA_GAP (the idea dies structurally, exactly as the
    spec anticipated: "if events are single-strike-only this is a DATA_GAP and the
    idea dies").

FINDING (this corpus): Kalshi crypto-15M tickers have the shape
    KX<ASSET>15M-<YYMMMDDHHMM>-<MM>
where the trailing segment is the CLOSE-MINUTE (it mirrors the HHMM in the time
segment: ...1715-15, ...2230-30), NOT a strike index. Each window is a SINGLE
above/below market whose one strike is the spot price at window open (DB
`evaluated_opportunities.threshold`, e.g. SOL 85.04 / BTC 68014.55). There is
exactly ONE strike per (asset, close-epoch). No ladder => no butterfly => DATA_GAP.

This script PROVES that from the real local corpus (FRAMES distinct-ticker scan +
DB threshold-per-window check over the full Feb->May ledger) rather than asserting
it, so the verdict is auditable. It does NOT fabricate a butterfly where none
exists; no fee/fill/CI stage is reachable because the input structure is absent.

Run:
    cd /Users/gabrielkagan/Documents/kalshi-bot
    python3 scripts/research/algo_zoo/convexity_pin_fade_rails.py
"""
from __future__ import annotations

import sqlite3
import sys
from collections import Counter, defaultdict

sys.path.insert(0, "/Users/gabrielkagan/Documents/kalshi-bot")

# Real, tested helpers. The convexity legs WOULD call reliable_nbbo_at on each
# rung (imported below to honour "reuse tested reconstruction"); that code path is
# never reached for this corpus because the ladder structure is absent.
from scripts.research.kalshi_book_reconstruct import reliable_nbbo_at  # noqa: E402,F401
from scripts.research.phase1b_real_price_economics import (  # noqa: E402
    _is_crypto_15m,
    close_epoch_from_ticker,
)

FRAMES = "/tmp/edge_daily/frames_crypto.jsonl"
DB = "/tmp/edge_daily/state.db"

# Bytes of `"market_ticker":"` exactly as they appear ESCAPED inside the _raw
# string field of a bronze envelope line (backslash-quote), so we can pull the
# ticker without json.loads on 9.77M rows.
_TK_KEY = '\\"market_ticker\\":\\"'


def scan_distinct_tickers(path: str, stride: int = 11) -> set[str]:
    """Cheap distinct-ticker scan across the whole FRAMES file (sampled 1/stride;
    the file is ticker-clustered on disk so a stride sample still surfaces every
    ticker with more than `stride` rows — and every live 15M market has thousands
    of frames)."""
    seen: set[str] = set()
    n = 0
    klen = len(_TK_KEY)
    with open(path) as fh:
        for line in fh:
            n += 1
            if n % stride:
                continue
            i = line.find(_TK_KEY)
            if i < 0:
                continue
            j = i + klen
            k = line.find('\\"', j)
            tk = line[j:k]
            if tk:
                seen.add(tk)
    return seen


def strikes_per_event_from_frames(tickers: set[str]) -> dict:
    """Group crypto-15M tickers by (asset, close_epoch) and collect distinct
    members. A 'ladder' event has >= 3 sibling strikes at one close epoch."""
    grp: dict = defaultdict(set)
    for tk in tickers:
        a = _is_crypto_15m(tk)
        if not a:
            continue
        try:
            ce = close_epoch_from_ticker(tk)
        except Exception:
            continue
        grp[(a, ce)].add(tk)
    return grp


def strikes_per_window_from_db(db_path: str) -> dict:
    """Authoritative cross-check: distinct DB thresholds per (asset, close-window)
    base across the FULL Feb->May ledger. If single-strike, max == 1 everywhere."""
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA busy_timeout=10000")
    conn.row_factory = sqlite3.Row
    base_strikes: dict = defaultdict(set)
    try:
        rows = conn.execute(
            "SELECT ticker, threshold FROM evaluated_opportunities "
            "WHERE ticker LIKE 'KX%15M-%' AND threshold IS NOT NULL"
        ).fetchall()
    finally:
        conn.close()
    for r in rows:
        parts = r["ticker"].split("-")
        base = "-".join(parts[:-1])  # asset + close-window, minus trailing minute
        base_strikes[base].add(round(float(r["threshold"]), 6))
    return base_strikes


def main() -> int:
    print("=" * 70)
    print("convexity_pin_fade_rails — DATA_GAP pre-check (cross-strike ladder?)")
    print("=" * 70)

    # ---- 1. FRAMES: distinct tickers + strikes per close epoch ----
    tickers = scan_distinct_tickers(FRAMES)
    crypto = {t for t in tickers if _is_crypto_15m(t)}
    print(f"\n[FRAMES] distinct crypto-15M tickers in window: {len(crypto)}")
    print(f"[FRAMES] by asset: {dict(Counter(_is_crypto_15m(t) for t in crypto))}")

    grp = strikes_per_event_from_frames(crypto)
    sizes = sorted((len(v) for v in grp.values()), reverse=True)
    n_events = len(grp)
    n_ladder = sum(1 for s in sizes if s >= 3)
    max_strikes = max(sizes) if sizes else 0
    print(f"[FRAMES] events (asset, close_epoch): {n_events}")
    print(f"[FRAMES] max strikes per event: {max_strikes}")
    print(f"[FRAMES] events with >=3 sibling strikes (ladder): {n_ladder}")
    print(f"[FRAMES] strike-count distribution: {dict(Counter(sizes))}")

    # ---- 2. DB: authoritative threshold-per-window over full ledger ----
    base_strikes = strikes_per_window_from_db(DB)
    db_sizes = [len(s) for s in base_strikes.values()]
    db_max = max(db_sizes) if db_sizes else 0
    print(f"\n[DB] distinct (asset, close-window) bases (Feb->May): "
          f"{len(base_strikes)}")
    print(f"[DB] max distinct strikes per window: {db_max}")
    print(f"[DB] strikes-per-window distribution: {dict(Counter(db_sizes))}")
    for b, s in list(base_strikes.items())[:4]:
        print(f"[DB]   {b} -> threshold {sorted(s)} (spot-at-open, not a ladder)")

    # ---- 3. Verdict ----
    is_ladder = (max_strikes >= 3) or (db_max >= 3)
    print("\n" + "-" * 70)
    if not is_ladder:
        print("VERDICT: DATA_GAP")
        print("Every crypto-15M event is SINGLE-STRIKE (one above/below market per")
        print("close epoch; the trailing ticker segment is the close-minute, not a")
        print("strike index; the one strike is spot-at-open). No ladder of")
        print("simultaneous strikes => the butterfly relation")
        print("P(K-1) - 2P(K) + P(K+1) >= 0 is undefined. The convexity_pin_fade")
        print("mechanism cannot be constructed from this corpus. The idea dies")
        print("structurally — no fee/fill/CI stage is reachable.")
    else:
        print("VERDICT: ladder structure PRESENT — proceed to convexity backtest")
        print("(NOT reached for this corpus).")
    print("-" * 70)

    return 0 if not is_ladder else 1


if __name__ == "__main__":
    sys.exit(main())
