"""F0.1 — Stale-quote sniping falsification (CT-MDP Attack #1, Phase 0).

Skeleton ship — defines the public API surface for the failing-assertion
test scaffold at `tests/research/test_f0_1_stale_quote_falsification.py`.
All algorithmic helpers raise `NotImplementedError`; subsequent commits
fill them in function-by-function (TDD GREEN progression).

Parent plan: kb/decisions/ct-mdp-f0-1-stale-quote-falsification-plan.md
Parent ClickUp: 86ba18zg8
Umbrella: kb/decisions/ct-mdp-attack-alpha-program-plan.md (ticket 86ba18zbv)

Hypothesis:
  Kalshi NBBO refreshes lag Coinbase price moves. During the lag, the
  resting Kalshi quote is takeable at a price inconsistent with current
  spot. Sum-of-(dislocation × duration × size × hit_probability) over the
  available data window estimates the annual ceiling on Attack #1.

Kill threshold (per ticket 86ba18zg8):
  If 95th-pct ceiling across 7 assets < $5K/yr, kill Attack #1.

Data sources (per RCA at plan-doc kickoff 2026-05-20):
  - market_observations_continuous: ~5d of Kalshi NBBO + cache_age_ms.
  - evaluated_opportunities: spot at decision moments (sparser cadence).
  - settled_trades: 7-asset universe + 15m window mapping.

Run:
  python3 scripts/research/f0_1_stale_quote_falsification.py --db state.db
"""

from __future__ import annotations

import argparse
import datetime as dt
from typing import Any, Iterable, Mapping, Sequence

# Anti-fantasy size clamp on observed available size (per plan-doc § Method).
MAX_TAKE: int = 100

# Schema invariants pinned by tests/research/test_f0_1_stale_quote_falsification.py.
MOC_REQUIRED_COLUMNS: tuple[str, ...] = (
    "ticker",
    "observation_time",
    "yes_bid_cents",
    "yes_ask_cents",
    "no_bid_cents",
    "no_ask_cents",
    "bid_depth",
    "ask_depth",
    "cache_age_ms",
)

EVAL_OPPS_SPOT_COLUMNS: tuple[str, ...] = (
    "btc_spot_at_decision",
    "eth_spot_at_decision",
    "sol_spot_at_decision",
    "xrp_spot_at_decision",
    "hype_spot_at_decision",
    "doge_spot_at_decision",
    "bnb_spot_at_decision",
)

SETTLED_TRADES_REQUIRED_COLUMNS: tuple[str, ...] = (
    "asset",
    "settled_at",
    "ticker",
    "event_ticker",
)


def _parse_iso(ts: str) -> dt.datetime:
    """Parse ISO-8601 timestamp with optional `Z` suffix to aware datetime.

    Lexicographic comparison on ISO strings breaks under mixed precision
    (e.g., `"2026-05-20T10:01:00Z"` vs `"2026-05-20T10:01:00.999999Z"` —
    `.` 0x2E < `Z` 0x5A, so a microsecond-precision row in the same second
    AFTER the event compares as `≤` a second-precision event timestamp).
    Parse to datetime objects and compare numerically instead.
    """
    if ts.endswith("Z"):
        ts = ts[:-1] + "+00:00"
    return dt.datetime.fromisoformat(ts)


def select_stale_snapshot(
    *,
    moc_rows: Sequence[Mapping[str, Any]],
    sigma_move_time: str,
) -> list[Mapping[str, Any]]:
    """Return moc rows whose observation_time ≤ sigma_move_time.

    Pinned no-look-ahead invariant. Compares via parsed datetime (NOT
    lexicographic) so mixed-precision timestamps (`...:00Z` vs
    `...:00.999999Z`) don't break the invariant.
    """
    event_dt = _parse_iso(sigma_move_time)
    return [row for row in moc_rows if _parse_iso(row["observation_time"]) <= event_dt]


def compute_ceiling(
    *,
    events: Iterable[Mapping[str, Any]],
    asset: str,
    regime_conditioned: bool = True,
) -> dict[str, float]:
    """Per-asset annualized dollar-ceiling, regime-conditioned when flag set.

    Returns dict with keys: vol_high, vol_low, day, night, total.
    """
    raise NotImplementedError("F0.1 ceiling computation lands in next commit")


def bootstrap_ceiling_ci(
    *,
    per_event_values: Sequence[float],
    n_resamples: int = 1000,
    seed: int | None = None,
) -> dict[str, float]:
    """Return {ci_low, point, ci_high} via percentile bootstrap on per-event $ values."""
    raise NotImplementedError("bootstrap CI lands in next commit")


def aggregate_event_value(
    *,
    dislocation_cents: float,
    available_size: int,
    hit_probability: float,
) -> float:
    """Per-event takeable value in CENTS, with MAX_TAKE size clamp + hit-prob sanity check.

    Raises ValueError if hit_probability is outside [0.0, 1.0].
    """
    if not (0.0 <= hit_probability <= 1.0):
        raise ValueError(
            f"hit_probability must be in [0,1]; got {hit_probability} "
            "(likely a methodology bug — see plan-doc § Method)"
        )
    size = min(available_size, MAX_TAKE)
    return abs(dislocation_cents) * size * hit_probability


def classify_verdict(
    *,
    per_asset_ceilings: Mapping[str, float],
    threshold_dollars: float = 5000.0,
) -> str:
    """Return 'KILL' if all assets < threshold; 'SURVIVE' if any ≥ threshold."""
    if any(c >= threshold_dollars for c in per_asset_ceilings.values()):
        return "SURVIVE"
    return "KILL"


def survival_diagnostics(
    *,
    per_asset_ceilings: Mapping[str, float],
    threshold_dollars: float = 5000.0,
) -> dict[str, Any]:
    """Return per-asset/program-level diagnostics for the verdict consumer.

    The umbrella program-level gate (`kb/decisions/ct-mdp-attack-alpha-program-plan.md`
    § Rules of engagement) is ≥2 of 3 falsifications survive their per-attack threshold.
    Per-attack F0.1 survives on `n_assets_clearing_threshold ≥ 1` — but downstream
    callers may want to weight a single-asset SURVIVE differently (false-survive risk
    at small sample size per R1-M5). Diagnostics let them.
    """
    clearing = [a for a, c in per_asset_ceilings.items() if c >= threshold_dollars]
    return {
        "verdict": classify_verdict(
            per_asset_ceilings=per_asset_ceilings,
            threshold_dollars=threshold_dollars,
        ),
        "n_assets_clearing_threshold": len(clearing),
        "assets_clearing_threshold": sorted(clearing),
        "threshold_dollars": threshold_dollars,
    }


def main(
    *,
    db_path: str,
    days: int = 5,
    sigma_threshold: float = 0.3,
    bootstrap_n: int = 1000,
    output_path: str | None = None,
) -> dict[str, Any]:
    """End-to-end Phase 0 falsification.

    Returns:
        {
            'verdict': 'KILL' | 'SURVIVE',
            'per_asset_ceilings': {'BTC': float, ...},
            'per_asset_ci': {'BTC': {'low': float, 'high': float}, ...},
            'window_days': int,
            'n_events_per_asset': {'BTC': int, ...},
            'regime_breakdown': {...},
        }
    """
    raise NotImplementedError("F0.1 end-to-end pipeline lands in subsequent commits")


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="F0.1 stale-quote sniping falsification")
    parser.add_argument("--db", default="state.db", help="Path to state.db (default: ./state.db)")
    parser.add_argument("--days", type=int, default=5, help="Analysis window in days")
    parser.add_argument("--sigma-threshold", type=float, default=0.3,
                        help="σ-move threshold for event detection")
    parser.add_argument("--bootstrap-n", type=int, default=1000,
                        help="Bootstrap resample count")
    parser.add_argument("--out", default=None,
                        help="Output markdown path (default: stdout)")
    return parser.parse_args(argv)


if __name__ == "__main__":  # pragma: no cover
    args = _parse_args()
    result = main(
        db_path=args.db,
        days=args.days,
        sigma_threshold=args.sigma_threshold,
        bootstrap_n=args.bootstrap_n,
        output_path=args.out,
    )
    print(result)
