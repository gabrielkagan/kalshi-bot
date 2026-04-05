---
status: active
updated: 2026-03-28
tags: [research, sports, bayesian, basketball]
---
# Sports Comeback Model — Complete Research

Source: Multiple sessions Feb-Mar 2026
Chat links: https://claude.ai/chat/0b8111df-893e-4e55-91cb-c8822aa72a43, https://claude.ai/chat/6c324e1d-4062-4087-b43b-6ce92c9a2220, https://claude.ai/chat/d9de894a-639c-4dea-9eae-e0136950f6cc, https://claude.ai/chat/932a113a-5dd3-4041-92e9-88d7a78ff5f6, https://claude.ai/chat/30bee726-fd81-46e4-bbd7-e46673c60dc0, https://claude.ai/chat/f0f1b3f8-2ad3-4f53-9003-05fe30525372

---

## Core Thesis

Prediction markets overreact to in-game scoring events. When a pregame favorite falls behind, the market oversells their win probability relative to historical base rates. A Bayesian model using pregame odds as priors + historical likelihood ratios produces better-calibrated probabilities than the market's knee-jerk reaction.

## Academic Foundation

**Choi & Hui (2014):** Studying Betfair's in-play soccer market with thousands of EPL/La Liga matches, found that betting strategies placed 2 minutes after an underdog goal yield 2.79% returns. The overreaction corrects within approximately 6 minutes regardless of trading volume.

**Croxson & Reade (2014):** Confirmed that expected goals (favorite scoring) are priced efficiently and quickly, while surprising goals (underdog scoring against the favorite) produce exploitable mispricings.

**Beuoy (2013):** NBA logistic regression achieves Brier scores of 0.15-0.17 with minimal features (score diff, time remaining, home/away, pregame spread).

**Yeh et al. (2022):** ESPN model achieves similar Brier range with richer feature set.

## Model Architecture: Bayesian Updating with Pregame Odds as Prior

This was chosen as primary over logistic regression, XGBoost, and Markov models because it produces reasonable probabilities on day one by leveraging market-implied priors, requires no training data, and directly exploits the overreaction thesis.

### Model Comparison Matrix (from research)

| Model | Inputs | Impl. Effort | Brier Score | POC Ready? | Free Data? |
|---|---|---|---|---|---|
| Logistic regression | Score diff, time, home/away, pregame spread | ~150 LOC | 0.15-0.17 (NBA) | Yes | Yes |
| XGBoost/LightGBM | + non-linear interactions, rolling stats | ~400 LOC | 0.10-0.15 | Overfits at n<1000 | Yes |
| **Bayesian updating** | Pregame odds + event likelihood ratios | ~250 LOC | Prior-dependent | **Best for POC** | Yes |
| Survival/Poisson | Attack/defense rates, time, score | ~600 LOC | Soccer-specific | Moderate | Yes |
| State-space/Markov | Score-diff × time transitions | ~400 LOC | Granularity-dependent | Moderate | Yes |
| Elo + in-game | Elo ratings + score adjustment | ~200 LOC | ~0.20 (pregame only) | Yes (pregame) | Yes |

### Implementation

```python
# 1. Prior: Convert pregame Kalshi mid-price to implied probability
#    Example: pregame mid-price for "Lakers win" = 68c → P(Lakers win) = 0.68

# 2. Likelihood ratios from historical base rates
#    For each game state (score differential, time remaining):
#    LR = P(this score state | favorite eventually wins) / P(this score state | favorite eventually loses)
#    Stratify by 5-minute time bins × score-differential buckets

# 3. Posterior update after each scoring event:
prior_odds = prior_prob / (1 - prior_prob)
posterior_odds = prior_odds * likelihood_ratio
posterior_prob = posterior_odds / (1 + posterior_odds)
```

### Building the LR Lookup Table (from free historical data)

```python
import pandas as pd
from nba_api.stats.endpoints import leaguegamefinder

# 1. Pull 5 seasons of NBA games → build (score_diff, time_bin) × outcome table
# 2. For each (diff, time_bin), count: n_fav_wins, n_fav_losses
# 3. LR(diff, time_bin) = P(state | fav_wins) / P(state | fav_loses)
#    = (n_fav_wins_in_state / total_fav_wins) / (n_fav_losses_in_state / total_fav_losses)

# Soccer: use Dixon-Coles team-specific attack/defense parameters
# estimated from Football-Data.co.uk CSVs to compute Poisson-based LRs
```

### Key Insight from Choi & Hui
Markets overreact to surprising goals (underdogs scoring) and underreact to expected goals. A principled Bayesian model that updates proportionally to historical base rates will naturally be more conservative than the market after surprise events — this IS the edge.

## Calibration Methodology (for 300-500 shadow events)

- **Use Platt scaling** (logistic regression on held-out predictions, 2 parameters) over isotonic regression (non-parametric, overfits below ~1,000 samples)
- **Reliability diagrams** with 5-6 bins maximum (≥50 events per bin at n=300)
- **Expected Calibration Error (ECE):** target < 0.05
- **Brier score decomposition:** reliability (calibration error) + resolution (discriminative ability) − uncertainty (base rate variance)
- **Cross-validation:** time-series split only — train on months 1...k, test on month k+1. Never random k-fold for temporal sports data.
- **Temperature scaling** (single parameter T dividing logits) most practical for very small samples.
- Key references: Guo et al. (2017) on temperature scaling, Kull et al. (2017) on beta calibration, Niculescu-Mizil & Caruana (2005) on GBT calibration needs.

## Empirical Results (as of March 2026)

### Overall Shadow: 19,532 settled signals across all sports

### Per-Sport Breakdown

**Basketball:**
- 69.2% WR, Fisher significant at p=0.035
- Model Brier beats market Brier
- SPRT crossed: LLR 3.126 vs 2.94 boundary
- +$37.30 on 42 trades
- Filtered version (pregame ≥60%, ask ≤70c): 70% WR on 20 trades

**Two distinct populations within basketball:**
- **1-point deficit, strong favorites (pregame ≥65%):** 91.7% WR on n=12, +$43.18. Real signal — NBA team favored 70%+ pregame, down 1 in Q1, comes back almost every time. Market underprices it because scoreboard watchers see "losing" and sell.
- **2+ point deficit:** 56.5% WR on n=28, -$5.88. Noise. Market roughly efficient on larger deficits.

**Tennis:** 52.2% WR, 92 games, -$1.64. Model Brier WORSE than market. 57% of volume but PnL-negative. Killed.

**Hockey:** Coin flip. Killed.

**Soccer:** Only 5 games. Insufficient data. Killed.

### Calibration Gap
- Overall model +14.6pp overconfident across every bucket
- Systematic overbetting from overconfident probabilities
- Even if underlying signal is real, bad sizing from overconfident probabilities eats edge

### CLV (Closing Line Value)
- Overall: -27.3c average CLV with only 24.5% positive
- Means systematically buying after the line has already moved toward correct outcome
- Basketball-specific CLV needs isolation — tennis may be dragging the number

### Time-of-Game Analysis
- Time remaining >85%: 62.7% WR, significant at p<.01 — early-game comebacks are real
- Late-game: garbage
- H1/H2 drift: 45.6% → 63.3% (could be model improving from tweaks or split-half variance)

### Score-Change Signal
- 55.6% WR when scoring event just happened vs 12.5% otherwise, Fisher p=0.02
- Strong enough to consider as a hard gate — only enter when a scoring event just happened

## Final Filter Configurations

### NBA Core (high conviction)
- Pregame ≥65%
- Deficit ≤1 point
- Q1 only (time remaining >75%)
- Price ≤70c
- ~3 signals/week

### NBA Wide (broader collection)
- Pregame ≥65%
- Deficit ≤3 points
- Time remaining >25%

## Swisstony Analysis (Polymarket sports bot)

A viral Polymarket sports bot was analyzed for applicability. Key finding: swisstony's edge is speed-based (getting stadium API data before the market). That works on Polymarket where counterparty is retail. On Kalshi, competing against institutional market makers with co-located infrastructure.

**What to take from swisstony:** The empirical confirmation that prediction market sports pricing is exploitable by bots with faster data processing. Validates the comeback thesis. The overreaction window is real.

**What NOT to take:** Don't pivot to latency arbitrage. The bot's edge on Kalshi is analytical superiority (better probability estimates) not speed superiority.

**Added monitoring:**
```python
# For every game tracked, also log:
yes_ask = get_kalshi_orderbook(favorite_market)["best_ask"]
no_ask = get_kalshi_orderbook(favorite_market)["best_no_ask"]
arb_gap = 1.00 - (yes_ask + no_ask)  # positive = free money
```
If consistent arb gaps exist during live games (even 1-2c after fees), that's a zero-risk overlay on top of directional comeback strategy.

## Data Sources
- **The Odds API:** Free tier, pregame and live odds from multiple bookmakers including Pinnacle no-vig lines
- **ESPN live scores:** Real-time score data for signal generation
- **nba_api:** Historical NBA data for LR lookup table construction
- **Football-Data.co.uk:** Historical soccer results for Dixon-Coles parameter estimation

## Open Questions
- NBA Core generates ~3 signals/week — is volume sufficient for meaningful allocation?
- Playoff dynamics may differ from regular season (starts mid-April)
- Basketball-specific CLV needs isolation from tennis drag
- At n=42, one bad week drops LLR back below threshold — need n=60+ before live capital

## Related (KB operational articles)
- [[kb/concepts/sports-engine.md]]
- [[kb/concepts/cal-engine-registry.md]]
