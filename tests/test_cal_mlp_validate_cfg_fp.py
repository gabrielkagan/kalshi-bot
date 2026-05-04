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
