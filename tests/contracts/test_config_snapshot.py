"""Per-decision config_hash schema chain (ticket 86b9zkp8p, 2026-05-17).

RCA: For "retrain at any moment", regime-filter of training data needs
deterministic reproducibility. Today it requires correlating
`evaluation_time` with `git log` of `bot/constants.py`, `bot/config.py`,
`market_config.py`, plus mid-day env-var flips (e.g. WEATHER_NO_SIDE_LIVE=1)
which leave no git trace. Operator runtime mutations are invisible.

Goal: every `evaluated_opportunities` + `rejected_opportunities` row
carries a FK to `config_snapshots(id)`. Replay = look up snapshot →
restore the config → re-run.

This test pins the full schema chain in ONE commit:
  - new `config_snapshots` table + indexes
  - new `config_snapshot_id` column on both insert tables
  - `bot.helpers.config_snapshot.{compute_config_snapshot, persist_config_snapshot}`
  - signature pin on `insert_evaluated_opportunity` + `insert_rejection`
  - `MainLoop.__init__` calls `persist_config_snapshot` and stores
    `self.config_snapshot_id`
  - every scanner call site to the two insert helpers passes
    `config_snapshot_id=` kwarg (AST guard)
  - helper is a leaf module (no bot.main_loop / bot.scanner / bot.state)

Bit shipped as schema-chain atomic: see bot/CLAUDE.md "`_shadow_diag`
schema chain" + the new "config_snapshot_id schema chain" entry.
"""

import ast
import inspect
import json
import os
import sys

import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)


# ── Test 1: Schema chain — config_snapshots table ─────────────────────────────


def test_config_snapshots_table_in_schema(tmp_path, monkeypatch):
    """Fresh StateManager creates the config_snapshots table on init."""
    monkeypatch.chdir(tmp_path)
    from bot.state import StateManager
    sm = StateManager()
    try:
        rows = sm.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='config_snapshots'"
        ).fetchall()
        assert len(rows) == 1, "config_snapshots table missing from schema"
        cols = {r[1] for r in sm.conn.execute("PRAGMA table_info(config_snapshots)").fetchall()}
        for required in {
            "id", "config_hash", "captured_at", "git_head_sha",
            "constants_sha", "config_sha", "market_config_sha", "env_flags_json",
        }:
            assert required in cols, f"config_snapshots missing column {required!r}"
    finally:
        sm.conn.close()


def test_evaluated_opportunities_has_config_snapshot_id_column(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    from bot.state import StateManager
    sm = StateManager()
    try:
        cols = {r[1] for r in sm.conn.execute("PRAGMA table_info(evaluated_opportunities)").fetchall()}
        assert "config_snapshot_id" in cols, (
            "evaluated_opportunities is missing the config_snapshot_id FK column"
        )
    finally:
        sm.conn.close()


def test_rejected_opportunities_has_config_snapshot_id_column(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    from bot.state import StateManager
    sm = StateManager()
    try:
        cols = {r[1] for r in sm.conn.execute("PRAGMA table_info(rejected_opportunities)").fetchall()}
        assert "config_snapshot_id" in cols, (
            "rejected_opportunities is missing the config_snapshot_id FK column"
        )
    finally:
        sm.conn.close()


# ── Test 2: compute_config_snapshot returns required bundle ──────────────────


REQUIRED_BUNDLE_KEYS = {
    "config_hash",
    "captured_at",
    "git_head_sha",
    "constants_sha",
    "config_sha",
    "market_config_sha",
    "env_flags_json",
}


def test_compute_config_snapshot_returns_required_keys():
    from bot.helpers.config_snapshot import compute_config_snapshot
    bundle = compute_config_snapshot()
    assert set(bundle.keys()) >= REQUIRED_BUNDLE_KEYS, (
        f"compute_config_snapshot missing keys: {REQUIRED_BUNDLE_KEYS - set(bundle.keys())}"
    )
    # config_hash is a hex sha256 string (64 chars)
    assert isinstance(bundle["config_hash"], str)
    assert len(bundle["config_hash"]) == 64
    int(bundle["config_hash"], 16)  # hex-parses
    # File shas are also hex sha256
    for k in ("constants_sha", "config_sha", "market_config_sha"):
        assert isinstance(bundle[k], str), f"{k} must be str"
        assert len(bundle[k]) == 64, f"{k} must be 64-char sha256 hex"
    # env_flags_json is JSON-parseable
    parsed = json.loads(bundle["env_flags_json"])
    assert isinstance(parsed, dict)


def test_config_hash_stable_across_identical_inputs(monkeypatch):
    """Two calls with the same file contents + env produce the same hash."""
    from bot.helpers.config_snapshot import compute_config_snapshot
    # Pin env to a known state (clear all tracked flags)
    for flag in (
        "CALMLP_ENABLED", "WEATHER_NO_SIDE_LIVE", "HOURLY_NO_SIDE_LIVE",
        "BRACKET_NO_ENABLED", "MEXC_FEED_ENABLED", "BINANCE_FEED_ENABLED",
        "BAND_CALIBRATION_DISABLED_CELLS",
    ):
        monkeypatch.delenv(flag, raising=False)
    a = compute_config_snapshot()
    b = compute_config_snapshot()
    assert a["config_hash"] == b["config_hash"], (
        "config_hash drifted across identical-input calls"
    )


def test_config_hash_changes_when_constants_file_changes(monkeypatch, tmp_path):
    """Mock the constants file path to point to different content; hash changes."""
    from bot.helpers import config_snapshot as cs_mod
    # Capture baseline
    baseline = cs_mod.compute_config_snapshot()
    # Swap the resolver so it reads a different file
    other = tmp_path / "fake_constants.py"
    other.write_text("# fake constants with different content\nDIFFERENT = True\n")
    monkeypatch.setattr(cs_mod, "_CONSTANTS_PATH", str(other))
    drifted = cs_mod.compute_config_snapshot()
    assert baseline["config_hash"] != drifted["config_hash"], (
        "config_hash should change when constants file content changes"
    )
    assert baseline["constants_sha"] != drifted["constants_sha"]


def test_config_hash_changes_when_env_var_changes(monkeypatch):
    from bot.helpers.config_snapshot import compute_config_snapshot
    for flag in (
        "CALMLP_ENABLED", "WEATHER_NO_SIDE_LIVE", "HOURLY_NO_SIDE_LIVE",
        "BRACKET_NO_ENABLED", "MEXC_FEED_ENABLED", "BINANCE_FEED_ENABLED",
        "BAND_CALIBRATION_DISABLED_CELLS",
    ):
        monkeypatch.delenv(flag, raising=False)
    baseline = compute_config_snapshot()
    monkeypatch.setenv("WEATHER_NO_SIDE_LIVE", "1")
    drifted = compute_config_snapshot()
    assert baseline["config_hash"] != drifted["config_hash"], (
        "config_hash should change when a tracked env var flips"
    )


def test_env_flags_json_omits_unset_flags(monkeypatch):
    """Unset env vars are NOT in the JSON — keeps legacy-env hash stable."""
    from bot.helpers.config_snapshot import compute_config_snapshot
    for flag in (
        "CALMLP_ENABLED", "WEATHER_NO_SIDE_LIVE", "HOURLY_NO_SIDE_LIVE",
        "BRACKET_NO_ENABLED", "MEXC_FEED_ENABLED", "BINANCE_FEED_ENABLED",
        "BAND_CALIBRATION_DISABLED_CELLS",
    ):
        monkeypatch.delenv(flag, raising=False)
    bundle = compute_config_snapshot()
    parsed = json.loads(bundle["env_flags_json"])
    # No tracked flags set → dict should be empty
    assert parsed == {}, (
        f"env_flags_json should omit unset flags, got: {parsed!r}"
    )
    # Now set one
    monkeypatch.setenv("CALMLP_ENABLED", "1")
    bundle2 = compute_config_snapshot()
    parsed2 = json.loads(bundle2["env_flags_json"])
    assert parsed2 == {"CALMLP_ENABLED": "1"}, (
        f"env_flags_json should include exactly the set flags, got: {parsed2!r}"
    )


def test_env_flags_json_is_sorted_key_form(monkeypatch):
    from bot.helpers.config_snapshot import compute_config_snapshot
    monkeypatch.setenv("WEATHER_NO_SIDE_LIVE", "1")
    monkeypatch.setenv("CALMLP_ENABLED", "1")
    monkeypatch.setenv("BRACKET_NO_ENABLED", "1")
    bundle = compute_config_snapshot()
    raw = bundle["env_flags_json"]
    # If keys were inserted in a stable sorted order, re-serializing with
    # sort_keys=True should be byte-identical.
    parsed = json.loads(raw)
    assert raw == json.dumps(parsed, sort_keys=True), (
        f"env_flags_json must be sorted-key JSON; got {raw!r}"
    )


# ── Test 3: persist_config_snapshot persists + returns id ─────────────────────


def test_persist_config_snapshot_returns_id(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    from bot.state import StateManager
    from bot.helpers.config_snapshot import persist_config_snapshot
    sm = StateManager()
    try:
        snap_id = persist_config_snapshot(sm.conn)
        assert isinstance(snap_id, int) and snap_id > 0
        # Verify a row landed
        row = sm.conn.execute(
            "SELECT id, config_hash FROM config_snapshots WHERE id=?", (snap_id,)
        ).fetchone()
        assert row is not None, "persist_config_snapshot did not insert a row"
        # Calling again with same hash returns the same id (INSERT OR IGNORE)
        snap_id_2 = persist_config_snapshot(sm.conn)
        assert snap_id_2 == snap_id, (
            "persist_config_snapshot should return the SAME id for the same hash "
            f"(first={snap_id}, second={snap_id_2})"
        )
        # Still only one row
        n_rows = sm.conn.execute(
            "SELECT COUNT(*) FROM config_snapshots WHERE config_hash=?",
            (row[1],),
        ).fetchone()[0]
        assert n_rows == 1
    finally:
        sm.conn.close()


def test_persist_config_snapshot_distinct_hashes_get_distinct_ids(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    from bot.state import StateManager
    from bot.helpers import config_snapshot as cs_mod
    sm = StateManager()
    try:
        # Baseline insert
        id_a = cs_mod.persist_config_snapshot(sm.conn)
        # Drift env to change the hash
        monkeypatch.setenv("WEATHER_NO_SIDE_LIVE", "1")
        id_b = cs_mod.persist_config_snapshot(sm.conn)
        assert id_a != id_b, (
            f"Distinct inputs should produce distinct ids ({id_a} vs {id_b})"
        )
        # And exactly TWO rows now
        n_rows = sm.conn.execute("SELECT COUNT(*) FROM config_snapshots").fetchone()[0]
        assert n_rows == 2
    finally:
        sm.conn.close()


# ── Test 4: signature pins on the two insert helpers ─────────────────────────


def test_insert_evaluated_opportunity_accepts_config_snapshot_id_kwarg():
    from bot.state import StateManager
    sig = inspect.signature(StateManager.insert_evaluated_opportunity)
    assert "config_snapshot_id" in sig.parameters, (
        "insert_evaluated_opportunity is missing the config_snapshot_id kwarg"
    )
    p = sig.parameters["config_snapshot_id"]
    # Must be nullable for backward compat (existing callers + tests)
    assert p.default is None, (
        "config_snapshot_id should default to None for backward compat"
    )


def test_insert_rejection_accepts_config_snapshot_id_kwarg():
    from bot.state import StateManager
    sig = inspect.signature(StateManager.insert_rejection)
    assert "config_snapshot_id" in sig.parameters, (
        "insert_rejection is missing the config_snapshot_id kwarg"
    )
    p = sig.parameters["config_snapshot_id"]
    assert p.default is None, (
        "config_snapshot_id should default to None for backward compat"
    )


# ── Test 5: round-trip — the value sticks ────────────────────────────────────


def test_inserted_eval_row_has_config_snapshot_id(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    from bot.state import StateManager
    from bot.helpers.config_snapshot import persist_config_snapshot
    sm = StateManager()
    try:
        snap_id = persist_config_snapshot(sm.conn)
        sm.insert_evaluated_opportunity(
            ticker="KXBTC-T1", event_ticker="KXBTC", asset="BTC",
            filter_stage="candidate", config_snapshot_id=snap_id,
        )
        row = sm.conn.execute(
            "SELECT config_snapshot_id FROM evaluated_opportunities WHERE ticker=?",
            ("KXBTC-T1",),
        ).fetchone()
        assert row is not None
        assert row[0] == snap_id, (
            f"config_snapshot_id should round-trip ({row[0]} != {snap_id})"
        )
    finally:
        sm.conn.close()


def test_inserted_rejection_row_has_config_snapshot_id(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    from bot.state import StateManager
    from bot.helpers.config_snapshot import persist_config_snapshot
    sm = StateManager()
    try:
        snap_id = persist_config_snapshot(sm.conn)
        sm.insert_rejection(
            ticker="KXBTC-T2", event_ticker="KXBTC", asset="BTC",
            rejection_reason="test_reason",
            z_score=None, spot_price=100.0, threshold=99.0, volatility=0.1,
            market_price=50, seconds_to_close=300.0, calibrated_prob=0.5,
            config_snapshot_id=snap_id,
        )
        row = sm.conn.execute(
            "SELECT config_snapshot_id FROM rejected_opportunities WHERE ticker=?",
            ("KXBTC-T2",),
        ).fetchone()
        assert row is not None
        assert row[0] == snap_id, (
            f"config_snapshot_id should round-trip on rejection rows ({row[0]} != {snap_id})"
        )
    finally:
        sm.conn.close()


def test_replay_lookup_by_evaluation_row(tmp_path, monkeypatch):
    """Given an eval row, JOIN to config_snapshots → reproduce env_flags_json."""
    monkeypatch.chdir(tmp_path)
    for flag in (
        "CALMLP_ENABLED", "WEATHER_NO_SIDE_LIVE", "HOURLY_NO_SIDE_LIVE",
        "BRACKET_NO_ENABLED", "MEXC_FEED_ENABLED", "BINANCE_FEED_ENABLED",
        "BAND_CALIBRATION_DISABLED_CELLS",
    ):
        monkeypatch.delenv(flag, raising=False)
    monkeypatch.setenv("WEATHER_NO_SIDE_LIVE", "1")
    from bot.state import StateManager
    from bot.helpers.config_snapshot import persist_config_snapshot
    sm = StateManager()
    try:
        snap_id = persist_config_snapshot(sm.conn)
        sm.insert_evaluated_opportunity(
            ticker="KXBTC-R1", event_ticker="KXBTC", asset="BTC",
            filter_stage="candidate", config_snapshot_id=snap_id,
        )
        joined = sm.conn.execute(
            """
            SELECT cs.env_flags_json
              FROM evaluated_opportunities eo
              JOIN config_snapshots cs ON cs.id = eo.config_snapshot_id
             WHERE eo.ticker = ?
            """,
            ("KXBTC-R1",),
        ).fetchone()
        assert joined is not None, "Replay JOIN returned no rows"
        parsed = json.loads(joined[0])
        assert parsed.get("WEATHER_NO_SIDE_LIVE") == "1"
    finally:
        sm.conn.close()


# ── Test 6: AST guards ───────────────────────────────────────────────────────


def _read_module_ast(relpath):
    full = os.path.join(PROJECT_ROOT, relpath)
    with open(full) as f:
        source = f.read()
    return ast.parse(source, filename=full)


def test_scanner_passes_config_snapshot_id_to_inserts():
    """Every scanner call to insert_evaluated_opportunity / insert_rejection
    must pass `config_snapshot_id=...` so the FK is populated.

    Per CLAUDE.md schema-chain discipline: a new column that isn't passed at
    the call site gets silently dropped at write time.
    """
    tree = _read_module_ast("bot/scanner/__init__.py")
    INSERT_NAMES = {"insert_evaluated_opportunity", "insert_rejection"}
    missing = []  # list of (lineno, call_name)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        # We only care about `something.insert_X(...)` method calls
        if not isinstance(node.func, ast.Attribute):
            continue
        if node.func.attr not in INSERT_NAMES:
            continue
        kwarg_names = {kw.arg for kw in node.keywords if kw.arg is not None}
        # `**_shadow_diag` and similar leave kw.arg = None — we treat the
        # explicit-name kwargs as the surface. config_snapshot_id is NOT in
        # _shadow_diag, so it MUST appear by name.
        if "config_snapshot_id" not in kwarg_names:
            missing.append((node.lineno, node.func.attr))
    assert not missing, (
        f"Scanner call sites missing config_snapshot_id kwarg ({len(missing)}):\n"
        + "\n".join(f"  line {ln}: {nm}(...)" for ln, nm in missing[:20])
        + (f"\n  ...and {len(missing)-20} more" if len(missing) > 20 else "")
    )


def test_helpers_leaf_contract_for_config_snapshot():
    """bot.helpers.config_snapshot must not import bot.main_loop / bot.scanner /
    bot.state — leaf rule (mirrors .importlinter helpers-leaf contract).

    Helper depends only on stdlib + reads `bot/constants.py` / `bot/config.py` /
    `market_config.py` as FILE CONTENTS (no bot.X module imports needed).
    """
    tree = _read_module_ast("bot/helpers/config_snapshot.py")
    FORBIDDEN = {"bot.main_loop", "bot.scanner", "bot.state"}
    bad = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            for f in FORBIDDEN:
                if mod == f or mod.startswith(f + "."):
                    bad.append((node.lineno, f"from {mod} import ..."))
        elif isinstance(node, ast.Import):
            for alias in node.names:
                for f in FORBIDDEN:
                    if alias.name == f or alias.name.startswith(f + "."):
                        bad.append((node.lineno, f"import {alias.name}"))
    assert not bad, (
        "bot.helpers.config_snapshot violates helpers-leaf contract:\n"
        + "\n".join(f"  line {ln}: {what}" for ln, what in bad)
    )


def test_main_loop_init_calls_persist_config_snapshot():
    """MainLoop.__init__ must call persist_config_snapshot() and store the id.

    Without this, the helper exists but nothing ever populates
    self.config_snapshot_id → scanner's `self._ml.config_snapshot_id` AttributeError.
    """
    tree = _read_module_ast("bot/main_loop.py")
    found_call = False
    found_self_assign = False
    for cls in ast.walk(tree):
        if not (isinstance(cls, ast.ClassDef) and cls.name == "MainLoop"):
            continue
        for fn in cls.body:
            if not (isinstance(fn, ast.FunctionDef) and fn.name == "__init__"):
                continue
            for node in ast.walk(fn):
                if isinstance(node, ast.Call):
                    f = node.func
                    if isinstance(f, ast.Name) and f.id == "persist_config_snapshot":
                        found_call = True
                    elif isinstance(f, ast.Attribute) and f.attr == "persist_config_snapshot":
                        found_call = True
                if isinstance(node, ast.Assign):
                    for tgt in node.targets:
                        if (isinstance(tgt, ast.Attribute)
                            and isinstance(tgt.value, ast.Name)
                            and tgt.value.id == "self"
                            and tgt.attr == "config_snapshot_id"):
                            found_self_assign = True
    assert found_call, "MainLoop.__init__ must call persist_config_snapshot(...)"
    assert found_self_assign, (
        "MainLoop.__init__ must assign self.config_snapshot_id = ..."
    )
