#!/usr/bin/env bash
# Setup systemd timers for state.db S3 backup + weekly restore-verify
# on the VPS. Run once: bash scripts/setup_state_db_backup_timer.sh
#
# Phase 0a per kb/decisions/autoresearch-design-may05.md hazards table.
# Ticket: 86b9vd9e3.
#
# Creates 2 service+timer pairs:
#   /etc/systemd/system/kalshi-state-db-backup.{service,timer}
#       Daily at 06:00 UTC. Wrapped via h4_run_with_alert.py for
#       Telegram failure alerts. Schedule slot is post H-4 cron chain
#       (04:00/04:30/05:00 UTC) to avoid contention.
#   /etc/systemd/system/kalshi-state-db-restore-verify.{service,timer}
#       Weekly Sunday 07:00 UTC (1h after Sunday backup). Pulls the
#       latest snapshot, runs PRAGMA integrity_check + row-count
#       parity (±5%) vs live state.db. Telegram alert on divergence.
#
# Re-runnable: tee overwrites unit files, daemon-reload picks up changes.
#
# Pre-flight checks: rclone installed, S3_BACKUP_* env vars present,
# rclone remote configured, .env present, scripts executable.
#
# What this script does NOT do (operator owns):
#   - Create the S3 bucket.
#   - Create the IAM user + access keys (writer + reader, see plan doc D3).
#   - Configure the S3 lifecycle rule (Standard -> Glacier IR @30d
#     -> Deep Archive @90d; never expire).
#   - Add S3_BACKUP_* keys to /home/botuser/kalshi-bot-repo/.env.
#   - Run `rclone config` to create the s3prod remote.
# All of these are documented in scripts/STATE_DB_BACKUP_SETUP.md
# (created at ship time).

set -euo pipefail

BOT_DIR="/home/botuser/kalshi-bot-repo"
VENV_PYTHON="${BOT_DIR}/venv/bin/python3"
DB="${BOT_DIR}/state.db"
WRAPPER="${BOT_DIR}/scripts/h4_run_with_alert.py"
BACKUP_SCRIPT="${BOT_DIR}/scripts/state_db_s3_backup.py"
RESTORE_SCRIPT="${BOT_DIR}/scripts/state_db_restore.py"
ENV_FILE="${BOT_DIR}/.env"
RCLONE_REMOTE="s3prod"

# `EnvironmentFile=-` makes systemd tolerate a missing .env at install
# time (matches setup_h4_cron.sh pattern). The pre-flight below catches
# the "missing required vars in live .env" case loudly.
ENV_FILE_DIRECTIVE="EnvironmentFile=-${ENV_FILE}"

# ── pre-flight: scripts present + executable ─────────────────────────────
for f in "$BACKUP_SCRIPT" "$RESTORE_SCRIPT" "$WRAPPER"; do
    if [ ! -f "$f" ]; then
        echo "FAIL: required script not found: $f"
        echo "      Make sure the deploy synced the latest commit."
        exit 1
    fi
done

# ── pre-flight: rclone installed ─────────────────────────────────────────
if ! command -v rclone >/dev/null 2>&1; then
    echo "FAIL: rclone not installed."
    echo "      Install with:  sudo apt-get update && sudo apt-get install -y rclone"
    echo "      Or single-binary: curl https://rclone.org/install.sh | sudo bash"
    exit 1
fi
echo "rclone: $(rclone version | head -1)"

# ── pre-flight: zstd installed (compression default) ─────────────────────
if ! command -v zstd >/dev/null 2>&1; then
    echo "WARNING: zstd not installed."
    echo "         Install with:  sudo apt-get install -y zstd"
    echo "         (script will fall back to gzip via --algorithm=gzip if zstd missing)"
    echo ""
fi

# ── pre-flight: /var/lock writable by botuser ───────────────────────────
# Round-3 finding B3-M2: state_db_s3_backup.py picks _LOCK_PATH at
# import time. If /var/lock isn't writable by botuser at that moment,
# it silently falls back to /tmp — which is namespaced under
# PrivateTmp=true on the systemd unit, defeating the entire single-
# runner protection. Catch this loudly at install time.
if ! sudo -u botuser test -w /var/lock 2>/dev/null; then
    # If we're not running as root (and so can't sudo -u botuser), at
    # least check whether the current user can write — better than nothing.
    if ! [ -w /var/lock ]; then
        echo "FAIL: /var/lock is not writable. The single-runner flock"
        echo "      mechanism would silently fall back to /tmp, which is"
        echo "      namespaced under PrivateTmp=true and breaks coordination"
        echo "      between manual + scheduled backup runs."
        echo "      Fix:  sudo chmod g+w /var/lock  (assuming botuser in lock group)"
        echo "      OR:   sudo install -d -m 1777 /var/lock"
        exit 1
    fi
fi

# ── pre-flight: required .env vars ──────────────────────────────────────
if [ ! -f "$ENV_FILE" ]; then
    echo "FAIL: $ENV_FILE not found. The bot's start.sh requires it; this"
    echo "      script does too. Populate from .env.example, then re-run."
    exit 1
fi
MISSING=()
for v in S3_BACKUP_BUCKET S3_BACKUP_REGION S3_BACKUP_AWS_ACCESS_KEY_ID S3_BACKUP_AWS_SECRET_ACCESS_KEY; do
    if ! grep -qE "^${v}=.+" "$ENV_FILE" 2>/dev/null; then
        MISSING+=("$v")
    fi
done
if [ ${#MISSING[@]} -gt 0 ]; then
    echo "FAIL: $ENV_FILE missing required vars (or set to empty value):"
    for v in "${MISSING[@]}"; do echo "  - $v"; done
    echo ""
    echo "Required env keys (drop into $ENV_FILE):"
    cat <<EOT
S3_BACKUP_BUCKET=<bucket-name>
S3_BACKUP_REGION=us-east-1
S3_BACKUP_AWS_ACCESS_KEY_ID=<writer-key-id>
S3_BACKUP_AWS_SECRET_ACCESS_KEY=<writer-secret>
EOT
    exit 1
fi

# Round-1 B-C3 + Round-2 R2-M1: the previous `grep | cut | tr` pipeline
# silently corrupted secrets with quotes; the next attempt (`set -a; . .env`)
# diverged from systemd's EnvironmentFile parser by performing shell
# expansion ($VAR, $(cmd), backslash escapes) that systemd does not.
# We need a parser that matches systemd's behavior: literal KEY=VALUE
# extraction with optional surrounding double/single quotes stripped,
# and NO interpolation. awk fits — the subshell here just runs awk.
read_env_var() {
    local var="$1"
    awk -v K="$var" '
        # Match lines starting with VAR=
        $0 ~ "^"K"=" {
            v = substr($0, length(K)+2)
            # Strip one layer of surrounding double or single quotes
            # (matches systemd EnvironmentFile parsing)
            if (v ~ /^".*"$/) v = substr(v, 2, length(v)-2)
            else if (v ~ /^'\''.*'\''$/) v = substr(v, 2, length(v)-2)
            print v
            exit
        }
    ' "$ENV_FILE"
}
S3_BUCKET="$(read_env_var S3_BACKUP_BUCKET)"
S3_REGION="$(read_env_var S3_BACKUP_REGION)"
S3_AKID="$(read_env_var S3_BACKUP_AWS_ACCESS_KEY_ID)"
S3_SECRET="$(read_env_var S3_BACKUP_AWS_SECRET_ACCESS_KEY)"

# Round-2 R2-M1: reject values containing chars where bash and systemd
# parsers diverge (`$`, backtick, backslash). systemd treats them
# literally; any future caller might shell-expand them. Fail loud.
for pair in "S3_BUCKET=$S3_BUCKET" "S3_REGION=$S3_REGION" \
            "S3_AKID=$S3_AKID" "S3_SECRET=$S3_SECRET"; do
    name="${pair%%=*}"
    val="${pair#*=}"
    if [[ "$val" =~ [[:space:]] ]]; then
        echo "FAIL: $name contains whitespace; reject (systemd parser drift risk)."
        exit 1
    fi
    if [[ "$val" =~ [\$\`\\] ]]; then
        echo "FAIL: $name contains shell-meta char (\$/\`/\\); reject."
        echo "      systemd EnvironmentFile parses these literally; bash would expand."
        exit 1
    fi
done

# Round-1 A-M4 / B-M6 + Round-2 R2-M2: validate bucket name BEFORE any
# rclone operation. Three layers: (1) AWS character-set regex, (2) ban
# AWS-illegal patterns the regex doesn't catch (consecutive dots,
# IP-like, reserved prefix/suffix), (3) length already in regex.
if ! [[ "$S3_BUCKET" =~ ^[a-z0-9][a-z0-9.\-]{1,61}[a-z0-9]$ ]]; then
    echo "FAIL: S3_BACKUP_BUCKET='${S3_BUCKET}' fails AWS bucket-name regex."
    echo "      Must be lowercase, 3-63 chars, [a-z0-9.-], alphanumeric start/end."
    exit 1
fi
if [[ "$S3_BUCKET" == *..* ]]; then
    echo "FAIL: S3_BACKUP_BUCKET='${S3_BUCKET}' contains consecutive dots (forbidden by AWS)."
    exit 1
fi
if [[ "$S3_BUCKET" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
    echo "FAIL: S3_BACKUP_BUCKET='${S3_BUCKET}' looks like an IP address (forbidden by AWS)."
    exit 1
fi
if [[ "$S3_BUCKET" =~ ^(xn--|sthree-|amzn-s3-demo-) ]] || [[ "$S3_BUCKET" =~ (-s3alias|--ol-s3)$ ]]; then
    echo "FAIL: S3_BACKUP_BUCKET='${S3_BUCKET}' uses an AWS-reserved prefix or suffix."
    exit 1
fi

# ── pre-flight: TELEGRAM creds for failure alerts ────────────────────────
# Not fatal — the wrapper degrades to "log to stderr only" if missing
# (mirrors h4_run_with_alert.py behavior). But warn loudly because
# silent failures are exactly what this whole exercise prevents.
if ! grep -qE '^TELEGRAM_BOT_TOKEN=.+' "$ENV_FILE" 2>/dev/null \
   || ! grep -qE '^TELEGRAM_CHAT_ID=.+' "$ENV_FILE" 2>/dev/null; then
    echo "WARNING: TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set in $ENV_FILE."
    echo "         Backup failures will only appear in journalctl, not Telegram."
    echo "         Continuing anyway in 5s..."
    sleep 5
fi

# ── rclone remote: create if not present (idempotent) ───────────────────
if ! rclone listremotes 2>/dev/null | grep -q "^${RCLONE_REMOTE}:$"; then
    echo "rclone remote ${RCLONE_REMOTE} not found; creating from env..."
    rclone config create "${RCLONE_REMOTE}" s3 \
        provider AWS \
        env_auth false \
        access_key_id "${S3_AKID}" \
        secret_access_key "${S3_SECRET}" \
        region "${S3_REGION}" \
        location_constraint "${S3_REGION}" \
        acl private \
        storage_class STANDARD \
        >/dev/null
    echo "rclone remote ${RCLONE_REMOTE} created."

    # Round-1 A-M5: rclone config file ships world-readable by default
    # on most distros. Tighten to 0600 so other users on the VPS can't
    # read writer creds.
    RCLONE_CONFIG_FILE="$(rclone config file 2>/dev/null | tail -1)"
    if [ -f "$RCLONE_CONFIG_FILE" ]; then
        chmod 600 "$RCLONE_CONFIG_FILE"
        echo "Tightened ${RCLONE_CONFIG_FILE} to 0600."
    fi
else
    echo "rclone remote ${RCLONE_REMOTE} already configured (skipping create)."
    # Apply chmod 600 even on subsequent re-runs in case the file
    # permission was lost (e.g., manual edit + save).
    RCLONE_CONFIG_FILE="$(rclone config file 2>/dev/null | tail -1)"
    if [ -f "$RCLONE_CONFIG_FILE" ]; then
        chmod 600 "$RCLONE_CONFIG_FILE"
    fi
fi

# ── pre-flight: bucket reachable + writer creds work ─────────────────────
# Touch a sentinel object then immediately delete. Wait — the writer
# IAM only has PutObject (no Delete), so we can't delete. Instead, just
# attempt PutObject of a tiny sentinel; if it succeeds the creds work.
# Use a date-stamped key under a `_install_check/` prefix so it's
# obvious this is a one-off.
SENTINEL_KEY="_install_check/setup-$(date -u +%Y%m%dT%H%M%S).txt"
TMP_SENTINEL="$(mktemp)"
echo "kalshi-bot install probe at $(date -u +%Y-%m-%dT%H:%M:%SZ)" > "$TMP_SENTINEL"
if ! rclone copyto --checksum "$TMP_SENTINEL" "${RCLONE_REMOTE}:${S3_BUCKET}/${SENTINEL_KEY}" 2>&1; then
    rm -f "$TMP_SENTINEL"
    echo "FAIL: rclone PutObject probe failed against ${S3_BUCKET}."
    echo "      Verify: bucket exists in region ${S3_REGION}, IAM creds valid."
    exit 1
fi
rm -f "$TMP_SENTINEL"
echo "S3 bucket ${S3_BUCKET} reachable; writer creds OK."

# ── install backup service+timer ────────────────────────────────────────
echo ""
echo "=== Installing kalshi-state-db-backup systemd timer ==="

sudo tee /etc/systemd/system/kalshi-state-db-backup.service > /dev/null <<EOF
[Unit]
Description=Kalshi state.db nightly backup to S3 (Phase 0a)
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
User=botuser
WorkingDirectory=${BOT_DIR}
${ENV_FILE_DIRECTIVE}
# Round-1 MN8: explicit PATH so rclone (often /usr/local/bin via curl
# install) is found regardless of systemd's default PATH minimization.
Environment="PATH=/usr/local/bin:/usr/bin:/bin"
ExecStart=${VENV_PYTHON} ${WRAPPER} --label state-db-backup -- ${VENV_PYTHON} ${BACKUP_SCRIPT} --db ${DB} --store s3 --rclone-remote ${RCLONE_REMOTE}
# Round-1 B-M2: sqlite3.Connection.backup() is a blocking C call that
# does not respond to SIGTERM until the current page batch finishes.
# 2400s = 40 min gives the snapshot+compress+upload chain plenty of
# headroom on the 1 vCPU VPS (steady-state ~2-3 min) without running
# the daily backup into the next deploy window.
TimeoutStartSec=2400
RuntimeMaxSec=2400
# Round-1 MN6: PrivateTmp gives the unit its own /tmp namespace so the
# backup's snapshot file isn't visible to other users/processes on the
# VPS. Free; matches the "writer creds on disk are sensitive" posture.
PrivateTmp=true
EOF

sudo tee /etc/systemd/system/kalshi-state-db-backup.timer > /dev/null <<EOF
[Unit]
Description=Daily timer for Kalshi state.db backup (06:00 UTC)

[Timer]
OnCalendar=*-*-* 06:00:00
AccuracySec=1min
# Round-1 B-M1: Persistent=false. If the VPS is rebooted at 14:00 UTC
# and the 06:00 backup was missed, we DO NOT want a catch-up backup
# to fire immediately at boot — that competes with bot startup + the
# H-4 cron chain for SQLite + disk. Daily snapshots are losable; the
# next 06:00 will pick it up. Tomorrow's daily is also tomorrow's
# weekly verify baseline; one missed daily is acceptable.
Persistent=false

[Install]
WantedBy=timers.target
EOF

# ── install restore-verify service+timer ────────────────────────────────
echo "=== Installing kalshi-state-db-restore-verify systemd timer ==="

sudo tee /etc/systemd/system/kalshi-state-db-restore-verify.service > /dev/null <<EOF
[Unit]
Description=Kalshi state.db weekly restore-verify (Phase 0a)
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
User=botuser
WorkingDirectory=${BOT_DIR}
${ENV_FILE_DIRECTIVE}
Environment="PATH=/usr/local/bin:/usr/bin:/bin"
ExecStart=${VENV_PYTHON} ${WRAPPER} --label state-db-restore-verify -- ${VENV_PYTHON} ${RESTORE_SCRIPT} --store s3 --rclone-remote ${RCLONE_REMOTE} --baseline-from-live ${DB} --tolerance-pct 0.05 --tolerance-abs 50
TimeoutStartSec=1200
RuntimeMaxSec=1200
PrivateTmp=true
EOF

sudo tee /etc/systemd/system/kalshi-state-db-restore-verify.timer > /dev/null <<EOF
[Unit]
Description=Weekly timer for Kalshi state.db restore-verify (Sun 07:00 UTC)

[Timer]
# Sunday 07:00 UTC — 1h after the daily backup, so the latest snapshot
# is fresh and we exercise the get-path while creds are still valid.
OnCalendar=Sun *-*-* 07:00:00
AccuracySec=1min
# Round-2 R2-M6: weekly verify uses Persistent=true (unlike the daily
# backup). A reboot near Sunday 07:00 with Persistent=false would skip
# the verify entirely — meaning a 7-day blind window for silent
# corruption to go undetected. Persistent=true catches it at next boot.
# (Daily backup keeps Persistent=false because losing one daily
# snapshot is acceptable; losing a week of integrity verification is not.)
Persistent=true

[Install]
WantedBy=timers.target
EOF

# ── enable + start ──────────────────────────────────────────────────────
sudo systemctl daemon-reload

sudo systemctl enable kalshi-state-db-backup.timer
sudo systemctl start  kalshi-state-db-backup.timer

sudo systemctl enable kalshi-state-db-restore-verify.timer
sudo systemctl start  kalshi-state-db-restore-verify.timer

echo ""
echo "=== Timers installed and started ==="
systemctl list-timers 'kalshi-state-db-*' --no-pager
echo ""
echo "First backup will fire at next 06:00 UTC."
echo "First restore-verify will fire at next Sunday 07:00 UTC."
echo ""
echo "Run a backup on demand:"
echo "  sudo systemctl start kalshi-state-db-backup.service"
echo ""
echo "Run a restore-verify on demand:"
echo "  sudo systemctl start kalshi-state-db-restore-verify.service"
echo ""
echo "View logs:"
echo "  journalctl -u kalshi-state-db-backup.service        --no-pager -n 50"
echo "  journalctl -u kalshi-state-db-restore-verify.service --no-pager -n 50"
