# Ops

Production infrastructure (systemd units, install scripts, deploy hooks). Tracked in git so changes are reviewable + drift-detectable.

## Conventions
- **Source of truth.** `ops/kalshi-bot.service` IS the bot systemd unit; `ops/kalshi-collector.service` (D1.5 SHIPPED 2026-05-16, ticket `86b9ypna4`) IS the Data Corpus collector systemd unit. Do NOT edit `/etc/systemd/system/kalshi-*.service` directly. Editing this directory triggers a drift check on the next deploy for the bot unit — see "Editing the unit" below. (The collector unit has no equivalent CI drift check yet — file a followup if drift becomes a problem in practice; the `make test-unit` invariants pin shape but not on-VPS divergence.)
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

`state.db` is backed up nightly at 06:00 UTC to S3 via two systemd timers installed by `scripts/ops/setup_state_db_backup_timer.sh`:

- `kalshi-state-db-backup.{service,timer}` — daily 06:00 UTC. Uses `sqlite3.Connection.backup()` (NOT rsync — a literal rsync of a hot WAL DB tears pages; see `kb/decisions/auto-research-phase-0a-plan-may09.md` RCA). Snapshot → zstd compress → `rclone copyto s3prod:bucket/daily/state-db-YYYY-MM-DD.db.zst`. Wrapped in `h4_run_with_alert.py` for Telegram failure alerts.
- `kalshi-state-db-restore-verify.{service,timer}` — Sunday 07:00 UTC. Pulls latest snapshot, runs `PRAGMA integrity_check`, compares row counts vs live (±5% tolerance). Telegram alert on divergence — closes the silent-corruption-stays-invisible-until-we-need-it case.

Bucket is configured server-side with multi-prefix lifecycle (audited live state — see `kb/findings/s3-existing-corpus-audit.md` §1): daily/ → GLACIER_IR @ 7d; journals/ → DEEP_ARCHIVE @ 30d; market_obs/ → GLACIER_IR @ 0d; bronze/ → DEEP_ARCHIVE @ 30d (D1.5); silver/ → GLACIER_IR @ 90d (D1.5); gold/ → Standard. **Snapshots never expire.** Cost ~$0.30/mo at year 5 for daily/; bronze/silver/gold projected per D0.3 §8. The `scripts/STATE_DB_BACKUP_SETUP.md` §2 template uses a daily/ cadence of `30d→GLACIER_IR→90d→DEEP_ARCHIVE` — DIVERGES from the audited 7d cadence; operators of the live bucket MUST `GET-merge-PUT` rather than verbatim re-run (see §2 Operator note).

IAM is paranoid: VPS writer creds have `s3:PutObject` + `s3:GetObject` + `s3:ListBucket` (the Get + List grants close the rclone HeadObject quirk per Path C ticket `86b9xgz66`) but NO `s3:DeleteObject*` — combined with the §2b bucket-policy Deny on `s3:DeleteObjectVersion`, a compromised VPS cannot delete prior snapshots (defense-in-depth). Post-Path-C the writer ALSO has GetObject read paths on all 6 archive prefixes — a strict widening of read scope over the pre-Path-C "PutObject only" posture, accepted because (a) the rclone HeadObject quirk forces it (the Get + List grants cover the two destination states — existing key vs new key — that rclone's pre-PUT probe hits across the collector's lifetime; see §3 prose for the GetObject-vs-ListBucket disclosure-rule breakdown), and (b) the immutability guarantee (the load-bearing property here) survives via the DeleteObjectVersion Deny. The scope widening is real: a compromised VPS can now enumerate + read historical journals/, market_obs/, and state.db snapshots that the live state.db doesn't contain — broader exfiltration surface than pre-Path-C. The immutability + ransomware-protection properties are preserved. Restore creds are read-only and live on the dev Mac in `~/.aws/credentials` profile `kalshi-state-db-restore`.

One-time bucket + IAM + lifecycle setup is operator-only — see `scripts/STATE_DB_BACKUP_SETUP.md` for the full runbook. After that, `bash scripts/ops/setup_state_db_backup_timer.sh` on the VPS handles everything (timers, rclone config, sentinel-upload probe). Re-runnable; idempotent.

**Drift-check note:** these timers live at `/etc/systemd/system/kalshi-state-db-*.{service,timer}` — outside `kalshi-bot.service`'s drift-check scope. Re-running `setup_state_db_backup_timer.sh` is the source-of-truth operation for them.

## market_observations_continuous archive (ticket 86b9xcdwg)

`market_observations_continuous` is the only retention-pruned table on the VPS — the hourly retention sweep DELETEs rows older than 14 days (`bot/snapshots/market_observations_snapshotter.py:539-575`). Without archival, ~35K NBBO rows/day are permanently lost. Nightly archive shipped via:

- `kalshi-market-obs-archive.{service,timer}` — daily 05:30 UTC (between `rotate_journals.sh` @04:00 and `kalshi-state-db-backup` @06:00). Reads rows for `target_date = today_utc - 13d` (day 13 of the 14d window — rows still exist for ≥1 more day) via a read-only SQLite connection, writes Parquet with internal zstd, then `rclone copyto s3prod:kalshi-bot-archive/market_obs/YYYY-MM-DD.parquet.zst`. Wrapped in `h4_run_with_alert.py` for Telegram failure alerts.

Idempotent: same date = S3 object overwrite. Bucket lifecycle routes `market_obs/` to Glacier IR from day 0 (rarely read, but want instant retrieval for research). Cost ~$0.02/mo at year 5.

Install with `bash scripts/ops/setup_market_obs_archive_timer.sh` on the VPS. Companion to `setup_state_db_backup_timer.sh` — expects that one to have already run (shares the `s3prod` rclone remote + bucket creds in `.env`).

**Drift-check note:** same posture as the state.db backup timers — `/etc/systemd/system/kalshi-market-obs-archive.*` lives outside `kalshi-bot.service`'s drift-check scope; re-run `setup_market_obs_archive_timer.sh` to update.

## journal_archives/ sync (ticket 86b9xgp7k)

`~/kalshi-bot-repo/journal_archives/` holds the per-tick forensic JSONL streams (`opportunity_journal_*`, `scan_journal_*`, `rejection_journal_*`, ...). `rotate_journals.sh` deletes them at the 90-day local retention boundary; without S3 archival they're gone forever.

- `kalshi-journal-archives-sync.{service,timer}` — daily 04:30 UTC (30 min AFTER `rotate_journals.sh` @04:00 so yesterday's journal is fully zstd-compressed before upload). Uses `rclone copy --checksum --immutable` (one-way: upload-or-skip, never deletes from S3) from the local archives dir → `s3prod:kalshi-bot-archive/journals/`. `--immutable` surfaces content divergence as exit 6 (bug/tampering alert). Live current-day `*.jsonl` files AND `rotation.log` (which is appended-to daily) are excluded via `--exclude` filter. Single-runner flock at `/var/lock/kalshi-journal-sync.lock`. Wrapped in `h4_run_with_alert.py` for Telegram failure alerts.

**WHY `copy` not `sync`** (R1 catch, ticket 86b9xgp7k): `rclone sync` mirror-deletes — when `rotate_journals.sh` prunes a journal locally at the 90-day boundary, `sync` would DELETE the S3 object too, defeating the entire archive. `rclone copy` is one-way.

Idempotent: re-runs are no-ops (rclone short-circuits per-file via S3 ETag). First run uploads the ~11 GB backlog (~33 days post-2026-04-10).

Install with `bash scripts/ops/setup_journal_archives_sync_timer.sh` on the VPS. Same companion-to-state.db posture as the market_obs timer (expects `s3prod` rclone remote already configured).

**Drift-check note:** same posture as siblings — `/etc/systemd/system/kalshi-journal-archives-sync.*` lives outside `kalshi-bot.service`'s drift-check scope; re-run `setup_journal_archives_sync_timer.sh` to update.

## D1.5 collector deploy (REQUIRES-APPROVAL discipline)

`ops/kalshi-collector.service` (SHIPPED 2026-05-16, ticket `86b9ypna4`) deploys the Data Corpus collector as a parallel systemd unit on the bot VPS. Key isolation knobs (pinned by `tests/contracts/test_kalshi_collector_systemd_unit.py`):

- `CPUAffinity=1` — pins collector to vCPU-1 (bot keeps vCPU-0 uncontended; 2-vCPU VPS, sustained CPU contention is real even at Nice=10).
- `Nice=10` — I/O-bound polite background (NOT -19; collector is not real-time).
- `MemoryMax=512M` + `MemorySwapMax=0` — kernel-kill the collector before it OOMs the 2GB box; 0 swap is the failure mode (D0.2 §3.2 measured 47 MB/conn × 7-8 conns = ~376 MB; 512 MB cap leaves writer/uploader headroom).
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

The one residual shared failure surface is root filesystem disk-full — D1.6 ships the `df < 20%` alerting that closes this gap.

## Files
- `kalshi-bot.service` — bot systemd unit, source of truth
- `kalshi-collector.service` — D1.5 collector systemd unit, source of truth
- `install.sh` — multi-unit install + reload (validates + enables BOTH)

## Revert / rollback

`git revert` of a Bit that touched `ops/` removes the file from the working tree but does NOT modify `/etc/systemd/system/kalshi-bot.service` on the VPS. After a revert that drops `ops/kalshi-bot.service`, the on-VPS unit retains the last-installed ExecStart and source-of-truth is gone (the bot keeps working — start.sh still exists post-revert in unchanged form — but the drift-detection invariant is broken).

Recovery options:
- (a) **Re-attempt the Bit** — preferred. Re-shipping restores the source-of-truth file + re-runs install.sh.
- (b) **Manually edit `/etc/systemd/system/kalshi-bot.service`** to restore the prior ExecStart, then `sudo systemctl daemon-reload`. Use only if (a) is blocked.
