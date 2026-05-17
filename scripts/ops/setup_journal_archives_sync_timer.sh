#!/usr/bin/env bash
# Setup systemd timer for sub-daily (every-4h) journal_archives/ → S3
# sync on the VPS. Run once: bash scripts/ops/setup_journal_archives_sync_timer.sh
#
# Ticket: 86b9xgp7k. Cadence-revised by ticket 86b9zkp89 (2026-05-17,
# Bronze durability — sub-daily sync to close the 24h loss window).
# Companion to setup_state_db_backup_timer.sh — expects that one to
# have already run (rclone, s3prod remote, kalshi-bot-archive bucket
# + IAM creds in .env).
#
# Creates 1 service+timer pair:
#   /etc/systemd/system/kalshi-journal-archives-sync.{service,timer}
#       Every 4h, 30 min offset (00:30, 04:30, 08:30, 12:30, 16:30,
#       20:30 UTC) — each tick fires 30 min AFTER its paired
#       rotate_journals.sh tick (rotation cadence was SSH-changed to
#       every-4h on 2026-05-17; the rotate_journals.sh script is
#       local-only on the VPS, not in git). The 30-min offset gives
#       the most-recently-rotated journal time to fully zstd-compress
#       before sync fires. Wrapped via h4_run_with_alert.py for
#       Telegram failure alerts. Pre-86b9zkp89 cadence was daily 04:30 UTC.
#
# Re-runnable: tee overwrites unit files, daemon-reload picks up changes.

set -euo pipefail

BOT_DIR="/home/botuser/kalshi-bot-repo"
VENV_PYTHON="${BOT_DIR}/venv/bin/python3"
ARCHIVES_DIR="${BOT_DIR}/journal_archives"
WRAPPER="${BOT_DIR}/scripts/ops/h4_run_with_alert.py"
SCRIPT="${BOT_DIR}/scripts/ops/journal_archives_s3_sync.py"
ENV_FILE="${BOT_DIR}/.env"
RCLONE_REMOTE="s3prod"
ENV_FILE_DIRECTIVE="EnvironmentFile=-${ENV_FILE}"

# ── pre-flight: scripts present ─────────────────────────────────────────
for f in "$SCRIPT" "$WRAPPER"; do
    if [ ! -f "$f" ]; then
        echo "FAIL: required script not found: $f"
        exit 1
    fi
done
if [ ! -d "$ARCHIVES_DIR" ]; then
    echo "FAIL: journal_archives dir not found: $ARCHIVES_DIR"
    echo "      Is rotate_journals.sh installed? (~botuser/kalshi-bot-repo/journal_archives/)"
    exit 1
fi

# ── pre-flight: rclone installed ────────────────────────────────────────
if ! command -v rclone >/dev/null 2>&1; then
    echo "FAIL: rclone not installed. Run scripts/ops/setup_state_db_backup_timer.sh first."
    exit 1
fi
echo "rclone: $(rclone version | head -1)"

# ── pre-flight: s3prod rclone remote exists ─────────────────────────────
if ! rclone listremotes 2>/dev/null | grep -q "^${RCLONE_REMOTE}:$"; then
    echo "FAIL: rclone remote '${RCLONE_REMOTE}' not configured."
    echo "      Run scripts/ops/setup_state_db_backup_timer.sh first — it creates the remote."
    exit 1
fi

# ── pre-flight: /var/lock writable ──────────────────────────────────────
# Same A-M6 concern as state_db backup — flock fallback to /tmp under
# PrivateTmp=true breaks the single-runner protection.
if ! sudo -u botuser test -w /var/lock 2>/dev/null; then
    if ! [ -w /var/lock ]; then
        echo "FAIL: /var/lock is not writable. Single-runner flock would silently"
        echo "      fall back to /tmp, namespaced under PrivateTmp=true."
        echo "      Fix: sudo install -d -m 1777 /var/lock"
        exit 1
    fi
fi

# ── pre-flight: required .env vars ──────────────────────────────────────
if [ ! -f "$ENV_FILE" ]; then
    echo "FAIL: $ENV_FILE not found."
    exit 1
fi
if ! grep -qE '^S3_BACKUP_BUCKET=.+' "$ENV_FILE" 2>/dev/null; then
    echo "FAIL: $ENV_FILE missing S3_BACKUP_BUCKET."
    exit 1
fi

# awk-based KEY=VAL parser (matches systemd's EnvironmentFile parser).
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
if ! [[ "$S3_BUCKET" =~ ^[a-z0-9][a-z0-9.\-]{1,61}[a-z0-9]$ ]]; then
    echo "FAIL: S3_BACKUP_BUCKET='${S3_BUCKET}' fails AWS bucket-name regex."
    exit 1
fi

# ── pre-flight: TELEGRAM (warn only) ────────────────────────────────────
if ! grep -qE '^TELEGRAM_BOT_TOKEN=.+' "$ENV_FILE" 2>/dev/null \
   || ! grep -qE '^TELEGRAM_CHAT_ID=.+' "$ENV_FILE" 2>/dev/null; then
    echo "WARNING: TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set."
    echo "         Sync failures will only appear in journalctl, not Telegram."
    echo "         Continuing in 5s..."
    sleep 5
fi

# ── install service+timer ───────────────────────────────────────────────
echo ""
echo "=== Installing kalshi-journal-archives-sync systemd timer ==="

sudo tee /etc/systemd/system/kalshi-journal-archives-sync.service > /dev/null <<EOF
[Unit]
Description=Kalshi journal_archives/ sub-daily S3 sync (86b9xgp7k, every 4h post-86b9zkp89)
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
User=botuser
WorkingDirectory=${BOT_DIR}
${ENV_FILE_DIRECTIVE}
Environment="PATH=/usr/local/bin:/usr/bin:/bin"
ExecStart=${VENV_PYTHON} ${WRAPPER} --label journal-archives-sync -- ${VENV_PYTHON} ${SCRIPT} --src ${ARCHIVES_DIR} --rclone-remote ${RCLONE_REMOTE}
# First run uploads ~11 GB (~33 days × ~250-360 MB compressed daily).
# Subsequent runs are no-ops (rclone --checksum short-circuits). 1 hour
# generous for the first-run backfill on 1-vCPU + S3 upload bandwidth.
TimeoutStartSec=3600
# PrivateTmp=true keeps any rclone scratch out of the host /tmp.
PrivateTmp=true
EOF

sudo tee /etc/systemd/system/kalshi-journal-archives-sync.timer > /dev/null <<EOF
[Unit]
Description=Sub-daily timer for Kalshi journal_archives/ S3 sync (every 4h, post-86b9zkp89)

[Timer]
# Fires at 00:30, 04:30, 08:30, 12:30, 16:30, 20:30 UTC. The systemd
# `start/step` shorthand `00/4` on the hour field, with minute=30,
# gives each tick a 30-min gap after the paired rotate_journals.sh
# tick (rotation now runs every 4h on the hour; the script itself is
# local-only on the VPS, not in git). The 30-min gap is load-bearing:
# zstd compression of the most-recently-rotated journal must complete
# before sync, else rclone --immutable would treat the partial file
# as content divergence and surface exit 6. Pre-86b9zkp89 cadence was
# `*-*-* 04:30:00` (daily); every-4h caps loss to ~4h instead of ~24h.
OnCalendar=*-*-* 00/4:30:00
AccuracySec=1min
# Persistent=false — same posture as the state.db backup timer. Sync is
# idempotent; missing a tick is recoverable by the next tick (rclone
# checksum picks up the gap automatically).
Persistent=false

[Install]
WantedBy=timers.target
EOF

# ── enable + start ──────────────────────────────────────────────────────
sudo systemctl daemon-reload
sudo systemctl enable kalshi-journal-archives-sync.timer
sudo systemctl start  kalshi-journal-archives-sync.timer

echo ""
echo "=== Timer installed and started ==="
systemctl list-timers 'kalshi-journal-archives-sync.*' --no-pager
echo ""
echo "First sync will fire at next 4h tick (00/04/08/12/16/20:30 UTC; first run uploads any backlog)."
echo ""
echo "Run on demand:"
echo "  sudo systemctl start kalshi-journal-archives-sync.service"
echo ""
echo "View logs:"
echo "  journalctl -u kalshi-journal-archives-sync.service --no-pager -n 50"
echo ""
echo "Verify S3 objects after first run:"
echo "  rclone ls ${RCLONE_REMOTE}:${S3_BUCKET}/journals/ | head -20"
