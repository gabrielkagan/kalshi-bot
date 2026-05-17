#!/bin/bash
# One-time installer for the systemd units that live in this directory:
#   - /etc/systemd/system/kalshi-bot.service        (Bit 2.0.5.1)
#   - /etc/systemd/system/kalshi-collector.service  (D1.5, 86b9ypna4)
#
# Source of truth = ops/*.service in this directory. Re-run this
# script after any edit to either unit file. The script is idempotent
# — re-running just re-installs the same content for both.
#
# Bit 2.0.5.1 of repo modularization plan
# (kb/decisions/repo-modularization-plan-may05.md). Created in
# response to the Bit 2.1a systemd-mismatch incident
# (kb/failures/bit-2.1a-systemd-mismatch-may06.md): the on-VPS unit
# previously invoked `python3 bot.py` directly, bypassing start.sh.
# After this script runs, the on-VPS unit invokes start.sh, which
# matches CLAUDE.md's documented sacred-rule chain.
#
# D1.5 (2026-05-16, ticket 86b9ypna4) extends the installer to also
# install kalshi-collector.service — the Data Corpus collector unit
# (separate process, vCPU-1 pinned, 512M cap, dedicated env file).
#
# This script does NOT restart the bot or collector — operator decides
# when. It WILL prompt for sudo password on the cp / daemon-reload /
# enable steps; only `sudo -n /bin/systemctl restart kalshi-bot` is
# granted NOPASSWD on the VPS, so install.sh is interactive-only by
# design. A `tty -s` guard fails loudly if invoked via non-interactive
# ssh instead of hanging on the password prompt forever.
set -euo pipefail

if ! tty -s; then
    echo "FAIL: install.sh needs an interactive shell — sudo will prompt."
    echo "      Re-run via 'ssh -t botuser@\$VPS_HOST bash ops/install.sh'"
    echo "      or directly on the VPS."
    exit 1
fi

SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
REPO_ROOT="$( cd "$SCRIPT_DIR/.." && pwd )"

# Per-unit metadata, parallel-array form (bash 3.2 has no associative
# arrays). For each unit the installer validates: source-file exists,
# the wrapper script is executable, the wrapper's env-source target is
# present, the unit's Exec/WorkingDir/EnvironmentFile match the
# expected paths, and `systemd-analyze verify` accepts the unit.
UNIT_NAMES=(
    "kalshi-bot"
    "kalshi-collector"
)
UNIT_WRAPPERS=(
    "$REPO_ROOT/start.sh"
    "$REPO_ROOT/collector-start.sh"
)
# Env-file paths that the unit's EnvironmentFile= directive references.
# Bot: repo-rooted .env (existing). Collector: home-rooted
# .env.collector (D1.5 — credential isolation, see ops/CLAUDE.md).
UNIT_ENV_FILES=(
    "$REPO_ROOT/.env"
    "/home/botuser/.env.collector"
)
# Expected directive lines per unit. Each entry is the literal
# `Key=Value` line that must appear at column 0 in the source unit.
# The ExecStart / WorkingDirectory / EnvironmentFile triple is what
# install.sh validates pre-cp; loosening this would re-open the Bit
# 2.1a incident class.
UNIT_EXPECTED_EXECSTART=(
    "ExecStart=$REPO_ROOT/start.sh"
    "ExecStart=$REPO_ROOT/collector-start.sh"
)
UNIT_EXPECTED_WORKINGDIR=(
    "WorkingDirectory=$REPO_ROOT"
    "WorkingDirectory=$REPO_ROOT"
)
UNIT_EXPECTED_ENVFILE=(
    "EnvironmentFile=$REPO_ROOT/.env"
    "EnvironmentFile=/home/botuser/.env.collector"
)

# Length-mismatch guard — protects against future edits adding to one
# parallel array but not the others (silent pass on empty index lookup).
N=${#UNIT_NAMES[@]}
for arr_name in UNIT_WRAPPERS UNIT_ENV_FILES UNIT_EXPECTED_EXECSTART \
                UNIT_EXPECTED_WORKINGDIR UNIT_EXPECTED_ENVFILE; do
    eval "len=\${#${arr_name}[@]}"
    if [ "$len" -ne "$N" ]; then
        echo "FAIL: install.sh internal error — ${arr_name} length $len, expected $N."
        echo "      Fix the parallel-array definitions at the top of install.sh."
        exit 1
    fi
done

for i in "${!UNIT_NAMES[@]}"; do
    name="${UNIT_NAMES[$i]}"
    unit_src="$SCRIPT_DIR/${name}.service"
    unit_dst="/etc/systemd/system/${name}.service"
    wrapper="${UNIT_WRAPPERS[$i]}"
    env_file="${UNIT_ENV_FILES[$i]}"
    expected_execstart="${UNIT_EXPECTED_EXECSTART[$i]}"
    expected_workingdir="${UNIT_EXPECTED_WORKINGDIR[$i]}"
    expected_envfile="${UNIT_EXPECTED_ENVFILE[$i]}"

    echo "==> Validating ${name}.service"

    if [ ! -f "$unit_src" ]; then
        echo "FAIL: $unit_src not found. Run from a checkout where ops/${name}.service exists."
        exit 1
    fi

    # Wrapper executable bit — if missing, the next systemd restart
    # fails with the same incident class Bit 2.1a hit. Catch here.
    if [ ! -x "$wrapper" ]; then
        echo "FAIL: $wrapper is not executable (or missing)."
        echo "      Fix with: chmod +x $wrapper"
        echo "      Then re-run install.sh."
        exit 1
    fi

    # EnvironmentFile target must exist — systemd's EnvironmentFile=
    # (without the `-` prefix variant) fail-starts the unit if missing.
    # For the bot, that's $REPO_ROOT/.env; for the collector, that's
    # /home/botuser/.env.collector (operator-provisioned; see
    # ops/CLAUDE.md "D1.5 collector .env.collector provisioning").
    if [ ! -f "$env_file" ]; then
        echo "FAIL: $env_file missing."
        if [ "$name" = "kalshi-collector" ]; then
            echo "      D1.5 operator runbook: provision /home/botuser/.env.collector"
            echo "      with KALSHI_COLLECTOR_KEY_ID / KALSHI_COLLECTOR_KEY_PATH /"
            echo "      COLLECTOR_BRONZE_ROOT / COLLECTOR_CONN_COUNT / RCLONE_REMOTE /"
            echo "      S3_BUCKET. See ops/CLAUDE.md + kb/decisions/d1-5-pickup-prompt-may16.md."
        else
            echo "      Copy from .env.example and populate credentials, then re-run install.sh."
        fi
        exit 1
    fi

    # Validate the unit's ExecStart / WorkingDirectory / EnvironmentFile
    # match expected values. Use `grep -qxF` (full-line literal match
    # at column 0) so directive lines inside a multi-line comment or a
    # different section header can't accidentally satisfy the check.
    for line in "$expected_execstart" "$expected_workingdir" "$expected_envfile"; do
        if ! grep -qxF "$line" "$unit_src"; then
            echo "FAIL: $unit_src missing expected directive line."
            echo "      Expected: $line"
            echo "      Either the repo is at a non-standard path or the unit was edited."
            echo "      (Note: install.sh is VPS-only by design. On a Mac dry-run this"
            echo "       failure is expected — units hardcode /home/botuser/kalshi-bot-repo"
            echo "       and the collector hardcodes /home/botuser/.env.collector.)"
            key="${line%%=*}"
            echo "      Found ${key}= line(s) in $unit_src:"
            grep "^${key}=" "$unit_src" | sed 's/^/        /'
            exit 1
        fi
    done

    # systemd-analyze verify — read-only syntactic + path-resolution
    # check. Part of systemd on Ubuntu/Debian (VPS); not present on
    # macOS, which is fine since install.sh is VPS-only by design.
    if ! systemd-analyze verify "$unit_src" 2>&1; then
        echo "FAIL: systemd-analyze verify rejected $unit_src."
        echo "      Fix the unit file before re-running install.sh."
        exit 1
    fi
done

# All validation passed for every unit. Install in a separate pass so
# a partial run can't half-install one unit and leave the other stale.
for i in "${!UNIT_NAMES[@]}"; do
    name="${UNIT_NAMES[$i]}"
    unit_src="$SCRIPT_DIR/${name}.service"
    unit_dst="/etc/systemd/system/${name}.service"
    echo "Installing ${name}.service from $unit_src -> $unit_dst"
    sudo cp "$unit_src" "$unit_dst"
done

sudo systemctl daemon-reload

for name in "${UNIT_NAMES[@]}"; do
    sudo systemctl enable "${name}"
done

echo "Done."
for name in "${UNIT_NAMES[@]}"; do
    echo "  Verify ${name}: systemctl cat ${name} | head -20"
done
echo "Restart hints (post-D1.5.2: deploy.yml auto-restarts kalshi-collector on collector-affecting deploys; manual restart only needed for first-install / post-stop resume / out-of-band hotfix):"
echo "  Bot       : sudo -n /bin/systemctl restart kalshi-bot"
echo "  Collector : sudo -n /bin/systemctl restart kalshi-collector  (NOPASSWD assumes operator has extended /etc/sudoers.d/botuser-systemctl-restart to include kalshi-collector; see feedback_vps_sudoers_collector_gap_may17)"
