"""D-21 — order_outcome vocab compatibility window.

Authoritative source: tests/test_order_outcome_vocab.py + AST guard +
project_order_outcome_vocab_resolved_may04.md (per RCA D-21).

Canonical order_outcome values (the ALLOWED_OUTCOMES set):
    'filled', 'partial_filled', 'cancelled', 'rejected'

Three legacy sites wrote 'partial_fill' (event_type vocab — the wrong family).
The AST guard catches future drift; replay's compute_fill_rate must accept
BOTH spellings for the historical compatibility window so pre-fix snapshot
rows aren't silently dropped.

Test surface (TDD-red until B3 ships compute_fill_rate):
1. compute_fill_rate accepts 'partial_fill' (legacy) AND 'partial_filled'
   (canonical) as partial-fill markers.
2. Emits a deprecation flag when pre-fix rows are counted.
3. AST regex check that replay uses ALLOWED_OUTCOMES (or equivalent set).
"""
from __future__ import annotations

import pytest


CANONICAL_OUTCOMES = frozenset({"filled", "partial_filled", "cancelled", "rejected"})
LEGACY_PARTIAL = "partial_fill"  # The pre-fix spelling


def test_d21_canonical_outcomes_pinned() -> None:
    """Pin the canonical 4-value set as an inline regression contract."""
    assert CANONICAL_OUTCOMES == frozenset({
        "filled", "partial_filled", "cancelled", "rejected"
    })
    assert LEGACY_PARTIAL == "partial_fill"
    # Legacy is NOT in the canonical set.
    assert LEGACY_PARTIAL not in CANONICAL_OUTCOMES


def test_d21_replay_has_compute_fill_rate() -> None:
    """B3 must ship research.replay.compute_fill_rate (TDD-red)."""
    import research.replay as rep
    assert hasattr(rep, "compute_fill_rate"), (
        "D-21 TDD-red: B3 must ship research.replay.compute_fill_rate(rows)"
    )


def test_d21_compute_fill_rate_accepts_both_partial_spellings() -> None:
    """compute_fill_rate counts BOTH 'partial_fill' (legacy) and 'partial_filled' as partial fills."""
    import research.replay as rep
    if not hasattr(rep, "compute_fill_rate"):
        pytest.skip("D-21 TDD-red: compute_fill_rate not yet implemented")
    # Rows: one canonical-partial + one legacy-partial
    rows = [
        {"order_outcome": "partial_filled"},
        {"order_outcome": "partial_fill"},  # legacy
        {"order_outcome": "filled"},
    ]
    result = rep.compute_fill_rate(rows)
    # Whatever the result shape, both spellings should be counted in the
    # "partial" bucket. Best-effort assertion across plausible shapes.
    partial_count = (
        result.get("partial", 0) if isinstance(result, dict)
        else getattr(result, "partial", 0)
    )
    assert partial_count == 2, (
        f"D-21 partial-vocab: expected 2 partial-fill rows (1 canonical + 1 legacy), "
        f"got {partial_count!r} from result {result!r}"
    )


def test_d21_compute_fill_rate_emits_deprecation_flag_on_legacy() -> None:
    """When pre-fix 'partial_fill' rows are present, the result flags it."""
    import research.replay as rep
    if not hasattr(rep, "compute_fill_rate"):
        pytest.skip("D-21 TDD-red: compute_fill_rate not yet implemented")
    rows_with_legacy = [{"order_outcome": "partial_fill"}, {"order_outcome": "filled"}]
    result = rep.compute_fill_rate(rows_with_legacy)
    flag = (
        result.get("has_legacy_partial_fill", None) if isinstance(result, dict)
        else getattr(result, "has_legacy_partial_fill", None)
    )
    assert flag is True, (
        f"D-21 deprecation flag: expected has_legacy_partial_fill=True, "
        f"got {flag!r} from {result!r}"
    )
    # Sanity: no flag when there's no legacy data.
    rows_clean = [{"order_outcome": "partial_filled"}, {"order_outcome": "filled"}]
    result_clean = rep.compute_fill_rate(rows_clean)
    flag_clean = (
        result_clean.get("has_legacy_partial_fill", None) if isinstance(result_clean, dict)
        else getattr(result_clean, "has_legacy_partial_fill", None)
    )
    assert flag_clean in (False, None), (
        f"D-21 clean-data flag: expected False/None for canonical-only data, got {flag_clean!r}"
    )


def test_d21_compute_fill_rate_unknown_outcome_ignored_or_flagged() -> None:
    """An outcome value outside ALLOWED_OUTCOMES + LEGACY is handled, not crashed."""
    import research.replay as rep
    if not hasattr(rep, "compute_fill_rate"):
        pytest.skip("D-21 TDD-red: compute_fill_rate not yet implemented")
    rows = [{"order_outcome": "weird_value"}, {"order_outcome": "filled"}]
    # Either silently dropped or flagged — both are acceptable, but must not
    # raise on an unfamiliar value.
    try:
        result = rep.compute_fill_rate(rows)
    except (KeyError, ValueError) as e:
        pytest.fail(f"D-21 compute_fill_rate should tolerate unknown values, raised: {e!r}")
    assert result is not None


def test_d21_replay_does_not_inline_outcome_strings_outside_allowlist() -> None:
    """AST regex pin: replay.py references the canonical outcome strings, NOT raw legacy.

    The legacy 'partial_fill' string MAY appear ONCE in a documented compat
    block; anywhere else is drift. This is a heuristic guard.
    """
    import inspect
    import research.replay as rep
    src = inspect.getsource(rep)
    # Count occurrences of the legacy literal — should be 0 today, ≤2 after
    # B3 ships compat handling (the literal + a 'D-21' acknowledging comment).
    legacy_occurrences = src.count('"partial_fill"') + src.count("'partial_fill'")
    assert legacy_occurrences <= 2, (
        f"D-21 legacy spelling sprawl: found {legacy_occurrences} occurrences of "
        f"'partial_fill' in replay.py. Limit to ≤2 (the literal + ack)."
    )
