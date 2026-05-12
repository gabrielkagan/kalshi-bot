#!/usr/bin/env bash
# Unified audit runner — wraps all 5 audit scripts with consistent interface.
#
# Usage:
#   ./scripts/audit/audit_runner.sh --module 15m                     # single module
#   ./scripts/audit/audit_runner.sh --module all --alert             # all + Telegram alerts
#   ./scripts/audit/audit_runner.sh --module hourly --since 2026-03-01
#   ./scripts/audit/audit_runner.sh --module all --fetch --alert     # SCP from VPS first
#   ./scripts/audit/audit_runner.sh --module all --vps               # run on VPS directly
#   ./scripts/audit/audit_runner.sh --module all --local             # use /tmp/state.db

set -euo pipefail

# Bit 11.2 fu3 (2026-05-12, L98 path-anchor): file now lives at
# `scripts/audit/audit_runner.sh` — REPO_DIR is TWO parent hops, not one.
# Prior `$SCRIPT_DIR/..` resolved to `scripts/`, breaking every
# `$REPO_DIR/scripts/audit/X.py` invocation below (path doubled to
# `scripts/scripts/audit/...`). Also broke `audit_alerts.py` at line 173.
# Operationally invoked by `scripts/ops/setup_full_audit_timer.sh` as a
# systemd timer with `--quiet`, so the silent-fail would have been
# invisible on the VPS until alerts stopped firing.
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"

# ── Defaults ────────────────────────────────────────────────────
MODULE="all"
SINCE=""
DB_MODE="fetch"       # fetch | vps | local
JSON_DIR="/tmp/audit_artifacts"
ALERT=false
QUIET=false

# Default regime timestamps (same as audit_cron.py)
declare -A REGIME_SINCE=(
    [15m]="2026-02-28T00:00:00"
    [hourly]="2026-02-28T18:30:00"
    [spx]="2026-03-02T00:00:00"
    [weather]="2026-03-02T16:54:00"
    [sports]="2026-03-01T00:00:00"
)

# ── Arg parsing ─────────────────────────────────────────────────
while [[ $# -gt 0 ]]; do
    case "$1" in
        --module)   MODULE="$2"; shift 2 ;;
        --since)    SINCE="$2"; shift 2 ;;
        --fetch)    DB_MODE="fetch"; shift ;;
        --vps)      DB_MODE="vps"; shift ;;
        --local)    DB_MODE="local"; shift ;;
        --json-dir) JSON_DIR="$2"; shift 2 ;;
        --alert)    ALERT=true; shift ;;
        --quiet)    QUIET=true; shift ;;
        -h|--help)
            echo "Usage: $0 [--module 15m|hourly|spx|weather|sports|all]"
            echo "          [--since YYYY-MM-DD] [--fetch|--vps|--local]"
            echo "          [--json-dir DIR] [--alert] [--quiet]"
            exit 0
            ;;
        *) echo "Unknown option: $1"; exit 1 ;;
    esac
done

# ── Resolve DB path ─────────────────────────────────────────────
case "$DB_MODE" in
    fetch)
        DB_PATH="/tmp/state.db"
        echo "[audit_runner] Fetching state.db from VPS..."
        # WAL checkpoint first to ensure consistency
        ssh botuser@45.55.181.30 "cd ~/kalshi-bot-repo && sqlite3 state.db 'PRAGMA wal_checkpoint(TRUNCATE);'" 2>/dev/null || true
        scp botuser@45.55.181.30:~/kalshi-bot-repo/state.db "$DB_PATH"
        echo "[audit_runner] state.db copied to $DB_PATH"
        ;;
    vps)
        DB_PATH="/home/botuser/kalshi-bot-repo/state.db"
        ;;
    local)
        DB_PATH="/tmp/state.db"
        if [[ ! -f "$DB_PATH" ]]; then
            echo "ERROR: $DB_PATH not found. Use --fetch to copy from VPS."
            exit 1
        fi
        ;;
esac

# ── Setup ────────────────────────────────────────────────────────
mkdir -p "$JSON_DIR"

# Activate venv if present
if [[ -f "$REPO_DIR/venv/bin/activate" ]]; then
    source "$REPO_DIR/venv/bin/activate"
fi

PYTHON="${REPO_DIR}/venv/bin/python3"
if [[ ! -x "$PYTHON" ]]; then
    PYTHON="python3"
fi

# ── Module list ──────────────────────────────────────────────────
if [[ "$MODULE" == "all" ]]; then
    MODULES=(15m hourly spx weather sports)
else
    MODULES=("$MODULE")
fi

log() {
    if [[ "$QUIET" == "false" ]]; then
        echo "$@"
    fi
}

FAILED=()
PASSED=()

for mod in "${MODULES[@]}"; do
    # Resolve --since: explicit > regime default
    mod_since="${SINCE:-${REGIME_SINCE[$mod]:-}}"
    since_flag=""
    if [[ -n "$mod_since" ]]; then
        since_flag="--since $mod_since"
    fi

    json_path="${JSON_DIR}/${mod}_audit.json"

    log ""
    log "━━━ Running $mod audit ━━━"

    case "$mod" in
        15m)
            cmd="$PYTHON $REPO_DIR/scripts/audit/15m_live_audit.py --db $DB_PATH $since_flag --json $json_path"
            ;;
        hourly)
            cmd="$PYTHON $REPO_DIR/scripts/audit/hourly_shadow_audit.py --db $DB_PATH $since_flag --json $json_path"
            ;;
        spx)
            cmd="$PYTHON $REPO_DIR/scripts/audit/spx_shadow_audit.py --db $DB_PATH $since_flag --json $json_path"
            ;;
        weather)
            cmd="$PYTHON $REPO_DIR/scripts/audit/weather_shadow_audit.py --db $DB_PATH $since_flag --json $json_path"
            ;;
        sports)
            cmd="$PYTHON $REPO_DIR/scripts/audit/sports_shadow_audit.py --db $DB_PATH $since_flag --json $json_path"
            ;;
        *)
            echo "ERROR: Unknown module: $mod"
            FAILED+=("$mod")
            continue
            ;;
    esac

    if [[ "$QUIET" == "true" ]]; then
        if eval "$cmd" > /dev/null 2>&1; then
            PASSED+=("$mod")
        else
            FAILED+=("$mod")
            log "  FAILED: $mod"
        fi
    else
        if eval "$cmd"; then
            PASSED+=("$mod")
        else
            FAILED+=("$mod")
            log "  FAILED: $mod"
        fi
    fi
done

# ── Summary ──────────────────────────────────────────────────────
log ""
log "━━━ Audit Summary ━━━"
log "  Passed: ${PASSED[*]:-none}"
if [[ ${#FAILED[@]} -gt 0 ]]; then
    log "  FAILED: ${FAILED[*]}"
fi
log "  Artifacts: $JSON_DIR/"

# ── Alert check ──────────────────────────────────────────────────
if [[ "$ALERT" == "true" ]]; then
    log ""
    log "━━━ Running invariant checks ━━━"
    alert_flags="--json-dir $JSON_DIR --telegram"
    $PYTHON "$REPO_DIR/scripts/audit/audit_alerts.py" $alert_flags || true
fi

# Exit with failure if any module failed
if [[ ${#FAILED[@]} -gt 0 ]]; then
    exit 1
fi
