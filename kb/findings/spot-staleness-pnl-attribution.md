---
clickup: 86ba1wrh7
parent_umbrella: 86ba1wrad
filed: 2026-05-21
status: finding
plan: kb/decisions/bit-s-2-spot-staleness-rca-plan.md
script: scripts/research/spot_staleness_pnl_attribution.py
artifacts: data/research_cache/spot_staleness/_results/
---

# S.2 — Spot-staleness vs PnL attribution (BNB / HYPE / DOGE since T4)

## Verdict — DATA INSUFFICIENT for S.3 threshold per asset; DEFER

**Headline:** proxy staleness at decision time tops out at **119s** across
**379 trades** (BNB n=9, HYPE n=238, DOGE n=132). **Zero trades** fall in
the 120-300s, 300-600s, or ≥600s buckets — the exact buckets where a
staleness gate would fire. We can neither **confirm** nor **refute** that
high-staleness windows degrade EV, because the bot already self-selects
out of those windows by the time a settled trade exists.

This is a **measurement-design fact**, not a model failure of the script:
the REST-gap statistic that motivated S.2 (BNB 34% / HYPE 11% / DOGE 1%
trade-less minutes) measures the **unconditional minute universe**; the
proxy-staleness-at-decision distribution measures the **conditional**
distribution at moments the bot acted. Coinbase trade-less minutes do
not propagate WS price ticks; without a WS tick the bot's spot cache
does not refresh; in the absence of a price refresh the engines often
do not generate a new prob/edge that would clear the entry filter, so a
candidate row never enters. **The staleness gate is implicitly present
already, via Coinbase WS quiescence + cache-staleness on the engine
inputs.** S.3 would add an EXPLICIT gate; this RCA cannot demonstrate
that the explicit gate would catch any trade the implicit gate misses.

Verdict per asset:

| Asset | n | net PnL (raw) | net PnL (phantom-corrected) | Pearson r [95% CI] | Decision |
|---|---:|---:|---:|---|---|
| BNB | 9 | $+5.17 | $+4.71 | -0.212 [-0.796, +0.471] | DEFER — n insufficient |
| HYPE | 238 | $-284.61 | $-179.76 | -0.052 [-0.184, +0.091] | DEFER — no high-stale data |
| DOGE | 132 | $+37.01 | $+25.59 | -0.017 [-0.128, +0.127] | DEFER — no high-stale data |

All three asset-level Pearson + Spearman CIs straddle zero. Sign is
weakly negative for BNB (small n), essentially zero for HYPE/DOGE.

## Method (per plan-doc `bit-s-2-spot-staleness-rca-plan.md`)

1. Sync production state.db (Mac /tmp/state.db via SCP of main + WAL +
   SHM in a single connection; mtime 2026-05-21T10:06 UTC).
2. Per asset, pull settled_trades (T4 window: BNB ≥ 2026-05-19, HYPE +
   DOGE ≥ 2026-05-14) + LEFT JOIN `phantom_corrections` deduped on
   `(ticker, side)` via `ROW_NUMBER() OVER (... ORDER BY detected_at DESC)`
   so multiple audit runs don't double-count.
3. Phantom-delta-multiplication guard: `phantom_corrections.delta_pnl_cents`
   is ticker-level, but `settled_trades` has 1 row per fill. Apply the
   delta only to the EARLIEST `settled_trades` row per `(ticker, side)`
   via the `st_anchor` CTE — first run pre-fix multiplied HYPE phantom
   by 2-fill rows, doubling the correction from $+105 → $+210. Post-fix
   numbers above use the corrected single-application.
4. Decision time per ticker = MIN(evaluation_time) over `filter_stage =
   'candidate'` OR `filter_stage LIKE 'decided_contract%'`. Initial spec
   used only `decided_contract*` and missed 7 of 9 BNB rows because BNB
   T4 trades route through mainline (no STC cell-block hit), so the
   `candidate` row is the only decision-time marker. Widened CTE → 0/9
   BNB fallbacks.
5. Per-ticker Coinbase REST 1-min candles fetched in 300-min chunks,
   cached to disk for idempotency (`data/research_cache/spot_staleness/<product>/<chunk_anchor>.json`).
   `proxy_staleness_s = decision_epoch - latest_candle_open_ts` where
   `latest_candle_open_ts <= decision_epoch` within a 10-min lookback.
6. Buckets: [0-30s, 30-120s, 120-300s, 300-600s, ≥600s] per plan.
7. Per-bucket: n, W-L (YES wins on result=yes; NO wins on result=no),
   Wilson 95% CI, mean |edge|, mean staleness, net PnL (gross −
   `fee_cents`) raw + phantom-corrected. Per-asset Pearson + Spearman
   correlation of `proxy_staleness_s` vs `net_pnl_cents` with 1000-iter
   bootstrap 95% CI (deterministic LCG, seed=42).

## Per-asset detail

### BNB (since 2026-05-19 T4, n=9)

- **WR 100% across the entire sample** (9-0). Honeymoon, low-n.
- All decisions cluster in 0-30s (n=6) + 30-120s (n=3).
- Staleness distribution (s): min=4, p50=15, p75=36, p95=66, max=66.
- Pearson r = −0.212 [−0.796, +0.471]; CI dominated by n=9.
- Phantom delta = −$0.46 (1 phantom row).
- **Per-asset decision: DEFER S.3 threshold; revisit at n ≥ 50.**

| Bucket | n | net PnL (raw) | net PnL (corr) | WR (Wilson) | mean stale (s) |
|---|---:|---:|---:|---|---:|
| 0-30s | 6 | $+3.47 | $+3.01 | 100.0% [61.0%, 100.0%] | 9.7 |
| 30-120s | 3 | $+1.70 | $+1.70 | 100.0% [43.8%, 100.0%] | 50.3 |
| 120-300s | 0 | — | — | — | — |
| 300-600s | 0 | — | — | — | — |
| ≥600s | 0 | — | — | — | — |

### HYPE (since 2026-05-14 T4, n=238)

- WR 94.7% / 91.0% across the two populated buckets — both very high;
  CI overlap is substantial (0-30s [90.3%, 97.2%] vs 30-120s [81.8%,
  95.8%]).
- Mean PnL drops slightly from raw 0-30s (−$1.10) to 30-120s (−$1.44),
  but phantom-corrected the gap narrows (−$0.54 vs −$1.31). Net sign on
  the slope is weakly negative but CI straddles zero (Pearson r =
  −0.052 [−0.184, +0.091]).
- Staleness distribution (s): min=0, p50=15, p75=33, p95=56, max=119.
- 20 phantom rows; phantom delta +$104.85 (raw -$284.61 → corrected
  -$179.76).
- 3 fallback-decision-time rows (no `candidate`/`decided_contract*` in
  the eval table — flag for separate orphan investigation).
- **Per-asset decision: DEFER — staleness exposure too compressed to
  estimate the EV cliff S.3 would protect.**

| Bucket | n | net PnL (raw) | net PnL (corr) | WR (Wilson) | mean stale (s) |
|---|---:|---:|---:|---|---:|
| 0-30s | 171 | $-187.81 | $-91.81 | 94.7% [90.3%, 97.2%] | 9.2 |
| 30-120s | 67 | $-96.80 | $-87.95 | 91.0% [81.8%, 95.8%] | 47.1 |
| 120-300s | 0 | — | — | — | — |
| 300-600s | 0 | — | — | — | — |
| ≥600s | 0 | — | — | — | — |

### DOGE (since 2026-05-14 T4, n=132)

- WR 95.5% / 93.0% across the two populated buckets; Wilson CIs
  overlap.
- Mean PnL drops from raw 0-30s (+$1.13) to 30-120s (−$1.48), and
  phantom-corrected (+$1.05 vs −$1.58). The sign of the bucket
  difference is the same in raw and corrected, but Pearson r =
  −0.017 [−0.128, +0.127] — CI straddles zero, no slope conclusion.
- Staleness distribution (s): min=0, p50=19, p75=37, p95=55, max=65.
- 10 phantom rows; phantom delta −$11.42 (raw $+37.01 → corrected
  $+25.59).
- **Per-asset decision: DEFER — same compression caveat as HYPE.**

| Bucket | n | net PnL (raw) | net PnL (corr) | WR (Wilson) | mean stale (s) |
|---|---:|---:|---:|---|---:|
| 0-30s | 89 | $+100.65 | $+93.52 | 95.5% [89.0%, 98.2%] | 11.0 |
| 30-120s | 43 | $-63.64 | $-67.93 | 93.0% [81.4%, 97.6%] | 44.9 |
| 120-300s | 0 | — | — | — | — |
| 300-600s | 0 | — | — | — | — |
| ≥600s | 0 | — | — | — | — |

## Phantom-corrected vs raw deltas

Per `feedback_use_corrected_pnl_always.md` we report both. Magnitudes:

| Asset | raw | phantom-corrected | delta |
|---|---:|---:|---:|
| BNB | $+5.17 | $+4.71 | $-0.46 |
| HYPE | $-284.61 | $-179.76 | $+104.85 |
| DOGE | $+37.01 | $+25.59 | $-11.42 |

The HYPE +$105 swing materially changes our headline PnL view on HYPE
since T4 (raw says -$285, corrected says -$180), but is **orthogonal**
to the staleness question — it's measurement noise (ghost fills) not
model noise (stale spot). The raw → corrected delta does not change the
qualitative slope verdict on any asset.

(**Bit-fix note:** the first dry-run of the script attributed phantom
delta to every per-fill `settled_trades` row, doubling HYPE delta to
+$210. The `st_anchor` CTE pinning the delta to MIN(`settled_at`) per
`(ticker, side)` corrects this; canonical numbers above use the fixed
pipeline.)

## Caveats / honest-NULLs

1. **Sample compression.** Across all 379 trades the max
   proxy_staleness was 119s. Buckets ≥120s have n=0. **This RCA cannot
   estimate the EV gradient in the regime S.3 would gate.** The signal
   we wanted to measure is fundamentally absent from the trade set
   because the bot's existing implicit filter (no WS tick → no
   eval-fire → no candidate row) already removes those moments.

2. **Proxy ≠ WS staleness.** REST 1-min candles approximate Coinbase
   trade activity, not the bot's WS cache age. WS staleness is the
   actual signal S.3 would gate on. Even if we had stale-bucket trades
   here, the magnitudes would be a lower bound on actual WS
   staleness (a candle absence at minute M means WS was definitionally
   stale during M; a candle PRESENCE at minute M does not guarantee a
   WS tick arrived within the candle minute, only that ≥1 trade
   happened on Coinbase). S.1 (the WS-staleness instrumentation Bit)
   is the right tool — and is the prerequisite for any data-driven S.3
   ship.

3. **BNB n=9 is too small.** Even within the populated buckets, BNB
   Wilson CIs are wide (e.g., 6/0 → [61%, 100%]). Per-asset BNB
   conclusions are no stronger than "still WR=100% in honeymoon".

4. **WR is binary and noisy on 15M.** Single-trade settlement (yes/no)
   dwarfs single-trade staleness signal. The slope test is an
   aggregate test; we lack the n to subdivide further.

5. **REST staleness measured at 1-min granularity.** The minimum
   meaningful `proxy_staleness_s` jump is 60s (one full candle). A
   candle at HH:MM:00 evaluated at HH:MM:30 produces staleness=30s
   even if the WS-tick rate was 30/min. We are NOT measuring WS
   freshness; we are measuring how many minutes the Coinbase ticker
   was trade-less leading up to the decision.

6. **3 HYPE fallback-decision-time rows.** Those 3 tickers had no
   `candidate` or `decided_contract*` row in `evaluated_opportunities`
   for the ticker — likely orphan-DB-watchdog-era rows or a pre-Bit-
   2.1a artifact. They use settled_at - 900s as decision_time. This
   does not affect the verdict (too few to move CIs) but warrants a
   followup orphan ticket.

7. **Phantom delta on HYPE is large.** The +$105 phantom correction on
   HYPE (vs $-12 on DOGE, $-0.46 on BNB) is concentrated in 2 tickers
   (KXHYPE15M-26MAY180530-30 +$115; KXHYPE15M-26MAY190645-45 +$14).
   See `feedback_use_corrected_pnl_always.md` precedent — this is the
   kind of distortion the LEFT-JOIN-phantom_corrections discipline
   exists to surface.

## Recommended next steps

1. **S.1 first** — ship WS spot-cache age instrumentation
   (`scan_iteration_*` log line emits per-asset WS cache age in ms at
   decision time). That gives us the actual gate input. S.2's REST
   proxy was a 1-day spike to assess whether the signal exists at all;
   the answer is "not in the trade set at the REST granularity, and we
   can't ladder the WS granularity from S.2's data".
2. **Defer S.3 explicit gate** until S.1 lands and we have ≥4-6 weeks
   of WS-age data covering at least one Coinbase outage or
   illiquid-period block. The implicit gate (no WS tick → no eval) is
   already in production; an explicit gate on top of it would only fire
   on edge cases that need their own ev measurement.
3. **Backfill BNB** — re-run this script after BNB n ≥ 50 (~2 more
   weeks at current cadence). Per-asset slope test power triples and
   the honeymoon-WR-100% effect dilutes.
4. **File followup** for the 3 HYPE orphan-decision-time rows
   (no `candidate`/`decided_contract*` in eval table for a settled
   ticker).

## Cross-refs

- Plan: `kb/decisions/bit-s-2-spot-staleness-rca-plan.md`
- Script: `scripts/research/spot_staleness_pnl_attribution.py`
- CSVs: `data/research_cache/spot_staleness/_results/{BNB,HYPE,DOGE}_{buckets,trades}.csv`
- Coinbase candle cache: `data/research_cache/spot_staleness/{BNB,HYPE,DOGE}-USD/`
- Memory: `feedback_use_corrected_pnl_always.md`, `db-sync.md`
