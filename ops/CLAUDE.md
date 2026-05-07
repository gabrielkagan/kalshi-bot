# Ops

Production infrastructure (systemd units, install scripts, deploy hooks). Tracked in git so changes are reviewable + drift-detectable.

## Conventions
- **Source of truth.** `ops/kalshi-bot.service` IS the systemd unit. Do NOT edit `/etc/systemd/system/kalshi-bot.service` directly — edit here, push, then `bash ops/install.sh` on VPS.
- **Install:** one-time per VPS via `bash ops/install.sh`. Re-run when this directory changes.
- **Drift detection:** planned for Bit 2.0.5.2 — a CI step will diff `systemctl cat kalshi-bot` against `ops/kalshi-bot.service` pre-deploy and fail on mismatch. Until that ships, drift is operator-detected via `bash ops/install.sh` re-run on suspicion.

## Install / re-install on VPS
```bash
cd ~/kalshi-bot-repo
git pull origin main
bash ops/install.sh   # prompts for sudo password
```

## Files
- `kalshi-bot.service` — systemd unit, source of truth
- `install.sh` — one-time install + reload

## Revert / rollback

`git revert` of a Bit that touched `ops/` removes the file from the working tree but does NOT modify `/etc/systemd/system/kalshi-bot.service` on the VPS. After a revert that drops `ops/kalshi-bot.service`, the on-VPS unit retains the last-installed ExecStart and source-of-truth is gone (the bot keeps working — start.sh still exists post-revert in unchanged form — but the drift-detection invariant is broken).

Recovery options:
- (a) **Re-attempt the Bit** — preferred. Re-shipping restores the source-of-truth file + re-runs install.sh.
- (b) **Manually edit `/etc/systemd/system/kalshi-bot.service`** to restore the prior ExecStart, then `sudo systemctl daemon-reload`. Use only if (a) is blocked.
