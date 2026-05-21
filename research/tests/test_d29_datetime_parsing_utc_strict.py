"""D-29 — datetime parsing strict-UTC.

Authoritative source: bot._impl::insert_evaluated_opportunity writes
`datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")` — ISO-8601
UTC with Z suffix and microsecond precision (per RCA D-29).

Replay must parse all snapshot timestamps as UTC-aware. Naive comparisons
silently misinterpret OR raise — both are bad. A single canonical parser is
required (pandas.to_datetime with utc=True + ISO8601 format).

Replay's regime_cutoff arg must reject naive datetimes (force explicit UTC).

Some tests TDD-red on B3's regime_cutoff arg validation; others test pandas
behavior directly.
"""
from __future__ import annotations

import datetime as dt

import pandas as pd
import pytest


def test_d29_pandas_parses_z_suffix_to_utc() -> None:
    """pd.to_datetime(..., utc=True, format='ISO8601') parses Z suffix as UTC."""
    parsed = pd.to_datetime("2026-05-05T12:00:00.000Z", utc=True, format="ISO8601")
    assert parsed.tzinfo == dt.timezone.utc
    assert parsed.year == 2026 and parsed.hour == 12


def test_d29_pandas_parses_plus_zero_offset_to_utc() -> None:
    """pd.to_datetime parses +00:00 offset identically to Z (both → UTC)."""
    parsed_z = pd.to_datetime("2026-05-05T12:00:00.000Z", utc=True, format="ISO8601")
    parsed_offset = pd.to_datetime("2026-05-05T12:00:00.000+00:00", utc=True, format="ISO8601")
    assert parsed_z == parsed_offset, (
        f"D-29 Z vs +00:00: {parsed_z} != {parsed_offset}"
    )


def test_d29_pandas_iso8601_format_handles_microseconds() -> None:
    """pd.to_datetime(format='ISO8601') handles 6-digit microsecond fractions.

    Production timestamps use `%f` (microsecond precision) — pin that the
    parser accepts them.
    """
    parsed = pd.to_datetime("2026-05-05T12:00:00.123456Z", utc=True, format="ISO8601")
    assert parsed.microsecond == 123456


def test_d29_naive_datetime_compares_as_aware_via_utc_localize() -> None:
    """Naive datetime + UTC localization == aware UTC datetime.

    The bug class this guards: comparing a naive snapshot time to an aware
    regime_cutoff raises in Python (`TypeError: can't compare offset-naive and
    offset-aware datetimes`). Pin the safe pattern.
    """
    naive = dt.datetime(2026, 5, 5, 12, 0, 0)
    aware = dt.datetime(2026, 5, 5, 12, 0, 0, tzinfo=dt.timezone.utc)
    # Naive cannot compare to aware:
    with pytest.raises(TypeError):
        _ = naive < aware
    # After localization, they compare:
    localized = naive.replace(tzinfo=dt.timezone.utc)
    assert localized == aware


def test_d29_replay_regime_cutoff_rejects_naive_datetime(tmp_path) -> None:
    """B3's evaluate_window raises on naive datetime regime_cutoff (TDD-red).

    Per RCA D-29 test surface: "Naive datetime input to regime_cutoff arg
    raises (don't silently assume UTC)."

    R1 finding M6: original test passed a nonexistent path, which could mask
    the tz-check with a file-open error. Use a real (empty) snapshot via
    tmp_path so the function reaches the tz validation.
    """
    import research.replay as rep
    if not hasattr(rep, "evaluate_window"):
        pytest.skip("D-29 TDD-red: evaluate_window not yet implemented")
    # Real empty snapshot file — bypasses the file-not-found path
    import sqlite3
    snap = tmp_path / "snapshot_d29_naive_tz.db"
    sqlite3.connect(str(snap)).close()
    naive = dt.datetime(2026, 4, 30, 16, 16, 0)  # NO tzinfo
    with pytest.raises((TypeError, ValueError)) as excinfo:
        rep.evaluate_window(
            snapshot_path=snap,
            regime_cutoff=naive,
        )
    msg = str(excinfo.value).lower()
    # Accept broader set of keywords — Python's native TypeError says
    # "offset-naive and offset-aware" which contains 'naive' + 'offset'.
    accepted = ["tz", "naive", "utc", "timezone", "offset", "aware"]
    assert any(k in msg for k in accepted), (
        f"D-29 raise: should mention {accepted}; got {excinfo.value!r}"
    )


def test_d29_replay_has_canonical_parser_or_uses_pd_to_datetime() -> None:
    """research/replay.py uses a single canonical parser pattern (TDD-red until B3).

    Either:
      (a) `pd.to_datetime(..., utc=True, format='ISO8601')` in replay.py, OR
      (b) a `_parse_utc(s: str)` helper function that wraps it.
    """
    import research.replay as rep
    import inspect
    src = inspect.getsource(rep)
    # Once B3 ships time parsing, one of these patterns should appear:
    patterns = [
        "pd.to_datetime",
        "_parse_utc",
        "parse_utc",
    ]
    if not any(p in src for p in patterns):
        pytest.skip("D-29 TDD-red: B3 has not yet shipped a canonical datetime parser")
    # If any pattern appears, ensure utc=True is in the source somewhere (best-effort heuristic).
    if "pd.to_datetime" in src:
        assert "utc=True" in src, (
            "D-29: replay uses pd.to_datetime but doesn't appear to pass utc=True. "
            "Naive parsing silently misinterprets Z-suffix strings."
        )


def test_d29_mixed_format_timestamps_parse_identically() -> None:
    """Mixed Z-suffix and +00:00 timestamps in one snapshot parse to the same datetime."""
    series = pd.Series([
        "2026-05-05T12:00:00.000Z",
        "2026-05-05T12:00:00.000+00:00",
        "2026-05-05T12:00:00.000000Z",  # 6-digit microsecond
    ])
    parsed = pd.to_datetime(series, utc=True, format="ISO8601")
    # All three are the same instant.
    assert len(parsed.unique()) == 1, (
        f"D-29 mixed-format: expected 1 unique timestamp, got {parsed.unique()}"
    )
