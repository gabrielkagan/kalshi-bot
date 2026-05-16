#!/bin/bash
# Wrapper invoked by ops/kalshi-collector.service (D1.5 systemd unit,
# requires-approval). Mirrors the start.sh pattern for the bot:
# `set -e` so a venv-activate or .env-source failure exits non-zero
# and systemd's Restart=always kicks in.
#
# Per D0.3 §6 isolation:
#   - SAME .env file as the bot (KALSHI_COLLECTOR_KEY_ID lives there;
#     D0.3 §12 item #3 pending operator decision on test-key rotation
#     vs new-key provisioning).
#   - SAME venv as the bot (Python deps overlap; collector/ has zero
#     bot.* imports but shares websocket-client + cryptography + zstd).
#   - DIFFERENT entrypoint: `python3 -m collector` (NOT `-m bot`).
#
# D1.1 stub: this script is not yet operator-installed on the VPS
# (D1.5 lays down ops/kalshi-collector.service + extends ops/install.sh
# to wire the unit). Locking the path-shape here so D1.5 has a stable
# ExecStart target.
set -eo pipefail
cd /home/botuser/kalshi-bot-repo
source /home/botuser/kalshi-bot-repo/venv/bin/activate
source /home/botuser/kalshi-bot-repo/.env
exec python3 -m collector
