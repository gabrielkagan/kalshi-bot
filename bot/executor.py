"""Bit 9.1: OrderExecutor extracted from bot/_impl.py to bot/executor.py.

Maker-first executor with adaptive taker escalation. Always enters via a
maker limit order (1-2¢ below fair value). tick() polls for fills and, if
unfilled, escalates to a taker order after an urgency-based wait window.
See class docstring at OrderExecutor.__doc__ for behavioral spec.

Cross-class coupling preserved via:
  - `_telegram_state._TELEGRAM` module-attribute access (Bit 8.1 path-A++
    pattern; 19 read sites in OrderExecutor class body). Canonical form
    is `import bot.notifier as _telegram_state` (L84) — `from bot import
    notifier as _telegram_state` would trigger _BotProxy.__getattr__ →
    circular ImportError.
  - **Bit 86b9vpp2z (2026-05-11) RETIRED** the `_get_opportunity_scanner()`
    method-body cycle-break helper. The 10 call sites previously routed
    through it (`_convert_orderbook_fp` + `_best_yes_ask_cents` accesses
    on OpportunityScanner) now use direct top-level imports from
    `bot/helpers/orderbook.py` where the underlying utilities live:
    `from bot.helpers.orderbook import best_yes_ask_cents, convert_orderbook_fp`
    near the top of this file. The bot.executor ↔ bot.scanner cycle is
    now broken structurally (zero back-edges). OpportunityScanner retains
    the staticmethods as 1-line delegates only so the ~20 test sites using
    `OpportunityScanner._X(...)` access pattern keep working unchanged.

Path-A++ relocations applied in this Bit:
  - `_append_raw_api_journal` → `bot/helpers/raw_api_journal.py`
    (this module reads via the explicit-import line below using the
    public name `append_raw_api_journal`; no late-binding helper, no
    `executor-no-impl-toplevel` `.importlinter` carve-out.)
    Bit 9.2 (2026-05-10) RETIRED the parallel L81 alias-import in
    bot/_impl.py — the SettlementTracker callers moved to bot/settlement.py
    with the class and now use the public name directly. bot/_impl.py
    has zero `_append_raw_api_journal` callers post-Bit-9.2.

Bug fix (closes ticket 86b9vn9r5): 4 sites in OrderExecutor body that
called `OpportunityScanner._best_ask_depth(...)` (latent AttributeError —
`_best_ask_depth` is a staticmethod on OrderExecutor itself, NOT on
OpportunityScanner) rewritten to `OrderExecutor._best_ask_depth(...)`.

Sister cleanup atomic in same commit:
  - `_get_order_executor()` helper retired from bot/scanner/__init__.py
  - `scanner-no-impl-toplevel` `.importlinter` contract dropped
  - 3 peer-pin tests in tests/contracts/test_import_linter_contracts.py
    dropped + `EXPECTED_CONTRACTS` shrunk to 5 entries
"""
from __future__ import annotations

import datetime
import heapq
import json
import logging
import math
import time
import uuid
from collections import deque
from datetime import timezone
from typing import Dict, List, Optional, Tuple

import bot.notifier as _telegram_state  # Bit 8.1 path-A++ alias — L84 form (explicit submodule import bypasses _BotProxy.__getattr__; the `from bot import notifier as _telegram_state` form would trigger a circular import)
import bot.constants  # Bit 9.3-iii.c kill-switch fix (2026-05-11): WEATHER_NO_SIDE_LIVE / HOURLY_NO_SIDE_LIVE accessed via module-attribute (`bot.constants.X`) rather than explicit-name imports — preserves mutation freshness so the scanner's auto-kill writes take effect on the next execute() call.

from bot.constants import (
    ADDON_ENABLED, ADDON_MAX_ENTRY_PRICE, ADDON_MIN_PRICE_IMPROVEMENT, ADDON_MIN_SECONDS_SINCE_FILL,
    ADDON_MIN_STC_REMAINING, ADDON_SIZE_FRACTION, BRACKET_NO_ASSUMED_PROB, BRACKET_NO_YES_MAX,
    BNB_MIN_ENTRY_PRICE,
    BRACKET_NO_YES_MIN, BTC_ESCALATION_WAIT_OVERRIDE, BTC_MIN_ENTRY_PRICE, DC_IOC_MAX_RETRIES,
    DC_IOC_RETRY_DELAY, DC_PRICE_TOLERANCE_MAX, DC_PRICE_TOLERANCE_START_RETRY, DECIDED_CONTRACT_MIN_PRICE,
    DECIDED_CONTRACT_T2_MAX_PRICE, DIP_ADDON_ENABLED, DIP_ADDON_MAX_TOTAL_RISK, DIP_ADDON_MIN_DROP_CENTS,
    DIP_ADDON_MIN_ENTRY_PRICE, DIP_ADDON_MIN_SECONDS_SINCE_FILL, DIP_ADDON_MIN_STC_REMAINING, DIP_ADDON_SHADOW_FLOOR,
    DOGE_MIN_ENTRY_PRICE,
    DIP_ADDON_SHADOW_MODE, DIP_ADDON_SIZE_FRACTION, DIRECT_TAKER_THRESHOLD, EARLY_ESCALATION_MIN_MOVE,
    ESCALATION_MAX_ENTRY, ESCALATION_WAIT_LONG, ESCALATION_WAIT_MEDIUM, ESCALATION_WAIT_SHORT,
    ETH_MIN_ENTRY_PRICE, FILL_MODEL_JOURNAL, HOURLY_FIXED_CONTRACTS, HOURLY_MAX_ENTRY_PRICE,
    HOURLY_MIN_EDGE_PCT, HOURLY_NO_FIXED_CONTRACTS, HOURLY_TAKER_ONLY, HYPE_MIN_ENTRY_PRICE,
    IOC_DRIFT_CHECK_COLD_START_RATIO, IOC_DRIFT_CHECK_DIVERGENCE_RATIO, IOC_DRIFT_CHECK_ENABLED, IOC_DRIFT_CHECK_MIN_CACHED_DEPTH,
    IOC_DRIFT_CHECK_REST_WINDOW_S, IOC_LIMIT_MAX_BUMP_CENTS, IOC_MIN_COUNT_AFTER_CLAMP, IOC_RETRY_OFFSET,
    IOC_TICKER_COOLDOWN, LADDER_ESCALATION_ELIGIBLE_STRATEGIES, LADDER_ESCALATION_ENABLED, LADDER_ESCALATION_MIN_REMAINDER,
    LADDER_ESCALATION_OFFSET, LOG_RAW_IOC_FILLS, LPNE_FIXED_CONTRACTS, LPNE_MAX_PRICE,
    LPNE_MIN_PRICE, MAKER_ONLY_THRESHOLD, MAKER_POLL_INTERVAL, MAKER_PRICE_OFFSET,
    MAKER_TAIL_AFTER_IOC_PARTIAL, MAKER_TAIL_ELIGIBLE_STRATEGIES, MAKER_TAIL_MAX_GLOBAL, MAKER_TAIL_MAX_PER_ASSET,
    MAKER_TAIL_MIN_REMAINDER, MAKER_TAIL_MIN_STC_SECONDS, MAKER_TAIL_TTL_SECONDS, MAKER_TIMEOUT_SECONDS,
    MAX_ENTRY_PRICE, MAX_TICKER_RISK, MAX_WINDOW_RISK, MIN_EDGE_PCT,
    MIN_ENTRY_PRICE, MIN_ORDER_SUBMIT_STC_S, MIN_SECONDS_BEFORE_CLOSE, NBBO_FALLBACK_GATES,
    OBSERVATION_MODE, POST_ONLY_DEGRADED_EXTRA_OFFSET, POST_ONLY_MAX_SAME_PRICE, POST_ONLY_REJECTION_EXPIRY,
    SOL_EMPTY_BOOK_MAKER_MIN_PRICE, SOL_EMPTY_BOOK_MIN_STC, SOL_MIN_ENTRY_PRICE, SOL_TAKER_FIRST,
    STACKING_ENABLED, STRATEGY_CLAMP_DEFAULT, STRATEGY_CLAMP_POLICY, STRATEGY_LIMIT_BUMP_DEFAULT_RESERVE,
    STRATEGY_LIMIT_BUMP_RESERVE_CENTS, TAKER_FIRST_ASSETS, TM_LIVE_STRATEGIES, TM_PRICE_SET,
    TM_SWEEP_CAPTURE_TIERS, TM_SWEEP_LIVE_ENABLED, TM_SWEEP_SHADOW_ENABLED,
    XRP_MIN_ENTRY_PRICE,
)
from bot.helpers.raw_api_journal import append_raw_api_journal  # Bit 9.1 path-A++ relocation — public name in leaf module
from bot.helpers.strings import dollars_str_to_cents, fp_str_to_int
from bot.helpers.tm_sweep import tm_compute_contracts, tm_sweep_extract_depths
from bot.helpers.orderbook import best_yes_ask_cents, convert_orderbook_fp  # Bit 86b9vpp2z (2026-05-11): orderbook utilities relocated from OpportunityScanner staticmethods to bot/helpers/orderbook.py. This direct top-level import RETIRES the `_get_opportunity_scanner()` cycle-break helper that previously existed in this module — OrderExecutor no longer needs a runtime back-edge to bot.scanner just to access the pure-utility orderbook functions.
from bot.kalshi_client import KalshiClient
from bot.trading_mode import asset_from_ticker as tm_asset_from_ticker, is_live as tm_is_live, mode_reason as tm_mode_reason, strategy_is_live as tm_strategy_is_live  # modular live/shadow gate
from bot.engines.probability import ProbabilityEngine
from bot.logger import Logger
from bot.state import StateManager
from market_config import get_market_config
from bot.models import calculate_fee, calculate_taker_fee, strategy_to_group


# Bit 86b9vpp2z (2026-05-11) RETIRED `_get_opportunity_scanner()` cycle-break
# helper. Was: a single-name late-binding helper returning OpportunityScanner
# so the 7 staticmethod call sites in OrderExecutor body could access
# `_convert_orderbook_fp` and `_best_yes_ask_cents` without a top-level import.
# Post-Bit: those two staticmethods are pure module-level functions in
# bot/helpers/orderbook.py and OrderExecutor imports them directly above. The
# bot.executor ↔ bot.scanner cycle is now broken structurally (no back-edge);
# OpportunityScanner retains the staticmethods as 1-line delegates only for
# the ~20 test sites that use `OpportunityScanner._X(...)` access pattern.


class OrderExecutor:
    """Maker-first executor with adaptive taker escalation.

    Always enters via a maker limit order (1-2¢ below fair value).
    tick() polls for fills and, if unfilled, escalates to a taker order
    after an urgency-based wait window:
      - 60-300s to close → wait 15s
      - 30-60s  to close → wait 10s
      - <30s    to close → wait 5s

    On escalation: cancel maker, re-fetch orderbook, validate price
    is in [MIN_ENTRY_PRICE, ESCALATION_MAX_ENTRY], and submit taker.

    UUID client_order_id, persist to SQLite before submission,
    log fills to trade_journal.jsonl, record position on fill.
    """

    def __init__(self, client: KalshiClient, state: StateManager,
                 logger: Logger, main_loop=None, kalshi_feed=None):
        self._client = client
        self._state = state
        self._logger = logger
        self._ml = main_loop
        self._kalshi_feed = kalshi_feed
        self._active_orders: Dict[str, Dict] = {}  # asset → order dict
        # Maker-tail tracking: order_id → record. Each record:
        #   {asset, ticker, strategy, count, price_cents,
        #    posted_monotonic, expires_monotonic}
        # Populated by _maybe_post_maker_tail, swept by
        # _sweep_maker_tails (called from tick()).
        self._maker_tails: Dict[str, Dict] = {}
        self._session_maker_tails_posted: int = 0
        self._session_maker_tails_skipped_cap: int = 0
        self._session_maker_tails_cancelled_ttl: int = 0
        # Ladder-escalation session counter. Mirrors maker-tail.
        # Increments on attempt (not on fill) — pair with success-rate
        # by comparing to settlement-level ladder fill counts.
        self._session_ladder_escalations: int = 0
        self._recent_taker_tickers: Dict[str, float] = {}  # ticker → timestamp (cooldown after IOC)
        # Session counters for execution engine stats
        self._session_amend_attempts: int = 0
        self._session_amend_successes: int = 0
        self._session_ioc_fills: int = 0
        self._session_ioc_unfilled: int = 0
        self._session_ws_fills: int = 0
        self._session_rest_fills: int = 0
        self._session_post_only_rejections: int = 0
        # Post-only rejection → taker escalation tracking
        self._post_only_rejections: Dict[str, Tuple[int, float]] = {}  # ticker → (count, first_rejection_ts)
        # Per-ticker API error cap: stop hammering after 3 consecutive api_errors
        self._ticker_api_errors: Dict[str, int] = {}  # ticker → consecutive error count
        self.TICKER_API_ERROR_CAP = 3
        # Rolling buffer of recent REST best-ask depth observations
        # per ticker, used to smooth the IOC drift-check clamp. Each
        # entry is (ts, depth); samples older than
        # IOC_DRIFT_CHECK_REST_WINDOW_S are pruned at observation time.
        # The clamp authority is `max(depths in window)` rather than
        # a single REST sample — see _rest_best_ask_depth_smoothed.
        self._rest_depth_observations: Dict[str, deque] = {}
        self._session_post_only_degraded_attempts: int = 0
        self._session_post_only_taker_escalations: int = 0
        self._session_post_only_taker_fills: int = 0
        # Direct taker counters (for <60s candidates)
        self._session_direct_taker_attempts: int = 0
        self._session_direct_taker_fills: int = 0
        self._session_direct_taker_unfilled: int = 0
        self._session_direct_taker_skipped: int = 0
        # Confirmation addon state
        self._addon_eligible: Dict[str, Dict] = {}   # ticker → metadata
        self._addon_completed: set = set()            # tickers already addon'd
        self._session_addon_attempts: int = 0
        self._session_addon_fills: int = 0
        self._session_addon_unfilled: int = 0
        self._session_addon_skipped: int = 0
        # Dip addon state
        self._dip_addon_completed: set = set()         # tickers already dip-addon'd
        self._session_dip_addon_attempts: int = 0
        self._session_dip_addon_fills: int = 0
        self._session_dip_addon_shadow: int = 0
        self._session_dip_addon_skipped: int = 0
        self._escalating_assets: set = set()  # Fix 5: guard against re-entry during escalation
        # Order suppression tracking — every gate logs when it blocks
        self._session_suppressed_asset_lock: int = 0
        self._session_suppressed_ticker_cooldown: int = 0
        self._session_suppressed_no_asks: int = 0
        self._session_nbbo_fallback_attempts: int = 0
        self._session_nbbo_fallback_blocked: int = 0
        self._session_suppressed_edge_recalc: int = 0
        self._session_suppressed_zero_size: int = 0
        # SOL empty-book maker fallback counters
        self._session_sol_empty_maker_attempt: int = 0
        self._session_sol_empty_maker_skip_price: int = 0
        self._session_sol_empty_maker_skip_stc: int = 0
        self._session_ioc_retries: int = 0
        self._session_ioc_retry_fills: int = 0
        self._kalshi_oft = None  # populated from scanner if available
        # SOL Path C shadow: pending observations {ticker → dict}
        self._sol_pathc_pending: Dict[str, Dict] = {}
        # DC IOC retry queue: non-blocking retries between scan cycles
        # Each entry: {candidate, original_count, total_filled, remaining, attempt, next_retry_ts, strategy}
        self._dc_retry_queue: List[Dict] = []
        self._session_dc_retries: int = 0
        self._session_dc_retry_fills: int = 0
        # Cancel-404 session counter. Explicit init removes the
        # attribute-missing race the prior `getattr(...)` lazy pattern
        # in `_handle_cancel_404` carried (`+= 1` is still non-atomic
        # under any future multi-thread refactor — explicit init
        # narrows the surface, doesn't make the counter thread-safe).
        # See kb/decisions/cancel-404-fix-v2-design-may04.md
        # "Counter initialization".
        self._cancel_404_count: int = 0

    @property
    def _active_order(self) -> Optional[Dict]:
        """Backwards compat for bot/snapshots/dashboard_snapshot.py."""
        if not self._active_orders:
            return None
        return next(iter(self._active_orders.values()))

    @property
    def has_active_order(self) -> bool:
        return len(self._active_orders) > 0

    # ── Post-only rejection tracking ────────────────────────────────────

    def _get_post_only_rejection_count(self, ticker: str) -> int:
        """Get active rejection count for ticker. Returns 0 if expired or missing."""
        entry = self._post_only_rejections.get(ticker)
        if entry is None:
            return 0
        count, first_ts = entry
        if time.time() - first_ts > POST_ONLY_REJECTION_EXPIRY:
            self._post_only_rejections.pop(ticker, None)
            return 0
        return count

    def _record_post_only_rejection(self, ticker: str):
        """Increment rejection count for ticker. Starts fresh if expired."""
        now = time.time()
        entry = self._post_only_rejections.get(ticker)
        if entry is None or (now - entry[1] > POST_ONLY_REJECTION_EXPIRY):
            self._post_only_rejections[ticker] = (1, now)
        else:
            self._post_only_rejections[ticker] = (entry[0] + 1, entry[1])

    # ── Pre-submit settlement-race gate ─────────────────────────────────

    def _should_skip_near_close(self, candidate: Dict) -> bool:
        """Return True when candidate STC is too close to settlement
        for a submission to land cleanly. None / non-numeric STC →
        return False (no info, allow submit — this path is shared with
        weather/sports where seconds_to_close may be unset)."""
        stc = candidate.get("seconds_to_close")
        try:
            stc_f = float(stc)
        except (TypeError, ValueError):
            return False
        return stc_f < MIN_ORDER_SUBMIT_STC_S

    def _abort_near_close(self, candidate: Dict, path: str) -> None:
        """Record the skip in evaluated_opportunities + log so the
        forensic trail makes the abort discoverable (a missing
        place_order would otherwise look like 'we never tried')."""
        ticker = candidate.get("ticker", "?")
        stc = candidate.get("seconds_to_close")
        logging.warning(
            "ORDER_ABORT_NEAR_CLOSE: %s path=%s stc=%s "
            "threshold=%.1fs (skip to avoid 409/404 race)",
            ticker, path, stc, MIN_ORDER_SUBMIT_STC_S)
        try:
            self._state.update_evaluated_opportunity_order(
                ticker, order_outcome="skipped_near_close")
        except Exception:
            logging.warning(
                "update_evaluated_opportunity_order(skipped_near_close) "
                "failed for %s", ticker, exc_info=True)

    # ── Sprint B Bit B.2b — order-decision snapshot emission ──────────
    # ONE row per maker-vs-taker route decision. Called from every
    # branch in execute() and from _escalate_to_taker_inner. The
    # decision_id is seeded on the candidate dict by execute() (or by
    # the first emit if absent — defensive) and reused by any
    # subsequent escalation row so a learner can join the two events.
    #
    # Best-effort: any exception is logged and swallowed — capture
    # failure must NEVER break order flow.

    def _emit_decision_snapshot(self, candidate: Dict,
                                decision_type: str) -> None:
        """Capture the route decision moment for future training data.

        Pulls or seeds candidate['decision_id'] (UUID4 hex). For
        escalations the SAME decision_id is reused (caller passes the
        original candidate dict carried on the order record).

        Auto-fills orderbook_levels_json from the StateManager's
        freshness-gated _scan_ob_cache (stale → NULL, never lie).
        Mirrors insert_order_lifecycle_snapshot's auto-fill contract.
        """
        try:
            decision_id = candidate.get("decision_id")
            if not decision_id:
                decision_id = uuid.uuid4().hex
                candidate["decision_id"] = decision_id
            self._state.insert_decision_snapshot(
                decision_id=decision_id,
                ticker=candidate.get("ticker", ""),
                asset=candidate.get("asset", ""),
                decision_type=decision_type,
                spot_price=candidate.get("spot_price"),
                seconds_to_close=candidate.get("seconds_to_close"),
                vol_regime=candidate.get("vol_regime"),
                source=candidate.get("strategy"),
            )
        except Exception:
            logging.warning(
                "_emit_decision_snapshot failed for %s decision_type=%s",
                candidate.get("ticker"), decision_type, exc_info=True)

    # ── Hourly taker-only execution ─────────────────────────────────────

    def _execute_hourly_taker(self, candidate: Dict) -> Optional[Dict]:
        """Hourly-only IOC execution. No per-asset lock, no maker, no escalation.

        Completely isolated from 15M execution path:
        - Does NOT write to _active_orders (no maker resting)
        - Does NOT write to _escalating_assets (no escalation)
        - Calls _submit_taker() directly → IOC resolves in <1s
        """
        ticker = candidate["ticker"]
        asset = candidate["asset"]
        best_ask = candidate["best_yes_ask"]
        count = min(candidate["position_size"], HOURLY_FIXED_CONTRACTS)  # Hard cap

        # Ticker cooldown (shared with all products — IOC-specific, safe)
        cooldown_ts = self._recent_taker_tickers.get(ticker)
        if cooldown_ts is not None:
            _cd_remaining = IOC_TICKER_COOLDOWN - (time.time() - cooldown_ts)
            if _cd_remaining > 0:
                logging.info("HOURLY_TAKER: %s cooldown %.0fs remaining", ticker, _cd_remaining)
                return None

        candidate["entry_path"] = "hourly_taker"
        # Sprint B Bit B.2b — decision snapshot at route choice.
        self._emit_decision_snapshot(candidate, "taker_first")

        # Apply ask+1c offset for fill certainty (same pattern as SOL taker-first).
        # At sub-60c, 1c worse entry is trivial vs the 20c+ per-trade edge.
        # Verify edge is still positive after the offset before submitting.
        _h_mcfg = get_market_config("hourly")
        ioc_price = min(best_ask + IOC_RETRY_OFFSET, HOURLY_MAX_ENTRY_PRICE)
        if ioc_price != best_ask:
            cal_prob = candidate.get("calibrated_prob", 0)
            _offset_fee = calculate_fee(HOURLY_FIXED_CONTRACTS, ioc_price, is_taker=True,
                                        fee_mult_taker=_h_mcfg.fee_multiplier_taker,
                                        fee_mult_maker=_h_mcfg.fee_multiplier_maker)
            _offset_edge = cal_prob - (ioc_price / 100.0) - (_offset_fee / (HOURLY_FIXED_CONTRACTS * 100.0))
            if _offset_edge >= HOURLY_MIN_EDGE_PCT / 100.0:
                candidate["best_yes_ask"] = ioc_price
                best_ask = ioc_price
            else:
                logging.info("HOURLY_TAKER: %s offset %d→%dc kills edge (%.4f < %.4f), using ask",
                             ticker, best_ask, ioc_price, _offset_edge, HOURLY_MIN_EDGE_PCT / 100.0)

        logging.info("HOURLY_TAKER: %s %dx@%dc edge=%.2f%% prob=%.1f%% stc=%.0fs",
                     ticker, count, best_ask,
                     candidate.get("fee_adjusted_edge", 0) * 100,
                     candidate.get("calibrated_prob", 0) * 100,
                     candidate.get("seconds_to_close", 0))

        if _telegram_state._TELEGRAM:
            _telegram_state._TELEGRAM.send(
                f"HOURLY: {asset} {count}x@{best_ask}c "
                f"edge={candidate.get('fee_adjusted_edge', 0):.2%} "
                f"stc={candidate.get('seconds_to_close', 0):.0f}s")

        result = self._submit_taker(candidate)
        if result is None:
            self._recent_taker_tickers[ticker] = time.time()
        return result

    def _execute_weather_no_taker(self, candidate: Dict) -> Optional[Dict]:
        """Weather NO-side IOC execution. Direct taker, no maker, no escalation.

        Weather NO books are structurally empty — resting NO asks at 30-40c don't
        exist. Maker-first always cancels. Small fixed sizing (WEATHER_NO_CONTRACT_COUNT)
        at ~39-40c keeps taker fee negligible vs the 30%+ assumed-prob edge.
        """
        ticker = candidate["ticker"]
        asset = candidate["asset"]
        best_ask = candidate["best_yes_ask"]  # NO price for NO-side
        count = candidate["position_size"]     # WEATHER_NO_CONTRACT_COUNT (gated, ex-LAS)

        # Ticker cooldown (shared with all products)
        cooldown_ts = self._recent_taker_tickers.get(ticker)
        if cooldown_ts is not None:
            _cd_remaining = IOC_TICKER_COOLDOWN - (time.time() - cooldown_ts)
            if _cd_remaining > 0:
                logging.info("WEATHER_NO_TAKER: %s cooldown %.0fs remaining", ticker, _cd_remaining)
                return None

        candidate["entry_path"] = "weather_no_taker"
        # Sprint B Bit B.2b — decision snapshot at route choice.
        self._emit_decision_snapshot(candidate, "taker_first")

        logging.info(
            "WEATHER_NO_TAKER: %s %dx@%dc edge=%.2f%% prob=%.0f%% stc=%.0fs",
            ticker, count, best_ask,
            candidate.get("fee_adjusted_edge", 0) * 100,
            candidate.get("calibrated_prob", 0) * 100,
            candidate.get("seconds_to_close", 0))

        if _telegram_state._TELEGRAM:
            _telegram_state._TELEGRAM.send(
                f"\u2601\ufe0f WX NO: {asset} {count}x@{best_ask}c "
                f"edge={candidate.get('fee_adjusted_edge', 0):.2%} "
                f"stc={candidate.get('seconds_to_close', 0) / 3600:.0f}h")

        result = self._submit_taker(candidate)
        if result is None:
            self._recent_taker_tickers[ticker] = time.time()
        return result

    def _execute_hourly_no_taker(self, candidate: Dict) -> Optional[Dict]:
        """Hourly NO-side IOC execution. 1-contract verification mode.

        Mirrors weather NO taker — direct IOC, no maker, no escalation.
        Completely isolated from 15M and hourly YES execution paths.
        """
        ticker = candidate["ticker"]
        asset = candidate["asset"]
        best_ask = candidate["best_yes_ask"]  # NO price for NO-side
        count = min(candidate["position_size"], HOURLY_NO_FIXED_CONTRACTS)

        # Ticker cooldown
        cooldown_ts = self._recent_taker_tickers.get(ticker)
        if cooldown_ts is not None:
            _cd_remaining = IOC_TICKER_COOLDOWN - (time.time() - cooldown_ts)
            if _cd_remaining > 0:
                logging.info("HOURLY_NO_TAKER: %s cooldown %.0fs remaining", ticker, _cd_remaining)
                return None

        candidate["entry_path"] = "hourly_no_taker"
        # Sprint B Bit B.2b — decision snapshot at route choice.
        self._emit_decision_snapshot(candidate, "taker_first")

        logging.info(
            "HOURLY_NO_TAKER: %s %s %dx@%dc edge=%.2f%% no_prob=%.1f%% stc=%.0fs",
            ticker, asset, count, best_ask,
            candidate.get("fee_adjusted_edge", 0) * 100,
            candidate.get("calibrated_prob", 0) * 100,
            candidate.get("seconds_to_close", 0))

        if _telegram_state._TELEGRAM:
            _telegram_state._TELEGRAM.send(
                f"HOURLY NO: {asset} {count}x@{best_ask}c "
                f"edge={candidate.get('fee_adjusted_edge', 0):.2%} "
                f"stc={candidate.get('seconds_to_close', 0):.0f}s")

        result = self._submit_taker(candidate)
        if result is None:
            self._recent_taker_tickers[ticker] = time.time()
        return result

    # ── Public interface ──────────────────────────────────────────────────

    @staticmethod
    def _existing_window_cost_for_timeslot(positions: List[Dict],
                                           timeslot: str) -> int:
        """Sum total_cost_cents across positions whose event_ticker shares
        `timeslot`. Defends against a transiently-None event_ticker (the
        race documented at `_window_timeslot`'s `WINDOW_TIMESLOT_NULL`
        warning and in `kb/failures/transient-none-event-ticker-may08.md`).

        Tick error 2026-05-08 14:44:36 fired here when an inline genexpr
        chained `.split(...)` directly off `p.get("event_ticker", "")` —
        `dict.get` returns the value (None) when the key is present, so
        the default-empty-string never coerced None.

        total_cost_cents is also coerced defensively: schema is INTEGER
        NOT NULL, but the same race that surfaced None event_ticker can
        plausibly surface other transiently-None columns. Skip the row
        rather than TypeError on `total += None`.
        """
        total = 0
        for p in positions:
            et = p.get("event_ticker")
            if not et:
                logging.warning(
                    "WINDOW_CAP_NULL_EVENT_TICKER: ticker=%s asset=%s "
                    "side=%s status=%s cost=%s",
                    p.get("ticker"), p.get("asset"), p.get("side"),
                    p.get("status"), p.get("total_cost_cents"))
                continue
            if et.split("-", 1)[-1] == timeslot:
                cost = p.get("total_cost_cents")
                if cost is None:
                    logging.warning(
                        "WINDOW_CAP_NULL_TOTAL_COST: ticker=%s event_ticker=%s",
                        p.get("ticker"), et)
                    continue
                total += cost
        return total

    def _execute_longshot_maker(self, candidate: Dict) -> Optional[Dict]:
        """Longshot premium-harvest maker placement (Bit L-1).

        Reached ONLY from execute() — i.e. strictly BELOW the trading-mode
        gate (bot/trading_mode.py), which stays the single live/shadow
        chokepoint (plus the kalshi_client.place_order backstop). Posts a
        post-only limit BUY of the candidate's buy side at 100-ask (= a
        maker SELL of the deep-OTM side), then hands lifecycle (T-3min
        cancel, condition-flip cancel, fill polling) to the LongshotEngine
        registered on MainLoop. No taker escalation, no maker tail, no
        per-asset lock. See bot/longshot.py +
        kb/decisions/longshot-twap-live-small-plan.md.
        """
        ticker = candidate["ticker"]
        engine = getattr(self._ml, "longshot_engine", None) if self._ml else None
        if engine is None:
            logging.warning(
                "LONGSHOT_SKIP_no_engine: %s — candidate reached execute() "
                "without a LongshotEngine on MainLoop", ticker)
            return None
        # Live-read kill switch (mirrors the engine-side check — a constants
        # flip between scan and execute must stop placement).
        if not bot.constants.LONGSHOT_ENABLED:
            logging.info("LONGSHOT_SKIP_disabled: %s", ticker)
            return None
        if OBSERVATION_MODE:
            logging.info(
                "OBSERVATION MODE: Would post longshot maker for %s at %dc "
                "for %d contracts", ticker,
                candidate.get("longshot_buy_price_cents", -1),
                candidate.get("position_size", 0))
            return None
        # R1-C2 stopgap (durable composite-PK rebuild ticketed 86badbf9t):
        # positions PK is (ticker), so a longshot fill on a ticker the main
        # pipeline also trades would INSERT OR REPLACE the main row (and
        # vice versa). Never quote a ticker with main-pipeline order flow
        # in flight: a resting main maker (_active_orders) or any pending
        # non-longshot order on the ticker blocks placement. Longshot's own
        # orders are recognized by the ls- client_oid prefix. Fail-closed
        # on query failure (skipping a quote is always safe).
        _ls_main_conflict = any(
            (o or {}).get("ticker") == ticker
            for o in self._active_orders.values())
        if not _ls_main_conflict:
            try:
                _ls_main_conflict = any(
                    not (ro.get("client_order_id") or "").startswith(
                        bot.constants.LONGSHOT_CLIENT_OID_PREFIX)
                    for ro in self._state.get_resting_orders(ticker))
            except Exception:
                logging.warning("longshot pending-order conflict query "
                                "failed for %s", ticker, exc_info=True)
                _ls_main_conflict = True
        if _ls_main_conflict:
            logging.info(
                "LONGSHOT_SKIP_main_conflict: %s has main-pipeline order "
                "flow in flight (ticker-PK stopgap, 86badbf9t)", ticker)
            return None
        # Execute-time cap re-check (scan->execute race: a sister fill may
        # have consumed the per-window-side or collateral cap).
        count = engine.authorize(candidate)
        if count <= 0:
            logging.info("LONGSHOT_SKIP_authorize: %s caps consumed", ticker)
            return None
        buy_side = candidate["longshot_buy_side"]
        price = int(candidate["longshot_buy_price_cents"])
        # R1-M1: prefix marks the order as longshot's for boot orphan
        # reconciliation (LongshotEngine._boot_reconcile_orphans) and the
        # per-strategy live-gate recognition in kalshi_client.place_order.
        client_oid = (bot.constants.LONGSHOT_CLIENT_OID_PREFIX
                      + str(uuid.uuid4()))
        # Persist BEFORE submission (order-ledger crash-safety contract,
        # mirrors the maker-first path / ticket 86ba0jb1g).
        self._state.insert_bot_order(
            client_oid, ticker, candidate["event_ticker"],
            candidate["asset"], buy_side, count, price, False)
        _price_kwarg = {"no_price": price} if buy_side == "no" else {"yes_price": price}
        resp = self._client.place_order(
            ticker=ticker, side=buy_side, action="buy",
            count=count, client_order_id=client_oid,
            post_only=True, **_price_kwarg,
        )
        if resp is None:
            self._state.mark_order_status(client_oid, "api_error")
            logging.warning(
                "LONGSHOT_MAKER_REJECTED: %s %s %dct @ %dc (post_only)",
                ticker, buy_side, count, price)
            return None
        order_id = (resp.get("order") or {}).get("order_id")
        if not order_id:
            # R2-MN3: non-None response with an empty/missing order dict —
            # we cannot key the cancel/fill lifecycle on an id Kalshi never
            # acknowledged (the old client_oid fallback registered a
            # phantom quote). Do NOT register; mark the ledger row off the
            # placeable path and best-effort cancel via the client_oid
            # (Kalshi cancel accepts it if the order somehow rested).
            logging.warning(
                "LONGSHOT_PLACE_MALFORMED: %s resp carried no order_id "
                "(order=%r) — not registering; best-effort cancel via "
                "client_oid %s", ticker, resp.get("order"), client_oid)
            self._state.mark_order_status(client_oid, "api_error")
            try:
                self._client.cancel_order(client_oid, ticker=ticker)
            except Exception:
                logging.warning(
                    "LONGSHOT_PLACE_MALFORMED cancel attempt failed for %s",
                    client_oid, exc_info=True)
            return None
        self._state.confirm_order_submitted(client_oid, order_id)
        engine.register_resting(
            order_id=order_id, client_order_id=client_oid, ticker=ticker,
            event_ticker=candidate["event_ticker"], asset=candidate["asset"],
            sell_side=candidate["longshot_sell_side"], buy_side=buy_side,
            buy_price_cents=price, count=count,
            seconds_to_close=candidate["seconds_to_close"])
        logging.info(
            "LONGSHOT_MAKER_POSTED: %s sell_%s@%dc -> buy_%s %dct @ %dc "
            "order=%s stc=%.0fs",
            ticker, candidate["longshot_sell_side"],
            candidate.get("longshot_ask_cents", -1), buy_side, count, price,
            order_id, candidate["seconds_to_close"])
        return {
            "order_id": order_id,
            "client_order_id": client_oid,
            "ticker": ticker,
            "event_ticker": candidate["event_ticker"],
            "asset": candidate["asset"],
            "side": buy_side,
            "price_cents": price,
            "count": count,
            "is_taker": False,
            "strategy": "longshot",
            "entry_path": "longshot_maker",
        }

    def _execute_twaplock_taker(self, candidate: Dict) -> Optional[Dict]:
        """TWAP-lock endgame taker placement (Bit T-1).

        Reached ONLY from execute() — strictly BELOW the trading-mode gate
        (bot/trading_mode.py), which stays the single live/shadow
        chokepoint (plus the kalshi_client.place_order backstop). Submits
        an IOC BUY of the locked side at the executable ask, reads the
        fill synchronously from the order response (FP-primary:
        ``fill_count_fp`` via fp_str_to_int, legacy ``fill_count``
        fallback — the L-1 R7 lesson applied from day 1), records the
        position at the LIMIT price (conservative — actual fills are
        <= limit; startup positions-API reconcile corrects prices, same
        posture as the ghost-fill Layer A register) and holds to
        settlement. NO resting lifecycle: an IOC never rests, so there is
        no registry, no cancel sweep, no fill polling thread. The
        post-fill record path (record_position_from_fill +
        mark_order_status) has NO retry-on-busy by design (R2-MN3
        advisory): it matches the pre-existing synchronous-taker
        posture, and a transient DB failure there self-heals via the
        engine's boot sweep + the startup positions-API reconcile. See
        bot/twaplock.py + kb/decisions/longshot-twap-live-small-plan.md.
        """
        ticker = candidate["ticker"]
        engine = getattr(self._ml, "twaplock_engine", None) if self._ml else None
        if engine is None:
            logging.warning(
                "TWAPLOCK_SKIP_no_engine: %s — candidate reached execute() "
                "without a TwaplockEngine on MainLoop", ticker)
            return None
        # Live-read kill switch (mirrors the engine-side check — a constants
        # flip between scan and execute must stop placement).
        if not bot.constants.TWAPLOCK_ENABLED:
            logging.info("TWAPLOCK_SKIP_disabled: %s", ticker)
            return None
        if OBSERVATION_MODE:
            logging.info(
                "OBSERVATION MODE: Would IOC twaplock %s %s at %dc for %d "
                "contracts", ticker, candidate.get("side"),
                candidate.get("twaplock_ask_cents", -1),
                candidate.get("position_size", 0))
            return None
        # Execute-time re-check (scan->execute race): one-shot latch +
        # cross-strategy ticker exclusion (ANY open position or ANY
        # pending/resting order row — ticker-PK stopgap, 86badbf9t) +
        # combined live-small disable rails, all inside authorize().
        count = engine.authorize(candidate)
        if count <= 0:
            logging.info("TWAPLOCK_SKIP_authorize: %s blocked", ticker)
            return None
        side = candidate["side"]
        price = int(candidate["twaplock_ask_cents"])
        # tw- prefix marks the order as twaplock's for the state.py
        # reconciler carve-outs + the per-strategy live-gate recognition
        # in kalshi_client.place_order (mirrors longshot's ls-).
        client_oid = (bot.constants.TWAPLOCK_CLIENT_OID_PREFIX
                      + str(uuid.uuid4()))
        # Consume the one-shot BEFORE the API call: a failed/ambiguous
        # placement still spends the window's shot (frequency loss is
        # cheap; a hot retry loop into a settling market is not).
        engine.register_entry(ticker)
        # Persist BEFORE submission (order-ledger crash-safety contract,
        # mirrors the maker-first path / ticket 86ba0jb1g).
        self._state.insert_bot_order(
            client_oid, ticker, candidate["event_ticker"],
            candidate["asset"], side, count, price, True)
        _price_kwarg = {"no_price": price} if side == "no" else {"yes_price": price}
        resp = self._client.place_order(
            ticker=ticker, side=side, action="buy",
            count=count, client_order_id=client_oid,
            time_in_force="immediate_or_cancel", **_price_kwarg,
        )
        if resp is None:
            try:
                engine.record_api_error()
            except Exception:
                logging.warning(
                    "twaplock record_api_error failed", exc_info=True)
            self._state.mark_order_status(client_oid, "api_error")
            logging.warning(
                "TWAPLOCK_IOC_REJECTED: %s %s %dct @ %dc (api error)",
                ticker, side, count, price)
            return None
        order = resp.get("order") or {}
        order_id = order.get("order_id")
        if not order_id:
            # Malformed response — no id to key anything on. An IOC never
            # rests, so no cancel attempt is needed; if a fill happened
            # invisibly, the startup positions-API reconcile imports it
            # (tw- pending history stamps strategy_group='twaplock').
            logging.warning(
                "TWAPLOCK_PLACE_MALFORMED: %s resp carried no order_id "
                "(order=%r) — ledger row marked api_error",
                ticker, resp.get("order"))
            try:
                engine.record_api_error()
            except Exception:
                logging.warning(
                    "twaplock record_api_error failed", exc_info=True)
            self._state.mark_order_status(client_oid, "api_error")
            return None
        self._state.confirm_order_submitted(client_oid, order_id)
        try:
            engine.record_api_ok()
        except Exception:
            logging.debug("twaplock record_api_ok failed", exc_info=True)
        # FP-primary fill read from the synchronous IOC response. A
        # malformed count field (R1-MN5) DEGRADES to the 0-fill path
        # below (row canceled, no position) — it must never raise past
        # confirm_order_submitted, which would strand the row 'resting'
        # and crash the scan tick. The money side of a fill hidden by a
        # garbage count is owned by the startup positions-API reconcile
        # (same posture as the no-order_id branch above).
        try:
            filled = fp_str_to_int(order.get("fill_count_fp")) or (
                order.get("fill_count") or 0)
            filled = min(int(filled), count)
        except (TypeError, ValueError, OverflowError):
            logging.warning(
                "TWAPLOCK_FILL_PARSE_MALFORMED: %s order=%s unparseable "
                "fill count (fill_count_fp=%r fill_count=%r) — treating "
                "as 0-fill; positions-API reconcile owns any hidden fill",
                ticker, order_id, order.get("fill_count_fp"),
                order.get("fill_count"))
            filled = 0
        if filled <= 0:
            self._state.mark_order_status(order_id, "canceled")
            logging.info(
                "TWAPLOCK_IOC_NO_FILL: %s %s %dct @ %dc — auto-canceled "
                "unfilled (one-shot consumed)", ticker, side, count, price)
            return None
        self._state.record_position_from_fill(
            ticker=ticker,
            event_ticker=candidate["event_ticker"],
            asset=candidate["asset"],
            side=side,
            count=filled,
            price_cents=price,
            strategy="twaplock",  # passes through strategy_to_group unchanged
            seconds_to_close=candidate.get("seconds_to_close"),
            calibrated_prob=candidate.get("calibrated_prob"),
            edge=candidate.get("edge"),
            is_taker=True,
            fill_source="twaplock_taker",
            execution_method="ioc",
        )
        self._state.mark_order_status(order_id, "filled")
        logging.info(
            "TWAPLOCK_IOC_FILLED: %s buy_%s %d/%dct @ %dc p_lock=%s "
            "order=%s stc=%.0fs", ticker, side, filled, count, price,
            candidate.get("twaplock_p_lock"), order_id,
            candidate.get("seconds_to_close") or -1)
        return {
            "order_id": order_id,
            "client_order_id": client_oid,
            "ticker": ticker,
            "event_ticker": candidate["event_ticker"],
            "asset": candidate["asset"],
            "side": side,
            "price_cents": price,
            "count": filled,
            "is_taker": True,
            "strategy": "twaplock",
            "entry_path": "twaplock_taker",
        }

    def execute(self, candidate: Dict) -> Optional[Dict]:
        """Always submit maker order. Escalation to taker happens in tick()."""
        # Sprint B Bit B.2b — seed decision_id ONCE at the top of
        # execute(). Each downstream branch's _emit_decision_snapshot
        # call reuses this id; if a maker_first later escalates,
        # _escalate_to_taker_inner reads candidate['decision_id'] off
        # the order dict it forked from and writes the 'escalate' row
        # with the SAME decision_id — a learner joining on decision_id
        # reconstructs the full route sequence.
        if not candidate.get("decision_id"):
            candidate["decision_id"] = uuid.uuid4().hex
        # Observation safety belt — should never reach here for obs-only types
        # Exceptions:
        #   - weather NO-side bypasses observation_only when WEATHER_NO_SIDE_LIVE=True
        #   - hourly NO-side bypasses observation_only when HOURLY_NO_SIDE_LIVE=True
        #     (NO-side verification runs independently of the YES-side kill switch)
        _exec_cfg = get_market_config(candidate.get("product_type"))
        if _exec_cfg.observation_only:
            _is_weather_no_live = (candidate.get("product_type") == "weather"
                                  and candidate.get("side") == "no"
                                  and bot.constants.WEATHER_NO_SIDE_LIVE)
            _is_hourly_no_live = (candidate.get("product_type") == "hourly"
                                 and candidate.get("side") == "no"
                                 and bot.constants.HOURLY_NO_SIDE_LIVE)
            if not (_is_weather_no_live or _is_hourly_no_live):
                logging.error("SAFETY: %s candidate reached execute() — should never happen. Ticker=%s",
                              _exec_cfg.product_type, candidate.get("ticker"))
                return None

        asset = candidate["asset"]
        ticker = candidate["ticker"]

        # ── Trading-mode gate (modular global + per-asset live/shadow) ─
        # Scoped by the 15M-crypto TICKER (KX<ASSET>15M-…) via asset_from_ticker —
        # the SAME scoping the kalshi_client.place_order backstop uses, so the two
        # chokepoints never disagree. This deliberately keys on the ticker, NOT
        # candidate["asset"], because HOURLY crypto reuses the same asset keys
        # (HOURLY_SERIES_TICKERS BTC→KXBTCD …) — keying on asset would wrongly
        # block hourly NO-side (governed independently by HOURLY_NO_SIDE_LIVE).
        # Hourly/daily (KX<ASSET>D) + weather (KXHIGH*) → asset_from_ticker None →
        # untouched. If a governed 15M asset isn't live-enabled, the candidate was
        # still evaluated + logged by the scanner — we just place NO real order.
        # Read live so a flag flip is a runtime kill-switch. See bot/trading_mode.py.
        # R1-M4 (extended at Bit T-1): strategy-aware form — identical to
        # is_live for every main-pipeline strategy; only candidate strategies
        # 'longshot' / 'twaplock' can pass via their LONGSHOT_LIVE_OVERRIDE /
        # TWAPLOCK_LIVE_OVERRIDE flags (single-strategy go-live).
        _tm_asset = tm_asset_from_ticker(ticker)
        if _tm_asset is not None and not tm_strategy_is_live(
                candidate.get("strategy"), _tm_asset):
            logging.info(
                "SHADOW_SKIP: %s %s reason=%s — evaluated, no live order placed",
                ticker, candidate.get("strategy"), tm_mode_reason(_tm_asset))
            return None

        # ── Unified exposure caps (always active) ─────────────────────
        _fresh_balance = None
        try:
            if self._ml and hasattr(self._ml, 'scanner'):
                _fresh_balance = self._ml.scanner._get_balance_cached()
            if not isinstance(_fresh_balance, (int, float)) or _fresh_balance <= 0:
                _fresh_balance = candidate.get("balance_at_scan")
        except Exception:
            _fresh_balance = candidate.get("balance_at_scan")

        if isinstance(_fresh_balance, (int, float)) and _fresh_balance > 0:
            # Per-ticker cap: 20% of balance
            _existing_ticker_cost = sum(
                p["total_cost_cents"]
                for p in self._state.get_open_positions()
                if p["ticker"] == ticker
            )
            _candidate_price = candidate.get("best_yes_ask",
                               candidate.get("best_ask", 96))
            _candidate_cost = candidate["position_size"] * _candidate_price
            _ticker_cap = _fresh_balance * MAX_TICKER_RISK

            if _existing_ticker_cost + _candidate_cost > _ticker_cap:
                _remaining = _ticker_cap - _existing_ticker_cost
                _reduced = max(0, int(_remaining / _candidate_price)) if _candidate_price > 0 else 0
                if _reduced <= 0:
                    logging.info(
                        "TICKER_CAP_SKIPPED: %s %s existing=%dc candidate=%dc cap=%dc",
                        ticker, candidate.get("strategy"),
                        _existing_ticker_cost, _candidate_cost, _ticker_cap)
                    return None
                else:
                    logging.info(
                        "TICKER_CAP_REDUCED: %s %s %d->%dct existing=%dc cap=%dc",
                        ticker, candidate.get("strategy"),
                        candidate["position_size"], _reduced,
                        _existing_ticker_cost, _ticker_cap)
                    candidate["position_size"] = _reduced
                    _candidate_cost = _reduced * _candidate_price

            # Per-window cap: cross-asset — sum ALL positions in the same 15-min timeslot
            _event_ticker = candidate.get("event_ticker")
            if _event_ticker:
                # Extract timeslot for cross-asset matching
                # Event tickers: KXBTC15M-26APR021000, KXETH15M-26APR021000 → timeslot=26APR021000
                _et_parts = _event_ticker.split("-", 1)
                _timeslot = _et_parts[1] if len(_et_parts) > 1 else _event_ticker
                _existing_window_cost = (
                    self._existing_window_cost_for_timeslot(
                        self._state.get_open_positions(), _timeslot))
                if _existing_window_cost > 0:
                    logging.debug("WINDOW_XASSET: timeslot=%s existing=$%.2f",
                                  _timeslot, _existing_window_cost / 100)
                _window_cap = _fresh_balance * MAX_WINDOW_RISK
                if _existing_window_cost + _candidate_cost > _window_cap:
                    _w_remaining = _window_cap - _existing_window_cost
                    _w_reduced = max(0, int(_w_remaining / _candidate_price)) if _candidate_price > 0 else 0
                    if _w_reduced <= 0:
                        logging.info(
                            "WINDOW_CAP_SKIPPED: %s %s window=%dc candidate=%dc cap=%dc",
                            _event_ticker, candidate.get("strategy"),
                            _existing_window_cost, _candidate_cost, _window_cap)
                        return None
                    else:
                        logging.info(
                            "WINDOW_CAP_REDUCED: %s %s %d->%dct window=%dc cap=%dc",
                            _event_ticker, candidate.get("strategy"),
                            candidate["position_size"], _w_reduced,
                            _existing_window_cost, _window_cap)
                        candidate["position_size"] = _w_reduced

        # ── HOURLY TAKER-ONLY PATH ──
        # Hourly uses IOC exclusively. No per-asset lock, no maker orders, no escalation.
        # This guarantees zero contention with 15M execution. Gated on product_type == "hourly".
        # Exception: hourly DC uses the DC taker path (strategy="hourly_dc"), not the hourly taker.
        if (candidate.get("product_type") == "hourly"
                and HOURLY_TAKER_ONLY
                and candidate.get("strategy") != "hourly_dc"
                and candidate.get("side") != "no"):
            return self._execute_hourly_taker(candidate)

        # ── HOURLY NO TAKER-ONLY PATH ──
        # Hourly NO-side verification: 1-contract IOC at NO ask price.
        # Same isolation as hourly YES taker — no per-asset lock, no maker, no escalation.
        if (candidate.get("product_type") == "hourly"
                and candidate.get("side") == "no"):
            return self._execute_hourly_no_taker(candidate)

        # ── WEATHER NO TAKER-ONLY PATH ──
        # Weather NO orderbooks are structurally empty — nobody posts resting NO
        # asks at 30-40c. Maker-first always cancels unfilled after escalation
        # timeout (3/3 canceled, 0% fill rate, Apr 12 2026). Direct IOC at ask.
        # 1 contract × 35c = $0.35 cost; ~30% assumed edge makes taker fee trivial.
        if (candidate.get("product_type") == "weather"
                and candidate.get("side") == "no"):
            return self._execute_weather_no_taker(candidate)

        # ── LONGSHOT PREMIUM-HARVEST MAKER PATH (Bit L-1) ──
        # Dispatched AFTER the trading-mode gate + unified exposure caps
        # (single live/shadow chokepoint respected, never duplicated) and
        # BEFORE Gate 1 — a resting longshot quote must neither consume nor
        # be blocked by the per-asset maker lock of the main pipeline.
        # No escalation, no maker-tail: rest at 100-ask, hold to settlement.
        if candidate.get("strategy") == "longshot":
            return self._execute_longshot_maker(candidate)

        # ── TWAP-LOCK ENDGAME TAKER PATH (Bit T-1) ──
        # Same placement as longshot: AFTER the trading-mode gate + unified
        # exposure caps, BEFORE Gate 1 (an IOC resolves synchronously and
        # must neither consume nor be blocked by the main pipeline's
        # per-asset maker lock). No escalation, no maker tail, no cooldown:
        # the engine's one-shot-per-window latch is the retry guard.
        if candidate.get("strategy") == "twaplock":
            return self._execute_twaplock_taker(candidate)

        # Gate 1: Per-asset lock for maker-first assets only.
        # Taker-first (SOL): IOC resolves synchronously (<1s), no concurrent order risk.
        # Maker-first (BTC/ETH/XRP): per-asset lock prevents two resting makers.
        if asset not in TAKER_FIRST_ASSETS:
            if asset in self._active_orders or asset in self._escalating_assets:
                logging.warning(
                    "ORDER_SUPPRESSED asset_lock: %s %s active_ticker=%s escalating=%s",
                    asset, ticker,
                    self._active_orders.get(asset, {}).get("ticker", "none"),
                    asset in self._escalating_assets)
                self._session_suppressed_asset_lock += 1
                return None

        # Gate 2: Ticker cooldown — skip tickers recently attempted via IOC
        cooldown_ts = self._recent_taker_tickers.get(ticker)
        if cooldown_ts is not None:
            _cd_remaining = IOC_TICKER_COOLDOWN - (time.time() - cooldown_ts)
            if _cd_remaining > 0:
                logging.warning(
                    "ORDER_SUPPRESSED ticker_cooldown: %s remaining=%.0fs",
                    ticker, _cd_remaining)
                self._session_suppressed_ticker_cooldown += 1
                return None
            del self._recent_taker_tickers[ticker]

        # Gate 3: Per-ticker API error cap — stop hammering after 3 consecutive failures.
        # Prevents hot retry loops on expired/closed markets (21 api_errors in 30s, Mar 23).
        _api_err_count = self._ticker_api_errors.get(ticker, 0)
        if _api_err_count >= self.TICKER_API_ERROR_CAP:
            logging.warning("ORDER_SUPPRESSED api_error_cap: %s errors=%d (capped at %d)",
                            ticker, _api_err_count, self.TICKER_API_ERROR_CAP)
            return None

        if OBSERVATION_MODE:
            logging.info(
                f"OBSERVATION MODE: Would place maker for {candidate['ticker']} "
                f"at {candidate.get('best_yes_ask', '?')}¢ for "
                f"{candidate.get('position_size', '?')} contracts"
            )
            try:
                _fv = candidate.get("best_yes_ask")
                _obs_offset = (MAKER_PRICE_OFFSET if _fv and _fv >= 90
                               else MAKER_PRICE_OFFSET + 1) if _fv else MAKER_PRICE_OFFSET
                self._logger.log_execution({
                    "action": "observation_would_trade",
                    "ticker": candidate["ticker"],
                    "asset": candidate["asset"],
                    "event_ticker": candidate["event_ticker"],
                    "best_yes_ask": candidate.get("best_yes_ask"),
                    "position_size": candidate.get("position_size"),
                    "edge": candidate.get("edge"),
                    "calibrated_prob": candidate.get("calibrated_prob"),
                    "strategy": candidate.get("strategy"),
                    "seconds_to_close": candidate.get("seconds_to_close"),
                    "vol_regime": candidate.get("vol_regime"),
                    "ofa_adjustment": candidate.get("ofa_adjustment"),
                    "balance_at_scan": candidate.get("balance_at_scan"),
                    "execution_params": {
                        "maker_price": (_fv - _obs_offset) if _fv else None,
                        "maker_offset": _obs_offset,
                        "post_only": True,
                        "escalation_strategy": "cancel_replace_ioc",
                        "taker_time_in_force": "ioc",
                    },
                })
                if _telegram_state._TELEGRAM:
                    _ba = candidate.get("best_yes_ask", "?")
                    _edge = candidate.get("edge")
                    _prob = candidate.get("calibrated_prob")
                    _sz = candidate.get("position_size", "?")
                    _asset = candidate.get("asset", "?")
                    _edge_s = f"{_edge:.1%}" if _edge is not None else "?"
                    _prob_s = f"{_prob:.0%}" if _prob is not None else "?"
                    _cost = (_ba * _sz / 100) if isinstance(_ba, (int, float)) and isinstance(_sz, (int, float)) else 0
                    _telegram_state._TELEGRAM.send(
                        f"\U0001f4ca {_asset} {_sz}ct @ {_ba}c "
                        f"(${_cost:.2f}) edge={_edge_s} prob={_prob_s}",
                        dedup_key=candidate["ticker"],
                    )
                if not hasattr(self, '_last_obs_ticker') or self._last_obs_ticker != candidate['ticker']:
                    self._last_obs_ticker = candidate['ticker']
                    _ba = candidate.get("best_yes_ask")
                    _cp = candidate.get("calibrated_prob")
                    _obs_fee_cfg = get_market_config(candidate.get("product_type"))
                    _fee1 = calculate_fee(1, _ba, is_taker=True, fee_mult_taker=_obs_fee_cfg.fee_multiplier_taker) if _ba else 0
                    _ev = (_cp * (100 - _ba)) - ((1 - _cp) * _ba) - _fee1 if (_ba and _cp) else None
                    self._state.insert_evaluated_opportunity(
                        candidate["ticker"], candidate["event_ticker"],
                        candidate["asset"], "observation_trade",
                        spot_price=candidate.get("spot"),
                        threshold=candidate.get("threshold"),
                        volatility=candidate.get("blended_rv"),
                        market_price=_ba,
                        seconds_to_close=candidate.get("seconds_to_close"),
                        calibrated_prob=_cp,
                        edge=candidate.get("edge"),
                        ofa_adjustment=candidate.get("ofa_adjustment"),
                        strategy=candidate.get("strategy"),
                        position_size=candidate.get("position_size"),
                        kelly_f=candidate.get("kelly_f"),
                        z_score=candidate.get("z_score"),
                        vol_regime=candidate.get("vol_regime"),
                        calibrated_prob_raw=candidate.get("calibrated_prob_raw"),
                        breakeven_wr=_ba / 100.0 if _ba else None,
                        expected_value=round(_ev, 2) if _ev is not None else None,
                        drawdown_scaler=candidate.get("drawdown_scaler"),
                        ask_depth=candidate.get("ob_snapshot", {}).get("ask_depth"),
                        best_ask_source=candidate.get("best_ask_source"),
                        ofa_confidence=candidate.get("ofa_confidence"),
                        raw_prob=candidate.get("raw_prob"),
                        calibration_method=candidate.get("calibration_method"),
                        old_system_prob=candidate.get("old_system_prob"),
                        fee_adjusted_edge=candidate.get("fee_adjusted_edge"),
                        egarch_sigma=candidate.get("egarch_sigma"),
                        egarch_blend_sigma=candidate.get("egarch_blend_sigma"),
                        egarch_blend_weight=candidate.get("egarch_blend_weight"),
                        mz_r_squared=candidate.get("mz_r_squared"),
                        shadow_tv_blend_rv=candidate.get("shadow_tv_blend_rv"),
                        mz_shadow_sigmoid_w=candidate.get("mz_shadow_sigmoid_w"),
                        mz_baseline_qlike=candidate.get("mz_baseline_qlike"),
                        mz_qlike=candidate.get("mz_qlike"),
                        counterfactual=candidate.get("counterfactual_json"),
                        shadow_cal_prob=candidate.get("shadow_cal_prob"),
                        shadow_cal_fee_edge=candidate.get("shadow_cal_fee_edge"),
                        shadow_cal_temperature=candidate.get("shadow_cal_temperature"),
                        oft_prob_adjustment=candidate.get("oft_prob_adjustment"),
                        oft_imbalance_ratio=candidate.get("oft_imbalance_ratio"),
                        oft_n_snapshots=candidate.get("oft_n_snapshots"),
                        product_type=candidate.get("product_type"),
                        wx_ensemble_mean=candidate.get("wx_ensemble_mean"),
                        wx_ensemble_std=candidate.get("wx_ensemble_std"),
                        wx_bias_correction=candidate.get("wx_bias_correction"),
                        wx_n_members=candidate.get("wx_n_members"),
                        wx_market_type=candidate.get("wx_market_type"),
                        wx_hrrr_temp=candidate.get("wx_hrrr_temp"),
                        wx_corrected_mean=candidate.get("wx_corrected_mean"),
                        hourly_pre_temp_prob=candidate.get("hourly_pre_temp_prob"),
                        hourly_applied_temp_t=candidate.get("hourly_applied_temp_t"),
                        hourly_shadow_temp_2_0=candidate.get("hourly_shadow_temp_2_0"),
                        hourly_shadow_temp_1_0=candidate.get("hourly_shadow_temp_1_0"),
                        hourly_shadow_temp_2_5=candidate.get("hourly_shadow_temp_2_5"),
                        hourly_shadow_blend_50=candidate.get("hourly_shadow_blend_50"),
                        hourly_shadow_temp_1_75=candidate.get("hourly_shadow_temp_1_75"),
                        hourly_shadow_temp_3_0=candidate.get("hourly_shadow_temp_3_0"),
                        hourly_shadow_blend_20=candidate.get("hourly_shadow_blend_20"),
                        hourly_shadow_blend_30=candidate.get("hourly_shadow_blend_30"),
                        hourly_shadow_blend_60=candidate.get("hourly_shadow_blend_60"),
                        hourly_post_temp_prob=candidate.get("hourly_post_temp_prob"),
                        available_balance_cents=candidate.get("balance_at_scan"),
                        # cal_mlp_* propagation: candidate dict carries these via
                        # **_shadow_diag splat at line ~15425. Without these kwargs
                        # the post-hoc processor's WHERE cal_mlp_request_id IS NOT NULL
                        # never matches the row → 0% annotation on real trades.
                        cal_mlp_request_id=candidate.get("cal_mlp_request_id"),
                        cal_mlp_skipped_reason=candidate.get("cal_mlp_skipped_reason"),
                        cal_mlp_p_mean=candidate.get("cal_mlp_p_mean"),
                        cal_mlp_p_std=candidate.get("cal_mlp_p_std"),
                        cal_mlp_final_lo=candidate.get("cal_mlp_final_lo"),
                        cal_mlp_final_hi=candidate.get("cal_mlp_final_hi"),
                        cal_mlp_train_id=candidate.get("cal_mlp_train_id"),
                        config_snapshot_id=(
                            self._ml.config_snapshot_id if self._ml else None
                        ))
            except Exception as e:
                logging.error(f"OBSERVATION_DB_INSERT_FAILED: {candidate.get('ticker')}: {e}")
            return None

        # ── Log candidate to evaluated_opportunities (live mode) ──
        try:
            _ba = candidate.get("best_yes_ask")
            _cp = candidate.get("calibrated_prob")
            _cand_fee_cfg = get_market_config(candidate.get("product_type"))
            _fee1 = calculate_fee(1, _ba, is_taker=True, fee_mult_taker=_cand_fee_cfg.fee_multiplier_taker) if _ba else 0
            _ev = (_cp * (100 - _ba)) - ((1 - _cp) * _ba) - _fee1 if (_ba and _cp) else None
            self._state.insert_evaluated_opportunity(
                candidate["ticker"], candidate["event_ticker"],
                candidate["asset"], "candidate",
                spot_price=candidate.get("spot"),
                threshold=candidate.get("threshold"),
                volatility=candidate.get("blended_rv"),
                market_price=_ba,
                seconds_to_close=candidate.get("seconds_to_close"),
                calibrated_prob=_cp,
                edge=candidate.get("edge"),
                ofa_adjustment=candidate.get("ofa_adjustment"),
                strategy=candidate.get("strategy"),
                position_size=candidate.get("position_size"),
                kelly_f=candidate.get("kelly_f"),
                z_score=candidate.get("z_score"),
                vol_regime=candidate.get("vol_regime"),
                calibrated_prob_raw=candidate.get("calibrated_prob_raw"),
                breakeven_wr=_ba / 100.0 if _ba else None,
                expected_value=round(_ev, 2) if _ev is not None else None,
                drawdown_scaler=candidate.get("drawdown_scaler"),
                ask_depth=candidate.get("ob_snapshot", {}).get("ask_depth"),
                best_ask_source=candidate.get("best_ask_source"),
                ofa_confidence=candidate.get("ofa_confidence"),
                raw_prob=candidate.get("raw_prob"),
                calibration_method=candidate.get("calibration_method"),
                old_system_prob=candidate.get("old_system_prob"),
                fee_adjusted_edge=candidate.get("fee_adjusted_edge"),
                egarch_sigma=candidate.get("egarch_sigma"),
                egarch_blend_sigma=candidate.get("egarch_blend_sigma"),
                egarch_blend_weight=candidate.get("egarch_blend_weight"),
                mz_r_squared=candidate.get("mz_r_squared"),
                shadow_tv_blend_rv=candidate.get("shadow_tv_blend_rv"),
                mz_shadow_sigmoid_w=candidate.get("mz_shadow_sigmoid_w"),
                mz_baseline_qlike=candidate.get("mz_baseline_qlike"),
                mz_qlike=candidate.get("mz_qlike"),
                counterfactual=candidate.get("counterfactual_json"),
                shadow_cal_prob=candidate.get("shadow_cal_prob"),
                shadow_cal_fee_edge=candidate.get("shadow_cal_fee_edge"),
                shadow_cal_temperature=candidate.get("shadow_cal_temperature"),
                oft_prob_adjustment=candidate.get("oft_prob_adjustment"),
                oft_imbalance_ratio=candidate.get("oft_imbalance_ratio"),
                oft_n_snapshots=candidate.get("oft_n_snapshots"),
                product_type=candidate.get("product_type"),
                wx_ensemble_mean=candidate.get("wx_ensemble_mean"),
                wx_ensemble_std=candidate.get("wx_ensemble_std"),
                wx_bias_correction=candidate.get("wx_bias_correction"),
                wx_n_members=candidate.get("wx_n_members"),
                wx_market_type=candidate.get("wx_market_type"),
                wx_no_side_edge=candidate.get("wx_no_side_edge"),
                wx_hrrr_temp=candidate.get("wx_hrrr_temp"),
                wx_corrected_mean=candidate.get("wx_corrected_mean"),
                hourly_pre_temp_prob=candidate.get("hourly_pre_temp_prob"),
                hourly_applied_temp_t=candidate.get("hourly_applied_temp_t"),
                hourly_shadow_temp_2_0=candidate.get("hourly_shadow_temp_2_0"),
                hourly_shadow_temp_1_0=candidate.get("hourly_shadow_temp_1_0"),
                hourly_shadow_temp_2_5=candidate.get("hourly_shadow_temp_2_5"),
                hourly_shadow_blend_50=candidate.get("hourly_shadow_blend_50"),
                hourly_shadow_temp_1_75=candidate.get("hourly_shadow_temp_1_75"),
                hourly_shadow_temp_3_0=candidate.get("hourly_shadow_temp_3_0"),
                hourly_shadow_blend_20=candidate.get("hourly_shadow_blend_20"),
                hourly_shadow_blend_30=candidate.get("hourly_shadow_blend_30"),
                hourly_shadow_blend_60=candidate.get("hourly_shadow_blend_60"),
                hourly_post_temp_prob=candidate.get("hourly_post_temp_prob"),
                available_balance_cents=candidate.get("balance_at_scan"),
                cal_mlp_request_id=candidate.get("cal_mlp_request_id"),
                cal_mlp_skipped_reason=candidate.get("cal_mlp_skipped_reason"),
                cal_mlp_p_mean=candidate.get("cal_mlp_p_mean"),
                cal_mlp_p_std=candidate.get("cal_mlp_p_std"),
                cal_mlp_final_lo=candidate.get("cal_mlp_final_lo"),
                cal_mlp_final_hi=candidate.get("cal_mlp_final_hi"),
                cal_mlp_train_id=candidate.get("cal_mlp_train_id"),
                config_snapshot_id=(
                    self._ml.config_snapshot_id if self._ml else None
                ))
        except Exception as e:
            logging.error(f"CANDIDATE_DB_INSERT_FAILED: {candidate.get('ticker')}: {e}")

        seconds_to_close = candidate.get("seconds_to_close")

        # ── Decided contract taker override ─────────────────────────
        # Must be checked BEFORE SOL taker-first and direct taker paths,
        # which apply MIN_EDGE_PCT (0.25%). DC uses -0.01 threshold.
        # Bug fix: SOL DC candidates were hitting SOL taker-first path
        # first, getting edge-gated at 0.25% when DC allows -1%.
        #
        # Non-blocking retry: Kalshi IOCs partial fill (fill whatever's on
        # the book, cancel the rest). On unfilled/partial, queue a retry
        # entry — processed at the TOP of next _tick() cycle (~5s later).
        # Data: 24% of DC tickers recover within 8-32s.
        _dc_strategy = candidate.get("strategy")
        if _dc_strategy in ("decided_t1", "decided_t1b", "decided_t2",
                            "decided_t2_z2", "decided_t2_z25", "hourly_dc"):
            return self._execute_dc_taker(candidate, asset, seconds_to_close)

        # ── Terminal momentum taker override ──────────────────────────
        if _dc_strategy and _dc_strategy.startswith("terminal_momentum"):
            return self._execute_tm_taker(candidate, asset, seconds_to_close)

        # ── LPNE taker override ──────────────────────────────────────
        if _dc_strategy == "low_price_near_expiry":
            return self._execute_lpne_taker(candidate, asset, seconds_to_close)

        # ── Bracket NO taker override ────────────────────────────────
        if _dc_strategy == "bracket_no":
            return self._execute_bracket_no_taker(candidate, asset, seconds_to_close)

        # ── SOL taker-first override ──────────────────────────────
        # SOL: bypass maker entirely, go direct IOC — UNLESS book is empty.
        # Data: 44.7% maker fill rate, $101/wk missed, 95% unfilled WR.
        # Empty-book fallback: post maker bid to attract counterparties (like BTC/ETH).
        # Data: 400 unfilled SOL depth=0 candidates at 87c+ have 95% hypothetical WR.
        _sol_empty_book_fallback = False
        if SOL_TAKER_FIRST and candidate.get("asset") == "SOL":
            _sol_depth = candidate.get("ob_snapshot", {}).get("ask_depth", 0)
            _sol_price = candidate.get("best_yes_ask", 0)
            _sol_stc = seconds_to_close or 0

            # Empty book + price >= 87c: fall through to maker path
            if _sol_depth == 0 and _sol_price >= SOL_EMPTY_BOOK_MAKER_MIN_PRICE:
                if _sol_stc < SOL_EMPTY_BOOK_MIN_STC:
                    logging.info(
                        "sol_empty_book_SKIP_STC: %s price=%dc depth=0 stc=%.0fs < %.0fs",
                        ticker, _sol_price, _sol_stc, SOL_EMPTY_BOOK_MIN_STC)
                    self._session_sol_empty_maker_skip_stc += 1
                    return None
                # Fall through to maker path (PATH 5)
                logging.info(
                    "sol_empty_book_MAKER_FALLBACK: %s price=%dc depth=0 stc=%.0fs",
                    ticker, _sol_price, _sol_stc)
                self._session_sol_empty_maker_attempt += 1
                _sol_empty_book_fallback = True
                # Need per-asset lock check (SOL normally skips it as taker-first)
                if asset in self._active_orders or asset in self._escalating_assets:
                    logging.warning(
                        "ORDER_SUPPRESSED asset_lock: %s %s (sol_empty_book_maker) active=%s",
                        asset, ticker, self._active_orders.get(asset, {}).get("ticker", "none"))
                    self._session_suppressed_asset_lock += 1
                    return None
                # Fall through — will hit PATH 5 (maker) below
            elif _sol_depth == 0:
                # Empty book but price < 87c: skip entirely
                logging.info(
                    "sol_empty_book_SKIP_PRICE: %s price=%dc depth=0 (< %dc floor)",
                    ticker, _sol_price, SOL_EMPTY_BOOK_MAKER_MIN_PRICE)
                self._session_sol_empty_maker_skip_price += 1
                return None

        if SOL_TAKER_FIRST and candidate.get("asset") == "SOL" and not _sol_empty_book_fallback:
            count = candidate["position_size"]
            price = candidate["best_yes_ask"]
            cal_prob = candidate["calibrated_prob"]

            if count <= 0:
                logging.warning("ORDER_SUPPRESSED zero_size: %s asset=SOL price=%d",
                                candidate["ticker"], price)
                self._session_suppressed_zero_size += 1
                return None

            taker_fee = calculate_taker_fee(count, price)
            net_edge = cal_prob - (price / 100.0) - (taker_fee / (count * 100.0))

            if net_edge < MIN_EDGE_PCT / 100.0:
                logging.warning(
                    "ORDER_SUPPRESSED edge_taker_fee: %s asset=SOL net_edge=%.4f < min=%.4f "
                    "price=%d taker_fee=%d¢ stc=%.0f",
                    candidate["ticker"], net_edge, MIN_EDGE_PCT / 100.0, price, taker_fee,
                    seconds_to_close or 0)
                self._session_suppressed_edge_recalc += 1
                return None

            fresh_ask = self._get_addon_best_ask(candidate["ticker"])
            if fresh_ask is None:
                fresh_ask = self._nbbo_fallback_price(candidate)
                if fresh_ask is None:
                    logging.warning("ORDER_SUPPRESSED no_asks: %s asset=SOL price=%d stc=%.0f",
                                    candidate["ticker"], price, seconds_to_close or 0)
                    self._session_suppressed_no_asks += 1
                    return None

            if fresh_ask != price:
                logging.info("sol_taker_override_price_update: %s scanner=%d¢ fresh=%d¢",
                             candidate["ticker"], price, fresh_ask)
                price = fresh_ask
                candidate["best_yes_ask"] = fresh_ask
                taker_fee = calculate_taker_fee(count, price)
                net_edge = cal_prob - (price / 100.0) - (taker_fee / (count * 100.0))
                if net_edge < MIN_EDGE_PCT / 100.0:
                    logging.warning(
                        "ORDER_SUPPRESSED edge_recalc: %s asset=SOL fresh_ask=%d net_edge=%.4f < min=%.4f",
                        candidate["ticker"], price, net_edge, MIN_EDGE_PCT / 100.0)
                    self._session_suppressed_edge_recalc += 1
                    return None

            # Apply ask+1c offset for fill certainty on taker-first
            ioc_price = min(price + IOC_RETRY_OFFSET, 99)
            if ioc_price != price:
                _offset_fee = calculate_taker_fee(count, ioc_price)
                _offset_edge = cal_prob - (ioc_price / 100.0) - (_offset_fee / (count * 100.0))
                if _offset_edge >= MIN_EDGE_PCT / 100.0:
                    price = ioc_price
                    candidate["best_yes_ask"] = ioc_price
                    net_edge = _offset_edge
                    taker_fee = _offset_fee

            logging.info(
                "sol_taker_override_ENTRY: %s %dx @ %d¢ "
                "seconds_to_close=%.0f net_edge=%.4f cal_prob=%.4f taker_fee=%d¢",
                candidate["ticker"], count, price,
                seconds_to_close or 0, net_edge, cal_prob, taker_fee)

            candidate["entry_path"] = "sol_taker_override"
            candidate["escalation_type"] = "sol_taker_override"
            # Sprint B Bit B.2b — decision snapshot at route choice.
            self._emit_decision_snapshot(candidate, "taker_first")
            self._recent_taker_tickers[candidate["ticker"]] = time.time()
            _order_submit_ts = datetime.datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
            self._session_direct_taker_attempts += 1
            result = self._submit_taker(candidate)
            if result is not None:
                logging.info("sol_taker_override_FILLED: %s", candidate["ticker"])
                self._session_direct_taker_fills += 1
                _taker_oid = result.get("order_id") if isinstance(result, dict) else None
                self._state.update_evaluated_opportunity_order(
                    candidate["ticker"], order_id=_taker_oid,
                    order_submitted_at=_order_submit_ts, order_outcome="filled",
                    taker_ask_at_submit=candidate.get("best_yes_ask"))
            else:
                # IOC retry: refresh ask, try once more at fresh_ask + offset
                _retry_ask = self._get_addon_best_ask(candidate["ticker"])
                _retry_result = None
                if _retry_ask is not None:
                    _retry_price = min(_retry_ask + IOC_RETRY_OFFSET, 99)
                    # Don't chase more than 2c above original submission price
                    if _retry_price <= price + 2:
                        _retry_fee = calculate_taker_fee(count, _retry_price)
                        _retry_edge = cal_prob - (_retry_price / 100.0) - (_retry_fee / (count * 100.0))
                        if _retry_edge >= MIN_EDGE_PCT / 100.0:
                            logging.info("sol_taker_IOC_RETRY: %s retry_price=%d¢ retry_edge=%.4f",
                                         candidate["ticker"], _retry_price, _retry_edge)
                            candidate["best_yes_ask"] = _retry_price
                            candidate["escalation_type"] = "ioc_retry"
                            self._session_ioc_retries += 1
                            _retry_result = self._submit_taker(candidate)
                            if _retry_result is not None:
                                logging.info("sol_taker_IOC_RETRY_FILLED: %s", candidate["ticker"])
                                self._session_ioc_retry_fills += 1
                                result = _retry_result
                                _taker_oid = result.get("order_id") if isinstance(result, dict) else None
                                self._state.update_evaluated_opportunity_order(
                                    candidate["ticker"], order_id=_taker_oid,
                                    order_submitted_at=_order_submit_ts, order_outcome="filled",
                                    taker_ask_at_submit=candidate.get("best_yes_ask"))
                if _retry_result is None:
                    logging.warning("sol_taker_override_UNFILLED: %s", candidate["ticker"])
                    self._session_direct_taker_unfilled += 1
                    self._state.update_evaluated_opportunity_order(
                        candidate["ticker"], order_submitted_at=_order_submit_ts,
                        order_outcome="unfilled",
                        taker_ask_at_submit=candidate.get("best_yes_ask"))

            # ── SOL Path C shadow: log what maker path would have done ──
            try:
                _pathc_fv = price  # current best ask (possibly refreshed)
                _pathc_offset = MAKER_PRICE_OFFSET if _pathc_fv >= 90 else MAKER_PRICE_OFFSET + 1
                _pathc_maker_price = _pathc_fv - _pathc_offset

                # Get depth at the hypothetical maker price level
                _pathc_depth = 0
                try:
                    scanner = self._ml.scanner if self._ml else None
                    if scanner:
                        _pc_ob, _ = scanner._get_orderbook_cached(candidate["ticker"])
                        if _pc_ob:
                            _pathc_depth = OrderExecutor._best_ask_depth(_pc_ob)
                except Exception:
                    pass

                _pathc_pos_size = candidate["position_size"]
                _eval_time = datetime.datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")

                self._state.insert_sol_pathc_shadow(
                    ticker=candidate["ticker"],
                    evaluation_time=_eval_time,
                    live_ask=price,
                    live_depth=_pathc_depth,
                    live_edge=net_edge,
                    live_stc=seconds_to_close or 0,
                    live_contracts=count,
                    live_entry_price=price,
                    live_cal_prob=cal_prob,
                    pathc_maker_price=_pathc_maker_price,
                    pathc_maker_offset=_pathc_offset,
                    pathc_depth_at_maker=_pathc_depth,
                    position_size=_pathc_pos_size,
                )

                # Schedule for deferred observation (check every tick during escalation window)
                _esc_wait = ESCALATION_WAIT_LONG  # SOL uses default 15s
                self._sol_pathc_pending[candidate["ticker"]] = {
                    "start_time": time.time(),
                    "escalation_wait": _esc_wait,
                    "maker_price": _pathc_maker_price,
                    "position_size": _pathc_pos_size,
                    "cal_prob": cal_prob,
                    "touched": False,
                }
                logging.info(
                    "sol_pathc_shadow_LOGGED: %s maker_price=%d¢ offset=%d depth=%d pos_size=%d",
                    candidate["ticker"], _pathc_maker_price, _pathc_offset, _pathc_depth, _pathc_pos_size)
            except Exception:
                logging.warning("sol_pathc_shadow logging failed", exc_info=True)

            return result

        # ── Direct taker for <180s candidates ───────────────────────
        # Maker-only below 90s: block direct taker, fall through to maker
        if (seconds_to_close is not None
                and seconds_to_close < MAKER_ONLY_THRESHOLD
                and seconds_to_close < DIRECT_TAKER_THRESHOLD):
            logging.info(
                "direct_taker_BLOCKED_maker_only: %s seconds_to_close=%.0f",
                candidate["ticker"], seconds_to_close)
        elif seconds_to_close is not None and seconds_to_close < DIRECT_TAKER_THRESHOLD:
            count = candidate["position_size"]
            price = candidate["best_yes_ask"]
            cal_prob = candidate["calibrated_prob"]

            if count <= 0:
                logging.warning("ORDER_SUPPRESSED zero_size: %s asset=%s path=direct_taker price=%d",
                                candidate["ticker"], asset, price)
                self._session_suppressed_zero_size += 1
                self._session_direct_taker_skipped += 1
                return None

            taker_fee = calculate_taker_fee(count, price)
            net_edge = cal_prob - (price / 100.0) - (taker_fee / (count * 100.0))

            if net_edge < MIN_EDGE_PCT / 100.0:
                logging.warning(
                    "ORDER_SUPPRESSED edge_taker_fee: %s asset=%s path=direct_taker "
                    "net_edge=%.4f < min=%.4f price=%d taker_fee=%d¢ stc=%.0f",
                    candidate["ticker"], asset, net_edge, MIN_EDGE_PCT / 100.0,
                    price, taker_fee, seconds_to_close)
                self._session_suppressed_edge_recalc += 1
                self._session_direct_taker_skipped += 1
                return None

            # Verify actual liquidity before submitting IOC
            fresh_ask = self._get_addon_best_ask(candidate["ticker"])
            if fresh_ask is None:
                fresh_ask = self._nbbo_fallback_price(candidate)
                if fresh_ask is None:
                    logging.warning(
                        "ORDER_SUPPRESSED no_asks: %s asset=%s path=direct_taker stc=%.0f",
                        candidate["ticker"], asset, seconds_to_close)
                    self._session_suppressed_no_asks += 1
                    self._session_direct_taker_skipped += 1
                    return None

            # Use fresh ask if it differs from scanner's (may be stale NBBO)
            if fresh_ask != price:
                logging.info(
                    "direct_taker_price_update: %s scanner=%d¢ fresh=%d¢",
                    candidate["ticker"], price, fresh_ask)
                price = fresh_ask
                candidate["best_yes_ask"] = fresh_ask
                taker_fee = calculate_taker_fee(count, price)
                net_edge = cal_prob - (price / 100.0) - (taker_fee / (count * 100.0))
                if net_edge < MIN_EDGE_PCT / 100.0:
                    logging.info(
                        "direct_taker_SKIPPED: %s fresh_ask=%d¢ net_edge=%.4f < min",
                        candidate["ticker"], price, net_edge)
                    self._session_direct_taker_skipped += 1
                    return None

            self._session_direct_taker_attempts += 1
            logging.info(
                "direct_taker_ENTRY: %s %dx @ %d¢ "
                "seconds_to_close=%.0f net_edge=%.4f cal_prob=%.4f taker_fee=%d¢",
                candidate["ticker"], count, price,
                seconds_to_close, net_edge, cal_prob, taker_fee)

            candidate["entry_path"] = "direct_taker"
            candidate["escalation_type"] = "direct_taker"
            # Sprint B Bit B.2b — decision snapshot at route choice.
            self._emit_decision_snapshot(candidate, "taker_first")
            self._recent_taker_tickers[candidate["ticker"]] = time.time()
            _order_submit_ts = datetime.datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
            result = self._submit_taker(candidate)
            if result is not None:
                self._session_direct_taker_fills += 1
                logging.info("direct_taker_FILLED: %s", candidate["ticker"])
                _taker_oid = result.get("order_id") if isinstance(result, dict) else None
                self._state.update_evaluated_opportunity_order(
                    candidate["ticker"], order_id=_taker_oid,
                    order_submitted_at=_order_submit_ts, order_outcome="filled")
            else:
                self._session_direct_taker_unfilled += 1
                logging.warning("direct_taker_UNFILLED: %s", candidate["ticker"])
                self._state.update_evaluated_opportunity_order(
                    candidate["ticker"], order_submitted_at=_order_submit_ts,
                    order_outcome="unfilled")
            return result

        # ── Three-tier post_only rejection escalation ──────────────
        ticker = candidate["ticker"]
        rejections = self._get_post_only_rejection_count(ticker)

        # Tier 3: Taker escalation (2 same-price + 1 degraded all failed)
        # Maker-only below 90s: block post-only taker escalation
        if (rejections >= POST_ONLY_MAX_SAME_PRICE + 1  # 3+
                and not (seconds_to_close is not None and seconds_to_close < MAKER_ONLY_THRESHOLD)):
            count = candidate["position_size"]
            price = candidate["best_yes_ask"]
            cal_prob = candidate["calibrated_prob"]

            if count <= 0:
                logging.warning("post_only_taker_SKIPPED: %s position_size=%d", ticker, count)
                self._post_only_rejections.pop(ticker, None)
                return None

            taker_fee = calculate_taker_fee(count, price)
            net_edge = cal_prob - (price / 100.0) - (taker_fee / (count * 100.0))

            if net_edge < MIN_EDGE_PCT / 100.0:
                logging.info(
                    "post_only_taker_SKIPPED: %s net_edge=%.4f < min=%.4f "
                    "taker_fee=%d¢ count=%d price=%d¢",
                    ticker, net_edge, MIN_EDGE_PCT / 100.0, taker_fee, count, price)
                self._post_only_rejections.pop(ticker, None)
                return None

            # Verify actual liquidity before submitting IOC
            fresh_ask = self._get_addon_best_ask(ticker)
            if fresh_ask is None:
                logging.info(
                    "post_only_taker_SKIPPED: %s no asks on orderbook", ticker)
                self._post_only_rejections.pop(ticker, None)
                return None

            if fresh_ask != price:
                logging.info(
                    "post_only_taker_price_update: %s scanner=%d¢ fresh=%d¢",
                    ticker, price, fresh_ask)
                price = fresh_ask
                candidate["best_yes_ask"] = fresh_ask
                taker_fee = calculate_taker_fee(count, price)
                net_edge = cal_prob - (price / 100.0) - (taker_fee / (count * 100.0))
                if net_edge < MIN_EDGE_PCT / 100.0:
                    logging.info(
                        "post_only_taker_SKIPPED: %s fresh_ask=%d¢ net_edge=%.4f < min",
                        ticker, price, net_edge)
                    self._post_only_rejections.pop(ticker, None)
                    return None

            logging.info(
                "post_only_taker_ESCALATION: %s %dx @ %d¢ "
                "rejections=%d net_edge=%.4f cal_prob=%.4f taker_fee=%d¢",
                ticker, count, price, rejections, net_edge, cal_prob, taker_fee)
            self._session_post_only_taker_escalations += 1
            candidate["entry_path"] = "post_only_taker"
            candidate["escalation_type"] = "post_only_taker"
            # Sprint B Bit B.2b — escalation snapshot. Same decision_id
            # reuse as the maker tier-1 emit upstream.
            self._emit_decision_snapshot(candidate, "escalate")
            self._recent_taker_tickers[ticker] = time.time()
            _order_submit_ts = datetime.datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
            result = self._submit_taker(candidate)
            if result is not None:
                self._post_only_rejections.pop(ticker, None)
                self._session_post_only_taker_fills += 1
                logging.info("post_only_taker_FILLED: %s", ticker)
                _taker_oid = result.get("order_id") if isinstance(result, dict) else None
                self._state.update_evaluated_opportunity_order(
                    ticker, order_id=_taker_oid,
                    order_submitted_at=_order_submit_ts, order_outcome="filled")
            else:
                # Clear rejections to prevent hot retry loop on persistent API errors
                self._post_only_rejections.pop(ticker, None)
                logging.warning("post_only_taker_UNFILLED: %s (cleared rejections, will re-evaluate)", ticker)
                self._state.update_evaluated_opportunity_order(
                    ticker, order_submitted_at=_order_submit_ts,
                    order_outcome="unfilled")
            return result

        # Tier 2: Degraded maker (1¢ worse, one attempt)
        if rejections == POST_ONLY_MAX_SAME_PRICE:  # 2
            logging.info(
                "post_only_degraded_maker: %s rejections=%d, trying %d¢ worse",
                ticker, rejections, POST_ONLY_DEGRADED_EXTRA_OFFSET)
            self._session_post_only_degraded_attempts += 1
            # Sprint B Bit B.2b — decision snapshot at degraded-maker
            # route choice. Same decision_id as the upstream tier-1
            # emit IF this candidate is re-entering execute() with the
            # original dict; defensive seed-if-missing in helper handles
            # the fresh-entry case (rejection re-evaluation).
            self._emit_decision_snapshot(candidate, "maker_first")
            self._submit_maker(candidate, degraded=True)
            _active = self._active_orders.get(candidate["asset"])
            if _active:
                self._state.update_evaluated_opportunity_order(
                    ticker, order_id=_active["order_id"],
                    order_submitted_at=datetime.datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
                    taker_ask_at_submit=candidate.get("best_yes_ask"))
            return None

        # Tier 1: Normal maker (attempt 1 or 2)
        # Sprint B Bit B.2b — decision snapshot at route choice.
        self._emit_decision_snapshot(candidate, "maker_first")
        self._submit_maker(candidate)
        _active = self._active_orders.get(candidate["asset"])
        if _active:
            self._state.update_evaluated_opportunity_order(
                candidate["ticker"], order_id=_active["order_id"],
                order_submitted_at=datetime.datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
                taker_ask_at_submit=candidate.get("best_yes_ask"))
        return None

    def tick(self) -> Optional[Dict]:
        """Called each main-loop tick.  Iterates all active orders,
        polls for fills, and handles escalation independently per order.
        Also sweeps maker-tail orders for TTL expiry — must run even
        when _active_orders is empty, since tails outlive the IOC.
        """
        # Maker-tail TTL sweep runs unconditionally (before the
        # active_orders early-out below). A tail can outlive its
        # parent IOC's _active_orders entry, so gating the sweep on
        # active_orders being non-empty would let tails leak past TTL.
        if MAKER_TAIL_AFTER_IOC_PARTIAL and self._maker_tails:
            try:
                self._sweep_maker_tails()
            except Exception:
                logging.warning(
                    "_sweep_maker_tails raised", exc_info=True)
        if not self._active_orders:
            return None

        # Drain WS fills once, group by order_id
        ws_fills_by_oid: Dict[str, list] = {}
        if self._kalshi_feed and self._kalshi_feed.is_connected:
            try:
                for ws_fill in self._kalshi_feed.pop_fills():
                    oid = ws_fill.get("order_id", "")
                    ws_fills_by_oid.setdefault(oid, []).append(ws_fill)
            except Exception:
                logging.warning("WS fill drain failed", exc_info=True)

        result = None
        for asset in list(self._active_orders):
            order = self._active_orders.get(asset)
            if order is None:
                continue  # removed by a prior iteration's escalation
            order_ws = ws_fills_by_oid.get(order.get("order_id", ""), [])
            r = self._tick_one(order, asset, order_ws)
            if r is not None:
                result = r
        return result

    def _tick_sol_pathc_observations(self):
        """Check orderbook every tick for pending SOL Path C shadow entries.

        During the escalation window (default 15s), continuously monitor the
        orderbook. If best ask ever touches the hypothetical maker price,
        set obs_maker_price_touched=1 (sticky). After escalation window expires,
        write final observation snapshot and remove from pending.
        """
        if not self._sol_pathc_pending:
            return

        now = time.time()
        completed = []

        for ticker, info in self._sol_pathc_pending.items():
            elapsed = now - info["start_time"]
            maker_price = info["maker_price"]

            # Fetch current orderbook
            obs_ask = self._get_addon_best_ask(ticker)
            obs_depth = 0
            if obs_ask is not None:
                try:
                    scanner = self._ml.scanner if self._ml else None
                    if scanner:
                        _ob, _ = scanner._get_orderbook_cached(ticker)
                        if _ob:
                            obs_depth = OrderExecutor._best_ask_depth(_ob)
                except Exception:
                    pass

            # Check if ask has touched maker price (sticky boolean)
            if obs_ask is not None and obs_ask <= maker_price:
                if not info["touched"]:
                    info["touched"] = True
                    try:
                        self._state.update_sol_pathc_touch(ticker)
                    except Exception:
                        logging.warning("sol_pathc_touch update failed for %s", ticker, exc_info=True)

            maker_would_fill = 1 if (obs_ask is not None and obs_ask <= maker_price) else 0

            # After escalation window: write final observation and compute escalation snapshot
            if elapsed >= info["escalation_wait"]:
                # Compute escalation taker edge
                esc_edge = None
                if obs_ask is not None:
                    esc_taker_fee = calculate_taker_fee(info["position_size"], obs_ask)
                    esc_edge = info["cal_prob"] - (obs_ask / 100.0) - (esc_taker_fee / (info["position_size"] * 100.0))

                obs_time = datetime.datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
                try:
                    self._state.update_sol_pathc_observation(
                        ticker=ticker,
                        obs_time=obs_time,
                        obs_elapsed=round(elapsed, 1),
                        obs_best_ask=obs_ask,
                        obs_depth=obs_depth,
                        obs_maker_would_fill=maker_would_fill,
                        obs_maker_price_touched=1 if info["touched"] else 0,
                        pathc_esc_ask=obs_ask,
                        pathc_esc_depth=obs_depth,
                        pathc_esc_edge=esc_edge,
                    )
                    logging.info(
                        "sol_pathc_obs_FINAL: %s elapsed=%.1fs ask=%s depth=%d touched=%s esc_edge=%s",
                        ticker, elapsed, obs_ask, obs_depth, info["touched"],
                        f"{esc_edge:.4f}" if esc_edge is not None else "None")
                except Exception:
                    logging.warning("sol_pathc_observation write failed for %s", ticker, exc_info=True)

                completed.append(ticker)

        for ticker in completed:
            self._sol_pathc_pending.pop(ticker, None)

    def _tick_one(self, order: Dict, asset: str,
                  ws_fills: list) -> Optional[Dict]:
        """Handle one active order: poll for fill, escalate if needed."""
        now = time.time()

        # Backstop: if more than 60s past expected close and order still
        # tracked, some path failed to clean up (404-variant the
        # targeted fix doesn't anticipate, missed WS expiry, etc.).
        # Force-pop with WARNING. See kb/failures/cancel-404-asset-lockout-may04.md
        elapsed = now - order["submit_time"]
        remaining = order["seconds_to_close_at_submit"] - elapsed
        if remaining < -60.0:
            filled = order.get("filled_so_far", 0)
            if filled > 0:
                db_status = "partial_canceled"
                outcome = "partial_filled"
                fm_label = "partial_canceled"
            else:
                db_status = "expired"
                outcome = "expired"
                fm_label = "expired"
            backstop_reason = (
                f"force_pop_after_close"
                f"_pending={order.get('cancel_pending', False)}"
                f"_esc={order.get('escalated', False)}"
            )
            # POP FIRST. Single outer try wraps audit writes — see
            # _handle_cancel_404 docstring for rationale.
            self._active_orders.pop(asset, None)
            try:
                self._state.mark_order_status(order["order_id"], db_status)
                self._logger.log_order({
                    "action": "maker_canceled",
                    "ticker": order["ticker"],
                    "order_id": order["order_id"],
                    "reason": backstop_reason,
                    "elapsed": round(elapsed, 1),
                    "filled_so_far": filled,
                })
                self._log_fill_model_sample(
                    order, fm_label, cancel_reason=backstop_reason)
                if asset not in self._escalating_assets:
                    self._state.update_evaluated_opportunity_order(
                        order["ticker"], order_outcome=outcome)
            except Exception:
                logging.error(
                    f"force_pop_audit_failed: {order['ticker']} "
                    f"{order['order_id']} — pop complete, audit "
                    f"incomplete.",
                    exc_info=True)
            logging.warning(
                f"force_pop_after_close: {order['ticker']} "
                f"{order['order_id']} remaining={remaining:.1f}s "
                f"filled={filled}/{order['count']} "
                f"reason={backstop_reason} — backstop fired.")
            return None

        # WS fills are already drained (zero API cost) and MUST be applied
        # even when MAKER_POLL_INTERVAL has not elapsed. Pre-fix this loop
        # sat BELOW the poll-interval early-return, so tick() pop_fills()
        # then dropped the batch. REST _check_for_fill recovered the
        # position ~2s later. See kb/failures/ws-fill-poll-interval-drop-sep06.md.
        #
        # Dedup mirrors _check_for_fill: resolve trade_id first, skip if
        # missing or already seen, stamp, then _on_fill. Applying first
        # double-counted REST-then-WS partials. A syn_ key cannot match
        # REST's real trade_id (feed _handle_fill never sets id/price).
        for ws_fill in ws_fills:
            ws_trade_id = ws_fill.get("trade_id") or ws_fill.get("id")
            if not ws_trade_id:
                logging.warning(
                    "WS fill missing trade_id for %s — skipping; REST poll "
                    "will recover",
                    order["ticker"])
                continue
            seen = order.setdefault("_seen_fill_ids", set())
            if ws_trade_id in seen:
                continue
            order["fill_source"] = "websocket"
            self._session_ws_fills += 1
            latency_ms = round((now - order["submit_time"]) * 1000, 1)
            logging.info(
                f"kalshi_ws_fill: {order['ticker']} order={order['order_id']} "
                f"latency={latency_ms}ms")
            # Apply then stamp. Stamp-first blacklisted the id from REST
            # if _on_fill raised (locked DB). Skip-if-seen still prevents
            # REST-then-WS double-count. See Claude MAJOR on PR #177.
            self._on_fill(ws_fill, order)
            seen.add(ws_trade_id)
            if order.get("filled_so_far", 0) >= order["count"]:
                self._active_orders.pop(asset, None)
                self._state.update_evaluated_opportunity_order(
                    order["ticker"], order_outcome="filled")
                return ws_fill
            logging.info(
                f"Partial WS fill — keeping order active "
                f"({order['filled_so_far']}/{order['count']})")

        if now - order["_last_poll"] < MAKER_POLL_INTERVAL:
            return None
        order["_last_poll"] = now

        # Reconcile cancel_pending orders: retry cancel via Kalshi API
        if order.get("cancel_pending"):
            try:
                cancel_resp = self._client.cancel_order(
                    order["order_id"], ticker=order.get("ticker"))
                # 404 sentinel — Kalshi has aged it; route through helper.
                if isinstance(cancel_resp, dict) and cancel_resp.get("_status_code") == 404:
                    self._handle_cancel_404(
                        order, asset, "cancel_pending_retry",
                        source="reconciliation")
                    return None
                if cancel_resp is not None:
                    logging.info(f"cancel_pending resolved: {order['ticker']} cancel succeeded on retry")
                    order.pop("cancel_pending", None)
                    self._state.mark_order_status(order["order_id"], "canceled")
                    self._active_orders.pop(asset, None)
                    self._state.update_evaluated_opportunity_order(
                        order["ticker"], order_outcome="canceled")
                    return None
                # Cancel still failing — check if order was already filled
                fills_resp = self._client.get_fills(ticker=order["ticker"])
                if fills_resp:
                    fills = fills_resp.get("fills", [])
                    for f in fills:
                        if f.get("order_id") == order["order_id"]:
                            logging.info(f"cancel_pending resolved: {order['ticker']} was filled")
                            order.pop("cancel_pending", None)
                            break  # Let normal fill detection handle it below
            except Exception as e:
                logging.error(f"cancel_pending reconciliation error for {order['ticker']}: {e}")

        # 1. Check for maker fill via REST
        fill = self._check_for_fill(order)
        if fill:
            order["fill_source"] = "rest_poll"
            self._session_rest_fills += 1
            self._on_fill(fill, order)
            if order.get("filled_so_far", 0) >= order["count"]:
                self._active_orders.pop(asset, None)
                self._state.update_evaluated_opportunity_order(
                    order["ticker"], order_outcome="filled")
                return fill
            logging.info(
                f"Partial REST fill — keeping order active "
                f"({order['filled_so_far']}/{order['count']})")

        elapsed = now - order["submit_time"]
        remaining = order["seconds_to_close_at_submit"] - elapsed

        # 2. Too close to expiry — cancel, don't escalate
        if remaining < MIN_SECONDS_BEFORE_CLOSE:
            self._cancel_order(asset, "close_approaching")
            return None

        # 2.5 Queue position polling (~every 5s, rate-limit friendly)
        if now - order["_last_queue_poll"] >= 5.0:
            order["_last_queue_poll"] = now
            try:
                qpos = self._client.get_queue_position(order["order_id"])
                if qpos is not None:
                    order["queue_position"] = qpos
                    logging.debug(
                        f"queue_position_check: {order['ticker']} "
                        f"order={order['order_id']} position={qpos}")
            except Exception:
                pass  # Non-critical, don't disrupt flow

        # 3. Escalation: maker waited long enough? (skip if already escalated)
        # Maker-only below 90s: no taker escalation, let maker fill or expire
        if not order.get("escalated") and remaining >= MAKER_ONLY_THRESHOLD:
            # ── Early escalation: ask confirms thesis ──────────────
            current_ask = self._get_addon_best_ask(order["ticker"])
            if current_ask is not None:
                order["_ask_history"].append((now, current_ask))
                ask_move = current_ask - order["price_cents"]
                if ask_move >= 2 and ask_move < EARLY_ESCALATION_MIN_MOVE:
                    # Shadow: log skipped early escalations (2-4c) for data collection
                    if not order.get("_ask_confirmed_skipped_logged"):
                        order["_ask_confirmed_skipped_logged"] = True
                        _skip_candidate = order["candidate"]
                        _skip_count = order["count"]
                        _skip_fee = calculate_taker_fee(_skip_count, current_ask)
                        _skip_edge = _skip_candidate["calibrated_prob"] - (current_ask / 100.0) - (_skip_fee / (_skip_count * 100.0))
                        logging.info(
                            "ask_confirmed_SKIPPED: %s ask=%d¢ (maker=%d¢ +%d¢) "
                            "net_edge=%.4f elapsed=%.1fs threshold=%d¢",
                            order["ticker"], current_ask, order["price_cents"],
                            ask_move, _skip_edge, elapsed, EARLY_ESCALATION_MIN_MOVE)
                if ask_move >= EARLY_ESCALATION_MIN_MOVE:
                    candidate = order["candidate"]
                    cal_prob = candidate["calibrated_prob"]
                    count = order["count"]
                    taker_fee = calculate_taker_fee(count, current_ask)
                    net_edge = cal_prob - (current_ask / 100.0) - (taker_fee / (count * 100.0))
                    if net_edge >= MIN_EDGE_PCT / 100.0:
                        logging.info(
                            "early_escalation_TRIGGER: %s ask=%d¢ (maker=%d¢ +%d¢) "
                            "net_edge=%.4f elapsed=%.1fs",
                            order["ticker"], current_ask, order["price_cents"],
                            ask_move, net_edge, elapsed)
                        return self._escalate_to_taker(order, remaining,
                                                       reason="ask_confirmed")

            # ── Standard time-based escalation (existing code) ─────
            escalation_wait = self._escalation_wait(remaining, asset=order.get("asset", ""))
            # Queue-aware: escalate earlier if deep in queue and time is short
            queue_pos = order.get("queue_position")
            if queue_pos is not None and queue_pos > 20 and remaining < 60:
                escalation_wait = min(escalation_wait, 5.0)
            if elapsed >= escalation_wait:
                return self._escalate_to_taker(order, remaining)

        # 4. Hard timeout fallback
        if elapsed >= MAKER_TIMEOUT_SECONDS:
            self._cancel_order(asset, "timeout")

        return None

    @staticmethod
    def _escalation_wait(remaining: float, asset: str = "") -> float:
        """Urgency-based maker wait before escalating to taker."""
        if remaining >= 180:
            # BTC: shorter wait (7s vs 15s) — ask_confirmed avg 2.7s, slip 3.4c
            if asset == "BTC" and BTC_ESCALATION_WAIT_OVERRIDE is not None:
                return BTC_ESCALATION_WAIT_OVERRIDE
            return ESCALATION_WAIT_LONG     # 15s — ample time, let maker fill
        elif remaining >= 120:
            return ESCALATION_WAIT_MEDIUM   # 7s — 86% of fills happen within 7s
        else:
            return ESCALATION_WAIT_SHORT    # 5s — tight, quick escalation

    # ── Market Intelligence Helpers ───────────────────────────────────────

    @staticmethod
    def _best_ask_depth(ob_data: Dict) -> int:
        """Depth (contracts) at the best YES ask (= highest NO bid level)."""
        no_bids = ob_data.get("no", [])
        if not no_bids:
            return 0
        best_price = -1
        best_qty = 0
        for entry in no_bids:
            if isinstance(entry, (list, tuple)) and len(entry) >= 2:
                price, qty = entry[0], int(entry[1])
            elif isinstance(entry, dict):
                price = entry.get("price", 0)
                qty = int(entry.get("quantity", 0))
            else:
                continue
            if isinstance(price, float) and price < 1.0:
                price_cents = round(price * 100)
            else:
                price_cents = int(price)
            if price_cents > best_price:
                best_price = price_cents
                best_qty = qty
        return best_qty

    @staticmethod
    def _compute_ladder_diag(live_ob) -> Dict:
        """F/U TM_99 zero-fill diagnostic. Extract yes_asks_top and
        no_bid_top (price + qty) from cached orderbook, compute the
        cross-side derivation `100 - no_bid_top` and whether the two
        ladders diverge.

        Distinguishes which hypothesis is right when ETH TM_99 IOCs
        fail to fill at 99c:
          - HYP A: Kalshi's matching engine fills only against the
            explicit yes_asks ladder, not synthetic cross-side. If
            yes_asks_top > our bid AND ladders diverge, our IOC
            can't cross.
          - HYP B: Kalshi matches both ladders, but no_bid is too
            thin and gets sniped before our IOC arrives.
        Logged at IOC submit. Pure observability — no behavior change.

        Returns: {yes_ask_top_price, yes_ask_top_qty, no_bid_top_price,
                  no_bid_top_qty, cross_side_ask, diverges}
        See kb/failures (when written).
        """
        out = {
            "yes_ask_top_price": None, "yes_ask_top_qty": 0,
            "no_bid_top_price": None, "no_bid_top_qty": 0,
            "cross_side_ask": None, "diverges": False,
            "one_side_empty": False,
        }
        if not live_ob:
            return out

        def _to_cents(p):
            # Handle: int (already cents), float < 1.0 (dollar format,
            # e.g. 0.99 = 99c), float in [1.0, 100.0] (could be dollar
            # 1.00 = 100c OR cents 1.0 = 1c — Kalshi never sends
            # "1.00 dollars" for binary 0-100c contracts, so treat as
            # cents), string (cast through float first — schema drift
            # defense, see MEMORY: feedback_kalshi_schema_drift).
            try:
                if isinstance(p, str):
                    p = float(p)
                if isinstance(p, float) and 0 < p < 1.0:
                    return round(p * 100)
                return int(p)
            except (TypeError, ValueError):
                return None

        # yes_asks: pick LOWEST price (best ask for buyer).
        for entry in (live_ob.get("yes") or []):
            if not (isinstance(entry, (list, tuple)) and len(entry) >= 2):
                continue
            p = _to_cents(entry[0])
            if p is None:
                continue
            try:
                q = int(entry[1])
            except (TypeError, ValueError):
                q = 0
            if (out["yes_ask_top_price"] is None
                    or p < out["yes_ask_top_price"]):
                out["yes_ask_top_price"] = p
                out["yes_ask_top_qty"] = q

        # no_bids: pick HIGHEST price (best NO bid → best cross-side).
        for entry in (live_ob.get("no") or []):
            if not (isinstance(entry, (list, tuple)) and len(entry) >= 2):
                continue
            p = _to_cents(entry[0])
            if p is None:
                continue
            try:
                q = int(entry[1])
            except (TypeError, ValueError):
                q = 0
            if (out["no_bid_top_price"] is None
                    or p > out["no_bid_top_price"]):
                out["no_bid_top_price"] = p
                out["no_bid_top_qty"] = q

        if out["no_bid_top_price"] is not None:
            out["cross_side_ask"] = 100 - out["no_bid_top_price"]

        if (out["yes_ask_top_price"] is not None
                and out["cross_side_ask"] is not None):
            out["diverges"] = (
                out["yes_ask_top_price"] != out["cross_side_ask"])

        # one_side_empty: tri-state signal for grep — captures the
        # case where one ladder is missing entirely. R-review [A4]:
        # `diverges=False + one_side_empty=False` means real
        # alignment; `diverges=False + one_side_empty=True` means
        # uninformative. Don't conflate.
        out["one_side_empty"] = (
            (out["yes_ask_top_price"] is None)
            != (out["no_bid_top_price"] is None)
        )

        return out

    @staticmethod
    def _pick_ioc_limit_for_depth(
            ob_data: Dict,
            best_yes_ask: int,
            target_qty: int,
            max_bump_cents: int,
            edge_ceiling_price: int,
            max_price: int = 99) -> int:
        """Walk the orderbook from `best_yes_ask` upward, return the
        smallest YES limit price where cumulative fillable depth
        meets `target_qty`. Hard-capped at:
          - `best_yes_ask + max_bump_cents` (operational ceiling)
          - `edge_ceiling_price` (EV ceiling — caller computes
            from `floor(calibrated_prob*100) - fee - reserve_cents`,
            where reserve_cents is per-strategy
            (STRATEGY_LIMIT_BUMP_RESERVE_CENTS, default 0). At
            limit = ceiling, worst-case fill has edge = reserve.
            Default reserve=0 means break-even after fee on worst
            fill; aggressive overrides (-1) tolerate ~1c negative
            edge on worst fill. NOTE: this no longer respects
            MIN_EDGE_PCT — that floor was a SCAN-time gate, not a
            submit-time gate. The submit gate uses per-strategy
            reserve directly.)
          - `max_price` (defaults to MAX_ENTRY_PRICE = 99)

        If no level inside the cap delivers `target_qty`, returns
        the highest level inside the cap (still better than
        best_yes_ask alone — Kalshi auto-cancels surplus at $0).

        If the orderbook has no fillable depth at any level inside
        the cap, returns `best_yes_ask` unchanged (caller will
        discover empty book via PHANTOM_ABORT).

        WHY (Apr 25 2026):
        Pre-Apr 23 the WS schema bug masked the orderbook → bot
        fell back to NBBO yes_ask (typically wider than orderbook
        best_ask) → IOC swept multiple price levels → 64-82ct
        avg fills. Post-Apr 23 fix made the bot use orderbook
        best_ask exactly → matches only top-of-book → 33ct avg.
        Liquidity didn't disappear — just sat 1-3c above our
        IOC limit. Production sample: 1ct at 66c, 151ct at 69c.
        This helper restores access to the deep level when the
        candidate's edge can absorb the bump.

        Mechanics: Kalshi orderbooks store YES asks via the NO
        bid stack — NO bid at price P = YES ask at (100 - P).
        We walk YES ask prices ascending from best_yes_ask, sum
        qty, return first price where cumul ≥ target.

        Sub-floor levels (YES asks below best_yes_ask) are NOT
        included in the walk because the picker only chooses
        the LIMIT, not the fill source — Kalshi's matching engine
        will sweep sub-floor asks at any limit ≥ them, but that's
        Variant B behavior intentional under the no_clamp policy."""
        # Caller's edge_ceiling_price might be below best_yes_ask
        # (defensive — candidate shouldn't have been generated, but
        # never return a price below best_yes_ask).
        if max_bump_cents <= 0 or target_qty <= 0:
            return best_yes_ask
        cap = min(
            best_yes_ask + max_bump_cents,
            max(edge_ceiling_price, best_yes_ask),
            max_price,
        )
        if cap < best_yes_ask:
            return best_yes_ask
        # Build the YES-ask ladder from the NO bid stack, filter to
        # prices in [best_yes_ask, cap], sort ascending.
        no_bids = ob_data.get("no") or []
        levels: list = []
        for entry in no_bids:
            if isinstance(entry, (list, tuple)) and len(entry) >= 2:
                price, qty = entry[0], entry[1]
            elif isinstance(entry, dict):
                price = entry.get("price", 0)
                qty = entry.get("quantity", 0)
            else:
                continue
            try:
                qty_int = int(qty)
            except (TypeError, ValueError):
                continue
            if qty_int <= 0:
                continue
            # Normalize price to cents.
            if isinstance(price, float) and price < 1.0:
                price_cents = round(price * 100)
            else:
                try:
                    price_cents = int(price)
                except (TypeError, ValueError):
                    continue
            yes_ask = 100 - price_cents
            if best_yes_ask <= yes_ask <= cap:
                levels.append((yes_ask, qty_int))
        if not levels:
            return best_yes_ask
        levels.sort()  # ascending YES price
        cumul = 0
        for yes_ask, qty in levels:
            cumul += qty
            if cumul >= target_qty:
                return yes_ask
        # Walked everything inside cap without hitting target.
        # Return highest level we reached — better than best_ask.
        return levels[-1][0]

    @staticmethod
    def _total_ob_depth(ob_data: Dict) -> int:
        """Total depth (contracts) across all orderbook levels."""
        total = 0
        for side in ("no", "yes"):
            for entry in (ob_data.get(side) or []):
                if isinstance(entry, (list, tuple)) and len(entry) >= 2:
                    total += int(entry[1])
                elif isinstance(entry, dict):
                    total += int(entry.get("quantity", 0))
        return total

    @staticmethod
    def _best_yes_bid(ob_data: Dict) -> Optional[int]:
        """Highest YES bid price in cents."""
        yes_bids = ob_data.get("yes", [])
        best = None
        for entry in yes_bids:
            if isinstance(entry, (list, tuple)) and len(entry) >= 2:
                price = entry[0]
            elif isinstance(entry, dict):
                price = entry.get("price", 0)
            else:
                continue
            price_cents = round(price * 100) if isinstance(price, float) and price < 1.0 else int(price)
            if best is None or price_cents > best:
                best = price_cents
        return best

    @staticmethod
    def _best_yes_bid_depth(ob_data: Dict) -> int:
        """Depth at the highest YES bid."""
        yes_bids = ob_data.get("yes", [])
        best_price = -1
        best_qty = 0
        for entry in yes_bids:
            if isinstance(entry, (list, tuple)) and len(entry) >= 2:
                price, qty = entry[0], int(entry[1])
            elif isinstance(entry, dict):
                price = entry.get("price", 0)
                qty = int(entry.get("quantity", 0))
            else:
                continue
            price_cents = round(price * 100) if isinstance(price, float) and price < 1.0 else int(price)
            if price_cents > best_price:
                best_price = price_cents
                best_qty = qty
        return best_qty

    @staticmethod
    def _extract_book_levels(ob_data: Optional[Dict], n: int = 10) -> Optional[str]:
        """Top-N YES-side ladder as compact JSON for forensic logging.

        Returns: '{"yes_bids":[[p,q],...],"yes_asks":[[p,q],...]}' or None.
        yes_bids sorted desc by price (best bid first).
        yes_asks derived from raw NO bids via 100-p, sorted asc (best ask first).

        Input contract:
        - ob_data must be a coalesced book (dict), not a delta frame.
          Non-dict input (None, list, str) returns None.
        - Float price <= 1.0 treated as probability (× 100 → cents).
          Float price > 1.0 treated as already-cents.

        Dropped (silently): NaN/Inf/negative/missing/bool qty,
        price < 0 or > 100, bool price, malformed entry shapes,
        duplicate price levels are merged (sum qty).
        """
        if not isinstance(ob_data, dict):
            return None

        def _parse_and_merge(entries):
            """Parse entries to {price_cents: total_qty} dict, merging duplicates."""
            out: Dict[int, int] = {}
            for entry in entries or []:
                if isinstance(entry, (list, tuple)):
                    if len(entry) < 2:
                        continue
                    price, qty = entry[0], entry[1]
                elif isinstance(entry, dict):
                    if "quantity" not in entry:
                        continue
                    price = entry.get("price")
                    qty = entry.get("quantity")
                else:
                    continue
                # Reject bools (subclass of int — silently poisons output)
                if isinstance(price, bool) or isinstance(qty, bool):
                    continue
                try:
                    if isinstance(qty, float) and not math.isfinite(qty):
                        continue
                    qty_int = int(qty)
                    if qty_int <= 0:
                        continue
                    if isinstance(price, float):
                        if not math.isfinite(price):
                            continue
                        if price <= 1.0:
                            price_cents = round(price * 100)
                        else:
                            price_cents = int(price)
                    else:
                        price_cents = int(price)
                except (TypeError, ValueError, OverflowError):
                    continue
                if price_cents < 0 or price_cents > 100:
                    continue
                out[price_cents] = out.get(price_cents, 0) + qty_int
            return out

        yes_bids_merged = _parse_and_merge(ob_data.get("yes"))
        no_bids_merged = _parse_and_merge(ob_data.get("no"))

        yes_bids = heapq.nlargest(n, yes_bids_merged.items(), key=lambda kv: kv[0])
        # NO bid >= 100c → derived YES ask <= 0, drop as nonsensical
        yes_asks_iter = ((100 - p, q) for p, q in no_bids_merged.items() if p < 100)
        yes_asks = heapq.nsmallest(n, yes_asks_iter, key=lambda pq: pq[0])

        return json.dumps(
            {"yes_bids": [[p, q] for p, q in yes_bids],
             "yes_asks": [[p, q] for p, q in yes_asks]},
            separators=(",", ":"),
        )

    # ── Repricing ─────────────────────────────────────────────────────────

    def _reprice_maker(self, new_price: int) -> bool:
        """Amend maker order to a new price. Returns True on success."""
        if self._active_order is None:
            return False
        order = self._active_order
        self._session_amend_attempts += 1
        try:
            _side = order.get("side", "yes")
            _price_kwarg = {"no_price": new_price} if _side == "no" else {"yes_price": new_price}
            resp = self._client.amend_order(
                order_id=order["order_id"], ticker=order["ticker"],
                side=_side, action="buy", count=order["count"],
                **_price_kwarg)
            if resp is None:
                logging.warning(
                    f"amend_failed_fallback: {order['ticker']} "
                    f"old={order['price_cents']}¢ new={new_price}¢")
                return False
            old_price = order["price_cents"]
            order["price_cents"] = new_price
            self._session_amend_successes += 1
            logging.info(
                f"amend_success: {order['ticker']} "
                f"{old_price}¢ → {new_price}¢ order={order['order_id']}")
            return True
        except Exception:
            logging.warning("Amend failed with exception", exc_info=True)
            return False

    def _escalate_to_taker(self, order: Dict, remaining: float,
                           reason: str = "escalation_wait") -> Optional[Dict]:
        """Escalate maker to taker via cancel-replace IOC."""
        ticker = order["ticker"]
        asset = order["asset"]
        self._escalating_assets.add(asset)
        try:
            return self._escalate_to_taker_inner(order, remaining, reason)
        finally:
            self._escalating_assets.discard(asset)

    def _escalate_to_taker_inner(self, order: Dict, remaining: float,
                                  reason: str = "escalation_wait") -> Optional[Dict]:
        """Inner escalation logic (guarded by _escalating_assets)."""
        ticker = order["ticker"]
        elapsed = time.time() - order["submit_time"]

        # Re-fetch orderbook for current best ask
        ob_raw = self._client.get_orderbook(ticker, depth=5)
        if ob_raw is None:
            logging.warning(f"Escalation aborted: orderbook fetch failed for {ticker}")
            self._cancel_order(order["asset"], reason)
            self._state.update_evaluated_opportunity_order(ticker, order_outcome="canceled")
            return None

        # Unwrap response envelope (same as _get_orderbook_cached)
        ob_fp = ob_raw.get("orderbook_fp") if ob_raw else None
        if ob_fp:
            ob_data = convert_orderbook_fp(ob_fp)
        else:
            ob_data = ob_raw.get("orderbook") or ob_raw

        best_ask = best_yes_ask_cents(ob_data)
        if best_ask is None:
            logging.warning(f"Escalation aborted: no asks on orderbook for {ticker}")
            self._cancel_order(order["asset"], reason)
            self._state.update_evaluated_opportunity_order(ticker, order_outcome="canceled")
            return None

        # Per-asset price floor for escalation (mirrors scanner + maker)
        _esc_floor = MIN_ENTRY_PRICE
        _esc_asset = order.get("asset")
        if _esc_asset == "BTC":
            _esc_floor = BTC_MIN_ENTRY_PRICE
        elif _esc_asset == "ETH":
            _esc_floor = ETH_MIN_ENTRY_PRICE
        elif _esc_asset == "SOL":
            _esc_floor = SOL_MIN_ENTRY_PRICE
        elif _esc_asset == "XRP":
            _esc_floor = XRP_MIN_ENTRY_PRICE
        elif _esc_asset == "HYPE":
            _esc_floor = HYPE_MIN_ENTRY_PRICE
        elif _esc_asset == "DOGE":
            _esc_floor = DOGE_MIN_ENTRY_PRICE
        elif _esc_asset == "BNB":
            _esc_floor = BNB_MIN_ENTRY_PRICE
        if best_ask < _esc_floor or best_ask > ESCALATION_MAX_ENTRY:
            logging.warning(
                f"Escalation aborted: price {best_ask}¢ out of range "
                f"[{_esc_floor}-{ESCALATION_MAX_ENTRY}¢] for {ticker}"
            )
            self._cancel_order(order["asset"], reason)
            self._state.update_evaluated_opportunity_order(ticker, order_outcome="canceled")
            return None

        # Determine urgency tier for logging
        if remaining >= 180:
            tier = "long"
        elif remaining >= 120:
            tier = "medium"
        else:
            tier = "short"

        price_slip = best_ask - order["price_cents"]
        self._logger.log_order({
            "action": "escalate_to_taker",
            "reason": reason,
            "ticker": ticker,
            "maker_price": order["price_cents"],
            "taker_price": best_ask,
            "price_slip": price_slip,
            "wait_time": round(elapsed, 1),
            "urgency_tier": tier,
            "remaining": round(remaining, 1),
            "execution_method": "cancel_replace_ioc",
        })

        # ── Edge recheck at escalated price ──────────────────────────
        # The candidate was evaluated with edge at the maker price. If the
        # taker price is higher, the edge may have evaporated or gone negative.
        # Data: 10 MAKER_PATIENT losses with drift>0 cost $704/2wk.
        _esc_prob = order["candidate"].get("calibrated_prob", 0)
        _esc_fee_1c = calculate_taker_fee(1, best_ask)
        _esc_edge = _esc_prob - best_ask / 100.0 - _esc_fee_1c / 100.0
        _esc_maker_price = order["price_cents"]
        if _esc_edge < 0 and best_ask > _esc_maker_price:
            logging.warning(
                "ESCALATION_EDGE_ABORT: %s prob=%.4f price=%d→%dc edge=%.4f "
                "(negative at escalated price, canceling)",
                ticker, _esc_prob, _esc_maker_price, best_ask, _esc_edge)
            self._cancel_order(order["asset"], "escalation_edge_abort")
            self._state.update_evaluated_opportunity_order(
                ticker, order_outcome="escalation_edge_abort")
            return None

        # Cancel maker + submit taker IOC
        logging.info(
            f"escalation_cancel_replace: {ticker} "
            f"(maker={order['price_cents']}¢ → taker={best_ask}¢ edge={_esc_edge:.4f})")
        cancel_ok = self._cancel_order(order["asset"], reason)
        if not cancel_ok:
            logging.error(f"Cancel failed for {ticker} — NOT submitting taker to prevent double position")
            self._state.update_evaluated_opportunity_order(ticker, order_outcome="canceled")
            return None

        # Build modified candidate with fresh best ask
        candidate = dict(order["candidate"])
        candidate["best_yes_ask"] = best_ask
        candidate["entry_path"] = "escalation_ioc"
        candidate["escalation_type"] = reason
        candidate["maker_price_cents"] = order["price_cents"]
        candidate["maker_wait_seconds"] = round(elapsed, 1)
        # Sprint B Bit B.2b — escalation snapshot. The candidate dict
        # was forked from order["candidate"] which carries the original
        # decision_id seeded by execute() at maker tier-1 routing time
        # — so this 'escalate' row shares decision_id with the
        # maker_first row written ~15s earlier. A learner joining on
        # decision_id reconstructs the full route sequence. Belt-and-
        # braces: ensure decision_id is set even if order was forged
        # in a code path that bypassed execute() seeding.
        if not candidate.get("decision_id"):
            candidate["decision_id"] = uuid.uuid4().hex
        self._emit_decision_snapshot(candidate, "escalate")
        filled = order.get("filled_so_far", 0)
        if filled > 0:
            candidate["position_size"] = max(1, candidate["position_size"] - filled)
            logging.info(
                f"escalation_partial_adjust: {ticker} "
                f"original={order['count']} filled={filled} "
                f"ioc_count={candidate['position_size']}")
        self._recent_taker_tickers[ticker] = time.time()

        result = self._submit_taker(candidate)
        if result is not None:
            _esc_oid = result.get("order_id") if isinstance(result, dict) else None
            self._state.update_evaluated_opportunity_order(
                ticker, order_id=_esc_oid, order_outcome="filled")
        else:
            self._state.update_evaluated_opportunity_order(
                ticker, order_outcome="unfilled")
        return result

    # ── DC Taker with Non-Blocking Retry Queue ─────────────────────────

    @staticmethod
    def _dc_retry_delay(seconds_to_close: float) -> float:
        """Adaptive retry delay based on urgency (STC). Shorter near settlement."""
        if seconds_to_close > 600:
            return 20.0
        elif seconds_to_close > 300:
            return 12.0
        elif seconds_to_close > 120:
            return 6.0
        elif seconds_to_close > 30:
            return 3.0
        else:
            return 1.0

    def _rest_best_ask_depth(self, ticker: str) -> Optional[int]:
        """Force-fetch best YES ask depth via REST /orderbook.

        Used as a pre-IOC drift check: the WS cache can diverge from
        Kalshi's real book (see kb/failures/kalshi-ws-schema-drift.md
        § "WS delta underflow"). REST is the ground truth. Returns None
        on any error so caller falls back to cached depth.

        Adds ~30-50ms latency per call. Should only be invoked from IOC
        submit paths where cache claims non-trivial depth — see
        IOC_DRIFT_CHECK_MIN_CACHED_DEPTH.

        Note: callers should prefer `_rest_best_ask_depth_smoothed`
        which records this single sample into the rolling-window
        buffer and returns the peak across recent observations.
        Single REST samples are themselves volatile — see
        WS_DRIFT_PROBE_REST_STABILITY.
        """
        try:
            ob_resp = self._client.get_orderbook(ticker, depth=5)
            if not ob_resp:
                return None
            ob_fp = ob_resp.get("orderbook_fp")
            if ob_fp and self._ml and hasattr(self._ml, 'scanner'):
                fresh_ob = convert_orderbook_fp(ob_fp)
            else:
                fresh_ob = ob_resp.get("orderbook")
            if not fresh_ob:
                return None
            return OrderExecutor._best_ask_depth(fresh_ob)
        except Exception:
            return None

    # Sanity bound for a recorded depth value. Kalshi best-ask
    # depths are typically <50k contracts; rejecting anything past
    # 100k catches realistic schema-drift bugs (e.g., REST returning
    # sum-of-levels rather than top-of-book = ~10–50× inflation).
    # Round 1 P1 + Round 2 [A5] tightening — 1M was too loose to
    # catch any plausible bug class.
    _REST_DEPTH_SAMPLE_MAX = 100_000

    # Minimum samples in the rolling window before the smoothed peak
    # is trusted as the clamp authority. With only 1 sample, the
    # smoothed helper degenerates to single-sample clamping — the
    # exact pre-fix bug. Round 2 [A1] cold-start gate: if the buffer
    # has <2 samples within the window, the drift-check skips the
    # clamp altogether and falls through to the existing policy
    # (cached `_ask_depth` + STRATEGY_CLAMP_POLICY), which is the
    # behavior that worked for months pre-a56ecc7. PHANTOM_ABORT
    # still fires on fresh=0 regardless of cold-start state.
    _REST_DEPTH_MIN_SAMPLES_FOR_CLAMP = 2

    # Threading: `_rest_depth_observations` is read/written ONLY from
    # the main thread (executor's `_submit_taker` and helpers). No
    # WS thread, refresh worker, or engine thread touches it today.
    # If a future engine ever calls into `_submit_taker` from a
    # different thread, wrap the deque ops in a lock — `popleft` is
    # atomic individually but the prune-then-append pattern in
    # `_record_rest_depth_observation` is not. (Round 2 [A3].)

    def _record_rest_depth_observation(self, ticker: str,
                                       depth: int) -> None:
        """Append (monotonic_now, depth) to the per-ticker rolling
        buffer and prune any samples older than
        IOC_DRIFT_CHECK_REST_WINDOW_S. Drops the dict entry entirely
        when the deque becomes empty after prune — bounds memory
        growth across the lifetime of the process (15M markets
        cycle every 15 min × 4 assets = ~16/hr new tickers; without
        cleanup the dict would leak indefinitely).

        Round 1 hardening:
          - `time.monotonic()` (not `time.time()`) — wall-clock NTP
            jumps backwards corrupt window math; the VPS has been
            logging 5–14s clock_drift_detected warnings every 30s.
          - Bounds-check on `depth`: out-of-bound values (negative
            or > _REST_DEPTH_SAMPLE_MAX) are dropped AND a
            once-per-ticker WARNING fires for diagnostics. Round 2
            [A7]: silent drop with no observability would mask a
            schema-drift bug that produced consistently-bad samples."""
        if depth is None or depth < 0 or depth > self._REST_DEPTH_SAMPLE_MAX:
            # Round 2 [A7]: log once per ticker so operators see a
            # signal if schema drift is poisoning the buffer.
            if not hasattr(self, "_rest_depth_drop_logged"):
                self._rest_depth_drop_logged = set()
            if ticker not in self._rest_depth_drop_logged:
                self._rest_depth_drop_logged.add(ticker)
                logging.warning(
                    "REST_DEPTH_SAMPLE_DROPPED: %s depth=%r — out of "
                    "bounds [0, %d]; smoothing buffer not updated. "
                    "Possible schema drift in REST /orderbook.",
                    ticker, depth, self._REST_DEPTH_SAMPLE_MAX)
            return
        now = time.monotonic()
        cutoff = now - IOC_DRIFT_CHECK_REST_WINDOW_S
        buf = self._rest_depth_observations.get(ticker)
        if buf is None:
            buf = deque()
            self._rest_depth_observations[ticker] = buf
        # Prune expired samples from the left.
        while buf and buf[0][0] < cutoff:
            buf.popleft()
        buf.append((now, int(depth)))

    def _rest_depth_window_count(self, ticker: str) -> int:
        """Return the number of unexpired samples in the per-ticker
        buffer. Used as the cold-start gate — when count < 2 the
        smoothed peak is just a rename of the single fresh sample
        and provides no actual smoothing. Round 2 [A1].
        Side-effect-free."""
        now = time.monotonic()
        cutoff = now - IOC_DRIFT_CHECK_REST_WINDOW_S
        buf = self._rest_depth_observations.get(ticker)
        if not buf:
            return 0
        return sum(1 for ts, _ in buf if ts >= cutoff)

    def _rest_depth_window_max(self, ticker: str) -> Optional[int]:
        """Return the peak depth observed for `ticker` within the
        last IOC_DRIFT_CHECK_REST_WINDOW_S seconds, or None if no
        samples are in the window. Pure read — does not record.

        Round 1 hardening: prunes expired samples in-place and
        DELETES the dict entry when its deque becomes empty. This
        bounds memory growth (settled tickers stop sending samples,
        their deque ages out, then this read drops the entry).

        Peak (not mean/median) is the right statistic for this
        clamp because:
          - real phantom WS → REST stays consistently low → peak stays low
          - transient REST noise → some samples high, some low →
            peak preserves the high reading and avoids false-clamp"""
        now = time.monotonic()
        cutoff = now - IOC_DRIFT_CHECK_REST_WINDOW_S
        buf = self._rest_depth_observations.get(ticker)
        if not buf:
            return None
        # Prune expired samples in-place so memory is reclaimed.
        while buf and buf[0][0] < cutoff:
            buf.popleft()
        if not buf:
            # All samples expired — drop the dict entry entirely.
            del self._rest_depth_observations[ticker]
            return None
        return max(d for _, d in buf)

    def _rest_best_ask_depth_smoothed(
            self, ticker: str) -> Tuple[Optional[int], Optional[int]]:
        """REST best-ask depth, smoothed over a rolling window.

        Returns a 2-tuple `(peak, fresh)`:
          - `peak`: max depth observed within
            IOC_DRIFT_CHECK_REST_WINDOW_S, after recording the
            fresh sample. Used as the clamp authority for IOC
            sizing — peak protects against single-sample REST
            volatility (delta_qty=-900 in 1s).
          - `fresh`: the just-fetched REST sample (or None on
            REST error). Required for PHANTOM_ABORT decisions —
            when fresh==0, the book IS empty right now regardless
            of historical peak, so the abort path must check fresh
            independently. Round 1 [P0 #2] regression guard.

        If REST errors, fresh=None and peak falls back to the
        historical buffer (or None if both are absent). The caller
        falls back to cached depth in that case — existing behavior."""
        fresh = self._rest_best_ask_depth(ticker)
        if fresh is not None:
            self._record_rest_depth_observation(ticker, fresh)
        peak = self._rest_depth_window_max(ticker)
        return peak, fresh

    def _dc_get_ask_with_depth(self, ticker: str, candidate: Dict):
        """Get best ask price AND depth for DC execution decisions.

        Returns (price, depth, source) where:
        - price: best YES ask in cents, or None
        - depth: contracts at best ask level, 0 if unknown
        - source: 'orderbook' or 'market_nbbo'
        """
        try:
            scanner = self._ml.scanner if self._ml else None
            if scanner:
                ob_data, _ = scanner._get_orderbook_cached(ticker)
                if ob_data:
                    price = best_yes_ask_cents(ob_data)
                    if price is not None:
                        depth = OrderExecutor._best_ask_depth(ob_data)
                        return price, depth, "orderbook"
        except Exception:
            pass

        # REST fallback
        try:
            ob_resp = self._client.get_orderbook(ticker, depth=5)
            if ob_resp:
                ob_fp = ob_resp.get("orderbook_fp")
                if ob_fp and self._ml and hasattr(self._ml, 'scanner'):
                    ob_data = convert_orderbook_fp(ob_fp)
                else:
                    ob_data = ob_resp.get("orderbook", ob_resp)
                if ob_data:
                    price = best_yes_ask_cents(ob_data)
                    if price is not None:
                        depth = OrderExecutor._best_ask_depth(ob_data)
                        return price, depth, "orderbook"
        except Exception:
            pass

        # NBBO fallback
        nbbo_price = self._nbbo_fallback_price(candidate)
        if nbbo_price is not None:
            return nbbo_price, 0, "market_nbbo"

        return None, 0, "none"

    def _execute_dc_taker(self, candidate: Dict, asset: str, seconds_to_close) -> Optional[Dict]:
        """Submit DC IOC. On unfilled/partial, queue non-blocking retry."""
        _dc_strategy = candidate.get("strategy")
        ticker = candidate["ticker"]
        count = candidate["position_size"]
        price = candidate["best_yes_ask"]
        cal_prob = candidate["calibrated_prob"]
        # Sprint B Bit B.2b — decision snapshot at route choice.
        self._emit_decision_snapshot(candidate, "taker_first")

        if count <= 0:
            logging.warning("ORDER_SUPPRESSED zero_size: %s asset=%s strategy=%s price=%d",
                            ticker, asset, _dc_strategy, price)
            self._session_suppressed_zero_size += 1
            return None

        taker_fee = calculate_taker_fee(count, price)
        net_edge = cal_prob - (price / 100.0) - (taker_fee / (count * 100.0))

        if net_edge < -0.01:
            logging.warning(
                "ORDER_SUPPRESSED edge_taker_fee: %s asset=%s strategy=%s net_edge=%.4f < -0.01 "
                "price=%d¢ cal_prob=%.4f taker_fee=%d¢",
                ticker, asset, _dc_strategy, net_edge, price, cal_prob, taker_fee)
            self._session_suppressed_edge_recalc += 1
            return None

        # Fresh ask check with depth — verify price, depth, and source
        _dc_scan_price = price  # preserve original scan price for drift check
        fresh_ask, fresh_depth, fresh_source = self._dc_get_ask_with_depth(ticker, candidate)

        if fresh_ask is None:
            logging.warning("ORDER_SUPPRESSED no_asks: %s asset=%s strategy=%s price=%d stc=%.0f",
                            ticker, asset, _dc_strategy, price, seconds_to_close or 0)
            self._session_suppressed_no_asks += 1
            if self._ml and hasattr(self._ml, "scanner"):
                self._ml.scanner._dc_skip_cooldown[ticker] = time.time() + 60
            # Queue retry — book may appear later
            _retry_delay = self._dc_retry_delay(seconds_to_close or 0)
            self._dc_retry_queue.append({
                "candidate": candidate.copy(),
                "original_count": count,
                "total_filled": 0,
                "remaining": count,
                "attempt": 1,
                "next_retry_ts": time.time() + _retry_delay,
                "strategy": _dc_strategy,
                "original_price": _dc_scan_price,
                "_queue_ts": time.time(),
            })
            logging.info("dc_retry_QUEUED: %s %s no_asks attempt=1/%d next_retry=%.0fs",
                         _dc_strategy, ticker, 1 + DC_IOC_MAX_RETRIES, _retry_delay)
            return None

        # Layer 1: Phantom depth flag — LOG ONLY, never block
        # Depth can appear between our check and the IOC hitting the matching engine.
        # Blocking here would kill real fills. Flag for analysis, submit IOC regardless.
        _phantom_depth = (fresh_depth == 0 and fresh_source == "market_nbbo")
        if _phantom_depth:
            logging.info("dc_taker_PHANTOM_FLAG: %s %s fresh_ask=%d¢ depth=0 source=nbbo — submitting anyway",
                         _dc_strategy, ticker, fresh_ask)

        # Price floor gate: refuse if fresh ask dropped below DC qualifying floor
        if fresh_ask < DECIDED_CONTRACT_MIN_PRICE:
            logging.warning("dc_taker_ABORT_PRICE_BELOW_FLOOR: %s fresh_ask=%d¢ < floor=%d¢ (scan=%d¢)",
                            ticker, fresh_ask, DECIDED_CONTRACT_MIN_PRICE, _dc_scan_price)
            return None

        if fresh_ask != price:
            logging.info("dc_taker_price_update: %s scanner=%d¢ fresh=%d¢ depth=%d src=%s",
                         ticker, price, fresh_ask, fresh_depth, fresh_source)
            price = fresh_ask
            candidate["best_yes_ask"] = fresh_ask
            taker_fee = calculate_taker_fee(count, price)
            net_edge = cal_prob - (price / 100.0) - (taker_fee / (count * 100.0))
            if net_edge < -0.01:
                logging.info("dc_taker_SKIPPED: %s fresh_ask=%d¢ net_edge=%.4f < -0.01",
                             ticker, price, net_edge)
                return None

        self._session_direct_taker_attempts += 1
        candidate["entry_path"] = "dc_taker"
        candidate["escalation_type"] = "direct_taker"
        self._recent_taker_tickers[ticker] = time.time()

        logging.info(
            "dc_taker_ENTRY: %s %s %dx @ %d¢ "
            "seconds_to_close=%.0f net_edge=%.4f cal_prob=%.4f taker_fee=%d¢ attempt=1/%d",
            _dc_strategy, ticker, count, price,
            seconds_to_close or 0, net_edge, cal_prob, taker_fee, 1 + DC_IOC_MAX_RETRIES)

        _order_submit_ts = datetime.datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        result = self._submit_taker(candidate)

        if result is not None:
            fill_count = result.get("filled_count", 0)
            remaining = count - fill_count

            if remaining <= 0:
                # Fully filled on first attempt
                self._session_direct_taker_fills += 1
                logging.info("dc_taker_FILLED: %s %s %d/%d", _dc_strategy, ticker, fill_count, count)
                _taker_oid = result.get("order_id") if isinstance(result, dict) else None
                self._state.update_evaluated_opportunity_order(
                    ticker, order_id=_taker_oid,
                    order_submitted_at=_order_submit_ts, order_outcome="filled")
                return result

            # Partial fill — queue retry for remaining
            logging.info("dc_taker_PARTIAL: %s %s filled=%d remaining=%d — queuing retry",
                         _dc_strategy, ticker, fill_count, remaining)
            self._dc_retry_queue.append({
                "candidate": candidate.copy(),
                "original_count": count,
                "total_filled": fill_count,
                "remaining": remaining,
                "attempt": 1,
                "next_retry_ts": time.time() + DC_IOC_RETRY_DELAY,
                "strategy": _dc_strategy,
                "original_price": _dc_scan_price,
                "_queue_ts": time.time(),
                "last_order_submit_ts": _order_submit_ts,
                "last_order_id": result.get("order_id"),
            })
            # Return result so the partial fill is tracked
            self._state.update_evaluated_opportunity_order(
                ticker, order_id=result.get("order_id"),
                order_submitted_at=_order_submit_ts, order_outcome="partial_retry")
            return result
        else:
            # Zero fill — queue retry
            self._session_direct_taker_unfilled += 1
            logging.warning("dc_taker_UNFILLED: %s %s — queuing retry", _dc_strategy, ticker)
            self._dc_retry_queue.append({
                "candidate": candidate.copy(),
                "original_count": count,
                "total_filled": 0,
                "remaining": count,
                "attempt": 1,
                "next_retry_ts": time.time() + DC_IOC_RETRY_DELAY,
                "strategy": _dc_strategy,
                "original_price": _dc_scan_price,
                "_queue_ts": time.time(),
                "last_order_submit_ts": _order_submit_ts,
            })
            self._state.update_evaluated_opportunity_order(
                ticker, order_submitted_at=_order_submit_ts,
                order_outcome="unfilled_retry")
            return None

    def _execute_tm_taker(self, candidate: Dict, asset: str, seconds_to_close) -> Optional[Dict]:
        """Execute terminal momentum trade — direct taker, fixed contracts, no retry."""
        ticker = candidate["ticker"]
        count = candidate["position_size"]  # scan-time: tm_compute_contracts(price, stc, balance)
        price = candidate["best_yes_ask"]
        cal_prob = candidate["calibrated_prob"]
        # Sprint B Bit B.2b — decision snapshot at route choice.
        self._emit_decision_snapshot(candidate, "taker_first")

        if count <= 0:
            logging.warning("ORDER_SUPPRESSED zero_size: %s asset=%s strategy=terminal_momentum price=%d",
                            ticker, asset, price)
            self._session_suppressed_zero_size += 1
            return None

        taker_fee = calculate_taker_fee(count, price)
        net_edge = cal_prob - (price / 100.0) - (taker_fee / (count * 100.0))

        # Fresh ask check — verify price hasn't moved outside TM_PRICE_SET
        _tm_scan_price = price
        fresh_ask, fresh_depth, fresh_source = self._dc_get_ask_with_depth(ticker, candidate)

        if fresh_ask is None:
            logging.warning("ORDER_SUPPRESSED no_asks: %s asset=%s strategy=terminal_momentum price=%d stc=%.0f",
                            ticker, asset, price, seconds_to_close or 0)
            self._session_suppressed_no_asks += 1
            return None

        if fresh_ask not in TM_PRICE_SET:
            logging.info("tm_taker_SKIP_PRICE: %s fresh_ask=%d¢ not in TM_PRICE_SET (scan=%d¢)",
                         ticker, fresh_ask, _tm_scan_price)
            return None

        if fresh_ask != price:
            logging.info("tm_taker_price_update: %s scanner=%d¢ fresh=%d¢ depth=%d src=%s",
                         ticker, price, fresh_ask, fresh_depth, fresh_source)
            price = fresh_ask
            candidate["best_yes_ask"] = fresh_ask
            # Re-derive sizing from execution-time price (scan price may have drifted)
            _exec_bal = candidate.get("balance_at_scan") or 100000
            _exec_buf_pct = candidate.get("spot_buffer_pct")
            # Adversary A6: sweep-aware risk cap — sizing against worst-case
            # fill price keeps cents-at-risk within the per-asset cap.
            _exec_risk_price = MAX_ENTRY_PRICE if TM_SWEEP_LIVE_ENABLED else None
            count = tm_compute_contracts(fresh_ask, seconds_to_close or 200,
                                         _exec_bal, asset,
                                         buf_pct=_exec_buf_pct,
                                         risk_cap_price=_exec_risk_price)
            candidate["position_size"] = count
            taker_fee = calculate_taker_fee(count, price)
            net_edge = cal_prob - (price / 100.0) - (taker_fee / (count * 100.0))

        # Race condition guard: check position at this price level
        _tm_exec_group = f"terminal_momentum_{price}"
        if STACKING_ENABLED:
            from bot.models import strategy_to_group
            if any(p.get("ticker") == ticker
                   and p.get("strategy_group", strategy_to_group(p.get("strategy", "")))
                       == _tm_exec_group
                   for p in self._state.get_open_positions()):
                logging.info("tm_taker_SKIP_POSITION: %s already held at %dc by TM", ticker, price)
                return None
        else:
            if any(p.get("ticker") == ticker for p in self._state.get_open_positions()):
                logging.info("tm_taker_SKIP_POSITION: %s already held", ticker)
                return None

        self._session_direct_taker_attempts += 1
        candidate["entry_path"] = "tm_taker"
        candidate["escalation_type"] = "direct_taker"
        self._recent_taker_tickers[ticker] = time.time()

        logging.info(
            "tm_taker_ENTRY: %s %dx @ %d¢ "
            "seconds_to_close=%.0f net_edge=%.4f cal_prob=%.4f taker_fee=%d¢",
            ticker, count, price,
            seconds_to_close or 0, net_edge, cal_prob, taker_fee)

        # tm_sweep_shadow: snapshot pre-fill depths at all four TM-relevant
        # tiers BEFORE the IOC. Wrapped to never break the trade path.
        _tmss_pre = self._tm_sweep_snapshot_depths(ticker) if TM_SWEEP_SHADOW_ENABLED else None

        _order_submit_ts = datetime.datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        result = self._submit_taker(candidate)

        # tm_sweep_shadow: snapshot post-fill depths immediately (option A —
        # models what a real sweep would see firing right after the IOC ack).
        # Insert one row regardless of fill outcome (full/partial/zero).
        #
        # Post-fill depth at the entry tier is RACY: we don't know whether the
        # WS delta from our own fill has landed in the local cache yet (adversary
        # A1). Storing raw is safer than guessing — over- or under-correcting at
        # random is worse than a documented bias. cf_pnl is unaffected because
        # it only consults sweep tiers (98/99), and a single-price IOC at the
        # entry tier cannot consume from higher tiers.
        if TM_SWEEP_SHADOW_ENABLED:
            try:
                _tmss_post = self._tm_sweep_snapshot_depths(ticker)
                # Adversary A2: clamp filled_count to non-negative int so
                # neither None (error sentinel) nor a future negative sentinel
                # corrupts unfilled_count.
                _tmss_raw = result.get("filled_count") if isinstance(result, dict) else 0
                _tmss_filled = max(0, int(_tmss_raw or 0))
                # Adversary R3 A1: read direct-bump status from candidate
                # (set by _submit_taker after its gate ran). Single source
                # of truth — eliminates the predicate-duplication that R2
                # A1 caught (capture over-reporting when picker happens to
                # fire and bump).
                _direct_bump = int(bool(candidate.get("_tm_direct_bump_fired", False)))
                self._state.insert_tm_sweep_shadow_row(
                    ticker=ticker,
                    event_ticker=candidate.get("event_ticker", ""),
                    asset=asset,
                    entry_time=_order_submit_ts,
                    entry_price_cents=int(price),
                    requested_count=int(count),
                    filled_count=_tmss_filled,
                    unfilled_count=int(count) - _tmss_filled,
                    depth_at_entry_pre_fill=fresh_depth,
                    depth_96c_pre=(_tmss_pre or {}).get(96),
                    depth_97c_pre=(_tmss_pre or {}).get(97),
                    depth_98c_pre=(_tmss_pre or {}).get(98),
                    depth_99c_pre=(_tmss_pre or {}).get(99),
                    depth_96c_post=(_tmss_post or {}).get(96),
                    depth_97c_post=(_tmss_post or {}).get(97),
                    depth_98c_post=(_tmss_post or {}).get(98),
                    depth_99c_post=(_tmss_post or {}).get(99),
                    seconds_to_close=seconds_to_close,
                    calibrated_prob=cal_prob,
                    buf_pct=candidate.get("spot_buffer_pct"),
                    best_ask_source=fresh_source,
                    direct_bump_applied=_direct_bump)
            except Exception:
                logging.debug("tm_sweep_shadow capture failed", exc_info=True)

        if result is not None:
            fill_count = result.get("filled_count", 0)
            if fill_count > 0:
                self._session_direct_taker_fills += 1
                logging.info("tm_taker_FILLED: %s %d/%d @ %d¢", ticker, fill_count, count, price)
                _taker_oid = result.get("order_id") if isinstance(result, dict) else None
                self._state.update_evaluated_opportunity_order(
                    ticker, order_id=_taker_oid,
                    order_submitted_at=_order_submit_ts, order_outcome="filled")
                # Telegram alert
                try:
                    _telegram_state._TELEGRAM.send(
                        f"TM: {asset} {fill_count}ct @ {price}c "
                        f"prob={cal_prob:.1%} stc={seconds_to_close or 0:.0f}s "
                        f"edge={net_edge:.2%}",
                        dedup_key=f"tm_{ticker}")
                except Exception:
                    logging.debug("TM telegram alert failed", exc_info=True)
                return result
            else:
                # Zero fill — no retry for TM (next scan cycle will re-evaluate)
                self._session_direct_taker_unfilled += 1
                logging.info("tm_taker_UNFILLED: %s @ %d¢ depth=%d", ticker, price, fresh_depth)
                self._state.update_evaluated_opportunity_order(
                    ticker, order_submitted_at=_order_submit_ts,
                    order_outcome="unfilled")
                return None
        return None

    def _tm_sweep_snapshot_depths(self, ticker: str) -> Optional[Dict[int, int]]:
        """Snapshot YES-ask depths at TM_SWEEP_CAPTURE_TIERS from the scanner's
        cached orderbook. Returns None on any error (caller treats as unknown).
        Never raises — instrumentation must not break the trade path."""
        try:
            scanner = self._ml.scanner if self._ml else None
            if not scanner:
                return None
            ob_data, _ = scanner._get_orderbook_cached(ticker)
            if not ob_data:
                return None
            ladder_json = OrderExecutor._extract_book_levels(ob_data, n=10)
            if not ladder_json:
                return None
            yes_asks = json.loads(ladder_json).get("yes_asks", [])
            return tm_sweep_extract_depths(yes_asks, TM_SWEEP_CAPTURE_TIERS)
        except Exception:
            return None

    def _execute_lpne_taker(self, candidate: Dict, asset: str, seconds_to_close) -> Optional[Dict]:
        """Execute low-price near-expiry trade — direct taker, fixed contracts, no retry.
        BTC 80-87c at STC<=120s. Mirrors _execute_tm_taker with LPNE constants."""
        ticker = candidate["ticker"]
        count = candidate["position_size"]  # LPNE_FIXED_CONTRACTS
        price = candidate["best_yes_ask"]
        cal_prob = candidate["calibrated_prob"]
        # Sprint B Bit B.2b — decision snapshot at route choice.
        self._emit_decision_snapshot(candidate, "taker_first")

        if count <= 0:
            logging.warning("ORDER_SUPPRESSED zero_size: %s asset=%s strategy=low_price_near_expiry price=%d",
                            ticker, asset, price)
            self._session_suppressed_zero_size += 1
            return None

        taker_fee = calculate_taker_fee(count, price)
        net_edge = cal_prob - (price / 100.0) - (taker_fee / (count * 100.0))

        # Fresh ask check — verify price hasn't moved outside LPNE range
        _lpne_scan_price = price
        fresh_ask, fresh_depth, fresh_source = self._dc_get_ask_with_depth(ticker, candidate)

        if fresh_ask is None:
            logging.warning("ORDER_SUPPRESSED no_asks: %s asset=%s strategy=low_price_near_expiry price=%d stc=%.0f",
                            ticker, asset, price, seconds_to_close or 0)
            self._session_suppressed_no_asks += 1
            return None

        if not (LPNE_MIN_PRICE <= fresh_ask <= LPNE_MAX_PRICE):
            logging.info("lpne_taker_SKIP_PRICE: %s fresh_ask=%d¢ outside LPNE range %d-%d (scan=%d¢)",
                         ticker, fresh_ask, LPNE_MIN_PRICE, LPNE_MAX_PRICE, _lpne_scan_price)
            return None

        if fresh_ask != price:
            logging.info("lpne_taker_price_update: %s scanner=%d¢ fresh=%d¢ depth=%d src=%s",
                         ticker, price, fresh_ask, fresh_depth, fresh_source)
            price = fresh_ask
            candidate["best_yes_ask"] = fresh_ask
            candidate["position_size"] = LPNE_FIXED_CONTRACTS  # no per-price overrides for LPNE
            count = LPNE_FIXED_CONTRACTS
            taker_fee = calculate_taker_fee(count, price)
            net_edge = cal_prob - (price / 100.0) - (taker_fee / (count * 100.0))

        # Race condition guard: check position one more time
        if any(p.get("ticker") == ticker for p in self._state.get_open_positions()):
            logging.info("lpne_taker_SKIP_POSITION: %s already held", ticker)
            return None

        self._session_direct_taker_attempts += 1
        candidate["entry_path"] = "lpne_taker"
        candidate["escalation_type"] = "direct_taker"
        self._recent_taker_tickers[ticker] = time.time()

        logging.info(
            "lpne_taker_ENTRY: %s %dx @ %d¢ "
            "seconds_to_close=%.0f net_edge=%.4f cal_prob=%.4f taker_fee=%d¢",
            ticker, count, price,
            seconds_to_close or 0, net_edge, cal_prob, taker_fee)

        _order_submit_ts = datetime.datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        result = self._submit_taker(candidate)

        if result is not None:
            fill_count = result.get("filled_count", 0)
            if fill_count > 0:
                self._session_direct_taker_fills += 1
                logging.info("lpne_taker_FILLED: %s %d/%d @ %d¢", ticker, fill_count, count, price)
                _taker_oid = result.get("order_id") if isinstance(result, dict) else None
                self._state.update_evaluated_opportunity_order(
                    ticker, order_id=_taker_oid,
                    order_submitted_at=_order_submit_ts, order_outcome="filled")
                try:
                    _telegram_state._TELEGRAM.send(
                        f"LPNE: {asset} {fill_count}ct @ {price}c "
                        f"prob={cal_prob:.1%} stc={seconds_to_close or 0:.0f}s "
                        f"edge={net_edge:.2%}",
                        dedup_key=f"lpne_{ticker}")
                except Exception:
                    logging.debug("LPNE telegram alert failed", exc_info=True)
                return result
            else:
                self._session_direct_taker_unfilled += 1
                logging.info("lpne_taker_UNFILLED: %s @ %d¢ depth=%d", ticker, price, fresh_depth)
                self._state.update_evaluated_opportunity_order(
                    ticker, order_submitted_at=_order_submit_ts,
                    order_outcome="unfilled")
                return None
        return None

    def _execute_bracket_no_taker(self, candidate: Dict, asset: str, seconds_to_close) -> Optional[Dict]:
        """Execute bracket NO trade — buy NO via IOC taker at computed price."""
        ticker = candidate["ticker"]
        count = candidate["position_size"]  # BRACKET_NO_FIXED_CONTRACTS (5)
        no_cost = candidate["best_yes_ask"]  # NO cost in cents (100 - yes_ask)
        yes_price = candidate.get("_bracket_yes_price", 100 - no_cost)
        # Sprint B Bit B.2b — decision snapshot at route choice.
        self._emit_decision_snapshot(candidate, "taker_first")

        if count <= 0:
            logging.warning("ORDER_SUPPRESSED zero_size: %s asset=%s strategy=bracket_no no_cost=%d",
                            ticker, asset, no_cost)
            self._session_suppressed_zero_size += 1
            return None

        # Fresh price check: get current YES ask and re-derive NO cost
        fresh_ask = self._get_addon_best_ask(ticker)
        if fresh_ask is None:
            fresh_ask = self._nbbo_fallback_price(candidate)
        if fresh_ask is not None:
            _fresh_no_cost = 100 - fresh_ask
            if _fresh_no_cost <= 0 or fresh_ask < BRACKET_NO_YES_MIN or fresh_ask > BRACKET_NO_YES_MAX:
                logging.info("bracket_no_SKIP_PRICE: %s fresh_yes=%dc (outside %d-%dc range)",
                             ticker, fresh_ask, BRACKET_NO_YES_MIN, BRACKET_NO_YES_MAX)
                return None
            if _fresh_no_cost != no_cost:
                logging.info("bracket_no_price_update: %s no_cost %dc→%dc (yes %dc→%dc)",
                             ticker, no_cost, _fresh_no_cost, yes_price, fresh_ask)
                no_cost = _fresh_no_cost
                candidate["best_yes_ask"] = no_cost  # Update for _submit_taker
                yes_price = fresh_ask

        # Ceiling check: NO cost must be ≤ 15c (generous margin above 4-12c target)
        if no_cost > 15:
            logging.info("bracket_no_SKIP_EXPENSIVE: %s no_cost=%dc > 15c", ticker, no_cost)
            return None

        # Race condition guard: check position one more time
        if any(p.get("ticker") == ticker for p in self._state.get_open_positions()):
            logging.info("bracket_no_SKIP_POSITION: %s already held", ticker)
            return None

        taker_fee = calculate_taker_fee(count, no_cost)
        net_edge = BRACKET_NO_ASSUMED_PROB - no_cost / 100.0 - taker_fee / (count * 100.0)

        self._session_direct_taker_attempts += 1
        candidate["entry_path"] = "bracket_no_taker"
        candidate["escalation_type"] = "direct_taker"
        self._recent_taker_tickers[ticker] = time.time()

        logging.info(
            "bracket_no_ENTRY: %s %dct NO @ %dc (YES=%dc) "
            "stc=%.0fs edge=%.2f%% assumed_prob=%.0f%% fee=%dc",
            ticker, count, no_cost, yes_price,
            seconds_to_close or 0, net_edge * 100,
            BRACKET_NO_ASSUMED_PROB * 100, taker_fee)

        _order_submit_ts = datetime.datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        result = self._submit_taker(candidate)

        if result is not None:
            fill_count = result.get("filled_count", 0)
            if fill_count > 0:
                self._session_direct_taker_fills += 1
                logging.info("bracket_no_FILLED: %s %d/%d NO @ %dc (YES=%dc)",
                             ticker, fill_count, count, no_cost, yes_price)
                _taker_oid = result.get("order_id") if isinstance(result, dict) else None
                self._state.update_evaluated_opportunity_order(
                    ticker, order_id=_taker_oid,
                    order_submitted_at=_order_submit_ts, order_outcome="filled")
                try:
                    _telegram_state._TELEGRAM.send(
                        f"BKT_NO: {ticker} {fill_count}ct NO @ {no_cost}c "
                        f"(YES@{yes_price}c) stc={seconds_to_close or 0:.0f}s",
                        dedup_key=f"bn_{ticker}")
                except Exception:
                    logging.debug("bracket_no telegram alert failed", exc_info=True)
                return result
            else:
                self._session_direct_taker_unfilled += 1
                logging.info("bracket_no_UNFILLED: %s NO @ %dc", ticker, no_cost)
                self._state.update_evaluated_opportunity_order(
                    ticker, order_submitted_at=_order_submit_ts, order_outcome="unfilled")
                return None
        return None

    def process_dc_retries(self):
        """Process queued DC IOC retries. Called at the top of each _tick().

        Non-blocking: each retry is a single IOC submission (<1s).
        Retries are spaced by DC_IOC_RETRY_DELAY (8s) via next_retry_ts.
        """
        if not self._dc_retry_queue:
            return

        now = time.time()
        still_pending = []

        for entry in self._dc_retry_queue:
            if now < entry["next_retry_ts"]:
                still_pending.append(entry)
                continue

            candidate = entry["candidate"]
            ticker = candidate["ticker"]
            _dc_strategy = entry["strategy"]
            attempt = entry["attempt"] + 1
            remaining = entry["remaining"]

            if attempt > 1 + DC_IOC_MAX_RETRIES:
                # Max retries exhausted
                if entry["total_filled"] > 0:
                    logging.info("dc_retry_DONE: %s %s partial_filled=%d/%d after %d attempts",
                                 _dc_strategy, ticker, entry["total_filled"],
                                 entry["original_count"], attempt - 1)
                    self._state.update_evaluated_opportunity_order(
                        ticker, order_outcome="partial_filled")
                else:
                    logging.warning("dc_retry_DONE: %s %s unfilled after %d attempts",
                                    _dc_strategy, ticker, attempt - 1)
                    self._state.update_evaluated_opportunity_order(
                        ticker, order_outcome="unfilled")
                continue

            # Fresh ask check with depth
            fresh_ask, fresh_depth, fresh_source = self._dc_get_ask_with_depth(ticker, candidate)
            # Coerce None / non-numeric → 0. dict.get only fills default
            # for MISSING key; an explicit None value still passes
            # through, and a JSON-parse string would crash later
            # comparisons. Pre-fix crash: `_dc_retry_delay(None)` raised
            # TypeError on `None > 600`.
            try:
                _stc_now = float(candidate.get("seconds_to_close") or 0)
            except (TypeError, ValueError):
                _stc_now = 0.0
            # Estimate current STC from original eval time
            # _queue_ts is set at every production append site (lines
            # 18833, 18905, 18927). Fallback `now - DC_IOC_RETRY_DELAY`
            # biases toward decay when missing (vs the previous `now`
            # default which kept _eval_age=0 → STC stuck at original
            # → entry could retry forever on a malformed entry). R1 [A5].
            _eval_age = now - entry.get(
                "_queue_ts", now - DC_IOC_RETRY_DELAY)
            if _stc_now and _stc_now > 0:
                _stc_now = max(0, _stc_now - _eval_age)
            _adaptive_delay = self._dc_retry_delay(_stc_now)

            # Apr 26 11:15 incident
            # (kb/failures/dc-retry-post-settlement-burn-2026-04-26.md):
            # candidate fired with STC=5s, hit IOC_ABORT_PHANTOM, queued
            # retry. Retries continued AFTER the 11:15 window close at
            # 1s adaptive delay (=1.0 when STC<30), each hitting
            # phantom + ABORT, burning scan-tick budget across all 11
            # attempts. Once the window has settled, no IOC will fill —
            # drop the entry and stop wasting scan-tick time.
            #
            # Guard: only drop if STC was ORIGINALLY positive AND has
            # decayed to ≤ 0. If `seconds_to_close` was missing or 0
            # at queue time, we cannot bound elapsed → we must NOT
            # drop on STC alone, because the queue's stated purpose
            # (line 18822: "book may appear later") is incompatible
            # with STC-based dropping when STC was never positive to
            # begin with. R1 [A1].
            try:
                _orig_stc = float(
                    candidate.get("seconds_to_close") or 0)
            except (TypeError, ValueError):
                _orig_stc = 0.0
            if _orig_stc > 0 and _stc_now <= 0:
                logging.info(
                    "dc_retry_DROP_WINDOW_CLOSED: %s %s "
                    "orig_stc=%.1fs eval_age=%.1fs (window settled); "
                    "dropping after %d attempts, total_filled=%d/%d",
                    _dc_strategy, ticker, _orig_stc, _eval_age,
                    attempt - 1,
                    entry["total_filled"], entry["original_count"])
                if entry["total_filled"] > 0:
                    self._state.update_evaluated_opportunity_order(
                        ticker, order_outcome="partial_filled")
                else:
                    self._state.update_evaluated_opportunity_order(
                        ticker, order_outcome="unfilled_window_closed")
                continue  # drop from queue

            if fresh_ask is None:
                logging.info("dc_retry_no_asks: %s %s attempt=%d/%d",
                             _dc_strategy, ticker, attempt, 1 + DC_IOC_MAX_RETRIES)
                entry["attempt"] = attempt
                entry["next_retry_ts"] = now + _adaptive_delay
                still_pending.append(entry)
                continue

            # Layer 1: Phantom depth flag — LOG ONLY, submit IOC regardless
            _phantom_depth = (fresh_depth == 0 and fresh_source == "market_nbbo")
            if _phantom_depth:
                logging.info("dc_retry_PHANTOM_FLAG: %s %s attempt=%d/%d depth=0 nbbo — submitting anyway",
                             _dc_strategy, ticker, attempt, 1 + DC_IOC_MAX_RETRIES)

            # Price floor gate: abort if ask dropped below DC qualifying floor
            if fresh_ask < DECIDED_CONTRACT_MIN_PRICE:
                _orig_p = entry.get("original_price", 0)
                logging.warning(
                    "dc_retry_ABORT_PRICE_COLLAPSED: %s %s fresh_ask=%d¢ < floor=%d¢ "
                    "(original=%d¢) — dropping from retry queue",
                    _dc_strategy, ticker, fresh_ask, DECIDED_CONTRACT_MIN_PRICE, _orig_p)
                if entry["total_filled"] > 0:
                    self._state.update_evaluated_opportunity_order(
                        ticker, order_outcome="partial_filled")
                else:
                    self._state.update_evaluated_opportunity_order(
                        ticker, order_outcome="unfilled_price_collapsed")
                continue  # Drop from queue

            # Price drift gate: abort if ask dropped 3c+ from original signal price
            _orig_price = entry.get("original_price", fresh_ask)
            if fresh_ask < (_orig_price - 3):
                logging.warning(
                    "dc_retry_ABORT_PRICE_DRIFT: %s %s fresh_ask=%d¢ original=%d¢ "
                    "(drift=%d¢) — dropping from retry queue",
                    _dc_strategy, ticker, fresh_ask, _orig_price, _orig_price - fresh_ask)
                if entry["total_filled"] > 0:
                    self._state.update_evaluated_opportunity_order(
                        ticker, order_outcome="partial_filled")
                else:
                    self._state.update_evaluated_opportunity_order(
                        ticker, order_outcome="unfilled_price_drift")
                continue  # Drop from queue

            # Layer 5: Price tolerance escalation on later retries
            # Retries 0-2: exact price. Retry 3+: widen by 1c per retry, max 3c.
            _retry_num = attempt - 1  # 0-indexed retry count (attempt 2 = retry 1)
            _price_offset = 0
            if _retry_num >= DC_PRICE_TOLERANCE_START_RETRY:
                _price_offset = min(_retry_num - DC_PRICE_TOLERANCE_START_RETRY + 1,
                                    DC_PRICE_TOLERANCE_MAX)

            price = min(fresh_ask + _price_offset, MAX_ENTRY_PRICE)
            candidate["best_yes_ask"] = price
            candidate["position_size"] = remaining
            cal_prob = candidate["calibrated_prob"]

            taker_fee = calculate_taker_fee(remaining, price)
            net_edge = cal_prob - (price / 100.0) - (taker_fee / (remaining * 100.0))
            if net_edge < -0.01:
                logging.info("dc_retry_SKIPPED: %s %s price=%d¢ (ask=%d+%d) net_edge=%.4f attempt=%d",
                             _dc_strategy, ticker, price, fresh_ask, _price_offset, net_edge, attempt)
                continue  # Drop from queue

            self._session_dc_retries += 1
            self._session_direct_taker_attempts += 1
            self._recent_taker_tickers[ticker] = now

            _offset_label = f" (+{_price_offset}c)" if _price_offset > 0 else ""
            logging.info(
                "dc_retry_ENTRY: %s %s %dx @ %d¢%s attempt=%d/%d total_filled=%d/%d depth=%d src=%s",
                _dc_strategy, ticker, remaining, price, _offset_label,
                attempt, 1 + DC_IOC_MAX_RETRIES,
                entry["total_filled"], entry["original_count"],
                fresh_depth, fresh_source)

            _order_submit_ts = datetime.datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
            result = self._submit_taker(candidate)

            if result is not None:
                fill_count = result.get("filled_count", 0)
                entry["total_filled"] += fill_count
                entry["remaining"] -= fill_count
                self._session_dc_retry_fills += 1

                logging.info("dc_retry_FILL: %s %s filled=%d total=%d/%d remaining=%d attempt=%d",
                             _dc_strategy, ticker, fill_count, entry["total_filled"],
                             entry["original_count"], entry["remaining"], attempt)

                if entry["remaining"] <= 0:
                    # Fully filled across retries
                    self._session_direct_taker_fills += 1
                    _taker_oid = result.get("order_id") if isinstance(result, dict) else None
                    self._state.update_evaluated_opportunity_order(
                        ticker, order_id=_taker_oid,
                        order_submitted_at=_order_submit_ts, order_outcome="filled")
                    continue  # Done — don't re-queue

                # Still more remaining — queue another retry
                entry["attempt"] = attempt
                entry["next_retry_ts"] = now + _adaptive_delay
                entry["last_order_submit_ts"] = _order_submit_ts
                entry["last_order_id"] = result.get("order_id")
                still_pending.append(entry)
            else:
                # Zero fill on retry — queue again
                logging.info("dc_retry_UNFILLED: %s %s attempt=%d/%d",
                             _dc_strategy, ticker, attempt, 1 + DC_IOC_MAX_RETRIES)
                entry["attempt"] = attempt
                entry["next_retry_ts"] = now + _adaptive_delay
                entry["last_order_submit_ts"] = _order_submit_ts
                still_pending.append(entry)

        self._dc_retry_queue = still_pending

    # ── Maker ─────────────────────────────────────────────────────────────

    # Hourly series prefixes — maker orders must NEVER be placed on these tickers.
    _HOURLY_SERIES_PREFIXES = ("KXBTCD-", "KXETHD-", "KXSOLD-", "KXXRPD-", "KXHYPED-", "KXDOGED-", "KXBNBD-", "KXADAD-", "KXBCHD-", "KXNEARD-", "KXZECD-")

    def _submit_maker(self, candidate: Dict, aggressive: bool = False, degraded: bool = False):
        """Submit maker limit order below fair value.

        Patient: 1-2¢ below fair value (wider spread).
        Aggressive: always 1¢ below (tighter, more likely to fill).
        Degraded: extra offset after post_only rejections (Tier 2).
        """
        ticker = candidate["ticker"]
        # Settlement-race gate — see MIN_ORDER_SUBMIT_STC_S.
        if self._should_skip_near_close(candidate):
            self._abort_near_close(candidate, path="maker")
            return
        # Block maker orders on hourly tickers — hourly must be taker-only (IOC).
        # Belt-and-suspenders: catches any code path that reaches maker with an hourly ticker.
        if any(ticker.startswith(p) for p in self._HOURLY_SERIES_PREFIXES):
            logging.warning("maker_blocked_hourly_ticker: %s — hourly tickers must use IOC only", ticker)
            return
        count = candidate["position_size"]
        fair_value = candidate["best_yes_ask"]
        balance = candidate["balance_at_scan"]

        if aggressive:
            offset = MAKER_PRICE_OFFSET  # always 1¢
        else:
            # 1¢ offset for prices ≥ 90¢, else 2¢
            offset = MAKER_PRICE_OFFSET if fair_value >= 90 else MAKER_PRICE_OFFSET + 1
        price = fair_value - offset
        if degraded:
            price -= POST_ONLY_DEGRADED_EXTRA_OFFSET
        # Per-asset price floor (mirrors scanner check at ~L2680)
        _pt = candidate.get("product_type")
        _asset = candidate.get("asset")
        _mcfg_exec = get_market_config(_pt)
        _floor = _mcfg_exec.min_entry_price
        if _pt in (None, "15m"):
            if _asset == "BTC":
                _floor = BTC_MIN_ENTRY_PRICE
            elif _asset == "ETH":
                _floor = ETH_MIN_ENTRY_PRICE
            elif _asset == "SOL":
                _floor = SOL_MIN_ENTRY_PRICE
            elif _asset == "XRP":
                _floor = XRP_MIN_ENTRY_PRICE
            elif _asset == "HYPE":
                _floor = HYPE_MIN_ENTRY_PRICE
            elif _asset == "DOGE":
                _floor = DOGE_MIN_ENTRY_PRICE
            elif _asset == "BNB":
                _floor = BNB_MIN_ENTRY_PRICE
        if price < _floor:
            logging.warning("Maker price %dc below %s floor %dc for %s — skipping",
                            price, _asset, _floor, ticker)
            return

        client_oid = str(uuid.uuid4())

        # Persist before submission
        _side = candidate.get("side", "yes")
        self._state.insert_bot_order(
            client_oid, ticker, candidate["event_ticker"],
            candidate["asset"], _side, count, price, False
        )

        # Submit with post_only to guarantee maker fees (4x cheaper)
        _price_kwarg = {"no_price": price} if _side == "no" else {"yes_price": price}
        resp = self._client.place_order(
            ticker=ticker, side=_side, action="buy",
            count=count, client_order_id=client_oid,
            post_only=True, **_price_kwarg,
        )

        if resp is None:
            self._state.mark_order_status(client_oid, "api_error")
            self._record_post_only_rejection(ticker)
            # Increment per-ticker api error counter (prevents hot retry loops)
            self._ticker_api_errors[ticker] = self._ticker_api_errors.get(ticker, 0) + 1
            rej_count = self._get_post_only_rejection_count(ticker)
            tier = "degraded" if degraded else "normal"
            logging.warning(
                "Maker order rejected (post_only): %s price=%d¢ tier=%s "
                "rej_count=%d/%d api_errors=%d fair=%d¢",
                ticker, price, tier, rej_count,
                POST_ONLY_MAX_SAME_PRICE + 1,
                self._ticker_api_errors[ticker], fair_value)
            self._session_post_only_rejections += 1
            return

        # Successful submission — reset api error counter for this ticker
        self._ticker_api_errors.pop(ticker, None)
        order_id = (resp.get("order") or {}).get("order_id", client_oid)
        self._state.confirm_order_submitted(client_oid, order_id)
        if self._ml:
            self._ml._session_maker_submissions += 1

        _now = time.time()
        order = {
            "order_id": order_id,
            "client_order_id": client_oid,
            "ticker": ticker,
            "event_ticker": candidate["event_ticker"],
            "asset": candidate["asset"],
            "side": _side,
            "price_cents": price,
            "count": count,
            "is_taker": False,
            "submit_time": _now,
            "seconds_to_close_at_submit": candidate["seconds_to_close"],
            "candidate": candidate,
            "balance_at_entry": balance,
            "entry_path": "maker",
            "_last_poll": _now,
            "_ask_history": deque(maxlen=30),
            "_last_queue_poll": 0.0,
        }
        self._active_orders[candidate["asset"]] = order

        # Clear rejection tracker on successful maker submission
        self._post_only_rejections.pop(ticker, None)

        self._logger.log_order({
            "action": "maker_submitted",
            "ticker": ticker,
            "order_id": order_id,
            "client_order_id": client_oid,
            "price_cents": price,
            "count": count,
            "fair_value": fair_value,
        })
        tier = "degraded" if degraded else ("aggressive" if aggressive else "patient")
        logging.info(
            f"Maker order: {ticker} {count}x @ {price}¢ "
            f"(fair={fair_value}¢, tier={tier})"
        )

    # ── Taker ─────────────────────────────────────────────────────────────

    def _submit_taker(self, candidate: Dict) -> Optional[Dict]:
        """Submit taker order at best ask. Blocks briefly to verify fill."""
        ticker = candidate["ticker"]
        # Settlement-race gate — see MIN_ORDER_SUBMIT_STC_S.
        if self._should_skip_near_close(candidate):
            self._abort_near_close(candidate, path="taker")
            return None
        count = candidate["position_size"]
        price = candidate["best_yes_ask"]
        balance = candidate["balance_at_scan"]
        # Ladder retries: avoid double-counting session-level IOC
        # metrics. The retry IS a new IOC submission to Kalshi, but
        # for SIGNAL-level metrics it's the same trading signal as
        # the parent. Mirrors the existing 'confirmation_addon'
        # exclusion pattern.
        _is_ladder_retry = bool(candidate.get("_is_ladder_retry"))

        # ── Smart IOC limit picker (Apr 25 2026) ──────────────────────────
        # Kalshi's matching engine fills against ALL ask levels at-or-below
        # our IOC limit. Pre-Apr 23 the bot used NBBO yes_ask (typically
        # wider than orderbook best_ask) — IOCs swept multiple levels and
        # filled 64-82ct on average. The 0ddcaf8 schema fix (Apr 23) made
        # the bot use orderbook best_ask exactly, dropping fills to 33ct
        # because liquidity sat 1-3c above our limit.
        #
        # The picker walks the visible orderbook from best_yes_ask upward,
        # finds the smallest limit price where cumulative fillable depth
        # ≥ position_size. Hard caps:
        #   - max_bump = IOC_LIMIT_MAX_BUMP_CENTS (3c default)
        #   - edge_ceiling = floor(prob*100) - fee_1c - reserve_cents
        #     (per-strategy reserve; default 0 = break-even after fee)
        #   - max_price = MAX_ENTRY_PRICE (99)
        # If picker can't fetch the live orderbook (no scanner ref / WS
        # cache empty), or the ticker is currently WS-drift-flagged
        # (cache untrusted), falls through to original `price` — no
        # regression on broken-cache paths.
        # The bumped limit is computed into `_ioc_limit_price` and used
        # ONLY for the place_order call below. We do NOT mutate
        # candidate["best_yes_ask"] — that stays at the scan-time value
        # for downstream telemetry/audit/post-fill analysis.
        _ioc_limit_price = price  # default to original
        # Hoist _bump_strategy so the TM_SWEEP_DIRECT_BUMP branch below
        # can reference it even when the picker block doesn't enter
        # (orderbook unavailable — the exact case the direct-bump fixes).
        _bump_strategy = candidate.get("strategy") or ""
        # Round 2 [P0-A]: picker is YES-side only. For NO-side
        # candidates (bracket_no, hourly_no_live, weather_no_live,
        # dc_shadow_no_side), `candidate["best_yes_ask"]` is set
        # to no_price — feeding it to the YES-side ladder walker
        # produces meaningless results and the bumped price gets
        # submitted as no_price, potentially overpaying. Bypass.
        _is_no_side = candidate.get("side") == "no"
        try:
            _scanner = self._ml.scanner if self._ml else None
            _live_ob = None
            # Bypass picker on drift-flagged tickers — the WS cache
            # the picker reads is the same one that's been wrong
            # (WS_DRIFT_AUTO_FLAG). Don't bump based on phantom data.
            _is_drift_flagged = (
                _scanner is not None
                and ticker in getattr(_scanner, "_ws_drift_cooldown", {}))
            if (_scanner is not None
                    and not _is_drift_flagged
                    and not _is_no_side):
                try:
                    _live_ob, _src = _scanner._get_orderbook_cached(ticker)
                except Exception:
                    _live_ob = None
            if _live_ob and isinstance(price, int) and price > 0:
                _cal_prob = candidate.get("calibrated_prob")
                if _cal_prob is not None and 0 < _cal_prob < 1:
                    _fee_1c = calculate_taker_fee(1, price)
                    # Per-strategy edge reserve — see
                    # STRATEGY_LIMIT_BUMP_RESERVE_CENTS in bot/_impl.py
                    # constants. Default (B) is 0 (break-even after
                    # fee). High-conviction strategies (DC tiers,
                    # addons) override to -1 (tolerate fee-cost on
                    # worst-fill margin).
                    # _bump_strategy hoisted above (used by the TM direct-bump
                    # branch outside this picker block).
                    _reserve_cents = STRATEGY_LIMIT_BUMP_RESERVE_CENTS.get(
                        _bump_strategy,
                        STRATEGY_LIMIT_BUMP_DEFAULT_RESERVE)
                    _edge_ceiling = (
                        int(_cal_prob * 100) - _fee_1c - _reserve_cents)
                    # TM Sweep Live: override edge_ceiling for the exact
                    # terminal_momentum tiers in TM_LIVE_STRATEGIES so the
                    # picker can bump up to MAX_ENTRY_PRICE (99c).
                    #
                    # Ladder-retry guard (adversary A4): the override does
                    # NOT apply on _is_ladder_retry candidates. The ladder
                    # escalation already does limit+1; compounding it with
                    # a fresh smart-picker bump is untested and could
                    # produce IOCs at price levels neither path validated.
                    if (TM_SWEEP_LIVE_ENABLED
                            and _bump_strategy in TM_LIVE_STRATEGIES
                            and not _is_ladder_retry):
                        _edge_ceiling = MAX_ENTRY_PRICE
                    _smart_limit = OrderExecutor._pick_ioc_limit_for_depth(
                        _live_ob,
                        best_yes_ask=int(price),
                        target_qty=int(count),
                        max_bump_cents=IOC_LIMIT_MAX_BUMP_CENTS,
                        edge_ceiling_price=_edge_ceiling,
                        max_price=MAX_ENTRY_PRICE,
                    )
                    if _smart_limit > price:
                        logging.info(
                            "IOC_LIMIT_BUMPED: %s %d→%d¢ "
                            "(target_qty=%d, prob=%.4f, "
                            "edge_ceiling=%d, max_bump=%d, "
                            "reserve=%+d strategy=%s) — sweeping "
                            "deeper levels",
                            ticker, price, _smart_limit,
                            count, _cal_prob, _edge_ceiling,
                            IOC_LIMIT_MAX_BUMP_CENTS, _reserve_cents,
                            _bump_strategy)
                        _ioc_limit_price = _smart_limit
                    elif _smart_limit == price:
                        logging.debug(
                            "IOC_LIMIT_AT_BEST: %s %d¢ (no bump needed "
                            "or no benefit within caps)",
                            ticker, price)
            elif _is_drift_flagged:
                logging.debug(
                    "IOC_LIMIT_PICKER_BYPASS: %s — ws_drift_cooldown "
                    "active; using scan-time best_ask=%d¢ unchanged",
                    ticker, price)
            elif _is_no_side:
                logging.debug(
                    "IOC_LIMIT_PICKER_BYPASS: %s — NO-side IOC "
                    "(picker is YES-side only); using "
                    "scan-time price=%d¢ unchanged",
                    ticker, price)
        except Exception:
            logging.warning(
                "smart_ioc_limit_picker failed", exc_info=True)
        # ── TM Sweep Live: direct limit bump (no orderbook required) ─────
        # The smart picker above is gated on _live_ob from the WS cache
        # and has no REST fallback. TM tickers consistently lack fresh WS
        # cache when TM fires (488/488 production rows showed
        # best_ask_source='market_nbbo' as of 2026-04-28), so the picker
        # silently no-ops for TM and the edge_ceiling override never runs.
        # This branch sets the limit directly when the sweep gates are
        # satisfied — independent of orderbook availability. Kalshi
        # auto-cancels surplus at $0 on unfilled IOC tail.
        # Conditions:
        #   - TM_SWEEP_LIVE_ENABLED env-var-gated kill switch
        #   - exact-set strategy match (no startswith footgun)
        #   - not a ladder retry (don't compound with +1c escalation)
        #   - not _is_no_side (NO-side `best_yes_ask` is no_price; bumping
        #     would submit a 99c NO buy = ~$1/contract overpay; matches
        #     the picker's NO-side bypass)
        #   - price < MAX_ENTRY_PRICE (no bump possible at 99c entry)
        #   - _ioc_limit_price < MAX_ENTRY_PRICE (don't lower a higher
        #     picker-chosen limit on the rare path where picker did fire)
        # Adversary R3 A1: write the gate result to the candidate dict so
        # the shadow capture in _execute_tm_taker reads from a single
        # source of truth — eliminating the predicate-duplication
        # fragility that R2 caught.
        _direct_bump_fired = (
            TM_SWEEP_LIVE_ENABLED
            and _bump_strategy in TM_LIVE_STRATEGIES
            and not _is_ladder_retry
            and not _is_no_side
            and isinstance(price, int)
            and price < MAX_ENTRY_PRICE
            and _ioc_limit_price < MAX_ENTRY_PRICE)
        candidate["_tm_direct_bump_fired"] = _direct_bump_fired
        if _direct_bump_fired:
            # Adversary R2 A3: assign FIRST, log AFTER — log records fact,
            # not intent. A logging handler exception between the log call
            # and the assignment would have created a misleading audit
            # trail (claiming bump while actually submitting scan-time price).
            _prev_limit = _ioc_limit_price
            _ioc_limit_price = MAX_ENTRY_PRICE
            logging.info(
                "TM_SWEEP_DIRECT_BUMP: %s %d→%d¢ strategy=%s "
                "(picker bypassed; orderbook unavailable for TM)",
                ticker, _prev_limit, _ioc_limit_price, _bump_strategy)
        # `_ioc_limit_price` is the actual price submitted to Kalshi.
        # `price` and `candidate["best_yes_ask"]` remain at scan-time
        # values for the downstream drift-check + PHANTOM_ABORT logic
        # and for telemetry/audit.

        # ── Option X v2: per-strategy IOC clamp (Apr 24) ──────────────────
        # Kalshi IOC matches at BEST available price up to our limit,
        # sweeping through the ladder. Variant B (sub-floor phantom-ask
        # sweep) can hand us sub-floor fills when top-of-book is thin and
        # real asks sit way below. Original Option X (Apr 15) clamped ALL
        # orderbook-source IOCs to top-of-book depth to prevent this.
        #
        # Post-WS-fix (0ddcaf8, Apr 23), top-of-book on TM_99 markets is
        # routinely 1-2ct. Pre-fix the clamp was silently bypassed because
        # ob_data came up empty (schema drift) → best_ask_source='market_nbbo'
        # → blind IOC path. Now that orderbook is real, the clamp fires
        # correctly but at a thin level, killing strategies that previously
        # benefited from blind-firing into Kalshi's real book.
        #
        # v2 picks the clamp policy per-strategy (STRATEGY_CLAMP_POLICY):
        #   top_of_book — cap count at quoted best ask depth. Preserves
        #     Variant B protection for sub-floor-risk paths.
        #   no_clamp    — submit full Kelly count. Kalshi IOC auto-cancels
        #     the unfilled remainder ($0 charge), so oversizing is free.
        #     PHANTOM_ABORT (ask_depth=0) still fires as the catastrophic
        #     tail guard. Correct for ceiling-triggered strategies (TM) and
        #     floor-triggered discounts with empirically-zero sweep tail.
        # See kb/decisions/no-floor-relaxation-on-ws-fix.md.
        _ask_src = candidate.get("best_ask_source")
        _ob_snap = candidate.get("ob_snapshot") or {}
        _ask_depth = _ob_snap.get("ask_depth")
        _strategy = candidate.get("strategy") or ""
        _policy = STRATEGY_CLAMP_POLICY.get(_strategy, STRATEGY_CLAMP_DEFAULT)

        # WS cache drift defense: when cache claims non-trivial depth, verify
        # against a fresh REST /orderbook fetch. If REST materially disagrees,
        # prefer REST as authoritative. This is a data-layer correction (not
        # a policy change); a confirmed drift means the book really IS thin
        # regardless of strategy policy, so we clamp even on no_clamp paths.
        # If cache was already thin (< threshold) OR REST agrees, no change.
        # See kb/failures/kalshi-ws-schema-drift.md § "WS delta underflow".
        # Apr 25 2026: clamp uses windowed peak (not single REST sample) for
        # the size decision; PHANTOM_ABORT uses fresh sample so a real-time
        # empty book is still caught regardless of historical peak.
        _drift_corrected = False
        _rest_fresh = None  # for PHANTOM_ABORT (Round 1 P0 #2)
        if (IOC_DRIFT_CHECK_ENABLED
                and _ask_src == "orderbook"
                and isinstance(_ask_depth, int)
                and _ask_depth >= IOC_DRIFT_CHECK_MIN_CACHED_DEPTH):
            # Smoothed helper returns (peak, fresh):
            #   peak — windowed-max for the size clamp (anti-flicker)
            #   fresh — most-recent REST sample for PHANTOM_ABORT
            # See IOC_DRIFT_CHECK_REST_WINDOW_S. A single REST call is
            # volatile — WS_DRIFT_PROBE_REST_STABILITY shows two
            # back-to-back calls disagreeing by hundreds of contracts.
            # Apr 25 2026: single-sample clamp caused a 65% drop in
            # position size across all assets.
            _rest_peak, _rest_fresh = self._rest_best_ask_depth_smoothed(ticker)
            # Round 2 [A1] cold-start gate: if the buffer has <2
            # samples in the window, peak ≈ fresh and the "smoothing"
            # degenerates to single-sample clamping — the exact
            # pre-fix bug. Skip the divergence/clamp branch on cold
            # start and let the existing policy
            # (cached _ask_depth + STRATEGY_CLAMP_POLICY) handle it.
            # _rest_fresh stays exposed for PHANTOM_ABORT below.
            _sample_count = self._rest_depth_window_count(ticker)
            _cold_start = (
                _sample_count
                < OrderExecutor._REST_DEPTH_MIN_SAMPLES_FOR_CLAMP)
            if _rest_peak is not None and not _cold_start:
                if _rest_peak < _ask_depth * IOC_DRIFT_CHECK_DIVERGENCE_RATIO:
                    logging.warning(
                        "IOC_CACHE_DRIFT: %s %dc ws_cache=%d rest_peak=%d "
                        "rest_fresh=%s (ratio=%.2f, samples=%d) "
                        "strategy=%s policy=%s "
                        "— using REST peak as authoritative",
                        ticker, price, _ask_depth, _rest_peak,
                        ("?" if _rest_fresh is None else str(_rest_fresh)),
                        _rest_peak / max(_ask_depth, 1), _sample_count,
                        _strategy, _policy)
                    _ask_depth = _rest_peak  # authoritative for clamp logic below
                    _drift_corrected = True
                else:
                    logging.info(
                        "IOC_CACHE_OK: %s %dc ws_cache=%d rest_peak=%d "
                        "rest_fresh=%s (samples=%d, strategy=%s)",
                        ticker, price, _ask_depth, _rest_peak,
                        ("?" if _rest_fresh is None else str(_rest_fresh)),
                        _sample_count, _strategy)
            elif _rest_peak is not None and _cold_start:
                # Cold-start: smoothed-peak gate isn't authoritative yet
                # (samples < _REST_DEPTH_MIN_SAMPLES_FOR_CLAMP). Default
                # behavior is to fall through to cached-depth policy.
                # ESCAPE HATCH (Apr 25 2026): if the single fresh REST
                # sample shows CATASTROPHIC divergence from cache
                # (≥10× drift, IOC_DRIFT_CHECK_COLD_START_RATIO), apply
                # the clamp anyway. Single-sample flicker risk is real
                # but bounded by the strict ratio; the alternative is
                # what we just measured — Kelly-size IOCs into 1ct books
                # producing 50+ micro-fills/day across freshly-discovered
                # tickers (~16/hr, all hit cold-start path).
                if (_rest_peak
                        < _ask_depth * IOC_DRIFT_CHECK_COLD_START_RATIO):
                    logging.warning(
                        "IOC_CACHE_DRIFT_COLD: %s %dc ws_cache=%d "
                        "rest_fresh=%d ratio=%.3f (samples=%d, "
                        "threshold=%.2f) strategy=%s policy=%s — "
                        "catastrophic drift on cold-start; using REST "
                        "as authoritative",
                        ticker, price, _ask_depth, _rest_peak,
                        _rest_peak / max(_ask_depth, 1), _sample_count,
                        IOC_DRIFT_CHECK_COLD_START_RATIO,
                        _strategy, _policy)
                    _ask_depth = _rest_peak
                    _drift_corrected = True
                else:
                    # Cold-start with non-catastrophic divergence: fall
                    # through to cached policy. Log once for diagnostics
                    # — this branch hits constantly for newly-discovered
                    # 15M tickers, so use INFO not WARNING to avoid log
                    # spam.
                    logging.info(
                        "IOC_CACHE_COLD_START: %s %dc ws_cache=%d "
                        "rest_fresh=%s (samples=%d < %d, ratio=%.3f) "
                        "— falling through to cached-depth policy",
                        ticker, price, _ask_depth,
                        ("?" if _rest_fresh is None else str(_rest_fresh)),
                        _sample_count,
                        OrderExecutor._REST_DEPTH_MIN_SAMPLES_FOR_CLAMP,
                        _rest_peak / max(_ask_depth, 1))

        if _ask_src == "orderbook" and isinstance(_ask_depth, int):
            # Catastrophic tail guard — fires regardless of policy. Uses
            # BOTH the drift-corrected _ask_depth (cached/peak) AND the
            # fresh REST sample. Fresh==0 means the book is empty RIGHT
            # NOW, regardless of any historical peak — the smoothing
            # window must not mask this signal. Round 1 P0 #2.
            if _ask_depth == 0 or _rest_fresh == 0:
                _abort_reason = (
                    "rest_fresh=0" if _rest_fresh == 0 else
                    "ask_depth=0 (orderbook-confirmed)")
                logging.warning(
                    "IOC_ABORT_PHANTOM: %s %dc count=%d %s "
                    "strategy=%s policy=%s — refusing IOC to prevent "
                    "ladder sweep",
                    ticker, price, count, _abort_reason, _strategy, _policy)
                if not _is_ladder_retry:
                    self._session_ioc_unfilled += 1
                return None
            if _policy == "no_clamp":
                # Default no_clamp: submit full Kelly, trust Kalshi auto-cancel.
                # Exception: if drift check corrected depth downward, clamp to
                # the REST-verified depth — that's a data-correctness override.
                if _drift_corrected and _ask_depth < count:
                    if _ask_depth < IOC_MIN_COUNT_AFTER_CLAMP:
                        logging.warning(
                            "IOC_ABORT_THIN_CLAMP: %s %dc rest_depth=%d < min=%d "
                            "(policy=no_clamp + drift, original_count=%d, asset=%s, strategy=%s) "
                            "— book genuinely thin, skipping; next scan retries after cooldown",
                            ticker, price, _ask_depth, IOC_MIN_COUNT_AFTER_CLAMP,
                            count, candidate.get("asset", "?"), _strategy)
                        if not _is_ladder_retry:
                            self._session_ioc_unfilled += 1
                        return None
                    logging.warning(
                        "IOC_DRIFT_CLAMP: %s %dc count %d -> %d "
                        "(policy=no_clamp + REST drift correction, asset=%s, strategy=%s)",
                        ticker, price, count, _ask_depth,
                        candidate.get("asset", "?"), _strategy)
                    count = _ask_depth
                    candidate["position_size"] = count
                else:
                    logging.info(
                        "IOC_NO_CLAMP: %s %dc count=%d (cached_depth=%d, asset=%s, strategy=%s)",
                        ticker, price, count, _ask_depth,
                        candidate.get("asset", "?"), _strategy)
            else:
                # top_of_book (default, conservative). Clamp to verified depth
                # (drift-corrected if REST fired, else cached) to prevent
                # ladder sweeps into sub-floor prices.
                if _ask_depth < count:
                    logging.warning(
                        "IOC_SIZE_CLAMP: %s %dc count %d -> %d "
                        "(policy=top_of_book, verified_depth=%d, drift=%s, asset=%s, strategy=%s)",
                        ticker, price, count, _ask_depth, _ask_depth,
                        _drift_corrected, candidate.get("asset", "?"), _strategy)
                    count = _ask_depth
                    candidate["position_size"] = count
        elif _ask_src == "market_nbbo":
            # NBBO blind path: no orderbook data → no depth signal,
            # no PHANTOM_ABORT possible. Round 2 [A2] / Round 3 [A4]
            # known scope limit — the smoothed-clamp + fresh-zero
            # phantom guard only protects orderbook-source IOCs.
            # If NBBO blind becomes the dominant path again (it was
            # pre-WS-fix), a separate defense is needed here.
            logging.info(
                "IOC_BLIND_SUBMIT: %s %dc count=%d asset=%s strategy=%s (NBBO fallback — no depth)",
                ticker, price, count, candidate.get("asset", "?"), _strategy)
        # ── end Option X v2 ───────────────────────────────────────────────

        client_oid = str(uuid.uuid4())

        # Persist before submission. price_cents records the LIMIT
        # actually submitted to Kalshi (= _ioc_limit_price), not the
        # scan-time best_ask. Round 2 [P1-B]: audit trail must
        # reflect what was actually sent.
        _side = candidate.get("side", "yes")
        self._state.insert_bot_order(
            client_oid, ticker, candidate["event_ticker"],
            candidate["asset"], _side, count, _ioc_limit_price, True
        )

        # F/U TM_99 zero-fill diagnostic (Apr 26): pre-IOC ladder
        # snapshot. Pairs with post-IOC snapshot below (after place_order
        # returns) to discriminate HYP A (Kalshi only matches yes_asks
        # ladder — no_bid stays unchanged on fail) from HYP B (Kalshi
        # matches both, no_bid sniped before our IOC arrives — no_bid
        # qty drops between pre and post). R-review [A1] fix.
        # See tests/integration/test_ioc_submit_ladder_diag.py.
        _diag_pre = None
        try:
            _diag_pre = OrderExecutor._compute_ladder_diag(_live_ob)
        except Exception:
            logging.debug("IOC_SUBMIT_LADDER_DIAG pre failed", exc_info=True)

        # Submit as IOC — exchange auto-cancels any unfilled remainder.
        # Uses _ioc_limit_price (smart picker output) for the actual
        # exchange submission, while `price` and candidate["best_yes_ask"]
        # remain at scan-time values for telemetry/audit/drift-check.
        _price_kwarg = (
            {"no_price": _ioc_limit_price} if _side == "no"
            else {"yes_price": _ioc_limit_price})
        resp = self._client.place_order(
            ticker=ticker, side=_side, action="buy",
            count=count, client_order_id=client_oid,
            time_in_force="immediate_or_cancel", **_price_kwarg,
        )

        # F/U TM_99 zero-fill diagnostic — post-IOC snapshot + outcome.
        # Includes fill_count so the divergence pattern can be
        # correlated with fill outcome via single-line grep
        # (R-review [A5]). Re-fetches the cached orderbook so we
        # observe post-fill state (WS push from Kalshi typically
        # arrives within ms of fill). If no_bid_qty dropped between
        # pre and post, the IOC matched against the no_bid → HYP B.
        # If no_bid_qty unchanged AND fill_count==0, our 99c bid
        # never reached the no_bid → HYP A.
        try:
            _diag_post = None
            if _scanner is not None:
                try:
                    _live_ob_post, _ = _scanner._get_orderbook_cached(ticker)
                    _diag_post = OrderExecutor._compute_ladder_diag(_live_ob_post)
                except Exception:
                    pass
            _fill_ct = 0
            if resp is not None:
                _fill_ct = (
                    fp_str_to_int(
                        (resp.get("order") or {}).get("fill_count_fp"))
                    or ((resp.get("order") or {}).get("fill_count") or 0)
                )
            _pre = _diag_pre or {}
            _post = _diag_post or {}
            logging.info(
                "IOC_SUBMIT_LADDER_DIAG: %s asset=%s strategy=%s "
                "bid=%d req=%d fill=%d "
                "PRE: yes=%s/%d no_bid=%s/%d cross=%s "
                "diverges=%s one_side_empty=%s "
                "POST: yes=%s/%d no_bid=%s/%d",
                ticker, candidate.get("asset", "?"),
                candidate.get("strategy", "?"),
                _ioc_limit_price, count, _fill_ct,
                _pre.get("yes_ask_top_price"), _pre.get("yes_ask_top_qty", 0),
                _pre.get("no_bid_top_price"), _pre.get("no_bid_top_qty", 0),
                _pre.get("cross_side_ask"),
                _pre.get("diverges"), _pre.get("one_side_empty"),
                _post.get("yes_ask_top_price"), _post.get("yes_ask_top_qty", 0),
                _post.get("no_bid_top_price"), _post.get("no_bid_top_qty", 0))
        except Exception:
            logging.debug("IOC_SUBMIT_LADDER_DIAG post failed", exc_info=True)

        if resp is None:
            self._state.mark_order_status(client_oid, "api_error")
            self._ticker_api_errors[ticker] = self._ticker_api_errors.get(ticker, 0) + 1
            logging.error("Taker order submission failed: %s (api_errors=%d)",
                          ticker, self._ticker_api_errors[ticker])
            if (candidate.get("entry_path") != "confirmation_addon"
                    and not _is_ladder_retry):
                self._session_ioc_unfilled += 1
            return None

        # Successful submission — reset api error counter
        self._ticker_api_errors.pop(ticker, None)
        order_id = (resp.get("order") or {}).get("order_id", client_oid)
        remaining_count = (resp.get("order") or {}).get("remaining_count", count)
        try:
            _order_fill_count = fp_str_to_int(
                (resp.get("order") or {}).get("fill_count_fp")) or (
                (resp.get("order") or {}).get("fill_count") or 0)
        except (TypeError, ValueError, OverflowError):
            logging.warning(
                "TAKER_FILL_PARSE_MALFORMED: %s order=%s fill_count_fp=%r "
                "fill_count=%r — treating as 0; confirm still runs",
                ticker, order_id,
                (resp.get("order") or {}).get("fill_count_fp"),
                (resp.get("order") or {}).get("fill_count"))
            _order_fill_count = 0
        self._state.confirm_order_submitted(client_oid, order_id)

        order_info = {
            "order_id": order_id,
            "client_order_id": client_oid,
            "ticker": ticker,
            "event_ticker": candidate["event_ticker"],
            "asset": candidate["asset"],
            "side": _side,
            # `price_cents` = limit actually submitted to Kalshi
            # (post smart-picker bump, if any). Round 2 [P1-B].
            "price_cents": _ioc_limit_price,
            "scan_time_best_ask": price,  # original for forensics
            "count": count,
            "is_taker": True,
            "submit_time": time.time(),
            "seconds_to_close_at_submit": candidate["seconds_to_close"],
            "candidate": candidate,
            "balance_at_entry": balance,
            "execution_method": "ioc",
            "entry_path": candidate.get("entry_path", "direct_taker"),
        }

        self._logger.log_order({
            "action": "taker_submitted",
            "ticker": ticker,
            "order_id": order_id,
            "client_order_id": client_oid,
            "price_cents": _ioc_limit_price,
            "scan_time_best_ask": price,
            "count": count,
            "time_in_force": "ioc",
        })
        logging.info(
            "Taker IOC order: %s %dx @ %d¢%s",
            ticker, count, _ioc_limit_price,
            (f" (bumped from {price}¢)"
             if _ioc_limit_price != price else ""))

        # Lifecycle snapshot at IOC submit — captures the book the order
        # was sent into. Pairs with fill snapshots in _on_fill so we can
        # later answer "was the 1ct stub the entire book at submit time
        # or did it shrink between scan and submit?"
        try:
            self._state.insert_order_lifecycle_snapshot(
                order_id=order_id, ticker=ticker, event_type="submit",
                source=candidate.get("strategy"))
        except Exception:
            logging.warning("insert_order_lifecycle_snapshot (submit) failed",
                            exc_info=True)

        # IOC resolves instantly; brief wait + collect ALL fill events.
        # An IOC can match against multiple resting orders, generating
        # multiple fill events.  _check_for_fill() returns one unseen
        # fill per call (tracks seen IDs), so loop until exhausted OR
        # until filled_so_far reaches the requested count. The second
        # break is the post-completion-fill-leak guard paired with the
        # `remaining <= 0` early-return in `_on_fill` (2026-05-19 HYPE
        # incident, KXHYPE15M-26MAY190645-45).
        time.sleep(0.3)
        total_filled = 0
        while True:
            fill = self._check_for_fill(order_info)
            if not fill:
                break
            fill_count = self._on_fill(fill, order_info)
            total_filled += fill_count
            if order_info.get("filled_so_far", 0) >= order_info["count"]:
                break

        # Second poll pass: catch late fills that arrived after initial 0.3s
        if total_filled > 0 and order_info.get("filled_so_far", 0) < order_info["count"]:
            time.sleep(0.5)
            while True:
                fill = self._check_for_fill(order_info)
                if not fill:
                    break
                fill_count = self._on_fill(fill, order_info)
                total_filled += fill_count
                if order_info.get("filled_so_far", 0) >= order_info["count"]:
                    break

        if total_filled > 0:
            if (candidate.get("entry_path") != "confirmation_addon"
                    and not _is_ladder_retry):
                self._session_ioc_fills += 1
            unfilled = count - total_filled
            logging.info(
                f"ioc_taker_result: {ticker} filled={total_filled} "
                f"remaining={unfilled}"
                f"{'' if unfilled == 0 else ' [PARTIAL]'}")
            if unfilled > 0:
                logging.warning(
                    f"IOC partial fill: {ticker} wanted {count} got "
                    f"{total_filled} — {unfilled} contracts unfilled")
                # Ladder escalation: actively retry once at +1¢ for the
                # remainder. Runs BEFORE the maker tail so coexistence
                # is layered: active reach first, passive rest second.
                # If escalation fully fills, the maker tail's MIN_
                # REMAINDER gate naturally suppresses the tail. If
                # escalation also partials, fall through to maker tail
                # at ORIGINAL price (where someone may return to).
                # Failures here MUST NOT break the IOC return path.
                _ladder_filled = 0
                if LADDER_ESCALATION_ENABLED:
                    try:
                        _ladder_result = self._maybe_ladder_escalate(
                            candidate=candidate,
                            original_limit=_ioc_limit_price,
                            remaining=unfilled,
                            ioc_filled=total_filled)
                        _ladder_filled = (
                            _ladder_result.get("escalated_filled", 0))
                        unfilled -= _ladder_filled
                    except Exception:
                        logging.warning(
                            "_maybe_ladder_escalate raised; IOC "
                            "result still returned to caller",
                            exc_info=True)
                # Maker-tail: post the unfilled remainder as a
                # post_only GTC limit so benign rotation flow can
                # still fill us. Eligibility, gates, caps all live in
                # _maybe_post_maker_tail. Failures here MUST NOT break
                # the IOC return path — this is a strict additive
                # behavior on top of the IOC outcome.
                if MAKER_TAIL_AFTER_IOC_PARTIAL:
                    try:
                        self._maybe_post_maker_tail(
                            candidate=candidate,
                            ioc_price=_ioc_limit_price,
                            remaining=unfilled,
                            ioc_filled=total_filled)
                    except Exception:
                        logging.warning(
                            "_maybe_post_maker_tail raised; IOC "
                            "result still returned to caller",
                            exc_info=True)
            order_info["filled_count"] = total_filled
            return order_info

        # ── Ghost fill detection (Layer A): remaining_count from order response ──
        # Kalshi's matching engine returns remaining_count=0 when the order was
        # fully matched. If fill polling found nothing, the fills API has latency
        # but the contracts DO exist. Register a defensive position at the limit
        # price (conservative — actual fills are ≤ limit). Reconciliation at next
        # startup will correct prices from Kalshi's positions API.
        #
        # CRITICAL: For IOC orders, remaining_count=0 can also mean the order was
        # auto-canceled with zero fills. Must verify fill_count > 0 from the order
        # response to distinguish real ghost fills from unfilled IOC cancellations.
        # (Bug: false ghost fill on KXSOL15M-26MAR061400-00 cost -$39.16, Mar 6 2026)
        if remaining_count == 0 and _order_fill_count > 0:
            logging.error(
                f"GHOST_FILL_DETECTED: {ticker} remaining_count=0 but no fill "
                f"events from API — Kalshi matched all {count} contracts. "
                f"Registering defensive position at limit price {price}¢")
            self._state.record_position_from_fill(
                ticker=ticker,
                event_ticker=candidate["event_ticker"],
                asset=candidate["asset"],
                side=_side,
                count=count,
                price_cents=price,
                strategy=candidate.get("strategy"),
                seconds_to_close=order_info.get("seconds_to_close_at_submit"),
                fill_latency=round(time.time() - order_info["submit_time"], 3),
                vol_regime=candidate.get("vol_regime"),
                calibrated_prob=candidate.get("calibrated_prob"),
                edge=candidate.get("edge"),
                kelly_f=candidate.get("kelly_f"),
                is_taker=True,
                fill_source="ghost_fill",
                execution_method="ioc",
                escalation_type=candidate.get("escalation_type"),
                maker_price_cents=candidate.get("maker_price_cents"),
                maker_wait_seconds=candidate.get("maker_wait_seconds"),
            )
            self._state.mark_order_status(order_id, "filled")
            if (candidate.get("entry_path") != "confirmation_addon"
                    and not _is_ladder_retry):
                self._session_ioc_fills += 1
            order_info["filled_count"] = count  # Ghost fill = assumed full fill
            return order_info

        # remaining_count=0 but fill_count=0: IOC was auto-canceled, not a ghost fill
        if remaining_count == 0 and _order_fill_count == 0:
            logging.info(
                f"IOC_CANCELED_NO_FILLS: {ticker} remaining_count=0 "
                f"fill_count=0 — order was canceled unfilled, not a ghost fill")

        # ── Ghost fill detection (Layer B): positions API verification ──
        # remaining_count > 0 suggests genuinely unfilled, but verify against
        # Kalshi's positions API in case of any untracked position.
        #
        # B1 fix (ticket 86b9zuczz, 2026-05-18): Kalshi's positions API is
        # ticker-level — it returns the CUMULATIVE position across all local
        # strategy_groups. Previously this code recorded the full cumulative
        # count, which (a) contaminated the new strategy's row with sibling-
        # strategy fills and (b) compounded on each dc_retry pass. Now we
        # compute the DELTA vs. the local sum across all strategy_groups for
        # the ticker, and only record the delta as a new fill. Lesson L105
        # (cumulative-vs-delta API semantics in fill paths) — see ClickUp
        # ticket for the full postmortem.
        try:
            _pos_resp = self._client.get_positions()
            if _pos_resp and _pos_resp.get("market_positions"):
                for _pos in _pos_resp["market_positions"]:
                    if _pos.get("ticker") == ticker:
                        _pos_count = fp_str_to_int(_pos.get("position_fp")) or (_pos.get("position") or 0)
                        if _pos_count != 0:
                            _ghost_side = "yes" if _pos_count > 0 else "no"
                            _pos_abs = abs(_pos_count)
                            _pos_cost_d = _pos.get("market_exposure_dollars")
                            _pos_cost = dollars_str_to_cents(_pos_cost_d) if _pos_cost_d else (_pos.get("market_exposure") or 0)
                            _pos_avg = _pos_cost // _pos_abs if _pos_abs else price
                            _local_count = self._state.get_local_position_count_for_ticker(
                                ticker, _ghost_side)
                            _delta = _pos_abs - _local_count
                            if _delta <= 0:
                                logging.info(
                                    f"GHOST_FILL_SKIP_NO_DELTA: {ticker} side={_ghost_side} "
                                    f"positions API shows {_pos_abs}, local already has "
                                    f"{_local_count} — no new fills to record")
                                # Fall through to the IOC-unfilled path below.
                                break
                            # Attribute cost to the delta contracts, not the
                            # cumulative weighted-average (R1-M2). When market
                            # moves between sibling-strategy fill and ghost-fill
                            # detection, _pos_avg ≠ actual new-fill price; the
                            # delta-cost reconstructs the new contracts' price.
                            _local_cost = self._state.get_local_position_cost_for_ticker(
                                ticker, _ghost_side)
                            _delta_cost = _pos_cost - _local_cost
                            _delta_avg = (_delta_cost // _delta
                                          if _delta and _delta_cost > 0
                                          else _pos_avg)
                            logging.error(
                                f"GHOST_FILL_DETECTED_VIA_POSITIONS: {ticker} side={_ghost_side} "
                                f"fill polling found nothing, remaining_count={remaining_count}, "
                                f"but positions API shows {_pos_count} contracts "
                                f"(cost={_pos_cost}¢, avg={_pos_avg}¢) — new_fills={_delta} "
                                f"@ {_delta_avg}¢ (api={_pos_abs}, local_was={_local_count})")
                            self._state.record_position_from_fill(
                                ticker=ticker,
                                event_ticker=candidate["event_ticker"],
                                asset=candidate["asset"],
                                side=_ghost_side,
                                count=_delta,
                                price_cents=_delta_avg,
                                strategy=candidate.get("strategy"),
                                seconds_to_close=order_info.get("seconds_to_close_at_submit"),
                                fill_latency=round(time.time() - order_info["submit_time"], 3),
                                vol_regime=candidate.get("vol_regime"),
                                calibrated_prob=candidate.get("calibrated_prob"),
                                edge=candidate.get("edge"),
                                kelly_f=candidate.get("kelly_f"),
                                is_taker=True,
                                fill_source="ghost_fill_positions_api",
                                execution_method="ioc",
                                escalation_type=candidate.get("escalation_type"),
                                maker_price_cents=candidate.get("maker_price_cents"),
                                maker_wait_seconds=candidate.get("maker_wait_seconds"),
                            )
                            self._state.mark_order_status(order_id, "filled")
                            if (candidate.get("entry_path") != "confirmation_addon"
                                    and not _is_ladder_retry):
                                self._session_ioc_fills += 1
                            order_info["filled_count"] = _delta
                            return order_info
        except Exception as e:
            logging.warning(f"Ghost fill positions API check failed for {ticker}: {e}")

        # IOC auto-cancels unfilled portion — no manual cancel needed
        self._state.mark_order_status(order_id, "canceled")
        if (candidate.get("entry_path") != "confirmation_addon"
                and not _is_ladder_retry):
            self._session_ioc_unfilled += 1
        self._logger.log_order({
            "action": "taker_ioc_unfilled",
            "ticker": ticker,
            "order_id": order_id,
            "remaining_count": remaining_count,
        })
        logging.warning(f"Taker IOC not filled: {ticker} (remaining={remaining_count})")
        return None

    # ── Maker-tail-after-IOC-partial ──────────────────────────────────────
    # Apr 25 2026: when IOC fills 9 of 50 because top of book is thin,
    # the unfilled 41 used to die on cancel ($0 EV). For high-conviction
    # strategies (DC tiers + TM-99/-98 + weekend/overnight discounts),
    # leaving a post_only=True GTC limit at the IOC price for a short TTL
    # gives benign rotation flow a chance to fill the remainder, with
    # adverse selection as the offsetting risk. Maker fee = $0, so the
    # only cost is escrowed capital + adverse-selection PnL.
    # Caps + min STC + min remainder bound the worst case.
    # See kb/decisions/maker-tail-after-ioc-partial.md (TBD).

    def _strategy_max_entry_price(self, strategy: str) -> int:
        """Per-strategy MAX_ENTRY_PRICE for ladder escalation cap.

        decided_t2 / decided_t2_z25 cap at DECIDED_CONTRACT_T2_MAX_PRICE
        (96¢) — escalating past it would put us in territory the
        strategy never endorsed (T2 only applies 93-96¢).
        All other eligible strategies cap at the global MAX_ENTRY_PRICE.
        """
        if strategy in ("decided_t2", "decided_t2_z25"):
            return DECIDED_CONTRACT_T2_MAX_PRICE
        return MAX_ENTRY_PRICE

    def _maybe_ladder_escalate(self, candidate: Dict, original_limit: int,
                               remaining: int, ioc_filled: int) -> Dict:
        """After an IOC partial fill, retry ONCE at +1¢ for the
        unfilled remainder.

        Returns dict with at least {"escalated": bool}; on success also
        carries {"escalated_filled": int, "escalated_limit": int}.

        Gates (any failure → silent skip, no exception):
          1. LADDER_ESCALATION_ENABLED kill switch
          2. ioc_filled > 0 (zero fill = phantom; don't push into another)
          3. remaining >= LADDER_ESCALATION_MIN_REMAINDER
          4. strategy in LADDER_ESCALATION_ELIGIBLE_STRATEGIES
          5. NOT already a ladder retry (recursion guard)
          6. escalated price ≤ strategy MAX_ENTRY_PRICE AND ≤ global cap
        """
        result = {"escalated": False}
        # Gate 1: kill switch.
        if not LADDER_ESCALATION_ENABLED:
            return result
        # Gate 2: zero fill = phantom-book signal; don't escalate.
        if ioc_filled <= 0:
            return result
        # Gate 3: min remainder.
        if remaining < LADDER_ESCALATION_MIN_REMAINDER:
            return result
        # Gate 4: eligible strategy.
        strategy = candidate.get("strategy") or ""
        if strategy not in LADDER_ESCALATION_ELIGIBLE_STRATEGIES:
            return result
        # Gate 5: recursion guard.
        if candidate.get("_is_ladder_retry"):
            return result
        # Gate 6: per-strategy + global price ceiling.
        strategy_max = self._strategy_max_entry_price(strategy)
        escalated_limit = original_limit + LADDER_ESCALATION_OFFSET
        if escalated_limit > strategy_max or escalated_limit > MAX_ENTRY_PRICE:
            logging.info(
                "LADDER_ESCALATION_AT_CAP: %s strategy=%s original=%dc "
                "would_escalate_to=%dc cap=%dc — skipping",
                candidate.get("ticker", "?"), strategy, original_limit,
                escalated_limit, min(strategy_max, MAX_ENTRY_PRICE))
            return result
        # Gate 7: re-check per-ticker risk cap. The retry adds size
        # to the same ticker; aggregate exposure (existing positions
        # which now include the parent's just-recorded fill +
        # remaining*escalated_limit) must remain inside MAX_TICKER_RISK.
        # Re-checking here is required because callers (execute(),
        # _execute_*_taker) gate at scan time before the parent IOC,
        # but we're inside _submit_taker by the time we reach here —
        # the caller's gate is bypassed for the retry. Fail-closed on
        # any error.
        ticker = candidate.get("ticker", "")
        try:
            balance = candidate.get("balance_at_scan") or 0
            if balance <= 0:
                # No balance signal — fail closed.
                logging.warning(
                    "LADDER_ESCALATION_NO_BALANCE: %s strategy=%s — "
                    "skipping (cannot validate ticker cap)",
                    ticker, strategy)
                return result
            existing_ticker_cost = sum(
                p.get("total_cost_cents", 0)
                for p in self._state.get_open_positions()
                if p.get("ticker") == ticker)
            ticker_cap_cents = balance * MAX_TICKER_RISK
            retry_cost = remaining * escalated_limit
            if existing_ticker_cost + retry_cost > ticker_cap_cents:
                logging.info(
                    "LADDER_ESCALATION_TICKER_CAP_BLOCKED: %s "
                    "strategy=%s existing=%dc retry_cost=%dc cap=%dc",
                    ticker, strategy, existing_ticker_cost,
                    retry_cost, int(ticker_cap_cents))
                return result
        except Exception:
            # Fail-closed on any error reading state.
            logging.warning(
                "LADDER_ESCALATION_CAP_CHECK_RAISED: %s strategy=%s — "
                "skipping defensively", ticker, strategy, exc_info=True)
            return result
        # Gate 8: explicit pre-retry phantom check. The recursive
        # _submit_taker's PHANTOM_ABORT branch only fires when
        # candidate.ob_snapshot.ask_depth is an int — but we set it to
        # None on the retry to avoid stale-depth artifacts (the parent
        # ob_snapshot referred to the original price level). That
        # silent skip would leave the retry with NO catastrophic-tail
        # guard. Round 3 fix: do an explicit fresh REST orderbook
        # fetch here and abort if the escalated level shows zero
        # depth. Failures (None / exception) → fail-closed skip.
        try:
            _ob_raw = self._client.get_orderbook(ticker)
            if not isinstance(_ob_raw, dict):
                logging.info(
                    "LADDER_ESCALATION_OB_UNAVAILABLE: %s strategy=%s "
                    "— skipping retry (cannot verify depth)",
                    ticker, strategy)
                return result
            # Kalshi REST returns three possible shapes (mirror prod
            # unwrap at bot/_impl.py:15810-15812 + 16419-16421):
            #   1. {"orderbook_fp": {"yes_dollars": [["0.99","48"]...]}}
            #      — current FP schema (Mar 2026 migration)
            #   2. {"orderbook": {"yes": [[99, 48], ...]}} — wrapped legacy
            #   3. {"yes": [[99, 48], ...]} — unwrapped (WS cache, older)
            # Try FP first (matches prod ordering), then wrapped/unwrapped.
            _ob_fp = _ob_raw.get("orderbook_fp")
            if _ob_fp:
                _ob = convert_orderbook_fp(_ob_fp)
            else:
                _ob = _ob_raw.get("orderbook", _ob_raw)
            if not isinstance(_ob, dict):
                logging.info(
                    "LADDER_ESCALATION_OB_MALFORMED: %s strategy=%s — "
                    "skipping retry", ticker, strategy)
                return result
            # YES-side ladder. We're a YES BUYER with limit at
            # escalated_limit. Kalshi will match our IOC against any
            # YES ask priced AT-OR-BELOW our limit. Depth check sums
            # those levels.
            _yes_levels = _ob.get("yes") or []
            _depth_at_or_below_limit = 0
            for lvl in _yes_levels:
                # Tolerate malformed levels: must be (price, qty) pair.
                if not (isinstance(lvl, (list, tuple)) and len(lvl) >= 2):
                    continue
                try:
                    _lp = int(lvl[0])
                    _lq = int(lvl[1])
                except (TypeError, ValueError):
                    continue
                if _lp <= escalated_limit and _lq > 0:
                    _depth_at_or_below_limit += _lq
            if _depth_at_or_below_limit <= 0:
                logging.warning(
                    "LADDER_ESCALATION_PHANTOM_ABORT: %s strategy=%s "
                    "escalated=%dc — fresh orderbook shows 0 depth "
                    "at <= limit; refusing retry",
                    ticker, strategy, escalated_limit)
                return result
        except Exception:
            logging.warning(
                "LADDER_ESCALATION_OB_CHECK_RAISED: %s strategy=%s — "
                "skipping defensively", ticker, strategy, exc_info=True)
            return result
        # Build the retry candidate. Carry _is_ladder_retry=True to
        # block recursive escalation AND recursive maker-tail.
        # Replace position_size with remaining; reset best_yes_ask to
        # the escalated limit so the smart-IOC-picker / drift-checks
        # operate on the right reference price.
        # CRITICAL: do NOT rename strategy. STRATEGY_CLAMP_POLICY and
        # STRATEGY_LIMIT_BUMP_RESERVE_CENTS are looked up by exact
        # string match — renaming silently routes the retry through
        # the default policy, which is top_of_book (not no_clamp). The
        # per-strategy IOC mechanics MUST be preserved on the retry.
        # Telemetry separation lives in the _is_ladder_retry flag and
        # the LADDER_ESCALATION_ATTEMPT log line, NOT the strategy
        # column.
        retry_candidate = dict(candidate)
        retry_candidate["_is_ladder_retry"] = True
        retry_candidate["position_size"] = remaining
        retry_candidate["best_yes_ask"] = escalated_limit
        # Drop the parent's ob_snapshot — its ask_depth value applies
        # to the original price level, not the escalated one. The
        # downstream PHANTOM_ABORT in _submit_taker no-ops on None,
        # but Gate 8 above did the equivalent check explicitly.
        retry_candidate["ob_snapshot"] = None
        logging.info(
            "LADDER_ESCALATION_ATTEMPT: %s strategy=%s original=%dc "
            "escalated=%dc remaining=%d ioc_filled=%d",
            ticker, strategy, original_limit,
            escalated_limit, remaining, ioc_filled)
        self._session_ladder_escalations += 1
        # Submit the retry IOC. Returns None on failure / no fill — we
        # surface that as escalated=True (we attempted) but no fill so
        # the caller still falls through to maker tail at original.
        retry_result = self._submit_taker(retry_candidate)
        result["escalated"] = True
        result["escalated_limit"] = escalated_limit
        result["escalated_filled"] = (
            (retry_result or {}).get("filled_count", 0))
        return result

    def _maybe_post_maker_tail(self, candidate: Dict, ioc_price: int,
                               remaining: int,
                               ioc_filled: int = 1) -> bool:
        """Post the unfilled IOC remainder as a post_only GTC limit
        if all gates pass. Returns True iff a maker order was placed.

        Gates (any failure → silent skip, no exception):
          1. ioc_filled > 0 (zero fill = phantom book, don't rest)
          2. remaining >= MAKER_TAIL_MIN_REMAINDER
          3. STC >= MAKER_TAIL_MIN_STC_SECONDS
          4. strategy in MAKER_TAIL_ELIGIBLE_STRATEGIES
          5. per-asset cap not breached
          6. global cap not breached
          7. NOT a ladder retry (the original IOC owns the tail)
        """
        # Gate 1: zero fill = phantom-book IOC; don't rest into nothing.
        if ioc_filled <= 0:
            return False
        # Gate 7: ladder retries must not post their own maker tail.
        # The original (outer) IOC's _submit_taker will post the tail
        # at the ORIGINAL price after the retry returns. Letting the
        # retry post its own tail at the ESCALATED price would create
        # two overlapping tails on the same ticker — the spec is one
        # tail at the original price.
        if candidate.get("_is_ladder_retry"):
            return False
        # Gate 2: min remainder.
        if remaining < MAKER_TAIL_MIN_REMAINDER:
            return False
        # Gate 3: min STC.
        stc = candidate.get("seconds_to_close")
        if stc is None or stc < MAKER_TAIL_MIN_STC_SECONDS:
            return False
        # Gate 4: eligible strategy.
        strategy = candidate.get("strategy") or ""
        if strategy not in MAKER_TAIL_ELIGIBLE_STRATEGIES:
            return False
        asset = candidate.get("asset") or "?"
        # Gate 5+6: concurrency caps. Active = entries in
        # self._maker_tails. Per-asset and global checked together so
        # a single pass over the dict suffices.
        per_asset_active = sum(
            1 for r in self._maker_tails.values()
            if r.get("asset") == asset)
        global_active = len(self._maker_tails)
        if per_asset_active >= MAKER_TAIL_MAX_PER_ASSET:
            self._session_maker_tails_skipped_cap += 1
            logging.info(
                "MAKER_TAIL_SKIP_CAP_ASSET: %s asset=%s strategy=%s "
                "per_asset_active=%d cap=%d",
                candidate.get("ticker", "?"), asset, strategy,
                per_asset_active, MAKER_TAIL_MAX_PER_ASSET)
            return False
        if global_active >= MAKER_TAIL_MAX_GLOBAL:
            self._session_maker_tails_skipped_cap += 1
            logging.info(
                "MAKER_TAIL_SKIP_CAP_GLOBAL: %s asset=%s strategy=%s "
                "global_active=%d cap=%d",
                candidate.get("ticker", "?"), asset, strategy,
                global_active, MAKER_TAIL_MAX_GLOBAL)
            return False
        # Submit. post_only=True is critical — never let this become
        # an unintended taker (would cross our own scan-time best ask
        # if the book moved).
        ticker = candidate.get("ticker") or ""
        side = candidate.get("side", "yes")
        client_oid = str(uuid.uuid4())
        _price_kwarg = (
            {"no_price": ioc_price} if side == "no"
            else {"yes_price": ioc_price})
        # Settlement-race gate (defense-in-depth): MAKER_TAIL_MIN_STC_SECONDS
        # currently dominates this check, but if that floor is ever lowered
        # below MIN_ORDER_SUBMIT_STC_S, this prevents the regression.
        if self._should_skip_near_close(candidate):
            self._abort_near_close(candidate, path="maker_tail")
            return False
        try:
            resp = self._client.place_order(
                ticker=ticker, side=side, action="buy",
                count=remaining, client_order_id=client_oid,
                time_in_force="good_till_canceled",
                post_only=True, **_price_kwarg)
        except Exception:
            logging.warning(
                "MAKER_TAIL_PLACE_FAILED: %s asset=%s strategy=%s "
                "count=%d price=%d", ticker, asset, strategy,
                remaining, ioc_price, exc_info=True)
            return False
        if resp is None:
            logging.warning(
                "MAKER_TAIL_PLACE_NONE: %s asset=%s strategy=%s "
                "count=%d price=%d (place_order returned None)",
                ticker, asset, strategy, remaining, ioc_price)
            return False
        order_id = (resp.get("order") or {}).get(
            "order_id", client_oid)
        now_mono = time.monotonic()
        self._maker_tails[order_id] = {
            "asset": asset,
            "ticker": ticker,
            "strategy": strategy,
            "count": remaining,
            "price_cents": ioc_price,
            "client_order_id": client_oid,
            "posted_monotonic": now_mono,
            "expires_monotonic": now_mono + MAKER_TAIL_TTL_SECONDS,
        }
        self._session_maker_tails_posted += 1
        logging.info(
            "MAKER_TAIL_POSTED: %s asset=%s strategy=%s count=%d "
            "price=%d¢ ttl=%ds order_id=%s (per_asset_active=%d "
            "global_active=%d)",
            ticker, asset, strategy, remaining, ioc_price,
            MAKER_TAIL_TTL_SECONDS, order_id,
            per_asset_active + 1, global_active + 1)
        return True

    def _sweep_maker_tails(self) -> None:
        """Cancel any maker tail past its TTL. Called from tick().

        Records are dropped from _maker_tails AFTER the cancel call
        completes (success or failure) — this prevents a leaked record
        if cancel raises, and prevents a permanent lock on the per-
        asset cap if Kalshi 404s the order.
        """
        if not self._maker_tails:
            return
        now = time.monotonic()
        expired_oids = [
            oid for oid, rec in self._maker_tails.items()
            if rec.get("expires_monotonic", 0) <= now]
        for oid in expired_oids:
            rec = self._maker_tails.pop(oid, None)
            if rec is None:
                continue
            try:
                self._client.cancel_order(oid, ticker=rec.get("ticker"))
            except Exception:
                logging.warning(
                    "MAKER_TAIL_CANCEL_FAILED: order_id=%s ticker=%s "
                    "(record dropped from tracker regardless)",
                    oid, rec.get("ticker", "?"), exc_info=True)
            self._session_maker_tails_cancelled_ttl += 1
            logging.info(
                "MAKER_TAIL_CANCELLED_TTL: order_id=%s ticker=%s "
                "asset=%s strategy=%s age=%.1fs",
                oid, rec.get("ticker", "?"), rec.get("asset", "?"),
                rec.get("strategy", "?"),
                now - rec.get("posted_monotonic", now))

    # ── Fill detection ────────────────────────────────────────────────────

    def _check_for_fill(self, order: Dict) -> Optional[Dict]:
        """Check if order has been filled via REST fills endpoint.

        Tracks seen fill IDs on the order dict to avoid double-counting
        partial fills on consecutive polls.
        """
        min_ts = int(order["submit_time"])
        resp = self._client.get_fills(
            ticker=order["ticker"], min_ts=min_ts
        )
        if LOG_RAW_IOC_FILLS and resp:
            append_raw_api_journal({
                "kind": "ioc_fills",
                "ticker": order["ticker"],
                "order_id": order.get("order_id"),
                "submit_count": order.get("count"),
                "min_ts": min_ts,
                "resp": resp,
            })
        if not resp or not resp.get("fills"):
            return None

        seen = order.setdefault("_seen_fill_ids", set())
        for fill in resp["fills"]:
            fill_id = fill.get("trade_id") or fill.get("id")
            if not fill_id:
                logging.warning(f"REST fill missing trade_id/id for {order['ticker']} — skipping to avoid double-count")
                continue
            if fill.get("order_id") == order["order_id"] and fill_id not in seen:
                seen.add(fill_id)
                return fill
        return None

    # ── Fill handling ─────────────────────────────────────────────────────

    def _on_fill(self, fill: Dict, order: Dict) -> int:
        """Handle fill: update SQLite, log trade, record position.

        Returns the fill_count recorded to the positions table so callers
        can track partial vs complete fills. Returns 0 (with a
        POST_COMPLETION_FILL_DROPPED WARNING) when the fill arrived after
        the order was already complete and was dropped without writing any
        side effects (positions row, trade journal, lifecycle snapshot,
        order-status mark, Telegram alert) — see the 2026-05-19 HYPE
        incident defense at the top of the function body.
        """
        order_id = order["order_id"]
        ticker = order["ticker"]
        candidate = order["candidate"]

        # Extract fill details — prefer FP/dollar fields, fall back to legacy
        try:
            raw_fill_count = fp_str_to_int(fill.get("count_fp"))
            if not raw_fill_count:
                raw_fill_count = fill.get("count") or order["count"]
            raw_fill_count = int(raw_fill_count)
        except (TypeError, ValueError, OverflowError):
            logging.warning(
                "ON_FILL_COUNT_PARSE_MALFORMED: %s order=%s count_fp=%r "
                "count=%r — skip (leave stamped so the taker loop "
                "cannot livelock on this row)",
                ticker, order_id, fill.get("count_fp"), fill.get("count"))
            return 0
        remaining = order["count"] - order.get("filled_so_far", 0)
        # Post-completion fill leak guard (2026-05-19 HYPE incident,
        # KXHYPE15M-26MAY190645-45 +10-ct phantom): when /portfolio/fills
        # returns a fill AFTER filled_so_far has already reached count
        # (duplicate trade_id, cross-order misattribution, transient
        # Kalshi over-fill, or any other source), drop it with a
        # WARNING. Without this guard, the leaky fill flowed past the
        # `> remaining > 0` cap and wrote a phantom row to positions.
        if remaining <= 0:
            logging.warning(
                "POST_COMPLETION_FILL_DROPPED: %s order=%s "
                "raw_fill_count=%d filled_so_far=%d count=%d — "
                "order already complete, discarding leaky fill",
                order["ticker"], order_id, raw_fill_count,
                order.get("filled_so_far", 0), order["count"])
            return 0
        if raw_fill_count > remaining:
            logging.warning(
                f"Fill count {raw_fill_count} exceeds remaining {remaining} for "
                f"{order['ticker']} — capping to {remaining}")
            fill_count = remaining
        else:
            fill_count = raw_fill_count
        # For NO-side orders, Kalshi returns yes_price as 100-no_price (YES-equivalent),
        # which is NOT the cost paid. Read no_price_dollars/no_price for NO fills.
        try:
            if order.get("side") == "no":
                fill_price_d = fill.get("no_price_dollars")
                fill_price = dollars_str_to_cents(fill_price_d) if fill_price_d else (fill.get("no_price") or order["price_cents"])
            else:
                fill_price_d = fill.get("yes_price_dollars")
                fill_price = dollars_str_to_cents(fill_price_d) if fill_price_d else (fill.get("yes_price") or order["price_cents"])
        except (TypeError, ValueError, OverflowError):
            logging.warning(
                "ON_FILL_PRICE_PARSE_MALFORMED: %s order=%s — using limit",
                ticker, order_id)
            fill_price = order["price_cents"]

        # Track cumulative fills for partial fill detection
        order["filled_so_far"] = min(
            order.get("filled_so_far", 0) + fill_count,
            order["count"]
        )
        is_complete = order["filled_so_far"] >= order["count"]

        # Update order status only when fully filled
        if is_complete:
            self._state.mark_order_status(order_id, "filled")
        else:
            logging.info(
                f"Partial fill: {ticker} {fill_count}/{order['count']} "
                f"(cumulative {order['filled_so_far']}/{order['count']})")

        # Log fill model sample for ML training
        self._log_fill_model_sample(order, "filled", fill=fill)

        # Track fill latency
        fill_latency = round(time.time() - order["submit_time"], 3)
        try:
            if self._ml:
                self._ml._recent_fill_latencies.append(fill_latency)
                self._ml._session_fill_count += 1
                if not order.get("is_taker", True):
                    self._ml._session_maker_fills += 1
        except Exception:
            logging.debug("Fill latency tracking failed", exc_info=True)
        logging.info(f"Fill latency: {fill_latency:.3f}s ({'taker' if order.get('is_taker') else 'maker'})")

        # Record position in SQLite
        self._state.record_position_from_fill(
            ticker=ticker,
            event_ticker=order["event_ticker"],
            asset=order["asset"],
            side=order.get("side", "yes"),
            count=fill_count,
            price_cents=fill_price,
            strategy=candidate.get("strategy"),
            seconds_to_close=order.get("seconds_to_close_at_submit"),
            fill_latency=fill_latency,
            vol_regime=candidate.get("vol_regime"),
            calibrated_prob=candidate.get("calibrated_prob"),
            edge=candidate.get("edge"),
            kelly_f=candidate.get("kelly_f"),
            is_taker=order.get("is_taker", False),
            fill_source=order.get("fill_source", "rest_poll"),
            execution_method=order.get("execution_method", "maker"),
            escalation_type=candidate.get("escalation_type", "none"),
            maker_price_cents=candidate.get("maker_price_cents"),
            maker_wait_seconds=candidate.get("maker_wait_seconds"),
        )

        # Invalidate scanner balance cache so next tick gets fresh balance
        try:
            if self._ml and hasattr(self._ml, 'scanner'):
                self._ml.scanner._balance_cache = (None, 0.0)
        except Exception:
            pass

        # Log trade with all required fields
        is_taker = order.get("is_taker", False)
        cost_cents = fill_count * fill_price
        fee_cents = calculate_fee(fill_count, fill_price, is_taker=is_taker)

        self._logger.log_trade({
            "ticker": ticker,
            "direction": "yes",
            "price": fill_price,
            "count": fill_count,
            "cost": cost_cents,
            "fee": fee_cents,
            "z_score": candidate.get("z_score"),
            "p_calibrated": candidate.get("calibrated_prob"),
            "balance_at_entry": order["balance_at_entry"],
            "tier": "taker" if is_taker else "maker",
            "is_taker": is_taker,
            "is_panic": order.get("is_panic", False),
            "edge": candidate.get("edge"),
            "kelly_f": candidate.get("kelly_f"),
            "asset": order["asset"],
            "event_ticker": order["event_ticker"],
            "order_id": order_id,
            "client_order_id": order["client_order_id"],
            "strategy_used": candidate.get("strategy"),
            "decision_scores": candidate.get("strategy_scores"),
            "orderbook_snapshot": candidate.get("ob_snapshot"),
        })

        logging.info(
            f"FILL: {ticker} {fill_count}x @ {fill_price}¢ "
            f"({'taker' if is_taker else 'maker'}) "
            f"cost={cost_cents}¢ fee={fee_cents}¢"
            f"{'' if is_complete else ' [PARTIAL ' + str(order['filled_so_far']) + '/' + str(order['count']) + ']'}"
        )

        # Lifecycle snapshot at fill — captures the book left behind after
        # our fill. Pairs with the submit snapshot to expose the book delta
        # and explain N→1 destruction patterns (XRP TM-96 case).
        # source = strategy (uniform vocab with submit event); execution tier
        # (taker/maker) is recoverable via order_id join with positions if needed.
        try:
            self._state.insert_order_lifecycle_snapshot(
                order_id=order_id, ticker=ticker,
                event_type=("fill" if is_complete else "partial_fill"),
                source=candidate.get("strategy"))
        except Exception:
            logging.warning("insert_order_lifecycle_snapshot (fill) failed",
                            exc_info=True)

        # ── Telegram trade alert ────────────────────────────────────────
        if _telegram_state._TELEGRAM and is_complete:
            try:
                _strategy = candidate.get("strategy") or order.get("entry_path", "core")
                _product = candidate.get("product_type", "15m")
                _edge = candidate.get("edge")
                _edge_str = f" edge={_edge:.2%}" if _edge is not None else ""
                _prob = candidate.get("calibrated_prob")
                _prob_str = f" prob={_prob:.1%}" if _prob is not None else ""
                _stc = order.get("seconds_to_close_at_submit")
                _stc_str = f" stc={_stc:.0f}s" if _stc is not None else ""
                _side = order.get("side", "yes").upper()
                _telegram_state._TELEGRAM.send(
                    f"TRADE: {order['asset']} {_side} {fill_count}ct @ {fill_price}c "
                    f"({'taker' if is_taker else 'maker'}) "
                    f"[{_product}/{_strategy}]{_prob_str}{_edge_str}{_stc_str} "
                    f"${cost_cents / 100:.2f}",
                    dedup_key=f"fill_{ticker}")
            except Exception:
                logging.debug("Trade telegram alert failed", exc_info=True)

        # ── Sub-floor fill alert ────────────────────────────────────────
        # Monitor fills below asset's MIN_ENTRY_PRICE. Position is already
        # recorded above — this is monitoring only, never blocks.
        _ASSET_FLOOR_MAP = {
            "BTC": BTC_MIN_ENTRY_PRICE, "ETH": ETH_MIN_ENTRY_PRICE,
            "SOL": SOL_MIN_ENTRY_PRICE, "XRP": XRP_MIN_ENTRY_PRICE,
            "HYPE": HYPE_MIN_ENTRY_PRICE, "DOGE": DOGE_MIN_ENTRY_PRICE,
            "BNB": BNB_MIN_ENTRY_PRICE,
        }
        _fill_asset = order["asset"]
        _fill_floor = _ASSET_FLOOR_MAP.get(_fill_asset, MIN_ENTRY_PRICE)
        if fill_price < _fill_floor:
            _floor_gap = _fill_floor - fill_price
            _nbbo_at_eval = order.get("price_cents", fill_price)
            logging.warning(
                "SUB_FLOOR_FILL: %s %s %dct @ %dc (floor %dc, gap %dc, NBBO %dc)",
                ticker, _fill_asset, fill_count, fill_price,
                _fill_floor, _floor_gap, _nbbo_at_eval)
            if _telegram_state._TELEGRAM:
                try:
                    _telegram_state._TELEGRAM.send(
                        f"\u26a0\ufe0f SUB-FLOOR FILL: {_fill_asset} {fill_price}c "
                        f"(floor {_fill_floor}c) NBBO={_nbbo_at_eval}c gap={_floor_gap}c "
                        f"{fill_count}ct ${cost_cents / 100:.2f} exposure",
                        dedup_key=f"subfloor_{ticker}")
                except Exception:
                    logging.debug("Sub-floor Telegram alert failed", exc_info=True)

        # Register for confirmation addon evaluation (only on complete fills)
        if is_complete:
            try:
                self._register_addon_eligible(order, fill_price, fill_latency)
            except Exception:
                logging.debug("addon registration failed", exc_info=True)

        return fill_count

    # ── Fill Model Logging ────────────────────────────────────────────────

    def _log_fill_model_sample(self, order: Dict, outcome: str,
                               fill: Optional[Dict] = None,
                               cancel_reason: Optional[str] = None):
        """Write one fill_model_sample to FILL_MODEL_JOURNAL for ML training.

        Sprint B Bit B.2a (2026-05-12) NULL audit + writer fixes
        (ticket 86b9vfznd). Production journal had several 100%-NULL
        columns; classified each as:

        - (i) writer bug -> ``fill_source`` was never set on IOC fills
          (the taker path doesn't go through ``_check_for_fill``'s
          ws/rest attribution). Now defaults to ``"ioc_inline"`` for
          IOC outcome=filled when no upstream tag exists.
        - (ii) only-when-applicable -> kept the column but added a
          predicate alongside so downstream ML can distinguish
          "NULL by design" from "missing data":
            * ``queue_position_polled`` (bool) - was the queue ever sampled?
              Maker orders polling fires every 5s; orders filled <5s
              legitimately have ``queue_position_final=None``.
            * ``ob_snapshot_source`` (str) - ``"scanner"`` |
              ``"addon_empty"`` | ``"missing"``. confirmation_addon /
              dip_addon paths set ``ob_snapshot={}`` because no fresh
              scanner OB exists mid-execution; this column distinguishes
              that from a true missing OB snapshot.
        - (iii) deprecated -> ``queue_position_initial`` (never written
          anywhere) and ``convergence_velocity`` (lives only on scanner
          helper dicts, never on the ``candidate`` dict) removed from
          the output entirely.

        See ``agent_docs/db_schema.md`` "fill_model_journal.jsonl"
        for the per-column predicate map.
        """
        try:
            candidate = order.get("candidate", {})
            now = time.time()
            elapsed = now - order["submit_time"]
            fill_latency = round(elapsed, 3) if outcome == "filled" else None
            ob_snap_raw = candidate.get("ob_snapshot")
            # ob_snapshot_source classification (B.2a predicate)
            if ob_snap_raw is None:
                ob_snapshot_source = "missing"
                ob_snap = {}
            elif ob_snap_raw == {}:
                # confirmation_addon / dip_addon set ob_snapshot={}
                # because no fresh scanner OB exists mid-execution.
                ob_snapshot_source = "addon_empty"
                ob_snap = {}
            else:
                ob_snapshot_source = "scanner"
                ob_snap = ob_snap_raw

            # B.2a (i) writer-bug fix: IOC fills now record a default
            # fill_source so the column isn't 100% NULL on taker rows.
            # Maker WS/REST paths still set order["fill_source"]
            # upstream -- that value wins.
            fill_source = order.get("fill_source")
            if fill_source is None and outcome == "filled" and order.get("is_taker"):
                fill_source = "ioc_inline"

            # B.2a (ii) predicate: was queue position polled at all?
            # Polling fires every 5s; maker orders that fill <5s
            # legitimately have queue_position_final=None.
            queue_position_polled = bool(order.get("_last_queue_poll", 0) > 0)

            sample = {
                "type": "fill_model_sample",
                "ts": datetime.datetime.utcnow().isoformat() + "Z",
                "ticker": order["ticker"],
                "asset": order["asset"],
                "outcome": outcome,
                "fill_latency_s": fill_latency,
                "fill_source": fill_source,
                # Submission context
                "price_cents": order["price_cents"],
                "fair_value": candidate.get("best_yes_ask"),
                "offset_cents": (candidate.get("best_yes_ask", 0) - order["price_cents"])
                    if candidate.get("best_yes_ask") else None,
                "count": order["count"],
                "post_only": not order.get("is_taker", False),
                # Market context at submission
                "seconds_to_close": order.get("seconds_to_close_at_submit"),
                "vol_regime": candidate.get("vol_regime"),
                "blended_rv": candidate.get("blended_rv"),
                "ask_depth": ob_snap.get("ask_depth"),
                "total_ob_depth": ob_snap.get("total_depth"),
                "spread_at_submit": ob_snap.get("spread"),
                "bid_depth": ob_snap.get("bid_depth"),
                "ob_snapshot_source": ob_snapshot_source,
                "z_score": candidate.get("z_score"),
                "edge": candidate.get("edge"),
                "kelly_f": candidate.get("kelly_f"),
                # Queue tracking
                "queue_position_final": order.get("queue_position"),
                "queue_position_polled": queue_position_polled,
                # Execution details
                "execution_method": order.get("execution_method", "maker"),
                "entry_path": order.get("entry_path", "maker"),
                "cancel_reason": cancel_reason,
                "elapsed_seconds": round(elapsed, 1),
                # WS state
                "ws_connected": (self._kalshi_feed.is_connected
                                 if self._kalshi_feed else False),
                # Config stamps for regime-filtered analysis
                "maker_only_threshold": MAKER_ONLY_THRESHOLD,
            }

            with open(FILL_MODEL_JOURNAL, "a") as f:
                f.write(json.dumps(sample) + "\n")
        except Exception:
            logging.debug("fill_model_sample write failed", exc_info=True)

    # ── Confirmation Addon ─────────────────────────────────────────────────

    def _register_addon_eligible(self, order: Dict,
                                actual_fill_price: int = 0,
                                fill_latency: float = 0.0):
        """After a fill, register the position for addon evaluation.

        Uses actual fill price (not maker limit price) and corrects STC
        for fill latency so addon timing is accurate.

        Skips if the fill itself is an addon (prevents recursive registration).
        """
        if not ADDON_ENABLED:
            return
        candidate = order.get("candidate", {})
        # Don't re-register addon fills
        if candidate.get("entry_path") in ("confirmation_addon", "dip_addon", "tm_taker", "bracket_no_taker"):
            return

        ticker = order["ticker"]
        # Use actual execution price, not the submitted limit price
        entry_price = actual_fill_price if actual_fill_price > 0 else order["price_cents"]
        fill_count = order.get("filled_so_far", order["count"])

        # Correct STC: subtract fill latency from submit-time STC
        stc_at_submit = order.get("seconds_to_close_at_submit")
        stc_at_fill = (stc_at_submit - fill_latency) if stc_at_submit is not None else None

        meta = {
            "ticker": ticker,
            "event_ticker": order["event_ticker"],
            "asset": order["asset"],
            "entry_price_cents": entry_price,
            "entry_count": fill_count,
            "fill_time": time.time(),
            "seconds_to_close_at_fill": stc_at_fill,
            "threshold": candidate.get("threshold"),
            "blended_rv": candidate.get("blended_rv"),
            "calibrated_prob": candidate.get("calibrated_prob"),
            "product_type": candidate.get("product_type"),
            "candidate": candidate,
        }
        self._addon_eligible[ticker] = meta
        logging.info(
            "addon_registered: %s entry=%d¢ count=%d stc=%.0f",
            ticker, entry_price, fill_count, stc_at_fill or 0)

    def _check_addon_opportunities(self):
        """Evaluate open positions for confirmation addon. Called from _tick."""
        if OBSERVATION_MODE:
            return  # Never execute addons in observation mode
        if not ADDON_ENABLED or not self._addon_eligible:
            return

        now = time.time()
        expired = []

        for ticker, meta in list(self._addon_eligible.items()):
            # Skip hourly fills — addons are for 15M maker-first price improvement only.
            # Hourly uses fixed-size taker IOC; addon would bypass hourly constraints
            # (price cap, fixed sizing, asset exclusion). (Bug fix: Mar 24 2026)
            if meta.get("product_type") == "hourly":
                expired.append(ticker)
                continue

            # Cleanup: remove entries >5min old
            if now - meta["fill_time"] > 300:
                expired.append(ticker)
                continue

            # Already addon'd this position
            if ticker in self._addon_completed:
                continue

            # Elapsed check
            elapsed = now - meta["fill_time"]
            if elapsed < ADDON_MIN_SECONDS_SINCE_FILL:
                continue

            # STC check
            stc_at_fill = meta.get("seconds_to_close_at_fill")
            if stc_at_fill is None:
                continue
            current_stc = stc_at_fill - elapsed
            if current_stc < ADDON_MIN_STC_REMAINING:
                self._session_addon_skipped += 1
                logging.info(
                    "addon_SKIP_stc: %s stc_remaining=%.0f < %.0f",
                    ticker, current_stc, ADDON_MIN_STC_REMAINING)
                expired.append(ticker)
                continue

            # Get current best ask
            current_ask = self._get_addon_best_ask(ticker)
            if current_ask is None:
                continue  # Deferred to next tick

            # Price improvement check
            improvement = current_ask - meta["entry_price_cents"]
            if improvement < ADDON_MIN_PRICE_IMPROVEMENT:
                continue  # Not enough improvement yet

            # Price cap
            if current_ask > ADDON_MAX_ENTRY_PRICE:
                self._session_addon_skipped += 1
                logging.info(
                    "addon_SKIP_price_cap: %s ask=%d¢ > %d¢",
                    ticker, current_ask, ADDON_MAX_ENTRY_PRICE)
                continue

            # Get current spot price
            asset = meta["asset"]
            spot = self._get_addon_spot(asset)
            if spot is None:
                continue

            # Recalculate probability with current spot and STC
            blended_rv = meta.get("blended_rv")
            try:
                if self._ml and hasattr(self._ml, 'vol'):
                    fresh_vol = self._ml.vol._cache.get(asset)
                    if fresh_vol and fresh_vol.get("blended_rv"):
                        blended_rv = fresh_vol["blended_rv"]
            except Exception:
                pass
            threshold = meta.get("threshold")
            if blended_rv is None or threshold is None:
                continue

            prob_result = ProbabilityEngine.compute(
                self._addon_decision_spot(asset, spot), threshold, current_stc,
                blended_rv, asset=asset,
                product_type=meta.get("candidate", {}).get("product_type"))
            cal_prob = prob_result.get("calibrated_prob")
            if cal_prob is None:
                continue

            # Taker edge check
            addon_count = max(1, int(meta["entry_count"] * ADDON_SIZE_FRACTION))
            taker_fee = calculate_taker_fee(addon_count, current_ask)
            net_edge = cal_prob - (current_ask / 100.0) - (taker_fee / (addon_count * 100.0))

            if net_edge < MIN_EDGE_PCT / 100.0:
                self._session_addon_skipped += 1
                logging.info(
                    "addon_SKIP_edge: %s net_edge=%.4f < %.4f ask=%d¢ "
                    "prob=%.4f fee=%d¢",
                    ticker, net_edge, MIN_EDGE_PCT / 100.0,
                    current_ask, cal_prob, taker_fee)
                continue

            # Balance check — addon cost capped at 50% of current balance
            balance = self._get_addon_balance()
            if balance is None:
                continue

            addon_cost = addon_count * current_ask
            max_addon_cost = int(balance * 0.50)
            if addon_cost > max_addon_cost:
                # Reduce count to fit within 50% of balance
                if current_ask > 0:
                    addon_count = max_addon_cost // current_ask
                if addon_count < 1:
                    self._session_addon_skipped += 1
                    logging.info(
                        "addon_SKIP_balance: %s cost=%d¢ > 50%% balance=%d¢",
                        ticker, addon_cost, balance)
                    continue
                addon_cost = addon_count * current_ask
                taker_fee = calculate_taker_fee(addon_count, current_ask)
                net_edge = cal_prob - (current_ask / 100.0) - (taker_fee / (addon_count * 100.0))
                if net_edge < MIN_EDGE_PCT / 100.0:
                    self._session_addon_skipped += 1
                    logging.info(
                        "addon_SKIP_edge_after_resize: %s count=%d edge=%.4f",
                        ticker, addon_count, net_edge)
                    continue

            # All checks passed — execute addon
            filled = self._execute_addon(
                meta, addon_count, current_ask, current_stc,
                cal_prob, net_edge, balance, spot)
            if filled:
                self._addon_completed.add(ticker)

        # Cleanup expired entries
        for t in expired:
            self._addon_eligible.pop(t, None)
        # Also clean completed tickers no longer in eligible
        for t in list(self._addon_completed):
            if t not in self._addon_eligible:
                self._addon_completed.discard(t)

    def _execute_addon(self, meta: Dict, count: int, price: int,
                       stc: float, prob: float, edge: float,
                       balance: int, spot: float) -> bool:
        """Submit taker IOC for confirmation addon. Returns True on fill."""
        ticker = meta["ticker"]
        self._session_addon_attempts += 1

        logging.info(
            "addon_TRIGGER: %s %dx @ %d¢ (entry=%d¢ +%d¢) "
            "stc=%.0f edge=%.4f prob=%.4f balance=%d¢ spot=%.2f",
            ticker, count, price, meta["entry_price_cents"],
            price - meta["entry_price_cents"],
            stc, edge, prob, balance, spot)

        # Build addon candidate for _submit_taker
        addon_candidate = {
            "ticker": ticker,
            "event_ticker": meta["event_ticker"],
            "asset": meta["asset"],
            "best_yes_ask": price,
            "position_size": count,
            "calibrated_prob": prob,
            "edge": edge,
            "seconds_to_close": stc,
            "balance_at_scan": balance,
            "entry_path": "confirmation_addon",
            "strategy": "CONFIRMATION_ADDON",
            "blended_rv": meta.get("blended_rv"),
            "threshold": meta.get("threshold"),
            "vol_regime": meta.get("candidate", {}).get("vol_regime"),
            "z_score": meta.get("candidate", {}).get("z_score"),
            "kelly_f": meta.get("candidate", {}).get("kelly_f"),
            "ob_snapshot": {},
            "original_entry_price": meta["entry_price_cents"],
            "original_entry_count": meta["entry_count"],
            "price_improvement": price - meta["entry_price_cents"],
        }

        if OBSERVATION_MODE:
            logging.info(
                "addon_OBSERVATION: %s %dx @ %d¢ — would submit taker IOC",
                ticker, count, price)
            self._logger.log_execution({
                "action": "addon_observation",
                "ticker": ticker,
                "asset": meta["asset"],
                "price": price,
                "count": count,
                "edge": edge,
                "prob": prob,
                "stc": stc,
                "original_entry_price": meta["entry_price_cents"],
                "price_improvement": price - meta["entry_price_cents"],
            })
            return True

        # Live: submit taker IOC
        result = self._submit_taker(addon_candidate)

        if result is not None:
            self._session_addon_fills += 1
            logging.info(
                "addon_FILLED: %s %dx @ %d¢ (+%d¢ from entry)",
                ticker, count, price,
                price - meta["entry_price_cents"])
            if _telegram_state._TELEGRAM:
                try:
                    _addon_cost = count * price / 100
                    _telegram_state._TELEGRAM.send(
                        f"\u2795 Addon: {meta.get('asset', '?')} {count}ct @ {price}c "
                        f"(${_addon_cost:.2f}, +{price - meta['entry_price_cents']}c slip)")
                except Exception:
                    pass

            self._logger.log_execution({
                "action": "addon_filled",
                "ticker": ticker,
                "asset": meta["asset"],
                "price": price,
                "count": count,
                "edge": edge,
                "prob": prob,
                "stc": stc,
                "original_entry_price": meta["entry_price_cents"],
                "price_improvement": price - meta["entry_price_cents"],
                "balance_after": balance - (count * price) - calculate_taker_fee(count, price),
            })
            return True
        else:
            self._session_addon_unfilled += 1
            self._addon_completed.add(ticker)  # Don't retry — single attempt
            logging.warning(
                "addon_UNFILLED: %s %dx @ %d¢", ticker, count, price)
            self._logger.log_execution({
                "action": "addon_unfilled",
                "ticker": ticker,
                "asset": meta["asset"],
                "price": price,
                "count": count,
            })
            return False

    def _get_addon_best_ask(self, ticker: str) -> Optional[int]:
        """Get best YES ask for ticker via scanner cache, then REST fallback."""
        try:
            scanner = self._ml.scanner if self._ml else None
            if scanner:
                ob_data, _ = scanner._get_orderbook_cached(ticker)
                if ob_data:
                    return best_yes_ask_cents(ob_data)
        except Exception:
            logging.debug("addon orderbook cache lookup failed", exc_info=True)

        # REST fallback
        try:
            ob_resp = self._client.get_orderbook(ticker, depth=5)
            if ob_resp:
                orderbook_fp = ob_resp.get("orderbook_fp")
                if orderbook_fp and self._ml and hasattr(self._ml, 'scanner'):
                    ob_data = convert_orderbook_fp(orderbook_fp)
                else:
                    ob_data = ob_resp.get("orderbook", ob_resp)
                if ob_data:
                    return best_yes_ask_cents(ob_data)
        except Exception:
            logging.debug("addon orderbook REST fallback failed", exc_info=True)
        return None

    def _nbbo_fallback_price(self, candidate: Dict) -> Optional[int]:
        """Return NBBO yes_ask price if candidate passes per-asset gates, else None.

        Called when _get_addon_best_ask() returns None (empty orderbook).
        Uses the NBBO price from scan() (candidate["best_yes_ask"]) which was
        sourced from the market listing's yes_ask field.
        """
        asset = candidate.get("asset", "")
        gate = NBBO_FALLBACK_GATES.get(asset)
        if gate is None:
            return None

        min_price, max_price, max_stc = gate
        nbbo_price = candidate.get("best_yes_ask")
        stc = candidate.get("seconds_to_close")

        if nbbo_price is None:
            return None

        # Price gate
        if nbbo_price < min_price or nbbo_price > max_price:
            logging.info(
                "nbbo_fallback_BLOCKED_price: %s asset=%s price=%dc gate=[%d-%d]",
                candidate.get("ticker", "?"), asset, nbbo_price, min_price, max_price)
            self._session_nbbo_fallback_blocked += 1
            return None

        # STC gate (None = no restriction)
        if max_stc is not None and stc is not None and stc >= max_stc:
            logging.info(
                "nbbo_fallback_BLOCKED_stc: %s asset=%s stc=%.0fs gate=<%.0fs",
                candidate.get("ticker", "?"), asset, stc, max_stc)
            self._session_nbbo_fallback_blocked += 1
            return None

        logging.info(
            "nbbo_fallback_USING: %s asset=%s price=%dc stc=%.0fs",
            candidate.get("ticker", "?"), asset, nbbo_price, stc or 0)
        self._session_nbbo_fallback_attempts += 1
        return nbbo_price

    def _get_addon_spot(self, asset: str) -> Optional[float]:
        """Get current spot price for asset via feed."""
        try:
            if self._ml and hasattr(self._ml, 'scanner'):
                return self._ml.scanner._feed.get_price(asset)
        except Exception:
            logging.debug("addon spot price lookup failed", exc_info=True)
        return None

    def _addon_decision_spot(self, asset: str, coinbase_spot: float) -> float:
        """RTI-6: gate the add-on / scale-in decision spot through the scanner's
        per-asset RTI gate, so a position SCALED is priced on the same effective
        spot as the original ENTRY. Without this, a promoted asset would enter
        on RTI spot (scanner) but scale on Coinbase spot (here) — the C1
        screen-vs-trade divergence, one layer down at the position lifecycle.
        Default-empty SYNTHETIC_RTI_LIVE_ASSETS => returns coinbase_spot
        unchanged. Defensive: any lookup failure falls back to Coinbase."""
        try:
            scanner = self._ml.scanner if self._ml else None
            if scanner is not None:
                return scanner._effective_decision_spot(
                    asset, coinbase_spot, self._state._scan_rti_cache)
        except Exception:
            logging.debug("addon RTI decision-spot gate failed", exc_info=True)
        return coinbase_spot

    def _get_addon_balance(self) -> Optional[int]:
        """Get current balance in cents."""
        try:
            if self._ml and hasattr(self._ml, 'scanner'):
                return self._ml.scanner._get_balance_cached()
        except Exception:
            logging.debug("addon balance lookup failed", exc_info=True)
        # Direct API fallback
        try:
            resp = self._client.get_balance()
            if resp:
                return resp.get("balance") or 0
        except Exception:
            logging.debug("addon balance API fallback failed", exc_info=True)
        return None

    # ── Dip Addon ──────────────────────────────────────────────────────────

    def _check_dip_addon_opportunities(self):
        """Check filled positions for dip buy opportunities.

        Two-tier logging:
          1. Shadow tier (>=50c): Every qualifying dip -> evaluated_opportunities
          2. Live tier (>=87c): Execution path (shadow or real)
        """
        if OBSERVATION_MODE:
            return  # Never execute dip addons in observation mode
        if not DIP_ADDON_ENABLED or not self._addon_eligible:
            return

        now = time.time()

        # Cleanup expired tickers from completed set
        for t in list(self._dip_addon_completed):
            if t not in self._addon_eligible:
                self._dip_addon_completed.discard(t)

        for ticker, meta in list(self._addon_eligible.items()):
            # Already dip-addon'd or expired
            if ticker in self._dip_addon_completed:
                continue
            if now - meta["fill_time"] > 300:
                continue

            # Don't dip-addon on addon fills
            if meta.get("candidate", {}).get("entry_path") in (
                    "confirmation_addon", "dip_addon", "tm_taker", "bracket_no_taker"):
                continue

            # Time checks
            elapsed = now - meta["fill_time"]
            if elapsed < DIP_ADDON_MIN_SECONDS_SINCE_FILL:
                continue

            stc_at_fill = meta.get("seconds_to_close_at_fill")
            if stc_at_fill is None:
                continue
            current_stc = stc_at_fill - elapsed
            if current_stc < DIP_ADDON_MIN_STC_REMAINING:
                continue

            # Get current ask
            current_ask = self._get_addon_best_ask(ticker)
            if current_ask is None:
                continue

            # DIP CHECK: ask must drop >= threshold below entry
            drop = meta["entry_price_cents"] - current_ask
            if drop < DIP_ADDON_MIN_DROP_CENTS:
                continue

            # ── Shared computation (needed by both tiers) ──────────
            asset = meta["asset"]
            spot = self._get_addon_spot(asset)
            if spot is None:
                continue

            blended_rv = meta.get("blended_rv")
            try:
                if self._ml and hasattr(self._ml, 'vol'):
                    fresh_vol = self._ml.vol._cache.get(asset)
                    if fresh_vol and fresh_vol.get("blended_rv"):
                        blended_rv = fresh_vol["blended_rv"]
            except Exception:
                pass

            threshold = meta.get("threshold")
            if blended_rv is None or threshold is None:
                continue

            # Recompute probability at current spot/vol/stc
            prob_result = ProbabilityEngine.compute(
                self._addon_decision_spot(asset, spot), threshold, current_stc,
                blended_rv, asset=asset,
                product_type=meta.get("candidate", {}).get("product_type"))
            cal_prob = prob_result.get("calibrated_prob")
            if cal_prob is None:
                continue

            # Sizing (computed once, used by both tiers)
            addon_count = max(1, int(
                meta["entry_count"] * DIP_ADDON_SIZE_FRACTION))
            taker_fee = calculate_taker_fee(addon_count, current_ask)
            net_edge = (cal_prob - (current_ask / 100.0)
                        - (taker_fee / (addon_count * 100.0)))

            # ── TIER 1: Shadow observation (50c floor) ─────────────
            if current_ask >= DIP_ADDON_SHADOW_FLOOR:
                self._session_dip_addon_shadow += 1
                # OFT signals for dip addon
                _dip_oft_db = {}
                if self._kalshi_oft is not None:
                    try:
                        _dip_koft = self._kalshi_oft.get_signals(ticker)
                        if _dip_koft:
                            _dip_oft_db = {
                                "oft_prob_adjustment": _dip_koft.get("prob_adjustment"),
                                "oft_imbalance_ratio": _dip_koft.get("imbalance_ratio"),
                                "oft_n_snapshots": _dip_koft.get("n_snapshots"),
                            }
                    except Exception:
                        pass
                try:
                    self._state.insert_evaluated_opportunity(
                        ticker=ticker,
                        event_ticker=meta["event_ticker"],
                        asset=asset,
                        filter_stage="dip_addon_shadow",
                        spot_price=spot,
                        threshold=threshold,
                        volatility=blended_rv,
                        market_price=current_ask,
                        seconds_to_close=current_stc,
                        calibrated_prob=cal_prob,
                        edge=net_edge,
                        strategy="DIP_ADDON_SHADOW",
                        position_size=addon_count,
                        z_score=meta.get("candidate", {}).get("z_score"),
                        vol_regime=meta.get("candidate", {}).get(
                            "vol_regime"),
                        raw_prob=prob_result.get("raw_prob"),
                        fee_adjusted_edge=net_edge,
                        counterfactual=(
                            "entry=%dc drop=%dc orig_count=%d"
                            % (meta["entry_price_cents"], drop,
                               meta["entry_count"])),
                        product_type="dip_addon_shadow",
                        hourly_pre_temp_prob=None, hourly_applied_temp_t=None,
                        hourly_shadow_temp_2_0=None, hourly_shadow_temp_1_0=None,
                        hourly_shadow_temp_2_5=None, hourly_shadow_blend_50=None,
                        hourly_shadow_temp_1_75=None, hourly_shadow_temp_3_0=None,
                        hourly_shadow_blend_20=None, hourly_shadow_blend_30=None,
                        hourly_shadow_blend_60=None, hourly_post_temp_prob=None,
                        config_snapshot_id=(
                            self._ml.config_snapshot_id if self._ml else None
                        ),
                        **_dip_oft_db,
                    )
                except Exception:
                    logging.debug("dip_addon shadow DB insert failed",
                                  exc_info=True)

                logging.info(
                    "dip_addon_SHADOW_OBS: %s ask=%dc entry=%dc drop=%dc "
                    "edge=%.4f prob=%.4f stc=%.0f count=%d",
                    ticker, current_ask, meta["entry_price_cents"], drop,
                    net_edge, cal_prob, current_stc, addon_count)

            # ── TIER 2: Live execution path (87c floor) ────────────
            # Mark completed after shadow log — one observation per ticker
            self._dip_addon_completed.add(ticker)

            if current_ask < DIP_ADDON_MIN_ENTRY_PRICE:
                logging.info("dip_addon_SKIP_floor: %s ask=%dc < %dc",
                             ticker, current_ask, DIP_ADDON_MIN_ENTRY_PRICE)
                continue

            # Edge check
            if net_edge < MIN_EDGE_PCT / 100.0:
                self._session_dip_addon_skipped += 1
                logging.info(
                    "dip_addon_SKIP_edge: %s edge=%.4f ask=%dc prob=%.4f",
                    ticker, net_edge, current_ask, cal_prob)
                continue

            # Balance + combined exposure check
            balance = self._get_addon_balance()
            if balance is None:
                continue

            original_cost = meta["entry_count"] * meta["entry_price_cents"]
            addon_cost = addon_count * current_ask
            total_exposure = original_cost + addon_cost
            max_allowed = int(
                (balance + original_cost) * DIP_ADDON_MAX_TOTAL_RISK)
            if total_exposure > max_allowed:
                addon_count = max(
                    0, (max_allowed - original_cost) // current_ask)
                if addon_count < 1:
                    self._session_dip_addon_skipped += 1
                    logging.info(
                        "dip_addon_SKIP_exposure: %s total=%dc > %dc",
                        ticker, total_exposure, max_allowed)
                    continue
                addon_cost = addon_count * current_ask
                taker_fee = calculate_taker_fee(addon_count, current_ask)
                net_edge = (cal_prob - (current_ask / 100.0)
                            - (taker_fee / (addon_count * 100.0)))
                if net_edge < MIN_EDGE_PCT / 100.0:
                    continue

            # ── EXECUTE (or shadow-log the live tier) ──────────────
            self._session_dip_addon_attempts += 1

            if DIP_ADDON_SHADOW_MODE:
                logging.info(
                    "dip_addon_LIVE_SHADOW: %s %dx @ %dc (entry=%dc -%dc) "
                    "stc=%.0f edge=%.4f prob=%.4f bal=%dc",
                    ticker, addon_count, current_ask,
                    meta["entry_price_cents"], drop, current_stc,
                    net_edge, cal_prob, balance)
                self._logger.log_execution({
                    "action": "dip_addon_live_shadow",
                    "ticker": ticker, "asset": asset,
                    "entry_price": meta["entry_price_cents"],
                    "dip_price": current_ask, "drop_cents": drop,
                    "addon_count": addon_count, "edge": net_edge,
                    "prob": cal_prob, "stc": current_stc,
                    "balance": balance,
                })
                return  # One per tick

            # LIVE: taker IOC, single attempt
            logging.info(
                "dip_addon_TRIGGER: %s %dx @ %dc (entry=%dc -%dc) "
                "stc=%.0f edge=%.4f prob=%.4f bal=%dc",
                ticker, addon_count, current_ask,
                meta["entry_price_cents"], drop, current_stc,
                net_edge, cal_prob, balance)

            addon_candidate = {
                "ticker": ticker,
                "event_ticker": meta["event_ticker"],
                "asset": asset,
                "best_yes_ask": current_ask,
                "position_size": addon_count,
                "calibrated_prob": cal_prob,
                "edge": net_edge,
                "seconds_to_close": current_stc,
                "balance_at_scan": balance,
                "entry_path": "dip_addon",
                "strategy": "DIP_ADDON",
                "blended_rv": blended_rv,
                "threshold": threshold,
                "vol_regime": meta.get("candidate", {}).get("vol_regime"),
                "z_score": meta.get("candidate", {}).get("z_score"),
                "kelly_f": meta.get("candidate", {}).get("kelly_f"),
                "ob_snapshot": {},
                "original_entry_price": meta["entry_price_cents"],
                "original_entry_count": meta["entry_count"],
                "price_drop": drop,
            }

            result = self._submit_taker(addon_candidate)
            if result is not None:
                self._session_dip_addon_fills += 1
                logging.info("dip_addon_FILLED: %s %dx @ %dc (-%dc)",
                             ticker, addon_count, current_ask, drop)
                if _telegram_state._TELEGRAM:
                    try:
                        _cost = addon_count * current_ask / 100
                        _telegram_state._TELEGRAM.send(
                            f"Dip addon: {asset} {addon_count}ct "
                            f"@ {current_ask}c "
                            f"(${_cost:.2f}, -{drop}c from entry)")
                    except Exception:
                        pass
            else:
                logging.warning("dip_addon_UNFILLED: %s %dx @ %dc",
                                ticker, addon_count, current_ask)
            return  # One per tick max

    # ── Cancel ────────────────────────────────────────────────────────────

    # Allowed sources for _handle_cancel_404 (round-5/6/7 reviews).
    # Bad source values are logged + downgraded to "unknown" rather
    # than raising — _tick_one's broad except would swallow an
    # AssertionError and leave the asset stuck.
    # See kb/decisions/cancel-404-fix-v2-design-may04.md
    _CANCEL_404_SOURCES = ("direct", "reconciliation")

    def _handle_cancel_404(self, order: Dict, asset: str,
                           reason: str, source: str) -> bool:
        """Handle a 404 from cancel API.

        404 *should* mean Kalshi already expired/canceled the order.
        Verify defensively via get_orders to guard against
        wrong-order-id / caller bugs (the conservative cancel_pending
        branch's original purpose).

        Args:
            order: Order dict in self._active_orders[asset].
            asset: Asset key (BTC/ETH/SOL/XRP).
            reason: Free-form trigger label (close_approaching, timeout,
                escalation_*, cancel_pending_retry). Threaded into
                cancel_reason for forensics.
            source: One of _CANCEL_404_SOURCES. Unknown values logged
                and downgraded to "unknown" — must not block the pop.

        Returns True if popped (caller may submit replacement).
        Returns False if held conservatively (order still resting on
        Kalshi — something is wrong).

        Note: pop happens BEFORE audit writes (mark_order_status,
        log_order, _log_fill_model_sample, update_evaluated_opportunity_order)
        so audit failures cannot resurrect the lockout. Of those four,
        only mark_order_status is un-self-guarded (can raise
        sqlite3.OperationalError); the others catch internally
        (bot/_impl.py:2716, 23630, 5089).
        """
        if source not in self._CANCEL_404_SOURCES:
            logging.error(
                f"_handle_cancel_404 unknown source={source!r} — "
                f"falling back to 'unknown' to keep pop priority")
            source = "unknown"
        self._cancel_404_count += 1
        filled = order.get("filled_so_far", 0)

        # Defensive verify: 404 should mean the order is gone, but
        # confirm via get_orders before popping. Three failure modes:
        #   (a) get_orders raises → exception path → log + pop
        #   (b) get_orders returns None (breaker OPEN) → log + pop
        #   (c) get_orders shows order resting → HOLD cancel_pending
        try:
            orders_resp = self._client.get_orders(ticker=order["ticker"])
            if orders_resp is None:
                logging.warning(
                    f"cancel_404_verify_unavailable: {order['ticker']} "
                    f"from {source} — get_orders returned None "
                    f"(breaker open?), falling through to pop.")
            elif orders_resp:
                for o in orders_resp.get("orders", []):
                    if (o.get("order_id") == order["order_id"]
                            and o.get("status") in ("resting", "open")):
                        logging.error(
                            f"cancel_404_but_resting: {order['ticker']} "
                            f"{order['order_id']} from {source} — "
                            f"Kalshi 404'd cancel but order still in "
                            f"/orders. Holding cancel_pending for retry.")
                        order["cancel_pending"] = True
                        return False
        except Exception:
            logging.warning(
                f"cancel_404_verify_failed: {order['ticker']} from "
                f"{source} — falling through to pop based on 404 signal",
                exc_info=True)

        # Preserve partial-fill labeling (kalshi_fill_simulator.py
        # treats partial_canceled as label=1).
        if filled > 0:
            db_status = "partial_canceled"
            outcome = "partial_filled"
            fm_label = "partial_canceled"
        else:
            db_status = "expired"
            outcome = "expired"
            fm_label = "expired"

        cancel_reason_str = f"kalshi_404_{source}_{reason}"

        # POP FIRST. Audit writes must NOT be a prerequisite to the
        # pop — if any audit write raises, the asset would stay stuck
        # and re-create the May 4 lockout.
        self._active_orders.pop(asset, None)

        # Of the four audit writes, only mark_order_status is un-self-
        # guarded (can raise sqlite3.OperationalError on lock
        # contention). log_order/_log_fill_model_sample/
        # update_evaluated_opportunity_order catch internally
        # (bot/_impl.py:2716, 23630, 5089). Single outer try is
        # belt-and-suspenders defense-in-depth — primary protection
        # is the pop above.
        try:
            self._state.mark_order_status(order["order_id"], db_status)
            self._logger.log_order({
                "action": "maker_canceled",
                "ticker": order["ticker"],
                "order_id": order["order_id"],
                "reason": cancel_reason_str,
                "elapsed": round(time.time() - order["submit_time"], 1),
                "filled_so_far": filled,
            })
            self._log_fill_model_sample(
                order, fm_label, cancel_reason=cancel_reason_str)
            if asset not in self._escalating_assets:
                self._state.update_evaluated_opportunity_order(
                    order["ticker"], order_outcome=outcome)
        except Exception:
            logging.error(
                f"cancel_404_audit_failed: {order['ticker']} "
                f"{order['order_id']} — pop already complete, audit "
                f"writes incomplete.",
                exc_info=True)

        logging.warning(
            f"cancel_404_already_gone: {order['ticker']} "
            f"{order['order_id']} filled={filled}/{order['count']} "
            f"from {source} (session count: {self._cancel_404_count})")
        return True

    def _cancel_order(self, asset: str, reason: str) -> bool:
        """Cancel the active maker order for a specific asset.

        Returns True if cancel succeeded (safe to submit replacement).
        Returns False if cancel API failed (order may still be live).
        """
        order = self._active_orders.get(asset)
        if order is None:
            return True  # nothing to cancel

        filled = order.get("filled_so_far", 0)

        cancel_resp = self._client.cancel_order(
            order["order_id"], ticker=order.get("ticker"))
        if cancel_resp is None:
            logging.error(f"Cancel API FAILED for {order['order_id']} — order may still be resting on exchange")
            # Don't mark canceled in DB — order may still be live on Kalshi
            order["cancel_pending"] = True
            # Do NOT pop — order may still be live, prevent double position
            return False

        # 404 sentinel — Kalshi has aged out the order. Route through
        # _handle_cancel_404 (defensive verify + pop-first + correct
        # labeling). See kb/failures/cancel-404-asset-lockout-may04.md
        if isinstance(cancel_resp, dict) and cancel_resp.get("_status_code") == 404:
            return self._handle_cancel_404(order, asset, reason, source="direct")

        # Normal success
        status = "partial_canceled" if filled > 0 else "canceled"
        self._state.mark_order_status(order["order_id"], status)

        # Log fill model sample for canceled order
        self._log_fill_model_sample(order, "canceled", cancel_reason=reason)

        self._logger.log_order({
            "action": "maker_canceled",
            "ticker": order["ticker"],
            "order_id": order["order_id"],
            "reason": reason,
            "elapsed": round(time.time() - order["submit_time"], 1),
            "filled_so_far": filled,
        })
        logging.info(
            f"Maker order canceled: {order['ticker']} reason={reason}"
            f"{' (partial fill: ' + str(filled) + '/' + str(order['count']) + ')' if filled > 0 else ''}"
        )
        self._active_orders.pop(asset, None)
        # Update order outcome — skip if escalating (escalation handler sets outcome)
        if asset not in self._escalating_assets:
            _outcome = "partial_filled" if filled > 0 else "canceled"
            self._state.update_evaluated_opportunity_order(
                order["ticker"], order_outcome=_outcome)
        return True

    def _cancel_active(self, reason: str):
        """Cancel all active maker orders. Used by _reprice_maker compat."""
        for asset in list(self._active_orders):
            self._cancel_order(asset, reason)
