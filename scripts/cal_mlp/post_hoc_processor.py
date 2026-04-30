"""R-p7-deploy-r9 post-hoc cal_mlp processor.

Runs in a daemon thread. Polls evaluated_opportunities for 15M rows that
were stamped with cal_mlp_request_id but haven't been calibrated yet,
reconstructs the v1 feature set from DB columns, runs predict(), and
UPDATEs the row.

Why post-hoc instead of inline scan-tick predict:
- Edit 4 ran feature-gathering AND predict-submit synchronously on every
  15M scan iteration. Even with predict() async, the feature gathering
  (compute_derived_features, _extended_feature_provider, dict construction,
  uuid generation) cost ~5-10ms × 4 markets per scan tick, contributing to
  SCAN_LOOP_SLOW events and ~50% throughput reduction during quiet periods.
- All v1 features (CONT_FEATURE_COLS) are derivable from DB columns:
  market_price, seconds_to_close, spot_distance_to_strike_sigma,
  prob_breakeven_gap (already DB cols); abs_spot_distance and
  time_decayed_proximity (derive from distance + STC); hour_sin/cos
  (analytical from evaluation_time).
- Edit 4 reduces to: `_shadow_diag['cal_mlp_request_id'] = uuid.uuid4().hex`.
  Negligible scan-thread cost.
- Post-hoc processor reads rows in batches, predicts, UPDATEs. Annotation
  lag: 5-15s after row INSERT (poll cadence + batch size). Acceptable for
  shadow audit data.

Trade-offs accepted:
- 5-15s annotation lag (vs. ~50ms with v1.5 inline async). Fine for shadow.
- Polling overhead (one SELECT every poll interval). Bounded by batch_size
  + WHERE clause that uses indexed columns.
- Re-running predict if a row's UPSERT-overwrite changes its uuid mid-poll:
  the new uuid lives on the new row, the old uuid disappears. No corruption.
"""
from __future__ import annotations

import logging
import math
import sqlite3
import threading
import time
from datetime import datetime, timezone, timedelta
from typing import Optional

logger = logging.getLogger('cal_mlp')


class CalMLPPostHocProcessor:
    """Daemon-thread polling processor for cal_mlp annotations.

    Lifecycle: start() at bot boot AFTER predictors are warmed; stop() at
    shutdown BEFORE state.db close. Honors CALMLP_ENABLED env at predict
    time (re-checked each row, so hot env flip is observed within one poll).
    """

    def __init__(
        self,
        db_path: str,
        predictors: dict,           # asset → CalMLPPredictor
        poll_interval_sec: float = 10.0,
        batch_size: int = 50,
        recent_window_sec: int = 300,  # only process rows ≤ 5min old
    ):
        self.db_path = db_path
        self.predictors = predictors
        self.poll_interval_sec = float(poll_interval_sec)
        self.batch_size = int(batch_size)
        self.recent_window_sec = int(recent_window_sec)
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._metrics = {
            'polls': 0,
            'rows_seen': 0,
            'rows_updated': 0,
            'predict_failed': 0,
            'no_predictor': 0,
            'missing_db_features': 0,
            'env_disabled': 0,
            'db_locked_skips': 0,
        }
        self._metrics_lock = threading.Lock()
        self._last_metrics_log_ts = 0.0

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            logger.warning("[CALMLP_POSTHOC] start called but thread already alive")
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run, name='cal_mlp_posthoc', daemon=True,
        )
        self._thread.start()
        logger.info(
            "[CALMLP_POSTHOC] started: poll_interval=%.1fs batch_size=%d "
            "recent_window=%ds",
            self.poll_interval_sec, self.batch_size, self.recent_window_sec,
        )

    def stop(self, timeout_sec: float = 10.0) -> None:
        """Signal stop + join. Bot's _cleanup() should call this BEFORE
        state.close() so a final tick can flush in-flight work."""
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout_sec)
            if self._thread.is_alive():
                logger.warning(
                    "[CALMLP_POSTHOC] stop timeout — thread still alive after %.1fs",
                    timeout_sec,
                )
        with self._metrics_lock:
            logger.info("[CALMLP_POSTHOC] stopped; metrics=%s", dict(self._metrics))

    def _run(self) -> None:
        """Main loop. Daemon thread; exits when _stop_event is set."""
        # Open ONE long-lived connection. WAL + busy_timeout per CLAUDE.md;
        # check_same_thread=False so any thread can close it on shutdown.
        try:
            conn = sqlite3.connect(
                self.db_path, timeout=2.0, isolation_level=None,
                check_same_thread=False,
            )
            conn.execute("PRAGMA journal_mode=WAL")
            # CLAUDE.md mandates busy_timeout=10000 for all sqlite3.connect()
            # call sites that share state.db.
            conn.execute("PRAGMA busy_timeout=10000")
        except Exception as e:
            logger.error("[CALMLP_POSTHOC] failed to open DB conn: %s", e)
            return

        try:
            while not self._stop_event.wait(self.poll_interval_sec):
                try:
                    self._process_one_batch(conn)
                    self._maybe_log_metrics()
                except Exception as e:
                    logger.warning(
                        "[CALMLP_POSTHOC] tick raised: %s", e, exc_info=True,
                    )
        finally:
            try:
                conn.close()
            except Exception:
                pass

    def _maybe_log_metrics(self) -> None:
        now = time.time()
        if now - self._last_metrics_log_ts >= 60.0:
            self._last_metrics_log_ts = now
            with self._metrics_lock:
                logger.info("[CALMLP_POSTHOC] metrics %s", dict(self._metrics))

    def _process_one_batch(self, conn: sqlite3.Connection) -> None:
        """Select up to batch_size unannotated 15M rows, process each."""
        import os
        if os.environ.get('CALMLP_ENABLED', '1').strip().lower() not in ('1', 'true', 'yes'):
            with self._metrics_lock:
                self._metrics['env_disabled'] += 1
                self._metrics['polls'] += 1
            return

        with self._metrics_lock:
            self._metrics['polls'] += 1

        # evaluation_time is ISO-8601 with 'Z' suffix; sqlite compares as text.
        cutoff_iso = (
            datetime.now(timezone.utc) - timedelta(seconds=self.recent_window_sec)
        ).isoformat().replace('+00:00', 'Z')

        try:
            rows = conn.execute(
                """SELECT id, ticker, asset, side, market_price, seconds_to_close,
                          spot_distance_to_strike_sigma, prob_breakeven_gap,
                          vol_regime, raw_prob, evaluation_time, cal_mlp_request_id
                   FROM evaluated_opportunities
                   WHERE product_type = '15m'
                     AND cal_mlp_request_id IS NOT NULL
                     AND cal_mlp_p_mean IS NULL
                     AND cal_mlp_skipped_reason IS NULL
                     AND evaluation_time > ?
                   LIMIT ?""",
                (cutoff_iso, self.batch_size),
            ).fetchall()
        except sqlite3.OperationalError as e:
            with self._metrics_lock:
                self._metrics['db_locked_skips'] += 1
            logger.debug("[CALMLP_POSTHOC] SELECT lock skip: %s", e)
            return

        with self._metrics_lock:
            self._metrics['rows_seen'] += len(rows)

        for row in rows:
            self._process_row(conn, row)

    def _process_row(self, conn: sqlite3.Connection, row: tuple) -> None:
        """Build features from DB columns, run predict, UPDATE row.

        Schema-tolerant: missing required columns → stamp 'missing_features'.
        Runtime errors → stamp 'predict_runtime'.
        """
        try:
            (row_id, ticker, asset, side, market_price, seconds_to_close,
             spot_dist_sigma, prob_breakeven_gap, vol_regime, raw_prob,
             evaluation_time, request_id) = row
        except ValueError:
            return  # row tuple shape changed underneath us; skip

        # Required for predict()
        if (raw_prob is None or market_price is None
                or seconds_to_close is None):
            self._stamp_skip(conn, request_id, 'missing_features')
            return

        predictor = self.predictors.get(asset)
        if predictor is None:
            self._stamp_skip(conn, request_id, 'no_predictor')
            return

        # Reconstruct v1 row_features from DB columns.
        try:
            dt = datetime.fromisoformat(evaluation_time.replace('Z', '+00:00'))
        except Exception:
            dt = datetime.now(timezone.utc)
        # R-p7-deploy-r9 Round-1#1 CRITICAL: training data used integer
        # `hour_of_day_utc` (24 discrete values; bot.py:1434 `now_utc.hour`)
        # then computed sin/cos. hour_sin/cos are identity_no_zscore (raw
        # passthrough — no normalization), so a continuous hour at predict
        # time produces minute-level coordinates the model never trained on.
        # Match training: use integer hour only.
        hour = float(dt.hour)

        # R-p7-deploy-r11 R3 CRITICAL: clip DB-loaded sigma to match the
        # train-time winsorize. extract_data clips at ±SIGMA_WINSOR_ABS_CAP
        # before training; without the matching clip here, model trained on
        # ±25 sees ±3,337 in prod (terminal-STC blowup). All three derived
        # values (abs, tdp, the row_features sigma key itself) MUST come
        # from the clipped value.
        from features import apply_sigma_winsor
        spot_dist_sigma = apply_sigma_winsor(spot_dist_sigma)

        if spot_dist_sigma is not None:
            decay = max(0.0, min(1.0, 1.0 - seconds_to_close / 900.0))
            tdp = spot_dist_sigma * decay
            abs_dist = abs(spot_dist_sigma)
        else:
            tdp = None
            abs_dist = None

        # numpy.digitize requires numpy; we already have it via integration.
        import numpy as np
        row_features = {
            'price_tier': int(np.digitize(market_price, [80, 90, 96], right=True)),
            'stc_bucket': int(np.digitize(seconds_to_close, [120, 300, 600], right=True)),
            'vol_regime_int': 1 if vol_regime == 'elevated' else 0,
            'vol_regime': vol_regime or 'normal',
            'spot_distance_to_strike_sigma': spot_dist_sigma,
            'abs_spot_distance_to_strike_sigma': abs_dist,
            'time_decayed_proximity': tdp,
            'prob_breakeven_gap': prob_breakeven_gap,
            'hour_sin': math.sin(2.0 * math.pi * hour / 24.0),
            'hour_cos': math.cos(2.0 * math.pi * hour / 24.0),
            'seconds_to_close': seconds_to_close,
        }

        # Run predict via the existing annotate function (handles all error
        # codes + builds the diag dict in canonical form).
        from integration import annotate_evaluation_kwargs
        diag: dict = {}
        try:
            annotate_evaluation_kwargs(
                diag, raw_prob=raw_prob, ticker=ticker,
                side=side or 'yes', entry_price_cents=market_price,
                row_features=row_features, predictor=predictor,
            )
        except Exception as e:
            logger.warning(
                "[CALMLP_POSTHOC] predict raised for row_id=%s: %s",
                row_id, e, exc_info=True,
            )
            self._stamp_skip(conn, request_id, 'predict_runtime')
            return

        # UPDATE row. Single-row WHERE on cal_mlp_request_id; explicit txn
        # so a concurrent UPSERT can't write an inconsistent partial state.
        try:
            conn.execute("BEGIN IMMEDIATE")
            cur = conn.execute(
                """UPDATE evaluated_opportunities
                   SET cal_mlp_p_mean=?, cal_mlp_p_std=?,
                       cal_mlp_final_lo=?, cal_mlp_final_hi=?,
                       cal_mlp_train_id=?, cal_mlp_skipped_reason=?
                   WHERE cal_mlp_request_id=?""",
                (diag.get('cal_mlp_p_mean'), diag.get('cal_mlp_p_std'),
                 diag.get('cal_mlp_final_lo'), diag.get('cal_mlp_final_hi'),
                 diag.get('cal_mlp_train_id'), diag.get('cal_mlp_skipped_reason'),
                 request_id),
            )
            conn.execute("COMMIT")
            with self._metrics_lock:
                self._metrics['rows_updated'] += cur.rowcount
                if diag.get('cal_mlp_skipped_reason'):
                    self._metrics['predict_failed'] += 1
        except sqlite3.OperationalError as e:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.OperationalError:
                pass
            with self._metrics_lock:
                self._metrics['db_locked_skips'] += 1
            logger.debug(
                "[CALMLP_POSTHOC] UPDATE lock skip row_id=%s: %s", row_id, e,
            )

    def _stamp_skip(
        self,
        conn: sqlite3.Connection,
        request_id: str,
        reason: str,
    ) -> None:
        """Set cal_mlp_skipped_reason on the row so it isn't re-processed."""
        with self._metrics_lock:
            self._metrics[reason] = self._metrics.get(reason, 0) + 1
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                """UPDATE evaluated_opportunities
                   SET cal_mlp_skipped_reason=?
                   WHERE cal_mlp_request_id=?""",
                (reason, request_id),
            )
            conn.execute("COMMIT")
        except sqlite3.OperationalError:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.OperationalError:
                pass
