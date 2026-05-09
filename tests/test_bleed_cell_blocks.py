"""TDD for bleed-cell blocks (R-bleed-1, 2026-04-30).

Two cell families bleeding heavily over the last 7d post-WS-fix:
  1. {BTC,ETH,XRP} × terminal_momentum_98 × 97-98¢ × 121-300s STC
     → -$28/-$154/-$47 over 7d → -$980/30d projected
  2. SOL × TAKER_NOW × 85-89¢ × 121-300s STC
     → -$182/7d → -$782/30d projected

Mirrors the prior-art `HIGH_PRICE_STC_BLOCK_*` pattern at bot/_impl.py:280-300:
  - env-flag controlled, default OFF
  - cell predicate + strategy-aware predicate
  - blocked candidates STILL get an evaluated_opportunity row written
    (filter_stage tag) so v2/v3 training data continues flowing
  - bleeder-string validator catches strategy-name drift at boot

Per CLAUDE.md: data collection for v2/v3 must NOT be impacted by these
blocks. The shadow-row pattern preserves market_result + raw_prob +
calibrated_prob + all v2/v3 cohort features for the cells we block.
"""
from pathlib import Path
import re
import sys

import pytest

REPO = Path(__file__).resolve().parents[1]


def _import_bot():
    if str(REPO) not in sys.path:
        sys.path.insert(0, str(REPO))
    import bot  # type: ignore
    return bot


# ---------------------------------------------------------------------------
# TM98 high-price bleed cell predicates
# ---------------------------------------------------------------------------

def test_tm98_bleed_constants_exist():
    """Module-level constants must be present and have sensible defaults."""
    bot = _import_bot()
    assert hasattr(bot, 'TM98_HIGHPRICE_BLEED_BLOCK_ENABLED')
    assert hasattr(bot, 'TM98_HIGHPRICE_BLEED_BLOCK_ASSETS')
    assert hasattr(bot, 'TM98_HIGHPRICE_BLEED_BLOCK_PRICE_LO')
    assert hasattr(bot, 'TM98_HIGHPRICE_BLEED_BLOCK_PRICE_HI')
    assert hasattr(bot, 'TM98_HIGHPRICE_BLEED_BLOCK_STC_LO_S')
    assert hasattr(bot, 'TM98_HIGHPRICE_BLEED_BLOCK_STC_HI_S')
    assert hasattr(bot, 'TM98_HIGHPRICE_BLEED_BLOCK_FILTER_STAGE')
    assert hasattr(bot, 'TM98_HIGHPRICE_BLEED_BLOCK_STRATEGIES')
    # Defaults match the data: BTC+ETH+XRP, 97-98¢, 121-300s
    assert bot.TM98_HIGHPRICE_BLEED_BLOCK_ASSETS == frozenset({'BTC', 'ETH', 'XRP'})
    assert bot.TM98_HIGHPRICE_BLEED_BLOCK_PRICE_LO == 97
    assert bot.TM98_HIGHPRICE_BLEED_BLOCK_PRICE_HI == 98
    assert bot.TM98_HIGHPRICE_BLEED_BLOCK_STC_LO_S == 121
    assert bot.TM98_HIGHPRICE_BLEED_BLOCK_STC_HI_S == 300
    assert 'terminal_momentum_98' in bot.TM98_HIGHPRICE_BLEED_BLOCK_STRATEGIES
    # Default disabled — must be flipped on VPS via env.
    assert bot.TM98_HIGHPRICE_BLEED_BLOCK_ENABLED is False


def test_tm98_bleed_predicate_returns_false_when_disabled():
    """Default-OFF: the predicate returns False even on a perfect-match cell."""
    bot = _import_bot()
    # Force enabled=False explicitly (the env-flag default).
    blocked = bot.should_block_tm98_highprice_bleed_candidate(
        asset='BTC', side='yes', entry_price_cents=98,
        seconds_to_close=200.0, strategy='terminal_momentum_98',
        enabled=False,
    )
    assert blocked is False


def test_tm98_bleed_predicate_blocks_in_cell():
    """Block fires for BTC/ETH/XRP × TM98 × 97-98¢ × 121-300s STC."""
    bot = _import_bot()
    for asset in ('BTC', 'ETH', 'XRP'):
        for price in (97, 98):
            for stc in (121.0, 200.0, 300.0):
                assert bot.should_block_tm98_highprice_bleed_candidate(
                    asset=asset, side='yes', entry_price_cents=price,
                    seconds_to_close=stc, strategy='terminal_momentum_98',
                    enabled=True,
                ) is True, (
                    f"Should block {asset}/{price}c/{stc}s/TM98"
                )


def test_tm98_bleed_predicate_does_not_block_outside_cell():
    """Tight cell: SOL excluded, 96¢/99¢ excluded, <121s/>300s excluded,
    non-TM98 strategy excluded, NO-side excluded."""
    bot = _import_bot()
    # SOL not in TM98 block (data showed SOL TM98 is small loss, not catastrophic)
    assert bot.should_block_tm98_highprice_bleed_candidate(
        asset='SOL', side='yes', entry_price_cents=98,
        seconds_to_close=200.0, strategy='terminal_momentum_98',
        enabled=True,
    ) is False
    # 96¢ excluded (HIGH_PRICE_STC_BLOCK already covers that for SOL/XRP)
    assert bot.should_block_tm98_highprice_bleed_candidate(
        asset='BTC', side='yes', entry_price_cents=96,
        seconds_to_close=200.0, strategy='terminal_momentum_98',
        enabled=True,
    ) is False
    # 99¢ excluded (TM-99 is profitable at 100% WR last 14d)
    assert bot.should_block_tm98_highprice_bleed_candidate(
        asset='BTC', side='yes', entry_price_cents=99,
        seconds_to_close=200.0, strategy='terminal_momentum_98',
        enabled=True,
    ) is False
    # STC < 121s excluded
    assert bot.should_block_tm98_highprice_bleed_candidate(
        asset='BTC', side='yes', entry_price_cents=98,
        seconds_to_close=120.0, strategy='terminal_momentum_98',
        enabled=True,
    ) is False
    # STC > 300s excluded
    assert bot.should_block_tm98_highprice_bleed_candidate(
        asset='BTC', side='yes', entry_price_cents=98,
        seconds_to_close=301.0, strategy='terminal_momentum_98',
        enabled=True,
    ) is False
    # Non-TM98 strategy excluded
    assert bot.should_block_tm98_highprice_bleed_candidate(
        asset='BTC', side='yes', entry_price_cents=98,
        seconds_to_close=200.0, strategy='terminal_momentum_99',
        enabled=True,
    ) is False
    # NO-side excluded (NO/YES asymmetry — TM98 is YES-side per scan flow)
    assert bot.should_block_tm98_highprice_bleed_candidate(
        asset='BTC', side='no', entry_price_cents=98,
        seconds_to_close=200.0, strategy='terminal_momentum_98',
        enabled=True,
    ) is False


def test_tm98_bleed_predicate_handles_none_inputs():
    """Defensive: None inputs (missing scan fields) → don't block."""
    bot = _import_bot()
    for kwargs in (
        dict(asset=None, side='yes', entry_price_cents=98, seconds_to_close=200.0,
             strategy='terminal_momentum_98', enabled=True),
        dict(asset='BTC', side='yes', entry_price_cents=None, seconds_to_close=200.0,
             strategy='terminal_momentum_98', enabled=True),
        dict(asset='BTC', side='yes', entry_price_cents=98, seconds_to_close=None,
             strategy='terminal_momentum_98', enabled=True),
        dict(asset='BTC', side='yes', entry_price_cents=98, seconds_to_close=200.0,
             strategy=None, enabled=True),
    ):
        assert bot.should_block_tm98_highprice_bleed_candidate(**kwargs) is False, (
            f"None input should not block: {kwargs}"
        )


# ---------------------------------------------------------------------------
# SOL TAKER low-price bleed cell predicates
# ---------------------------------------------------------------------------

def test_sol_taker_bleed_constants_exist():
    bot = _import_bot()
    assert hasattr(bot, 'SOL_TAKER_LOWPRICE_BLEED_BLOCK_ENABLED')
    assert bot.SOL_TAKER_LOWPRICE_BLEED_BLOCK_ASSETS == frozenset({'SOL'})
    assert bot.SOL_TAKER_LOWPRICE_BLEED_BLOCK_PRICE_LO == 85
    assert bot.SOL_TAKER_LOWPRICE_BLEED_BLOCK_PRICE_HI == 89
    assert bot.SOL_TAKER_LOWPRICE_BLEED_BLOCK_STC_LO_S == 121
    assert bot.SOL_TAKER_LOWPRICE_BLEED_BLOCK_STC_HI_S == 300
    assert 'TAKER_NOW' in bot.SOL_TAKER_LOWPRICE_BLEED_BLOCK_STRATEGIES
    assert bot.SOL_TAKER_LOWPRICE_BLEED_BLOCK_ENABLED is False


def test_sol_taker_bleed_predicate_blocks_in_cell():
    """Block fires for SOL × TAKER_NOW × 85-89¢ × 121-300s STC."""
    bot = _import_bot()
    for price in (85, 86, 87, 88, 89):
        for stc in (121.0, 200.0, 300.0):
            assert bot.should_block_sol_taker_lowprice_bleed_candidate(
                asset='SOL', side='yes', entry_price_cents=price,
                seconds_to_close=stc, strategy='TAKER_NOW',
                enabled=True,
            ) is True, f"Should block SOL/{price}c/{stc}s/TAKER_NOW"


def test_sol_taker_bleed_predicate_does_not_block_outside_cell():
    bot = _import_bot()
    # Wrong asset
    assert bot.should_block_sol_taker_lowprice_bleed_candidate(
        asset='BTC', side='yes', entry_price_cents=87,
        seconds_to_close=200.0, strategy='TAKER_NOW', enabled=True,
    ) is False
    # 84¢ excluded (below cell)
    assert bot.should_block_sol_taker_lowprice_bleed_candidate(
        asset='SOL', side='yes', entry_price_cents=84,
        seconds_to_close=200.0, strategy='TAKER_NOW', enabled=True,
    ) is False
    # 90¢ excluded (above cell — SOL @ 90+ is profitable per data)
    assert bot.should_block_sol_taker_lowprice_bleed_candidate(
        asset='SOL', side='yes', entry_price_cents=90,
        seconds_to_close=200.0, strategy='TAKER_NOW', enabled=True,
    ) is False
    # MAKER strategy excluded (different cohort)
    assert bot.should_block_sol_taker_lowprice_bleed_candidate(
        asset='SOL', side='yes', entry_price_cents=87,
        seconds_to_close=200.0, strategy='MAKER_PATIENT', enabled=True,
    ) is False


# ---------------------------------------------------------------------------
# AST: scan() must call both predicates AND insert an evaluated_opportunity
# row when blocking (preserving v2/v3 training data).
# ---------------------------------------------------------------------------

def test_bot_py_scan_calls_tm98_bleed_predicate():
    """The block must be wired in scan(); otherwise the env flag is dead."""
    bot_py = REPO / 'bot/_impl.py'
    src = bot_py.read_text()
    assert 'should_block_tm98_highprice_bleed_candidate' in src, (
        "scan() must invoke should_block_tm98_highprice_bleed_candidate"
    )


def test_bot_py_scan_calls_sol_taker_bleed_predicate():
    bot_py = REPO / 'bot/_impl.py'
    src = bot_py.read_text()
    assert 'should_block_sol_taker_lowprice_bleed_candidate' in src


def test_blocked_candidates_get_evaluated_opportunity_row():
    """CRITICAL for v2/v3 data flow: when the block drops a candidate,
    the source must insert_evaluated_opportunity for that row with the
    correct filter_stage tag. Otherwise we lose:
      - market_result on settlement
      - cal_mlp_p_mean annotation
      - the v2/v3 cohort features for the would-be trade

    AST-style: search for both filter_stage strings in proximity to
    insert_evaluated_opportunity."""
    bot_py = REPO / 'bot/_impl.py'
    src = bot_py.read_text()
    # The TM98 filter_stage tag must appear in an insert_evaluated_opportunity
    # call. Search for the pattern: block iter writes a shadow row.
    assert 'TM98_HIGHPRICE_BLEED_BLOCK_FILTER_STAGE' in src, (
        "scan() must write blocked-trade rows with TM98 filter_stage"
    )
    assert 'SOL_TAKER_LOWPRICE_BLEED_BLOCK_FILTER_STAGE' in src, (
        "scan() must write blocked-trade rows with SOL_TAKER filter_stage"
    )
    # Both must be in proximity to insert_evaluated_opportunity calls.
    # Heuristic: the filter_stage string must appear within an 80-line
    # window of an insert_evaluated_opportunity call. The block body is
    # ~75 lines because it gathers all the v2/v3 training feature columns.
    for stage_const in ('TM98_HIGHPRICE_BLEED_BLOCK_FILTER_STAGE',
                         'SOL_TAKER_LOWPRICE_BLEED_BLOCK_FILTER_STAGE'):
        constant_lines = [
            i for i, line in enumerate(src.splitlines())
            if stage_const in line and 'BLOCK_FILTER_STAGE = ' not in line
        ]
        assert constant_lines, (
            f"{stage_const} declared but never used — block is dead code"
        )
        # R9-H1 added uses of these constants in the CalEngine `_stages`
        # tuple (which is unrelated to insert_evaluated_opportunity).
        # Only require AT LEAST ONE usage to be near an insert call.
        any_in_proximity = False
        for line_num in constant_lines:
            context = '\n'.join(src.splitlines()[max(0, line_num - 5):line_num + 80])
            if 'insert_evaluated_opportunity' in context:
                any_in_proximity = True
                break
        assert any_in_proximity, (
            f"{stage_const} must appear in proximity to at least one "
            f"insert_evaluated_opportunity call (so v2/v3 training data is "
            f"preserved when the gate fires). All usages were far from any "
            f"insert call."
        )


# ---------------------------------------------------------------------------
# Bleeder-string drift validator (Bit 3.0.5: shared registry-membership helper
# `_validate_bleeders_against_runtime_registry` post the STRATEGY_PANIC_CAPTURE
# constants block in bot/_impl.py). Catches strategy-name renames that would
# silently no-op the gate.
# ---------------------------------------------------------------------------

def test_bot_py_tm_strategy_fstring_format_matches_block_strategies():
    """R-bleed-1 R1-H2: the runtime TM strategy is built via
    `f"terminal_momentum_{best_ask}"` at scan-time. If that f-string
    template is renamed (e.g., to `f"tm_{...}"`), the strategy values
    produced at runtime no longer match TM98_HIGHPRICE_BLEED_BLOCK_STRATEGIES
    and the gate silently no-ops, even though literal "terminal_momentum_98"
    still appears in source (in BLOCK_STRATEGIES, comments, etc).

    Lock the f-string format as a source invariant — if the rename happens,
    this test fails loudly."""
    bot_py = REPO / 'bot/_impl.py'
    src = bot_py.read_text()
    # The f-string template must appear (single OR double quote)
    has_template = (
        'f"terminal_momentum_{' in src
        or "f'terminal_momentum_{" in src
    )
    assert has_template, (
        "scan() must generate TM strategies via f\"terminal_momentum_{...}\". "
        "Rename detected — also update TM98_HIGHPRICE_BLEED_BLOCK_STRATEGIES "
        "AND this test, otherwise the gate silently no-ops."
    )


def test_taker_now_constant_value_matches_block_strategies():
    """R-bleed-1 R1-H2 + R3-H3: assert at RUNTIME that
    `bot.STRATEGY_TAKER_NOW == "TAKER_NOW"`. The earlier source-grep
    was false-positive on indirect declarations like
    `STRATEGY_TAKER_NOW = _TN` where `_TN = "TAKER_NOW"`.

    Runtime check catches every rename / value change / refactor without
    false positives — and proves the value matches what
    SOL_TAKER_LOWPRICE_BLEED_BLOCK_STRATEGIES expects."""
    bot = _import_bot()
    assert bot.STRATEGY_TAKER_NOW in bot.SOL_TAKER_LOWPRICE_BLEED_BLOCK_STRATEGIES, (
        f"STRATEGY_TAKER_NOW={bot.STRATEGY_TAKER_NOW!r} must be in "
        f"SOL_TAKER_LOWPRICE_BLEED_BLOCK_STRATEGIES="
        f"{bot.SOL_TAKER_LOWPRICE_BLEED_BLOCK_STRATEGIES!r}. If renamed, "
        f"update the BLOCK_STRATEGIES frozenset to match the new value."
    )


def test_cross_exchange_consensus_min_when_binance_disabled_default():
    """R-bleed-1 R3-LOW9 regression test (False branch — code-default
    state, NOT shell-env-default). R6-MED1: explicitly clear
    BINANCE_FEED_ENABLED in subprocess env so an engineer who has the
    var set in their .env doesn't see this test fail.
    """
    import subprocess
    import os
    script = (
        "import os; os.environ.pop('BINANCE_FEED_ENABLED', None); "
        "import sys; sys.path.insert(0, %r); "
        "import bot; "
        "assert bot.BINANCE_FEED_ENABLED is False, "
        "    f'BINANCE_FEED_ENABLED={bot.BINANCE_FEED_ENABLED}'; "
        "assert bot._CROSS_EXCHANGE_FEEDS_ACTIVE == 2, "
        "    f'_CROSS_EXCHANGE_FEEDS_ACTIVE={bot._CROSS_EXCHANGE_FEEDS_ACTIVE}'; "
        "assert bot.CROSS_EXCHANGE_CONSENSUS_MIN == 2, "
        "    f'CROSS_EXCHANGE_CONSENSUS_MIN={bot.CROSS_EXCHANGE_CONSENSUS_MIN}'; "
        "print('OK')"
    ) % str(REPO)
    # Pass a clean env so child does not inherit BINANCE_FEED_ENABLED.
    env = {k: v for k, v in os.environ.items() if k != 'BINANCE_FEED_ENABLED'}
    res = subprocess.run(
        [sys.executable, '-c', script],
        capture_output=True, text=True, timeout=60, env=env,
    )
    assert res.returncode == 0, (
        f"Subprocess assertion failed.\nstdout={res.stdout}\nstderr={res.stderr}"
    )
    assert 'OK' in res.stdout


def test_cross_exchange_consensus_min_derivation_when_binance_enabled():
    """R-bleed-1 R4-M1 regression test (True branch): exercise the
    derivation when BINANCE_FEED_ENABLED=1.

    R5-CRITICAL: importlib.reload(bot) poisons 17+ other test files that
    do `from bot import X` — those keep stale pre-reload references while
    the patched module sits in sys.modules. Confirmed by full-suite run:
    4 tests in test_execution.py fail when this test runs first.

    Fix: spawn a fresh subprocess. Isolated module state, no pollution.
    """
    import subprocess
    script = (
        "import os; os.environ['BINANCE_FEED_ENABLED']='1'; "
        "import sys; sys.path.insert(0, %r); "
        "import bot; "
        "assert bot.BINANCE_FEED_ENABLED is True, "
        "    f'BINANCE_FEED_ENABLED={bot.BINANCE_FEED_ENABLED}'; "
        "assert bot._CROSS_EXCHANGE_FEEDS_ACTIVE == 3, "
        "    f'_CROSS_EXCHANGE_FEEDS_ACTIVE={bot._CROSS_EXCHANGE_FEEDS_ACTIVE}'; "
        "assert bot.CROSS_EXCHANGE_CONSENSUS_MIN == 3, "
        "    f'CROSS_EXCHANGE_CONSENSUS_MIN={bot.CROSS_EXCHANGE_CONSENSUS_MIN}'; "
        "print('OK')"
    ) % str(REPO)
    res = subprocess.run(
        [sys.executable, '-c', script],
        capture_output=True, text=True, timeout=60,
    )
    assert res.returncode == 0, (
        f"Subprocess assertion failed.\nstdout={res.stdout}\nstderr={res.stderr}"
    )
    assert 'OK' in res.stdout


def test_bot_py_calengine_accepts_bleed_block_stages():
    """R-bleed-1 R9-H1: bleed-cell rows are written under cell-tag
    filter_stage values, not 'candidate'. Per-asset 15M CalEngines
    use a hardcoded `accepted_stages` tuple — without including the
    cell tags, the engines silently stop receiving observations from
    the cells we just gated.

    AST source-grep: bot/_impl.py 15M CalEngine `_stages` tuple must
    reference all 3 BLOCK_FILTER_STAGE constants."""
    bot_py = REPO / 'bot/_impl.py'
    src = bot_py.read_text()
    # Locate the 15M CalEngine `_stages` declaration.
    import re
    block_match = re.search(
        r'_stages\s*=\s*\(\s*\(\s*"candidate"[\s\S]+?\)\s*if\s*_pt\s*==\s*"15m"',
        src,
    )
    if not block_match:
        # Fall back to single-line form
        block_match = re.search(
            r'_stages\s*=\s*\([^)]*"candidate"[^)]*\)\s*if\s*_pt\s*==\s*"15m"',
            src,
        )
    assert block_match, "15M CalEngine _stages declaration not found in bot/_impl.py"
    stages_block = block_match.group(0)
    assert 'HIGH_PRICE_STC_BLOCK_FILTER_STAGE' in stages_block, (
        "CalEngine 15M _stages must include HIGH_PRICE_STC_BLOCK_FILTER_STAGE — "
        "otherwise HPSB-blocked rows are excluded from per-asset CalEngine training. "
        "See R-bleed-1 R9-H1."
    )
    assert 'TM98_HIGHPRICE_BLEED_BLOCK_FILTER_STAGE' in stages_block, (
        "CalEngine 15M _stages must include TM98_HIGHPRICE_BLEED_BLOCK_FILTER_STAGE — "
        "blocked TM-98 rows would be excluded from training otherwise."
    )
    assert 'SOL_TAKER_LOWPRICE_BLEED_BLOCK_FILTER_STAGE' in stages_block, (
        "CalEngine 15M _stages must include SOL_TAKER_LOWPRICE_BLEED_BLOCK_FILTER_STAGE."
    )


def test_filter_stage_value_consistency_across_files():
    """R-bleed-1 R10-MED1 + R11: scope is THREE lockstep-required sites
    only (the ones where bleed-cell rows MUST be included for downstream
    correctness). The remaining 9 audit/dashboard scripts in the kb
    decision doc are intentionally NOT covered — those are documented
    as "post-activation deflation acceptable, operator manually adapts
    rollup queries". See `kb/decisions/bleed-cell-blocks-2026-04-30.md`
    "Audit deflation warning" for the full 12-file list and the
    correctness vs. cutover distinction.

    Lockstep-required (this test enforces — runtime correctness depends
    on these matching bot/_impl.py constants):
      - fifteenm_shadow.py (per-asset temperature recalibration training set)
      - scripts/backtest.py (expansion-signal universe for counterfactual)
      - scripts/generate_whitepaper_stats.py (Brier/calibration sample)

    Manual-cutover (kb decision doc warns operators; not enforced):
      - scripts/{15m_live_audit, 15m_alpha_research, alpha_audit,
        data_health_monitor, maker_opportunity_cost, quiet_market_monitor}.py
      - dashboard_snapshot.py, analyst.py, auditor.py, researcher.py
      - .claude/skills/status/SKILL.md

    If anyone renames a constant's value, bot/_impl.py keeps working (uses
    constant) but the 3 lockstep sites would silently break — they'd
    still look for the OLD value while bot/_impl.py writes rows under the
    new value. Comments/docstrings containing the value strings count
    as matches (acceptable: a comment documenting why the value is
    referenced still proves the file authors knew about the dependency).
    """
    bot = _import_bot()
    constants = {
        'HIGH_PRICE_STC_BLOCK_FILTER_STAGE': bot.HIGH_PRICE_STC_BLOCK_FILTER_STAGE,
        'TM98_HIGHPRICE_BLEED_BLOCK_FILTER_STAGE': bot.TM98_HIGHPRICE_BLEED_BLOCK_FILTER_STAGE,
        'SOL_TAKER_LOWPRICE_BLEED_BLOCK_FILTER_STAGE': bot.SOL_TAKER_LOWPRICE_BLEED_BLOCK_FILTER_STAGE,
    }
    target_files = (
        REPO / 'fifteenm_shadow.py',
        REPO / 'scripts' / 'backtest.py',
        REPO / 'scripts' / 'generate_whitepaper_stats.py',
    )
    for fpath in target_files:
        if not fpath.exists():
            continue
        src = fpath.read_text()
        for const_name, const_value in constants.items():
            assert const_value in src, (
                f"{fpath.relative_to(REPO)} must contain string literal "
                f"{const_value!r} (value of bot.{const_name}). "
                f"If the constant value changed, this site needs updating "
                f"in lockstep — otherwise the file silently filters on the "
                f"old value while bot/_impl.py writes rows under the new value."
            )


def test_fifteenm_shadow_temperature_query_includes_bleed_stages():
    """R-bleed-1 R9-H2: fifteenm_shadow.py temperature recalibration
    uses a hardcoded filter_stage IN list. Without the bleed tags,
    per-asset T grid-search post-activation loses the high-signal
    bleed-cell predictions (where calibrator overconfidence shows up
    most clearly).
    """
    shadow_path = REPO / 'fifteenm_shadow.py'
    src = shadow_path.read_text()
    # Each bleed filter_stage VALUE (string literal) must appear in the source.
    for stage_value in (
        '96C_SOL_XRP_STC_DANGER_BAND',
        'TM98_97_98C_2_5MIN_BLEED',
        'SOL_TAKER_85_89C_2_5MIN_BLEED',
    ):
        assert stage_value in src, (
            f"fifteenm_shadow.py temperature query must include "
            f"'{stage_value}' filter_stage — otherwise the per-asset T "
            f"grid-search loses bleed-cell observations post-activation."
        )


def test_bleed_block_strategies_have_runtime_validator():
    """If 'terminal_momentum_98' is renamed in scan() without updating the
    BLEED_BLOCK_STRATEGIES frozenset, the gate silently no-ops. Bit 3.0.5:
    boot-time validator delegates to the shared registry-membership helper
    `_validate_bleeders_against_runtime_registry` (see bot/_impl.py post the
    `STRATEGY_PANIC_CAPTURE` constants block). Full invariant coverage in
    tests/test_strategy_drift.py.
    """
    bot_py = REPO / 'bot/_impl.py'
    src = bot_py.read_text()
    # A validator function must exist for the new gates.
    assert '_validate_bleed_block_bleeder_strings' in src or \
           '_validate_tm98_bleed_block' in src or \
           '_validate_bleed_cell_strategies' in src, (
        "Need a startup validator that asserts bleed-block strategy "
        "strings reach scan() as candidate.strategy values (catches rename drift). "
        "Bit 3.0.5: shared helper `_validate_bleeders_against_runtime_registry`."
    )


def test_bleed_block_validator_callable():
    """Re-invoke `_validate_bleed_block_bleeder_strings()` at runtime,
    separate from the boot-time binding `_BLEED_BLOCK_MISSING_BLEEDERS`.
    Symmetric to HPSB's `test_validator_callable` in
    tests/test_high_price_stc_band_gate.py — catches a regression that
    breaks the wrapper after module load (e.g., monkey-patching, import-
    order issues, future refactor that swallows exceptions silently)."""
    bot = _import_bot()
    result = bot._validate_bleed_block_bleeder_strings()
    assert result == [], result


# ---------------------------------------------------------------------------
# Binance feed disable
# ---------------------------------------------------------------------------

def test_binance_feed_has_enable_flag():
    """BinanceFeed should be gated by env flag; default OFF on US-VPS
    deploys (HTTP 451 geoblock). Currently spamming reconnect every ~70s.

    Bit 4.5a (2026-05-08): CrossExchangeFeed moved to bot/feeds/cross_exchange.py.
    The flag is consumed there now, not in bot/_impl.py.
    """
    cross_py = REPO / 'bot/feeds/cross_exchange.py'
    src = cross_py.read_text()
    assert 'BINANCE_FEED_ENABLED' in src, (
        "Add BINANCE_FEED_ENABLED env flag (default OFF) so VPS "
        "doesn't reconnect-loop a geoblocked endpoint."
    )


def test_binance_feed_default_disabled():
    """Default OFF — the VPS is geoblocked from Binance.com WebSocket.
    R6-MED1: subprocess + clean env so a developer with BINANCE_FEED_ENABLED=1
    set in their shell doesn't break this test.
    """
    import subprocess
    import os
    script = (
        "import os; os.environ.pop('BINANCE_FEED_ENABLED', None); "
        "import sys; sys.path.insert(0, %r); "
        "import bot; "
        "assert hasattr(bot, 'BINANCE_FEED_ENABLED'); "
        "assert bot.BINANCE_FEED_ENABLED is False, "
        "    f'BINANCE_FEED_ENABLED={bot.BINANCE_FEED_ENABLED}'; "
        "print('OK')"
    ) % str(REPO)
    env = {k: v for k, v in os.environ.items() if k != 'BINANCE_FEED_ENABLED'}
    res = subprocess.run(
        [sys.executable, '-c', script],
        capture_output=True, text=True, timeout=60, env=env,
    )
    assert res.returncode == 0, (
        f"Subprocess assertion failed.\nstdout={res.stdout}\nstderr={res.stderr}"
    )
    assert 'OK' in res.stdout


def test_binance_feed_start_respects_flag():
    """The feed's start() (or the call site that invokes it) must check
    BINANCE_FEED_ENABLED before launching the asyncio task. Otherwise
    the flag is dead code.

    Bit 4.5a (2026-05-08): CrossExchangeFeed (which hosts the Binance
    asyncio task) moved to bot/feeds/cross_exchange.py — the flag check
    lives there now.
    """
    cross_py = REPO / 'bot/feeds/cross_exchange.py'
    src = cross_py.read_text()
    # The check should appear near either:
    #   - BinanceFeed instantiation (legacy name), OR
    #   - the binance task addition / call site
    has_gating = bool(re.search(
        r'BINANCE_FEED_ENABLED.*?(BinanceFeed|binance|_binance|\.start\(\))',
        src, re.DOTALL,
    )) or bool(re.search(
        r'(BinanceFeed|binance|_binance).*?BINANCE_FEED_ENABLED',
        src, re.DOTALL,
    ))
    assert has_gating, (
        "BINANCE_FEED_ENABLED must gate the Binance asyncio task in "
        "bot/feeds/cross_exchange.py."
    )
