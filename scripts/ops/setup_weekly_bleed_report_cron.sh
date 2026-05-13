#!/usr/bin/env bash
# Setup systemd timer for weekly_bleed_report.py on VPS.
# Run once: bash scripts/ops/setup_weekly_bleed_report_cron.sh
#
# Creates:
#   /etc/systemd/system/kalshi-weekly-bleed-report.service
#   /etc/systemd/system/kalshi-weekly-bleed-report.timer
#
# Fires Mondays at 13:13 UTC. Intentionally 6 minutes AFTER the P1.1
# nightly cohort_attribution cron at 13:07 UTC so the weekly report
# reads a post-nightly-commit cohort_attribution_daily row.
#
# Ticket: 86b9x3kn2 (P1.4 — Money Printer Roadmap Phase 1, last Bit).
# Design: kb/decisions/cohort-measurement-design-may12.md.

set -euo pipefail

SERVICE_FILE="/etc/systemd/system/kalshi-weekly-bleed-report.service"
TIMER_FILE="/etc/systemd/system/kalshi-weekly-bleed-report.timer"
BOT_DIR="/home/botuser/kalshi-bot-repo"
VENV_PYTHON="${BOT_DIR}/venv/bin/python3"
SCRIPT="${BOT_DIR}/scripts/audit/weekly_bleed_report.py"
DB="${BOT_DIR}/state.db"
OUTPUT_DIR="${BOT_DIR}/kb/findings"

echo "=== Installing kalshi-weekly-bleed-report systemd timer ==="

# Service unit
sudo tee "$SERVICE_FILE" > /dev/null <<EOF
[Unit]
Description=Kalshi Weekly Bleed Report Generator (P1.4)
After=network.target

[Service]
Type=oneshot
User=botuser
WorkingDirectory=${BOT_DIR}
ExecStart=${VENV_PYTHON} ${SCRIPT} --db ${DB} --output-dir ${OUTPUT_DIR}
TimeoutStartSec=300
EOF

# Timer unit — Mondays 13:13 UTC (6min after P1.1's 13:07 UTC nightly)
sudo tee "$TIMER_FILE" > /dev/null <<EOF
[Unit]
Description=Run Kalshi weekly bleed report Mondays at 13:13 UTC

[Timer]
OnCalendar=Mon *-*-* 13:13:00 UTC
AccuracySec=1min
Persistent=true

[Install]
WantedBy=timers.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable kalshi-weekly-bleed-report.timer
sudo systemctl start kalshi-weekly-bleed-report.timer

echo ""
echo "=== Timer installed and started ==="
echo ""
systemctl status kalshi-weekly-bleed-report.timer --no-pager
echo ""
echo "View logs:    journalctl -u kalshi-weekly-bleed-report.service --no-pager -n 50"
echo "Next run:     systemctl list-timers kalshi-weekly-bleed-report.timer"
echo "Run now:      sudo systemctl start kalshi-weekly-bleed-report.service"
echo ""
echo "Manual one-shot for a specific date:"
echo "  sudo -u botuser ${VENV_PYTHON} ${SCRIPT} --db ${DB} --report-date 2026-05-18 --output-dir ${OUTPUT_DIR}"
