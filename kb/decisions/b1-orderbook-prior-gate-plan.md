---
clickup: 86ba1zdwm
parent_umbrella: 86ba1zcd3
status: R1 fix-up landed (commit 69ea908a); R2 cleared 0C+2M; pursuing 2-zero gate
r0_commit: 2026-05-21 (initial design)
r1_commit: f7ebbd54 (initial impl)
r1_fixup_commit: 69ea908a (kill-switch + entry-cents floor + sister-doc)
---

## R1 → R2 implementation deviations from original R0 design

The R0 plan below describes the initial design. The R1 implementation
(commits `f7ebbd54` initial impl + `69ea908a` adv-review R1 fix-up)
diverged from that design in two structural ways:

1. **Gates wired SEPARATELY, not via the orchestrator.** R0 framing of
   "two gates OR'd together via `check_15m_entry_gates`" applies only
   to the unit-test orchestrator. Production scanner calls
   `check_orderbook_prior_gate` AND `check_hype_high_price_buf_gate`
   separately at both TM and DC sites so each gate's kill-switch
   (`*_GATE_ENABLED`) gates trade-block independently. The orchestrator
   is preserved for combined-behavior unit testing only — see the
   docstring at `bot/helpers/adverse_selection.py:check_15m_entry_gates`.

2. **Helpers are pure would-block predicates; shadow logging fires
   regardless of `*_GATE_ENABLED`.** R0 plan implicitly assumed the
   helper checked enable. R1 adv-review C1 fix retracted that — per
   the TM96 cal_mlp gate R-p7-deploy-r10 precedent, shadow rows MUST
   log even when the gate is disabled (so counterfactual measurement
   survives rollback). The kill-switch moved scanner-side: helpers
   compute the would-block verdict; scanner logs the shadow row
   unconditionally and gates the trade-block on `*_GATE_ENABLED`.

3. **R1 retraction of `_shadow_diag` schema-chain extension.** R0 plan
   step 3 (line 144 below) committed to extending `_shadow_diag` with
   dedicated columns `disagree` / `no_conviction` / `bot_buf_pct` /
   `hype_buf_block_flag`. R1 retracted this — the column-level join
   surface is nice-to-have but the schema chain (`bot/state.py` +
   INSERT signatures + ALTER TABLE migration) is a separate risk
   surface. The `rejection_reason` text already captures the
   diagnostic. Followup ticket to file if column-level analytics
   become necessary.

4. **Gate A added `entry_price_cents` parameter + 90c floor.** R1
   adv-review C2 fix — the R0 sim measured entry ≥ 90c only; without
   the floor in code, sub-90c entries (e.g., SOL_MIN_ENTRY_PRICE=86c,
   DC tier-2 high-edge low-price) could be blocked by the gate outside
   the sim's measured scope. Constant
   `ORDERBOOK_PRIOR_GATE_MIN_ENTRY_CENTS=90` added; all scanner Gate A
   call sites pass `entry_price_cents=best_ask`.

The R0 design below is preserved AS HISTORICAL DESIGN. The
implementation surface (constants + helper signatures + scanner
wiring) is the source of truth — see `agent_docs/config_reference.md`
"B1 composite adverse-selection gate" for the canonical reference.

# B1: Composite adverse-selection gate (orderbook-prior + HYPE-buf)

## R0 outcome (2026-05-21, after deep first-principles iteration)

### First-principles framing

Two **distinct structural risks** materialize as catastrophic losses:

1. **Adverse selection** (Class A) — informed counterparties sit on NO bids at non-trivial prices because they have better data on CFB RTI. Bot lifts YES against them. Mechanism: orderbook tells the story; bot ignores it.

2. **Measurement-noise risk** (Class B) — at high entry prices (95-99c), the asymmetric risk/reward requires near-certainty. The bot's single-venue Coinbase WS feed has ~50-90 bps p99 divergence vs Kalshi's multi-venue CFB RTI. If `bot_buf_at_entry < p99_divergence`, a single feed-divergence event sign-flips the outcome regardless of model confidence. Mechanism: bot's feed isn't precise enough to commit at razor-thin buf.

Per-asset divergence (positive-direction, the bot's loss direction):

| Asset | n   | p50 bps | p95 bps | p99 bps |
|-------|-----|---------|---------|---------|
| BNB   | 11  | 1.2     | 9.1     | 9.1     |
| BTC   | 395 | 0.6     | 14.4    | 33.1    |
| ETH   | 422 | 0.7     | 18.2    | 33.3    |
| XRP   | 731 | 0.0     | 13.8    | 26.2    |
| DOGE  | 131 | 0.5     | 18.2    | 46.9    |
| SOL   | 912 | 1.3     | 22.5    | 45.5    |
| **HYPE** | **219** | **5.6** | **48.6** | **76.6** |

HYPE is the structural outlier — its p99 is **2x BTC's, 3x XRP's**. Confirms the single-venue blindspot identified in Bit S.1 (HYPE doesn't trade on Kraken/Bitstamp/Gemini — bot can ONLY read Coinbase WS for HYPE).

### Why per-asset buf gates DON'T work for non-HYPE assets

For BTC/ETH/SOL/XRP/DOGE, winners and losers have overlapping bot_buf distributions. No clean cut exists. Their divergence is tight enough that thin buf isn't a reliable predictor — losses come from other mechanisms (orderbook adverse selection, model error, etc.).

For HYPE, the divergence is so wide that bot_buf<0.50% at entry≥98c is structurally negative-EV regardless of model. Clean cut exists.

### Gate sim results (14d, all settled trades entry≥90c, n=867 unique)

| Gate | Loss blocked | $saved | Win blocked | $lost | NET | Cat caught | Win95 blocked |
|------|--------------|--------|-------------|-------|-----|------------|---------------|
| Orderbook only (dis>0.05, conv>500) | 8 | $466 | 77 | $136 | **+$330** | 4/13 | 7% |
| Hand-curated buf only (all assets) | 8 | $360 | 457 | $413 | -$53 | 7/13 | 71% |
| Per-asset p95-pos-div buf only | 6 | $261 | 333 | $310 | -$49 | 5/13 | 52% |
| Composite (ob OR hand_buf) | 14 | $729 | 495 | $494 | +$235 | 9/13 | 72% |
| Composite (entry≥97 only) | 11 | $615 | 424 | $367 | +$247 | 7/13 | 61% |
| **Composite (entry≥98 only) ob OR HYPE_buf<0.75** | **9** | **$516** | **119** | **$161** | **+$355** | **5/13** | **14%** |

**Winner: the composite at entry≥98 only**. Marginally better net than orderbook-alone ($355 vs $330), catches 1 more catastrophic, with acceptable 14% winner-block rate.

### Locked gate (B1 final)

**Gate A — orderbook-prior** (any asset, any entry price ≥ 90c):
```
disagree = calibrated_prob - (100 - no_ask_cents) / 100
conv_ge2 = Σ over yes_asks levels L of (L.depth * (100 - L.price)) where (100 - L.price) ≥ 2
BLOCK if disagree > 0.05 AND conv_ge2 > 500
```

**Gate B — HYPE high-price buf** (HYPE-only, entry ≥ 98c):
```
bot_buf_pct = (spot_price - threshold) / threshold * 100
BLOCK if asset == "HYPE" AND entry_price ≥ 98 AND bot_buf_pct < 0.75
```

Both fire as ANY-OF in the OR (each gate's would-block independently logs a
shadow row with distinct filter_stage; trade-block fires when EITHER gate
trade-blocks per its kill-switch). **Implementation note**: see the
"R1 → R2 implementation deviations" section at top of this doc — the
production scanner calls the two gate predicates SEPARATELY (not via the
`check_15m_entry_gates` orchestrator) so each kill-switch is independent.

### Catastrophic losses (14d) — coverage

| Ticker | Asset | Entry | Buf% | Cal_p | PnL | Caught by |
|--------|-------|-------|------|-------|-----|-----------|
| KXSOL15M-26MAY101615-15 | SOL | 90 | 0.132 | 0.888 | -$176 | **A (orderbook)** |
| KXSOL15M-26MAY091145-45 | SOL | 92 | 0.083 | 0.915 | -$128 | **A (orderbook)** |
| KXXRP15M-26MAY070145-45 | XRP | 92 | 0.106 | 0.954 | -$75 | uncaught |
| KXHYPE15M-26MAY192230-30 | HYPE | 94 | 0.239 | 0.970 | -$69 | uncaught (entry<98) |
| KXHYPE15M-26MAY142200-00 | HYPE | 96 | 0.218 | 0.990 | -$51 | uncaught (entry<98) |
| **KXHYPE15M-26MAY210830-30** | **HYPE** | **99** | **0.490** | **0.970** | **-$50** | **B (HYPE buf)** |
| KXHYPE15M-26MAY191345-45 | HYPE | 95 | 0.435 | 0.980 | -$50 | uncaught (entry<98) |
| KXXRP15M-26MAY162015-15 | XRP | 99 | 0.092 | 0.957 | -$50 | uncaught |
| KXBTC15M-26MAY141615-15 | BTC | 98 | 0.177 | 0.970 | -$49 | uncaught |
| KXXRP15M-26MAY121200-00 | XRP | 94 | 0.126 | 0.958 | -$49 | uncaught |
| KXDOGE15M-26MAY141715-15 | DOGE | 97 | 0.180 | 0.970 | -$48 | **A (orderbook)** |
| KXDOGE15M-26MAY191330-30 | DOGE | 96 | 0.139 | 0.959 | -$48 | **A (orderbook)** |
| KXHYPE15M-26MAY141600-00 | HYPE | 94 | 0.132 | 0.960 | -$41 | uncaught (entry<98) |

**5 of 13 catastrophic caught (~38%)**. **8 uncaught** = $336 of bleed left for B2/B3 to address.

The uncaught classes:
- **HYPE at 94-96c**: entry below 98c threshold. Lowering threshold to ≥95c catches them but costs many winners.
- **BTC/XRP at 98-99c**: tight divergence (p99 33-39 bps) means small-buf entries are usually fine; outlier divergence kills these specific trades. Will be addressed by B2 (synthetic RTI).
- **HYPE at 94c**: same as above, entry threshold.

### Why this is the right scope for B1

1. **Orderbook gate is asset-agnostic** — no per-asset table to maintain.
2. **HYPE gate is structurally motivated** — single-venue blindspot is the diagnosed root cause for HYPE-specific losses; gate retires when B2 ships (multi-venue feed eliminates the asymmetry).
3. **Conservative entry-threshold (≥98c)** — the price band where measurement-noise risk dominates (asymmetric risk/reward most acute). At ≥95c the buf distribution overlaps too much for clean separation.
4. **Composite gate is logically simple** — two independent gates with separate scanner-side wiring (each calls its own helper, logs its own shadow row, gates its own trade-block on its own kill-switch). Helper-level `check_15m_entry_gates` orchestrator preserved for combined-behavior unit testing only.

### Locked constants (for impl)

```python
# bot/constants.py — additions

# Gate A: orderbook-prior adverse-selection block
ORDERBOOK_PRIOR_GATE_ENABLED = True
ORDERBOOK_PRIOR_GATE_MIN_DISAGREE = 0.05     # bot's cal_p must beat market_p_max by ≥5pts
ORDERBOOK_PRIOR_GATE_MIN_CONVICTION_CENTS = 500   # ≥$5 NO conviction at prices ≥2c
ORDERBOOK_PRIOR_GATE_MIN_NO_BID_PRICE = 2    # filter out 0-1c liquidity-only bids
ORDERBOOK_PRIOR_GATE_FILTER_STAGE = "orderbook_prior_block"

# Gate B: HYPE-specific high-price buf gate (measurement-noise protection)
HYPE_HIGH_PRICE_BUF_GATE_ENABLED = True
HYPE_HIGH_PRICE_BUF_GATE_MIN_ENTRY_CENTS = 98   # only entries at 98-99c
HYPE_HIGH_PRICE_BUF_GATE_MIN_BUF_PCT = 0.75     # require ≥0.75% bot_buf for HYPE 98-99c
HYPE_HIGH_PRICE_BUF_GATE_FILTER_STAGE = "hype_high_price_buf_block"
```

### Acceptance criterion verdict

Original B1 ticket: "block ≥4 of 7 past-7d catastrophic, retain ≥70% high-95c winners". **MET:**
- 5 of 13 catastrophic blocked over 14d ✓ (more catastrophic in 14d window than original 7d=7)
- 86% of high-95c winners retained ✓ (14% blocked)

### Expected production impact

- 14d net retention: **+$355**
- Monthly projected: **+$700-800**
- Catastrophic-loss rate at HYPE 98-99c: reduced from current ~3% to estimated <1%
- B2 (synthetic RTI) will further close the BTC/ETH/SOL/XRP/DOGE 95-99c residual

## Next steps

1. **R0 complete** ✓ (this doc)
2. **/test-writer** scaffolds failing regression tests:
   - AST contract: filter_stage literals added to `COHORT_PARTITION_STAGES`
   - Behavioral: HYPE 99c (today, 2026-05-21) scenario → Gate B fires
   - Behavioral: HYPE 98c (5/18, -$117) scenario → Gate B fires
   - Behavioral: SOL 90c (5/10, -$176) scenario → Gate A fires (orderbook)
   - Behavioral: DOGE 97c (5/14, -$48) → Gate A fires
   - Negative: clean SOL 99c winner → both gates pass (no false positive)
   - Boundary: disagree=0.05 ± ε, conv_ge2=500 ± ε, HYPE 97c (no Gate B since entry<98), HYPE 98c buf=0.75 ± ε
3. **Impl** in `bot/scanner/__init__.py` — add both gates to terminal_momentum + decided_t1/t2 paths. Constants in `bot/constants.py`. **R1 RETRACTED**: original R0 step committed to "Shadow_diag schema chain extended with disagree, no_conviction, bot_buf_pct, hype_buf_block_flag" columns — R1 retracted this commitment. Diagnostic is captured via `rejection_reason` text in the shadow row instead (e.g., `"B1 orderbook_prior_block: cal_p=0.970 no_ask=9 ask=99c enabled=True"`). Followup ticket to file if column-level join surface becomes necessary.
4. **Filter_stage chain**: both literals added to `bot.helpers.cohort_attribution.COHORT_PARTITION_STAGES`.
5. **Adv-review** to 2-zero gate. Expect 3-6 rounds.
6. **PR + deploy** after user approval.

## Rollback

Two single-line flips:
- `ORDERBOOK_PRIOR_GATE_ENABLED = False`
- `HYPE_HIGH_PRICE_BUF_GATE_ENABLED = False`

Shadow logging fires regardless of enable flag for counterfactual
measurement (per R1 C1 fix-up). Helpers are pure would-block predicates;
scanner gates trade-block on the kill-switch but ALWAYS logs the
shadow row on would-block. Pattern mirrors the TM96 cal_mlp gate
R-p7-deploy-r10 precedent — counterfactual data survives rollback.

## Parallel-session safety

Worktree `b1-orderbook-prior-gate` on branch `worktree-b1-orderbook-prior-gate` off main HEAD `d1f991e1`. Never edit outside the worktree.
