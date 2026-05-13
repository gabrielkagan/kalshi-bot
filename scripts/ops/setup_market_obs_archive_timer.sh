#!/usr/bin/env bash
# Setup systemd timer for the nightly market_observations_continuous
# S3 archive on the VPS. Run once: bash scripts/ops/setup_market_obs_archive_timer.sh
#
# Ticket: 86b9xcdwg. Companion to setup_state_db_backup_timer.sh —
# expects that one to have already run (rclone, s3prod remote,
# kalshi-bot-archive bucket + IAM creds in .env).
#
# Creates 1 service+timer pair:
#   /etc/systemd/system/kalshi-market-obs-archive.{service,timer}
#       Daily at 05:30 UTC (between rotate_journals @04:00 and
#       state.db backup @06:00). Wrapped via h4_run_with_alert.py
#       for Telegram failure alerts.
#
# Re-runnable: tee overwrites unit files, daemon-reload picks up changes.

set -euo pipefail

BOT_DIR="/home/botuser/kalshi-bot-repo"
VENV_PYTHON="${BOT_DIR}/venv/bin/python3"
DB="${BOT_DIR}/state.db"
WRAPPER="${BOT_DIR}/scripts/ops/h4_run_with_alert.py"
SCRIPT="${BOT_DIR}/scripts/ops/export_market_obs_to_s3.py"
ENV_FILE="${BOT_DIR}/.env"
RCLONE_REMOTE="s3prod"
ENV_FILE_DIRECTIVE="EnvironmentFile=-${ENV_FILE}"

# ── pre-flight: scripts present ─────────────────────────────────────────
for f in "$SCRIPT" "$WRAPPER"; do
    if [ ! -f "$f" ]; then
        echo "FAIL: required script not found: $f"
        echo "      Make sure the deploy synced the latest commit."
        exit 1
    fi
done

# ── pre-flight: rclone + pyarrow installed ──────────────────────────────
if ! command -v rclone >/dev/null 2>&1; then
    echo "FAIL: rclone not installed. Run scripts/ops/setup_state_db_backup_timer.sh first."
    exit 1
fi
if ! "$VENV_PYTHON" -c "import pyarrow" 2>/dev/null; then
    echo "FAIL: pyarrow not importable in $VENV_PYTHON."
    echo "      Install with: $VENV_PYTHON -m pip install 'pyarrow>=14,<22'"
    exit 1
fi
echo "rclone: $(rclone version | head -1)"
echo "pyarrow: $($VENV_PYTHON -c 'import pyarrow; print(pyarrow.__version__)')"

# ── pre-flight: s3prod rclone remote exists ─────────────────────────────
if ! rclone listremotes 2>/dev/null | grep -q "^${RCLONE_REMOTE}:$"; then
    echo "FAIL: rclone remote '${RCLONE_REMOTE}' not configured."
    echo "      Run scripts/ops/setup_state_db_backup_timer.sh first — it creates the remote."
    exit 1
fi

# ── pre-flight: required .env vars ──────────────────────────────────────
if [ ! -f "$ENV_FILE" ]; then
    echo "FAIL: $ENV_FILE not found."
    exit 1
fi
MISSING=()
for v in S3_BACKUP_BUCKET; do
    if ! grep -qE "^${v}=.+" "$ENV_FILE" 2>/dev/null; then
        MISSING+=("$v")
    fi
done
if [ ${#MISSING[@]} -gt 0 ]; then
    echo "FAIL: $ENV_FILE missing required vars:"
    for v in "${MISSING[@]}"; do echo "  - $v"; done
    exit 1
fi

# Same awk-based KEY=VAL parser as setup_state_db_backup_timer.sh (mirrors
# systemd's EnvironmentFile parser without shell-expansion drift).
read_env_var() {
    local var="$1"
    awk -v K="$var" '
        $0 ~ "^"K"=" {
            v = substr($0, length(K)+2)
            if (v ~ /^".*"$/) v = substr(v, 2, length(v)-2)
            else if (v ~ /^'\''.*'\''$/) v = substr(v, 2, length(v)-2)
            print v
            exit
        }
    ' "$ENV_FILE"
}
S3_BUCKET="$(read_env_var S3_BACKUP_BUCKET)"

# Bucket regex (same as state_db backup setup script).
if ! [[ "$S3_BUCKET" =~ ^[a-z0-9][a-z0-9.\-]{1,61}[a-z0-9]$ ]]; then
    echo "FAIL: S3_BACKUP_BUCKET='${S3_BUCKET}' fails AWS bucket-name regex."
    exit 1
fi

# ── pre-flight: TELEGRAM creds (warn only) ──────────────────────────────
if ! grep -qE '^TELEGRAM_BOT_TOKEN=.+' "$ENV_FILE" 2>/dev/null \
   || ! grep -qE '^TELEGRAM_CHAT_ID=.+' "$ENV_FILE" 2>/dev/null; then
    echo "WARNING: TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set in $ENV_FILE."
    echo "         Archive failures will only appear in journalctl, not Telegram."
    echo "         Continuing in 5s..."
    sleep 5
fi

# ── install service+timer ───────────────────────────────────────────────
echo ""
echo "=== Installing kalshi-market-obs-archive systemd timer ==="

sudo tee /etc/systemd/system/kalshi-market-obs-archive.service > /dev/null <<EOF
[Unit]
Description=Kalshi market_observations_continuous nightly S3 archive (86b9xcdwg)
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
User=botuser
WorkingDirectory=${BOT_DIR}
${ENV_FILE_DIRECTIVE}
Environment="PATH=/usr/local/bin:/usr/bin:/bin"
ExecStart=${VENV_PYTHON} ${WRAPPER} --label market-obs-archive -- ${VENV_PYTHON} ${SCRIPT} --db ${DB} --rclone-remote ${RCLONE_REMOTE}
# Reading 14d × 35K rows + Parquet write + S3 upload — typically <60s on
# the 1-vCPU VPS. 600s (10min) gives generous headroom for an off-hours
# bucket reachability blip without running into the 06:00 state.db backup.
TimeoutStartSec=600
RuntimeMaxSec=600
PrivateTmp=true
EOF

sudo tee /etc/systemd/system/kalshi-market-obs-archive.timer > /dev/null <<EOF
[Unit]
Description=Daily timer for Kalshi market_observations_continuous S3 archive (05:30 UTC)

[Timer]
OnCalendar=*-*-* 05:30:00
AccuracySec=1min
# Persistent=false — losing one daily archive is acceptable; tomorrow's
# run picks up the next day. Same posture as state.db backup timer.
Persistent=false

[Install]
WantedBy=timers.target
EOF

# ── enable + start ──────────────────────────────────────────────────────
sudo systemctl daemon-reload
sudo systemctl enable kalshi-market-obs-archive.timer
sudo systemctl start  kalshi-market-obs-archive.timer

echo ""
echo "=== Timer installed and started ==="
systemctl list-timers 'kalshi-market-obs-archive.*' --no-pager
echo ""
echo "First archive will fire at next 05:30 UTC."
echo ""
echo "Run on demand:"
echo "  sudo systemctl start kalshi-market-obs-archive.service"
echo ""
echo "View logs:"
echo "  journalctl -u kalshi-market-obs-archive.service --no-pager -n 50"
echo ""
echo "Verify S3 object after first run:"
echo "  rclone ls ${RCLONE_REMOTE}:${S3_BUCKET}/market_obs/"
