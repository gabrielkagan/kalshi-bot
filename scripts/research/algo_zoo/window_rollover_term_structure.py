"""window_rollover_term_structure — intra-asset relative-value at the 15M rollover.

Family: term-structure / intra-asset relative value.

MECHANISM (genuinely new; NOT in any tried/dead list)
-----------------------------------------------------
Consecutive 15M above/below windows on ONE underlying form a tiny term
structure. At each rollover the about-to-close window W0 (strike S0, small
secs_left) and the just-opened window W1 (strike S1, ~900s left) coexist for a
brief overlap. When W0 is NEAR-RESOLVED (reliable mid <= 3c or >= 97c) it pins
the current spot LOCATION relative to S0 with very high confidence. We combine
that pin with the consensus spot at the decision time T to compute a model fair
value for W1:

    fair_w1 = Phi( (spot - S1) / (sigma * sqrt(secs_left)) )

(Bachelier/Brownian-style; sigma = realized per-sqrt-second stdev of consensus
spot ABS returns over a trailing window). We then compare fair_w1 to the W1
reliable mid. If |fair_w1 - w1_mid| >= edge_min AND the W0 near-resolution gate
passes, we enter TAKER on W1 toward fair_w1 (buy YES at ask if fair>mid; buy NO
at no-ask if fair<mid).

WHY IT MIGHT HAVE EDGE: at rollover the freshly-opened W1 may lag in pricing the
high-confidence spot location that the near-resolved W0 already reveals
(operator / quote latency on the new window). Intra-asset relative value — one
ticker informs the other — avoids the absolute-fair-value calibration trap.

LABEL: W1 TERMINAL BOOK reliable mid at W1.close_epoch (yes settles if > 50).
Built independently from the signal book — no look-ahead.

FILLS: TAKER only (the spec). Cross the W1 reliable book at ask (YES) /
no-ask (NO). Full ceil-fee on entry. Maker rebate 0. Must survive the W1
half-spread + fee or NO_EDGE.

NO LOOK-AHEAD: every input uses ts <= T (decision = W0.close - DECISION_LEAD_S,
which is < W0.close <<< W1.close). The W0 pin, consensus spot, sigma, and W1
signal mid all use cutoff = T. The label book is the W1 terminal book at its own
close, strictly later and separate.

CI: cluster bootstrap by ROLLOVER PAIR (the independent unit). EDGE only if the
TAKER net-cents CI lower bound > 0.

DATA: frames_crypto.jsonl (both windows' books + terminal label) +
coinbase_spot.jsonl (consensus spot anchor). HYPE/DOGE/BNB have NO local
coinbase mid -> DATA_GAP per asset (skipped). Strikes from state.db threshold.

Local corpus only (~31h, 2026-05-30T10Z -> 05-31T17Z; spot from 05-30T21Z).
Wide CIs, humility.
"""

from __future__ import annotations

import sys
sys.path.insert(0, "/Users/gabrielkagan/Documents/kalshi-bot")

import json
import math
import random
import re
from collections import defaultdict
from datetime import datetime, timezone

import scripts.research.kalshi_book_reconstruct as kbr
from scripts.research.phase1b_real_price_economics import (
    close_epoch_from_ticker,
    load_frames_jsonl,
    load_outcomes_db,
)

FRAMES = "/tmp/edge_daily/frames_crypto.jsonl"
DB = "/tmp/edge_daily/state.db"
SPOT = "/tmp/edge_daily/coinbase_spot.jsonl"

# Assets with local coinbase consensus spot. HYPE/DOGE/BNB -> DATA_GAP (no mid).
ASSETS = ["BTC", "ETH", "SOL", "XRP"]
SPOT_PRODUCT = {"BTC": "BTC-USD", "ETH": "ETH-USD", "SOL": "SOL-USD", "XRP": "XRP-USD"}

WINDOW_S = 900.0           # 15-minute window
ROLLOVER_TOL_S = 5.0       # W1.close - W0.close must equal WINDOW_S +/- this
# Decision time = W0.close + DECISION_LAG. W0 has just settled (terminal pin) and
# W1 has been open DECISION_LAG seconds (book reconstructable; ~900-LAG left). The
# pin still leaks the spot LOCATION that the fresh W1 may not yet have repriced.
# Sweep multiple lags: the just-opened W1 book is often NOT reliably reconstructable
# in the first ~20s (no snapshot baseline yet in the historical bronze), so we
# search for the earliest lag where W1 reconstructs AND the leak survives fees.
DECISION_LAGS = [5.0, 15.0, 30.0, 60.0, 120.0]
PIN_LO = 3.0               # W0 near-resolved iff reliable mid <= PIN_LO or >= 100-PIN_LO
SIGMA_LOOKBACK_S = 1800.0  # trailing window for realized sigma of consensus spot
EDGE_MIN = 5.0             # |fair_w1 - w1_mid| >= this (cents) to fire
N_BOOT = 2000
RNG = random.Random(20260531)


def _epoch(iso: str) -> float:
    s = iso.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def _fee(price_cents: float) -> float:
    """ceil(0.07 * C * P * (1-P)) cents/contract, C=1, P=price/100."""
    p = price_cents / 100.0
    return math.ceil(0.07 * p * (1.0 - p) * 100.0) / 100.0


def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def load_spot() -> dict:
    """{product_id: [(ts_epoch, mid), ...]} sorted by ts."""
    out = defaultdict(list)
    with open(SPOT) as fh:
        for line in fh:
            if not line.strip():
                continue
            try:
                d = json.loads(line)
            except ValueError:
                continue
            pid, mid, ts = d.get("product_id"), d.get("mid"), d.get("ts")
            if pid is None or mid is None or ts is None:
                continue
            out[pid].append((_epoch(ts), float(mid)))
    for pid in out:
        out[pid].sort(key=lambda x: x[0])
    return out


def spot_mid_at(series, cutoff_epoch):
    """Last mid with ts <= cutoff (no look-ahead). None if none."""
    val = None
    for ts, mid in series:
        if ts > cutoff_epoch:
            break
        val = mid
    return val


def realized_sigma_per_sec(series, cutoff_epoch, lookback_s):
    """Per-sqrt-second stdev of consensus spot ABS returns (price units) over the
    trailing window ending at cutoff (no look-ahead). Brownian scaling:
    variance contribution = dpx^2 / dt. Returns (sigma_per_sqrt_sec, last_mid) or
    (None, None)."""
    lo = cutoff_epoch - lookback_s
    sub = [(ts, mid) for ts, mid in series if lo <= ts <= cutoff_epoch]
    if len(sub) < 10:
        return None, None
    var_acc = 0.0
    n = 0
    for i in range(1, len(sub)):
        dt = sub[i][0] - sub[i - 1][0]
        if dt <= 0:
            continue
        dpx = sub[i][1] - sub[i - 1][1]
        var_acc += (dpx * dpx) / dt
        n += 1
    if n < 5:
        return None, None
    sigma_per_sec = math.sqrt(var_acc / n)
    return sigma_per_sec, sub[-1][1]


def _is_crypto15m(tk, asset):
    return bool(re.match(rf"KX{asset}15M", tk))


def cluster_bootstrap_mean(per_cluster_vals, n_boot=N_BOOT):
    """Cluster bootstrap by rollover pair: each cluster contributes one trade
    value. Resample clusters with replacement."""
    if not per_cluster_vals:
        return (float("nan"), float("nan"), float("nan"))
    vals = list(per_cluster_vals)
    n = len(vals)
    point = sum(vals) / n
    boots = []
    for _ in range(n_boot):
        s = sum(vals[RNG.randrange(n)] for _ in range(n))
        boots.append(s / n)
    boots.sort()
    lo = boots[int(0.025 * n_boot)]
    hi = boots[int(0.975 * n_boot)]
    return point, lo, hi


def _new_diag():
    return {
        "n_rollover_pairs_examined": 0,
        "skip_no_w0_strike": 0,
        "skip_no_w1_strike": 0,
        "skip_no_w0_pin_book": 0,      # W0 terminal book not reliably reconstructable
        "skip_w0_not_pinned": 0,       # W0 terminal mid not in [<=PIN_LO or >=100-PIN_LO]
        "skip_no_spot": 0,
        "skip_no_sigma": 0,
        "skip_no_w1_reliable_signal": 0,  # W1 book not reliably reconstructable at T
        "skip_no_w1_terminal_label": 0,
        "no_edge_signal": 0,
        "fired": 0,
        "fired_yes": 0,
        "fired_no": 0,
        "won": 0,
    }


def evaluate_lag(pairs, series_by_asset, lag):
    """One full pass over the pre-built rollover `pairs` at decision lag `lag`
    (seconds after W0.close). pairs: list of dicts with precomputed strikes,
    books, and W0-terminal pin. Returns (per_cluster_net, diag, fired_meta)."""
    diag = _new_diag()
    net_vals = []
    fired_meta = []

    for p in pairs:
        diag["n_rollover_pairs_examined"] += 1
        asset = p["asset"]
        s1 = p["s1"]
        w0_fr = p["w0_fr"]
        w1_fr = p["w1_fr"]
        w0_close = p["w0_close"]
        w1_close = p["w1_close"]
        series = series_by_asset[asset]

        # strikes already validated in build step
        # --- W0 TERMINAL near-resolution pin (book at W0.close) ---
        b0, a0 = kbr.reliable_nbbo_at(w0_fr, w0_close)
        if b0 is None or a0 is None or not (0 <= a0 <= 100) or b0 > a0:
            diag["skip_no_w0_pin_book"] += 1
            continue
        w0_term_mid = (b0 + a0) / 2.0
        if not (w0_term_mid <= PIN_LO or w0_term_mid >= 100.0 - PIN_LO):
            diag["skip_w0_not_pinned"] += 1
            continue

        t_dec = w0_close + lag       # W1 has been open `lag` seconds
        secs_left = w1_close - t_dec
        if secs_left <= 0:
            continue

        # --- consensus spot + sigma at decision time (ts <= t_dec) ---
        sigma_ps, spot_now = realized_sigma_per_sec(series, t_dec, SIGMA_LOOKBACK_S)
        if spot_now is None:
            diag["skip_no_spot"] += 1
            continue
        if sigma_ps is None or sigma_ps <= 0:
            diag["skip_no_sigma"] += 1
            continue

        denom = sigma_ps * math.sqrt(secs_left)
        if denom <= 0:
            diag["skip_no_sigma"] += 1
            continue
        z = (spot_now - s1) / denom
        fair_w1 = _norm_cdf(z) * 100.0   # prob(spot_close > S1), cents

        # --- W1 reliable signal mid at decision time ---
        b1, a1 = kbr.reliable_nbbo_at(w1_fr, t_dec)
        if b1 is None or a1 is None or not (0 < a1 <= 100) or b1 > a1:
            diag["skip_no_w1_reliable_signal"] += 1
            continue
        w1_mid = (b1 + a1) / 2.0

        # --- W1 terminal label (independent book at W1.close) ---
        tb, ta = kbr.reliable_nbbo_at(w1_fr, w1_close)
        if tb is None or ta is None:
            diag["skip_no_w1_terminal_label"] += 1
            continue
        won_yes = ((tb + ta) / 2.0) > 50.0

        edge = fair_w1 - w1_mid
        if abs(edge) < EDGE_MIN:
            diag["no_edge_signal"] += 1
            continue

        if edge > 0:
            sig = "yes"    # fair > mid -> W1 underpriced -> buy YES at ask
            entry = a1
            win_side = won_yes
        else:
            sig = "no"
            entry = 100.0 - b1
            win_side = (not won_yes)
        if not (0 < entry < 100):
            continue

        gross = (100.0 - entry) if win_side else (-entry)
        n = gross - _fee(entry)

        diag["fired"] += 1
        diag["fired_yes" if sig == "yes" else "fired_no"] += 1
        if win_side:
            diag["won"] += 1
        net_vals.append(n)
        fired_meta.append((asset, p["w0_tk"], p["w1_tk"], sig,
                           round(fair_w1, 1), round(w1_mid, 1), win_side,
                           round(n, 2)))
    return net_vals, diag, fired_meta


def run():
    print("Loading Kalshi crypto-15M frames (4.4GB)...", flush=True)
    all_frames = load_frames_jsonl(FRAMES)
    print(f"  loaded {len(all_frames)} tickers", flush=True)

    print("Loading coinbase spot anchor...", flush=True)
    spot = load_spot()
    print(f"  spot products: {list(spot)}", flush=True)

    spot_lo = _epoch("2026-05-30T21:00:00Z")
    span_hi = _epoch("2026-05-31T17:59:59Z")

    # --- build rollover pairs once (asset-agnostic of lag) ---
    pairs = []
    series_by_asset = {}
    pair_build = {"examined": 0, "no_w1": 0, "no_w0_strike": 0, "no_w1_strike": 0,
                  "no_frames": 0, "built": 0}
    for asset in ASSETS:
        prod = SPOT_PRODUCT[asset]
        series = spot.get(prod, [])
        if not series:
            print(f"[{asset}] NO coinbase spot -> DATA_GAP skip", flush=True)
            continue
        series_by_asset[asset] = series

        tickers = []
        for tk in all_frames:
            if not _is_crypto15m(tk, asset):
                continue
            try:
                ce = close_epoch_from_ticker(tk)
            except Exception:
                continue
            if spot_lo <= ce <= span_hi:
                tickers.append((tk, ce))
        tickers.sort(key=lambda x: x[1])
        outcomes = load_outcomes_db(DB, set(tk for tk, _ in tickers))

        for w0_tk, w0_close in tickers:
            target = w0_close + WINDOW_S
            w1 = None
            for tk, ce in tickers:
                if abs(ce - target) <= ROLLOVER_TOL_S and tk != w0_tk:
                    w1 = (tk, ce)
                    break
            if w1 is None:
                pair_build["no_w1"] += 1
                continue
            w1_tk, w1_close = w1
            pair_build["examined"] += 1
            s0 = outcomes.get(w0_tk, {}).get("strike")
            s1 = outcomes.get(w1_tk, {}).get("strike")
            if s0 is None:
                pair_build["no_w0_strike"] += 1
                continue
            if s1 is None:
                pair_build["no_w1_strike"] += 1
                continue
            w0_fr = all_frames.get(w0_tk)
            w1_fr = all_frames.get(w1_tk)
            if not w0_fr or not w1_fr:
                pair_build["no_frames"] += 1
                continue
            pairs.append({
                "asset": asset, "w0_tk": w0_tk, "w1_tk": w1_tk,
                "w0_close": w0_close, "w1_close": w1_close,
                "s0": float(s0), "s1": float(s1),
                "w0_fr": w0_fr, "w1_fr": w1_fr,
            })
            pair_build["built"] += 1

    print(f"\nPair build: {json.dumps(pair_build)}", flush=True)
    print(f"Usable rollover pairs (both strikes + frames): {len(pairs)}\n", flush=True)

    # --- sweep decision lags ---
    results = {}
    best = None
    for lag in DECISION_LAGS:
        net_vals, diag, fired_meta = evaluate_lag(pairs, series_by_asset, lag)
        if net_vals:
            point, lo, hi = cluster_bootstrap_mean(net_vals)
        else:
            point, lo, hi = float("nan"), float("nan"), float("nan")
        wr = diag["won"] / diag["fired"] if diag["fired"] else float("nan")
        results[lag] = {
            "n": len(net_vals), "point": point, "lo": lo, "hi": hi,
            "winrate": wr, "diag": diag, "fired_meta": fired_meta[:10],
        }
        print(f"=== LAG {lag:.0f}s ===", flush=True)
        print(f"  fired n={len(net_vals)} mean={point:.4f}c CI[{lo:.4f},{hi:.4f}] "
              f"winrate={wr:.3f}", flush=True)
        print(f"  funnel: w0_pin_book_fail={diag['skip_no_w0_pin_book']} "
              f"not_pinned={diag['skip_w0_not_pinned']} "
              f"w1_signal_fail={diag['skip_no_w1_reliable_signal']} "
              f"no_edge={diag['no_edge_signal']} fired={diag['fired']}", flush=True)
        if net_vals and (best is None or len(net_vals) > results[best]["n"]):
            best = lag

    print("\n=== SWEEP SUMMARY (per decision lag) ===", flush=True)
    for lag in DECISION_LAGS:
        r = results[lag]
        print(f"  lag={lag:>5.0f}s  n={r['n']:>4d}  mean={r['point']:.4f}c  "
              f"CI[{r['lo']:.4f},{r['hi']:.4f}]  wr={r['winrate']:.3f}", flush=True)

    # Headline = the lag with the most fills (most data); report its CI honestly.
    out = {"results": {str(k): {kk: vv for kk, vv in v.items() if kk != "diag"}
                       for k, v in results.items()},
           "best_lag": best, "pair_build": pair_build, "n_pairs": len(pairs)}
    if best is not None:
        r = results[best]
        print(f"\nHEADLINE (lag={best:.0f}s, most fills): n={r['n']} "
              f"mean={r['point']:.4f}c CI[{r['lo']:.4f},{r['hi']:.4f}] "
              f"winrate={r['winrate']:.3f}", flush=True)
        print("Sample fired (asset,w0,w1,sig,fair,mid,won,net):", flush=True)
        for m in r["fired_meta"]:
            print("  ", m, flush=True)
        out["headline"] = {"lag": best, "n": r["n"], "point": r["point"],
                           "lo": r["lo"], "hi": r["hi"], "winrate": r["winrate"]}
    else:
        print("\nNO PAIRS FIRED AT ANY LAG", flush=True)
    return out


if __name__ == "__main__":
    run()
