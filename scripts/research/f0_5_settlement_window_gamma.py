"""F0.5 — Settlement-window gamma falsification (CT-MDP Attack #5, Phase 0).

Scaffold-ship STUB. Implementation lands post scaffold-gate clearance (2
consecutive 0C+0M adv rounds at the scaffold tier).

Parent plan: kb/decisions/ct-mdp-f0-5-settlement-window-gamma-plan.md
Parent ClickUp: 86ba18zhr
Umbrella: kb/decisions/ct-mdp-attack-alpha-program-plan.md (ticket 86ba18zbv)

Hypothesis (per plan-doc § Hypothesis):
  The last 60-120s of a 15m Kalshi window carries panic-unwind / gamma-style
  flow that's more predictive of settle direction than earlier states. A
  classifier fit on (state at T-60s before close) → settle direction should
  achieve ROC AUC > 0.55 in at least one tradeable cell (vol-regime ×
  day/night × moneyness).

Kill threshold (per ticket 86ba18zhr):
  T-60s AUC < 0.55 in EVERY tradeable cell (CI upper-bound < 0.55) → KILL.
  At least one cell with AUC ≥ 0.55 lower-CI → SURVIVE.

Data sources (per plan-doc § RCA):
  - settled_trades: 90d+ outcome label + window-close timestamp.
  - evaluated_opportunities: spot trajectory via *_spot_at_decision per asset.
  - market_observations_continuous: 5d NBBO trajectory near window expiry.

Run (post-impl):
  python3 scripts/research/f0_5_settlement_window_gamma.py --db state.db
"""

from __future__ import annotations

if __name__ == "__main__":
    raise NotImplementedError("scaffold-only; impl pending")
