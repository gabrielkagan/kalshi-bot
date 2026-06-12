"""Longshot premium-harvest maker strategy engine (Bit L-1, 2026-06-11).

Validated via scripts/research/genhunt/02b_longshot_fillable_validation.py
(fillable-only +4.58c/ct, day-bootstrap CI [+2.82, +6.26], 12/12 days, all
6 assets positive). Plan: kb/decisions/longshot-twap-live-small-plan.md.

Mechanics: for each open 15M crypto market at STC 180..720s, compute
``p_normal`` = Phi(signed distance-to-strike in sigma) from live spot +
blended_rv (same z denominator as bot/engines/probability.py:
``sigma_move = spot * blended_rv * sqrt(stc / 5)``). For each side whose
executable ask is in 4-15c: if ``p_normal_side <= ask * LONGSHOT_EDGE_RATIO``
(prob units), rest a maker SELL of that side — i.e. post the OPPOSITE side's
bid at ``100 - ask`` — and hold to settlement. Cancel/refresh when the
condition stops holding or at T-3min.

Division of labor (no new architecture — mirrors the DC/TM overlay pattern):

* ``OpportunityScanner.scan()`` calls :meth:`LongshotEngine.evaluate_market`
  per 15M market; returned candidates flow through the NORMAL candidate
  list as an overlay (bypass single-asset filter, like ``bracket_no``).
* ``OrderExecutor.execute()`` is the SINGLE order chokepoint — the
  trading-mode gate (bot/trading_mode.py) lives there and is NOT
  duplicated here. This module consults ``trading_mode.is_live`` READ-ONLY
  to label evaluated_opportunities rows ``longshot_live`` vs
  ``longshot_shadow`` (cell-block string-literal discipline).
  Order placement happens in ``OrderExecutor._execute_longshot_maker``
  which calls :meth:`authorize` (cap re-check) then registers the resting
  quote here via :meth:`register_resting`.
* ``MainLoop._tick()`` calls :meth:`tick` every tick — T-3min cancel sweep,
  kill-switch/auto-disable cancel sweep, and maker fill polling
  (fills recorded via ``StateManager.record_position_from_fill``;
  hold-to-settlement, the normal SettlementTracker settles the position).

Risk rails (all in bot/constants.py, read live via module-attribute access
so a constants flip is a runtime kill-switch):

* ``LONGSHOT_ENABLED`` master flag (default OFF).
* ``LONGSHOT_MAX_CONTRACTS_PER_WINDOW_SIDE`` per (ticker, side), counting
  open longshot positions AND resting longshot quotes.
* ``LONGSHOT_MAX_CONCURRENT_COLLATERAL_DOLLARS`` across all resting quotes
  + open longshot positions.
* ``LIVE_SMALL_DAILY_LOSS_CAP_DOLLARS`` — COMBINED realized+marked PnL
  today across ``strategy IN ('longshot','twaplock')`` at or below -cap ->
  same-day auto-disable (log signature LONGSHOT_DAILY_CAP_HIT). Single
  source of truth: bot/strategy_caps.py (Bit T-1 retargeted the Bit L-1
  per-strategy rail here, closing the R2-MN4 note — both engines latch
  off the same combined numbers).
* ``LIVE_SMALL_CONSECUTIVE_LOSING_DAYS_DISABLE`` consecutive completed
  COMBINED losing days -> persistent disable; operator clears via
  ``LIVE_SMALL_STREAK_RESET_UTC_DATE``.

Regression lock: tests/integration/test_longshot_strategy.py.
"""
from __future__ import annotations

import datetime
import logging
import math
import threading
import time
from datetime import timezone
from typing import Callable, Dict, List, Optional, Tuple

import bot.constants as C
from bot import strategy_caps, trading_mode
from bot.helpers.strings import dollars_str_to_cents, fp_str_to_int

LONGSHOT_STRATEGY = "longshot"
LONGSHOT_FILTER_STAGE_LIVE = "longshot_live"
LONGSHOT_FILTER_STAGE_SHADOW = "longshot_shadow"

# R1-M2: a quote this far past its window close (cancel kept failing —
# Kalshi auto-cancels at close anyway) is dropped after one final fill
# poll instead of being re-cancelled forever.
_STALE_DROP_GRACE_SECONDS = 120.0

# R1-M6: cursor-pagination bound on the per-tick fills snapshot. 5 pages
# x 200 fills is far beyond anything live-small sizing can produce.
_MAX_FILL_PAGES = 5

# R1-MN1/M3: per-ticker bookkeeping (_eval_row_seen, _mark_inputs) is
# pruned on tick once older than this — windows are 15 minutes, so 30
# minutes comfortably outlives any live entry.
_SEEN_TTL_SECONDS = 1800.0

_SQRT2 = math.sqrt(2.0)

# R2-M1: 15M window length — fallback lower bound for the fills min_ts of
# a boot-reconciled order when created_time is unavailable (window open =
# close - 900s; an order cannot predate its window).
_WINDOW_SECONDS = 900.0

_MONTH_MAP = {"JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
              "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12}


def _parse_event_ts(value) -> Optional[float]:
    """Best-effort epoch-seconds parse of an order timestamp.

    Kalshi REST returns RFC3339 strings (``2026-06-11T12:00:00Z`` /
    fractional / ``+00:00`` offset); the local pending_orders ledger
    stores the same shape. Numeric epoch passes through. None/garbage ->
    None (callers fall back to window-open / now-window bounds)."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value) if value > 0 else None
    try:
        s = str(value).strip()
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        dt = datetime.datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except (ValueError, TypeError):
        return None


def _close_epoch_from_ticker(ticker: str) -> Optional[float]:
    """Window close epoch from a 15M ticker (KXBTC15M-26JUN111200-T104).

    Same close-time-in-ET (+4h to UTC during EDT) convention as
    ``StateManager.cleanup_expired_resting_orders`` (bot/state.py)."""
    import re
    m = re.match(r"KX\w+15M-(\d{2})([A-Z]{3})(\d{2})(\d{2})(\d{2})-",
                 ticker or "")
    if not m:
        return None
    yy, mon, dd, hh, mm = m.groups()
    mon_num = _MONTH_MAP.get(mon)
    if not mon_num:
        return None
    try:
        close_et = datetime.datetime(2000 + int(yy), mon_num, int(dd),
                                     int(hh), int(mm))
        close_utc = close_et.replace(tzinfo=timezone.utc) \
            + datetime.timedelta(hours=4)
        return close_utc.timestamp()
    except (ValueError, OverflowError):
        return None


def _boot_fill_min_ts(created_time, ticker: str, now: float) -> float:
    """R2-M1: fills lower bound for a boot-reconciled order — the order's
    created_time when parseable, else window open (close - 900s), else
    now - 900s (an order cannot predate its 15M window)."""
    created_ts = _parse_event_ts(created_time)
    if created_ts is not None:
        return created_ts
    close_epoch = _close_epoch_from_ticker(ticker)
    if close_epoch is not None:
        return close_epoch - _WINDOW_SECONDS
    return now - _WINDOW_SECONDS


def compute_p_normal(spot: Optional[float], threshold: Optional[float],
                     seconds_to_close: Optional[float],
                     blended_rv: Optional[float],
                     ) -> Tuple[Optional[float], Optional[float]]:
    """P(YES settles) under a normal diffusion + the signed z it came from.

    Returns ``(p_yes, z)`` where ``z = (spot - threshold) / sigma_move`` is
    the SIGNED distance-to-strike in sigma (positive = spot above strike)
    and ``p_yes = Phi(z)``. Same sigma denominator as
    ``bot/engines/probability.py::compute``:
    ``sigma_move = spot * blended_rv * sqrt(seconds_to_close / 5)``
    (blended_rv is per-5s vol). Returns ``(None, None)`` on any
    non-positive/missing input — callers must treat that as "no signal",
    never as 0 or 0.5.
    """
    if spot is None or spot <= 0 or threshold is None or threshold <= 0:
        return (None, None)
    if seconds_to_close is None or seconds_to_close <= 0:
        return (None, None)
    if blended_rv is None or blended_rv <= 0:
        return (None, None)
    sigma_move = spot * blended_rv * math.sqrt(seconds_to_close / 5.0)
    if sigma_move <= 0:
        return (None, None)
    z = (spot - threshold) / sigma_move
    p_yes = 0.5 * (1.0 + math.erf(z / _SQRT2))
    return (p_yes, z)


def _best_bid_cents(levels) -> Optional[int]:
    """Best (highest) bid price from a Kalshi orderbook side list.

    Kalshi book shape: ``{"yes": [[price, qty], ...], "no": [[...]]}``
    where each list holds that side's BIDS.
    """
    if not levels:
        return None
    best = None
    for lvl in levels:
        try:
            px = int(lvl[0])
        except (TypeError, ValueError, IndexError):
            continue
        if best is None or px > best:
            best = px
    return best


def _executable_asks(ob: Dict) -> Tuple[Optional[int], Optional[int]]:
    """(yes_ask_cents, no_ask_cents) from a Kalshi orderbook dict.

    YES ask = 100 - best NO bid; NO ask = 100 - best YES bid (crossing the
    other side's resting bids is the only executable price on Kalshi).
    Tolerates the ``{"orderbook": {...}}`` REST wrapper.
    """
    inner = ob.get("orderbook", ob) if isinstance(ob, dict) else None
    if not isinstance(inner, dict):
        return (None, None)
    best_yes_bid = _best_bid_cents(inner.get("yes"))
    best_no_bid = _best_bid_cents(inner.get("no"))
    yes_ask = (100 - best_no_bid) if best_no_bid is not None else None
    no_ask = (100 - best_yes_bid) if best_yes_bid is not None else None
    return (yes_ask, no_ask)


class LongshotEngine:
    """Condition logic, risk rails, and resting-quote lifecycle for longshot.

    Holds NO order-placement authority: placement goes through
    ``OrderExecutor.execute()`` (the trading-mode chokepoint). The engine's
    client is used only for fill polling (``get_fills``) and cancels
    (``cancel_order`` — intentionally ungated at the kalshi_client layer
    because cancels only reduce exposure).
    """

    def __init__(self, client, state, logger=None):
        self._client = client
        self._state = state
        self._logger = logger
        self._lock = threading.RLock()
        # order_id -> resting-quote record
        self._resting: Dict[str, Dict] = {}
        self._disabled_reason: Optional[str] = None
        self._disabled_utc_date: Optional[str] = None
        # R1-M1: one-shot boot orphan reconciliation latch (first tick).
        self._boot_reconciled = False
        # R1-M3: ticker -> (spot, threshold, ts) — latest engine inputs,
        # captured per evaluate_market call, used to MARK open positions
        # (sold side currently ITM = full loss) against the daily cap.
        self._mark_inputs: Dict[str, Tuple[float, float, float]] = {}
        # R1-MN1: (ticker, side) -> ts of the one eval-row write (mirrors
        # the scanner's _eval_opp_seen dedup; candidates still emit every
        # tick — only the DB write is deduped). Pruned via _SEEN_TTL.
        self._eval_row_seen: Dict[Tuple[str, str], float] = {}
        # R1-M1 fix round (Bit V.1): asset -> last LONGSHOT_SPOT_STALE log
        # ts. 60s/asset throttle (the scanner's TAPE_RV_NONE idiom) — the
        # gate fires per TICKER per tick on gapped feeds (BNB: 34% of
        # 1-min intervals stale), unthrottled would spam the journal.
        self._spot_stale_log_ts: Dict[str, float] = {}
        # R2-M1 fix round (Bit V.1): ticker -> wall-clock ts of the FIRST
        # stale/unmeasured-spot eval of the CURRENT stale episode. A fresh
        # eval pops the slot (episode over); an episode persisting past
        # LONGSHOT_STALE_CANCEL_GRACE_SECONDS cancels the ticker's resting
        # quotes (see the gate in evaluate_market). Pruned via _SEEN_TTL
        # in tick() like the sibling per-ticker dicts.
        self._stale_first_seen: Dict[str, float] = {}
        # Bit T-1: the COMBINED live-small cap sums marked open losses
        # across engines — register this engine's R1-M3 mark so the
        # sibling (twaplock) latch sees it too. Same-key re-registration
        # replaces a stale closure on restart/fresh-instance.
        strategy_caps.register_mark_provider(
            LONGSHOT_STRATEGY, self._marked_open_loss_cents)

    # ── public surface ────────────────────────────────────────────────────

    def disabled_reason(self) -> Optional[str]:
        """'daily_cap' | 'consec_days' | None (as of the last refresh)."""
        return self._disabled_reason

    def resting_count(self) -> int:
        """Number of resting longshot quotes (quotes, not contracts)."""
        with self._lock:
            return len(self._resting)

    def register_resting(self, *, order_id: str, client_order_id: str,
                         ticker: str, event_ticker: str, asset: str,
                         sell_side: str, buy_side: str, buy_price_cents: int,
                         count: int, seconds_to_close: float,
                         fill_min_ts: Optional[float] = None,
                         boot_fill_skip: int = 0) -> None:
        """Track a successfully-posted maker quote for lifecycle management.

        ``fill_min_ts`` (R2-M1) is the epoch lower bound for fill polling;
        defaults to registration time (correct for fresh placements). Boot
        orphan reconciliation passes the order's created_time instead so
        fills landed BEFORE the restart stay inside the fetch bound.

        ``boot_fill_skip`` (R3-M2 + R4-M2) is the number of
        already-recorded contracts the fill-apply path must SKIP (oldest
        fills first) before recording positions — boot reconciliation
        passes the order's OWN ``recorded_fill_count`` (per-order seed;
        (ticker, side) aggregate only for legacy NULL rows) so
        re-fetched pre-restart fills (record_position_from_fill
        ACCUMULATES and seen_trade_ids is reborn empty at restart) are
        not double-counted. Fresh placements leave it 0.
        """
        now = time.time()
        with self._lock:
            self._resting[order_id] = {
                "order_id": order_id,
                "client_order_id": client_order_id,
                "ticker": ticker,
                "event_ticker": event_ticker,
                "asset": asset,
                "sell_side": sell_side,
                "buy_side": buy_side,
                "buy_price_cents": int(buy_price_cents),
                "count": int(count),
                "filled": 0,
                "seen_trade_ids": set(),
                "registered_ts": now,
                "fill_min_ts": float(fill_min_ts) if fill_min_ts is not None
                else now,
                "stc_at_register": float(seconds_to_close),
                "boot_skip_remaining": max(0, int(boot_fill_skip)),
            }

    def evaluate_market(self, *, ticker: str, event_ticker: str, asset: str,
                        product_type: Optional[str], spot: Optional[float],
                        threshold: Optional[float],
                        seconds_to_close: Optional[float],
                        blended_rv: Optional[float],
                        orderbook_fetch: Callable[[], Optional[Dict]],
                        config_snapshot_id: Optional[int],
                        balance_at_scan: Optional[float]) -> List[Dict]:
        """Evaluate one market; return 0..2 longshot candidates (one/side).

        Side effects: writes evaluated_opportunities rows
        (filter_stage longshot_live/longshot_shadow — labeling consults
        bot.trading_mode READ-ONLY; the gate stays at executor.execute()),
        and cancels resting quotes on this ticker whose condition no longer
        holds. Returns [] without touching the DB when LONGSHOT_ENABLED is
        off or an auto-disable rail is latched.
        """
        if not C.LONGSHOT_ENABLED:
            return []
        # R1-M3: capture the latest mark inputs BEFORE the disable refresh
        # so the marked-loss term sees this tick's spot/threshold.
        if (spot is not None and spot > 0
                and threshold is not None and threshold > 0):
            with self._lock:
                self._mark_inputs[ticker] = (float(spot), float(threshold),
                                             time.time())
        self._refresh_disabled()
        if self._disabled_reason:
            return []

        # Frozen/unmeasured-spot gate (R1-M1 fix round, Bit V.1 — mirror
        # of twaplock's TWAPLOCK_SPOT_STALE R2-MN1 pattern; longshot
        # shipped with NO staleness gate). A frozen Coinbase WS price
        # keeps flowing through get_price_with_ts/get_buffer with fresh
        # sampler re-stamps, so p_normal would be priced off a stale spot
        # and the seam's tape rv300 is simultaneously gated off (same
        # event-time signal) — leaving only the deflation-prone
        # blended_rv. The validated backtest (02_longshot_tick_floor
        # STALE_S=30.0; tracked in-repo equivalent 02b ::_at/_rv_pure)
        # ABSTAINED at every such decision point; abstain live too: no
        # candidates, no eval rows. Resting quotes are NOT left up
        # through the episode (R2-M1 fix round): the backtest's fill
        # model (02b zscore -> None on a >30s-stale spot at print time)
        # EXCLUDED stale-episode fills from the +4.58c economics, and the
        # only measured tolerance for a quote lingering past signal death
        # is 02b's 10s cancel-latency arm — so once the episode persists
        # past LONGSHOT_STALE_CANCEL_GRACE_SECONDS this ticker's resting
        # quotes are cancelled (reason spot_stale; the engine's ungated
        # cancel machinery — cancels only reduce exposure). The grace
        # absorbs flickery staleness (no churn); a fresh eval pops the
        # episode clock below. NOTE on _mark_inputs: marks are captured
        # ABOVE this gate, so during the episode they stay frozen at the
        # last evaluated spot — see _marked_open_loss_cents for why that
        # is tolerated. The scanner stamps the per-asset event-time
        # staleness into state._scan_spot_staleness_cache each tick
        # (Bit S.1) and pops the slot on warmup-NULL; missing or stale
        # -> NO SIGNAL.
        _staleness = self._state._scan_spot_staleness_cache.get(asset)
        if (_staleness is None
                or _staleness > C.LONGSHOT_MAX_SPOT_STALENESS_SECONDS):
            _now_stale = time.time()
            if (_now_stale - self._spot_stale_log_ts.get(asset, 0.0)
                    >= 60.0):
                self._spot_stale_log_ts[asset] = _now_stale
                # info, not warning — fires routinely on thin assets
                # (S.2 RCA gap rates in the constant's comment,
                # bot/constants.py); 60s/asset throttle.
                logging.info(
                    "LONGSHOT_SPOT_STALE: %s %s spot_staleness=%s vs max "
                    "%.1fs (None = unmeasured this tick) — no signal",
                    ticker, asset, _staleness,
                    C.LONGSHOT_MAX_SPOT_STALENESS_SECONDS)
            with self._lock:
                _first_stale = self._stale_first_seen.setdefault(
                    ticker, _now_stale)
            if (_now_stale - _first_stale
                    >= C.LONGSHOT_STALE_CANCEL_GRACE_SECONDS):
                for q in self._resting_for_ticker(ticker):
                    self._cancel_quote(q["order_id"], "spot_stale")
            return []
        with self._lock:
            self._stale_first_seen.pop(ticker, None)

        stc_ok = (seconds_to_close is not None
                  and C.LONGSHOT_MIN_STC_SECONDS <= seconds_to_close
                  <= C.LONGSHOT_MAX_STC_SECONDS)
        p_yes, z = compute_p_normal(spot, threshold, seconds_to_close,
                                    blended_rv)

        resting_here = self._resting_for_ticker(ticker)
        if p_yes is None:
            # No signal — leave resting quotes alone (tick() still enforces
            # the T-3min sweep); emit nothing.
            return []
        p_by_side = {"yes": p_yes, "no": 1.0 - p_yes}

        # Cheap pre-filter: when neither side can possibly qualify and we
        # have no resting quote to verify, skip the orderbook fetch
        # entirely (bounds REST load to genuinely deep-OTM strikes).
        max_qualifying_p = (C.LONGSHOT_MAX_ASK_CENTS / 100.0) * C.LONGSHOT_EDGE_RATIO
        if (not resting_here
                and (not stc_ok
                     or min(p_by_side.values()) > max_qualifying_p)):
            return []

        ob = orderbook_fetch()
        if ob is None:
            return []
        yes_ask, no_ask = _executable_asks(ob)
        ask_by_side = {"yes": yes_ask, "no": no_ask}

        def _condition(sell_side: str) -> bool:
            ask = ask_by_side.get(sell_side)
            p = p_by_side.get(sell_side)
            return (stc_ok
                    and ask is not None and p is not None
                    and C.LONGSHOT_MIN_ASK_CENTS <= ask <= C.LONGSHOT_MAX_ASK_CENTS
                    and p <= (ask / 100.0) * C.LONGSHOT_EDGE_RATIO)

        # Refresh pass: cancel resting quotes whose condition flipped.
        for q in resting_here:
            if not _condition(q["sell_side"]):
                self._cancel_quote(q["order_id"], "condition_flipped")

        candidates: List[Dict] = []
        for sell_side in ("yes", "no"):
            if not _condition(sell_side):
                continue
            ask = ask_by_side[sell_side]
            buy_side = "no" if sell_side == "yes" else "yes"
            buy_price = 100 - ask
            size = self._allowed_size(ticker, sell_side, buy_side, buy_price)
            if size <= 0:
                continue
            p_buy = 1.0 - p_by_side[sell_side]
            edge = p_buy - (buy_price / 100.0)
            # R1-M4: strategy-aware (LONGSHOT_LIVE_OVERRIDE) so eval rows
            # label longshot_live under a longshot-only go-live. Still
            # READ-ONLY labeling — the gate stays at executor.execute().
            live = trading_mode.strategy_is_live(LONGSHOT_STRATEGY, asset)
            filter_stage = (LONGSHOT_FILTER_STAGE_LIVE if live
                            else LONGSHOT_FILTER_STAGE_SHADOW)
            # R1-MN1: one eval row per (ticker, side) — not one per tick
            # (mirrors scanner _eval_opp_seen). Candidates still emit.
            _seen_key = (ticker, buy_side)
            with self._lock:
                _row_seen = _seen_key in self._eval_row_seen
                if not _row_seen:
                    self._eval_row_seen[_seen_key] = time.time()
            if not _row_seen:
                try:
                    self._state.insert_evaluated_opportunity(
                        ticker, event_ticker, asset, filter_stage,
                        spot_price=spot, threshold=threshold,
                        volatility=blended_rv,
                        market_price=ask,  # the SOLD side's executable ask
                        seconds_to_close=seconds_to_close,
                        calibrated_prob=round(p_buy, 6),
                        raw_prob=round(p_buy, 6),
                        edge=round(edge, 6),
                        strategy=LONGSHOT_STRATEGY,
                        position_size=size,
                        z_score=round(z, 4) if z is not None else None,
                        side=buy_side,
                        product_type=product_type or "15m",
                        config_snapshot_id=config_snapshot_id,
                    )
                except Exception:
                    logging.warning(
                        "insert_evaluated_opportunity failed (%s)",
                        filter_stage, exc_info=True)
            candidates.append({
                "ticker": ticker,
                "event_ticker": event_ticker,
                "asset": asset,
                "product_type": product_type or "15m",
                "spot": spot,
                "threshold": threshold,
                "seconds_to_close": round(float(seconds_to_close), 1),
                "blended_rv": blended_rv,
                "strategy": LONGSHOT_STRATEGY,
                "side": buy_side,
                "longshot_sell_side": sell_side,
                "longshot_ask_cents": ask,
                "longshot_buy_side": buy_side,
                "longshot_buy_price_cents": buy_price,
                # Per-contract cost convention (bracket_no pattern): the
                # executor's exposure caps read best_yes_ask as cents-at-risk
                # per contract, which for a maker buy is the posted price.
                "best_yes_ask": buy_price,
                "position_size": size,
                "calibrated_prob": round(p_buy, 6),
                "edge": round(edge, 6),
                "z_score": round(z, 4) if z is not None else None,
                "balance_at_scan": balance_at_scan,
            })
        return candidates

    def authorize(self, candidate: Dict) -> int:
        """Execute-time cap re-check (scan->execute race defense).

        Returns the contract count the executor may post (0 = blocked).
        Re-reads positions + resting state so a sister fill between scan
        and execute shrinks/blocks this order.
        """
        if not C.LONGSHOT_ENABLED:
            return 0
        self._refresh_disabled()
        if self._disabled_reason:
            return 0
        size = self._allowed_size(
            candidate["ticker"], candidate["longshot_sell_side"],
            candidate["longshot_buy_side"],
            candidate["longshot_buy_price_cents"])
        return max(0, min(int(candidate.get("position_size") or 0), size))

    def tick(self, now: Optional[float] = None) -> None:
        """Per-main-loop-tick lifecycle sweep.

        1. Kill-switch / auto-disable: cancel ALL resting quotes.
        2. Fill polling: record maker fills as positions
           (hold-to-settlement; dedup by trade_id). Runs BEFORE the
           cancel sweep (R1-C1) so fills landed since the last tick are
           recorded before their quote can be canceled+popped;
           _cancel_quote additionally runs a final poll of its own.
        3. T-3min sweep: cancel quotes whose window ran down past
           LONGSHOT_MIN_STC_SECONDS.
        """
        if now is None:
            now = time.time()
        # R1-M1: boot orphan reconciliation runs FIRST, even when disabled —
        # orphans from a pre-restart enabled run must still be cancelled
        # (cancel only reduces exposure).
        if not self._boot_reconciled:
            self._boot_reconcile_orphans()

        # R1-MN1/M3 + R2-MN2: prune per-ticker bookkeeping past TTL (15M
        # windows are long gone after 30 min; keeps both dicts bounded).
        # MUST run ABOVE the disabled early-returns: evaluate_market stamps
        # _mark_inputs BEFORE its own disable check, so a latched engine
        # still accretes entries every scan tick — pruning only on the
        # enabled path made the dicts unbounded exactly when disabled.
        with self._lock:
            cutoff = now - _SEEN_TTL_SECONDS
            self._eval_row_seen = {k: ts for k, ts
                                   in self._eval_row_seen.items()
                                   if ts >= cutoff}
            self._mark_inputs = {k: v for k, v
                                 in self._mark_inputs.items()
                                 if v[2] >= cutoff}
            # R2-M1: same TTL for the stale-episode clocks (a ticker that
            # stops being evaluated — window closed mid-episode — would
            # otherwise pin its entry forever). A >TTL-long episode
            # re-latches via setdefault and re-cancels idempotently
            # (registry empty after the first SUCCESSFUL cancel; a still-failing cancel just waits one extra grace after re-latch).
            self._stale_first_seen = {k: ts for k, ts
                                      in self._stale_first_seen.items()
                                      if ts >= cutoff}

        if not C.LONGSHOT_ENABLED:
            self._cancel_all("longshot_disabled")
            return
        self._refresh_disabled()
        if self._disabled_reason:
            self._cancel_all(self._disabled_reason)
            return

        # R1-M6: ONE unfiltered paginated fills fetch per tick, dispatched
        # across all resting quotes (was one REST call per quote).
        with self._lock:
            quotes = list(self._resting.values())
        if quotes:
            fills, complete = self._fetch_fills_snapshot()
            if fills is not None:
                for q in quotes:
                    self._apply_fills(q, fills)
                if complete:
                    # R3-MN2: a COMPLETE bulk poll on this (subsequent)
                    # tick satisfies the clean-poll requirement that a
                    # CANCEL_FILL_MISMATCH placed on the quote — the
                    # 404-path terminal pop in _cancel_quote may proceed
                    # again. Partial polls leave the hold in place.
                    for q in quotes:
                        q.pop("needs_clean_poll", None)

        # R1-MN4: a live->shadow trading-mode flip mid-flight must cancel
        # already-resting quotes. The executor gate only protects NEW
        # placements and cancel_order is intentionally ungated, so the
        # engine cancels its own quotes here (after the bulk poll, like
        # every other cancel sweep — C1 ordering).
        with self._lock:
            quotes = list(self._resting.values())
        for q in quotes:
            if not trading_mode.strategy_is_live(LONGSHOT_STRATEGY,
                                                 q["asset"]):
                self._cancel_quote(q["order_id"], "mode_flip")

        with self._lock:
            quotes = list(self._resting.values())
        for q in quotes:
            elapsed = max(0.0, now - q["registered_ts"])
            remaining = q["stc_at_register"] - elapsed
            if remaining < C.LONGSHOT_MIN_STC_SECONDS:
                self._cancel_quote(q["order_id"], "t_minus_3min")

        # R1-M2: stale-drop backstop. If the cancel keeps failing (API
        # None / network) past 120s AFTER window close, the order no
        # longer exists on Kalshi (auto-cancelled at close) — one final
        # fill poll, then drop the entry so it can't pollute caps and
        # REST budget forever.
        with self._lock:
            quotes = list(self._resting.values())
        for q in quotes:
            elapsed = max(0.0, now - q["registered_ts"])
            remaining = q["stc_at_register"] - elapsed
            if remaining < -_STALE_DROP_GRACE_SECONDS:
                # R5-MN1: the drop is TERMINAL (the entry's dedup state
                # dies with it), so the final poll must be COMPLETE —
                # a failed/partial snapshot could hide a last-moment
                # fill forever. On a failed/partial poll, leave the
                # entry: the stale condition re-fires next tick (one
                # retry per tick). Bounded worst case: the entry
                # persists one tick per failed poll while it
                # over-reserves caps — safe direction — and the window
                # is already closed, so no NEW fills accrue;
                # seen_trade_ids dedup keeps the re-polls (and the
                # partial pages' recorded fills) idempotent.
                if not self._poll_fills(q):
                    logging.warning(
                        "LONGSHOT_STALE_DROP_DEFERRED: %s %s final fill "
                        "poll failed/partial — retry next tick (R5-MN1)",
                        q["ticker"], q["order_id"])
                    continue
                with self._lock:
                    _popped = self._resting.pop(q["order_id"], None)
                # R2-C1: stale drop is a pop site too — clear the ledger row
                # (Kalshi auto-cancelled the order at window close). Skip
                # when the final poll fully filled it (already marked).
                if _popped is not None:
                    self._mark_pending(q["order_id"], "canceled")
                logging.warning(
                    "LONGSHOT_STALE_DROP: %s %s %.0fs past close with "
                    "cancel still failing — entry dropped after final "
                    "fill poll (filled %d/%d)", q["ticker"], q["order_id"],
                    -remaining, q["filled"], q["count"])

    # ── internals ─────────────────────────────────────────────────────────

    def _boot_reconcile_orphans(self) -> None:
        """R1-M1 + R2-M1: reconcile longshot orders that survived a restart.

        The _resting registry is in-memory only, so a restart orphans any
        live quote (no T-3min cancel, no fill recording). Every longshot
        client_order_id carries LONGSHOT_CLIENT_OID_PREFIX at placement.
        On the first tick:

        1. STILL-RESTING orphans (Kalshi /orders status=resting): adopt as
           synthetic resting entries — with fill_min_ts derived from the
           order's created_time (R2-M1; registered_ts = restart time would
           put pre-restart fills outside the fetch bound) — then route
           through _cancel_quote (final fill poll before pop).
        2. NO-LONGER-RESTING ls- rows (local pending_orders still
           status='resting' OR stranded in 'pending' — R5-MN3, a crash
           between insert_bot_order and confirm_order_submitted — but
           absent from the API list — fully filled or expired
           pre-restart, or never acknowledged): fetch fills bounded by
           the row's created_at, record positions, and mark the row
           filled/canceled. Step 1 repairs API-present 'pending' rows
           via confirm_order_submitted first, so step 2 only sees
           pending rows with no live order.

        On get_orders failure the latch stays unset so the next tick
        retries; a fills-fetch failure in step 2 also leaves the latch
        unset (rows still 'resting' are re-examined; step 1 re-adoption is
        idempotent — an order_id already in _resting is not re-registered,
        preserving its seen_trade_ids dedup).

        R3-M2 invariant — boot fill application is RECONCILE-AWARE
        (delta-apply): record_position_from_fill ACCUMULATES,
        seen_trade_ids is in-memory (reborn empty here), and the R2-M1
        created_time bound re-fetches fills already recorded pre-restart
        or just imported by StateManager._reconcile_positions. R4-M2
        seed: both steps seed ``boot_skip_remaining`` from the order's
        OWN ``pending_orders.recorded_fill_count`` (maintained by
        _apply_fills + the RECONCILE_IMPORT_LONGSHOT attribution) via
        _boot_skip_seed; the (ticker, side) open-row aggregate is the
        LEGACY fallback for NULL (pre-R4) rows only — the aggregate
        misattributes across sequential same-(ticker, side) orders and
        silently drops a real later-order fill. _apply_fills records
        only max(0, fetched - skip) contracts, oldest fills first.
        This also makes step-2 retries after a PARTIAL snapshot (R3-MN1)
        idempotent: the rebuilt quote's skip re-reads the counter the
        partial pass already incremented.
        """
        now = time.time()
        try:
            resp = self._client.get_orders(status="resting")
        except Exception:
            logging.warning("LONGSHOT_BOOT_RECONCILE_FAILED — retry next "
                            "tick", exc_info=True)
            return
        if resp is None:
            logging.warning("LONGSHOT_BOOT_RECONCILE_FAILED (api None) — "
                            "retry next tick")
            return
        api_orders = resp.get("orders") or []
        api_order_ids = {o.get("order_id") for o in api_orders
                         if o.get("order_id")}

        # Step 1 — adopt-and-kill still-resting orphans.
        for o in api_orders:
            coid = o.get("client_order_id") or ""
            if not coid.startswith(C.LONGSHOT_CLIENT_OID_PREFIX):
                continue
            order_id = o.get("order_id")
            ticker = o.get("ticker") or ""
            if not order_id or not ticker:
                continue
            with self._lock:
                already = order_id in self._resting
            if already:
                continue  # retry pass — keep the existing entry's dedup set
            # R5-MN3: repair a crash-between-place-and-confirm row. The
            # ledger row was inserted status='pending' with
            # order_id=client_order_id; if the process died before
            # confirm_order_submitted, the row never learned the server
            # order_id — every later lifecycle mark (keyed on the server
            # id) would match nothing and the row would stay 'pending'
            # forever. confirm_order_submitted is a no-op for
            # already-confirmed rows (WHERE status='pending'), and the
            # flip also moves the row OUT of step 2's pending scope so
            # step 1 cleanly owns API-present orders. DB failure must
            # not break adoption — log and continue.
            try:
                self._state.confirm_order_submitted(coid, order_id)
            except Exception:
                logging.warning("longshot boot pending-row repair failed "
                                "for %s", coid, exc_info=True)
            buy_side = o.get("side") or "yes"
            sell_side = "no" if buy_side == "yes" else "yes"
            # R7-M1: dollars-first price extraction — post-FP-transition
            # /orders objects carry *_price_dollars and the deprecated
            # integer fields read as None (kb/failures/ppo-monitor-bugs
            # Feb-26 lesson; mirrors state.py _reconcile_orders). The
            # pre-fix legacy-only read adopted orphans at
            # buy_price_cents=0 -> zero cost basis on recovered fills ->
            # daily-cap/streak rails blind to exactly the boot-path
            # losses the R1-M1/R2-M1 machinery exists to capture.
            _pd = (o.get("no_price_dollars") if buy_side == "no"
                   else o.get("yes_price_dollars"))
            price = dollars_str_to_cents(_pd) if _pd else (
                (o.get("no_price") if buy_side == "no"
                 else o.get("yes_price")) or 0)
            event_ticker = ticker.rsplit("-", 1)[0]
            asset = trading_mode.asset_from_ticker(ticker) or ""
            _skip = self._boot_skip_seed(order_id, ticker, buy_side)
            # R2-M2: FP-primary remaining-count extraction
            # (state.py:1622 pattern) — `count` is the ORIGINAL size.
            remaining = fp_str_to_int(o.get("remaining_count_fp")) or (
                o.get("remaining_count") or 0)
            if not remaining:
                # R5-MN2: BOTH remaining fields absent — derive from
                # cumulative truth: original minus already-recorded
                # (skip). The pre-fix original-count fallback made the
                # registered count original+skip (the register below
                # adds the skip back for R4-MN3 cumulative units), so a
                # fully-recorded orphan could never reach
                # filled >= count and ended 'canceled', not 'filled'.
                # R7-M1 adjacent: count_fp-primary twin (same
                # FP-transition class on the original-size field).
                # R8-MN2 CAVEAT: 'count_fp'/'count' as ORDER-object field
                # names are uncorroborated elsewhere in the repo (they are
                # fills-object names; the only other /orders reader uses
                # remaining_count_* only). This branch fires only when BOTH
                # remaining fields are absent — verify the live /orders
                # schema before trusting it (go-live checklist item).
                _orig = fp_str_to_int(o.get("count_fp")) or int(
                    o.get("count") or 0)
                remaining = max(0, _orig - _skip)
            # R5-M3: seed the REAL remaining window life — the stale-drop
            # backstop measures `stc_at_register - elapsed < -grace`, so
            # the pre-fix 0.0 seed made it fire 120s after RESTART, not
            # 120s after the real close, abandoning the entry (and its
            # fill polling) while the order could still be live and
            # filling on Kalshi. Unparseable ticker -> 0.0 (backstop
            # falls back to restart-anchored, the old conservative shape).
            _close_epoch = _close_epoch_from_ticker(ticker)
            _stc = (max(0.0, _close_epoch - now)
                    if _close_epoch is not None else 0.0)
            # R4-MN3: register count in CUMULATIVE units (remaining +
            # already-recorded skip). _apply_fills accumulates q["filled"]
            # over ALL fetched fills — skipped pre-restart contracts
            # included — so the pop condition (filled >= count) and the
            # LONGSHOT_FILL (filled/count) log need count in the same
            # units. Pre-fix count=remaining made a 2-of-3-prefilled
            # orphan pop as 'filled' (2 >= 1, with a (2/1) log) even
            # though the remainder was actually CANCELED; money behavior
            # was already safe (the skip budget prevented re-recording).
            self.register_resting(
                order_id=order_id, client_order_id=coid, ticker=ticker,
                event_ticker=event_ticker, asset=asset,
                sell_side=sell_side, buy_side=buy_side,
                buy_price_cents=int(price),
                count=int(remaining) + _skip,
                seconds_to_close=_stc,
                fill_min_ts=_boot_fill_min_ts(o.get("created_time"),
                                              ticker, now),
                boot_fill_skip=_skip)
            logging.warning("LONGSHOT_BOOT_ORPHAN: adopted %s %s — "
                            "final fill poll + cancel", ticker, order_id)
            self._cancel_quote(order_id, "boot_orphan")

        # Step 2 — reconcile ls- rows that are no longer resting on Kalshi
        # (fully filled / expired pre-restart): their fills were never
        # recorded and the row would stay 'resting' forever.
        all_fetched = True
        try:
            # R5-MN3: 'pending' included — a crash between
            # insert_bot_order and confirm_order_submitted strands the
            # row in 'pending' (order_id=client_order_id, no server id),
            # invisible to a status='resting'-only query forever.
            # API-present pending rows were repaired to 'resting' with
            # the server order_id by step 1 above (this query runs
            # AFTER that loop), so any 'pending' row reaching here has
            # no order resting on Kalshi. 'pending' is otherwise only a
            # transient state WITHIN the executor's synchronous
            # placement call on MainThread — the same thread that runs
            # this boot step — so no live placement can race this scan.
            rows = self._state.conn.execute(
                "SELECT order_id, client_order_id, ticker, event_ticker, "
                "asset, side, count, price_cents, created_at "
                "FROM pending_orders WHERE status IN ('resting','pending') "
                "AND client_order_id LIKE ?",
                (C.LONGSHOT_CLIENT_OID_PREFIX + "%",)).fetchall()
        except Exception:
            logging.warning("longshot boot non-resting query failed",
                            exc_info=True)
            return  # latch unset — retry next tick
        for r in rows:
            if r["order_id"] in api_order_ids:
                continue  # still resting — step 1 owns it
            _buy_side = r["side"] or "yes"
            # R5-MN3: a 'pending' row carries no server order_id (its
            # order_id column holds the client_order_id from
            # insert_bot_order); mark_order_status matches either
            # column, but fall back to client_order_id explicitly so a
            # NULL-order_id legacy shape still gets marked.
            _row_key = r["order_id"] or r["client_order_id"]
            q = {
                "order_id": _row_key,
                "client_order_id": r["client_order_id"],
                "ticker": r["ticker"],
                "event_ticker": r["event_ticker"] or "",
                "asset": r["asset"] or "",
                "buy_side": _buy_side,
                "buy_price_cents": int(r["price_cents"] or 0),
                "count": int(r["count"] or 0),
                "filled": 0,
                "seen_trade_ids": set(),
                "registered_ts": now,
                "fill_min_ts": _boot_fill_min_ts(r["created_at"],
                                                 r["ticker"] or "", now),
                "stc_at_register": 0.0,
                # R3-M2 delta-apply + R4-M2 per-order seed: the order's
                # OWN recorded_fill_count when non-NULL; (ticker, side)
                # aggregate only for legacy rows (see method docstring).
                "boot_skip_remaining": self._boot_skip_seed(
                    _row_key, r["ticker"] or "", _buy_side),
            }
            if not self._poll_fills(q):
                all_fetched = False  # retry next tick; row keeps its status
                continue
            status = ("filled" if q["count"] > 0 and q["filled"] >= q["count"]
                      else "canceled")
            self._mark_pending(_row_key, status)
            logging.warning(
                "LONGSHOT_BOOT_GONE: %s %s no longer resting on Kalshi — "
                "recorded %d/%d pre-restart fill contracts, row marked %s",
                r["ticker"], _row_key, q["filled"], q["count"], status)
        if all_fetched:
            self._boot_reconciled = True

    def _boot_skip_seed(self, order_id: str, ticker: str, side: str) -> int:
        """R4-M2: per-order boot delta-apply skip seed.

        Seeds from the order's OWN ``pending_orders.recorded_fill_count``
        when non-NULL (the counter _apply_fills maintains, plus the
        RECONCILE_IMPORT_LONGSHOT attribution); falls back to the
        (ticker, side) aggregate ONLY for legacy NULL rows (pre-R4
        schema). The aggregate misattributes across SEQUENTIAL
        same-(ticker, side) orders — order-1 partially fills (recorded)
        then cancels, order-2 quotes the remainder, crash before
        order-2's fills are polled: the aggregate (=order-1's count)
        would skip a REAL order-2 fill, a permanent under-record.
        Query failure -> 0 (fail toward recording, same direction as
        _existing_longshot_count)."""
        try:
            row = self._state.conn.execute(
                "SELECT recorded_fill_count FROM pending_orders "
                "WHERE order_id=? OR client_order_id=?",
                (order_id, order_id)).fetchone()
        except Exception:
            logging.warning("longshot boot-skip-seed query failed",
                            exc_info=True)
            return 0
        if row is not None and row["recorded_fill_count"] is not None:
            return max(0, int(row["recorded_fill_count"]))
        return self._existing_longshot_count(ticker, side)

    def _increment_recorded_fill_count(self, order_id: str, n: int) -> None:
        """R4-M2: bump the order's per-order recorded-fill counter after a
        SUCCESSFUL record_position_from_fill (the delta-skip branch never
        increments — skipped contracts were recorded by an earlier pass).
        Matches order_id OR client_order_id (mark_order_status pattern).
        Failure is logged and swallowed: an under-counted counter seeds a
        smaller boot skip, which fails toward recording (bounded
        live-small double-count healed by the next reconcile) instead of
        silently losing a position."""
        try:
            self._state.conn.execute(
                "UPDATE pending_orders SET recorded_fill_count = "
                "COALESCE(recorded_fill_count, 0) + ? "
                "WHERE order_id=? OR client_order_id=?",
                (int(n), order_id, order_id))
            self._state.conn.commit()
        except Exception:
            logging.warning(
                "longshot recorded_fill_count bump failed for %s (+%d)",
                order_id, n, exc_info=True)

    def _existing_longshot_count(self, ticker: str, side: str) -> int:
        """R3-M2: open longshot contracts already recorded for
        (ticker, side) — the LEGACY (NULL recorded_fill_count) boot
        delta-apply skip seed; per-order rows seed from their own counter
        via _boot_skip_seed (R4-M2). Returns 0 on query failure (fail
        toward recording: a bounded live-small double-count is healed by
        the next restart's reconcile, whereas a too-large skip silently
        loses a real position forever)."""
        try:
            return int(self._state.conn.execute(
                "SELECT COALESCE(SUM(count), 0) FROM positions "
                "WHERE ticker=? AND side=? AND strategy_group=? "
                "AND status='open'",
                (ticker, side, LONGSHOT_STRATEGY)).fetchone()[0] or 0)
        except Exception:
            logging.warning("longshot existing-count query failed",
                            exc_info=True)
            return 0

    def _mark_pending(self, order_id: str, status: str) -> None:
        """R2-C1: flip the pending_orders row off status='resting' whenever
        a quote is popped from the in-memory registry (canceled / filled /
        stale-dropped). The scanner's `_get_occupied_timeslots` ALSO
        excludes ls- rows defensively, but the ledger must still tell the
        truth — a permanently-'resting' row outlives the quote and leaks
        into dashboards + the executor's pending-order conflict check.
        mark_order_status matches order_id OR client_order_id
        (bot/state.py), so the server-assigned id works here. DB failure
        must never break a cancel sweep — log and continue."""
        try:
            self._state.mark_order_status(order_id, status)
        except Exception:
            logging.warning("longshot mark_order_status(%s, %s) failed",
                            order_id, status, exc_info=True)

    def _resting_for_ticker(self, ticker: str) -> List[Dict]:
        with self._lock:
            return [q for q in self._resting.values()
                    if q["ticker"] == ticker]

    def has_open_main_pipeline_position(self, ticker: str) -> Optional[bool]:
        """R1-C2 stopgap predicate: True iff any OPEN positions row on this
        ticker belongs to a non-longshot strategy_group (NULL counts as
        main — legacy rows predate the column). None on query failure
        (callers treat None as a conflict: fail-closed for placement).

        Why: the positions table PK is (ticker) and
        record_position_from_fill uses INSERT OR REPLACE keyed on
        (ticker, strategy_group) lookup — a longshot fill landing on a
        ticker the main pipeline holds would REPLACE the main row. This
        predicate (consumed by _allowed_size + the scanner overlay) plus
        the executor _active_orders/pending-order guard keep longshot off
        such tickers. REMAINDER for ticket 86badbf9t (durable composite-PK
        rebuild): the reverse direction — the MAIN pipeline initiating on
        a ticker where longshot already holds a row — is NOT guarded at
        L-1 scope. Under dual-live (main pipeline + longshot both
        placing) that is a COMMON PATH, not a narrow timing race: nothing
        on the main side consults longshot's rows before entering, so any
        main-pipeline fill on a longshot-held ticker clobbers the
        longshot positions row via INSERT OR REPLACE. Acceptable only
        while exactly one side is live; 86badbf9t must land before
        dual-live.
        """
        try:
            row = self._state.conn.execute(
                "SELECT 1 FROM positions WHERE ticker=? AND status='open' "
                "AND (strategy_group IS NULL OR strategy_group != ?) "
                "LIMIT 1",
                (ticker, LONGSHOT_STRATEGY)).fetchone()
        except Exception:
            logging.warning("longshot main-conflict query failed",
                            exc_info=True)
            return None
        return row is not None

    def has_opposite_side_longshot_position(self, ticker: str,
                                            buy_side: str) -> Optional[bool]:
        """R4-M1: True iff any OPEN strategy_group='longshot' positions row
        exists on this ticker with side != ``buy_side``. None on query
        failure (callers treat None as a conflict: fail-closed for
        placement, same convention as has_open_main_pipeline_position).

        Why ONE open longshot row per ticker: the positions table PK is
        (ticker) and record_position_from_fill matches WHERE
        ticker+strategy_group with NO side predicate — an opposite-side
        longshot fill ACCUMULATES into the existing row under the OLD
        side. Concrete path: a sell-YES fill creates (ticker, side='no');
        spot crosses the strike; sell-NO qualifies; its fill lands inside
        the side='no' row, so settlement books winners as losers and
        caps/marks/streaks corrupt. Until the durable composite-PK rebuild
        lands (ticket 86badbf9t — this self-collision instance is noted on
        that ticket), the invariant is one open longshot row per ticker:
        enforced in _allowed_size (engine sizing + executor authorize) and
        mirrored defensively at the scanner overlay so a stale-cache
        evaluate can't slip a candidate through.
        """
        try:
            row = self._state.conn.execute(
                "SELECT 1 FROM positions WHERE ticker=? AND status='open' "
                "AND strategy_group=? AND side != ? LIMIT 1",
                (ticker, LONGSHOT_STRATEGY, buy_side)).fetchone()
        except Exception:
            logging.warning("longshot side-conflict query failed",
                            exc_info=True)
            return None
        return row is not None

    def has_opposite_side_resting_quote(self, ticker: str,
                                        buy_side: str) -> bool:
        """R5-M2: registry-side twin of
        has_opposite_side_longshot_position — True iff any REGISTERED
        quote on this ticker has ``q["buy_side"] != buy_side``. Registry
        quotes ARE future positions rows (a fill records under their
        buy_side), so the one-open-longshot-row-per-ticker invariant
        (R4-M1, ticker-PK stopgap 86badbf9t) must cover them too:
        sell-YES rests -> spot crosses -> quote picked off; the same-tick
        refresh cancel hits CANCEL_FILL_MISMATCH (fill held, unrecorded);
        sell-NO qualifies — the position-row guard alone sees nothing,
        sell-NO posts, and the two fills accumulate under one side.
        Mismatch-held entries are still in the registry, so they block
        here by construction. Pure in-memory scan under the engine lock —
        no query, no failure mode. Consumed by _allowed_size (inside the
        same RLock hold as the registry sizing scan) and mirrored
        defensively at the scanner overlay."""
        with self._lock:
            return any(q["ticker"] == ticker and q["buy_side"] != buy_side
                       for q in self._resting.values())

    def _allowed_size(self, ticker: str, sell_side: str, buy_side: str,
                      buy_price_cents: int) -> int:
        """min(per-window-side cap remainder, collateral cap remainder).

        Returns 0 outright when the ticker has main-pipeline open rows
        (R1-C2 stopgap — see has_open_main_pipeline_position) OR an open
        longshot row on the OPPOSITE side (R4-M1 self-collision guard —
        see has_opposite_side_longshot_position: one open longshot row
        per ticker until 86badbf9t's composite-PK rebuild) OR a
        REGISTERED quote on the OPPOSITE side (R5-M2 — registry quotes
        are future rows; see has_opposite_side_resting_quote)."""
        conflict = self.has_open_main_pipeline_position(ticker)
        if conflict is None or conflict:
            if conflict:
                logging.info(
                    "LONGSHOT_SKIP_main_conflict: %s has open non-longshot "
                    "position rows (ticker-PK stopgap, 86badbf9t)", ticker)
            return 0
        # R4-M1: opposite-side self-collision guard. None (query failure)
        # also blocks: fail-closed for placement.
        side_conflict = self.has_opposite_side_longshot_position(
            ticker, buy_side)
        if side_conflict is None or side_conflict:
            if side_conflict:
                logging.info(
                    "LONGSHOT_SKIP_side_conflict: %s has an open longshot "
                    "row on the opposite side — one open longshot row per "
                    "ticker (ticker-PK stopgap, 86badbf9t)", ticker)
            return 0
        # Per-(window, side) cap: open longshot positions on this
        # (ticker, buy_side) + unfilled resting contracts on the same side.
        try:
            open_count = self._state.conn.execute(
                "SELECT COALESCE(SUM(count), 0) FROM positions "
                "WHERE ticker=? AND side=? AND strategy_group=? "
                "AND status='open'",
                (ticker, buy_side, LONGSHOT_STRATEGY)).fetchone()[0] or 0
        except Exception:
            logging.warning("longshot position-count query failed",
                            exc_info=True)
            return 0
        with self._lock:
            # R5-M2: opposite-side REGISTERED quote — same invariant as
            # the open-row guard above (registry quotes are future rows;
            # a CANCEL_FILL_MISMATCH-held entry with an unrecorded fill
            # is exactly the shape that reproduced the R4-M1 corruption).
            # RLock re-entry keeps the check inside the SAME lock hold
            # as the sizing scan below.
            if self.has_opposite_side_resting_quote(ticker, buy_side):
                logging.info(
                    "LONGSHOT_SKIP_side_conflict: %s has a RESTING "
                    "longshot quote on the opposite side — one open "
                    "longshot row per ticker (ticker-PK stopgap, "
                    "86badbf9t)", ticker)
                return 0
            resting_same = sum(
                max(0, q["count"] - q["filled"])
                for q in self._resting.values()
                if q["ticker"] == ticker and q["sell_side"] == sell_side)
            resting_collateral_cents = sum(
                max(0, q["count"] - q["filled"]) * q["buy_price_cents"]
                for q in self._resting.values())
        cap_remaining = (C.LONGSHOT_MAX_CONTRACTS_PER_WINDOW_SIDE
                         - int(open_count) - resting_same)

        # Concurrent-collateral cap: resting quotes (any ticker) + open
        # longshot positions (any ticker).
        try:
            open_collateral_cents = self._state.conn.execute(
                "SELECT COALESCE(SUM(total_cost_cents), 0) FROM positions "
                "WHERE strategy_group=? AND status='open'",
                (LONGSHOT_STRATEGY,)).fetchone()[0] or 0
        except Exception:
            logging.warning("longshot collateral query failed", exc_info=True)
            return 0
        collateral_cap_cents = int(
            round(C.LONGSHOT_MAX_CONCURRENT_COLLATERAL_DOLLARS * 100))
        collateral_avail = (collateral_cap_cents
                            - int(open_collateral_cents)
                            - resting_collateral_cents)
        if buy_price_cents <= 0:
            return 0
        collateral_max = collateral_avail // int(buy_price_cents)
        return max(0, min(cap_remaining, collateral_max))

    def _marked_open_loss_cents(self) -> int:
        """R1-M3: full-loss mark on open longshot positions.

        A position whose SOLD side is currently ITM (latest spot vs strike
        from this engine's evaluate_market inputs) is a near-certain full
        loss at settlement — its total_cost_cents counts toward the daily
        cap. Sold side = opposite of the position's (bought) side: bought
        NO -> sold YES, ITM when spot > threshold; bought YES -> sold NO,
        ITM when spot < threshold. Positions without a mark (window no
        longer evaluated) contribute 0 — settlement realizes them within
        minutes anyway. Returns 0 on query failure (the realized term
        still applies; see the fail-open note in _refresh_disabled).

        Mark freshness during stale episodes (R2-M1 fix round, Bit V.1):
        evaluate_market captures _mark_inputs ABOVE the frozen-spot gate,
        so while an asset's spot is event-stale the marks FREEZE at the
        last evaluated (pre-stale) spot. A frozen mark UNDERSTATES the
        marked loss if spot moved adversely during the episode (the
        sold side may have gone ITM without the mark flipping). Tolerated
        WITHOUT a mark-side fix because the same gate cancels the
        ticker's resting quotes once the episode persists past
        LONGSHOT_STALE_CANCEL_GRACE_SECONDS — residual stale-episode
        exposure is therefore already-filled positions held to
        settlement, which settlement realizes into the daily cap (the
        authoritative realized term) within minutes of window close.
        """
        try:
            rows = self._state.conn.execute(
                "SELECT ticker, side, total_cost_cents FROM positions "
                "WHERE strategy_group=? AND status='open'",
                (LONGSHOT_STRATEGY,)).fetchall()
        except Exception:
            logging.warning("longshot marked-loss query failed",
                            exc_info=True)
            return 0
        with self._lock:
            marks = dict(self._mark_inputs)
        marked = 0
        for r in rows:
            m = marks.get(r["ticker"])
            if not m:
                continue
            spot, threshold, _ts = m
            if r["side"] == "no":
                sold_itm = spot > threshold     # sold YES wins above strike
            else:
                sold_itm = spot < threshold     # sold NO wins below strike
            if sold_itm:
                marked += int(r["total_cost_cents"] or 0)
        return marked

    def _refresh_disabled(self) -> None:
        """Re-derive the auto-disable latch from the COMBINED rails.

        Bit T-1 retargeted the Bit L-1 per-strategy queries to
        bot/strategy_caps.py (single source of truth shared with
        TwaplockEngine — realized + marked PnL summed across
        ``strategy IN ('longshot','twaplock')`` vs the LIVE_SMALL_*
        constants; closes the R2-MN4 note). daily_cap clears at the next
        UTC day; consec_days is recomputed every call so
        LIVE_SMALL_STREAK_RESET_UTC_DATE takes effect without a restart.

        R1-MN3 fail-open/fail-closed asymmetry (INTENTIONAL): query
        failures here `return` early, PRESERVING the last latch state —
        i.e. fail-OPEN when the engine was enabled. The sizing-side
        queries (_allowed_size / has_open_main_pipeline_position) fail-
        CLOSED (size 0 / conflict) instead. The constraint: failing
        closed here would let one transient `database is locked` blip
        flip a healthy engine into a cancel-all sweep of every resting
        quote (a destructive, order-working action), while failing open
        on sizing would PLACE orders on unverified caps. Skipped quotes
        are always safe; spurious mass-cancels are not.
        """
        today = datetime.datetime.now(timezone.utc).date()
        today_iso = today.isoformat()

        # Same-day daily loss cap: COMBINED realized (fee-inclusive) +
        # MARKED across both live-small engines (this engine's R1-M3 mark
        # + twaplock's, via the strategy_caps mark-provider registry).
        try:
            hit, realized, marked = strategy_caps.combined_daily_cap_hit(
                self._state.conn, today_iso)
        except Exception:
            logging.warning("longshot combined daily-cap query failed",
                            exc_info=True)
            return
        if hit:
            if self._disabled_reason != "daily_cap":
                cap_cents = int(round(
                    C.LIVE_SMALL_DAILY_LOSS_CAP_DOLLARS * 100))
                logging.warning(
                    "LONGSHOT_DAILY_CAP_HIT: COMBINED live-small PnL today "
                    "realized %dc + marked -%dc <= -%dc (strategies=%s) — "
                    "auto-disabled for the rest of the UTC day",
                    realized, marked, cap_cents,
                    ",".join(strategy_caps.LIVE_SMALL_STRATEGIES))
            self._disabled_reason = "daily_cap"
            self._disabled_utc_date = today_iso
            return
        if (self._disabled_reason == "daily_cap"
                and self._disabled_utc_date != today_iso):
            self._disabled_reason = None
            self._disabled_utc_date = None

        # Consecutive completed COMBINED losing days (before today).
        n_disable = C.LIVE_SMALL_CONSECUTIVE_LOSING_DAYS_DISABLE
        try:
            streak = strategy_caps.combined_consecutive_losing_days(
                self._state.conn, today, n_disable=n_disable,
                reset_date=C.LIVE_SMALL_STREAK_RESET_UTC_DATE)
        except Exception:
            logging.warning("longshot combined streak query failed",
                            exc_info=True)
            return
        if streak >= n_disable:
            if self._disabled_reason != "consec_days":
                logging.warning(
                    "LONGSHOT_CONSEC_DAYS_DISABLE: %d consecutive COMBINED "
                    "losing days — auto-disabled until operator sets "
                    "LIVE_SMALL_STREAK_RESET_UTC_DATE", streak)
            self._disabled_reason = "consec_days"
        elif self._disabled_reason == "consec_days":
            self._disabled_reason = None

    def _cancel_all(self, reason: str) -> None:
        with self._lock:
            order_ids = list(self._resting.keys())
        for oid in order_ids:
            self._cancel_quote(oid, reason)

    def _cancel_quote(self, order_id: str, reason: str) -> None:
        """Cancel a resting quote. On API failure the quote stays
        registered so the next tick/evaluate retries the cancel
        (cancel_order is ungated at kalshi_client — reduces exposure)."""
        with self._lock:
            q = self._resting.get(order_id)
        if q is None:
            return
        try:
            resp = self._client.cancel_order(order_id)
        except Exception:
            logging.warning("LONGSHOT_CANCEL_FAILED: %s %s reason=%s — "
                            "retry next tick", q["ticker"], order_id, reason,
                            exc_info=True)
            return
        if resp is None:
            logging.warning("LONGSHOT_CANCEL_FAILED: %s %s reason=%s "
                            "(api None) — retry next tick",
                            q["ticker"], order_id, reason)
            return
        # R1-M2: order-not-found is TERMINAL — Kalshi already expired/
        # cancelled it (idempotent-DELETE 404 sentinel from _request).
        # Fall through to the final fill poll + pop; never retry.
        if isinstance(resp, dict) and resp.get("_error"):
            if resp.get("_status_code") != 404:
                logging.warning(
                    "LONGSHOT_CANCEL_FAILED: %s %s reason=%s (api error "
                    "%s) — retry next tick", q["ticker"], order_id, reason,
                    resp.get("_status_code"))
                return
            # R3-MN2: if a previous cancel kept this entry via
            # CANCEL_FILL_MISMATCH, the mismatched fills have NOT been
            # re-polled yet — a terminal pop here would lose them
            # forever (the 404 response carries no fill data). Hold the
            # entry until one clean (complete) fills poll lands on a
            # SUBSEQUENT tick (tick()'s bulk poll clears the hold); the
            # stale-drop backstop still bounds the worst case.
            if q.get("needs_clean_poll"):
                logging.warning(
                    "LONGSHOT_CANCEL_GONE_DEFERRED: %s %s reason=%s — 404 "
                    "after fill mismatch; waiting for a clean fills poll "
                    "before the terminal pop (R3-MN2)",
                    q["ticker"], order_id, reason)
                return
            logging.info("LONGSHOT_CANCEL_GONE: %s %s reason=%s — order "
                         "already expired/cancelled on Kalshi (404)",
                         q["ticker"], order_id, reason)
        # R1-C1: final fill poll BEFORE popping — a fill can land between
        # the last tick poll and the cancel taking effect; popping first
        # would orphan it (position held to settlement with no local row).
        self._poll_fills(q)
        # R1-C1: reconcile against the DELETE response when it carries a
        # filled count. If Kalshi says more contracts filled than we have
        # recorded (fills API lag), KEEP the entry registered: the next
        # tick re-polls, and the re-cancel hits the 404 idempotent path.
        api_filled = None
        if isinstance(resp, dict):
            # R2-M2: FP-primary reconcile (executor.py taker-submit
            # fill_count_fp pattern) — a DELETE response carrying only
            # fill_count_fp must not read as "no fills".
            _ord = resp.get("order") or {}
            api_filled = fp_str_to_int(_ord.get("fill_count_fp")) or \
                _ord.get("fill_count")
        if isinstance(api_filled, int) and api_filled > q["filled"]:
            # R3-MN2: require one clean (complete) fills poll on a
            # SUBSEQUENT tick before any 404-path terminal pop —
            # cleared by tick()'s bulk poll when complete.
            q["needs_clean_poll"] = True
            logging.warning(
                "LONGSHOT_CANCEL_FILL_MISMATCH: %s %s api_filled=%d "
                "recorded=%d — keeping entry for fill-poll retry",
                q["ticker"], order_id, api_filled, q["filled"])
            return
        with self._lock:
            _popped = self._resting.pop(order_id, None)
        # R2-C1: the pop must flip the pending_orders row off 'resting' —
        # otherwise the (timeslot, asset) occupancy block outlives the
        # quote. Skip when the final poll above already fully filled the
        # quote (_apply_fills popped it and marked the row 'filled').
        if _popped is not None:
            self._mark_pending(order_id, "canceled")
        logging.info("LONGSHOT_CANCEL: %s %s reason=%s", q["ticker"],
                     order_id, reason)

    def _fetch_fills_snapshot(self, min_ts: Optional[float] = None,
                              ) -> Tuple[Optional[List[Dict]], bool]:
        """R1-M6: ONE unfiltered, cursor-paginated get_fills pass.

        min_ts defaults to the earliest registered quote's fill_min_ts
        (minus slack) so the result set stays tiny; pages are followed up
        to _MAX_FILL_PAGES. R2-M1: callers reconciling orders that are no
        longer in _resting (boot non-resting reconcile / final polls)
        pass an explicit ``min_ts`` — that also bypasses the
        empty-registry early return.

        R3-MN1: returns ``(fills, complete)``. ``fills`` is None on a
        first-page failure (callers skip this tick); a mid-pagination
        failure returns the partial list with ``complete=False`` —
        per-quote trade_id dedup makes re-reads idempotent. A cursor
        still present after _MAX_FILL_PAGES also means ``complete=False``
        (page-cap exhaustion). Callers making TERMINAL decisions
        (boot step-2 row marking, the MN2 404-pop hold) must treat
        ``complete=False`` as a fetch failure; recording the partial
        page's fills still proceeds.
        """
        if min_ts is None:
            with self._lock:
                if not self._resting:
                    return ([], True)
                min_ts = min(q.get("fill_min_ts", q["registered_ts"])
                             for q in self._resting.values())
        fills: List[Dict] = []
        cursor: Optional[str] = None
        for _page in range(_MAX_FILL_PAGES):
            try:
                resp = self._client.get_fills(
                    min_ts=int(min_ts) - 60, cursor=cursor)
            except Exception:
                logging.warning("longshot get_fills failed", exc_info=True)
                return (fills, False) if fills else (None, False)
            if resp is None:
                return (fills, False) if fills else (None, False)
            fills.extend(resp.get("fills") or [])
            cursor = resp.get("cursor")
            if not cursor:
                return (fills, True)
        return (fills, False)  # page-cap exhausted with a live cursor

    def _poll_fills(self, q: Dict) -> bool:
        """Final per-quote poll (cancel / boot-orphan / stale-drop paths):
        one snapshot fetch applied to this quote only, bounded by the
        quote's own fill_min_ts (R2-M1 — works even when the quote is not
        in _resting, e.g. boot non-resting reconcile). The per-tick bulk
        path in tick() fetches ONCE and dispatches via _apply_fills.
        Returns False when the fetch failed outright OR was PARTIAL
        (R3-MN1 — partial snapshots must not drive terminal decisions;
        the partial page's fills are still applied before returning)."""
        fills, complete = self._fetch_fills_snapshot(
            min_ts=q.get("fill_min_ts", q.get("registered_ts")))
        if fills is None:
            return False
        self._apply_fills(q, fills)
        return complete

    def _apply_fills(self, q: Dict, fills: List[Dict]) -> None:
        """Record this quote's new fills as positions (dispatch by
        order_id; dedup by trade_id, synthetic key when absent).

        R3-M2: when the quote carries a boot-reconcile skip budget
        (``boot_skip_remaining`` > 0 — see _boot_reconcile_orphans
        docstring), the OLDEST fills are consumed against the budget
        WITHOUT recording (they are already embodied in existing open
        longshot rows); only the excess is recorded. Skipped contracts
        still count toward ``q["filled"]`` — the order WAS filled, the
        position just already exists locally.
        """
        matched = [f for f in fills
                   if f.get("order_id") == q["order_id"]]
        if q.get("boot_skip_remaining"):
            # Oldest first so the skip budget consumes the pre-restart
            # fills (the recorded ones) and post-restart fills survive.
            matched.sort(key=lambda f: _parse_event_ts(
                f.get("created_time") or f.get("ts")) or 0.0)
        for f in matched:
            trade_id = f.get("trade_id") or f.get("id")
            if not trade_id:
                trade_id = "syn_%s_%s_%s" % (
                    f.get("order_id", ""), f.get("count", ""),
                    f.get("price", ""))
            if trade_id in q["seen_trade_ids"]:
                continue
            # R2-M2: FP-primary count extraction (executor.py _on_fill /
            # settlement.py loss-cross-check pattern) — fills shaped with
            # only count_fp must not parse to 0.
            fill_count = fp_str_to_int(f.get("count_fp")) or int(
                f.get("count") or 0)
            if fill_count <= 0:
                # R2-M2: do NOT stamp seen_trade_ids on a zero-parse fill —
                # stamping before validation permanently blacklisted the
                # trade_id, so a transiently-malformed snapshot row could
                # never be recorded by a later, well-formed snapshot.
                continue
            q["seen_trade_ids"].add(trade_id)
            skip = min(int(q.get("boot_skip_remaining") or 0), fill_count)
            if skip:
                q["boot_skip_remaining"] -= skip
                logging.info(
                    "LONGSHOT_BOOT_FILL_DELTA: %s trade=%s skipped %d/%d "
                    "already-recorded contracts (R3-M2 delta-apply)",
                    q["ticker"], trade_id, skip, fill_count)
            record_count = fill_count - skip
            if record_count > 0:
                try:
                    self._state.record_position_from_fill(
                        q["ticker"], q["event_ticker"], q["asset"],
                        q["buy_side"], record_count, q["buy_price_cents"],
                        strategy=LONGSHOT_STRATEGY,
                        seconds_to_close=q["stc_at_register"],
                        is_taker=False, fill_source="longshot_maker",
                        execution_method="longshot_maker",
                        maker_price_cents=q["buy_price_cents"])
                except Exception:
                    logging.error("longshot record_position_from_fill failed "
                                  "for %s", q["ticker"], exc_info=True)
                    # R5-M1: un-stamp the trade_id + restore the consumed
                    # skip budget so the NEXT poll retries this fill
                    # cleanly. Pre-fix the trade_id stayed stamped (and
                    # the budget stayed consumed) on a transient record
                    # failure (`database is locked` —
                    # record_position_from_fill has no retry-on-busy), so
                    # every later poll deduped the fill, the quote
                    # eventually popped 'canceled', and the position was
                    # invisible to all rails. In the rare
                    # committed-despite-raise race the retry records the
                    # fill a second time — a bounded double-record,
                    # accepted because it fails TOWARD recording (same
                    # direction as the documented _boot_skip_seed /
                    # _existing_longshot_count failure posture; the next
                    # restart's reconcile heals it).
                    q["seen_trade_ids"].discard(trade_id)
                    if skip:
                        q["boot_skip_remaining"] = (
                            int(q.get("boot_skip_remaining") or 0) + skip)
                    continue
                # R4-M2: per-order recorded-fill counter — bumped ONLY
                # after a successful record (the skip branch above never
                # reaches here with record_count > 0 contracts of its
                # own). Seeds this order's boot_skip_remaining at the
                # next restart.
                self._increment_recorded_fill_count(q["order_id"],
                                                    record_count)
            q["filled"] += fill_count
            logging.info(
                "LONGSHOT_FILL: %s %s %dct @ %dc (%d/%d) trade=%s",
                q["ticker"], q["buy_side"], fill_count,
                q["buy_price_cents"], q["filled"], q["count"], trade_id)
        # R8-MN1: count > 0 guard mirrors boot step-2's form — a zero-count
        # adoption (all count fields absent, zero skip) must not pop as
        # 'filled' on its first poll when nothing filled.
        if q["count"] > 0 and q["filled"] >= q["count"]:
            with self._lock:
                _popped = self._resting.pop(q["order_id"], None)
            # R2-C1: fully filled -> ledger row leaves 'resting' (idempotent
            # when the entry was already popped by a sister path).
            if _popped is not None:
                self._mark_pending(q["order_id"], "filled")
