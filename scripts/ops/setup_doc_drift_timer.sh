#!/usr/bin/env bash
# Setup systemd timer for daily doc drift check on VPS.
# Run once: bash scripts/ops/setup_doc_drift_timer.sh
#
# Creates:
#   /etc/systemd/system/kalshi-doc-drift.service
#   /etc/systemd/system/kalshi-doc-drift.timer
#
# Runs daily at 06:00 UTC. Sends Telegram alert if drift found.
# Logs go to journalctl.

set -euo pipefail

SERVICE_FILE="/etc/systemd/system/kalshi-doc-drift.service"
TIMER_FILE="/etc/systemd/system/kalshi-doc-drift.timer"
BOT_DIR="/home/botuser/kalshi-bot-repo"
VENV_PYTHON="${BOT_DIR}/venv/bin/python3"
SCRIPT="${BOT_DIR}/scripts/audit/doc_drift_check.py"

echo "=== Installing kalshi-doc-drift systemd timer ==="

# Service unit — EnvironmentFile loads .env for Telegram creds
sudo tee "$SERVICE_FILE" > /dev/null <<EOF
[Unit]
Description=Kalshi Documentation Drift Check
After=network.target

[Service]
Type=oneshot
User=botuser
WorkingDirectory=${BOT_DIR}
EnvironmentFile=${BOT_DIR}/.env
ExecStart=${VENV_PYTHON} ${SCRIPT} --telegram
# Exit code 1 = drift found (expected), don't treat as failure
SuccessExitStatus=1
TimeoutStartSec=60
EOF

# Timer unit — daily at 06:00 UTC
sudo tee "$TIMER_FILE" > /dev/null <<EOF
[Unit]
Description=Run documentation drift check daily

[Timer]
OnCalendar=*-*-* 06:00:00
AccuracySec=5min
Persistent=true

[Install]
WantedBy=timers.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable kalshi-doc-drift.timer
sudo systemctl start kalshi-doc-drift.timer

echo ""
echo "=== Timer installed and started ==="
echo ""
systemctl status kalshi-doc-drift.timer --no-pager
echo ""
echo "View logs:  journalctl -u kalshi-doc-drift.service --no-pager -n 20"
echo "Next run:   systemctl list-timers kalshi-doc-drift.timer"
echo "Run now:    sudo systemctl start kalshi-doc-drift.service"
