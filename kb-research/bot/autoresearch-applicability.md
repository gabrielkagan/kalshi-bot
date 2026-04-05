---
status: pending
updated: 2026-03-30
tags: [research, automation, backtesting]
---
# Autoresearch Applicability — Complete Analysis

Source: Mar 2026
Chat links: https://claude.ai/chat/d9defa68-4647-4c9e-ac0a-8249c619a752, https://claude.ai/chat/f0bcdddf-0618-4dad-8476-3d383c8445ff

---

## Karpathy's Autoresearch Concept
From github.com/karpathy/autoresearch. Core loop: agent proposes code change → run 5-minute experiment → evaluate against single metric (val loss) → keep or discard → repeat. Applied to LLM training — agent works on git feature branch, accumulates commits that find better neural network architecture, optimizer settings, hyperparameters.

## How It Maps to the Kalshi Bot

### What maps cleanly
Tunable surface area is large:
- Edge thresholds (price-dependent schedule)
- Kelly fractions (full, quarter, eighth per product)
- Temperature scaling (T values per pipeline)
- Blend weights (model/market per product)
- Per-asset filters (entry floors, risk caps)
- STC boundaries (when to enter, when to escalate)
- Weekend/overnight multipliers
- EGARCH vs LightGBM vs HAR-RV model choice

Evaluation infrastructure already exists: shadow mode, counterfactual dashboard on gabekagan.io, Wilson CI, SPRT. Claude Code is already the dev partner with full CLAUDE.md context.

### The Hard Part: Evaluation Cycle Time
- Karpathy: 5-minute experiments with clean signal (val loss)
- Bot: needs real market data flowing through → evaluation takes DAYS
- Naive autoresearch would be glacially slow

### The Unlock: Fast Backtesting Harness
- Replay historical JSONL data through candidate configuration
- Compute counterfactual Brier/PnL in seconds
- Agent iterates at Karpathy-like speed against historical data
- Only graduate promising configs to live shadow for real-world validation
- This is the blocking prerequisite — NOT BUILT YET

### Overfitting Risk (The Elephant in the Room)
With limited historical data, aggressive optimization WILL find configurations that look great on history and fail forward. Their "Sharpe 21.4" problem, but worse because our N is smaller.

**Mitigation:**
1. Split historical signals into train/validation (60/40 by time, not random)
2. Let loop optimize on train only
3. Score against validation as acceptance criterion
4. Any promoted config still enters shadow — autoresearch output is a CANDIDATE for shadow, not a bypass of the promotion process

## Regime Sensitivity and Agent Adaptability

### The Changing Data Problem
Markets don't just have noise — they have structural shifts. Kalshi prediction markets are sensitive to:
- Changes in implied vol regimes
- Shifts in market maker behavior
- Liquidity regime changes (weekend vs weekday, new institutional participants)
- Contract design changes from Kalshi

### How to Handle Regime Shifts

**Regime-tagged evaluations:** Every shadow variant evaluation should include a regime tag — at minimum, realized vol bucket and liquidity bucket for the evaluation window. Don't ask "did this beat baseline?" Ask "did this beat baseline in conditions similar to what we expect going forward?"

**Regime-aware circuit breakers:** If BTC vol doubles and bot underperforms, that's different from underperforming in stable conditions. Circuit breaker should pause when performance degrades relative to what's expected given current conditions, not just absolutely.

**Continuous validation:** Promoted strategies need ongoing "still working?" checks that can demote. Promotion is earned continuously, not once. Graduated exposure (shadow → 10% → 25% → etc.) with each level having its own validation threshold.

## Weather-Specific Application
Weather is a natural autoresearch candidate because the parameter space is well-defined:
- Temperature scaling factor (currently needs T≥3.0)
- Per-city enable/disable (19 cities, most underwater)
- Lead-time window (STC 1-8h looks promising vs longer)
- YES vs NO side gating
- Edge threshold per configuration

466+ settled signals exist for backtesting. But with way fewer per city/config slice, aggressive optimization will absolutely find false positives.

**Constrained approach:** NO-side only, lead time ≤8h, optimize T + city selection + edge threshold within that box. Don't try to fix everything — narrow the search space.

## Status
Concept validated as applicable. Blocked on fast backtesting harness implementation. Not started.

## Related (KB operational articles)
- [[kb/concepts/edge-thresholds.md]]
- [[kb/concepts/cal-engine-registry.md]]
