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
* ``LONGSHOT_DAILY_LOSS_CAP_DOLLARS`` — realized longshot PnL today at or
  below -cap -> same-day auto-disable (log signature LONGSHOT_DAILY_CAP_HIT).
* ``LONGSHOT_CONSECUTIVE_LOSING_DAYS_DISABLE`` consecutive completed losing
  days -> persistent disable; operator clears via
  ``LONGSHOT_STREAK_RESET_UTC_DATE``.

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
from bot import trading_mode

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

_SQRT2 = math.sqrt(2.0)


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
                         count: int, seconds_to_close: float) -> None:
        """Track a successfully-posted maker quote for lifecycle management."""
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
                "registered_ts": time.time(),
                "stc_at_register": float(seconds_to_close),
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
                    "insert_evaluated_opportunity failed (%s)", filter_stage,
                    exc_info=True)
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
            fills = self._fetch_fills_snapshot()
            if fills is not None:
                for q in quotes:
                    self._apply_fills(q, fills)

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
                self._poll_fills(q)
                with self._lock:
                    self._resting.pop(q["order_id"], None)
                logging.warning(
                    "LONGSHOT_STALE_DROP: %s %s %.0fs past close with "
                    "cancel still failing — entry dropped after final "
                    "fill poll (filled %d/%d)", q["ticker"], q["order_id"],
                    -remaining, q["filled"], q["count"])

    # ── internals ─────────────────────────────────────────────────────────

    def _boot_reconcile_orphans(self) -> None:
        """R1-M1: adopt-and-kill longshot orders that survived a restart.

        The _resting registry is in-memory only, so a restart orphans any
        live quote (no T-3min cancel, no fill recording). Every longshot
        client_order_id carries LONGSHOT_CLIENT_OID_PREFIX at placement;
        on the first tick we list open orders, adopt the prefixed ones as
        synthetic resting entries, then route them through _cancel_quote
        (which final-polls fills before popping). On API failure the latch
        stays unset so the next tick retries.
        """
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
        self._boot_reconciled = True
        for o in (resp.get("orders") or []):
            coid = o.get("client_order_id") or ""
            if not coid.startswith(C.LONGSHOT_CLIENT_OID_PREFIX):
                continue
            order_id = o.get("order_id")
            ticker = o.get("ticker") or ""
            if not order_id or not ticker:
                continue
            buy_side = o.get("side") or "yes"
            sell_side = "no" if buy_side == "yes" else "yes"
            price = (o.get("no_price") if buy_side == "no"
                     else o.get("yes_price")) or 0
            event_ticker = ticker.rsplit("-", 1)[0]
            asset = trading_mode.asset_from_ticker(ticker) or ""
            self.register_resting(
                order_id=order_id, client_order_id=coid, ticker=ticker,
                event_ticker=event_ticker, asset=asset,
                sell_side=sell_side, buy_side=buy_side,
                buy_price_cents=int(price), count=int(o.get("count") or 0),
                seconds_to_close=0.0)
            logging.warning("LONGSHOT_BOOT_ORPHAN: adopted %s %s — "
                            "final fill poll + cancel", ticker, order_id)
            self._cancel_quote(order_id, "boot_orphan")

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
        rebuild): the reverse race — the MAIN pipeline initiating on a
        ticker where longshot already holds a row AFTER these checks ran —
        is NOT guarded at L-1 scope.
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

    def _allowed_size(self, ticker: str, sell_side: str, buy_side: str,
                      buy_price_cents: int) -> int:
        """min(per-window-side cap remainder, collateral cap remainder).

        Returns 0 outright when the ticker has main-pipeline open rows
        (R1-C2 stopgap — see has_open_main_pipeline_position)."""
        conflict = self.has_open_main_pipeline_position(ticker)
        if conflict is None or conflict:
            if conflict:
                logging.info(
                    "LONGSHOT_SKIP_main_conflict: %s has open non-longshot "
                    "position rows (ticker-PK stopgap, 86badbf9t)", ticker)
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
        """Re-derive the auto-disable latch from settled_trades.

        daily_cap clears at the next UTC day; consec_days is recomputed
        every call so LONGSHOT_STREAK_RESET_UTC_DATE takes effect without
        a restart.
        """
        today = datetime.datetime.now(timezone.utc).date()
        today_iso = today.isoformat()

        # Same-day daily loss cap: realized (fee-inclusive) + MARKED —
        # open longshot positions whose sold side is currently ITM count
        # as full loss (R1-M3; plan doc "realized+marked").
        try:
            today_pnl = self._state.conn.execute(
                "SELECT COALESCE(SUM(pnl_cents - COALESCE(fee_cents, 0)), 0) "
                "FROM settled_trades WHERE strategy=? "
                "AND substr(settled_at, 1, 10) = ?",
                (LONGSHOT_STRATEGY, today_iso)).fetchone()[0] or 0
        except Exception:
            logging.warning("longshot daily-pnl query failed", exc_info=True)
            return
        marked_cents = self._marked_open_loss_cents()
        cap_cents = int(round(C.LONGSHOT_DAILY_LOSS_CAP_DOLLARS * 100))
        if today_pnl - marked_cents <= -cap_cents:
            if self._disabled_reason != "daily_cap":
                logging.warning(
                    "LONGSHOT_DAILY_CAP_HIT: longshot PnL today realized "
                    "%dc + marked -%dc <= -%dc — auto-disabled for the "
                    "rest of the UTC day", today_pnl, marked_cents,
                    cap_cents)
            self._disabled_reason = "daily_cap"
            self._disabled_utc_date = today_iso
            return
        if (self._disabled_reason == "daily_cap"
                and self._disabled_utc_date != today_iso):
            self._disabled_reason = None
            self._disabled_utc_date = None

        # Consecutive completed losing days (calendar days before today).
        n_disable = C.LONGSHOT_CONSECUTIVE_LOSING_DAYS_DISABLE
        reset_date = (C.LONGSHOT_STREAK_RESET_UTC_DATE or "").strip()
        streak = 0
        for back in range(1, n_disable + 1):
            day_iso = (today - datetime.timedelta(days=back)).isoformat()
            if reset_date and day_iso <= reset_date:
                break
            try:
                row = self._state.conn.execute(
                    "SELECT SUM(pnl_cents - COALESCE(fee_cents, 0)) "
                    "FROM settled_trades WHERE strategy=? "
                    "AND substr(settled_at, 1, 10) = ?",
                    (LONGSHOT_STRATEGY, day_iso)).fetchone()
            except Exception:
                logging.warning("longshot streak query failed", exc_info=True)
                return
            day_pnl = row[0] if row else None
            if day_pnl is None or day_pnl >= 0:
                break  # no activity or non-losing day ends the streak
            streak += 1
        if streak >= n_disable:
            if self._disabled_reason != "consec_days":
                logging.warning(
                    "LONGSHOT_CONSEC_DAYS_DISABLE: %d consecutive losing "
                    "days — auto-disabled until operator sets "
                    "LONGSHOT_STREAK_RESET_UTC_DATE", streak)
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
            api_filled = (resp.get("order") or {}).get("fill_count")
        if isinstance(api_filled, int) and api_filled > q["filled"]:
            logging.warning(
                "LONGSHOT_CANCEL_FILL_MISMATCH: %s %s api_filled=%d "
                "recorded=%d — keeping entry for fill-poll retry",
                q["ticker"], order_id, api_filled, q["filled"])
            return
        with self._lock:
            self._resting.pop(order_id, None)
        logging.info("LONGSHOT_CANCEL: %s %s reason=%s", q["ticker"],
                     order_id, reason)

    def _fetch_fills_snapshot(self) -> Optional[List[Dict]]:
        """R1-M6: ONE unfiltered, cursor-paginated get_fills pass.

        min_ts is bounded to the earliest registered quote (minus slack)
        so the result set stays tiny; pages are followed up to
        _MAX_FILL_PAGES. Returns None on a first-page failure (callers
        skip this tick); a mid-pagination failure returns the partial
        list — per-quote trade_id dedup makes re-reads idempotent.
        """
        with self._lock:
            if not self._resting:
                return []
            min_reg = min(q["registered_ts"]
                          for q in self._resting.values())
        fills: List[Dict] = []
        cursor: Optional[str] = None
        for _page in range(_MAX_FILL_PAGES):
            try:
                resp = self._client.get_fills(
                    min_ts=int(min_reg) - 60, cursor=cursor)
            except Exception:
                logging.warning("longshot get_fills failed", exc_info=True)
                return fills if fills else None
            if resp is None:
                return fills if fills else None
            fills.extend(resp.get("fills") or [])
            cursor = resp.get("cursor")
            if not cursor:
                break
        return fills

    def _poll_fills(self, q: Dict) -> None:
        """Final per-quote poll (cancel / boot-orphan / stale-drop paths):
        one snapshot fetch applied to this quote only. The per-tick bulk
        path in tick() fetches ONCE and dispatches via _apply_fills."""
        fills = self._fetch_fills_snapshot()
        if fills is None:
            return
        self._apply_fills(q, fills)

    def _apply_fills(self, q: Dict, fills: List[Dict]) -> None:
        """Record this quote's new fills as positions (dispatch by
        order_id; dedup by trade_id, synthetic key when absent)."""
        for f in fills:
            if f.get("order_id") != q["order_id"]:
                continue
            trade_id = f.get("trade_id") or f.get("id")
            if not trade_id:
                trade_id = "syn_%s_%s_%s" % (
                    f.get("order_id", ""), f.get("count", ""),
                    f.get("price", ""))
            if trade_id in q["seen_trade_ids"]:
                continue
            q["seen_trade_ids"].add(trade_id)
            fill_count = int(f.get("count") or 0)
            if fill_count <= 0:
                continue
            try:
                self._state.record_position_from_fill(
                    q["ticker"], q["event_ticker"], q["asset"],
                    q["buy_side"], fill_count, q["buy_price_cents"],
                    strategy=LONGSHOT_STRATEGY,
                    seconds_to_close=q["stc_at_register"],
                    is_taker=False, fill_source="longshot_maker",
                    execution_method="longshot_maker",
                    maker_price_cents=q["buy_price_cents"])
            except Exception:
                logging.error("longshot record_position_from_fill failed "
                              "for %s", q["ticker"], exc_info=True)
                continue
            q["filled"] += fill_count
            logging.info(
                "LONGSHOT_FILL: %s %s %dct @ %dc (%d/%d) trade=%s",
                q["ticker"], q["buy_side"], fill_count,
                q["buy_price_cents"], q["filled"], q["count"], trade_id)
        if q["filled"] >= q["count"]:
            with self._lock:
                self._resting.pop(q["order_id"], None)
