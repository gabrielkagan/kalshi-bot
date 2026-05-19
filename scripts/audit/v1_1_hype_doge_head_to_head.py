"""Bit D (86ba0jn50) of HYPE/DOGE cal_mlp v1.1 retrain umbrella 86ba0jmyq.

Head-to-head Brier comparison: cal_mlp v1.1 candidate (from Bit C) vs
the production baseline `(1 - W) × raw_prob + W × market_price` blend
on the HYPE/DOGE test fold.

Output: structured dict with Brier per side, delta, bootstrap CI on
delta, per-price-tier sub-buckets, and a SHIP/HOLD recommendation.

DOES NOT FLIP THE CURRENT POINTER. The pointer flip is a SEPARATE
operator-approved action gated on this Bit's verdict.

Plan doc: kb/decisions/v1-1-D-head-to-head-plan.md.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


# Defensive Mac-only guard (mirrors Bit A pattern).
def _refuse_vps_path(path: str) -> None:
    """Mac-only invariant per `feedback_vps_compute_isolation`. Bit D runs
    on the operator's Mac against Bit C's Mac-local artifacts."""
    abs_path = os.path.abspath(path)
    if abs_path.startswith("/home/botuser/"):
        raise ValueError(
            f"Refusing VPS path {path!r}. Bit D is Mac-only. Bit C "
            "artifacts at data/cal_mlp/ + models/cal_mlp_*/ are Mac-local."
        )


# Production blend weights from bot/constants.py.
_MARKET_BLEND_W_BY_ASSET = {
    "BTC": 0.10, "ETH": 0.20, "SOL": 0.80, "XRP": 0.90,
    "HYPE": 0.80, "DOGE": 0.60,
}


# ── Loaders ──────────────────────────────────────────────────────────


def _load_test_fold(asset: str, project_root: Optional[Path] = None) -> pd.DataFrame:
    """Load the Bit C extract bundle's test rows for `asset`."""
    root = Path(project_root) if project_root else _REPO_ROOT
    _refuse_vps_path(str(root))
    extract_dir = root / "data" / "cal_mlp" / asset
    current_file = extract_dir / "CURRENT"
    if not current_file.exists():
        raise FileNotFoundError(
            f"No CURRENT pointer at {current_file}. Run Bit C extract first."
        )
    train_id = current_file.read_text().strip()
    parquet_path = extract_dir / train_id / "fold0.parquet"
    if not parquet_path.exists():
        raise FileNotFoundError(f"No fold0.parquet at {parquet_path}")
    df = pd.read_parquet(parquet_path)
    test = df[df["split"] == "test"].copy()
    if test.empty:
        raise ValueError(f"No test-split rows in {parquet_path}")
    # Bit D R1 M1: filter to side='yes' rows. The bot evaluates both
    # 'yes' and 'no' sides; the cal_mlp `predict()` call has a
    # different breakeven calculation per side (see _helpers.py:277
    # `breakeven = market_implied_prob_yes(entry_price_cents, side)`)
    # and the baseline `(1-W)*raw + W*mkt` formula assumes the entry
    # price is the side's ASK. Mixing sides without per-row flips
    # would mis-compute Brier for the small no-side population
    # (HYPE 3/189, DOGE 3/220 on 2026-05-18 test fold).
    yes_only = test[test["side"].astype(str) == "yes"].copy()
    if yes_only.empty:
        raise ValueError(
            f"No side=='yes' rows in {parquet_path} test split — Bit D's "
            f"baseline formula assumes side='yes' (filter dropped all rows)."
        )
    return yes_only


def _load_predictor(asset: str, project_root: Optional[Path] = None):
    """Load the Bit C trained CalMLPPredictor for `asset`."""
    if project_root:
        os.environ["KALSHI_PROJECT_ROOT"] = str(Path(project_root).resolve())
    sys.path.insert(0, str(_REPO_ROOT / "scripts" / "cal_mlp"))
    from integration import CalMLPPredictor
    p = CalMLPPredictor(asset)
    p.warmup()
    if not p._loaded:
        raise RuntimeError(
            f"CalMLPPredictor({asset!r}).warmup() returned without loading."
        )
    return p


# ── Brier + bootstrap ────────────────────────────────────────────────


def _brier(preds: np.ndarray, outcomes: np.ndarray) -> float:
    """Brier = mean((p - y)^2)."""
    return float(np.mean((preds - outcomes) ** 2))


def _bootstrap_delta_ci(
    baseline_preds: np.ndarray,
    v1_1_preds: np.ndarray,
    outcomes: np.ndarray,
    *,
    b: int = 2000,
    seed: int = 42,
) -> tuple[float, float]:
    """Bootstrap 95% CI on delta_brier = v1_1 - baseline.

    Percentile method (simple; documented in finding doc). For small
    test fold sizes (n<300), percentile-CI tends to be conservative.
    """
    rng = np.random.default_rng(seed)
    n = len(outcomes)
    deltas = np.empty(b)
    for i in range(b):
        idx = rng.integers(0, n, size=n)
        bp = baseline_preds[idx]
        vp = v1_1_preds[idx]
        oc = outcomes[idx]
        deltas[i] = _brier(vp, oc) - _brier(bp, oc)
    return (float(np.percentile(deltas, 2.5)), float(np.percentile(deltas, 97.5)))


# ── Sub-bucket helpers ───────────────────────────────────────────────


_PRICE_TIER_LABELS = {0: "<80c", 1: "80-89c", 2: "90-95c", 3: ">=96c"}


def _compute_sub_buckets(
    df: pd.DataFrame,
    baseline_preds: np.ndarray,
    v1_1_preds: np.ndarray,
    outcomes: np.ndarray,
) -> dict:
    """Per-price-tier Brier breakdown."""
    out = {}
    tiers = df["price_tier"].to_numpy() if "price_tier" in df.columns else \
        np.digitize(df["market_price"].to_numpy(), [80, 90, 96], right=True)
    for tier_int, tier_label in _PRICE_TIER_LABELS.items():
        mask = tiers == tier_int
        n = int(mask.sum())
        if n == 0:
            out[tier_label] = {
                "n": 0, "baseline_brier": None, "v1_1_brier": None,
                "delta": None,
            }
            continue
        b_br = _brier(baseline_preds[mask], outcomes[mask])
        v_br = _brier(v1_1_preds[mask], outcomes[mask])
        out[tier_label] = {
            "n": n,
            "baseline_brier": round(b_br, 4),
            "v1_1_brier": round(v_br, 4),
            "delta": round(v_br - b_br, 4),
        }
    return out


# ── Main analysis ────────────────────────────────────────────────────


def _build_row_features(row: pd.Series) -> dict:
    """Assemble the row_features dict for CalMLPPredictor.predict()."""
    return {
        "calibrated_prob": float(row.get("calibrated_prob", row["raw_prob"])),
        "spot_distance_to_strike_sigma": float(row.get("spot_distance_to_strike_sigma", 0.0)),
        "abs_spot_distance_to_strike_sigma": float(row.get("abs_spot_distance_to_strike_sigma", 0.0)),
        "time_decayed_proximity": float(row.get("time_decayed_proximity", 0.0)),
        "prob_breakeven_gap": float(row.get("prob_breakeven_gap", 0.0)),
        "hour_sin": float(row.get("hour_sin", 0.0)),
        "hour_cos": float(row.get("hour_cos", 0.0)),
        "seconds_to_close": float(row.get("seconds_to_close", 300.0)),
        "market_price": int(row.get("market_price", 85)),
        "vol_regime": str(row.get("vol_regime", "normal")),
        "vol_regime_int": int(row.get("vol_regime_int", 0)),
    }


def compute_brier_headtohead(
    asset: str,
    *,
    project_root: Optional[Path] = None,
    bootstrap_b: int = 2000,
) -> dict:
    """Head-to-head Brier: production blend vs cal_mlp v1.1 on test fold."""
    if asset not in _MARKET_BLEND_W_BY_ASSET:
        raise ValueError(f"Unknown asset {asset!r}")
    blend_w = _MARKET_BLEND_W_BY_ASSET[asset]

    df = _load_test_fold(asset, project_root=project_root)
    n = len(df)
    raw = df["raw_prob"].to_numpy(dtype=float)
    mkt = df["market_price"].to_numpy(dtype=float) / 100.0
    # Production baseline: (1-W)*raw + W*mkt
    baseline_preds = (1.0 - blend_w) * raw + blend_w * mkt
    # Outcome: market_result == 'yes' (side is always 'yes' for these rows)
    outcomes = (df["market_result"].astype(str) == "yes").to_numpy(dtype=float)

    predictor = _load_predictor(asset, project_root=project_root)
    v1_1_preds = np.empty(n)
    for i, (_, row) in enumerate(df.iterrows()):
        try:
            cal_prob, _ens_std, _lo, _hi = predictor.predict(
                raw_prob=float(row["raw_prob"]),
                ticker=str(row["ticker"]),
                side="yes",
                entry_price_cents=int(row["market_price"]),
                row_features=_build_row_features(row),
            )
            v1_1_preds[i] = float(cal_prob)
        except Exception as e:
            logger.warning("predict failed for row %d: %s", i, e)
            # Fall back to raw_prob on failure — same as production fallback.
            v1_1_preds[i] = float(row["raw_prob"])

    baseline_brier = _brier(baseline_preds, outcomes)
    v1_1_brier = _brier(v1_1_preds, outcomes)
    delta = v1_1_brier - baseline_brier
    ci = _bootstrap_delta_ci(baseline_preds, v1_1_preds, outcomes, b=bootstrap_b)
    sub_buckets = _compute_sub_buckets(df, baseline_preds, v1_1_preds, outcomes)

    # Decision logic
    ship_gate_brier = delta < 0
    ship_gate_ci = ci[1] < 0
    worst_sub_delta = max(
        (b["delta"] for b in sub_buckets.values() if b["delta"] is not None),
        default=0.0,
    )
    ship_gate_sub = worst_sub_delta < 0.02
    ship = ship_gate_brier and ship_gate_ci and ship_gate_sub
    rationale_parts = [
        f"delta_brier={delta:+.4f} (gate: <0 → {'PASS' if ship_gate_brier else 'FAIL'})",
        f"CI95={ci[0]:+.4f}..{ci[1]:+.4f} (gate: upper<0 → {'PASS' if ship_gate_ci else 'FAIL'})",
        f"worst sub-bucket delta={worst_sub_delta:+.4f} (gate: <+0.02 → {'PASS' if ship_gate_sub else 'FAIL'})",
    ]

    return {
        "asset": asset,
        "n_test": int(n),
        "baseline_brier": round(baseline_brier, 4),
        "v1_1_brier": round(v1_1_brier, 4),
        "delta_brier": round(delta, 4),
        "bootstrap_ci_95": (round(ci[0], 4), round(ci[1], 4)),
        "baseline_w": blend_w,
        "sub_buckets": sub_buckets,
        "recommendation": "SHIP" if ship else "HOLD",
        "rationale": " | ".join(rationale_parts),
    }


# ── CLI ──────────────────────────────────────────────────────────────


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Bit D (86ba0jn50): head-to-head Brier — production blend vs "
            "cal_mlp v1.1 — for HYPE/DOGE on Bit C's test fold."
        )
    )
    parser.add_argument(
        "--asset", choices=["HYPE", "DOGE"], action="append", default=None,
        help="Asset to analyze; pass twice for both. Default: both.",
    )
    parser.add_argument(
        "--bootstrap-b", type=int, default=2000,
        help="Bootstrap resample count for CI on delta_brier.",
    )
    parser.add_argument(
        "--project-root", default=None,
        help="Override project root (default: worktree containing this script).",
    )
    parser.add_argument(
        "--log-level", default="INFO",
        help="Logging level.",
    )
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(message)s",
    )
    assets = args.asset or ["HYPE", "DOGE"]
    out = {}
    for asset in assets:
        out[asset] = compute_brier_headtohead(
            asset,
            project_root=args.project_root,
            bootstrap_b=args.bootstrap_b,
        )
    print(json.dumps(out, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
