---
name: bit-collector-reconnect-drop-elimination-plan
description: Eliminate the conn C drop class on the collector during REST-refresh-induced reconnect cascades — single-file constant bump (10K→50K) + write_queue_peak instrumentation. RCA confirmed; TDD-first; 2-zero adv gate.
type: decision
status: in flight
clickup: 86ba1xraq
parent_clickup: 86ba1h0cb
---

# Bit — Collector reconnect-cascade drop elimination

## Discipline preamble (READ FIRST)

Per CLAUDE.md "Extraction-bit discipline" and `feedback_long_arc_adv_review_durable_fixes`:

1. **RCA every CRITICAL/MAJOR finding before patching.** ✓ Done — root cause identified as `_write_queue.put_nowait` → `queue.Full` on a 10K-cap queue during the post-reconnect data burst on conn C (single unlucky conn during the cascade load apex). NOT memory pressure (peak unchanged at 1189 MB through the storm).
2. **TDD-first via the Pillar 4 hook.** Use `/test-writer` to scaffold the failing regression test BEFORE touching `_DEFAULT_WRITE_QUEUE_MAXSIZE`. Test must demonstrate: 30K-frame burst injected into an archiver with `maxsize=50_000` produces `dropped_frames=0` after drain; same test on `maxsize=10_000` produces `dropped_frames > 0` (confirming the failing-test baseline).
3. **Adversarial review to 2 consecutive zero-CRITICAL/MAJOR rounds.** Expected 3-5 rounds; drift class = sister-doc lockstep across 5 sites (file the 2-zero count in commit messages with R-prefix).
4. **Never open the PR until the gate clears** — per `feedback_adv_review_before_pr_may19`, opening a PR fires paid `test.yml` + `deploy.yml` CI. Iterate locally first.

## Parallel-session caution (CRITICAL)

- Main checkout is on `ct-mdp-f0-1-stale-quote-falsification` branch with 25+ modified files including `collector/ws_connection.py` itself. **DO NOT EDIT the main checkout's collector files.**
- All edits MUST go in an isolated worktree off `origin/main c30244b5` per `feedback_worktree_isolation_for_parallel_sessions`.
- Read source from `git show origin/main:<path>` to verify what's actually on main, not from the local working tree.
- Re-fetch + re-rebase before every commit. Re-grep sister docs before claiming lockstep.
- Per `feedback_parallel_sessions`: F0.4/F0.5 parallel session is in flight (`52a3eb78`, `c30244b5` were their recent ships); they may push to main during this Bit's gate. Verify origin/main HEAD before every commit + before PR open.

## TL;DR

**Single-file fix:** `collector/ws_connection.py` — bump `_DEFAULT_WRITE_QUEUE_MAXSIZE` from `10_000` → `50_000`, add `_write_queue_peak` high-water-mark instrumentation exposed via `get_health_snapshot()`.

**Why this and only this:** RCA confirmed the burst overflowed by ~1290 frames against a 10K queue cap. Other options (stagger lengthening, parallel workers, zstd level reduction) either don't address the actual mechanism (bursts arrive 3-4 min AFTER reconnect events — stagger is unrelated to burst timing) or expand blast radius unnecessarily (per CLAUDE.md "Don't add features beyond what the task requires").

**Adv-review band:** 3-5 rounds; the change is small but the sister-doc lockstep surface is medium (5 sites). Drift class precedent from `feedback_adv_review_doc_spike_drift_may17`.

## RCA section (numbers + journalctl excerpts)

### Smoking gun

```
May 21 11:20:50 [WARNING] BronzeArchiver write_queue full (conn=C seq=1887283) — dropping frame; total dropped=1
May 21 11:20:51 [WARNING] BronzeArchiver write_queue full (conn=C seq=1888282) — dropping frame; total dropped=1000
```

999 frames dropped in 1 second on conn C. Seq delta = 999 = drop delta = every incoming frame in that second dropped (queue at 10K cap, draining at ~957/s, producing at ~1000/s during burst).

### State at investigation time (T+1h32m, 11:33:56 UTC)

| Metric | Value |
|---|---|
| collector restart | 2026-05-21 10:01:37 UTC (cap-raise live) |
| MemoryMax | 2147483648 (2048 MiB) |
| MemoryHigh | 1677721600 (1600 MiB) |
| MemoryPeak | 1247145984 (1189 MB) — UNCHANGED since boot |
| MemoryCurrent | 666 MB |
| NRestarts | 0 |
| Universe size | 316,233 tickers (REST snapshot at 10:09:51) |
| subscribe_frames per conn (boot) | 567 |
| subscribe_frames per conn (T+1h17m) | 594 (+27 batches ≈ +15K tickers/hour growth) |

### Per-conn drop pattern

| Conn | ack_frames_processed | dropped | collector_seq |
|---|---|---|---|
| A | 2310 | 0 | 2.21M |
| B | 3243 | 0 | 2.32M |
| **C** | **4383 (highest)** | **1290** | 2.28M |
| D | 2810 | 0 | 2.24M |
| E | 3456 | 0 | 2.23M |
| F | 2345 | 0 | 1.91M |
| G | 2023 | 0 | 2.13M |

**Only conn C dropped.** Other 6 conns absorbed their bursts in 10K queue. Conn C has highest ack volume (4383, 35% more than next-highest) — fattest ticker share + lands at cascade apex.

### Cascade timeline (REST refresh @ 11:17:04)

| Event | Time |
|---|---|
| RestSnapshotRefresher ticker-set change | 11:17:04 UTC (after boot's first refresh at 10:09:51) |
| Replan conn=B → reconnect requested | 11:17:25 |
| Replan conn=C → reconnect requested | 11:18:00 |
| (rest of cascade: D, E, F, G at 20s stagger) | 11:18-11:19 |
| **conn C burst → first drop at seq=1887283** | **11:20:50 (3:46 after conn C reconnect)** |
| conn C drop #1000 | 11:20:51 |
| Last kalshi_ws_disconnected event | 11:23:38 |
| Storm cleared | 11:25 (17+ min silence since) |

**Critical observation:** conn C's burst arrived 3:46 AFTER its own reconnect. This means the burst is NOT caused by the reconnect event itself — it's Kalshi pushing deferred data after the new session is fully established. Stagger lengthening (20s → 60s) wouldn't help because the bursts are decoupled from the reconnect timing.

### Mechanism (origin/main `c30244b5` code references)

`collector/ws_connection.py:173`: `_DEFAULT_WRITE_QUEUE_MAXSIZE = 10_000`

`collector/ws_connection.py:605` (`_on_frame`, asyncio thread):
```python
try:
    self._write_queue.put_nowait((frame, channel, seq))
except queue.Full:
    with self._lock:
        self._dropped_frames += 1
```

`collector/ws_connection.py:629-710` (`_drain_loop`, worker thread): `build_envelope` + `writer.write` (zstd compress + disk IO).

**Producer rate during burst:** ~1000 frames/sec (measured from drop-log seq delta)
**Worker drain rate:** ~957 frames/sec (calculated: 30s burst, 1290 drops, ~30K incoming → 28710 drained / 30s)
**Gap:** ~43 frames/sec sustained for ~30s = ~1290 frame deficit = matches measured drops

### Misattribution corrected

`feedback_collector_universe_capacity` previously characterized a similar 09:09 storm under the old 512M cap as "universe-size memory pressure → asyncio thread time slip → missed WS ping deadline." The current RCA falsifies that characterization:

- The 09:09 and 11:18 storms both occurred at REST-refresh-induced cascade reconnects (1 hour apart), NOT at random load peaks.
- Memory peak was UNDER the cap in both cases (538M / 512M and 1189M / 2048M respectively).
- The "no close frame received or sent" warnings are normal `kalshi_wire.WSClient` behavior on a controlled close, not Kalshi-side ping starvation.
- Cap raise (4×, 512M → 2048M) did NOT eliminate the drop class.

**Updated mental model:** The drop class is queue-overflow during single-conn post-reconnect data bursts, modulated by ticker-mix load distribution across conns. Not memory pressure. Not stagger timing. Pure producer/consumer rate mismatch on the unlucky conn during the cascade load apex.

## Fix surface

### Code changes (1 file)

`collector/ws_connection.py`:

1. **Line 165-180** — bump constant + update inline comment:
   ```python
   # OLD:
   _DEFAULT_WRITE_QUEUE_MAXSIZE = 10_000
   # comment: "10000 envelopes × few KB ≈ tens of MB worst case"

   # NEW:
   _DEFAULT_WRITE_QUEUE_MAXSIZE = 50_000
   # comment: "50000 envelopes × ~500B avg frame ≈ 25 MB per archiver; ~175 MB across 7 archivers worst case (well under the 2GB cap-raise as of 2026-05-21). 4.4× margin over measured peak burst (11.3K on conn C 2026-05-21 11:20-21 UTC)."
   ```

2. **Line 266-270** — update kwarg docstring:
   ```python
   # OLD: "D1.3-fu4 default 10_000 ≈ ~10s buffering at typical load"
   # NEW: "Default 50_000 ≈ ~50s buffering at typical load. Bumped from 10_000 (D1.3-fu4 initial) to 50_000 2026-05-21 (ticket 86ba1xraq) after universal-mode RCA showed 11.3K peak burst on conn C during REST-refresh cascade."
   ```

3. **`__init__`** — add peak counter:
   ```python
   self._write_queue_peak: int = 0
   ```

4. **`_on_frame`** — update peak after successful put (in the success path, NOT the queue.Full path):
   ```python
   try:
       self._write_queue.put_nowait((frame, channel, seq))
       size = self._write_queue.qsize()
       if size > self._write_queue_peak:
           with self._lock:
               if size > self._write_queue_peak:  # double-check after lock
                   self._write_queue_peak = size
   except queue.Full:
       # ... existing path
   ```
   _Note: qsize() racy by design (queue spec), but for high-water mark a single-frame undercount is fine._

5. **`get_health_snapshot()`** — add `write_queue_peak_size` field:
   ```python
   return {
       ...,
       "write_queue_peak_size": self._write_queue_peak,
       ...
   }
   ```

6. **`start()` re-spawn reset** — reset `_write_queue_peak = 0` alongside `_dropped_frames = 0`:
   ```python
   self._dropped_frames = 0
   self._drop_log_counter = 0
   self._write_queue_peak = 0  # NEW
   ```

### Sister-doc lockstep (5 sites)

1. `collector/ws_connection.py:172` — comment in constant block (covered above)
2. `collector/ws_connection.py:173` — constant value (covered above)
3. `collector/ws_connection.py:266-270` — kwarg docstring (covered above)
4. `agent_docs/bot_layout.md:251` — "bounded `_write_queue` (default maxsize=10_000)" → "bounded `_write_queue` (default maxsize=50_000, bumped from 10_000 at ticket 86ba1xraq 2026-05-21)"
5. `tests/contracts/test_d1_3_fu5_ack_not_enqueued.py:4` — docstring "bounded `queue.Queue(maxsize=10_000)`" → "bounded `queue.Queue(maxsize=50_000)`"

### Coinbase asymmetry (intentional)

`collector/coinbase_archiver.py:157` keeps `_DEFAULT_WRITE_QUEUE_MAXSIZE = 10_000`. Rationale:

- Coinbase is single-conn (1 archiver, not 7).
- Coinbase has no REST-refresh-induced reconnect cascade — channels/product_ids set is fixed at boot; no `_replan_for_archivers` path.
- Coinbase load pattern is steady-state, not bursty (no post-reconnect data flood).
- Coinbase MemoryMax cap is 256M (vs Kalshi 2048M); a 5× bump would over-allocate.

If adv-reviewer asks why Kalshi gets 50K and Coinbase stays at 10K: answer is "different load class". Document inline in plan if R1 surfaces.

## TDD test design

### Failing regression test

Path: `tests/contracts/test_bronze_archiver_burst_capacity.py` (new file)

Pattern (mirrors `test_bronze_archiver_worker_thread.py` fixtures):

```python
def test_archiver_absorbs_30k_burst_at_default_maxsize_without_drops():
    """RCA from ticket 86ba1xraq: REST-refresh-induced cascade reconnect
    causes ~1000 frames/sec burst on the unlucky conn (conn C in the
    2026-05-21 incident). With default maxsize=50_000, archiver MUST
    absorb a 30K-frame burst (~30s at peak rate) with zero drops.

    Pre-bump default maxsize=10_000 FAILED this contract: burst > queue
    capacity → put_nowait raised queue.Full → _dropped_frames > 0.
    """
    archiver, _, writer = _make_archiver(monkeypatch)  # default maxsize
    # Stub worker to slow drain (simulate disk IO contention during burst)
    # ...
    # Inject 30K synthetic frames at producer rate ~1000/sec
    # ...
    # Wait for queue to drain
    archiver.stop()
    # Assert
    assert archiver._dropped_frames == 0, (
        f"Burst-capacity contract: 30K-frame burst on default maxsize "
        f"must produce zero drops; got dropped_frames={archiver._dropped_frames}. "
        f"See ticket 86ba1xraq RCA."
    )
```

### Sub-tests (additional contract coverage)

- `test_default_maxsize_is_50000`: pin the new default value (catch accidental revert).
- `test_write_queue_peak_size_in_health_snapshot`: new instrumentation present.
- `test_write_queue_peak_resets_on_worker_respawn`: per-session observability invariant.
- `test_write_queue_peak_tracks_high_water_mark`: peak increases monotonically across puts until reset.

### Existing tests to update

- `tests/contracts/test_bronze_health_sidecar.py` — add `write_queue_peak_size` to expected schema (additive backward-compat; `schema_version` stays at 1 per existing pattern for additive changes).
- `tests/contracts/test_bronze_archiver_worker_thread.py` — review for any `default == 10_000` assertion (none found in grep; `default >= 1000` floor assertion at line 162 still passes).

### Health monitor (no change needed)

`scripts/ops/collector_health_monitor.py::check_dropped_frames` reads `dropped_frames` field — unchanged. The new `write_queue_peak_size` is additive; existing monitor doesn't need to consume it (future cron addition optional, file as followup if desired).

## Risk analysis

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| Memory ceiling exceeded at 7 × 50K × frame size | LOW | HIGH (OOM kill) | 7 × 50K × 5KB upper bound = 1.75 GB. Average frame ~500B → 175 MB typical. MemoryPeak post-cap-raise is 1189 MB with 859 MB headroom. Safe. Reverts trivially. |
| Bigger queue → bigger latency on drain → silver ETL lag | LOW | LOW (bronze is not realtime) | 50K queue at ~1000 frames/sec drain = ~50s worst case latency. Silver ETL runs nightly; 50s irrelevant. |
| Universe grows past 50K-margin within 24h soak | LOW | MEDIUM | At +15K tickers/hour growth, universe could be ~660K in 24h. Producer rate scales sub-linearly with tickers (only some tickers are active orderbook updaters). 4.4× margin holds. Soak surfaces this empirically; followup if reached. |
| Adv-reviewer flags Coinbase asymmetry | MEDIUM | LOW | Pre-empt in plan doc + commit message. Coinbase has different load class (single conn, no REST cascade). |
| Adv-reviewer flags ws_connection.py:172 numeric drift | HIGH | LOW | Sister-doc lockstep is the dominant drift class for this Bit. Pre-flight 5-site grep before each adv-round. Pinned in this plan. |
| `qsize()` race on `_write_queue_peak` mutation | LOW | LOW | Peak is a high-water-mark; single-frame undercount is invisible in practice. Use lock for the assignment per double-checked-locking pattern (idiomatic but read pattern is fine without). |
| Parallel session pushes conflicting commits to `origin/main` during this Bit's gate | MEDIUM | MEDIUM | Re-fetch + verify origin/main HEAD before every commit. Isolated worktree means I don't accidentally pick up their working-tree edits. |

## Adv-review expected drift class

Per `feedback_adv_review_doc_spike_drift_may17`:
- Sister-doc lockstep across 5 sites → expect ~1 minor per round on missed citations
- Numeric arithmetic on the "175 MB worst case" calculation → expect 1 round of arithmetic verification
- Anti-drift contract tests (test_default_maxsize_is_50000) — should land first commit

**Expected rounds:** 3-5 (within the doc-spike band; not the long 6-8 arc since code surface is small).

## Acceptance criteria

### Bit-internal (before merge)

- [ ] Failing regression test scaffolded + confirmed RED on `_DEFAULT_WRITE_QUEUE_MAXSIZE=10_000`
- [ ] Constant bump 10K → 50K
- [ ] Instrumentation: `_write_queue_peak` + `write_queue_peak_size` in health snapshot
- [ ] All 5 sister-doc sites updated
- [ ] All existing collector tests pass (no regression)
- [ ] Failing test now passes
- [ ] Adv-review 2-zero CRITICAL/MAJOR cleared
- [ ] PR opened ONLY post-gate

### Live verification (post-deploy)

- [ ] VPS pulls new commit hash; collector restarts clean
- [ ] First REST refresh cycle after restart: drops=0 across all 7 conns
- [ ] 24h soak: drops=0 sustained on all 7 conns
- [ ] `write_queue_peak_size` field present in bronze_health.json; peak values logged
- [ ] MemoryPeak < 1.5 GB sustained
- [ ] NRestarts=0

## Rollback path

Single-commit revert via standard `git revert <SHA>` + push + deploy. No data migration, no schema change, no protocol change. Worst case: revert to `c30244b5` + restart collector. Estimated rollback time: ~5 min.

## Working approach

1. Write this plan doc (DONE)
2. Create isolated worktree off `origin/main c30244b5` at `/Users/gabrielkagan/Documents/kalshi-bot/.claude/worktrees/collector-drop-fix` via `EnterWorktree`
3. `/test-writer` scaffolds the failing regression test (TDD-first; confirm RED on baseline)
4. Implement the 6-step code surface above (single commit on a feature branch)
5. Adv-review chain — R1, R2, ... until 2 consecutive zero-CRITICAL/MAJOR
6. Push branch + open PR (only post-gate)
7. Verify deploy + start 24h soak
8. Close ticket `86ba1xraq` + parent `86ba1h0cb` if soak clears

## Followups (file as separate tickets if needed)

- **F1 (optional):** Extend `scripts/ops/collector_health_monitor.py` with a `check_queue_peak_pressure` cron — alert if `write_queue_peak_size / write_queue_maxsize > 0.5` sustained (early warning before drops happen).
- **F2 (optional):** Memory-update: `feedback_collector_universe_capacity` claim about "universe-size memory pressure" needs correction post this Bit's ship. The 09:09 storm was the same REST-refresh-cascade mechanism, not memory pressure. File memory edit after Bit ships.
- **F3 (deferred):** If 24h soak shows `write_queue_peak_size` regularly >25K, consider worker parallelization (1 worker → 2 workers per archiver) as a follow-up Bit. Would address drain rate, not just buffer.
