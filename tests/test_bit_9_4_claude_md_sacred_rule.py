"""Bit 9.4 — Root CLAUDE.md sacred-rule semantics rewrite (2026-05-10).

Per master plan L2218-2222: Bit 2.1a updated the path text (bot.py →
bot/__main__.py) but kept the "sacred boundary" framing. Sprint 9 Bit 9.4
finishes the semantic shift:

  WAS: "bot/__main__.py is the runtime entrypoint; bot/_impl.py is the
        body. ... Logic moves out per the modularization track..."

  NOW: "bot/__main__.py is the entrypoint shim — sacred boundary, no
        logic. Logic lives in bot/<subpackage>/<module>.py."

This shift is load-bearing because:
  1. Post-Bit-9.3-ii (2026-05-10), bot/__main__.py imports MainLoop
     directly from bot.main_loop (NOT from bot._impl). The "bot/_impl.py
     is the body" framing is factually wrong.
  2. Future agents reading CLAUDE.md need to know that the canonical
     layout has logic in bot/<subpackage>/<module>.py, not in a
     monolithic body file.
  3. The Bit 9.3-iii deletion of bot/_impl.py finishes the structural
     shift; the semantic update belongs here, ahead of that deletion.

These pins protect the new framing against future drift (e.g., if a
maintainer reverts the sacred-rule text without realizing the
modularization is done).
"""
from __future__ import annotations

from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent
CLAUDE_MD = REPO_ROOT / "CLAUDE.md"


def _claude_md_text() -> str:
    return CLAUDE_MD.read_text()


# ─── Positive pins (post-9.4 framing must be present) ─────────────────

def test_claude_md_has_entrypoint_shim_framing():
    """Post-9.4 sacred rule: bot/__main__.py is the entrypoint shim — no logic.

    The new framing must appear somewhere in CLAUDE.md so agents grep'ing
    `entrypoint shim` find the correct mental model."""
    src = _claude_md_text()
    assert "entrypoint shim" in src, (
        "CLAUDE.md missing the post-9.4 `entrypoint shim` framing for "
        "bot/__main__.py. Per master plan L2218-2222, the sacred rule "
        "should state: `bot/__main__.py is the entrypoint shim — sacred "
        "boundary, no logic`."
    )


def test_claude_md_has_subpackage_logic_framing():
    """Post-9.4 sacred rule: logic lives in bot/<subpackage>/<module>.py.

    Either the literal pattern `bot/<subpackage>/` or `bot/<module>` should
    appear in the rule so the canonical layout is visible to agents."""
    src = _claude_md_text()
    has_subpackage = (
        "bot/<subpackage>" in src
        or "bot/<subpackage>/<module>" in src
    )
    assert has_subpackage, (
        "CLAUDE.md missing the post-9.4 `bot/<subpackage>/<module>.py` "
        "logic-location framing. Per master plan L2218-2222."
    )


# ─── Negative pins (stale framing must be GONE) ───────────────────────

def test_claude_md_does_not_claim_bot_impl_is_the_body():
    """Stale pre-9.4 framing: `bot/_impl.py is the body`. Post-9.3-ii,
    bot/__main__.py no longer imports from bot._impl — the body has moved
    to bot/main_loop.py + bot/scanner/__init__.py + bot/executor.py +
    bot/settlement.py + bot/order_flow.py + bot/orphan_db_watchdog.py +
    other subpackages. The `bot/_impl.py is the body` text is factually
    wrong and must be removed."""
    src = _claude_md_text()
    assert "bot/_impl.py is the body" not in src, (
        "CLAUDE.md still claims `bot/_impl.py is the body` — that framing "
        "is stale post-Bit-9.3-ii (bot/__main__.py imports directly from "
        "bot.main_loop now). The post-9.4 sacred rule should describe "
        "bot/__main__.py as the entrypoint shim and bot/<subpackage>/ "
        "as the logic home."
    )


def test_claude_md_does_not_claim_logic_moves_out_per_modularization_track():
    """Stale pre-9.4 framing: `Logic moves out per the modularization track`.
    Sprint 9 Bit 9.3-ii closed the main-class chunk; the modularization
    track moved logic out. The future-tense framing is stale."""
    src = _claude_md_text()
    assert "Logic moves out per the modularization track" not in src, (
        "CLAUDE.md still has `Logic moves out per the modularization track` "
        "framing. Sprint 9 is mostly done — the framing should be present "
        "tense (logic LIVES IN bot/<subpackage>/<module>.py), not future "
        "tense."
    )


# ─── Anti-patterns section: refresh the bot/_impl.py rule ─────────────

def test_claude_md_anti_pattern_does_not_target_bot_impl_only():
    """Stale anti-pattern: `Don't refactor bot/_impl.py into multiple files
    outside the planned modularization track`. Post-Bit-9.3-ii, bot/_impl.py
    is a 582-LOC shim that's scheduled for deletion in Bit 9.3-iii. The
    anti-pattern should now target the general pattern (don't carve bot/
    submodules outside the modularization track), NOT bot/_impl.py
    specifically."""
    src = _claude_md_text()
    # The literal stale anti-pattern wording must be replaced.
    stale = "Don't refactor `bot/_impl.py` into multiple files outside the planned modularization track"
    assert stale not in src, (
        "CLAUDE.md anti-pattern still names bot/_impl.py specifically. "
        "Post-Bit-9.3-ii the modularization track moved logic out of "
        "bot/_impl.py; the anti-pattern should generalize to bot/<subpackage>/ "
        "carve-outs."
    )


def test_claude_md_critical_rules_no_stale_runtime_chain_arrow():
    """Stale runtime chain: `python -m bot → bot/__main__.py → bot/_impl.py`.

    Post-Bit-9.3-ii the chain is `python -m bot → bot/__main__.py →
    bot.main_loop`. The arrow ending at bot/_impl.py is wrong."""
    src = _claude_md_text()
    assert "bot/__main__.py → bot/_impl.py" not in src, (
        "CLAUDE.md still has the stale runtime arrow "
        "`bot/__main__.py → bot/_impl.py`. Post-Bit-9.3-ii bot/__main__.py "
        "imports MainLoop directly from bot.main_loop; the arrow should "
        "end at bot.main_loop."
    )
