#!/usr/bin/env bash
# Install systemd timer for full audit runs 4x/day on VPS.
# Run once: bash scripts/setup_full_audit_timer.sh
#
# Schedule: 00:15, 06:15, 12:15, 18:15 UTC
# (offset 15 min from existing audit_cron 30-min timer to avoid collision)
#
# Creates:
#   /etc/systemd/system/kalshi-full-audit.service
#   /etc/systemd/system/kalshi-full-audit.timer

set -euo pipefail

SERVICE_FILE="/etc/systemd/system/kalshi-full-audit.service"
TIMER_FILE="/etc/systemd/system/kalshi-full-audit.timer"
BOT_DIR="/home/botuser/kalshi-bot-repo"

echo "=== Installing kalshi-full-audit systemd timer ==="

# Service unit
sudo tee "$SERVICE_FILE" > /dev/null <<EOF
[Unit]
Description=Kalshi Bot Full Audit (5 modules + alerts)
After=network.target

[Service]
Type=oneshot
User=botuser
WorkingDirectory=${BOT_DIR}
Environment="PATH=${BOT_DIR}/venv/bin:/usr/local/bin:/usr/bin:/bin"
EnvironmentFile=/home/botuser/.env
ExecStart=${BOT_DIR}/scripts/audit_runner.sh --module all --vps --json-dir /tmp/audit_artifacts --alert --quiet
TimeoutStartSec=300

[Install]
WantedBy=multi-user.target
EOF

# Timer unit — 4x/day at :15 past the hour
sudo tee "$TIMER_FILE" > /dev/null <<EOF
[Unit]
Description=Run Kalshi full audit 4x/day (00:15, 06:15, 12:15, 18:15 UTC)

[Timer]
OnCalendar=*-*-* 00,06,12,18:15:00 UTC
AccuracySec=1min
Persistent=true

[Install]
WantedBy=timers.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable kalshi-full-audit.timer
sudo systemctl start kalshi-full-audit.timer

echo ""
echo "=== Timer installed and started ==="
echo ""
systemctl status kalshi-full-audit.timer --no-pager
echo ""
echo "View logs:  journalctl -u kalshi-full-audit.service --no-pager -n 40"
echo "Next run:   systemctl list-timers kalshi-full-audit.timer"
echo "Run now:    sudo systemctl start kalshi-full-audit.service"
