---
status: decided
updated: 2026-03-21
tags: [decision, architecture, refactor]
date: 2026-03-21
---
# Decision: Extract config.py and models.py from bot.py

Date: 2026-03-21
Status: Decided

## Context
bot.py had grown to ~14,000 lines with all trading constants and pure-math model classes inline. This caused two problems:

1. **Constant duplication**: `market_config.py` needed to mirror bot.py constants for startup validation (`validate_market_configs()`). Any mismatch caused crash loops on VPS (learned Mar 1: `MAX_SECONDS_BEFORE_CLOSE` changed in bot.py but not market_config.py caused 80-second crash loop).
2. **Test dependency bloat**: Tests importing constants from bot.py pulled in the entire dependency tree (requests, websockets, cryptography, etc.), slowing test startup and causing import failures in CI environments without all production dependencies.

## Options Considered

### 1. Full modular refactor (split bot.py into 10+ files)
- Would reduce bot.py from 14K to ~3K lines
- **Rejected**: systemd service, `start.sh`, and the deploy pipeline (`deploy.yml`) all depend on the single-file `bot.py` structure. Changing this requires coordinated updates to VPS systemd units, GitHub Actions, and startup scripts. The risk of a broken deploy on a live trading bot is unacceptable.
- Engines (spx_engine.py, weather_engine.py, analyst.py, sports_engine.py) are exceptions because they run as separate threads/processes with their own entry points.

### 2. Extract only shared constants into config.py
- Solves the duplication problem
- Tests and market_config.py can import from config.py without pulling bot.py
- Low risk: `from config import *` in bot.py means runtime behavior is unchanged

### 3. Extract constants AND pure-math classes
- Extends option 2 by also extracting stateless model classes
- Classes: `EGARCHEstimator`, `MincerZarnowitzTracker`, `PositionSizer`, fee calculations, TV RK weight logic
- These have zero side effects (no API calls, no DB, no websockets) — only depend on config.py constants and scipy
- Makes models independently testable

## Decision
**Option 3**: Extract both `config.py` and `models.py`.

- **config.py**: Shared constants, `from config import *` in bot.py. Contains ASSETS list, probability engine params, per-asset distribution config loader, sizing tiers, EGARCH bounds, drawdown thresholds, and all constants needed by both bot.py and market_config.py.
- **models.py**: Pure-math classes with `from models import ...` in bot.py. Contains EGARCHEstimator, MincerZarnowitzTracker, PositionSizer, fee calculators, TV RK weights. Depends only on config.py and standard library + scipy.

## Consequences

### Positive
- market_config.py imports constants from config.py — single source of truth, no duplication
- Tests import from config.py/models.py without triggering bot.py's heavy imports
- Models are independently testable with fast imports
- bot.py line count reduced (still ~14,600 lines with all trading logic)

### Negative
- Three files to check when changing constants (config.py is now the source, but grep is still required)
- `from config import *` is a namespace pollution pattern — acceptable here because bot.py already treated these as globals

### Import Chain
```
config.py  <-- market_config.py, tests, models.py
models.py  <-- bot.py (from models import ...)
bot.py     <-- from config import *, from models import ...
```

## Related
- [[concepts/market-config-system.md]]
