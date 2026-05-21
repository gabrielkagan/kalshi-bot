---
clickup: 86ba1wrh7
parent_umbrella: 86ba1wrad
filed: 2026-05-21
status: plan
---

# Bit S.2 — RCA: BNB/HYPE/DOGE PnL vs Coinbase staleness

## Discipline checklist

1. **Numbers first.** Per CLAUDE.md "Answer first, plan later" for investigations.
2. **Read-only.** No code change to bot. Read against `state.db` (VPS MCP) and Coinbase public REST. Mac-side script under `scripts/research/`.
3. **Kelly-sized PnL.** `settled_trades.pnl_cents` is already Kelly-sized from production. Per `scripts/CLAUDE.md`: must net fees via `SUM(pnl_cents - COALESCE(fee_cents, 0))`.
4. **LEFT JOIN phantom_corrections** per `feedback_use_corrected_pnl_always.md` — local fill ledger drifts from Kalshi truth.
5. **Wilson CI** on win rates when n<200.
6. **Honest-NULL** small-n buckets. BNB n=9 currently; will under-power any per-bucket inference. Don't paper over.

## Question

For each asset (BNB, HYPE, DOGE) since T4 promotion: does PnL trend negative as proxy staleness at decision time increases?

Proxy = how far before the decision was the last Coinbase REST 1-min candle for that asset. (We don't have WS staleness yet — that's S.1's job. REST gaps ≈ trade-less minutes ≈ WS staleness, but not identical.)

## Method

### Step 1: Pull settled-trade decision times

```sql
SELECT
  st.ticker, st.asset, st.product_type, st.settled_at,
  st.pnl_cents, st.fee_cents,
  st.entry_price_cents, st.count,
  st.calibrated_prob, st.edge,
  pc.delta_count, pc.delta_pnl_cents,
  eo.evaluation_time, eo.filter_stage, eo.spot_price
FROM settled_trades st
LEFT JOIN phantom_corrections pc ON st.ticker = pc.ticker
LEFT JOIN evaluated_opportunities eo
  ON eo.ticker = st.ticker
  AND eo.filter_stage LIKE 'decided_contract%'
WHERE st.asset IN ('BNB','HYPE','DOGE')
  AND st.product_type = '15m'
  AND st.settled_at >= '2026-05-14'  -- HYPE/DOGE T4; BNB T4=2026-05-19
ORDER BY st.asset, st.settled_at;
```

Note: an `evaluation_time` may have multiple matching rows; join may multiply. Dedupe on `(ticker, filter_stage)` and pick earliest `evaluation_time` per match.

### Step 2: For each (asset, evaluation_time), pull Coinbase candles

For each unique decision time, fetch Coinbase REST candles for the asset's product (`{asset}-USD`) over `[eval_time - 600s, eval_time]`. Find the latest candle with `candle_open_ts <= eval_time`. Compute `proxy_staleness_s = eval_time - candle_open_ts`.

If no candle in the 10-min lookback: stale ≥ 600s; bucket as "extreme".

Rate-limit courtesy: 100ms between requests; batch by asset.

### Step 3: Bucket + aggregate

Buckets: [0-30s, 30-120s, 120-300s, 300-600s, ≥600s].

Per (asset, bucket):
- n trades
- net PnL = `SUM(pnl_cents - COALESCE(fee_cents,0))` over corrected counts
- mean PnL per trade
- W-L-T at settlement (market_result = 'yes' counts as win for YES-side; mirror for NO)
- Wilson CI on win rate
- mean |edge| at entry
- mean staleness within bucket

### Step 4: Slope test

For each asset:
- Pearson correlation: `proxy_staleness_s` vs `pnl_cents_net`
- Spearman rank correlation (more robust to outliers)
- 95% CI via bootstrap (1000 resamples)

If slope is significantly negative → staleness costs us. Document the asset-specific slope magnitude.

### Step 5: Cross-check vs phantom_corrections

If a ticker has phantom_corrections rows with `|delta_count| > 0`, that trade's PnL is uncertain. Report (a) all-trades-included PnL, (b) phantom-corrected PnL. Don't conflate measurement noise (ghost fills) with model noise (stale spot).

## Deliverables

1. `scripts/research/spot_staleness_pnl_attribution.py` — read-only, idempotent, takes `--asset {BNB,HYPE,DOGE,all}`. Outputs a markdown table + CSV per asset.
2. `kb/findings/spot-staleness-pnl-attribution.md` — verdict doc with:
   - Per-asset bucket table
   - Slope tests
   - Phantom-corrected vs raw PnL comparison
   - Recommendation for S.3 threshold per asset
   - Confidence statement (esp. low-n BNB)

## Acceptance

- Per-asset bucket table populated; small-n buckets flagged
- Slope test reported per asset
- Finding doc filed
- S.3 plan doc cross-links the per-asset threshold recommendation

## Caveats up front

- BNB n=9 today → very low power. May need to wait + re-run.
- HYPE/DOGE n=132-237 → moderate power; can split into 4 buckets at most.
- REST staleness is a PROXY for WS staleness. Sign should be the same but magnitudes may differ.
- Settlement randomness (binary outcomes on 15m markets) dwarfs single-trade staleness signal at small n. The test is whether AGGREGATE staleness correlates with AGGREGATE drift, not per-trade attribution.

## Followup if signal exists

- File ticket: "Phase 2 staleness recovery" — re-evaluate trades placed during high-staleness windows for whether the post-decision price move went *against* the bot's prediction (causation evidence, not just correlation).
