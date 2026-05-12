#!/bin/bash
# Pre-deploy validation script.
# Run before every push to main (auto-deploys to VPS).
# Exit 1 = block deploy, exit 0 = safe to deploy.

set -e
# Bit 11.2 fu6 (2026-05-12, R6 indep-adv C-1): 2-hop CD after
# scripts/ → scripts/ops/ relocation. ONE hop resolves to `scripts/`
# and silently breaks every repo-root-relative step below.
cd "$(dirname "$0")/../.."

# Activate venv if present (bot.main_loop imports websockets, etc.)
if [ -f "venv/bin/activate" ]; then
    source venv/bin/activate
fi

echo "=== Pre-Deploy Check ==="

# 1. Syntax check critical files
# Post-Bit-9.3-iii.c (2026-05-11): bot/_impl.py DELETED. Canonical
# submodules listed below. Sprint 10 Bit 10.4 (2026-05-12)
# dashboard_snapshot.py → bot/snapshots/; Sprint 10.1b/c/d (2026-05-11):
# spx/weather/sports engines all relocated to bot/engines/.
echo "[1/4] Syntax checking critical files..."
for f in bot/main_loop.py bot/scanner/__init__.py bot/constants.py market_config.py bot/snapshots/dashboard_snapshot.py bot/engines/sports_engine.py bot/engines/spx_engine.py bot/engines/weather_engine.py; do
    if [ -f "$f" ]; then
        python3 -c "import ast; ast.parse(open('$f').read())" 2>&1 || {
            echo "FAIL: $f has syntax errors"
            exit 1
        }
    fi
done
echo "  OK: All files parse cleanly"

# 2. Config sync check (import market_config which calls validate_market_configs)
echo "[2/4] Validating config sync (bot.constants ↔ market_config.py)..."
python3 -c "
import sys
sys.path.insert(0, '.')
from market_config import validate_market_configs
validate_market_configs()
print('  OK: All configs in sync')
" 2>&1 || {
    echo "FAIL: Config mismatch between bot.constants and market_config.py"
    exit 1
}

# 3. Run regression tests
echo "[3/4] Running regression tests..."
python3 -m pytest tests/integration/test_regression.py -x -q --tb=short 2>&1
if [ $? -ne 0 ]; then
    echo "FAIL: Regression tests failed"
    exit 1
fi

# 4. Verify critical constants haven't changed unexpectedly
echo "[4/4] Verifying critical constants..."
python3 -c "
import sys
sys.path.insert(0, '.')
import bot.constants
checks = [
    ('OBSERVATION_MODE', bot.constants.OBSERVATION_MODE, False),
    ('MIN_ENTRY_PRICE', bot.constants.MIN_ENTRY_PRICE, 86),
    ('MAX_ENTRY_PRICE', bot.constants.MAX_ENTRY_PRICE, 99),
    ('MAX_SECONDS_BEFORE_CLOSE', bot.constants.MAX_SECONDS_BEFORE_CLOSE, 900),
]
changed = []
for name, actual, expected in checks:
    if actual != expected:
        changed.append(f'  {name}: {expected} -> {actual}')
if changed:
    print('WARNING: Critical constants changed:')
    for c in changed:
        print(c)
    print('  Verify this is intentional before deploying.')
else:
    print('  OK: All critical constants at expected values')
" 2>&1

echo ""
echo "=== All pre-deploy checks passed ==="
