"""DB INSERT ↔ schema parity contract.

Failure class: a new column is added to the table schema (via
_create_tables migration loop) but the INSERT statement that writes to
that table is not updated — the column is silently 100% NULL forever.

Apr 22 2026 shipped TWO bugs of this exact shape in one week:
- calibration_confidence added to evaluated_opportunities schema but
  StateManager.insert_evaluated_opportunity hardcoded the input to None
- sports Tier 4/5 columns added to evaluated_opportunities schema but
  sports_engine._insert_evaluated_opportunity omitted them from its raw
  INSERT column list

Both were invisible for ~7 days each because no test asserted the
invariant. This module enforces three contracts:

    A. Canonical INSERT covers schema.
       bot/_impl.py's primary INSERT INTO evaluated_opportunities must list
       every column the schema defines (minus AUTOINCREMENT `id`).
       Catches calibration_confidence-class bugs.

    B. INSERT columns exist in schema.
       Every column listed in any production INSERT must be a real
       column in the table. Catches typos / stale column names.

    C. Raw-inserter coverage.
       Files in tests/test_call_sites.py::ALLOWED_RAW_INSERTERS that
       bypass StateManager must still list all Tier 4 + Tier 5 columns
       in their raw INSERT statement. Catches sports-engine-class bugs.
"""

import os
import re
import sys

import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)


TIER_4_COLUMNS = frozenset({
    "hour_of_day_utc", "day_of_week", "is_weekend",
    "minutes_since_us_open", "is_fomc_day", "is_cpi_day",
})

TIER_5_COLUMNS = frozenset({
    "spot_distance_to_strike_sigma", "prob_breakeven_gap",
    "kelly_vs_cap_ratio", "calibration_confidence",
})

# Files permitted to write directly to evaluated_opportunities without
# routing through StateManager.insert_evaluated_opportunity. The
# canonical definition lives in tests/test_call_sites.py:184; this set
# is duplicated here to avoid a cross-test-module import. The
# `test_allowed_raw_inserters_set_matches_canonical` test below pins
# both copies in lock-step — Round-3 adversarial MAJOR-1 fix for the
# silent-bypass class where a future raw inserter is added to one set
# but not the other and slips past the data_provenance contract.
ALLOWED_RAW_INSERTERS = {"bot/_impl.py", "sports_engine.py"}


def test_allowed_raw_inserters_set_matches_canonical():
    """Lock-step pin: the local ALLOWED_RAW_INSERTERS must equal the
    canonical set in tests/test_call_sites.py. If they drift, a future
    raw inserter added to test_call_sites.py won't be checked by
    TestRawInserterTierCoverage or TestRawInserterDataProvenance, which
    is the exact failure mode this bit's writer fix was filed to
    prevent. See kb/decisions/sprint-a-bit-2-shipped-may09.md round-3
    MAJOR-1 fix.
    """
    from tests.test_call_sites import (
        TestEvaluatedOpportunitiesTierContract as _Canonical,
    )
    canonical = set(_Canonical.ALLOWED_RAW_INSERTERS)
    assert canonical == ALLOWED_RAW_INSERTERS, (
        f"ALLOWED_RAW_INSERTERS drift detected:\n"
        f"  tests/test_call_sites.py:184 → {sorted(canonical)}\n"
        f"  tests/test_insert_schema_parity.py:54 → "
        f"{sorted(ALLOWED_RAW_INSERTERS)}\n"
        f"Update both copies in lock-step. The local copy in "
        f"test_insert_schema_parity.py drives "
        f"RAW_INSERTERS_FOR_PROVENANCE which gates the data_provenance "
        f"contract tests."
    )

# Columns that are intentionally NULL at INSERT time and populated by a
# later UPDATE statement. Adding to this allowlist is an explicit
# declaration that the column is UPDATE-populated; new columns are
# required to be INSERT-populated unless listed here. Search bot/_impl.py for
# `UPDATE evaluated_opportunities` to verify a column belongs here.
EVAL_OPP_UPDATE_POPULATED = frozenset({
    "counterfactual_pnl",     # SettlementTracker on settle
    "market_result",          # SettlementTracker on settle
    "settled_time",           # SettlementTracker on settle
    "taker_ask_at_submit",    # OrderExecutor on maker→taker escalation
})

# Production files scanned for INSERT statements.
PRODUCTION_FILES = [
    "bot/_impl.py",
    "analyst.py",
    "auditor.py",
    "capital_allocator.py",
    "dashboard_snapshot.py",
    "fifteenm_shadow.py",
    "hourly_alt_shadow.py",
    "researcher.py",
    "sports_engine.py",
    "spx_engine.py",
    "spx_harrv_shadow.py",
    "supabase_sync.py",
    "watchdog.py",
    "weather_engine.py",
]


def _load_live_schema():
    """Build the full schema by instantiating StateManager against an
    in-memory sqlite DB. Returns {table_name: frozenset(column_names)}.

    This is the single source of truth for what columns exist. It picks
    up CREATE TABLE definitions AND the ALTER TABLE ADD COLUMN migration
    loops that extend each table over time.
    """
    import bot
    sm = bot.StateManager(":memory:")
    tables = sm.conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()
    schema = {}
    for row in tables:
        name = row["name"]
        cols = sm.conn.execute(f"PRAGMA table_info({name})").fetchall()
        schema[name] = frozenset(c["name"] for c in cols)
    return schema


# One regex to find INSERT … INTO <table> (col1, col2, …) across the
# entire file, tolerating any whitespace/newlines inside the column list
# parentheses. Non-greedy on the inner match to avoid swallowing a VALUES
# clause from a later INSERT.
INSERT_WITH_COLS = re.compile(
    r"INSERT\s+(?:OR\s+\w+\s+)?INTO\s+"
    r"(?P<table>\w+)\s*"
    r"\(\s*(?P<cols>[^)]+?)\s*\)",
    re.IGNORECASE | re.DOTALL,
)


def _extract_insert_columns(source: str):
    """Yield (table, [col, col, …], char_offset) for every
    `INSERT [OR REPLACE] INTO <table> (col, …)` in source."""
    for m in INSERT_WITH_COLS.finditer(source):
        table = m.group("table")
        cols_blob = m.group("cols")
        cols = [c.strip() for c in cols_blob.split(",") if c.strip()]
        # Guard against matching INSERT statements whose parenthesized
        # group is actually a VALUES tuple ("?,?,?,?") — skip those.
        if cols and all(re.match(r"^\??\w*$", c) for c in cols) and "?" in cols[0]:
            continue
        # Only yield when every "column" looks like a real identifier.
        if cols and all(re.match(r"^[A-Za-z_][A-Za-z_0-9]*$", c) for c in cols):
            yield table, cols, m.start()


@pytest.fixture(scope="module")
def live_schema():
    return _load_live_schema()


class TestCanonicalInsertCoversSchema:
    """bot/_impl.py's INSERT INTO evaluated_opportunities must cover every
    schema column except `id` (AUTOINCREMENT).

    This is the bug-pattern that let calibration_confidence stay 100%
    NULL for 7 days in April 2026: the ALTER TABLE added the column,
    but the primary INSERT statement was not updated.
    """

    def test_evaluated_opportunities_canonical_insert_covers_all_columns(
        self, live_schema
    ):
        schema_cols = live_schema["evaluated_opportunities"]

        # Bit 7.1 retarget (2026-05-10): StateManager (incl. the canonical
        # insert_evaluated_opportunity INSERT) moved to bot/state.py.
        bot_path = os.path.join(PROJECT_ROOT, "bot/state.py")
        with open(bot_path) as f:
            source = f.read()

        # The canonical INSERT in bot/_impl.py is the one inside
        # StateManager.insert_evaluated_opportunity. It's distinguished
        # by being a plain `INSERT INTO evaluated_opportunities` (not
        # `INSERT OR REPLACE`) and by being the longest column list —
        # auto-compute + Tier 4/5 live there.
        best_cols = None
        for table, cols, _ in _extract_insert_columns(source):
            if table != "evaluated_opportunities":
                continue
            if best_cols is None or len(cols) > len(best_cols):
                best_cols = cols

        assert best_cols is not None, (
            "No INSERT INTO evaluated_opportunities found in bot/_impl.py — "
            "canonical path has been removed or renamed."
        )

        insert_cols = set(best_cols)
        required = schema_cols - {"id"} - EVAL_OPP_UPDATE_POPULATED
        missing = required - insert_cols

        assert not missing, (
            f"bot/_impl.py canonical INSERT INTO evaluated_opportunities is "
            f"missing {len(missing)} schema column(s): {sorted(missing)}. "
            f"These columns exist in the schema but the INSERT does not "
            f"populate them — every row will have NULL for these columns. "
            f"Fix options: (a) add them to the column list + VALUES tuple "
            f"in StateManager.insert_evaluated_opportunity, or (b) if the "
            f"column is intentionally UPDATE-populated later (like "
            f"market_result), add it to EVAL_OPP_UPDATE_POPULATED with a "
            f"comment naming the UPDATE site."
        )


class TestInsertColumnsExistInSchema:
    """Every column named in any production INSERT must be a real
    column in the table's live schema.

    Catches:
      - Typos (`calibration_confidences`)
      - Legacy column names after a rename
      - Copy-paste between tables with different schemas
    """

    def test_all_production_insert_columns_valid(self, live_schema):
        failures = []
        for fname in PRODUCTION_FILES:
            fpath = os.path.join(PROJECT_ROOT, fname)
            if not os.path.exists(fpath):
                continue
            with open(fpath) as f:
                source = f.read()
            for table, cols, offset in _extract_insert_columns(source):
                if table not in live_schema:
                    # Third-party/test table — skip rather than fail.
                    continue
                schema_cols = live_schema[table]
                unknown = [c for c in cols if c not in schema_cols]
                if unknown:
                    line = source.count("\n", 0, offset) + 1
                    failures.append(
                        f"{fname}:{line} INSERT INTO {table} references "
                        f"unknown column(s): {unknown}"
                    )
        assert not failures, "\n".join(failures)


class TestRawInserterTierCoverage:
    """Files in ALLOWED_RAW_INSERTERS that write directly to
    evaluated_opportunities must include every Tier 4 + Tier 5 column
    in their INSERT column list (not just mention the column name
    somewhere in the file).

    Stricter than TestEvaluatedOpportunitiesTierContract in
    test_call_sites.py — that test only checks `hour_of_day_utc` appears
    anywhere in source. A comment would satisfy it. This checks the
    actual INSERT column list.
    """

    def test_sports_engine_raw_inserts_populate_tier_4_5(self, live_schema):
        fpath = os.path.join(PROJECT_ROOT, "sports_engine.py")
        if not os.path.exists(fpath):
            pytest.skip("sports_engine.py not present")
        with open(fpath) as f:
            source = f.read()

        inserts = [
            (cols, offset) for t, cols, offset in _extract_insert_columns(source)
            if t == "evaluated_opportunities"
        ]
        assert inserts, (
            "sports_engine.py is in ALLOWED_RAW_INSERTERS but has no "
            "INSERT INTO evaluated_opportunities — allowlist is stale."
        )

        required = TIER_4_COLUMNS | TIER_5_COLUMNS
        failures = []
        for cols, offset in inserts:
            line = source.count("\n", 0, offset) + 1
            missing = required - set(cols)
            if missing:
                failures.append(
                    f"sports_engine.py:{line} INSERT missing Tier 4/5 "
                    f"columns: {sorted(missing)}"
                )

        assert not failures, (
            "\n".join(failures) +
            "\nsports_engine.py bypasses StateManager's auto-populate "
            "path (separate sqlite conn for cross-thread writes) and "
            "must hand-populate Tier 4 + Tier 5 in every raw INSERT. "
            "See kb/concepts/contract-testing.md."
        )


# Files in ALLOWED_RAW_INSERTERS that the data_provenance contract
# tests should iterate over. bot/_impl.py is the canonical writer using
# `INSERT … ON CONFLICT … DO UPDATE … COALESCE` (preserves prior
# provenance) and is enforced by TestCanonicalInsertCoversSchema +
# StateManager's default param at bot/_impl.py:2119, NOT by these tests.
# Round-2 M2.1: parametrize so any future raw-inserter file added to
# ALLOWED_RAW_INSERTERS automatically inherits the contract.
RAW_INSERTERS_FOR_PROVENANCE = sorted(ALLOWED_RAW_INSERTERS - {"bot/_impl.py"})


class TestRawInserterDataProvenance:
    """Sprint A.2 — every raw INSERT in ALLOWED_RAW_INSERTERS that writes
    to evaluated_opportunities must include `data_provenance` in its
    column list AND pass the literal `'live_ws'` value (matching the
    `StateManager.insert_evaluated_opportunity` default in bot/_impl.py).

    Bug history: sports_engine.py:_insert_evaluated_opportunity bypasses
    StateManager via its own DB connection (cross-thread safety), and
    its raw INSERT column list omitted `data_provenance` from G-6 ship
    (2026-05-02) onward. Result: every sports row through 2026-05-09
    was written with NULL data_provenance — 1,183 rows on live VPS.
    The G-6 stamp script (`scripts/stamp_data_provenance.py`) is 15m-
    scoped, so it never fixes sports rows; only the writer can.

    See kb/decisions/sprint-a-bit-2-rca-may09.md for full RCA.
    """

    @pytest.mark.parametrize("fname", RAW_INSERTERS_FOR_PROVENANCE)
    def test_raw_inserts_carry_data_provenance(self, fname):
        fpath = os.path.join(PROJECT_ROOT, fname)
        if not os.path.exists(fpath):
            pytest.skip(f"{fname} not present")
        with open(fpath) as f:
            source = f.read()

        inserts = [
            (cols, offset) for t, cols, offset in _extract_insert_columns(source)
            if t == "evaluated_opportunities"
        ]
        assert inserts, (
            f"{fname} is in ALLOWED_RAW_INSERTERS but has no "
            f"INSERT INTO evaluated_opportunities — allowlist is stale "
            f"(remove from ALLOWED_RAW_INSERTERS in tests/test_call_sites.py)."
        )

        failures = []
        for cols, offset in inserts:
            line = source.count("\n", 0, offset) + 1
            if "data_provenance" not in cols:
                failures.append(
                    f"{fname}:{line} INSERT missing data_provenance column"
                )

        assert not failures, (
            "\n".join(failures) +
            f"\n{fname} raw INSERTs bypass "
            "StateManager.insert_evaluated_opportunity's default "
            "(`data_provenance='live_ws'`), so every column it writes "
            "must be hand-populated. Adding the column to the column "
            "list is necessary; the value passed must be 'live_ws' to "
            "match StateManager's default. Pin enforced by "
            "test_raw_inserts_pass_live_ws_value below. "
            "See kb/decisions/sprint-a-bit-2-rca-may09.md."
        )

    @pytest.mark.parametrize("fname", RAW_INSERTERS_FOR_PROVENANCE)
    def test_raw_inserts_pass_live_ws_value(self, fname):
        """The column-list pin above is necessary but not sufficient —
        a future drift could add the column but pass `None` or a
        wrong-vocab string. This guard asserts the literal `'live_ws'`
        appears in each INSERT's *python parameters tuple* (not the
        surrounding comments or SQL).

        Round-1 adversarial M2 fix: a window-based check would pass if
        a comment containing `'live_ws'` lived nearby (e.g., the very
        comment we added documenting the value). This implementation
        anchors on `conn.execute(\"\"\"…\"\"\", (…))` and scans only the
        captured python params group — a future maintainer who removes
        `'live_ws'` from VALUES while leaving the comment will fail.

        Round-2 adversarial M2.2 fix: the regex now accepts the broader
        `INSERT [OR <RESOLUTION>] INTO` family (covers OR REPLACE, OR
        IGNORE, plain INSERT, and the migration target form `INSERT …
        ON CONFLICT … DO UPDATE …`). A future migration to ON CONFLICT
        per the M1 comment block at sports_engine.py won't silently
        retire the value-pin — the regex will keep matching.
        """
        fpath = os.path.join(PROJECT_ROOT, fname)
        if not os.path.exists(fpath):
            pytest.skip(f"{fname} not present")
        with open(fpath) as f:
            source = f.read()

        # Match `conn.execute("""…INSERT [OR <RESOLUTION>] INTO
        # evaluated_opportunities…""", (<python_tuple>))`. Anchoring on
        # `"""\s*,\s*\(` ensures the captured group is exactly the
        # python params tuple — comments above the call cannot leak in.
        # Accept any conflict-resolution clause (OR REPLACE, OR IGNORE,
        # bare INSERT, or no clause at all — ON CONFLICT lives after
        # the column list and doesn't affect this anchor).
        block_re = re.compile(
            r'conn\.execute\(\s*"""\s*'
            r'INSERT(?:\s+OR\s+\w+)?\s+INTO\s+evaluated_opportunities'
            r'.*?"""\s*,\s*\((?P<params>.*?)\)\s*\)',
            re.DOTALL | re.IGNORECASE,
        )
        matches = list(block_re.finditer(source))
        assert matches, (
            f"{fname}: no INSERT INTO evaluated_opportunities "
            f"conn.execute(...) blocks found via the params-tuple regex. "
            f"If the call shape changed (refactored to a helper, or the "
            f"SQL is no longer in a triple-quoted string), update this "
            f"test to match — but DO NOT drop the data_provenance value "
            f"pin. See kb/decisions/sprint-a-bit-2-shipped-may09.md."
        )

        failures = []
        for m in matches:
            params = m.group("params")
            line = source.count("\n", 0, m.start()) + 1
            if "'live_ws'" not in params and '"live_ws"' not in params:
                failures.append(
                    f"{fname}:{line} INSERT params tuple does not "
                    f"contain literal 'live_ws' for data_provenance. "
                    f"If the column was added but value is wrong, the "
                    f"row writes a NULL or wrong-vocab provenance — "
                    f"breaks `extract_data.py --provenance-filter` "
                    f"semantics."
                )
        assert not failures, "\n".join(failures)
