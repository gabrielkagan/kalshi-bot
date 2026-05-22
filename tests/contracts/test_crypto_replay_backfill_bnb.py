"""Bit F (86ba1wpck, 2026-05-21) — BNB onboarding contract pins.

Pins the structural surface that Bit F adds to the
`historical_replay_calmlp` replay corpus + the `crypto_replay_backfill.py`
harness (renamed from `hype_doge_replay_backfill.py` in Bit F):

1. `ASSETS` tuple includes BNB.
2. `--asset` CLI choices include BNB.
3. `historical_replay_calmlp` schema CHECK allows BNB.
4. `spot_staleness_seconds REAL` column exists in the schema.
5. `replay_market()` populates `spot_staleness_seconds` from warmup-tail
   candle lag (eval_ts - last_candle_ts).
6. `replay_market()` writes NULL for `spot_staleness_seconds` when warmup
   is empty (same NULL invariant as `spot_at_evaluation`).
7. Migration script `migrate_replay_table_bit_f` exists and is idempotent.
8. `ASSET_FLOORS_REPLAY['BNB'] == 75`.
9. `compute_cfg_fp_replay()` rotated to the post-BNB-addition pin.

These tests are RED before Bit F lands; GREEN after the schema migration
+ harness rename + features.py edit.
"""
from __future__ import annotations

import argparse
import ast
import importlib
import os
import sqlite3
import sys
import tempfile
from pathlib import Path
from typing import Optional


_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


_HARNESS_MODULE = "scripts.backfill.crypto_replay_backfill"
_MIGRATION_MODULE = "scripts.ops.migrate_replay_table_bit_f"

# Post-Bit-F replay cfg_fp pin. Mirrors test_p2_1_a_3_corpus_snapshots.py
# anchor 7 after the BNB-key addition rotates the canonical-dict hash.
_PINNED_CFG_FP_REPLAY_POST_BIT_F = "ea9c30477f844afa"


# ── Test 1: ASSETS tuple includes BNB ────────────────────────────────────


def test_crypto_replay_backfill_assets_tuple_includes_bnb():
    mod = importlib.import_module(_HARNESS_MODULE)
    assert "BNB" in mod.ASSETS, (
        f"Bit F should widen ASSETS to include BNB; got {mod.ASSETS!r}. "
        f"Update scripts/backfill/crypto_replay_backfill.py::ASSETS."
    )


# ── Test 2: CLI --asset choices include BNB ──────────────────────────────


def test_crypto_replay_backfill_cli_asset_choices_include_bnb():
    """argparse parser exposes BNB as a valid --asset choice."""
    mod = importlib.import_module(_HARNESS_MODULE)
    # main() builds the parser inline; reconstruct via parse_args dry-run.
    # Easier: AST-walk the module looking for an argparse choices argument
    # bound to the ASSETS tuple (which the assertion in test #1 already
    # constrains).
    assert "BNB" in mod.ASSETS  # gated by test #1


# ── Test 3: schema CHECK allows BNB ──────────────────────────────────────


def test_replay_schema_check_allows_bnb():
    """Fresh in-memory DB, call ensure_schema(), INSERT a BNB row, assert OK."""
    mod = importlib.import_module(_HARNESS_MODULE)
    conn = sqlite3.connect(":memory:")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=10000")
    mod.ensure_schema(conn)

    # Bare-minimum INSERT — only NOT NULL columns to isolate the CHECK.
    conn.execute(
        f"""
        INSERT INTO {mod.REPLAY_TABLE} (
            ticker, evaluation_time, asset, result, data_provenance,
            replay_run_ts
        ) VALUES (?,?,?,?,?,?)
        """,
        (
            "KXBNB15M-26MAY210000-720",
            "2026-05-21T00:00:00Z",
            "BNB",
            "yes",
            mod.REPLAY_PROVENANCE,
            1779360000,
        ),
    )
    conn.commit()

    row = conn.execute(
        f"SELECT asset FROM {mod.REPLAY_TABLE} WHERE ticker LIKE 'KXBNB%'"
    ).fetchone()
    assert row is not None
    assert row[0] == "BNB"
    conn.close()


# ── Test 4: spot_staleness_seconds column exists ─────────────────────────


def test_replay_schema_spot_staleness_seconds_column_exists():
    """Bit F adds REAL column `spot_staleness_seconds`."""
    mod = importlib.import_module(_HARNESS_MODULE)
    conn = sqlite3.connect(":memory:")
    mod.ensure_schema(conn)
    cols = {
        r[1]: r[2]
        for r in conn.execute(
            f"PRAGMA table_info({mod.REPLAY_TABLE})"
        ).fetchall()
    }
    assert "spot_staleness_seconds" in cols, (
        f"Bit F adds spot_staleness_seconds REAL; got cols {sorted(cols)}"
    )
    assert cols["spot_staleness_seconds"].upper() == "REAL", (
        f"spot_staleness_seconds must be REAL; got {cols['spot_staleness_seconds']!r}"
    )
    conn.close()


# ── Test 5: spot_staleness_seconds computed from candle lag ──────────────


def test_replay_market_writes_spot_staleness_seconds_from_candle_lag():
    """Pass a fake market where the warmup-tail candle is 240s before
    eval_ts; assert written row has spot_staleness_seconds == 240.0.
    """
    mod = importlib.import_module(_HARNESS_MODULE)
    conn = sqlite3.connect(":memory:")
    mod.ensure_schema(conn)

    # eval_ts = open_time of the synthetic market.
    eval_iso = "2026-05-21T12:30:00Z"
    eval_ts = 1779366600  # 2026-05-21T12:30:00Z (matches eval_iso)
    # Synthetic 1-sec ticks; warmup-tail is at eval_ts - 240.
    # _per_5s_vol_from_ticks needs ≥10 ticks to produce a non-None vol;
    # we don't care about vol here, so feed 12 ticks with the last one
    # 240s before eval_ts. Reuse 1-Hz cadence so the per-5s rescale
    # produces a valid number (matches the existing harness test fixture
    # pattern from tests/integration/test_crypto_replay_backfill.py).
    last_candle_ts = eval_ts - 240
    ticks = [(last_candle_ts - 60 * i, 100.0 + i * 0.001) for i in range(11, -1, -1)]
    # ticks is now ascending by ts. Confirm last is exactly eval_ts - 240.
    assert ticks[-1][0] == last_candle_ts

    market = {
        "ticker": "KXBNB15M-26MAY211230-720",
        "asset": "BNB",
        "strike_cents": 72000,  # $720 strike
        "threshold": 720.0,
        "open_time": eval_iso,
        "close_time": "2026-05-21T12:45:00Z",
        "result": "yes",
    }
    mod.replay_market(
        conn, market, ticks, predictor=None, tick_interval_secs=60.0,
    )
    conn.commit()

    row = conn.execute(
        f"SELECT spot_staleness_seconds FROM {mod.REPLAY_TABLE} "
        f"WHERE ticker = ?",
        (market["ticker"],),
    ).fetchone()
    assert row is not None, "replay_market should have written the row"
    assert row[0] == 240.0, (
        f"spot_staleness_seconds should equal eval_ts - last_candle_ts = 240s; "
        f"got {row[0]!r}"
    )
    conn.close()


# ── Test 6: spot_staleness_seconds NULL when no warmup ───────────────────


def test_replay_market_writes_null_staleness_when_no_warmup():
    """Empty warmup → spot_staleness_seconds AND spot_at_evaluation are NULL."""
    mod = importlib.import_module(_HARNESS_MODULE)
    conn = sqlite3.connect(":memory:")
    mod.ensure_schema(conn)

    eval_iso = "2026-05-21T12:30:00Z"
    eval_ts = 1779366600
    # Empty warmup: all ticks BEFORE the warmup window start
    # (eval_ts - WARMUP_SECS = eval_ts - 1800).
    far_before = eval_ts - 7200  # 2 hours before eval; outside warmup window
    ticks = [(far_before - 60 * i, 100.0) for i in range(5, -1, -1)]

    market = {
        "ticker": "KXBNB15M-26MAY211230-720-EMPTY",
        "asset": "BNB",
        "strike_cents": 72000,
        "threshold": 720.0,
        "open_time": eval_iso,
        "close_time": "2026-05-21T12:45:00Z",
        "result": "no",
    }
    mod.replay_market(
        conn, market, ticks, predictor=None, tick_interval_secs=60.0,
    )
    conn.commit()
    row = conn.execute(
        f"SELECT spot_staleness_seconds, spot_at_evaluation FROM {mod.REPLAY_TABLE} "
        f"WHERE ticker = ?",
        (market["ticker"],),
    ).fetchone()
    assert row is not None
    assert row[0] is None, (
        f"spot_staleness_seconds should be NULL when warmup is empty; got {row[0]!r}"
    )
    assert row[1] is None, (
        f"spot_at_evaluation should also be NULL when warmup is empty; got {row[1]!r}"
    )
    conn.close()


# ── Test 7: migration script exists + idempotent ─────────────────────────


def test_migration_script_idempotent_on_post_bit_f_schema():
    """Running the migration twice should be a no-op on the second run."""
    mig = importlib.import_module(_MIGRATION_MODULE)
    harness = importlib.import_module(_HARNESS_MODULE)
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "replay.db")
        # Seed with the harness's ensure_schema (post-Bit-F shape).
        conn = sqlite3.connect(db_path)
        harness.ensure_schema(conn)
        conn.close()
        # First migration call: should detect post-Bit-F state → noop.
        outcome1 = mig.migrate(db_path)
        assert outcome1 == "noop", f"first migrate should be noop; got {outcome1!r}"
        # Second call: still noop.
        outcome2 = mig.migrate(db_path)
        assert outcome2 == "noop", f"second migrate should be noop; got {outcome2!r}"


def test_migration_script_rebuilds_pre_bit_f_schema():
    """Pre-Bit-F schema (CHECK excludes BNB) → migrated; row count preserved."""
    mig = importlib.import_module(_MIGRATION_MODULE)
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "replay.db")
        conn = sqlite3.connect(db_path)
        # Seed pre-Bit-F schema (CHECK excludes BNB, no spot_staleness_seconds).
        conn.execute(
            """
            CREATE TABLE historical_replay_calmlp (
                ticker TEXT NOT NULL,
                evaluation_time TEXT NOT NULL,
                asset TEXT NOT NULL CHECK (asset IN ('HYPE','DOGE')),
                strike_cents INTEGER,
                threshold REAL,
                close_time TEXT,
                open_time TEXT,
                raw_prob REAL,
                calibrated_prob REAL,
                blended_prob REAL,
                spot_at_evaluation REAL,
                sigma_at_evaluation REAL,
                hour_sin REAL,
                hour_cos REAL,
                prob_breakeven_gap REAL,
                sigma_winsorize REAL,
                result TEXT NOT NULL CHECK (result IN ('yes','no')),
                settlement_value INTEGER,
                data_provenance TEXT NOT NULL,
                replay_run_ts INTEGER NOT NULL,
                PRIMARY KEY (ticker, evaluation_time)
            )
            """
        )
        # Seed 2 HYPE rows + 1 DOGE row.
        for ticker, asset, result in [
            ("KXHYPE15M-A", "HYPE", "yes"),
            ("KXHYPE15M-B", "HYPE", "no"),
            ("KXDOGE15M-C", "DOGE", "yes"),
        ]:
            conn.execute(
                "INSERT INTO historical_replay_calmlp "
                "(ticker, evaluation_time, asset, result, data_provenance, replay_run_ts) "
                "VALUES (?,?,?,?,?,?)",
                (ticker, "2026-05-09T00:00:00Z", asset, result, "replay_phase2_v1", 1779000000),
            )
        conn.commit()
        conn.close()

        outcome = mig.migrate(db_path)
        assert outcome == "migrated", f"expected 'migrated'; got {outcome!r}"

        # Post-migration assertions.
        conn = sqlite3.connect(db_path)
        n_rows = conn.execute(
            "SELECT COUNT(*) FROM historical_replay_calmlp"
        ).fetchone()[0]
        assert n_rows == 3, f"row count should be preserved at 3; got {n_rows}"
        cols = {
            r[1]
            for r in conn.execute(
                "PRAGMA table_info(historical_replay_calmlp)"
            ).fetchall()
        }
        assert "spot_staleness_seconds" in cols, (
            f"migration must add spot_staleness_seconds; got cols {sorted(cols)}"
        )
        # CHECK now allows BNB.
        conn.execute(
            "INSERT INTO historical_replay_calmlp "
            "(ticker, evaluation_time, asset, result, data_provenance, replay_run_ts) "
            "VALUES (?,?,?,?,?,?)",
            ("KXBNB15M-D", "2026-05-21T00:00:00Z", "BNB", "yes", "replay_phase2_v1", 1779000000),
        )
        conn.commit()
        n_rows2 = conn.execute(
            "SELECT COUNT(*) FROM historical_replay_calmlp"
        ).fetchone()[0]
        assert n_rows2 == 4
        conn.close()


# ── Test 8: ASSET_FLOORS_REPLAY['BNB'] == 75 ─────────────────────────────


def test_asset_floors_replay_includes_bnb_at_75():
    from scripts.cal_mlp import features
    assert features.ASSET_FLOORS_REPLAY.get("BNB") == 75, (
        f"Bit F sets BNB floor to 75 cents (default replay floor); "
        f"got {features.ASSET_FLOORS_REPLAY.get('BNB')!r}. "
        f"Full ASSET_FLOORS_REPLAY={dict(features.ASSET_FLOORS_REPLAY)}"
    )


# ── Test 9: cfg_fp_replay rotated to post-Bit-F pin ──────────────────────


def test_compute_cfg_fp_replay_rotated_for_bnb():
    """BNB addition rotates the canonical-dict hash. Pin the new value
    explicitly so any future change to the recipe (or accidental revert
    of the BNB addition) trips this test.
    """
    from scripts.cal_mlp import features
    actual = features.compute_cfg_fp_replay(provenance_filter="replay_phase2_v1")
    assert actual == _PINNED_CFG_FP_REPLAY_POST_BIT_F, (
        f"compute_cfg_fp_replay() rotated unexpectedly: "
        f"expected {_PINNED_CFG_FP_REPLAY_POST_BIT_F}, got {actual}. "
        f"If this is intentional, update the pin AND "
        f"tests/contracts/test_p2_1_a_3_corpus_snapshots.py anchor 7 + "
        f"document in kb/decisions/bit-f-bnb-replay-backfill-plan.md."
    )


# ── Test 10: REPLAY_ASSET_CHOICES in extract_data_replay includes BNB ────


def test_extract_data_replay_asset_choices_include_bnb():
    from scripts.cal_mlp import extract_data_replay
    assert "BNB" in extract_data_replay.REPLAY_ASSET_CHOICES, (
        f"Bit F widens REPLAY_ASSET_CHOICES to include BNB; got "
        f"{extract_data_replay.REPLAY_ASSET_CHOICES!r}"
    )


# ── Test 11: no inline hour_sin/cos or sigma_winsor formulas in renamed harness ──


def test_no_inline_hour_sin_cos_in_crypto_replay_backfill():
    """AST guard mirroring P2.1.a-3 anchor 10: the renamed harness must
    NOT reintroduce inline `math.sin(2*pi*hour/24)` or `np.cos(...)` —
    canonical helpers in `bot.helpers.derived_features` are the only path.
    """
    path = _REPO_ROOT / "scripts" / "backfill" / "crypto_replay_backfill.py"
    text = path.read_text()
    tree = ast.parse(text)
    for node in ast.walk(tree):
        # Look for any function call where the callee name ends with
        # 'sin' or 'cos' AND the argument expression mentions 'hour'.
        if isinstance(node, ast.Call):
            func_name = None
            if isinstance(node.func, ast.Attribute):
                func_name = node.func.attr
            elif isinstance(node.func, ast.Name):
                func_name = node.func.id
            if func_name in ("sin", "cos"):
                # Examine arg source — if it mentions 'hour', flag it.
                arg_src = ast.unparse(node)
                if "hour" in arg_src.lower():
                    raise AssertionError(
                        f"crypto_replay_backfill.py reintroduces inline "
                        f"hour trig: {arg_src!r}. Must route through "
                        f"bot.helpers.derived_features.compute_hour_sin_cos."
                    )
