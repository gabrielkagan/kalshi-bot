---
status: active
updated: 2026-04-01
tags: [calibration, registry, per-product]
---
# CalEngine Registry

## Summary
The `_CAL_REGISTRY` is a global dict in bot.py mapping `"{product_type}_{subtype}"` keys to `CalibrationEngine` instances. It provides per-market-type probability calibration for all non-15M systems. 15M uses `_CALIBRATION_ENGINE` directly and is explicitly excluded from the registry (enforced by startup assertion). The registry currently holds engines for hourly, spx_hourly, 5 weather cities, and 8 sport groups.

## Registry Architecture

### Global State
```python
_CALIBRATION_ENGINE: Optional["CalibrationEngine"] = None   # 15M ONLY
_CAL_REGISTRY: Dict[str, "CalibrationEngine"] = {}          # non-15M engines
```

### Key Format
- **Per-product (no subtypes)**: `"hourly"`, `"spx_hourly"` -- one engine for the entire product type
- **Per-subtype**: `"weather_NYC"`, `"weather_CHI"`, `"sports_basketball"`, `"sports_hockey"` -- subtype is city or sport group
- Subtype mappings defined in `market_config.py` via `MarketTypeConfig.cal_subtypes` dict

### Registration (at startup in `MainLoop.__init__`)
1. Iterate all product types in `MARKET_CONFIGS`
2. If `cal_subtypes` is non-empty, register one engine per subtype (e.g., 5 weather cities, 8 sport groups)
3. If `cal_engine_state_path` is set but no subtypes, register one engine for the product type (hourly, spx_hourly)
4. Clear registry on restart for clean state
5. **Startup assertion**: `assert "15m" not in _CAL_REGISTRY` -- 15M must never be in the registry

### Current Registry Contents
| Key | State File | Enabled | Notes |
|-----|-----------|---------|-------|
| `hourly` | `hourly_calibration_state.json` | False | Disabled: passthrough + T=1.45 is better |
| `spx_hourly` | `spx_hourly_calibration_state.json` | True | SPX-D: learned temperature active |
| `weather_NYC` | `cal_weather_NYC.json` | True | Per-city weather CalEngine |
| `weather_CHI` | `cal_weather_CHI.json` | True | |
| `weather_MIA` | `cal_weather_MIA.json` | True | |
| `weather_DEN` | `cal_weather_DEN.json` | True | |
| `weather_LAX` | `cal_weather_LAX.json` | True | |
| `sports_basketball` | `cal_sports_basketball.json` | True | Per-sport-group CalEngine |
| `sports_hockey` | `cal_sports_hockey.json` | True | |
| `sports_tennis` | `cal_sports_tennis.json` | True | |
| `sports_soccer` | `cal_sports_soccer.json` | True | |
| `sports_baseball` | `cal_sports_baseball.json` | True | |
| `sports_football` | `cal_sports_football.json` | True | |
| `sports_mma` | `cal_sports_mma.json` | True | |
| `sports_esports` | `cal_sports_esports.json` | True | |

## Lookup: `_resolve_cal_engine()`
```python
def _resolve_cal_engine(product_type, asset=None, require_enabled=False):
```
1. If `product_type` is None or `"15m"`, returns None (15M uses `_CALIBRATION_ENGINE` directly)
2. If the product type has `cal_subtypes` and asset is provided, derives the subtype key and looks up `"{product_type}_{subtype}"`
3. Falls back to bare `product_type` key (for hourly, spx_hourly)
4. If `require_enabled=True`, also returns None if `cal_engine_enabled=False` in config (prevents disabled engines from affecting predictions while still collecting observations)

## Settlement Routing
During settlement (`SettlementTracker`), each settled evaluation is routed to the correct CalEngine:
1. Call `_resolve_cal_engine(product_type, asset)` -- note: no `require_enabled` flag, so disabled engines still receive observations
2. If a registry engine is found, call `engine.add_observation(raw_prob, binary_outcome)`
3. If no registry engine and the filter stage is a known observation type (candidate, observation_trade, hourly_observation, etc.), route to `_CALIBRATION_ENGINE` (15M fallback) if the product config has `cal_eligible=True`

This means even disabled engines (like hourly) accumulate training data in shadow, so they can be evaluated for promotion without data gaps.

## The "Same Commit" Rule
When wiring any engine to the CalEngine pipeline, all three must ship in the SAME commit:
1. **Engine INSERT includes `raw_prob`** -- without this, settled rows have `raw_prob=NULL` and cannot feed the CalEngine
2. **Settlement code routes to the correct CalEngine** -- `_resolve_cal_engine()` must return the right engine
3. **Audit script checks for CalEngine observations** -- so monitoring catches gaps

Learned the hard way: sports `raw_prob` was added to the audit script before the INSERT was fixed, resulting in 134 rows with NULL `raw_prob` that could never feed the CalEngine (Mar 4, 2026).

## Rolling Brier Tracking
Each CalEngine instance tracks rolling Brier score for its own domain. The dashboard exposes per-engine diagnostics via `snap["cal_registry"]`:
- `n_observations`: total settled observations fed to the engine
- `active_method`: current calibration method (beta, platt, isotonic, or passthrough)
- `rolling_brier`: Brier score on recent predictions
- `learned_method_active`: whether a learned method has enough data to be active

## State Persistence
Each engine persists its state to a JSON file (listed in the table above). State includes fitted calibration parameters, observation history, and rolling metrics. On startup, engines load from their state files. On clean restarts, `_CAL_REGISTRY.clear()` ensures no stale engine references.

## Why 15M Is Excluded
The 15M CalEngine (`_CALIBRATION_ENGINE`) predates the registry system and has special behavior:
- Trained on 15M data only (hourly data explicitly excluded from `load_training_data_from_db()`)
- Uses different training criteria and warmup behavior
- Has its own dedicated code paths in `ProbabilityEngine.calibrate()`
- Mixing it into the registry would risk cross-contamination with non-15M calibration data

## Related
- [[concepts/sports-engine.md]]
- [[concepts/spx-engine.md]]
- [[concepts/weather-system.md]]
