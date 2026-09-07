"""86b9wy7v3 Phase 2 + Bit F (86ba1wpck) — HYPE/DOGE/BNB shadow backfill via
prediction-pipeline replay.

Pins the contract for `scripts/backfill/crypto_replay_backfill.py`:

- `historical_replay_calmlp` schema (cols + PK + CHECK constraints).
- PRIMARY KEY `(ticker, evaluation_time)` with INSERT OR REPLACE idempotency.
- `data_provenance` stamped `'replay_phase2_v1'` on every harness-written row.
- `result` CHECK-constrained to {'yes', 'no'}; `asset` CHECK-constrained to
  {'HYPE', 'DOGE', 'BNB'} (BNB added Bit F 2026-05-21).
- Lock-step parity: `hour_sin`/`hour_cos`/`sigma_winsorize`/`prob_breakeven_gap`
  must route through `bot.helpers.derived_features` canonical helpers — the
  harness MUST NOT duplicate the math. Mirrors the surface
  `tests/contracts/test_calmlp_lockstep.py` seals for the live-write sites
  (A.1b 2026-05-12, ticket `86b9veppa`).
- Bot-state features (market_price, OFT, depth, NBBO) are NULL on replay rows
  — Phase 2 v1 doesn't pull historical Kalshi orderbook; calibration-health
  use case (sister `86b9wy15n`) doesn't need them.

Phase 1 finding (POSITIVE): `kb/findings/hype-doge-kalshi-market-history-may12.md`.
Phase 1 outcome memory: `project_hype_doge_phase1_positive_may12.md`.
"""
from __future__ import annotations

import datetime
import sqlite3
import sys
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent.parent
HARNESS_SCRIPT = REPO_ROOT / "scripts" / "backfill" / "crypto_replay_backfill.py"

# Canonical helpers — the harness MUST call these (lock-step contract).
sys.path.insert(0, str(REPO_ROOT))
from bot.helpers.derived_features import (  # noqa: E402
    SIGMA_WINSOR_ABS_CAP,
    compute_hour_sin_cos,
)


REPLAY_TABLE = "historical_replay_calmlp"
REPLAY_PROVENANCE = "replay_phase2_v1"

# 21 cols post-Bit-F (19 pre-P2.3.b-fu2 + `threshold REAL` ticket `86b9xtam7`
# 2026-05-13 + `spot_staleness_seconds REAL` ticket `86ba1wpck` Bit F
# 2026-05-21). This list is the minimum REQUIRED subset; the schema-check
# below uses `set(EXPECTED_COLS) - set(cols)` so post-fu2/post-Bit-F extra
# cols are accepted. `threshold` deliberately NOT listed here so pre-fu2
# corpora (HYPE legacy) still satisfy the contract —
# `tests/contracts/test_p2_3_b_fu2_threshold_precision.py` pins the post-fu2
# `threshold` column presence separately. `spot_staleness_seconds` is
# pinned in `tests/contracts/test_crypto_replay_backfill_bnb.py`.
EXPECTED_COLS = [
    "ticker", "evaluation_time", "asset", "strike_cents",
    "close_time", "open_time",
    "raw_prob", "calibrated_prob", "blended_prob",
    "spot_at_evaluation", "sigma_at_evaluation",
    "hour_sin", "hour_cos",
    "prob_breakeven_gap", "sigma_winsorize",
    "result", "settlement_value", "data_provenance",
    "replay_run_ts",
]


def _open(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path), timeout=10.0)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=10000")
    return conn


def _fixture_tick_buffer(close_ts: int, base_price: float = 45.0,
                         warmup_secs: int = 1800) -> list[tuple[int, float]]:
    """Synthesize a 30-min tick history ending at close_ts (CoinbaseFeed shape).

    1 tick / second. Prices drift linearly +$0.50 over the warmup window so
    realized vol is small-but-nonzero (engine won't NULL it out).
    """
    return [
        (close_ts - warmup_secs + i, base_price + (i / warmup_secs) * 0.5)
        for i in range(warmup_secs)
    ]


def _fixture_market(
    asset: str = "HYPE",
    open_iso: str = "2026-04-01T11:45:00Z",
    close_iso: str = "2026-04-01T12:00:00Z",
    strike_cents: int = 4500,
    result: str = "yes",
) -> dict:
    series = f"KX{asset}15M"  # Bit F (2026-05-21): generalized from
                                # HYPE/DOGE-only ternary to support BNB
                                # without latent ticker corruption.
    return {
        "ticker": f"{series}-26APR011200-15",
        "event_ticker": f"{series}-26APR011200",
        "asset": asset,
        "strike_cents": strike_cents,
        "open_time": open_iso,
        "close_time": close_iso,
        "result": result,
    }


def _close_ts(iso: str) -> int:
    return int(
        datetime.datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()
    )


# ── Pillar 4 (TDD): harness script must exist ─────────────────────────


def test_harness_script_exists():
    assert HARNESS_SCRIPT.is_file(), (
        f"Harness script not found at {HARNESS_SCRIPT}. "
        "Phase 2 ticket 86b9wy7v3 must ship "
        "scripts/backfill/crypto_replay_backfill.py."
    )


def test_harness_exposes_api():
    """The harness must expose `replay_market`, `ensure_schema`, and the
    constants `REPLAY_PROVENANCE` + `REPLAY_TABLE`."""
    from scripts.backfill import crypto_replay_backfill as h
    assert hasattr(h, "replay_market"), "missing replay_market()"
    assert hasattr(h, "ensure_schema"), "missing ensure_schema()"
    assert getattr(h, "REPLAY_PROVENANCE", None) == REPLAY_PROVENANCE
    assert getattr(h, "REPLAY_TABLE", None) == REPLAY_TABLE


# ── Schema contracts ──────────────────────────────────────────────────


def test_table_created_with_expected_columns(tmp_path: Path):
    """`ensure_schema()` creates `historical_replay_calmlp` with at least the
    19 REQUIRED-minimum cols listed in `EXPECTED_COLS` (subset check;
    post-Bit-F the table has 21 cols total — `threshold REAL` added by
    P2.3.b-fu2 + `spot_staleness_seconds REAL` added by Bit F 2026-05-21)."""
    from scripts.backfill.crypto_replay_backfill import ensure_schema
    db = tmp_path / "state.db"
    conn = _open(db)
    ensure_schema(conn)
    cols = [
        row[1]
        for row in conn.execute(f"PRAGMA table_info({REPLAY_TABLE})").fetchall()
    ]
    conn.close()
    missing = set(EXPECTED_COLS) - set(cols)
    assert not missing, f"Missing columns: {missing}"


def test_primary_key_is_ticker_evaluation_time(tmp_path: Path):
    """PK must be composite (ticker, evaluation_time) — supports INSERT OR REPLACE
    idempotency on multi-evaluation replay (one row per evaluation moment per market)."""
    from scripts.backfill.crypto_replay_backfill import ensure_schema
    db = tmp_path / "state.db"
    conn = _open(db)
    ensure_schema(conn)
    pk_cols = sorted(
        row[1]
        for row in conn.execute(f"PRAGMA table_info({REPLAY_TABLE})").fetchall()
        if row[5] > 0
    )
    conn.close()
    assert pk_cols == ["evaluation_time", "ticker"], (
        f"PK should be (ticker, evaluation_time), got {pk_cols}"
    )


def test_result_check_constraint_rejects_invalid(tmp_path: Path):
    """`result` column must be CHECK-constrained to {'yes', 'no'} only."""
    from scripts.backfill.crypto_replay_backfill import ensure_schema
    db = tmp_path / "state.db"
    conn = _open(db)
    ensure_schema(conn)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            f"INSERT INTO {REPLAY_TABLE} "
            "(ticker, evaluation_time, asset, result, data_provenance, replay_run_ts) "
            "VALUES ('FAKE','2026-01-01T00:00:00Z','HYPE','maybe','x',0)"
        )
    conn.close()


def test_asset_check_constraint_rejects_unknown(tmp_path: Path):
    """`asset` must be CHECK-constrained to {'HYPE', 'DOGE', 'BNB'} post-Bit-F
    (BNB added via Bit F `86ba1wpck` 2026-05-21).

    If a future phase widens the replay to BTC/ETH/SOL/XRP, the CHECK gets
    widened in the same commit as the harness change (this guard catches
    accidental writes from other assets via copy-paste reuse)."""
    from scripts.backfill.crypto_replay_backfill import ensure_schema
    db = tmp_path / "state.db"
    conn = _open(db)
    ensure_schema(conn)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            f"INSERT INTO {REPLAY_TABLE} "
            "(ticker, evaluation_time, asset, result, data_provenance, replay_run_ts) "
            "VALUES ('BTC-X','2026-01-01T00:00:00Z','BTC','yes','x',0)"
        )
    conn.close()


# ── End-to-end shape: replay_market writes a row per market ───────────


@pytest.mark.parametrize("asset", ["HYPE", "DOGE"])
def test_replay_market_writes_one_row(tmp_path: Path, asset: str):
    """`replay_market(conn, market, ticks)` writes exactly one row to
    `historical_replay_calmlp` with the expected provenance + asset/result echo."""
    from scripts.backfill.crypto_replay_backfill import (
        ensure_schema,
        replay_market,
    )
    close_ts = _close_ts("2026-04-01T12:00:00Z")
    db = tmp_path / "state.db"
    conn = _open(db)
    ensure_schema(conn)

    market = _fixture_market(asset=asset)
    ticks = _fixture_tick_buffer(close_ts)
    # Fixture is 1-second-spaced; production is 1-min Coinbase klines.
    n_written = replay_market(conn, market, ticks, tick_interval_secs=1.0)
    conn.commit()

    n = conn.execute(f"SELECT COUNT(*) FROM {REPLAY_TABLE}").fetchone()[0]
    prov, written_asset, written_result = conn.execute(
        f"SELECT data_provenance, asset, result FROM {REPLAY_TABLE}"
    ).fetchone()
    conn.close()

    assert n_written == 1, "replay_market should return 1 for a single market"
    assert n == 1
    assert prov == REPLAY_PROVENANCE
    assert written_asset == asset
    assert written_result in ("yes", "no")


def test_idempotent_insert_or_replace(tmp_path: Path):
    """Re-running `replay_market` on the same market does NOT duplicate the row."""
    from scripts.backfill.crypto_replay_backfill import (
        ensure_schema,
        replay_market,
    )
    close_ts = _close_ts("2026-04-01T12:00:00Z")
    db = tmp_path / "state.db"
    conn = _open(db)
    ensure_schema(conn)
    market = _fixture_market(asset="HYPE")
    ticks = _fixture_tick_buffer(close_ts)

    replay_market(conn, market, ticks, tick_interval_secs=1.0)
    replay_market(conn, market, ticks, tick_interval_secs=1.0)  # second run, same PK
    conn.commit()

    n = conn.execute(f"SELECT COUNT(*) FROM {REPLAY_TABLE}").fetchone()[0]
    conn.close()
    assert n == 1, (
        "INSERT OR REPLACE on (ticker, evaluation_time) must keep row count at 1 "
        "across repeated runs (re-run safety per scripts/CLAUDE.md provenance pattern)"
    )


# ── Lock-step parity: derived features route through canonical helpers ──


def test_hour_sin_cos_matches_canonical(tmp_path: Path):
    """`hour_sin`/`hour_cos` MUST equal `compute_hour_sin_cos(hour_of_day_utc)`.

    This is the lock-step contract A.1b 2026-05-12 sealed at scripts/cal_mlp/ —
    extending it to the replay backfill site. Any inline drift (e.g.,
    `math.sin(2*pi*h/24)` written directly) breaks parity with the live-write
    distribution and re-opens the train/serve-skew surface.
    """
    from scripts.backfill.crypto_replay_backfill import (
        ensure_schema,
        replay_market,
    )
    open_iso = "2026-04-01T11:45:00Z"
    close_iso = "2026-04-01T12:00:00Z"
    close_ts = _close_ts(close_iso)

    db = tmp_path / "state.db"
    conn = _open(db)
    ensure_schema(conn)
    market = _fixture_market(open_iso=open_iso, close_iso=close_iso)
    ticks = _fixture_tick_buffer(close_ts)
    replay_market(conn, market, ticks, tick_interval_secs=1.0)
    conn.commit()

    hs, hc, eval_time = conn.execute(
        f"SELECT hour_sin, hour_cos, evaluation_time FROM {REPLAY_TABLE}"
    ).fetchone()
    conn.close()

    eval_hour = datetime.datetime.fromisoformat(
        eval_time.replace("Z", "+00:00")
    ).hour
    exp_hs, exp_hc = compute_hour_sin_cos(eval_hour)
    assert hs == pytest.approx(exp_hs, abs=1e-12), (
        f"hour_sin must match canonical helper (lock-step). "
        f"Got {hs}, expected {exp_hs}."
    )
    assert hc == pytest.approx(exp_hc, abs=1e-12), (
        f"hour_cos must match canonical helper (lock-step). "
        f"Got {hc}, expected {exp_hc}."
    )


def test_sigma_winsorize_clamps_at_cap(tmp_path: Path):
    """`sigma_winsorize` must clamp at ±SIGMA_WINSOR_ABS_CAP (=25.0).

    Construct inputs where raw sigma > 25 (spot far from strike, tiny vol)
    so the canonical-helper clamp fires."""
    from scripts.backfill.crypto_replay_backfill import (
        ensure_schema,
        replay_market,
    )
    close_ts = _close_ts("2026-04-01T12:00:00Z")
    db = tmp_path / "state.db"
    conn = _open(db)
    ensure_schema(conn)
    market = _fixture_market(strike_cents=4500)  # $45 strike
    # Spot ≈ $50 (far above strike); tick prices nearly flat (low realized vol).
    # buf_pct = (50 - 45)/45 × 100 = 11.1% on $5 separation; small vol → big sigma.
    ticks = [
        (close_ts - 1800 + i, 50.0 + i * 1e-7)
        for i in range(1800)
    ]
    replay_market(conn, market, ticks, tick_interval_secs=1.0)
    conn.commit()
    sigma = conn.execute(
        f"SELECT sigma_winsorize FROM {REPLAY_TABLE}"
    ).fetchone()[0]
    conn.close()

    if sigma is None:
        pytest.skip(
            "sigma was honest-NULL (engine returned None vol); cannot exercise clamp"
        )
    assert abs(sigma) <= SIGMA_WINSOR_ABS_CAP, (
        f"sigma_winsorize must clamp at ±{SIGMA_WINSOR_ABS_CAP} (canonical helper), "
        f"got {sigma}"
    )


def test_prob_breakeven_gap_honest_null_v1(tmp_path: Path):
    """Phase 2 v1 doesn't pull historical Kalshi orderbook → market_price
    unavailable → `prob_breakeven_gap` is honest-NULL.

    If a future Phase 2.5 adds historical orderbook fetch, the harness must
    route the gap derivation through `bot.helpers.derived_features.compute_derived_features`
    (lock-step). The honest-NULL is acceptable because the sister 86b9wy15n
    calibration health check only needs (calibrated_prob, result) pairs —
    not the gap feature."""
    from scripts.backfill.crypto_replay_backfill import (
        ensure_schema,
        replay_market,
    )
    close_ts = _close_ts("2026-04-01T12:00:00Z")
    db = tmp_path / "state.db"
    conn = _open(db)
    ensure_schema(conn)
    market = _fixture_market()
    ticks = _fixture_tick_buffer(close_ts)
    replay_market(conn, market, ticks, tick_interval_secs=1.0)
    conn.commit()
    gap = conn.execute(
        f"SELECT prob_breakeven_gap FROM {REPLAY_TABLE}"
    ).fetchone()[0]
    conn.close()
    assert gap is None, (
        "Phase 2 v1: prob_breakeven_gap must be honest-NULL "
        "(market_price unavailable without historical Kalshi orderbook)"
    )


# ── Settlement value echoes Kalshi market.result → numeric encoding ───


def test_warmup_window_is_bounded(tmp_path: Path):
    """Regression — R2 adv-review C2.

    The harness's per-market vol must use only the trailing `WARMUP_SECS`
    of ticks before `eval_ts`, NOT all preceding history. Mirrors the
    live `bot/engines/volatility.py` rolling 15-min deque convention.

    Construct a tick buffer where the first half has high vol and the
    second half (which contains the warmup-window) has near-zero vol.
    The replay's `sigma_at_evaluation` should reflect the second half
    only — if it reflects the first half, the bound was leaky.
    """
    from scripts.backfill.crypto_replay_backfill import (
        WARMUP_SECS,
        ensure_schema,
        replay_market,
    )
    open_iso = "2026-04-01T12:00:00Z"
    close_iso = "2026-04-01T12:15:00Z"
    eval_ts = _close_ts(open_iso)

    # Pre-warmup region (10h before eval_ts): wildly volatile ticks.
    # Warmup region [eval_ts - WARMUP_SECS, eval_ts]: near-flat ticks.
    pre_lo = eval_ts - 36_000  # 10h pre-warmup history
    pre_hi = eval_ts - WARMUP_SECS - 1
    rng = pre_hi - pre_lo
    pre_ticks = [
        (
            pre_lo + i,
            45.0 * (1.0 + 0.05 * (((i * 97) % 200) - 100) / 100.0),
        )
        for i in range(0, rng + 1, 60)  # 1-min spaced
    ]
    warmup_ticks = [
        (eval_ts - WARMUP_SECS + i * 60, 50.0 + i * 1e-7)
        for i in range(WARMUP_SECS // 60 + 1)
    ]
    all_ticks = pre_ticks + warmup_ticks

    db = tmp_path / "state.db"
    conn = _open(db)
    ensure_schema(conn)
    market = _fixture_market(open_iso=open_iso, close_iso=close_iso)
    replay_market(conn, market, all_ticks)  # tick_interval_secs default 60.0
    conn.commit()

    sigma_at_eval, spot_at_eval = conn.execute(
        "SELECT sigma_at_evaluation, spot_at_evaluation "
        f"FROM {REPLAY_TABLE}"
    ).fetchone()
    conn.close()

    # Pre-warmup buffer drifts 45 ± 5% on a 60s tick scale ⇒ stdev ~0.01-0.05.
    # Warmup buffer is near-flat at $50 ⇒ stdev_5s should be tiny (≈1e-6).
    # If the bound is leaky, sigma_at_eval inherits the pre-warmup magnitude.
    assert sigma_at_eval is not None, "vol should be computable from warmup ticks"
    assert sigma_at_eval < 1e-3, (
        f"sigma_at_evaluation must reflect ONLY the trailing {WARMUP_SECS}s "
        f"warmup window (near-flat = ~1e-7); got {sigma_at_eval} — "
        f"likely the unbounded pre-warmup region leaked in (R2 adv-review C2)"
    )
    # And spot is from the LAST tick in the warmup window, not the pre-warmup region.
    assert spot_at_eval is not None and abs(spot_at_eval - 50.0) < 1e-3


def test_settlement_value_encodes_result(tmp_path: Path):
    """`settlement_value` mirrors Kalshi conventions: 100 on 'yes', 0 on 'no'
    (the YES-contract payout at settlement). The pair (result, settlement_value)
    is redundant by design — keeps both columns usable for downstream queries
    that compute calibration error vs. binary outcome OR vs. dollar PnL."""
    from scripts.backfill.crypto_replay_backfill import (
        ensure_schema,
        replay_market,
    )
    close_ts = _close_ts("2026-04-01T12:00:00Z")
    db = tmp_path / "state.db"
    conn = _open(db)
    ensure_schema(conn)
    yes_market = _fixture_market(asset="HYPE", result="yes")
    no_market = _fixture_market(asset="DOGE", result="no")
    # Different ticker so PK doesn't collide
    no_market["ticker"] = "KXDOGE15M-26APR011200-15"
    ticks = _fixture_tick_buffer(close_ts)
    replay_market(conn, yes_market, ticks, tick_interval_secs=1.0)
    replay_market(conn, no_market, ticks, tick_interval_secs=1.0)
    conn.commit()

    rows = conn.execute(
        f"SELECT result, settlement_value FROM {REPLAY_TABLE} ORDER BY result"
    ).fetchall()
    conn.close()
    assert rows == [("no", 0), ("yes", 100)], (
        f"settlement_value must encode 'yes'→100, 'no'→0; got {rows}"
    )
