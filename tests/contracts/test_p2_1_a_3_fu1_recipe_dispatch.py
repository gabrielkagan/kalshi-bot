"""Phase 2 P2.1.a-3-fu1 — recipe_namespace dispatch contract (ClickUp 86b9xbd2u).

P2.1.a-3 produced bundles in two distinct recipe namespaces:
  - production v1.1 (BTC/ETH/SOL/XRP, cfg_fp 345978797274721f / ablation
    1969b12c6c0c39bf, 8 CONT_FEATURE_COLS)
  - replay_v1     (HYPE/DOGE/BNB — BNB added Bit F 86ba1wpck 2026-05-21,
    cfg_fp ea9c30477f844afa (pre-Bit-F: 9347942aaba71146), 4
    CONT_FEATURE_COLS_REPLAY)

train.py/validate.py/conformal.py today hardcode `--asset` choices to
`['BTC','ETH','SOL','XRP']` and import the production `CONT_FEATURE_COLS`
at module-top. Without dispatch, training/validation on HYPE/DOGE replay
bundles is structurally blocked — argparse rejects the asset, and even
if we hand-bypassed that, the replay parquets lack production columns
(market_price, prob_breakeven_gap) so Phase4Dataset construction would
KeyError.

This Bit adds a single dispatch surface: `features.resolve_recipe(ns)`.
Bundles produced by `extract_data_replay.py` stamp
`recipe_namespace='replay_v1'`; pre-P2.1.a-3 production bundles do NOT
stamp the field (back-compat default = 'v1.1_production'). train.py /
validate.py / conformal.py:
  1.  Widen `--asset` choices to the 6-asset set.
  2.  Read `ext_bundle.get('recipe_namespace', 'v1.1_production')` and
      route through `resolve_recipe(...)` to pick the right CONT_FEATURE_COLS
      / CONT_FEATURE_TRANSFORMS / MISSING_INDICATOR_COLS / ASSET_FLOORS.
  3.  Hard-fail when `asset not in recipe.asset_floors` (e.g.,
      `--asset HYPE` paired with a production-namespace bundle, or
      `--asset BTC` paired with a replay-namespace bundle).

Sister-anchor to `test_p2_1_a_3_corpus_snapshots.py` (which pins the
producer side); this file pins the consumer side so producer + consumer
agree on the dispatch contract.
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
CAL_MLP = REPO_ROOT / "scripts" / "cal_mlp"

TRAIN_PY = CAL_MLP / "train.py"
VALIDATE_PY = CAL_MLP / "validate.py"
CONFORMAL_PY = CAL_MLP / "conformal.py"
FEATURES_PY = CAL_MLP / "features.py"


SIX_ASSETS = frozenset({"BTC", "ETH", "SOL", "XRP", "HYPE", "DOGE"})
PRODUCTION_ASSETS = frozenset({"BTC", "ETH", "SOL", "XRP"})
# Bit F (2026-05-21, ticket 86ba1wpck) widened replay recipe to include BNB
# (ASSET_FLOORS_REPLAY['BNB']=75). The replay-namespace anchor must mirror.
REPLAY_ASSETS = frozenset({"HYPE", "DOGE", "BNB"})

NAMESPACE_PRODUCTION = "v1.1_production"
NAMESPACE_REPLAY = "replay_v1"


def _import_features():
    """Lazy import of scripts/cal_mlp/features so collection survives an
    unwritten helper (yields a clean RED with a meaningful assert)."""
    if str(CAL_MLP) not in sys.path:
        sys.path.insert(0, str(CAL_MLP))
    import features  # noqa: WPS433
    return features


def _argparse_asset_choices(script_path: Path) -> set[str] | None:
    """AST-walk a script and return the literal `choices=[...]` set passed
    to `add_argument('--asset', ...)`. Returns None if the script can't be
    parsed or the argument isn't found — caller decides how to fail.

    Why AST not import: the scripts have heavy import-time side effects
    (`torch`, `psutil`, `pandas`) and acquire flock; importing them in a
    test process is fragile. AST inspection is side-effect-free."""
    if not script_path.exists():
        return None
    tree = ast.parse(script_path.read_text())
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        # Match `.add_argument('--asset', ...)`.
        if not (isinstance(node.func, ast.Attribute)
                and node.func.attr == "add_argument"):
            continue
        if not node.args:
            continue
        first = node.args[0]
        if not (isinstance(first, ast.Constant) and first.value == "--asset"):
            continue
        for kw in node.keywords:
            if kw.arg != "choices":
                continue
            # choices=['BTC',...] OR choices=list(CONSTANT) OR
            # choices=(CONSTANT,).
            if isinstance(kw.value, (ast.List, ast.Tuple, ast.Set)):
                vals = set()
                for elt in kw.value.elts:
                    if isinstance(elt, ast.Constant) and isinstance(elt.value, str):
                        vals.add(elt.value)
                return vals
            # choices=list(CONSTANT) form (e.g., extract_data_replay.py
            # uses `choices=list(REPLAY_ASSET_CHOICES)`). Resolve by name.
            if isinstance(kw.value, ast.Call):
                # Best-effort: read the call target name; tests will
                # bypass this branch by checking the named constant
                # instead. Return None to signal "needs higher-level
                # check".
                return None
    return None


# ─────────────────────────────────────────────────────────────────────
# Anchor 1-5: resolve_recipe helper shape + namespace dispatch
# ─────────────────────────────────────────────────────────────────────

def test_resolve_recipe_helper_exists():
    """Anchor 1: features.resolve_recipe is the single dispatch entry. Without
    it, train.py / validate.py have no centralized place to pick recipe
    constants per bundle's recipe_namespace."""
    features = _import_features()
    assert hasattr(features, "resolve_recipe"), (
        "features.py must export resolve_recipe(namespace) — the single "
        "source of truth for routing a bundle's recipe_namespace to its "
        "CONT_FEATURE_COLS / CONT_FEATURE_TRANSFORMS / MISSING_INDICATOR_COLS "
        "/ ASSET_FLOORS quartet. See ClickUp 86b9xbd2u for design."
    )
    assert callable(features.resolve_recipe), "resolve_recipe must be callable"


def test_resolve_recipe_production_namespace():
    """Anchor 2: production namespace resolves to v1.1 CONT_FEATURE_COLS
    (8 features including market_price + prob_breakeven_gap) and to
    production ASSET_FLOORS (BTC/ETH/SOL/XRP)."""
    features = _import_features()
    recipe = features.resolve_recipe(NAMESPACE_PRODUCTION)
    # Recipe must expose the four routing fields explicitly. Tuple-style
    # access keeps callers free to unpack OR namespace-access.
    cont_cols = tuple(recipe.cont_feature_cols)
    assert cont_cols == tuple(features.CONT_FEATURE_COLS), (
        f"production recipe cont_feature_cols drift: {cont_cols} vs "
        f"{tuple(features.CONT_FEATURE_COLS)}"
    )
    # Sanity: v1.1 includes market_price + prob_breakeven_gap (the two
    # features that don't exist in replay corpora).
    assert "market_price" in cont_cols
    assert "prob_breakeven_gap" in cont_cols
    # Asset floors contract:
    # - PRE-Bit-C (86ba0jn2b, 2026-05-19): exactly == PRODUCTION_ASSETS (the
    #   CORE 4 baked into cfg_fp).
    # - POST-Bit-C: SUPERSET of PRODUCTION_ASSETS, because the recipe now
    #   merges ASSET_FLOORS (CORE) with ASSET_FLOORS_EXT (extension list
    #   for HYPE/DOGE + future Kalshi crypto rollouts). The contract is
    #   now `PRODUCTION_ASSETS ⊆ recipe.asset_floors`. The cfg_fp identity
    #   (`345978797274721f`) is the load-bearing invariant — pinned by
    #   `tests/contracts/test_asset_floors_ext_extensibility.py`.
    assert PRODUCTION_ASSETS <= set(recipe.asset_floors.keys()), (
        f"production recipe must include CORE {PRODUCTION_ASSETS}; "
        f"missing: {PRODUCTION_ASSETS - set(recipe.asset_floors.keys())}"
    )
    # Recipe namespace field round-trips.
    assert recipe.namespace == NAMESPACE_PRODUCTION


def test_resolve_recipe_replay_namespace():
    """Anchor 3: replay_v1 namespace resolves to CONT_FEATURE_COLS_REPLAY
    (4 features, NO market_price, NO prob_breakeven_gap, NO seconds_to_close,
    NO time_decayed_proximity) and to ASSET_FLOORS_REPLAY (HYPE/DOGE/BNB —
    BNB added Bit F `86ba1wpck` 2026-05-21).

    Excluding these production-recipe features is load-bearing — see
    `features.py` CONT_FEATURE_COLS_REPLAY comment (zero-variance stc in
    replay corpus, no historical Kalshi orderbook for prob_breakeven_gap,
    no market_price in replay schema)."""
    features = _import_features()
    recipe = features.resolve_recipe(NAMESPACE_REPLAY)
    cont_cols = tuple(recipe.cont_feature_cols)
    assert cont_cols == tuple(features.CONT_FEATURE_COLS_REPLAY), (
        f"replay recipe cont_feature_cols drift: {cont_cols} vs "
        f"{tuple(features.CONT_FEATURE_COLS_REPLAY)}"
    )
    # Load-bearing exclusions (RCA F1 from P2.1.a-3 session resume + the
    # `Methodology gotchas` section of crypto_replay_backfill.py).
    for excluded in ("market_price", "prob_breakeven_gap",
                     "seconds_to_close", "time_decayed_proximity"):
        assert excluded not in cont_cols, (
            f"replay recipe must NOT include {excluded} — see features.py "
            f"CONT_FEATURE_COLS_REPLAY commentary on why each is dropped."
        )
    # Asset floors are the HYPE/DOGE pair.
    assert set(recipe.asset_floors.keys()) == REPLAY_ASSETS, (
        f"replay recipe asset_floors must be exactly {REPLAY_ASSETS}, "
        f"got {set(recipe.asset_floors.keys())}"
    )
    assert recipe.namespace == NAMESPACE_REPLAY


def test_resolve_recipe_unknown_namespace_raises():
    """Anchor 4: unknown namespace must raise ValueError loudly. A silent
    fallback to production would let a typo'd bundle silently mis-train."""
    features = _import_features()
    with pytest.raises(ValueError, match=r"recipe_namespace|namespace|unknown"):
        features.resolve_recipe("v9_nonexistent_recipe")


def test_resolve_recipe_default_back_compat():
    """Anchor 5: pre-P2.1.a-3 production bundles do NOT stamp the
    `recipe_namespace` field. The dispatch must default to production
    when the field is absent — verified by passing None OR by passing the
    sentinel 'v1.1_production' explicitly. Either route MUST resolve
    identically to spare callers from sprinkling defaults at each call site.

    The actual call shape in train.py / validate.py is:
        recipe = resolve_recipe(ext_bundle.get('recipe_namespace'))
    (no default arg — `dict.get` returns None on absent key) so None is
    the canonical 'absent' signal — accept it as a synonym for the
    production default. `resolve_recipe('v1.1_production')` and
    `resolve_recipe(None)` MUST resolve identically."""
    features = _import_features()
    explicit = features.resolve_recipe(NAMESPACE_PRODUCTION)
    via_none = features.resolve_recipe(None)
    assert tuple(via_none.cont_feature_cols) == tuple(explicit.cont_feature_cols)
    assert set(via_none.asset_floors.keys()) == set(explicit.asset_floors.keys())
    assert via_none.namespace == NAMESPACE_PRODUCTION


# ─────────────────────────────────────────────────────────────────────
# Anchor 6: recipe quartet exposes transforms + missing-indicator fields
# ─────────────────────────────────────────────────────────────────────

def test_resolve_recipe_exposes_transforms_and_missing():
    """Anchor 6: recipe quartet MUST expose `cont_feature_transforms` and
    `missing_indicator_cols` alongside `cont_feature_cols` — train.py reads
    all three to construct Phase4Dataset + apply_norm + model_def. Missing
    either field forces callers back to module-globals (the dispatch
    bug we're closing)."""
    features = _import_features()
    for ns in (NAMESPACE_PRODUCTION, NAMESPACE_REPLAY):
        recipe = features.resolve_recipe(ns)
        # transforms dict round-trips: per-col transform names like
        # 'log_cents_to_dollars' / 'identity_no_zscore'.
        assert isinstance(recipe.cont_feature_transforms, dict)
        # Production has 'market_price' under 'log_cents_to_dollars';
        # replay has 'hour_sin' under 'identity_no_zscore'.
        if ns == NAMESPACE_PRODUCTION:
            assert recipe.cont_feature_transforms.get("market_price") == "log_cents_to_dollars"
        else:
            assert recipe.cont_feature_transforms.get("hour_sin") == "identity_no_zscore"
        # missing-indicator cols is a sequence (production v1 = [], replay
        # = []; both empty for now but must be exposed for the dispatch
        # to be uniform across recipes).
        assert hasattr(recipe, "missing_indicator_cols")
        assert isinstance(recipe.missing_indicator_cols, (list, tuple))


# ─────────────────────────────────────────────────────────────────────
# Anchor 7-9: argparse --asset choices widening
# ─────────────────────────────────────────────────────────────────────

def test_train_py_asset_choices_includes_replay_assets():
    """Anchor 7: train.py argparse --asset must accept HYPE + DOGE. Before
    fix, choices=['BTC','ETH','SOL','XRP'] rejected HYPE at parse_args
    and Phase4 training for replay bundles was structurally blocked."""
    choices = _argparse_asset_choices(TRAIN_PY)
    assert choices is not None, "could not parse --asset choices from train.py"
    missing = REPLAY_ASSETS - choices
    assert not missing, (
        f"train.py --asset choices missing {missing}; got {choices}. "
        f"Widen to include HYPE + DOGE for P2.1.b training on replay bundles."
    )
    # Defense-in-depth: original 4-asset set MUST still be accepted.
    assert PRODUCTION_ASSETS <= choices, (
        f"train.py --asset choices regressed on production assets; got {choices}"
    )


def test_validate_py_asset_choices_includes_replay_assets():
    """Anchor 8: validate.py argparse --asset must accept HYPE + DOGE for
    P2.1.c Brier+coverage on replay-namespace bundles. (sim_pnl counterfactual
    skipped via separate runtime guard; see Bit description.)"""
    choices = _argparse_asset_choices(VALIDATE_PY)
    assert choices is not None, "could not parse --asset choices from validate.py"
    missing = REPLAY_ASSETS - choices
    assert not missing, (
        f"validate.py --asset choices missing {missing}; got {choices}."
    )
    assert PRODUCTION_ASSETS <= choices


def test_conformal_py_asset_choices_includes_replay_assets():
    """Anchor 9: conformal.py (Phase 5) argparse --asset must accept HYPE
    + DOGE so a P2.1.b-trained HYPE/DOGE bundle can advance to Phase 5
    conformal-fit on its way to P2.1.c validate."""
    choices = _argparse_asset_choices(CONFORMAL_PY)
    assert choices is not None, "could not parse --asset choices from conformal.py"
    missing = REPLAY_ASSETS - choices
    assert not missing, (
        f"conformal.py --asset choices missing {missing}; got {choices}."
    )
    assert PRODUCTION_ASSETS <= choices


# ─────────────────────────────────────────────────────────────────────
# Anchor 10: train.py uses resolve_recipe (consumer wiring)
# ─────────────────────────────────────────────────────────────────────

def test_train_py_imports_resolve_recipe():
    """Anchor 10: train.py must reference `resolve_recipe` (the dispatch
    entry). Belt-and-braces companion to anchors 7-9 — choices-widening
    alone is insufficient without the actual recipe routing. Without
    `resolve_recipe`, train.py would still construct Phase4Dataset with
    module-global CONT_FEATURE_COLS regardless of bundle namespace."""
    if not TRAIN_PY.exists():
        pytest.skip("train.py not found")
    src = TRAIN_PY.read_text()
    assert "resolve_recipe" in src, (
        "train.py does not reference features.resolve_recipe — recipe "
        "dispatch is the entire point of this Bit. Without it, replay "
        "bundles silently route through production CONT_FEATURE_COLS."
    )


def test_validate_py_imports_resolve_recipe():
    """Anchor 11: validate.py must reference `resolve_recipe`. Sister to
    anchor 10 for the Phase 6 read path."""
    if not VALIDATE_PY.exists():
        pytest.skip("validate.py not found")
    src = VALIDATE_PY.read_text()
    assert "resolve_recipe" in src, (
        "validate.py does not reference features.resolve_recipe — without "
        "it, apply_norm on replay test_df would attempt to z-score absent "
        "columns (market_price, prob_breakeven_gap) and KeyError."
    )


# ─────────────────────────────────────────────────────────────────────
# Anchor 12: validate.py sim_pnl-skip path for replay bundles
# ─────────────────────────────────────────────────────────────────────

def test_validate_py_sim_pnl_skip_marker_for_replay():
    """Anchor 12: validate.py for replay-namespace bundles must SKIP the
    sim_pnl counterfactual replay block (replay corpus rows aren't in
    `state.db::evaluated_opportunities`; sim_pnl SQL would return 0 rows
    and silently emit a zero-PnL audit). The skip must surface a
    documented marker rather than silently emitting an empty result.

    AST-level check: validate.py source mentions BOTH `sim_pnl_skipped`
    (the marker key) AND a guard on `recipe.namespace == 'replay_v1'` or
    equivalent. Caught at contract gate so a future refactor can't drop
    the guard."""
    if not VALIDATE_PY.exists():
        pytest.skip("validate.py not found")
    src = VALIDATE_PY.read_text()
    assert "sim_pnl_skipped" in src, (
        "validate.py must emit a `sim_pnl_skipped` marker for replay-namespace "
        "bundles. Without it, replay validation runs would silently produce "
        "empty sim_pnl audit blocks (0 trades, 0 PnL) and look like 'sim_pnl "
        "ran but found nothing to trade' — misleading."
    )
    # The guard must reference the replay namespace — either the literal
    # string 'replay_v1' or the canonical `REPLAY_RECIPE_NAMESPACE` const
    # (validate.py imports the const from features.py for type safety;
    # both flavors are valid recipe-gated checks). The assertion proves
    # the skip is recipe-gated, not unconditional (which would break
    # production-asset validation).
    recipe_guarded = (
        NAMESPACE_REPLAY in src
        or "REPLAY_RECIPE_NAMESPACE" in src
    )
    assert recipe_guarded, (
        f"validate.py must check `recipe.namespace == {NAMESPACE_REPLAY!r}` "
        f"(or against the canonical REPLAY_RECIPE_NAMESPACE constant) "
        f"before skipping sim_pnl. Anchor 12 catches an unconditional skip."
    )


# ─────────────────────────────────────────────────────────────────────
# Anchor 13: markdown report skip-branch for sim_pnl sections
# ─────────────────────────────────────────────────────────────────────

def test_validate_py_report_md_skips_sim_pnl_sections_for_replay():
    """Anchor 13 (R1 C1): the operator-facing markdown report
    (`_write_report_md`) must skip sections §3 (Sim PnL summary) and §4
    (Drawdown) for replay-namespace bundles. Without this branch, the
    raw `sp.get(...)` defaults would render literal '$0.00 Pessimistic /
    $0.00 Modeled / 0.000 risk drop / hwm=unknown' — a false 'sim_pnl
    ran and found nothing' signal in the on-disk report.

    AST-level check: validate.py's `_write_report_md` body must mention
    `sim_pnl_skipped` (the marker key) AND emit a `_skipped_` placeholder
    line. The skip branch must be co-located with the sections it
    guards (rather than just suppressing the entire audit), so operators
    see the section header + 'skipped' explanation and know Brier +
    coverage are still authoritative.

    Caught at contract gate so a future refactor of the report writer
    can't drop the guard and silently regress to zero-emitting output."""
    if not VALIDATE_PY.exists():
        pytest.skip("validate.py not found")
    src = VALIDATE_PY.read_text()
    # The `_skipped_` placeholder convention surfaces the skip in the
    # markdown report. Two occurrences required — one per section
    # (§3 Sim PnL + §4 Drawdown) — so a future refactor that drops the
    # §4 branch and leaves §3 alone still trips this guard. (R2 MN1.)
    n_skipped_markers = src.count("_skipped_")
    assert n_skipped_markers >= 2, (
        f"validate.py `_write_report_md` must emit a `_skipped_` "
        f"placeholder line in BOTH §3 (Sim PnL) AND §4 (Drawdown) when "
        f"the audit's sim_pnl block carries the `sim_pnl_skipped` "
        f"marker — found {n_skipped_markers} occurrence(s) of "
        f"`_skipped_`. Without both branches, dropping the §4 guard "
        f"would still leave `worst_7d_drawdown_ratio: 1.00 / "
        f"hwm_init_source: unknown` rendering for replay bundles."
    )
    # Belt-and-braces: the skip branch must be co-located with the
    # marker key check (not e.g., suppressing the whole audit), so
    # operators see the skip rationale next to the section header.
    assert "sim_pnl_skipped" in src.split("## 7. Provenance")[0], (
        "`sim_pnl_skipped` marker check must appear in the report-write "
        "section (before `## 7. Provenance`), not just in the soft-flags "
        "block — otherwise §3/§4 render the false zeros and only the "
        "soft-flags list mentions the skip."
    )
