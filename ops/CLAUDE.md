# Ops

Production infrastructure (systemd units, install scripts, deploy hooks). Tracked in git so changes are reviewable + drift-detectable.

## Conventions
- **Source of truth.** `ops/kalshi-bot.service` IS the systemd unit. Do NOT edit `/etc/systemd/system/kalshi-bot.service` directly. Editing this directory triggers a drift check on the next deploy — see "Editing the unit" below.
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

Bucket is configured server-side with lifecycle: Standard → Glacier IR (30d) → Deep Archive (90d). **Snapshots never expire.** Cost ~$0.30/mo at year 5.

IAM is paranoid: VPS writer creds have `s3:PutObject` only (no Delete/Get/List), so a compromised VPS cannot ransomware backups. Restore creds are read-only and live on the dev Mac in `~/.aws/credentials` profile `kalshi-state-db-restore`.

One-time bucket + IAM + lifecycle setup is operator-only — see `scripts/STATE_DB_BACKUP_SETUP.md` for the full runbook. After that, `bash scripts/ops/setup_state_db_backup_timer.sh` on the VPS handles everything (timers, rclone config, sentinel-upload probe). Re-runnable; idempotent.

**Drift-check note:** these timers live at `/etc/systemd/system/kalshi-state-db-*.{service,timer}` — outside `kalshi-bot.service`'s drift-check scope. Re-running `setup_state_db_backup_timer.sh` is the source-of-truth operation for them.

## Files
- `kalshi-bot.service` — systemd unit, source of truth
- `install.sh` — one-time install + reload

## Revert / rollback

`git revert` of a Bit that touched `ops/` removes the file from the working tree but does NOT modify `/etc/systemd/system/kalshi-bot.service` on the VPS. After a revert that drops `ops/kalshi-bot.service`, the on-VPS unit retains the last-installed ExecStart and source-of-truth is gone (the bot keeps working — start.sh still exists post-revert in unchanged form — but the drift-detection invariant is broken).

Recovery options:
- (a) **Re-attempt the Bit** — preferred. Re-shipping restores the source-of-truth file + re-runs install.sh.
- (b) **Manually edit `/etc/systemd/system/kalshi-bot.service`** to restore the prior ExecStart, then `sudo systemctl daemon-reload`. Use only if (a) is blocked.
