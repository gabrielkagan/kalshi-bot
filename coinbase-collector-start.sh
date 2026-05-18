#!/bin/bash
# Wrapper invoked by ops/kalshi-coinbase-collector.service (D2.5 systemd
# unit, ticket 86b9znq4w, requires-approval). Mirrors collector-start.sh
# (D1.5 Kalshi wrapper) — `set -e` so a venv-activate or .env-source
# failure exits non-zero and systemd's Restart=on-failure kicks in.
#
# Per D0.3 §6 isolation extended at D2.5:
#   - DEDICATED env file at /home/botuser/.env.coinbase-collector (NOT
#     the bot's /home/botuser/kalshi-bot-repo/.env AND NOT the Kalshi
#     collector's /home/botuser/.env.collector). Lives OUTSIDE the repo
#     so any future credential rotation (HMAC for private channels in
#     a later Bit) does not require a redeploy, and `git reset --hard
#     origin/main` cannot wipe it.
#   - SAME venv as the bot + Kalshi collector (shared Python deps:
#     websockets + zstandard + cryptography). Per D0.3 §6 "SAME venv"
#     convention enforced at D1.5.1 by deploy.yml's `pip install -r
#     requirements.txt` step.
#   - DIFFERENT entrypoint: `python3 -m collector.coinbase_main_loop`
#     (NOT `-m collector` which is the Kalshi-side entrypoint).
#
# `.env.coinbase-collector` is loaded twice on purpose: (1) systemd via
# the unit's `EnvironmentFile=` directive sets values into the process
# env before exec, (2) bash `source .env.coinbase-collector` is belt-
# and-suspenders for any future codepath that invokes this script
# outside systemd. Both loaders require strict KEY=VALUE lines.
#
# D2.5 SHIPPED: ops/kalshi-coinbase-collector.service is operator-
# installed via `bash ops/install.sh` on the VPS; this script is its
# ExecStart target.
set -eo pipefail
cd /home/botuser/kalshi-bot-repo
source /home/botuser/kalshi-bot-repo/venv/bin/activate
source /home/botuser/.env.coinbase-collector
exec python3 -m collector.coinbase_main_loop
