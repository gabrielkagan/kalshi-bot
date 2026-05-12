"""TDD for sim_pnl H4 — gate probability source.

Per kb/findings/sim-pnl-live-ws-divergence-rca-may05.md H4:

    Sim_pnl's `gate_passes` (sim_pnl.py:135 + call site sim_pnl.py:820)
    is invoked with `final_lo` (conformal lower bound = p_center − q_alpha).
    Production's gate (bot/_impl.py:13486) checks `fee_adjusted_edge` derived
    from `final_prob` (= post-blend center, stored in DB as
    `calibrated_prob`).

    `final_lo` is strictly less than `p_center` whenever `q_alpha > 0`,
    so sim_pnl is consistently MORE conservative than production at the
    gate. On post-cutover live_ws data this contributes to the regime
    divergence (sim_pnl: -$229/3d actual vs production: +$222/3d).

Fix shape (H4 contract — what these tests pin):

    1. `gate_passes` first parameter is the post-calibration probability
       (the "what the bot thinks YES will resolve at"), NOT a conservative
       lower bound. Param name reflects this — anything except `final_lo`.

    2. `_replay_one_path` accepts a `gate_prob_source` selector:
         - 'p_mean' (default; safe for v2 challenger inference and any run
           where the row's stored calibrated_prob doesn't reflect THIS
           bundle's prediction) → pass `p_mean` to the gate.
         - 'stored_calibrated_prob' (used for the BASE replay where the
           bundle == production's bundle, so row['calibrated_prob'] is
           the exact production decision-time value) → pass
           `row['calibrated_prob']` to the gate, with `p_mean` fallback
           when the row's value is NULL.

    3. `final_lo` (conformal lower bound) is preserved on the result for
       coverage diagnostics, but does NOT drive the gate.

These tests fail pre-fix and pass post-fix.
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "scripts" / "cal_mlp"))


# ── Behavioral tests ───────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _require_deps():
    pytest.importorskip("pandas")
    pytest.importorskip("numpy")
    pytest.importorskip("torch")


def _one_row_df(*, p_pred=0.95, entry=90, side='yes', strategy='decided_t1',
                stored_calibrated_prob=0.95):
    """Single-row candidate DataFrame matching the columns _replay_one_path
    indexes. Defaults are calibrated so:
        breakeven(YES, 90c) = 0.90
        fee_frac(1ct @ 90c) ≈ 0.01
        min_edge_for_price(90c)  = 0.0025
        gate-pass condition      : prob - 0.90 - 0.01 >= 0.0025
                                   ⟹ prob >= 0.9125
    Pick p_pred=0.95 → p_center=0.95 (with market_blend_w=0) → passes.
    Patched predict_with_interval below returns final_lo=0.90 → fails the
    gate (under the buggy code path that uses final_lo).
    """
    import pandas as pd
    return pd.DataFrame({
        'evaluation_time': [pd.Timestamp('2026-05-04T16:00:00Z')],
        'ticker': ['T1'],
        'p_pred': [float(p_pred)],
        'p_std': [0.01],
        'price_tier': [2],
        'stc_bucket': [1],
        'vol_regime_int': [0],
        'entry_price_cents': [int(entry)],
        'side': [side],
        'is_weekend': [0],
        'hour_of_day_utc': [16],
        'seconds_to_close': [400.0],
        'fee_adjusted_edge': [0.05],
        'available_balance_cents': [50000],
        'strategy': [strategy],
        'market_result': ['yes'],
        'calibrated_prob': [stored_calibrated_prob],
        'raw_prob': [float(p_pred)],
    })


def _install_predict_stub(monkeypatch, *, p_center, p_std, final_lo, final_hi):
    """Replace sim_pnl.predict_with_interval with a constant-return stub
    so the test controls the (p_center, final_lo) tuple precisely. All
    inputs are ignored."""
    import sim_pnl

    def fake_predict(p_pred, p_std_arg, conformal_artifact, row_features,
                     entry_price_cents, side, market_blend_w, mode='inference'):
        return (float(p_center), float(p_std), float(final_lo), float(final_hi))

    monkeypatch.setattr(sim_pnl, 'predict_with_interval', fake_predict)


def _install_gate_spy(monkeypatch, *, return_value: bool = False):
    """Replace sim_pnl.gate_passes with a spy that records its first
    positional argument and returns `return_value` to short-circuit
    downstream sizing/PnL accounting (we only care what was passed in)."""
    import sim_pnl

    record = {'calls': []}

    def spy(prob_arg, breakeven, min_edge_frac, *args, **kwargs):
        record['calls'].append({
            'prob_arg': float(prob_arg),
            'breakeven': float(breakeven),
            'min_edge_frac': float(min_edge_frac),
        })
        return bool(return_value)

    monkeypatch.setattr(sim_pnl, 'gate_passes', spy)
    return record


def test_gate_receives_p_mean_not_final_lo_by_default(monkeypatch):
    """H4 contract A — DEFAULT gate_prob_source ('p_mean'): the gate must
    receive the post-blend p_mean (= 0.95 in this fixture), NOT the
    conformal lower bound final_lo (= 0.90).

    Pre-fix: gate is called with 0.90 (final_lo). Test asserts 0.95
    (p_mean) and FAILS.
    Post-fix: gate is called with 0.95 (p_mean). Test passes.
    """
    import sim_pnl

    _install_predict_stub(monkeypatch, p_center=0.95, p_std=0.01,
                          final_lo=0.90, final_hi=1.00)
    spy = _install_gate_spy(monkeypatch, return_value=False)

    df = _one_row_df()
    sim_pnl._replay_one_path(
        df=df, conformal_artifact={}, market_blend_w=0.0,
        asset='ETH', block_enabled=False,
        hwm_init_cents=50000, start_balance_cents=50000,
    )

    assert len(spy['calls']) == 1, (
        f"gate_passes should be called exactly once for the single row; "
        f"got {len(spy['calls'])} calls. Call list: {spy['calls']}"
    )
    prob_arg = spy['calls'][0]['prob_arg']
    assert prob_arg == pytest.approx(0.95, abs=1e-9), (
        f"H4: default gate_prob_source must pass p_mean (= 0.95) to "
        f"gate_passes, NOT final_lo (= 0.90). Got prob_arg={prob_arg}. "
        f"See kb/findings/sim-pnl-live-ws-divergence-rca-may05.md H4."
    )


def test_gate_receives_stored_calibrated_prob_when_base_mode(monkeypatch):
    """H4 contract B — `gate_prob_source='stored_calibrated_prob'` (used
    by the BASE replay against production-matching bundle): the gate
    must receive `row['calibrated_prob']` (= 0.93 here), NOT the
    re-computed p_mean (= 0.95) and NOT final_lo (= 0.88).

    Stored calibrated_prob is the exact value production used at decision
    time (bot/_impl.py writes final_prob into this column at sites including
    bot/_impl.py:13675/13702/13739/13832/13949). Replaying with stored value
    is what makes the BASE run match production within tolerance.

    Pre-fix: function does not accept `gate_prob_source` kwarg → TypeError.
    Post-fix: gate receives 0.93. Test passes.
    """
    import sim_pnl

    _install_predict_stub(monkeypatch, p_center=0.95, p_std=0.01,
                          final_lo=0.88, final_hi=1.00)
    spy = _install_gate_spy(monkeypatch, return_value=False)

    df = _one_row_df(stored_calibrated_prob=0.93)
    sim_pnl._replay_one_path(
        df=df, conformal_artifact={}, market_blend_w=0.0,
        asset='ETH', block_enabled=False,
        hwm_init_cents=50000, start_balance_cents=50000,
        gate_prob_source='stored_calibrated_prob',
    )

    assert len(spy['calls']) == 1, (
        f"gate_passes should be called exactly once; got {len(spy['calls'])}"
    )
    prob_arg = spy['calls'][0]['prob_arg']
    assert prob_arg == pytest.approx(0.93, abs=1e-9), (
        f"H4: with gate_prob_source='stored_calibrated_prob' the gate "
        f"must receive row['calibrated_prob'] (= 0.93), NOT the recomputed "
        f"p_mean (= 0.95) and NOT final_lo (= 0.88). Got prob_arg={prob_arg}. "
        f"This is the BASE-replay mode used to match production decisions "
        f"exactly per RCA H4 'Use calibrated_prob for the gate (matches "
        f"production)'."
    )


def test_gate_falls_back_to_p_mean_when_stored_calibrated_prob_is_null(monkeypatch):
    """H4 contract C — defensive fallback. If gate_prob_source is
    'stored_calibrated_prob' but the row has NaN/NULL calibrated_prob
    (legacy rows pre-cal_mlp-annotation, or rare gaps), fall back to
    p_mean. Silent crash / KeyError / NaN-poisoning would all be worse
    than degrading to recomputed p_mean.
    """
    import numpy as np
    import sim_pnl

    _install_predict_stub(monkeypatch, p_center=0.95, p_std=0.01,
                          final_lo=0.88, final_hi=1.00)
    spy = _install_gate_spy(monkeypatch, return_value=False)

    df = _one_row_df(stored_calibrated_prob=np.nan)
    sim_pnl._replay_one_path(
        df=df, conformal_artifact={}, market_blend_w=0.0,
        asset='ETH', block_enabled=False,
        hwm_init_cents=50000, start_balance_cents=50000,
        gate_prob_source='stored_calibrated_prob',
    )

    assert len(spy['calls']) == 1
    prob_arg = spy['calls'][0]['prob_arg']
    assert prob_arg == pytest.approx(0.95, abs=1e-9), (
        f"H4: when row['calibrated_prob'] is NaN, gate must fall back to "
        f"p_mean (= 0.95). Got prob_arg={prob_arg}. NaN/None must NEVER "
        f"reach gate_passes (would silently propagate NaN through fee_adj "
        f"comparison and short-circuit to False without surfacing the "
        f"missing-data condition)."
    )


def test_replay_accepts_borderline_trade_post_h4_fix(monkeypatch):
    """End-to-end behavioral test (the user-visible H4 effect).

    Construct a row where:
      - p_center = 0.95 → fee_adj_edge_via_p_center = (0.95-0.90)-0.01 = 0.04
                          ≥ min_edge(90c)=0.0025 → PASS
      - final_lo = 0.90 → fee_adj_edge_via_final_lo  = (0.90-0.90)-0.01 = -0.01
                          < min_edge(90c)=0.0025  → FAIL

    Pre-fix: sim_pnl uses final_lo → REJECTS → no PnL recorded.
    Post-fix: sim_pnl uses p_mean → ACCEPTS → PnL recorded
    (market_result='yes' AND side='yes' → win, payoff = 100-90 = 10c per
    contract gross, less fees).

    Asserts pnl_per_band has a non-zero entry in the 0.92-0.96 band
    (where p_pred=0.95 lands).
    """
    import sim_pnl

    # NOTE: we do NOT spy on gate_passes here — let the REAL gate run.
    # We only stub predict_with_interval so the (p_center, final_lo)
    # values are deterministic.
    _install_predict_stub(monkeypatch, p_center=0.95, p_std=0.01,
                          final_lo=0.90, final_hi=1.00)

    df = _one_row_df()
    result = sim_pnl._replay_one_path(
        df=df, conformal_artifact={}, market_blend_w=0.0,
        asset='ETH', block_enabled=False,
        hwm_init_cents=50000, start_balance_cents=50000,
    )

    # _replay_one_path returns per-band PnL in DOLLARS under the key
    # `per_band_pnl_30d` (sim_pnl.py:937 — divides cents by 100).
    band_pnl = dict(result.get('per_band_pnl_30d', {}))
    assert band_pnl.get('0.92-0.96', 0) > 0, (
        f"H4 end-to-end: row at p_mean=0.95 (gate-pass via post-blend) "
        f"should produce a winning trade (side=yes, market_result=yes, "
        f"entry=90c). Pre-fix gate uses final_lo=0.90 → row rejected → "
        f"band 0.92-0.96 PnL = 0. Post-fix gate uses p_mean=0.95 → row "
        f"accepted → band PnL > 0. Got per_band_pnl_30d={band_pnl}. "
        f"Result keys: {sorted(result.keys())}."
    )


# ── AST guards ────────────────────────────────────────────────────────


SIM_PNL_PATH = REPO / "scripts" / "cal_mlp" / "sim_pnl.py"


def _sim_pnl_tree() -> ast.Module:
    return ast.parse(SIM_PNL_PATH.read_text())


def _find_func(name: str) -> ast.FunctionDef:
    for node in _sim_pnl_tree().body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"function {name!r} not found at module level in sim_pnl.py")


def test_gate_passes_first_param_is_not_named_final_lo():
    """AST guard: `gate_passes` first parameter must reflect the
    post-calibration semantic, not the conservative lower bound. The
    name `final_lo` was the original buggy contract; reverting to it
    silently re-introduces H4."""
    fn = _find_func('gate_passes')
    first_param = fn.args.args[0].arg if fn.args.args else None
    assert first_param is not None, "gate_passes has no parameters"
    assert first_param != 'final_lo', (
        f"H4 AST guard: gate_passes first parameter is named 'final_lo' — "
        f"that was the buggy pre-H4 contract (gate fed by conformal lower "
        f"bound, NOT post-blend probability). Rename to a name that "
        f"reflects post-calibration semantics, e.g. 'prob_yes_calibrated' "
        f"or 'prob_post_blend'."
    )


def test_replay_one_path_accepts_gate_prob_source_kwarg():
    """AST guard: `_replay_one_path` must accept `gate_prob_source` as
    a keyword-or-positional parameter so callers can choose between
    'p_mean' (challenger / default) and 'stored_calibrated_prob' (base
    replay against production-matching bundle)."""
    fn = _find_func('_replay_one_path')
    all_param_names = [a.arg for a in fn.args.args] + [a.arg for a in fn.args.kwonlyargs]
    assert 'gate_prob_source' in all_param_names, (
        f"H4 AST guard: `_replay_one_path` must accept `gate_prob_source` "
        f"to let callers pick the gate's probability input. Found params: "
        f"{all_param_names}."
    )


def test_replay_one_path_does_not_pass_final_lo_to_gate_passes():
    """AST guard: inside `_replay_one_path`, the call to `gate_passes`
    must NOT pass `final_lo` as the first positional arg. Pre-fix
    (sim_pnl.py:820) had `gate_passes(final_lo, breakeven, min_edge_frac, ...)`.
    """
    fn = _find_func('_replay_one_path')
    found_call = False
    for node in ast.walk(fn):
        if not isinstance(node, ast.Call):
            continue
        callee = None
        if isinstance(node.func, ast.Name):
            callee = node.func.id
        elif isinstance(node.func, ast.Attribute):
            callee = node.func.attr
        if callee != 'gate_passes':
            continue
        found_call = True
        if not node.args:
            raise AssertionError(
                "gate_passes call inside _replay_one_path has no positional args"
            )
        first = node.args[0]
        first_name = None
        if isinstance(first, ast.Name):
            first_name = first.id
        # Ban the literal name `final_lo`. Anything else (a variable named
        # `gate_prob`, `prob_yes_calibrated`, an inline conditional, etc.)
        # is acceptable — the behavioral tests above validate the value.
        assert first_name != 'final_lo', (
            f"H4 AST guard: `_replay_one_path` calls "
            f"`gate_passes(final_lo, ...)` at sim_pnl.py:{node.lineno}. "
            f"Pre-H4 contract — must select p_mean or "
            f"row['calibrated_prob'] per gate_prob_source."
        )
    assert found_call, (
        "no gate_passes call found inside _replay_one_path — refactor "
        "moved the call site? Update this AST guard."
    )


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
