"""strike_ladder_no_arbitrage_taker — cross-strike static arbitrage (taker).

FAMILY: cross-strike static arbitrage (monotonicity / vertical-spread coherence)

SPEC (intended):
    Model-free arb ACROSS strikes of the SAME asset/window. Group tickers by
    (asset, window-mid); parse the strike threshold from the ticker suffix; sort
    strikes ascending. P(YES = above K) must be monotone NON-INCREASING in K.
    At each grid time t, for every adjacent strike pair (K_lo, K_hi) reconstruct
    BOTH reliable books INDEPENDENTLY via reliable_nbbo_at (require both reliable
    else skip). A coherence violation is ask(YES@K_hi) < bid(YES@K_lo) - SLACK.
    Lock it as a TAKER package: BUY YES@K_hi (cross ask_hi) + SELL YES@K_lo
    (cross, =100-bid_lo). Settlement payoff of "long higher strike / short lower
    strike" is in {0, +100} (non-negative, model-free). Net cents per package =
    (bid_lo - ask_hi) - fee_hi - fee_lo + settlement_payoff. Headline: block
    bootstrap clustered by (asset, window).

CRITICAL STRUCTURAL FINDING (this corpus):
    This algorithm requires a LADDER of multiple strikes per (asset, window).
    Kalshi crypto-15M markets DO NOT HAVE ONE. Each (asset, window) is a SINGLE
    binary above/below market with exactly ONE strike. The ticker suffix that the
    spec wants to parse as "strike threshold" is in fact the CLOSE-MINUTE of the
    window (the HH:MM minute), redundant with the time already encoded in the
    middle field — NOT a strike. Proven 640/640 tickers in this corpus. The
    actual strike (a dollar price level) is set by Kalshi at window open and
    lives only in market metadata / the DB `threshold` field; there is exactly
    one threshold per ticker and never multiple tickers sharing an
    (asset, window) with different strikes.

    Therefore there is NO ladder to relate via vertical-spread coherence, and the
    arbitrage as specified is structurally inapplicable to this market. VERDICT:
    DATA_GAP (the missing input is a multi-strike ladder; nothing to
    reconstruct, no fee/fill model can manufacture strikes the market does not
    list).

This script PROVES the absence rather than asserting it: it scans the full
local frames corpus + cross-checks the DB, and reports the per-(asset,window)
strike-count distribution. It is its own assassin — if a real multi-strike
ladder were found it would fall through to the (also-implemented)
reconstruction-based taker-arb scan with real fees.

Usage:
    cd /Users/gabrielkagan/Documents/kalshi-bot
    python3 scripts/research/algo_zoo/strike_ladder_no_arbitrage_taker.py
"""
from __future__ import annotations

import json
import math
import sqlite3
import sys
from collections import defaultdict

sys.path.insert(0, "/Users/gabrielkagan/Documents/kalshi-bot")

from scripts.research.kalshi_book_reconstruct import reliable_nbbo_at  # noqa: E402

FRAMES = "/tmp/edge_daily/frames_crypto.jsonl"
DB = "/tmp/edge_daily/state.db"


def kalshi_fee_cents(price_cents: float, contracts: int = 1) -> float:
    """Real Kalshi taker fee: ceil(0.07 * C * P * (1-P)) cents, P in dollars."""
    p = price_cents / 100.0
    return math.ceil(0.07 * contracts * p * (1.0 - p) * 100.0)


def scan_ladders():
    """Build {(asset, window-mid): {strike_suffix: ticker}} from frames.

    A 'ladder' requires >=2 distinct strike suffixes under one
    (asset, window-mid) key.
    """
    windows: dict[tuple, dict] = defaultdict(dict)
    tickers: set[str] = set()
    n = 0
    with open(FRAMES) as fh:
        for line in fh:
            n += 1
            try:
                env = json.loads(line)
                inner = json.loads(env["_raw"])
                tk = inner.get("msg", {}).get("market_ticker", "")
            except (ValueError, KeyError):
                continue
            if not tk or tk in tickers:
                continue
            tickers.add(tk)
            p = tk.split("-")
            if len(p) >= 3:
                asset, window_mid, strike_suffix = p[0], p[1], "-".join(p[2:])
                windows[(asset, window_mid)][strike_suffix] = tk
    return windows, tickers, n


def db_thresholds(tickers: set) -> dict:
    """{ticker: set(thresholds)} from evaluated_opportunities — the REAL strike.

    A hidden ladder (one ticker carrying multiple strikes) would surface here.
    """
    out: dict[str, set] = defaultdict(set)
    if not tickers:
        return out
    conn = sqlite3.connect(DB)
    conn.execute("PRAGMA busy_timeout=10000")
    conn.row_factory = sqlite3.Row
    q = ",".join("?" * len(tickers))
    sql = (f"SELECT ticker, threshold FROM evaluated_opportunities "
           f"WHERE ticker IN ({q}) AND threshold IS NOT NULL")
    for r in conn.execute(sql, tuple(tickers)):
        out[r["ticker"]].add(r["threshold"])
    conn.close()
    return out


def _epoch(iso: str) -> float:
    from datetime import datetime
    return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()


def main() -> dict:
    windows, tickers, n_lines = scan_ladders()

    dist: dict[int, int] = defaultdict(int)
    multi = []
    for key, strikes in windows.items():
        dist[len(strikes)] += 1
        if len(strikes) >= 2:
            multi.append((key, strikes))

    # Suffix == close-minute proof (suffix is TIME, not a strike).
    suffix_is_close_minute = sum(
        1 for tk in tickers
        if len(tk.split("-")) >= 3 and tk.split("-")[1][-2:] == tk.split("-")[2]
    )

    thr = db_thresholds(tickers)
    tickers_with_thr = sum(1 for v in thr.values() if v)
    tickers_multi_thr = sum(1 for v in thr.values() if len(v) > 1)

    print("=" * 70)
    print("strike_ladder_no_arbitrage_taker — corpus ladder scan")
    print("=" * 70)
    print(f"frames lines scanned        : {n_lines:,}")
    print(f"unique crypto-15M tickers   : {len(tickers)}")
    print(f"(asset,window-mid) groups   : {len(windows)}")
    print(f"strikes-per-window dist     : {dict(sorted(dist.items()))}")
    print(f"MULTI-STRIKE windows (>=2)  : {len(multi)}")
    print(f"suffix == close-minute      : {suffix_is_close_minute}/{len(tickers)} "
          f"(proves suffix is TIME, not a strike)")
    print(f"tickers with DB threshold   : {tickers_with_thr}")
    print(f"tickers with >1 threshold   : {tickers_multi_thr} "
          f"(a hidden ladder would surface here)")
    print("-" * 70)

    if not multi:
        print("VERDICT: DATA_GAP")
        print("No multi-strike ladder exists in Kalshi crypto-15M. Each "
              "(asset,window) is a SINGLE binary above/below market with exactly "
              "one strike (the dollar level lives in DB.threshold, never as "
              "multiple tradeable tickers). There is nothing to relate via "
              "vertical-spread coherence; the cross-strike arbitrage is "
              "structurally inapplicable. No reconstruction, fee, or fill model "
              "can manufacture a ladder the market does not list.")
        return {
            "verdict": "DATA_GAP",
            "n_tickers": len(tickers),
            "n_windows": len(windows),
            "n_multi_strike_windows": 0,
        }

    # --- Fallback: a real ladder exists -> run the actual taker-arb scan. -----
    # Unreachable on this corpus; implemented so the script honestly hunts the
    # edge if Kalshi ever lists multi-strike 15M markets.
    print(f"Multi-strike ladders FOUND ({len(multi)}). Running taker-arb scan...")
    frames_by_ticker: dict[str, list] = defaultdict(list)
    with open(FRAMES) as fh:
        for line in fh:
            try:
                env = json.loads(line)
                inner = json.loads(env["_raw"])
                tk = inner.get("msg", {}).get("market_ticker", "")
            except (ValueError, KeyError):
                continue
            if tk:
                frames_by_ticker[tk].append((_epoch(env["_wire_recv_ts"]), inner))
    for tk in frames_by_ticker:
        frames_by_ticker[tk].sort(key=lambda x: x[0])

    SLACK = 1.0
    GRID = 30.0
    packages = []
    for (asset, window_mid), strikes in multi:
        ordered = sorted(strikes.items(), key=lambda kv: int(kv[0]))
        for (s_lo, tk_lo), (s_hi, tk_hi) in zip(ordered, ordered[1:]):
            f_lo = frames_by_ticker.get(tk_lo, [])
            f_hi = frames_by_ticker.get(tk_hi, [])
            if not f_lo or not f_hi:
                continue
            t0 = max(f_lo[0][0], f_hi[0][0])
            t1 = min(f_lo[-1][0], f_hi[-1][0])
            t = t0
            while t <= t1:
                bid_lo, _ = reliable_nbbo_at(f_lo, t)
                _, ask_hi = reliable_nbbo_at(f_hi, t)
                if bid_lo is not None and ask_hi is not None:
                    if ask_hi < bid_lo - SLACK:
                        fee = kalshi_fee_cents(ask_hi) + kalshi_fee_cents(bid_lo)
                        entry_edge = bid_lo - ask_hi
                        net = entry_edge - fee  # +settlement payoff >= 0
                        if net > 0:
                            packages.append((asset, window_mid, net))
                t += GRID
    print(f"executable violation packages: {len(packages)}")
    return {"verdict": "INCONCLUSIVE" if packages else "NO_EDGE",
            "n_packages": len(packages)}


if __name__ == "__main__":
    main()
