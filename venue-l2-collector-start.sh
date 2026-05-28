#!/bin/bash
# Wrapper invoked by ops/kalshi-venue-l2-collector.service (B2a-1 systemd
# unit, ticket 86ba1zf5j, requires-approval — deploy starts the ~14d
# bronze-accumulation clock for the synthetic-RTI RMSE validation gate).
# Mirrors coinbase-collector-start.sh (D2.5) — `set -e` so a venv-activate
# or .env-source failure exits non-zero and systemd's Restart=on-failure
# kicks in.
#
# Per D0.3 §6 isolation:
#   - DEDICATED env file at /home/botuser/.env.venue-l2-collector (NOT the
#     bot's repo-rooted .env AND NOT any other collector's env file). Lives
#     OUTSIDE the repo so deploy-tunable knobs survive `git reset --hard
#     origin/main`.
#   - SAME venv as the bot + other collectors (shared deps: websockets +
#     zstandard + orjson). Per D0.3 §6 "SAME venv" convention enforced at
#     D1.5.1 by deploy.yml's `pip install -r requirements.txt` step.
#   - DIFFERENT entrypoint: `python3 -m collector.venue_l2_main_loop`.
#
# `.env.venue-l2-collector` carries VENUE_L2_BRONZE_ROOT / RCLONE_REMOTE /
# S3_BUCKET (+ optional VENUE_L2_HEALTH_SIDECAR_PATH). NO PEM, NO KEY_ID —
# all three venues use free PUBLIC L2 WS (no auth).
#
# B2a-1 SHIPPED: ops/kalshi-venue-l2-collector.service is operator-
# installed via `bash ops/install.sh` on the VPS; this script is its
# ExecStart target.
set -eo pipefail
cd /home/botuser/kalshi-bot-repo
source /home/botuser/kalshi-bot-repo/venv/bin/activate
source /home/botuser/.env.venue-l2-collector
exec python3 -m collector.venue_l2_main_loop
