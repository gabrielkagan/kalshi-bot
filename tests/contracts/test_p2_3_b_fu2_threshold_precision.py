"""P2.3.b-fu2 (2026-05-13, ticket 86b9xtam7) — replay backfill strike-precision UNITS BUG fix.

Pins the contract that DOGE replay rows preserve sub-cent strike precision
end-to-end through the backfill → extract pipeline.

Root cause (`kb/findings/replay-backfill-strike-precision-bug-may13.md`):
    `_floor_strike_to_cents` rounded Kalshi's `floor_strike` USD float to
    integer cents. For DOGE (spot ~$0.11, strike granularity $0.0001) this
    collapsed the 1-cent strike-value space and drove the BS probability
    calculation against a fundamentally different market than Kalshi
    actually settled. Result: 4871-row DOGE corpus had only 6 distinct
    strike_cents values; raw_prob Brier 0.4752 vs 0.250 coinflip baseline.

Fix surface:
  - scripts/backfill/hype_doge_replay_backfill.py
      * NEW `threshold REAL` column (kept `strike_cents INTEGER` for HYPE
        back-compat; HYPE bundles correct as-is per `kb/findings/...`)
      * NEW `_floor_strike_to_db_fields(floor_strike) -> (threshold_real,
        strike_cents_legacy)` replaces `_floor_strike_to_cents`
      * `replay_market` consumes `market["threshold"]` REAL for BS calc,
        NOT `strike_cents / 100.0`
      * INSERT writes both columns
  - scripts/cal_mlp/extract_data_replay.py
      * `threshold` added as OPTIONAL in `_check_schema` (HYPE legacy
        bundles pre-fix have no column → fall back to strike_cents/100.0)
      * `build_feature_frame` prefers `df['threshold']` when present;
        falls back to `df['strike_cents']/100.0` when NULL

Production safety: bug surface is the Mac-local replay backfill only. Live
bot `bot/scanner/__init__.py::_extract_strike` reads `floor_strike` as float
without rounding (UNAFFECTED). Production cal_mlp pipeline reads
`evaluated_opportunities.threshold REAL` (UNAFFECTED).
"""
from __future__ import annotations

import ast
import datetime
import sqlite3
import sys
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent.parent
HARNESS_SCRIPT = REPO_ROOT / "scripts" / "backfill" / "hype_doge_replay_backfill.py"
EXTRACT_SCRIPT = REPO_ROOT / "scripts" / "cal_mlp" / "extract_data_replay.py"
REPLAY_TABLE = "historical_replay_calmlp"


def _open(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path), timeout=10.0)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=10000")
    return conn


def _fixture_tick_buffer(close_ts: int, base_price: float = 0.1134,
                         warmup_secs: int = 1800) -> list[tuple[int, float]]:
    """Synthesize 30-min flat-spot tick history. Mirrors the sister-test
    pattern in tests/integration/test_hype_doge_replay_backfill.py but at
    DOGE-scale prices ($0.11) where the precision bug fires."""
    return [
        (close_ts - warmup_secs + i, base_price + (i / warmup_secs) * 0.0001)
        for i in range(warmup_secs)
    ]


def _close_ts(iso: str) -> int:
    return int(
        datetime.datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()
    )


# ── Anchor (b): AST guard on _floor_strike_to_db_fields signature ─────


def test_floor_strike_to_db_fields_exists_and_returns_tuple():
    """The split helper must exist and have signature `(floor_strike) -> tuple`.

    The legacy `_floor_strike_to_cents` returning a bare int destroyed sub-cent
    precision. The replacement returns BOTH the REAL threshold (precision-
    preserving) and the legacy INTEGER strike_cents (HYPE back-compat).
    """
    src = HARNESS_SCRIPT.read_text()
    tree = ast.parse(src)
    fn_names = {
        node.name for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef)
    }
    assert "_floor_strike_to_db_fields" in fn_names, (
        f"P2.3.b-fu2 contract: scripts/backfill/hype_doge_replay_backfill.py "
        f"must expose `_floor_strike_to_db_fields(floor_strike) -> tuple` "
        f"(replaces `_floor_strike_to_cents`). Got functions: {sorted(fn_names)}"
    )

    from scripts.backfill.hype_doge_replay_backfill import (
        _floor_strike_to_db_fields,
    )
    out = _floor_strike_to_db_fields(0.1134)
    assert isinstance(out, tuple) and len(out) == 2, (
        f"_floor_strike_to_db_fields must return (threshold_real, strike_cents_legacy); "
        f"got {out!r}"
    )
    threshold_real, strike_cents_legacy = out
    assert isinstance(threshold_real, float)
    assert strike_cents_legacy is None or isinstance(strike_cents_legacy, int)


def test_floor_strike_to_db_fields_handles_missing_invalid():
    """Honest-NULL contract on missing/invalid/NaN/Inf input — preserves the
    existing `_floor_strike_to_cents` None-passthrough so the call site's
    `if strike_cents is None: skip` gate keeps working unchanged. R1 MIN-2:
    `float('nan')` parses as float successfully but `int(round(nan*100))`
    raises ValueError; the NaN/Inf guard returns `(None, None)` as the
    typed-None contract."""
    from scripts.backfill.hype_doge_replay_backfill import (
        _floor_strike_to_db_fields,
    )
    assert _floor_strike_to_db_fields(None) == (None, None)
    assert _floor_strike_to_db_fields("not_a_number") == (None, None)
    assert _floor_strike_to_db_fields([]) == (None, None)
    assert _floor_strike_to_db_fields(float("nan")) == (None, None)
    assert _floor_strike_to_db_fields(float("inf")) == (None, None)
    assert _floor_strike_to_db_fields(float("-inf")) == (None, None)


# ── Anchor (a): sub-cent DOGE strike round-trips through DB ────────────


def test_doge_subcent_threshold_roundtrips_to_db(tmp_path):
    """DOGE floor_strike=0.1134 must land in DB as threshold=0.1134 (NOT 0.11).

    This is the regression check for the precision-collapse bug. Pre-fix:
    0.1134 → `int(round(0.1134 * 100))` = 11 → `strike_cents/100.0` = 0.11.
    Post-fix: threshold REAL preserves all 4 decimal places.
    """
    from scripts.backfill.hype_doge_replay_backfill import (
        ensure_schema, _floor_strike_to_db_fields,
    )
    db = tmp_path / "state.db"
    conn = _open(db)
    ensure_schema(conn)

    threshold_real, strike_cents_legacy = _floor_strike_to_db_fields(0.1134)
    assert threshold_real == pytest.approx(0.1134, abs=1e-9), (
        f"_floor_strike_to_db_fields must preserve sub-cent precision; "
        f"got threshold_real={threshold_real}, expected 0.1134"
    )
    # Legacy strike_cents stays integer for HYPE back-compat
    assert strike_cents_legacy == 11

    # Verify schema has threshold column
    cols = {row[1] for row in conn.execute(
        f"PRAGMA table_info({REPLAY_TABLE})"
    ).fetchall()}
    conn.close()
    assert "threshold" in cols, (
        f"`ensure_schema()` must add threshold REAL column to {REPLAY_TABLE}. "
        f"Got cols: {sorted(cols)}"
    )


def test_doge_replay_market_writes_real_threshold(tmp_path):
    """`replay_market` end-to-end DOGE: pass market with floor_strike=0.1134
    (via `_floor_strike_to_db_fields`), verify the written DB row has
    `threshold=0.1134` REAL (not 0.11 derived from int strike_cents)."""
    from scripts.backfill.hype_doge_replay_backfill import (
        ensure_schema, replay_market, _floor_strike_to_db_fields,
    )
    db = tmp_path / "state.db"
    conn = _open(db)
    ensure_schema(conn)

    close_iso = "2026-04-01T12:00:00Z"
    open_iso = "2026-04-01T11:45:00Z"
    close_ts = _close_ts(close_iso)
    threshold_real, strike_cents_legacy = _floor_strike_to_db_fields(0.1134)

    market = {
        "ticker": "KXDOGE15M-26APR011200-1134",
        "event_ticker": "KXDOGE15M-26APR011200",
        "asset": "DOGE",
        "threshold": threshold_real,
        "strike_cents": strike_cents_legacy,
        "open_time": open_iso,
        "close_time": close_iso,
        "result": "yes",
    }
    ticks = _fixture_tick_buffer(close_ts, base_price=0.1134)
    replay_market(conn, market, ticks, tick_interval_secs=1.0)
    conn.commit()

    row = conn.execute(
        f"SELECT threshold, strike_cents FROM {REPLAY_TABLE}"
    ).fetchone()
    conn.close()
    assert row[0] == pytest.approx(0.1134, abs=1e-9), (
        f"DB threshold column must preserve sub-cent precision; got {row[0]}"
    )
    assert row[1] == 11  # legacy strike_cents stays integer


# ── Anchor (c): replay_market consumes threshold_real, not strike_cents/100.0 ──


def test_replay_market_uses_threshold_not_strike_cents_division(tmp_path):
    """Differential: write the SAME market twice with disagreeing thresholds,
    holding everything else (spot, vol, stc, strike_cents) identical. If
    `replay_market` reads `market["threshold"]` for the BS calc, the two
    rows have DIFFERENT raw_prob. If it secretly derives `strike_cents/100.0`,
    the two rows have IDENTICAL raw_prob (the strike_cents collision masks
    the threshold change → bug reproducer).

    Concretely:
      Market A: threshold=0.1234 REAL (precision-preserving DOGE)
      Market B: threshold=0.1100 REAL (legacy `strike_cents/100.0` of 11)
      Both: strike_cents=11, identical ticks/spot/vol.
    Under the fix, Market A's raw_prob < Market B's raw_prob (spot ~$0.115
    is BELOW $0.1234 but ABOVE $0.11). Under the bug, the two raw_probs
    are equal (because BS used strike_cents/100.0 = 0.11 for both).
    """
    from scripts.backfill.hype_doge_replay_backfill import (
        ensure_schema, replay_market,
    )
    db = tmp_path / "state.db"
    conn = _open(db)
    ensure_schema(conn)

    close_iso = "2026-04-01T12:00:00Z"
    open_iso = "2026-04-01T11:45:00Z"
    close_ts = _close_ts(close_iso)

    market_a = {
        "ticker": "KXDOGE15M-DIFFERENTIAL-A",
        "event_ticker": "KXDOGE15M-DIFFERENTIAL",
        "asset": "DOGE",
        "threshold": 0.1234,  # precision-preserved REAL
        "strike_cents": 11,   # legacy rounded (deliberate disagreement vs threshold)
        "open_time": open_iso,
        "close_time": close_iso,
        "result": "yes",
    }
    market_b = dict(market_a)
    market_b["ticker"] = "KXDOGE15M-DIFFERENTIAL-B"
    market_b["threshold"] = 0.1100  # what legacy strike_cents/100.0 would produce

    # Deterministic ticks: linear drift 0.114 → 0.116 over 1800s. Spot at
    # eval ≈ 0.116. Vol is dominated by the drift; raw_prob = f(spot, threshold, vol).
    ticks_a = [
        (close_ts - 1800 + i, 0.114 + (i / 1800.0) * 0.002)
        for i in range(1800)
    ]
    ticks_b = list(ticks_a)  # identical history → identical vol input
    replay_market(conn, market_a, ticks_a, tick_interval_secs=1.0)
    replay_market(conn, market_b, ticks_b, tick_interval_secs=1.0)
    conn.commit()

    rows = dict(conn.execute(
        f"SELECT ticker, raw_prob FROM {REPLAY_TABLE} ORDER BY ticker"
    ).fetchall())
    conn.close()

    raw_a = rows.get("KXDOGE15M-DIFFERENTIAL-A")
    raw_b = rows.get("KXDOGE15M-DIFFERENTIAL-B")
    if raw_a is None or raw_b is None:
        pytest.skip("raw_prob honest-NULL; vol estimator degenerate on fixture")
    # Differential expectation: A's threshold is HIGHER than B's, so for the
    # same spot, A.raw_prob < B.raw_prob (less confident that spot will stay
    # ABOVE the higher threshold). If `replay_market` ignored `market["threshold"]`
    # and re-derived from strike_cents, raw_a == raw_b — bug surface.
    assert abs(raw_a - raw_b) > 0.01, (
        f"raw_prob must depend on `market['threshold']`, not `strike_cents/100.0`. "
        f"raw_a={raw_a:.6f} (threshold=0.1234) vs raw_b={raw_b:.6f} "
        f"(threshold=0.1100): equality implies the legacy strike_cents/100.0 "
        f"derivation is still in effect inside `replay_market`."
    )
    assert raw_a < raw_b, (
        f"With same spot, higher threshold should reduce P(spot > threshold). "
        f"raw_a={raw_a:.4f} (threshold=0.1234), raw_b={raw_b:.4f} "
        f"(threshold=0.1100); expected raw_a < raw_b."
    )


# ── Anchor (d): HYPE integer-cent floor_strike round-trips identically ──


def test_hype_integer_floor_strike_no_regression(tmp_path):
    """HYPE floor_strike=45.0 (integer cents at this asset magnitude) must
    round-trip identically: threshold=45.0, strike_cents=4500.

    Regression: HYPE corpus (4847 markets) is correct as-is per the RCA doc.
    Post-fix re-runs on HYPE must not perturb the existing values."""
    from scripts.backfill.hype_doge_replay_backfill import (
        ensure_schema, replay_market, _floor_strike_to_db_fields,
    )
    db = tmp_path / "state.db"
    conn = _open(db)
    ensure_schema(conn)

    threshold_real, strike_cents_legacy = _floor_strike_to_db_fields(45.0)
    assert threshold_real == pytest.approx(45.0, abs=1e-9)
    assert strike_cents_legacy == 4500

    close_iso = "2026-04-01T12:00:00Z"
    open_iso = "2026-04-01T11:45:00Z"
    close_ts = _close_ts(close_iso)
    market = {
        "ticker": "KXHYPE15M-26APR011200-4500",
        "event_ticker": "KXHYPE15M-26APR011200",
        "asset": "HYPE",
        "threshold": threshold_real,
        "strike_cents": strike_cents_legacy,
        "open_time": open_iso,
        "close_time": close_iso,
        "result": "yes",
    }
    ticks = _fixture_tick_buffer(close_ts, base_price=45.0)
    # Drift over 30 min: +$0.50 (linear), so spot at eval ≈ 45.5
    replay_market(conn, market, ticks, tick_interval_secs=1.0)
    conn.commit()
    row = conn.execute(
        f"SELECT threshold, strike_cents FROM {REPLAY_TABLE}"
    ).fetchone()
    conn.close()
    assert row[0] == pytest.approx(45.0, abs=1e-9)
    assert row[1] == 4500


# ── Anchor (e): extract uses threshold field when present ─────────────


def test_extract_build_feature_frame_uses_threshold_when_present():
    """`build_feature_frame` must prefer `df['threshold']` REAL over
    `df['strike_cents'] / 100.0`. Without this, the precision-preserving
    backfill change is functionally inert for downstream extract.
    """
    import importlib
    sys.path.insert(0, str(REPO_ROOT / "scripts" / "cal_mlp"))
    mod = importlib.import_module("extract_data_replay")
    importlib.reload(mod)
    build_feature_frame = mod.build_feature_frame
    from bot.helpers.derived_features import compute_hour_sin_cos
    # Eval at 11:45 UTC → hour=11. Fixture must use canonical helper or
    # `build_feature_frame` raises Phase2ContractError on the lock-step check.
    h_sin, h_cos = compute_hour_sin_cos(11)

    rows = [{
        "ticker": "KXDOGE15M-26APR011200-1134",
        "evaluation_time": "2026-04-01T11:45:00Z",
        "close_time": "2026-04-01T12:00:00Z",
        "open_time": "2026-04-01T11:45:00Z",
        "asset": "DOGE",
        "threshold": 0.1134,        # NEW — precision-preserving
        "strike_cents": 11,         # legacy rounded
        "raw_prob": 0.5,
        "calibrated_prob": 0.5,
        "blended_prob": None,
        "spot_at_evaluation": 0.1134,
        "sigma_at_evaluation": 0.003,
        "hour_sin": h_sin,
        "hour_cos": h_cos,
        "prob_breakeven_gap": None,
        "sigma_winsorize": 0.0,
        "result": "yes",
        "settlement_value": 100,
        "data_provenance": "replay_phase2_v1",
        "replay_run_ts": 0,
    }]
    df = build_feature_frame(rows)
    # spot_distance_to_strike_sigma = (spot - strike_$) / (sigma * sqrt(stc/5) * 100)
    # spot = 0.1134, strike_$ from threshold = 0.1134 → numerator = 0 → sd = 0
    # If extract used strike_cents/100.0 = 0.11, numerator = 0.0034 > 0 → sd > 0
    sd = float(df["spot_distance_to_strike_sigma"].iloc[0])
    assert abs(sd) < 1e-3, (
        f"build_feature_frame must derive strike_dollars from REAL `threshold` "
        f"(0.1134), not legacy `strike_cents/100.0` (0.11). "
        f"With spot==threshold, sd should be ~0; got {sd:.6f}. If |sd| ~0.3, "
        f"extract is still using the rounded path."
    )


# ── Anchor (f): extract falls back to strike_cents when threshold NULL ─


def test_extract_build_feature_frame_falls_back_to_strike_cents_when_threshold_null():
    """HYPE legacy bundles pre-fix have threshold=NULL (column may not exist
    or may be NULL after migration). Extract must fall back to
    `strike_cents/100.0` so existing HYPE corpora don't break.
    """
    import importlib
    sys.path.insert(0, str(REPO_ROOT / "scripts" / "cal_mlp"))
    mod = importlib.import_module("extract_data_replay")
    importlib.reload(mod)
    build_feature_frame = mod.build_feature_frame
    from bot.helpers.derived_features import compute_hour_sin_cos
    h_sin, h_cos = compute_hour_sin_cos(11)

    rows = [{
        "ticker": "KXHYPE15M-26APR011200-4500",
        "evaluation_time": "2026-04-01T11:45:00Z",
        "close_time": "2026-04-01T12:00:00Z",
        "open_time": "2026-04-01T11:45:00Z",
        "asset": "HYPE",
        "threshold": None,          # NULL → fallback path
        "strike_cents": 4500,
        "raw_prob": 0.5,
        "calibrated_prob": 0.5,
        "blended_prob": None,
        "spot_at_evaluation": 45.0,
        "sigma_at_evaluation": 0.003,
        "hour_sin": h_sin,
        "hour_cos": h_cos,
        "prob_breakeven_gap": None,
        "sigma_winsorize": 0.0,
        "result": "yes",
        "settlement_value": 100,
        "data_provenance": "replay_phase2_v1",
        "replay_run_ts": 0,
    }]
    df = build_feature_frame(rows)
    # spot == strike_cents/100.0 == 45.0 → sd ≈ 0
    sd = float(df["spot_distance_to_strike_sigma"].iloc[0])
    assert abs(sd) < 1e-3, (
        f"build_feature_frame must fall back to `strike_cents/100.0` when "
        f"`threshold` is NULL (HYPE legacy path). With spot==45.0 and "
        f"strike_cents=4500, sd should be ~0; got {sd:.6f}."
    )


# ── Anchor (e+f extension): replay_market dispatches on threshold not strike_cents ──


def test_replay_market_call_site_reads_threshold_key(tmp_path):
    """AST + behavior: `replay_market` reads `market["threshold"]` (REAL)
    for the ProbabilityEngine BS path. If a future refactor accidentally
    reintroduces `threshold = market["strike_cents"] / 100.0` inside
    `replay_market`, this test fails.

    Implementation: pass a market dict where threshold and strike_cents
    DISAGREE in the precision-critical way (threshold=0.1134, strike_cents=99).
    If replay_market uses threshold, BS computes at the at-the-money point
    (spot 0.1134 == threshold). If it uses strike_cents/100.0=0.99, the spot
    is wildly below — raw_prob should be ~0% rather than ~50%.
    """
    from scripts.backfill.hype_doge_replay_backfill import (
        ensure_schema, replay_market,
    )
    db = tmp_path / "state.db"
    conn = _open(db)
    ensure_schema(conn)

    close_iso = "2026-04-01T12:00:00Z"
    open_iso = "2026-04-01T11:45:00Z"
    close_ts = _close_ts(close_iso)
    # Deliberate disagreement: threshold REAL = 0.1134, but strike_cents
    # is a stale unrelated value 99. Production code path always derives
    # both from the same `floor_strike` via `_floor_strike_to_db_fields`,
    # but the contract here is: replay_market trusts `threshold`.
    market = {
        "ticker": "KXDOGE15M-DISAGREEMENT-TEST",
        "event_ticker": "KXDOGE15M-DISAGREEMENT",
        "asset": "DOGE",
        "threshold": 0.1134,
        "strike_cents": 99,
        "open_time": open_iso,
        "close_time": close_iso,
        "result": "yes",
    }
    ticks = [
        (close_ts - 1800 + i, 0.1134 + i * 1e-9)
        for i in range(1800)
    ]
    replay_market(conn, market, ticks, tick_interval_secs=1.0)
    conn.commit()
    raw_prob = conn.execute(
        f"SELECT raw_prob FROM {REPLAY_TABLE}"
    ).fetchone()[0]
    conn.close()

    if raw_prob is None:
        pytest.skip("raw_prob honest-NULL; cannot verify threshold dispatch")
    # With spot==0.1134 and threshold==0.1134 → at-money → raw_prob ~50%.
    # If replay_market wrongly used strike_cents/100.0=0.99 → spot of 0.1134
    # is 89% BELOW strike → raw_prob ~0%.
    assert raw_prob > 0.20, (
        f"replay_market must read `market['threshold']` for BS calc, not "
        f"derive `strike_cents/100.0`. raw_prob={raw_prob:.4f} suggests "
        f"the stale strike_cents=99 → $0.99 was used (spot far below)."
    )
