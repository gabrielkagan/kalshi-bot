"""btc_impulse_alt_stale_taker — cross-asset lead-lag latency capture (directional taker).

HYPOTHESIS
----------
A sharp BTC spot impulse (>= J bps over <= 2s on the local coinbase BTC-USD feed)
leads its alts (ETH/SOL/XRP). The alt's 15M Kalshi book is operator/quote-latency
bound and may LAG the BTC-led move by seconds. If, tau seconds after a BTC impulse,
the alt Kalshi reliable mid has NOT yet moved by the beta-implied amount, the book is
"stale" and we directionally TAKE it (YES if implied up & alt should rise above
strike; short-YES/NO if down) at its ask/bid, then EXIT at the alt reliable mid H
seconds later -- pure catch-up capture, NOT held to settlement.

DESIGN / ANTI-LOOK-AHEAD
------------------------
- Beta estimated by OLS of alt-spot returns vs BTC-spot returns on a STRICTLY PRIOR
  window (ending at impulse start t0). No label data touches the signal.
- Implied kalshi prob shift = Gaussian-CDF strike-distance model evaluated at t0
  (entry-time inputs only) before vs after the beta-implied alt move.
- Entry book read at t0+tau via reliable_nbbo_at (snapshot-anchored, REFUSES drifted
  books -- we honor the refusal). Exit book read at t0+tau+H independently.
- Full outcome distribution (incl. noise impulses where BTC reverts and the alt
  never catches up -> losers) is in the reported mean. No survivorship.

COST MODEL
----------
- Kalshi taker fee: ceil(0.07 * C * P * (1-P)) dollars, P in [0,1] dollars. BOTH legs
  (entry taker + exit taker). Maker rebate = 0 (taker on both legs).
- Entry crosses the spread (pay ask for YES / sell at bid for short-YES). Exit also
  taker (cross to close). We report mean NET cents/contract.

VERDICT RULE
------------
- EDGE only if mean net cents/trade > 0 AND cluster-bootstrap CI (by impulse event)
  excludes zero. Anything weaker -> NO_EDGE / INCONCLUSIVE. Missing inputs -> DATA_GAP.

DATA
----
- coinbase_spot.jsonl (parsed: {ts, product_id, mid, bid, ask, last}). BTC+ETH+SOL+XRP.
  Spot starts 2026-05-30T21:00Z -> only the spot-covered subwindow yields signals.
- frames_crypto.jsonl (kalshi_ws orderbook envelopes; _raw is a JSON string).
- ETH/SOL/XRP only. BTC is the leader; HYPE/DOGE/BNB have NO local coinbase mid ->
  excluded (DATA_GAP for those, named in lookahead_risks).
"""

from __future__ import annotations

import json
import math
import os
import sys
from bisect import bisect_right
from collections import defaultdict
from datetime import datetime, timezone

sys.path.insert(0, "/Users/gabrielkagan/Documents/kalshi-bot")

from scripts.research.kalshi_book_reconstruct import reliable_nbbo_at  # noqa: E402
from scripts.research.phase1b_real_price_economics import (  # noqa: E402
    close_epoch_from_ticker,
)

SPOT = "/tmp/edge_daily/coinbase_spot.jsonl"
FRAMES = "/tmp/edge_daily/frames_crypto.jsonl"
DB = "/tmp/edge_daily/state.db"

ALTS = ("ETH", "SOL", "XRP")
ALT_PRODUCT = {"ETH": "ETH-USD", "SOL": "SOL-USD", "XRP": "XRP-USD"}

# --- signal params (env-overridable for sweeps) ---
J_BPS = float(os.environ.get("J_BPS", 4.0))          # min BTC impulse (bps) over <=2s
IMPULSE_WIN_S = 2.0    # impulse measured over <= this many seconds
TAU_S = float(os.environ.get("TAU_S", 2.0))          # read alt book at t0 + tau
H_LIST = (10.0, 30.0)  # exit horizons (catch-up capture window)
BETA_WIN_S = 120.0     # strictly-prior OLS window length (seconds) for beta
BETA_STEP_S = 1.0      # resample cadence for beta returns
MIN_BETA_PTS = 20      # require this many return points to trust beta
STALE_FRAC = float(os.environ.get("STALE_FRAC", 0.5))  # realized < frac*implied move
IMPULSE_COOLDOWN_S = 30.0  # per leader cooldown so one move isn't double-counted
SIGMA_FLOOR_BPS = 5.0  # floor on per-asset return vol used by the prob model
MIN_DPROB = float(os.environ.get("MIN_DPROB", 0.0))  # min implied prob shift to act
                                                     # (0 = keep full distribution
                                                     #  incl. noise impulses, per spec)

N_BOOT = 2000


def _epoch(ts: str) -> float:
    return datetime.strptime(ts, "%Y-%m-%dT%H:%M:%S.%fZ").replace(
        tzinfo=timezone.utc
    ).timestamp()


# ----------------------------------------------------------------------------
# Spot loading
# ----------------------------------------------------------------------------
def load_spot():
    """{product_id: ([epochs_sorted], [mids])} for BTC + alts."""
    series = defaultdict(lambda: ([], []))
    with open(SPOT) as fh:
        for line in fh:
            if not line.strip():
                continue
            try:
                r = json.loads(line)
                pid = r["product_id"]
                mid = float(r["mid"])
                ep = _epoch(r["ts"])
            except (ValueError, KeyError, TypeError):
                continue
            ts_arr, mid_arr = series[pid]
            ts_arr.append(ep)
            mid_arr.append(mid)
    out = {}
    for pid, (ts_arr, mid_arr) in series.items():
        pairs = sorted(zip(ts_arr, mid_arr))
        out[pid] = ([p[0] for p in pairs], [p[1] for p in pairs])
    return out


def mid_at(series, pid, t):
    """Last mid at-or-before t (no look-ahead). None if before coverage."""
    ts_arr, mid_arr = series.get(pid, ([], []))
    if not ts_arr:
        return None
    i = bisect_right(ts_arr, t) - 1
    if i < 0:
        return None
    return mid_arr[i]


# ----------------------------------------------------------------------------
# Impulse detection on BTC
# ----------------------------------------------------------------------------
def detect_btc_impulses(series):
    """An impulse fires when |ret| over a <=IMPULSE_WIN_S window exceeds J_BPS.
    Returns (t0, ret_bps, btc_p0, btc_p1); t0 = impulse START (decision anchor;
    we act at t0+tau). Cooldown so a single move fires once."""
    ts_arr, mid_arr = series.get("BTC-USD", ([], []))
    impulses = []
    last_fire = -1e18
    n = len(ts_arr)
    for i in range(1, n):
        t1 = ts_arr[i]
        j0 = bisect_right(ts_arr, t1 - IMPULSE_WIN_S)
        if j0 >= i:
            continue
        p_start = mid_arr[j0]
        p_end = mid_arr[i]
        if p_start <= 0:
            continue
        ret_bps = (p_end / p_start - 1.0) * 1e4
        if abs(ret_bps) < J_BPS:
            continue
        t0 = ts_arr[j0]
        if t0 - last_fire < IMPULSE_COOLDOWN_S:
            continue
        last_fire = t0
        impulses.append((t0, ret_bps, p_start, p_end))
    return impulses


# ----------------------------------------------------------------------------
# Beta on strictly-prior window
# ----------------------------------------------------------------------------
def _grid(t0):
    g = []
    t = t0 - BETA_WIN_S
    while t <= t0 + 1e-9:
        g.append(t)
        t += BETA_STEP_S
    return g


def estimate_beta(series, alt_pid, t0):
    """OLS slope of alt-returns on BTC-returns over [t0-BETA_WIN_S, t0], strictly
    prior. (beta, n_pts) or (None, n)."""
    grid = _grid(t0)
    btc_p = [mid_at(series, "BTC-USD", g) for g in grid]
    alt_p = [mid_at(series, alt_pid, g) for g in grid]
    xs, ys = [], []
    for k in range(1, len(grid)):
        if (btc_p[k] is None or btc_p[k - 1] is None or btc_p[k - 1] <= 0
                or alt_p[k] is None or alt_p[k - 1] is None or alt_p[k - 1] <= 0):
            continue
        xs.append(btc_p[k] / btc_p[k - 1] - 1.0)
        ys.append(alt_p[k] / alt_p[k - 1] - 1.0)
    if len(xs) < MIN_BETA_PTS:
        return None, len(xs)
    n = len(xs)
    mx = sum(xs) / n
    my = sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    if sxx <= 1e-18:
        return None, n
    return sxy / sxx, n


def alt_return_vol_per_s(series, alt_pid, t0):
    """Std of alt 1s fractional returns over the prior window. Floored."""
    grid = _grid(t0)
    p = [mid_at(series, alt_pid, g) for g in grid]
    rets = []
    for k in range(1, len(grid)):
        if p[k] is None or p[k - 1] is None or p[k - 1] <= 0:
            continue
        rets.append(p[k] / p[k - 1] - 1.0)
    floor = SIGMA_FLOOR_BPS / 1e4
    if len(rets) < 5:
        return floor
    m = sum(rets) / len(rets)
    var = sum((r - m) ** 2 for r in rets) / (len(rets) - 1)
    return max(math.sqrt(var), floor)


# ----------------------------------------------------------------------------
# Strike-distance prob model (entry-time inputs only)
# ----------------------------------------------------------------------------
_NORM = 1.0 / math.sqrt(2.0)


def _phi(z):
    return 0.5 * (1.0 + math.erf(z * _NORM))


def implied_prob_shift(alt_spot, strike, beta_move_frac, sigma_per_sqrt_s,
                       secs_to_close):
    """P(close above strike) under a Gaussian terminal model, before vs after the
    beta-implied alt move. Returns (dprob, direction). direction=+1 means YES prob
    rises."""
    if alt_spot <= 0 or strike <= 0 or secs_to_close <= 0:
        return 0.0, 0
    sig_total = sigma_per_sqrt_s * math.sqrt(secs_to_close)
    if sig_total <= 1e-9:
        return 0.0, 0
    z0 = math.log(alt_spot / strike) / sig_total
    p0 = _phi(z0)
    alt_after = alt_spot * (1.0 + beta_move_frac)
    if alt_after <= 0:
        return 0.0, 0
    z1 = math.log(alt_after / strike) / sig_total
    p1 = _phi(z1)
    dprob = p1 - p0
    direction = 1 if dprob > 0 else (-1 if dprob < 0 else 0)
    return dprob, direction


# ----------------------------------------------------------------------------
# Strike / window helpers
# ----------------------------------------------------------------------------
def load_strikes_db(db_path, like_prefixes):
    """{ticker: strike(threshold)} from evaluated_opportunities. The strike is the
    above/below level FIXED at window open (announced at open), so reading it for an
    in-window decision is NOT look-ahead -- it's a static window attribute. The
    ticker's trailing segment is a MINUTE marker, not a price; the real strike lives
    only in the DB threshold column (verified against bronze snapshots)."""
    import sqlite3
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA busy_timeout=10000")
    conn.row_factory = sqlite3.Row
    out = {}
    for pre in like_prefixes:
        sql = ("SELECT DISTINCT ticker, threshold FROM evaluated_opportunities "
               f"WHERE ticker LIKE '{pre}-%' AND threshold IS NOT NULL")
        for r in conn.execute(sql):
            out[r["ticker"]] = float(r["threshold"])
    conn.close()
    return out


def window_open_epoch(close_ep):
    return close_ep - 900.0


# ----------------------------------------------------------------------------
# Fee model: Kalshi taker fee = ceil(0.07 * C * P * (1-P)) in dollars (P in dollars).
# ----------------------------------------------------------------------------
def kalshi_taker_fee_cents(price_cents, contracts=1):
    p = max(0.0, min(1.0, price_cents / 100.0))
    fee_dollars = math.ceil(0.07 * contracts * p * (1.0 - p) * 100.0) / 100.0
    return fee_dollars * 100.0  # cents


# ----------------------------------------------------------------------------
# Main backtest
# ----------------------------------------------------------------------------
def run():
    if not (os.path.exists(SPOT) and os.path.exists(FRAMES)):
        return {"data_available": False, "trades": [], "reason": "missing local files"}

    print("[1/4] loading spot ...", flush=True)
    series = load_spot()
    have = {p: len(series.get(p, ([], []))[0]) for p in
            ("BTC-USD", "ETH-USD", "SOL-USD", "XRP-USD")}
    print("       spot points:", have, flush=True)
    if have["BTC-USD"] == 0:
        return {"data_available": False, "trades": [], "reason": "no BTC spot"}
    btc_ts = series["BTC-USD"][0]
    spot_lo, spot_hi = btc_ts[0], btc_ts[-1]
    print(f"       BTC spot coverage {datetime.utcfromtimestamp(spot_lo)}Z "
          f"-> {datetime.utcfromtimestamp(spot_hi)}Z", flush=True)

    print("[2/4] detecting BTC impulses ...", flush=True)
    impulses = detect_btc_impulses(series)
    print(f"       {len(impulses)} impulses (J={J_BPS}bps over <={IMPULSE_WIN_S}s)",
          flush=True)
    if not impulses:
        return {"data_available": True, "trades": [], "reason": "no impulses",
                "n_impulses": 0}

    imp_ts = [im[0] for im in impulses]
    imp_lo = min(imp_ts) - 60.0
    imp_hi = max(imp_ts) + max(H_LIST) + TAU_S + 60.0

    strikes = {}
    if os.path.exists(DB):
        strikes = load_strikes_db(DB, ("KXETH15M", "KXSOL15M", "KXXRP15M"))
        print(f"       loaded {len(strikes)} ticker strikes from DB", flush=True)
    if not strikes:
        return {"data_available": False, "trades": [],
                "reason": "no DB strikes (threshold)", "n_impulses": len(impulses)}

    print("[3/4] streaming alt frames (ETH/SOL/XRP) into per-ticker books ...",
          flush=True)
    frames_by_tk = defaultdict(list)
    tk_meta = {}
    kept_lines = 0
    with open(FRAMES) as fh:
        for line in fh:
            if ("KXETH15M" not in line and "KXSOL15M" not in line
                    and "KXXRP15M" not in line):
                continue
            try:
                env = json.loads(line)
                inner = json.loads(env["_raw"])
            except (ValueError, KeyError):
                continue
            tk = inner.get("msg", {}).get("market_ticker", "")
            if not tk.startswith(("KXETH15M", "KXSOL15M", "KXXRP15M")):
                continue
            if tk not in tk_meta:
                asset = ("ETH" if tk.startswith("KXETH15M") else
                         "SOL" if tk.startswith("KXSOL15M") else "XRP")
                try:
                    close_ep = close_epoch_from_ticker(tk)
                except Exception:
                    tk_meta[tk] = None
                    continue
                strike = strikes.get(tk)
                open_ep = window_open_epoch(close_ep)
                if close_ep < imp_lo or open_ep > imp_hi or strike is None:
                    tk_meta[tk] = None
                else:
                    tk_meta[tk] = (asset, strike, open_ep, close_ep)
            meta = tk_meta[tk]
            if meta is None:
                continue
            ep = _epoch(env["_wire_recv_ts"])
            frames_by_tk[tk].append((ep, inner))
            kept_lines += 1
    for tk in frames_by_tk:
        frames_by_tk[tk].sort(key=lambda x: x[0])
    print(f"       kept {kept_lines} frames across "
          f"{len(frames_by_tk)} relevant tickers", flush=True)

    print("[4/4] evaluating impulse x alt-window pairs ...", flush=True)
    tickers_by_asset = defaultdict(list)
    for tk, meta in tk_meta.items():
        if meta is None or tk not in frames_by_tk:
            continue
        tickers_by_asset[meta[0]].append(tk)

    trades = []
    diag = defaultdict(int)
    diag_dprobs = []
    diag_z = []

    for imp_idx, (t0, ret_bps, btc_p0, btc_p1) in enumerate(impulses):
        t_entry = t0 + TAU_S
        for asset in ALTS:
            alt_pid = ALT_PRODUCT[asset]
            active = None
            for tk in tickers_by_asset.get(asset, []):
                a, strike, open_ep, close_ep = tk_meta[tk]
                if open_ep <= t_entry <= close_ep - (max(H_LIST) + TAU_S):
                    active = tk
                    break
            if active is None:
                diag["no_active_window"] += 1
                continue
            a, strike, open_ep, close_ep = tk_meta[active]
            secs_to_close = close_ep - t0
            frames = frames_by_tk[active]

            beta, n_beta = estimate_beta(series, alt_pid, t0)
            if beta is None:
                diag["no_beta"] += 1
                continue
            sigma_per_sqrt_s = alt_return_vol_per_s(series, alt_pid, t0)

            btc_move_frac = ret_bps / 1e4
            beta_move_frac = beta * btc_move_frac
            if abs(beta_move_frac) < 1e-6:
                diag["tiny_implied_move"] += 1
                continue

            alt_spot_t0 = mid_at(series, alt_pid, t0)
            if alt_spot_t0 is None or alt_spot_t0 <= 0:
                diag["no_alt_spot"] += 1
                continue

            dprob, direction = implied_prob_shift(
                alt_spot_t0, strike, beta_move_frac,
                sigma_per_sqrt_s, secs_to_close)
            diag_dprobs.append(abs(dprob))
            if strike > 0 and alt_spot_t0 > 0:
                _sig_tot = sigma_per_sqrt_s * math.sqrt(max(secs_to_close, 1e-9))
                diag_z.append(
                    abs(math.log(alt_spot_t0 / strike) / max(_sig_tot, 1e-9)))
            if direction == 0 or abs(dprob) < MIN_DPROB:
                diag["tiny_dprob"] += 1
                continue
            implied_cent_shift = dprob * 100.0

            yb, ya = reliable_nbbo_at(frames, t_entry)
            if yb is None or ya is None:
                diag["entry_book_unreliable"] += 1
                continue
            mid_entry = (yb + ya) / 2.0
            if (ya - yb) < 0:
                diag["crossed_entry"] += 1
                continue

            yb0, ya0 = reliable_nbbo_at(frames, t0)
            if yb0 is None or ya0 is None:
                diag["base_book_unreliable"] += 1
                continue
            mid_t0 = (yb0 + ya0) / 2.0
            realized_shift = mid_entry - mid_t0
            realized_in_dir = realized_shift if direction > 0 else -realized_shift
            implied_in_dir = abs(implied_cent_shift)
            if realized_in_dir >= STALE_FRAC * implied_in_dir:
                diag["not_stale"] += 1
                continue

            if direction > 0:
                entry_px = ya          # buy YES at ask
            else:
                entry_px = yb          # short YES at bid
            fee_in = kalshi_taker_fee_cents(entry_px)

            for H in H_LIST:
                t_exit = t_entry + H
                if t_exit > close_ep:
                    diag["exit_past_close"] += 1
                    continue
                ybx, yax = reliable_nbbo_at(frames, t_exit)
                if ybx is None or yax is None:
                    diag["exit_book_unreliable"] += 1
                    continue
                if (yax - ybx) < 0:
                    diag["crossed_exit"] += 1
                    continue
                mid_exit = (ybx + yax) / 2.0
                if direction > 0:
                    exit_px = ybx       # sell YES at bid (taker close)
                    gross = exit_px - entry_px
                else:
                    exit_px = yax       # cover short at ask (taker close)
                    gross = entry_px - exit_px
                fee_out = kalshi_taker_fee_cents(exit_px)
                net = gross - fee_in - fee_out
                trades.append({
                    "cluster": imp_idx, "asset": asset, "H": H, "ticker": active,
                    "ret_bps": ret_bps, "beta": beta, "n_beta": n_beta,
                    "dprob": dprob, "direction": direction,
                    "implied_cent_shift": implied_cent_shift,
                    "realized_shift_t0_entry": realized_shift,
                    "entry_px": entry_px, "exit_px": exit_px,
                    "mid_entry": mid_entry, "mid_exit": mid_exit,
                    "fee_in": fee_in, "fee_out": fee_out,
                    "gross": gross, "net": net,
                })

    print(f"       diag: {dict(diag)}", flush=True)
    if diag_dprobs:
        diag_dprobs.sort()
        n = len(diag_dprobs)
        print(f"       |dprob| dist (n={n}): "
              f"min={diag_dprobs[0]:.4f} "
              f"med={diag_dprobs[n//2]:.4f} "
              f"p90={diag_dprobs[int(0.9*n)]:.4f} "
              f"max={diag_dprobs[-1]:.4f}", flush=True)
    if diag_z:
        diag_z.sort()
        n = len(diag_z)
        print(f"       |z-to-strike| dist (n={n}): "
              f"min={diag_z[0]:.2f} med={diag_z[n//2]:.2f} "
              f"p90={diag_z[int(0.9*n)]:.2f} max={diag_z[-1]:.2f}", flush=True)
    return {
        "data_available": True, "trades": trades,
        "spot_cov": (spot_lo, spot_hi), "n_impulses": len(impulses),
        "diag": dict(diag),
    }


# ----------------------------------------------------------------------------
# Cluster bootstrap by impulse event
# ----------------------------------------------------------------------------
def cluster_bootstrap_mean(values_by_cluster, n_boot=N_BOOT, seed=12345):
    import random
    rng = random.Random(seed)
    clusters = list(values_by_cluster.keys())
    if not clusters:
        return None, None, None
    all_vals = [v for vs in values_by_cluster.values() for v in vs]
    point = sum(all_vals) / len(all_vals)
    boots = []
    K = len(clusters)
    for _ in range(n_boot):
        samp = []
        for _ in range(K):
            c = clusters[rng.randrange(K)]
            samp.extend(values_by_cluster[c])
        if samp:
            boots.append(sum(samp) / len(samp))
    boots.sort()
    lo = boots[int(0.025 * len(boots))]
    hi = boots[int(0.975 * len(boots))]
    return point, lo, hi


def main():
    res = run()
    if not res["data_available"]:
        print("DATA_GAP:", res.get("reason"))
        res["verdict"] = "DATA_GAP"
        return res
    trades = res["trades"]
    print(f"\n=== RESULTS === total trades: {len(trades)}")
    if not trades:
        print("No qualifying trades.")
        res["verdict"] = "DATA_GAP" if res.get("n_impulses", 0) == 0 else "INCONCLUSIVE"
        return res

    summary = {}
    for H in H_LIST:
        sub = [t for t in trades if t["H"] == H]
        if not sub:
            continue
        by_cluster = defaultdict(list)
        for t in sub:
            by_cluster[t["cluster"]].append(t["net"])
        point, lo, hi = cluster_bootstrap_mean(by_cluster)
        mean_gross = sum(t["gross"] for t in sub) / len(sub)
        mean_fee = sum(t["fee_in"] + t["fee_out"] for t in sub) / len(sub)
        wins = sum(1 for t in sub if t["net"] > 0)
        summary[H] = {
            "n": len(sub), "n_clusters": len(by_cluster),
            "mean_net": point, "ci_lo": lo, "ci_hi": hi,
            "mean_gross": mean_gross, "mean_fee": mean_fee,
            "winrate": wins / len(sub),
        }
        print(f"\nH={H}s  n_trades={len(sub)}  n_clusters={len(by_cluster)}")
        print(f"  mean GROSS cents/trade : {mean_gross:+.3f}")
        print(f"  mean FEE   cents/trade : {mean_fee:.3f}")
        print(f"  mean NET   cents/trade : {point:+.3f}  CI95=[{lo:+.3f}, {hi:+.3f}]")
        print(f"  win rate (net>0)       : {wins/len(sub):.1%}")

    res["summary"] = summary
    if summary:
        hH = max(summary, key=lambda h: summary[h]["n"])
        s = summary[hH]
        if s["ci_lo"] > 0 and s["mean_net"] > 0:
            verdict = "EDGE"
        elif s["mean_net"] <= 0:
            verdict = "NO_EDGE"
        else:
            verdict = "INCONCLUSIVE"
        res["headline_H"] = hH
        res["verdict"] = verdict
        print(f"\nHEADLINE H={hH}s -> verdict {verdict}")
    return res


if __name__ == "__main__":
    main()
