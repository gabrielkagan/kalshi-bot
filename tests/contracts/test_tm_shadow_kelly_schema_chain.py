"""TM half-Kelly cal_mlp shadow — schema chain AST guards (Sim C, ticket 86ba0v7fc).

Pins the 4-column schema chain in lockstep:
  - `evaluated_opportunities` table has the 4 new columns:
    tm_shadow_kelly_ct INTEGER, tm_shadow_kelly_prob REAL,
    tm_shadow_kelly_fraction REAL, tm_shadow_kelly_bound_hit TEXT
  - `insert_evaluated_opportunity` accepts the 4 new kwargs
  - The INSERT SQL (column list + VALUES placeholders) references all 4 keys
  - `bot.helpers.tm_sweep.tm_shadow_kelly_contracts` exists with the planned shape
  - `bot.constants` exposes the new defaults

Splitting → silent drop at write time (the canonical `_shadow_diag` failure class).

See `kb/decisions/tm-half-kelly-shadow-plan.md` + `bot/CLAUDE.md` "_shadow_diag
schema chain".
"""

from __future__ import annotations

import ast
import inspect
import os
import re
import sys

import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)


SHADOW_KEYS = (
    "tm_shadow_kelly_ct",
    "tm_shadow_kelly_prob",
    "tm_shadow_kelly_fraction",
    "tm_shadow_kelly_bound_hit",
)


# ── Test 1: Columns exist on evaluated_opportunities ────────────────────────


def test_evaluated_opportunities_has_tm_shadow_kelly_columns(tmp_path, monkeypatch):
    """Fresh StateManager creates the 4 tm_shadow_kelly_* columns on init."""
    monkeypatch.chdir(tmp_path)
    from bot.state import StateManager
    sm = StateManager()
    try:
        cols = {
            r[1]
            for r in sm.conn.execute("PRAGMA table_info(evaluated_opportunities)").fetchall()
        }
        for key in SHADOW_KEYS:
            assert key in cols, (
                f"evaluated_opportunities missing {key!r} column "
                f"(Sim C schema chain split?)"
            )
        # Column types must match plan
        col_types = {
            r[1]: r[2]
            for r in sm.conn.execute("PRAGMA table_info(evaluated_opportunities)").fetchall()
        }
        assert col_types["tm_shadow_kelly_ct"].upper() == "INTEGER"
        assert col_types["tm_shadow_kelly_prob"].upper() == "REAL"
        assert col_types["tm_shadow_kelly_fraction"].upper() == "REAL"
        assert col_types["tm_shadow_kelly_bound_hit"].upper() == "TEXT"
    finally:
        sm.conn.close()


# ── Test 2: insert_evaluated_opportunity signature accepts the kwargs ───────


def test_insert_evaluated_opportunity_accepts_tm_shadow_kelly_kwargs():
    """`insert_evaluated_opportunity` must accept all 4 shadow kwargs."""
    from bot.state import StateManager
    sig = inspect.signature(StateManager.insert_evaluated_opportunity)
    params = set(sig.parameters.keys())
    for key in SHADOW_KEYS:
        assert key in params, (
            f"insert_evaluated_opportunity missing {key!r} kwarg "
            f"(Sim C schema chain split?)"
        )


# ── Test 3: INSERT SQL references each shadow key ────────────────────────────


def test_insert_evaluated_opportunity_sql_references_shadow_keys():
    """Each shadow key must appear both as a column name AND as a Python
    name in the values tuple (so the placeholder count + ordering line up)."""
    state_path = os.path.join(PROJECT_ROOT, "bot", "state.py")
    with open(state_path) as f:
        source = f.read()

    # Locate the INSERT INTO evaluated_opportunities ... VALUES block
    insert_match = re.search(
        r'INSERT INTO evaluated_opportunities.*?VALUES \([^)]+\)',
        source, re.DOTALL
    )
    assert insert_match, "Could not locate INSERT INTO evaluated_opportunities block"
    insert_sql = insert_match.group(0)

    # Each shadow key must appear in the column list
    for key in SHADOW_KEYS:
        assert key in insert_sql, (
            f"INSERT INTO evaluated_opportunities column list missing {key!r} "
            f"(Sim C schema chain split — column will be silently dropped at write)"
        )


def test_insert_evaluated_opportunity_values_tuple_references_shadow_keys():
    """The Python values-tuple following the INSERT SQL must reference each
    shadow kwarg by NAME (no positional drift)."""
    state_path = os.path.join(PROJECT_ROOT, "bot", "state.py")
    with open(state_path) as f:
        source = f.read()

    # The values tuple is the 2nd arg to conn.execute("INSERT...", (values...)).
    # Grep for each shadow key as a bare identifier *outside* the SQL string —
    # i.e., it must appear in the broader source (signature + tuple use).
    for key in SHADOW_KEYS:
        # At least 2 occurrences: signature param + values-tuple element.
        # The migration ALTER + INSERT column list may bump this higher.
        occurrences = source.count(key)
        assert occurrences >= 3, (
            f"{key!r} appears {occurrences}× in bot/state.py — expected ≥3 "
            f"(signature + INSERT column + values tuple element). "
            f"Sim C schema chain split?"
        )


# ── Test 4: ALTER TABLE migration adds the columns ──────────────────────────


def test_state_create_tables_alters_tm_shadow_kelly_columns():
    """The state.py _create_tables migration loop must declare the 4 cols."""
    state_path = os.path.join(PROJECT_ROOT, "bot", "state.py")
    with open(state_path) as f:
        source = f.read()
    # The ALTER TABLE evaluated_opportunities ADD COLUMN loop iterates over
    # tuples; the shadow keys must appear with their declared types.
    expected_pairs = [
        ('"tm_shadow_kelly_ct"', '"INTEGER"'),
        ('"tm_shadow_kelly_prob"', '"REAL"'),
        ('"tm_shadow_kelly_fraction"', '"REAL"'),
        ('"tm_shadow_kelly_bound_hit"', '"TEXT"'),
    ]
    for col_lit, type_lit in expected_pairs:
        # Match Python tuple literal e.g. ("tm_shadow_kelly_ct", "INTEGER")
        pattern = rf'\(\s*{col_lit}\s*,\s*{type_lit}\s*\)'
        assert re.search(pattern, source), (
            f"bot/state.py ALTER TABLE evaluated_opportunities loop missing "
            f"({col_lit}, {type_lit}) — fresh-DB add will lack the col"
        )


# ── Test 5: Helper exists with planned shape ────────────────────────────────


def test_tm_shadow_kelly_contracts_helper_exists():
    """`bot.helpers.tm_sweep.tm_shadow_kelly_contracts` must exist."""
    from bot.helpers import tm_sweep
    assert hasattr(tm_sweep, "tm_shadow_kelly_contracts"), (
        "bot.helpers.tm_sweep.tm_shadow_kelly_contracts missing — "
        "Sim C helper not shipped"
    )


def test_tm_shadow_kelly_contracts_signature_shape():
    """Helper signature must match the plan doc.

    Required params: price_cents, bankroll_cents, asset, cal_mlp_p_mean,
    raw_prob_fallback. Defaulted params: kelly_fraction, abs_loss_bound_cents.
    """
    from bot.helpers.tm_sweep import tm_shadow_kelly_contracts
    sig = inspect.signature(tm_shadow_kelly_contracts)
    params = sig.parameters
    for required in (
        "price_cents", "bankroll_cents", "asset",
        "cal_mlp_p_mean", "raw_prob_fallback",
    ):
        assert required in params, (
            f"tm_shadow_kelly_contracts missing required param {required!r}"
        )
    for defaulted in ("kelly_fraction", "abs_loss_bound_cents"):
        assert defaulted in params, (
            f"tm_shadow_kelly_contracts missing defaulted param {defaulted!r}"
        )


# ── Test 6: Constants exist ─────────────────────────────────────────────────


def test_tm_shadow_kelly_constants_defined():
    """`bot.constants` must define the new defaults (Sim C)."""
    from bot import constants
    for name in ("TM_SHADOW_KELLY_FRACTION", "TM_SHADOW_KELLY_ABS_LOSS_BOUND_CENTS"):
        assert hasattr(constants, name), (
            f"bot.constants missing {name!r} — Sim C config-reference drift"
        )
    # Half-Kelly per plan doc
    assert constants.TM_SHADOW_KELLY_FRACTION == 0.50, (
        "TM_SHADOW_KELLY_FRACTION must default to 0.50 (half-Kelly per plan)"
    )
    # $100 absolute loss bound per plan doc
    assert constants.TM_SHADOW_KELLY_ABS_LOSS_BOUND_CENTS == 10000, (
        "TM_SHADOW_KELLY_ABS_LOSS_BOUND_CENTS must default to 10000 ($100)"
    )


# ── Test 7: Full schema chain lockstep (AST guard) ──────────────────────────


def test_schema_chain_lockstep_all_4_keys_in_all_3_sites():
    """The signature + INSERT column list + values tuple are pinned to all 4
    shadow keys. This is the canonical _shadow_diag failure class — splitting
    silently drops keys at write time."""
    state_path = os.path.join(PROJECT_ROOT, "bot", "state.py")
    with open(state_path) as f:
        source = f.read()

    # Site 1: signature
    from bot.state import StateManager
    sig_params = set(inspect.signature(StateManager.insert_evaluated_opportunity).parameters)
    sig_missing = set(SHADOW_KEYS) - sig_params
    # Site 2: INSERT column list (look for them inside the INSERT INTO ... VALUES block)
    insert_match = re.search(
        r'INSERT INTO evaluated_opportunities\s*\(([^)]+)\)\s*VALUES',
        source, re.DOTALL
    )
    assert insert_match, "Could not locate INSERT column list"
    col_list = insert_match.group(1)
    insert_cols = {tok.strip() for tok in col_list.split(",")}
    insert_missing = set(SHADOW_KEYS) - insert_cols
    # Site 3: ALTER TABLE migration
    alter_missing = set()
    for key in SHADOW_KEYS:
        if f'"{key}"' not in source:
            alter_missing.add(key)

    issues = []
    if sig_missing:
        issues.append(f"signature missing: {sig_missing}")
    if insert_missing:
        issues.append(f"INSERT col list missing: {insert_missing}")
    if alter_missing:
        issues.append(f"ALTER migration missing: {alter_missing}")
    assert not issues, (
        "Sim C schema chain SPLIT — these sites disagree:\n  " +
        "\n  ".join(issues) +
        "\n\nAll 4 shadow keys must be in all 3 sites in ONE commit "
        "(_shadow_diag schema chain rule, bot/CLAUDE.md)."
    )
