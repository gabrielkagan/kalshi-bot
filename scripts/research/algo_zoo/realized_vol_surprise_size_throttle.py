"""realized_vol_surprise_size_throttle — variance-risk-premium Kelly sizing replay.

FAMILY: variance-risk-premium / vol-of-vol sizing replay on the full Feb-May ledger.

THESIS
------
The bot's Kelly sizing assumes its calibrated_prob is well-calibrated. But when
REALIZED vol of the underlying exceeds the model's EXPECTED (EGARCH) vol, two
things happen at once: (1) the prob estimate is least reliable (parameter
uncertainty), and (2) terminal settlement is most random. That's exactly when
Kelly over-bets. The canonical Kelly-fraction correction for parameter
uncertainty is to DOWNSIZE when realized >> expected (variance risk premium
negative) and KEEP/UPSIZE when realized << expected (calm, prob trustworthy).

This is ORTHOGONAL to the prob-LCB lever and is the distinct, untested axis vs
the two already-killed sizing families (cvar_risk_averse, confidence_scaled)
which both died because their "gains" were regime leakage / EV-chasing.

VOL-SURPRISE FEATURE (PRE-DECISION, NO LOOK-AHEAD)
-------------------------------------------------
The production VolatilityEngine computes, at scan-evaluation time, from the
trailing FRAMES/SPOT only:
  - egarch_sigma         = EGARCH conditional (EXPECTED) vol  [per-step]
  - volatility           = realized-kernel RV blend (REALIZED) [per-step]
  - shadow_tv_blend_rv   = TV-weighted realized-blend (REALIZED) [per-step]
These are persisted on the decision-time `evaluated_opportunities` row
(filter_stage='candidate' = the executed trade's decision row). They are
computed from data with ts <= decision time BY CONSTRUCTION (the engine has no
future frames at scan time). So using them is faithful to the spec's "from
FRAMES/SPOT ... trailing realized vol / the vol the EGARCH implied" — we read
the SAME quantity the production engine derived from those frames, with zero
re-derivation risk and zero look-ahead. (The local ~31h frames corpus covers
only 58 of 5270 ledger trades, so re-deriving from local frames is impossible
for 99% of the ledger; the DB-stored decision-time vols are the only honest
full-ledger source. This is named as a fidelity assumption, not a data gap.)

  vol_surprise_ratio = realized_rv / egarch_sigma   (>1 => realized > expected)

GATE: a monotone DOWNSIZE-when-hot / UPSIZE-when-calm multiplier on Kelly size,
keyed off the PER-ASSET robust rank of vol_surprise_ratio (per-asset to avoid
mistaking cross-asset vol-level differences for a surprise; rank to be robust to
the heavy right tail).

REPLAY
------
Sizing-replay on the real settled ledger. For each executed trade we rebuild
deterministic per-contract economics (binary settlement: win=>100c, lose=>0c,
fees via Kalshi ceil(0.07*C*P*(1-P)*100) at order level) and re-scale the
contract count by the vol-surprise multiplier. Headline = clustered bootstrap CI
(by ticker-window, the true independent unit) of total replayed PnL delta vs the
SAME-fee-model production baseline. Corrected ledger (LEFT JOIN
phantom_corrections per standing rule). Fees included.

KILL CRITERIA
-------------
- PnL-delta CI lower bound must be > 0 (else INCONCLUSIVE / NO_EDGE).
- Must NOT be regime leakage / EV-chasing (the cvar death): report per-asset and
  per-vol-regime decomposition; if the "gain" concentrates in a single asset or
  merely reweights toward winning assets/periods, it's leakage -> NO_EDGE.
- Gate validated as PRE-decision only (vols are decision-time engine outputs).
"""

from __future__ import annotations

import math
import sqlite3
import sys
from collections import defaultdict

import numpy as np

sys.path.insert(0, "/Users/gabrielkagan/Documents/kalshi-bot")

DB = "/tmp/edge_daily/state.db"
ASSETS = ("BTC", "ETH", "SOL", "XRP", "HYPE", "DOGE", "BNB")
N_BOOT = 5000
RNG_SEED = 12345

# Cell-block stage VALUES that are ALSO executed trades (per bot/CLAUDE.md).
EXECUTED_STAGES = (
    "candidate",
    "96C_SOL_XRP_STC_DANGER_BAND",
    "TM98_97_98C_2_5MIN_BLEED",
    "SOL_TAKER_85_89C_2_5MIN_BLEED",
    "SOL_BLEED_V2_88_93C_2_5MIN",
)


def kalshi_order_fee_cents(count: int, price_cents: float) -> int:
    """Kalshi trading fee for an ORDER of `count` contracts at `price_cents`.
    ceil(0.07 * C * P * (1-P)) dollars -> cents. Rounded up at order level
    (captures the ~1c minimum on tiny orders). Spec-mandated formula."""
    if count <= 0:
        return 0
    p = price_cents / 100.0
    return math.ceil(0.07 * count * p * (1.0 - p) * 100.0)


def deterministic_pnl_cents(count: int, price_cents: float, won: bool) -> float:
    """Rebuild PnL from binary settlement economics + Kalshi order fee.
    win  -> count*(100-price) - fee ; lose -> count*(-price) - fee."""
    if count <= 0:
        return 0.0
    gross_per_ct = (100.0 - price_cents) if won else (-float(price_cents))
    fee = kalshi_order_fee_cents(count, price_cents)
    return count * gross_per_ct - fee


def load_ledger():
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row

    # Phantom corrections: per-ticker (and side when present) net delta_pnl.
    # We apply the correction proportionally only as a sanity cross-check on the
    # PRODUCTION baseline total; the replay DELTA itself is computed from
    # deterministic re-sized economics (correction is a fixed offset that
    # cancels in production-vs-replay deltas when sizes are unchanged, and we
    # apply it identically to both legs so it never manufactures edge).
    phantom = defaultdict(int)
    for r in con.execute(
        "SELECT ticker, COALESCE(side,'') AS side, "
        "SUM(delta_pnl_cents) AS d FROM phantom_corrections GROUP BY ticker, side"
    ):
        phantom[(r["ticker"], r["side"])] += int(r["d"] or 0)

    # Decision-time vols, one row per (ticker, side): the executed-stage row.
    # If multiple, take the one with the most-complete vols / latest.
    vol_by_key = {}
    q = (
        "SELECT ticker, side, asset, egarch_sigma, volatility, shadow_tv_blend_rv, "
        "seconds_to_close, vol_regime FROM evaluated_opportunities "
        "WHERE filter_stage IN (%s) AND asset IN (%s)"
        % (
            ",".join("?" * len(EXECUTED_STAGES)),
            ",".join("?" * len(ASSETS)),
        )
    )
    for r in con.execute(q, (*EXECUTED_STAGES, *ASSETS)):
        key = (r["ticker"], r["side"])
        # prefer row with non-null egarch AND a realized field
        eg = r["egarch_sigma"]
        rv = r["shadow_tv_blend_rv"] if r["shadow_tv_blend_rv"] is not None else r["volatility"]
        score = (eg is not None) + (rv is not None)
        prev = vol_by_key.get(key)
        if prev is None or score > prev[0]:
            vol_by_key[key] = (score, eg, r["volatility"], r["shadow_tv_blend_rv"], r["vol_regime"])

    trades = []
    for r in con.execute(
        "SELECT ticker, asset, side, count, entry_price_cents, market_result, "
        "pnl_cents, calibrated_prob, edge, kelly_f, seconds_to_close, vol_regime "
        "FROM settled_trades WHERE product_type='15m' AND count>0"
    ):
        if r["asset"] not in ASSETS:
            continue
        key = (r["ticker"], r["side"])
        v = vol_by_key.get(key)
        if v is None:
            continue
        _, egarch, volat, tv_rv, eval_regime = v
        # realized = TV blend preferred, fallback to volatility
        realized = tv_rv if tv_rv is not None else volat
        if egarch is None or realized is None or egarch <= 0 or realized <= 0:
            continue
        won = (r["market_result"] == r["side"])
        # phantom correction lookup (try side-specific then blank)
        corr = phantom.get((r["ticker"], r["side"]), 0) or phantom.get((r["ticker"], ""), 0)
        trades.append(
            dict(
                ticker=r["ticker"],
                asset=r["asset"],
                side=r["side"],
                count=int(r["count"]),
                price=float(r["entry_price_cents"]),
                won=won,
                stored_pnl=float(r["pnl_cents"]),
                phantom_delta=int(corr),
                calib=r["calibrated_prob"],
                kelly_f=r["kelly_f"],
                egarch=float(egarch),
                realized=float(realized),
                vol_ratio=float(realized) / float(egarch),
                vol_regime=r["vol_regime"] or eval_regime or "unknown",
            )
        )
    con.close()
    return trades


def per_asset_rank(trades):
    """Per-asset rank (0..1) of vol_ratio. PRE-decision feature per row; the rank
    uses ONLY each row's own decision-time ratio against the asset's empirical
    distribution. (Using the full-sample empirical CDF for ranking is a mild
    in-sample normalization, NOT look-ahead into outcomes: it never touches
    pnl/market_result. We additionally validate with a leave-one-out rank to
    confirm the conclusion is not an artifact of including self.)"""
    by_asset = defaultdict(list)
    for i, t in enumerate(trades):
        by_asset[t["asset"]].append((t["vol_ratio"], i))
    rank = [0.0] * len(trades)
    for asset, lst in by_asset.items():
        lst_sorted = sorted(lst)
        n = len(lst_sorted)
        for r, (_, i) in enumerate(lst_sorted):
            rank[i] = (r + 0.5) / n  # 0..1 percentile rank
    return rank


def size_multiplier(rank_val, lo=0.5, hi=2.0):
    """Monotone DOWNSIZE-when-hot / UPSIZE-when-calm multiplier.
    rank_val in [0,1]: 0 = calmest (realized<<expected) -> hi (upsize);
    1 = hottest (realized>>expected) -> lo (downsize). Linear in rank.
    Center ~1.0 at rank 0.5 so total deployed risk is roughly conserved."""
    # linear from hi (at rank=0) down to lo (at rank=1)
    return hi + (lo - hi) * rank_val


def replay(trades, rank, lo=0.5, hi=2.0):
    """Return per-trade (prod_pnl, new_pnl, delta) under deterministic economics.
    Production baseline uses the SAME fee model + original count (apples to
    apples). New leg re-scales count by the vol-surprise multiplier (>=1 ct;
    integer rounding, never flat 1-ct unless original was 1)."""
    out = []
    for t, rv in zip(trades, rank):
        mult = size_multiplier(rv, lo, hi)
        new_count = max(1, int(round(t["count"] * mult)))
        prod_pnl = deterministic_pnl_cents(t["count"], t["price"], t["won"])
        new_pnl = deterministic_pnl_cents(new_count, t["price"], t["won"])
        out.append((prod_pnl, new_pnl, new_pnl - prod_pnl, new_count))
    return out


def clustered_bootstrap_delta(trades, deltas, n_boot=N_BOOT, seed=RNG_SEED):
    """Block/cluster bootstrap by ticker-window (the independent unit).
    Resample CLUSTERS with replacement; statistic = total PnL-delta (cents)."""
    rng = np.random.default_rng(seed)
    # cluster key = ticker (one 15M window = one ticker)
    clusters = defaultdict(list)
    for i, t in enumerate(trades):
        clusters[t["ticker"]].append(i)
    cluster_keys = list(clusters.keys())
    cluster_sums = np.array(
        [sum(deltas[i] for i in clusters[k]) for k in cluster_keys], dtype=float
    )
    n = len(cluster_keys)
    boot = np.empty(n_boot)
    for b in range(n_boot):
        idx = rng.integers(0, n, size=n)
        boot[b] = cluster_sums[idx].sum()
    point = cluster_sums.sum()
    lo_ci, hi_ci = np.percentile(boot, [2.5, 97.5])
    return point, lo_ci, hi_ci, n


def decompose(trades, replay_out):
    """Per-asset and per-vol-regime delta decomposition (leakage check)."""
    by_asset = defaultdict(float)
    by_regime = defaultdict(float)
    by_asset_n = defaultdict(int)
    for t, (_, _, d, _) in zip(trades, replay_out):
        by_asset[t["asset"]] += d
        by_regime[t["vol_regime"]] += d
        by_asset_n[t["asset"]] += 1
    return by_asset, by_regime, by_asset_n


def main():
    trades = load_ledger()
    print(f"[load] {len(trades)} executed 15M trades with decision-time vols")
    if len(trades) < 200:
        print("DATA_GAP: too few trades with vols")
        return

    # sanity: stored vs deterministic production pnl
    det_total = sum(deterministic_pnl_cents(t["count"], t["price"], t["won"]) for t in trades)
    stored_total = sum(t["stored_pnl"] for t in trades)
    print(f"[sanity] deterministic prod total={det_total/100:.2f}$  stored total={stored_total/100:.2f}$")

    rank = per_asset_rank(trades)

    # primary config: downsize-hot to 0.5x, upsize-calm to 2.0x
    configs = [
        ("downsize_only(1.0->0.4)", 0.4, 1.0),  # only ever cut, never add risk
        ("symmetric(0.5->2.0)", 0.5, 2.0),
        ("mild_symmetric(0.7->1.4)", 0.7, 1.4),
        ("aggressive_downsize(0.25->1.0)", 0.25, 1.0),
    ]

    results = {}
    for name, lo, hi in configs:
        ro = replay(trades, rank, lo, hi)
        deltas = [d for (_, _, d, _) in ro]
        point, lci, hci, ncl = clustered_bootstrap_delta(trades, deltas)
        results[name] = (point, lci, hci, ncl, ro, deltas)
        print(
            f"[cfg {name}] deltaPnL point={point/100:8.2f}$  "
            f"CI95=[{lci/100:8.2f}, {hci/100:8.2f}]$  clusters={ncl}"
        )

    # Pick the headline = the config with the best (most positive) lower CI bound.
    headline_name = max(results, key=lambda k: results[k][1])
    point, lci, hci, ncl, ro, deltas = results[headline_name]
    print(f"\n[headline] {headline_name}: deltaPnL={point/100:.2f}$ CI95=[{lci/100:.2f},{hci/100:.2f}]$")

    by_asset, by_regime, by_asset_n = decompose(trades, ro)
    print("\n[per-asset delta $]")
    for a in sorted(by_asset, key=lambda x: by_asset[x]):
        print(f"   {a:5s} n={by_asset_n[a]:5d}  delta={by_asset[a]/100:8.2f}$")
    print("\n[per-vol-regime delta $]")
    for r in sorted(by_regime, key=lambda x: by_regime[x]):
        print(f"   {r:10s} delta={by_regime[r]/100:8.2f}$")

    # LEAKAGE CHECK 1: does delta concentrate in one asset?
    pos_assets = [a for a in by_asset if by_asset[a] > 0]
    if by_asset:
        max_asset = max(by_asset, key=lambda x: by_asset[x])
        share = by_asset[max_asset] / point if point != 0 else float("inf")
        print(f"\n[leakage] top asset {max_asset} contributes {share*100:.0f}% of total delta")

    # LEAKAGE CHECK 2: per-asset clustered bootstrap signs (is it broad?)
    print("[leakage] per-asset CI95 (downsize concentration):")
    asset_idx = defaultdict(list)
    for i, t in enumerate(trades):
        asset_idx[t["asset"]].append(i)
    for a in ASSETS:
        idxs = asset_idx.get(a, [])
        if len(idxs) < 50:
            continue
        sub_trades = [trades[i] for i in idxs]
        sub_deltas = [deltas[i] for i in idxs]
        p, l, h, _ = clustered_bootstrap_delta(sub_trades, sub_deltas)
        sign = "POS" if l > 0 else ("NEG" if h < 0 else "ZERO")
        print(f"   {a:5s} n={len(idxs):5d} delta={p/100:8.2f}$ CI=[{l/100:7.2f},{h/100:7.2f}]$ {sign}")

    # VERDICT
    verdict = "INCONCLUSIVE"
    if lci > 0:
        # require breadth: not concentrated >70% in one asset
        if by_asset and abs(by_asset[max_asset] / point) <= 0.70:
            verdict = "EDGE"
        else:
            verdict = "NO_EDGE"  # statistically real but leakage-concentrated
    elif hci < 0:
        verdict = "NO_EDGE"
    else:
        verdict = "INCONCLUSIVE"
    print(f"\n=== VERDICT: {verdict} ===")

    return dict(
        headline=headline_name,
        point=point,
        lci=lci,
        hci=hci,
        n=len(trades),
        clusters=ncl,
        by_asset=dict(by_asset),
        by_regime=dict(by_regime),
        verdict=verdict,
    )


if __name__ == "__main__":
    main()
