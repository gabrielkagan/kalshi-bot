"""TDD for sim_pnl H3+H5 — candidate pool restriction to production-
eligible filter_stages.

Per kb/findings/sim-pnl-live-ws-divergence-rca-may05.md H3 + H5:

    sim_pnl currently pulls ALL evaluated_opportunities rows in the
    audit window (no `filter_stage` predicate). It then runs the gate
    on each row. For rows that production REJECTED at runtime (cooldowns,
    cell-blocks, cal_mlp gate, etc.), sim_pnl has no equivalent block —
    so it accepts those rows and counts them as wins/losses. Production
    never took these rows.

    Direct evidence on the post-cutover live_ws snapshot (3-day window):
        TM98_97_98C_2_5MIN_BLEED         n=80   calibrated_prob=80, strategy=80
        SOL_TAKER_85_89C_2_5MIN_BLEED    n=13   calibrated_prob=13, strategy=13
        tm96_calmlp_gate_blocked         n=38   calibrated_prob=38, strategy=38
        silent_loss_cooldown             n=34   calibrated_prob= 0, strategy= 0
        zero_sizing                      n=30   calibrated_prob=30, strategy= 0
        dead_hour_passed                 n=31   calibrated_prob=31, strategy= 0
        usaft_short_stc                  n=16   calibrated_prob=16, strategy= 0

    Rows with calibrated_prob would all pass the H4-fixed gate. Sum=242
    extra rows sim_pnl would admit that production didn't.

Fix shape (H3+H5 contract — what these tests pin):

    1. Module constant `_PRODUCTION_RUNTIME_BLOCKED_STAGES: frozenset[str]`
       enumerates the stages production rejects at runtime. Sim_pnl can't
       model these (cooldowns, cell-blocks, the cal_mlp TM96 gate) and
       must not pretend to.

    2. Helper `_exclude_production_runtime_blocked(df) -> df` drops rows
       whose `filter_stage` is in the blocked set; preserves all others.
       Raises if `filter_stage` column is missing (SELECT-list regression
       guard).

    3. `run_sim_pnl` calls the helper after the SQL pull so downstream
       imputation / replay never sees these rows.

    4. SQL SELECT-list includes `filter_stage` (without it the helper
       can't apply the filter).
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "scripts" / "cal_mlp"))


# ── Constant lock ─────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _require_deps():
    pytest.importorskip("pandas")
    pytest.importorskip("numpy")
    pytest.importorskip("torch")


# Canonical set per the live_ws snapshot evidence above (RCA H3+H5).
# These are stages where production's runtime gate rejected the row for
# reasons sim_pnl does not model:
#   - cooldowns (silent_loss_cooldown, dead_hour_passed)
#   - sizing-failure rejections (zero_sizing) — production sizes via
#     PositionSizer with concurrent-position state sim_pnl can't replay
#   - per-time-window STC tightenings (usaft_short_stc) sim_pnl doesn't
#     know about
#   - cell-blocks shipped 2026-04-30 (TM98, SOL_TAKER, HPSB) and
#     cal_mlp TM-96 gate. sim_pnl only re-implements HPSB internally
#     (block_off / block_on), not the others.
_CANONICAL_BLOCKED_STAGES = {
    'silent_loss_cooldown',
    'dead_hour_passed',
    'zero_sizing',
    'usaft_short_stc',
    'TM98_97_98C_2_5MIN_BLEED',
    'SOL_TAKER_85_89C_2_5MIN_BLEED',
    'SOL_BLEED_V2_88_93C_2_5MIN',
    '96C_SOL_XRP_STC_DANGER_BAND',  # HPSB (no rows in May-2-6 window; lock anyway)
    'tm96_calmlp_gate_blocked',
}


def test_production_runtime_blocked_stages_constant_exists():
    """sim_pnl must export the module constant with the canonical set
    (or a superset). Drift = silent regression: if a new cell-block
    ships in bot/_impl.py and the maintainer forgets to add it here, sim_pnl
    starts admitting blocked rows again."""
    import sim_pnl

    assert hasattr(sim_pnl, '_PRODUCTION_RUNTIME_BLOCKED_STAGES'), (
        "sim_pnl must define module-level constant "
        "`_PRODUCTION_RUNTIME_BLOCKED_STAGES: frozenset[str]` per H3+H5 fix. "
        "See kb/findings/sim-pnl-live-ws-divergence-rca-may05.md."
    )
    blocked = sim_pnl._PRODUCTION_RUNTIME_BLOCKED_STAGES
    assert isinstance(blocked, frozenset), (
        f"_PRODUCTION_RUNTIME_BLOCKED_STAGES must be frozenset (immutable); "
        f"got {type(blocked).__name__}."
    )
    missing = _CANONICAL_BLOCKED_STAGES - set(blocked)
    assert not missing, (
        f"_PRODUCTION_RUNTIME_BLOCKED_STAGES missing canonical stages: "
        f"{sorted(missing)}. These are the stages production rejects at "
        f"runtime that sim_pnl does NOT model — without them, sim_pnl "
        f"admits rows production never took. See "
        f"kb/findings/sim-pnl-live-ws-divergence-rca-may05.md H3+H5."
    )


# ── Helper-function behavior ─────────────────────────────────────────


def test_exclude_helper_drops_blocked_stages():
    """Helper drops rows whose filter_stage is in the blocked set;
    preserves all others. Empty input → empty output.

    Updated post-H7 (round-1 adversarial review CRITICAL #1+#2): the
    weekend_discount / overnight_discount / decided_contract_* /
    terminal_momentum / *_shadow filter_stages are scan-block precursor
    rows that the bot/_impl.py executor double-logs at filter_stage='candidate'
    when fired. Sim_pnl dedupes by dropping the precursor; the
    `candidate` row carries the populated `strategy` field that H7's
    dispatcher needs.
    """
    import pandas as pd
    import sim_pnl

    df = pd.DataFrame({
        'filter_stage': [
            'candidate',                      # KEEP — canonical executor row
            'insufficient_edge',              # KEEP (gate-eligible)
            'silent_loss_cooldown',           # DROP
            'TM98_97_98C_2_5MIN_BLEED',       # DROP
            'SOL_TAKER_85_89C_2_5MIN_BLEED',  # DROP
            'SOL_BLEED_V2_88_93C_2_5MIN',     # DROP (new SOL_BLEED_V2 gate)
            'tm96_calmlp_gate_blocked',       # DROP
            'zero_sizing',                    # DROP
            'dead_hour_passed',               # DROP
            'usaft_short_stc',                # DROP
            '96C_SOL_XRP_STC_DANGER_BAND',    # DROP (HPSB)
            'weekend_discount',               # DROP (H7: precursor scan-block log)
            'weekend_discount_shadow',        # DROP (H7: shadow precursor)
            'overnight_discount',             # DROP (H7: precursor)
            'overnight_discount_shadow',      # DROP (H7: shadow precursor)
            'decided_contract_t1',            # DROP (H7: precursor)
            'decided_contract_t1b',           # DROP (H7: precursor)
            'decided_contract_t2',            # DROP (H7: precursor)
            'decided_contract_t2_z25',        # DROP (H7: precursor)
            'decided_contract_t2_z2',         # DROP (H7: precursor)
            'terminal_momentum',              # DROP (H7: precursor)
            'tm_nbbo_buffer_shadow',          # DROP (H7: NBBO gate)
            'low_price_shadow',               # KEEP (shadow path, gate-eligible)
        ],
        'pnl_marker': list(range(23)),
    })

    out = sim_pnl._exclude_production_runtime_blocked(df)
    kept_stages = set(out['filter_stage'])
    assert kept_stages == {
        'candidate', 'insufficient_edge', 'low_price_shadow',
    }, f"helper kept unexpected stages: {sorted(kept_stages)}"
    # Index is reset so downstream .iloc-based code doesn't surprise.
    assert list(out.index) == list(range(len(out))), (
        f"helper must reset_index; got index={list(out.index)}"
    )


def test_exclude_helper_raises_on_missing_filter_stage_column():
    """A regression where the SELECT-list drops `filter_stage` would
    silently bypass the H3+H5 filter. Helper must fail fast."""
    import pandas as pd
    import sim_pnl

    df = pd.DataFrame({'ticker': ['T1'], 'pnl_marker': [0]})  # no filter_stage
    with pytest.raises(Exception) as exc_info:
        sim_pnl._exclude_production_runtime_blocked(df)
    msg = str(exc_info.value).lower()
    assert 'filter_stage' in msg, (
        f"error must mention `filter_stage` to make the SELECT-list "
        f"regression obvious. Got: {exc_info.value!r}"
    )


def test_exclude_helper_handles_empty_dataframe():
    """Empty input is legitimate (no candidates in window); helper must
    not raise."""
    import pandas as pd
    import sim_pnl

    df = pd.DataFrame({'filter_stage': pd.Series([], dtype=str),
                       'pnl_marker': pd.Series([], dtype=int)})
    out = sim_pnl._exclude_production_runtime_blocked(df)
    assert len(out) == 0
    assert 'filter_stage' in out.columns


def test_ticker_dedup_keeps_candidate_drops_shadows():
    """R6 MAJOR #1 — when a ticker has BOTH a 'candidate' row and one or
    more shadow-tag rows in the same window (low_price_shadow,
    golden_hour_shadow, relaxed_edge_shadow, dc_shadow_t2_z2, etc.),
    sim_pnl pre-fix admitted ALL rows through the gate and counted the
    ticker's outcome 2-N times. Production realized at most ONE trade per
    ticker. Helper `_dedup_by_ticker_keep_canonical` collapses rows-per-
    ticker keeping the canonical 'candidate' row when present."""
    import pandas as pd
    import sim_pnl

    df = pd.DataFrame({
        'ticker':        ['T1', 'T1', 'T1', 'T2', 'T3', 'T3'],
        'evaluation_time': [
            '2026-05-03T00:00:00Z',  # T1 shadow at t1
            '2026-05-03T00:00:30Z',  # T1 candidate at t2 (later)
            '2026-05-03T00:01:00Z',  # T1 another shadow at t3
            '2026-05-03T00:00:45Z',  # T2 shadow only
            '2026-05-03T00:02:00Z',  # T3 shadow earlier
            '2026-05-03T00:03:00Z',  # T3 shadow later (no candidate)
        ],
        'filter_stage': [
            'low_price_shadow',     # DROP (T1 has candidate sibling)
            'candidate',            # KEEP (canonical for T1)
            'golden_hour_shadow',   # DROP
            'relaxed_edge_shadow',  # KEEP (T2 shadow-only, latest by default)
            'dc_shadow_t2_z2',      # DROP (T3 has later shadow row)
            'low_price_shadow',     # KEEP (T3 latest)
        ],
        'strategy': [None, 'MAKER_PATIENT', None, None, None, None],
    })
    out = sim_pnl._dedup_by_ticker_keep_canonical(df)
    assert len(out) == 3, f"expected 3 unique tickers; got {len(out)}"
    by_ticker = {row['ticker']: row['filter_stage']
                 for _, row in out.iterrows()}
    assert by_ticker == {
        'T1': 'candidate',           # candidate beats shadows
        'T2': 'relaxed_edge_shadow', # shadow-only kept
        'T3': 'low_price_shadow',    # latest among same-priority shadows
    }, f"dedup picked wrong row: {by_ticker}"


def test_ticker_dedup_handles_empty_dataframe():
    """Edge case — empty input should not raise."""
    import pandas as pd
    import sim_pnl

    df = pd.DataFrame({
        'ticker': pd.Series([], dtype=str),
        'evaluation_time': pd.Series([], dtype=str),
        'filter_stage': pd.Series([], dtype=str),
    })
    out = sim_pnl._dedup_by_ticker_keep_canonical(df)
    assert len(out) == 0


def test_ticker_dedup_raises_on_missing_columns():
    """Helper must fail fast if `ticker` or `filter_stage` is missing
    so SQL SELECT-list regressions are caught at the universe-build
    boundary, not silently returned as duplicate-counted PnL."""
    import pandas as pd
    import pytest as _pytest
    import sim_pnl

    # Missing ticker
    df1 = pd.DataFrame({'filter_stage': ['candidate']})
    with _pytest.raises(RuntimeError, match='ticker'):
        sim_pnl._dedup_by_ticker_keep_canonical(df1)

    # Missing filter_stage
    df2 = pd.DataFrame({'ticker': ['T1']})
    with _pytest.raises(RuntimeError, match='filter_stage'):
        sim_pnl._dedup_by_ticker_keep_canonical(df2)


def test_run_sim_pnl_select_excludes_null_side_rows():
    """R6 CRITICAL #1 — SQL must include `side IN ('yes', 'no')` so
    side=NULL rows (dc_shadow_t1b_93c family per bot/_impl.py:13543) don't
    reach the gate. The 46 such rows in May 2-6 window have
    market_result='yes' but side=NULL → trade_pnl_cents would treat
    them all as systematic losses (`str(None) != 'yes'`)."""
    import ast
    fn = _find_func('run_sim_pnl')
    select_literal = None
    for node in ast.walk(fn):
        if not isinstance(node, ast.Call):
            continue
        callee = None
        if isinstance(node.func, ast.Attribute):
            callee = node.func.attr
        elif isinstance(node.func, ast.Name):
            callee = node.func.id
        if callee != 'read_sql':
            continue
        if not node.args:
            continue
        first = node.args[0]
        if isinstance(first, ast.Constant) and isinstance(first.value, str):
            select_literal = first.value
            break
    assert select_literal is not None
    upper = select_literal.upper()
    assert "SIDE IN ('YES', 'NO')" in upper, (
        f"R6 CRITICAL #1 AST guard: SQL WHERE clause must include "
        f"`side IN ('yes', 'no')` to exclude NULL-side shadow rows. "
        f"Got: {select_literal[:600]!r}"
    )


def test_dedup_executed_trade_counts_once():
    """Round-2 adversarial MINOR #1 — invariant test for the precursor
    dedup. bot/_impl.py logs each executed TM/DC/weekend/overnight live trade
    TWICE: once at the strategy-specific scan-block (`filter_stage='decided_contract_t1'`
    etc.) and once at the executor (`filter_stage='candidate'`). The
    fix adds the precursor stages to the blocked set; this test pins
    the invariant that the 'candidate' row survives and the precursor
    is dropped — preventing a future maintainer from removing a stage
    and silently regressing dedup."""
    import pandas as pd
    import sim_pnl

    # Synthetic 6-row fixture: 3 paired (precursor + candidate) trades
    # for terminal_momentum, decided_t1, weekend_discount.
    df = pd.DataFrame({
        'filter_stage': [
            'terminal_momentum',     # precursor — DROP
            'candidate',             # canonical — KEEP
            'decided_contract_t1',   # precursor — DROP
            'candidate',             # canonical — KEEP
            'weekend_discount',      # precursor — DROP
            'candidate',             # canonical — KEEP
        ],
        'ticker': [
            'KXBTC15M-A', 'KXBTC15M-A',
            'KXETH15M-B', 'KXETH15M-B',
            'KXSOL15M-C', 'KXSOL15M-C',
        ],
        'strategy': [
            'terminal_momentum_98',  # precursor populated
            'terminal_momentum_98',  # candidate populated (canonical)
            None,                    # precursor strategy=NULL (DC pattern)
            'decided_t1',            # candidate populated
            None,                    # precursor strategy=NULL (weekend pattern)
            'weekend_discount',      # candidate populated
        ],
    })
    out = sim_pnl._exclude_production_runtime_blocked(df)
    # Exactly 3 rows survive (one per ticker, all are 'candidate').
    assert len(out) == 3, (
        f"dedup invariant: 3 paired trades → 3 surviving rows; got {len(out)}"
    )
    assert set(out['filter_stage']) == {'candidate'}, (
        f"only 'candidate' rows must survive dedup; got "
        f"{sorted(set(out['filter_stage']))}"
    )
    # All three canonical rows have populated strategy (H7 dispatch works).
    assert out['strategy'].notna().all(), (
        f"all surviving 'candidate' rows must carry populated strategy; "
        f"got {out['strategy'].tolist()}"
    )
    assert set(out['strategy']) == {
        'terminal_momentum_98', 'decided_t1', 'weekend_discount',
    }, f"strategy field drift: {sorted(set(out['strategy']))}"


def test_exclude_helper_preserves_all_non_blocked_columns():
    """Filtering must not silently drop or rename columns."""
    import pandas as pd
    import sim_pnl

    df = pd.DataFrame({
        'filter_stage': ['candidate', 'silent_loss_cooldown'],
        'ticker': ['T1', 'T2'],
        'asset': ['BTC', 'ETH'],
        'extra_col': [1.5, 2.5],
    })
    out = sim_pnl._exclude_production_runtime_blocked(df)
    assert set(out.columns) == set(df.columns), (
        f"column drift: in={sorted(df.columns)}, out={sorted(out.columns)}"
    )
    assert len(out) == 1
    assert out.iloc[0]['ticker'] == 'T1'


# ── AST guards ────────────────────────────────────────────────────────


SIM_PNL_PATH = REPO / "scripts" / "cal_mlp" / "sim_pnl.py"


def _sim_pnl_tree() -> ast.Module:
    return ast.parse(SIM_PNL_PATH.read_text())


def _find_func(name: str) -> ast.FunctionDef:
    for node in _sim_pnl_tree().body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"function {name!r} not found at module level")


def test_run_sim_pnl_select_includes_filter_stage():
    """SQL SELECT-list must include `filter_stage` so the helper has the
    column to filter on. AST guard against a regression that drops the
    column. Mirrors the test_sql_select_includes_* pattern from
    test_cal_mlp_sim_pnl_candidate_features.py."""
    fn = _find_func('run_sim_pnl')
    select_literal = None
    for node in ast.walk(fn):
        if not isinstance(node, ast.Call):
            continue
        callee = None
        if isinstance(node.func, ast.Attribute):
            callee = node.func.attr
        elif isinstance(node.func, ast.Name):
            callee = node.func.id
        if callee != 'read_sql':
            continue
        if not node.args:
            continue
        first = node.args[0]
        if isinstance(first, ast.Constant) and isinstance(first.value, str):
            select_literal = first.value
            break
    assert select_literal is not None, (
        "run_sim_pnl: no `read_sql(<str literal>, ...)` found"
    )
    upper = select_literal.upper()
    sel_start = upper.find('SELECT')
    from_start = upper.find('FROM')
    assert sel_start >= 0 and from_start > sel_start, (
        f"SQL literal missing SELECT/FROM: {select_literal[:200]!r}"
    )
    select_list = select_literal[sel_start:from_start]
    assert 'filter_stage' in select_list, (
        f"H3+H5 AST guard: run_sim_pnl's SQL SELECT-list must include "
        f"`filter_stage` so `_exclude_production_runtime_blocked` can "
        f"drop production-runtime-rejected rows. SELECT was: "
        f"{select_list!r}"
    )


def test_run_sim_pnl_calls_exclude_helper():
    """AST guard: run_sim_pnl must call `_exclude_production_runtime_blocked`
    on the post-SQL DataFrame. Without the call, the constant exists but
    nothing happens — silent regression."""
    fn = _find_func('run_sim_pnl')
    found = False
    for node in ast.walk(fn):
        if not isinstance(node, ast.Call):
            continue
        if isinstance(node.func, ast.Name) and node.func.id == '_exclude_production_runtime_blocked':
            found = True
            break
    assert found, (
        "H3+H5 AST guard: `run_sim_pnl` must call "
        "`_exclude_production_runtime_blocked(candidate_df)` after the "
        "SQL pull. Without the call, the H3+H5 fix is a no-op."
    )


def test_exclude_helper_called_before_replay():
    """Order matters: exclude BEFORE _replay_one_path, otherwise blocked
    rows reach the gate and contribute PnL. Lock the ordering."""
    fn = _find_func('run_sim_pnl')

    def _first_call_lineno(name: str) -> int:
        for node in ast.walk(fn):
            if not isinstance(node, ast.Call):
                continue
            callee = None
            if isinstance(node.func, ast.Name):
                callee = node.func.id
            elif isinstance(node.func, ast.Attribute):
                callee = node.func.attr
            if callee == name:
                return node.lineno
        return -1

    exclude_ln = _first_call_lineno('_exclude_production_runtime_blocked')
    replay_ln = _first_call_lineno('_replay_one_path')
    assert exclude_ln > 0, "_exclude_production_runtime_blocked call not found"
    assert replay_ln > 0, "_replay_one_path call not found"
    assert exclude_ln < replay_ln, (
        f"H3+H5 AST guard: `_exclude_production_runtime_blocked` "
        f"(line {exclude_ln}) must be called BEFORE `_replay_one_path` "
        f"(line {replay_ln}). Reverse order = blocked rows reach the "
        f"gate and contribute PnL."
    )


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
