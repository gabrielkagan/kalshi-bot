# Config Regime Report

Show the current configuration state and regime boundaries for all systems. Used to determine correct `--since` filters for analysis.

## Steps

1. **Extract all key config constants from bot.py** — grep for:
   ```
   OBSERVATION_MODE
   MIN_ENTRY_PRICE
   MAX_ENTRY_PRICE
   MAX_SECONDS_BEFORE_CLOSE
   STC_SHADOW_THRESHOLD
   MARKET_BLEND_W
   MIN_EDGE_BY_PRICE (just the dict)
   MAX_RISK_PER_TRADE
   XRP_MAX_RISK_PER_TRADE
   MAKER_ONLY_THRESHOLD
   HOURLY_OBSERVATION_ONLY
   HOURLY_MARKET_BLEND_W
   HOURLY_TEMPERATURE_T
   HOURLY_KELLY_FRACTION
   HOURLY_MIN_ENTRY_PRICE
   HOURLY_MAX_RISK_PER_TRADE
   HOURLY_CALIBRATION_ENABLED
   HOURLY_MIN_STC_ENTRY
   HOURLY_MAX_STC_ENTRY
   HOURLY_EXCLUDED_ASSETS
   HOURLY_MAX_POSITIONS_PER_WINDOW
   HOURLY_MAX_WINDOW_RISK
   SPX_HOURLY_OBSERVATION_ONLY
   SPX_HOURLY_MIN_ENTRY_PRICE
   SPX_HOURLY_MARKET_BLEND_W
   SPX_HOURLY_TEMPERATURE_T
   SPX_HOURLY_MAX_POSITIONS_PER_WINDOW
   SPX_HOURLY_MAX_WINDOW_RISK
   WEATHER_OBSERVATION_ONLY
   WEATHER_MARKET_BLEND_W
   WEATHER_MIN_EDGE_PCT
   WEATHER_MIN_ENTRY_PRICE
   ```

2. **Show per-system config tables**:

   **15M (LIVE)**
   | Config | Value |
   With current status: LIVE TRADING / OBSERVATION ONLY

   **Hourly Crypto (SHADOW)**
   | Config | Value |

   **SPX Hourly (SHADOW)**
   | Config | Value |

   **Weather (SHADOW)**
   | Config | Value |

   **Sports (SHADOW)** — check sports_engine.py for constants

3. **Determine regime boundaries** — check recent git log for config changes:
   ```
   git log --oneline --since="2 weeks ago" | head -20
   ```
   For each system, identify the most recent commit that changed its configs.
   Present as:

   | System | Regime Start | Commit | What Changed |
   |--------|-------------|--------|-------------|
   | 15M | ... | ... | ... |
   | Hourly | 2026-02-28T18:30:00 | acc4db1 | three-layer optimization |
   | SPX | 2026-03-02 | 2bad00f | per-window limits |
   | Weather | 2026-03-02T16:54:00 | 4a7ab9e | ensemble fix + instrumentation |
   | Sports | 2026-03-01 | 2d43606 | price ceiling + LR scale |

4. **Cross-check market_config.py** — verify all mirrored constants match:
   ```
   python3 -c "import market_config; market_config.validate_market_configs()"
   ```
   Report PASS or list mismatches.

5. **Output** — present in this order:
   - System status overview (1 line per system: mode, since, key metric)
   - Per-system config tables
   - Regime boundaries with git commits
   - market_config.py validation result
