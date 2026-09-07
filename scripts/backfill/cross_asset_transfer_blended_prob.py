"""Bit A (86ba0jmzu) of HYPE/DOGE cal_mlp v1.1 retrain umbrella 86ba0jmyq.

Cross-asset transfer backfill: feed `historical_replay_calmlp` HYPE/DOGE
rows through `CalMLPPredictor("BTC")` and write the resulting cross-asset
prediction to the `blended_prob` column.

Why BTC's predictor on HYPE/DOGE rows?
    No HYPE/DOGE-trained cal_mlp predictor exists at either deployment site
    (per `scripts/cal_mlp/integration.py:1156` — production
    `_calmlp_predictors` covers only BTC/ETH/SOL/XRP). The original Phase 2
    replay backfill (`scripts/backfill/crypto_replay_backfill.py`)
    docstring explicitly scopes a "future cross-asset transfer eval (fu1 of
    86b9wy7v3)" through `CalMLPPredictor("BTC")` for exactly this reason.
    BTC's predictor sees an unseen HYPE/DOGE ticker and routes it to vocab
    ID 0 (`integration.py:1037` `_vocab.get(str(ticker), 0)`) — the
    documented cross-asset transfer mechanism.

Honest-NULL semantics:
    `CalMLPPredictor.predict()` raises `CalMLPError('missing_features')` when
    any `CONT_FEATURE_COLS` entry is missing AND has no `*_missing` indicator
    AND is not in `_IDENTITY_NO_Z`. On the CURRENT replay corpus
    (data/replay/state.db audit, 2026-05-19):

        asset | rows | NULL bp_gap | NULL raw_prob
        HYPE  | 4897 | 4897         | 50
        DOGE  | 4897 | 4897         | 26

    Every row has NULL `prob_breakeven_gap`. The 9,794-row candidate set
    partitions across two skip categories: 76 rows are short-circuited at
    the `if raw_prob is None: continue` pre-filter (predict never invoked);
    the remaining 9,718 rows reach predict but raise
    `CalMLPError('missing_features')` on the NULL bp_gap check. The
    counters dict exposes both buckets separately
    (`null_raw_prob=76` + `predict_raised=9,718`); `updated=0` on the
    current corpus. This is data telling us that Bit E (Kalshi
    trade-history scrape to backfill bp_gap) is a prerequisite for
    non-trivial Bit A outcome, NOT a Bit-D-gated optional follow-up. No
    fabricated values — the row stays NULL with a logged skip-reason.
    Once Bit E lands and bp_gap is populated, this script's idempotent
    re-run produces the real backfill.

Lock-step contract:
    All feature transforms route through canonical helpers
    (`bot.helpers.derived_features` + `scripts.cal_mlp.features.apply_sigma_winsor`).
    No inline math.sin/cos on hour-of-day; no inline sigma winsorization.
    Mirrors the sister-script pattern at
    `scripts/backfill/crypto_replay_backfill.py:80-84` (lock-step
    contract A.1b 2026-05-12, ticket 86b9veppa).

Plan doc: kb/decisions/v1-1-A-cross-asset-transfer-plan.md.
"""
from __future__ import annotations

import argparse
import logging
import os
import sqlite3
import sys
import time
from typing import Iterable, Optional

# Canonical helpers — lock-step contract per bot/CLAUDE.md "cal_mlp feature
# transforms (lock-step)". AST tests in tests/integration/ guard against
# inline drift reintroduction.
_REPO_ROOT = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
)
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from bot.helpers.derived_features import (  # noqa: E402
    compute_derived_features,
    compute_hour_sin_cos,
)


REPLAY_TABLE = "historical_replay_calmlp"

ASSETS = ("HYPE", "DOGE")

# Replay markets are 15-minute windows: open_time → close_time = 900s. We
# default to 900 if the row's close_time/open_time don't parse (defensive).
DEFAULT_SECONDS_TO_CLOSE = 900.0

# Replay rows have no live market_price (Kalshi historical orderbook not
# stored). Sentinel matches `crypto_replay_backfill.py:360`'s
# entry_price_cents=0 convention.
ENTRY_PRICE_CENTS_SENTINEL = 0

logger = logging.getLogger(__name__)


# ── Mac-only defensive guard ─────────────────────────────────────────


def _refuse_vps_path(db_path: str) -> None:
    """Mac-only invariant per feedback_vps_compute_isolation. The VPS has
    2vCPU/2GB/0-swap and would OOM under cal_mlp predictor warmup; this
    script is explicitly Mac compute.

    Refuses paths under /home/botuser/ (VPS layout). On VPS the bot's
    state.db lives at /home/botuser/kalshi-bot-repo/state.db.
    """
    abs_path = os.path.abspath(db_path)
    if abs_path.startswith("/home/botuser/"):
        raise ValueError(
            f"Refusing to operate on VPS path {db_path!r}. This script is "
            "Mac-only per feedback_vps_compute_isolation. Sync state.db to "
            "data/replay/ on the Mac and pass that path instead."
        )


# ── Predictor build ──────────────────────────────────────────────────


def _build_btc_predictor():
    """Build + warmup CalMLPPredictor('BTC'). Returns the predictor on
    success; raises on failure (caller exits non-zero).

    Failure modes (any of these raise; caller's try/except catches and
    exits before touching DB rows):
        - bundle missing (CURRENT pointer broken / no models/cal_mlp_BTC/)
        - torch import OOM
        - flock failure on shared volume
        - phase mismatch
    """
    from scripts.cal_mlp.integration import CalMLPPredictor
    predictor = CalMLPPredictor("BTC")
    predictor.warmup()
    if not getattr(predictor, "_loaded", False):
        raise RuntimeError(
            "CalMLPPredictor('BTC').warmup() returned without loading the "
            "bundle — bundle missing or warmup soft-failed silently."
        )
    return predictor


# ── Row-features assembly ─────────────────────────────────────────────


def _seconds_to_close(open_iso: Optional[str], close_iso: Optional[str]) -> float:
    """Return close_time − open_time in seconds, or DEFAULT_SECONDS_TO_CLOSE
    if either is missing/unparseable (replay table has them populated as
    ISO-8601 UTC strings; defensive default matches 15-min market convention)."""
    if not open_iso or not close_iso:
        return DEFAULT_SECONDS_TO_CLOSE
    try:
        import datetime
        open_dt = datetime.datetime.fromisoformat(open_iso.replace("Z", "+00:00"))
        close_dt = datetime.datetime.fromisoformat(close_iso.replace("Z", "+00:00"))
        delta = (close_dt - open_dt).total_seconds()
        return float(delta) if delta > 0 else DEFAULT_SECONDS_TO_CLOSE
    except (ValueError, TypeError):
        return DEFAULT_SECONDS_TO_CLOSE


def _build_row_features(row: dict) -> dict:
    """Assemble the `row_features` dict CalMLPPredictor.predict() consumes.

    Reads stored DB values for the lock-step features
    (`hour_sin`/`hour_cos`/`sigma_winsorize`/`prob_breakeven_gap`) — the DB
    is the source of truth. If a lock-step value is NULL (schema drift
    defense), we re-derive via the canonical helper using the other DB
    columns; this keeps the helper invocation present in the import graph
    (lock-step AST pin) AND hardens against partial-write schema drift.

    The `compute_derived_features` re-derive uses the SAME canonical helper
    the bot uses at live evaluation (`bot/state.py:2035`/`:2380` +
    `bot/engines/sports_engine.py:2174`/`:2246`) — preserves train/serve
    parity. `compute_hour_sin_cos` mirror sites: `bot/state.py:2026`.
    """
    seconds_to_close = _seconds_to_close(
        row.get("open_time"), row.get("close_time")
    )

    # Lock-step feature 1: hour_sin/cos. Use stored values; fall back via
    # canonical helper on NULL (schema-drift hardening).
    hour_sin = row.get("hour_sin")
    hour_cos = row.get("hour_cos")
    if hour_sin is None or hour_cos is None:
        import datetime
        open_iso = row.get("open_time") or row.get("evaluation_time")
        if open_iso:
            try:
                open_dt = datetime.datetime.fromisoformat(
                    open_iso.replace("Z", "+00:00")
                )
                hour_sin, hour_cos = compute_hour_sin_cos(open_dt.hour)
            except (ValueError, TypeError):
                pass

    # Lock-step feature 2: sigma_winsorize + prob_breakeven_gap. Use stored
    # values; canonical helper re-derive is invoked as fallback to keep the
    # import edge live AND to harden against schema drift.
    sigma_winsorize = row.get("sigma_winsorize")
    prob_breakeven_gap = row.get("prob_breakeven_gap")
    if sigma_winsorize is None or prob_breakeven_gap is None:
        # market_price_cents=None in replay (Kalshi historical orderbook
        # absent); prob_breakeven_gap stays None on this path. The predict
        # call below will raise CalMLPError on the NULL bp_gap — caller
        # catches + skips. NO fabricated value here.
        derived = compute_derived_features(
            spot_price=row.get("spot_at_evaluation"),
            threshold=row.get("threshold"),
            volatility=row.get("sigma_at_evaluation"),
            seconds_to_close=seconds_to_close,
            calibrated_prob=row.get("calibrated_prob"),
            market_price_cents=None,
        )
        if sigma_winsorize is None:
            # Re-derive uses raw sigma; predictor expects post-winsor. We
            # leave the stored NULL un-replaced (predict() raises) rather
            # than apply_sigma_winsor here — passing a re-derived winsor
            # value silently when the original was NULL would mask schema
            # drift downstream.
            pass
        if prob_breakeven_gap is None:
            prob_breakeven_gap = derived.get("prob_breakeven_gap")
            # Still None when market_price_cents=None — predict() raises;
            # caller skips.

    abs_sigma = abs(sigma_winsorize) if sigma_winsorize is not None else None
    if sigma_winsorize is not None:
        time_decayed_proximity = sigma_winsorize * max(
            0.0, min(1.0, 1.0 - seconds_to_close / 900.0)
        )
    else:
        time_decayed_proximity = None

    return {
        "calibrated_prob": row.get("calibrated_prob"),
        "spot_distance_to_strike_sigma": sigma_winsorize,
        "abs_spot_distance_to_strike_sigma": abs_sigma,
        "time_decayed_proximity": time_decayed_proximity,
        "hour_sin": hour_sin,
        "hour_cos": hour_cos,
        "prob_breakeven_gap": prob_breakeven_gap,
        "seconds_to_close": seconds_to_close,
        "vol_regime": "normal",
        "vol_regime_int": 0,
        "asset": row.get("asset"),
        "side": "yes",
        "side_int": 1,
    }


# ── Backfill driver ──────────────────────────────────────────────────


def backfill_db(
    db_path: str,
    *,
    assets: Iterable[str] = ASSETS,
    limit: Optional[int] = None,
    dry_run: bool = False,
) -> dict:
    """Run the cross-asset transfer backfill against db_path.

    Returns a counters dict:
        {
            "candidates": <int>,           # rows matching WHERE blended_prob IS NULL
            "updated":    <int>,           # rows UPDATEd (0 on dry-run)
            "skipped":    {
                "null_raw_prob": <int>,
                "predict_raised": <int>,
                ...
            },
        }

    Idempotent: only touches `WHERE blended_prob IS NULL` rows.
    """
    _refuse_vps_path(db_path)
    assets_tuple = tuple(assets)

    # Build predictor BEFORE touching DB rows — fail loud if the bundle is
    # broken rather than half-process and abort partway.
    predictor = _build_btc_predictor()

    counters = {
        "candidates": 0,
        "updated": 0,
        "skipped": {
            "null_raw_prob": 0,
            "predict_raised": 0,
            "no_features": 0,
        },
    }

    conn = sqlite3.connect(db_path, timeout=10.0)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=10000")
    conn.row_factory = sqlite3.Row

    placeholders = ",".join("?" for _ in assets_tuple)
    sql = (
        f"SELECT ticker, evaluation_time, asset, threshold, strike_cents, "
        f"open_time, close_time, raw_prob, calibrated_prob, blended_prob, "
        f"spot_at_evaluation, sigma_at_evaluation, hour_sin, hour_cos, "
        f"prob_breakeven_gap, sigma_winsorize, result, settlement_value "
        f"FROM {REPLAY_TABLE} "
        f"WHERE blended_prob IS NULL AND asset IN ({placeholders}) "
        f"ORDER BY ticker, evaluation_time"
    )
    if limit:
        sql += f" LIMIT {int(limit)}"
    rows = conn.execute(sql, assets_tuple).fetchall()
    counters["candidates"] = len(rows)

    if dry_run:
        # Categorize without invoking the predictor (faster + matches the
        # "report what would happen" semantics of dry-run).
        for r in rows:
            row_d = dict(r)
            if row_d.get("raw_prob") is None:
                counters["skipped"]["null_raw_prob"] += 1
            elif row_d.get("prob_breakeven_gap") is None:
                # Predict would raise — we project the skip category here.
                counters["skipped"]["predict_raised"] += 1
        conn.close()
        return counters

    batch_writes = 0
    for r in rows:
        row_d = dict(r)
        raw_prob = row_d.get("raw_prob")
        if raw_prob is None:
            counters["skipped"]["null_raw_prob"] += 1
            continue
        try:
            row_features = _build_row_features(row_d)
        except Exception as e:
            logger.warning(
                "Feature build failed for %s @ %s: %s",
                row_d.get("ticker"),
                row_d.get("evaluation_time"),
                e,
            )
            counters["skipped"]["no_features"] += 1
            continue

        try:
            cal_prob, _ens_std, _final_lo, _final_hi = predictor.predict(
                raw_prob=float(raw_prob),
                ticker=str(row_d.get("ticker")),
                side="yes",
                entry_price_cents=ENTRY_PRICE_CENTS_SENTINEL,
                row_features=row_features,
            )
        except Exception as e:
            logger.debug(
                "predict() raised for %s @ %s: %s",
                row_d.get("ticker"),
                row_d.get("evaluation_time"),
                e,
            )
            counters["skipped"]["predict_raised"] += 1
            continue

        conn.execute(
            f"UPDATE {REPLAY_TABLE} SET blended_prob = ? "
            f"WHERE ticker = ? AND evaluation_time = ?",
            (float(cal_prob), row_d["ticker"], row_d["evaluation_time"]),
        )
        counters["updated"] += 1
        batch_writes += 1
        # Per bot/CLAUDE.md SQLite rules: ≤50 rows per commit.
        if batch_writes >= 50:
            conn.commit()
            batch_writes = 0

    if batch_writes:
        conn.commit()
    conn.close()
    return counters


# ── CLI ──────────────────────────────────────────────────────────────


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Bit A (86ba0jmzu) — cross-asset transfer blended_prob "
            "backfill for HYPE/DOGE replay rows via CalMLPPredictor('BTC')."
        )
    )
    parser.add_argument("--db", required=True, help="state.db path (Mac-only)")
    parser.add_argument(
        "--asset",
        choices=ASSETS,
        action="append",
        default=None,
        help="Asset to backfill; pass twice for both. Default: both.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Max rows (smoke mode)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report categorized skip/candidate counts; don't UPDATE.",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        help="Logging level (DEBUG/INFO/WARNING/ERROR)",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(message)s",
    )

    assets = tuple(args.asset) if args.asset else ASSETS
    t0 = time.time()
    counters = backfill_db(
        args.db, assets=assets, limit=args.limit, dry_run=args.dry_run
    )
    elapsed = time.time() - t0
    mode = "DRY-RUN" if args.dry_run else "WROTE"
    logger.info(
        "Bit A backfill %s: candidates=%d updated=%d skipped=%s in %.1fs",
        mode,
        counters["candidates"],
        counters["updated"],
        counters["skipped"],
        elapsed,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
