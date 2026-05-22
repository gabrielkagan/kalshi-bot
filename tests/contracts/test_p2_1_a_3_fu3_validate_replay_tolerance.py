"""Phase 2 P2.1.a-3-fu3 — validate.py::empirical_coverage replay-namespace
tolerance for HYPE/DOGE (ClickUp 86b9xe3ku).

P2.1.a-3-fu2 (86b9xd9hn, SHIPPED b566ae9) closed the
Phase4Dataset/preds_df/CalibrationDataset categorical-FE dispatch surface
via `RecipeSpec.categorical_feature_cols`. Secondary gap remained at the
Phase 5 coverage audit step:

  scripts/cal_mlp/validate.py::empirical_coverage iterates rows of the
  replay-namespace test_df and reads `row['price_tier']`,
  `row['vol_regime_int']`, `row['market_price']`, `row['side']` directly.
  Replay parquets (extract_data_replay.py) structurally lack all four
  columns (no `market_price` orderbook → no PRICE_BIN_CUTOFFS digitize;
  no vol regime feed for HYPE/DOGE; `side_int=1` hardcoded YES). Result:
  `python -m scripts.cal_mlp.validate --asset HYPE` KeyErrors at the
  empirical_coverage per-row loop after Phase 4 prediction succeeds.

fu3 (Option A — minimal patch, no cfg_fp_replay bump): add a keyword-
only `recipe` parameter to `empirical_coverage`. When `recipe.namespace
== REPLAY_RECIPE_NAMESPACE`, default the absent categoricals to 0
(matching Phase4Dataset's int64 zero defaults per fu2) and short-circuit
the breakeven inputs to 50¢ YES (neutral; replay should run with
market_blend_w=0 in any case since there's no orderbook).

Sister anchors:
  - `tests/integration/test_cal_mlp_validate_empirical_coverage.py` —
    production-schema regression (T-A1/T-A2 from the entry_price_cents
    bug; preserved unchanged by fu3).
  - `tests/contracts/test_p2_1_a_3_fu2_categorical_dispatch.py` —
    upstream Phase4Dataset/preds_df/CalibrationDataset categorical-FE
    dispatch (this file's prerequisite).
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
CAL_MLP = REPO_ROOT / "scripts" / "cal_mlp"
VALIDATE_PY = CAL_MLP / "validate.py"

NAMESPACE_PRODUCTION = "v1.1_production"
NAMESPACE_REPLAY = "replay_v1"


def _import_validate():
    """Lazy-import scripts/cal_mlp/validate.py. validate's module-level
    imports include torch; tests skip when torch isn't available."""
    pytest.importorskip("torch")
    pytest.importorskip("pandas")
    pytest.importorskip("numpy")
    if str(CAL_MLP) not in sys.path:
        sys.path.insert(0, str(CAL_MLP))
    import validate as validate_mod  # noqa: WPS433
    return validate_mod


def _import_features():
    pytest.importorskip("numpy")
    if str(CAL_MLP) not in sys.path:
        sys.path.insert(0, str(CAL_MLP))
    import features  # noqa: WPS433
    return features


def _empty_artifact() -> dict:
    """Minimal conformal_artifact that forces every row down the
    dispatch_miss path (q_alpha=None → final_lo=None → continue). Lets
    the test isolate the row[...] arg-eval code path without needing
    a real cells/quantiles table."""
    return {
        'cells': [],
        'merged_axes': [],
        'bleed_collapsed_by_merge': False,
        'bleed_collapsed_by_merge_per_vr': {},
        'bleed_fallback_quantiles': {},
    }


# ── Functional regression ──────────────────────────────────────────────


class TestEmpiricalCoverageReplaySchemaTolerance:
    """fu3 anchor 1: empirical_coverage must accept a replay-shape
    DataFrame (no price_tier / vol_regime_int / market_price / side) when
    called with `recipe=resolve_recipe(REPLAY_RECIPE_NAMESPACE)`."""

    def _make_replay_row(self, stc_bucket: int = 0) -> dict:
        """Build one row mirroring the empirical_coverage-relevant subset
        of extract_data_replay.build_feature_frame's output (+ the columns
        added inside validate.main before empirical_coverage is called:
        p_pred, p_std). The replay parquet also writes
        seconds_to_close/spot_distance/hour_sin/hour_cos/method_output_raw/
        logit_raw_prob_clipped/calibrated_prob_audit/result_yes_int —
        none of those are read by empirical_coverage, so they're omitted
        from the fixture for clarity.

        Present in the fixture (the load-bearing reads — structural
        anchors to dodge line-cite drift per the long-arc lesson):
          - `stc_bucket` (`build_feature_frame` hardcodes
            `df['stc_bucket'] = np.int8(3)`; always 3 in production
            replay since evaluation_time == open_time → stc=900s, but the
            test exercises multiple buckets to prove the row-iter doesn't
            crash).
          - `outcome` (`build_feature_frame` derives from `result` per
            its YES-side label convention; bot-side label).
          - `side_int` (`build_feature_frame` hardcodes
            `df['side_int'] = np.int8(1)`; always 1 per "always YES
            side in replay").
          - `p_pred`, `p_std` added by validate.main's predictor pass.

        Notably ABSENT (the 4 columns fu3 must tolerate):
          - `price_tier` (no market_price to digitize per
            `build_feature_frame`'s "No price_tier (no market_price
            column..." comment block).
          - `vol_regime_int` (no vol regime feed for any replay-recipe
            asset — HYPE/DOGE/BNB; BNB added Bit F `86ba1wpck`
            2026-05-21).
          - `market_price` (no orderbook in replay).
          - `side` string column (only `side_int`, hardcoded 1).
        """
        return {
            'stc_bucket': int(stc_bucket),
            'side_int': 1,
            'outcome': 1.0,
            'p_pred': 0.85,
            'p_std': 0.05,
        }

    def test_empirical_coverage_does_not_keyerror_on_replay_schema(self):
        """Pre-fix: calling empirical_coverage on a replay-shape df raises
        KeyError: 'price_tier' (or 'vol_regime_int' / 'market_price' /
        'side', depending on column order in the row dict). Post-fix:
        returns cleanly with all rows in dispatch_miss when the caller
        passes recipe=resolve_recipe(REPLAY_RECIPE_NAMESPACE)."""
        import pandas as pd

        validate_mod = _import_validate()
        features = _import_features()

        df = pd.DataFrame([
            self._make_replay_row(stc_bucket=0),
            self._make_replay_row(stc_bucket=1),
            self._make_replay_row(stc_bucket=3),
        ])
        # Replay-schema contract: ALL four production-only columns absent.
        for absent in ('price_tier', 'vol_regime_int', 'market_price', 'side'):
            assert absent not in df.columns, (
                f"replay-schema test_df must NOT contain {absent!r}; this "
                f"fixture mirrors extract_data_replay.build_feature_frame "
                f"which only writes stc_bucket/outcome/side_int (+ p_pred/"
                f"p_std added by validate.main)."
            )

        replay_recipe = features.resolve_recipe(features.REPLAY_RECIPE_NAMESPACE)
        assert replay_recipe.namespace == NAMESPACE_REPLAY

        # Pre-fix: KeyError on first iter; post-fix: clean dispatch_miss.
        cell_audit, cov_summary = validate_mod.empirical_coverage(
            df, _empty_artifact(), market_blend_w=0.0, recipe=replay_recipe,
        )

        assert cov_summary['n_total_test_rows'] == 3
        assert cov_summary['n_eval_cells'] == 0
        assert cov_summary['n_total_dispatch_miss'] == 3
        assert cell_audit == []

    def test_empirical_coverage_replay_with_nonzero_blend_w_raises(self):
        """fu3 R1 M2: replay-mode with market_blend_w != 0 silently distorts
        p_center via the neutral-50¢ breakeven default. The function must
        hard-fail at entry rather than emit subtly-wrong coverage stats.

        Production-resolution stack at validate.py:551-567 can pick up a
        non-zero blend_w for HYPE/DOGE via MARKET_CONFIGS['15m']
        .market_blend_w fallback even without --override-market-blend-w —
        so "operator should set it to 0" is honor-system; the function
        enforces it instead.
        """
        import pandas as pd

        validate_mod = _import_validate()
        features = _import_features()

        df = pd.DataFrame([self._make_replay_row(stc_bucket=0)])
        replay_recipe = features.resolve_recipe(features.REPLAY_RECIPE_NAMESPACE)

        # market_blend_w=0.40 is the production scalar fallback for
        # assets absent from MARKET_BLEND_W_BY_ASSET (HYPE/DOGE per
        # P2.1.d ship-resume). This call SHOULD raise; the patched
        # function refuses the combination loudly rather than producing
        # a structurally-wrong p_center.
        with pytest.raises(SystemExit, match="market_blend_w"):
            validate_mod.empirical_coverage(
                df, _empty_artifact(), market_blend_w=0.40, recipe=replay_recipe,
            )

    def test_empirical_coverage_replay_with_zero_blend_w_succeeds(self):
        """Pair to the M2 hard-fail test: market_blend_w=0.0 is the ONLY
        legal blend weight on a replay-namespace bundle (no orderbook).
        Lock that the function accepts it. Mirrors the standard-config
        path that validate.main resolves to when the operator passes
        --override-market-blend-w 0."""
        import pandas as pd

        validate_mod = _import_validate()
        features = _import_features()

        df = pd.DataFrame([self._make_replay_row(stc_bucket=0)])
        replay_recipe = features.resolve_recipe(features.REPLAY_RECIPE_NAMESPACE)

        # blend_w=0.0 → neutral breakeven cancels, p_center = p_pred.
        cell_audit, cov_summary = validate_mod.empirical_coverage(
            df, _empty_artifact(), market_blend_w=0.0, recipe=replay_recipe,
        )
        assert cov_summary['n_total_test_rows'] == 1
        assert cov_summary['n_total_dispatch_miss'] == 1
        assert cov_summary['n_eval_cells'] == 0
        assert cell_audit == []

    def test_empirical_coverage_replay_recipe_kwarg_is_keyword_only(self):
        """fu3 anchor 2: `recipe` must be keyword-only — positional passing
        would silently shift the meaning of `market_blend_w` on legacy
        callers if the kwarg were positional."""
        validate_mod = _import_validate()
        import inspect

        sig = inspect.signature(validate_mod.empirical_coverage)
        recipe_param = sig.parameters.get('recipe')
        assert recipe_param is not None, (
            "empirical_coverage must declare a `recipe` parameter "
            "(fu3 ClickUp 86b9xe3ku)."
        )
        assert recipe_param.kind == inspect.Parameter.KEYWORD_ONLY, (
            f"`recipe` parameter must be keyword-only (kind="
            f"{recipe_param.kind!r}); positional would alias market_blend_w "
            f"on legacy callers."
        )
        # Default must be None for back-compat (callers that don't know
        # about recipe continue to work — they get production semantics).
        assert recipe_param.default is None, (
            f"`recipe` default must be None for back-compat (got "
            f"{recipe_param.default!r})."
        )


class TestEmpiricalCoverageProductionRecipeNoRegression:
    """fu3 anchor 3: production-shape DataFrame + recipe=production must
    behave identically to the legacy no-recipe call. Locks fu3 against
    accidental regression on the BTC/ETH/SOL/XRP path."""

    def _make_production_row(
        self, price_tier: int = 0, stc_bucket: int = 0, vol_regime_int: int = 0,
    ) -> dict:
        return {
            'price_tier': int(price_tier),
            'stc_bucket': int(stc_bucket),
            'vol_regime_int': int(vol_regime_int),
            'p_pred': 0.85,
            'p_std': 0.05,
            'market_price': 92,
            'side': 'yes',
            'outcome': 1.0,
        }

    def test_production_recipe_matches_no_recipe(self):
        """Calling with recipe=production should be byte-equivalent to
        calling without recipe (the legacy code path)."""
        import pandas as pd

        validate_mod = _import_validate()
        features = _import_features()

        df = pd.DataFrame([
            self._make_production_row(price_tier=0, stc_bucket=0, vol_regime_int=0),
            self._make_production_row(price_tier=1, stc_bucket=1, vol_regime_int=0),
            self._make_production_row(price_tier=3, stc_bucket=2, vol_regime_int=1),
        ])

        # Production recipe (4 cont features + 4 categoricals + 4 missing-
        # indicator cols).
        prod_recipe = features.resolve_recipe(features.RECIPE_NAMESPACE_V1_1_PRODUCTION)
        assert prod_recipe.namespace == NAMESPACE_PRODUCTION

        cell_audit_a, cov_summary_a = validate_mod.empirical_coverage(
            df, _empty_artifact(), market_blend_w=0.0,
        )
        cell_audit_b, cov_summary_b = validate_mod.empirical_coverage(
            df, _empty_artifact(), market_blend_w=0.0, recipe=prod_recipe,
        )

        assert cell_audit_a == cell_audit_b
        assert cov_summary_a == cov_summary_b
        assert cov_summary_b['n_total_test_rows'] == 3
        assert cov_summary_b['n_total_dispatch_miss'] == 3

    def test_production_recipe_still_raises_on_missing_market_price(self):
        """Defensive: if a production-recipe df is missing `market_price`,
        empirical_coverage should still KeyError (don't silently default
        to 50¢ neutral on the production path — that would mask a
        legitimate extraction bug)."""
        import pandas as pd

        validate_mod = _import_validate()
        features = _import_features()

        bad_row = self._make_production_row()
        del bad_row['market_price']
        df = pd.DataFrame([bad_row])

        prod_recipe = features.resolve_recipe(features.RECIPE_NAMESPACE_V1_1_PRODUCTION)

        with pytest.raises(KeyError):
            validate_mod.empirical_coverage(
                df, _empty_artifact(), market_blend_w=0.0, recipe=prod_recipe,
            )


# ── AST anchor on the validate.py call site ────────────────────────────


class TestValidateMainPassesRecipeToEmpiricalCoverage:
    """fu3 anchor 4: validate.main's empirical_coverage call site (line ~678)
    must forward `recipe=recipe`. Without this, my function-signature change
    is dead code on the real pipeline. AST guard locks the wiring at the
    source.
    """

    def _empirical_coverage_callsite_kwargs(self) -> set:
        """Walk validate.py and return the set of kwarg names passed to
        the empirical_coverage call(s) inside the module."""
        src = VALIDATE_PY.read_text()
        tree = ast.parse(src)
        found_kwargs: set = set()
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            # Match bare name `empirical_coverage(...)`.
            if isinstance(func, ast.Name) and func.id == 'empirical_coverage':
                for kw in node.keywords:
                    if kw.arg is not None:
                        found_kwargs.add(kw.arg)
        return found_kwargs

    def test_validate_callsite_passes_recipe_kwarg(self):
        kwargs = self._empirical_coverage_callsite_kwargs()
        assert 'recipe' in kwargs, (
            f"validate.main must call `empirical_coverage(..., recipe=recipe)` "
            f"to forward the loaded RecipeSpec to the row-iter — without this "
            f"forwarding, the keyword-only kwarg added by fu3 is dead code "
            f"on the real validate pipeline. Found call-site kwargs: {kwargs}"
        )

    def test_validate_callsite_recipe_value_is_recipe_identifier_not_literal(self):
        """R1 M3: kwarg presence alone is not enough — a regression like
        `empirical_coverage(..., recipe=None)` (hardcoded None) would
        satisfy the name-only guard but silently defeat the wiring (the
        function defaults to production semantics on recipe=None). Pin
        the value as the local `recipe` identifier."""
        src = VALIDATE_PY.read_text()
        tree = ast.parse(src)
        found_call_count = 0
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if not (isinstance(node.func, ast.Name) and node.func.id == 'empirical_coverage'):
                continue
            found_call_count += 1
            recipe_kw = next(
                (kw for kw in node.keywords if kw.arg == 'recipe'), None,
            )
            assert recipe_kw is not None, (
                f"call site at line {node.lineno} missing recipe= kwarg"
            )
            # Value must be a Name node (= local variable), not a Constant
            # (None / literal) — catches the "passed None for back-compat"
            # silent-regression.
            assert isinstance(recipe_kw.value, ast.Name), (
                f"empirical_coverage(recipe=...) at line {node.lineno} "
                f"must pass a Name (identifier); got "
                f"{type(recipe_kw.value).__name__} "
                f"({ast.dump(recipe_kw.value)}). A hardcoded None or "
                f"literal defeats the fu3 wiring."
            )
            assert recipe_kw.value.id == 'recipe', (
                f"empirical_coverage(recipe=X) at line {node.lineno} — "
                f"X must be the local `recipe` variable (= the "
                f"RecipeSpec loaded at validate.main's resolve_recipe(...) "
                f"site), got {recipe_kw.value.id!r}."
            )
        assert found_call_count >= 1, (
            "no bare-name `empirical_coverage(...)` call sites found in "
            "validate.py — was the function renamed or the call moved to "
            "attribute form? Update this guard to match."
        )
