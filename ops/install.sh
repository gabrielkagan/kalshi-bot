#!/bin/bash
# One-time installer for /etc/systemd/system/kalshi-bot.service.
#
# Source of truth = ops/kalshi-bot.service (this directory). Re-run
# this script after any edit to that file. The script is idempotent —
# re-running just re-installs the same content.
#
# Bit 2.0.5.1 of repo modularization plan
# (kb/decisions/repo-modularization-plan-may05.md). Created in
# response to the Bit 2.1a systemd-mismatch incident
# (kb/failures/bit-2.1a-systemd-mismatch-may06.md): the on-VPS unit
# previously invoked `python3 bot.py` directly, bypassing start.sh.
# After this script runs, the on-VPS unit invokes start.sh, which
# matches CLAUDE.md's documented sacred-rule chain.
#
# This script does NOT restart the bot — operator decides when. It
# WILL prompt for sudo password on the cp / daemon-reload / enable
# steps; only `sudo -n /bin/systemctl restart kalshi-bot` is granted
# NOPASSWD on the VPS, so install.sh is interactive-only by design.
# A `tty -s` guard fails loudly if invoked via non-interactive ssh
# instead of hanging on the password prompt forever.
set -euo pipefail

if ! tty -s; then
    echo "FAIL: install.sh needs an interactive shell — sudo will prompt."
    echo "      Re-run via 'ssh -t botuser@\$VPS_HOST bash ops/install.sh'"
    echo "      or directly on the VPS."
    exit 1
fi

SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
REPO_ROOT="$( cd "$SCRIPT_DIR/.." && pwd )"
UNIT_SRC="$SCRIPT_DIR/kalshi-bot.service"
UNIT_DST="/etc/systemd/system/kalshi-bot.service"

if [ ! -f "$UNIT_SRC" ]; then
    echo "FAIL: $UNIT_SRC not found. Run from a checkout where ops/kalshi-bot.service exists."
    exit 1
fi

# Verify start.sh is executable. The unit's ExecStart points at
# /home/botuser/kalshi-bot-repo/start.sh; if it lacks the +x bit,
# the next systemd restart fails with the same incident class
# Bit 2.1a hit. Catch the regression here, before sudo cp activates
# the new ExecStart.
START_SH="$REPO_ROOT/start.sh"
if [ ! -x "$START_SH" ]; then
    echo "FAIL: $START_SH is not executable (or missing)."
    echo "      Fix with: chmod +x $START_SH"
    echo "      Then re-run install.sh."
    exit 1
fi

# Verify .env exists at the path the unit's EnvironmentFile= line
# references. systemd's EnvironmentFile (without the `-` prefix
# variant) fail-starts the unit if the file is missing — on a fresh
# VPS clone, install.sh would succeed but the next restart crash-
# loops (R3 review #3). Catch the regression here.
ENV_FILE="$REPO_ROOT/.env"
if [ ! -f "$ENV_FILE" ]; then
    echo "FAIL: $ENV_FILE missing."
    echo "      Copy from .env.example and populate credentials, then re-run install.sh."
    exit 1
fi

# Sanity-check that the unit's ExecStart, WorkingDirectory, and
# EnvironmentFile paths all match where the repo actually lives. If
# the operator cloned to a non-standard path, install.sh + a future
# restart would brick the bot identically to the Bit 2.1a incident.
# (R2 review #6: extending the check beyond just ExecStart so a
# future unit edit that introduces $REPO_ROOT-style ExecStart while
# leaving WorkingDirectory hardcoded gets caught here.)
#
# Implementation note: parallel arrays + index loop instead of
# `declare -A` — bash 3.2 (macOS default) lacks associative arrays,
# and while install.sh is documented as VPS-only, an operator who
# accidentally runs it on a Mac should get the path validation
# error, not a confusing `declare: -A: invalid option`.
EXPECTED_KEYS=("ExecStart" "WorkingDirectory" "EnvironmentFile")
EXPECTED_VALS=(
    "ExecStart=$REPO_ROOT/start.sh"
    "WorkingDirectory=$REPO_ROOT"
    "EnvironmentFile=$REPO_ROOT/.env"
)
# R3 review #1: guard against future edits adding to one array but
# not the other (length mismatch → silent pass on empty grep -qxF "").
if [ "${#EXPECTED_KEYS[@]}" -ne "${#EXPECTED_VALS[@]}" ]; then
    echo "FAIL: install.sh internal error — EXPECTED_KEYS / EXPECTED_VALS length mismatch."
    echo "      ${#EXPECTED_KEYS[@]} keys, ${#EXPECTED_VALS[@]} vals. Fix the script."
    exit 1
fi
for i in "${!EXPECTED_KEYS[@]}"; do
    key="${EXPECTED_KEYS[$i]}"
    expected="${EXPECTED_VALS[$i]}"
    if ! grep -qxF "$expected" "$UNIT_SRC"; then
        echo "FAIL: $UNIT_SRC $key does not match repo path."
        echo "      Expected line: $expected"
        echo "      Either the repo is at a non-standard path or the unit was edited."
        echo "      (Note: install.sh is VPS-only by design. On a Mac dry-run this"
        echo "       failure is expected — the unit hardcodes /home/botuser/kalshi-bot-repo.)"
        echo "      Found $key line(s):"
        grep "^$key=" "$UNIT_SRC" | sed 's/^/        /'
        exit 1
    fi
done

# R4 review #2: validate the unit file syntactically before installing.
# `systemd-analyze verify` instantiates the unit (resolves WorkingDir,
# ExecStart path, etc.) and reports any error before sudo cp commits
# the bad unit to /etc/. Read-only check; no sudo needed. Note: this
# is part of the systemd package on Ubuntu/Debian — not present on
# macOS, so it implicitly enforces VPS-only execution alongside the
# path-validation check above.
if ! systemd-analyze verify "$UNIT_SRC" 2>&1; then
    echo "FAIL: systemd-analyze verify rejected $UNIT_SRC."
    echo "      Fix the unit file before re-running install.sh."
    exit 1
fi

echo "Installing kalshi-bot.service from $UNIT_SRC -> $UNIT_DST"
sudo cp "$UNIT_SRC" "$UNIT_DST"
sudo systemctl daemon-reload
sudo systemctl enable kalshi-bot
echo "Done. Verify: systemctl cat kalshi-bot | head -20"
echo "If bot is currently running, restart with: sudo -n /bin/systemctl restart kalshi-bot"
