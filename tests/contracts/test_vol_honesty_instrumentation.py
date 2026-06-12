"""Bit V.3 — vol-honesty persistence + continuous monitor contract tests.

Postmortem: ``kb/failures/vol-engine-beta-dvol-deflation-jun12.md`` —
``blended_rv`` for non-BTC/ETH assets was ``clamp(beta, 0.5, 3.0) ×
BTC_DVOL`` for ~4 months and NO in-scope artifact disagreed with it.
Lesson **L-VOL-2: every model input with a cheap independent estimate
gets a continuous honesty monitor.** Adversarial review cannot catch a
wrong number every in-scope artifact agrees on.

V.3 is the soak's measurement layer (re-arm gate per
``kb/decisions/longshot-twap-live-small-plan.md``, pre-registered
2026-06-12: per-asset median raw_blended_rv/tape_rv300 ∈ [0.8, 1.25] +
zero monitor breaches):

1. SCHEMA CHAIN (mirrors the Bit-S.1 ``spot_staleness_seconds`` 8-site
   pattern exactly): persist ``tape_rv300 REAL`` + ``raw_blended_rv
   REAL`` on ``evaluated_opportunities``, auto-filled from the two V.1
   per-asset StateManager caches (``_scan_tape_rv_cache`` /
   ``_scan_raw_blended_rv_cache``). The raw cache is OVERWRITE-ONLY
   (never popped — V.1-R5), so its auto-fill is GATED on the tape cache
   having a value for the asset: tape-present is the freshness
   certificate (the tape cache pops on every dishonest/stale tick), and
   the only consumer of raw_blended_rv — the honesty ratio — is
   undefined without the tape denominator anyway. Pairing invariant on
   auto-filled rows: raw non-NULL ⇒ tape non-NULL.
2. MONITOR: ``bot/helpers/vol_honesty.py::VolHonestyMonitor`` — per
   asset, an in-memory deque of (ts, raw/tape) ratio samples fed by the
   scanner at the V.1 ``_strategy_vol`` seam; every ≥60s per asset,
   median over the trailing ``VOL_HONESTY_WINDOW_S``; WARN
   ``VOL_HONESTY_BREACH`` + Telegram (1/hour/asset throttle) when the
   median leaves [``VOL_HONESTY_LOW``, ``VOL_HONESTY_HIGH``].

This file is RED before any production code lands (TDD hook contract).
"""
from __future__ import annotations

import ast
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock

import pytest


# Mock heavy deps that the import chain might pull in transitively.
_HEAVY = (
    "websockets", "websocket",
    "cryptography",
    "cryptography.hazmat",
    "cryptography.hazmat.primitives",
    "cryptography.hazmat.primitives.serialization",
    "cryptography.hazmat.primitives.hashes",
    "cryptography.hazmat.primitives.asymmetric",
    "cryptography.hazmat.primitives.asymmetric.padding",
)
for _m in _HEAVY:
    sys.modules.setdefault(_m, MagicMock())


REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

STATE_PATH = REPO_ROOT / "bot" / "state.py"
SCANNER_PATH = REPO_ROOT / "bot" / "scanner" / "__init__.py"
HELPER_PATH = REPO_ROOT / "bot" / "helpers" / "vol_honesty.py"
FIXTURE_PATH = REPO_ROOT / "tests" / "fixtures" / "state_db_schema_baseline.txt"


def _build_temp_state_db():
    """Spin a temp StateManager that creates the production schema."""
    from bot.state import StateManager
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    sm = StateManager(db_path=tmp.name)
    return sm, tmp.name


# ----------------------------------------------------------------------
# 1. evaluated_opportunities has tape_rv300 + raw_blended_rv columns
# ----------------------------------------------------------------------

def test_evaluated_opportunities_has_vol_honesty_columns():
    """The migration loop in `bot/state.py::_create_tables` MUST add
    `tape_rv300 REAL` + `raw_blended_rv REAL` to evaluated_opportunities."""
    sm, path = _build_temp_state_db()
    try:
        cols = [row[1] for row in sm.conn.execute(
            "PRAGMA table_info(evaluated_opportunities)"
        ).fetchall()]
        for col in ("tape_rv300", "raw_blended_rv"):
            assert col in cols, (
                f"evaluated_opportunities is missing {col}; "
                f"cols = {sorted(cols)}"
            )
    finally:
        sm.conn.close()
        os.unlink(path)


def test_vol_honesty_columns_type_is_real():
    """Both columns carry REAL affinity."""
    sm, path = _build_temp_state_db()
    try:
        info = sm.conn.execute(
            "PRAGMA table_info(evaluated_opportunities)"
        ).fetchall()
        for col in ("tape_rv300", "raw_blended_rv"):
            rows = [r for r in info if r[1] == col]
            assert rows, f"{col} missing"
            assert rows[0][2].upper() == "REAL", (
                f"{col} type should be REAL; got {rows[0][2]}"
            )
    finally:
        sm.conn.close()
        os.unlink(path)


def test_baseline_fixture_contains_vol_honesty_columns():
    """The Bit-7.1 schema baseline fixture must carry both new columns —
    pins the fixture-bump site of the chain so the column add can't ship
    without `tests/integration/test_state_extraction.py::
    test_schema_zero_delta_per_table` staying green."""
    text = FIXTURE_PATH.read_text()
    assert "name=tape_rv300" in text, (
        "tests/fixtures/state_db_schema_baseline.txt missing tape_rv300 — "
        "regenerate the evaluated_opportunities section"
    )
    assert "name=raw_blended_rv" in text, (
        "tests/fixtures/state_db_schema_baseline.txt missing raw_blended_rv"
    )


# ----------------------------------------------------------------------
# 2. insert_evaluated_opportunity signature + write/NULL/auto-fill pins
# ----------------------------------------------------------------------

def test_insert_evaluated_opportunity_accepts_vol_honesty_kwargs():
    """Signature includes `tape_rv300` + `raw_blended_rv` Optional kwargs."""
    tree = ast.parse(STATE_PATH.read_text())
    sig_kwarg_names: list[str] = []
    for node in ast.walk(tree):
        if (isinstance(node, ast.FunctionDef)
                and node.name == "insert_evaluated_opportunity"):
            args = node.args
            sig_kwarg_names = [a.arg for a in args.args + args.kwonlyargs]
            break
    assert sig_kwarg_names, "insert_evaluated_opportunity not found in bot/state.py"
    for kw in ("tape_rv300", "raw_blended_rv"):
        assert kw in sig_kwarg_names, (
            f"insert_evaluated_opportunity missing {kw} kwarg; "
            f"got {sig_kwarg_names}"
        )


def test_insert_persists_explicit_vol_honesty_kwargs():
    """Behavioral: explicit kwargs write through to the row."""
    sm, path = _build_temp_state_db()
    try:
        sm.insert_evaluated_opportunity(
            ticker="KXHYPE15MTEST-T-EXPL",
            event_ticker="KXHYPE15MTEST",
            asset="HYPE",
            filter_stage="candidate",
            spot_price=30.0,
            tape_rv300=3.0e-4,
            raw_blended_rv=8.97e-5,
            product_type="15m",
        )
        row = sm.conn.execute(
            "SELECT tape_rv300, raw_blended_rv FROM evaluated_opportunities "
            "WHERE ticker='KXHYPE15MTEST-T-EXPL'"
        ).fetchone()
        assert row is not None, "insert did not persist row"
        assert row[0] == pytest.approx(3.0e-4)
        assert row[1] == pytest.approx(8.97e-5)
    finally:
        sm.conn.close()
        os.unlink(path)


def test_insert_defaults_vol_honesty_to_null():
    """Behavioral: no kwargs + empty caches → both columns NULL (honest)."""
    sm, path = _build_temp_state_db()
    try:
        sm.insert_evaluated_opportunity(
            ticker="KXHYPE15MTEST-T-NULL",
            event_ticker="KXHYPE15MTEST",
            asset="HYPE",
            filter_stage="candidate",
            product_type="15m",
        )
        row = sm.conn.execute(
            "SELECT tape_rv300, raw_blended_rv FROM evaluated_opportunities "
            "WHERE ticker='KXHYPE15MTEST-T-NULL'"
        ).fetchone()
        assert row is not None
        assert row[0] is None, f"tape_rv300 default must be NULL; got {row[0]}"
        assert row[1] is None, f"raw_blended_rv default must be NULL; got {row[1]}"
    finally:
        sm.conn.close()
        os.unlink(path)


def test_insert_auto_fills_both_from_caches_when_tape_present():
    """Behavioral: both V.1 caches populated → both columns auto-fill
    (the no-per-site-threading contract across the 115+ insert sites)."""
    sm, path = _build_temp_state_db()
    try:
        sm._scan_tape_rv_cache["HYPE"] = 2.9e-4
        sm._scan_raw_blended_rv_cache["HYPE"] = 9.0e-5
        sm.insert_evaluated_opportunity(
            ticker="KXHYPE15MTEST-T-AUTO",
            event_ticker="KXHYPE15MTEST",
            asset="HYPE",
            filter_stage="candidate",
            spot_price=30.0,
            product_type="15m",
            # NOTE: neither vol-honesty kwarg passed
        )
        row = sm.conn.execute(
            "SELECT tape_rv300, raw_blended_rv FROM evaluated_opportunities "
            "WHERE ticker='KXHYPE15MTEST-T-AUTO'"
        ).fetchone()
        assert row is not None
        assert row[0] == pytest.approx(2.9e-4), (
            f"tape_rv300 auto-fill from _scan_tape_rv_cache broken; got {row[0]}"
        )
        assert row[1] == pytest.approx(9.0e-5), (
            f"raw_blended_rv auto-fill from _scan_raw_blended_rv_cache broken; "
            f"got {row[1]}"
        )
    finally:
        sm.conn.close()
        os.unlink(path)


def test_raw_auto_fill_gated_on_tape_cache_presence():
    """NULL-honesty pin for the OVERWRITE-ONLY raw cache (V.1-R5): the raw
    cache is never popped, so without a gate a stale engine estimate from
    an arbitrarily old tick could leak onto rows whose tape side honestly
    abstained. Gate = the TAPE cache must hold a value for the asset
    (tape-present certifies the most recent seam pass produced an honest
    rv300; the ratio is undefined without the denominator anyway).
    Raw cache set + tape cache EMPTY → BOTH columns NULL."""
    sm, path = _build_temp_state_db()
    try:
        sm._scan_raw_blended_rv_cache["HYPE"] = 9.0e-5
        # _scan_tape_rv_cache deliberately EMPTY (popped: rv300 was None)
        sm.insert_evaluated_opportunity(
            ticker="KXHYPE15MTEST-T-GATE",
            event_ticker="KXHYPE15MTEST",
            asset="HYPE",
            filter_stage="candidate",
            spot_price=30.0,
            product_type="15m",
        )
        row = sm.conn.execute(
            "SELECT tape_rv300, raw_blended_rv FROM evaluated_opportunities "
            "WHERE ticker='KXHYPE15MTEST-T-GATE'"
        ).fetchone()
        assert row is not None
        assert row[0] is None, "tape_rv300 must be NULL when cache empty"
        assert row[1] is None, (
            f"raw_blended_rv must be NULL when the tape cache has no value "
            f"for the asset (overwrite-only raw cache gate); got {row[1]}"
        )
    finally:
        sm.conn.close()
        os.unlink(path)


def test_explicit_vol_honesty_kwargs_win_over_caches():
    """Caller-supplied values override the cache lookups."""
    sm, path = _build_temp_state_db()
    try:
        sm._scan_tape_rv_cache["HYPE"] = 2.9e-4
        sm._scan_raw_blended_rv_cache["HYPE"] = 9.0e-5
        sm.insert_evaluated_opportunity(
            ticker="KXHYPE15MTEST-T-WIN",
            event_ticker="KXHYPE15MTEST",
            asset="HYPE",
            filter_stage="candidate",
            tape_rv300=1.0e-4,
            raw_blended_rv=2.0e-4,
            product_type="15m",
        )
        row = sm.conn.execute(
            "SELECT tape_rv300, raw_blended_rv FROM evaluated_opportunities "
            "WHERE ticker='KXHYPE15MTEST-T-WIN'"
        ).fetchone()
        assert row is not None
        assert row[0] == pytest.approx(1.0e-4)
        assert row[1] == pytest.approx(2.0e-4)
    finally:
        sm.conn.close()
        os.unlink(path)


def test_upsert_coalesce_preserves_first_vol_honesty_reading():
    """ON CONFLICT(ticker, filter_stage, side) DO UPDATE must COALESCE both
    columns so the FIRST (decision-time) reading survives subsequent
    UPSERTs — mirrors the spot_staleness_seconds / config_snapshot_id
    pattern. side='yes' on both inserts so the 3-col unique index actually
    conflicts (NULL side rows never conflict in SQLite)."""
    sm, path = _build_temp_state_db()
    try:
        sm.insert_evaluated_opportunity(
            ticker="KXHYPE15MTEST-T-UPS",
            event_ticker="KXHYPE15MTEST",
            asset="HYPE",
            filter_stage="candidate",
            side="yes",
            tape_rv300=3.0e-4,
            raw_blended_rv=9.0e-5,
            product_type="15m",
        )
        sm.insert_evaluated_opportunity(
            ticker="KXHYPE15MTEST-T-UPS",
            event_ticker="KXHYPE15MTEST",
            asset="HYPE",
            filter_stage="candidate",
            side="yes",
            tape_rv300=9.9e-1,
            raw_blended_rv=9.8e-1,
            product_type="15m",
        )
        rows = sm.conn.execute(
            "SELECT tape_rv300, raw_blended_rv FROM evaluated_opportunities "
            "WHERE ticker='KXHYPE15MTEST-T-UPS'"
        ).fetchall()
        assert len(rows) == 1, f"expected upsert (1 row), got {len(rows)}"
        assert rows[0][0] == pytest.approx(3.0e-4), (
            f"COALESCE broken for tape_rv300 — first reading must survive; "
            f"got {rows[0][0]}"
        )
        assert rows[0][1] == pytest.approx(9.0e-5), (
            f"COALESCE broken for raw_blended_rv; got {rows[0][1]}"
        )
    finally:
        sm.conn.close()
        os.unlink(path)


# ----------------------------------------------------------------------
# 3. Constants — pre-registered honesty band + monitor knobs
# ----------------------------------------------------------------------

def test_vol_honesty_constants_exist_with_preregistered_values():
    """bot/constants.py carries the 4 monitor knobs. The [0.6, 1.8] alert
    band is deliberately WIDER than the [0.8, 1.25] re-arm soak gate
    (plan doc) — the monitor is the always-on tripwire for the deflation
    CLASS (HYPE stored/realized ran 0.15-0.21), not the soak pass-bar."""
    import bot.constants as C
    assert C.VOL_HONESTY_LOW == pytest.approx(0.6)
    assert C.VOL_HONESTY_HIGH == pytest.approx(1.8)
    assert C.VOL_HONESTY_WINDOW_S == pytest.approx(600.0)
    assert C.VOL_HONESTY_ALERT_THROTTLE_S == pytest.approx(3600.0)
    assert C.VOL_HONESTY_LOW < C.VOL_HONESTY_HIGH


# ----------------------------------------------------------------------
# 4. VolHonestyMonitor behavior (bot/helpers/vol_honesty.py)
# ----------------------------------------------------------------------

def _make_monitor():
    from bot.helpers.vol_honesty import VolHonestyMonitor
    return VolHonestyMonitor()


def _feed_ratio(mon, asset, ratio, t0, n=20, tape=1.0e-4, step=5.0):
    """Feed n samples of a constant raw/tape ratio, advancing time by step.
    Returns the FIRST non-None record() result (the Telegram message fires
    on the first breaching 60s check, not the last tick) and the final
    timestamp."""
    msg = None
    t = t0
    for i in range(n):
        t = t0 + i * step
        out = mon.record(asset, ratio * tape, tape, now=t)
        if msg is None:
            msg = out
    return msg, t


def test_monitor_in_band_median_never_breaches(caplog):
    """Healthy ratios (≈1.0) → no WARN, no telegram message."""
    import logging as _logging
    mon = _make_monitor()
    with caplog.at_level(_logging.WARNING):
        out, _ = _feed_ratio(mon, "HYPE", 1.0, t0=1000.0, n=30)
    assert out is None
    assert "VOL_HONESTY_BREACH" not in caplog.text


def test_monitor_deflation_breach_warns_and_returns_message(caplog):
    """The postmortem signature (raw/tape ≈ 0.3 on HYPE) must trip:
    WARN log carries the VOL_HONESTY_BREACH signature and record()
    returns a Telegram-ready message naming the asset."""
    import logging as _logging
    mon = _make_monitor()
    with caplog.at_level(_logging.WARNING):
        out, _ = _feed_ratio(mon, "HYPE", 0.3, t0=1000.0, n=30)
    assert "VOL_HONESTY_BREACH" in caplog.text
    assert out is not None and "HYPE" in out


def test_monitor_inflation_breach_also_trips():
    """Symmetric: median above VOL_HONESTY_HIGH trips too (an engine
    reading 2x the tape is also dishonest — wrong direction for the
    short-vol overlays but wrong is wrong)."""
    mon = _make_monitor()
    out, _ = _feed_ratio(mon, "DOGE", 2.5, t0=1000.0, n=30)
    assert out is not None and "DOGE" in out


def test_monitor_telegram_throttled_one_per_hour_per_asset(caplog):
    """Second breaching CHECK within VOL_HONESTY_ALERT_THROTTLE_S still
    WARN-logs (60s check cadence) but returns None (no telegram);
    past the throttle horizon the message returns again."""
    import logging as _logging
    mon = _make_monitor()
    out1, t_last = _feed_ratio(mon, "HYPE", 0.3, t0=1000.0, n=30)
    assert out1 is not None
    # Keep breaching for another ~2 checks inside the hour.
    caplog.clear()
    with caplog.at_level(_logging.WARNING):
        out2, t_last = _feed_ratio(
            mon, "HYPE", 0.3, t0=t_last + 5.0, n=30)
    assert out2 is None, "telegram must throttle to 1/hour/asset"
    assert "VOL_HONESTY_BREACH" in caplog.text, (
        "WARN log must keep firing on the 60s check cadence during the "
        "telegram-throttled window"
    )
    # Jump past the throttle horizon → message returns.
    out3 = None
    t0 = t_last + 3600.0 + 61.0
    for i in range(30):
        out3 = mon.record("HYPE", 0.3e-4, 1.0e-4, now=t0 + i * 5.0)
        if out3 is not None:
            break
    assert out3 is not None, "alert must re-arm after the throttle horizon"


def test_monitor_check_interval_throttles_to_60s(caplog):
    """Within one check interval, at most ONE breach WARN fires per asset
    (the median compute + log are 60s-throttled, not per-tick)."""
    import logging as _logging
    mon = _make_monitor()
    # Warm the window with breaching samples + force the first check.
    _feed_ratio(mon, "HYPE", 0.3, t0=1000.0, n=30)
    caplog.clear()
    with caplog.at_level(_logging.WARNING):
        # 10 ticks all inside one 60s interval right after the last check.
        for i in range(10):
            mon.record("HYPE", 0.3e-4, 1.0e-4, now=1146.0 + i * 1.0)
    assert caplog.text.count("VOL_HONESTY_BREACH") <= 1


def test_monitor_min_samples_guard():
    """A couple of (possibly wild) samples must not alert — the median
    needs a minimum window population before it means anything."""
    mon = _make_monitor()
    out = mon.record("XRP", 0.1e-4, 1.0e-4, now=1000.0)
    out = mon.record("XRP", 0.1e-4, 1.0e-4, now=1061.0) or out
    assert out is None


def test_monitor_none_tape_contributes_no_samples():
    """tape_rv300 None (honest abstention) adds no ratio sample — an asset
    with a dead tape can never breach (TAPE_RV_NONE covers that condition;
    the monitor only judges ticks where BOTH estimates exist)."""
    mon = _make_monitor()
    out = None
    for i in range(100):
        out = mon.record("BNB", 9.0e-5, None, now=1000.0 + i * 5.0) or out
    assert out is None


# ----------------------------------------------------------------------
# 5. Scanner wiring — monitor constructed + fed at the V.1 seam
# ----------------------------------------------------------------------

def test_scanner_constructs_vol_honesty_monitor():
    """AST: OpportunityScanner.__init__ assigns self._vol_honesty."""
    tree = ast.parse(SCANNER_PATH.read_text())
    init_assigns: list[str] = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.FunctionDef) and node.name == "__init__"):
            continue
        for stmt in ast.walk(node):
            if isinstance(stmt, ast.Assign):
                for tgt in stmt.targets:
                    if (isinstance(tgt, ast.Attribute)
                            and isinstance(tgt.value, ast.Name)
                            and tgt.value.id == "self"):
                        init_assigns.append(tgt.attr)
            elif isinstance(stmt, ast.AnnAssign):
                tgt = stmt.target
                if (isinstance(tgt, ast.Attribute)
                        and isinstance(tgt.value, ast.Name)
                        and tgt.value.id == "self"):
                    init_assigns.append(tgt.attr)
    assert "_vol_honesty" in init_assigns, (
        "OpportunityScanner.__init__ must construct self._vol_honesty "
        "(VolHonestyMonitor)"
    )


def test_scanner_calls_vol_honesty_record():
    """AST: scan() calls self._vol_honesty.record(...) — the per-tick feed
    at the V.1 _strategy_vol seam where raw blended_rv + tape rv300 are
    both in hand."""
    tree = ast.parse(SCANNER_PATH.read_text())
    found = False
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            if (isinstance(func, ast.Attribute) and func.attr == "record"
                    and isinstance(func.value, ast.Attribute)
                    and func.value.attr == "_vol_honesty"):
                found = True
                break
    assert found, (
        "bot/scanner/__init__.py must call self._vol_honesty.record(...) "
        "at the _strategy_vol seam"
    )


def test_scanner_forwards_breach_to_telegram_with_dedup_key():
    """Source pin: the breach message is forwarded via the canonical
    `_telegram_state._TELEGRAM.send(..., dedup_key='vol_honesty_<asset>')`
    pattern (notifier-side 60s dedup is belt-and-braces on top of the
    monitor's 1/hour throttle)."""
    src = SCANNER_PATH.read_text()
    assert "vol_honesty_" in src, (
        "scanner must send the breach message with a vol_honesty_<asset> "
        "dedup_key via _telegram_state._TELEGRAM.send"
    )
