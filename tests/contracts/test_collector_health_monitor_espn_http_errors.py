"""Ticket 86bbvqhyr (2026-09-06) — ``collector_health_monitor.py`` gains a
content-class check for the ESPN tier + a sports-silence check for the
bot tier.

"Chunks landing" ≠ "data landing" (L-espn-1; same class as L-rot-7 in
kb/failures/vps-disk-full-journal-rotation-collision-sep05.md). The
ESPN tier previously ran disk + collector_active + dropped_frames —
all three stayed green for 5 weeks of 100% HTTP 403 bronze rows.

Structural pins (source-level, mirroring the D1.11.a tier tests):
  1. ``check_espn_http_errors`` is defined.
  2. The ``espn_checks`` block dispatches it under the name
     ``"http_errors"`` so the tier prefix yields dedup key
     ``d1_11_http_errors``.
  3. ``check_sports_eval_silence`` is defined and dispatched in the
     ``bot_checks`` block as ``"sports_eval_silence"``
     (→ ``b3_fu3_sports_eval_silence``).
Behavioral tests live in tests/integration/test_espn_http_health_regression.py.
"""
from __future__ import annotations

import ast
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
MONITOR_FILE = REPO_ROOT / "scripts" / "ops" / "collector_health_monitor.py"


def _src() -> str:
    return MONITOR_FILE.read_text()


def _block(src: str, marker: str) -> str:
    idx = src.find(marker)
    assert idx != -1, f"{marker!r} block missing in collector_health_monitor.py"
    depth = 0
    for i in range(idx, len(src)):
        if src[i] == "[":
            depth += 1
        elif src[i] == "]":
            depth -= 1
            if depth == 0:
                return src[idx:i + 1]
    raise AssertionError(f"unterminated {marker!r} block")


def _top_level_function_names() -> set:
    tree = ast.parse(_src())
    return {
        n.name for n in ast.iter_child_nodes(tree)
        if isinstance(n, ast.FunctionDef)
    }


def test_check_espn_http_errors_defined():
    assert "check_espn_http_errors" in _top_level_function_names(), (
        "collector_health_monitor.py must define check_espn_http_errors "
        "(ticket 86bbvqhyr) — reads the per-league 1h non-200 rate from "
        "the ESPN bronze_health.json sidecar."
    )


def test_espn_tier_dispatches_http_errors_check():
    block = _block(_src(), "espn_checks = [")
    assert '"http_errors"' in block, (
        "espn_checks must dispatch (\"http_errors\", …) so the d1_11 "
        "tier prefix yields dedup key d1_11_http_errors."
    )
    assert "check_espn_http_errors" in block


def test_espn_tier_keeps_prior_three_checks():
    block = _block(_src(), "espn_checks = [")
    for name in ('"disk"', '"collector_active"', '"dropped_frames"'):
        assert name in block


def test_check_sports_eval_silence_defined():
    assert "check_sports_eval_silence" in _top_level_function_names(), (
        "collector_health_monitor.py must define check_sports_eval_silence "
        "(ticket 86bbvqhyr) — bot-tier alert when ESPN reported live games "
        "but evaluated_opportunities has 0 sports rows in the window."
    )


def test_bot_tier_dispatches_sports_eval_silence_check():
    block = _block(_src(), "bot_checks = [")
    assert '"sports_eval_silence"' in block, (
        "bot_checks must dispatch (\"sports_eval_silence\", …) → dedup key "
        "b3_fu3_sports_eval_silence."
    )
    assert "check_sports_eval_silence" in block
    assert '"insert_eval_failures"' in block, "existing bot check must remain"


def test_espn_tier_still_excludes_ws_reconnects():
    block = _block(_src(), "espn_checks = [")
    assert '"ws_reconnects"' not in block
