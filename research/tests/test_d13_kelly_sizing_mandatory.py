"""D-13 — Kelly sizing is mandatory; no flat 1-contract sims (modified-config path).

Authoritative source: scripts/CLAUDE.md "Sim PnL and counterfactuals must use
the bot's actual Kelly + risk parameters. Never flat 1-contract." Per RCA D-13:
the hybrid in D-7 (NULL position_size → 1ct) is for LIVE-replicated cf only.

For `replay(snapshot, modified_config)` calls (autoresearch use case), replay
must compute sizing via PositionSizer.compute(win_prob, price_cents,
balance_cents) — NOT fall back to 1ct.

Caveat: rows with NULL inputs to PositionSizer (raw_prob NULL, balance NULL)
cannot be sized — replay must (a) flag and (b) skip from the aggregate, NOT
silently size at 1ct.

TDD-red until B3 ships research.replay.size_under_config (or equivalent).
"""
from __future__ import annotations

import inspect

import pytest


def test_d13_replay_has_size_under_config() -> None:
    """B3 must ship research.replay.size_under_config (TDD-red)."""
    import research.replay as rep
    assert hasattr(rep, "size_under_config"), (
        "D-13 TDD-red: B3 must ship research.replay.size_under_config(row, config)"
    )


def test_d13_size_under_config_with_missing_inputs_returns_skip(tmp_path) -> None:
    """NULL raw_prob or balance → (contracts=None, reason='missing_inputs')."""
    import research.replay as rep
    if not hasattr(rep, "size_under_config"):
        pytest.skip("D-13 TDD-red: size_under_config not yet implemented")
    # NULL raw_prob
    result = rep.size_under_config(
        row={"raw_prob": None, "market_price": 85, "available_balance_cents": 50000},
        config={},
    )
    contracts = result.get("contracts") if isinstance(result, dict) else getattr(result, "contracts", "?")
    reason = result.get("reason") if isinstance(result, dict) else getattr(result, "reason", "?")
    assert contracts is None, f"D-13 NULL raw_prob: expected contracts=None, got {contracts!r}"
    assert "missing" in (reason or "").lower(), (
        f"D-13 NULL raw_prob reason: expected 'missing_*', got {reason!r}"
    )
    # NULL balance
    result2 = rep.size_under_config(
        row={"raw_prob": 0.8, "market_price": 85, "available_balance_cents": None},
        config={},
    )
    contracts2 = result2.get("contracts") if isinstance(result2, dict) else getattr(result2, "contracts", "?")
    assert contracts2 is None, f"D-13 NULL balance: expected contracts=None, got {contracts2!r}"


def test_d13_replay_no_literal_1_contract_fallback_outside_d7_path() -> None:
    """AST regex: replay.py size_under_config must NOT have `return 1` or `count = 1` outside D-7 path.

    The D-7 hybrid (`count = position_size or 1`) is in replay_cf_pnl. Any
    SEPARATE function that returns 1 silently as a fallback is the bug class.
    Heuristic check on source.
    """
    import research.replay as rep
    src = inspect.getsource(rep)
    # If size_under_config doesn't exist yet, skip
    if "def size_under_config" not in src:
        pytest.skip("D-13 TDD-red: size_under_config not yet shipped")
    # Find the function body
    import re
    fn_match = re.search(
        r"def size_under_config[^\n]*\n(?:[ \t]+[^\n]*\n)+", src
    )
    if fn_match:
        body = fn_match.group(0)
        # Should NOT contain a bare `return 1` (use return None or a sentinel)
        assert "return 1" not in body or "# D-7" in body, (
            "D-13 size_under_config: literal `return 1` outside D-7 acknowledgement"
        )


def test_d13_replay_cf_pnl_d7_path_is_documented() -> None:
    """The D-7 1ct hybrid is explicitly the LIVE-replicated path; pin docstring acknowledgement."""
    import research.replay as rep
    src = inspect.getsource(rep.replay_cf_pnl)
    # Either a docstring reference to D-7 hybrid, or the literal `or 1` pattern
    # for position_size default. B1's replay.py uses `count = position_size or 1`.
    assert ("position_size or 1" in src or "or 1" in src), (
        "D-13: replay_cf_pnl missing the D-7 1ct hybrid pattern."
    )


def test_d13_position_sizer_compute_signature_pinned() -> None:
    """If/when replay imports PositionSizer or its formula, the signature is pinned.

    Per RCA D-13 source-of-truth: `PositionSizer.compute(win_prob, price_cents,
    balance_cents)`. Document the expected interface. Skip if not yet imported.
    """
    import research.replay as rep
    # B1 doesn't import PositionSizer (clean port). If B3 ports the formula
    # inline, the signature contract is the same. Best-effort source scan.
    src = inspect.getsource(rep)
    if "win_prob" in src or "raw_prob" in src:
        # B3 has started porting sizing logic. Check that it takes (win/raw_prob,
        # price_cents, balance_cents) — heuristic.
        assert "price_cents" in src or "market_price" in src, (
            "D-13 sizing signature: expected price_cents/market_price as input"
        )
        assert "balance_cents" in src or "balance" in src, (
            "D-13 sizing signature: expected balance_cents/balance as input"
        )
