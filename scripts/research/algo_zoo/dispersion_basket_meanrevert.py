#!/usr/bin/env python3
"""dispersion_basket_meanrevert — cross-sectional statistical-arb / relative-value.

MECHANISM
---------
At a common close-offset (decision = close - OFFSET_S), for each of the 4
spot-covered assets (BTC/ETH/SOL/XRP) compute a model-free
    GAP_i = kalshi_implied_i - spot_implied_i
where
    kalshi_implied_i = reliable_nbbo YES-mid (refuses drifted books) at decision,
    spot_implied_i   = frozen-drift Gaussian P(spot_close > strike):
        z = (strike - spot_now) / sig_rem
        P_above = 1 - Phi(z)          (NO drift term)
        sig_rem = sqrt(realized_var_per_sec * remaining_horizon) * spot_now,
                  realized_var_per_sec from trailing-15min coinbase log-returns.

Cross-sectionally z-score the 4 GAPs at each window timestamp. The asset whose
gap is the largest-magnitude OUTLIER vs the *basket mean* is FADED:
    z_outlier > 0  (Kalshi too high vs spot, relative to basket) -> take NO
    z_outlier < 0  (Kalshi too low  vs spot, relative to basket) -> take YES
RELATIVE construction: demeaning across the basket cancels any systematic
spot-vs-Kalshi bias / stale-quote artifact (which killed the prior absolute
info-edge idea). A surviving signal is one alt's market lagging a move the
other three already priced.

FILLS — honest maker-cross
--------------------------
We post a resting maker order at the dislocated quote (passive NO bid when
fading-high; passive YES bid when fading-low). It FILLS only when a real trade
print crosses it within the window (mm_markout_evaluator first_yes_bid_fill_ts /
first_no_bid_fill_ts). No cross -> NO TRADE (unfilled, contributes 0, recorded).

OUTCOME / PnL
-------------
Outcome derived from the TERMINAL BOOK mid at close (reliable_nbbo at close;
fallback to DB market_result when the terminal book is ambiguous/unreliable).
Net PnL per contract = settlement(0/100) - entry_cost - fee, with
fee = ceil(0.07*C*P*(1-P)) cents/contract on entry, maker rebate = 0.

CI: bootstrap clustered by (asset, window) — each dislocation IS one such unit;
unfilled dislocations enter as net=0. EDGE only if net-PnL CI lower bound > 0
after the basket-demeaning.

NON-NEGOTIABLES honored: real fees, honest fill, no look-ahead (signal book and
label book built independently with strict cutoffs), reliable-book refusal.
"""
from __future__ import annotations

import json
import math
import random
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timezone

sys.path.insert(0, "/Users/gabrielkagan/Documents/kalshi-bot")

from scripts.research.kalshi_book_reconstruct import reliable_nbbo_at  # noqa: E402
from scripts.research.phase1b_real_price_economics import (  # noqa: E402
    close_epoch_from_ticker,
)
from scripts.research.mm_markout_evaluator import (  # noqa: E402
    first_no_bid_fill_ts,
    first_yes_bid_fill_ts,
)

FRAMES = "/tmp/edge_daily/frames_crypto.jsonl"
TRADES = "/tmp/edge_daily/trades_crypto.jsonl"
SPOT = "/tmp/edge_daily/coinbase_spot.jsonl"
DB = "/tmp/edge_daily/state.db"

ASSETS = ("BTC", "ETH", "SOL", "XRP")
SPOT_PROD = {"BTC": "BTC-USD", "ETH": "ETH-USD", "SOL": "SOL-USD", "XRP": "XRP-USD"}
OFFSET_S = 120.0            # decision time = close - 120s
VOL_LOOKBACK_S = 900.0      # trailing window for realized vol (15 min)
HORIZON_S = 900.0           # 15M window length
Z_THRESHOLD = 1.0           # only trade if |outlier z| exceeds this
FEE_RATE = 0.07
MAKER_REBATE_C = 0.0
N_BOOT = 5000

random.seed(7)


def _epoch(iso: str) -> float:
    if iso.endswith("Z"):
        iso = iso[:-1] + "+00:00"
    return datetime.fromisoformat(iso).timestamp()


def fee_cents(price_c: float) -> float:
    """ceil(0.07 * C * P * (1-P)) cents/contract, C=1, P=price/100."""
    p = price_c / 100.0
    return float(math.ceil(FEE_RATE * 1.0 * p * (1.0 - p) * 100.0))


def norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def load_windows():
    """4-asset windows whose close is after spot coverage starts, with DB
    strike + result cross-check per asset leg."""
    spot_start = datetime(2026, 5, 30, 21, 0, 0, tzinfo=timezone.utc).timestamp()
    conn = sqlite3.connect(DB)
    conn.execute("PRAGMA busy_timeout=10000")
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT DISTINCT ticker, threshold, market_result FROM evaluated_opportunities "
        "WHERE ticker LIKE 'KX%15M-26MAY3%' AND threshold IS NOT NULL "
        "AND market_result IN ('yes','no')"
    ).fetchall()
    by_close = defaultdict(dict)
    for r in rows:
        tk = r["ticker"]
        a = tk[2:5]
        if a not in ASSETS:
            continue
        ce = close_epoch_from_ticker(tk)
        by_close[round(ce)][a] = {
            "ticker": tk, "strike": float(r["threshold"]),
            "db_result": r["market_result"], "close": ce,
        }
    windows = []
    for ce, d in sorted(by_close.items()):
        if len(d) == 4 and ce > spot_start + VOL_LOOKBACK_S:
            windows.append((float(ce), d))
    return windows


def load_frames_for(tickers: set) -> dict:
    frames = defaultdict(list)
    with open(FRAMES) as fh:
        for line in fh:
            if not line:
                continue
            try:
                env = json.loads(line)
                inner = json.loads(env["_raw"])
            except (ValueError, KeyError):
                continue
            tk = inner.get("msg", {}).get("market_ticker", "")
            if tk in tickers:
                frames[tk].append((_epoch(env["_wire_recv_ts"]), inner))
    for tk in frames:
        frames[tk].sort(key=lambda x: x[0])
    return frames


def load_trades_for(tickers: set) -> dict:
    """{ticker: [(ts, yes_price_c, taker_side), ...]} for the fill primitives."""
    out = defaultdict(list)
    with open(TRADES) as fh:
        for line in fh:
            if not line:
                continue
            try:
                env = json.loads(line)
                msg = json.loads(env["_raw"])["msg"]
            except (ValueError, KeyError):
                continue
            tk = msg.get("market_ticker", "")
            if tk not in tickers:
                continue
            try:
                ts = float(msg["ts"])
                yp = float(msg["yes_price_dollars"]) * 100.0
                side = msg["taker_side"]
            except (KeyError, ValueError):
                continue
            out[tk].append((ts, yp, side))
    for tk in out:
        out[tk].sort(key=lambda x: x[0])
    return out


def load_spot() -> dict:
    """{product_id: [(epoch, mid), ...]} sorted ascending."""
    out = defaultdict(list)
    with open(SPOT) as fh:
        for line in fh:
            if not line:
                continue
            try:
                o = json.loads(line)
                mid = float(o["mid"])
                ts = _epoch(o["ts"])
            except (ValueError, KeyError):
                continue
            out[o["product_id"]].append((ts, mid))
    for p in out:
        out[p].sort(key=lambda x: x[0])
    return out


def _bisect_le(seq, t):
    lo, hi = 0, len(seq)
    while lo < hi:
        mid = (lo + hi) // 2
        if seq[mid][0] <= t:
            lo = mid + 1
        else:
            hi = mid
    return lo - 1 if lo > 0 else None


def spot_now_and_sigma(spot_series, decision_t):
    """spot mid at/<=decision_t and a drift-free price-std over HORIZON_S from
    trailing log-returns. NO look-ahead: only ticks with ts <= decision_t."""
    idx = _bisect_le(spot_series, decision_t)
    if idx is None:
        return None, None
    spot_now = spot_series[idx][1]
    start = decision_t - VOL_LOOKBACK_S
    window = [(t, m) for (t, m) in spot_series[: idx + 1] if t >= start]
    if len(window) < 5:
        return spot_now, None
    rets = []
    for i in range(1, len(window)):
        p0, p1 = window[i - 1][1], window[i][1]
        if p0 > 0 and p1 > 0:
            rets.append(math.log(p1 / p0))
    if len(rets) < 4:
        return spot_now, None
    elapsed = window[-1][0] - window[0][0]
    if elapsed <= 0:
        return spot_now, None
    total_var = sum(r * r for r in rets)   # realized variance, drift-free
    per_sec_var = total_var / elapsed
    sigma_price = spot_now * math.sqrt(per_sec_var * HORIZON_S)
    if sigma_price <= 0:
        return spot_now, None
    return spot_now, sigma_price


def build_signals(windows, frames, spot):
    out = []
    for close_t, assets in windows:
        decision_t = close_t - OFFSET_S
        remaining = close_t - decision_t  # == OFFSET_S
        legs = {}
        for a in ASSETS:
            info = assets[a]
            tk = info["ticker"]
            fr = frames.get(tk)
            if not fr:
                continue
            yb, ya = reliable_nbbo_at(fr, decision_t)
            if yb is None or ya is None:
                continue  # refuse drifted/unreliable book
            kalshi_implied = (yb + ya) / 200.0  # YES-mid as P(above) in [0,1]
            sp_series = spot.get(SPOT_PROD[a])
            if not sp_series:
                continue
            spot_now, sigma = spot_now_and_sigma(sp_series, decision_t)
            if spot_now is None or sigma is None:
                continue
            sig_rem = sigma * math.sqrt(remaining / HORIZON_S)
            if sig_rem <= 0:
                continue
            z = (info["strike"] - spot_now) / sig_rem
            spot_implied = 1.0 - norm_cdf(z)  # P(spot_close > strike)
            legs[a] = {
                "ticker": tk, "strike": info["strike"], "close": close_t,
                "decision_t": decision_t, "yes_bid_c": yb, "yes_ask_c": ya,
                "kalshi_implied": kalshi_implied, "spot_implied": spot_implied,
                "gap": kalshi_implied - spot_implied,
                "db_result": info["db_result"],
            }
        if len(legs) >= 3:
            out.append({"close": close_t, "legs": legs})
    return out


def terminal_outcome(fr, close_t, db_result):
    """Label from terminal book mid at close (look-ahead OK — independent label
    book). Fallback to DB result if the terminal book is ambiguous/unreliable."""
    yb, ya = reliable_nbbo_at(fr, close_t)
    if yb is not None and ya is not None:
        mid = (yb + ya) / 2.0
        if mid >= 99.0:
            return "yes"
        if mid <= 1.0:
            return "no"
    return db_result


def run():
    windows = load_windows()
    tickers = set()
    for _, d in windows:
        for a in ASSETS:
            tickers.add(d[a]["ticker"])
    print(f"[load] {len(windows)} 4-asset windows, {len(tickers)} tickers", file=sys.stderr)

    frames = load_frames_for(tickers)
    print(f"[load] frames for {len(frames)}/{len(tickers)} tickers", file=sys.stderr)
    trades = load_trades_for(tickers)
    print(f"[load] trades for {len(trades)} tickers", file=sys.stderr)
    spot = load_spot()
    print(f"[load] spot products {sorted(spot.keys())}", file=sys.stderr)

    signals = build_signals(windows, frames, spot)
    print(f"[signal] {len(signals)} windows with >=3 reliable legs", file=sys.stderr)

    records = []
    n_dislocations = 0
    n_unfilled = 0
    n_filled = 0
    detail = []

    for w in signals:
        legs = w["legs"]
        gaps = {a: legs[a]["gap"] for a in legs}
        if len(gaps) < 2:
            continue
        mean_gap = sum(gaps.values()) / len(gaps)
        var = sum((g - mean_gap) ** 2 for g in gaps.values()) / (len(gaps) - 1)
        sd = math.sqrt(var)
        if sd <= 1e-9:
            continue
        zs = {a: (gaps[a] - mean_gap) / sd for a in gaps}
        outlier = max(zs, key=lambda a: abs(zs[a]))
        z_out = zs[outlier]
        if abs(z_out) < Z_THRESHOLD:
            continue

        leg = legs[outlier]
        tk = leg["ticker"]
        close_t = leg["close"]
        decision_t = leg["decision_t"]
        fr = frames[tk]
        tr = trades.get(tk, [])
        outcome = terminal_outcome(fr, close_t, leg["db_result"])

        if z_out > 0:
            side = "no"
            no_bid_c = 100.0 - leg["yes_ask_c"]   # passive NO entry
            if no_bid_c <= 0 or no_bid_c >= 100:
                continue
            n_dislocations += 1
            fill_ts = first_no_bid_fill_ts(tr, decision_t, no_bid_c)
            entry_c = no_bid_c
            win = (outcome == "no")
        else:
            side = "yes"
            yes_bid_c = leg["yes_bid_c"]           # passive YES entry
            if yes_bid_c <= 0 or yes_bid_c >= 100:
                continue
            n_dislocations += 1
            fill_ts = first_yes_bid_fill_ts(tr, decision_t, yes_bid_c)
            entry_c = yes_bid_c
            win = (outcome == "yes")

        if fill_ts is None or fill_ts > close_t:
            n_unfilled += 1
            records.append({"asset": outlier, "window": close_t, "filled": False, "net": 0.0})
            continue

        n_filled += 1
        gross = (100.0 if win else 0.0) - entry_c
        fee = fee_cents(entry_c) - MAKER_REBATE_C
        net = gross - fee
        records.append({"asset": outlier, "window": close_t, "filled": True, "net": net})
        detail.append((tk, side, round(entry_c, 1), outcome, "WIN" if win else "LOSS",
                       round(net, 2), round(z_out, 2)))

    if not records:
        print("[result] no dislocations produced", file=sys.stderr)
        return None

    filled = [r for r in records if r["filled"]]
    nets_all = [r["net"] for r in records]
    nets_filled = [r["net"] for r in filled]
    mean_all = sum(nets_all) / len(nets_all)
    mean_filled = (sum(nets_filled) / len(nets_filled)) if nets_filled else float("nan")

    def boot_ci(recs):
        if not recs:
            return (float("nan"), float("nan"))
        n = len(recs)
        means = []
        for _ in range(N_BOOT):
            s = 0.0
            for _ in range(n):
                s += recs[random.randrange(n)]["net"]
            means.append(s / n)
        means.sort()
        return (means[int(0.025 * N_BOOT)], means[int(0.975 * N_BOOT)])

    ci_all = boot_ci(records)
    ci_filled = boot_ci(filled)

    print("\n=== dispersion_basket_meanrevert ===", file=sys.stderr)
    print(f"n_dislocations : {n_dislocations}", file=sys.stderr)
    print(f"n_filled       : {n_filled}", file=sys.stderr)
    print(f"n_unfilled     : {n_unfilled}", file=sys.stderr)
    if n_dislocations:
        print(f"unfilled_rate  : {n_unfilled / n_dislocations:.3f}", file=sys.stderr)
    print(f"mean net (incl unfilled=0): {mean_all:+.3f} c/contract  95%CI {ci_all}", file=sys.stderr)
    print(f"mean net (FILLED only)    : {mean_filled:+.3f} c/contract  95%CI {ci_filled}", file=sys.stderr)
    print("\nfilled detail (ticker, side, entry_c, outcome, w/l, net_c, z):", file=sys.stderr)
    for d in detail:
        print("  ", d, file=sys.stderr)

    return {
        "n_dislocations": n_dislocations, "n_filled": n_filled, "n_unfilled": n_unfilled,
        "mean_net_all": mean_all, "ci_all": list(ci_all),
        "mean_net_filled": mean_filled, "ci_filled": list(ci_filled),
    }


if __name__ == "__main__":
    res = run()
    print(json.dumps(res, default=str))
