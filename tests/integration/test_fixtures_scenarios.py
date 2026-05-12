"""Sprint 13 Bit 13.4-rest (2026-05-11) — fixture-scenario smoke gate.

Each `tests/fixtures/scenarios/<name>.sql` file must `executescript`
cleanly into a fresh sqlite3 DB and seed >0 rows in each of the tables
it CREATEs. This is a "no-broken-fixtures" gate — not a behavioral
test. Behavioral assertions live in the consumer tests.

Mirrors the WAL + busy_timeout pragmas the bot uses at runtime (per
`scripts/CLAUDE.md`) so the smoke also catches accidental DDL/pragma
incompatibility.

Two parametrizations:

1. ``blank-slate`` — load the scenario into an empty DB. Verifies the
   scenario's own CREATE-TABLE DDL + INSERTs are internally consistent.
   This is the back-compat smoke gate.
2. ``canonical-ddl`` — initialize the canonical bot tables FIRST
   (StateManager + FifteenMShadowEngine + market-observations
   snapshotter DDL), THEN apply the scenario. Since the scenarios use
   ``CREATE TABLE IF NOT EXISTS``, the canonical schemas win — any
   column drift in the scenario's INSERTs surfaces as ``OperationalError``.
   This is the LOAD-BEARING test (R1 M3): blank-slate alone is
   silent-accept for fabricated columns.

Companion to ``tests/unit/test_makefile.py::test_bit_13_4_*`` which pins
directory + README + the seed scenario.
"""

from __future__ import annotations

import logging
import re
import sqlite3
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCENARIOS_DIR = REPO_ROOT / "tests/fixtures/scenarios"


def _discover_scenarios() -> list[Path]:
    """All .sql fixture scenarios."""
    return sorted(SCENARIOS_DIR.glob("*.sql"))


def _table_names_from_sql(sql: str) -> list[str]:
    """Extract table names from `CREATE TABLE [IF NOT EXISTS] <name> (`."""
    pattern = re.compile(
        r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?(\w+)\s*\(",
        re.IGNORECASE,
    )
    return pattern.findall(sql)


def _seed_canonical_tables(db_path: Path) -> None:
    """Pre-create the canonical bot tables in ``db_path`` (StateManager +
    FifteenMShadowEngine + market_observations_continuous via the
    snapshotter DDL). Returns nothing; on success the DB has the
    production schema.

    Used by the ``canonical-ddl`` parametrization to catch column drift
    in scenario INSERTs that ``CREATE TABLE IF NOT EXISTS`` would
    otherwise silent-accept against a blank slate.
    """
    # Silence the cal_mlp warmup tracebacks that StateManager triggers when
    # no models dir is present — the canonical-DDL test only cares about
    # schema, not the calibration warmup contract.
    logging.disable(logging.CRITICAL)
    try:
        from bot.state import StateManager
        # StateManager init runs _create_tables + all migrations.
        mgr = StateManager(str(db_path))
        try:
            # 15M shadow signals table (separate engine).
            from bot.shadows.fifteenm_shadow import FifteenMShadowEngine
            shadow = FifteenMShadowEngine(str(db_path))
            shadow._ensure_db()
            if shadow._db_conn is not None:
                shadow._db_conn.close()
            # Snapshotter DDL for market_observations_continuous —
            # the canonical writer is market_observations_snapshotter.py.
            import market_observations_snapshotter as snap
            mgr.conn.executescript(snap._DDL)
            mgr.conn.commit()
        finally:
            mgr.conn.close()
    finally:
        logging.disable(logging.NOTSET)


def test_scenarios_directory_discoverable():
    """The directory + at least the 3 Bit-13.4 scenarios must exist."""
    assert SCENARIOS_DIR.is_dir(), (
        f"scenarios dir missing at {SCENARIOS_DIR}"
    )
    scenarios = _discover_scenarios()
    names = {p.name for p in scenarios}
    # Bit 13.4-narrow seed + Bit 13.4-rest pair.
    for required in (
        "typical-15m-trade.sql",
        "sub-floor-ioc-loss-2.sql",
        "cell-block-rejection-3.sql",
    ):
        assert required in names, (
            f"expected scenario {required!r} missing from {SCENARIOS_DIR}"
        )


@pytest.mark.parametrize(
    "scenario_path",
    _discover_scenarios(),
    ids=lambda p: p.name,
)
def test_scenario_loads_cleanly_and_seeds_rows(scenario_path: Path, tmp_path):
    """Blank-slate parametrization: every scenario .sql must:

    1. ``executescript`` cleanly into a fresh sqlite3 DB (no SQL errors).
    2. Seed >0 rows in EACH table it CREATEs.

    This is the back-compat smoke gate (silent-accept of fabricated
    columns is intentional here — the load-bearing test below catches
    that).
    """
    sql = scenario_path.read_text()
    table_names = _table_names_from_sql(sql)
    assert table_names, (
        f"{scenario_path.name} declares no CREATE TABLE — every scenario "
        f"must declare its own DDL for self-containment."
    )

    db_path = tmp_path / "scenario.db"
    conn = sqlite3.connect(str(db_path))
    try:
        # Match bot runtime pragmas (scripts/CLAUDE.md).
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=10000")
        # (1) Loads cleanly.
        conn.executescript(sql)
        # (2) Each declared table has >0 rows.
        for table in table_names:
            n = conn.execute(
                f"SELECT COUNT(*) FROM {table}"
            ).fetchone()[0]
            assert n > 0, (
                f"{scenario_path.name}: table {table!r} has 0 rows after "
                f"executescript — scenario should seed representative data, "
                f"not just declare DDL."
            )
    finally:
        conn.close()


@pytest.mark.parametrize(
    "scenario_path",
    _discover_scenarios(),
    ids=lambda p: p.name,
)
def test_scenario_loads_against_canonical_ddl(scenario_path: Path, tmp_path):
    """Canonical-DDL parametrization (R1 M3 — LOAD-BEARING):

    Pre-create the canonical bot tables via ``StateManager`` +
    ``FifteenMShadowEngine`` + snapshotter DDL, THEN apply the scenario.
    ``CREATE TABLE IF NOT EXISTS`` becomes a no-op, so the scenario's
    INSERTs must use ONLY columns that exist in the production schema.

    This catches the failure mode the blank-slate test cannot: a
    scenario that fabricates a column name (e.g., the R1 C2 finding
    where ``fifteenm_shadow_signals`` had invented ``approach`` /
    ``signal_time`` / ``would_size`` / ``counterfactual_pnl`` columns)
    will now surface as ``OperationalError`` here.
    """
    db_path = tmp_path / "canonical.db"
    _seed_canonical_tables(db_path)

    sql = scenario_path.read_text()
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=10000")
        # Apply scenario — any column drift surfaces as OperationalError.
        conn.executescript(sql)
        # Sanity: every CREATEd table is non-empty after applying.
        for table in _table_names_from_sql(sql):
            n = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            assert n > 0, (
                f"{scenario_path.name}: table {table!r} has 0 rows after "
                f"applying scenario onto canonical schema."
            )
    finally:
        conn.close()
