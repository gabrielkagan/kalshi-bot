#!/usr/bin/env python3
"""btc_beta_alt_fade  (family: cross_asset / lead_lag)

HYPOTHESIS: BTC leads alts. ALT-window market-makers key off the ALT's own spot,
not BTC's, so the ALT Kalshi book may lag BTC information. We test whether a
BTC-implied ALT move (beta * BTC_return) predicts the ALT terminal above/below
outcome, and whether trading the underpriced side as a TAKER clears a full
spread + Kalshi fee.

DATA: coinbase_spot.jsonl (BTC + ETH/SOL/XRP event-time mids, post-21Z),
frames_crypto.jsonl (ALT Kalshi NBBO + terminal book mid label),
state.db evaluated_opportunities.threshold (strike). HYPE/DOGE/BNB excluded.

DECISION at a fixed eval offset before close:
  1. Compute BTC signed return over short lookback (sweep 2/5/10s).
  2. Rolling-5min OLS beta of ALT step-return to BTC step-return.
  3. expected ALT move = beta * BTC_return (fractional).
  4. If |expected move| > threshold -> reconstruct ALT reliable NBBO at decision.
  5. spot->prob map: z = (proj_spot - strike) / (alt_mid * sigma*sqrt(ttc));
     model_prob_yes = Student-t CDF(z). If model says the BTC-implied side is
     cheaper than its ask -> lift that side as TAKER at the ask (depth-checked,
     full fee).
  6. Label by terminal book mid at close. PnL/ct = 100*win - entry_ask - fee.

HEADLINE = mean net PnL per entered contract; ticker-clustered bootstrap CI.
Pre-cost sanity: gross "BTC-implied direction == terminal outcome" hit-rate.
EDGE iff net CI excludes zero on the positive side.

NO look-ahead: signal-book and label-book are built independently; decision uses
only spot/book data with ts <= decision_ts (a scanned offset before close).
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

import numpy as np  # noqa: E402

from scripts.research.kalshi_book_reconstruct import (  # noqa: E402
    reliable_nbbo_at, book_at,
)
from scripts.research.phase1b_real_price_economics import (  # noqa: E402
    close_epoch_from_ticker,
)

SPOT_PATH = "/tmp/edge_daily/coinbase_spot.jsonl"
# FRAMES_PATH: a grep -F -f <target-tickers> pre-filter of the 4.4GB
# frames_crypto.jsonl down to just the ETH/SOL/XRP target tickers (826MB), so the
# Python json parse stays tractable. Regenerate with build_prefilter() below or:
#   grep -F -f /tmp/btc_alt_target_tickers.txt frames_crypto.jsonl > FRAMES_PATH
FRAMES_PATH = "/tmp/btc_alt_frames.jsonl"
FRAMES_FULL_PATH = "/tmp/edge_daily/frames_crypto.jsonl"
DB_PATH = "/tmp/edge_daily/state.db"

ALTS = ("ETH", "SOL", "XRP")
ALT_PRODUCT = {"ETH": "ETH-USD", "SOL": "SOL-USD", "XRP": "XRP-USD"}
BTC_PRODUCT = "BTC-USD"

# We SCAN multiple decision instants per window (not one fixed offset): the
# cross-asset lead can only fire at the moments BTC has just jumped, so we test
# the window's last DECISION_FROM_S..DECISION_TO_S seconds-before-close on a
# DECISION_STEP_S grid, and act only when |exp_move| clears a threshold. At-most
# one entry per (ticker, side) is taken (first qualifying instant) to avoid
# multiple bites of the same eventual settlement.
DECISION_FROM_S = 600.0        # earliest decision: 10 min before close
DECISION_TO_S = 30.0           # latest decision: 30 s before close
DECISION_STEP_S = 15.0         # scan grid
BETA_WINDOW_S = 300.0          # rolling 5-min OLS beta
BETA_STEP_S = 5.0
# Diagnostic showed BTC 2-10s |returns| p99 ~ 2-3 bps; exp_move clears 5bps ~0%.
# Calibrate the action gate to where signal actually exists.
BTC_LOOKBACKS = (5.0, 10.0)
EXP_MOVE_THRESHOLDS = (0.0001, 0.0002, 0.0005)  # 1bp / 2bp / 5bp
SIGMA_SAMPLE_S = 900.0         # vol estimate window for the prob map
MIN_DEPTH = 1.0                # contracts required at the ask we lift
T_DOF = 4                      # Student-t dof for fat-tailed prob map
FEE_RATE = 0.07
N_BOOT = 2000


# ---------------------------------------------------------------- spot loading
def _epoch_iso(s: str) -> float:
    s = s.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def load_spot() -> dict[str, tuple[list[float], list[float]]]:
    """{product_id: (sorted_ts[], mid[])} from coinbase_spot.jsonl."""
    raw: dict[str, list[tuple[float, float]]] = defaultdict(list)
    want = {BTC_PRODUCT} | set(ALT_PRODUCT.values())
    with open(SPOT_PATH) as fh:
        for line in fh:
            if not line.strip():
                continue
            try:
                d = json.loads(line)
                pid = d["product_id"]
                if pid not in want:
                    continue
                raw[pid].append((_epoch_iso(d["ts"]), float(d["mid"])))
            except (ValueError, KeyError):
                continue
    out: dict[str, tuple[list[float], list[float]]] = {}
    for pid, rows in raw.items():
        rows.sort(key=lambda x: x[0])
        out[pid] = ([r[0] for r in rows], [r[1] for r in rows])
    return out


def mid_le(series, t: float):
    """Latest mid at or before t (no look-ahead). None if t precedes data."""
    ts, mid = series
    i = bisect_right(ts, t) - 1
    if i < 0:
        return None
    return mid[i]


def ret_over(series, t_end: float, lookback: float):
    """Signed fractional return of the mid over [t_end-lookback, t_end]."""
    p1 = mid_le(series, t_end)
    p0 = mid_le(series, t_end - lookback)
    if p1 is None or p0 is None or p0 <= 0:
        return None
    return (p1 - p0) / p0


def rolling_beta(alt_series, btc_series, t_end: float):
    """OLS beta of ALT step-return on BTC step-return over the trailing window."""
    n_steps = int(BETA_WINDOW_S // BETA_STEP_S)
    if n_steps < 5:
        return None
    bx, by = [], []
    for k in range(n_steps):
        g = t_end - BETA_WINDOW_S + BETA_STEP_S * (k + 1)
        rb = ret_over(btc_series, g, BETA_STEP_S)
        ra = ret_over(alt_series, g, BETA_STEP_S)
        if rb is None or ra is None:
            continue
        bx.append(rb)
        by.append(ra)
    if len(bx) < 5:
        return None
    x = np.asarray(bx)
    y = np.asarray(by)
    vx = float(np.var(x))
    if vx <= 1e-18:
        return None
    return float(np.cov(x, y, bias=True)[0, 1] / vx)


def realized_sigma(series, t_end: float):
    """Per-second return stdev of the ALT mid over the trailing window."""
    n_steps = int(SIGMA_SAMPLE_S // BETA_STEP_S)
    rets = []
    for k in range(n_steps):
        g = t_end - SIGMA_SAMPLE_S + BETA_STEP_S * (k + 1)
        r = ret_over(series, g, BETA_STEP_S)
        if r is not None:
            rets.append(r / BETA_STEP_S)
    if len(rets) < 5:
        return None
    return float(np.std(rets, ddof=1))


# ----------------------------------------------------------- prob map (CDFs)
def t_cdf(z: float, dof: int) -> float:
    x = dof / (dof + z * z)
    ib = 0.5 * _betainc(dof / 2.0, 0.5, x)
    return 1.0 - ib if z > 0 else ib


def _betainc(a: float, b: float, x: float) -> float:
    if x <= 0:
        return 0.0
    if x >= 1:
        return 1.0
    lbeta = math.lgamma(a) + math.lgamma(b) - math.lgamma(a + b)
    front = math.exp(math.log(x) * a + math.log(1 - x) * b - lbeta) / a
    f, c, d = 1.0, 1.0, 0.0
    for i in range(0, 300):
        m = i // 2
        if i == 0:
            num = 1.0
        elif i % 2 == 0:
            num = (m * (b - m) * x) / ((a + 2 * m - 1) * (a + 2 * m))
        else:
            num = -((a + m) * (a + b + m) * x) / ((a + 2 * m) * (a + 2 * m + 1))
        d = 1.0 + num * d
        if abs(d) < 1e-30:
            d = 1e-30
        d = 1.0 / d
        c = 1.0 + num / c
        if abs(c) < 1e-30:
            c = 1e-30
        cd = c * d
        f *= cd
        if abs(1.0 - cd) < 1e-10:
            break
    return front * (f - 1.0)


# ----------------------------------------------------------- strikes from DB
def load_alt_strikes() -> dict[str, float]:
    """{ticker: strike} for ALL ETH/SOL/XRP 15M tickers with a threshold,
    closing on the corpus days (26MAY30/31). The frame-loader filters to the
    intersection of these and the spot-covered window."""
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA busy_timeout=10000")
    conn.row_factory = sqlite3.Row
    out: dict[str, float] = {}
    for a in ALTS:
        for day in ("26MAY30", "26MAY31"):
            sql = ("SELECT ticker, threshold FROM evaluated_opportunities "
                   "WHERE ticker LIKE ? AND threshold IS NOT NULL")
            for r in conn.execute(sql, (f"KX{a}15M-{day}%",)):
                out[r["ticker"]] = float(r["threshold"])
    conn.close()
    return out


def load_db_outcomes() -> dict[str, int]:
    """{ticker: yes_win 1/0} from the settled DB market_result. These windows are
    settled in the fresh backup (157/161 targets covered). This is the AUTHORITY
    label — the terminal Kalshi book at close is one-sided (settling YES->0 or
    NO->0) and reliable_nbbo_at correctly REFUSES it (145/161), so we label by the
    realized settlement, not a degenerate close book."""
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA busy_timeout=10000")
    conn.row_factory = sqlite3.Row
    out: dict[str, int] = {}
    for r in conn.execute("SELECT DISTINCT ticker, market_result FROM "
                          "evaluated_opportunities WHERE market_result "
                          "IN ('yes','no')"):
        out[r["ticker"]] = 1 if r["market_result"] == "yes" else 0
    conn.close()
    return out


def build_prefilter(want: set[str]) -> None:
    """grep -F -f <tickers> the 4.4GB full frames file down to just `want` into
    FRAMES_PATH (C-speed; ~826MB vs 4.4GB so the Python parse is tractable).
    Idempotent: skips if FRAMES_PATH already exists and is non-empty."""
    import os
    import subprocess
    import tempfile
    if os.path.exists(FRAMES_PATH) and os.path.getsize(FRAMES_PATH) > 0:
        return
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as tf:
        for tk in sorted(want):
            tf.write(tk + "\n")
        patt = tf.name
    with open(FRAMES_PATH, "w") as out:
        subprocess.run(["grep", "-F", "-f", patt, FRAMES_FULL_PATH],
                       stdout=out, check=False)
    os.unlink(patt)


def load_frames_for_tickers(want: set[str]) -> dict[str, list]:
    """Single streaming pass over the (pre-filtered) frames file, keeping only
    frames whose market_ticker is in `want`. Returns {ticker: [(recv_epoch,
    inner), ...] sorted asc}."""
    build_prefilter(want)
    frames: dict[str, list] = defaultdict(list)
    with open(FRAMES_PATH) as fh:
        for line in fh:
            if not line.strip():
                continue
            # cheap prefilter: ticker substring must be present before full parse
            try:
                env = json.loads(line)
                inner = json.loads(env["_raw"])
            except (ValueError, KeyError):
                continue
            tk = inner.get("msg", {}).get("market_ticker", "")
            if tk not in want:
                continue
            ts = env.get("_wire_recv_ts")
            if ts is None:
                continue
            frames[tk].append((_epoch_iso(ts), inner))
    for tk in frames:
        frames[tk].sort(key=lambda x: x[0])
    return frames


# ----------------------------------------------------------- terminal label
def terminal_yes_win(frames: list, close_epoch: float):
    """Label = above/below per the TERMINAL reliable book mid at close.
    YES wins (1) if terminal yes-mid > 50c, else 0. Honor refusal -> None."""
    yb, ya = reliable_nbbo_at(frames, close_epoch)
    if yb is None or ya is None:
        return None
    mid = 0.5 * (yb + ya)
    if abs(mid - 50.0) < 1e-6:
        return None
    return 1 if mid > 50.0 else 0


# ----------------------------------------------------------- kalshi fee
def fee_cents(entry_cents: float) -> float:
    """ceil(0.07 * C * P * (1-P)) cents/contract, C=1."""
    p = entry_cents / 100.0
    return math.ceil(FEE_RATE * p * (1.0 - p) * 100.0)


# ----------------------------------------------------------- bootstrap
def cluster_bootstrap(values_by_ticker: dict[str, list[float]], n_boot: int):
    keys = list(values_by_ticker.keys())
    if not keys:
        return (float("nan"), float("nan"), float("nan"))
    flat = [v for k in keys for v in values_by_ticker[k]]
    point = float(np.mean(flat))
    rng = np.random.default_rng(12345)
    karr = list(keys)
    boots = np.empty(n_boot)
    for b in range(n_boot):
        idx = rng.integers(0, len(karr), len(karr))
        vals = []
        for j in idx:
            vals.extend(values_by_ticker[karr[j]])
        boots[b] = np.mean(vals) if vals else 0.0
    lo, hi = np.percentile(boots, [2.5, 97.5])
    return point, float(lo), float(hi)


# ----------------------------------------------------------- main
def run():
    print("loading spot...", flush=True)
    spot = load_spot()
    for p in (BTC_PRODUCT, *ALT_PRODUCT.values()):
        if p not in spot:
            print(f"DATA_GAP: spot product {p} missing")
            return None
    btc_series = spot[BTC_PRODUCT]
    spot_start = btc_series[0][0]
    print(f"  spot products: {list(spot.keys())}, BTC start "
          f"{datetime.utcfromtimestamp(spot_start)}", flush=True)

    print("loading ALT strikes + settled outcomes from DB...", flush=True)
    strikes = load_alt_strikes()
    db_outcomes = load_db_outcomes()
    print(f"  {len(strikes)} ALT tickers with strike (26MAY30/31)", flush=True)

    # Candidate ALT tickers: have a strike AND beta+sigma windows fully covered
    # by spot. Build the target set BEFORE touching the 4.4GB frames file.
    alt_tickers = {}
    need_cover = max(BETA_WINDOW_S, SIGMA_SAMPLE_S)
    for tk, _strike in strikes.items():
        a = next((x for x in ALTS if tk.startswith(f"KX{x}15M-")), None)
        if a is None:
            continue
        try:
            ce = close_epoch_from_ticker(tk)
        except Exception:
            continue
        # earliest decision instant is DECISION_FROM_S before close; its beta +
        # sigma windows must be fully spot-covered.
        earliest_dec = ce - DECISION_FROM_S
        if earliest_dec - need_cover >= spot_start:
            alt_tickers[tk] = (a, ce)
    print(f"  {len(alt_tickers)} ALT tickers in spot-covered window", flush=True)

    print("streaming frames for target tickers only...", flush=True)
    frames_by_ticker = load_frames_for_tickers(set(alt_tickers.keys()))
    print(f"  loaded frames for {len(frames_by_ticker)} tickers", flush=True)

    grids: dict[tuple, dict[str, list[float]]] = {}
    sanity: dict[tuple, list[int]] = {}
    refusal = defaultdict(int)
    n_eval_total = 0

    # first-entry guard: at most one entry per (ticker, lb, thr) cell, taken at
    # the first decision instant that qualifies (avoids re-betting the same
    # eventual settlement across the scan grid).
    taken: set[tuple] = set()
    dec_grid = [DECISION_FROM_S - DECISION_STEP_S * k
                for k in range(int((DECISION_FROM_S - DECISION_TO_S)
                                   // DECISION_STEP_S) + 1)]

    for tk, (asset, close_ep) in alt_tickers.items():
        strike = strikes[tk]
        frames = frames_by_ticker[tk]
        alt_series = spot[ALT_PRODUCT[asset]]

        # LABEL = realized settlement from the DB (authority). The terminal Kalshi
        # book is one-sided at close (settling) and reliable_nbbo_at refuses it.
        win = db_outcomes.get(tk)
        if win is None:
            refusal["no_db_outcome"] += 1
            continue

        counted_reliable = False
        for offset in dec_grid:                  # offset seconds before close
            dec_ts = close_ep - offset
            ttc = offset
            if ttc <= 0:
                continue

            alt_mid = mid_le(alt_series, dec_ts)
            if alt_mid is None or alt_mid <= 0:
                continue
            beta = rolling_beta(alt_series, btc_series, dec_ts)
            if beta is None:
                continue
            sig = realized_sigma(alt_series, dec_ts)
            if sig is None or sig <= 0:
                continue
            sigma_ttc = sig * math.sqrt(ttc)
            if sigma_ttc <= 0:
                continue

            # reliable book at THIS decision instant (no look-ahead; honor refusal)
            bk, _ = book_at(frames, dec_ts)
            yb, ya = reliable_nbbo_at(frames, dec_ts)
            if yb is None or ya is None:
                continue
            if not counted_reliable:
                n_eval_total += 1
                counted_reliable = True
            yes_ask_depth = bk.best_yes_ask_depth()
            no_ask_depth = bk.best_yes_bid_depth()

            for lb in BTC_LOOKBACKS:
                btc_ret = ret_over(btc_series, dec_ts, lb)
                if btc_ret is None:
                    continue
                exp_move = beta * btc_ret
                proj_spot = alt_mid * (1.0 + exp_move)
                z = (proj_spot - strike) / (alt_mid * sigma_ttc)
                model_prob_yes = t_cdf(z, T_DOF)
                implied_yes = model_prob_yes >= 0.5

                sk = (lb,)
                sanity.setdefault(sk, [0, 0])
                if abs(exp_move) >= EXP_MOVE_THRESHOLDS[0]:
                    sanity[sk][1] += 1
                    if ((implied_yes and win == 1)
                            or ((not implied_yes) and win == 0)):
                        sanity[sk][0] += 1

                for thr in EXP_MOVE_THRESHOLDS:
                    if abs(exp_move) < thr:
                        continue
                    gkey = (lb, thr)
                    if (tk, lb, thr) in taken:
                        continue  # already entered this window for this cell
                    grids.setdefault(gkey, defaultdict(list))

                    if implied_yes:
                        entry_ask = ya
                        depth = yes_ask_depth
                        if entry_ask is None or depth is None or depth < MIN_DEPTH:
                            continue
                        if model_prob_yes * 100.0 <= entry_ask:
                            continue  # not underpriced vs ask
                        fee = fee_cents(entry_ask)
                        payoff = 100.0 if win == 1 else 0.0
                        pnl = payoff - entry_ask - fee
                    else:
                        entry_ask = (100.0 - yb) if yb is not None else None
                        depth = no_ask_depth
                        if entry_ask is None or depth is None or depth < MIN_DEPTH:
                            continue
                        model_prob_no = 1.0 - model_prob_yes
                        if model_prob_no * 100.0 <= entry_ask:
                            continue
                        fee = fee_cents(entry_ask)
                        payoff = 100.0 if win == 0 else 0.0
                        pnl = payoff - entry_ask - fee

                    grids[gkey][tk].append(pnl)
                    taken.add((tk, lb, thr))

    print(f"\nn_eval_total (reliable decision books): {n_eval_total}")
    print("refusals:", dict(refusal))

    print("\n--- PRE-COST SANITY: BTC-implied direction hit-rate vs terminal ---")
    for sk, (hit, tot) in sorted(sanity.items()):
        if tot:
            print(f"  lookback={sk[0]}s: {hit}/{tot} = {hit/tot:.3f}")

    print("\n--- NET PnL/contract grid (cluster-bootstrap CI, ticker unit) ---")
    best = None
    for gkey in sorted(grids.keys()):
        vbt = grids[gkey]
        n_entries = sum(len(v) for v in vbt.values())
        n_tk = len(vbt)
        if n_entries < 20:
            print(f"  lb={gkey[0]}s thr={gkey[1]}: n={n_entries} (too few, skip)")
            continue
        point, lo, hi = cluster_bootstrap(vbt, N_BOOT)
        flag = "EDGE" if lo > 0 else ("NEG" if hi < 0 else "straddle")
        print(f"  lb={gkey[0]}s thr={gkey[1]}: n={n_entries} tickers={n_tk} "
              f"net={point:+.2f}c CI=[{lo:+.2f},{hi:+.2f}] {flag}")
        cand = {"point": point, "lo": lo, "hi": hi, "n": n_entries,
                "n_tk": n_tk, "gkey": gkey}
        if best is None or point > best["point"]:
            best = cand

    return {"best": best, "sanity": sanity, "n_eval_total": n_eval_total}


if __name__ == "__main__":
    res = run()
    if res and res.get("best"):
        b = res["best"]
        print(f"\nBEST cell: {b['gkey']} net={b['point']:+.2f}c "
              f"CI=[{b['lo']:+.2f},{b['hi']:+.2f}] n={b['n']} tickers={b['n_tk']}")
