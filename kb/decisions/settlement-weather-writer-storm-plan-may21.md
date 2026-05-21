---
clickup: 86ba1xdwp
status: PLANNED
ship_date: TBD
---
# Settlement weather writer-storm fix — plan

**ClickUp:** [86ba1xdwp](https://app.clickup.com/t/86ba1xdwp)
**Date:** 2026-05-21
**Risk tier:** caution (touches production settlement hot path)
**Effort:** S

## Discipline preamble (every plan-doc rule applies)

1. **RCA every CRITICAL/MAJOR finding before patching.** — Done above and in the ticket; mechanical root cause identified at `bot/settlement.py::SettlementTracker._poll_evaluated_opportunities` weather phase.
2. **TDD-first via Pillar 4 hook.** — `/test-writer` scaffolds a failing regression test BEFORE the implementation edit; test pins the post-fix invariant (no shared-conn writer-lock held across HTTP fetches in the settlement weather loop).
3. **Adversarial review to 2 consecutive zero-CRITICAL/MAJOR rounds.** — Doc-spike adv-review band per `feedback_adv_review_doc_spike_drift_may17.md` is 5-7 rounds; this is closer to a code-spike (small surgical diff), expected band 2-4 rounds.
4. **NO PR until 2-zero gate clears** — `feedback_adv_review_before_pr_may19.md`.

## Load-bearing observations

### O1. The bug is mechanical, not load

Python's `sqlite3` default `isolation_level=""` (deferred) auto-BEGINs a transaction on the first DML and holds the writer lock continuously until explicit `.commit()`. In the pre-fix weather loop, the first `self._state.conn.execute("UPDATE evaluated_opportunities SET wx_actual_high_temp=...")` acquires the writer lock; the matching commit is OUTSIDE the loop (gated by `_wx_dirty`). With N settled weather brackets in `_weather_updates`, the lock is held across N HTTP calls × 1-5s each.

### O2. The author's intent was correct, the implementation accidentally re-acquires the lock

The pre-fix Phase 3 comment ("These run AFTER the write lock is released") states the design intent. The bug is that the `UPDATE` on `self._state.conn` was inserted later without realizing it re-opens an auto-tx that holds the writer lock through the HTTP loop.

### O3. `_weather_updates` is unbounded

Collected during Phase 1 of `_poll_evaluated_opportunities` — every settled weather row with `wx_actual_high_temp IS NULL` per tick. With a quiet overnight queue + Open-Meteo archive availability around 04-06 UTC, 10-20+ cities can drain in one settlement tick at 11:04-11:07 UTC.

### O4. `WeatherProbabilityModel._save_bias` opens its own connection

`bot/engines/weather_engine.py` `_save_bias` — `sqlite3.connect(self._db_path)` per call, sets `PRAGMA busy_timeout=10000`, INSERT OR REPLACE on `weather_bias`, commit, close. It DOES NOT share `StateManager.conn`. So while the shared conn holds the writer lock, save_bias's separate conn busy-waits up to 10s.

### O5. The `_backfill_weather_actual_temps` method is NOT affected by this bug

It already does `self._state.conn.commit()` BEFORE `update_bias()` per row. So the per-row commit pattern is proven safe in the codebase. The fix carries the same pattern into the inline weather loop.

### O6. Cascading victims

Every separate-connection writer in the bot blocks for up to 10s during the shared-conn lock hold:
- `weather_engine._save_bias` (each city)
- `market_obs_snapshotter` (1Hz, own conn)
- `phantom_reconcile_monitor` cron (own conn at `scripts/audit/phantom_pnl_audit.py`)
- `CALMLP_POSTHOC` updates (own conn)
- `fifteenm_shadow insert_signal` via StateManager.conn — different victim class: it's the SAME conn so the issue isn't busy_timeout but cross-thread auto-tx interference (B3-fu1 territory); 30s hold is the upper bound of its retry pattern.

## Risk class

**Caution.** Touches a production hot path (settlement tracker thread). Changes to the weather settlement code path have a long history of regressions when transaction boundaries are altered. Mitigation:
- TDD-first regression test.
- AST-based invariant guard in tests: no `_wx_eng._model.update_bias(` inside the for-loop body in `bot/settlement.py`.
- Behavioral test: simulate N=3 settled weather brackets with a stub fetcher; assert max separate-conn lock-wait < 250 ms.
- Operator approval required before deploy (per `feedback_collector_universe_capacity` discipline — every requires-approval ship pauses for explicit confirmation).

## The fix

Refactor the weather phase in `bot/settlement.py::SettlementTracker._poll_evaluated_opportunities` into two strict phases:

**Phase 3a (HTTP-only, NO DB writes):**
```python
_wx_observations = []  # list of (opp_id, ticker, _wx_city, _market_date, _obs_high, forecast_mean)
for (opp_id, ticker, row) in _weather_updates:
    try:
        _wx_city = row["asset"].replace("_TEMP", "")
        _market_date = self._parse_weather_market_date(ticker)
        if not _market_date or _market_date >= _today:
            continue
        if _wx_eng is None:
            continue
        _obs_high = _wx_eng._fetcher.fetch_observed_high(_wx_city, _market_date)
        if _obs_high is None:
            continue
        forecast_mean = row.get("spot_price")
        _wx_observations.append((opp_id, ticker, _wx_city, _market_date, _obs_high, forecast_mean))
    except Exception as e:
        logging.warning("weather_observed_temp fetch failed for %s: %s", ticker, e)
```

**Phase 3b (DB-only, tight per-row tx):**
```python
for (opp_id, ticker, _wx_city, _market_date, _obs_high, forecast_mean) in _wx_observations:
    try:
        self._state.conn.execute(
            "UPDATE evaluated_opportunities SET wx_actual_high_temp=? WHERE id=?",
            (_obs_high, opp_id))
        self._state.conn.commit()  # release lock immediately
        logging.info("weather_observed_temp: %s %s %.1fF",
                     _wx_city, _market_date, _obs_high)
        if forecast_mean:
            _wx_eng._model.update_bias(
                _wx_city, _obs_high, forecast_mean,
                market_date=_market_date)
            logging.info("weather_bias_update: %s %s actual=%.1fF forecast=%.1fF",
                         _wx_city, _market_date, _obs_high, forecast_mean)
    except Exception as e:
        logging.warning("weather_observed_temp write failed for %s: %s", ticker, e)
```

`_wx_eng` is hoisted ONCE above Phase 3a (`_wx_eng = getattr(self._ml, "weather_engine", None) if self._ml else None`). Phase 3a guarantees only non-None `_wx_eng` paths populate `_wx_observations`, so Phase 3b's `update_bias` call site needs no defensive recheck.

Net effect:
- Writer lock held only during the UPDATE+commit window (~10-50ms per row), never across HTTP latency
- `update_bias` runs AFTER the shared-conn commit per row, so its separate-conn INSERT can acquire the writer lock cleanly without competing
- The `_wx_dirty` flag + after-loop commit are removed

## Tests (TDD-first)

### T1 — Contract test (AST guard)

Path: `tests/contracts/test_settlement_weather_writer_lock_phase3.py`

Asserts:
1. The function/method containing the weather settlement loop has TWO for-loops over `_weather_updates` / `_wx_observations` (Phase 3a + Phase 3b), not one.
2. No `_wx_eng._model.update_bias(` call inside any for-loop body that also contains a `_wx_eng._fetcher.fetch_observed_high(` call. (Pins separation.)
3. `self._state.conn.commit()` is called inside the Phase 3b loop body (per-row commit), NOT only after.
4. `_wx_dirty` no longer appears in the method.

### T2 — Behavioral test

Path: `tests/integration/test_settlement_weather_writer_storm_regression.py`

Scenario (as shipped — narrowed to bound CI runtime):
1. Build a real on-disk `StateManager` (WAL, production busy_timeout=10000); also seed a `weather_bias` table for the stub bias writer + contention probe.
2. Seed N=3 settled weather brackets in `evaluated_opportunities` with NULL `wx_actual_high_temp` (`product_type='weather'`, `status='pending'`, `spot_price=75.0`).
3. Construct a `SettlementTracker` with a MagicMock client returning `result='yes'` per ticker; inject a fake `_ml.weather_engine` whose `_fetcher.fetch_observed_high()` sleeps 0.2 s per call (simulating HTTP latency) and whose `_model.update_bias()` opens a SEPARATE sqlite3 conn with a test-only 1 s busy_timeout (production stays at 10 s — the shorter timeout bounds the demonstration of the bug to ~3 s wall instead of ~30 s, without changing the bug class).
4. From a second daemon thread (`_ContentionProbe`), open a separate sqlite3.connect() to the same DB and attempt INSERT OR REPLACE on `weather_bias` every 30 ms during the settlement run, measuring max wall-clock duration per attempt.
5. Run `tracker._poll_evaluated_opportunities()` from the test thread; stop the probe.
6. Verify all 3 cities had `update_bias` called (eligibility + persistence path exercised).
7. **Assertion (RED before fix):** `max(probe.durations_ms) < 250 ms`. Pre-fix: probe sees ~1000 ms waits (busy_timeout exhaustion). Post-fix: probe waits ~5-50 ms (per-row commit pattern).

## Sister-doc lockstep (run pre-flight per `feedback_sister_doc_onion_rings_may20`)

Surfaces to update in the same commit:
- `bot/settlement.py` — primary edit
- `tests/contracts/test_settlement_weather_writer_lock_phase3.py` — new contract test
- `tests/integration/test_settlement_weather_writer_storm_regression.py` — new regression test
- `kb/decisions/settlement-weather-writer-storm-plan-may21.md` — this doc (flip status PLANNED → SHIPPED at ship)
- `bot/CLAUDE.md` — SQLite section: add a one-liner referencing the 2-phase-loop discipline (similar in spirit to the `MarketObsSnapshotter` retention-window-as-contention-control entry already there)

Sister-doc surfaces deferred to post-ship hygiene (not load-bearing for the fix):
- `kb/failures/` — postmortem doc after ship: `kb/failures/settlement-weather-writer-storm-may21.md`
- `MEMORY.md` index — add active-bug entry that flips to durable-architecture entry on ship

NO updates needed in: `README.md`, `agent_docs/db_schema.md` (no schema change), `agent_docs/config_reference.md` (no constant change), `whitepaper.md` / `whitepaper_investor.md` (no user-facing surface change).

## Verification (post-ship)

1. After deploy, watch the next 11:04-11:07 UTC cycle on VPS.
2. `journalctl -u kalshi-bot --since "<next-day> 11:04:00 UTC" --until "<next-day> 11:08:30 UTC" | grep WRITER_ACTIVE` — expect NO `status=fail` entries during weather settlement phase.
3. `tail ~/phantom_reconcile.log` at <next-day> 11:08 UTC — expect normal INFO line, no ERROR.
4. Spot-check `market_observations_continuous` row counts for the 11:04-11:07 UTC window — expect normal density (~6 rows/sec × 180 sec ≈ 1080 rows), not the ~0 rows seen in the storm.

## Non-goals

- Not touching the broader StateManager cross-thread auto-tx racing class (B3-fu1 territory).
- Not migrating `WeatherProbabilityModel` to share `StateManager.conn` — that's a larger architectural shift; the per-row commit before `update_bias` is sufficient to drop the cascade.
- Not changing `_backfill_weather_actual_temps()` — it's already safe.
- Not addressing the 1-vCPU/1GB bot VPS size (`s-1vcpu-1gb-nyc3-01`) — sizing is a separate ticket if needed.

## Open questions for operator at kickoff

None. The fix is mechanical and doesn't depend on operator decisions. Deploy gating happens at the standard `requires-approval` confirmation step.
