#!/usr/bin/env bash
# Setup systemd timer for cohort_attribution_nightly.py on VPS.
# Run once: bash scripts/ops/setup_cohort_attribution_cron.sh
#
# Creates:
#   /etc/systemd/system/kalshi-cohort-attribution.service
#   /etc/systemd/system/kalshi-cohort-attribution.timer
#
# Fires daily at 13:07 UTC. Intentionally 6 minutes before the P1.4
# Monday 13:13 weekly cron so the weekly report reads a post-nightly-
# commit cohort_attribution_daily row.
#
# Ticket: 86b9x3kgd (P1.1 — Money Printer Roadmap Phase 1).
# Design: kb/decisions/cohort-measurement-design-may12.md.

set -euo pipefail

SERVICE_FILE="/etc/systemd/system/kalshi-cohort-attribution.service"
TIMER_FILE="/etc/systemd/system/kalshi-cohort-attribution.timer"
BOT_DIR="/home/botuser/kalshi-bot-repo"
VENV_PYTHON="${BOT_DIR}/venv/bin/python3"
SCRIPT="${BOT_DIR}/scripts/audit/cohort_attribution_nightly.py"
DB="${BOT_DIR}/state.db"

echo "=== Installing kalshi-cohort-attribution systemd timer ==="

# Service unit
sudo tee "$SERVICE_FILE" > /dev/null <<EOF
[Unit]
Description=Kalshi Cohort Attribution Nightly Aggregation (P1.1)
After=network.target

[Service]
Type=oneshot
User=botuser
WorkingDirectory=${BOT_DIR}
ExecStart=${VENV_PYTHON} ${SCRIPT} --db ${DB}
TimeoutStartSec=600
EOF

# Timer unit — 13:07 UTC daily (off-:00 per scripts/CLAUDE.md cron convention)
sudo tee "$TIMER_FILE" > /dev/null <<EOF
[Unit]
Description=Run Kalshi cohort attribution aggregation nightly at 13:07 UTC

[Timer]
OnCalendar=*-*-* 13:07:00 UTC
AccuracySec=1min
Persistent=true

[Install]
WantedBy=timers.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable kalshi-cohort-attribution.timer
sudo systemctl start kalshi-cohort-attribution.timer

echo ""
echo "=== Timer installed and started ==="
echo ""
systemctl status kalshi-cohort-attribution.timer --no-pager
echo ""
echo "View logs:  journalctl -u kalshi-cohort-attribution.service --no-pager -n 50"
echo "Next run:   systemctl list-timers kalshi-cohort-attribution.timer"
echo "Run now:    sudo systemctl start kalshi-cohort-attribution.service"
echo ""
echo "One-shot 73d backfill (do once after first deploy):"
echo "  sudo -u botuser ${VENV_PYTHON} ${SCRIPT} --db ${DB} --backfill-days 73"
