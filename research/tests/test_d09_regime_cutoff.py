"""D-9 — regime cutoff handling.

Authoritative source: scripts/alpha_audit.py::REGIME_CUTOFFS list (per RCA D-9).
The `--regime-cutoff` CLI flag clamps the lookback `since` so pre-cutoff data is
excluded. CLAUDE.md "Performance analysis filters to current config regime."

Current registered cutoff:
    ("2026-04-30T16:16:00", "Bleed-cell blocks LIVE")

Replay's regime_cutoff parameter must:
1. Filter every query reading evaluated_opportunities or settled_trades to
   `WHERE evaluation_time >= cutoff` (or `settled_at >= cutoff` per D-20).
2. Refuse to silently aggregate cross-regime windows without an explicit
   `regime_cutoff=...` opt-in.

These tests are TDD-RED until B3 ships:
    research.replay.REGIME_CUTOFFS    : list of (iso_string, label)
    research.replay.evaluate_window(snapshot_path, since, until, regime_cutoff=None)
"""
from __future__ import annotations

import datetime as dt
import sqlite3
import textwrap
from pathlib import Path

import pytest


CANONICAL_CUTOFF_ISO = "2026-04-30T16:16:00"
CANONICAL_CUTOFF_LABEL = "Bleed-cell blocks LIVE"


@pytest.fixture(scope="module")
def synthetic_snapshot(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """In-memory-ish snapshot fixture with pre + post cutoff rows.

    Schema mirrors the relevant subset of evaluated_opportunities for the
    regime-cutoff and evaluation_time/settled_at tests. Real snapshot column
    set is verified by D-10 (PRAGMA-first); D-9 only needs the time columns.
    """
    db = tmp_path_factory.mktemp("synth") / "synthetic_snapshot.db"
    conn = sqlite3.connect(str(db))
    try:
        conn.executescript(textwrap.dedent("""
            CREATE TABLE evaluated_opportunities (
                id INTEGER PRIMARY KEY,
                evaluation_time TEXT NOT NULL,
                settled_time TEXT,
                market_result TEXT,
                side TEXT DEFAULT 'yes',
                market_price INTEGER,
                position_size INTEGER,
                product_type TEXT,
                filter_stage TEXT DEFAULT 'candidate',
                status TEXT DEFAULT 'settled',
                counterfactual_pnl INTEGER
            );
        """))
        # Column name `settled_time` matches the real snapshot. RCA D-20 calls
        # it `settled_at` — drift documented in the wave-5 commit + ship doc.
        conn.executemany(
            """INSERT INTO evaluated_opportunities
               (evaluation_time, settled_time, market_result, market_price, position_size,
                product_type, filter_stage, status, counterfactual_pnl)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            [
                # 4 pre-cutoff (2026-04-29 19:00 = 21h before cutoff)
                ("2026-04-29T19:00:00.000Z", "2026-04-29T19:15:00.000Z", "yes", 85, 1, "15m", "candidate", "settled", 14),
                ("2026-04-29T20:00:00.000Z", "2026-04-29T20:15:00.000Z", "no",  85, 1, "15m", "candidate", "settled", -86),
                ("2026-04-29T21:00:00.000Z", "2026-04-29T21:15:00.000Z", "yes", 90, 1, "15m", "candidate", "settled", 9),
                ("2026-04-30T15:00:00.000Z", "2026-04-30T15:15:00.000Z", "yes", 85, 1, "15m", "candidate", "settled", 14),
                # 4 post-cutoff (after 2026-04-30T16:16:00)
                ("2026-04-30T17:00:00.000Z", "2026-04-30T17:15:00.000Z", "yes", 85, 1, "15m", "candidate", "settled", 14),
                ("2026-04-30T18:00:00.000Z", "2026-04-30T18:15:00.000Z", "no",  85, 1, "15m", "candidate", "settled", -86),
                ("2026-05-01T12:00:00.000Z", "2026-05-01T12:15:00.000Z", "yes", 90, 1, "15m", "candidate", "settled", 9),
                ("2026-05-04T12:00:00.000Z", "2026-05-04T12:15:00.000Z", "yes", 85, 1, "15m", "candidate", "settled", 14),
            ],
        )
        conn.commit()
    finally:
        conn.close()
    return db


def test_d09_replay_exposes_regime_cutoffs() -> None:
    """research.replay.REGIME_CUTOFFS lists the canonical cutoff (TDD-red until B3)."""
    import research.replay as rep
    assert hasattr(rep, "REGIME_CUTOFFS"), (
        "D-9 TDD-red: B3 must ship research.replay.REGIME_CUTOFFS"
    )
    # Each entry: (iso_string, label)
    iso_strings = [entry[0] for entry in rep.REGIME_CUTOFFS]
    assert CANONICAL_CUTOFF_ISO in iso_strings, (
        f"D-9 cutoff drift: {CANONICAL_CUTOFF_ISO} not in {iso_strings}"
    )
    labels = [entry[1] for entry in rep.REGIME_CUTOFFS]
    assert CANONICAL_CUTOFF_LABEL in labels, (
        f"D-9 label drift: {CANONICAL_CUTOFF_LABEL!r} not in {labels}"
    )


def test_d09_replay_evaluate_window_signature() -> None:
    """B3's evaluate_window accepts regime_cutoff kwarg (TDD-red until B3 ships)."""
    import inspect
    import research.replay as rep
    assert hasattr(rep, "evaluate_window"), (
        "D-9 TDD-red: B3 must ship research.replay.evaluate_window"
    )
    sig = inspect.signature(rep.evaluate_window)
    assert "regime_cutoff" in sig.parameters, (
        f"D-9 signature drift: evaluate_window missing regime_cutoff. Got: {sig}"
    )


def test_d09_replay_with_cutoff_excludes_pre_cutoff_rows(synthetic_snapshot: Path) -> None:
    """Replay with regime_cutoff returns only post-cutoff rows (TDD-red until B3)."""
    import research.replay as rep
    if not hasattr(rep, "evaluate_window"):
        pytest.skip("D-9 TDD-red: evaluate_window not yet implemented")
    cutoff = dt.datetime(2026, 4, 30, 16, 16, 0, tzinfo=dt.timezone.utc)
    result = rep.evaluate_window(
        snapshot_path=synthetic_snapshot,
        regime_cutoff=cutoff,
    )
    # Expect 4 post-cutoff rows only.
    assert getattr(result, "row_count", None) == 4, (
        f"D-9 cutoff filtering: expected 4 post-cutoff rows, got {result!r}"
    )


def test_d09_replay_without_cutoff_on_crossing_window_raises_or_flags(synthetic_snapshot: Path) -> None:
    """Replay without regime_cutoff on a cross-regime window raises or sets a flag (TDD-red).

    Per RCA D-9 quirk: "Replay should likewise refuse to silently aggregate
    cross-regime windows without an explicit regime_cutoff=... opt-in."

    B3 implements either as raise or as a flag in the result; either is OK so
    long as it's not silent. Test accepts both.
    """
    import research.replay as rep
    if not hasattr(rep, "evaluate_window"):
        pytest.skip("D-9 TDD-red: evaluate_window not yet implemented")
    # Window: 2026-04-29 to 2026-05-05 spans the 2026-04-30T16:16 cutoff.
    try:
        result = rep.evaluate_window(
            snapshot_path=synthetic_snapshot,
            since=dt.datetime(2026, 4, 29, tzinfo=dt.timezone.utc),
            until=dt.datetime(2026, 5, 5, tzinfo=dt.timezone.utc),
            regime_cutoff=None,
        )
        # If didn't raise, must have a flag.
        flag = getattr(result, "crosses_regime_cutoff", None)
        assert flag is True, (
            f"D-9 cross-regime silent: expected raise OR crosses_regime_cutoff flag, "
            f"got result={result!r} with flag={flag!r}"
        )
    except (ValueError, RuntimeError) as e:
        # Raising is also acceptable.
        assert "cutoff" in str(e).lower() or "regime" in str(e).lower(), (
            f"D-9 raise: message should reference cutoff/regime, got {e!r}"
        )


def test_d09_cutoff_iso_format_pinned() -> None:
    """The canonical cutoff is in `YYYY-MM-DDTHH:MM:SS` format (no tz suffix in alpha_audit).

    alpha_audit.REGIME_CUTOFFS uses the bare ISO-8601 form without Z. Replay
    must accept this format for parsing. Pins the format contract.
    """
    parsed = dt.datetime.fromisoformat(CANONICAL_CUTOFF_ISO)
    assert parsed.year == 2026
    assert parsed.month == 4
    assert parsed.day == 30
    assert parsed.hour == 16
    assert parsed.minute == 16
    # alpha_audit's format is naive (no tz). Replay must clarify the contract
    # in evaluate_window — accept naive and assume UTC, OR require tz-aware.
    # D-29 covers the strict-UTC contract.
    assert parsed.tzinfo is None, (
        "D-9 format contract: alpha_audit.REGIME_CUTOFFS values are naive ISO. "
        "If this fails, alpha_audit.py changed its format and replay must adapt."
    )


def test_d09_replay_no_silent_cross_regime_aggregation() -> None:
    """AST guard: research/replay.py does not perform unfiltered SUM(counterfactual_pnl).

    Catches the bug class where someone writes
        `SELECT SUM(counterfactual_pnl) FROM evaluated_opportunities`
    without a regime_cutoff filter.
    """
    import inspect
    import research.replay as rep
    src = inspect.getsource(rep)
    forbidden_patterns = [
        "SELECT SUM(counterfactual_pnl)",
        "select sum(counterfactual_pnl)",
    ]
    for needle in forbidden_patterns:
        # B3 may legitimately reference these in safer contexts; the test
        # will need an allowlist once that ships. For now, B1 has neither.
        assert needle not in src, (
            f"D-9 unfiltered SUM(cf): research/replay.py contains {needle!r}. "
            f"Add regime_cutoff filtering."
        )
