---
status: active
updated: 2026-04-03
tags: [config, market-config, validation]
---
# Market Config System

## Summary

`market_config.py` provides a centralized, frozen dataclass (`MarketTypeConfig`)
that holds per-product-type configuration. It replaced scattered if/elif
branches and duplicated constants across files. A startup validator
(`validate_market_configs()`) asserts that bot.py constants match the mirrored
values in market_config.py, preventing the silent mismatches that previously
caused crash loops.

## MarketTypeConfig Dataclass

Each product type (15m, hourly, spx_hourly, weather, sports) gets one frozen
instance. Key fields:

| Field | Purpose |
|-------|---------|
| `product_type` | String identifier ("15m", "hourly", etc.) |
| `observation_only` | Whether the product is shadow/observation only |
| `min_entry_price` / `max_entry_price` | Price range in cents |
| `min_seconds_before_close` / `max_seconds_before_close` | STC window |
| `max_risk_per_trade` | Kelly sizing cap |
| `kelly_fraction` | Fractional Kelly multiplier (1.0 = full) |
| `market_blend_w` | Weight toward market price in probability blend |
| `temperature_t` | Calibration temperature scaling |
| `fee_multiplier_taker` / `fee_multiplier_maker` | Fee rates |
| `excluded_assets` | Frozen set of excluded asset symbols |
| `cal_engine_enabled` | Whether to instantiate a CalEngine |
| `cal_subtypes` | Dict mapping subtype codes to state file paths |

The dataclass is frozen (immutable) -- values cannot be changed after creation.
This prevents accidental mutation during runtime.

## MARKET_CONFIGS Registry

A module-level dict mapping product_type strings to MarketTypeConfig instances:

```
MARKET_CONFIGS = {
    "15m": MarketTypeConfig(...),
    "hourly": MarketTypeConfig(...),
    "spx_hourly": MarketTypeConfig(...),
    "weather": MarketTypeConfig(...),
    "sports": MarketTypeConfig(...),
}
```

## Lookup Function

`get_market_config(product_type)` returns the config for a given product type.
If `product_type` is None or unknown, it falls back to the "15m" config. This
ensures backward compatibility with code that predates the config system.

## Startup Validation

`validate_market_configs()` runs at bot startup. It imports bot.py and asserts
that every mirrored constant matches. Example assertions:

- `MARKET_CONFIGS["15m"].min_entry_price == bot.MIN_ENTRY_PRICE`
- `MARKET_CONFIGS["hourly"].observation_only == bot.HOURLY_OBSERVATION_ONLY`
- `MARKET_CONFIGS["spx_hourly"].kelly_fraction == bot.SPX_HOURLY_KELLY_FRACTION`

If any assertion fails, the bot crashes immediately with a clear error message
showing both values. This is intentional -- a silent mismatch is worse than
a crash.

## Origin: The MAX_SECONDS_BEFORE_CLOSE Incident

This system was created after a crash loop on March 1, 2026. The constant
MAX_SECONDS_BEFORE_CLOSE was changed in bot.py but not in market_config.py.
The startup validator caught the mismatch and crashed, but only after the
mismatch was deployed to VPS. The validator was added retroactively.

**Critical rule**: after ANY constant change in bot.py, grep the constant name
across ALL files -- especially market_config.py. The validator catches
mismatches at startup, but the goal is to never deploy one.

## Related

- [[concepts/per-asset-rules.md]] - Per-asset overrides that live in bot.py, not market_config.py
