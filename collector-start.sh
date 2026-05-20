#!/bin/bash
# Wrapper invoked by ops/kalshi-collector.service (D1.5 systemd unit,
# requires-approval). Mirrors the start.sh pattern for the bot:
# `set -e` so a venv-activate or .env-source failure exits non-zero
# and systemd's Restart=on-failure kicks in.
#
# Per D0.3 §6 isolation (strengthened at D1.5):
#   - DEDICATED env file at /home/botuser/.env.collector (NOT the bot's
#     /home/botuser/kalshi-bot-repo/.env). Lives OUTSIDE the repo so
#     credential rotation for KALSHI_COLLECTOR_KEY_ID does not require
#     a redeploy, and `git reset --hard origin/main` cannot wipe it.
#   - SAME venv as the bot (Python deps overlap; collector/ has zero
#     bot.* imports but shares websocket-client + cryptography + zstd).
#   - DIFFERENT entrypoint: `python3 -m collector` (NOT `-m bot`).
#
# `.env.collector` is loaded twice on purpose: (1) systemd via the
# unit's `EnvironmentFile=` directive sets values into the process
# env before exec, (2) bash `source .env.collector` is belt-and-
# suspenders for any future codepath that invokes collector-start.sh
# outside systemd. Both loaders require strict KEY=VALUE lines.
#
# D1.5 SHIPPED: ops/kalshi-collector.service is operator-installed via
# `bash ops/install.sh` on the VPS; this script is its ExecStart target.
set -eo pipefail
cd /home/botuser/kalshi-bot-repo
source /home/botuser/kalshi-bot-repo/venv/bin/activate
source /home/botuser/.env.collector
exec python3 -O -m collector
