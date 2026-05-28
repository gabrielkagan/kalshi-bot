#!/bin/bash
# One-time installer for the systemd units that live in this directory:
#   - /etc/systemd/system/kalshi-bot.service              (Bit 2.0.5.1)
#   - /etc/systemd/system/kalshi-collector.service        (D1.5, 86b9ypna4)
#   - /etc/systemd/system/kalshi-coinbase-collector.service (D2.5, 86b9znq4w)
#   - /etc/systemd/system/kalshi-weather-collector.service (D1.8, 86ba0duck)
#   - /etc/systemd/system/kalshi-espn-collector.service   (D1.11.a, 86ba0ppy0)
#   - /etc/systemd/system/kalshi-venue-l2-collector.service (B2a-1, 86ba1zf5j)
#
# Source of truth = ops/*.service in this directory. Re-run this
# script after any edit to any unit file. The script is idempotent —
# re-running just re-installs the same content for all six.
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
# (separate process, Nice=10 polite-background, 512M cap, dedicated env
# file). CPUAffinity=1 vCPU-1 pin retired 2026-05-19 (ticket 86ba12rv6).
#
# D2.5 (2026-05-18, ticket 86b9znq4w) extends the installer further
# to install kalshi-coinbase-collector.service — the Coinbase-side
# Data Corpus collector unit (single-conn, no CPUAffinity, 256M cap,
# dedicated .env.coinbase-collector). Three-unit parallel-array form;
# the length-mismatch guard now expects N=3.
#
# D1.8 (2026-05-18, ticket 86ba0duck) extends the installer further
# to install kalshi-weather-collector.service — the weather bronze
# collector unit (first non-WS source; HTTP poll at 60-min cadence,
# no CPUAffinity, 128M cap, dedicated .env.weather-collector).
# Four-unit parallel-array form; the length-mismatch guard now expects N=4.
#
# D1.11.a (2026-05-19, ticket 86ba0ppy0) extends the installer further
# to install kalshi-espn-collector.service — the ESPN bronze collector
# unit (second non-WS source; HTTP poll at 60-second cadence across 24
# leagues, no CPUAffinity, 256M cap, dedicated .env.espn-collector).
# Five-unit parallel-array form; the length-mismatch guard now expects N=5.
#
# B2a-1 (2026-05-28, ticket 86ba1zf5j) extends the installer further to
# install kalshi-venue-l2-collector.service — the multi-venue lean L2
# bronze recorder (Kraken + Bitstamp + Gemini public WS; no CPUAffinity,
# 512M cap, dedicated .env.venue-l2-collector). Requires-approval: deploy
# starts the ~14d bronze-accumulation clock for the synthetic-RTI RMSE
# validation gate. Six-unit parallel-array form; the length-mismatch guard
# now expects N=6.
#
# This script does NOT restart the bot or collectors — operator decides
# when. It WILL prompt for sudo password on the cp / daemon-reload /
# enable steps; only `sudo -n /bin/systemctl restart kalshi-bot` is
# granted NOPASSWD on the VPS, so install.sh is interactive-only by
# design. A `tty -s` guard fails loudly if invoked via non-interactive
# ssh instead of hanging on the password prompt forever.
#
# PREREQUISITE NOTE (post-B2a-1): the script validates ALL SIX units
# (incl. their env-files) BEFORE any `sudo cp` lands. First-time
# install of any newly-added unit therefore requires its dedicated
# home-rooted env-file to be provisioned per the matching ops/CLAUDE.md
# deploy runbook BEFORE running this script:
#   - kalshi-collector       → /home/botuser/.env.collector
#                              (ops/CLAUDE.md "D1.5 collector deploy")
#   - kalshi-coinbase-collector → /home/botuser/.env.coinbase-collector
#                              (ops/CLAUDE.md "D2.5 Coinbase collector deploy")
#   - kalshi-weather-collector → /home/botuser/.env.weather-collector
#                              (ops/CLAUDE.md "D1.8 weather collector deploy")
#   - kalshi-espn-collector  → /home/botuser/.env.espn-collector
#                              (ops/CLAUDE.md "D1.11.a ESPN collector deploy")
#   - kalshi-venue-l2-collector → /home/botuser/.env.venue-l2-collector
#                              (ops/CLAUDE.md "B2a-1 venue-L2 collector deploy")
# The two-pass design (validate-ALL then install-ALL) prevents half-
# installed state — a routine re-install for a bot-only systemd edit
# still requires every collector env-file present. If the operator
# hasn't yet provisioned a required env-file, the per-unit FAIL hint
# (grep for the matching `D<N>.<m> operator runbook` line in this
# script's failure output) tells them what to populate.
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
    "kalshi-coinbase-collector"
    "kalshi-weather-collector"
    "kalshi-espn-collector"
    "kalshi-venue-l2-collector"
)
UNIT_WRAPPERS=(
    "$REPO_ROOT/start.sh"
    "$REPO_ROOT/collector-start.sh"
    "$REPO_ROOT/coinbase-collector-start.sh"
    "$REPO_ROOT/weather-collector-start.sh"
    "$REPO_ROOT/espn-collector-start.sh"
    "$REPO_ROOT/venue-l2-collector-start.sh"
)
# Env-file paths that the unit's EnvironmentFile= directive references.
# Bot: repo-rooted .env (existing). Kalshi collector: home-rooted
# .env.collector (D1.5 — credential isolation, see ops/CLAUDE.md).
# Coinbase collector: home-rooted .env.coinbase-collector (D2.5 — same
# isolation pattern as Kalshi, separate file for separate failure
# domain + separate credential surface). Weather collector: home-rooted
# .env.weather-collector (D1.8 — same isolation; no API key needed
# since Open-Meteo is keyless, but the env file still carries bronze
# root + rclone remote + bucket knobs that should survive deploys).
# ESPN collector: home-rooted .env.espn-collector (D1.11.a — same
# isolation; no API key since site.api.espn.com is keyless, but the
# env file carries bronze root + rclone remote + bucket + poll
# interval knobs).
UNIT_ENV_FILES=(
    "$REPO_ROOT/.env"
    "/home/botuser/.env.collector"
    "/home/botuser/.env.coinbase-collector"
    "/home/botuser/.env.weather-collector"
    "/home/botuser/.env.espn-collector"
    "/home/botuser/.env.venue-l2-collector"
)
# Expected directive lines per unit. Each entry is the literal
# `Key=Value` line that must appear at column 0 in the source unit.
# The ExecStart / WorkingDirectory / EnvironmentFile triple is what
# install.sh validates pre-cp; loosening this would re-open the Bit
# 2.1a incident class.
UNIT_EXPECTED_EXECSTART=(
    "ExecStart=$REPO_ROOT/start.sh"
    "ExecStart=$REPO_ROOT/collector-start.sh"
    "ExecStart=$REPO_ROOT/coinbase-collector-start.sh"
    "ExecStart=$REPO_ROOT/weather-collector-start.sh"
    "ExecStart=$REPO_ROOT/espn-collector-start.sh"
    "ExecStart=$REPO_ROOT/venue-l2-collector-start.sh"
)
UNIT_EXPECTED_WORKINGDIR=(
    "WorkingDirectory=$REPO_ROOT"
    "WorkingDirectory=$REPO_ROOT"
    "WorkingDirectory=$REPO_ROOT"
    "WorkingDirectory=$REPO_ROOT"
    "WorkingDirectory=$REPO_ROOT"
    "WorkingDirectory=$REPO_ROOT"
)
UNIT_EXPECTED_ENVFILE=(
    "EnvironmentFile=$REPO_ROOT/.env"
    "EnvironmentFile=/home/botuser/.env.collector"
    "EnvironmentFile=/home/botuser/.env.coinbase-collector"
    "EnvironmentFile=/home/botuser/.env.weather-collector"
    "EnvironmentFile=/home/botuser/.env.espn-collector"
    "EnvironmentFile=/home/botuser/.env.venue-l2-collector"
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
    # For the bot, that's $REPO_ROOT/.env; for the Kalshi collector,
    # that's /home/botuser/.env.collector; for the Coinbase collector,
    # that's /home/botuser/.env.coinbase-collector (both operator-
    # provisioned; see ops/CLAUDE.md).
    if [ ! -f "$env_file" ]; then
        echo "FAIL: $env_file missing."
        if [ "$name" = "kalshi-collector" ]; then
            echo "      D1.5 operator runbook: provision /home/botuser/.env.collector"
            echo "      with KALSHI_COLLECTOR_KEY_ID / KALSHI_COLLECTOR_KEY_PATH /"
            echo "      COLLECTOR_BRONZE_ROOT / COLLECTOR_CONN_COUNT / RCLONE_REMOTE /"
            echo "      S3_BUCKET. See ops/CLAUDE.md + kb/decisions/d1-5-pickup-prompt-may16.md."
        elif [ "$name" = "kalshi-coinbase-collector" ]; then
            echo "      D2.5 operator runbook: provision /home/botuser/.env.coinbase-collector"
            echo "      with COINBASE_BRONZE_ROOT (recommended"
            echo "      /var/lib/kalshi-coinbase-collector/bronze) / RCLONE_REMOTE /"
            echo "      S3_BUCKET. No PEM or KEY_ID needed at D2.5 (public channels only;"
            echo "      D2.1.5 narrowed auth scope). See ops/CLAUDE.md \"D2.5 Coinbase"
            echo "      collector deploy\" + feedback_vps_sudoers_collector_gap_may17"
            echo "      (sudoers NOPASSWD extension for kalshi-coinbase-collector)."
        elif [ "$name" = "kalshi-weather-collector" ]; then
            echo "      D1.8 operator runbook: provision /home/botuser/.env.weather-collector"
            echo "      with WEATHER_BRONZE_ROOT (recommended"
            echo "      /var/lib/kalshi-weather-collector/bronze) / RCLONE_REMOTE /"
            echo "      S3_BUCKET / WEATHER_POLL_INTERVAL_SECONDS (default 3600). No PEM"
            echo "      or KEY_ID needed at D1.8 (Open-Meteo is free + keyless;"
            echo "      kb-research/bot/weather-nwp-analysis.md confirms 10K req/day quota)."
            echo "      See ops/CLAUDE.md \"D1.8 weather collector deploy\" +"
            echo "      feedback_vps_sudoers_collector_gap_may17 (sudoers NOPASSWD extension"
            echo "      for kalshi-weather-collector)."
        elif [ "$name" = "kalshi-espn-collector" ]; then
            echo "      D1.11.a operator runbook: provision /home/botuser/.env.espn-collector"
            echo "      with ESPN_BRONZE_ROOT (recommended"
            echo "      /var/lib/kalshi-espn-collector/bronze) / RCLONE_REMOTE /"
            echo "      S3_BUCKET / ESPN_POLL_INTERVAL_SECONDS (default 60). No PEM"
            echo "      or KEY_ID needed at D1.11.a (site.api.espn.com is free + keyless;"
            echo "      the bot has polled it for ~year without throttling)."
            echo "      See ops/CLAUDE.md \"D1.11.a ESPN collector deploy\" +"
            echo "      feedback_vps_sudoers_collector_gap_may17 (sudoers NOPASSWD extension"
            echo "      for kalshi-espn-collector)."
        elif [ "$name" = "kalshi-venue-l2-collector" ]; then
            echo "      B2a-1 operator runbook (ticket 86ba1zf5j): provision"
            echo "      /home/botuser/.env.venue-l2-collector with VENUE_L2_BRONZE_ROOT"
            echo "      (recommended /var/lib/kalshi-venue-l2-collector/bronze) /"
            echo "      RCLONE_REMOTE / S3_BUCKET (+ optional VENUE_L2_HEALTH_SIDECAR_PATH)."
            echo "      No PEM or KEY_ID needed (Kraken + Bitstamp + Gemini public L2 WS;"
            echo "      no auth). Requires-approval: starting this unit begins the ~14d"
            echo "      bronze-accumulation clock for the synthetic-RTI RMSE validation."
            echo "      See ops/CLAUDE.md \"B2a-1 venue-L2 collector deploy\" +"
            echo "      feedback_vps_sudoers_collector_gap_may17 (sudoers NOPASSWD extension"
            echo "      for kalshi-venue-l2-collector)."
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
echo "Restart hints (post-D2.5 deploy.yml auto-restarts kalshi-collector + kalshi-coinbase-collector on path-affecting deploys; D1.8 weather collector + D1.11.a ESPN collector + B2a-1 venue-L2 collector are REQUIRES-APPROVAL at ship — no path-aware restart block yet, file as fu if desired; manual restart only needed for first-install / post-stop resume / out-of-band hotfix):"
echo "  Bot                : sudo -n /bin/systemctl restart kalshi-bot"
echo "  Kalshi collector   : sudo -n /bin/systemctl restart kalshi-collector  (NOPASSWD assumes operator has extended /etc/sudoers.d/botuser-systemctl-restart to include kalshi-collector; see feedback_vps_sudoers_collector_gap_may17)"
echo "  Coinbase collector : sudo -n /bin/systemctl restart kalshi-coinbase-collector  (NOPASSWD assumes operator has further extended sudoers to include kalshi-coinbase-collector; see ops/CLAUDE.md D2.5 deploy section)"
echo "  Weather collector  : sudo -n /bin/systemctl restart kalshi-weather-collector  (NOPASSWD assumes operator has further extended sudoers to include kalshi-weather-collector; see ops/CLAUDE.md D1.8 deploy section)"
echo "  ESPN collector     : sudo -n /bin/systemctl restart kalshi-espn-collector  (NOPASSWD assumes operator has further extended sudoers to include kalshi-espn-collector; see ops/CLAUDE.md D1.11.a deploy section)"
echo "  Venue-L2 collector : sudo -n /bin/systemctl restart kalshi-venue-l2-collector  (NOPASSWD assumes operator has further extended sudoers to include kalshi-venue-l2-collector; see ops/CLAUDE.md B2a-1 deploy section)"
