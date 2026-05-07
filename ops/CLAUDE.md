# Ops

Production infrastructure (systemd units, install scripts, deploy hooks). Tracked in git so changes are reviewable + drift-detectable.

## Conventions
- **Source of truth.** `ops/kalshi-bot.service` IS the systemd unit. Do NOT edit `/etc/systemd/system/kalshi-bot.service` directly. Editing this directory triggers a drift check on the next deploy — see "Editing the unit" below.
- **Install:** one-time per VPS via `bash ops/install.sh`. Re-run after any change in this directory.
- **Drift detection:** `.github/workflows/deploy.yml` diffs `systemctl cat kalshi-bot` against `git show <deploy-sha>:ops/kalshi-bot.service` before `git reset --hard`. If the on-VPS unit differs, the deploy aborts and the operator must re-sync via the recovery one-liner below, then re-trigger the deploy. Local invariant: `tests/test_ops_systemd_unit_matches_repo.py` (6 tests, wired into `make test-fast`). Bit 2.0.5.2 of repo modularization plan.

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

## Files
- `kalshi-bot.service` — systemd unit, source of truth
- `install.sh` — one-time install + reload

## Revert / rollback

`git revert` of a Bit that touched `ops/` removes the file from the working tree but does NOT modify `/etc/systemd/system/kalshi-bot.service` on the VPS. After a revert that drops `ops/kalshi-bot.service`, the on-VPS unit retains the last-installed ExecStart and source-of-truth is gone (the bot keeps working — start.sh still exists post-revert in unchanged form — but the drift-detection invariant is broken).

Recovery options:
- (a) **Re-attempt the Bit** — preferred. Re-shipping restores the source-of-truth file + re-runs install.sh.
- (b) **Manually edit `/etc/systemd/system/kalshi-bot.service`** to restore the prior ExecStart, then `sudo systemctl daemon-reload`. Use only if (a) is blocked.
