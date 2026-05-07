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
# routing through StateManager.insert_evaluated_opportunity.
ALLOWED_RAW_INSERTERS = {"bot/_impl.py", "sports_engine.py"}

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

        bot_path = os.path.join(PROJECT_ROOT, "bot/_impl.py")
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
