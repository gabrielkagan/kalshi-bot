#!/usr/bin/env bash
# Setup systemd timers for H-4 daily backfills on VPS.
# Run once: bash scripts/setup_h4_cron.sh
#
# Per kb/decisions/phase-h-forward-going-capture-required-may02.md
# (decision matrix), H-4a/b/c use Option C — daily cron over the last
# 24h of new rows. The backfill scripts themselves are idempotent
# (WHERE col IS NULL); this script just installs systemd timers to
# invoke them daily, staggered to avoid simultaneous DB / API contention.
#
# Creates 3 service+timer pairs:
#   /etc/systemd/system/kalshi-h4-gdelt.{service,timer}        (04:00 UTC)
#   /etc/systemd/system/kalshi-h4-glassnode.{service,timer}    (04:30 UTC)
#   /etc/systemd/system/kalshi-h4-cryptocompare.{service,timer} (05:00 UTC)
#
# Logs go to journalctl per-service. Failures additionally trigger a
# Telegram alert via scripts/h4_run_with_alert.py (so multi-day H-4
# outages don't stay invisible until the v2 acceptance gate fires
# weeks later).
#
#   journalctl -u kalshi-h4-gdelt.service --no-pager -n 50
#
# First-run / catch-up note: each backfill processes ALL rows where
# its target column IS NULL, starting from a checkpoint. With ~80K
# historical backfill rows + recent live_ws rows, the FIRST few daily
# runs may approach (or hit) TimeoutStartSec while draining the
# backlog. systemd's checkpoint-resume + the IS NULL filter means no
# data is lost — day N+1 picks up where day N stopped. Steady state
# (after backlog drain) is ~minutes per service.
#
# Re-runnable: tee overwrites unit files, daemon-reload picks up changes.

set -euo pipefail

BOT_DIR="/home/botuser/kalshi-bot-repo"
VENV_PYTHON="/home/botuser/kalshi-bot-repo/venv/bin/python3"
DB="/home/botuser/kalshi-bot-repo/state.db"
WRAPPER="/home/botuser/kalshi-bot-repo/scripts/h4_run_with_alert.py"
# EnvironmentFile prefix `-` makes systemd tolerate a missing .env
# (operator may run install on a fresh box before .env lands). On a
# real deploy .env is required by start.sh so this is a safety belt
# only; preflight below catches the live-misconfiguration case.
ENV_FILE_DIRECTIVE="EnvironmentFile=-/home/botuser/kalshi-bot-repo/.env"

# ── preflight: warn if optional API keys are absent in the live .env ──
# H-4b (Glassnode) silently skips paid metrics without GLASSNODE_API_KEY.
# Without this warning the operator finds out only at v2 acceptance gate.
# Pattern requires .+ so an empty value (`GLASSNODE_API_KEY=`) also triggers
# the warning — empty value still produces silent skip in glassnode_backfill.py.
if [ -f "/home/botuser/kalshi-bot-repo/.env" ]; then
    if ! grep -qE '^GLASSNODE_API_KEY=.+' "/home/botuser/kalshi-bot-repo/.env" 2>/dev/null; then
        echo ""
        echo "WARNING: GLASSNODE_API_KEY not set (or empty) in .env."
        echo "  H-4b will silently skip paid metrics (e.g."
        echo "  btc_exchange_inflow_24h_zscore stays NULL forever)."
        echo "  v2 acceptance gate (null_pct < 0.10 per phase-h doc)"
        echo "  will block at deploy time. Set the key now or accept"
        echo "  the column drop. Continuing anyway in 5s..."
        sleep 5
    fi
fi

echo "=== Installing H-4 daily backfill systemd timers ==="

# ── H-4a: GDELT (04:00 UTC) ───────────────────────────────────────────
sudo tee /etc/systemd/system/kalshi-h4-gdelt.service > /dev/null <<EOF
[Unit]
Description=Kalshi H-4a: GDELT news event backfill (daily)
After=network.target

[Service]
Type=oneshot
User=botuser
WorkingDirectory=${BOT_DIR}
${ENV_FILE_DIRECTIVE}
ExecStart=${VENV_PYTHON} ${WRAPPER} --label gdelt -- ${VENV_PYTHON} ${BOT_DIR}/scripts/gdelt_backfill.py --db ${DB}
TimeoutStartSec=3600
EOF

sudo tee /etc/systemd/system/kalshi-h4-gdelt.timer > /dev/null <<EOF
[Unit]
Description=Daily timer for Kalshi H-4a (GDELT)

[Timer]
OnCalendar=*-*-* 04:00:00
AccuracySec=1min
Persistent=true

[Install]
WantedBy=timers.target
EOF

# ── H-4b: Glassnode (04:30 UTC) ───────────────────────────────────────
sudo tee /etc/systemd/system/kalshi-h4-glassnode.service > /dev/null <<EOF
[Unit]
Description=Kalshi H-4b: Glassnode on-chain metrics backfill (daily)
After=network.target

[Service]
Type=oneshot
User=botuser
WorkingDirectory=${BOT_DIR}
${ENV_FILE_DIRECTIVE}
ExecStart=${VENV_PYTHON} ${WRAPPER} --label glassnode -- ${VENV_PYTHON} ${BOT_DIR}/scripts/glassnode_backfill.py --db ${DB}
TimeoutStartSec=3600
EOF

sudo tee /etc/systemd/system/kalshi-h4-glassnode.timer > /dev/null <<EOF
[Unit]
Description=Daily timer for Kalshi H-4b (Glassnode)

[Timer]
OnCalendar=*-*-* 04:30:00
AccuracySec=1min
Persistent=true

[Install]
WantedBy=timers.target
EOF

# ── H-4c: CryptoCompare (05:00 UTC) ───────────────────────────────────
sudo tee /etc/systemd/system/kalshi-h4-cryptocompare.service > /dev/null <<EOF
[Unit]
Description=Kalshi H-4c: CryptoCompare news sentiment backfill (daily)
After=network.target

[Service]
Type=oneshot
User=botuser
WorkingDirectory=${BOT_DIR}
${ENV_FILE_DIRECTIVE}
ExecStart=${VENV_PYTHON} ${WRAPPER} --label cryptocompare -- ${VENV_PYTHON} ${BOT_DIR}/scripts/cryptocompare_news_backfill.py --db ${DB}
TimeoutStartSec=3600
EOF

sudo tee /etc/systemd/system/kalshi-h4-cryptocompare.timer > /dev/null <<EOF
[Unit]
Description=Daily timer for Kalshi H-4c (CryptoCompare)

[Timer]
OnCalendar=*-*-* 05:00:00
AccuracySec=1min
Persistent=true

[Install]
WantedBy=timers.target
EOF

# ── enable + start ────────────────────────────────────────────────────
sudo systemctl daemon-reload

sudo systemctl enable kalshi-h4-gdelt.timer
sudo systemctl start  kalshi-h4-gdelt.timer

sudo systemctl enable kalshi-h4-glassnode.timer
sudo systemctl start  kalshi-h4-glassnode.timer

sudo systemctl enable kalshi-h4-cryptocompare.timer
sudo systemctl start  kalshi-h4-cryptocompare.timer

echo ""
echo "=== H-4 timers installed and started ==="
echo ""
systemctl list-timers 'kalshi-h4-*' --no-pager
echo ""
echo "View logs (per source):"
echo "  journalctl -u kalshi-h4-gdelt.service        --no-pager -n 50"
echo "  journalctl -u kalshi-h4-glassnode.service    --no-pager -n 50"
echo "  journalctl -u kalshi-h4-cryptocompare.service --no-pager -n 50"
echo ""
echo "Run a backfill on demand:"
echo "  sudo systemctl start kalshi-h4-gdelt.service"
