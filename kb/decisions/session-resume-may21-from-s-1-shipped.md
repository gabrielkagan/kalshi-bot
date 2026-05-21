---
filed: 2026-05-21
status: pickup-prompt
session_purpose: resume HYPE/DOGE/BNB cal_mlp v1.1 retrain work after Bit S.1 ships
ticket_chain: 86ba0jmyq (umbrella) → 86ba1wrad (S umbrella) → 86ba1wrcg (S.1 SHIPPED) → 86ba1wrh7 (S.2 SHIPPED) → 86ba1wrka (S.3 DEFERRED) → 86ba1wpck (F) → 86ba1wpgf (G) → 86ba1wpjp (H)
---

# Session Resume — Post-Bit-S.1 Ship → Resume F/G/H Training Track

## TL;DR

A new agent picking this up should know:

1. **Bit S.1 SHIPPED 2026-05-21** (commit hash TBD — see git log). 8-round adv-review chain cleared 2-zero gate at R7+R8. Production now records `spot_staleness_seconds` on every Coinbase scan-path insert via per-asset cache auto-fill. NO behavior change — observability only.

2. **S.2 RCA SHIPPED 2026-05-21** (read-only investigation). Verdict: zero settled trades in 379-row corpus have proxy staleness ≥120s — the high-staleness regime is absent from the trade set, likely due to implicit selection effects in downstream gates. Finding doc: `kb/findings/spot-staleness-pnl-attribution.md`.

3. **S.3 DEFERRED** (ticket `86ba1wrka`). Production gate is no longer urgent. Re-evaluate after ≥24h of S.1 instrumentation data (or 4-6 weeks per S.2 reviewer's recommendation).

4. **F/G/H now UNBLOCKED**: BNB replay backfill (F), HYPE/DOGE corpus extend (G), 3-asset head-to-head verdict (H). Detailed plans at:
   - `kb/decisions/bit-f-bnb-replay-backfill-plan.md` (Bit F, ticket `86ba1wpck`)
   - Bit G + Bit H plans NOT YET written — file them as the first hygiene task.

## What changed in the S.1 ship

8 files in lock-step (per `bot/CLAUDE.md` § "spot_staleness_seconds schema chain"):

1. `bot/feeds/coinbase.py` — `self._price_ts: Dict[str, float]` in `__init__`; lock-step write with `_prices` in `_on_frame`; new `get_price_with_ts(asset) -> Optional[Tuple[float, float]]` method.
2. `bot/state.py::__init__` — `self._scan_spot_staleness_cache: Dict[str, float] = {}` cache (per-asset, populated by scanner each tick, read by `insert_evaluated_opportunity` for auto-fill).
3. `bot/state.py::_create_tables` — `ALTER TABLE evaluated_opportunities ADD COLUMN spot_staleness_seconds REAL` (cid=141, between tm_shadow_kelly_bound_hit and cal_mlp_p_mean).
4. `bot/state.py::insert_evaluated_opportunity` — new kwarg + INSERT column + VALUES placeholder + COALESCE in ON CONFLICT DO UPDATE + auto-fill block via `_scan_spot_staleness_cache`.
5. `bot/scanner/__init__.py` (~line 1634) — Coinbase scan-path `else` branch reads `get_price_with_ts`, computes staleness, writes to `_scan_spot_staleness_cache[asset]` (or pops on warmup-NULL).
6. `tests/fixtures/state_db_schema_baseline.txt` — bumped to 149 cols.
7. `tests/contracts/test_spot_staleness_instrumentation.py` (NEW, 19 tests).
8. `agent_docs/db_schema.md` — entry under `evaluated_opportunities`.

Plus:
- `bot/CLAUDE.md` — new schema-chain section.
- `kb/decisions/bit-s-1-spot-staleness-instrumentation-plan.md` — plan doc with R2 + R5 fix-up notes.
- `kb/decisions/bit-s-2-spot-staleness-rca-plan.md` — S.2 plan doc.
- `kb/findings/spot-staleness-pnl-attribution.md` — S.2 verdict.
- `scripts/research/spot_staleness_pnl_attribution.py` — S.2 RCA script (read-only).

## Why F/G/H are now unblocked

S.2 RCA showed zero settled trades with proxy staleness ≥120s. The production-data-quality concern that was the original "don't retrain on broken data" worry doesn't have empirical support. We can retrain on the live `evaluated_opportunities` + replay corpus knowing:

- Settled trades (the test fold) are already implicitly fresh-spot.
- Going forward, S.1 instrumentation will tell us if BNB/HYPE/DOGE rows in `evaluated_opportunities` carry meaningful staleness — and `spot_staleness_seconds` becomes available as either a recipe feature or a row-filter for Bit H's head-to-head test fold.

## Pickup actions for fresh session

**Order matters. F → G → H sequence.**

### 1. Hygiene first (5 min)

- Verify the S.1 ship hash via `git log -1 --oneline`
- Run `make test-affected` to confirm tests still GREEN
- Read MEMORY.md for any new feedback entries
- Check VPS deploy status (post-S.1 ship) via `mcp__kalshi-vps__get_bot_status`

### 2. Write Bit G + H plan docs (15-30 min)

Mirror the shape of `kb/decisions/bit-f-bnb-replay-backfill-plan.md`:
- `kb/decisions/bit-g-hype-doge-corpus-extend-plan.md`
- `kb/decisions/bit-h-3-asset-head-to-head-plan.md`

Both should reference S.1's `spot_staleness_seconds` column as a potential audit/filter input.

### 3. Execute Bit F (BNB replay backfill, ticket `86ba1wpck`)

Plan doc: `kb/decisions/bit-f-bnb-replay-backfill-plan.md`. Discipline: RCA + TDD-first + 2-zero adv gate + Mac-only.

Key gotcha: `historical_replay_calmlp` schema CHECK is asset IN ('HYPE','DOGE') — one-shot migration needed in `scripts/ops/migrate_replay_table_bit_f.py`. Plan doc has the migration logic spelled out.

Coinbase BNB-USD pre-flight: 65.86% 1-min coverage over May 9-21 (verified 2026-05-21). Decision per first principles: keep ALL rows + add `spot_staleness_seconds` AUDIT column to the replay table (NOT a feature) — preserves train-serve symmetry with production.

Expected output: ~1,000-1,150 BNB rows, fresh v1.1 replay-recipe bundle. Cfg_fp_replay rotates from `9347942aaba71146` → new fingerprint.

### 4. Execute Bit G (HYPE/DOGE corpus extend, ticket `86ba1wpgf`)

Plan doc: write first. Strategy: run extended `crypto_replay_backfill.py` (from Bit F) for HYPE+DOGE May 10 → today (~600 new rows each). Re-extract with `--train-days 50` (was 30). Retrain v1.1 HYPE+DOGE bundles.

### 5. Execute Bit H (3-asset head-to-head, ticket `86ba1wpjp`)

Plan doc: write first. Mirror Bit D + D-F1 methodology. Apply `--align-w` (HYPE=0.80, DOGE=0.60, BNB=0.20 from `MARKET_BLEND_W_BY_ASSET`). Per-asset verdict: HOLD or PROMOTE. Optional: use `spot_staleness_seconds` as a test-fold filter to verify v1.1 doesn't artificially benefit from staleness-correlated noise.

VERDICT ONLY — no deploy. CURRENT pointer flip is a separate ship (would file Bit I or analog of P2.1.d atomic deploy) requiring operator confirmation.

## What NOT to do

- Don't push S.3 production gate until 24h+ of S.1 data is in hand.
- Don't change BTC/ETH/SOL/XRP bundles — they're under the 30d revalidation soak (due 2026-06-12, ticket `86b9xk9v3`).
- Don't combine F+G+H into a single Bit — discipline requires per-ship gating.
- Don't `git add -A` — there are many pre-existing parallel-session changes in the working tree (`feedback_parallel_sessions.md`).

## Useful references

- Plan docs: `kb/decisions/bit-{f,s-1,s-2}-*.md`
- Finding doc: `kb/findings/spot-staleness-pnl-attribution.md`
- Original umbrella: `kb/decisions/hype-doge-cal-mlp-v1-1-retrain-pickup-prompt-may19.md`
- Bit D HOLD verdict (predecessor to F/G/H): `kb/findings/` (search "bit_d_head_to_head_hold")
- MEMORY: long-arc adv-review patterns; sister-doc onion-ring drift; collector universe; CT-MDP F0.1.

## ClickUp tickets snapshot

| Bit | Ticket | Status |
|---|---|---|
| Umbrella S | `86ba1wrad` | open |
| S.1 instrumentation | `86ba1wrcg` | SHIPPED 2026-05-21 |
| S.2 RCA | `86ba1wrh7` | SHIPPED 2026-05-21 |
| S.3 gate | `86ba1wrka` | DEFERRED (post-S.1 soak) |
| Bit F BNB | `86ba1wpck` | open, ready |
| Bit G HYPE/DOGE | `86ba1wpgf` | open, ready |
| Bit H verdict | `86ba1wpjp` | open, blocks Bit I (deploy) |

End of pickup prompt.
