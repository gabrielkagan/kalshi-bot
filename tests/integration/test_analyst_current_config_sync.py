"""Regression tests pinning analyst.CURRENT_CONFIG to bot.constants values.

analyst.py is a standalone script that does not import bot/_impl.py — it ships
a manually-maintained CURRENT_CONFIG snapshot used as system-prompt context for
the param-optimizer LLM call (see analyst.py:1168). When bot constants drift
from this snapshot, the LLM gets stale numbers and emits stale recommendations.

CURRENT_CONFIG is read via AST (not import), so these tests work on any Python
checkout — analyst.py top-level imports anthropic / requests / pydantic which
may not be installed on a developer machine.
"""

from __future__ import annotations

import ast
import importlib
from pathlib import Path

import pytest
import bot.constants  # noqa: F401
import bot.config as config  # noqa: F401

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
ANALYST_PATH = REPO_ROOT / "analyst.py"


def _load_literal_via_ast(name: str, source: str):
    """Parse `source` and return the literal value of the assignment `name = ...`.
    Handles `Assign`, `AnnAssign`, and rejects `AugAssign` loudly.

    Returns the LAST matching assignment (matches Python name-resolution
    semantics — Bit 1.3 R3 lesson). If a future maintainer adds a second
    assignment to `name` (feature-flag branch, conditional override, etc.),
    the test pins the effective value, not the first definition.

    Avoids importing analyst.py (which pulls in `anthropic` etc.). Pattern
    matches Bit 1.3 R3 lessons (handle annotated targets so type-hint adoption
    doesn't silently disable the test).
    """
    tree = ast.parse(source)
    last_value = None
    found = False
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == name:
                    last_value = ast.literal_eval(node.value)
                    found = True
        elif isinstance(node, ast.AnnAssign):
            target = node.target
            if isinstance(target, ast.Name) and target.id == name:
                if node.value is None:
                    raise AssertionError(
                        f"{name} declared without a value (AnnAssign without RHS)."
                    )
                last_value = ast.literal_eval(node.value)
                found = True
        elif isinstance(node, ast.AugAssign):
            target = node.target
            if isinstance(target, ast.Name) and target.id == name:
                raise AssertionError(
                    f"{name} found as AugAssign (e.g. `{name} += ...`); "
                    f"this test expects a single literal definition."
                )
    if not found:
        raise AssertionError(
            f"analyst.{name} assignment not found via AST; layout changed."
        )
    return last_value


def _load_current_config_via_ast():
    return _load_literal_via_ast("CURRENT_CONFIG", ANALYST_PATH.read_text())


def _load_edge_schedule_via_ast():
    """Extract the `_EDGE_SCHEDULE` list literal from inside
    `compute_edge_stats` — it is function-scoped, not module-scoped.
    """
    return _load_literal_via_ast("_EDGE_SCHEDULE", ANALYST_PATH.read_text())


@pytest.fixture(scope="module")
def current_config():
    return _load_current_config_via_ast()


@pytest.fixture(scope="module")
def bot_module():
    """A lookup proxy that searches bot.constants then config for the constant name.

    Bit 9.3-iii.b (2026-05-11): pre-retirement this was `import bot` which routed
    `bot.X` through `_BotProxy.__getattr__` → `bot._impl.X` (resolved via either
    `from bot.constants import *` or `from bot.config import *`). Post-retirement the
    proxy is gone, so this fixture explicitly searches the same two canonical homes
    in the same priority order (bot.constants first — it's the canonical home for
    constants extracted in Bit 3.1; config second — shared constants like
    MAX_RISK_PER_TRADE / HOURLY_KELLY_FRACTION live there).
    """
    import bot.constants
    import bot.config as config

    class _ConstantLookup:
        def __getattr__(self, name):
            if hasattr(bot.constants, name):
                return getattr(bot.constants, name)
            if hasattr(config, name):
                return getattr(config, name)
            raise AttributeError(
                f"constant {name!r} not in bot.constants or config"
            )

    return _ConstantLookup()


SCALAR_KEYS = (
    "MIN_ENTRY_PRICE",
    "BTC_MIN_ENTRY_PRICE",
    "ETH_MIN_ENTRY_PRICE",
    "XRP_MIN_ENTRY_PRICE",
    "XRP_15M_SHADOW",
    "MAX_ENTRY_PRICE",
    "MARKET_BLEND_W",
    "MAX_RISK_PER_TRADE",
    "MAX_SECONDS_BEFORE_CLOSE",
    "STC_SHADOW_THRESHOLD",
    "XRP_MAX_RISK_PER_TRADE",
    "BTC_MAX_RISK_PER_TRADE",
    "SOL_MIN_EDGE",
    "MAKER_ONLY_THRESHOLD",
    "DRAWDOWN_HALF_THRESHOLD",
    "DRAWDOWN_QUARTER_THRESHOLD",
    "DRAWDOWN_HALT_THRESHOLD",
    "HOURLY_OBSERVATION_ONLY",
    "HOURLY_MARKET_BLEND_W",
    "HOURLY_MIN_ENTRY_PRICE",
    "HOURLY_MAX_RISK_PER_TRADE",
    "HOURLY_MAX_SECONDS_BEFORE_CLOSE",
    "HOURLY_TEMPERATURE_T",
    "HOURLY_KELLY_FRACTION",
)


@pytest.mark.parametrize("key", SCALAR_KEYS)
def test_current_config_scalar_matches_bot_constants(current_config, bot_module, key):
    snapshot_value = current_config[key]
    bot_value = getattr(bot_module, key)
    assert snapshot_value == bot_value, (
        f"analyst.CURRENT_CONFIG[{key!r}] = {snapshot_value!r} drifted from "
        f"bot.{key} = {bot_value!r}. Update analyst.py CURRENT_CONFIG in the "
        f"same commit that changed bot.constants."
    )


def _format_edge_pct(edge_frac):
    pct = edge_frac * 100
    if pct == int(pct):
        return f"{int(pct)}.0%"
    return f"{pct:g}%"


def test_current_config_min_edge_by_price_renders_actual_tiers(
    current_config, bot_module
):
    """The MIN_EDGE_BY_PRICE entry is a human-readable summary string. Pin it
    to the actual bot.constants.MIN_EDGE_BY_PRICE list — including the catchall (floor=0)
    tier that covers the 75-88c band, where ETH 75c+ is live.
    """
    snapshot = current_config["MIN_EDGE_BY_PRICE"]
    tiers = bot_module.MIN_EDGE_BY_PRICE
    catchall_seen = False
    for floor_cents, edge_frac in tiers:
        edge_repr = _format_edge_pct(edge_frac)
        if floor_cents == 0:
            assert "default" in snapshot, (
                f"MIN_EDGE_BY_PRICE summary missing 'default' marker for the "
                f"catchall tier (covers prices below the lowest explicit "
                f"floor). Snapshot: {snapshot!r}"
            )
            assert f"default→{edge_repr}" in snapshot, (
                f"MIN_EDGE_BY_PRICE summary missing 'default→{edge_repr}' for "
                f"catchall tier ({floor_cents}, {edge_frac}). The catchall "
                f"covers 75-88c (ETH 75c+ live tier) — losing this line means "
                f"the LLM sees no floor for sub-89c entries. "
                f"Snapshot: {snapshot!r}"
            )
            catchall_seen = True
            continue
        marker = f"{floor_cents}c→{edge_repr}"
        assert marker in snapshot, (
            f"MIN_EDGE_BY_PRICE summary missing {marker!r} for tier "
            f"({floor_cents}, {edge_frac}). Snapshot: {snapshot!r}"
        )
    assert catchall_seen, (
        "bot.constants.MIN_EDGE_BY_PRICE has no catchall (floor=0) tier — this test "
        "assumes the catchall exists. Update the test if bot drops it."
    )


def test_current_config_sizing_tiers_renders_actual_tiers(current_config, bot_module):
    """SIZING_TIERS entry is a stringified list. Verify every tier in the
    config.SIZING_TIERS list is referenced in the snapshot string."""
    snapshot = current_config["SIZING_TIERS"]
    for edge_floor, risk_frac in bot_module.SIZING_TIERS:
        marker = f"({edge_floor},{risk_frac})"
        marker_spaced = f"({edge_floor}, {risk_frac})"
        assert marker in snapshot or marker_spaced in snapshot, (
            f"SIZING_TIERS summary missing {marker!r}. Snapshot: {snapshot!r}"
        )


def test_current_config_keys_unchanged(current_config):
    """If the key set changes, the test must be updated alongside the change."""
    expected = set(SCALAR_KEYS) | {"MIN_EDGE_BY_PRICE", "SIZING_TIERS"}
    assert set(current_config.keys()) == expected, (
        f"analyst.CURRENT_CONFIG key set drifted. Expected: {sorted(expected)}, "
        f"got: {sorted(current_config.keys())}. Update SCALAR_KEYS in this "
        f"test or remove the obsolete key from analyst.py."
    )


def test_edge_counterfactual_price_band_uses_current_config():
    """Both price-band gates in compute_edge_stats (the missed-profit
    accumulator AND the halve-thresholds counterfactual) must use
    CURRENT_CONFIG['MIN_ENTRY_PRICE'] / ['MAX_ENTRY_PRICE'], not hardcoded
    literals — otherwise drift in the entry-price floor silently excludes
    in-scope rejections from BOTH the missed-profit rollup and the
    recapture estimate, and the LLM gets inconsistent denominators between
    `missed_winners` (no price gate) and `missed_profit_cents` (gated).
    """
    src = ANALYST_PATH.read_text()
    tree = ast.parse(src)
    func = next(
        (
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef)
            and node.name == "compute_edge_stats"
        ),
        None,
    )
    assert func is not None, "compute_edge_stats not found in analyst.py"
    func_src = ast.get_source_segment(src, func) or ""
    # Both gates use CURRENT_CONFIG (count both quote styles).
    min_refs = func_src.count('CURRENT_CONFIG["MIN_ENTRY_PRICE"]') + func_src.count(
        "CURRENT_CONFIG['MIN_ENTRY_PRICE']"
    )
    max_refs = func_src.count('CURRENT_CONFIG["MAX_ENTRY_PRICE"]') + func_src.count(
        "CURRENT_CONFIG['MAX_ENTRY_PRICE']"
    )
    assert min_refs >= 2, (
        f"compute_edge_stats has only {min_refs} CURRENT_CONFIG['MIN_ENTRY_PRICE']"
        f" references; expected >=2 (missed-profit gate + counterfactual gate). "
        f"A second hardcoded `80 <= price` was found and migrated in Bit 4.2.5.1 R3."
    )
    assert max_refs >= 2, (
        f"compute_edge_stats has only {max_refs} CURRENT_CONFIG['MAX_ENTRY_PRICE']"
        f" references; expected >=2 (missed-profit gate + counterfactual gate)."
    )
    # Belt-and-suspenders: no leftover hardcoded `80 <= price` or `price <= 99`
    # anywhere in the function.
    assert "80 <= price" not in func_src, (
        "Found hardcoded `80 <= price` in compute_edge_stats — replace with "
        "CURRENT_CONFIG['MIN_ENTRY_PRICE']."
    )
    assert "price <= 99" not in func_src, (
        "Found hardcoded `price <= 99` in compute_edge_stats — replace with "
        "CURRENT_CONFIG['MAX_ENTRY_PRICE']."
    )


def test_price_bucket_lower_label_matches_min_entry_price():
    """`_price_bucket` lower bucket must label its floor with
    CURRENT_CONFIG['MIN_ENTRY_PRICE'] so the label stays correct as the entry
    floor moves. The bucket label is parsed downstream (loss-context SQL
    range query at compute_loss_context) so the floor must match the actual
    in-scope crypto entries.
    """
    src = ANALYST_PATH.read_text()
    tree = ast.parse(src)
    func = next(
        (
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef) and node.name == "_price_bucket"
        ),
        None,
    )
    assert func is not None, "_price_bucket not found in analyst.py"
    func_src = ast.get_source_segment(src, func) or ""
    assert (
        'CURRENT_CONFIG["MIN_ENTRY_PRICE"]' in func_src
        or "CURRENT_CONFIG['MIN_ENTRY_PRICE']" in func_src
    ), (
        "_price_bucket lower-bucket label should use "
        "CURRENT_CONFIG['MIN_ENTRY_PRICE'], not a hardcoded literal like '80-84'."
    )
    assert '"80-84"' not in func_src and "'80-84'" not in func_src, (
        "Found hardcoded '80-84' literal in _price_bucket — should be "
        "f-string using CURRENT_CONFIG['MIN_ENTRY_PRICE']."
    )


def test_edge_schedule_matches_bot_min_edge_by_price(bot_module):
    """analyst._EDGE_SCHEDULE (function-local in compute_edge_stats) must
    mirror bot.constants.MIN_EDGE_BY_PRICE exactly — it drives the "halve thresholds"
    counterfactual in the alpha audit and feeds the LLM. Bit 4.2.5.1 found this
    schedule was 4-10x stale, breaking the counterfactual numerics.
    """
    schedule = _load_edge_schedule_via_ast()
    bot_tiers = list(bot_module.MIN_EDGE_BY_PRICE)
    assert schedule == bot_tiers, (
        f"analyst._EDGE_SCHEDULE = {schedule} drifted from "
        f"bot.constants.MIN_EDGE_BY_PRICE = {bot_tiers}. The 'halve thresholds' "
        f"counterfactual in compute_edge_stats uses this schedule — "
        f"any drift makes recaptured_profit_cents arithmetically wrong."
    )


def test_ann_assign_handled_by_ast_helper():
    """Regression for Bit 1.3 R3 lesson: the AST helper must read AnnAssign
    targets so a future maintainer adding `CURRENT_CONFIG: dict[str, ...] = {}`
    doesn't silently disable this whole test file.
    """
    src = "FOO: dict = {'k': 1}\n"
    assert _load_literal_via_ast("FOO", src) == {"k": 1}


def test_aug_assign_rejected_by_ast_helper():
    """Augmented assignment (`X += ...`) is not a single-literal definition;
    the helper must fail loudly rather than silently fall through. A bare
    AugAssign (no prior Assign) parses fine at the AST level even though it
    would NameError at runtime — this defends against accumulator-style
    refactors that would mask drift.
    """
    src_aug = "BAZ += 1\n"
    with pytest.raises(AssertionError, match="AugAssign"):
        _load_literal_via_ast("BAZ", src_aug)
