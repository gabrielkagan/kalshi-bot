"""validate.py — empirical_coverage parquet-schema column tests.

Pre-fix bug: `empirical_coverage` (validate.py:269) referenced
`row['entry_price_cents']`, but the parquet test fold (written by
extract_data.py) uses the canonical Phase 2 column name `market_price`.
The label `entry_price_cents` is sim_pnl.py's *internal rename* of the
SQL-pulled candidate_df — different code path. validate.py's
empirical_coverage operates on the parquet `test_df` (validate.py:558),
so it must read the parquet's column name.

Locked by:
  - functional regression: empirical_coverage on parquet-schema df must
    not KeyError (T-A1)
  - AST guard: validate.py's empirical_coverage body must reference
    `row['market_price']` and not `row['entry_price_cents']` (T-A2)
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "scripts" / "cal_mlp"))


# ── Functional regression ──────────────────────────────────────────────


class TestEmpiricalCoverageParquetSchema:
    """T-A1: feed empirical_coverage a parquet-shaped DataFrame and
    confirm it doesn't KeyError on a wrong column name. Uses a minimal
    conformal_artifact that forces dispatch_miss for every row, so the
    iteration never tries to compute outcome stats — we only need to
    prove the row[...] reads inside the iteration succeed."""

    @pytest.fixture(autouse=True)
    def _require_deps(self):
        pytest.importorskip("pandas")
        pytest.importorskip("numpy")
        pytest.importorskip("torch")  # validate.py imports torch at module load

    def _make_parquet_row(self, price_tier=0, stc_bucket=0, vol_regime_int=0):
        """Build one row matching the canonical parquet schema written by
        extract_data.py + the columns added inside validate.main before
        empirical_coverage is called (p_pred, p_std)."""
        return {
            'price_tier': int(price_tier),
            'stc_bucket': int(stc_bucket),
            'vol_regime_int': int(vol_regime_int),
            # These two come from validate.main's predictor pass (lines 591-592).
            'p_pred': 0.85,
            'p_std': 0.05,
            # CANONICAL parquet column — extract_data.py writes this name.
            # Pre-fix: code read row['entry_price_cents'] → KeyError.
            'market_price': 92,
            'side': 'yes',
            # Read only when final_lo is not None (no dispatch_miss); we keep
            # it valid so that even if dispatch ever lands, downstream is sane.
            'outcome': 1.0,
        }

    def test_empirical_coverage_does_not_keyerror_on_parquet_schema(self):
        import pandas as pd

        from validate import empirical_coverage

        df = pd.DataFrame([
            self._make_parquet_row(price_tier=0, stc_bucket=0, vol_regime_int=0),
            self._make_parquet_row(price_tier=1, stc_bucket=1, vol_regime_int=0),
            self._make_parquet_row(price_tier=3, stc_bucket=2, vol_regime_int=1),
        ])
        # Confirm parquet shape — no entry_price_cents column. If this assertion
        # ever fires, the test setup itself diverged from the parquet contract
        # and the regression value is gone.
        assert 'entry_price_cents' not in df.columns, (
            "test_df must mirror the parquet schema (no entry_price_cents); "
            "this column only exists inside sim_pnl after its internal rename."
        )
        assert 'market_price' in df.columns

        # Minimal artifact — empty cells + empty merged_axes forces every
        # row down the dispatch_miss path (q_alpha=None → final_lo=None →
        # continue). That isolates the test to the "row[...] arg-eval" code
        # path that contained the bug.
        conformal_artifact = {
            'cells': [],
            'merged_axes': [],
            'bleed_collapsed_by_merge': False,
            'bleed_collapsed_by_merge_per_vr': {},
            'bleed_fallback_quantiles': {},
        }

        # Pre-fix: this call raised KeyError: 'entry_price_cents' during
        # arg-evaluation of the predict_with_interval call. Post-fix: returns
        # cleanly with all rows in dispatch_miss.
        cell_audit, cov_summary = empirical_coverage(
            df, conformal_artifact, market_blend_w=0.0,
        )

        assert cov_summary['n_total_test_rows'] == 3
        # All dispatch_miss so n_eval_cells stays 0 (cell_stats keys exist
        # but n=0 → filtered out at line 297).
        assert cov_summary['n_eval_cells'] == 0
        assert cov_summary['n_total_dispatch_miss'] == 3
        assert cell_audit == []


# ── AST guard ─────────────────────────────────────────────────────────


class TestEmpiricalCoverageAstGuard:
    """T-A2: lock the column name at the source. Future re-introduction
    of `row['entry_price_cents']` inside `empirical_coverage` would be
    silently fatal because the function is only exercised on real
    bundles. AST guard fires loudly in CI."""

    VALIDATE_PATH = PROJECT_ROOT / "scripts" / "cal_mlp" / "validate.py"

    def _empirical_coverage_row_reads(self) -> set:
        """Return the set of literal column-name keys read off `row` inside
        the empirical_coverage function body. Covers BOTH:
          - `row['<literal>']`   (Subscript with Constant slice)
          - `row.get('<literal>', ...)` (Call to .get with Constant first arg)

        The Call form is included because a regression like
        `row.get('entry_price_cents', row['market_price'])` would otherwise
        bypass the Subscript-only guard.

        Walks ONLY module-level FunctionDefs so a hypothetical inner
        function named `empirical_coverage` (e.g., a closure inside main)
        can't accidentally satisfy the guard. Raises AssertionError if
        the function is renamed/removed so the failure surfaces as
        "function moved" rather than the misleading "key missing"."""
        src = self.VALIDATE_PATH.read_text()
        tree = ast.parse(src)
        for fn in tree.body:
            if not isinstance(fn, ast.FunctionDef):
                continue
            if fn.name != 'empirical_coverage':
                continue
            keys: set = set()
            for node in ast.walk(fn):
                # Form A: row['<literal>']
                if isinstance(node, ast.Subscript) and \
                        isinstance(node.value, ast.Name) and node.value.id == 'row':
                    slice_node = node.slice
                    if isinstance(slice_node, ast.Constant):
                        keys.add(slice_node.value)
                    elif hasattr(ast, 'Index') and isinstance(slice_node, ast.Index) \
                            and isinstance(slice_node.value, ast.Constant):
                        keys.add(slice_node.value.value)
                # Form B: row.get('<literal>', ...) — Call to Attribute
                elif isinstance(node, ast.Call) \
                        and isinstance(node.func, ast.Attribute) \
                        and node.func.attr == 'get' \
                        and isinstance(node.func.value, ast.Name) \
                        and node.func.value.id == 'row' \
                        and node.args \
                        and isinstance(node.args[0], ast.Constant):
                    keys.add(node.args[0].value)
            return keys
        raise AssertionError(
            "empirical_coverage FunctionDef not found at module level in "
            "validate.py — was it renamed or moved into a closure? Update "
            "this guard to point at the new location."
        )

    def test_empirical_coverage_reads_market_price_not_entry_price_cents(self):
        keys = self._empirical_coverage_row_reads()
        assert 'entry_price_cents' not in keys, (
            f"empirical_coverage must NOT read row['entry_price_cents'] — "
            f"that's sim_pnl's internal rename. The parquet test_df uses "
            f"`market_price` (extract_data.py:218). Found row[...] keys: {keys}"
        )
        assert 'market_price' in keys, (
            f"empirical_coverage must read row['market_price'] (the parquet's "
            f"canonical column). Found row[...] keys: {keys}"
        )

    def test_empirical_coverage_uses_vol_regime_int_not_vol_regime(self):
        """Adjacent regression class: validate.py:245-248 documents a prior
        bug where `int(row['vol_regime'])` raised ValueError on the string
        'elevated'. The parquet has BOTH columns; conformal lookup needs
        the int. Lock the int form."""
        keys = self._empirical_coverage_row_reads()
        assert 'vol_regime' not in keys, (
            f"empirical_coverage must NOT read row['vol_regime'] — that's "
            f"the string column ('normal'/'elevated') and would ValueError "
            f"on int(). Use row['vol_regime_int']. See validate.py:245-248 "
            f"comment block. Found row[...] keys: {keys}"
        )
        assert 'vol_regime_int' in keys, (
            f"empirical_coverage must read row['vol_regime_int'] (the int "
            f"column for conformal cell dispatch). Found row[...] keys: {keys}"
        )

    def test_empirical_coverage_reads_outcome(self):
        """`outcome` is the per-row coverage indicator (line ~287). Locking
        the canonical name guards against a future rename to e.g.
        `outcome_int` or `y_true` — same lock-step principle as
        market_price and vol_regime_int."""
        keys = self._empirical_coverage_row_reads()
        assert 'outcome' in keys, (
            f"empirical_coverage must read row['outcome'] for empirical "
            f"coverage tally. Found row[...] keys: {keys}"
        )
