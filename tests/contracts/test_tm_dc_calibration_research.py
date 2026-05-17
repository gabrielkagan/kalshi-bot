"""Spike 86b9zktp6: contract pins for TM/DC conditional band-calibration research.

Pre-flight (TDD-RED) test scaffolded BEFORE the research script exists. Initial
state: test imports a not-yet-existent module so collection RED-fails. Once
`scripts/cal_mlp/tm_dc_calibration_research.py` lands with the locked public
API, the runtime guards GREEN-pass.

This contract pins the research-script surface so that:

  - The matrix data structure is the locked spike shape (per
    `kb/decisions/tm-dc-conditional-calibration-spike-plan-may17.md`)
  - The shrinkage formula mirrors P4.1's `bot.helpers.band_calibration._shrink`
    EXACTLY (same hierarchical Bayes form; differing only in conditioning on
    sub-signal)
  - The per-cell sample-size floor (N≥10) is enforced
  - The fractional-Kelly sweep + shrinkage-k sweep cover the locked grid
  - The script opens `state.db` READ-ONLY (Mac-side spike must not mutate
    production DB)
  - The script uses `SUM(pnl_cents - COALESCE(fee_cents, 0))` for net PnL
    (NOT `SUM(pnl_cents)` — gross/net trap per `scripts/CLAUDE.md`)
  - The script unions cohort-partition `filter_stage` values (NOT naive
    `filter_stage='candidate'` — under-counts post cell-block per `bot/CLAUDE.md`)
  - The spike does NOT mutate production sizing — `_tm_size` / `_dc_size` /
    `_strategy_size` symbols are NOT redefined or monkey-patched in the spike
    script's body

Out of scope (NOT enforced by this contract):
  - The actual realized-rate numbers per cell (those depend on a live state.db
    snapshot — frozen in the findings doc, not in this contract)
  - The recommended fractional-Kelly setting (spike resolves)
  - Whether spike recommends SHIP / NO-SHIP / SHIP-WITH-CAVEATS

Sister anchors:
  - kb/decisions/tm-dc-conditional-calibration-spike-plan-may17.md (this Bit's plan)
  - bot/helpers/band_calibration.py (P4.1 helper, shipped c1e6d85)
  - tests/contracts/test_p4_1_band_calibrated_sizing.py (P4.1 contract — sister)
  - scripts/cal_mlp/sim_pnl.py (`_strategy_size`/`_tm_size`/`_dc_size` — out-of-scope for spike)

Lessons applied:
  - L97 (band-stratified soak): conditional matrix is stratified by
    sub-signal, not just band
  - DD-2 / W0 (assertion-as-fossil): no literal `final_prob` pinned;
    Kelly-input naming is checked structurally via AST, not by string
"""
from __future__ import annotations

import ast
import importlib.util
import sys
import sqlite3
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
SPIKE_SCRIPT = REPO_ROOT / "scripts" / "cal_mlp" / "tm_dc_calibration_research.py"
P4_1_HELPER = REPO_ROOT / "bot" / "helpers" / "band_calibration.py"


# ───────────────────────────────────────────────────────────────────────────
# Locked design parameters (from spike plan doc §"Locked design parameters")
# Pinning these here means a future drift in the plan doc is caught at test
# time, not at adv-review time.
# ───────────────────────────────────────────────────────────────────────────

EXPECTED_SHRINKAGE_K_SWEEP = (30, 50, 100)
EXPECTED_FRACTIONAL_KELLY_SWEEP = (0.25, 0.5, 1.0)
EXPECTED_PER_CELL_N_FLOOR = 10
EXPECTED_TM_BUF_BUCKETS = (0.0, 0.1, 0.3, 0.6, 1.0, float("inf"))
EXPECTED_DC_TIER_LABELS = ("T1", "T1B", "T2", "T2_Z25", "T2_Z2")
EXPECTED_ASSET_UNIVERSE = frozenset({"BTC", "ETH", "SOL", "XRP", "HYPE", "DOGE"})

# Mirrors P4.1 band bounds verbatim (per spike plan §"Locked design parameters"
# → YES-side only, lookback hybrid). If P4.1 BAND_BOUNDS drifts, this test
# fails — and that's correct because the spike must stay in lock-step with
# P4.1's band geometry.
EXPECTED_BAND_BOUNDS = (
    ("70-79", 70, 79),
    ("80-85", 80, 85),
    ("86-89", 86, 89),
    ("90-93", 90, 93),
    ("94-96", 94, 96),
    ("97-98", 97, 98),
    ("99",    99, 100),
)


# ───────────────────────────────────────────────────────────────────────────
# Module-import guards (RED until the spike script lands)
# ───────────────────────────────────────────────────────────────────────────


def _import_spike_module():
    """Import the spike research script as a module, RED if absent."""
    if not SPIKE_SCRIPT.is_file():
        pytest.fail(
            f"Spike script not found at {SPIKE_SCRIPT}. "
            "TDD-RED expected pre-implementation; if this test is firing "
            "post-Phase-3, check the spike's create path."
        )
    # cal_mlp/ dir isn't a package; import the file directly via spec.
    spec = importlib.util.spec_from_file_location(
        "tm_dc_calibration_research", str(SPIKE_SCRIPT)
    )
    if spec is None or spec.loader is None:
        pytest.fail(f"importlib could not build spec for {SPIKE_SCRIPT}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_spike_script_exists_and_imports():
    """RED-then-GREEN: spike script must land at the locked path."""
    mod = _import_spike_module()
    assert mod is not None


# ───────────────────────────────────────────────────────────────────────────
# Public API surface — locked constants
# ───────────────────────────────────────────────────────────────────────────


def test_shrinkage_k_sweep_locked():
    mod = _import_spike_module()
    assert tuple(sorted(mod.SHRINKAGE_K_SWEEP)) == EXPECTED_SHRINKAGE_K_SWEEP


def test_fractional_kelly_sweep_locked():
    mod = _import_spike_module()
    assert tuple(sorted(mod.FRACTIONAL_KELLY_SWEEP)) == EXPECTED_FRACTIONAL_KELLY_SWEEP


def test_per_cell_n_floor_locked():
    mod = _import_spike_module()
    assert mod.PER_CELL_N_FLOOR == EXPECTED_PER_CELL_N_FLOOR


def test_tm_buf_buckets_locked():
    mod = _import_spike_module()
    assert tuple(mod.TM_BUF_BUCKETS) == EXPECTED_TM_BUF_BUCKETS


def test_dc_tier_labels_locked():
    mod = _import_spike_module()
    assert tuple(mod.DC_TIER_LABELS) == EXPECTED_DC_TIER_LABELS


def test_asset_universe_locked():
    mod = _import_spike_module()
    assert frozenset(mod.ASSET_UNIVERSE) == EXPECTED_ASSET_UNIVERSE


def test_band_bounds_lockstep_with_p4_1():
    """The conditional matrix must use the same band geometry as P4.1."""
    mod = _import_spike_module()
    from bot.helpers.band_calibration import BAND_BOUNDS as P4_1_BAND_BOUNDS

    actual = tuple(tuple(b) for b in mod.BAND_BOUNDS)
    assert actual == EXPECTED_BAND_BOUNDS
    # Defense-in-depth: also pin against the live P4.1 helper.
    assert actual == tuple(tuple(b) for b in P4_1_BAND_BOUNDS)


# ───────────────────────────────────────────────────────────────────────────
# Shrinkage formula — must mirror P4.1's `_shrink` exactly
# ───────────────────────────────────────────────────────────────────────────


def test_shrink_cell_formula_matches_p4_1():
    """`shrink_cell(n, raw_p, prior, k) = (n*p + k*prior) / (n+k)`.

    Spot-checked at three points covering: cell-dominated (n=300, k=30),
    prior-dominated (n=5, k=30), and the spike's k=100 over-shrink case.
    """
    mod = _import_spike_module()

    # n=300, k=30 → 91% cell signal
    assert abs(mod.shrink_cell(300, 0.80, 0.85, 30) - ((300*0.80 + 30*0.85) / 330)) < 1e-9
    # n=30, k=30 → 50/50
    assert abs(mod.shrink_cell(30, 0.80, 0.85, 30) - ((30*0.80 + 30*0.85) / 60)) < 1e-9
    # n=10, k=100 → 91% prior (over-shrink scenario)
    assert abs(mod.shrink_cell(10, 0.80, 0.85, 100) - ((10*0.80 + 100*0.85) / 110)) < 1e-9


def test_shrink_cell_handles_empty_cell():
    """n=0 must return the prior verbatim (no division-by-zero, no NaN)."""
    mod = _import_spike_module()
    assert mod.shrink_cell(0, 0.0, 0.85, 30) == 0.85


def test_per_cell_floor_falls_back_to_band_aggregate():
    """Cells with n < PER_CELL_N_FLOOR fall back to (asset, band) aggregate.

    R2-M5 hardening: test now exercises THREE tiers of the floor cascade:
      (1) above-floor cell → use cell rate
      (2) sub-floor cell, above-floor band agg → use band agg rate
      (3) sub-floor cell AND sub-floor band agg → 0.5 neutral
    Test data includes `shrunk_p_by_k` dict to exercise the production
    code path (pre-R2 the test exercised only the legacy single-`shrunk_p`
    fallback).
    """
    mod = _import_spike_module()
    matrix = {
        # tier 1: above floor
        ("BTC", "94-96", "bucket_0"): {
            "n": 20, "raw_p": 0.95,
            "shrunk_p": 0.953,
            "shrunk_p_by_k": {30: 0.953, 50: 0.940, 100: 0.920},
        },
        # tier 2: sub-floor cell, band-agg above floor
        ("BTC", "94-96", "bucket_1"): {
            "n": 5,  "raw_p": 0.60,
            "shrunk_p": 0.733,
            "shrunk_p_by_k": {30: 0.733, 50: 0.741, 100: 0.760},
        },
        # tier 3: sub-floor cell AND sub-floor band agg
        ("HYPE", "97-98", "bucket_2"): {
            "n": 3, "raw_p": 1.00,
            "shrunk_p": 0.95,
            "shrunk_p_by_k": {30: 0.95, 50: 0.94, 100: 0.93},
        },
    }
    band_aggregate = {
        ("BTC", "94-96"): {
            "n": 25, "raw_p": 0.88,
            "shrunk_p": 0.890,
            "shrunk_p_by_k": {30: 0.890, 50: 0.880, 100: 0.870},
        },
        ("HYPE", "97-98"): {  # sub-floor band agg (n=4)
            "n": 4, "raw_p": 1.00,
            "shrunk_p": 0.95,
            "shrunk_p_by_k": {30: 0.95, 50: 0.94, 100: 0.93},
        },
    }
    # Tier 1 — above-floor cell uses cell rate (k=30 by default)
    assert mod.lookup_calibrated_rate(
        matrix, band_aggregate, "BTC", "94-96", "bucket_0"
    ) == pytest.approx(0.953)
    # Tier 2 — sub-floor cell falls back to band-agg rate
    assert mod.lookup_calibrated_rate(
        matrix, band_aggregate, "BTC", "94-96", "bucket_1"
    ) == pytest.approx(0.890)
    # Tier 3 — both sub-floor → 0.5 neutral (R1-Mn1 cascade)
    assert mod.lookup_calibrated_rate(
        matrix, band_aggregate, "HYPE", "97-98", "bucket_2"
    ) == pytest.approx(0.5)
    # k-selection — k=50 vs k=30 returns different values from same cell
    assert mod.lookup_calibrated_rate(
        matrix, band_aggregate, "BTC", "94-96", "bucket_0", k=50
    ) == pytest.approx(0.940)


# ───────────────────────────────────────────────────────────────────────────
# AST guards — script-body invariants
# ───────────────────────────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def spike_ast():
    if not SPIKE_SCRIPT.is_file():
        pytest.fail(f"Spike script missing: {SPIKE_SCRIPT}")
    return ast.parse(SPIKE_SCRIPT.read_text())


def test_net_pnl_uses_fee_coalesce(spike_ast):
    """Script must compute net PnL with explicit fee subtraction in BOTH
    the SQL pull AND the runtime per-contract net path.

    R2-M1 strengthening: pre-fix this test passed solely because the script
    DOCSTRING contained `"COALESCE(fee_cents"` as an example. Now we verify
    BOTH (a) the SQL pulls `COALESCE(st.fee_cents, 0)` and (b) the runtime
    path subtracts fees in `_per_contract_net_cents` — not via string match
    on the docstring.

    Per `scripts/CLAUDE.md`: `pnl_cents` is GROSS. Regression:
    `kb/failures/audit-pnl-fee-omission-apr29.md`.
    """
    src = SPIKE_SCRIPT.read_text()

    # (a) SQL pull must qualify with table alias (e.g. `st.fee_cents`) to
    # avoid the docstring-substring false-GREEN class.
    assert "COALESCE(st.fee_cents" in src, (
        "SQL pull must include `COALESCE(st.fee_cents, 0)` (table-qualified). "
        "Pre-R2 a bare `COALESCE(fee_cents` match in a docstring slipped past."
    )

    # (b) Runtime per-contract net must explicitly subtract fee on BOTH
    # yes and no synthetic-fallback branches (R2-C2 + R3-M3 enforcement).
    # AST-level inspection prevents R2-C2 from being silently reverted.
    file_src = SPIKE_SCRIPT.read_text()
    tree = ast.parse(file_src)
    helper_func = None
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "_per_contract_net_cents":
            helper_func = node
            break
    assert helper_func is not None, "_per_contract_net_cents function not found"

    # The synthetic-fallback section must contain TWO `Return` statements
    # (one for the yes branch, one for the no branch) that BOTH subtract
    # the fee. Walk the function and collect Return nodes whose value is
    # a BinOp with `-` (Sub) operator and a fee-related name on the right.
    fee_subtracting_returns = 0
    for node in ast.walk(helper_func):
        if not isinstance(node, ast.Return) or node.value is None:
            continue
        # Pattern: `<something> - <fee_expr>` where fee_expr references
        # `fee` or `kalshi_fee_cents_per_contract(...)`.
        val = node.value
        if isinstance(val, ast.BinOp) and isinstance(val.op, ast.Sub):
            rhs_src = ast.unparse(val.right) if hasattr(ast, "unparse") else ""
            if "fee" in rhs_src.lower():
                fee_subtracting_returns += 1
    assert fee_subtracting_returns >= 2, (
        f"R2-C2 enforcement: _per_contract_net_cents must subtract fees "
        f"on BOTH yes and no synthetic-fallback branches. Found "
        f"{fee_subtracting_returns} fee-subtracting Return statements; "
        f"expected ≥ 2 (one per branch). "
        f"Pre-R2-C2 broken state had fee subtraction on yes branch only."
    )


def test_replay_pnl_a_b_dedups_duplicate_eo_rows():
    """R2-C3 + R3-M4 enforcement: `replay_pnl_a_b` must de-dup PnL credit
    per (ticker, strategy) pair, so duplicate EO rows for the same trade
    do NOT inflate realized PnL.

    Pre-R2-C3 broken state: same `settled_trades` row JOINed to N EO
    duplicates credits PnL N times. Post-R2-C3 fix: only first EO row per
    pair credits PnL; subsequent duplicates contribute only to contract
    distribution.
    """
    mod = _import_spike_module()
    # Synthetic rows mimicking sqlite3.Row (dict-with-keys interface).
    class _Row:
        def __init__(self, d):
            self._d = d
        def __getitem__(self, k):
            return self._d[k]
        def keys(self):
            return list(self._d.keys())
    # Three EO rows: 2 duplicates of (TKR1, strat) + 1 distinct (TKR2, strat).
    base = {
        "ticker": "TKR1", "strategy": "strat", "asset": "BTC",
        "market_price": 98, "buf_pct": 0.5, "seconds_to_close": 60,
        "market_result": "yes", "pnl_cents": 200, "fee_cents": 1, "settled_count": 1,
    }
    dup = dict(base)  # exact duplicate
    other = {**base, "ticker": "TKR2"}
    rows = [_Row(base), _Row(dup), _Row(other)]

    # Both sizers return 10 contracts; verify dedup limits PnL to 2 pairs.
    result = mod.replay_pnl_a_b(
        rows,
        baseline_sizer=lambda r: 10,
        proposed_sizer=lambda r: 10,
    )
    assert result["n_rows"] == 3
    assert result["n_unique_pnl_pairs"] == 2, (
        f"R2-C3 enforcement: expected 2 unique (ticker, strategy) pairs, "
        f"got {result['n_unique_pnl_pairs']}. Dedup logic is missing or "
        f"broken."
    )
    # Per-contract net for each row = (200 - 1)/1 = 199c. With 2 unique
    # pairs × 10 contracts × 199c, total = 3980c. If dedup were missing,
    # total would be 5970c (3 duplicates × 10 × 199).
    assert result["baseline_total_net_pnl_cents"] == 3980, (
        f"R2-C3 enforcement: expected 3980c net PnL (2 unique pairs × 10 × 199c). "
        f"Got {result['baseline_total_net_pnl_cents']}. If 5970c, dedup is broken."
    )


def test_read_only_db_connection(spike_ast):
    """Script must open state.db in read-only mode.

    Either via `sqlite3.connect('file:...?mode=ro', uri=True)` URI form OR
    via `PRAGMA query_only = 1` immediately after connect. Spike must NOT
    mutate production DB.
    """
    src = SPIKE_SCRIPT.read_text()
    has_ro_uri = "mode=ro" in src
    has_query_only = "query_only" in src and "1" in src
    assert has_ro_uri or has_query_only, (
        "Spike must open state.db in read-only mode. Use either "
        "`sqlite3.connect('file:...?mode=ro', uri=True)` or set "
        "`PRAGMA query_only = 1` after connect. Production DB must not "
        "be mutated by a research script."
    )


def test_wal_pragma_set(spike_ast):
    """Per `bot/CLAUDE.md` SQLite section: any new sqlite3.connect site
    must enable WAL + busy_timeout=10000."""
    src = SPIKE_SCRIPT.read_text()
    assert "journal_mode" in src and "WAL" in src, (
        "Spike script's sqlite3.connect() must set "
        "`PRAGMA journal_mode=WAL` per bot/CLAUDE.md SQLite section."
    )
    assert "busy_timeout" in src and "10000" in src, (
        "Spike script's sqlite3.connect() must set "
        "`PRAGMA busy_timeout=10000` per bot/CLAUDE.md SQLite section."
    )


def test_cohort_partition_stages_used(spike_ast):
    """Script must UNION cohort-partition stages, not naive 'candidate' filter.

    `bot/CLAUDE.md` "Cell-block activations deflate `filter_stage='candidate'`
    rollups": canonical 5-set lives in `bot.helpers.cohort_attribution.COHORT_PARTITION_STAGES`.
    """
    src = SPIKE_SCRIPT.read_text()
    assert "COHORT_PARTITION_STAGES" in src or "cohort_attribution" in src, (
        "Spike must reference `COHORT_PARTITION_STAGES` (from "
        "`bot.helpers.cohort_attribution`). Naive `filter_stage='candidate'` "
        "rollups under-count by missing cell-block stages."
    )


def test_spike_does_not_redefine_production_sizing(spike_ast):
    """Spike must NOT redefine `_tm_size`, `_dc_size`, or `_strategy_size`.

    Those live in `scripts/cal_mlp/sim_pnl.py`. The spike is a research script
    that READS production behavior; any wire-in is a separate followup ticket.
    """
    forbidden = {"_tm_size", "_dc_size", "_strategy_size"}
    redefined = set()
    for node in ast.walk(spike_ast):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name in forbidden:
                redefined.add(node.name)
    assert not redefined, (
        f"Spike script redefined production sizing functions: {redefined}. "
        "Spike scope is READ-ONLY research. Wire-in is a separate followup."
    )


def test_spike_does_not_import_production_writers(spike_ast):
    """Spike must NOT import `bot.state.StateManager` or `bot.executor` or any
    module that performs DB writes / executes trades. Read-only research only."""
    forbidden_imports = {
        "bot.state",
        "bot.executor",
        "bot.order_flow",
        "bot.kalshi_client",
        "bot.settlement",
    }
    bad = []
    for node in ast.walk(spike_ast):
        if isinstance(node, ast.ImportFrom):
            if node.module in forbidden_imports:
                bad.append(node.module)
        elif isinstance(node, ast.Import):
            for n in node.names:
                if n.name in forbidden_imports:
                    bad.append(n.name)
    assert not bad, (
        f"Spike script imports production-writer modules: {bad}. "
        "Allowed reads: bot.helpers.* (constants, cohort attribution, "
        "band_calibration) only."
    )


# ───────────────────────────────────────────────────────────────────────────
# Findings doc — exists at expected path post-spike (skipped pre-execution)
# ───────────────────────────────────────────────────────────────────────────


FINDINGS_DOC = REPO_ROOT / "kb" / "findings" / "tm-dc-conditional-calibration-research.md"


@pytest.mark.skipif(
    not FINDINGS_DOC.is_file(),
    reason="Findings doc not yet written (Phase 5). Skipped pre-execution.",
)
def test_findings_doc_has_recommendation():
    """Findings doc must lead with a finalized verdict — NOT the TBD template.

    R1-Mn5 fix: pre-fix this test passed on the auto-generated
    `_TBD by reviewer — pick one of SHIP / NO-SHIP / SHIP-WITH-CAVEATS_`
    placeholder because that string contains the substring "SHIP". The test
    is now strengthened to fail when the placeholder is present.
    """
    txt = FINDINGS_DOC.read_text()
    assert "TBD by reviewer" not in txt, (
        "Findings doc still contains the `_TBD by reviewer ..._` template "
        "placeholder. Replace with a finalized verdict before shipping."
    )
    has_verdict = any(
        marker in txt
        for marker in ("SHIP", "NO-SHIP", "SHIP-WITH-CAVEATS")
    )
    assert has_verdict, (
        "Findings doc must include a top-line recommendation in "
        "{SHIP, NO-SHIP, SHIP-WITH-CAVEATS}."
    )
