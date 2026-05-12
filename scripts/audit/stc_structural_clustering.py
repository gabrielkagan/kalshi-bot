"""STC structural clustering audit — spike 86b9wxr6p (2026-05-12).

Parent: 86b9wwqn9 (admit-SHAP) + outcome-SHAP follow-up. Outcome-SHAP
Model B surfaced `seconds_to_close` mean |SHAP| 5.96 — the dominant
non-gate W/L predictor. This audit answers the actionable follow-up:

> Are there specific (asset × STC band) cells where counterfactual
> Kelly-sized PnL is materially negative AND not already covered by
> an existing cell-block stage?

Methodology (read-only, runs on Mac against a state.db snapshot):

1. Corpus
   - 15M crypto only (BTC/ETH/SOL/XRP) — cal_mlp doesn't gate other surfaces.
   - filter_stage = 'candidate' ONLY. Cell-block stages excluded because
     they reflect what the gate ALREADY catches — the audit's job is to
     find UNCOVERED bands; mixing in already-blocked rows is circular.
   - market_result IN ('yes','no'). NULL / 'push' / 'all_*' dropped.

2. STC band partition
   - 11 bins: 0-60, 60-120, ..., 540-600, plus a 600+ tail. Pinned to
     60s increments so the breakpoints align with the existing
     cell-block STC bounds (121-300s) — qualifying combos either fall
     IN existing bands or OUTSIDE them.

3. Wilson95 CI
   - Used as gate-trigger metric (upper bound). Inline implementation
     mirrors `scripts/audit/wilson_ci.py` pattern; uses the same z=1.96
     for alpha=0.05.

4. Cell-block coverage
   - Per (asset × STC band), classify whether existing cell-blocks
     cover the empirical price + strategy mix. Delegates to the 4
     canonical predicates in `bot/helpers/cell_blocks.py`.

5. Counterfactual Kelly PnL
   - `counterfactual_pnl` is already Kelly-sized at decision time (the
     bot's actual sizer wrote `position_size` then `counterfactual_pnl`
     at settlement). NEVER flat 1-contract. Honest-NULL derivation
     pattern: if DB value is NULL but `position_size + market_price +
     market_result` are populated, derive (yes_win = pos × (100-mp),
     no_loss = -pos × mp).

6. Gate-trigger criteria
   - (a) n_admit ≥ 50, (b) Wilson95 upper < 0.92 (below the 95.3%
     global WR floor), (c) NOT covered by any existing cell-block,
     (d) abs(cf_pnl_30d) > $50.

Time-window homogeneity caveat: SOL_BLEED_V2 shipped 2026-05-10
(b07a028). For pre/post-May-10 stratification the operator must
re-run with `--since 2026-05-10` and report the delta — out of scope
for the headline pass.

Usage:
    python3 scripts/audit/stc_structural_clustering.py \\
        --db /tmp/stc_clustering/corpus.db \\
        --out kb/findings/stc-structural-clustering-may12/
"""
from __future__ import annotations

import argparse
import csv
import math
import sqlite3
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT))

from bot.helpers import cell_blocks  # noqa: E402  (path insert above)
from bot.constants import (  # noqa: E402
    HIGH_PRICE_STC_BLOCK_BLEEDER_STRATEGIES,
    TM98_HIGHPRICE_BLEED_BLOCK_STRATEGIES,
    SOL_TAKER_LOWPRICE_BLEED_BLOCK_STRATEGIES,
    SOL_BLEED_V2_BLOCK_STRATEGIES,
)


# ── Universe constants ───────────────────────────────────────────────────────

ASSETS = ("BTC", "ETH", "SOL", "XRP")
PRODUCT_TYPE = "15m"
CANDIDATE_FILTER_STAGE = "candidate"

# STC band edges in seconds. 11 bins (0-60, 60-120, ..., 540-600) + 600+ tail.
STC_BAND_EDGES: Tuple[int, ...] = (0, 60, 120, 180, 240, 300, 360, 420, 480, 540, 600)
STC_BAND_TAIL = "600+"

# Settlement-time leakage cols (mirror outcome-SHAP spike).
# counterfactual_pnl is EXCLUDED from this list — it IS the target quantity
# the audit aggregates; dropping it would be self-defeating. Other settlement
# cols are not needed for band-partitioning and dropped defensively.
SETTLEMENT_LEAKAGE_COLS: Tuple[str, ...] = (
    "final_spot_price",
    "knockout_time_relative",
    "max_excursion_from_strike",
    "time_above_strike_seconds",
    "time_below_strike_seconds",
    "settled_time",
    "order_outcome",
    "minutes_above_strike",
)


# ── Cell-block predicate references (no inline reimplementation) ─────────────

HIGH_PRICE_STC_BLOCK = cell_blocks.should_block_high_price_stc_candidate
TM98_HIGHPRICE_BLEED_BLOCK = cell_blocks.should_block_tm98_highprice_bleed_candidate
SOL_TAKER_LOWPRICE_BLEED_BLOCK = cell_blocks.should_block_sol_taker_lowprice_bleed_candidate
SOL_BLEED_V2_BLOCK = cell_blocks.should_block_sol_bleed_v2_candidate

# Mapping for stage-name labels. Each gate's predicate has its own env
# enabled-default; we force enabled=True so the classifier asks "would
# this row be gated if the operator flipped the flag" not "is it gated
# right now". That's the audit-relevant question.
_CELL_BLOCK_PREDICATES: Tuple[Tuple[str, Callable[..., bool]], ...] = (
    ("HIGH_PRICE_STC_BLOCK", HIGH_PRICE_STC_BLOCK),
    ("TM98_HIGHPRICE_BLEED_BLOCK", TM98_HIGHPRICE_BLEED_BLOCK),
    ("SOL_TAKER_LOWPRICE_BLEED_BLOCK", SOL_TAKER_LOWPRICE_BLEED_BLOCK),
    ("SOL_BLEED_V2", SOL_BLEED_V2_BLOCK),
)


# ── STC band partitioning ────────────────────────────────────────────────────


def assign_stc_band(stc: Optional[float]) -> Optional[str]:
    """Return STC band label for a value, or None if input is None.

    Inclusive lower / exclusive upper: 0.0 → '0-60', 60.0 → '60-120'.
    """
    if stc is None:
        return None
    for i in range(len(STC_BAND_EDGES) - 1):
        lo = STC_BAND_EDGES[i]
        hi = STC_BAND_EDGES[i + 1]
        if lo <= stc < hi:
            return f"{lo}-{hi}"
    return STC_BAND_TAIL


def _band_sort_key(band: str) -> float:
    if band == STC_BAND_TAIL:
        return float(STC_BAND_EDGES[-1])
    return float(band.split("-")[0])


# ── Wilson CI ────────────────────────────────────────────────────────────────


def wilson_ci(k: int, n: int, alpha: float = 0.05) -> Tuple[float, float]:
    """Wilson score CI for binomial proportion k/n.

    Returns (lower, upper). Edge cases:
      n=0 → (0.0, 1.0) — max uncertainty, no data
      k=0 → lower = 0.0
      k=n → upper = 1.0
    """
    if n <= 0:
        return (0.0, 1.0)
    if alpha == 0.05:
        z = 1.959963984540054  # two-sided 95%
    else:
        # rare; compute via stdlib inverse
        from statistics import NormalDist
        z = NormalDist().inv_cdf(1.0 - alpha / 2.0)
    phat = k / n
    denom = 1.0 + z * z / n
    center = phat + z * z / (2.0 * n)
    delta = z * math.sqrt(phat * (1.0 - phat) / n + z * z / (4.0 * n * n))
    lo = max(0.0, (center - delta) / denom)
    hi = min(1.0, (center + delta) / denom)
    return (lo, hi)


# ── Counterfactual PnL ───────────────────────────────────────────────────────


def row_counterfactual_cents(row: Dict[str, Any]) -> Optional[int]:
    """Return the row's counterfactual Kelly PnL in cents.

    Prefers the pre-populated `counterfactual_pnl` DB value (already
    Kelly-sized at decision time). Falls back to derivation from
    position_size + market_price + market_result. Honest-NULL passthrough
    on any missing input.
    """
    cf = row.get("counterfactual_pnl")
    if cf is not None:
        try:
            return int(cf)
        except (TypeError, ValueError):
            return None
    pos = row.get("position_size")
    mp = row.get("market_price")
    mr = row.get("market_result")
    if pos is None or mp is None or mr not in ("yes", "no"):
        return None
    try:
        pos_i = int(pos)
        mp_i = int(mp)
    except (TypeError, ValueError):
        return None
    if mr == "yes":
        return pos_i * (100 - mp_i)
    return -pos_i * mp_i


# ── Cell-block coverage classifier ───────────────────────────────────────────


def classify_cell_block_coverage(
    asset: Optional[str],
    side: Optional[str],
    entry_price_cents: Optional[int],
    seconds_to_close: Optional[float],
    strategy: Optional[str],
) -> Optional[str]:
    """For a single row, return the FIRST cell-block stage name whose
    predicate evaluates True (enabled=True), or None if no gate covers.

    Force `enabled=True` so the answer is "WOULD this row be gated IF
    the operator flipped the flag" — orthogonal to the operator's current
    env settings. The audit recommends new gates, so existing operator
    state is irrelevant to coverage classification.
    """
    for name, pred in _CELL_BLOCK_PREDICATES:
        try:
            if pred(asset=asset, side=side, entry_price_cents=entry_price_cents,
                    seconds_to_close=seconds_to_close, strategy=strategy,
                    enabled=True):
                return name
        except TypeError:
            continue
    return None


def _strategy_in_cell_block_bleeders(name: str, strategy: Optional[str]) -> bool:
    """For majority-strategy classification: would the named gate fire
    for this strategy if everything else matched?"""
    if strategy is None:
        return False
    if name == "HIGH_PRICE_STC_BLOCK":
        return strategy in HIGH_PRICE_STC_BLOCK_BLEEDER_STRATEGIES
    if name == "TM98_HIGHPRICE_BLEED_BLOCK":
        return strategy in TM98_HIGHPRICE_BLEED_BLOCK_STRATEGIES
    if name == "SOL_TAKER_LOWPRICE_BLEED_BLOCK":
        return strategy in SOL_TAKER_LOWPRICE_BLEED_BLOCK_STRATEGIES
    if name == "SOL_BLEED_V2":
        return strategy in SOL_BLEED_V2_BLOCK_STRATEGIES
    return False


def classify_band_cell_block_coverage(
    band_rows: Iterable[Dict[str, Any]],
) -> Optional[str]:
    """For a band of admit rows, classify coverage:

      - return a cell-block stage name if the MAJORITY strategy in the
        band is in that gate's bleeder strategies AND a representative row
        is covered by the gate's full predicate
      - return 'partial' if SOME rows are covered but the majority strategy
        isn't — i.e., the gate catches a minority slice
      - return None if no rows are covered

    The 'partial' label lets the recommendation memo call out bands that
    a gate clips but doesn't dominate, rather than silently mislabeling
    them.
    """
    rows = list(band_rows)
    if not rows:
        return None

    # Per-row coverage tally (None if uncovered).
    per_row_cov: List[Optional[str]] = []
    for r in rows:
        per_row_cov.append(classify_cell_block_coverage(
            asset=r.get("asset"), side=r.get("side") or "yes",
            entry_price_cents=r.get("market_price"),
            seconds_to_close=r.get("seconds_to_close"),
            strategy=r.get("strategy")))

    covered_count = sum(1 for c in per_row_cov if c is not None)
    if covered_count == 0:
        return None

    # Majority strategy in band.
    strat_counter = Counter(r.get("strategy") for r in rows)
    majority_strat, majority_n = strat_counter.most_common(1)[0]
    majority_frac = majority_n / len(rows)

    # Find which gate(s) catch the majority strategy.
    if majority_frac >= 0.5:
        for name, _pred in _CELL_BLOCK_PREDICATES:
            if not _strategy_in_cell_block_bleeders(name, majority_strat):
                continue
            # Check at least one row with this majority strat is covered by this gate.
            for r, cov in zip(rows, per_row_cov):
                if r.get("strategy") == majority_strat and cov == name:
                    return name
    return "partial"


# ── Gate-trigger criteria ────────────────────────────────────────────────────


GATE_TRIGGER_MIN_N = 50
GATE_TRIGGER_WILSON_HI_BELOW = 0.92
GATE_TRIGGER_MIN_ABS_CF_30D_CENTS = 50_00  # $50


def qualifies_for_gate_trigger(
    n_admit: int,
    wilson_hi: float,
    cf_pnl_30d_cents: float,
    covered_by: Optional[str],
) -> bool:
    """Apply the 4-criterion gate-trigger filter from the spike brief."""
    if n_admit < GATE_TRIGGER_MIN_N:
        return False
    if wilson_hi >= GATE_TRIGGER_WILSON_HI_BELOW:
        return False
    if covered_by is not None:
        return False
    if abs(cf_pnl_30d_cents) <= GATE_TRIGGER_MIN_ABS_CF_30D_CENTS:
        return False
    return True


# ── Loader ───────────────────────────────────────────────────────────────────


def _table_columns(conn: sqlite3.Connection, table: str) -> List[str]:
    return [r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]


def load_admit_corpus(db_path: Path):
    """Return a DataFrame of `candidate` filter_stage admits with
    market_result IN ('yes','no'). Settlement-leakage cols dropped
    (counterfactual_pnl preserved — it's the target quantity).
    """
    import pandas as pd

    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        avail = set(_table_columns(conn, "evaluated_opportunities"))
        want = [
            "ticker", "asset", "filter_stage", "evaluation_time", "market_result",
            "market_price", "seconds_to_close", "position_size", "kelly_f",
            "strategy", "side", "counterfactual_pnl", "calibrated_prob",
            "raw_prob", "product_type",
        ]
        cols = [c for c in want if c in avail]
        sql = f"""
            SELECT {', '.join(cols)}
            FROM evaluated_opportunities
            WHERE asset IN ({','.join(['?'] * len(ASSETS))})
              AND product_type = ?
              AND filter_stage = ?
              AND market_result IN ('yes','no')
        """
        params: List[Any] = [*ASSETS, PRODUCT_TYPE, CANDIDATE_FILTER_STAGE]
        df = pd.read_sql_query(sql, conn, params=params)
    finally:
        conn.close()

    if "side" in df.columns:
        df["side"] = df["side"].fillna("yes")

    # Defensive: drop settlement-leakage cols if they ever sneak into SELECT *.
    for c in SETTLEMENT_LEAKAGE_COLS:
        if c in df.columns:
            df = df.drop(columns=[c])

    return df


# ── Per-band table ───────────────────────────────────────────────────────────


def build_per_band_table(df, window_days: float):
    """Return a per-(asset × STC band) table with all aggregates.

    Columns: asset, stc_band, n_admit, n_yes, n_no, wr, wilson_lo,
    wilson_hi, cf_pnl_cents, cf_pnl_30d_cents, covered_by,
    qualifies_for_gate, dominant_strategy, dominant_price_band.
    """
    import pandas as pd

    df = df.copy()
    df["stc_band"] = df["seconds_to_close"].apply(assign_stc_band)
    df = df[df["stc_band"].notna()]
    if "market_result" in df.columns:
        df = df[df["market_result"].isin(("yes", "no"))]
    # Recompute cf_pnl per-row with honest-NULL derivation as a fallback.
    df["cf_cents_resolved"] = df.apply(lambda r: row_counterfactual_cents(r.to_dict()),
                                       axis=1)
    rows: List[Dict[str, Any]] = []
    factor = 30.0 / max(window_days, 1e-9)
    for (asset, band), grp in df.groupby(["asset", "stc_band"], sort=False):
        n_admit = len(grp)
        n_yes = int((grp["market_result"] == "yes").sum())
        n_no = int((grp["market_result"] == "no").sum())
        wr = n_yes / n_admit if n_admit else 0.0
        wilson_lo, wilson_hi = wilson_ci(k=n_yes, n=n_admit, alpha=0.05)
        cf_cents = int(grp["cf_cents_resolved"].dropna().sum())
        cf_30d = cf_cents * factor
        band_rows = grp.to_dict(orient="records")
        coverage = classify_band_cell_block_coverage(band_rows)
        # dominant strategy in band (for the recommendation memo)
        strat_top = Counter(grp["strategy"].dropna()).most_common(1)
        dom_strat = strat_top[0][0] if strat_top else None
        # rough price band (min-max range — for memo readability)
        try:
            dom_px = f"{int(grp['market_price'].min())}-{int(grp['market_price'].max())}"
        except Exception:
            dom_px = ""
        qualifies = qualifies_for_gate_trigger(
            n_admit=n_admit, wilson_hi=wilson_hi,
            cf_pnl_30d_cents=cf_30d, covered_by=coverage)
        rows.append({
            "asset": asset, "stc_band": band,
            "n_admit": n_admit, "n_yes": n_yes, "n_no": n_no,
            "wr": round(wr, 4),
            "wilson_lo": round(wilson_lo, 4),
            "wilson_hi": round(wilson_hi, 4),
            "cf_pnl_cents": cf_cents,
            "cf_pnl_30d_cents": cf_30d,
            "covered_by": coverage,
            "qualifies_for_gate": qualifies,
            "dominant_strategy": dom_strat,
            "dominant_price_band": dom_px,
        })
    out = pd.DataFrame(rows)
    if not out.empty:
        out = out.sort_values(
            by=["asset", "stc_band"],
            key=lambda c: c.map(_band_sort_key) if c.name == "stc_band" else c,
        ).reset_index(drop=True)
    return out


# ── CLI ──────────────────────────────────────────────────────────────────────


def _window_days_from_df(df) -> float:
    import pandas as pd
    ts = pd.to_datetime(df["evaluation_time"], utc=True, errors="coerce").dropna()
    if ts.empty:
        return 1.0
    span = (ts.max() - ts.min()).total_seconds() / 86400.0
    return max(span, 1.0)


def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(description="STC structural clustering audit (86b9wxr6p)")
    p.add_argument("--db", type=Path, default=Path("/tmp/stc_clustering/corpus.db"))
    p.add_argument("--out", type=Path,
                   default=Path("kb/findings/stc-structural-clustering-may12"))
    p.add_argument("--since", type=str, default=None,
                   help="ISO date floor for stratified runs (e.g. pre/post SOL_BLEED_V2).")
    args = p.parse_args(argv)

    if not args.db.exists():
        print(f"DB not found: {args.db}", file=sys.stderr)
        return 2
    args.out.mkdir(parents=True, exist_ok=True)

    print(f"Loading corpus from {args.db} ...")
    df = load_admit_corpus(args.db)
    if args.since:
        df = df[df["evaluation_time"] >= args.since]
    window_days = _window_days_from_df(df)
    print(f"  rows={len(df)}  window_days={window_days:.1f}  per_asset={dict(df['asset'].value_counts())}")

    table = build_per_band_table(df, window_days=window_days)

    # Print markdown
    print()
    print(f"## Per-(asset × STC band) table — window={window_days:.1f}d, "
          f"30d normalization factor={30.0/window_days:.3f}")
    print()
    print("| asset | stc_band | n | n_yes | n_no | WR | Wilson95 | cf_pnl_30d | covered_by | gate? | dom_strat | dom_px |")
    print("|---|---|---|---|---|---|---|---|---|---|---|---|")
    for _, r in table.iterrows():
        gate_mark = "**QUALIFIES**" if r["qualifies_for_gate"] else ""
        ci = f"[{r['wilson_lo']:.3f},{r['wilson_hi']:.3f}]"
        cov = r["covered_by"] or ""
        print(f"| {r['asset']} | {r['stc_band']} | {r['n_admit']} | {r['n_yes']} | "
              f"{r['n_no']} | {r['wr']:.3f} | {ci} | "
              f"${r['cf_pnl_30d_cents']/100:.2f} | {cov} | {gate_mark} | "
              f"{r['dominant_strategy'] or ''} | {r['dominant_price_band']} |")

    actionable = table[table["qualifies_for_gate"]].copy()
    print()
    print(f"## Actionable combos (n={len(actionable)})")
    if actionable.empty:
        print("None — all bands either healthy (Wilson95_hi≥0.92) or already gated.")
    else:
        print()
        print("| asset | stc_band | n | WR | Wilson95_hi | cf_pnl_30d | dom_strat | dom_px |")
        print("|---|---|---|---|---|---|---|---|")
        for _, r in actionable.iterrows():
            print(f"| {r['asset']} | {r['stc_band']} | {r['n_admit']} | "
                  f"{r['wr']:.3f} | {r['wilson_hi']:.3f} | "
                  f"${r['cf_pnl_30d_cents']/100:.2f} | "
                  f"{r['dominant_strategy'] or ''} | {r['dominant_price_band']} |")

    # CSV output
    csv_path = args.out / "per_band_table.csv"
    with open(csv_path, "w", newline="") as f:
        w = csv.writer(f)
        cols = list(table.columns)
        w.writerow(cols)
        for _, r in table.iterrows():
            w.writerow([r[c] for c in cols])
    print(f"\nCSV saved to {csv_path}")

    csv_actionable = args.out / "actionable_combos.csv"
    with open(csv_actionable, "w", newline="") as f:
        w = csv.writer(f)
        if not actionable.empty:
            cols = list(actionable.columns)
            w.writerow(cols)
            for _, r in actionable.iterrows():
                w.writerow([r[c] for c in cols])
        else:
            w.writerow(["status"])
            w.writerow(["no_qualifying_combos"])
    print(f"Actionable CSV saved to {csv_actionable}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
