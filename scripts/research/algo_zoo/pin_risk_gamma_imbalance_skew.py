"""pin_risk_gamma_imbalance_skew — ATM pin-risk / depth-imbalance skewed quoting.

FAMILY: at-the-money pin-risk / depth-imbalance skewed maker quoting.

MECHANISM
---------
In the final minutes of a 15M above/below window, when spot sits very close to the
strike the contract is at-the-money: tiny spot moves flip the binary outcome (max
gamma / pin risk). At those moments the Kalshi book depth imbalance (size resting on
the YES bid vs the NO bid, from the RELIABLE reconstructed book) is most plausibly
informative — real informed flow concentrates where the outcome is genuinely in doubt.

Rather than TAKE on the imbalance (generic OFI failed unconditionally, -1.5c net), we
use imbalance as a QUOTE-SKEW gate: only inside a small ATM band, post a resting maker
bid on the side the depth imbalance FAVORS (tighten the favored side, skip/widen the
toxic side). We then ask, per (asset, ATM-band, imbalance-sign) cell: does the
favored-side maker fill earn positive SETTLEMENT markout net of Kalshi fees, where
flat (unconditional both-sides) quoting does not?

HONEST MODELING (this hunt has killed ~15 candidates; be the assassin)
---------------------------------------------------------------------
- Signal book: RELIABLE reconstructed Kalshi book at decision_ts (snapshot-anchored;
  refuses drifted books). Depth imbalance read from the reliable book ONLY.
- Spot: coinbase mid at-or-before decision_ts (no look-ahead).
- Fill model: a resting maker bid fills ONLY when a real trade print crosses it
  (mm_markout_evaluator primitives). A fill is usually a fill you regret.
- Outcome / settlement: DB evaluated_opportunities.market_result (terminal truth).
  Strike = DB threshold. Settlement markout net of price-dependent Kalshi fee.
- Fee: ceil(0.07 * P * (1-P) * 100) cents/contract, maker rebate = 0 (default).
- CI: block bootstrap clustered by WINDOW (ticker) — the true independent unit.
- No look-ahead: decision_ts strictly precedes fill scan; label is terminal.

VERDICT LOGIC
-------------
EDGE only if a SKEWED-ATM cell (favored side, inside ATM band) clears the pre-registered
gate (bootstrap-CI lower bound of settlement-markout-net-of-fee > 0 AND mean markout > 0
AND n_fills >= floor) where the corresponding FLAT (unconditional) quoting does NOT.
Anything weaker -> NO_EDGE / INCONCLUSIVE. Missing inputs -> DATA_GAP.

Run:
  cd /Users/gabrielkagan/Documents/kalshi-bot
  python3 scripts/research/algo_zoo/pin_risk_gamma_imbalance_skew.py
"""
from __future__ import annotations

import bisect
import json
import math
import random
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime

sys.path.insert(0, "/Users/gabrielkagan/Documents/kalshi-bot")

from scripts.research import kalshi_book_reconstruct as kbr
from scripts.research.phase1b_real_price_economics import (
    _is_crypto_15m, close_epoch_from_ticker, load_frames_jsonl,
)
from scripts.research.phase1b_retail_flow import parse_trade

FRAMES = "/tmp/edge_daily/frames_crypto.jsonl"
TRADES = "/tmp/edge_daily/trades_crypto.jsonl"
SPOT = "/tmp/edge_daily/coinbase_spot.jsonl"
DB = "/tmp/edge_daily/state.db"

# Subsample: BTC + ETH (deepest, most-traded crypto-15M books) x final-2min.
ASSETS = ("BTC", "ETH")
PROD = {"BTC": "BTC-USD", "ETH": "ETH-USD"}

# Decision anchors in the final 2 min (seconds-before-close).
DECISION_SECS_BEFORE_CLOSE = (120, 90, 60, 30)
# ATM band: relative distance |spot-strike|/strike. Final-minute crypto moves are
# small, so a few-bp band captures the genuine pin zone.
ATM_BANDS = (("atm_2bp", 0.0002), ("atm_5bp", 0.0005), ("atm_10bp", 0.0010))
IMB_MIN_RATIO = 1.5  # require a meaningful depth lean before calling a side "favored"

MARKOUT_HORIZON_S = 30.0
MIN_FILLS_FLOOR = 20
MAKER_REBATE_CENTS = 0.0
N_BOOT = 2000


def fee_cents(price_cents: float) -> float:
    """ceil(0.07 * P * (1-P) * 100) per contract, maker rebate applied (default 0)."""
    p = price_cents / 100.0
    raw = math.ceil(7.0 * p * (1.0 - p))
    return max(0.0, raw - MAKER_REBATE_CENTS)


def settlement_markout_net(fill_price: float, side: str, result: str) -> float:
    gross = (100.0 - fill_price) if side == result else -float(fill_price)
    return gross - fee_cents(fill_price)


# ---------------------------------------------------------------------------
def load_spot(path):
    """{product_id: sorted [(epoch, mid)]} from the preprocessed coinbase file
    ({ts, product_id, mid, bid, ask, last})."""
    out = defaultdict(list)
    with open(path) as fh:
        for line in fh:
            if not line.strip():
                continue
            try:
                e = json.loads(line)
                ts = e["ts"]
                if ts.endswith("Z"):
                    ts = ts[:-1] + "+00:00"
                ep = datetime.fromisoformat(ts).timestamp()
                out[e["product_id"]].append((ep, float(e["mid"])))
            except (ValueError, KeyError):
                continue
    for p in out:
        out[p].sort(key=lambda x: x[0])
    return out


def spot_at(series, eps_sorted, at_ep):
    """Last mid at-or-before at_ep (no look-ahead)."""
    i = bisect.bisect_right(eps_sorted, at_ep) - 1
    return series[i][1] if i >= 0 else None


def load_trades_by_ticker(path):
    """{ticker: sorted [(ts, yes_c, taker_side)]} for crypto-15M trades."""
    out = defaultdict(list)
    with open(path) as fh:
        for line in fh:
            if not line.strip():
                continue
            t = parse_trade(line)
            if not t:
                continue
            out[t["ticker"]].append((t["ts"], t["yes_c"], t["taker_side"]))
    for tk in out:
        out[tk].sort(key=lambda x: x[0])
    return out


def load_outcomes(db_path, tickers):
    """{ticker: {result, strike}} from evaluated_opportunities (terminal truth)."""
    conn = sqlite3.connect(db_path)
    out = {}
    q = ("SELECT DISTINCT ticker, market_result, threshold FROM "
         "evaluated_opportunities WHERE ticker = ?")
    for tk in tickers:
        row = conn.execute(q, (tk,)).fetchone()
        if row and row[1] in ("yes", "no") and row[2] is not None:
            out[tk] = {"result": row[1], "strike": float(row[2])}
    conn.close()
    return out


# ---------------------------------------------------------------------------
def book_depth_imbalance(frames, at_ep):
    """Reliable book at at_ep -> (yes_bid_c, yes_ask_c, yes_bid_depth, no_bid_depth,
    imb_logratio) or None if the book is unreliable (refusal honored)."""
    yb, ya = kbr.reliable_nbbo_at(frames, at_ep)  # snapshot-anchored, refuses drift
    if yb is None or ya is None:
        return None
    b, _ = kbr.book_at(frames, at_ep)
    if not b.is_reliable():
        return None
    yd = b.best_yes_bid_depth()
    nd = b.best_no_bid_depth()
    if not yd or not nd:
        return None
    imb = math.log(yd / nd)
    return (yb, ya, yd, nd, imb)


def bootstrap_ci_clustered(values_by_window, *, n_boot=N_BOOT, alpha=0.05, seed=7):
    """Block bootstrap clustered by window: resample WINDOWS (tickers) with
    replacement, pool their fills, take the pooled per-fill mean."""
    keys = list(values_by_window.keys())
    if not keys:
        return (float("nan"), float("nan"), float("nan"))
    flat = [v for k in keys for v in values_by_window[k]]
    if not flat:
        return (float("nan"), float("nan"), float("nan"))
    point = sum(flat) / len(flat)
    rng = random.Random(seed)
    n = len(keys)
    means = []
    for _ in range(n_boot):
        pool = []
        for _ in range(n):
            pool.extend(values_by_window[keys[rng.randrange(n)]])
        if pool:
            means.append(sum(pool) / len(pool))
    if not means:
        return (point, float("nan"), float("nan"))
    means.sort()
    lo = means[int((alpha / 2) * len(means))]
    hi = means[min(len(means) - 1, int((1 - alpha / 2) * len(means)))]
    return (point, lo, hi)


# ---------------------------------------------------------------------------
def first_yes_bid_fill_ts(trades, post_ts, bid_cents):
    """First real YES-sell (taker_side='no') at yes_price<=our bid after post."""
    for ts, yp, side in trades:  # trades sorted ascending; first crossing is earliest
        if ts > post_ts and side == "no" and yp <= bid_cents:
            return ts
    return None


def first_no_bid_fill_ts(trades, post_ts, no_bid_cents):
    """First real YES-buy (taker_side='yes') at yes_price>=100-no_bid after post."""
    thresh = 100.0 - no_bid_cents
    for ts, yp, side in trades:
        if ts > post_ts and side == "yes" and yp >= thresh:
            return ts
    return None


def _markout_30s(frames, fill_ts, side, fill_price, close_ep):
    at = min(fill_ts + MARKOUT_HORIZON_S, close_ep)
    yb, ya = kbr.reliable_nbbo_at(frames, at)
    if yb is None or ya is None:
        return None
    ymid = (yb + ya) / 2.0
    smid = ymid if side == "yes" else 100.0 - ymid
    return smid - fill_price


def _simulate_side(frames, tk_trades, dts, close_ep, side, yb, ya, result):
    """Post a resting maker bid on `side` at the best bid as of dts; fill ONLY when a
    real print crosses (honest adverse selection). Returns (settle_net, mk30) or None."""
    if side == "yes":
        bid_c = yb
        if bid_c is None:
            return None
        ft = first_yes_bid_fill_ts(tk_trades, dts, bid_c)
        fill_price = bid_c  # buy YES at our resting YES bid
    else:
        no_bid_c = 100.0 - ya  # best NO bid = complement of best YES ask
        ft = first_no_bid_fill_ts(tk_trades, dts, no_bid_c)
        fill_price = no_bid_c  # buy NO at our resting NO bid
    if ft is None or ft > close_ep:
        return None
    mk_settle = settlement_markout_net(fill_price, side, result)
    mk30 = _markout_30s(frames, ft, side, fill_price, close_ep)
    return (mk_settle, mk30)


# ---------------------------------------------------------------------------
def main():
    print("[load] frames ...", flush=True)
    frames = load_frames_jsonl(FRAMES)
    frames = {tk: fr for tk, fr in frames.items() if _is_crypto_15m(tk) in ASSETS}
    print(f"[load] {len(frames)} BTC/ETH tickers with frames", flush=True)

    print("[load] trades ...", flush=True)
    trades = load_trades_by_ticker(TRADES)

    print("[load] spot ...", flush=True)
    spot = load_spot(SPOT)
    spot_eps = {p: [x[0] for x in s] for p, s in spot.items()}

    print("[load] outcomes (DB) ...", flush=True)
    outcomes = load_outcomes(DB, set(frames.keys()))
    print(f"[load] {len(outcomes)} tickers with terminal outcome+strike", flush=True)

    # cell key = (asset, band, regime, label) -> {window: [settle_net]}
    cells = defaultdict(lambda: defaultdict(list))
    cells30 = defaultdict(lambda: defaultdict(list))

    n_atm_events = 0
    n_windows_atm = set()
    diag = defaultdict(int)

    for tk, fr in frames.items():
        asset = _is_crypto_15m(tk)
        oc = outcomes.get(tk)
        if not oc:
            diag["no_outcome"] += 1
            continue
        strike, result = oc["strike"], oc["result"]
        close_ep = close_epoch_from_ticker(tk)
        sser = spot.get(PROD[asset])
        if not sser:
            continue
        seps = spot_eps[PROD[asset]]
        tk_trades = trades.get(tk, [])

        for sec in DECISION_SECS_BEFORE_CLOSE:
            dts = close_ep - sec
            sp = spot_at(sser, seps, dts)
            if sp is None:
                diag["no_spot"] += 1
                continue
            rel = abs(sp - strike) / strike
            in_bands = [name for name, w in ATM_BANDS if rel <= w]
            if not in_bands:
                continue
            bk = book_depth_imbalance(fr, dts)
            if bk is None:
                diag["unreliable_book"] += 1
                continue
            yb, ya, yd, nd, imb = bk
            n_atm_events += 1
            n_windows_atm.add(tk)

            ratio = max(yd, nd) / min(yd, nd)
            favored = ("yes" if imb > 0 else "no") if ratio >= IMB_MIN_RATIO else None

            for band in in_bands:
                # FLAT: quote BOTH sides unconditionally in the ATM zone.
                for side in ("yes", "no"):
                    rec = _simulate_side(fr, tk_trades, dts, close_ep, side, yb, ya, result)
                    if rec is not None:
                        cells[(asset, band, "flat", side)][tk].append(rec[0])
                        if rec[1] is not None:
                            cells30[(asset, band, "flat", side)][tk].append(rec[1])
                # SKEW: quote ONLY the depth-favored side.
                if favored is not None:
                    rec = _simulate_side(fr, tk_trades, dts, close_ep, favored, yb, ya, result)
                    if rec is not None:
                        sign = "yesfav" if favored == "yes" else "nofav"
                        cells[(asset, band, "skew", sign)][tk].append(rec[0])
                        if rec[1] is not None:
                            cells30[(asset, band, "skew", sign)][tk].append(rec[1])

    print(f"\n[scan] n_ATM_events={n_atm_events}  n_windows_ATM={len(n_windows_atm)}")
    print(f"[scan] diag={dict(diag)}\n")

    results = []
    for cell, by_win in cells.items():
        n_fills = sum(len(v) for v in by_win.values())
        if n_fills == 0:
            continue
        point, lo, hi = bootstrap_ci_clustered(by_win)
        flat30 = [v for vs in cells30.get(cell, {}).values() for v in vs]
        mean30 = (sum(flat30) / len(flat30)) if flat30 else float("nan")
        survives = (n_fills >= MIN_FILLS_FLOOR and lo > 0
                    and not math.isnan(mean30) and mean30 > 0)
        results.append({
            "cell": cell, "n_fills": n_fills, "n_windows": len(by_win),
            "settle_mean_net": point, "ci_lo": lo, "ci_hi": hi,
            "markout_30s_net": mean30, "survives": survives,
        })

    results.sort(key=lambda r: -r["n_fills"])
    print(f"{'cell':<42}{'n':>6}{'win':>5}{'mean¢':>9}{'ci_lo':>9}{'ci_hi':>9}{'mk30':>9}  surv")
    for r in results:
        c = "/".join(str(x) for x in r["cell"])
        m30 = r["markout_30s_net"]
        print(f"{c:<42}{r['n_fills']:>6}{r['n_windows']:>5}{r['settle_mean_net']:>9.3f}"
              f"{r['ci_lo']:>9.3f}{r['ci_hi']:>9.3f}{m30:>9.3f}"
              f"  {'YES' if r['survives'] else ''}")

    skew_survivors = [r for r in results if r["cell"][2] == "skew" and r["survives"]]
    flat_survivors = [r for r in results if r["cell"][2] == "flat" and r["survives"]]
    eligible = [r for r in results if r["cell"][2] == "skew" and r["n_fills"] >= MIN_FILLS_FLOOR]
    headline = max(eligible, key=lambda r: r["ci_lo"]) if eligible else None

    return {
        "n_atm_events": n_atm_events, "n_windows_atm": len(n_windows_atm),
        "results": results, "skew_survivors": skew_survivors,
        "flat_survivors": flat_survivors, "headline": headline,
    }


if __name__ == "__main__":
    out = main()
    print("\n=== SUMMARY ===")
    print(f"skew survivors: {len(out['skew_survivors'])}  flat survivors: {len(out['flat_survivors'])}")
    if out["headline"]:
        h = out["headline"]
        print(f"headline skew cell: {h['cell']} n={h['n_fills']} "
              f"mean={h['settle_mean_net']:.3f}¢ CI=[{h['ci_lo']:.3f},{h['ci_hi']:.3f}] "
              f"mk30={h['markout_30s_net']:.3f}")
    else:
        print("no eligible skew cell (n>=floor) — DATA_GAP / INCONCLUSIVE on n")
