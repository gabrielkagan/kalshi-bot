#!/usr/bin/env python3
"""cvar_risk_averse_sizing — CVaR / risk-averse re-sizing backtest (family: sizing-gate).

THESIS UNDER TEST: "a few big losses outweigh many small gains." We re-size each
historical decided-15M trade to minimize the portfolio's 95% CVaR (expected
shortfall, the mean of the worst-5% per-contract outcomes) rather than to
maximize Kelly growth, holding the TOTAL contract budget fixed (capital-neutral
re-allocation). Headline: change in 95% CVaR (cents) AND change in total PnL
vs the actual half-Kelly book, each with a >=1000-resample bootstrap CI.

DATA
----
Population = `evaluated_opportunities` rows the bot DECIDED to size in-window
(crypto-15M, product_type='15m', evaluation_time >= 2026-05-30T21:06Z,
kelly_f>0, position_size>0, market_result IS NOT NULL, market_price in [1,99]).
n=866 such rows. The bot's `position_size` already encodes its half-Kelly
sizing, so it IS the "actual half-Kelly book" — no re-derivation needed.

We do NOT use settled_trades for statistics (only n=14 in-window; per the
corpus brief). Outcomes come from market_result. This is a COUNTERFACTUAL-PnL
backtest on already-decided trades: we never invent new trades, only re-weight
the sizes the bot chose, so there is no fill-model fabrication on the entry —
the bot's actual entries are taken as given at their recorded market_price as
TAKER fills (conservative; maker-rebate=0 assumed, see below).

PNL MECHANICS (per contract, cents)
-----------------------------------
YES contract bought at p cents:  win -> +(100-p)-fee ; loss -> -p-fee
NO  contract bought at p cents:  win -> +(100-p)-fee ; loss -> -p-fee
(market_price is the entry price for the chosen side; "win" = market_result
matches the side.) Fee = ceil(0.07 * count * p * (100-p) / 100) on the count,
amortized per-contract for the re-sizer. Taker fee on entry; settlement has no
fee. Maker rebate assumed $0 (Kalshi pays no maker rebate — see bot/models.py).

RE-SIZER
--------
For each (asset, side, price-tier) cell we estimate the realized per-contract
outcome distribution {win w/ prob phat, loss}. The actual book holds n_i
contracts in cell i. We re-allocate the SAME total contracts across cells to
minimize the portfolio 95% CVaR while not destroying expected value below a
floor. Concretely we compute, per cell, the per-contract mean mu_i and the
per-contract CVaR-95 contribution, then solve a simple risk-averse allocation:
weight_i proportional to max(0, mu_i) / (1 + lambda * tail_i), renormalized to
the original total contract count. lambda sweeps; we report the lambda that
minimizes portfolio CVaR (the risk-averse extreme) AND the resulting PnL.
This is a transparent mean-CVaR heuristic (not a full LP) appropriate for a
1-day, thin-tier corpus; it answers the headline directly: does shifting weight
away from fat-tail cells reduce 95% CVaR, and at what PnL cost?

LOOK-AHEAD: the per-tier phat used to re-size IS computed from the same
realized outcomes we then score (in-sample). This is the canonical danger and
is stated loudly in lookahead_risks — a 1-day corpus cannot support an
out-of-sample tier model. The headline is therefore an UPPER BOUND on the
achievable CVaR reduction, not a deployable strategy.
"""
import sys
import json
import math
import sqlite3
from collections import defaultdict

sys.path.insert(0, "/Users/gabrielkagan/Documents/kalshi-bot")

DB = "/tmp/edge_daily/state.db"
WINDOW_START = "2026-05-30T21:06:00"
FEE_MULT_TAKER = 0.07
CVAR_ALPHA = 0.95  # worst 5%
N_BOOT = 2000
RNG_SEED = 12345


def taker_fee_cents(count, price_cents):
    """Total taker fee in cents for `count` contracts at price_cents."""
    return math.ceil(FEE_MULT_TAKER * count * price_cents * (100 - price_cents) / 100.0)


def per_contract_pnl(side, price_cents, market_result):
    """Per-contract settlement PnL in cents BEFORE fee. Win=+(100-p), loss=-p."""
    won = (market_result == side)
    if won:
        return (100 - price_cents)
    return -price_cents


def price_tier(p):
    """Coarse price tier for outcome-distribution pooling (keeps cells non-tiny)."""
    if p < 30:
        return "lt30"
    if p < 60:
        return "30_60"
    if p < 80:
        return "60_80"
    if p < 90:
        return "80_90"
    return "90_99"


def load_population():
    c = sqlite3.connect(DB)
    q = """
    SELECT asset, side, market_price, market_result, position_size, kelly_f,
           calibrated_prob, vol_regime, evaluation_time
    FROM evaluated_opportunities
    WHERE product_type='15m'
      AND evaluation_time >= ?
      AND kelly_f > 0 AND position_size > 0
      AND market_result IS NOT NULL
      AND market_price BETWEEN 1 AND 99
      AND side IN ('yes','no')
    ORDER BY evaluation_time
    """
    rows = []
    for asset, side, mp, mr, ps, kf, cp, vr, et in c.execute(q, (WINDOW_START,)):
        rows.append({
            "idx": len(rows),  # STABLE key (never use id() — it gets reused)
            "asset": asset, "side": side, "price": int(round(mp)),
            "result": mr, "n": int(ps), "kelly_f": kf,
            "cp": cp, "vol_regime": vr, "eval_time": et,
            "tier": price_tier(mp),
        })
    c.close()
    return rows


def actual_pnl_per_contract(rows):
    """One per-contract PnL value per CONTRACT (expanded), net of amortized fee."""
    vals = []
    for r in rows:
        gross = per_contract_pnl(r["side"], r["price"], r["result"])
        fee_total = taker_fee_cents(r["n"], r["price"])
        fee_per = fee_total / r["n"]
        net = gross - fee_per
        vals.extend([net] * r["n"])
    return vals


def cvar(losses_sorted, alpha):
    """CVaR_alpha of a PnL sample: mean of the worst (1-alpha) fraction (a loss
    is a negative PnL). Returns the expected shortfall as a NEGATIVE cents value
    (more negative = worse tail). Input is the raw per-contract PnL list."""
    if not losses_sorted:
        return 0.0
    s = sorted(losses_sorted)  # ascending: worst (most negative) first
    k = max(1, int(math.ceil((1 - alpha) * len(s))))
    tail = s[:k]
    return sum(tail) / len(tail)


def cell_stats(rows):
    """Per (asset, side, tier) cell: realized win rate, per-contract mean, tier tail."""
    cells = defaultdict(list)
    for r in rows:
        key = (r["asset"], r["side"], r["tier"])
        gross = per_contract_pnl(r["side"], r["price"], r["result"])
        fee_per = taker_fee_cents(r["n"], r["price"]) / r["n"]
        cells[key].append((gross - fee_per, r["price"]))
    stats = {}
    for key, vals in cells.items():
        pcs = [v[0] for v in vals]
        mu = sum(pcs) / len(pcs)
        # tail = magnitude of expected shortfall. cvar() returns the mean of the
        # worst 5% of per-contract PnL; for a cell with NO losing tail that mean
        # is still a GAIN (positive), so -cvar would go negative. Clamp to >=0:
        # "no downside in the worst 5%" == zero tail risk, never a NEGATIVE risk
        # weight (which previously drove negative contract counts -> budget blow-up).
        tail = max(0.0, -cvar(pcs, CVAR_ALPHA))
        stats[key] = {"mu": mu, "tail": tail, "n": len(pcs),
                      "avg_price": sum(v[1] for v in vals) / len(vals)}
    return stats


def resize_book(rows, stats, lam, stats_for_weights=None):
    """Re-allocate the SAME total contracts across cells with risk-averse weights.
    weight_i ~ max(eps, mu_i) / (1 + lam * tail_i). The budget (total contracts)
    is preserved EXACTLY via largest-remainder rounding so the comparison is
    capital-neutral. `stats_for_weights` (if given) supplies the mu/tail used to
    BUILD the weights — pass a TRAIN-fold stats dict here for an out-of-sample
    re-size while `rows`/`stats` are the TEST fold being scored."""
    sw = stats_for_weights if stats_for_weights is not None else stats
    total = sum(r["n"] for r in rows)
    cell_w = {}
    for key, st in sw.items():
        base = max(1e-6, st["mu"])  # only allocate to non-negative-EV cells
        denom = max(1e-6, 1.0 + lam * st["tail"])  # tail>=0 so denom>=1; guard anyway
        cell_w[key] = base / denom
    # original contracts per cell (in the rows being scored)
    cell_orig = defaultdict(int)
    for r in rows:
        cell_orig[(r["asset"], r["side"], r["tier"])] += r["n"]
    # cells with no train-weight (unseen in train fold) get zero -> drop to floor
    raw = {k: cell_w.get(k, 0.0) for k in cell_orig}
    s = sum(raw.values())
    if s <= 0:
        return {r["idx"]: r["n"] for r in rows}  # degenerate: keep actual
    # real-valued cell targets that sum to `total`
    cell_target_f = {k: total * raw[k] / s for k in raw}
    # split each cell target across its rows pro-rata to original size (real)
    row_target_f = {}
    for key in cell_orig:
        members = [r for r in rows
                   if (r["asset"], r["side"], r["tier"]) == key]
        orig_mass = cell_orig[key] or 1
        for r in members:
            row_target_f[r["idx"]] = cell_target_f[key] * (r["n"] / orig_mass)
    # largest-remainder rounding to hit EXACTLY `total` contracts
    floored = {k: int(math.floor(v)) for k, v in row_target_f.items()}
    deficit = total - sum(floored.values())
    rema = sorted(row_target_f.items(),
                  key=lambda kv: kv[1] - math.floor(kv[1]), reverse=True)
    new_n = dict(floored)
    i = 0
    while deficit > 0 and rema:
        new_n[rema[i % len(rema)][0]] += 1
        deficit -= 1
        i += 1
    assert all(v >= 0 for v in new_n.values()), "negative contract count"
    assert sum(new_n.values()) == total, (
        f"budget violated: {sum(new_n.values())} != {total}")
    return new_n


def book_pnl_per_contract(rows, n_map):
    vals = []
    for r in rows:
        n = n_map[r["idx"]]
        if n <= 0:
            continue
        gross = per_contract_pnl(r["side"], r["price"], r["result"])
        fee_per = taker_fee_cents(n, r["price"]) / n
        net = gross - fee_per
        vals.extend([net] * n)
    return vals


def bootstrap_ci(actual_vals, resized_vals, n_boot, seed):
    """Bootstrap CI on (delta_cvar, delta_total_pnl). Resamples each book's
    per-contract PnL list independently with replacement (paired by index where
    sizes equal is not possible since lengths differ -> resample each book by its
    own contracts). Returns dict of point + ci for both metrics."""
    import random
    rng = random.Random(seed)
    a = actual_vals
    b = resized_vals
    na, nb = len(a), len(b)
    base_cvar_a = cvar(a, CVAR_ALPHA)
    base_cvar_b = cvar(b, CVAR_ALPHA)
    base_total_a = sum(a)
    base_total_b = sum(b)
    d_cvar = base_cvar_b - base_cvar_a   # >0 means resized tail is LESS negative = better
    d_total = base_total_b - base_total_a
    dc, dt = [], []
    for _ in range(n_boot):
        sa = [a[rng.randrange(na)] for _ in range(na)]
        sb = [b[rng.randrange(nb)] for _ in range(nb)]
        dc.append(cvar(sb, CVAR_ALPHA) - cvar(sa, CVAR_ALPHA))
        dt.append(sum(sb) - sum(sa))
    dc.sort(); dt.sort()

    def ci(arr):
        lo = arr[int(0.025 * len(arr))]
        hi = arr[int(0.975 * len(arr))]
        return lo, hi
    return {
        "cvar_actual": base_cvar_a, "cvar_resized": base_cvar_b,
        "d_cvar": d_cvar, "d_cvar_ci": ci(dc),
        "total_actual": base_total_a, "total_resized": base_total_b,
        "d_total": d_total, "d_total_ci": ci(dt),
    }


def main():
    rows = load_population()
    n_rows = len(rows)
    total_contracts = sum(r["n"] for r in rows)
    print(f"[pop] decided-sized in-window 15M rows = {n_rows}, "
          f"total contracts = {total_contracts}")

    actual_vals = actual_pnl_per_contract(rows)
    actual_total = sum(actual_vals)
    actual_cvar = cvar(actual_vals, CVAR_ALPHA)
    print(f"[actual] total PnL = {actual_total:.0f}c (${actual_total/100:.2f}), "
          f"CVaR95 = {actual_cvar:.2f}c/contract over {len(actual_vals)} contracts")

    LAMBDAS = [0.0, 0.05, 0.1, 0.25, 0.5, 1.0, 2.0, 5.0, 10.0]

    # ---------------------------------------------------------------
    # (1) IN-SAMPLE (hindsight upper bound). Weights built from the SAME
    #     realized outcomes we score. This is NOT deployable; it shows the
    #     mechanical ceiling and exposes the look-ahead magnitude.
    # ---------------------------------------------------------------
    stats = cell_stats(rows)
    print(f"[cells] {len(stats)} (asset,side,tier) cells (in-sample)")
    is_best = None
    print("[IN-SAMPLE lambda sweep] (HINDSIGHT — upper bound, not deployable)")
    for lam in LAMBDAS:
        n_map = resize_book(rows, stats, lam)
        vals = book_pnl_per_contract(rows, n_map)
        if not vals:
            continue
        cv = cvar(vals, CVAR_ALPHA)
        tot = sum(vals)
        print(f"  lam={lam:<5} CVaR95={cv:8.2f}c  totalPnL={tot:8.0f}c "
              f"(${tot/100:7.2f})  contracts={len(vals)}")
        if is_best is None or cv > is_best["cvar"]:
            is_best = {"lam": lam, "cvar": cv, "total": tot, "vals": vals}

    # ---------------------------------------------------------------
    # (2) OUT-OF-SAMPLE (the HONEST headline). Time-split: build per-cell
    #     weights on the FIRST 60% of trades (by eval_time), apply them to
    #     re-size the LAST 40% (scored on its own realized outcomes the
    #     weights never saw). Unseen cells fall to zero weight.
    # ---------------------------------------------------------------
    split = int(0.60 * n_rows)
    train_rows = rows[:split]
    test_rows = rows[split:]
    train_stats = cell_stats(train_rows)
    test_stats = cell_stats(test_rows)
    test_actual_vals = actual_pnl_per_contract(test_rows)
    print(f"\n[OOS] train={len(train_rows)} rows / test={len(test_rows)} rows; "
          f"test actual total={sum(test_actual_vals):.0f}c "
          f"CVaR95={cvar(test_actual_vals, CVAR_ALPHA):.2f}c "
          f"over {len(test_actual_vals)} contracts")
    # Lambda is selected on the TRAIN fold ONLY (re-size train, score train's own
    # CVaR), so test-fold outcomes never touch hyperparameter choice. No peeking.
    lam_best, train_cv_best = None, None
    print("[OOS lambda selection on TRAIN fold] (no test peeking)")
    for lam in LAMBDAS:
        n_map_tr = resize_book(train_rows, train_stats, lam)
        vals_tr = book_pnl_per_contract(train_rows, n_map_tr)
        if not vals_tr:
            continue
        cv_tr = cvar(vals_tr, CVAR_ALPHA)
        if train_cv_best is None or cv_tr > train_cv_best:
            train_cv_best, lam_best = cv_tr, lam
    print(f"  selected lambda={lam_best} (train CVaR95={train_cv_best:.2f}c)")

    # Apply the train-selected lambda to re-size the TEST fold (weights from train).
    n_map = resize_book(test_rows, test_stats, lam_best,
                        stats_for_weights=train_stats)
    oos_vals = book_pnl_per_contract(test_rows, n_map)
    oos_cv = cvar(oos_vals, CVAR_ALPHA)
    oos_tot = sum(oos_vals)
    print(f"\n[HEADLINE = OOS] lambda={lam_best} (train-selected) "
          f"CVaR95={oos_cv:.2f}c totalPnL={oos_tot:.0f}c "
          f"contracts={len(oos_vals)}")
    oos_best = {"lam": lam_best, "cvar": oos_cv, "total": oos_tot, "vals": oos_vals}
    boot = bootstrap_ci(test_actual_vals, oos_best["vals"], N_BOOT, RNG_SEED)
    print("\n[bootstrap CI on OOS headline, n_boot=%d]" % N_BOOT)
    print(f"  CVaR95: actual={boot['cvar_actual']:.2f}c  "
          f"resized={boot['cvar_resized']:.2f}c  "
          f"DELTA={boot['d_cvar']:+.2f}c  CI=[{boot['d_cvar_ci'][0]:+.2f}, "
          f"{boot['d_cvar_ci'][1]:+.2f}]  (>0 = tail improved)")
    print(f"  TotalPnL: actual={boot['total_actual']:.0f}c  "
          f"resized={boot['total_resized']:.0f}c  "
          f"DELTA={boot['d_total']:+.0f}c (${boot['d_total']/100:+.2f})  "
          f"CI=[{boot['d_total_ci'][0]:+.0f}, {boot['d_total_ci'][1]:+.0f}]c")

    out = {
        "n_rows": n_rows, "total_contracts": total_contracts,
        "n_contracts_actual": len(actual_vals),
        "in_sample_best_lambda": is_best["lam"],
        "in_sample_d_cvar": is_best["cvar"] - actual_cvar,
        "oos_n_test_rows": len(test_rows),
        "oos_n_test_contracts": len(test_actual_vals),
        "oos_train_selected_lambda": lam_best,
        "headline_metric": "delta-cvar95-cents-per-contract-OOS",
        "headline_d_cvar": boot["d_cvar"],
        "headline_d_cvar_ci": boot["d_cvar_ci"],
        "headline_d_total_pnl_cents": boot["d_total"],
        "headline_d_total_pnl_ci": boot["d_total_ci"],
        "boot": boot,
    }
    print("\nJSON " + json.dumps(out, default=lambda o: list(o)
                                 if isinstance(o, tuple) else o))


if __name__ == "__main__":
    main()
