---
name: sports-alpha
description: "Deep sports comeback alpha research — SPRT sequential test, deficit analysis, per-sport breakdown, CLV, calibration. Use when: \"sports deep dive\", \"sports alpha research\", \"is sports ready?\", \"how are comebacks doing?\", \"sports shadow analysis\""
---

# Sports Comeback Alpha Analyzer

Systematic alpha research on sports comeback prediction market data. Discovers profitable configurations by sport group, league, deficit, timing, and pregame strength. Validates robustness with Wilson CIs, Fisher tests, SPRT, and time stability.

## Usage
```
/sports-alpha
/sports-alpha fresh    # Force fresh DB copy from VPS
```

## Steps

1. **Sync the database.** Follow `.claude/skills/references/db-sync.md` to sync the database. If user says "fresh", always re-sync regardless of cache age.

2. **Run the existing audit first** (for context on data quality and settlement gaps):
   ```bash
   python3 scripts/sports_shadow_audit.py --db /tmp/state.db --regime auto 2>&1 | head -100
   ```

3. **Run the alpha research script**:
   ```bash
   python3 scripts/sports_alpha_research.py --db /tmp/state.db 2>&1
   ```
   Or with regime filter:
   ```bash
   python3 scripts/sports_alpha_research.py --db /tmp/state.db --regime auto 2>&1
   ```
   Or focused on one sport:
   ```bash
   python3 scripts/sports_alpha_research.py --db /tmp/state.db --sport-group basketball 2>&1
   ```

4. **Present findings** with focus on:
   - Has the verdict changed since last run? (STRONG ALPHA / ALPHA / MARGINAL / NO ALPHA)
   - Which sport groups are profitable vs losing?
   - Top robust configurations (PnL>0, CI_lo>50%, PF>1.2)
   - SPRT convergence status (per group and overall)
   - Key calibration issues (overconfident / underconfident per group)

5. **Track evolution** — note whether:
   - Any sport group SPRT has converged to a decision
   - CLV is positive (entry price < closing price = real edge)
   - Time stability holds (H1 vs H2 drift < 10pp)
   - Specific deficit sizes or time windows show concentrated alpha

## SPRT — WHY and how to read it

**What is SPRT?** Sequential Probability Ratio Test — a statistical method that says "keep collecting data" until there's enough evidence to ACCEPT or REJECT a hypothesis. Unlike fixed-sample tests (which require choosing n upfront), SPRT adapts: it can stop early when evidence is overwhelming, or keep going when results are ambiguous.

**Why SPRT for sports?** Sports shadow data arrives slowly (a few games per day). Fixed-sample testing would require waiting months for a predetermined n. SPRT lets us make a decision as soon as the evidence is strong enough, which could be 50 games or 500 games.

**How to read SPRT output:**

| SPRT Decision | Log-Likelihood Ratio (LLR) | Meaning |
|---------------|---------------------------|---------|
| REJECT_H0 | LLR > upper boundary (typ. 2.94) | Confirmed edge — the strategy wins more than chance. Safe to promote. |
| ACCEPT_H0 | LLR < lower boundary (typ. -2.94) | No edge — the strategy is not better than coin flip. Kill it. |
| CONTINUE | Between boundaries | Inconclusive — need more data. Keep shadow running. |

**WHY boundaries at ±2.94?** This corresponds to Type I and Type II error rates of α=β=0.05 (5%). The boundary is `ln((1-β)/α)` = `ln(0.95/0.05)` = 2.94. Tightening to α=β=0.01 would require ±4.60, meaning much more data before a decision. 5% error rate balances speed vs accuracy for our use case.

**WHY test against 50% (coin flip)?** The null hypothesis is "the model's comeback predictions are no better than chance." If the model can't beat 50% WR on games it flags as comebacks, it has no edge. Note: this is for the game-level outcome (did the team come back?), not the market-level outcome (which depends on price).

## Key Metrics to Watch
- **Overall game WR** and 95% Wilson CI lower bound (need > 50% — if CI lower bound is below 50%, we can't reject the null)
- **Per-sport-group SPRT decisions** (REJECT_H0 = confirmed edge for that sport)
- **Profit Factor** per group (> 1.3 credible, > 1.5 strong — WHY 1.3? Below 1.3, transaction costs and fee variance can easily flip PnL negative. 1.3 gives ~30% gross margin over breakeven.)
- **Model Brier vs Market Brier** (model should beat market — if it doesn't, the market already prices comebacks correctly and there's no edge)
- **CLV average** (positive = genuine information advantage — we're buying before the market catches up)
- **Robust config count** from grid search

## Interpretation Guide
- **STRONG ALPHA**: 5+/6 checks pass (WR>55%, PnL>0, CI_lo>50%, SPRT rejects H0)
- **ALPHA DETECTED**: 4/6 checks pass
- **MARGINAL ALPHA**: 3/6 checks pass with positive PnL
- **NO ALPHA**: <3 checks pass or negative PnL
- **Time stability**: H1/H2 WR within 10pp = stable (WHY 10pp? At n~50 per half, standard error is ~7pp. A 10pp difference is ~1.4σ, not significant. Above 10pp starts to suggest regime drift.)
- **Fisher p < 0.05**: Statistically significant difference between groups

## Script Sections (16 total)
1. Data Overview + verdict
2. Sport Group analysis (WR, PnL, PF, Brier per group)
3. League analysis (per-league breakdown)
4. Deficit analysis (1pt vs 2pt vs 3pt+ comebacks)
5. Time remaining (optimal entry timing)
6. Pregame favorite strength (does it predict comeback?)
7. LR scale analysis (model sensitivity parameter)
8. Price analysis (WR by entry price band with breakeven)
9. Signal quality / counterfactual (live vs hypothetical thresholds)
10. Pregame capture method impact
11. SPRT sequential test (overall + per group convergence)
12. Calibration (predicted vs actual, Brier scores)
13. Game flow (score changes, period patterns)
14. Robustness (Wilson CIs, Fisher tests, time stability)
15. Optimal configuration grid search
16. Closing Line Value analysis
+ Executive summary with checklist and sport group ranking

## Error Handling

| Situation | Action |
|-----------|--------|
| `sports_shadow_log` table is empty | Sports engine may not be running or no games are in progress. Check: `ssh botuser@45.55.181.30 "journalctl -u kalshi-bot --no-pager -n 50 \| grep -i sport"`. Report: "No sports shadow data — engine may not be active or no games currently." |
| Script errors with `no such column` | Schema may have changed. Run `PRAGMA table_info(sports_shadow_log)` and report the mismatch. |
| Very few settled rows (n < 20) | Report all metrics but add **"LOW SAMPLE — all conclusions are provisional"** warning on every finding. Don't make promotion recommendations. |
| SPRT says REJECT_H0 but PnL is negative | This means the model predicts comebacks correctly (WR > 50%) but the trades are priced too expensively (buy high, win small). The edge is in prediction, not in trading. Report: "Model has predictive edge but market pricing absorbs it. Check price tier analysis." |
| One sport group dominates the results | Common — basketball may be 80% of data. Always present per-group breakdowns. A strategy that works for basketball but fails for hockey is not a general sports strategy. |
| Script output is very long (>500 lines) | Show the executive summary and section headers. Offer to show specific sections on request rather than dumping everything. |
