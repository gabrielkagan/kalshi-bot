#!/bin/bash
# Wrapper invoked by ops/kalshi-bot.service (Bit 2.0.5.1). Load-bearing
# once on-VPS unit's ExecStart points here — `set -e` guarantees a
# venv-activate failure exits non-zero so systemd treats it as a crash
# (Restart=always retries) instead of silently falling back to the
# system python3 with missing dependencies.
#
# `.env` is loaded twice on purpose: (1) systemd via the unit's
# `EnvironmentFile=` directive sets values into the process env
# before exec, (2) bash `source .env` is belt-and-suspenders for any
# future codepath that invokes start.sh outside systemd. Both loaders
# require strict `KEY=VALUE` lines — no shell metacharacters, no
# unquoted spaces in values, no `$(...)` expansion — because systemd's
# parser is KEY=VALUE-strict and `set -e` causes bash to exit on a
# `source` failure.
set -eo pipefail
cd /home/botuser/kalshi-bot-repo
source /home/botuser/kalshi-bot-repo/venv/bin/activate
source /home/botuser/kalshi-bot-repo/.env
exec python3 -m bot
