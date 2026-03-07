# Kalshi Bot — Testing Strategy

**Goal:** Proactively catch the bug classes that have historically caused production incidents, before they reach the VPS. Every test category below maps directly to a real past failure or a known risk.

---

## Current State

We have 9 standalone test files (~6,700 lines) covering EGARCH, HAR, ghost fills, adaptive jump/RK, buffer persistence, dashboard contracts, and a small regression suite. Tests run locally via pytest and in CI — but **CI doesn't gate deploys**. The deploy pipeline only does `ast.parse` (syntax check) and post-deploy log scanning.

**What's missing:** config consistency tests, DB schema/signature alignment tests, cross-file wiring tests, pipeline integration tests, and pre-deploy CI gating.

---

## Test Categories (Priority Order)

### 1. Config & Wiring Consistency Tests (`test_config_consistency.py`)

**Failure mode:** MarketTypeConfig drifts from bot.py constants → crash loop on VPS startup.
**Past incident:** Multiple — any time a constant was changed in bot.py but not market_config.py.

**Tests to write:**

- **Config-to-constant parity**: Import bot.py and market_config.py, assert every field in every `MarketTypeConfig` matches its corresponding bot.py constant. This duplicates what `validate_market_configs()` does at runtime, but catches it *before deploy* in CI.
- **Config completeness**: Every product type in `MARKET_CONFIGS` has all required fields (no `None` where a number is expected).
- **MIN_EDGE_BY_PRICE monotonicity**: Assert the edge schedule is non-decreasing as price increases. A typo inverting two values silently weakens the edge filter.
- **Observation mode flags**: Assert that observation-only product types have `observation_only=True` in their config and vice versa. Catches the scenario where you flip a bot.py constant but forget market_config.py.

**Why this is #1:** Config mismatches cause immediate crash loops that block the entire bot. Highest blast radius, easiest to test.

---

### 2. DB Signature Alignment Tests (`test_db_signatures.py`)

**Failure mode:** New key added to `_shadow_diag` without updating `insert_rejection()` / `insert_evaluated_opportunity()` signatures → runtime crash on first trade attempt.
**Past incident:** Documented in CLAUDE.md as a critical rule.

**Tests to write:**

- **`_shadow_diag` key coverage**: Parse the `_shadow_diag = {…}` dict literal in `scan()`, extract all keys. Then inspect the signatures of `insert_rejection()` and `insert_evaluated_opportunity()` — every `_shadow_diag` key must appear as a parameter in both functions. This is what the runtime assertion at line ~6363 does, but testing it statically in CI means you catch it before the first live trade fails.
- **SQL column alignment**: Parse the `CREATE TABLE` statements for `rejected_opportunities` and `evaluated_opportunities`. Every column (minus `id`, `timestamp`) should have a corresponding parameter in the insert function. Catches "added column to SQL but forgot the function param" bugs.
- **`busy_timeout` enforcement**: `grep` every `sqlite3.connect()` call across all `.py` files. Assert each one includes `PRAGMA busy_timeout` within the next 5 lines. Catches the sports_engine.py "database is locked" class of bug.

**Why this is #2:** Silent at first, then crashes on first real trade — the worst time to discover a bug.

---

### 3. Cross-File Call-Site Integrity Tests (`test_call_sites.py`)

**Failure mode:** Function signature changed in bot.py but callers in other files (analyst.py, dashboard_snapshot.py, spx_engine.py, etc.) still use old signature → `TypeError` at runtime.
**Past incident:** CLAUDE.md rule about grepping all call sites after signature changes.

**Tests to write:**

- **Public API smoke imports**: Import every module that imports from bot.py. Verify no `ImportError` or `AttributeError`. This catches renamed/removed functions.
- **Cross-module function call arity**: For key functions (`insert_rejection`, `insert_evaluated_opportunity`, `calculate_fee`, `calculate_maker_fee`, `get_market_config`), find all call sites across all `.py` files via AST parsing. Verify each call passes the correct number of positional args and only uses valid keyword arg names. This is the static version of "grep ALL call sites."
- **CalEngine pipeline triple-ship**: When any engine file (spx_engine.py, weather_engine.py, sports_engine.py) has an `INSERT` that includes `raw_prob`, verify that (a) the corresponding CalEngine is routed in `_resolve_cal_engine`, and (b) the audit script checks for that engine's observations. Catches the "three things must ship together" rule.

**Why this is #3:** These are the bugs that pass `ast.parse` but blow up at runtime in a specific code path.

---

### 4. Pipeline Integration Tests (`test_pipeline.py`)

**Failure mode:** Individual components work in isolation but the end-to-end pipeline produces wrong output — e.g., calibration contamination (hourly data in 15M training), dead observation gates, wrong product_type routing.
**Past incidents:** Hourly data contaminating CalibrationEngine training (35.5% of data). Dead STC shadow gate (checked `is None` but value was `'15m'`).

**Tests to write:**

- **Calibration data isolation**: Create a mock StateManager with both 15M and hourly settled trades. Train a CalibrationEngine. Assert the training set contains zero hourly trades. Catches the contamination bug.
- **Product type routing**: For each product type string (`"15m"`, `"hourly"`, `"spx_hourly"`, `"weather"`, `"sports"`), simulate a window dict with that product_type. Run it through the observation gate logic, STC shadow gate, and config lookup. Assert each gate behaves correctly. Catches the dead-STC-gate class of bug where a comparison assumes `None` but gets a string.
- **Probability pipeline end-to-end**: Feed known inputs (spot price, strike, volatility, time-to-close) through the full chain: raw stat prob → CalibrationEngine → temperature scaling → dynamic cap → market blend → fee-adjusted edge. Assert the output matches a hand-calculated expected value. Catches silent regressions in any pipeline stage.
- **Observation mode enforcement**: For every product type with `observation_only=True`, simulate a trade opportunity that passes all filters. Assert the bot logs it as an observation but does NOT submit an order. Catches "observation gate bypassed" bugs.

**Why this is #4:** These are the subtle bugs that don't crash the bot — they just lose money silently.

---

### 5. Property-Based / Invariant Tests (`test_invariants.py`)

**Failure mode:** Edge cases in math/logic that fixed test vectors don't cover.

**Tests to write:**

- **Fee formula properties**: For any `(count, price)` where `count > 0` and `0 < price < 100`: taker fee ≥ 0, maker fee == 0, taker fee ≤ count (can't pay more in fees than contracts). Use `hypothesis` library to generate random inputs.
- **Kelly sizing bounds**: For any valid inputs, position size is in `[0, max_risk_per_trade * bankroll]`. Never negative, never exceeds risk limit.
- **Probability bounds**: After every pipeline stage (calibration, temperature, cap, blend), output is in `(0, 1)` exclusive. Catches overflow/underflow.
- **Edge monotonicity**: Higher model probability at a given price → higher or equal edge. Edge should never decrease when we become more confident.
- **STC gate consistency**: For every `product_type`, the `(min_seconds_before_close, max_seconds_before_close)` range is valid (min < max, both > 0). `STC_SHADOW_THRESHOLD` for 15M falls within the STC range.

---

### 6. Snapshot / Contract Tests (`test_contracts.py`)

**Failure mode:** Dashboard, Supabase sync, or analyst.py expect a specific data shape that bot.py silently changes.

**Tests to write:**

- **Dashboard state schema**: `dashboard_snapshot.py` builds state dicts. Assert the output dict has all keys that the Supabase `dashboard_state` table expects. Catches "added field to dashboard but forgot Supabase column" bugs.
- **Telegram alert format**: `analyst.py` formats alerts. Assert alert strings don't exceed Telegram's 4096 char limit and contain required fields (asset, PnL, trade ID).
- **JSONL schema stability**: `opportunity_journal.jsonl`, `scan_journal.jsonl`, and `fill_model_journal.jsonl` each have an expected schema. Assert a sample record from each has the expected keys. Catches "renamed a field but broke downstream analysis scripts."

---

### 7. Regression Test Expansion (`tests/test_regression.py`)

**Current state:** Small file with fee calculation and a few other tests.

**Expand with a test for every CLAUDE.md "Learned" annotation:**

- **MarketTypeConfig mismatch** (crash loop): Change one bot.py constant, call `validate_market_configs()`, assert it raises.
- **Dead STC shadow gate** (98c954d, Mar 1): Create a window with `product_type='15m'`, assert the STC shadow gate still triggers correctly (not silently bypassed).
- **NULL raw_prob** (Mar 4): Assert that every engine's INSERT statement includes `raw_prob` as a non-NULL column.
- **Missing busy_timeout** (Mar 2): Assert every `sqlite3.connect()` sets busy_timeout.
- **Hourly contamination**: Assert CalibrationEngine excludes hourly from training.

Each test should include a comment with the commit hash and date of the original incident.

---

## Infrastructure Changes

### A. CI Gating (`.github/workflows/test.yml`)

Create a new workflow that runs **before** deploy:

```yaml
name: Tests
on: [push, pull_request]
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: '3.11'
      - name: Install dependencies
        run: |
          pip install pytest hypothesis
          pip install -r requirements.txt
      - name: Syntax check
        run: python3 -c "import ast; ast.parse(open('bot.py').read())"
      - name: Run tests
        run: pytest tests/ test_*.py -v --tb=short -x
```

Update `deploy.yml` to depend on the test workflow passing. This is the single highest-leverage change — it turns every test into a deploy gate.

### B. `conftest.py` (Shared Fixtures)

Create `conftest.py` at project root with:

- **`mock_state_db`**: In-memory SQLite with all tables created, busy_timeout set. Reusable across all tests that need DB access.
- **`sample_window_15m` / `sample_window_hourly` / etc.**: Dict fixtures representing a window for each product type with realistic field values.
- **`mock_kalshi_client`**: Patched KalshiClient that returns canned orderbook/fill responses.
- **`sample_price_buffer`**: Pre-filled price array for volatility model tests.

This eliminates the "inline copy of bot.py classes" pattern that causes tests to drift from reality.

### C. `pytest.ini`

```ini
[pytest]
testpaths = tests .
python_files = test_*.py
python_classes = Test*
python_functions = test_*
markers =
    slow: marks tests as slow (deselect with '-m "not slow"')
    integration: marks integration tests
    smoke: marks quick smoke tests for CI
addopts = -v --tb=short
```

### D. Pre-Commit Hook (Optional but Recommended)

A lightweight git pre-commit hook that runs the fast subset:

```bash
#!/bin/bash
pytest -m "not slow and not integration" -x -q
```

This catches config mismatches and signature bugs before they even get committed.

---

## Implementation Priority

| Phase | What | Effort | Impact |
|-------|------|--------|--------|
| **1** | CI gating + `conftest.py` + `pytest.ini` | 1 day | Turns all existing tests into deploy gates |
| **2** | Config consistency tests | 0.5 day | Prevents crash loops (highest blast radius) |
| **3** | DB signature alignment tests | 0.5 day | Prevents first-trade crashes |
| **4** | Cross-file call-site tests | 1 day | Prevents runtime TypeErrors |
| **5** | Pipeline integration tests | 1-2 days | Prevents silent money-losing bugs |
| **6** | Property-based tests (hypothesis) | 1 day | Catches edge cases in math |
| **7** | Contract tests + regression expansion | 0.5 day | Prevents downstream breakage |

**Total: ~5-6 days of work for comprehensive coverage of all known failure modes.**

---

## What This Doesn't Cover (and Why)

- **Live market behavior testing**: Can't unit-test whether the bot makes profitable trades. That's what observation mode and shadow features are for.
- **Network failure resilience**: WebSocket disconnects, API timeouts — these need chaos testing or manual testing, not unit tests.
- **Performance/load testing**: 330MB/day of scan_journal.jsonl, DB contention under load — would need a separate load testing setup.

These are real risks but require different approaches (chaos engineering, load testing, canary deploys). The strategy above focuses on the bugs that *can* be caught statically before deploy.

---

## Success Criteria

After implementing this strategy, the following should be true:

1. **No push to main without tests passing** — CI gates all deploys
2. **Every CLAUDE.md "Learned" bug has a regression test** — history doesn't repeat
3. **Config changes are validated statically** — no more crash loops from constant drift
4. **DB schema changes are validated statically** — no more first-trade crashes from missing columns
5. **Cross-file signature changes are caught** — no more "works on my machine" TypeErrors
6. **Pipeline integration is verified** — no more silent calibration contamination
