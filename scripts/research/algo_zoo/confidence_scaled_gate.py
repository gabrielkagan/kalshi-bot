#!/usr/bin/env python3
"""confidence_scaled_gate  (family: sizing-gate)

Alex's confidence rule, formalized.

Replay the realized 15m settled_trades ledger from state.db under a gate that
only takes a trade when the LOWER confidence bound on true prob beats price:

    q_hat - z * sigma_q  >  p          (z = 1.65, one-sided ~95%)

where
  q_hat   = model calibrated_prob for the trade (the model's true-prob estimate)
  p       = entry_price_cents / 100        (price = market-implied prob of YES)
  sigma_q = per-price-tier uncertainty on q_hat, estimated from REALIZED
            calibration error inside that tier (binomial SE of the realized
            win-rate, which is the empirical sampling noise of the true prob
            in that tier).

Headline: counterfactual total PnL (USD) of gate-survivors vs actual realized,
with a bootstrap CI (>=1000 resamples) over trades. We also report which price
tiers (esp 90-94c and 97-98c) get filtered.

FEES: pnl_cents in settled_trades already nets Kalshi fees (revenue_cents -
cost - fee_cents == pnl_cents up to per-fill integer rounding). So the
counterfactual is fee-inclusive by construction: a surviving trade keeps its
realized fee-netted pnl_cents; a filtered trade contributes 0. No maker-fill
simulation is needed -- this is a ledger replay, not a book-fill backtest.

LOOK-AHEAD: sigma_q is estimated from the SAME realized outcomes that produce
pnl. This is in-sample by construction (the gate "knows" each tier's realized
calibration). We report it honestly and also run a leave-tier-out sanity check.
This corpus is the FULL settled ledger (Feb 24 -> May 31), not just the 1-day
bronze window, because the in-window realized-trade sample is only n=14 and the
spec is explicitly a settled_trades replay.
"""
import sys
import sqlite3
import math

sys.path.insert(0, "/Users/gabrielkagan/Documents/kalshi-bot")

import numpy as np

DB = "/tmp/edge_daily/state.db"
Z = 1.65
TIER_W = 5  # cents per tier bucket
N_BOOT = 5000
RNG = np.random.default_rng(20260531)


def load_trades():
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    rows = con.execute(
        """
        SELECT ticker, asset, side, count, entry_price_cents,
               calibrated_prob, edge, market_result, pnl_cents
        FROM settled_trades
        WHERE product_type='15m'
          AND calibrated_prob IS NOT NULL
          AND edge IS NOT NULL
          AND entry_price_cents IS NOT NULL
          AND count IS NOT NULL
          AND pnl_cents IS NOT NULL
          AND market_result IN ('yes','no')
        """
    ).fetchall()
    con.close()
    return [dict(r) for r in rows]


def tier_of(price_cents):
    return (int(price_cents) // TIER_W) * TIER_W


def main():
    trades = load_trades()
    n = len(trades)
    if n == 0:
        print("DATA_GAP: no usable 15m settled_trades with calibrated_prob+edge+pnl")
        return

    # All side='yes' in this corpus; the "win" of a YES trade is market_result=='yes'.
    # Generalize anyway: a trade wins when side == market_result.
    for t in trades:
        t["tier"] = tier_of(t["entry_price_cents"])
        t["p"] = t["entry_price_cents"] / 100.0
        t["win"] = 1 if t["side"] == t["market_result"] else 0
        t["pnl_usd"] = t["pnl_cents"] / 100.0

    # ---- Per-tier sigma_q from realized calibration error -------------------
    # sigma_q = binomial SE of the realized win-rate within the tier:
    #   SE = sqrt( w*(1-w) / N_trades_in_tier )
    # This is the empirical sampling noise on the tier's true-prob estimate.
    tiers = sorted({t["tier"] for t in trades})
    tier_stats = {}
    for tr in tiers:
        sub = [t for t in trades if t["tier"] == tr]
        m = len(sub)
        w = float(np.mean([s["win"] for s in sub]))
        # binomial SE; guard m>=2 and w in (0,1). Floor SE so a tier with
        # w==1.0 (SE==0) still carries irreducible small-sample uncertainty
        # via the Wilson-style add: use sqrt(w(1-w)/m) with a 1/(2m) floor.
        if m >= 2:
            se = math.sqrt(max(w * (1 - w), 0.0) / m)
            se = max(se, 1.0 / (2.0 * m))  # never claim zero uncertainty
        else:
            se = 0.25  # singleton tier: maximally uncertain
        tier_stats[tr] = {"m": m, "win_rate": w, "sigma_q": se}

    # ---- Apply the gate ------------------------------------------------------
    for t in trades:
        sq = tier_stats[t["tier"]]["sigma_q"]
        t["sigma_q"] = sq
        lcb = t["calibrated_prob"] - Z * sq
        t["lcb"] = lcb
        t["take"] = 1 if lcb > t["p"] else 0

    actual_pnl = sum(t["pnl_usd"] for t in trades)
    gated_pnl = sum(t["pnl_usd"] for t in trades if t["take"])
    n_take = sum(t["take"] for t in trades)
    delta = gated_pnl - actual_pnl  # headline: improvement from gating

    # ---- Bootstrap CI on the headline (delta = gated - actual) --------------
    # Resample trades (with replacement); recompute both totals on each draw.
    pnl_arr = np.array([t["pnl_usd"] for t in trades])
    take_arr = np.array([t["take"] for t in trades])
    idx = np.arange(n)
    boot_delta = np.empty(N_BOOT)
    boot_gated = np.empty(N_BOOT)
    for b in range(N_BOOT):
        s = RNG.choice(idx, size=n, replace=True)
        a = pnl_arr[s].sum()
        g = pnl_arr[s][take_arr[s] == 1].sum()
        boot_delta[b] = g - a
        boot_gated[b] = g
    ci_lo, ci_hi = np.percentile(boot_delta, [2.5, 97.5])
    gci_lo, gci_hi = np.percentile(boot_gated, [2.5, 97.5])

    # ---- Per-tier filtering report ------------------------------------------
    print("=" * 78)
    print(f"n_trades={n}  contracts={sum(t['count'] for t in trades)}")
    print(f"actual realized PnL  = ${actual_pnl:,.2f}  (fees included)")
    print(f"gated-survivor  PnL  = ${gated_pnl:,.2f}  (n_take={n_take}, "
          f"{100*n_take/n:.1f}% of trades survive)")
    print(f"DELTA (gated-actual) = ${delta:,.2f}  "
          f"95% boot CI [${ci_lo:,.2f}, ${ci_hi:,.2f}]")
    print(f"gated PnL 95% CI     = [${gci_lo:,.2f}, ${gci_hi:,.2f}]")
    print("=" * 78)
    print(f"{'tier':>5} {'N':>5} {'win%':>6} {'sigma_q':>8} {'avg_cprob':>9} "
          f"{'avg_p':>6} {'kept':>5} {'pnl_kept':>10} {'pnl_cut':>10}")
    for tr in tiers:
        sub = [t for t in trades if t["tier"] == tr]
        kept = [t for t in sub if t["take"]]
        cut = [t for t in sub if not t["take"]]
        print(f"{tr:>5} {len(sub):>5} {100*tier_stats[tr]['win_rate']:>5.1f} "
              f"{tier_stats[tr]['sigma_q']:>8.4f} "
              f"{np.mean([s['calibrated_prob'] for s in sub]):>9.3f} "
              f"{np.mean([s['p'] for s in sub]):>6.3f} "
              f"{len(kept):>5} ${sum(t['pnl_usd'] for t in kept):>8.2f} "
              f"${sum(t['pnl_usd'] for t in cut):>8.2f}")

    # Highlight the asked-about tiers
    print("-" * 78)
    for lo, hi, label in [(90, 94, "90-94c"), (97, 98, "97-98c"), (95, 99, "95-99c")]:
        sub = [t for t in trades if lo <= t["entry_price_cents"] <= hi]
        if not sub:
            continue
        kept = sum(t["take"] for t in sub)
        pnl_all = sum(t["pnl_usd"] for t in sub)
        pnl_kept = sum(t["pnl_usd"] for t in sub if t["take"])
        print(f"{label}: n={len(sub)} kept={kept} ({100*kept/len(sub):.0f}%) "
              f"pnl_all=${pnl_all:,.2f} pnl_kept=${pnl_kept:,.2f} "
              f"pnl_filtered=${pnl_all-pnl_kept:,.2f}")

    # ---- Leave-tier-out sanity (reduce in-sample leakage) -------------------
    # Re-estimate each tier's sigma_q WITHOUT using that trade's own row is hard
    # at row level; instead, a coarse check: does the gate still help if we use
    # a single global sigma_q (no per-tier info)? If the win comes only from the
    # most-leaky tier estimate, this collapses.
    global_w = float(np.mean([t["win"] for t in trades]))
    global_se = math.sqrt(global_w * (1 - global_w) / n)
    g_take = [(t["calibrated_prob"] - Z * global_se) > t["p"] for t in trades]
    g_pnl = sum(t["pnl_usd"] for t, k in zip(trades, g_take) if k)
    print("-" * 78)
    print(f"[sanity] global-sigma gate: sigma={global_se:.4f} "
          f"n_take={sum(g_take)} pnl=${g_pnl:,.2f} "
          f"delta=${g_pnl-actual_pnl:,.2f}")

    # machine-readable summary line for the harness
    print("RESULT_JSON " + repr({
        "n": n, "actual_pnl": actual_pnl, "gated_pnl": gated_pnl,
        "delta": delta, "ci_lo": ci_lo, "ci_hi": ci_hi,
        "n_take": n_take, "pct_survive": 100 * n_take / n,
    }))


if __name__ == "__main__":
    main()
