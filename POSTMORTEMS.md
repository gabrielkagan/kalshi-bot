# Post-Mortems

## PM-001: "database is locked" contention incident — 2026-03-09

### Timeline (UTC)

| Time | Event |
|------|-------|
| ~13:14 | "database is locked" errors begin firing in `_poll_evaluated_opportunities()` (bot.py:13235; pre-Bit-2.1a — file is now `bot/_impl.py`). Errors come in bursts of 10-24 per settlement cycle (~30s intervals). |
| ~13:15 | CPU spikes to 100% — settlement loop hammering 91 individual Kalshi API calls + 91 individual DB commits in tight loop, compounded by retry-on-lock. |
| ~13:28 | Errors subside as contested hourly tickers settle and pending backlog clears. |
| 13:36 | Fix deployed (commit `38754ef`). Bot restarts cleanly, no errors. |

### Root Cause

`_poll_evaluated_opportunities()` processed 91 pending `evaluated_opportunities` rows across only 30 unique tickers. For each of the 91 rows, it:
1. Called `self._client.get_market(ticker)` — same ticker fetched 3-5x redundantly
2. Ran `UPDATE ... SET status='settled'` + `COMMIT` — 91 separate commits

This collided with `supabase_sync.py` running **165 SQL queries every 10 seconds** on the same WAL database. The supabase sync thread holds read locks that prevent the main thread's writes from completing within the 10s busy_timeout.

**Aggravating factor:** Weather observation mode added 79 of the 91 pending rows (same ticker appearing up to 5x for different filter_stages). This volume didn't exist before weather was enabled.

### Impact

**None on live trading.** The errors occurred in the counterfactual settlement checker, not the live trading or order execution path. The `except Exception` handler caught and logged each error; tickers were retried next cycle. No trades were missed, no data was lost. All evaluated_opportunities hours have continuous data with no gaps.

### Fix Applied (commit `38754ef`)

1. **Deduped API calls:** Group pending rows by ticker, call `get_market()` once per unique ticker (91 → 30 calls).
2. **Batched commits:** All `mark_evaluated_opportunity_settled()` calls use `commit=False`, single `COMMIT` at end (91 → 1 commits).
3. **Shadow settles per-ticker:** `settle_signals()` calls (fifteenm, hourly_alt, spx_harrv, sol_pathc) moved from per-row to per-ticker — they're idempotent per ticker.
4. **Added `busy_timeout=5000` to `analyst.py`** (`_open_db()` line 212) — was the only production file missing it.

### Gaps Exposed

| Gap | Proposed Rule |
|-----|---------------|
| analyst.py missing busy_timeout | **Already had rule** in CLAUDE.md (line 106) — wasn't applied to analyst.py. Need a regression test. |
| Per-row commits in settlement loop | **New rule:** Never commit inside a loop — always batch. |
| No WAL requirement documented | **New rule:** All `sqlite3.connect()` calls on state.db must set `PRAGMA journal_mode=WAL`. |
| supabase_sync runs 165 queries/10s | **Audit needed:** Reduce query count or increase sync interval. Not urgent but contributes to contention window. |
| Weather observation flooding evaluated_opportunities | **Monitor:** 79 rows per settlement cycle from weather alone. If this grows, add a cleanup policy or reduce observation granularity. |
| Missing busy_timeout not caught pre-deploy | `TestBusyTimeout` regression test exists but didn't cover analyst.py. Now it should. |
