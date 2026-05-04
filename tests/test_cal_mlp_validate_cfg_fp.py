"""validate.py — `--allow-cfg-fp-mismatch` flag tests.

Per `kb/decisions/v2-cal-mlp-deploy-runbook-may03.md`, the v2 ablation
compares two bundles whose `cfg_fp` differs only by the new
`provenance_filter` value (live_only vs full_dataset). validate.py's
default cfg_fp guard refuses cross-cfg_fp comparison; this flag mirrors
the existing `--allow-alpha-mismatch` escape hatch.

Mirrors pattern from `--allow-alpha-mismatch` (validate.py:352, 429-433).
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "scripts" / "cal_mlp"))


# ── _check_cfg_fp_compat helper ────────────────────────────────────────


class TestCheckCfgFpCompat:
    """The cfg_fp guard, extracted into a unit-testable helper."""

    @pytest.fixture(autouse=True)
    def _require_torch(self):
        # validate.py imports torch at module load; skip in CI without torch.
        pytest.importorskip("torch")
        pytest.importorskip("pandas")

    def test_matching_cfg_fp_passes(self):
        from validate import _check_cfg_fp_compat

        # No raise expected.
        _check_cfg_fp_compat(
            base_cfg_fp="abc123",
            challenger_cfg_fp="abc123",
            allow_mismatch=False,
        )

    def test_mismatched_cfg_fp_raises_by_default(self):
        from validate import _check_cfg_fp_compat

        with pytest.raises(SystemExit) as exc_info:
            _check_cfg_fp_compat(
                base_cfg_fp="abc123",
                challenger_cfg_fp="def456",
                allow_mismatch=False,
            )
        assert "cfg_fp mismatch" in str(exc_info.value).lower()

    def test_mismatched_cfg_fp_with_flag_passes(self):
        from validate import _check_cfg_fp_compat

        # No raise expected — operator explicitly opted in.
        _check_cfg_fp_compat(
            base_cfg_fp="abc123",
            challenger_cfg_fp="def456",
            allow_mismatch=True,
        )

    def test_matching_cfg_fp_with_flag_still_passes(self):
        """The flag should be a no-op when cfg_fps already match."""
        from validate import _check_cfg_fp_compat

        _check_cfg_fp_compat(
            base_cfg_fp="abc123",
            challenger_cfg_fp="abc123",
            allow_mismatch=True,
        )


# ── method_output construction (single source of truth) ───────────────


class TestComputeMethodOutputHelper:
    """`sim_pnl.compute_method_output(df)` is the canonical "production
    output baseline" formula (calibrated_prob.fillna(raw_prob)). Both
    Phase-6 call sites — `sim_pnl.run_sim_pnl` and `validate.main` —
    MUST go through this helper so the formula can't drift between
    them. Pre-existing pre-helper bug: validate.py KeyError'd on
    `method_output` because the column wasn't constructed at all.
    Regression locked at the helper level + AST guards on call sites."""

    @pytest.fixture(autouse=True)
    def _require_pandas(self):
        pytest.importorskip("pandas")
        pytest.importorskip("numpy")
        pytest.importorskip("torch")  # sim_pnl.py imports torch at module load

    def test_helper_uses_calibrated_when_present(self):
        import numpy as np
        import pandas as pd
        from sim_pnl import compute_method_output

        df = pd.DataFrame({
            'calibrated_prob': [0.85, 0.70, 0.50],
            'raw_prob':        [0.99, 0.99, 0.99],
        })
        out = compute_method_output(df)
        assert list(out) == pytest.approx([0.85, 0.70, 0.50]), (
            "When calibrated_prob is present, helper must use it (NOT "
            "fall back to raw_prob)"
        )

    def test_helper_falls_back_to_raw_when_calibrated_null(self):
        import numpy as np
        import pandas as pd
        from sim_pnl import compute_method_output

        df = pd.DataFrame({
            'calibrated_prob': [None, np.nan, 0.50],
            'raw_prob':        [0.85, 0.70, 0.99],
        })
        out = compute_method_output(df)
        # First two rows: calibrated NULL → use raw_prob
        # Third row: calibrated present → use calibrated
        assert list(out) == pytest.approx([0.85, 0.70, 0.50])

    def test_helper_returns_float32(self):
        import pandas as pd
        from sim_pnl import compute_method_output

        df = pd.DataFrame({
            'calibrated_prob': [0.85],
            'raw_prob':        [0.99],
        })
        out = compute_method_output(df)
        assert out.dtype.name == "float32", (
            f"helper must return float32 to match parquet's "
            f"calibrated_prob_audit dtype; got {out.dtype}"
        )

    def test_helper_coerces_string_inputs(self):
        """Defensive: legacy data with stringified probabilities must
        coerce gracefully, not raise."""
        import pandas as pd
        from sim_pnl import compute_method_output

        df = pd.DataFrame({
            'calibrated_prob': ["0.85", "garbage", None],
            'raw_prob':        ["0.99", "0.70",    "0.50"],
        })
        out = compute_method_output(df)
        # 0.85 (cal valid), 0.70 (cal coerced to NaN → raw), 0.50 (cal NaN → raw)
        assert list(out) == pytest.approx([0.85, 0.70, 0.50])


class TestBothCallSitesUseHelper:
    """AST-level guard: if either sim_pnl.py or validate.py inlines the
    formula instead of calling the helper, drift between them becomes
    possible (which was exactly the failure mode pre-fix).

    Uses `ast.parse` + walks the tree for an `Assign` to a Subscript
    indexed by 'method_output' whose RHS is a `Call` to `compute_method_output`.
    Substring matches alone are too loose — a comment mentioning the
    helper would falsely satisfy them."""

    @pytest.fixture(autouse=True)
    def _require_pandas(self):
        pytest.importorskip("pandas")
        pytest.importorskip("torch")

    def _find_method_output_assignments(self, src: str) -> list:
        """Return list of (df_name, rhs_func_name) tuples for every
        `<df>['method_output'] = <call>` assignment in src."""
        import ast
        tree = ast.parse(src)
        results = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Assign):
                continue
            if len(node.targets) != 1:
                continue
            tgt = node.targets[0]
            if not isinstance(tgt, ast.Subscript):
                continue
            # Subscript value: the df name (e.g., test_df)
            if not isinstance(tgt.value, ast.Name):
                continue
            df_name = tgt.value.id
            # Subscript index: must be the literal 'method_output'
            slice_node = tgt.slice
            # Python 3.9+ uses ast.Constant; older uses ast.Index wrapping Constant
            if isinstance(slice_node, ast.Constant):
                key = slice_node.value
            elif hasattr(ast, 'Index') and isinstance(slice_node, ast.Index) \
                    and isinstance(slice_node.value, ast.Constant):
                key = slice_node.value.value
            else:
                continue
            if key != 'method_output':
                continue
            # RHS must be a Call to compute_method_output
            rhs = node.value
            if isinstance(rhs, ast.Call) and isinstance(rhs.func, ast.Name):
                results.append((df_name, rhs.func.id))
        return results

    def test_validate_py_assigns_method_output_via_helper(self):
        validate_path = (
            PROJECT_ROOT / "scripts" / "cal_mlp" / "validate.py"
        )
        assigns = self._find_method_output_assignments(validate_path.read_text())
        # Must be at least one such assignment, and it must call compute_method_output.
        assert assigns, (
            "validate.py must contain an assignment "
            "`<df>['method_output'] = compute_method_output(...)` before "
            "per_band_brier (line ~599); otherwise validate.py KeyErrors "
            "on every real run (regression of pre-fix bug)."
        )
        helpers_used = {h for (_df, h) in assigns}
        assert "compute_method_output" in helpers_used, (
            f"validate.py must use compute_method_output helper, not an "
            f"inlined formula; got call sites using: {helpers_used}"
        )

    def test_sim_pnl_py_assigns_method_output_via_helper(self):
        sim_pnl_path = (
            PROJECT_ROOT / "scripts" / "cal_mlp" / "sim_pnl.py"
        )
        assigns = self._find_method_output_assignments(sim_pnl_path.read_text())
        assert assigns, (
            "sim_pnl.py must contain an assignment "
            "`<df>['method_output'] = compute_method_output(...)`."
        )
        helpers_used = {h for (_df, h) in assigns}
        assert "compute_method_output" in helpers_used, (
            f"sim_pnl.py must call compute_method_output (its own helper), "
            f"not an inlined formula; got: {helpers_used}"
        )

    def test_validate_py_imports_helper(self):
        """Belt-and-suspenders: ensure the import is present so the AST
        helper-call test can resolve."""
        validate_path = (
            PROJECT_ROOT / "scripts" / "cal_mlp" / "validate.py"
        )
        src = validate_path.read_text()
        assert "from sim_pnl import" in src and "compute_method_output" in src, (
            "validate.py must `from sim_pnl import compute_method_output`"
        )


# ── Audit-trail persistence ────────────────────────────────────────────


class TestAuditTrailIncludesFlag:
    """Per adversarial review P1: the override flag MUST be persisted in
    the audit JSON alongside `allow_alpha_mismatch` and
    `allow_shipblocker_fail`. Operators reviewing reports later need a
    durable record that the cfg_fp guard was bypassed; the
    logging.warning is ephemeral and the cfg_fp values alone don't tell
    the audit reader whether the override was deliberate."""

    def test_audit_dict_construction_includes_allow_cfg_fp_mismatch(self):
        """AST-style guard: assert that the validate.py source contains
        the audit-dict entry for `allow_cfg_fp_mismatch`. Catches future
        regressions where someone adds a new override flag but forgets
        to persist it."""
        validate_path = (
            PROJECT_ROOT / "scripts" / "cal_mlp" / "validate.py"
        )
        src = validate_path.read_text()
        assert "'allow_cfg_fp_mismatch':" in src, (
            "validate.py audit dict must persist allow_cfg_fp_mismatch "
            "for forensic review (mirrors allow_alpha_mismatch / "
            "allow_shipblocker_fail). Missing entry breaks audit-trail "
            "integrity per adversarial review round 1 P1."
        )
        # Belt-and-suspenders: confirm it's specifically in the audit-dict
        # construction context, not e.g. a comment or test fixture string.
        assert "'allow_cfg_fp_mismatch': bool(args.allow_cfg_fp_mismatch)" in src


# ── CLI flag wiring ────────────────────────────────────────────────────


class TestCliFlagSurface:
    """validate.py exposes --allow-cfg-fp-mismatch matching the
    --allow-alpha-mismatch precedent."""

    @pytest.fixture(autouse=True)
    def _require_torch(self):
        pytest.importorskip("torch")
        pytest.importorskip("pandas")

    def test_cli_help_advertises_flag(self):
        """Smoke: `validate.py --help` mentions the new flag so operators
        can discover it the standard way."""
        import subprocess

        result = subprocess.run(
            [
                "venv/bin/python3", "scripts/cal_mlp/validate.py", "--help",
            ],
            cwd=str(PROJECT_ROOT),
            capture_output=True,
            text=True,
            timeout=30,
        )
        # argparse exits 0 on --help.
        assert result.returncode == 0, (
            f"--help exited {result.returncode}; stderr={result.stderr[:200]}"
        )
        assert "--allow-cfg-fp-mismatch" in result.stdout, (
            "validate.py --help must advertise --allow-cfg-fp-mismatch "
            f"(matches --allow-alpha-mismatch); got:\n{result.stdout[-500:]}"
        )
