"""spot_realized_vol_strike_distance_taker — variance-risk-premium directional taker.

MECHANISM
  A 15M above/below is a digital option on spot crossing the strike. Its fair price
  is Phi(d / (RV*sqrt(remaining_time))) where d = (spot-strike)/spot is the signed
  log-ish distance and RV is the realized vol of recent 1s spot returns. The Kalshi
  book often misprices when the recent realized vol diverges from the vol implied in
  the digital price. The VRP thesis: in CALM regimes the market over-charges for the
  tail crossing (implied vol > realized vol), so the high-probability near-strike side
  is cheap and we TAKE it at the ask.

DECISION (no look-ahead, per ticker)
  decision_epoch = close_epoch - LEAD_S (default 180s before close).
  spot, RV  : from coinbase_spot 1s mids over the trailing RV_WINDOW_S, all <= decision.
  strike    : evaluated_opportunities.threshold (DB) — the digital strike level.
  p_mkt     : reliable reconstructed YES book mid at decision (REFUSE drifted books).
  p_model   : Phi(d / sigma_T) with d = log(spot/strike), sigma_T = RV*sqrt(rem/RV_WINDOW_S).
  SIGNAL    : take the side p_model favors iff |p_model - p_mkt| > THRESH and that
              side is priced < MAX_TAKE_PRICE (default 70c). Cross to the ask.

LABEL
  terminal outcome from the DB settlement (market_result yes/no) — the actual
  settled truth, which is the terminal-book outcome by construction.

ECONOMICS
  entry at the ASK (taker), fee = ceil(0.07 * C * P * (1-P)) cents/contract, C=1,
  P = entry_price/100. Win pays (100 - entry). Loss pays -entry. Net = payoff - fee.
  No maker rebate (taker only).

HEADLINE: per-contract net cents, bucketed by RV regime (low/mid/high terciles).
Block bootstrap clustered by TICKER (the independent unit). BTC/ETH subsample reported.

DATA: coinbase_spot (BTC/ETH/SOL/XRP only -> HYPE/DOGE/BNB DATA_GAP), frames_crypto,
state.db thresholds+outcomes. Spot starts 05-30T21Z so windows restricted to >=21Z.
"""
from __future__ import annotations

import json
import math
import sqlite3
import sys
from bisect import bisect_right
from collections import defaultdict
from datetime import datetime, timezone

sys.path.insert(0, "/Users/gabrielkagan/Documents/kalshi-bot")

from scripts.research.kalshi_book_reconstruct import reliable_nbbo_at  # noqa: E402
from scripts.research.phase1b_real_price_economics import (  # noqa: E402
    close_epoch_from_ticker,
    load_frames_jsonl,
)

FRAMES = "/tmp/edge_daily/frames_crypto.jsonl"
SPOT = "/tmp/edge_daily/coinbase_spot.jsonl"
DB = "/tmp/edge_daily/state.db"

SPOT_ASSETS = ("BTC", "ETH", "SOL", "XRP")
PRODUCT = {a: f"{a}-USD" for a in SPOT_ASSETS}

LEAD_S = 180.0          # decision at close - 180s
RV_WINDOW_S = 300.0     # realized vol from last 300s of 1s spot returns
THRESH = 0.10           # |p_model - p_mkt| signal gate
MAX_TAKE_PRICE = 70     # only take the favored side if its ask < this (cents)
SPOT_START = datetime(2026, 5, 30, 21, 0, 0, tzinfo=timezone.utc).timestamp()
N_BOOT = 2000


def fee_cents(price_cents: float) -> float:
    p = price_cents / 100.0
    return math.ceil(0.07 * 1 * p * (1.0 - p))


def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _epoch_iso(s: str) -> float:
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    return datetime.fromisoformat(s).timestamp()


def load_spot() -> dict:
    """{asset: (times[], mids[])} sorted ascending. coinbase_spot is pre-flattened."""
    rev = {v: k for k, v in PRODUCT.items()}
    rows = defaultdict(list)
    with open(SPOT) as fh:
        for line in fh:
            if not line.strip():
                continue
            try:
                d = json.loads(line)
                a = rev.get(d["product_id"])
                if a is None:
                    continue
                mid = d.get("mid")
                if mid is None or mid <= 0:
                    continue
                rows[a].append((_epoch_iso(d["ts"]), float(mid)))
            except (ValueError, KeyError):
                continue
    out = {}
    for a, lst in rows.items():
        lst.sort(key=lambda x: x[0])
        out[a] = ([t for t, _ in lst], [m for _, m in lst])
    return out


def realized_vol(times, mids, decision_epoch):
    """Per-second realized vol of log returns over the trailing RV_WINDOW_S, all
    samples <= decision_epoch. Returns (rv_per_sqrt_sec_step, spot_at_decision, n)
    or (None, None, 0). rv is std of consecutive log-returns sampled ~1/sec."""
    hi = bisect_right(times, decision_epoch)
    lo = bisect_right(times, decision_epoch - RV_WINDOW_S)
    if hi - lo < 30:
        return None, None, 0
    win_t = times[lo:hi]
    win_m = mids[lo:hi]
    spot = win_m[-1]
    # downsample to ~1 obs/sec to make returns comparable across products
    sampled = []
    last_s = None
    for t, m in zip(win_t, win_m):
        s = int(t)
        if s != last_s:
            sampled.append((t, m))
            last_s = s
    if len(sampled) < 20:
        return None, None, 0
    rets = []
    for i in range(1, len(sampled)):
        m0, m1 = sampled[i - 1][1], sampled[i][1]
        if m0 > 0 and m1 > 0:
            rets.append(math.log(m1 / m0))
    if len(rets) < 15:
        return None, None, 0
    mu = sum(rets) / len(rets)
    var = sum((r - mu) ** 2 for r in rets) / (len(rets) - 1)
    rv = math.sqrt(var)  # per ~1s step
    if rv <= 0:
        return None, None, 0
    return rv, spot, len(rets)


def load_db_outcomes():
    """{ticker: (strike, result_yes_bool)} for in-window crypto-15M settled."""
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    out = {}
    sql = ("SELECT DISTINCT ticker, threshold, market_result FROM evaluated_opportunities "
           "WHERE ticker LIKE 'KX%15M-%' AND threshold IS NOT NULL "
           "AND market_result IN ('yes','no')")
    for r in conn.execute(sql):
        out[r["ticker"]] = (float(r["threshold"]), r["market_result"] == "yes")
    conn.close()
    return out


def asset_of(ticker):
    for a in SPOT_ASSETS:
        if ticker.startswith(f"KX{a}15M-"):
            return a
    return None


def block_bootstrap(rows, n_boot=N_BOOT, seed=7):
    """Cluster bootstrap by ticker. rows = list of (ticker, net_cents). Resample
    tickers with replacement; statistic = mean net over all trades in drawn tickers."""
    import random
    rng = random.Random(seed)
    by_tk = defaultdict(list)
    for tk, net in rows:
        by_tk[tk].append(net)
    keys = list(by_tk.keys())
    if not keys:
        return None, None, None
    point = sum(net for _, net in rows) / len(rows)
    means = []
    for _ in range(n_boot):
        pool = []
        for _ in range(len(keys)):
            pool.extend(by_tk[rng.choice(keys)])
        if pool:
            means.append(sum(pool) / len(pool))
    means.sort()
    lo = means[int(0.025 * len(means))]
    hi = means[int(0.975 * len(means))]
    return point, lo, hi


def regime_label(rv, lo_cut, hi_cut):
    if rv <= lo_cut:
        return "low"
    if rv >= hi_cut:
        return "high"
    return "mid"


def main():
    print("Loading spot...", flush=True)
    spot = load_spot()
    for a in SPOT_ASSETS:
        n = len(spot.get(a, ([],))[0])
        print(f"  spot {a}: {n} ticks", flush=True)

    print("Loading DB outcomes...", flush=True)
    outcomes = load_db_outcomes()
    print(f"  {len(outcomes)} settled tickers in DB", flush=True)

    print("Loading frames (this is the slow part, 4.4GB)...", flush=True)
    frames = load_frames_jsonl(FRAMES)
    print(f"  {len(frames)} tickers with reconstructed frames", flush=True)

    # First pass: compute per-ticker signal records (no fill decision yet — collect RV
    # to set regime cutoffs from the in-sample distribution).
    recs = []
    diag = defaultdict(int)
    for tk, frs in frames.items():
        a = asset_of(tk)
        if a is None:
            diag["not_spot_asset"] += 1
            continue
        if tk not in outcomes:
            diag["no_db_outcome"] += 1
            continue
        strike, won = outcomes[tk]
        close_ep = close_epoch_from_ticker(tk)
        if close_ep < SPOT_START + LEAD_S:
            diag["before_spot_start"] += 1
            continue
        decision = close_ep - LEAD_S
        times, mids = spot.get(a, ([], []))
        if not times:
            diag["no_spot_for_asset"] += 1
            continue
        rv, sp, nret = realized_vol(times, mids, decision)
        if rv is None:
            diag["no_rv"] += 1
            continue
        # reconstructed reliable YES book mid at decision
        yb, ya = reliable_nbbo_at(frs, decision)
        if yb is None or ya is None:
            diag["book_refused"] += 1
            continue
        p_mkt = (yb + ya) / 2.0 / 100.0
        if p_mkt <= 0 or p_mkt >= 1:
            diag["degenerate_mkt"] += 1
            continue
        # model digital prob: YES = P(spot_close >= strike). d = log(spot/strike).
        d = math.log(sp / strike)
        rem = LEAD_S  # remaining time at decision = close - decision = LEAD_S
        sigma_T = rv * math.sqrt(rem / 1.0)  # rv per ~1s step; rem seconds of steps
        if sigma_T <= 0:
            diag["zero_sigma"] += 1
            continue
        p_model = _norm_cdf(d / sigma_T)
        recs.append({
            "tk": tk, "asset": a, "strike": strike, "won": won,
            "rv": rv, "spot": sp, "p_mkt": p_mkt, "p_model": p_model,
            "yes_bid": yb, "yes_ask": ya,
        })

    print(f"Diagnostics: {dict(diag)}", flush=True)
    print(f"Usable signal records: {len(recs)}", flush=True)
    if len(recs) < 20:
        return ("DATA_GAP", len(recs), recs, None)

    # regime cutoffs = terciles of RV across usable records (per the WHY: VRP lives
    # in the calm/low regime). Pooled across assets but RV is per-step log-return std,
    # comparable across products.
    rvs = sorted(r["rv"] for r in recs)
    lo_cut = rvs[len(rvs) // 3]
    hi_cut = rvs[2 * len(rvs) // 3]

    # decide trades
    trades = []  # (ticker, net_cents, regime, asset, side)
    for r in recs:
        gap = r["p_model"] - r["p_mkt"]
        if abs(gap) <= THRESH:
            continue
        # p_model favors YES if p_model > p_mkt; favors NO if p_model < p_mkt.
        if gap > 0:
            side = "yes"
            ask = r["yes_ask"]          # cross to YES ask
            entry = ask
            win = r["won"]
        else:
            side = "no"
            # NO ask = 100 - YES bid (taker buying NO crosses the YES bid side)
            entry = 100 - r["yes_bid"]
            win = not r["won"]
        if entry is None or entry <= 0 or entry >= 100:
            continue
        if entry >= MAX_TAKE_PRICE:   # only take the cheap favored side
            continue
        fee = fee_cents(entry)
        payoff = (100 - entry) if win else (-entry)
        net = payoff - fee
        regime = regime_label(r["rv"], lo_cut, hi_cut)
        trades.append((r["tk"], net, regime, r["asset"], side))

    print(f"\nTrades taken: {len(trades)}", flush=True)
    return ("OK", len(recs), recs, (trades, lo_cut, hi_cut))


def report(status, n_recs, recs, payload):
    if status == "DATA_GAP" or payload is None:
        return
    trades, lo_cut, hi_cut = payload
    if not trades:
        print("No trades passed the signal+price gate -> NO tradeable signal.")
        return

    rows_all = [(tk, net) for tk, net, _, _, _ in trades]
    print(f"\nRV regime cutoffs (per-step log-ret std): low<= {lo_cut:.2e}  high>= {hi_cut:.2e}")
    print("\n=== OVERALL (all spot assets, taker, fee-net) ===")
    p, lo, hi = block_bootstrap(rows_all)
    n_tk = len({tk for tk, _ in rows_all})
    print(f"  n_trades={len(rows_all)} n_tickers={n_tk}  mean net = {p:+.3f}c  CI95 [{lo:+.3f}, {hi:+.3f}]")

    for reg in ("low", "mid", "high"):
        sub = [(tk, net) for tk, net, r, _, _ in trades if r == reg]
        if not sub:
            print(f"  [{reg:>4}] no trades")
            continue
        pp, ll, hh = block_bootstrap(sub)
        print(f"  [{reg:>4}] n={len(sub)} tk={len({t for t,_ in sub})} mean {pp:+.3f}c CI95 [{ll:+.3f},{hh:+.3f}]")

    print("\n=== BTC/ETH SUBSAMPLE (tightest spot) ===")
    sub = [(tk, net) for tk, net, _, a, _ in trades if a in ("BTC", "ETH")]
    if sub:
        pp, ll, hh = block_bootstrap(sub)
        print(f"  n={len(sub)} tk={len({t for t,_ in sub})} mean {pp:+.3f}c CI95 [{ll:+.3f},{hh:+.3f}]")
    else:
        print("  none")

    return p, lo, hi, len(rows_all)


if __name__ == "__main__":
    status, n_recs, recs, payload = main()
    report(status, n_recs, recs, payload)
