#!/usr/bin/env bash
# Setup systemd timer for audit_cron.py on VPS.
# Run once: bash scripts/ops/setup_audit_cron.sh
#
# Creates:
#   /etc/systemd/system/kalshi-audit.service
#   /etc/systemd/system/kalshi-audit.timer
#
# The timer runs every 30 minutes. Logs go to journalctl.

set -euo pipefail

SERVICE_FILE="/etc/systemd/system/kalshi-audit.service"
TIMER_FILE="/etc/systemd/system/kalshi-audit.timer"
BOT_DIR="/home/botuser/kalshi-bot-repo"
VENV_PYTHON="${BOT_DIR}/venv/bin/python3"
SCRIPT="${BOT_DIR}/scripts/audit/audit_cron.py"
DB="${BOT_DIR}/state.db"

echo "=== Installing kalshi-audit systemd timer ==="

# Service unit
sudo tee "$SERVICE_FILE" > /dev/null <<EOF
[Unit]
Description=Kalshi Bot Audit Metrics Pre-Computation
After=network.target

[Service]
Type=oneshot
User=botuser
WorkingDirectory=${BOT_DIR}
ExecStart=${VENV_PYTHON} ${SCRIPT} --db ${DB}
TimeoutStartSec=60
EOF

# Timer unit
sudo tee "$TIMER_FILE" > /dev/null <<EOF
[Unit]
Description=Run Kalshi audit metrics every 30 minutes

[Timer]
OnBootSec=5min
OnUnitActiveSec=30min
AccuracySec=1min
Persistent=true

[Install]
WantedBy=timers.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable kalshi-audit.timer
sudo systemctl start kalshi-audit.timer

echo ""
echo "=== Timer installed and started ==="
echo ""
systemctl status kalshi-audit.timer --no-pager
echo ""
echo "View logs:  journalctl -u kalshi-audit.service --no-pager -n 20"
echo "Next run:   systemctl list-timers kalshi-audit.timer"
echo "Run now:    sudo systemctl start kalshi-audit.service"
