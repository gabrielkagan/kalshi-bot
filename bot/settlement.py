"""SettlementTracker + discover_active_windows().

Bit 9.2 (Sprint 9) leaf extraction (path-A++, 2026-05-10).

Extracted verbatim from bot/_impl.py:1003-2090 (SettlementTracker class)
and bot/_impl.py:2097-2175 (discover_active_windows module-level function).
The bundling deviates from master plan Phase AA (which originally placed
discover_active_windows with MainLoop in Bit 9.3) — discover_active_windows
is settlement-adjacent in source layout, KalshiClient is already needed
for SettlementTracker, and only true caller is MainLoop._refresh_active_windows.

Re-imported into bot/_impl.py via:
    from bot.settlement import SettlementTracker, discover_active_windows

so MainLoop construction (`self.tracker = SettlementTracker(self.client,
self.state, self.logger, main_loop=self)`) and the single MainLoop call
site `discover_active_windows(self.client)` both resolve via the
bot._impl namespace.

## Path-A++ deviations (NOT byte-for-byte)

1. The 2 SettlementTracker call sites that read the L81-aliased
   underscore-prefixed name (formerly bot/_impl.py:1132 + 1374) are
   rewritten to the public name `append_raw_api_journal` — mirrors the
   bot/executor.py:98 convention from Bit 9.1. The L81 alias-import in
   bot/_impl.py:285 retired atomically with this Bit (zero callers remain).

## Cross-module access patterns (preserved verbatim from bot/_impl.py)

- **Telegram singleton via `_telegram_state` alias** (Bit 8.1 path-A++):
  4 read sites in SettlementTracker reach the singleton via
  `import bot.notifier as _telegram_state` plus the
  `_telegram_state.<NAME>` module-attribute access pattern. Plain
  `from bot.notifier import <NAME>` would capture by value at import time
  and silently freeze at None when `MainLoop.__init__` later mutates the
  singleton (L83). Likewise `from bot import notifier as _telegram_state`
  triggers `_BotProxy.__getattr__` → circular ImportError (L84). The
  canonical form is `import bot.notifier as _telegram_state`.
- **Calibration singleton + helpers via `_cal_state` alias** (Bit 6.3 path-B):
  same module-attribute access pattern, but for the calibration runtime:
  `from bot.engines import calibration as _cal_state` → `_cal_state.<NAME>`.
  Identical mutation-freshness reasoning as the Telegram singleton above.
- **`self._ml.X`** constructor injection: `self._ml = main_loop` is set in
  `__init__`. Sub-attribute reads (`self._ml.scanner._balance_cache`,
  `self._ml.fifteenm_shadow`, `self._ml.hourly_alt_shadow`,
  `self._ml.spx_harrv_shadow`, `self._ml.weather_engine`) are
  constructor-injected, NOT bare-name lookups. Construction order in
  MainLoop.__init__ guarantees the dependencies are populated before any
  SettlementTracker method runs.

## Forbidden top-level imports

- **No torch / sklearn / pandas / numpy / scipy direct imports.**
  Settlement is pure stdlib + sqlite3 + requests-via-KalshiClient. Locked
  by `tests/integration/test_settlement_extraction.py::test_settlement_no_forbidden_numerical_imports`.
- **No `bot._impl` at module top.** SettlementTracker has zero references
  to names defined below the line-117 re-export point in bot/_impl.py;
  no late-binding helper needed and no `.importlinter` carve-out
  (settlement-no-impl-toplevel) added — net contracts stays at 5.

## `discover_active_windows()` cross-checks

After changes to discover_active_windows() or product_type assignments:
grep every `window.get("product_type")` in `bot/scanner/__init__.py::scan()`.
The two sides must stay in sync — a new product_type that scan() doesn't
know about silently drops the window.
"""
from __future__ import annotations

import datetime
import json
import logging
import re
import sqlite3
import threading
import time
from datetime import timezone
from typing import Dict, List, Optional, Set

from bot.constants import (
    HOURLY_OBSERVATION_ENABLED,
    HOURLY_SERIES_TICKERS,
    LOG_RAW_IOC_FILLS,
    LOG_RAW_SETTLEMENTS,
    SERIES_TICKERS,
    SETTLEMENT_CHECK_SECONDS,
    SETTLEMENT_PNL_DIVERGENCE_THRESHOLD_CENTS,
    TM_SWEEP_SHADOW_ENABLED,
    WEATHER_MIN_ENTRY_PRICE,
)
from bot.helpers.raw_api_journal import append_raw_api_journal  # Bit 9.2 path-A++ — public name (L81 alias retired in same commit)
from bot.helpers.strings import dollars_str_to_cents, fp_str_to_int
from bot.kalshi_client import KalshiClient  # type ann on __init__ + discover_active_windows param
from bot.logger import Logger  # type ann on __init__
from bot.state import StateManager  # type ann on __init__

from bot.engines import calibration as _cal_state  # Bit 6.3 path-B alias — _cal_state._CALIBRATION_ENGINE / _cal_state._resolve_cal_engine
import bot.notifier as _telegram_state  # Bit 8.1 path-A++ alias — _telegram_state._TELEGRAM (NOT `from bot import notifier as ...` per L84)

from market_config import get_market_config
from bot.models import calculate_fee, calculate_maker_fee, calculate_taker_fee


class SettlementTracker:
    """Incremental settlement poller. API is the single source of truth.

    Polls GET /portfolio/settlements?min_ts={last_check} every 30 seconds.
    On settlement: look up trade in SQLite, record WIN/LOSS from market_result,
    calculate net P&L from revenue, log to settlement_journal.jsonl, update
    running balance.

    Never uses z-score heuristics or balance deltas to determine outcomes.
    """

    def __init__(self, client: KalshiClient, state: StateManager,
                 logger: Logger, main_loop=None):
        self._client = client
        self._state = state
        self._logger = logger
        self._ml = main_loop
        self._last_check_ts: int = 0
        self._last_poll_time: float = 0.0
        self._last_fallback_sweep: float = 0.0
        self._last_order_cleanup: float = 0.0
        self._processed_tickers: Set[str] = set()
        self._pending_rejection_tickers: Set[str] = set()
        self._settled_rejection_tickers: Set[str] = set()
        # Re-entry guard for tick() worker thread. Prevents thread
        # pile-up if a settlement cycle takes longer than
        # SETTLEMENT_CHECK_SECONDS. Apr 25 00:39 incident: synchronous
        # tick was 4.84-5.54s per cycle; threading the body restores
        # main-loop cadence.
        self._worker_running: bool = False

    # ── Startup ──────────────────────────────────────────────────────────

    def startup(self):
        """Initialize watermark to 24h ago, load dedup set, sweep once."""
        self._last_check_ts = int(
            (datetime.datetime.now(timezone.utc) - datetime.timedelta(hours=24)).timestamp()
        )
        self._load_processed_tickers()
        self._load_pending_rejections()
        self._poll()

    def _load_pending_rejections(self):
        """Load unsettled rejected tickers from DB."""
        rows = self._state.get_unsettled_rejections()
        self._pending_rejection_tickers = {r["ticker"] for r in rows}
        logging.info(
            f"SettlementTracker: loaded {len(self._pending_rejection_tickers)} "
            f"pending rejected opportunities"
        )

    def _load_processed_tickers(self):
        """Load already-settled tickers from DB for deduplication."""
        rows = self._state.conn.execute(
            "SELECT ticker FROM settled_trades"
        ).fetchall()
        self._processed_tickers = {row["ticker"] for row in rows}
        logging.info(
            f"SettlementTracker: loaded {len(self._processed_tickers)} "
            f"previously settled tickers"
        )

    # ── Tick (called every main-loop iteration) ──────────────────────────

    def tick(self):
        """Self-throttled: only polls every SETTLEMENT_CHECK_SECONDS.

        The tick body runs in a daemon worker thread to keep the main
        scan loop unblocked. With 400+ pending evaluated_opportunities
        rows, the synchronous version was 4.84-5.54s per cycle (Apr 25
        00:39 SLOW_SCAN_TICK incident). The `_worker_running` flag
        prevents thread pile-up if a cycle exceeds the throttle.
        """
        now = time.time()
        if now - self._last_poll_time < SETTLEMENT_CHECK_SECONDS:
            return
        if self._worker_running:
            # Previous worker still running; skip this cycle. The
            # next call after _last_poll_time advances will spawn fresh.
            return
        self._last_poll_time = now
        self._worker_running = True

        def _worker():
            try:
                self._poll()
                self._poll_rejections()
                self._poll_evaluated_opportunities()
                # Fallback: sweep for positions stuck past market close (every 5 min)
                _wn = time.time()
                if _wn - self._last_fallback_sweep >= 300.0:
                    self._last_fallback_sweep = _wn
                    self._sweep_stuck_positions()
                # Cleanup expired resting orders (every 60s)
                if _wn - self._last_order_cleanup >= 60.0:
                    self._last_order_cleanup = _wn
                    try:
                        self._state.cleanup_expired_resting_orders()
                    except Exception:
                        logging.debug("cleanup_expired_resting_orders failed",
                                      exc_info=True)
            except Exception:
                logging.error("SettlementTracker worker thread failed",
                              exc_info=True)
            finally:
                self._worker_running = False

        try:
            threading.Thread(
                target=_worker,
                daemon=True,
                name="settlement_tracker",
            ).start()
        except Exception:
            self._worker_running = False
            logging.debug(
                "SettlementTracker worker thread spawn failed",
                exc_info=True)

    # ── Core poll ────────────────────────────────────────────────────────

    def _poll(self):
        """Fetch new settlements from API and process them."""
        unsettled = self._state.get_unsettled_positions()
        if not unsettled:
            return

        resp = self._client.get_settlements(min_ts=self._last_check_ts)
        if LOG_RAW_SETTLEMENTS and resp:
            append_raw_api_journal({
                "kind": "settlements",
                "min_ts": self._last_check_ts,
                "resp": resp,
            })
        if not resp or "settlements" not in resp:
            return

        settlements = resp["settlements"]
        if not settlements:
            return

        our_tickers = {p["ticker"] for p in unsettled}
        processed_any = False

        for s in settlements:
            ticker = s.get("ticker", "")

            # Skip if already processed (dedup)
            if ticker in self._processed_tickers:
                continue

            # Only process settlements for our open positions
            if ticker not in our_tickers:
                continue

            try:
                self._process_settlement(s)
                processed_any = True
            except Exception as e:
                logging.error(f"Settlement processing failed for {ticker}: {e}", exc_info=True)

        # Advance watermark to now (even if nothing processed, to shrink window)
        self._last_check_ts = int(datetime.datetime.now(timezone.utc).timestamp())

        # Refresh balance after processing settlements
        if processed_any:
            balance_resp = self._client.get_balance()
            if balance_resp:
                new_balance = balance_resp.get("balance") or 0
                logging.info(
                    f"Balance after settlements: ${new_balance / 100:.2f}"
                )
            # Invalidate scanner balance cache so next tick's record_balance()
            # gets post-settlement balance. Without this, the 10s cache TTL
            # causes record_balance to record stale pre-settlement balance,
            # compressing drawdown_scaler for one tick. (Learned: 33% of
            # candidates got ds<1.0 from stale cache, Mar 29-30 2026.)
            try:
                if self._ml and hasattr(self._ml, 'scanner'):
                    self._ml.scanner._balance_cache = (None, 0.0)
            except Exception:
                pass

    # ── Fallback sweep for stuck positions ───────────────────────────────

    def _sweep_stuck_positions(self):
        """Detect positions whose market close time has passed and settle via
        individual market lookup.  This catches positions that were skipped by
        the watermark-based settlement poll (e.g. unknown market_result at the
        time, revenue=0 on WIN timing race, etc.).
        """
        import re
        _MONTH_MAP = {"JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
                       "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12}
        unsettled = self._state.get_unsettled_positions()
        if not unsettled:
            return
        now_utc = datetime.datetime.now(timezone.utc).replace(tzinfo=None)
        for pos in unsettled:
            ticker = pos["ticker"]
            if ticker in self._processed_tickers:
                continue
            # Parse close time from 15M ticker format
            m = re.match(r'KX\w+15M-(\d{2})([A-Z]{3})(\d{2})(\d{2})(\d{2})-', ticker)
            if not m:
                continue  # Non-15M — hourly/weather have different settlement paths
            yy, mon, dd, hh, mm = m.groups()
            mon_num = _MONTH_MAP.get(mon)
            if not mon_num:
                continue
            try:
                close_et = datetime.datetime(2000 + int(yy), mon_num, int(dd), int(hh), int(mm))
                close_utc = close_et + datetime.timedelta(hours=4)  # ET→UTC (EDT)
            except (ValueError, OverflowError):
                continue
            # Only sweep if market closed >5 min ago (allow normal settlement path time)
            if now_utc < close_utc + datetime.timedelta(minutes=5):
                continue
            # Fetch market result directly from API
            try:
                mkt = self._client.get_market(ticker)
                if not mkt or "market" not in mkt:
                    continue
                market_data = mkt["market"]
                result = market_data.get("result", "")
                if not result:
                    continue  # Not yet settled on Kalshi
                # Capture expiration_value (CFB RTI settlement price) for divergence analysis
                _exp_val = market_data.get("expiration_value")
                logging.warning(
                    "sweep_stuck_positions: recovering %s (result=%s, "
                    "close_utc=%s, stuck >5min, expiration_value=%s)",
                    ticker, result, close_utc, _exp_val)
                # Build a synthetic settlement dict and process it.
                # _from_sweep=True tells _process_settlement to skip the
                # revenue=0/WIN guard (we compute PnL from first principles).
                settlement = {
                    "ticker": ticker,
                    "market_result": result,
                    "revenue_dollars": None,
                    "revenue": 0,
                    "settled_time": market_data.get("close_time", ""),
                    "_from_sweep": True,
                    "_expiration_value": _exp_val,
                }
                self._process_settlement(settlement)
            except Exception as e:
                logging.error("sweep_stuck_positions failed for %s: %s", ticker, e, exc_info=True)

    # ── Process a single settlement ──────────────────────────────────────

    def _process_settlement(self, settlement: Dict):
        """Record outcome, P&L, and log to journal.

        Handles stacked positions: fetches ALL position rows for the ticker,
        computes per-row PnL from first principles, records each to settled_trades,
        then marks all settled in one UPDATE.
        """
        ticker = settlement["ticker"]
        market_result = settlement.get("market_result", "")
        rev_d = settlement.get("revenue_dollars")
        revenue = dollars_str_to_cents(rev_d) if rev_d else (settlement.get("revenue") or 0)

        # Look up ALL position rows for this ticker
        pos_rows = self._state.conn.execute(
            "SELECT * FROM positions WHERE ticker=?", (ticker,)
        ).fetchall()
        if not pos_rows:
            logging.warning(
                f"SettlementTracker: no position found for {ticker}"
            )
            return

        # B4 (86b9zudcc): capture pre-settlement Kalshi cash balance for
        # the post-settlement WIN-side divergence cross-check (see the
        # SETTLEMENT_PNL_DIVERGENCE block in the Telegram-alert path
        # below). The post-balance fetch in that block, paired with this
        # pre-balance snapshot, gives us the cash motion attributable to
        # this settlement — which on a WIN should equal `aggregate_count
        # × 100¢` and on a LOSS should equal `0`. Failure here is
        # harmless: the cross-check short-circuits on `None` and the
        # alert fires unchanged (per ticket: don't suppress the report).
        _pre_balance_cents: Optional[int] = None
        try:
            _pre_bal_resp = self._client.get_balance()
            if _pre_bal_resp:
                _pre_balance_cents = _pre_bal_resp.get("balance")
        except Exception:
            logging.debug("B4 pre-settlement get_balance failed", exc_info=True)
        positions = [dict(r) for r in pos_rows]
        is_stacked = len(positions) > 1

        # Determine WIN/LOSS from market_result only (API is truth)
        side = positions[0]["side"]
        if market_result == "yes":
            outcome = "WIN" if side == "yes" else "LOSS"
        elif market_result == "no":
            outcome = "WIN" if side == "no" else "LOSS"
        elif market_result == "all_no":
            outcome = "WIN" if side == "no" else "LOSS"
        elif market_result == "all_yes":
            outcome = "WIN" if side == "yes" else "LOSS"
        else:
            outcome = "UNKNOWN"
            logging.critical(
                f"UNKNOWN market_result '{market_result}' for {ticker} "
                f"— skipping settlement to prevent bad P&L recording"
            )
            return

        # Aggregate count across all position rows for cross-checks
        aggregate_count = sum(p["count"] for p in positions)
        aggregate_cost = sum(p["total_cost_cents"] for p in positions)

        # Cross-check: revenue=0 on a WIN is almost certainly a false position.
        # Skip this guard for sweep-recovered settlements — they always have
        # revenue=0 and compute PnL from first principles.
        from_sweep = settlement.get("_from_sweep", False)
        if outcome == "WIN" and revenue == 0 and aggregate_count > 0 and not from_sweep:
            logging.critical(
                f"SETTLEMENT REVENUE ZERO ON WIN {ticker}: "
                f"market_result={market_result} side={side} count={aggregate_count} "
                f"cost={aggregate_cost}¢ fill_source={positions[0].get('fill_source')} — "
                f"Kalshi likely has no matching position. "
                f"Skipping settlement to prevent false -{aggregate_cost}¢ loss.")
            return

        # Cross-check: detect count mismatch between internal tracking
        # and Kalshi settlement.  For YES wins, revenue = real_count * 100.
        if revenue > 0 and outcome == "WIN" and side == "yes":
            implied_count = revenue // 100
            if implied_count != aggregate_count:
                logging.error(
                    f"SETTLEMENT COUNT MISMATCH {ticker}: "
                    f"internal={aggregate_count} kalshi={implied_count} "
                    f"revenue={revenue}¢ n_rows={len(positions)}")
                if implied_count == 0 and aggregate_count > 0:
                    # Sub-dollar revenue (1-99¢) floors to 0 contracts under
                    # `revenue // 100`. Auto-zeroing a confirmed-filled
                    # position on the basis of a sub-dollar revenue value is
                    # almost always wrong: it fabricates a $0 settled_trade
                    # and silently diverges local cost tracking from reality.
                    # Trust the local fill record; alert and fall through to
                    # per-row PnL computed from first principles.
                    # (Learned: KXXRP15M-26APR241200-00 141ct WIN and
                    # KXSOL15M-26APR230200-00 32ct WIN both silently zeroed
                    # Apr 23-24 2026.)
                    logging.critical(
                        f"SETTLEMENT_REVENUE_SUB_DOLLAR {ticker}: "
                        f"kalshi_revenue={revenue}¢ implied_count=0 vs "
                        f"internal={aggregate_count} — REFUSING to auto-zero. "
                        f"Trusting local count; investigate Kalshi payload.")
                    if _telegram_state._TELEGRAM:
                        try:
                            _telegram_state._TELEGRAM.send(
                                f"🚨 SETTLEMENT_REVENUE_SUB_DOLLAR {ticker}: "
                                f"Kalshi revenue={revenue}¢ on "
                                f"{aggregate_count}ct WIN — refused auto-zero. "
                                f"Check journal for payload.")
                        except Exception:
                            pass
                elif len(positions) == 1:
                    # Single row: auto-correct with strategy_group
                    p = positions[0]
                    sg = p.get("strategy_group", "main")
                    corrected_cost = implied_count * p["avg_price_cents"]
                    self._state.conn.execute(
                        "UPDATE positions SET count=?, total_cost_cents=? "
                        "WHERE ticker=? AND strategy_group=?",
                        (implied_count, corrected_cost, ticker, sg))
                    p["count"] = implied_count
                    p["total_cost_cents"] = corrected_cost
                    aggregate_count = implied_count
                    aggregate_cost = corrected_cost
                else:
                    logging.warning(
                        "SETTLEMENT_MULTI_MISMATCH: %s — NOT auto-correcting stacked positions",
                        ticker)

        # Cross-check on LOSSES: revenue=0 gives no count info, so fetch the
        # authoritative fill count from Kalshi's fills API. Catches IOC-path
        # double-count bugs that the WIN-side check can't see.
        # (Learned: XRP 06:15 Apr 19 2026 recorded 208ct local vs 104ct Kalshi
        # -> $99 over-reported loss; 1 of 2 divergent in 30d/48 IOC losses.)
        if outcome == "LOSS" and len(positions) == 1 and positions[0].get("is_taker"):
            try:
                _fresp = self._client.get_fills(ticker=ticker, limit=200)
                if LOG_RAW_IOC_FILLS and _fresp:
                    append_raw_api_journal({
                        "kind": "loss_check_fills",
                        "ticker": ticker,
                        "aggregate_count": aggregate_count,
                        "resp": _fresp,
                    })
                if _fresp and _fresp.get("fills"):
                    _local_order_ids = set()
                    for _r in self._state.conn.execute(
                        "SELECT order_id FROM pending_orders WHERE ticker=?",
                        (ticker,)
                    ).fetchall():
                        _oid = _r["order_id"] if isinstance(_r, sqlite3.Row) else _r[0]
                        if _oid:
                            _local_order_ids.add(_oid)
                    _kalshi_count = 0
                    for _f in _fresp["fills"]:
                        if _f.get("order_id") in _local_order_ids:
                            _c = fp_str_to_int(_f.get("count_fp")) or int(_f.get("count") or 0)
                            _kalshi_count += _c
                    if _kalshi_count > 0 and _kalshi_count != aggregate_count:
                        logging.error(
                            f"SETTLEMENT_LOSS_COUNT_MISMATCH {ticker}: "
                            f"internal={aggregate_count} kalshi={_kalshi_count} "
                            f"(loss-side cross-check) — auto-correcting")
                        p = positions[0]
                        sg = p.get("strategy_group", "main")
                        corrected_cost = _kalshi_count * p["avg_price_cents"]
                        self._state.conn.execute(
                            "UPDATE positions SET count=?, total_cost_cents=? "
                            "WHERE ticker=? AND strategy_group=?",
                            (_kalshi_count, corrected_cost, ticker, sg))
                        p["count"] = _kalshi_count
                        p["total_cost_cents"] = corrected_cost
                        aggregate_count = _kalshi_count
                        aggregate_cost = corrected_cost
            except Exception:
                logging.warning(
                    "Loss-side count cross-check failed for %s", ticker,
                    exc_info=True)

        # Process each position row independently
        combined_pnl = 0
        combined_fee = 0
        now = datetime.datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        for pos in positions:
            row_count = pos["count"]
            row_cost = pos["total_cost_cents"]
            # Revenue from first principles: WIN yes-side → count*100, LOSS → 0
            if outcome == "WIN":
                if side == "yes":
                    row_revenue = row_count * 100
                else:
                    row_revenue = row_count * 100  # NO-side win: paid (100-p), get 100
            else:
                row_revenue = 0
            row_pnl = row_revenue - row_cost
            row_is_taker = bool(pos.get("is_taker"))
            row_fee = calculate_fee(row_count, pos["avg_price_cents"], is_taker=row_is_taker)

            combined_pnl += row_pnl
            combined_fee += row_fee

            # Record each row to settled_trades (revenue_override prevents
            # stacked positions from each getting the full API aggregate revenue)
            self._state.record_settlement(
                settlement, revenue_override=row_revenue,
                pnl_override=row_pnl, fee_override=row_fee, pos=pos)

        # Mark ALL positions for this ticker as settled (once, outside loop)
        self._state.conn.execute("""
            UPDATE positions SET status='settled', updated_at=?
            WHERE ticker=?
        """, (now, ticker))
        self._state.conn.commit()

        # Mark as processed for dedup (once)
        self._processed_tickers.add(ticker)

        # Fetch expiration_value (CFB RTI settlement price) for divergence analysis.
        # For sweep settlements, it's already in the settlement dict.
        # For normal settlements, one extra API call (non-blocking, after PnL recorded).
        _exp_val = settlement.get("_expiration_value")
        if _exp_val is None:
            try:
                _exp_mkt = self._client.get_market(ticker)
                if _exp_mkt:
                    _exp_val = _exp_mkt.get("market", _exp_mkt).get("expiration_value")
            except Exception:
                pass
        if _exp_val is not None:
            logging.info("EXPIRATION_VALUE: %s expiration_value=%s asset=%s",
                         ticker, _exp_val, positions[0]["asset"])

        # Rich journal entry with combined PnL
        self._logger.log_settlement({
            "ticker": ticker,
            "event_ticker": positions[0]["event_ticker"],
            "asset": positions[0]["asset"],
            "outcome": outcome,
            "market_result": market_result,
            "side": side,
            "count": aggregate_count,
            "entry_price_cents": positions[0]["avg_price_cents"],
            "total_cost_cents": aggregate_cost,
            "revenue_cents": revenue,
            "fee_cents": combined_fee,
            "pnl_cents": combined_pnl,
            "pnl_net_cents": combined_pnl - combined_fee,
            "settled_time": settlement.get("settled_time", ""),
            "is_stacked": is_stacked,
            "n_positions": len(positions),
            "expiration_value": _exp_val,
        })

        stacked_tag = f" [STACKED x{len(positions)}]" if is_stacked else ""
        logging.info(
            f"Settlement: {ticker} -> {outcome}{stacked_tag} "
            f"(market_result={market_result}, "
            f"revenue={revenue}¢, cost={aggregate_cost}¢, "
            f"pnl={combined_pnl}¢, fee={combined_fee}¢)"
        )
        if _telegram_state._TELEGRAM:
            emoji = "\u2705" if outcome == "WIN" else "\u274c"
            sign = "+" if combined_pnl >= 0 else ""
            pnl_dollars = combined_pnl / 100
            bal_str = ""
            divergence_tag = ""
            _post_balance_cents: Optional[int] = None
            try:
                bal_resp = self._client.get_balance()
                if bal_resp:
                    _post_balance_cents = bal_resp.get("balance")
                    # Cash + open-position cost-basis matches Kalshi UI's
                    # Portfolio total. Cash alone undercounts when other
                    # positions are still pending settlement.
                    try:
                        _exposure_cents = self._state.get_open_position_exposure_cents()
                    except Exception:
                        _exposure_cents = 0
                    _total_cents = (_post_balance_cents or 0) + _exposure_cents
                    bal_str = f" | Balance: ${_total_cents / 100:.2f}"
            except Exception:
                pass

            # B4 (86b9zudcc): cross-check the cash actually credited at
            # settle against what local books expected. Only `revenue`
            # moves cash at the settle event itself (`cost` + `fee` were
            # debited at fill time, hours/days earlier \u2014 comparing against
            # `combined_pnl \u2212 combined_fee` would diverge by ~cost+fee on
            # every real settlement, defeating the goal). The right
            # comparison is therefore:
            #   - WIN: `aggregate_count \u00d7 100` (yes-side and no-side both
            #     pay 100\u00a2/contract at WIN settlement); LOSS: 0\u00a2 credited.
            # Phantom-inflated local count \u2192 expected_credit overstates the
            # actual balance delta \u2192 divergence > threshold \u2192 log + tag.
            # B4 defense-in-depth note: this surface catches WIN-side
            # phantoms only \u2014 LOSS cash motion is structurally 0 so a LOSS
            # over-count goes undetected here. `scripts/audit/phantom_pnl_audit.py`
            # is the retroactive fills-based complement that covers LOSSes.
            if (_pre_balance_cents is not None
                    and _post_balance_cents is not None):
                _balance_delta_cents = _post_balance_cents - _pre_balance_cents
                _expected_credit_cents = (aggregate_count * 100
                                          if outcome == "WIN" else 0)
                _divergence_cents = _expected_credit_cents - _balance_delta_cents
                if abs(_divergence_cents) > SETTLEMENT_PNL_DIVERGENCE_THRESHOLD_CENTS:
                    logging.warning(
                        "SETTLEMENT_PNL_DIVERGENCE %s: "
                        "expected_credit=%d\u00a2 balance_delta=%d\u00a2 "
                        "divergence=%+d\u00a2 (pre=%d\u00a2 post=%d\u00a2 "
                        "outcome=%s count=%d cost=%d\u00a2 revenue=%d\u00a2 "
                        "fee=%d\u00a2 threshold=%d\u00a2)",
                        ticker, _expected_credit_cents, _balance_delta_cents,
                        _divergence_cents, _pre_balance_cents,
                        _post_balance_cents, outcome, aggregate_count,
                        aggregate_cost, revenue, combined_fee,
                        SETTLEMENT_PNL_DIVERGENCE_THRESHOLD_CENTS,
                    )
                    _delta_sign = "-" if _balance_delta_cents < 0 else "+"
                    _expected_sign = "-" if _expected_credit_cents < 0 else "+"
                    divergence_tag = (
                        f" \u26a0\ufe0fKALSHI_DELTA="
                        f"{_delta_sign}${abs(_balance_delta_cents) / 100:.2f} "
                        f"(expected "
                        f"{_expected_sign}${abs(_expected_credit_cents) / 100:.2f})"
                    )

            _telegram_state._TELEGRAM.send(
                f"{emoji} {outcome} {positions[0]['asset']} {aggregate_count}ct "
                f"@{positions[0]['avg_price_cents']}c {sign}${abs(pnl_dollars):.2f}"
                f"{stacked_tag}{divergence_tag}{bal_str}"
            )

    # ── Rejection Settlement ─────────────────────────────────────────────

    def register_rejection_ticker(self, ticker: str):
        """Called by scanner when a new rejection is recorded."""
        self._pending_rejection_tickers.add(ticker)

    def _poll_rejections(self):
        """Check if any rejected-opportunity tickers have settled."""
        # Refresh from DB to pick up rejections inserted by scanner since last poll
        db_rows = self._state.get_unsettled_rejections()
        for r in db_rows:
            self._pending_rejection_tickers.add(r["ticker"])

        if not self._pending_rejection_tickers:
            return

        # Snapshot to iterate safely
        tickers_to_check = list(
            self._pending_rejection_tickers - self._settled_rejection_tickers
        )
        for ticker in tickers_to_check:
            try:
                resp = self._client.get_market(ticker)
                if not resp:
                    continue
                market = resp.get("market", resp)
                result = market.get("result", "")
                if result:
                    self._process_rejection_settlement(market, ticker)
            except Exception as e:
                logging.warning(
                    f"Rejection settlement check failed for {ticker}: {e}", exc_info=True)

    def _process_rejection_settlement(self, market: Dict, ticker: str):
        """Compute counterfactual P&L for a rejected opportunity that settled."""
        result = market.get("result", "")

        # Look up the rejection row from SQLite
        row = self._state.conn.execute(
            "SELECT * FROM rejected_opportunities WHERE ticker=?", (ticker,)
        ).fetchone()
        if not row:
            return

        entry_price = row["market_price"]
        # If we never had a market price (pre-filter rejection), skip P&L calc
        if entry_price is None:
            would_have_profit = None
            assumed_fee = 0
            counterfactual_outcome = "unknown_no_price"
        else:
            # Counterfactual: bought 1 YES contract at entry_price (include taker fee)
            # Note: rejected_opportunities are always YES-side. NO-side goes through
            # evaluated_opportunities which has its own side-aware settlement in
            # _poll_evaluated_opportunities().
            assumed_fee = calculate_taker_fee(1, int(entry_price))
            if result in ("yes", "all_yes"):
                would_have_profit = (100 - entry_price) - assumed_fee  # cents
                counterfactual_outcome = "would_have_won"
            elif result in ("no", "all_no"):
                would_have_profit = -(entry_price + assumed_fee)  # cents
                counterfactual_outcome = "would_have_lost"
            else:
                would_have_profit = None
                counterfactual_outcome = f"unknown_result_{result}"

        cf_json = json.dumps({
            "outcome": counterfactual_outcome,
            "would_have_profit_cents": would_have_profit,
            "assumed_fee_cents": assumed_fee,
            "entry_price": entry_price,
        })

        self._logger.log_rejection({
            "type": "rejection_settlement",
            "ticker": ticker,
            "event_ticker": row["event_ticker"],
            "asset": row["asset"],
            "rejection_reason": row["rejection_reason"],
            "market_result": result,
            "entry_price_if_traded": entry_price,
            "counterfactual_outcome": counterfactual_outcome,
            "would_have_profit_cents": would_have_profit,
            "assumed_fee_cents": assumed_fee,
            "assumed_contracts": 1,
            "z_score": row["z_score"],
            "spot_price": row["spot_price"],
            "threshold": row["threshold"],
        })

        self._state.mark_rejection_settled(ticker, market_result=result,
                                           counterfactual=cf_json)
        self._settled_rejection_tickers.add(ticker)
        self._pending_rejection_tickers.discard(ticker)

        logging.info(
            f"Rejection settled: {ticker} -> {counterfactual_outcome} "
            f"(result={result}, would_have_profit={would_have_profit}¢)"
        )

    # ── Evaluated Opportunity Settlement ──────────────────────────────────

    @staticmethod
    def _parse_weather_market_date(ticker: str) -> Optional[str]:
        """Extract the market date from a weather ticker as YYYY-MM-DD.

        Ticker format: KXHIGHNY-26FEB28-T50 → date segment '26FEB28' → '2026-02-28'
        """
        parts = ticker.split("-")
        if len(parts) < 2:
            return None
        raw = parts[1]  # e.g. '26FEB28', '26MAR01', '26MAR03'
        if len(raw) < 7:
            return None
        try:
            dt = datetime.datetime.strptime(raw, "%y%b%d")
            return dt.strftime("%Y-%m-%d")
        except ValueError:
            return None

    @staticmethod
    def _estimate_actual_temp_from_bracket(ticker, threshold, spot_price=None):
        """Estimate actual temperature from a settled weather bracket for bias update.

        B-type: midpoint of bracket (threshold is floor_strike, brackets ~2°F wide)
        T-type: threshold ± 2°F based on tail direction (uses spot_price to disambiguate)
        Returns estimated actual temperature or None.
        """
        parts = ticker.split("-")
        if len(parts) < 3:
            return None
        strike_part = parts[-1]
        if strike_part.startswith("B"):
            # Bracket: threshold is floor_strike, bracket ~2°F wide → midpoint
            return threshold + 1.0
        elif strike_part.startswith("T"):
            # Tail: use spot_price (ensemble mean at evaluation time) to determine direction
            if spot_price is not None:
                if threshold < spot_price:
                    return threshold - 2.0  # lower tail: actual below threshold
                else:
                    return threshold + 2.0  # upper tail: actual above threshold
            return None  # can't determine tail direction without spot_price
        return None

    def _poll_evaluated_opportunities(self):
        """Check if any evaluated opportunities have settled for counterfactual tracking.
        Groups rows by ticker to avoid redundant API calls and batches DB commits."""
        try:
            rows = self._state.get_unsettled_evaluated_opportunities()
        except Exception as e:
            logging.warning(f"get_unsettled_evaluated_opportunities failed: {e}", exc_info=True)
            return

        if not rows:
            return

        # Volume warning — high pending count means observation modes are flooding the table
        if len(rows) > 50:
            logging.warning(
                "eval_opp_settlement: %d pending rows (>50 threshold) — "
                "check observation mode volume (weather=%d, hourly=%d, spx=%d, sports=%d, 15m=%d)",
                len(rows),
                sum(1 for r in rows if r.get("product_type") == "weather"),
                sum(1 for r in rows if r.get("product_type") == "hourly"),
                sum(1 for r in rows if r.get("product_type") == "spx_hourly"),
                sum(1 for r in rows if r.get("product_type") == "sports"),
                sum(1 for r in rows if r.get("product_type") == "15m"),
            )

        # Group rows by ticker — one API call per unique ticker
        from collections import defaultdict
        ticker_groups: dict = defaultdict(list)
        for row in rows:
            ticker_groups[row["ticker"]].append(row)

        # Fetch market result once per unique ticker
        ticker_results: dict = {}
        for ticker in ticker_groups:
            try:
                resp = self._client.get_market(ticker)
                if not resp:
                    continue
                market = resp.get("market", resp)
                result = market.get("result", "")
                if result:
                    ticker_results[ticker] = result
            except Exception as e:
                logging.warning(f"get_market failed for {ticker}: {e}")

        if not ticker_results:
            return

        logging.info("eval_opp_settlement: %d unique tickers settled (from %d pending rows)",
                     len(ticker_results), len(rows))

        # ── Phase 1: Compute settlement results in memory (NO DB writes) ──
        # This avoids holding a write lock during the computation + JSONL logging.
        # Each entry: (opp_id, ticker, result, row, would_have_profit, counterfactual_outcome,
        #              count, taker_fee, maker_fee, pnl_taker, pnl_maker)
        _settlement_batch: list = []
        _cal_observations: list = []  # (raw_p, cal_binary, _opp_pt, asset, filter_stage)
        _weather_updates: list = []   # (opp_id, ticker, row) — need API calls, done after commit
        for ticker, result in ticker_results.items():
            for row in ticker_groups[ticker]:
                opp_id = row["id"]
                try:
                    entry_price = row["market_price"]
                    _opp_pt = row.get("product_type")
                    if entry_price is None:
                        would_have_profit = None
                        taker_fee = 0
                        maker_fee = 0
                        pnl_taker = None
                        pnl_maker = None
                        count = row.get("position_size") or 1
                        counterfactual_outcome = "unknown_no_price"
                    elif _opp_pt == "weather" and entry_price < WEATHER_MIN_ENTRY_PRICE:
                        count = row.get("position_size") or 1
                        would_have_profit = 0
                        counterfactual_outcome = "untradeable_price"
                        taker_fee = 0
                        maker_fee = 0
                        pnl_taker = 0
                        pnl_maker = 0
                    else:
                        count = row.get("position_size") or 1
                        taker_fee = calculate_taker_fee(count, int(entry_price))
                        maker_fee = calculate_maker_fee(count, int(entry_price))
                        _opp_side = row.get("side") or "yes"
                        if _opp_side == "no":
                            _is_win = result in ("no", "all_no")
                            _is_loss = result in ("yes", "all_yes")
                        else:
                            _is_win = result in ("yes", "all_yes")
                            _is_loss = result in ("no", "all_no")
                        if _is_win:
                            pnl_taker = (100 - entry_price) * count - taker_fee
                            pnl_maker = (100 - entry_price) * count - maker_fee
                            counterfactual_outcome = "would_have_won"
                        elif _is_loss:
                            pnl_taker = -(entry_price * count + taker_fee)
                            pnl_maker = -(entry_price * count + maker_fee)
                            counterfactual_outcome = "would_have_lost"
                        else:
                            pnl_taker = None
                            pnl_maker = None
                            taker_fee = 0
                            maker_fee = 0
                            counterfactual_outcome = f"unknown_result_{result}"
                        would_have_profit = pnl_taker

                    # JSONL logging (no DB write)
                    self._logger.log_rejection({
                        "type": "evaluated_settlement",
                        "ticker": ticker,
                        "event_ticker": row["event_ticker"],
                        "asset": row["asset"],
                        "filter_stage": row["filter_stage"],
                        "rejection_reason": row.get("rejection_reason"),
                        "market_result": result,
                        "entry_price_if_traded": entry_price,
                        "counterfactual_outcome": counterfactual_outcome,
                        "would_have_profit_cents": would_have_profit,
                        "assumed_contracts": count,
                        "taker_fee_cents": taker_fee,
                        "maker_fee_cents": maker_fee,
                        "pnl_taker_cents": pnl_taker,
                        "pnl_maker_cents": pnl_maker,
                        "calibrated_prob": row.get("calibrated_prob"),
                        "edge": row.get("edge"),
                        "strategy": row.get("strategy"),
                        "position_size": row.get("position_size"),
                        "kelly_f": row.get("kelly_f"),
                        "vol_regime": row.get("vol_regime"),
                        "z_score": row.get("z_score"),
                        "raw_prob": row.get("raw_prob"),
                        "calibration_method": row.get("calibration_method"),
                        "fee_adjusted_edge": row.get("fee_adjusted_edge"),
                        "old_system_prob": row.get("old_system_prob"),
                    })

                    _settlement_batch.append((opp_id, ticker, result, row,
                                              would_have_profit, counterfactual_outcome))

                    # Prepare CalEngine observations
                    raw_p = row.get("raw_prob")
                    filter_stage = row.get("filter_stage", "")
                    _opp_side = row.get("side") or "yes"
                    if (raw_p is not None and _opp_side == "yes"
                            and result in ("yes", "all_yes", "no", "all_no")
                            and not filter_stage.endswith("_v2")):
                        cal_binary = 1 if result in ("yes", "all_yes") else 0
                        _cal_observations.append((raw_p, cal_binary, _opp_pt,
                                                  row.get("asset"), filter_stage,
                                                  row.get("seconds_to_close")))

                    # Queue weather temp fetches for after commit
                    if (_opp_pt == "weather" and result in ("yes", "all_yes", "no", "all_no")
                            and row.get("wx_actual_high_temp") is None):
                        _weather_updates.append((opp_id, ticker, row))

                    logging.info(
                        f"Evaluated opp settled: {ticker} ({row['filter_stage']}) "
                        f"-> {counterfactual_outcome} (profit={would_have_profit}¢)"
                    )
                except Exception as e:
                    logging.warning(f"Evaluated opp settlement check failed for {ticker}: {e}", exc_info=True)

        # ── Phase 2: Fast DB writes (short lock, no API calls) ──
        # Commit in chunks of 50 to keep write-lock duration short.
        # Large batches (200+) hold the lock long enough to deadlock with
        # supabase_sync reader + WAL checkpoint. (Mar 16 2026)
        _SETTLEMENT_BATCH_SIZE = 50
        _settled_count = 0
        if _settlement_batch:
            for _chunk_start in range(0, len(_settlement_batch), _SETTLEMENT_BATCH_SIZE):
                _chunk = _settlement_batch[_chunk_start:_chunk_start + _SETTLEMENT_BATCH_SIZE]
                try:
                    for (opp_id, ticker, result, row,
                         would_have_profit, counterfactual_outcome) in _chunk:
                        self._state.mark_evaluated_opportunity_settled(
                            opp_id, market_result=result,
                            counterfactual_pnl=would_have_profit,
                            commit=False)
                        _settled_count += 1
                    self._state.conn.commit()
                except Exception as e:
                    try:
                        self._state.conn.rollback()
                    except Exception:
                        pass
                    logging.warning("eval_opp_settlement batch commit failed (chunk %d-%d): %s",
                                    _chunk_start, _chunk_start + len(_chunk), e, exc_info=True)
            if _settled_count:
                logging.info("eval_opp_settlement: committed %d rows in %d chunks",
                             _settled_count,
                             (len(_settlement_batch) + _SETTLEMENT_BATCH_SIZE - 1) // _SETTLEMENT_BATCH_SIZE)

        # ── Phase 3: Post-commit work (CalEngine, shadow settlement, weather) ──
        # These run AFTER the write lock is released.

        # Feed CalEngine observations
        for _cal_item in _cal_observations:
            raw_p, cal_binary, _opp_pt, _asset, filter_stage = _cal_item[0], _cal_item[1], _cal_item[2], _cal_item[3], _cal_item[4]
            _cal_stc = _cal_item[5] if len(_cal_item) > 5 else None
            _settle_engine = _cal_state._resolve_cal_engine(_opp_pt, _asset)
            if _settle_engine is not None:
                _settle_engine.add_observation(raw_p, cal_binary, filter_stage=filter_stage,
                                              seconds_to_close=_cal_stc)
            # Dual-feed: 15M per-asset engines AND global engine (keeps shadow pipeline working)
            if _opp_pt in (None, "15m") and _cal_state._CALIBRATION_ENGINE is not None:
                _cal_state._CALIBRATION_ENGINE.add_observation(raw_p, cal_binary, seconds_to_close=_cal_stc)
            elif (_settle_engine is None
                  and filter_stage in ("candidate", "observation_trade",
                                       "hourly_observation", "spx_observation",
                                       "weather_observation")
                  and get_market_config(_opp_pt).cal_eligible):
                if _cal_state._CALIBRATION_ENGINE is not None:
                    _cal_state._CALIBRATION_ENGINE.add_observation(raw_p, cal_binary)

        # Settle shadow signals (per unique settled ticker)
        for ticker, result in ticker_results.items():
            if result not in ("yes", "all_yes", "no", "all_no"):
                continue
            if self._ml and getattr(self._ml, "fifteenm_shadow", None):
                try:
                    self._ml.fifteenm_shadow.settle_signals(ticker, result)
                except Exception:
                    logging.warning("fifteenm_shadow settle failed for %s", ticker, exc_info=True)

            if self._ml and getattr(self._ml, "hourly_alt_shadow", None):
                try:
                    self._ml.hourly_alt_shadow.settle_signals(ticker, result)
                except Exception:
                    logging.warning("hourly_alt_shadow settle failed for %s", ticker, exc_info=True)

            if self._ml and getattr(self._ml, "spx_harrv_shadow", None):
                try:
                    self._ml.spx_harrv_shadow.settle_signals(ticker, result)
                except Exception:
                    logging.warning("spx_harrv_shadow settle failed for %s", ticker, exc_info=True)

            # Settle SOL Path C shadow entry for this ticker
            try:
                _pc_row = self._state.conn.execute(
                    "SELECT * FROM sol_pathc_shadow WHERE ticker=? AND status='pending'",
                    (ticker,)).fetchone()
                if _pc_row:
                    _pc = dict(_pc_row)
                    _is_win = result in ("yes", "all_yes")
                    _live_price = _pc["live_entry_price"]
                    _live_contracts = _pc["live_contracts"]
                    _pos_size = _pc["position_size"]

                    _live_fee = calculate_taker_fee(_live_contracts, _live_price)
                    if _is_win:
                        _live_pnl = (100 - _live_price) * _live_contracts - _live_fee
                    else:
                        _live_pnl = -(_live_price * _live_contracts + _live_fee)

                    _maker_price = _pc["pathc_maker_price"]
                    _maker_depth = _pc["pathc_depth_at_maker"] or 0
                    _maker_touched = _pc["obs_maker_price_touched"] or 0
                    _maker_contracts = min(_pos_size, _maker_depth) if _maker_touched else 0
                    _maker_fee = calculate_maker_fee(_maker_contracts, _maker_price) if _maker_contracts > 0 else 0
                    if _maker_contracts > 0:
                        if _is_win:
                            _maker_pnl = (100 - _maker_price) * _maker_contracts - _maker_fee
                        else:
                            _maker_pnl = -(_maker_price * _maker_contracts + _maker_fee)
                    else:
                        _maker_pnl = 0

                    _esc_ask = _pc["pathc_esc_ask"]
                    _esc_depth = _pc["pathc_esc_depth"] or 0
                    if _esc_ask is not None and _esc_depth > 0:
                        _esc_contracts = min(_pos_size, _esc_depth)
                        _esc_fee = calculate_taker_fee(_esc_contracts, _esc_ask)
                        if _is_win:
                            _esc_pnl = (100 - _esc_ask) * _esc_contracts - _esc_fee
                        else:
                            _esc_pnl = -(_esc_ask * _esc_contracts + _esc_fee)
                    else:
                        _esc_contracts = 0
                        _esc_pnl = 0

                    if _maker_touched and _maker_contracts > 0:
                        _remainder = max(0, _pos_size - _maker_contracts)
                        if _remainder > 0 and _esc_ask is not None and _esc_depth > 0:
                            _rem_contracts = min(_remainder, _esc_depth)
                            _rem_fee = calculate_taker_fee(_rem_contracts, _esc_ask)
                            if _is_win:
                                _rem_pnl = (100 - _esc_ask) * _rem_contracts - _rem_fee
                            else:
                                _rem_pnl = -(_esc_ask * _rem_contracts + _rem_fee)
                        else:
                            _rem_pnl = 0
                        _best_pnl = _maker_pnl + _rem_pnl
                    else:
                        _best_pnl = _esc_pnl

                    self._state.settle_sol_pathc_shadow(
                        ticker=ticker, market_result=result,
                        live_pnl=_live_pnl,
                        pathc_maker_pnl=_maker_pnl,
                        pathc_maker_contracts=_maker_contracts,
                        pathc_esc_pnl=_esc_pnl,
                        pathc_esc_contracts=_esc_contracts,
                        pathc_best_pnl=_best_pnl)
                    logging.info(
                        "sol_pathc_settled: %s result=%s live_pnl=%d maker_pnl=%d esc_pnl=%d best_pnl=%d",
                        ticker, result, _live_pnl, _maker_pnl, _esc_pnl, _best_pnl)
            except Exception:
                logging.warning("sol_pathc_shadow settle failed for %s", ticker, exc_info=True)

        # Settle low_price_shadow_signals
        for ticker, result in ticker_results.items():
            if result not in ("yes", "all_yes", "no", "all_no"):
                continue
            try:
                _lps_rows = self._state.conn.execute(
                    "SELECT id, market_price, full_kelly_contracts, capped_contracts "
                    "FROM low_price_shadow_signals WHERE ticker=? AND status='open'",
                    (ticker,)).fetchall()
                for _lps in _lps_rows:
                    _lps_id = _lps["id"]
                    _lps_price = _lps["market_price"]
                    _is_win = result in ("yes", "all_yes")
                    # Full Kelly PnL
                    _full_ct = _lps["full_kelly_contracts"] or 1
                    _full_fee = calculate_taker_fee(_full_ct, _lps_price)
                    if _is_win:
                        _full_pnl = (100 - _lps_price) * _full_ct - _full_fee
                    else:
                        _full_pnl = -(_lps_price * _full_ct + _full_fee)
                    # Capped Kelly PnL
                    _cap_ct = _lps["capped_contracts"] or 1
                    _cap_fee = calculate_taker_fee(_cap_ct, _lps_price)
                    if _is_win:
                        _cap_pnl = (100 - _lps_price) * _cap_ct - _cap_fee
                    else:
                        _cap_pnl = -(_lps_price * _cap_ct + _cap_fee)
                    self._state.conn.execute(
                        "UPDATE low_price_shadow_signals SET status='settled', "
                        "market_result=?, counterfactual_pnl_full=?, counterfactual_pnl_capped=?, "
                        "settled_at=? WHERE id=?",
                        (result, _full_pnl, _cap_pnl,
                         datetime.datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                         _lps_id))
                if _lps_rows:
                    self._state.conn.commit()
            except Exception:
                logging.warning("low_price_shadow settle failed for %s", ticker, exc_info=True)

        # Settle tm_sweep_shadow rows. Reuses the same ticker_results aggregation
        # as low_price_shadow above. Idempotent — only acts on rows with
        # status='open', so safe if this method is invoked repeatedly.
        if TM_SWEEP_SHADOW_ENABLED:
            for ticker, result in ticker_results.items():
                if result not in ("yes", "all_yes", "no", "all_no"):
                    continue
                try:
                    self._state.update_tm_sweep_shadow_on_settlement(ticker, result)
                except Exception:
                    logging.warning("tm_sweep_shadow settle failed for %s", ticker, exc_info=True)

        # Weather: two-phase to avoid holding the shared-conn writer lock across
        # HTTP latency. Pre-2026-05-21 the inline form (single loop, after-loop
        # commit) cascaded the daily 11:04-11:07 UTC writer-storm — ticket
        # 86ba1xdwp. See kb/decisions/settlement-weather-writer-storm-plan-may21.md.
        _wx_eng = getattr(self._ml, "weather_engine", None) if self._ml else None
        _today = datetime.datetime.now(timezone.utc).strftime("%Y-%m-%d")

        # Phase 3a: HTTP-only — no DB writes, so the shared conn never
        # auto-BEGINs a tx during synchronous fetch_observed_high calls.
        _wx_observations: list = []  # (opp_id, ticker, _wx_city, _market_date, _obs_high, forecast_mean)
        for (opp_id, ticker, row) in _weather_updates:
            try:
                _wx_city = row["asset"].replace("_TEMP", "")
                _market_date = self._parse_weather_market_date(ticker)
                if not _market_date or _market_date >= _today:
                    continue
                if _wx_eng is None:
                    continue
                _obs_high = _wx_eng._fetcher.fetch_observed_high(_wx_city, _market_date)
                if _obs_high is None:
                    continue
                forecast_mean = row.get("spot_price")
                _wx_observations.append(
                    (opp_id, ticker, _wx_city, _market_date, _obs_high, forecast_mean))
            except Exception as e:
                logging.warning("weather_observed_temp fetch failed for %s: %s", ticker, e)

        # Phase 3b: DB-only — per-row commit releases the writer lock before
        # update_bias's separate-conn INSERT, breaking the cascade with
        # market_obs_snapshotter / phantom_reconcile / posthoc / save_bias.
        for (opp_id, ticker, _wx_city, _market_date, _obs_high, forecast_mean) in _wx_observations:
            try:
                self._state.conn.execute(
                    "UPDATE evaluated_opportunities SET wx_actual_high_temp=? WHERE id=?",
                    (_obs_high, opp_id))
                self._state.conn.commit()
                logging.info("weather_observed_temp: %s %s %.1fF",
                             _wx_city, _market_date, _obs_high)
                if forecast_mean:
                    _wx_eng._model.update_bias(
                        _wx_city, _obs_high, forecast_mean,
                        market_date=_market_date)
                    logging.info("weather_bias_update: %s %s actual=%.1fF forecast=%.1fF",
                                 _wx_city, _market_date, _obs_high, forecast_mean)
            except Exception as e:
                logging.warning("weather_observed_temp write failed for %s: %s", ticker, e)

        # Backfill wx_actual_high_temp for settled weather entries that missed it
        self._backfill_weather_actual_temps()

    def _backfill_weather_actual_temps(self):
        """Retry archive API fetch for settled weather entries missing wx_actual_high_temp."""
        try:
            rows = self._state.conn.execute(
                "SELECT id, ticker, asset, spot_price FROM evaluated_opportunities "
                "WHERE product_type='weather' AND status='settled' "
                "AND wx_actual_high_temp IS NULL LIMIT 10"
            ).fetchall()
        except Exception:
            return
        if not rows:
            return
        _today = datetime.datetime.now(timezone.utc).strftime("%Y-%m-%d")
        _wx_eng = getattr(self._ml, "weather_engine", None) if self._ml else None
        if not _wx_eng:
            return
        for r in rows:
            opp_id, ticker, asset, forecast_mean = r
            try:
                _wx_city = asset.replace("_TEMP", "")
                _market_date = self._parse_weather_market_date(ticker)
                if not _market_date or _market_date >= _today:
                    continue
                _obs_high = _wx_eng._fetcher.fetch_observed_high(_wx_city, _market_date)
                if _obs_high is not None:
                    self._state.conn.execute(
                        "UPDATE evaluated_opportunities SET wx_actual_high_temp=? WHERE id=?",
                        (_obs_high, opp_id))
                    self._state.conn.commit()
                    logging.info("weather_backfill_temp: %s %s %.1fF", _wx_city, _market_date, _obs_high)
                    # Bias update with real observed temp
                    if forecast_mean:
                        _wx_eng._model.update_bias(
                            _wx_city, _obs_high, forecast_mean,
                            market_date=_market_date)
            except Exception as e:
                logging.warning("weather_backfill failed for %s: %s", ticker, e)


# ═════════════════════════════════════════════════════════════════════════════
#  Market Discovery
# ═════════════════════════════════════════════════════════════════════════════

def discover_active_windows(client: KalshiClient) -> List[Dict]:
    """
    Query Kalshi for currently open crypto windows (15M + hourly).

    Uses the events endpoint (GET /events) with status=open and
    with_nested_markets=true to find tradeable markets. The markets
    endpoint (GET /markets) with series_ticker only returns pre-created
    'initialized' markets on production, missing the active ones.

    Returns list of dicts with asset, event_ticker, close_time,
    seconds_to_close, markets list, and product_type.
    """
    now = datetime.datetime.now(timezone.utc)
    windows: List[Dict] = []

    # Build combined series list: 15M always, hourly when enabled
    series_list = [(a, s, "15m") for a, s in SERIES_TICKERS.items()]
    if HOURLY_OBSERVATION_ENABLED:
        series_list += [(a, s, "hourly") for a, s in HOURLY_SERIES_TICKERS.items()]

    for asset, series, product_type in series_list:
        result = client.get_events(
            series_ticker=series,
            status="open",
            with_nested_markets=True,
            limit=100,
        )
        events = result.get("events") if result else None
        if not events:
            logging.warning(
                f"Market discovery: {asset} ({series}) — API returned no data"
            )
            continue
        market_count = 0

        for event in events:
            event_ticker = event.get("event_ticker", "")
            nested_markets = event.get("markets", [])
            if not isinstance(nested_markets, list):
                continue

            # Filter to actual market dicts (not string references)
            mkts = [m for m in nested_markets if isinstance(m, dict)]
            if not mkts:
                continue

            market_count += len(mkts)

            close_time_str = mkts[0].get("close_time", "")
            try:
                close_time = datetime.datetime.fromisoformat(
                    close_time_str.replace("Z", "+00:00")
                )
            except (ValueError, AttributeError):
                continue

            seconds_to_close = (close_time - now).total_seconds()
            if seconds_to_close < 0:
                continue
            windows.append({
                "asset": asset,
                "event_ticker": event_ticker,
                "close_time": close_time,
                "seconds_to_close": seconds_to_close,
                "markets": mkts,
                "product_type": product_type,
            })

        if market_count == 0:
            logging.info(
                f"Market discovery: {asset} ({series}) — 0 open markets"
            )
        else:
            logging.info(
                f"Market discovery: {asset} ({series}) — "
                f"{market_count} markets in {len(events)} windows"
            )

    return windows
