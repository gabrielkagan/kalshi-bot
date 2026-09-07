"""cross_asset_dispersion_strike_basis_taker
=================================================

Family: cross-asset common-factor lead (REALIZED beta via causal PCA-1, not a
fixed pairwise BTC beta).

Hypothesis
----------
Prior cross-asset leads (btc_beta_alt_fade, btc_impulse_alt_stale_taker) used a
FIXED single-leader beta and bled on idiosyncratic BTC noise. Here we condition
on the realized COMMON FACTOR: per window, causal first-PC of the 4 demeaned
log-return series of BTC/ETH/SOL/XRP (spot <= decision time only). The asset's
loading b_a on the factor times the recent factor move Delta_f predicts a signed
return for the alt over (t-LAG, t). When that factor impulse is large (top decile
for the asset) AND the Kalshi reliable mid hasn't yet moved (within 1.5c over the
same lag), the thin/slow 15M alt book is presumed to lag the market-wide impulse.
We TAKE in the predicted direction (cross the reliable ask), pay the taker fee,
settle to the TERMINAL reliable-book outcome at close.

Honest boundaries
-----------------
* ETH / SOL / XRP only (BTC is a factor input; DOGE/BNB/HYPE have NO local
  coinbase spot -> excluded, stated, not fabricated).
* Strike = DB `evaluated_opportunities.threshold` (spot-price units). The Kalshi
  15M ticker suffix is the close MINUTE (00/15/30/45), NOT the strike, so the
  spec's "parse suffix" does not apply to this ticker format -- the threshold
  lives in the DB. Windows with no DB threshold are skipped (named DATA_GAP cause).
* Outcome label = TERMINAL reliable book mid at close_epoch (NOT the DB), per the
  rules: in-window 5/30-31 settlements are sparse. A window with no reliable
  terminal book is dropped (cannot label honestly).
* Fees: ceil(7 * P * (1-P)) cents/contract, C=1, taker. Maker rebate = 0
  (we are crossing -> taker only).
* No look-ahead: signal uses spot<=t and Kalshi book<=t; label book is built
  independently and read at close.
* Bootstrap CI: block bootstrap clustered by (ticker, hour) -- the true
  independent unit -- >= 1000 resamples. CI straddling zero => NOT an edge.

Run:
  cd /Users/gabrielkagan/Documents/kalshi-bot
  python3 scripts/research/algo_zoo/cross_asset_dispersion_strike_basis_taker.py
"""
from __future__ import annotations

import json
import math
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime

sys.path.insert(0, "/Users/gabrielkagan/Documents/kalshi-bot")

import numpy as np

from scripts.research import kalshi_book_reconstruct as kbr
from scripts.research.phase1b_real_price_economics import (
    close_epoch_from_ticker,
    load_frames_jsonl,
)

FRAMES_PATH = "/tmp/edge_daily/frames_crypto.jsonl"
SPOT_PATH = "/tmp/edge_daily/coinbase_spot.jsonl"
DB_PATH = "/tmp/edge_daily/state.db"

ASSETS = ("ETH", "SOL", "XRP")          # tradeable alts (have spot)
FACTOR_PRODUCTS = ("BTC-USD", "ETH-USD", "SOL-USD", "XRP-USD")
PROD_OF = {"ETH": "ETH-USD", "SOL": "SOL-USD", "XRP": "XRP-USD"}

WINDOW_LEN_S = 15 * 60
WARMUP_S = 10 * 60          # restrict grid to t after >=10min into the window
LAG_S = 30                  # factor-move / mid-stale measurement lag
MARKOUT_S = 30             # markout horizon for the secondary gate
GRID_STEP_S = 30           # decision-time grid spacing inside a window
MID_STALE_CENTS = 1.5      # Kalshi reliable-mid unchanged tolerance
TOP_DECILE = 0.90          # |Delta_f| top-decile gate (per asset)

N_BOOT = 2000
RNG = np.random.default_rng(20260531)


def _epoch(iso: str) -> float:
    s = iso.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    return datetime.fromisoformat(s).timestamp()


def _fee_cents(price_cents: float) -> float:
    """ceil(7 * P * (1-P)) cents/contract (Kalshi rounds per-order fee up)."""
    p = price_cents / 100.0
    return float(math.ceil(7.0 * p * (1.0 - p)))


# ----------------------------------------------------------------------------
# Spot loading: per-product 1s-resampled (forward-filled) mid series.
# ----------------------------------------------------------------------------
def load_spot_series():
    raw = defaultdict(list)
    with open(SPOT_PATH) as fh:
        for line in fh:
            if not line.strip():
                continue
            try:
                d = json.loads(line)
                p = d["product_id"]
                mid = float(d["mid"])
                ts = _epoch(d["ts"])
            except (ValueError, KeyError):
                continue
            if p in FACTOR_PRODUCTS:
                raw[p].append((ts, mid))
    series = {}
    g_max = None
    for p, rows in raw.items():
        rows.sort(key=lambda x: x[0])
        ts0 = math.floor(rows[0][0])
        ts1 = math.ceil(rows[-1][0])
        grid = np.arange(ts0, ts1 + 1, 1.0)
        ep = np.array([r[0] for r in rows])
        mid = np.array([r[1] for r in rows])
        idx = np.searchsorted(ep, grid, side="right") - 1
        idx = np.clip(idx, 0, len(mid) - 1)
        series[p] = (grid, mid[idx])
        g_max = ts1 if g_max is None else min(g_max, ts1)
    return series, g_max


def mid_at_spot(series, product, t):
    grid, mid = series[product]
    i = int(t - grid[0])
    if i < 0 or i >= len(mid):
        return None
    return float(mid[i])


# ----------------------------------------------------------------------------
# Causal common factor: first PC of 4 demeaned log-return series over [open, t].
# ----------------------------------------------------------------------------
def causal_factor_and_loading(series, asset, w_open, t):
    cols = FACTOR_PRODUCTS
    t0 = math.ceil(w_open)
    t1 = math.floor(t)
    if t1 - t0 < 60:
        return None
    mids = []
    for p in cols:
        grid, m = series[p]
        i0 = int(t0 - grid[0])
        i1 = int(t1 - grid[0])
        if i0 < 1 or i1 >= len(m) or i1 <= i0:
            return None
        mids.append(m[i0 - 1: i1 + 1])
    L = min(len(x) for x in mids)
    mids = [x[-L:] for x in mids]
    M = np.column_stack(mids)
    if np.any(M <= 0):
        return None
    logret = np.diff(np.log(M), axis=0)
    if logret.shape[0] < 30:
        return None
    Rd = logret - logret.mean(axis=0, keepdims=True)
    try:
        U, S, Vt = np.linalg.svd(Rd, full_matrices=False)
    except np.linalg.LinAlgError:
        return None
    v1 = Vt[0]
    if np.dot(v1, Rd.mean(axis=0)) < 0:
        v1 = -v1
    f = Rd @ v1
    a_col = cols.index(PROD_OF[asset])
    b_a = float(v1[a_col])
    lag_steps = min(LAG_S, len(f))
    delta_f = float(np.sum(f[-lag_steps:]))
    return b_a, delta_f


def reliable_mid_at(frames, cutoff):
    yb, ya = kbr.reliable_nbbo_at(frames, cutoff)
    if yb is None or ya is None:
        return None, None, None
    return (yb + ya) / 2.0, yb, ya


# ----------------------------------------------------------------------------
# Main backtest.
# ----------------------------------------------------------------------------
def main():
    print("[1/5] loading spot ...", flush=True)
    series, g_max = load_spot_series()
    spot_first = min(series[p][0][0] for p in FACTOR_PRODUCTS)
    warmup_floor = spot_first + 600.0
    print(f"  spot products: {list(series)}  end={g_max}", flush=True)

    print("[2/5] opening DB ...", flush=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    print("[3/5] loading frames (crypto 15M) ...", flush=True)
    frames_by_ticker = load_frames_jsonl(FRAMES_PATH)
    tickers = [tk for tk in frames_by_ticker
               if any(tk.startswith(f"KX{a}15M-") for a in ASSETS)]
    print(f"  ETH/SOL/XRP tickers with frames: {len(tickers)}", flush=True)

    strikes = {}
    for tk in tickers:
        r = conn.execute(
            "SELECT threshold FROM evaluated_opportunities "
            "WHERE ticker=? AND threshold IS NOT NULL LIMIT 1", (tk,)).fetchone()
        if r:
            strikes[tk] = float(r["threshold"])
    print(f"  tickers with DB strike: {len(strikes)}", flush=True)

    print("[4/5] generating candidate decisions ...", flush=True)
    raw_candidates = []
    n_windows = 0
    n_no_strike = 0
    n_no_term = 0
    for tk in tickers:
        asset = next(a for a in ASSETS if tk.startswith(f"KX{a}15M-"))
        if tk not in strikes:
            n_no_strike += 1
            continue
        close_ep = close_epoch_from_ticker(tk)
        w_open = close_ep - WINDOW_LEN_S
        frames = frames_by_ticker[tk]
        if not frames:
            continue
        if w_open < warmup_floor or close_ep > g_max:
            continue
        n_windows += 1
        strike = strikes[tk]
        term_mid, _, _ = reliable_mid_at(frames, close_ep)
        if term_mid is None:
            n_no_term += 1
            continue
        won_yes = term_mid >= 50.0
        t = w_open + WARMUP_S
        while t <= close_ep - LAG_S - MARKOUT_S:
            if t < warmup_floor:
                t += GRID_STEP_S
                continue
            fac = causal_factor_and_loading(series, asset, w_open, t)
            if fac is None:
                t += GRID_STEP_S
                continue
            b_a, delta_f = fac
            s_t = mid_at_spot(series, PROD_OF[asset], t)
            if s_t is None:
                t += GRID_STEP_S
                continue
            pred_ret = b_a * delta_f
            implied_spot = s_t + s_t * pred_ret
            buy_yes = implied_spot >= strike
            mid_t, yb_t, ya_t = reliable_mid_at(frames, t)
            mid_lag, _, _ = reliable_mid_at(frames, t - LAG_S)
            if mid_t is None or mid_lag is None:
                t += GRID_STEP_S
                continue
            mid_unchanged = abs(mid_t - mid_lag) <= MID_STALE_CENTS
            raw_candidates.append({
                "ticker": tk, "asset": asset, "t": t, "close_ep": close_ep,
                "strike": strike, "s_t": s_t, "b_a": b_a, "delta_f": delta_f,
                "buy_yes": buy_yes, "mid_unchanged": mid_unchanged,
                "yes_bid": yb_t, "yes_ask": ya_t, "won_yes": won_yes,
            })
            t += GRID_STEP_S

    print(f"  windows used: {n_windows}  (no_strike={n_no_strike} no_term={n_no_term})",
          flush=True)
    print(f"  raw grid candidates: {len(raw_candidates)}", flush=True)

    thr = {}
    for a in ASSETS:
        vals = [abs(c["delta_f"]) for c in raw_candidates if c["asset"] == a]
        thr[a] = float(np.quantile(vals, TOP_DECILE)) if vals else float("inf")
    print(f"  per-asset |delta_f| 90pct thresholds: "
          f"{ {a: round(v, 6) for a, v in thr.items()} }", flush=True)

    print("[5/5] applying gate + simulating taker fills ...", flush=True)
    trades = []
    n_gate_mid = 0
    n_gate_decile = 0
    for c in raw_candidates:
        if not c["mid_unchanged"]:
            continue
        n_gate_mid += 1
        if abs(c["delta_f"]) < thr[c["asset"]]:
            continue
        n_gate_decile += 1
        if c["buy_yes"]:
            if c["yes_ask"] is None:
                continue
            entry = c["yes_ask"]
            if entry <= 0 or entry >= 100:
                continue
            won = c["won_yes"]
        else:
            if c["yes_bid"] is None:
                continue
            entry = 100.0 - c["yes_bid"]
            if entry <= 0 or entry >= 100:
                continue
            won = not c["won_yes"]
        gross = (100.0 - entry) if won else (-entry)
        net = gross - _fee_cents(entry)
        frames = frames_by_ticker[c["ticker"]]
        mid_now, _, _ = reliable_mid_at(frames, c["t"])
        mid_fwd, _, _ = reliable_mid_at(frames, c["t"] + MARKOUT_S)
        markout = None
        if mid_now is not None and mid_fwd is not None:
            move = mid_fwd - mid_now
            markout = move if c["buy_yes"] else -move
        hour = int(c["t"] // 3600)
        trades.append({
            "ticker": c["ticker"], "asset": c["asset"], "hour": hour,
            "net": net, "markout": markout, "side": "yes" if c["buy_yes"] else "no",
            "entry": entry,
        })
    print(f"  passed mid-stale gate: {n_gate_mid}  passed decile gate: {n_gate_decile}",
          flush=True)
    return summarize(trades)


def block_bootstrap_ci(values_by_cluster, n_boot=N_BOOT):
    clusters = list(values_by_cluster.values())
    all_vals = [v for c in clusters for v in c]
    if not all_vals:
        return None
    point = float(np.mean(all_vals))
    k = len(clusters)
    means = np.empty(n_boot)
    for b in range(n_boot):
        pick = RNG.integers(0, k, size=k)
        pooled = []
        for i in pick:
            pooled.extend(clusters[i])
        means[b] = np.mean(pooled) if pooled else 0.0
    lo, hi = np.percentile(means, [2.5, 97.5])
    return point, float(lo), float(hi), len(all_vals), k


def summarize(trades):
    print(f"\n  total gated taker trades: {len(trades)}", flush=True)
    result = {"n_total": len(trades), "per_asset": {}, "overall": None}
    if not trades:
        print("  NO TRADES survived the gate.", flush=True)
        return result
    by_cluster = defaultdict(list)
    for tr in trades:
        by_cluster[(tr["ticker"], tr["hour"])].append(tr["net"])
    ov = block_bootstrap_ci(by_cluster)
    result["overall"] = {"net": ov}
    print(f"  OVERALL net cents/contract: point={ov[0]:.3f} "
          f"CI[{ov[1]:.3f},{ov[2]:.3f}] n={ov[3]} clusters={ov[4]}", flush=True)
    for a in ASSETS:
        atr = [tr for tr in trades if tr["asset"] == a]
        if not atr:
            print(f"  {a}: no trades", flush=True)
            continue
        net_cl = defaultdict(list)
        mk_cl = defaultdict(list)
        for tr in atr:
            net_cl[(tr["ticker"], tr["hour"])].append(tr["net"])
            if tr["markout"] is not None:
                mk_cl[(tr["ticker"], tr["hour"])].append(tr["markout"])
        net_ci = block_bootstrap_ci(net_cl)
        mk_ci = block_bootstrap_ci(mk_cl) if mk_cl else None
        result["per_asset"][a] = {"net": net_ci, "markout": mk_ci, "n": len(atr)}
        nstr = (f"net point={net_ci[0]:.3f} CI[{net_ci[1]:.3f},{net_ci[2]:.3f}]"
                if net_ci else "net n/a")
        mstr = (f"markout@30s point={mk_ci[0]:.3f} CI[{mk_ci[1]:.3f},{mk_ci[2]:.3f}]"
                if mk_ci else "markout n/a")
        print(f"  {a}: n={len(atr)}  {nstr}  {mstr}", flush=True)
    return result


if __name__ == "__main__":
    res = main()
    print("\n=== GATE VERDICT (per-asset CI-lower>0 net of fee AND markout@30s>0) ===")
    any_edge = False
    for a, d in res.get("per_asset", {}).items():
        net = d.get("net"); mk = d.get("markout")
        net_ok = net is not None and net[1] > 0
        mk_ok = mk is not None and mk[1] > 0
        edge = net_ok and mk_ok
        any_edge = any_edge or edge
        print(f"  {a}: net_CI_lower>0={net_ok}  markout_CI_lower>0={mk_ok}  EDGE={edge}")
    print(f"\nANY EDGE: {any_edge}")
