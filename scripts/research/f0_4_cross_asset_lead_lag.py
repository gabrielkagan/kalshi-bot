"""F0.4 — Cross-Asset Lead-Lag Falsification (CT-MDP Attack #4, Phase 0).

Scaffold-only stub. All algorithmic helpers (`compute_lead_edge`,
`bootstrap_lead_edge_ci`, `bonferroni_adjusted_ci`, `classify_verdict`,
`survival_diagnostics`, `main`) are NotImplementedError stubs at this
ship; the failing-assertion test suite at
`tests/research/test_f0_4_cross_asset_lead_lag.py` pins the
methodological invariants for the impl-Bit follow-up.

Parent plan: kb/decisions/ct-mdp-f0-4-cross-asset-lead-lag-plan.md
Parent ClickUp: 86ba18zgx
Umbrella: kb/decisions/ct-mdp-attack-alpha-program-plan.md (ticket 86ba18zbv)
Predecessor: scripts/research/f0_1_stale_quote_falsification.py (F0.1, SHIPPED PR #132).

Hypothesis:
  BTC leads ETH/SOL/XRP/HYPE/DOGE/BNB by 3-30s in high-vol regimes.
  For each BTC σ-move at t_C, the laggard's Kalshi mid does not re-price
  for Δt seconds; lead-edge per event = (M_responded − M_stale) signed
  by BTC direction. Per laggard × regime (4 buckets), aggregate mean
  lead-edge with bootstrap CI; apply Bonferroni correction across 24
  simultaneous tests.

Kill threshold (per ticket 86ba18zgx):
  If no laggard asset shows >3¢/trade lead-edge in ANY regime with
  Bonferroni-adjusted CI excluding zero, kill Attack #4.

Data sources:
  - market_observations_continuous: laggard Kalshi mid trajectory (~6s cadence).
  - evaluated_opportunities: per-asset *_spot_at_decision (cross-asset cadence ~30s mean).
  - 7-asset universe pinned at ASSET_TICKER_PREFIX; 6-laggard universe pinned at LAGGARD_ASSETS.

Run (post-impl-Bit):
  python3 scripts/research/f0_4_cross_asset_lead_lag.py --db state.db
"""

from __future__ import annotations

# Anti-fantasy clamps + Bonferroni constants (impl-Bit fills the rest).

#: Max plausible cross-asset lead-edge in cents over the Δt window. A
#: laggard Kalshi mid moving > 25¢ in 30s from a single BTC σ-event is
#: implausible (binary range is 0-100) and likely indicates a methodology
#: bug (e.g., comparing across ticker expiries).
MAX_PLAUSIBLE_EDGE_CENTS: float = 25.0

#: Per-trade lead-edge floor (cents) per umbrella ticket 86ba18zgx
#: kill rule.
KILL_THRESHOLD_CENTS: float = 3.0

#: Family-wise α = 0.05 / 24 across 6 laggards × 4 regimes.
BONFERRONI_N_TESTS_DEFAULT: int = 24

#: Min per-cell event count below which a cell reports "insufficient".
MIN_EVENTS_PER_CELL: int = 30


if __name__ == "__main__":
    raise NotImplementedError("scaffold-only; impl pending")
