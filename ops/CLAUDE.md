# Ops

Production infrastructure (systemd units, install scripts, deploy hooks). Tracked in git so changes are reviewable + drift-detectable.

**Off-VPS surfaces (NOT this directory):** `silver/` (D3.0 SHIPPED 2026-05-18, ticket `86b9zxc6t`) — silver ETL runs on the operator's Mac via launchd nightly @ 02:00 local; lifecycle owned by `silver/launchd/com.kalshi.silver-etl.plist` + `silver/scripts/etl_run.sh`. No bot-VPS systemd unit (off-VPS by D0.3 §13:422 — silver ETL is CPU-bound and must not contend with the bot's 2vCPU/2GB-RAM VPS). See `silver/README.md` for the operator runbook.

## Conventions
- **Source of truth.** `ops/kalshi-bot.service` IS the bot systemd unit; `ops/kalshi-collector.service` (D1.5 SHIPPED 2026-05-16, ticket `86b9ypna4`) IS the Kalshi-side Data Corpus collector systemd unit; `ops/kalshi-coinbase-collector.service` (D2.5 SHIPPED 2026-05-18, ticket `86b9znq4w`) IS the Coinbase-side Data Corpus collector systemd unit. Do NOT edit `/etc/systemd/system/kalshi-*.service` directly. Editing this directory triggers a drift check on the next deploy for the bot unit — see "Editing the unit" below. (Neither collector unit has an equivalent CI drift check yet — file a followup if drift becomes a problem in practice; the `make test-unit` + `make test-contract` invariants pin shape but not on-VPS divergence.)
- **Install:** one-time per VPS via `bash ops/install.sh`. Re-run after any change in this directory.
- **Drift detection:** `.github/workflows/deploy.yml` diffs `systemctl cat kalshi-bot` against `git show <deploy-sha>:ops/kalshi-bot.service` before `git reset --hard`. If the on-VPS unit differs, the deploy aborts and the operator must re-sync via the recovery one-liner below, then re-trigger the deploy. Local invariant: `tests/unit/test_ops_systemd_unit_matches_repo.py` (6 tests, wired into `make test-unit`). Bit 2.0.5.2 of repo modularization plan.
- **Post-deploy startup-pattern gate:** `.github/workflows/post_deploy_verify.yml` step `[6/6]` invokes `venv/bin/python3 scripts/audit/post_deploy_scan_gate.py --db state.db --window-seconds 300 --max-events 2` after the existing 5 checks. The script queries `SELECT COUNT(*) FROM bot_startup_log WHERE julianday(ts) > julianday('now', ?)` via Python's stdlib sqlite3 module (the VPS has no sqlite3 CLI installed, so the gate must be Python — also matches the `postdeploy_verify.py` / `audit_cron.py` separate-script convention). Cal_mlp integration writes ONE row UNCONDITIONALLY at startup (`scripts/cal_mlp/integration.py:457`) — after WAL verify + cal_mlp imports, before parity assertion. Threshold: `0 in 5 min` → FAIL (Bit 2.1a class — deploy didn't restart, OR bot crashed before init); `1-2 in 5 min` → OK (healthy deploy + at most 1 auto-restart from `bot.py:28053` (pre-Bit-2.1a; file is now `bot/_impl.py`)); `≥ 3 in 5 min` → FAIL (post-init crash loop). Empirical: 92 historical startup pairs over 8d (cal_mlp integration shipped 2026-04-29 — older rows do not exist in `bot_startup_log` by construction), max 2 events in any 5-min window — ≥3 has zero historical false-positives. Catalog gaps + market quietude don't cause restarts → no false positives from those classes. Catches crash-loop deploys (`systemctl is-active` returns "activating", not "failed", during the loop) — fires the existing Telegram failure alert ~5 min into the next deploy. Local invariant: `tests/unit/test_post_deploy_scan_gate.py` (18 tests — 4 invariant pins on workflow + 14 behavioral fixtures covering all threshold branches, both ISO-8601 timestamp quirks, the schema-error-not-misclassified-as-crash-loop case from R3 MAJOR #2, and the argparse-validation-rejects-nonsense-args case from R4 MAJOR #2; wired into `make test-unit`). Bit 2.0.5.3 of repo modularization plan; full 2-pivot journey across 3 candidate signals (`evaluated_opportunities` → `market_observations_continuous` → `bot_startup_log`) documented at `kb/decisions/bit-2.0.5.3-spec-correction-may07.md`.

## Install / re-install on VPS
```bash
ssh -t botuser@$VPS_HOST 'cd ~/kalshi-bot-repo && git fetch origin main && git reset --hard origin/main && bash ops/install.sh'
# install.sh prompts for sudo password — requires interactive tty (the `-t` above).
# `git fetch + reset --hard origin/main` mirrors what deploy.yml does and
# is robust to a dirty on-VPS working tree (which a `git pull` would refuse).
```

## Editing the unit (`ops/kalshi-bot.service`)

Any edit triggers the deploy.yml drift check on the next push to `main`. Realistic flow:

1. Edit + commit + push.
2. CI deploy starts; **drift check aborts** with a `FAIL: on-VPS systemd unit differs ...` message that includes the recovery one-liner.
3. ssh -t to VPS and run the **same one-liner** as Install / re-install above (`cd ~/kalshi-bot-repo && git fetch origin main && git reset --hard origin/main && bash ops/install.sh`). A bare `bash ops/install.sh` would re-install the PRIOR commit's unit (working tree is unchanged after the abort) — the next deploy would loop on the same FAIL.
4. Re-trigger the deploy from the GitHub Actions UI ("Re-run failed jobs" — only `deploy` needs to re-fire, not `test`).
5. Drift check now passes; deploy completes; bot restart picks up the new unit + new code.

The on-VPS bot continues running on the prior commit's code throughout the abort window — **no downtime**.

## state.db backup + restore (Phase 0a)

`state.db` is backed up every 4h to S3 via two systemd timers installed by `scripts/ops/setup_state_db_backup_timer.sh` (cadence revised from daily 06:00 UTC → every-4h by ticket `86b9zkp89` 2026-05-17 — Bronze durability, caps VPS-failure data-loss window at ~4h instead of ~24h):

- `kalshi-state-db-backup.{service,timer}` — every 4h on the hour (00:00, 04:00, 08:00, 12:00, 16:00, 20:00 UTC). Uses `sqlite3.Connection.backup()` (NOT rsync — a literal rsync of a hot WAL DB tears pages; see `kb/decisions/auto-research-phase-0a-plan-may09.md` RCA). Snapshot → zstd compress → `rclone copyto s3prod:bucket/daily/state-db-YYYY-MM-DD.db.zst`. The S3 key is keyed on UTC date only (`state_db_s3_backup.compute_object_key`), so all 6 sub-daily ticks within the same UTC day overwrite the same `daily/<date>.db.zst` object — each day's S3 archive reflects the most-recent successful run of that day; recovery to an earlier intra-day point would have to come from a prior day's S3 archive or a manual snapshot. Wrapped in `h4_run_with_alert.py` for Telegram failure alerts.
- `kalshi-state-db-restore-verify.{service,timer}` — Sunday 07:00 UTC (unchanged by 86b9zkp89; verification stays weekly). Pulls latest snapshot, runs `PRAGMA integrity_check`, compares row counts vs live (±5% tolerance). Telegram alert on divergence — closes the silent-corruption-stays-invisible-until-we-need-it case.

Bucket is configured server-side with multi-prefix lifecycle (audited live state — see `kb/findings/s3-existing-corpus-audit.md` §1): daily/ → GLACIER_IR @ 7d; journals/ → DEEP_ARCHIVE @ 30d; market_obs/ → GLACIER_IR @ 0d; bronze/ → DEEP_ARCHIVE @ 30d (D1.5); silver/ → GLACIER_IR @ 90d (D1.5); gold/ → Standard. **Snapshots never expire.** Cost ~$0.30/mo at year 5 for daily/; bronze/silver/gold projected per D0.3 §8. The `scripts/STATE_DB_BACKUP_SETUP.md` §2 template uses a daily/ cadence of `30d→GLACIER_IR→90d→DEEP_ARCHIVE` — DIVERGES from the audited 7d cadence; operators of the live bucket MUST `GET-merge-PUT` rather than verbatim re-run (see §2 Operator note).

IAM is paranoid: VPS writer creds have `s3:PutObject` + `s3:GetObject` + `s3:ListBucket` (the Get + List grants close the rclone HeadObject quirk per Path C ticket `86b9xgz66`) but NO `s3:DeleteObject*` — combined with the §2b bucket-policy Deny on `s3:DeleteObjectVersion`, a compromised VPS cannot delete prior snapshots (defense-in-depth). Post-Path-C the writer ALSO has GetObject read paths on all 6 archive prefixes — a strict widening of read scope over the pre-Path-C "PutObject only" posture, accepted because (a) the rclone HeadObject quirk forces it (the Get + List grants cover the two destination states — existing key vs new key — that rclone's pre-PUT probe hits across the collector's lifetime; see §3 prose for the GetObject-vs-ListBucket disclosure-rule breakdown), and (b) the immutability guarantee (the load-bearing property here) survives via the DeleteObjectVersion Deny. The scope widening is real: a compromised VPS can now enumerate + read historical journals/, market_obs/, and state.db snapshots that the live state.db doesn't contain — broader exfiltration surface than pre-Path-C. The immutability + ransomware-protection properties are preserved. Restore creds are read-only and live on the dev Mac in `~/.aws/credentials` profile `kalshi-state-db-restore`.

One-time bucket + IAM + lifecycle setup is operator-only — see `scripts/STATE_DB_BACKUP_SETUP.md` for the full runbook. After that, `bash scripts/ops/setup_state_db_backup_timer.sh` on the VPS handles everything (timers, rclone config, sentinel-upload probe). Re-runnable; idempotent.

**Drift-check note:** these timers live at `/etc/systemd/system/kalshi-state-db-*.{service,timer}` — outside `kalshi-bot.service`'s drift-check scope. Re-running `setup_state_db_backup_timer.sh` is the source-of-truth operation for them.

## market_observations_continuous archive (ticket 86b9xcdwg)

`market_observations_continuous` is the only retention-pruned table on the VPS — the hourly retention sweep DELETEs rows older than 14 days (`bot/snapshots/market_observations_snapshotter.py:539-575`). Without archival, ~35K NBBO rows/day are permanently lost. Nightly archive shipped via:

- `kalshi-market-obs-archive.{service,timer}` — daily 05:30 UTC (unchanged by 86b9zkp89; weekly market_obs volume is tiny and per-day archival is sufficient). Falls between the 04:00 and 08:00 every-4h state.db backup ticks. Reads rows for `target_date = today_utc - 13d` (day 13 of the 14d window — rows still exist for ≥1 more day) via a read-only SQLite connection, writes Parquet with internal zstd, then `rclone copyto s3prod:kalshi-bot-archive/market_obs/YYYY-MM-DD.parquet.zst`. Wrapped in `h4_run_with_alert.py` for Telegram failure alerts.

Idempotent: same date = S3 object overwrite. Bucket lifecycle routes `market_obs/` to Glacier IR from day 0 (rarely read, but want instant retrieval for research). Cost ~$0.02/mo at year 5.

Install with `bash scripts/ops/setup_market_obs_archive_timer.sh` on the VPS. Companion to `setup_state_db_backup_timer.sh` — expects that one to have already run (shares the `s3prod` rclone remote + bucket creds in `.env`).

**Drift-check note:** same posture as the state.db backup timers — `/etc/systemd/system/kalshi-market-obs-archive.*` lives outside `kalshi-bot.service`'s drift-check scope; re-run `setup_market_obs_archive_timer.sh` to update.

## journal_archives/ sync (ticket 86b9xgp7k)

`~/kalshi-bot-repo/journal_archives/` holds the per-tick forensic JSONL streams (`opportunity_journal_*`, `scan_journal_*`, `rejection_journal_*`, ...). `ops/rotate_journals.sh` prunes local archives with `find -mtime +14` (`ROTATE_LOCAL_RETENTION_DAYS`, i.e. ≥15 full days old; the live VPS value — earlier docs said 90 d and were wrong); without S3 archival they're gone forever.

### Journal rotation (`ops/rotate_journals.sh`, ticket 86bbvd50a, 2026-09-05)

Tracked in git since 2026-09-05. Every 4 h (`0 */4 * * *`) it atomically renames (`mv`, same filesystem) each journal ≥ 10 MiB to `journal_archives/<journal>_YYYY-MM-DDTHH.jsonl` and compresses it to `.zst` — the stamp carries the **UTC hour**. `mv` rather than the old copy-then-truncate because the bot opens the journal with mode `a` per append (no held handle): the rename loses nothing, whereas every line appended between `cp` finishing and the truncate was dropped, and the copy transiently doubled the largest journal on disk. The previous VPS-local, untracked copy used a date-only stamp; when PR #64 moved the cron from daily to every-4h the second+ run of each day hit `zstd: ... already exists; not overwritten`, the raw copy stayed, and the next run's `cp` clobbered it — 436 intermediate 4-h chunks per journal (opportunity 52.0 GB raw, scan 41.5 GB raw; 16 of every 24 h) were destroyed over 109 days and 30.2 GB of orphan raws filled the disk on 2026-09-04 (`kb/failures/vps-disk-full-journal-rotation-collision-sep05.md`). The script fails CLOSED before touching the live file (existing archive name / failed `mv` → ERROR, live untouched, exit 1), retries any leftover uncompressed raw in the archive dir on every run, and ends each run with `Done. Disk free: … errors=N`. Because cron ignores exit codes under the `>> rotation.log` redirect, `monitor_watchdog.py` watches `rotation.log` freshness (`journal_rotation`, 500 min) AND alerts on `errors=N>0` (`check_rotation_errors`, dedup `monitor_watchdog_journal_rotation_errors`). `ops/install.sh` validates it (`+x` + `bash -n`) on every install and prints the crontab line. Env seams for tests: `ROTATE_REPO_DIR`, `ROTATE_ARCHIVE_DIR`, `ROTATE_MIN_SIZE_BYTES`, `ROTATE_LOCAL_RETENTION_DAYS`, `ROTATE_JOURNALS`, `ROTATE_STAMP`. Pinned by `tests/contracts/test_rotate_journals_sh.py`.

Crontab line (operator-owned; **`SHELL=/bin/bash` must be the FIRST line of the crontab** — see "Crontab SHELL ordering" below):
```
0 */4 * * * /bin/bash /home/botuser/kalshi-bot-repo/ops/rotate_journals.sh >> /home/botuser/kalshi-bot-repo/journal_archives/rotation.log 2>&1
```
After switching the line, delete the legacy untracked `~/kalshi-bot-repo/rotate_journals.sh` (+ its `.bak-*` siblings) so nothing can run the date-only version again.

### Crontab SHELL ordering (RCA 2026-09-05, ticket 86bbvd50a)

cron applies `SHELL=` only to the lines BELOW it. On the VPS the directive sat mid-file (added 2026-05-19 with the phantom-reconcile line), so every line above it — `quiet_market_monitor`, `data_health_monitor`, `collector_health_monitor` — ran under `/bin/sh` (dash), where `source` is not a builtin: `sh: 1: source: not found`, and the `&&` chain died before the `>> log` redirect, so nothing was logged. Those three monitors have been DEAD since 2026-05-19 ~21:25 UTC (`~/collector_health.log` mtime), which is why the 80 % disk canary never fired while `/` sat at ~95 % for 4+ weeks. `monitor_watchdog.py` (below the directive) alerted `alerts_sent=3` every 10 min for 109 days — ~47K Telegram messages, unactioned. Rules: `SHELL=/bin/bash` on line 1 of the crontab; prefer `. venv/bin/activate` over `source` in cron lines; check `crontab -l | grep -n SHELL` whenever a cron job's log goes quiet.

- `kalshi-journal-archives-sync.{service,timer}` — every 4h, 30-min offset (00:30, 04:30, 08:30, 12:30, 16:30, 20:30 UTC); cadence revised from daily 04:30 UTC by ticket `86b9zkp89` (2026-05-17). Each tick fires 30 min AFTER its paired `rotate_journals.sh` tick (rotation cadence was SSH-changed from daily @04:00 UTC to every-4h on the hour on 2026-05-17 by the same Bronze-durability push; `rotate_journals.sh` was VPS-local until 2026-09-05 — it now lives at `ops/rotate_journals.sh`, see "Journal rotation" above). The 30-min offset is load-bearing: zstd compression of the most-recently-rotated journal must complete before sync, else `rclone --immutable` would treat the partial file as content divergence and surface exit 6. Uses `rclone copy --checksum --immutable` (one-way: upload-or-skip, never deletes from S3) from the local archives dir → `s3prod:kalshi-bot-archive/journals/`. Live (current, uncompressed) `*.jsonl` files AND `rotation.log` (which is appended-to on every run, every 4 h) are excluded via `--exclude` filter. Single-runner flock at `/var/lock/kalshi-journal-sync.lock`. Wrapped in `h4_run_with_alert.py` for Telegram failure alerts.

**WHY `copy` not `sync`** (R1 catch, ticket 86b9xgp7k): `rclone sync` mirror-deletes — when `rotate_journals.sh` prunes a journal locally at the 14-day boundary, `sync` would DELETE the S3 object too, defeating the entire archive. `rclone copy` is one-way.

Idempotent: re-runs are no-ops (rclone short-circuits per-file via S3 ETag). First run uploads the ~11 GB backlog (~33 days post-2026-04-10).

Install with `bash scripts/ops/setup_journal_archives_sync_timer.sh` on the VPS. Same companion-to-state.db posture as the market_obs timer (expects `s3prod` rclone remote already configured).

**Drift-check note:** same posture as siblings — `/etc/systemd/system/kalshi-journal-archives-sync.*` lives outside `kalshi-bot.service`'s drift-check scope; re-run `setup_journal_archives_sync_timer.sh` to update.

## D1.5 collector deploy (REQUIRES-APPROVAL discipline)

`ops/kalshi-collector.service` (SHIPPED 2026-05-16, ticket `86b9ypna4`) deploys the Data Corpus collector as a parallel systemd unit on the bot VPS. Key isolation knobs (pinned by `tests/contracts/test_kalshi_collector_systemd_unit.py`):

- **`CPUAffinity` — RETIRED 2026-05-19** (ticket `86ba12rv6`, umbrella `86ba12rf0`). At 754K+ tickers the single-vCPU pin saturated at 90% CPU on 1 core while vCPU-0 sat idle, driving ~33% sustained frame drop. The collector now floats across both vCPUs; the `Nice=10` polite-background posture below remains the load-bearing guarantee that bot scan latency cannot be starved. See `kb/decisions/collector-queue-saturation-fix-plan.md`.
- `Nice=10` — I/O-bound polite background (NOT -19; collector is not real-time).
- `MemoryHigh=1600M` + `MemoryMax=2048M` + `MemorySwapMax=0` — soft cgroup throttle at 1600M (78% of MemoryMax) originally added 2026-05-20 (umbrella `86ba12rf0`) at 400M/512M after the 2026-05-19 unexplained collector exits at 22:06 / 22:26 / 00:04 UTC correlated with peak memory at 472M / 512M cap (92%); raised to 1600M/2048M same day (ticket `86ba1h0cb`) in lockstep with the s-2vcpu-2gb → s-4vcpu-8gb droplet resize when post-resize universe growth (705K tickers measured 2026-05-20 vs runbook's 386K estimate) pushed peak cgroup memory to 513M during the universal-mode boot, crossing the prior 512M cap. `MemoryHigh` triggers kernel-side throttling at the soft cap, surfacing memory pressure in cgroup pressure counters BEFORE the hard SIGKILL at `MemoryMax`. The 0 swap is the failure mode (D0.2 §3.2 measured 47 MB/conn × 7-8 conns = ~376 MB on the prior 2GB box; the 2048M cap on the 8GB box leaves ~5.5GB physical headroom even with all four collectors + bot at cap).
- `LimitNOFILE=4096` — 6 conns × 3 channels × rotation + rclone need; default 1024 is tight.
- `Restart=on-failure` + `RestartSec=10s` — NOT the bot's `Restart=always` + `30s`. A clean `systemctl stop` must halt the collector deliberately (for credential rotation); non-zero exits restart after 10s.
- `EnvironmentFile=/home/botuser/.env.collector` — DEDICATED home-rooted env file (NOT the bot's repo-rooted `.env`). Lives OUTSIDE the cloned repo so `git reset --hard origin/main` deploys cannot wipe collector credentials.

### Operator runbook: provision `/home/botuser/.env.collector`

D1.5 ships the systemd unit + installer; the collector's env file is operator-provisioned (NOT in git, never written by deploys). On the VPS:

**Pre-flight (operator's dev Mac, before touching the VPS):** verify the bucket / writer-IAM / reader-IAM are at the post-Path-C 6-prefix shape per `scripts/STATE_DB_BACKUP_SETUP.md` §12 verification one-liners. If the bucket was provisioned before 2026-05-16, apply the per-section fork: §2 (lifecycle) requires `GET-merge-PUT` per the §2 Operator note (verbatim `put-bucket-lifecycle-configuration` would silently mutate the audited daily/ cadence from 7d → GLACIER_IR forever to the template's 30d → GLACIER_IR → 90d → DEEP_ARCHIVE); §2b (bucket policy), §3 (writer IAM), and §5 (reader IAM) `put-*-policy` calls ARE idempotent post-Path-C and safe to re-run verbatim. **Important:** verify §3 with the post-Path-C templates (this commit), NOT a git blame snapshot from before 2026-05-16 — pre-Path-C verbatim §3 would drop the Get/List grants live IAM carries for rclone (ticket `86b9xgz66`). A bucket whose `kalshi-state-db-backup-writer` IAM does NOT enumerate `bronze/*` will 403 the collector's first chunk upload, and the F6 invariant must be in place before `systemctl start kalshi-collector`.

```bash
cat > /home/botuser/.env.collector <<'ENV'
KALSHI_COLLECTOR_KEY_ID=<dedicated key id from Kalshi dashboard>
KALSHI_COLLECTOR_KEY_PATH=/home/botuser/.kalshi/kalshi-collector.pem
COLLECTOR_BRONZE_ROOT=/var/lib/kalshi-collector/bronze
COLLECTOR_CONN_COUNT=7
COLLECTOR_BATCH_SIZE=1000
COLLECTOR_REST_REFRESH_SECONDS=3600
RCLONE_REMOTE=s3prod
S3_BUCKET=kalshi-bot-archive
ENV
chmod 600 /home/botuser/.env.collector
chown botuser:botuser /home/botuser/.env.collector

# Bronze root preparation
sudo mkdir -p /var/lib/kalshi-collector/bronze
sudo chown -R botuser:botuser /var/lib/kalshi-collector
sudo chmod 750 /var/lib/kalshi-collector

# Provision the collector PEM (move + chmod from operator's dev machine)
mkdir -p /home/botuser/.kalshi
chmod 700 /home/botuser/.kalshi
# scp the PEM to /home/botuser/.kalshi/kalshi-collector.pem
chmod 600 /home/botuser/.kalshi/kalshi-collector.pem

# Install BOTH systemd units (kalshi-bot.service + kalshi-collector.service)
bash ops/install.sh
# install.sh validates: source file exists, wrapper executable, env-file
# present, ExecStart/WorkingDirectory/EnvironmentFile match expected paths,
# `systemd-analyze verify` accepts the unit. Failures abort BEFORE sudo cp.

# Start the collector (operator-decided timing; NOT auto-started by deploy.yml)
sudo systemctl start kalshi-collector
journalctl -u kalshi-collector -n 50  # boot logs clean?
ls -lh /var/lib/kalshi-collector/bronze/  # outbox/ + in_flight/ dirs created?

# Within 30 minutes of start: first chunk should land in S3 (bronze day-zero).
rclone lsf s3prod:kalshi-bot-archive/bronze/ | head
```

### Three D0.3 §12 operator decisions resolved at D1.5 kickoff

1. **Lifecycle Standard → DEEP_ARCHIVE @ 30d** (skip IA). Matches existing journals/ precedent; saves ~$140/yr. STATE_DB_BACKUP_SETUP.md §2 lifecycle JSON extended with bronze/silver/gold rules.
2. **`Nice=10`** (NOT -19, NOT 0). Pinned by `tests/contracts/test_kalshi_collector_systemd_unit.py::test_service_nice_is_10`.
3. **`KALSHI_COLLECTOR_KEY_ID` provisioning** — dedicated collector key (NOT the D0.2 test key). Generated via Kalshi dashboard, dropped into `.env.collector` + `.kalshi/kalshi-collector.pem` per the runbook above. D0.2's test key can be revoked once the collector is live.

### S3 bucket-side template alignment (D1.5 + Path C)

Before the first bronze chunk lands, four S3-side surfaces must be at the canonical 6-prefix shape (`daily/`, `journals/`, `market_obs/`, `bronze/`, `silver/`, `gold/`). Two of these surfaces (§2 lifecycle + §2b bucket-policy) constitute F6 from D0.1, closed by D1.5. The other two (§3 writer-IAM + §5 reader-IAM) are adjacent template-vs-live drift in the same class as F6 but outside F6's original scope.

1. **Lifecycle policy** (§2 of `scripts/STATE_DB_BACKUP_SETUP.md`) — F6 surface #1, closed by D1.5. Bronze/silver rules added (Standard → DEEP_ARCHIVE @ 30d for bronze; Standard → GLACIER_IR @ 90d for silver; gold no-op default).
2. **Bucket policy Deny** (§2b) — F6 surface #2, closed by D1.5. `s3:DeleteObjectVersion` deny statement Resource array expanded from `daily/*` only to all 6 prefixes (ransomware protection against compromised collector creds).
3. **Writer IAM** (§3) — adjacent template-vs-live drift, outside F6 scope. D1.5 expanded `s3:PutObject` Resource from `daily/*` only to all 6 prefixes (closes the bronze-write 403 gap). Path C (ticket `86b9xgz66`, 2026-05-16) added `s3:GetObject` + bucket-level `s3:ListBucket` to align the template with the live policy: rclone's pre-PUT `HeadObject` probe needs `s3:GetObject` to read existing keys without 403, AND `s3:ListBucket` to receive 404-instead-of-403 on non-existent keys; without both, the probe fails for at least one of the two destination states. **Without the Resource expansion, the collector's first `rclone copyto` returns 403, the KEEP-local-on-failure posture accumulates chunks in `outbox/`, and the 2 GB VPS fills the root volume within hours of bronze day-zero.**
4. **Reader IAM** (§5) — adjacent template-vs-live drift, outside F6 scope. D1.5 expanded `s3:GetObject` / `s3:GetObjectVersion` / `s3:RestoreObject` Resource lists to all 6 prefixes, preemptively unblocking the D2.x off-VPS DuckDB+dbt silver/gold ETL chain. Path C subsequently DROPPED the `ListBucketAndVersions` `Condition.StringLike` (cosmetic — see §5 prose); the reader can now `ListBucket` on the bucket root unconditionally, but `GetObject` is still gated to the 6 archive prefixes by the `ReadOnly` Sid Resource list.

On a fresh bucket: run §1-§5 verbatim from the runbook (templates are now at the post-Path-C 6-prefix shape with the rclone-compatible Actions). On an existing pre-D1.5 bucket: §1 (CreateBucket) is a no-op + §2 (lifecycle) requires `GET-merge-PUT` to preserve the audited daily/ cadence drift (see `scripts/STATE_DB_BACKUP_SETUP.md` §2 Operator note); §2b/§3/§5 (bucket-policy + writer-IAM + reader-IAM) IAM `put-*-policy` calls ARE idempotent post-Path-C and safe to re-run verbatim — pre-Path-C, verbatim §3 against the live bucket would have DROPPED the Get/List grants live IAM already carried, which is the gap Path C closes. See §12 "Bucket-side multi-prefix expansion" for the verification one-liners that catch a half-extended bucket BEFORE the collector starts.

### Off-switch

`sudo systemctl stop kalshi-collector` → bot unaffected. `sudo systemctl stop kalshi-bot` → collector unaffected. Verified structurally: separate process group, separate WS conns, separate API key (in dedicated `.env.collector`), zero `bot.*` imports (`.importlinter` enforced), separate disk path (`COLLECTOR_BRONZE_ROOT`), no shared SQLite writes.

The one residual shared failure surface is root filesystem disk-full — D1.6 (`86b9zk4we`) ships a passive `shutil.disk_usage` ≥ 80% used Telegram alert (via `scripts/ops/collector_health_monitor.py`, operator-installed cron every 5 min) that closes this gap.

## D2.5 Coinbase collector deploy (REQUIRES-APPROVAL discipline)

`ops/kalshi-coinbase-collector.service` (SHIPPED 2026-05-18, ticket `86b9znq4w`) deploys the Coinbase-side Data Corpus collector as a THIRD parallel systemd unit on the bot VPS, alongside `kalshi-bot.service` + `kalshi-collector.service`. Option B isolation posture (operator-decided at kickoff): separate process / separate cgroup / separate env file / separate bronze root / separate health sidecar. Key isolation knobs (pinned by `tests/contracts/test_kalshi_coinbase_collector_systemd_unit.py`):

- **NO `CPUAffinity`** — the 2-vCPU VPS lets the kernel scheduler float all tenants (bot + kalshi-collector + this unit) freely across both vCPUs. `Nice=10` (collector-tier polite background) vs `Nice=0` (bot) gates priority when CPU is contended; `MemoryMax` floor + tight `LimitNOFILE` cap the resource blast radius. (Pre-2026-05-19 ops/kalshi-collector.service had `CPUAffinity=1` pinning Kalshi to vCPU-1; retired under ticket `86ba12rv6` when the single-vCPU pin saturated at 90% CPU under 754K+ tickers.)
- `Nice=10` — same I/O-bound polite-background posture as kalshi-collector.
- `MemoryMax=384M` + `MemorySwapMax=0` — bumped 256M → 384M on 2026-05-30 when ADA-USD + BCH-USD were added for the 9-asset corpus (collector collects 9 products via `collector/coinbase_main_loop.COLLECTOR_PRODUCT_IDS`; the bot still trades 7 and the shared `coinbase_wire.DEFAULT_PRODUCT_IDS` stays at 7). Coinbase single-conn × 9 products × 5 channels (post-D2.5 level2_batch promotion) is structurally lighter than Kalshi 7-conn × ~21K-subs. Measured peak was 229M/256M (90%) at 7 products; 9 products project to ~295M, so 384M restores headroom (host has 5.4G free — the per-unit cap is the only constraint). Leaves room for worker queue (10K items) + zstd buffer + rclone overhead.
- `LimitNOFILE=512` — 1 conn × 5 channels × rotation + rclone needs ~30 fd typical; 512 gives ~10× headroom (tighter than Kalshi's 4096 to surface fd-leak regressions early).
- `Restart=on-failure` + `RestartSec=10s` — same lifecycle posture as kalshi-collector. A clean `systemctl stop` halts the unit deliberately (for env-file rotation); non-zero exits restart after 10s.
- `EnvironmentFile=/home/botuser/.env.coinbase-collector` — DEDICATED home-rooted env file (NOT the bot's repo-rooted `.env` AND NOT the Kalshi collector's `.env.collector`). Lives OUTSIDE the cloned repo so `git reset --hard origin/main` deploys cannot wipe Coinbase-side knobs.

### Operator runbook: provision `/home/botuser/.env.coinbase-collector`

D2.5 ships the systemd unit + installer extension; the env file is operator-provisioned (NOT in git, never written by deploys). On the VPS:

```bash
cat > /home/botuser/.env.coinbase-collector <<'ENV'
COINBASE_BRONZE_ROOT=/var/lib/kalshi-coinbase-collector/bronze
RCLONE_REMOTE=s3prod
S3_BUCKET=kalshi-bot-archive
ENV
chmod 600 /home/botuser/.env.coinbase-collector
chown botuser:botuser /home/botuser/.env.coinbase-collector

# No PEM / no KEY_ID — D2.5 ships public channels only (D2.1.5 narrowed
# the auth scope; HMAC private-channel support is deferred to a future
# Bit with corresponding auth.py body landing).

# Bronze root preparation
sudo mkdir -p /var/lib/kalshi-coinbase-collector/bronze
sudo chown -R botuser:botuser /var/lib/kalshi-coinbase-collector
sudo chmod 750 /var/lib/kalshi-coinbase-collector

# Install all THREE systemd units (kalshi-bot + kalshi-collector +
# kalshi-coinbase-collector) — ops/install.sh extends to a 3-unit
# parallel-array installer at D2.5.
bash ops/install.sh
# install.sh validates each unit: source file exists, wrapper
# executable, env-file present, ExecStart/WorkingDirectory/
# EnvironmentFile match expected paths, `systemd-analyze verify`
# accepts the unit. Failures abort BEFORE sudo cp.

# Start the Coinbase collector (operator-decided timing; NOT auto-
# started by deploy.yml).
sudo systemctl start kalshi-coinbase-collector
journalctl -u kalshi-coinbase-collector -n 50  # boot logs clean?
ls -lh /var/lib/kalshi-coinbase-collector/bronze/  # outbox/ + in_flight/?

# Within ~5 minutes (one rotation interval at low Coinbase load,
# faster with level2_batch on) the first chunk should land in S3.
rclone lsf s3prod:kalshi-bot-archive/bronze/coinbase_ws/ticker/ | head
rclone lsf s3prod:kalshi-bot-archive/bronze/coinbase_ws/level2_batch/ | head
```

### Sudoers NOPASSWD extension (pre-deploy-first prerequisite)

The D2.5 deploy.yml path-aware restart block uses `sudo -n /bin/systemctl restart kalshi-coinbase-collector`. The VPS's existing `/etc/sudoers.d/botuser-systemctl-restart` was extended at D1.5 to include `kalshi-collector` (see `feedback_vps_sudoers_collector_gap_may17`); D2.5 requires an ADDITIONAL extension for `kalshi-coinbase-collector`:

```bash
# As root on the VPS (one-time, before the first D2.5 deploy fires):
sudo visudo -f /etc/sudoers.d/botuser-systemctl-restart
# Add a line:
#   botuser ALL=(root) NOPASSWD: /bin/systemctl restart kalshi-coinbase-collector
```

Without this extension, the first D2.5-affecting deploy fires `sudo -n /bin/systemctl restart kalshi-coinbase-collector`, which fails immediately with exit 1 + a recognizable error — the deploy aborts BEFORE the broken restart silently leaves the unit un-restarted. The fail-loud posture matches the D1.5.2 R3-C1 lesson.

### Off-switch

`sudo systemctl stop kalshi-coinbase-collector` → bot + Kalshi collector unaffected. `sudo systemctl stop kalshi-collector` → Coinbase collector unaffected. `sudo systemctl stop kalshi-bot` → both collectors unaffected. Verified structurally: separate process groups, separate WS conns (different upstream hosts: Coinbase `ws-feed.exchange.coinbase.com` vs Kalshi WS), no shared `state.db`, no shared API keys, separate disk paths (`COINBASE_BRONZE_ROOT` vs `COLLECTOR_BRONZE_ROOT`), separate bronze_health.json sidecar files.

### D2.5 health monitoring

`scripts/ops/collector_health_monitor.py` extends to TRIPLE-TIER dispatch at B3-fu3 (2026-05-18, ticket `86b9zxb4c`): the 4 collector check functions (`check_disk`, `check_ws_reconnects`, `check_collector_active`, `check_dropped_frames`) run per-cron-tick for BOTH kalshi-collector AND kalshi-coinbase-collector (D2.5 dual-tier shape), PLUS a 5th check function `check_insert_evaluated_opportunity_failures` runs for kalshi-bot. The bot-tier check alerts on `insert_evaluated_opportunity failed` WARNINGs in the bot journal — post-B3-fu7 (`86ba067mg`, 2026-05-18) the marker substring matches WARN sites across `bot/scanner/__init__.py` + `bot/state.py`, of which 46 are narrowed to `sqlite3.OperationalError` (2 B3-fu2/fu6 + 44 B3-fu7) and 12 COMPLEX sites still use bare `except Exception:` (deferred per-site review — try-bodies contain non-DB compute that needs case-by-case judgment). A hit at a narrowed site is a genuine DB error; a hit at one of the 12 bare-except sister sites is an exception (DB or otherwise — NameError / UnboundLocalError / AttributeError / KeyError class). Per-tier dedup-key prefixes (`d1_6_<check>` for the Kalshi collector / `d2_5_<check>` for the Coinbase collector / `b3_fu3_<check>` for the bot tier) keep alert dedup independent — a Kalshi disk-pressure alert does NOT dedup-suppress a Coinbase disk-pressure alert (their underlying mount points are structurally separate), and the bot-tier alert never collides with either collector tier.

## watchdog.py (2-min health monitor, Sprint 14-A Bit X.5 relocation)

`ops/watchdog.py` is the standalone cron-driven bot health monitor (relocated from repo root to `ops/` by Sprint 14-A Bit X.5, 2026-05-17; umbrella ticket `86b9zfbt8`). Operator-installed via VPS crontab; runs every 2 minutes; sends Telegram alerts on bot-down, log-stall, loss streaks, low balance, high memory, and Layer 3.5 orphan-DB holders (`scripts/backfill/*` PIDs mid-session). Not a systemd unit — `bot/orphan_db_watchdog.py` and `ops/kalshi-bot.service` are separate concerns.

Key file invariants:
- `STATE_FILE = Path(__file__).parent.parent / ".watchdog_state.json"` and `DB_PATH = Path(__file__).parent.parent / "state.db"` — the `.parent.parent` traversal anchors both lookups at the repo root, NOT under `ops/`. A future move that breaks this anchoring would silently point STATE_FILE / DB_PATH at the wrong files; the contract is pinned by `tests/contracts/test_sprint_14_a_x5_watchdog_move.py::test_watchdog_paths_resolve_to_repo_root`.
- `ops/__init__.py` (empty) exists so `import ops.watchdog` resolves from the test suite. Removing it would break `tests/integration/test_watchdog_orphan_detection.py` collection.
- Usage docstring (line 7 of the file) carries the canonical cron line — operators copy it into the crontab verbatim.

### Crontab line

```
*/2 * * * * cd ~/kalshi-bot-repo && . venv/bin/activate && set -a && . ~/.env && set +a && python3 ops/watchdog.py
```

### Post-Bit-X.5 operator action (one-time, post-merge)

The crontab lives in `~/` (NOT in-repo) and is operator-edited:

```bash
ssh -t botuser@$VPS_HOST 'crontab -e'
# Edit the existing watchdog cron line: `python3 watchdog.py` → `python3 ops/watchdog.py`
```

The `git reset --hard origin/main` deploy step moves the file but does NOT touch the crontab. Until the operator edits it, the cron line invokes the (now-missing) repo-root `watchdog.py` and the 2-min monitor silently no-ops — the bot itself continues running but Layer-3.5-orphan + service-down + log-stall + loss-streak alerts go dark. Verify post-edit with `crontab -l | grep watchdog` and watch for the next scheduled Telegram heartbeat / silence pattern.

## D1.8 weather collector deploy (REQUIRES-APPROVAL discipline)

`ops/kalshi-weather-collector.service` (SHIPPED 2026-05-18, ticket `86ba0duck`) deploys the weather bronze collector as a FOURTH parallel systemd unit on the bot VPS, alongside `kalshi-bot.service` + `kalshi-collector.service` + `kalshi-coinbase-collector.service`. First non-WS bronze source. Key isolation knobs (pinned by `tests/contracts/test_weather_collector_systemd_unit.py`):

- **NO `CPUAffinity`** — with 4 tenants on a 2-vCPU box (bot + Kalshi + Coinbase + Weather), the kernel scheduler floats them all across both vCPUs; `Nice=10` (collectors) vs `Nice=0` (bot) gates priority. Weather is the LIGHTEST tier — HTTP-poll at 60-min cadence × 19 cities × ≤4 channels per cycle (57 envelopes typical = 3 forecast × 19 cities; 76 max = +19 archive_observed at 06:00 UTC).
- `Nice=10` — same I/O-bound polite-background posture as the WS collectors.
- `MemoryMax=128M` + `MemorySwapMax=0` — HALF of Coinbase's pre-2026-05-30 256M cap (Coinbase now 384M; and a quarter of Kalshi's 512M). Measured working set ~0.4 MB; 128M provides ~300× headroom for retry buffers + zstd compression.
- `LimitNOFILE=512` — same as Coinbase. 4 writers × 2 rotation files + rclone subprocess + HTTP keep-alive sockets ≈ 30 fd typical; 512 gives ~15× headroom.
- `Restart=on-failure` + `RestartSec=10s` — same lifecycle posture as the other 2 collector units.
- `EnvironmentFile=/home/botuser/.env.weather-collector` — DEDICATED home-rooted env file (NOT shared with bot/Kalshi/Coinbase env files).

### Operator runbook: provision `/home/botuser/.env.weather-collector`

D1.8 ships the systemd unit + installer extension; the env file is operator-provisioned. On the VPS:

```bash
cat > /home/botuser/.env.weather-collector <<'ENV'
WEATHER_BRONZE_ROOT=/var/lib/kalshi-weather-collector/bronze
RCLONE_REMOTE=s3prod
S3_BUCKET=kalshi-bot-archive
WEATHER_POLL_INTERVAL_SECONDS=3600
ENV
chmod 600 /home/botuser/.env.weather-collector
chown botuser:botuser /home/botuser/.env.weather-collector

# No PEM / no KEY_ID — Open-Meteo is free + keyless (10K req/day quota,
# per kb-research/bot/weather-nwp-analysis.md). Combined burn from bot
# (~5,472/day) + collector at 60-min (~1,387/day incl. archive_observed) ≈ 6,859/day — well
# under the 10K quota.

# Bronze root preparation
sudo mkdir -p /var/lib/kalshi-weather-collector/bronze
sudo chown -R botuser:botuser /var/lib/kalshi-weather-collector
sudo chmod 750 /var/lib/kalshi-weather-collector

# Install all FOUR systemd units — ops/install.sh extends to N=4 at D1.8.
bash ops/install.sh

# Start the weather collector (operator-decided timing; NOT auto-started
# by deploy.yml — D1.8 ships REQUIRES-APPROVAL).
sudo systemctl start kalshi-weather-collector
journalctl -u kalshi-weather-collector -n 50  # boot logs clean?
ls -lh /var/lib/kalshi-weather-collector/bronze/  # outbox/ + in_flight/?

# Within ~60 minutes (one rotation interval at 60-min cadence) the first
# chunk lands in S3. Weather day-zero.
rclone lsf s3prod:kalshi-bot-archive/bronze/open_meteo/ensemble_gfs/ | head
rclone lsf s3prod:kalshi-bot-archive/bronze/open_meteo/forecast_hrrr/ | head
```

### Sudoers NOPASSWD extension (pre-deploy prerequisite)

Mirrors the D1.5 + D2.5 sudoers extension pattern:

```bash
# As root on the VPS (one-time, before manual restart works without sudo prompt):
sudo visudo -f /etc/sudoers.d/botuser-systemctl-restart
# Add a line:
#   botuser ALL=(root) NOPASSWD: /bin/systemctl restart kalshi-weather-collector
```

### Off-switch

`sudo systemctl stop kalshi-weather-collector` → bot + Kalshi collector + Coinbase collector all unaffected. Inverse holds: stopping any other unit leaves the weather collector running. Verified structurally: separate process group, separate HTTP keep-alive sockets, no shared `state.db`, no API keys, separate disk path (`WEATHER_BRONZE_ROOT`).

### D1.8 health monitoring

`scripts/ops/collector_health_monitor.py` extends to FOUR-TIER dispatch at D1.8 (post-B3-fu3 triple-tier shape adds a fourth `kalshi-weather-collector` tier with dedup-key prefix `d1_8_*`). Weather subset of 3 checks:
- `check_disk` — bronze accumulates on `/var/lib/kalshi-weather-collector`
- `check_collector_active` — `systemctl is-active` gate
- `check_dropped_frames` — schema-parity with Kalshi+Coinbase sidecars (weather has no worker-queue drops by design; sidecar emits `total_dropped_frames=0` unconditionally)
- NOT `check_ws_reconnects` — HTTP polling has no persistent WS connection; a log-marker filter would never match (always-OK false negative).

## Phantom reconcile cron (TBD, 2026-05-19)

`scripts/ops/phantom_reconcile_monitor.py` is the hourly cron-driven Telegram-alerting wrapper around `scripts/audit/phantom_pnl_audit.py`. Pre-this-Bit the audit was manual-only (operator ran `--run-id may18` on 2026-05-18; nothing since); per-PnL phantom drift was silently accumulating. Memory record `feedback_use_corrected_pnl_always` confirms the magnitude (2026-05-18: raw 7d -$98.92 vs corrected -$39.02; $60 swing on one ticker).

Operator install:
```
crontab -e
# Add (offset 7 min past the hour to avoid auditor.py at :00).
# Sources BOTH env files: TELEGRAM_* live in ~/.env (user-scope cron
# convention shared by auditor.py / analyst.py / watchdog.py); KALSHI_*
# live in ~/kalshi-bot-repo/.env (systemd EnvironmentFile= for kalshi-bot.service).
7 * * * * cd /home/botuser/kalshi-bot-repo && set -a && . ~/.env && . .env && set +a && . venv/bin/activate && python3 scripts/ops/phantom_reconcile_monitor.py >> ~/phantom_reconcile.log 2>&1
```

Pre-install operator checks:
- `~/.env` exports `TELEGRAM_BOT_TOKEN` + `TELEGRAM_CHAT_ID` (production VPS layout; same file the existing cron jobs source).
- `~/kalshi-bot-repo/.env` exports `KALSHI_API_KEY` (or `KALSHI_API_KEY_ID`) + `KALSHI_PRIVATE_KEY_PATH` (same file the kalshi-bot systemd unit sources via `EnvironmentFile=`).
- The exact split may differ on a fresh setup — what matters is that ALL four keys reach the cron process after the `set -a && . ... && set +a` block. Verify with `crontab -l` + a manual dry-run (`cd ~/kalshi-bot-repo && set -a && . ~/.env && . .env && set +a && python3 -c 'import os; [print(k, "=<set>" if os.environ.get(k) else "=MISSING") for k in ("KALSHI_API_KEY","KALSHI_API_KEY_ID","KALSHI_PRIVATE_KEY_PATH","TELEGRAM_BOT_TOKEN","TELEGRAM_CHAT_ID")]'`).
- `~/kalshi-bot-repo/phantom_reconcile_dedup.json` + `.lock` siblings writable (auto-created on first run; gitignored).

Three alert classes with day-stable cross-process dedup via JSON sidecar at `./phantom_reconcile_dedup.json` (configurable via `PHANTOM_RECONCILE_DEDUP_PATH` env or `--dedup-sidecar`):
- **SUMMARY** (prefix `phantom_reconcile_summary`) — aggregated material drift `|delta_pnl_cents| >= $5`, top 5 by |Δpnl|.
- **UNVERIFIED** (prefix `phantom_reconcile_unverified`) — Kalshi REST left ≥50% (ticker, side) pairs unverified (visibility-degraded signal; closes the silent-fail class where n_divergent=0 looks healthy but really we're blind).
- **CRASH** (prefix `phantom_reconcile_crash`) — auditor itself raised `Exception` (NOT `BaseException` — `KeyboardInterrupt` / `SystemExit` propagate so operator Ctrl-C aborts cleanly and missing-env `sys.exit(1)` from `load_client()` lands in journalctl).

`audit_run_id` is day-granular `auto-YYYY-MM-DD` (UTC). 24 hourly cron firings within a UTC day share one run_id; `INSERT OR REPLACE` on `UNIQUE(audit_run_id, ticker, side)` keeps `phantom_corrections` to AT MOST one row per (day, ticker, side). Downstream LEFT JOIN consumers see no row multiplication. The `auto-` prefix namespace-isolates from operator manual `--run-id <name>` runs.

Cron convention: always exits 0 (cron's mail-spool reservation; signal goes via Telegram, not exit code).

## Root-filesystem usage alert in `monitor_watchdog.py` (ticket 86bbvd50a, 2026-09-05)

`scripts/ops/monitor_watchdog.py` (cron `*/10`, BELOW the `SHELL=` directive → alive) now also checks `WATCHED_DISKS = (WatchedDisk("root", "/", 85),)` via `shutil.disk_usage` — percentage computed as `used / (used + free)` so it equals `df`'s Use% (not `used/total`, which hides the ext4 reserved blocks) — and Telegram-alerts with dedup key `monitor_watchdog_disk_root`; alert text is printed to `~/monitor_watchdog.log` too so the log keeps a history. It is an INDEPENDENT second canary — `collector_health_monitor.check_disk` (80 %) still exists but was dead for 3.5 months because of the crontab SHELL ordering above. 85 % ≈ 7 GB free on the 48 GB root (≈ 25 days at the 0.28 GB/day raw-leak rate of the incident); healthy steady state is ~30 % used (~14 GB: repo + venv + state.db + in-flight bronze; measured 29 % on 2026-09-05 post-recovery). Pinned by `tests/contracts/test_monitor_watchdog_disk.py`.

## Collector boot on the persisted ticker set (ticket 86bbvdcat, 2026-09-05)

`kalshi-collector` writes the last successful REST ticker set to `COLLECTOR_TICKER_CACHE_PATH` (default `/var/lib/kalshi-collector/last_tickers.json`, ~10 MB) after every refresh. On boot, if the file exists the archivers are planned from it: the first WS conn starts ~5 min after ActiveEnter (3.0 min restart sweep + salvage, then the ~2 min plan/frame-build/connect the 09-05 timeline showed after the page-through), all 7 within ~7 min (6 × 20 s stagger — no leading stagger before the first conn) — confirm on the first post-deploy restart; the refresher's immediate first tick re-pages the universe in the background and force-reconnects only if the set changed. Measured before the change (2026-09-05 12:13Z restart): synchronous page-through 54.9 min (~18,500 pages ≈ 3.7M rows → 358,625 tickers after `COLLECTOR_EXCLUDED_SERIES`), first `kalshi_ws_connected` 59.8 min after ActiveEnter, cgroup MemoryPeak 1.68 GB / 2 GB, and 17 GB of REST bronze parked on local disk because the drain thread started after the fetch (it now starts first). `bronze_health.json` gains additive keys `state` (`booting`/`running`), `state_since`, `ticker_set_source` (`persisted`/`rest`/`file`/`empty`), `ticker_cache_age_seconds`, `rest_refresh{in_progress,last_duration_seconds,last_ticker_count,last_outcome,...}`; `collector_health_monitor.check_boot_state` alerts (`d1_6_boot_state`) after 1200 s of `booting`. The FIRST restart after deploy still pays the synchronous page-through (no cache yet); every later restart boots from the cache. Delete the cache file to force a synchronous boot.

## D1.11.a ESPN collector deploy (REQUIRES-APPROVAL discipline)

`ops/kalshi-espn-collector.service` (SHIPPED 2026-05-19, ticket `86ba0ppy0`) deploys the ESPN bronze collector as a FIFTH parallel systemd unit on the bot VPS. Second non-WS bronze source after D1.8 weather. Key isolation knobs (pinned by `tests/contracts/test_kalshi_espn_collector_systemd_unit.py`): NO `CPUAffinity` (5 tenants on 2-vCPU box; ESPN floats on either vCPU), `Nice=10`, `MemoryMax=256M` + `MemorySwapMax=0` (matched Coinbase's original cap; Coinbase bumped to 384M on 2026-05-30 for the 9-asset corpus, ESPN unchanged; `ESPNArchiver.poll_once` is sequential single-`Session` so peak in-flight is 1 response × ~50 KB + 24 BronzeWriter buffers × ~8 KB ≈ ~250 KB working set — 256M matches Coinbase precedent rather than right-sizing tightly), `LimitNOFILE=512`, `Restart=on-failure` + `RestartSec=10s`, `EnvironmentFile=/home/botuser/.env.espn-collector`.

### Operator runbook: provision `/home/botuser/.env.espn-collector`

```bash
cat > /home/botuser/.env.espn-collector <<'ENV'
ESPN_BRONZE_ROOT=/var/lib/kalshi-espn-collector/bronze
RCLONE_REMOTE=s3prod
S3_BUCKET=kalshi-bot-archive
ESPN_POLL_INTERVAL_SECONDS=60
ENV
chmod 600 /home/botuser/.env.espn-collector
chown botuser:botuser /home/botuser/.env.espn-collector

# No PEM / no KEY_ID — site.api.espn.com is free + keyless.

sudo mkdir -p /var/lib/kalshi-espn-collector/bronze
sudo chown -R botuser:botuser /var/lib/kalshi-espn-collector
sudo chmod 750 /var/lib/kalshi-espn-collector

bash ops/install.sh   # 5-unit installer post-D1.11.a

sudo systemctl start kalshi-espn-collector
journalctl -u kalshi-espn-collector -n 50
rclone lsf s3prod:kalshi-bot-archive/bronze/espn/nba/ | head
```

### Sudoers NOPASSWD extension (pre-deploy prerequisite)

```bash
sudo visudo -f /etc/sudoers.d/botuser-systemctl-restart
# Add: botuser ALL=(root) NOPASSWD: /bin/systemctl restart kalshi-espn-collector
```

### Off-switch + health monitoring

`sudo systemctl stop kalshi-espn-collector` → bot + Kalshi/Coinbase/Weather collectors all unaffected. `scripts/ops/collector_health_monitor.py` extends to FIVE-TIER dispatch (SIX post-B2a-1 venue-L2) with dedup prefix `d1_11_*`; same HTTP-poll subset as weather (disk + collector_active + dropped_frames; NO ws_reconnects) **plus, post-86bbvqhyr (2026-09-06), `http_errors`** — `check_espn_http_errors` reads the per-league rolling-1h `espn_http_status_1h` block that `collector/espn_main_loop.py::write_bronze_health_sidecar` publishes (schema_version stays 1, additive) and alerts at >50% non-200 for any league with ≥10 polls in the window (dedup `d1_11_http_errors`). The bot tier gained `sports_eval_silence` in the same Bit (`check_sports_eval_silence`: the sports engine's own `sports_health.json` sidecar (written next to `state.db` every 30 s tick by `SportsEngine._note_tick`) reports ≥20 ticks with live ESPN games in its 24 h window AND 0 `product_type='sports'` rows in `evaluated_opportunities` for that window — no journal decode: a 24 h `journalctl -g` pull measured ~50 s on the VPS, so the engine self-reports (24 h, not shorter: healthy days have 1-43 sports rows clustered 17:00-05:00 UTC, so a 3 h window false-alarms several times a day); read-only sqlite at `STATE_DB_PATH` else `~/kalshi-bot-repo/state.db`; dedup `b3_fu3_sports_eval_silence`). Both exist because ESPN 403'd the `KalshiBot/1.0` User-Agent for ~5 weeks while disk / collector_active / dropped_frames stayed green — "chunks landing" ≠ "data landing" (`kb/failures/espn-403-user-agent-silent-outage-sep06.md`). **deploy.yml gained a path-aware `kalshi-espn-collector` restart block in the same Bit** (R4 of the gate found the ESPN collector had NO restart block, so a push touching `collector/espn_archiver.py` would have left the old process — old UA, no `espn_http_status_1h` key — running while the deploy looked green; mirror of the D2.5 Coinbase block, path set `collector/espn_archiver.py` / `collector/espn_main_loop.py` / `collector/writer.py` / `collector/uploader.py` / `kalshi_wire/ws_client.py` / `ops/kalshi-espn-collector.service` / `espn-collector-start.sh` / `requirements.txt`, unit-drift guard, `sudo -n`; pinned by `tests/contracts/test_deploy_yml_path_aware_espn_collector_restart.py`; `kalshi-weather-collector` still has no block — followup ticket to file — ClickUp was disconnected at ship time on 2026-09-06). Post-deploy verification, in this order: (1) `python3 scripts/ops/espn_live_probe.py` on the VPS (exit 0 on 200, 1 on other status, 2 on transport error); (2) `espn_http_status_1h` present in `/var/lib/kalshi-espn-collector/bronze_health.json` with `non_200 == 0`; (3) `http_status: 200` in the next bronze chunk; (4) `sports_health.json` appears in `~/kalshi-bot-repo/` within one 30 s tick. Sports bronze restarts at the first 200 chunk after deploy — ESPN keeps no scoreboard history.

### LEAGUES_ESPN drift discipline

`collector/espn_archiver.py` defines `LEAGUES_ESPN` (24 entries at D1.11.a ship) mirroring `bot.engines.sports_data.LEAGUES` enabled+espn-eligible subset. Contract test `tests/contracts/test_collector_espn_archiver.py::test_leagues_espn_mirrors_bot_leagues` fails RED on drift — operator must update both sides + `sudo -n /bin/systemctl restart kalshi-espn-collector` so the new channel registers as a BronzeWriter on next boot.

## B2a-1 venue-L2 collector deploy (REQUIRES-APPROVAL discipline)

`ops/kalshi-venue-l2-collector.service` (ticket `86ba1zf5j`) deploys the multi-venue lean L2 bronze recorder as a SIXTH parallel systemd unit on the bot VPS — the synthetic-RTI validation collector (B2a). It opens Kraken + Bitstamp + Gemini public L2 WebSockets in one asyncio thread (mirrors `bot/feeds/cross_exchange.py`; NOT a wire library) and archives RAW frames via `collector/writer.py` + `kalshi_wire.build_envelope`. Coinbase L2 bronze is already live via `kalshi-coinbase-collector` (`level2_batch`). **Starting this unit begins the ~14d bronze-accumulation clock for the offline RMSE gate** (`scripts/research/synthetic_rti_rmse.py`); per-asset RMSE ≤15 bps (≤25 HYPE) → build B2b, else escalate to B3 (paid CFB). Key isolation knobs (pinned by `tests/contracts/test_kalshi_venue_l2_collector_systemd_unit.py`): NO `CPUAffinity` (kernel floats it; `Nice=10` gates priority), `MemoryMax=512M` + `MemorySwapMax=0` (3 WS conns + periodic large Gemini full-book snapshots + the synchronous zstd compress-whole-in-flight spike at rotation — heavier than Coinbase single-conn 384M), `LimitNOFILE=512`, `Restart=on-failure` + `RestartSec=10s`, `EnvironmentFile=/home/botuser/.env.venue-l2-collector`.

### Operator runbook: provision `/home/botuser/.env.venue-l2-collector`

```bash
cat > /home/botuser/.env.venue-l2-collector <<'ENV'
VENUE_L2_BRONZE_ROOT=/var/lib/kalshi-venue-l2-collector/bronze
RCLONE_REMOTE=s3prod
S3_BUCKET=kalshi-bot-archive
ENV
chmod 600 /home/botuser/.env.venue-l2-collector
chown botuser:botuser /home/botuser/.env.venue-l2-collector

# No PEM / no KEY_ID — Kraken + Bitstamp + Gemini public L2 WS are keyless.

sudo mkdir -p /var/lib/kalshi-venue-l2-collector/bronze
sudo chown -R botuser:botuser /var/lib/kalshi-venue-l2-collector
sudo chmod 750 /var/lib/kalshi-venue-l2-collector

bash ops/install.sh   # 6-unit installer post-B2a-1

sudo systemctl start kalshi-venue-l2-collector
journalctl -u kalshi-venue-l2-collector -n 50
rclone lsf s3prod:kalshi-bot-archive/bronze/kraken_ws/book/ | head
```

### Sudoers NOPASSWD extension (pre-deploy prerequisite)

```bash
sudo visudo -f /etc/sudoers.d/botuser-systemctl-restart
# Add: botuser ALL=(root) NOPASSWD: /bin/systemctl restart kalshi-venue-l2-collector
```

### Off-switch + health monitoring

`sudo systemctl stop kalshi-venue-l2-collector` → bot + Kalshi/Coinbase/Weather/ESPN collectors all unaffected. Per-venue kill via env (e.g. set `VENUE_L2_BRONZE_ROOT` to a venue subset is not supported; stop the unit to halt all three). `scripts/ops/collector_health_monitor.py` extends to SIX-TIER dispatch with dedup prefix `b2a_*`; FULL WS check set (disk + ws_reconnects + collector_active + dropped_frames — UNLIKE the HTTP-poll weather/ESPN tiers, since the recorder runs 3 persistent WS conns). The `ws_reconnects` check is passed `log_marker="venue_l2_ws_disconnected"` (the recorder's per-venue disconnect marker).

## Files
- `kalshi-bot.service` — bot systemd unit, source of truth
- `kalshi-collector.service` — D1.5 collector systemd unit, source of truth
- `kalshi-coinbase-collector.service` — D2.5 Coinbase collector systemd unit, source of truth (ticket `86b9znq4w`, 2026-05-18)
- `kalshi-weather-collector.service` — D1.8 weather collector systemd unit, source of truth (ticket `86ba0duck`, 2026-05-18)
- `kalshi-espn-collector.service` — D1.11.a ESPN collector systemd unit, source of truth (ticket `86ba0ppy0`, 2026-05-19)
- `kalshi-venue-l2-collector.service` — B2a-1 venue-L2 collector systemd unit, source of truth (ticket `86ba1zf5j`, 2026-05-28)
- `install.sh` — 6-unit install + reload (validates + enables ALL SIX — kalshi-bot + kalshi-collector + kalshi-coinbase-collector + kalshi-weather-collector + kalshi-espn-collector + kalshi-venue-l2-collector) + validates `ops/rotate_journals.sh` (+x, `bash -n`) and prints its crontab line (ticket 86bbvd50a)
- `watchdog.py` — 2-min cron health monitor (Sprint 14-A Bit X.5, 2026-05-17)
- `__init__.py` — empty file; makes `ops/` a Python package so `import ops.watchdog` resolves

## Revert / rollback

`git revert` of a Bit that touched `ops/` removes the file from the working tree but does NOT modify `/etc/systemd/system/kalshi-bot.service` on the VPS. After a revert that drops `ops/kalshi-bot.service`, the on-VPS unit retains the last-installed ExecStart and source-of-truth is gone (the bot keeps working — start.sh still exists post-revert in unchanged form — but the drift-detection invariant is broken).

Recovery options:
- (a) **Re-attempt the Bit** — preferred. Re-shipping restores the source-of-truth file + re-runs install.sh.
- (b) **Manually edit `/etc/systemd/system/kalshi-bot.service`** to restore the prior ExecStart, then `sudo systemctl daemon-reload`. Use only if (a) is blocked.
