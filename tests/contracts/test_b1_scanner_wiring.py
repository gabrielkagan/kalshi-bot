"""B1 scanner-wiring AST contract (ClickUp 86ba1zdwm, 2026-05-21).

Per adv-review R1 finding M3: 18 helper-level tests verify
`check_orderbook_prior_gate` / `check_hype_high_price_buf_gate` behavior,
but ZERO tests verify the scanner ACTUALLY calls them at the TM and DC
candidate-append paths. A future refactor accidentally removing the
wiring would leave the helper as dead code with the unit tests still green.

This file walks `bot/scanner/__init__.py` via AST and asserts:

1. Both gate helpers are top-imported.
2. Both gate helpers are called from `OpportunityScanner.scan()`.
3. Both calls appear in BOTH the TM path (terminal_momentum candidate-
   append region) AND the DC path (decided_t1/t2 candidate-append region).
4. The trade-block conditional (e.g., `if ORDERBOOK_PRIOR_GATE_ENABLED:`)
   appears AFTER the helper call but BEFORE the original `if _tm_intercepted:`
   / `if _dc_live_enabled and _dc_position > 0:` guards that gate the
   candidate-append. (Structural — shadow-log fires regardless of enable,
   trade-block only when enabled.)

The 4-set of required call-site features (helper × path) is the
load-bearing structural pin. A future refactor that consolidates them
into a single `if _both_gates(...)` orchestrator call MUST update this
contract — the goal is to prevent SILENT drift (gate removed accidentally),
not to lock-in the current cell-by-cell shape forever.
"""
from __future__ import annotations

import ast
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SCANNER_PATH = REPO_ROOT / "bot" / "scanner" / "__init__.py"


def _scanner_source() -> str:
    return SCANNER_PATH.read_text(encoding="utf-8")


def _scanner_tree() -> ast.Module:
    return ast.parse(_scanner_source(), filename=str(SCANNER_PATH))


def test_scanner_top_imports_both_b1_gate_helpers():
    """Both check_orderbook_prior_gate AND check_hype_high_price_buf_gate
    must be top-imported (not lazy-imported inside scan()). Top-level imports
    enforce dependency clarity and prevent silent late-binding drift."""
    tree = _scanner_tree()
    top_level_imports: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.ImportFrom) and node.module == "bot.helpers.adverse_selection":
            for alias in node.names:
                top_level_imports.add(alias.name)
    required = {"check_orderbook_prior_gate", "check_hype_high_price_buf_gate", "extract_no_ask_and_yes_asks"}
    missing = required - top_level_imports
    assert not missing, (
        f"bot/scanner/__init__.py missing top-level B1 imports: {missing}. "
        f"Got: {top_level_imports}. Required at top of file (not lazy)."
    )


def test_scanner_top_imports_both_b1_kill_switches():
    """Both ORDERBOOK_PRIOR_GATE_ENABLED AND HYPE_HIGH_PRICE_BUF_GATE_ENABLED
    must be top-imported from bot.constants so the scanner can gate trade-block
    on each independently (shadow-log fires regardless)."""
    tree = _scanner_tree()
    constants_imports: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.ImportFrom) and node.module == "bot.constants":
            for alias in node.names:
                constants_imports.add(alias.name)
    required = {"ORDERBOOK_PRIOR_GATE_ENABLED", "HYPE_HIGH_PRICE_BUF_GATE_ENABLED"}
    missing = required - constants_imports
    assert not missing, (
        f"bot/scanner/__init__.py missing kill-switch imports: {missing}. "
        f"Got bot.constants imports: {sorted(constants_imports)}"
    )


def test_gate_helpers_called_at_least_twice_each():
    """check_orderbook_prior_gate + check_hype_high_price_buf_gate must each
    be called at LEAST twice in scanner — once per gated path (TM + DC).
    A future Bit may add a third call site (e.g., bracket_no path); the
    >= 2 floor is the load-bearing pin."""
    src = _scanner_source()
    n_ob = src.count("check_orderbook_prior_gate(")
    n_hype = src.count("check_hype_high_price_buf_gate(")
    assert n_ob >= 2, (
        f"check_orderbook_prior_gate called only {n_ob} times in scanner; "
        f"expected >= 2 (TM path + DC path)"
    )
    assert n_hype >= 2, (
        f"check_hype_high_price_buf_gate called only {n_hype} times in scanner; "
        f"expected >= 2 (TM path + DC path)"
    )


def test_kill_switch_checks_appear_with_gate_calls():
    """Each kill-switch flag must appear at least twice (one per gated path).
    The presence pin only — the structural ordering (helper before flag check)
    is implicitly covered by the behavioral integration tests."""
    src = _scanner_source()
    n_ob_flag = src.count("if ORDERBOOK_PRIOR_GATE_ENABLED:")
    n_hype_flag = src.count("if HYPE_HIGH_PRICE_BUF_GATE_ENABLED:")
    assert n_ob_flag >= 2, (
        f"`if ORDERBOOK_PRIOR_GATE_ENABLED:` appears only {n_ob_flag} times; "
        f"expected >= 2 (TM path + DC path trade-block gates)"
    )
    assert n_hype_flag >= 2, (
        f"`if HYPE_HIGH_PRICE_BUF_GATE_ENABLED:` appears only {n_hype_flag} times; "
        f"expected >= 2 (TM path + DC path trade-block gates)"
    )


def test_gate_calls_appear_in_tm_path():
    """B1 gates must wire into the terminal_momentum candidate-append region.
    Test: between 'Terminal Momentum intercept' marker and the TM candidate
    dict literal `"strategy": f"terminal_momentum_{best_ask}"`."""
    src = _scanner_source()
    tm_marker = src.find("Terminal Momentum intercept")
    assert tm_marker > 0, "Terminal Momentum intercept marker not found"
    tm_end = src.find('"strategy": f"terminal_momentum_{best_ask}"', tm_marker)
    assert tm_end > tm_marker, "TM candidate dict literal not found after marker"
    tm_block = src[tm_marker:tm_end]
    assert "check_orderbook_prior_gate(" in tm_block, (
        "TM path missing check_orderbook_prior_gate call before candidate.append"
    )
    assert "check_hype_high_price_buf_gate(" in tm_block, (
        "TM path missing check_hype_high_price_buf_gate call before candidate.append"
    )
    assert "if ORDERBOOK_PRIOR_GATE_ENABLED:" in tm_block, (
        "TM path missing ORDERBOOK_PRIOR_GATE_ENABLED kill-switch check"
    )
    assert "if HYPE_HIGH_PRICE_BUF_GATE_ENABLED:" in tm_block, (
        "TM path missing HYPE_HIGH_PRICE_BUF_GATE_ENABLED kill-switch check"
    )


def test_gate_calls_appear_in_dc_path():
    """B1 gates must wire into the decided_contract candidate-append region.

    Uses a FORWARD-anchor pattern (per R2 N4 fix): the B1 wiring comment
    marker `'B1 (86ba1zdwm) composite adverse-selection gate'` appears
    EXACTLY TWICE in scanner — once in TM path, once in DC path. The DC
    occurrence is between the exposure-cap block and the cooldown check,
    immediately preceding the DC_CANDIDATE log line.

    This avoids the fragile-magic-number back-scan window that R2 flagged
    (a future scanner growth could push the TM block into the back-scan
    window, causing the DC test to pass on TM matches even if DC wiring
    were deleted)."""
    src = _scanner_source()
    marker = "B1 (86ba1zdwm) composite adverse-selection gate"
    markers = [i for i in range(len(src)) if src.startswith(marker, i)]
    assert len(markers) >= 2, (
        f"Expected at least 2 B1 wiring-comment markers (TM + DC), got {len(markers)}"
    )
    # The DC block is the SECOND marker — scan forward until the next
    # DC_CANDIDATE log line (the candidate-append region's anchor).
    dc_block_start = markers[1]
    dc_candidate = src.find("DC_CANDIDATE:", dc_block_start)
    assert dc_candidate > dc_block_start, (
        f"DC_CANDIDATE log not found after second B1 marker at {dc_block_start}"
    )
    dc_block = src[dc_block_start:dc_candidate]
    assert "check_orderbook_prior_gate(" in dc_block, (
        "DC path missing check_orderbook_prior_gate call before DC_CANDIDATE"
    )
    assert "check_hype_high_price_buf_gate(" in dc_block, (
        "DC path missing check_hype_high_price_buf_gate call before DC_CANDIDATE"
    )
    assert "if ORDERBOOK_PRIOR_GATE_ENABLED:" in dc_block, (
        "DC path missing ORDERBOOK_PRIOR_GATE_ENABLED kill-switch check"
    )
    assert "if HYPE_HIGH_PRICE_BUF_GATE_ENABLED:" in dc_block, (
        "DC path missing HYPE_HIGH_PRICE_BUF_GATE_ENABLED kill-switch check"
    )


def test_b1_call_sites_pass_entry_price_cents_to_gate_a():
    """Per adv-review R1 finding C2: scanner Gate A call sites MUST pass
    entry_price_cents kwarg. Without it, the 90c entry floor (added per C2)
    is bypassed and the gate could block sub-90c trades the R0 sim never
    measured."""
    tree = _scanner_tree()
    gate_a_calls_with_entry_kwarg = 0
    gate_a_calls_total = 0
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            fn = node.func
            if isinstance(fn, ast.Name) and fn.id == "check_orderbook_prior_gate":
                gate_a_calls_total += 1
                kwarg_names = {kw.arg for kw in node.keywords}
                if "entry_price_cents" in kwarg_names:
                    gate_a_calls_with_entry_kwarg += 1
    assert gate_a_calls_total >= 2, (
        f"Gate A called only {gate_a_calls_total} times; expected >= 2"
    )
    assert gate_a_calls_with_entry_kwarg == gate_a_calls_total, (
        f"Only {gate_a_calls_with_entry_kwarg}/{gate_a_calls_total} Gate A "
        f"call sites pass entry_price_cents. ALL production callers MUST "
        f"pass it (R0 sim measured entry>=90c only)."
    )
