"""R-p7-deploy-r10: cal_mlp conformal-lower-bound gate for TM-96.

Background: ETH terminal_momentum_96 trade on 2026-04-29 lost -$140 because
TM-96 fires on the TM signal regardless of edge. The candidate had:
- raw_prob 0.9307 → edge -0.0293 (negative)
- cal_mlp_p_mean 0.9583
- **cal_mlp_final_lo 0.8634** (conformal lower bound)

Market price 96¢ ↔ break-even 0.96. cal_mlp's conformal lower bound 0.86
said "the true prob could plausibly be 10pp below market" — a clear skip
signal that the bot ignored because v1 is shadow-only.

This module + gate adds an OPTIONAL synchronous cal_mlp check at TM-96
decision time. Gate fires (blocks the trade) when cal_mlp_final_lo <
market_price/100. Env-gated by `TM96_CALMLP_GATE_ENABLED` (default 0 =
shadow-only — gate is computed and logged but doesn't block trades).

Why TM-96 only:
- Smallest attack surface — TM-96 fires ~5×/day, total inline cost ~125ms/day
- Targets the exact loss pattern we just hit
- Tests the cal_mlp→trading wiring without committing to full v1 promotion
- If TM-96 gating works for 7 days, we expand to TAKER_NOW etc.
"""
import sys
from pathlib import Path

import pytest

_CAL_MLP_DIR = Path(__file__).resolve().parents[1] / 'scripts' / 'cal_mlp'
if str(_CAL_MLP_DIR) not in sys.path:
    sys.path.insert(0, str(_CAL_MLP_DIR))


def _stub_predictor(p_mean=None, p_std=0.01, final_lo=None, final_hi=None,
                     train_id="test-train", raise_exc=None):
    """Build a predictor stub for testing. predict() returns the configured
    (cal_prob, ens_std, final_lo, final_hi) tuple OR raises raise_exc."""
    class StubPredictor:
        def __init__(self):
            self.train_id = train_id
            self.asset = "ETH"
            self.predict_calls = []
        def predict(self, raw_prob, ticker, side, entry_price_cents, row_features):
            self.predict_calls.append({
                'raw_prob': raw_prob, 'ticker': ticker, 'side': side,
                'entry_price_cents': entry_price_cents,
                'row_features': dict(row_features),
            })
            if raise_exc is not None:
                raise raise_exc
            return (p_mean, p_std, final_lo, final_hi)
    return StubPredictor()


def test_gate_blocks_when_final_lo_below_market_price():
    """The motivating case: cal_mlp_final_lo 0.86 vs market_price 96¢ → BLOCK."""
    import integration
    pred = _stub_predictor(p_mean=0.9583, final_lo=0.8634, final_hi=1.0)
    would_block, diag = integration.should_block_tm96(
        predictor=pred,
        raw_prob=0.9307, calibrated_prob=0.9307, ticker="KXETH15M-TEST", market_price=96,
        seconds_to_close=294.0, spot=2400.5, threshold=2350.0,
        blended_rv=0.001, vol_regime="normal",
    )
    assert would_block is True, (
        f"Gate should fire when final_lo (0.86) < market_price/100 (0.96); diag={diag}"
    )
    assert diag.get('cal_mlp_final_lo') == 0.8634
    assert diag.get('cal_mlp_p_mean') == 0.9583


def test_gate_allows_when_final_lo_above_market_price():
    """Trade is safe per cal_mlp: lo=0.97 > market 96¢ → ALLOW."""
    import integration
    pred = _stub_predictor(p_mean=0.985, final_lo=0.97, final_hi=1.0)
    would_block, diag = integration.should_block_tm96(
        predictor=pred,
        raw_prob=0.97, calibrated_prob=0.97, ticker="KXBTC15M-TEST", market_price=96,
        seconds_to_close=240.0, spot=72000, threshold=71500,
        blended_rv=0.001, vol_regime="normal",
    )
    assert would_block is False, f"Gate should NOT fire; final_lo > market; diag={diag}"


def test_gate_fails_open_on_predictor_none():
    """No predictor available (e.g., bundle load failed) → don't block.
    Bot's existing trading behavior is unchanged."""
    import integration
    would_block, diag = integration.should_block_tm96(
        predictor=None,
        raw_prob=0.93, calibrated_prob=0.93, ticker="X", market_price=96,
        seconds_to_close=240.0, spot=2400, threshold=2350,
        blended_rv=0.001, vol_regime="normal",
    )
    assert would_block is False
    assert diag.get('cal_mlp_skipped_reason') == 'no_predictor'


def test_gate_fails_open_on_predict_exception():
    """If predictor.predict() raises, we DO NOT BLOCK the trade. Failing
    closed (block on error) would silently kill all TM-96 trades whenever
    cal_mlp had a bug, which is much worse than missing the gate signal."""
    import integration
    pred = _stub_predictor(raise_exc=RuntimeError("bundle missing"))
    would_block, diag = integration.should_block_tm96(
        predictor=pred,
        raw_prob=0.93, calibrated_prob=0.93, ticker="X", market_price=96,
        seconds_to_close=240.0, spot=2400, threshold=2350,
        blended_rv=0.001, vol_regime="normal",
    )
    assert would_block is False
    assert 'predict' in (diag.get('cal_mlp_skipped_reason') or '')


def test_gate_fails_open_when_final_lo_is_none():
    """predict returns (cal_prob, std, None, None) for some skip paths.
    None can't be compared to market_price; treat as fail-open."""
    import integration
    pred = _stub_predictor(p_mean=0.95, final_lo=None, final_hi=None)
    would_block, diag = integration.should_block_tm96(
        predictor=pred,
        raw_prob=0.93, calibrated_prob=0.93, ticker="X", market_price=96,
        seconds_to_close=240.0, spot=2400, threshold=2350,
        blended_rv=0.001, vol_regime="normal",
    )
    assert would_block is False


def test_gate_uses_integer_hour_matching_training():
    """R-p7-deploy-r7-r3 hour_sin/cos training-drift bug: training uses
    integer hour_of_day_utc. The gate's row_features must use the same
    integer hour, not continuous dt.hour + dt.minute/60. Lock by
    inspecting the row_features the predictor sees."""
    import integration
    pred = _stub_predictor(p_mean=0.95, final_lo=0.90, final_hi=1.0)
    integration.should_block_tm96(
        predictor=pred,
        raw_prob=0.93, calibrated_prob=0.93, ticker="X", market_price=96,
        seconds_to_close=240.0, spot=2400, threshold=2350,
        blended_rv=0.001, vol_regime="normal",
    )
    assert len(pred.predict_calls) == 1
    rf = pred.predict_calls[0]['row_features']
    # hour_sin/cos must be derived from INTEGER dt.hour (not minute-fractional).
    # Verify by checking that hour_sin = sin(2*pi*int_hour/24) where
    # int_hour is dt.hour (no minute term).
    import math
    from datetime import datetime, timezone
    int_hour = float(datetime.now(timezone.utc).hour)
    expected_sin = math.sin(2.0 * math.pi * int_hour / 24.0)
    expected_cos = math.cos(2.0 * math.pi * int_hour / 24.0)
    # Allow a tiny epsilon for float roundoff.
    assert abs(rf['hour_sin'] - expected_sin) < 1e-6, (
        f"hour_sin {rf['hour_sin']} != expected {expected_sin} (integer hour). "
        f"Training-drift regression — must NOT use minute-fractional hour."
    )
    assert abs(rf['hour_cos'] - expected_cos) < 1e-6


def test_gate_passes_v1_required_features():
    """row_features must include all v1 CONT_FEATURE_COLS that aren't
    auto-seeded or identity_no_zscore. Otherwise predict()'s safety net
    raises 'missing_features' and the gate fails open silently."""
    import integration
    import features
    pred = _stub_predictor(p_mean=0.95, final_lo=0.90, final_hi=1.0)
    integration.should_block_tm96(
        predictor=pred,
        raw_prob=0.93, calibrated_prob=0.93, ticker="X", market_price=96,
        seconds_to_close=240.0, spot=2400, threshold=2350,
        blended_rv=0.001, vol_regime="normal",
    )
    rf = pred.predict_calls[0]['row_features']
    cont_cols = list(features.CONT_FEATURE_COLS)
    transforms = features.CONT_FEATURE_TRANSFORMS
    AUTO_SEEDED = {'market_price'}
    required = [
        c for c in cont_cols
        if c not in AUTO_SEEDED
        and transforms.get(c) != 'identity_no_zscore'
    ]
    missing = [k for k in required if k not in rf]
    assert not missing, f"row_features missing v1 keys: {missing}"


def test_prob_breakeven_gap_uses_calibrated_not_raw_prob():
    """Train/serve consistency invariant: prob_breakeven_gap MUST be computed
    from calibrated_prob (post-CalEngine), NOT raw_prob.

    Training: extract_data.py reads `prob_breakeven_gap` from the DB column
    populated via bot.py:1513 `compute_derived_features(calibrated_prob=...)`.
    Therefore serving paths must match the same formula or the model is
    served features in a different distribution than training.

    Concrete test: pass calibrated_prob != raw_prob, assert the row_features
    breakeven_gap matches calibrated-based, NOT raw-based.
    """
    import integration
    pred = _stub_predictor(p_mean=0.95, final_lo=0.90, final_hi=1.0)
    # Construct a case where they differ by a knowable amount.
    raw = 0.92
    calibrated = 0.85  # CalEngine pulled raw down 7pp
    market_price = 96
    integration.should_block_tm96(
        predictor=pred,
        raw_prob=raw, calibrated_prob=calibrated, ticker="X",
        market_price=market_price,
        seconds_to_close=240.0, spot=2400, threshold=2350,
        blended_rv=0.001, vol_regime="normal",
    )
    rf = pred.predict_calls[0]['row_features']
    expected_cb_based = calibrated - market_price / 100.0  # 0.85 - 0.96 = -0.11
    expected_raw_based = raw - market_price / 100.0  # 0.92 - 0.96 = -0.04
    actual = rf['prob_breakeven_gap']
    assert abs(actual - expected_cb_based) < 1e-9, (
        f"prob_breakeven_gap={actual} should match calibrated-based "
        f"({expected_cb_based}), got raw-based ({expected_raw_based})? "
        f"Train/serve skew regression — see bot.py:1513 + integration.py:1196."
    )


def test_post_hoc_processor_reads_breakeven_gap_from_db():
    """The post-hoc processor MUST read prob_breakeven_gap from the DB column
    rather than recomputing it. The DB column is populated via
    compute_derived_features which uses calibrated_prob; recomputing from
    raw_prob in the post-hoc path would re-introduce the train/serve skew.

    AST-style check: post_hoc_processor.py SELECTs prob_breakeven_gap from
    evaluated_opportunities (vs computing it inline).
    """
    src_path = Path(__file__).resolve().parents[1] / 'scripts' / 'cal_mlp' / 'post_hoc_processor.py'
    src = src_path.read_text()
    # SELECT must include prob_breakeven_gap (we read from DB).
    assert 'prob_breakeven_gap' in src
    # Must NOT recompute it from raw_prob in the row_features dict.
    # Specifically, look for the inline-recompute pattern that was in v1.5.
    assert 'raw_prob - (market_price' not in src and 'raw_prob - market_price' not in src, (
        "post_hoc_processor must NOT recompute prob_breakeven_gap from raw_prob; "
        "read the DB column to match training distribution."
    )


def test_bot_py_passes_same_prob_value_to_gate_and_evaluation_insert():
    """R-p7-deploy-r11 R2: the train/serve invariant is that the value
    passed as `calibrated_prob` in `should_block_tm96` must match the
    value passed as `calibrated_prob` to `insert_evaluated_opportunity`
    for the SAME row. Otherwise the gate sees a different distribution
    than training.

    This is stronger than the prior test (which only inspected the
    formula in compute_derived_features). It locks the BOT.PY CALL
    SITE: should_block_tm96(calibrated_prob=final_prob, ...) and
    insert_evaluated_opportunity(..., calibrated_prob=final_prob, ...)
    use the same identifier.

    AST scan: find both call expressions, extract the `calibrated_prob=`
    keyword argument value, assert they're textually identical.
    """
    bot_py = Path(__file__).resolve().parents[1] / 'bot.py'
    if not bot_py.exists():
        pytest.skip('bot.py not present')
    src = bot_py.read_text()

    # The TM-96 should_block_tm96 call: must pass calibrated_prob=<X>
    # where <X> is the same identifier used elsewhere as the final
    # post-CalEngine probability. Find the call site, then within its
    # body (up to the first balanced-paren close), find the
    # calibrated_prob= kwarg. Naive [^)]* doesn't work because nested
    # calls (e.g. _calmlp_predictors.get(asset)) introduce embedded
    # parens. Easiest: locate the start, scan forward 1500 chars
    # which covers the multi-line call.
    import re
    gate_start = src.find('should_block_tm96(')
    # Skip the 'from integration import' line if encountered first.
    while gate_start != -1 and 'import' in src[max(0, gate_start - 80):gate_start]:
        gate_start = src.find('should_block_tm96(', gate_start + 1)
    assert gate_start != -1, "should_block_tm96 call site not found in bot.py"
    call_window = src[gate_start:gate_start + 1500]
    cb_match = re.search(
        r'calibrated_prob\s*=\s*([a-zA-Z_][\w\.]*)',
        call_window,
    )
    assert cb_match, (
        "calibrated_prob= kwarg not found in should_block_tm96 call. "
        "Train/serve invariant requires this kwarg present + bound to final_prob."
    )
    gate_arg = cb_match.group(1)

    # The gate's docs + R2 fix reference final_prob. Lock this name.
    assert gate_arg == 'final_prob', (
        f"should_block_tm96 must receive `calibrated_prob=final_prob` to match "
        f"the value persisted to evaluated_opportunities.calibrated_prob. "
        f"Got `calibrated_prob={gate_arg}` — train/serve skew regression."
    )


def test_post_hoc_processor_breakeven_gap_value_is_db_column_not_recomputed():
    """R-p7-deploy-r11 R2 stronger version + R3-L1 scoping fix: AST-walk
    post_hoc_processor's _process_row function specifically (not every
    Dict in the file). The 'prob_breakeven_gap' value MUST bind to a
    Name node referencing the variable unpacked from the SELECT tuple.
    NOT a BinOp that recomputes from raw_prob.

    R3-L1: the prior version walked `ast.walk(tree)` and broke on the
    first matching Dict. A future refactor adding a config/sentinel dict
    above _process_row could match the wrong dict. Scoping to the actual
    function definition removes that fragility."""
    import ast
    src_path = Path(__file__).resolve().parents[1] / 'scripts' / 'cal_mlp' / 'post_hoc_processor.py'
    src = src_path.read_text()
    tree = ast.parse(src)

    # R3-L1: locate the _process_row method specifically.
    process_row_node: ast.AST | None = None
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == '_process_row':
            process_row_node = node
            break
    assert process_row_node is not None, (
        "post_hoc_processor.py must define _process_row method"
    )

    # Find the row_features dict assignment WITHIN _process_row body only.
    found_value: ast.AST | None = None
    for node in ast.walk(process_row_node):
        if isinstance(node, ast.Dict):
            for key, val in zip(node.keys, node.values):
                if isinstance(key, ast.Constant) and key.value == 'prob_breakeven_gap':
                    found_value = val
                    break
        if found_value is not None:
            break

    assert found_value is not None, (
        "post_hoc_processor._process_row must construct a row_features dict "
        "with 'prob_breakeven_gap' as a key."
    )
    # The value must be a simple Name reference (the unpacked DB column),
    # not a computed expression.
    assert isinstance(found_value, ast.Name), (
        f"post_hoc_processor row_features['prob_breakeven_gap'] must be a "
        f"plain Name reference to the unpacked DB column, not a "
        f"{type(found_value).__name__}. Recomputing from raw_prob would "
        f"re-introduce train/serve skew. Got AST: {ast.dump(found_value)}"
    )
    # And the variable name should be 'prob_breakeven_gap' itself (matching
    # the SELECT tuple unpack at line 200).
    assert found_value.id == 'prob_breakeven_gap', (
        f"Expected the value to reference 'prob_breakeven_gap' (the DB "
        f"column unpack), got '{found_value.id}'."
    )

    # R3-L1 stronger contract: the variable name must also appear in the
    # SELECT-tuple unpack target list at the top of _process_row.
    found_in_unpack = False
    for node in ast.walk(process_row_node):
        # Look for `(row_id, ticker, ..., prob_breakeven_gap, ...) = row` pattern.
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            tgt = node.targets[0]
            if isinstance(tgt, ast.Tuple):
                names = [
                    elt.id for elt in tgt.elts if isinstance(elt, ast.Name)
                ]
                if 'prob_breakeven_gap' in names:
                    found_in_unpack = True
                    break
    assert found_in_unpack, (
        "_process_row must unpack 'prob_breakeven_gap' from the DB SELECT "
        "tuple. If it's not in the unpack target list, the row_features "
        "dict is referencing a different variable (or the SELECT is missing "
        "the column)."
    )


def test_should_block_tm96_uses_calibrated_not_raw_prob():
    """Renamed for accuracy (was test_prob_breakeven_gap_uses_calibrated_not_raw_prob).
    Test scope is just the synchronous gate function, not all of cal_mlp."""
    import integration
    pred = _stub_predictor(p_mean=0.95, final_lo=0.90, final_hi=1.0)
    raw = 0.92
    calibrated = 0.85
    market_price = 96
    integration.should_block_tm96(
        predictor=pred,
        raw_prob=raw, calibrated_prob=calibrated, ticker="X",
        market_price=market_price,
        seconds_to_close=240.0, spot=2400, threshold=2350,
        blended_rv=0.001, vol_regime="normal",
    )
    rf = pred.predict_calls[0]['row_features']
    expected_cb = calibrated - market_price / 100.0
    expected_raw = raw - market_price / 100.0
    actual = rf['prob_breakeven_gap']
    assert abs(actual - expected_cb) < 1e-9, (
        f"should_block_tm96 prob_breakeven_gap={actual} should match "
        f"calibrated-based ({expected_cb}), got raw-based ({expected_raw})?"
    )


def test_bot_py_compute_derived_uses_calibrated_prob():
    """compute_derived_features in bot.py:1472+ MUST compute breakeven_gap
    from calibrated_prob. This is the source of truth — any drift here
    silently corrupts every prob_breakeven_gap row in evaluated_opportunities,
    invalidating the training data for v2/v3.
    """
    import re
    bot_py = Path(__file__).resolve().parents[1] / 'bot.py'
    src = bot_py.read_text()
    # Find the compute_derived_features body around the gap formula.
    # Must compute as: gap = calibrated_prob - (market_price_cents / 100.0)
    m = re.search(
        r'def\s+compute_derived_features\b.*?return\s*\{',
        src, flags=re.DOTALL,
    )
    assert m, "compute_derived_features not found in bot.py"
    body = m.group(0)
    assert 'gap = calibrated_prob - (market_price_cents / 100.0)' in body, (
        "compute_derived_features must use the canonical formula "
        "`calibrated_prob - market_price_cents/100.0`. Train/serve skew "
        "regression — changing this breaks every prob_breakeven_gap row."
    )


def test_bot_py_tm96_intercept_calls_gate():
    """AST-style regression: bot.py's TM-96 intercept block must call
    `should_block_tm96` (or equivalent gate function name). If the gate
    isn't wired at the decision point, env-flag has no effect."""
    bot_py = Path(__file__).resolve().parents[1] / 'bot.py'
    if not bot_py.exists():
        pytest.skip('bot.py not present')
    src = bot_py.read_text()
    # The intercept block sets `_tm_intercepted = True` then computes size.
    # Gate must be invoked between intercept-true and append.
    assert 'should_block_tm96' in src or 'tm96_should_block' in src, (
        "bot.py TM-96 intercept must call cal_mlp gate function "
        "`should_block_tm96` (or `tm96_should_block`)"
    )


def test_tm96_gate_blocked_trade_initialized_at_outer_scope():
    """Round-2 H1 regression: `_tm96_gate_blocked_trade` MUST be initialized
    at the same indent level as `_tm_intercepted = False` (outer scope of
    the insufficient_edge branch), NOT inside the nested TM-eligibility
    chain. Otherwise any non-TM-eligible insufficient_edge candidate
    (price not in {96,98,99}, or fails any other guard) reaches the read
    site at the `if _tm96_gate_blocked_trade: continue` line with the
    local UNBOUND → UnboundLocalError → scan iteration crash.

    Inspects bot.py source to verify the init line is at the same column
    as `_tm_intercepted = False` and appears BEFORE the
    `if (TERMINAL_MOMENTUM_ENABLED` guard.
    """
    bot_py = Path(__file__).resolve().parents[1] / 'bot.py'
    if not bot_py.exists():
        pytest.skip('bot.py not present')
    src = bot_py.read_text()
    # Find both lines and capture leading whitespace.
    import re
    intercept_match = re.search(r'^([ \t]*)_tm_intercepted = False[ ]*(?:#.*)?$', src, re.MULTILINE)
    gate_match = re.search(r'^([ \t]*)_tm96_gate_blocked_trade = False[ ]*(?:#.*)?$', src, re.MULTILINE)
    assert intercept_match, "`_tm_intercepted = False` not found in bot.py"
    assert gate_match, "`_tm96_gate_blocked_trade = False` not found in bot.py"
    assert intercept_match.group(1) == gate_match.group(1), (
        f"_tm96_gate_blocked_trade init at column {len(gate_match.group(1))} "
        f"must match _tm_intercepted column {len(intercept_match.group(1))} "
        f"to avoid UnboundLocalError on non-TM-eligible candidates."
    )
    # Gate init must appear BEFORE the TM_ENABLED guard.
    tm_enabled_pos = src.find('if (TERMINAL_MOMENTUM_ENABLED', intercept_match.end())
    assert gate_match.start() < tm_enabled_pos, (
        "_tm96_gate_blocked_trade init must precede the TM-eligibility guard."
    )


def test_bot_py_has_tm96_gate_env_flag():
    """The gate must be env-flag controlled so an operator can flip it
    off in <30s if cal_mlp starts over-blocking. Default OFF (shadow-only)
    until validated."""
    bot_py = Path(__file__).resolve().parents[1] / 'bot.py'
    if not bot_py.exists():
        pytest.skip('bot.py not present')
    src = bot_py.read_text()
    assert 'TM96_CALMLP_GATE_ENABLED' in src, (
        "Define `TM96_CALMLP_GATE_ENABLED` env-controlled flag in bot.py "
        "for safe rollback."
    )
