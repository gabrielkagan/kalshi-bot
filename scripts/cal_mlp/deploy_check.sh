#!/bin/bash
# Operator deploy-readiness aggregator. Runs ALL pre-deploy gates in order.
# Run from repo root: `bash scripts/cal_mlp/deploy_check.sh`
#
# Exit 0 = ALL gates pass; safe to merge deploy/p2-cal-mlp → main and push.
# Exit non-zero = at least one gate failed; do NOT merge.
#
# Gates (in order; each must exit 0 to proceed):
#   1. ast.parse on bot/_impl.py + cal_mlp modules — catches syntax errors
#   2. pytest tests/test_cal_mlp_invariants.py — 33 regression tests
#   3. pytest tests/ -m "not fragile" — full ~2046-test suite
#   4. python3 scripts/cal_mlp/smoke_check.py — 6 end-to-end synthetic checks
#   5. (skipped if no models yet) bash scripts/cal_mlp/run_pipeline.sh
#
# Time budget: ~30 seconds for gates 1-4 on a modern laptop;
# +1-2 hours if gate 5 also runs (initial bundle generation).
#
# Why aggregate: ensures the operator runs ALL gates in a known order
# rather than relying on memory. Each gate is independently runnable
# but their outputs are interdependent (e.g., smoke_check assumes
# pytest passed; run_pipeline assumes smoke_check passed).

set -uo pipefail

if [ ! -f "scripts/cal_mlp/deploy_check.sh" ]; then
    echo "ERROR: run from repo root" >&2
    exit 2
fi

GATE_FAILED=()

run_gate() {
    local name="$1"
    shift
    echo "================================================================"
    echo "[$(date -u +%H:%M:%S)] GATE: $name"
    echo "================================================================"
    if "$@"; then
        echo "[$(date -u +%H:%M:%S)] [OK]   $name"
    else
        echo "[$(date -u +%H:%M:%S)] [FAIL] $name (exit $?)"
        GATE_FAILED+=("$name")
    fi
    echo
}

# Gate 1: syntax check
run_gate "ast.parse bot/_impl.py" \
    python3 -c "import ast; ast.parse(open('bot/_impl.py').read())"

run_gate "ast.parse cal_mlp modules" \
    python3 -c "
import ast, glob
for f in sorted(glob.glob('scripts/cal_mlp/*.py')):
    ast.parse(open(f).read())
print('all cal_mlp modules parsed clean')"

# Gate 2: cal_mlp regression suite (stdlib-only, fast)
run_gate "pytest cal_mlp invariants" \
    python3 -m pytest tests/test_cal_mlp_invariants.py -x --no-header -q

# Gate 3: full pytest suite (catches cross-test interactions)
run_gate "pytest full suite (not fragile)" \
    python3 -m pytest tests/ -m "not fragile" --no-header -q --tb=line

# Gate 4: smoke_check (requires torch + pandas + pyarrow + psutil)
if python3 -c "import torch, pandas, pyarrow, psutil" 2>/dev/null; then
    run_gate "smoke_check.py" \
        python3 scripts/cal_mlp/smoke_check.py
else
    echo "[$(date -u +%H:%M:%S)] [SKIP] smoke_check (deps not installed locally; VPS only)"
    echo
fi

# Gate 5: run_pipeline (only if no bundles exist AND deps available)
if python3 -c "import torch" 2>/dev/null; then
    has_any_bundle=0
    for asset in BTC ETH SOL XRP; do
        if [ -f "models/cal_mlp_$asset/CURRENT" ]; then
            has_any_bundle=1
            break
        fi
    done
    if [ "$has_any_bundle" -eq 0 ]; then
        echo "[$(date -u +%H:%M:%S)] No bundles found — gate 5 (run_pipeline) NOT run automatically."
        echo "[$(date -u +%H:%M:%S)] To generate bundles: bash scripts/cal_mlp/run_pipeline.sh"
        echo
    else
        echo "[$(date -u +%H:%M:%S)] Bundles already deployed; skipping run_pipeline."
        echo "[$(date -u +%H:%M:%S)] To regenerate: bash scripts/cal_mlp/run_pipeline.sh"
        echo
    fi
fi

# Summary
echo "================================================================"
echo "[$(date -u +%H:%M:%S)] DEPLOY-CHECK SUMMARY"
echo "================================================================"
if [ "${#GATE_FAILED[@]}" -eq 0 ]; then
    echo "All gates passed."
    echo "Safe to merge deploy/p2-cal-mlp → main and push."
    echo
    echo "Next steps:"
    echo "  1. (if needed) bash scripts/cal_mlp/run_pipeline.sh   # generate bundles"
    echo "  2. git checkout main && git merge --no-ff deploy/p2-cal-mlp"
    echo "  3. export CALMLP_ENABLED=0   # initial shadow mode"
    echo "  4. git push                  # auto-deploys via systemd"
    echo "  5. Verify boot logs: journalctl -u kalshi-bot -n 100 | grep CALMLP"
    exit 0
else
    echo "FAILED gates: ${GATE_FAILED[*]}"
    echo "Do NOT merge deploy/p2-cal-mlp → main."
    echo "Read the per-gate output above and fix before re-running."
    exit 1
fi
