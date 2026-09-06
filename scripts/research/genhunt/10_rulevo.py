"""10_rulevo — evolutionary DSL rule-search harness over dataset_interim.csv.

PRE-REGISTRATION (stated BEFORE any data was run)
-------------------------------------------------
WHY: MANDATED paradigm lens (evolutionary-DSL harness). Day-split + once-touched
holdout + complexity penalty + offset-300-only is exactly the discipline future
hunts are gated on. Dead families (book-imbalance / microprice / hawkes / queue)
are EXCLUDED from the feature space by construction. Zero survivors is itself a
publishable efficiency datapoint reinforcing the 41-family null.

(a) COUNTERPARTY: maker-side (quote_*) rules have a named payer BY DEFAULT —
    pooled retail taker flow (prior from this corpus: retail takers lose
    0.2-0.8c/ct gross), specifically the takers whose prints strictly cross our
    resting quote. Taker-side (take_*) winners have NO default payer (the
    counterparty is a resting maker; we'd need to show they are slow/stale) —
    any taker-side survivor is auto-flagged ANOMALY and QUARANTINED, not traded.

(b) SIGNAL + ENTRY/EXIT (exact):
    RULE := IF conj(<=3 predicates) THEN action.
    Predicates: {feature <= theta | feature >= theta} over offset==300 rows of
    dataset_interim.csv columns: yes_mid, spread, rv_5s, mom_60s, btc_mom_60s,
    dist_sigma, edge_pn := p_normal - yes_mid/100, hour-of-day (UTC, from
    close_ts), weekend (UTC dow in {Sat,Sun}), asset one-hots. NaN feature ->
    predicate False (HYPE/BNB have NaN spot features; they can only be reached
    via one-hot/book predicates).
    Actions (fixed 1 contract, hold to settlement, delta in {1c, 2c}):
      take_yes      buy YES at executable yes_ask (taker)
      take_no       buy NO at executable 100-yes_bid (taker)
      quote_yes@d   rest a YES bid at floor(yes_mid)-d; FILLS ONLY IF a later
                    strict-cross print exists (a trade in CR/trades for this
                    ticker, event ts strictly inside (decision_ts, close_ts],
                    printing STRICTLY BELOW our bid). Per-ticker ts_ms sort.
      quote_no@d    rest a YES ask at ceil(yes_mid)+d (== buy NO at 100-that);
                    fills only on a later print STRICTLY ABOVE our ask.
      abstain
    Decision time = close_ts - 300 (offset-300 rows ONLY: one obs per window —
    pseudo-replication guard). No look-ahead: features are the dataset's
    as-of-decision values; fills use only prints AFTER decision time.

(c) FEE MATH: taker pays kalshi_fee_per_contract_cents(price) = 7*p*(1-p) cents
    (amortized large-order rate). Maker fee = ZERO; the crossing taker pays
    Kalshi — i.e. maker PnL mirrors taker GROSS, not taker net (gotcha 1).
    Maker net PnL = settle - price (cents), no fee term.

    FITNESS (GA, TRAIN only): total net PnL summed over triggered+executed rows
    divided by the number of ALL train windows (abstain/no-fill rows count 0),
    minus 0.05c per DSL node (node count = n_predicates + 1 action node).

    SEARCH: population 500, tournament-4 selection, elitism top-10, mutation on
    theta/feature/op/action (+ add/drop predicate within <=3), 50 generations,
    3 independent seeded restarts (seeds 101, 202, 303). Theta values drawn
    from TRAIN-quantile grids only.

    SPLITS (by UTC day of close_ts):
      TRAIN      2026-05-30 .. 2026-06-02   (GA fitness)
      VALIDATION 2026-06-03 .. 2026-06-05   (ALL selection happens here)
      HOLDOUT    2026-06-06+ rows of tonight's fuller dataset.csv, touched
                 EXACTLY ONCE by the single pre-registered champion per restart
                 (3 holdout evaluations total, no exceptions).
    CHAMPION (pre-registered selector, per restart): among final population +
    hall-of-fame, candidates with >=30 VALIDATION trades; champion = argmax of
    validation fitness (val total PnL / n_val_windows - 0.05*nodes). If no
    candidate reaches 30 validation trades, the restart yields zero survivors.

(d) KILL CRITERION (numeric, per rule): DEAD unless ALL of
      1. >= 30 VALIDATION trades
      2. validation day-bootstrap (fairvalue_model.day_bootstrap_ci, resample
         DAYS never rows) 95% CI lower bound on per-trade net PnL > 0
      3. complexity <= 6 DSL nodes
      4. HOLDOUT net total PnL has the same sign as validation net total PnL
    HARNESS DEAD if 3 seeded restarts yield zero validation survivors ->
    publish as a market-efficiency datapoint reinforcing the 41-family null
    (verdict NO_EDGE). Survivor passing 1-3 but holdout unavailable ->
    INCONCLUSIVE (pending holdout; holdout stays untouched).

GATING (HARD): does not read dataset_interim.csv until REPORT_interim.txt
contains a line starting "[fv] DONE" (the csv streams while writing). Holdout
likewise gated on dataset.csv's own "[fv] DONE" in REPORT.txt.

Labels: 'won'; cross-checked against phase1b load_determined (lifecycle truth).
Settlement endgame (<120s to close) is informed-flow territory: maker fills
landing there are counted but ALSO reported separately in the funnel (gotcha 4).

Usage:
  python3 scripts/research/genhunt/10_rulevo.py [--smoke] [--gate-wait-min 90]
      [--holdout-wait-min 0] [--pop 500] [--gens 50]
"""
from __future__ import annotations

import argparse
import math
import os
import pickle
import re
import subprocess
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone

import numpy as np
import pandas as pd

_REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)

from scripts.research.fairvalue_model import day_bootstrap_ci  # noqa: E402
from scripts.research.phase1b_real_price_economics import load_determined  # noqa: E402
from scripts.research.settlement_convergence_p1a import (  # noqa: E402
    kalshi_fee_per_contract_cents,
)
from scripts.research.zstd_stream import assert_zstd_ok  # noqa: E402  (repo root on sys.path above)

CR = os.path.expanduser("~/kalshi-research-data/fairvalue")
TRAIN_DAYS = {"2026-05-30", "2026-05-31", "2026-06-01", "2026-06-02"}
VAL_DAYS = {"2026-06-03", "2026-06-04", "2026-06-05"}
HOLDOUT_FROM = "2026-06-06"
OFFSET = 300
NODE_PENALTY = 0.05            # cents per DSL node, on per-window mean
SEEDS = (101, 202, 303)
ACTIONS = ("abstain", "take_yes", "take_no",
           "quote_yes_d1", "quote_yes_d2", "quote_no_d1", "quote_no_d2")
MAKER_ACTIONS = {"quote_yes_d1", "quote_yes_d2", "quote_no_d1", "quote_no_d2"}
ENDGAME_S = 120

TRADE_RE = re.compile(
    r'\\"market_ticker\\":\\"([A-Z0-9.-]+)\\",\\"yes_price_dollars\\":\\"([0-9.]+)\\"'
    r'.*?\\"count_fp\\":\\"([0-9.]+)\\",\\"taker_side\\":\\"(yes|no)\\"'
    r'.*?\\"ts_ms\\":(\d+)')


def log(msg):
    print(f"[rulevo] {msg}", flush=True)


# ---------------------------------------------------------------- gating ----

def wait_for_done(report_path: str, wait_min: float, tag: str) -> bool:
    """Poll report for a line starting '[fv] DONE'. True if gate opens."""
    deadline = time.time() + wait_min * 60
    while True:
        if os.path.exists(report_path):
            with open(report_path) as f:
                if any(ln.startswith("[fv] DONE") for ln in f):
                    log(f"GATE OPEN ({tag}): '[fv] DONE' present in {report_path}")
                    return True
        if time.time() >= deadline:
            log(f"GATE CLOSED ({tag}): no '[fv] DONE' in {report_path} "
                f"after {wait_min:.0f} min")
            return False
        log(f"gate ({tag}) not open yet; polling... "
            f"({(deadline - time.time())/60:.1f} min left)")
        time.sleep(30)


# ------------------------------------------------------------- data load ----

def load_rows(csv_path: str) -> pd.DataFrame:
    df = pd.read_csv(csv_path)
    n_raw = len(df)
    df = df[df["offset_s"] == OFFSET].copy()        # pseudo-replication guard
    n_off = len(df)
    dts = pd.to_datetime(df["close_ts"], unit="s", utc=True)
    df["day"] = dts.dt.strftime("%Y-%m-%d")
    df["hour"] = dts.dt.hour.astype(float)
    df["weekend"] = (dts.dt.dayofweek >= 5).astype(float)
    df["edge_pn"] = df["p_normal"] - df["yes_mid"] / 100.0
    df["decision_ts"] = df["close_ts"] - OFFSET
    log(f"funnel[{os.path.basename(csv_path)}]: rows_total={n_raw} "
        f"offset300={n_off} tickers={df['ticker'].nunique()}")
    return df


def load_trades_for(tickers: set, close_by_ticker: dict, days: list) -> dict:
    """{ticker: np.array[(ts_ms, price_c, count, taker_yes)] sorted by ts_ms},
    restricted to event-ts in [close-420s, close] per ticker."""
    out = defaultdict(list)
    n_lines = n_match = n_kept = 0
    for day in days:
        path = f"{CR}/trades/day={day}.jsonl.zst"
        if not os.path.exists(path):
            log(f"trades file MISSING: {path}")
            continue
        proc = subprocess.Popen(["zstd", "-dc", path], stdout=subprocess.PIPE,
                                bufsize=1 << 20)
        for raw in proc.stdout:
            n_lines += 1
            m = TRADE_RE.search(raw.decode("utf-8", "replace"))
            if not m:
                continue
            n_match += 1
            tkr = m.group(1)
            close = close_by_ticker.get(tkr)
            if close is None or tkr not in tickers:
                continue
            ts = int(m.group(5)) / 1000.0
            if not (close - 420.0 <= ts <= close):
                continue
            out[tkr].append((int(m.group(5)),
                             round(float(m.group(2)) * 100.0),
                             float(m.group(3)),
                             1.0 if m.group(4) == "yes" else 0.0))
            n_kept += 1
        proc.stdout.close()
        proc.wait()
        # ticket 86bbvrx1t: this loop has NO `break`, so reaching here is a
        # true EOF — a non-zero zstd exit means the day was TRUNCATED and the
        # trade counts below would silently under-report.
        assert_zstd_ok(proc, path, exhausted=True, require_nonempty=False)
    res = {}
    for tkr, rows in out.items():
        rows.sort(key=lambda r: r[0])               # per-ticker ts_ms sort
        res[tkr] = np.array(rows, dtype=float)
    log(f"trades: lines={n_lines:,} regex_matched={n_match:,} "
        f"kept_in_window={n_kept:,} tickers_with_prints={len(res)}")
    if n_lines and n_match / n_lines < 0.5:
        log("WARNING: trade regex matched <50% of lines — parser may be broken")
    return res


# ------------------------------------------------- per-row action payoffs ----

def build_payoffs(df: pd.DataFrame, trades: dict) -> dict:
    """Vectorized per-row net PnL (cents/contract) + executed flags per action."""
    n = len(df)
    won = df["won"].values.astype(float)
    ask = df["yes_ask"].values.astype(float)
    bid = df["yes_bid"].values.astype(float)
    mid = df["yes_mid"].values.astype(float)
    dec_ms = (df["decision_ts"].values.astype(float)) * 1000.0
    close_ms = (df["close_ts"].values.astype(float)) * 1000.0

    # strict-cross extremes of prints in (decision, close]
    min_p = np.full(n, np.nan)
    max_p = np.full(n, np.nan)
    last_fill_age = np.full(n, np.nan)      # seconds-to-close of LAST print
    tickers = df["ticker"].values
    for i in range(n):
        arr = trades.get(tickers[i])
        if arr is None or not len(arr):
            continue
        sel = (arr[:, 0] > dec_ms[i]) & (arr[:, 0] <= close_ms[i])
        if not sel.any():
            continue
        px = arr[sel, 1]
        min_p[i], max_p[i] = px.min(), px.max()
        last_fill_age[i] = (close_ms[i] - arr[sel, 0].max()) / 1000.0

    pay = {}
    # taker: buy YES at ask
    v = (ask >= 1) & (ask <= 99) & np.isfinite(ask)
    pnl = np.where(v, won * 100.0 - ask
                   - np.vectorize(kalshi_fee_per_contract_cents)(np.clip(ask, 1, 99)), 0.0)
    pay["take_yes"] = (pnl * v, v)
    # taker: buy NO at 100-bid
    cost_no = 100.0 - bid
    v = (cost_no >= 1) & (cost_no <= 99) & np.isfinite(cost_no)
    pnl = np.where(v, (1.0 - won) * 100.0 - cost_no
                   - np.vectorize(kalshi_fee_per_contract_cents)(np.clip(cost_no, 1, 99)), 0.0)
    pay["take_no"] = (pnl * v, v)
    # maker quotes — fill ONLY on later strict-cross print; maker fee zero
    for d in (1, 2):
        b = np.floor(mid) - d
        filled = (b >= 1) & (b <= 99) & np.isfinite(min_p) & (min_p < b)
        pay[f"quote_yes_d{d}"] = (np.where(filled, won * 100.0 - b, 0.0), filled)
        a = np.ceil(mid) + d
        filled = (a >= 1) & (a <= 99) & np.isfinite(max_p) & (max_p > a)
        pay[f"quote_no_d{d}"] = (np.where(filled, a - won * 100.0, 0.0), filled)
    pay["abstain"] = (np.zeros(n), np.zeros(n, dtype=bool))
    pay["_endgame_fill_possible"] = np.isfinite(last_fill_age) & (last_fill_age < ENDGAME_S)
    return pay


# ------------------------------------------------------------- DSL + GA -----

FEATURES = ["yes_mid", "spread", "rv_5s", "mom_60s", "btc_mom_60s",
            "dist_sigma", "edge_pn", "hour", "weekend"]


def feature_matrix(df: pd.DataFrame, assets: list) -> np.ndarray:
    cols = [df[f].values.astype(float) for f in FEATURES]
    for a in assets:
        cols.append((df["asset"].values == a).astype(float))
    return np.column_stack(cols)


def theta_grids(X: np.ndarray, n_feat_base: int) -> list:
    grids = []
    for j in range(X.shape[1]):
        col = X[:, j]
        col = col[np.isfinite(col)]
        if j >= n_feat_base:                      # one-hots
            grids.append(np.array([0.5]))
            continue
        qs = np.unique(np.quantile(col, np.linspace(0.02, 0.98, 49)))
        grids.append(qs if len(qs) else np.array([0.0]))
    return grids


def rule_key(rule):
    preds, act = rule
    return (tuple(sorted(preds)), act)


def eval_rule(rule, X, pay, n_windows, day_idx=None, n_days=0):
    preds, act = rule
    mask = np.ones(X.shape[0], dtype=bool)
    for (fi, op, th) in preds:
        col = X[:, fi]
        with np.errstate(invalid="ignore"):
            mask &= (col <= th) if op == 0 else (col >= th)
    pnl_vec, exec_vec = pay[ACTIONS[act]]
    trig = mask & exec_vec
    total = float(pnl_vec[trig].sum())
    n_tr = int(trig.sum())
    nodes = len(preds) + 1
    fit = total / max(n_windows, 1) - NODE_PENALTY * nodes
    per_day = None
    if day_idx is not None:
        per_day = np.bincount(day_idx[trig], weights=pnl_vec[trig], minlength=n_days)
    return fit, total, n_tr, trig, per_day


def random_rule(rng, n_feats, grids):
    k = rng.integers(1, 4)
    preds = []
    for _ in range(k):
        fi = int(rng.integers(0, n_feats))
        preds.append((fi, int(rng.integers(0, 2)),
                      float(rng.choice(grids[fi]))))
    return (tuple(preds), int(rng.integers(0, len(ACTIONS))))


def mutate(rule, rng, n_feats, grids):
    preds, act = list(rule[0]), rule[1]
    choice = rng.integers(0, 6)
    if choice == 0 and len(preds) < 3:            # add predicate
        fi = int(rng.integers(0, n_feats))
        preds.append((fi, int(rng.integers(0, 2)), float(rng.choice(grids[fi]))))
    elif choice == 1 and len(preds) > 1:          # drop predicate
        preds.pop(rng.integers(0, len(preds)))
    elif choice == 2:                             # theta jitter on grid
        i = rng.integers(0, len(preds))
        fi, op, th = preds[i]
        g = grids[fi]
        pos = int(np.searchsorted(g, th)) + int(rng.integers(-3, 4))
        preds[i] = (fi, op, float(g[np.clip(pos, 0, len(g) - 1)]))
    elif choice == 3:                             # feature swap
        i = rng.integers(0, len(preds))
        fi = int(rng.integers(0, n_feats))
        preds[i] = (fi, int(rng.integers(0, 2)), float(rng.choice(grids[fi])))
    elif choice == 4:                             # op flip
        i = rng.integers(0, len(preds))
        fi, op, th = preds[i]
        preds[i] = (fi, 1 - op, th)
    else:                                         # action change
        act = int(rng.integers(0, len(ACTIONS)))
    return (tuple(preds), act)


def run_restart(seed, Xtr, pay_tr, n_tr_windows, pop_size, gens, n_feats, grids):
    rng = np.random.default_rng(seed)
    pop = [random_rule(rng, n_feats, grids) for _ in range(pop_size)]
    memo = {}
    hof = {}

    def fit_of(rule):
        k = rule_key(rule)
        if k not in memo:
            memo[k] = eval_rule(rule, Xtr, pay_tr, n_tr_windows)[0]
        return memo[k]

    for g in range(gens):
        fits = np.array([fit_of(r) for r in pop])
        order = np.argsort(fits)[::-1]
        for i in order[:25]:
            hof[rule_key(pop[i])] = pop[i]
        elites = [pop[i] for i in order[:10]]
        children = []
        while len(children) < pop_size - len(elites):
            idx = rng.integers(0, pop_size, size=4)
            parent = pop[idx[np.argmax(fits[idx])]]
            children.append(mutate(parent, rng, n_feats, grids))
        pop = elites + children
        if g % 10 == 0 or g == gens - 1:
            log(f"  seed={seed} gen={g:>2} best_train_fit={fits.max():+.4f} "
                f"c/window (memo={len(memo)})")
    cands = {rule_key(r): r for r in pop}
    cands.update(hof)
    return list(cands.values())


# ------------------------------------------------------------- reporting ----

def fmt_rule(rule, assets):
    names = FEATURES + [f"is_{a}" for a in assets]
    preds, act = rule
    ps = " AND ".join(f"{names[fi]} {'<=' if op == 0 else '>='} {th:.4g}"
                      for fi, op, th in preds)
    return f"IF {ps} THEN {ACTIONS[act]}"


def tape_excerpt(ticker, dec_ts, close_ts, trades, lines):
    arr = trades.get(ticker)
    lines.append(f"    tape {ticker} (decision T-300 = {datetime.fromtimestamp(dec_ts, tz=timezone.utc):%H:%M:%S}Z):")
    if arr is None or not len(arr):
        lines.append("      (no prints in [-60s,+120s] of decision)")
        return
    sel = (arr[:, 0] >= (dec_ts - 60) * 1000) & (arr[:, 0] <= (dec_ts + 120) * 1000)
    rows = arr[sel][:12]
    if not len(rows):
        lines.append("      (no prints in [-60s,+120s] of decision)")
    for ts_ms, px, cnt, ty in rows:
        lines.append(f"      t={ts_ms/1000 - dec_ts:+7.1f}s  yes@{int(px):>2}c "
                     f"x{cnt:>8.2f}  taker={'yes' if ty else 'no '}"
                     f"  ttc={close_ts - ts_ms/1000:6.1f}s")


# ------------------------------------------------------------------ main ----

def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--smoke", action="store_true",
                    help="pipeline smoke: 1 sealed train day, tiny GA, no holdout")
    ap.add_argument("--gate-wait-min", type=float, default=90)
    ap.add_argument("--holdout-wait-min", type=float, default=0)
    ap.add_argument("--pop", type=int, default=500)
    ap.add_argument("--gens", type=int, default=50)
    a = ap.parse_args(argv)

    t0 = time.time()
    if not wait_for_done(f"{CR}/REPORT_interim.txt", a.gate_wait_min, "interim"):
        log("VERDICT: DATA_GAP — interim extraction gate never opened")
        return 2

    df = load_rows(f"{CR}/dataset_interim.csv")
    if a.smoke:
        train_days, val_days = {"2026-06-01"}, {"2026-06-02"}
        pop_size, gens, seeds = 60, 5, (101,)
    else:
        train_days, val_days = TRAIN_DAYS, VAL_DAYS
        pop_size, gens, seeds = a.pop, a.gens, SEEDS

    tr = df[df["day"].isin(train_days)].reset_index(drop=True)
    va = df[df["day"].isin(val_days)].reset_index(drop=True)
    log(f"funnel: train_windows={len(tr)} (days {sorted(train_days)}) "
        f"val_windows={len(va)} (days {sorted(val_days)})")
    if len(tr) < 200 or len(va) < 200:
        log("VERDICT: DATA_GAP — too few windows in train/validation split")
        return 2

    # ---- label cross-check vs lifecycle ground truth -----------------------
    if not a.smoke:
        pkl = f"{CR}/_tmp_genhunt_lifecycle.pkl"
        det = None
        if os.path.exists(pkl):
            try:
                with open(pkl, "rb") as f:
                    det = pickle.load(f)
            except Exception:
                det = None
        if not isinstance(det, dict) or not det:
            det = load_determined(f"{CR}/lifecycle",
                                  set(df["asset"].unique()))
        both = df[df["ticker"].isin(det.keys())]
        mism = sum(1 for t, w in zip(both["ticker"], both["won"])
                   if (det[t]["result"] == "yes") != bool(w))
        log(f"label cross-check vs load_determined: n={len(both)} mismatches={mism}")
        if len(both) and mism / len(both) > 0.01:
            log("VERDICT: DATA_GAP — 'won' labels disagree with lifecycle truth")
            return 2

    # ---- trades (fill validation + tape) -----------------------------------
    need = pd.concat([tr, va])
    close_by_ticker = dict(zip(need["ticker"], need["close_ts"].astype(float)))
    days_needed = sorted(set(need["day"]))
    trades = load_trades_for(set(need["ticker"]), close_by_ticker, days_needed)

    pay_tr = build_payoffs(tr, trades)
    pay_va = build_payoffs(va, trades)
    for tag, d, pay in (("train", tr, pay_tr), ("val", va, pay_va)):
        fills = {act: int(pay[act][1].sum()) for act in MAKER_ACTIONS}
        log(f"funnel[{tag}]: maker strict-cross fill availability {fills} "
            f"of {len(d)} windows; taker-valid={int(pay['take_yes'][1].sum())}; "
            f"endgame(<{ENDGAME_S}s)-fill-possible={int(pay['_endgame_fill_possible'].sum())}")

    assets = sorted(df["asset"].unique())
    Xtr = feature_matrix(tr, assets)
    Xva = feature_matrix(va, assets)
    n_feats = Xtr.shape[1]
    grids = theta_grids(Xtr, len(FEATURES))

    va_days_sorted = sorted(va["day"].unique())
    va_day_idx = np.array([va_days_sorted.index(d) for d in va["day"]])

    report = [f"RULEVO counterparty-inspection report  {datetime.now(timezone.utc):%Y-%m-%dT%H:%M:%SZ}"]
    champions = []
    all_top = []
    for seed in seeds:
        log(f"restart seed={seed}: GA pop={pop_size} gens={gens}")
        cands = run_restart(seed, Xtr, pay_tr, len(tr), pop_size, gens, n_feats, grids)
        scored = []
        for r in cands:
            vfit, vtot, vn, vtrig, vper_day = eval_rule(
                r, Xva, pay_va, len(va), va_day_idx, len(va_days_sorted))
            scored.append((vfit, vtot, vn, r, vtrig, vper_day))
        scored.sort(key=lambda s: s[0], reverse=True)
        log(f"--- seed={seed} TOP-10 by validation fitness ---")
        for vfit, vtot, vn, r, _, vper_day in scored[:10]:
            pd_s = " ".join(f"{d[-5:]}:{p:+.0f}" for d, p in zip(va_days_sorted, vper_day))
            log(f"  vfit={vfit:+.4f} c/win  total={vtot:+8.1f}c  n_trades={vn:>4}  "
                f"perday[{pd_s}]  {fmt_rule(r, assets)}")
        all_top.extend(scored[:10])
        elig = [s for s in scored if s[2] >= 30]
        champ = elig[0] if elig else None
        champions.append((seed, champ))
        if champ is None:
            log(f"  seed={seed}: NO candidate with >=30 validation trades -> zero survivors")

    # ---- kill-criterion evaluation per champion ----------------------------
    survivors = []
    pending_holdout = []
    for seed, champ in champions:
        if champ is None:
            continue
        vfit, vtot, vn, r, vtrig, _ = champ
        pnl_vec = pay_va[ACTIONS[r[1]]][0]
        tdf = pd.DataFrame({"day": va["day"].values[vtrig], "pnl": pnl_vec[vtrig]})
        mean, lo, hi = day_bootstrap_ci(tdf, "pnl")
        nodes = len(r[0]) + 1
        ok = (vn >= 30) and (lo > 0) and (nodes <= 6)
        log(f"champion seed={seed}: {fmt_rule(r, assets)}")
        log(f"  val n={vn} mean={mean:+.3f}c/ct CI95=[{lo:+.3f},{hi:+.3f}] "
            f"nodes={nodes} -> {'PASS val-stage' if ok else 'DEAD'}")
        report.append(f"\nchampion seed={seed}: {fmt_rule(r, assets)}  "
                      f"val n={vn} mean={mean:+.3f}c CI=[{lo:+.3f},{hi:+.3f}]")
        is_maker = ACTIONS[r[1]] in MAKER_ACTIONS
        story = ("payer: pooled retail taker flow crossing our resting quote "
                 "(prior 0.2-0.8c/ct gross loss)" if is_maker else
                 "ANOMALY: taker-side rule — counterparty is a resting maker; "
                 "no default payer story -> QUARANTINED")
        report.append(f"  counterparty: {story}")
        ent = va[vtrig].head(3)
        for _, row in ent.iterrows():
            tape_excerpt(row["ticker"], row["decision_ts"], row["close_ts"],
                         trades, report)
        if ok:
            pending_holdout.append((seed, champ, vtot, is_maker))

    holdout_done = False
    if pending_holdout and not a.smoke:
        if wait_for_done(f"{CR}/REPORT.txt", a.holdout_wait_min, "holdout") \
                and os.path.exists(f"{CR}/dataset.csv"):
            hd = load_rows(f"{CR}/dataset.csv")
            hd = hd[hd["day"] >= HOLDOUT_FROM].reset_index(drop=True)
            log(f"funnel: holdout_windows={len(hd)} days={sorted(hd['day'].unique())}")
            cbt = dict(zip(hd["ticker"], hd["close_ts"].astype(float)))
            htr = load_trades_for(set(hd["ticker"]), cbt, sorted(set(hd["day"])))
            pay_hd = build_payoffs(hd, htr)
            Xhd = feature_matrix(hd, assets)
            holdout_done = True
            for seed, champ, vtot, is_maker in pending_holdout:
                _, htot, hn, _, _ = eval_rule(champ[3], Xhd, pay_hd, len(hd))
                same = (htot > 0) == (vtot > 0) and hn > 0
                log(f"HOLDOUT (touched ONCE) seed={seed}: total={htot:+.1f}c "
                    f"n={hn} same_sign_as_val={same}")
                if same:
                    survivors.append((seed, champ, is_maker, htot, hn))
        else:
            log("holdout dataset.csv not ready — survivors stay PENDING_HOLDOUT")

    # ---- verdict ------------------------------------------------------------
    log("=" * 70)
    n_val_stage = len(pending_holdout)
    if n_val_stage == 0:
        log("VERDICT: NO_EDGE — HARNESS DEAD: 3 seeded restarts, zero validation "
            "survivors (>=30 trades AND day-bootstrap CI low>0 AND <=6 nodes). "
            "Market-efficiency datapoint reinforcing the 41-family null.")
        verdict = "NO_EDGE"
    elif not holdout_done:
        log(f"VERDICT: INCONCLUSIVE — {n_val_stage} champion(s) pass the "
            "validation stage but holdout dataset.csv is not available yet; "
            "holdout untouched (re-run with --holdout-wait-min when it lands).")
        verdict = "INCONCLUSIVE"
    elif survivors:
        quar = [s for s in survivors if not s[2]]
        live = [s for s in survivors if s[2]]
        if live:
            log(f"VERDICT: EDGE_CANDIDATE — {len(live)} maker-side survivor(s) "
                f"pass all 4 kill conditions; {len(quar)} taker-side ANOMALY quarantined.")
            verdict = "EDGE_CANDIDATE"
        else:
            log(f"VERDICT: NO_EDGE — only taker-side survivors; all ANOMALY-"
                "quarantined per counterparty contract (no named payer).")
            verdict = "NO_EDGE"
    else:
        log("VERDICT: NO_EDGE — champions failed holdout sign agreement.")
        verdict = "NO_EDGE"

    rp = f"{CR}/genhunt10_rulevo_report.txt"
    with open(rp, "w") as f:
        f.write("\n".join(report) + "\n")
    log(f"counterparty-inspection report -> {rp}")
    log(f"wall time {time.time() - t0:.0f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
