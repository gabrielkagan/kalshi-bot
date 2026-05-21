---
clickup: 86ba1wrcg
parent_umbrella: 86ba1wrad
filed: 2026-05-21
status: plan
---

# Bit S.1 — CoinbaseFeed spot-staleness instrumentation

## Discipline checklist

1. **RCA every CRITICAL/MAJOR before patching.** 2-zero adv gate. Expect 4-6 rounds (production code + schema chain + sister docs).
2. **TDD-first via `/test-writer`.** Failing tests scaffold first.
3. **Operator-confirm before push.** Production change; touches CoinbaseFeed AND `state.db` schema (ALTER TABLE ADD COLUMN — additive, safe).
4. **24h soak post-ship.** Query staleness distribution per asset before S.3 plan.
5. **Schema-chain lock-step** per `bot/CLAUDE.md` `_shadow_diag` discipline — column add, signature update, CALLER update, schema baseline bump in ONE commit.

## Goal

Add per-asset spot-price timestamp tracking to `CoinbaseFeed` and record `spot_staleness_seconds` on every `evaluated_opportunities` row. **No behavior change.** This is observability only — S.3 is the gate.

## Why this Bit doesn't gate

We need to measure production staleness distribution before picking thresholds. Picking thresholds from REST gap data is a useful prior; picking from production WS reality is what S.3 needs. Discipline: measure first, gate second.

S.3 ticket: `86ba1wrka` (deferred — S.2 RCA found zero settled trades with proxy staleness ≥120s; data-informed thresholds need ≥24h soak of S.1 distribution before S.3 can ship).

## Affected surfaces (schema chain)

1. `bot/feeds/coinbase.py`
   - `__init__`: add `self._price_ts: Dict[str, float] = {}`
   - WS message handler: alongside `self._prices[asset] = price`, set `self._price_ts[asset] = time.monotonic()`
   - NEW: `def get_price_with_ts(self, asset) -> Optional[Tuple[float, float]]`
   - Existing `get_price()` unchanged (preserves call sites that don't need staleness)

2. `bot/state.py`
   - `_create_tables` (or `_migrate_schema` block per existing pattern): `ALTER TABLE evaluated_opportunities ADD COLUMN spot_staleness_seconds REAL` if missing
   - `insert_evaluated_opportunity` signature: `spot_staleness_seconds: Optional[float] = None`
   - INSERT column + VALUES placeholder + `COALESCE(?, spot_staleness_seconds)` in ON CONFLICT DO UPDATE so subsequent UPSERTs don't NULL-out a value (mirror config_snapshot_id pattern per `bot/CLAUDE.md`)

3. `bot/scanner/__init__.py`
   - Coinbase scan-path `else` branch (~line 1634; reach: `_pt in (None, "15m", "hourly")`):
     ```python
     _spot_pair = self._feed.get_price_with_ts(asset)
     if _spot_pair is None:
         spot = None
         spot_staleness_seconds = None
     else:
         spot, _last_ts = _spot_pair
         spot_staleness_seconds = max(0.0, time.monotonic() - _last_ts)
     ```
   - Pass `spot_staleness_seconds=spot_staleness_seconds` to ALL `insert_evaluated_opportunity` calls downstream of this spot fetch
   - **DO NOT** alter the existing `if spot is None or spot <= 0: continue` branch — it stays the same (silent_spot_none). The staleness column is recorded across the whole gamut: candidate, decided, rejected.

4. `tests/fixtures/state_db_schema_baseline.txt`
   - `evaluated_opportunities` col count: 152 → 153 (verified via PRAGMA; if current count differs from my reading, lock-step with actual)

5. `agent_docs/db_schema.md`
   - Add `spot_staleness_seconds REAL  -- seconds since last Coinbase WS tick at evaluation; NULL when spot is None`

6. `tests/contracts/test_spot_staleness_instrumentation.py` (NEW)
   - `test_coinbase_feed_tracks_price_timestamps` — push WS msg → `get_price_with_ts(asset)` returns recent monotonic ts
   - `test_get_price_with_ts_returns_none_for_unseen_asset`
   - `test_get_price_unchanged_when_get_price_with_ts_added` — backward compat
   - `test_evaluated_opportunities_has_spot_staleness_seconds_column`
   - `test_insert_evaluated_opportunity_accepts_spot_staleness_kwarg`
   - `test_insert_evaluated_opportunity_persists_spot_staleness`
   - `test_scanner_passes_spot_staleness_to_insert` (AST + runtime)
   - `test_silent_spot_none_branch_unchanged` — regression on the existing skip path

## TDD-first order

All tests in `tests/contracts/test_spot_staleness_instrumentation.py` RED before any production code lands. Then:

1. Implement `_price_ts` + `get_price_with_ts` → tests 1-3 GREEN
2. Implement schema migration + signature → tests 4-6 GREEN
3. Implement scanner wiring → tests 7-8 GREEN
4. Schema baseline bump
5. Doc updates
6. R1 adv-review

## Adversarial review focus

- **Race condition risk**: `_price_ts[asset]` written under `self._lock` in WS thread; read in scan thread. Confirm GIL + atomic dict get is sufficient. (`get_price` already runs under `with self._lock`; mirror for `get_price_with_ts`.)
- **time.monotonic() vs time.time()**: monotonic is the right choice (no jumps from NTP/leap-seconds). But the *delta* between WS-thread monotonic and scan-thread monotonic must be on same clock. monotonic() is process-wide → safe.
- **Schema baseline drift**: bump must match actual current col count post-Bit-86b9zkp8p (`config_snapshot_id`) and any later schema adds. Run `PRAGMA table_info(evaluated_opportunities)` before bumping fixture.
- **silent_spot_none branch**: existing skip-on-`spot is None` path keeps unchanged behavior; staleness column NULL on those rows. Test pins this.
- **Sister-doc drift**: `bot/CLAUDE.md` SQLite section + `_shadow_diag` schema chain — confirm we follow the schema-chain discipline.
- **Schema baseline contract regression**: `tests/fixtures/state_db_schema_baseline.txt` is pinned by `tests/contracts/test_state_db_schema_baseline.py`. Bump in same commit.
- **Cell-block lock-step**: NOT required for S.1 (no new filter_stage); S.3 will add `spot_stale`. Confirm in adv-review that no filter_stage was accidentally introduced here.

## Acceptance

- Tests 1-8 GREEN
- `evaluated_opportunities.spot_staleness_seconds` column present; existing rows NULL
- Scanner records non-NULL staleness on all post-deploy Coinbase scan-path rows (`_pt in (None, "15m", "hourly")`) where `spot` is not None
- 2-zero adv gate cleared
- `make ast-check` clean
- `make doc-drift` clean (no constant changed — should be a no-op)
- 24h soak: per-asset staleness distribution (p50/p90/p95/p99) captured and recorded in S.3 plan doc

## Out of scope

- ~~The hourly path's spot read~~ — R2 fix-up 2026-05-21: hourly actually
  shares the Coinbase `else` branch with 15M (`_pt in (None, "15m",
  "hourly")`) so hourly rows DO carry staleness once S.1 ships. This
  was a sister-doc onion-ring drift that R2 adv-review caught.
- Weather / SPX / sports engine spot paths (those use other feeds — confirmed: separate if/elif branches at scanner line ~1619-1631 short-circuit before the Coinbase `else`)
- Adding staleness as a feature to cal_mlp recipe (deferred to a future Bit; requires cfg_fp rotation)

## Followups (file at ship time)

- ~~HOURLY scanner spot path instrumentation~~ — R2 fix-up 2026-05-21: covered by S.1 since hourly shares the Coinbase `else` branch with 15M.
- Telegram alert if any asset's staleness p99 > Xs over a 1h window (canary for WS feed drift)
- Dashboard panel: per-asset rolling staleness percentiles
