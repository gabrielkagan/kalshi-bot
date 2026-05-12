"""Bit 11.1b (Sprint 11, 2026-05-11) — null-rate eligibility filters
in scripts/audit/data_health_monitor.py::check_null_rates.

RCA in kb/findings/skill-audit-may11-bit-11.1b.md confirmed that the
function's CRIT/WARN signal was producing false positives because:
  - `position_size` / `entry_price` are conditionally written (only
    on sizing-eligible filter_stages). Pre-sizing rejection rows
    correctly have NULL, but the function counted them in the
    denominator.
  - `egarch_sigma` / `egarch_blend_sigma` are crypto-only. Weather +
    sports product_types correctly have 100% NULL, but the function
    flagged them as WARN/CRIT.

This test file pins:
  1. position_size NOT flagged when high NULL rate is entirely
     attributable to rejection-stage rows.
  2. egarch_sigma NOT flagged for product_type='weather' (skip
     applies).
  3. Always-write columns (calibrated_prob, edge) STILL flagged when
     their NULL rate is genuinely high — no regression in coverage of
     real writer-path bugs.
"""
from __future__ import annotations

import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts" / "audit"))

# Import lazily inside test functions to avoid touching script side
# effects at module import.


# Minimal schema mirroring evaluated_opportunities's columns used by
# check_null_rates. Real schema has many more columns; only the ones
# the function reads are needed here.
EVAL_SCHEMA = """
CREATE TABLE evaluated_opportunities (
    id INTEGER PRIMARY KEY,
    ticker TEXT,
    event_ticker TEXT,
    asset TEXT,
    product_type TEXT,
    filter_stage TEXT,
    evaluation_time TEXT,
    spot_price REAL,
    market_price INTEGER,
    seconds_to_close REAL,
    calibrated_prob REAL,
    raw_prob REAL,
    edge REAL,
    volatility REAL,
    egarch_sigma REAL,
    egarch_blend_sigma REAL,
    position_size REAL
)
"""


def _make_db(tmp_path: Path) -> sqlite3.Connection:
    """Build a tmp evaluated_opportunities DB with the schema."""
    db = tmp_path / "state.db"
    conn = sqlite3.connect(db)
    conn.execute(EVAL_SCHEMA)
    conn.commit()
    return conn


def _insert(conn, **kw):
    """Insert one row with defaults filled."""
    defaults = dict(
        ticker="KX-TEST", event_ticker="EVT", asset="BTC",
        product_type="15m", filter_stage="candidate",
        evaluation_time=datetime.now(tz=timezone.utc).isoformat(),
        spot_price=50000.0, market_price=50, seconds_to_close=300,
        calibrated_prob=0.6, raw_prob=0.65, edge=0.05, volatility=0.02,
        egarch_sigma=0.018, egarch_blend_sigma=0.019, position_size=10.0,
    )
    defaults.update(kw)
    cols = ", ".join(defaults.keys())
    placeholders = ", ".join("?" * len(defaults))
    conn.execute(
        f"INSERT INTO evaluated_opportunities ({cols}) VALUES ({placeholders})",
        tuple(defaults.values()),
    )


def test_position_size_not_flagged_for_structural_nulls(tmp_path):
    """100 sizing-eligible rows (all with position_size populated) +
    200 rejection-stage rows (all NULL position_size). Overall NULL rate
    = 200/300 = 66.7% (>20% threshold). Without eligibility filter, this
    flags WARN. With filter, sizing-eligible-only NULL rate = 0/100 = 0%
    (clean). This pins the Bit 11.1b semantic: structural NULLs from
    rejection-pre-sizing filter_stages do NOT trigger WARN."""
    from data_health_monitor import check_null_rates

    conn = _make_db(tmp_path)
    # 100 sizing-eligible rows (candidate filter_stage; position_size populated)
    for i in range(100):
        _insert(conn, filter_stage="candidate", position_size=10.0)
    # 200 rejection-stage rows (price_out_of_range; position_size NULL)
    for i in range(200):
        _insert(conn, filter_stage="price_out_of_range", position_size=None)
    conn.commit()

    results = check_null_rates(conn, datetime.now(tz=timezone.utc))
    for sev, msg in results:
        assert "position_size" not in msg, (
            f"Bit 11.1b regression: position_size flagged despite "
            f"structural NULLs being entirely from rejection-pre-sizing "
            f"filter_stage. Message: {msg!r}"
        )


def test_egarch_not_flagged_for_weather(tmp_path):
    """Weather product_type has 100% NULL egarch by design (crypto-only
    feature). Pin: check_null_rates skips egarch columns for weather."""
    from data_health_monitor import check_null_rates

    conn = _make_db(tmp_path)
    # 50 weather rows, all with NULL egarch (by design)
    for i in range(50):
        _insert(
            conn, product_type="weather", filter_stage="candidate",
            egarch_sigma=None, egarch_blend_sigma=None, volatility=None,
        )
    conn.commit()

    results = check_null_rates(conn, datetime.now(tz=timezone.utc))
    for sev, msg in results:
        # The Wx label is what data_health_monitor uses for weather.
        if msg.startswith("Wx:") or "weather" in msg.lower():
            assert "egarch_sigma" not in msg, (
                f"Bit 11.1b regression: egarch_sigma flagged for weather "
                f"product_type (crypto-only feature, 100% NULL by design). "
                f"Message: {msg!r}"
            )
            assert "egarch_blend_sigma" not in msg, (
                f"Bit 11.1b regression: egarch_blend_sigma flagged for "
                f"weather (crypto-only). Message: {msg!r}"
            )


def test_always_write_column_still_flagged_for_real_bug(tmp_path):
    """No-regression pin: if calibrated_prob is genuinely NULL on >20%
    of rows (a real writer-path bug), check_null_rates must still flag
    it. The Bit 11.1b filter must NOT silence real bugs."""
    from data_health_monitor import check_null_rates

    conn = _make_db(tmp_path)
    # 100 rows, 30 with NULL calibrated_prob (30% NULL — above threshold)
    for i in range(70):
        _insert(conn, filter_stage="candidate")
    for i in range(30):
        _insert(conn, filter_stage="candidate", calibrated_prob=None)
    conn.commit()

    results = check_null_rates(conn, datetime.now(tz=timezone.utc))
    found_cp_flag = any(
        "calibrated_prob" in msg for sev, msg in results
    )
    assert found_cp_flag, (
        "Bit 11.1b regression: calibrated_prob 30% NULL not flagged. "
        "The eligibility filter must not silence real writer-path bugs "
        "on always-write columns."
    )


def test_eligibility_constants_defined():
    """Pin the existence and structure of the eligibility constants
    so a future refactor can't silently remove them. R1 adversarial fix
    2026-05-11: predicate switched from deny-list to allow-list shape
    (rationale in scripts/audit/data_health_monitor.py module-level comment +
    kb/findings/skill-audit-may11-bit-11.1b.md)."""
    from data_health_monitor import (
        PRODUCT_TYPE_SKIP, SIZING_ELIGIBLE_ONLY,
        SIZING_ELIGIBLE_FILTER_STAGES,
        SIZING_ELIGIBLE_FILTER_STAGE_PREDICATE,
    )

    # PRODUCT_TYPE_SKIP must include the crypto-only columns mapped to
    # the non-crypto product_types that don't write them. R2 fix
    # removed spx_hourly — VPS evidence showed spx_hourly DOES write
    # egarch (0% NULL on 7d window); skipping would mask real bugs.
    assert "egarch_sigma" in PRODUCT_TYPE_SKIP
    assert "weather" in PRODUCT_TYPE_SKIP["egarch_sigma"]
    assert "sports" in PRODUCT_TYPE_SKIP["egarch_sigma"]
    assert "spx_hourly" not in PRODUCT_TYPE_SKIP["egarch_sigma"], (
        "spx_hourly writes egarch_sigma at 0% NULL on VPS — skipping "
        "would mask real writer-path bugs. R2 fix removed it."
    )
    assert "egarch_blend_sigma" in PRODUCT_TYPE_SKIP
    assert "weather" in PRODUCT_TYPE_SKIP["egarch_blend_sigma"]
    assert "sports" in PRODUCT_TYPE_SKIP["egarch_blend_sigma"]
    assert "spx_hourly" not in PRODUCT_TYPE_SKIP["egarch_blend_sigma"], (
        "spx_hourly writes egarch_blend_sigma at 0% NULL on VPS — "
        "skipping would mask real writer-path bugs. R2 fix removed it."
    )

    # SIZING_ELIGIBLE_ONLY must scope position_size + entry_price.
    assert "position_size" in SIZING_ELIGIBLE_ONLY
    assert "entry_price" in SIZING_ELIGIBLE_ONLY

    # Allow-list must include `candidate` (the canonical actual-trade
    # filter_stage). Excluding it would silence the writer-path bug
    # detection signal entirely.
    assert "candidate" in SIZING_ELIGIBLE_FILTER_STAGES, (
        "SIZING_ELIGIBLE_FILTER_STAGES must include 'candidate' — the "
        "canonical actual-trade filter_stage. Without it, the eligibility "
        "filter would have an empty allow-list and silence real writer "
        "bugs affecting candidate sizing."
    )

    # The predicate is `filter_stage IN (...)` — allow-list shape per
    # R1 adversarial fix (deny-list shape drifts as new rejection
    # stages get added).
    assert "filter_stage IN" in SIZING_ELIGIBLE_FILTER_STAGE_PREDICATE
    assert "candidate" in SIZING_ELIGIBLE_FILTER_STAGE_PREDICATE
