#!/bin/bash
# Pre-deploy validation script.
# Run before every push to main (auto-deploys to VPS).
# Exit 1 = block deploy, exit 0 = safe to deploy.

set -e
cd "$(dirname "$0")/.."

# Activate venv if present (bot/_impl.py imports websockets, etc.)
if [ -f "venv/bin/activate" ]; then
    source venv/bin/activate
fi

echo "=== Pre-Deploy Check ==="

# 1. Syntax check critical files
echo "[1/4] Syntax checking critical files..."
for f in bot/_impl.py market_config.py dashboard_snapshot.py sports_engine.py bot/engines/spx_engine.py weather_engine.py; do  # Sprint 10.1b (2026-05-11): spx_engine relocated to bot/engines/
    if [ -f "$f" ]; then
        python3 -c "import ast; ast.parse(open('$f').read())" 2>&1 || {
            echo "FAIL: $f has syntax errors"
            exit 1
        }
    fi
done
echo "  OK: All files parse cleanly"

# 2. Config sync check (import market_config which calls validate_market_configs)
echo "[2/4] Validating config sync (bot/_impl.py ↔ market_config.py)..."
python3 -c "
import sys
sys.path.insert(0, '.')
from market_config import validate_market_configs
validate_market_configs()
print('  OK: All configs in sync')
" 2>&1 || {
    echo "FAIL: Config mismatch between bot/_impl.py and market_config.py"
    exit 1
}

# 3. Run regression tests
echo "[3/4] Running regression tests..."
python3 -m pytest tests/test_regression.py -x -q --tb=short 2>&1
if [ $? -ne 0 ]; then
    echo "FAIL: Regression tests failed"
    exit 1
fi

# 4. Verify critical constants haven't changed unexpectedly
echo "[4/4] Verifying critical constants..."
python3 -c "
import sys
sys.path.insert(0, '.')
import bot
checks = [
    ('OBSERVATION_MODE', bot.OBSERVATION_MODE, False),
    ('MIN_ENTRY_PRICE', bot.MIN_ENTRY_PRICE, 86),
    ('MAX_ENTRY_PRICE', bot.MAX_ENTRY_PRICE, 99),
    ('MAX_SECONDS_BEFORE_CLOSE', bot.MAX_SECONDS_BEFORE_CLOSE, 900),
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
