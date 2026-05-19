#!/bin/bash
# Wrapper invoked by ops/kalshi-weather-collector.service (D1.8 systemd
# unit, ticket 86ba0duck, requires-approval). Mirrors
# coinbase-collector-start.sh (D2.5) — `set -e` so a venv-activate or
# .env-source failure exits non-zero and systemd's Restart=on-failure
# kicks in.
#
# Per D0.3 §6 isolation extended at D1.8:
#   - DEDICATED env file at /home/botuser/.env.weather-collector (NOT
#     the bot's /home/botuser/kalshi-bot-repo/.env AND NOT the Kalshi
#     collector's /home/botuser/.env.collector AND NOT the Coinbase
#     collector's /home/botuser/.env.coinbase-collector). Lives OUTSIDE
#     the repo so any future credential rotation does not require a
#     redeploy, and `git reset --hard origin/main` cannot wipe it.
#   - SAME venv as the bot + Kalshi collector + Coinbase collector
#     (shared Python deps: requests + zstandard + cryptography).
#     Per D0.3 §6 "SAME venv" convention enforced at D1.5.1 by
#     deploy.yml's `pip install -r requirements.txt` step.
#   - DIFFERENT entrypoint: `python3 -m collector.weather_main_loop`
#     (NOT the Kalshi `-m collector` or Coinbase `-m collector.coinbase_main_loop`).
#
# `.env.weather-collector` is loaded twice on purpose: (1) systemd via
# the unit's `EnvironmentFile=` directive sets values into the process
# env before exec, (2) bash `source .env.weather-collector` is belt-
# and-suspenders for any future codepath that invokes this script
# outside systemd. Both loaders require strict KEY=VALUE lines.
#
# D1.8 SHIPPED: ops/kalshi-weather-collector.service is operator-
# installed via `bash ops/install.sh` on the VPS; this script is its
# ExecStart target.
set -eo pipefail
cd /home/botuser/kalshi-bot-repo
source /home/botuser/kalshi-bot-repo/venv/bin/activate
source /home/botuser/.env.weather-collector
exec python3 -m collector.weather_main_loop
