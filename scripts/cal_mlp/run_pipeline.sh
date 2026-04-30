#!/bin/bash
# Operator runner: extract → train → conformal for all 4 assets.
# Run from repo root: `bash scripts/cal_mlp/run_pipeline.sh [asset1 asset2 ...]`
#
# - Default: BTC ETH SOL XRP (in that order)
# - Each asset is sequential — earlier failures abort later ones
# - Logs to scripts/cal_mlp/run_pipeline_<UTC>.log AND stdout
# - Exit 0 = all assets produced phase5_bundle artifacts; exit non-zero on any
#   failure (read the log for the failed asset's traceback)
#
# Time budget: ~10-30 min per asset depending on row count, ~1-2 hours total.
# Memory: ~2-3 GB peak per asset (M=5 ensemble).
#
# This script is IDEMPOTENT — re-running after a partial run reuses cached
# extract bundles via cfg_fp lookup. Train.py respects --allow-resume for
# member-level recovery within a fold.
#
# Pre-deploy: run AFTER `python3 scripts/cal_mlp/smoke_check.py` exits 0.
#
# Pre-deploy expectations:
# - Repo at deploy/p2-cal-mlp branch (or merged to main)
# - state.db has at least the trailing 30+ days of evaluated_opportunities
# - models/cal_mlp_<asset>/ directories will be created if absent
#
# After this script: each asset has
#   models/cal_mlp_<asset>/<train_id>/cal_mlp_<asset>_<train_id>_phase5_bundle.json
#   plus member checkpoints + conformal artifact
#   plus models/cal_mlp_<asset>/CURRENT pointing at the latest train_id
#
# DATA-SCOPE FLAG (R-p7-deploy-r11, 2026-04-29):
#   By default this pipeline passes `--include-sub-floor` to extract_data.py,
#   which lowers the per-asset extract floor from ASSET_FLOORS (BTC 88 / ETH 90
#   / SOL 86 / XRP 92) to GLOBAL_MIN_ENTRY_PRICE=75. Without this flag, all
#   `floor_raise_shadow` / `eth_low_floor_shadow` / `low_price_shadow` rows
#   below per-asset MIN_ENTRY get bucketed into `below_asset_floor` and
#   discarded — wasting the carefully-collected sub-floor shadow data.
#
#   Override for legacy/v1-style runs: set INCLUDE_SUB_FLOOR=0 in env to
#   reproduce the v1 (Apr 28) per-asset-floor cfg_fp. Note this changes
#   cfg_fp, so the resulting bundle is NOT bit-equivalent to v1 even if
#   trained on the same window — bundles with different cfg_fp can't be
#   A/B compared at Phase 6.
#
#   Decision rationale: kb/concepts/calibrator-data-hygiene-apr29.md.

set -uo pipefail  # NOT -e — we want to capture per-asset failures, not abort

# Default: include sub-floor data (v2/v3-and-beyond).
# Override: set `INCLUDE_SUB_FLOOR=0 bash scripts/cal_mlp/run_pipeline.sh ...`
# to reproduce the v1 cfg_fp with per-asset floors only.
INCLUDE_SUB_FLOOR="${INCLUDE_SUB_FLOOR:-1}"
if [ "$INCLUDE_SUB_FLOOR" = "1" ]; then
    SUB_FLOOR_FLAG="--include-sub-floor"
    BUNDLE_CLASS="v2+ (sub-floor data INCLUDED + sigma winsor=25; new cfg_fp lineage)"
else
    SUB_FLOOR_FLAG=""
    # NOT bit-equivalent to the original v1 (Apr 28) bundle: cfg_fp now
    # also includes sigma_winsor_abs_cap=25 which the original v1 lacked.
    # Strict v1 reproduction would also require reverting the winsorize.
    BUNDLE_CLASS="v1+winsor (per-asset floors only, sigma winsor=25; NOT bit-equal to Apr 28 v1)"
fi
echo "================================================================"
echo "[pipeline] cfg_fp class: $BUNDLE_CLASS"
echo "[pipeline] To switch: INCLUDE_SUB_FLOOR=$([ "$INCLUDE_SUB_FLOOR" = "1" ] && echo 0 || echo 1) bash $0 ..."
echo "================================================================"

if [ ! -f "scripts/cal_mlp/run_pipeline.sh" ]; then
    echo "ERROR: run from repo root (where scripts/cal_mlp/run_pipeline.sh lives)"
    exit 1
fi

ASSETS=("${@:-BTC ETH SOL XRP}")
if [ "${#ASSETS[@]}" -eq 1 ] && [ "${ASSETS[0]}" = "BTC ETH SOL XRP" ]; then
    ASSETS=(BTC ETH SOL XRP)
fi

CUTOFF_END="${CUTOFF_END:-$(date -u +%Y-%m-%dT%H:%M:%SZ)}"
LOG="scripts/cal_mlp/run_pipeline_$(date -u +%Y%m%dT%H%M%SZ).log"

# Smoke-check first — abort if env or invariants are wrong.
echo "[$(date -u +%H:%M:%S)] running smoke_check.py" | tee "$LOG"
if ! python3 scripts/cal_mlp/smoke_check.py 2>&1 | tee -a "$LOG"; then
    echo "[$(date -u +%H:%M:%S)] FAIL: smoke_check failed; aborting before training" | tee -a "$LOG"
    exit 2
fi
echo "[$(date -u +%H:%M:%S)] smoke_check passed; proceeding with pipeline" | tee -a "$LOG"

declare -a SUCCEEDED FAILED
for ASSET in "${ASSETS[@]}"; do
    echo "" | tee -a "$LOG"
    echo "================================================================" | tee -a "$LOG"
    echo "[$(date -u +%H:%M:%S)] $ASSET — Phase 2 (extract)" | tee -a "$LOG"
    echo "================================================================" | tee -a "$LOG"
    echo "[$(date -u +%H:%M:%S)] $ASSET extract: INCLUDE_SUB_FLOOR=$INCLUDE_SUB_FLOOR (flag=\"$SUB_FLOOR_FLAG\")" | tee -a "$LOG"
    if ! python3 scripts/cal_mlp/extract_data.py --asset "$ASSET" \
            --cutoff-end "$CUTOFF_END" $SUB_FLOOR_FLAG 2>&1 | tee -a "$LOG"; then
        echo "[$(date -u +%H:%M:%S)] FAIL $ASSET — extract" | tee -a "$LOG"
        FAILED+=("$ASSET (extract)")
        continue
    fi

    # extract_data.py prints the train_id as part of its summary JSON; parse it.
    EXTRACT_TRAIN_ID=$(python3 - <<EOF
import json, sys
log = open("$LOG").read()
# Find the LAST JSON summary line (extract_data.py emits one)
for line in reversed(log.splitlines()):
    line = line.strip()
    if line.startswith('{') and '"asset": "$ASSET"' in line and '"train_id"' in line:
        try:
            print(json.loads(line)["train_id"])
            sys.exit(0)
        except Exception:
            pass
sys.exit(1)
EOF
    )
    if [ -z "${EXTRACT_TRAIN_ID:-}" ]; then
        echo "[$(date -u +%H:%M:%S)] FAIL $ASSET — could not parse extract train_id" | tee -a "$LOG"
        FAILED+=("$ASSET (extract train_id parse)")
        continue
    fi
    echo "[$(date -u +%H:%M:%S)] $ASSET extract train_id: $EXTRACT_TRAIN_ID" | tee -a "$LOG"

    echo "" | tee -a "$LOG"
    echo "[$(date -u +%H:%M:%S)] $ASSET — Phase 4 (train M=5 ensemble)" | tee -a "$LOG"
    if ! python3 scripts/cal_mlp/train.py --asset "$ASSET" \
            --extract-train-id "$EXTRACT_TRAIN_ID" \
            --allow-resume 2>&1 | tee -a "$LOG"; then
        echo "[$(date -u +%H:%M:%S)] FAIL $ASSET — train" | tee -a "$LOG"
        FAILED+=("$ASSET (train)")
        continue
    fi

    # train.py emits a summary JSON with bundle_sha (= phase4_bundle_sha)
    P4_SHA=$(python3 - <<EOF
import json, sys
log = open("$LOG").read()
for line in reversed(log.splitlines()):
    line = line.strip()
    if line.startswith('{') and '"asset": "$ASSET"' in line and '"phase4_bundle_sha"' in line:
        try:
            print(json.loads(line)["phase4_bundle_sha"])
            sys.exit(0)
        except Exception:
            pass
sys.exit(1)
EOF
    )
    if [ -z "${P4_SHA:-}" ]; then
        echo "[$(date -u +%H:%M:%S)] FAIL $ASSET — could not parse phase4_bundle_sha" | tee -a "$LOG"
        FAILED+=("$ASSET (train sha parse)")
        continue
    fi
    echo "[$(date -u +%H:%M:%S)] $ASSET phase4_bundle_sha: $P4_SHA" | tee -a "$LOG"

    echo "" | tee -a "$LOG"
    echo "[$(date -u +%H:%M:%S)] $ASSET — Phase 5 (Mondrian conformal)" | tee -a "$LOG"
    if ! python3 scripts/cal_mlp/conformal.py --asset "$ASSET" \
            --bundle-sha "$P4_SHA" 2>&1 | tee -a "$LOG"; then
        echo "[$(date -u +%H:%M:%S)] FAIL $ASSET — conformal" | tee -a "$LOG"
        FAILED+=("$ASSET (conformal)")
        continue
    fi

    SUCCEEDED+=("$ASSET")
    echo "[$(date -u +%H:%M:%S)] $ASSET — pipeline complete (Phase 5 bundle deployed)" | tee -a "$LOG"
done

echo "" | tee -a "$LOG"
echo "================================================================" | tee -a "$LOG"
echo "[$(date -u +%H:%M:%S)] PIPELINE SUMMARY" | tee -a "$LOG"
echo "================================================================" | tee -a "$LOG"
echo "Succeeded: ${#SUCCEEDED[@]} (${SUCCEEDED[*]:-none})" | tee -a "$LOG"
echo "Failed: ${#FAILED[@]} (${FAILED[*]:-none})" | tee -a "$LOG"
echo "Log: $LOG" | tee -a "$LOG"

# Verify each succeeded asset has a CURRENT pointer + phase5_bundle on disk
echo "" | tee -a "$LOG"
echo "Disk verification:" | tee -a "$LOG"
ALL_GOOD=1
for ASSET in "${SUCCEEDED[@]}"; do
    DIR="models/cal_mlp_$ASSET"
    if [ ! -f "$DIR/CURRENT" ]; then
        echo "  $ASSET: MISSING $DIR/CURRENT" | tee -a "$LOG"
        ALL_GOOD=0
        continue
    fi
    TID=$(cat "$DIR/CURRENT")
    BUNDLE="$DIR/$TID/cal_mlp_${ASSET}_${TID}_phase5_bundle.json"
    if [ ! -f "$BUNDLE" ]; then
        echo "  $ASSET: MISSING $BUNDLE" | tee -a "$LOG"
        ALL_GOOD=0
    else
        echo "  $ASSET: $BUNDLE OK" | tee -a "$LOG"
    fi
done

if [ "${#FAILED[@]}" -gt 0 ] || [ "$ALL_GOOD" -eq 0 ]; then
    echo "[$(date -u +%H:%M:%S)] PIPELINE INCOMPLETE" | tee -a "$LOG"
    exit 1
fi
echo "[$(date -u +%H:%M:%S)] PIPELINE COMPLETE — all assets ready for deploy" | tee -a "$LOG"
exit 0
