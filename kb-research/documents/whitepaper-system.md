---
status: active
updated: 2026-03-26
tags: [research, documentation, whitepaper]
---
# Whitepaper & Documentation System — Complete Record

Source: Multiple sessions Feb-Mar 2026
Chat links: https://claude.ai/chat/94a77bfd-bff6-4d5a-b1aa-d458b1fef422, https://claude.ai/chat/bfdc79ec-9688-4a57-b8eb-12a59a72d7f2, https://claude.ai/chat/30bec92a-f790-454a-8a5e-eb9e3f88948b, https://claude.ai/chat/932a113a-5dd3-4041-92e9-88d7a78ff5f6, https://claude.ai/chat/eb047d9b-31ac-41fa-ac57-60d3781e1d07

---

## Documents Produced

### Investor Whitepaper (PDF)
- Audience: potential investors/stakeholders
- Modern fintech style (not bland LaTeX)
- Covers: market structure, strategy pipeline, risk management, performance
- Built with reportlab (Python) — custom cover page, accent colors (teal, blue), data visualizations, callout boxes

### Technical Whitepaper (PDF)
- Audience: technical evaluation
- Denser but still modern-looking
- Architecture diagrams, model specifications, code-level details
- All 5 verticals with per-engine breakdowns

### 16-Slide Technical Deck (PPTX)
- Dark theme: bg #0D1117, teal/blue/orange accents
- Built with pptxgenjs (Node.js)
- Slide breakdown:
  1. Title — hero stats (5 verticals, 88.8% WR, 258 trades, 10.4K LOC)
  2. What It Does — five vertical cards with status badges (LIVE/OBS/SHADOW)
  3. Strategy Pipeline — 6-step numbered grid (Observe→Estimate→Filter→Size→Execute→Settle)
  4. Key Differentiators — 6 cards with icons
  5. System Architecture — pipeline flow diagram + supporting components
  6. Volatility Engine — RK, EGARCH, MZ, jump detection, DVOL, cross-beta
  7. Probability Model — 5-step flow + sanity checks + calibration progression
  8. Edge Detection & Fees — fee formulas, min-edge table, market selection
  9. Execution Strategy — 3-tier cards + time escalation + safety features
  10. Position Sizing & Risk — sizing tiers, drawdown scaling, controls
  11. S&P 500 Engine — equity adaptations, config, data sources
  12. Weather Engine — ensemble model, config, 19-city grid
  13. Sports Engine — Bayesian model, 28 leagues, tennis pipeline, lifecycle
  14. Performance — big stat callouts + vertical status breakdown
  15. Infrastructure — deployment, persistence, dashboard, monitoring
  16. Closing — summary stats + tagline

### Rebuild Playbook (DOCX)
- 11 sections from earliest bot conversations
- Complete institutional memory in document form:
  1. What Kalshi is and how crypto markets work (including KXBTC15M is NOT multi-strike gotcha)
  2. The API — auth, rate limits, orderbook quirks, order types, WebSocket, AmendOrder
  3. Every API gotcha that cost real money — fee rounding bug (21-43x overcharge), auth path bug, phantom fills, balance-based settlement failures, z-score dead zone, cross-asset exposure bug, stale metadata after amend
  4. The strategy — execution phases, statistical model pipeline, Kelly sizing, Thompson Sampling
  5. Infrastructure — exact server details, systemd config, every command needed, dashboard architecture
  6. Deployment checklist — copy-paste commands with actual IP and paths
  7. Logging — what files to create, what fields to log, lessons learned
  8. How to catch bugs — pre-deployment audits, runtime monitoring, post-mortem analysis
  9. Architecture recommendations — simplest design that works + everything to do differently
  10. Lessons learned — strategy, engineering, process
  11. Quick reference card — correct fee formulas, tech stack, file structure, version history

## Dynamic Updating System Design

### The Problem
Whitepapers drift from codebase within days of any change. Every update requires manual editing.

### The Solution: Separate Facts from Narrative

**Two kinds of content in whitepapers:**
- Human-written narrative ("our approach uses fractional Kelly sizing to manage risk") — stays human-written
- Factual claims from codebase ("T=1.45", "4 assets", "Quarter-Kelly") — should be extracted from code automatically

**Pipeline:**
```
Codebase changes
      │
      ▼
extract_doc_facts.py  ──→  facts.json
      │
      ├──→  investor_template + facts  ──→  Investor PDF
      ├──→  technical_template + facts  ──→  Technical PDF
      └──→  readme_template + facts    ──→  README.md

Triggered by: deploy / on-demand / staleness warning
```

**Dynamic facts identified:**
- Lines of code count
- Assets traded and timeframes
- Model parameters (temperature values, blend weights, Kelly fraction)
- Architecture diagram components
- Infrastructure specs
- Feature list
- Performance metrics

**Staleness check:** CI step that diffs current codebase facts against last generated docs. Even without auto-regeneration, at least you know when docs are stale.

## Accuracy Drift Issues Found (March 2026 Audit)
- Bot size: docs said ~10,400 LOC, actual ~13,600
- SOL_TAKER_FIRST = True: not reflected in docs
- DIRECT_TAKER_THRESHOLD = 180s: docs said 60s or 75s
- Three-tier escalation timing (15s/7s/5s by STC band): not in docs
- Weather: docs didn't reflect NO ALPHA verdict (+32pp overconfidence)
- Sports: docs didn't reflect basketball-only signal (tennis killed)
- Kelly: 15M uses full Kelly (1.0), not quarter or third — docs were wrong
- Hourly uses quarter-Kelly (0.25), SPX uses eighth-Kelly (0.125)
- Firebase removed Mar 6 but still referenced in docs
- CalEngine progression and per-pipeline status: not current
- Decided Contract tiered risk for SOL: not in docs
- Addon system (price improvement live, dip shadow): not in docs

## Whitepaper CI Fix
GitHub Actions workflow for auto-generating whitepapers had been broken since Mar 6 — every auto-generate was failing silently. Fixed by updating the CI workflow (documentation-only change, no bot impact).

## Related (KB operational articles)
- (No direct kb/ counterpart)
