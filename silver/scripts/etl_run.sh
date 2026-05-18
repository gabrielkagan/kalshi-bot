#!/usr/bin/env bash
# silver/scripts/etl_run.sh — launchd entry point for nightly silver ETL.
#
# D3.0 (ticket 86b9zxc6t, 2026-05-18). Plan doc: kb/decisions/d3-0-silver-foundations-plan.md.
#
# Invoked by launchd at 02:00 Mac-local (per decision #2 in plan doc).
# Defers `target_date` computation to silver.scripts.etl_run (TZ-independent;
# always processes the prior UTC day).
#
# Operator must export the following environment before invocation:
#   AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY  (or AWS_PROFILE)
#   AWS_DEFAULT_REGION  (default: us-east-1)
# Either via launchd EnvironmentVariables or via a .envrc the operator
# sources from this script (see README.md "Operator setup" section).
#
# Exits 0 on success, non-zero on any silver-ETL failure (launchd surfaces
# non-zero exits via StandardErrorPath = /tmp/silver-etl.err).

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SILVER_ROOT="$(dirname "$SCRIPT_DIR")"
REPO_ROOT="$(dirname "$SILVER_ROOT")"

# Optional venv activation — if silver/venv/ exists, prefer it; otherwise
# rely on the operator's system Python having the requirements.txt deps
# installed (per the Bit-kickoff verify list item #1).
if [[ -f "$SILVER_ROOT/venv/bin/activate" ]]; then
    # shellcheck disable=SC1091
    source "$SILVER_ROOT/venv/bin/activate"
fi

# Allow override; default to S3 production.
BRONZE_ROOT="${SILVER_BRONZE_ROOT:-s3://kalshi-bot-archive/bronze}"
SILVER_ROOT_S3="${SILVER_SILVER_ROOT:-s3://kalshi-bot-archive/silver/v1}"

cd "$REPO_ROOT"
exec python3 -m silver.scripts.etl_run \
    --bronze-root "$BRONZE_ROOT" \
    --silver-root "$SILVER_ROOT_S3" \
    "$@"
