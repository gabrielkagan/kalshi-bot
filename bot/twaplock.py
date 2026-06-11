"""TWAP-lock endgame taker strategy engine (Bit T-1, 2026-06-11).

Validated via scripts/research/genhunt/01b_twap_lock_validation.py
(+14.4c/ct, day-bootstrap CI [+11.1, +17.7], n=359 over 12 days, 29.9
locks/day on the honest 4-venue index, print cross-check 99.2%, all 7
assets positive). Plan: kb/decisions/longshot-twap-live-small-plan.md.

Mechanics: Kalshi settles each 15M crypto window on a 60s TWAP of its
reference index (final ``TWAPLOCK_TWAP_WINDOW_SECONDS`` before close). In
the final ``TWAPLOCK_ENTRY_WINDOW_SECONDS`` (90s — the validated decision
grid's DEC_FROM; entries stop below ``_MIN_SUBMIT_STC_SECONDS`` = 10s, the
grid's DEC_TO) this engine computes the LIVE Coinbase-anchored estimate of
the settlement TWAP:

* ``accrued`` — time-weighted mean of per-tick Coinbase spot over the
  ELAPSED portion of the final-60s window (per-asset ring buffer fed from
  the scanner's per-tick spot read; the CoinbaseFeed internals are never
  touched).
* remaining-variance term — Brownian time-average variance from
  ``blended_rv`` (same per-5s vol surface as bot/engines/probability.py:
  per-second sigma = spot * blended_rv / sqrt(5)).

Once the locked side's probability ``p_lock`` clears
``TWAPLOCK_P_LOCK_THRESHOLD`` (0.99 live — STRICTER than the validated
0.95 because the Coinbase-vs-RTI basis adds proxy error vs the honest
4-venue validation index; undercounting events costs frequency, not
correctness — the degraded-index lesson), BUY that side as a TAKER (IOC)
if the executable ask leaves >= taker_fee + ``TWAPLOCK_MIN_EDGE_CENTS``
vs ~100c settlement. Hold to settlement: NO resting-quote lifecycle, NO
registry, NO cancel sweeps (an IOC never rests — much simpler than
bot/longshot.py by design).

Division of labor (mirrors the longshot overlay pattern):

* ``OpportunityScanner.scan()`` calls :meth:`evaluate_market` per 15M
  market; candidates flow through the NORMAL candidate list as an overlay
  (tail partition, like ``longshot``/``bracket_no``).
* ``OrderExecutor.execute()`` is the SINGLE order chokepoint — the
  trading-mode gate (bot/trading_mode.py) lives there and is NOT
  duplicated here. This module consults ``trading_mode.strategy_is_live``
  READ-ONLY to label evaluated_opportunities rows ``twaplock_live`` vs
  ``twaplock_shadow`` (cell-block string-literal discipline). Placement
  happens in ``OrderExecutor._execute_twaplock_taker`` which calls
  :meth:`authorize` (gate re-check) then :meth:`register_entry`.
* ``MainLoop._tick()`` calls :meth:`tick` — one-shot boot sweep of
  stranded tw- ledger rows + bookkeeping prune. No order-working actions.

Risk rails (all in bot/constants.py, read live via module-attribute
access so a constants flip is a runtime kill-switch):

* ``TWAPLOCK_ENABLED`` master flag (default OFF).
* ``TWAPLOCK_MAX_CONTRACTS_PER_ENTRY`` per IOC.
* ONE entry per window per asset — STRUCTURAL, not a knob (the former
  ``TWAPLOCK_MAX_ENTRIES_PER_WINDOW`` constant was retired at R1-MN4):
  binary in-memory latch + DB-derived (any tw- pending_orders row on the
  ticker consumed the shot, even a zero-fill canceled IOC; survives
  restart).
* Cross-strategy ticker exclusion: ANY open position or ANY
  pending/resting order row on the ticker blocks entry (positions PK is
  still single-ticker until 86badbf9t — same clobber class as longshot's
  R1-C2 stopgap; twaplock blocks on EVERY strategy including longshot).
* COMBINED daily loss cap + combined consecutive-losing-days disable via
  bot/strategy_caps.py (LIVE_SMALL_* constants — shared with longshot;
  the Bit L-1 per-strategy cap was retargeted here in this Bit).

Regression lock: tests/integration/test_twaplock_strategy.py.
"""
from __future__ import annotations

import datetime
import logging
import math
import threading
import time
from collections import deque
from datetime import timezone
from typing import Callable, Deque, Dict, List, Optional, Tuple

import bot.constants as C
from bot import strategy_caps, trading_mode
# Shared Kalshi-book executable-ask extraction (single source of truth —
# bot/longshot.py owns the helpers; no cycle: longshot never imports
# twaplock).
from bot.longshot import _executable_asks
from bot.models import calculate_taker_fee

TWAPLOCK_STRATEGY = "twaplock"
TWAPLOCK_FILTER_STAGE_LIVE = "twaplock_live"
TWAPLOCK_FILTER_STAGE_SHADOW = "twaplock_shadow"

# Below this STC an IOC races settlement (API round trip + matching) —
# the MIN_ORDER_SUBMIT_STC_S settlement-race class — AND the validated
# decision grid ends here (01b_twap_lock_validation.py DEC_TO=10): no
# backtest evidence for [5, 10), so we don't trade it (R1-MN1).
_MIN_SUBMIT_STC_SECONDS = 10.0

# Per-asset spot ring buffer: covers the 90s entry window + slack at the
# scanner's per-tick cadence; samples older than this are pruned.
_SPOT_BUFFER_TTL_SECONDS = 180.0
_SPOT_BUFFER_MAXLEN = 720
# Drop same-tick duplicate reads (one window per asset can surface the
# same spot through multiple evaluate_market calls in one scan tick).
_SPOT_MIN_INTERVAL_SECONDS = 0.25

# Per-ticker bookkeeping (_entered / _eval_row_seen / _mark_inputs) is
# pruned once older than this — windows are 15 minutes, so 30 minutes
# comfortably outlives any entry (same TTL as bot/longshot.py).
_SEEN_TTL_SECONDS = 1800.0

_SQRT2 = math.sqrt(2.0)


def compute_p_lock(spot: Optional[float], threshold: Optional[float],
                   seconds_to_close: Optional[float],
                   blended_rv: Optional[float],
                   accrued_mean: Optional[float] = None,
                   twap_window_seconds: float = 60.0,
                   ) -> Tuple[Optional[float], Optional[float]]:
    """P(YES settles) for a TWAP-settled market + the signed z it came from.

    The settlement value is the time-average of the reference index over
    the final ``twap_window_seconds`` (W). Under a Brownian spot with
    per-second sigma ``sigma_s = spot * blended_rv / sqrt(5)`` (blended_rv
    is per-5s vol — same denominator family as
    bot/engines/probability.py), with ``r = seconds_to_close``:

    * BEFORE the window (r >= W): TWAP ~ Normal(spot,
      sigma_s^2 * ((r - W) + W/3)) — drift-to-window-start variance plus
      the time-average term.
    * INSIDE the window (0 < r < W): elapsed fraction f = (W - r)/W is
      already accrued at ``accrued_mean`` (A); the remainder averages a
      Brownian path from the current spot: TWAP ~ Normal(
      f*A + (1-f)*spot, (1-f)^2 * sigma_s^2 * r/3).

    Returns ``(p_yes, z)`` with ``z = (mean - threshold) / sd`` and
    ``p_yes = Phi(z)``. Returns ``(None, None)`` on any non-positive /
    missing input AND when inside the window with ``accrued_mean=None``
    (restart mid-window / feed gap) — callers must treat that as
    "no signal", never as 0 or 0.5. Zero/negative vol is NO SIGNAL, not
    certainty: a dead vol reading is a data problem, and this strategy's
    one-sided payoff makes overconfidence the expensive direction.
    """
    if spot is None or spot <= 0 or threshold is None or threshold <= 0:
        return (None, None)
    if seconds_to_close is None or seconds_to_close <= 0:
        return (None, None)
    if blended_rv is None or blended_rv <= 0:
        return (None, None)
    if twap_window_seconds <= 0:
        return (None, None)
    sigma_s = spot * blended_rv / math.sqrt(5.0)
    r = float(seconds_to_close)
    w = float(twap_window_seconds)
    if r >= w:
        mean = float(spot)
        var = sigma_s * sigma_s * ((r - w) + w / 3.0)
    else:
        if accrued_mean is None or accrued_mean <= 0:
            return (None, None)
        f = (w - r) / w
        mean = f * float(accrued_mean) + (1.0 - f) * float(spot)
        var = (1.0 - f) ** 2 * sigma_s * sigma_s * (r / 3.0)
    if var <= 0:
        return (None, None)
    z = (mean - float(threshold)) / math.sqrt(var)
    p_yes = 0.5 * (1.0 + math.erf(z / _SQRT2))
    return (p_yes, z)


class TwaplockEngine:
    """Lock detection, risk rails, and one-shot bookkeeping for TWAP-lock.

    Holds NO order-placement authority: placement goes through
    ``OrderExecutor.execute()`` (the trading-mode chokepoint) into
    ``_execute_twaplock_taker``. The engine never cancels anything — IOC
    orders cannot rest. The ``client`` is currently unused (kept for
    constructor symmetry with LongshotEngine and future REST needs).
    """

    def __init__(self, client, state, logger=None):
        self._client = client
        self._state = state
        self._logger = logger
        self._lock = threading.RLock()
        # asset -> deque[(ts, price)] — per-tick Coinbase spot samples.
        self._spot_buf: Dict[str, Deque[Tuple[float, float]]] = {}
        # ticker -> ts of the one entry SHOT (set by register_entry when
        # the executor places — consumed even on zero fill / api_error so
        # a failing market is never hammered).
        self._entered: Dict[str, float] = {}
        # (ticker, side) -> ts of the one eval-row write (mirrors the
        # longshot/scanner _eval_opp_seen dedup; candidates still emit
        # every tick — only the DB write is deduped).
        self._eval_row_seen: Dict[Tuple[str, str], float] = {}
        # ticker -> (spot, threshold, ts) — latest engine inputs, used to
        # MARK open twaplock positions (bought side currently OTM = full
        # loss) for the COMBINED daily cap (bot/strategy_caps.py).
        self._mark_inputs: Dict[str, Tuple[float, float, float]] = {}
        self._disabled_reason: Optional[str] = None
        self._disabled_utc_date: Optional[str] = None
        # One-shot boot sweep latch (first tick): stranded tw- ledger rows.
        self._boot_swept = False
        # The COMBINED cap sums marks across engines — register ours.
        strategy_caps.register_mark_provider(
            TWAPLOCK_STRATEGY, self._marked_open_loss_cents)

    # ── public surface ────────────────────────────────────────────────────

    def disabled_reason(self) -> Optional[str]:
        """'daily_cap' | 'consec_days' | None (as of the last refresh)."""
        return self._disabled_reason

    def record_spot(self, asset: str, spot: float,
                    ts: Optional[float] = None) -> None:
        """Append a (ts, price) sample to the asset's ring buffer.

        Fed from the per-tick scanner read (the spot evaluate_market
        receives originated from CoinbaseFeed.get_price_with_ts — the
        feed internals are never touched). Same-tick duplicates are
        dropped; samples older than the buffer TTL are pruned.
        """
        if spot is None or spot <= 0:
            return
        if ts is None:
            ts = time.time()
        with self._lock:
            buf = self._spot_buf.get(asset)
            if buf is None:
                buf = deque(maxlen=_SPOT_BUFFER_MAXLEN)
                self._spot_buf[asset] = buf
            if buf and ts - buf[-1][0] < _SPOT_MIN_INTERVAL_SECONDS:
                return
            buf.append((float(ts), float(spot)))
            cutoff = ts - _SPOT_BUFFER_TTL_SECONDS
            while buf and buf[0][0] < cutoff:
                buf.popleft()

    def register_entry(self, ticker: str, now: Optional[float] = None) -> None:
        """Consume the one-shot for this window (executor placement path).

        Called BEFORE the API call fires so a failed/ambiguous placement
        still consumes the shot — defensive: one-shot frequency loss is
        cheap, a hot retry loop into a settling market is not."""
        with self._lock:
            self._entered[ticker] = now if now is not None else time.time()

    def evaluate_market(self, *, ticker: str, event_ticker: str, asset: str,
                        product_type: Optional[str], spot: Optional[float],
                        threshold: Optional[float],
                        seconds_to_close: Optional[float],
                        blended_rv: Optional[float],
                        orderbook_fetch: Callable[[], Optional[Dict]],
                        config_snapshot_id: Optional[int],
                        balance_at_scan: Optional[float],
                        now: Optional[float] = None) -> List[Dict]:
        """Evaluate one market; return 0..1 twaplock candidates.

        Side effects: feeds the per-asset spot ring buffer, stamps mark
        inputs, and writes evaluated_opportunities rows (filter_stage
        twaplock_live/twaplock_shadow — labeling consults bot.trading_mode
        READ-ONLY; the gate stays at executor.execute()). Returns []
        without touching the DB when TWAPLOCK_ENABLED is off or an
        auto-disable rail is latched.
        """
        if not C.TWAPLOCK_ENABLED:
            return []
        if now is None:
            now = time.time()
        # Buffer + marks accrue on EVERY enabled tick (the buffer must
        # predate the TWAP window for the accrued mean to exist, and the
        # marked-loss term needs this tick's spot/threshold) — BEFORE the
        # disable refresh, mirroring longshot's R1-M3 ordering.
        self.record_spot(asset, spot, ts=now)
        if (spot is not None and spot > 0
                and threshold is not None and threshold > 0):
            with self._lock:
                self._mark_inputs[ticker] = (float(spot), float(threshold),
                                             now)
        self._refresh_disabled()
        if self._disabled_reason:
            return []

        if (seconds_to_close is None
                or not (_MIN_SUBMIT_STC_SECONDS <= seconds_to_close
                        <= C.TWAPLOCK_ENTRY_WINDOW_SECONDS)):
            return []
        if self._already_entered(ticker):
            return []

        w = C.TWAPLOCK_TWAP_WINDOW_SECONDS
        accrued = None
        if seconds_to_close < w:
            accrued = self._accrued_mean(
                asset, now - (w - seconds_to_close), now)
        p_yes, z = compute_p_lock(spot, threshold, seconds_to_close,
                                  blended_rv, accrued_mean=accrued,
                                  twap_window_seconds=w)
        if p_yes is None:
            return []
        thr = C.TWAPLOCK_P_LOCK_THRESHOLD
        if p_yes >= thr:
            side = "yes"
        elif (1.0 - p_yes) >= thr:
            side = "no"
        else:
            return []
        p_side = p_yes if side == "yes" else 1.0 - p_yes

        # Cross-strategy ticker exclusion (single-ticker positions PK
        # until 86badbf9t): ANY open position or ANY pending/resting
        # order row blocks. Fail-closed on query failure.
        if self._conflicting_exposure(ticker):
            return []

        ob = orderbook_fetch()
        if ob is None:
            return []
        yes_ask, no_ask = _executable_asks(ob)
        ask = yes_ask if side == "yes" else no_ask
        if ask is None or not (0 < ask <= 99):
            return []
        # Fee + margin gate vs ~100c settlement. Per-1-contract taker fee
        # is conservative (Kalshi ceils the TOTAL, so per-contract fee at
        # count=2 can only be lower).
        fee_cents = calculate_taker_fee(1, ask)
        if ask > 100 - fee_cents - C.TWAPLOCK_MIN_EDGE_CENTS:
            return []

        size = int(C.TWAPLOCK_MAX_CONTRACTS_PER_ENTRY)
        if size <= 0:
            return []
        edge = p_side - (ask / 100.0)
        live = trading_mode.strategy_is_live(TWAPLOCK_STRATEGY, asset)
        filter_stage = (TWAPLOCK_FILTER_STAGE_LIVE if live
                        else TWAPLOCK_FILTER_STAGE_SHADOW)
        # One eval row per (ticker, side) — not one per tick (mirrors
        # longshot R1-MN1 / scanner _eval_opp_seen). Candidates still emit.
        _seen_key = (ticker, side)
        with self._lock:
            _row_seen = _seen_key in self._eval_row_seen
            if not _row_seen:
                self._eval_row_seen[_seen_key] = now
        if not _row_seen:
            try:
                self._state.insert_evaluated_opportunity(
                    ticker, event_ticker, asset, filter_stage,
                    spot_price=spot, threshold=threshold,
                    volatility=blended_rv,
                    market_price=ask,  # the locked side's executable ask
                    seconds_to_close=seconds_to_close,
                    calibrated_prob=round(p_side, 6),
                    raw_prob=round(p_side, 6),
                    edge=round(edge, 6),
                    strategy=TWAPLOCK_STRATEGY,
                    position_size=size,
                    z_score=round(z, 4) if z is not None else None,
                    side=side,
                    product_type=product_type or "15m",
                    config_snapshot_id=config_snapshot_id,
                )
            except Exception:
                logging.warning(
                    "insert_evaluated_opportunity failed (%s)",
                    filter_stage, exc_info=True)
        return [{
            "ticker": ticker,
            "event_ticker": event_ticker,
            "asset": asset,
            "product_type": product_type or "15m",
            "spot": spot,
            "threshold": threshold,
            "seconds_to_close": round(float(seconds_to_close), 1),
            "blended_rv": blended_rv,
            "strategy": TWAPLOCK_STRATEGY,
            "side": side,
            "twaplock_ask_cents": ask,
            "twaplock_p_lock": round(p_yes if side == "yes"
                                     else 1.0 - p_yes, 6),
            "twaplock_accrued_mean": accrued,
            "twaplock_fee_cents": fee_cents,
            # Per-contract cost convention (longshot/bracket_no pattern):
            # the executor's exposure caps read best_yes_ask as
            # cents-at-risk per contract — for a taker buy that is the
            # executable ask of the bought side.
            "best_yes_ask": ask,
            "position_size": size,
            "calibrated_prob": round(p_side, 6),
            "edge": round(edge, 6),
            "z_score": round(z, 4) if z is not None else None,
            "balance_at_scan": balance_at_scan,
        }]

    def authorize(self, candidate: Dict) -> int:
        """Execute-time gate re-check (scan->execute race defense).

        Returns the contract count the executor may submit (0 = blocked).
        Re-reads the one-shot latch + cross-strategy exposure so a sister
        placement between scan and execute blocks this order.
        """
        if not C.TWAPLOCK_ENABLED:
            return 0
        self._refresh_disabled()
        if self._disabled_reason:
            return 0
        ticker = candidate["ticker"]
        if self._already_entered(ticker):
            return 0
        if self._conflicting_exposure(ticker):
            return 0
        return max(0, min(int(candidate.get("position_size") or 0),
                          int(C.TWAPLOCK_MAX_CONTRACTS_PER_ENTRY)))

    def tick(self, now: Optional[float] = None) -> None:
        """Per-main-loop-tick housekeeping (NO order-working actions).

        1. One-shot boot sweep: tw- ledger rows stranded in
           'pending'/'resting' by a crash mid-placement are flipped to
           'canceled' — an IOC never rests on Kalshi, so the rows are
           lies; the money side is owned by StateManager's positions-API
           reconcile (RECONCILE_IMPORT stamps strategy_group='twaplock'
           from the tw- pending history).
        2. Prune per-ticker bookkeeping past TTL.
        """
        if now is None:
            now = time.time()
        if not self._boot_swept:
            self._boot_sweep_stranded_rows()
        with self._lock:
            cutoff = now - _SEEN_TTL_SECONDS
            self._entered = {k: ts for k, ts in self._entered.items()
                             if ts >= cutoff}
            self._eval_row_seen = {k: ts for k, ts
                                   in self._eval_row_seen.items()
                                   if ts >= cutoff}
            self._mark_inputs = {k: v for k, v in self._mark_inputs.items()
                                 if v[2] >= cutoff}

    # ── internals ─────────────────────────────────────────────────────────

    def _accrued_mean(self, asset: str, window_start: float,
                      now: float) -> Optional[float]:
        """Time-weighted (step-hold) mean of spot over [window_start, now].

        Requires at least one sample at-or-before ``window_start`` (the
        buffer is fed from entry-window scan ticks, which begin ~60s
        before the TWAP window opens, so this holds in normal operation;
        a restart mid-window or feed gap returns None — no signal).
        """
        if now <= window_start:
            return None
        with self._lock:
            samples = list(self._spot_buf.get(asset) or ())
        if not samples:
            return None
        # value at window_start = last sample at-or-before it
        start_val = None
        for ts, px in samples:
            if ts <= window_start:
                start_val = px
            else:
                break
        if start_val is None:
            return None
        area = 0.0
        cur_t, cur_v = window_start, start_val
        for ts, px in samples:
            if ts <= window_start:
                continue
            if ts >= now:
                break
            area += (ts - cur_t) * cur_v
            cur_t, cur_v = ts, px
        area += (now - cur_t) * cur_v
        return area / (now - window_start)

    def _already_entered(self, ticker: str) -> bool:
        """One shot per window per asset — STRUCTURAL invariant, no knob.

        (The former TWAPLOCK_MAX_ENTRIES_PER_WINDOW constant was retired
        at R1-MN4: the binary latch below could never honor any value but
        1.) In-memory latch (set at placement) OR DB-derived: ANY tw-
        ledger row on the ticker — including a zero-fill canceled IOC or
        an api_error attempt — consumed the shot (defensive: never hammer
        a settling market; survives restart). Fail-closed on query
        failure.
        """
        with self._lock:
            if ticker in self._entered:
                return True
        try:
            row = self._state.conn.execute(
                "SELECT 1 FROM pending_orders WHERE ticker=? "
                "AND client_order_id LIKE ? LIMIT 1",
                (ticker, C.TWAPLOCK_CLIENT_OID_PREFIX + "%"),
            ).fetchone()
        except Exception:
            logging.warning("twaplock entered-count query failed",
                            exc_info=True)
            return True
        return row is not None

    def _conflicting_exposure(self, ticker: str) -> bool:
        """True when the ticker has ANY open position (any strategy —
        main, longshot, twaplock) or ANY pending/resting order row.

        Why every strategy: the positions PK is still (ticker) until
        86badbf9t's composite-PK rebuild, so a twaplock fill landing on a
        held ticker would INSERT OR REPLACE someone else's row (and a
        same-ticker resting maker could fill after us into the same
        collision). Fail-closed on query failure (skipping an entry is
        always safe). Mirrors longshot's R1-C2 stopgap, widened to ALL
        strategies because twaplock has no carve-out need of its own.
        """
        try:
            row = self._state.conn.execute(
                "SELECT 1 FROM positions WHERE ticker=? AND status='open' "
                "LIMIT 1", (ticker,)).fetchone()
            if row is not None:
                logging.info(
                    "TWAPLOCK_SKIP_position_conflict: %s has an open "
                    "position (ticker-PK stopgap, 86badbf9t)", ticker)
                return True
            row = self._state.conn.execute(
                "SELECT 1 FROM pending_orders WHERE ticker=? "
                "AND status IN ('pending','resting') LIMIT 1",
                (ticker,)).fetchone()
            if row is not None:
                logging.info(
                    "TWAPLOCK_SKIP_order_conflict: %s has a pending/resting "
                    "order row (any strategy)", ticker)
                return True
        except Exception:
            logging.warning("twaplock exposure-conflict query failed",
                            exc_info=True)
            return True
        return False

    def _marked_open_loss_cents(self) -> int:
        """Full-loss mark on open twaplock positions for the COMBINED cap.

        A position whose BOUGHT side is currently OTM (latest spot vs
        strike from this engine's evaluate_market inputs) means the lock
        broke — count its total_cost_cents as full loss. Positions
        without a mark contribute 0 (settlement realizes them within
        minutes anyway). Returns 0 on query failure (the realized term
        still applies — same posture as longshot's R1-M3 mark).
        Registered with bot/strategy_caps.py so BOTH engines' combined
        cap sees it.
        """
        try:
            rows = self._state.conn.execute(
                "SELECT ticker, side, total_cost_cents FROM positions "
                "WHERE strategy_group=? AND status='open'",
                (TWAPLOCK_STRATEGY,)).fetchall()
        except Exception:
            logging.warning("twaplock marked-loss query failed",
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
            if r["side"] == "yes":
                bought_otm = spot < threshold   # bought YES loses below
            else:
                bought_otm = spot > threshold   # bought NO loses above
            if bought_otm:
                marked += int(r["total_cost_cents"] or 0)
        return marked

    def _refresh_disabled(self) -> None:
        """Re-derive the auto-disable latch from the COMBINED rails.

        daily_cap clears at the next UTC day; consec_days is recomputed
        every call so LIVE_SMALL_STREAK_RESET_UTC_DATE takes effect
        without a restart. Query failures `return` early, PRESERVING the
        last latch state (fail-OPEN — same R1-MN3 asymmetry rationale as
        bot/longshot.py: a transient `database is locked` blip must not
        flip a healthy engine into a latched state; the placement-side
        checks fail CLOSED instead).
        """
        today = datetime.datetime.now(timezone.utc).date()
        today_iso = today.isoformat()
        try:
            hit, realized, marked = strategy_caps.combined_daily_cap_hit(
                self._state.conn, today_iso)
        except Exception:
            logging.warning("twaplock combined daily-cap query failed",
                            exc_info=True)
            return
        if hit:
            if self._disabled_reason != "daily_cap":
                cap_cents = int(round(
                    C.LIVE_SMALL_DAILY_LOSS_CAP_DOLLARS * 100))
                logging.warning(
                    "TWAPLOCK_DAILY_CAP_HIT: COMBINED live-small PnL today "
                    "realized %dc + marked -%dc <= -%dc "
                    "(strategies=%s) — auto-disabled for the rest of the "
                    "UTC day", realized, marked, cap_cents,
                    ",".join(strategy_caps.LIVE_SMALL_STRATEGIES))
            self._disabled_reason = "daily_cap"
            self._disabled_utc_date = today_iso
            return
        if (self._disabled_reason == "daily_cap"
                and self._disabled_utc_date != today_iso):
            self._disabled_reason = None
            self._disabled_utc_date = None

        n_disable = C.LIVE_SMALL_CONSECUTIVE_LOSING_DAYS_DISABLE
        try:
            streak = strategy_caps.combined_consecutive_losing_days(
                self._state.conn, today, n_disable=n_disable,
                reset_date=C.LIVE_SMALL_STREAK_RESET_UTC_DATE)
        except Exception:
            logging.warning("twaplock combined streak query failed",
                            exc_info=True)
            return
        if streak >= n_disable:
            if self._disabled_reason != "consec_days":
                logging.warning(
                    "TWAPLOCK_CONSEC_DAYS_DISABLE: %d consecutive COMBINED "
                    "losing days — auto-disabled until operator sets "
                    "LIVE_SMALL_STREAK_RESET_UTC_DATE", streak)
            self._disabled_reason = "consec_days"
        elif self._disabled_reason == "consec_days":
            self._disabled_reason = None

    def _boot_sweep_stranded_rows(self) -> None:
        """One-shot first-tick sweep of stranded tw- ledger rows.

        A crash between insert_bot_order and the synchronous post-IOC
        status mark strands a tw- row in 'pending' (or 'resting' when the
        crash hit between confirm_order_submitted and the mark). IOC
        orders never rest on Kalshi, so the rows can only be lies — flip
        them to 'canceled' so they don't poison the executor's
        pending-order conflict checks or dashboards forever. The MONEY
        side (a fill the crash hid) is owned by StateManager's
        positions-API reconcile at startup, which imports unknown
        positions with strategy_group='twaplock' from the tw- pending
        history (prefix-map stamp in bot/state.py). The state.py
        reconciler carve-outs deliberately skip tw- rows so this sweep is
        the single owner. DB failure leaves the latch unset (retry next
        tick). No race with live placements: this runs on MainThread —
        the same thread the executor's synchronous placement uses.
        """
        try:
            rows = self._state.conn.execute(
                "SELECT order_id, client_order_id, ticker FROM "
                "pending_orders WHERE status IN ('pending','resting') "
                "AND client_order_id LIKE ?",
                (C.TWAPLOCK_CLIENT_OID_PREFIX + "%",)).fetchall()
            for r in rows:
                key = r["order_id"] or r["client_order_id"]
                self._state.mark_order_status(key, "canceled")
                logging.warning(
                    "TWAPLOCK_BOOT_STRANDED: %s %s flipped to 'canceled' "
                    "(IOC rows cannot legitimately rest; positions-API "
                    "reconcile owns any hidden fill)", r["ticker"], key)
        except Exception:
            logging.warning("twaplock boot sweep failed — retry next tick",
                            exc_info=True)
            return
        self._boot_swept = True
