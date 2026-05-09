"""KalshiFeed — Kalshi WebSocket for fills + orderbook deltas.

Extracted from bot/_impl.py in Sprint 4 Bit 4.5b (2026-05-09). The
class is the largest leaf in the Sprint 4 modularization track at
~1,790 lines: full asyncio daemon-thread loop, fill notifications,
orderbook delta + snapshot handling, get_snapshot health watchdog
(R1 / A1 / R2 / P0 hardening), and the Apr 26 2026 unsubscribe
blacklist (window-rotation race defense). Sister classes
``CoinbaseFeed``, ``CrossExchangeFeed``, and the
``OrderbookSchemaError`` exception (raised inside this class) shipped
in the earlier Bit 4.5a; this bit completes the ``bot/feeds/``
subpackage.

Imports are deliberate: stdlib (``asyncio``, ``base64``, ``json``,
``logging``, ``random``, ``threading``, ``time``,
``collections.deque``, ``typing``) + ``websockets`` +
``cryptography.hazmat.primitives.hashes`` +
``cryptography.hazmat.primitives.asymmetric.padding`` (RSA-PSS
signature for the WS handshake — same auth shape as the REST
``KalshiClient`` extracted in Bit 4.3) + ``bot.constants`` (13
explicit names; all WS_* tunables plus ``KALSHI_WS_URL``) + sibling
``bot.feeds.orderbook_schema.OrderbookSchemaError`` (Bit 4.5a — the
exception was extracted ahead of this larger move so the raise/except
sites would land local to the ``bot/feeds/`` package). Does NOT
import ``bot._impl`` (would create a circular import — ``_impl``
re-exports this class via ``from bot.feeds import KalshiFeed``).

Construction site: ``MainLoop.__init__`` in ``bot/_impl.py`` does
``self.kalshi_feed = KalshiFeed(api_key, self.client.private_key)``
where ``self.client`` is a ``KalshiClient`` (bot/kalshi_client.py,
Bit 4.3). The WS handshake reuses the REST client's already-loaded
``private_key`` so we don't re-deserialize the PEM.

Sister state inside ``OpportunityScanner.scan`` (still in
``bot/_impl.py``) interacts with ``unsubscribe_ticker`` via the
window-rotation cleanup loop — the relevant call site is anchored
with the ``ws_expired = set(expired) | set(expired_ob)`` search
string (L30: search anchors > literal line refs across cross-file
boundaries that shift with each modularization bit).
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import random
import threading
import time
from collections import deque
from typing import Dict, List, Optional, Set, Tuple

import websockets
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding

from bot.constants import (
    KALSHI_WS_URL,
    WS_FORCE_RESUB_COOLDOWN_S,
    WS_FORCE_RESUB_RECOVERY_TIMEOUT_S,
    WS_GET_SNAPSHOT_DISABLE_AFTER,
    WS_OUTSTANDING_SUBSCRIBE_TIMEOUT_S,
    WS_RAW_LOG_DURATION_S,
    WS_RAW_LOG_MAX_PER_SESSION,
    WS_RAW_LOG_TRUNCATE,
    WS_SILENCE_GRACE_SECONDS,
    WS_SILENCE_TIMEOUT_SECONDS,
    WS_SNAPSHOT_REQUEST_TIMEOUT_S,
    WS_UNSUBSCRIBE_BLACKLIST_S,
    WS_WATCHDOG_CHECK_INTERVAL,
)
from bot.feeds.orderbook_schema import OrderbookSchemaError


class KalshiFeed:
    """Kalshi WebSocket feed for real-time fill notifications and orderbook data.

    Runs an asyncio event loop in a daemon thread (same pattern as CoinbaseFeed).
    Shares fill/orderbook data with the synchronous main loop via lock-protected state.

    Channels:
      - fill: instant fill notifications (subscribed once at connect)
      - orderbook_delta: real-time OB snapshots + deltas (per-ticker)

    Wire contract (Kalshi 2026 schema, verified against docs.kalshi.com):
      orderbook_snapshot.msg: {market_ticker, yes_dollars_fp, no_dollars_fp, ...}
        where *_dollars_fp is an array of [dollar_str, fp_qty_str] pairs.
      orderbook_delta.msg:    {market_ticker, price_dollars, delta_fp, side, ...}
        — single additive update, NOT grouped by side.
    """

    def __init__(self, api_key: str, private_key):
        self._api_key = api_key
        self._private_key = private_key
        self._lock = threading.Lock()
        self._connected = False
        self._thread: Optional[threading.Thread] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._stop_event: Optional[asyncio.Event] = None
        # Shared state (lock-protected)
        self._orderbooks: Dict[str, Dict] = {}
        self._recent_fills: deque = deque(maxlen=10000)
        self._subscribed_tickers: Set[str] = set()
        self._pending_subscribes: List[str] = []
        self._pending_unsubscribes: List[str] = []
        # Phase 2: WS cache reconciliation. Tickers in this list get
        # an `update_subscription` with `action: get_snapshot` sent
        # next time _process_pending_subs runs. Snapshot arrival is
        # tracked in `_snapshot_request_pending`; if no snapshot
        # arrives within WS_SNAPSHOT_REQUEST_TIMEOUT_S, falls back to
        # queuing the ticker into _pending_unsubscribes + _pending_subscribes.
        self._pending_snapshot_requests: List[str] = []
        # ticker -> monotonic timestamp when get_snapshot was sent
        # (cleared by _handle_ob_snapshot on receipt; checked by
        # _check_snapshot_timeouts to trigger fallback).
        self._snapshot_request_pending: Dict[str, float] = {}
        # ticker -> monotonic timestamp of last force_resubscribe call
        # (rate-limit per WS_FORCE_RESUB_COOLDOWN_S; prevents loops).
        self._force_resub_cooldown: Dict[str, float] = {}
        # Apr 26 2026 incident: window-rotation race produced
        # subscribe→delete→subscribe→delete loop on settled tickers.
        # `unsubscribe_ticker` records ticker->unblock_monotonic_ts here;
        # `subscribe_ticker` and `force_resubscribe` consult and skip if
        # the entry is fresh. Prevents lazy `_get_orderbook_cached`
        # paths in scan body (running on stale `_local_windows`) from
        # undoing the worker thread's discovery cleanup.
        # See kb/failures/scan-loop-stall-window-rotation-2026-04-26.md.
        self._unsubscribe_blacklist: Dict[str, float] = {}
        # R1 / A1 [P0] + R2 / P0-2 [CRITICAL]: track get_snapshot
        # health. Original "consecutive timeouts counter +=
        # len(timed_out)" was wrong — with 4 tickers in a periodic
        # sweep, ONE bad sweep with 3+ ticker timeouts would trip
        # disable, even though the issue could be a transient blip.
        # New semantics: count consecutive fully-failed sweeps.
        # A "failed sweep" = a `_check_snapshot_timeouts` call that
        # produced ≥1 timeout AND no snapshot has arrived since
        # last reset. ANY successful snapshot resets the counter
        # to 0. After WS_GET_SNAPSHOT_DISABLE_AFTER consecutive
        # failed sweeps, primary path auto-disables.
        # Reset on every WS reconnect (R2 / P1-5) so a fresh
        # session gets to re-prove the contract.
        self._get_snapshot_consecutive_failed_sweeps: int = 0
        self._get_snapshot_disabled: bool = False
        self._get_snapshot_disabled_logged: bool = False
        # R1 / A5 [P1]: ticker -> monotonic deadline by which the
        # cache is expected to be repopulated (after a force_resub).
        # Watchdog walks this in _check_snapshot_timeouts and warns
        # if the ticker is still missing from _orderbooks past the
        # deadline. Cleared on snapshot receipt.
        self._force_resub_recovery_deadline: Dict[str, float] = {}
        self._force_resub_recovery_warned: Dict[str, bool] = {}
        # Phase 2.5: ticker -> Kalshi-assigned subscription id (sid).
        # Per Kalshi WS docs, `update_subscription` requires the sid
        # in params (NOT market_tickers). Pre-Phase-2.5 we sent
        # `params.market_tickers` which Kalshi rejected with an
        # error frame; the rejection was silently dropped because
        # `_handle_message` only routed snapshot/delta/fill types.
        # Result: 100% of get_snapshot requests timed out → primary
        # path auto-disabled on every restart (R5 finding,
        # 2026-04-25).
        # Map is populated from envelope-level `sid` on incoming
        # orderbook_snapshot / orderbook_delta messages — every
        # Kalshi message carries the sid for its subscription so we
        # learn the binding naturally. Cleared on unsubscribe and
        # on WS reconnect (sids are session-scoped).
        self._ticker_to_sid: Dict[str, int] = {}
        # Phase 2.5: dedup set for WS_ERROR_FRAME logging.
        # Keyed by (id, code) so repeat errors during a session
        # produce one WARNING line, not thousands. Cleared on
        # WS reconnect (different session = errors may be transient).
        self._ws_error_frame_seen: Set[Tuple] = set()
        # Phase 2.6: AUTHORITATIVE sid tracking. Phase 2.5 tried to
        # learn ticker→sid from envelope-level `sid` on incoming
        # orderbook_snapshot/orderbook_delta messages. Kalshi
        # rejected the resulting `update_subscription` commands
        # with code=7 "Unknown subscription ID" — proving the
        # envelope sid is NOT the same id Kalshi expects in
        # commands. Per Kalshi docs the subscription id is
        # returned in the `type=subscribed` response to subscribe
        # commands. To match the response back to the ticker we
        # subscribed, we use unique monotonic command IDs and
        # this outstanding map. (Pre-2.6 we used static id=2 for
        # all subscribes — making response matching impossible.)
        self._next_msg_id: int = 100  # avoid collision with id=1 (fill)
        self._outstanding_subscribes: Dict[int, str] = {}
        # Phase 2.6 R2 / B2: per-cmd-id timestamp for outstanding
        # subscribe watchdog. If type=subscribed never arrives
        # within WS_OUTSTANDING_SUBSCRIBE_TIMEOUT_S, log WARNING
        # and pop the entry — otherwise the ticker is permanently
        # stuck in "subscribe in flight" with force_resubscribe
        # SKIPing forever.
        self._outstanding_subscribe_ts: Dict[int, float] = {}
        # Phase 2.6 R2 / B1: dedup set for WS_ORPHAN_SID warnings.
        # Keyed by (ticker, envelope_sid, expected_sid) so each
        # leak event logs once per session. Cleared on reconnect.
        self._ws_orphan_sid_seen: Set[Tuple] = set()
        # Phase 2.6 R4 / A1+A2+A3: when B2 watchdog detects a
        # stuck subscribe AND the ticker is still in
        # _subscribed_tickers, set this flag. Silence watchdog
        # observes it and force-closes the WS so the bot
        # reconnects fresh — only path that recovers a stuck
        # ticker, since periodic resnap and drift detector both
        # SKIP a sid-less ticker. Without this, one stuck
        # subscribe = one ticker on permanent stale cache until
        # natural disconnect (could be hours).
        self._force_reconnect_requested: bool = False
        # Phase 2.6 R4 / A8: tickers whose subscribe is in flight
        # at the moment of unsubscribe_ticker. The type=subscribed
        # response handler will use the freshly-learned sid to
        # immediately queue an unsubscribe (rather than silently
        # dropping the sid → leaving a leaked Kalshi subscription
        # we cannot reach).
        self._pending_late_unsubscribes: Set[str] = set()
        # Phase 2.9 R-review A3: per-session raw log line counter.
        # Reset on every reconnect via _cleanup_session_state.
        self._raw_log_count: int = 0
        self._raw_log_capped_logged: bool = False
        self._ws = None
        # One-shot schema probes — log the first snapshot/delta msg keys per run so
        # post-deploy verifier can confirm the live wire matches the contract.
        # Remove in follow-up commit after verification.
        self._snapshot_schema_probed = False
        self._delta_schema_probed = False
        # Empirical delta-semantic probe: log first N deltas' before/after qty
        # to verify additive vs absolute interpretation. See
        # kb/failures/kalshi-ws-schema-drift.md § "test-as-spec addendum".
        self._delta_probe_count = 0
        self._delta_probe_max = 5
        # WS sequence-gap detector (H3 hypothesis for delta underflows).
        # Kalshi WS envelope carries (sid, seq) per subscription. Seq should be
        # monotonically increasing per sid. Gaps = dropped/reordered messages.
        # Diagnostic only — remove after hypothesis confirmed/rejected.
        # See kb/failures/kalshi-ws-schema-drift.md § "WS delta underflow".
        self._ws_last_seq: Dict[int, int] = {}
        self._ws_seq_gap_logs = 0
        self._ws_seq_gap_max_logs = 500
        # WS silence watchdog (2026-04-24 17:30 UTC 15M outage defense).
        # Kalshi's WS can stay "connected" while delivering zero protocol
        # messages — pings/pongs are handled internally by the websockets
        # library and don't surface in the message iterator. When this
        # happens, subscribes never flush, no snapshots arrive, 15M dies
        # silently. Track last message arrival and force reconnect if idle
        # too long. See kb/failures/ws-15m-silence-2026-04-24.md.
        self._ws_last_msg_ts: float = 0.0
        self._ws_connect_ts: float = 0.0

    # ── Public API (called from main thread) ──────────────────────────────

    def start(self):
        self._thread = threading.Thread(target=self._run_thread, daemon=True)
        self._thread.start()

    def stop(self):
        if self._loop and self._stop_event:
            self._loop.call_soon_threadsafe(self._stop_event.set)

    def subscribe_ticker(self, ticker: str):
        with self._lock:
            # Apr 26 incident: silent-skip if recently unsubscribed.
            # Lazy `_get_orderbook_cached` calls in scan body running
            # on a stale `_local_windows` snapshot would otherwise
            # undo the worker thread's window-rotation cleanup,
            # producing the 1Hz subscribe→delete loop.
            now_mono = time.monotonic()
            self._sweep_unsubscribe_blacklist(now_mono)
            unblock = self._unsubscribe_blacklist.get(ticker)
            if unblock is not None and now_mono < unblock:
                return
            if ticker not in self._subscribed_tickers:
                self._pending_subscribes.append(ticker)
                self._subscribed_tickers.add(ticker)

    def _sweep_unsubscribe_blacklist(self, now_mono: float) -> None:
        """Prune expired entries from `_unsubscribe_blacklist`. Called
        from every subscribe_ticker / force_resubscribe / unsubscribe_ticker
        invocation so the dict cannot grow unbounded over the bot's
        lifetime — settled 15M tickers (~384/day) and weather/SPX/
        sports tickers create unique strings that, without sweeping,
        would accumulate forever in the dict. (R1 [A4].)

        Caller MUST hold `self._lock`. Cheap O(n) scan; n is bounded
        by tickers unsubscribed in the last WS_UNSUBSCRIBE_BLACKLIST_S
        seconds, so n is small in steady state.
        """
        expired = [t for t, exp in self._unsubscribe_blacklist.items()
                   if now_mono >= exp]
        for t in expired:
            del self._unsubscribe_blacklist[t]

    def get_subscribed_tickers(self) -> List[str]:
        """R1 / A3 [P1]: thread-safe snapshot of currently-subscribed
        tickers. The WS thread mutates `_subscribed_tickers` via
        add()/discard(); MainLoop must hold the lock to read it
        atomically. Returns a list (copy) so the caller can iterate
        without race."""
        with self._lock:
            return list(self._subscribed_tickers)

    def force_resubscribe(
        self,
        ticker: str,
        *,
        purge_cache: bool = True,
        bypass_cooldown: bool = False,
        track_recovery: bool = True,
    ) -> None:
        """Phase 2 / Apr 25 2026: force a fresh server snapshot for
        a ticker whose WS cache may be drifted. Tries
        `update_subscription` with `action: get_snapshot` first
        (per Kalshi docs — preserves subscription, no gap). If no
        snapshot arrives within WS_SNAPSHOT_REQUEST_TIMEOUT_S, falls
        back to unsubscribe + resubscribe (definitively works).

        Rate-limited per ticker via WS_FORCE_RESUB_COOLDOWN_S to
        prevent re-sub loops on flapping connections / repeat
        detector firings. Periodic insurance bypasses the cooldown
        so a long-running drift detector firing doesn't starve the
        scheduled resnap.

        Args:
            ticker: market ticker to reset.
            purge_cache: if True (default), drop the cached orderbook
                immediately so scan paths see no_orderbook (and dedup
                via _eval_opp_seen) rather than reading drifted data.
                R1 / A2 [P0]: periodic insurance passes False so the
                5-min sweep doesn't simultaneously evict caches for
                every 15M ticker, creating a system-wide gap. Detected-
                drift callers (flag_ticker_drifted) keep True since
                we KNOW that cache is bad.
            bypass_cooldown: if True, skip the per-ticker rate limit.
                R1 / A7 [P1]: periodic 5-min sweep should always run,
                regardless of whether flag_ticker_drifted recently
                fired for the same ticker.
            track_recovery: if True (default), set/refresh
                `_force_resub_recovery_deadline[ticker]` and clear
                any prior `_force_resub_recovery_warned` flag — the
                watchdog will surface the ticker if cache stays empty
                past the deadline. R3 / P0-B + P1-D + P1-F:
                drift-detector path needs this even with
                purge_cache=False (drift was DETECTED — silent
                failure must be observable). Periodic insurance
                passes False — periodic doesn't constitute a
                detected-drift event, so its watchdog timer would
                only generate noise.

        No-op if ticker isn't currently subscribed (nothing to reset).

        Standard practice (Binance, Bybit, Kraken, Polymarket):
        snapshot reset is the only reliable cure for cumulative WS
        cache drift. The Apr 24 60s REST-bypass cooldown was a
        symptom-bandaid; this is the actual reset action.
        """
        with self._lock:
            # Apr 26 incident: defense-in-depth. Even if some path
            # re-added the ticker to _subscribed_tickers (bypassing
            # subscribe_ticker's blacklist check), the blacklist
            # gate here prevents the R1 watchdog / drift-detector
            # paths from injecting subscribe/snapshot work on a
            # ticker that was just unsubscribed.
            now_mono = time.monotonic()
            self._sweep_unsubscribe_blacklist(now_mono)
            unblock = self._unsubscribe_blacklist.get(ticker)
            if unblock is not None and now_mono < unblock:
                return
            if ticker not in self._subscribed_tickers:
                return  # not subscribed; nothing to reset
            now = now_mono
            if not bypass_cooldown:
                last = self._force_resub_cooldown.get(ticker)
                if (last is not None
                        and (now - last) < WS_FORCE_RESUB_COOLDOWN_S):
                    # Within cooldown — skip to prevent loops.
                    return
            self._force_resub_cooldown[ticker] = now

            sid_known = ticker in self._ticker_to_sid

            # Phase 2.6 R-review A1 [P0]: if the subscribe is
            # in flight (no sid yet), we CANNOT take any action:
            #   - primary get_snapshot requires sid → can't send
            #   - fallback unsub also requires sid → would SKIP
            #   - resub-only would create a DUPLICATE
            #     subscription on Kalshi's side (sid_v2), leaking
            #     sid_v1 forever. We could never unsubscribe
            #     sid_v1 because we'd never know its value.
            # The CORRECT behavior is true no-op: when sid lands
            # via type=subscribed, future force_resubscribe calls
            # will work normally. Caller (drift detector,
            # periodic) accepts brief degraded recovery for the
            # subscribe-startup window (~100ms-2s).
            if not sid_known:
                logging.info(
                    "force_resubscribe SKIPPED: ticker=%s has no "
                    "sid yet (subscribe in flight). Skipping to "
                    "avoid duplicate-subscription leak; future "
                    "calls will run after sid is learned.", ticker)
                return

            # R1 / A1 [P0]: if the primary path was auto-disabled
            # after 3 consecutive failed sweeps, skip get_snapshot
            # and queue the proper sid-based unsub+resub fallback.
            if self._get_snapshot_disabled:
                if purge_cache:
                    self._orderbooks.pop(ticker, None)
                if ticker not in self._pending_unsubscribes:
                    self._pending_unsubscribes.append(ticker)
                if ticker not in self._pending_subscribes:
                    self._pending_subscribes.append(ticker)
                if track_recovery:
                    self._force_resub_recovery_deadline[ticker] = (
                        now + WS_FORCE_RESUB_RECOVERY_TIMEOUT_S)
                    self._force_resub_recovery_warned.pop(
                        ticker, None)
                return

            # Optional cache purge. R1 / A2: periodic skips this so
            # all-tickers-purged-at-once doesn't happen.
            if purge_cache:
                # Phase 1's rejection wiring makes the brief gap
                # observable (no_orderbook).
                self._orderbooks.pop(ticker, None)
            # Primary path: queue update_subscription/get_snapshot.
            # If a snapshot arrives, _handle_ob_snapshot clears the
            # pending entry. If timeout fires, we fall back to
            # unsub+resub via _check_snapshot_timeouts.
            if ticker not in self._pending_snapshot_requests:
                self._pending_snapshot_requests.append(ticker)
            self._snapshot_request_pending[ticker] = now
            if track_recovery:
                # R3 / P0-B + P1-D + P1-F: refresh deadline + clear
                # warned flag. Decoupled from purge_cache so
                # drift-detector's purge=False path still gets a
                # watchdog (drift was DETECTED — silent failure must
                # surface). Periodic passes track_recovery=False so
                # its purge=False sweep doesn't generate watchdog
                # noise.
                self._force_resub_recovery_deadline[ticker] = (
                    now + WS_FORCE_RESUB_RECOVERY_TIMEOUT_S)
                self._force_resub_recovery_warned.pop(ticker, None)

    def _check_snapshot_timeouts(self) -> List[str]:
        """Call from the WS event loop. For any ticker whose
        get_snapshot request hasn't been fulfilled within
        WS_SNAPSHOT_REQUEST_TIMEOUT_S, queue an unsubscribe +
        resubscribe as the definitive fallback.

        Also runs the R1/A5 post-resub recovery watchdog: any ticker
        still missing from `_orderbooks` past its recovery deadline
        gets a one-shot WARNING.

        Also drives R1/A1: counts consecutive timeouts; after
        WS_GET_SNAPSHOT_DISABLE_AFTER, sets `_get_snapshot_disabled`
        so subsequent force_resubscribe calls skip the primary path.

        Returns the list of tickers that fell back (for logging)."""
        now = time.monotonic()
        timed_out: List[str] = []
        recovery_warns: List[str] = []
        disabled_now = False
        with self._lock:
            for t, req_ts in list(self._snapshot_request_pending.items()):
                if now - req_ts > WS_SNAPSHOT_REQUEST_TIMEOUT_S:
                    timed_out.append(t)
                    del self._snapshot_request_pending[t]
            for t in timed_out:
                if t in self._subscribed_tickers:
                    if t not in self._pending_unsubscribes:
                        self._pending_unsubscribes.append(t)
                    if t not in self._pending_subscribes:
                        self._pending_subscribes.append(t)
                    # Phase 2.6 R6 / A1: do NOT pre-pop _ticker_to_sid.
                    # The drain's `_send_ob_unsubscribe` needs the
                    # sid to actually send the unsubscribe to Kalshi;
                    # it pops on successful send. Pre-pop = drain
                    # SKIP = Kalshi-side subscription leak. Same bug
                    # as R5 (removed from unsubscribe_ticker), this
                    # is the symmetric path. The original Phase 2.5
                    # monotonic-guard concern is no longer relevant
                    # in 2.6 since envelope sids aren't learned at
                    # all (sids come from type=subscribed only).

            # R1 / A1 + R2 / P0-2: count consecutive FAILED SWEEPS,
            # not per-ticker timeouts. A sweep with ≥1 timeout =
            # one failed sweep. _handle_ob_snapshot resets the
            # counter on any successful snapshot. With 4 tickers,
            # this means a single bad sweep can't permanently
            # disable the primary path; it takes
            # WS_GET_SNAPSHOT_DISABLE_AFTER consecutive sweeps
            # producing ZERO snapshot fulfillments to disable.
            if timed_out and not self._get_snapshot_disabled:
                self._get_snapshot_consecutive_failed_sweeps += 1
                if (self._get_snapshot_consecutive_failed_sweeps
                        >= WS_GET_SNAPSHOT_DISABLE_AFTER):
                    self._get_snapshot_disabled = True
                    disabled_now = (
                        not self._get_snapshot_disabled_logged)
                    self._get_snapshot_disabled_logged = True

            # Phase 2.6 R2 / B2: outstanding-subscribe watchdog.
            # If type=subscribed never arrives within
            # WS_OUTSTANDING_SUBSCRIBE_TIMEOUT_S, the ticker is
            # permanently stuck in "no sid" state and
            # force_resubscribe SKIPs forever. Pop the entry +
            # log so the ticker can be re-subscribed via a fresh
            # _send_ob_subscribe path (or just gets resub'd on
            # next reconnect / market_refresh cycle).
            stuck_subscribes: List[Tuple[int, str]] = []
            for cid, ts in list(
                    self._outstanding_subscribe_ts.items()):
                if (now - ts
                        > WS_OUTSTANDING_SUBSCRIBE_TIMEOUT_S):
                    stuck_ticker = (
                        self._outstanding_subscribes.pop(cid, None))
                    del self._outstanding_subscribe_ts[cid]
                    if stuck_ticker is not None:
                        stuck_subscribes.append((cid, stuck_ticker))
                        # Phase 2.6 R4 / A1+A2+A3: if the stuck
                        # ticker is still in _subscribed_tickers,
                        # request a WS reconnect — only path that
                        # gets it a fresh sid (periodic resnap
                        # and drift detector both SKIP). Without
                        # forced reconnect, the silence watchdog
                        # never fires (other tickers keep
                        # _ws_last_msg_ts fresh) and the ticker
                        # is silently stuck on stale cache.
                        if stuck_ticker in self._subscribed_tickers:
                            self._force_reconnect_requested = True

            # R1 / A5 + R4 / F2: walk recovery deadlines, surface
            # stuck tickers. The signal "snapshot didn't arrive"
            # is "deadline still in dict past expiry" — because
            # `_handle_ob_snapshot` pops the deadline on receipt.
            # Pre-R4 this checked `t not in self._orderbooks`,
            # which was wrong for the drift-detector path
            # (purge_cache=False keeps the cache populated, so
            # `t in _orderbooks` was always True → silent skip,
            # even when no fresh snapshot ever arrived → exact
            # "drift-triggered failures are silent" bug R3 was
            # supposed to fix).
            for t, deadline in list(
                    self._force_resub_recovery_deadline.items()):
                if now > deadline:
                    if not self._force_resub_recovery_warned.get(t):
                        recovery_warns.append(t)
                        self._force_resub_recovery_warned[t] = True
                    # Don't pop the deadline — let the next call
                    # to force_resubscribe(track_recovery=True)
                    # refresh it (clearing the warned flag) so a
                    # repeated drift firing produces a fresh
                    # warning. _handle_ob_snapshot pops both
                    # deadline and warned flag on a successful
                    # snapshot, which is the proper "recovered"
                    # signal.
        # Logging outside the lock.
        if disabled_now:
            logging.error(
                "WS_GET_SNAPSHOT_DISABLED — %d consecutive timeouts "
                "on update_subscription/get_snapshot path. Falling "
                "back to unsub+resub for all future force_resubscribe "
                "calls. Investigate Kalshi WS contract.",
                WS_GET_SNAPSHOT_DISABLE_AFTER)
        for t in recovery_warns:
            logging.warning(
                "WS_RESUB_STUCK %s — no fresh snapshot %ds after "
                "force_resubscribe. Drift-detected ticker may still "
                "be serving stale cache; REST fallback path will "
                "fill the gap.",
                t, int(WS_FORCE_RESUB_RECOVERY_TIMEOUT_S))
        for cid, stuck_ticker in stuck_subscribes:
            logging.warning(
                "WS_SUBSCRIBE_STUCK ticker=%s id=%s — "
                "type=subscribed never arrived within %ds. Popped "
                "orphan. Drift recovery DISABLED for this ticker "
                "(force_resubscribe will SKIP — no sid). Recovery "
                "happens on next WS reconnect (silence watchdog "
                "or session error).",
                stuck_ticker, cid,
                int(WS_OUTSTANDING_SUBSCRIBE_TIMEOUT_S))
        return timed_out

    def unsubscribe_ticker(self, ticker: str):
        # KNOWN LIMITATION (R2 [A1]): the blacklist applies regardless
        # of whether `ticker` is a held-position ticker. Two callers
        # traverse window-rotation cleanup:
        #   - `discovery_ob_subscribe` (worker thread) excludes
        #     `_held_tickers` from the expired set before calling
        #     this method.
        #   - `OpportunityScanner` scan-tick cleanup (search anchor
        #     "ws_expired = set(expired)" in bot/_impl.py)
        #     iterates `ws_expired = expired ∪ expired_ob` from
        #     `_ticker_ask_history`/`_ob_cache` minus `active_tickers`,
        #     and does NOT apply the same held-tickers exclusion.
        # If a held ticker is ever unsubscribed by ANY caller (e.g.,
        # the scan-tick path during a transient `_ticker_ask_history`
        # mismatch), position-monitor WS subscribes will silent-skip
        # for up to WS_UNSUBSCRIBE_BLACKLIST_S. Practical incidence
        # is low (worker cleanup excludes held tickers, and held
        # 15M positions only exist while the window is still active
        # → ticker is in `active_tickers` → not expired). Mitigation
        # if observed: add a held-tickers callback parameter and skip
        # blacklist entry for held tickers, OR add the same held-
        # tickers exclusion to the scan-tick cleanup loop.
        # Out of scope for the immediate fix.
        with self._lock:
            # Apr 26 incident: blacklist the ticker for
            # WS_UNSUBSCRIBE_BLACKLIST_S so any concurrent lazy
            # subscribe_ticker (scan body's stale _local_windows)
            # or force_resubscribe (R1 watchdog reading stale
            # active_windows) is suppressed until the bot's
            # in-memory views converge on the new window set.
            now_mono = time.monotonic()
            self._sweep_unsubscribe_blacklist(now_mono)
            self._unsubscribe_blacklist[ticker] = (
                now_mono + WS_UNSUBSCRIBE_BLACKLIST_S)
            if ticker in self._subscribed_tickers:
                self._pending_unsubscribes.append(ticker)
                self._subscribed_tickers.discard(ticker)
                self._orderbooks.pop(ticker, None)
            # R2 / P0-1: clean up ALL Phase 2 state for the ticker.
            # Without this, settled-window churn:
            #   (a) emits false WS_RESUB_STUCK warnings 30s later
            #       (deadline still set, _orderbooks empty for an
            #       UNRELATED reason — the ticker rolled),
            #   (b) leaves stale snapshot-request entries that
            #       eventually time out, incrementing the disable
            #       counter from ordinary lifecycle (not real
            #       Kalshi-contract failures),
            #   (c) leaks _force_resub_cooldown entries forever
            #       (unbounded dict growth across days of trading).
            self._snapshot_request_pending.pop(ticker, None)
            self._force_resub_cooldown.pop(ticker, None)
            self._force_resub_recovery_deadline.pop(ticker, None)
            self._force_resub_recovery_warned.pop(ticker, None)
            # Phase 2.6 R5 / P1: do NOT pre-pop _ticker_to_sid here.
            # The drain's `_send_ob_unsubscribe` needs the sid to
            # actually send the unsubscribe; it pops on successful
            # send. Pre-popping here would silently drop the
            # freshly-learned sid (in the benign race where
            # type=subscribed lands seconds before unsubscribe_ticker)
            # → drain SKIPs → Kalshi-side subscription leaks. The
            # late-unsub fence below covers the OTHER race
            # (subscribe still in flight); together they close
            # both windows.
            try:
                self._pending_snapshot_requests.remove(ticker)
            except ValueError:
                pass
            # Phase 2.6 R4 / A8: if subscribe is in flight (cmd_id
            # registered in _outstanding_subscribes for this
            # ticker), the type=subscribed response will arrive
            # AFTER this unsubscribe call. Without intervention,
            # we'd silently drop the response (ticker not in
            # _subscribed_tickers) and Kalshi would retain a live
            # subscription we can never unsubscribe — leaked.
            # Mark the ticker so the subscribed handler knows to
            # send an unsubscribe with the freshly-learned sid.
            if any(t == ticker
                   for t in self._outstanding_subscribes.values()):
                self._pending_late_unsubscribes.add(ticker)

    def get_orderbook(self, ticker: str) -> Optional[Dict]:
        with self._lock:
            return self._orderbooks.get(ticker)

    def pop_fills(self) -> List[Dict]:
        with self._lock:
            fills = list(self._recent_fills)
            self._recent_fills.clear()
            return fills

    def _cleanup_session_state(self) -> None:
        """R4 / F1: clear all WS-session-bound state. Called from
        BOTH the exception path (BEFORE the backoff sleep — so
        is_connected reads False during reconnect wait) AND the
        graceful-close path (so the prior session's _orderbooks
        don't survive into the next iteration).
        """
        with self._lock:
            self._connected = False
            self._orderbooks.clear()
            # Phase 2.5: sids are session-scoped — Kalshi assigns
            # fresh ones on reconnect. Stale sids from the prior
            # session would cause `_send_ob_get_snapshot` to send
            # invalid sids → Kalshi error frames → silent failure.
            self._ticker_to_sid.clear()
            # Phase 2.5: error-frame dedup is also session-scoped.
            # A new session may legitimately retry the same
            # command and we want fresh observability.
            self._ws_error_frame_seen.clear()
            # Phase 2.6: outstanding subscribe responses bound to
            # the prior session's command IDs — orphan after
            # reconnect. Clear so the new session's responses are
            # matched correctly. _next_msg_id is NOT reset (the
            # counter staying monotonic helps avoid id collisions
            # across reconnects in case of late-arriving frames).
            self._outstanding_subscribes.clear()
            self._outstanding_subscribe_ts.clear()
            # Phase 2.6 R2 / B1: orphan-sid dedup is also
            # session-scoped — fresh session = clean state.
            self._ws_orphan_sid_seen.clear()
            # Phase 2.6 R4 / A1+A2+A3: reset force-reconnect flag.
            # If we're closing the session, the request has been
            # honored.
            self._force_reconnect_requested = False
            # Phase 2.6 R4 / A8: clear pending late-unsubscribes
            # (subscriptions in flight at reconnect-time will be
            # naturally cleaned up — Kalshi drops the old session's
            # subs on disconnect).
            self._pending_late_unsubscribes.clear()
            # Phase 2.9 R-review A3: reset raw-log counter so
            # the new session gets fresh diagnostic budget.
            self._raw_log_count = 0
            self._raw_log_capped_logged = False
        self._ws = None
        # Reset seq tracking — new WS session starts fresh sids;
        # old state would produce spurious gap warnings.
        self._ws_last_seq.clear()
        self._ws_seq_gap_logs = 0

    @property
    def is_connected(self) -> bool:
        with self._lock:
            return self._connected

    def get_subscribed_count(self) -> int:
        with self._lock:
            return len(self._subscribed_tickers)

    def get_cached_ob_count(self) -> int:
        with self._lock:
            return len(self._orderbooks)

    def get_all_orderbooks(self) -> Dict[str, Dict]:
        """Return a shallow copy of all cached orderbooks (thread-safe).

        Used by DashboardSnapshotBuilder for dashboard visibility only.
        Does NOT affect trading, scanning, or order execution.
        """
        with self._lock:
            return dict(self._orderbooks)

    def get_all_orderbooks_snapshot(self) -> Dict[str, Dict]:
        """Return a DEEP snapshot of all cached orderbooks, safe for
        cross-thread iteration.

        Phase H-3a — the snapshotter thread iterates yes/no level lists
        outside the WS thread. The shallow `get_all_orderbooks()` would
        give back references to lists that the WS thread mutates in place
        under `_apply_fp_delta` (pop/append/setitem on `levels`), causing
        `RuntimeError: list changed size during iteration` or stale-by-one
        reads + non-atomic ts/level pairing. This deep-copy variant takes
        the lock once, copies entire (yes, no, ts) trios atomically, and
        returns objects no other thread can mutate.

        Holding `_lock` across deepcopy is the right trade-off: the deep
        copy of ~30 active 15M tickers × ~5 levels per side is ~300 ints,
        which is sub-millisecond. The WS thread waits at most that long
        on the next delta — much shorter than the 10s polling cadence of
        the only caller.
        """
        import copy as _copy
        with self._lock:
            return _copy.deepcopy(self._orderbooks)

    # ── Auth ───────────────────────────────────────────────────────────────

    def _create_ws_headers(self) -> Dict[str, str]:
        """Create auth headers for Kalshi WS handshake (same RSA-PSS as REST)."""
        timestamp_ms = str(int(time.time() * 1000))
        # WS auth signs: timestamp + "GET" + "/trade-api/ws/v2"
        message = f"{timestamp_ms}GET/trade-api/ws/v2".encode("utf-8")
        sig = self._private_key.sign(
            message,
            padding.PSS(
                mgf=padding.MGF1(hashes.SHA256()),
                salt_length=padding.PSS.DIGEST_LENGTH,
            ),
            hashes.SHA256(),
        )
        signature = base64.b64encode(sig).decode("utf-8")
        return {
            "KALSHI-ACCESS-KEY": self._api_key,
            "KALSHI-ACCESS-TIMESTAMP": timestamp_ms,
            "KALSHI-ACCESS-SIGNATURE": signature,
        }

    # ── Background thread ──────────────────────────────────────────────────

    def _run_thread(self):
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self._stop_event = asyncio.Event()
        try:
            self._loop.run_until_complete(self._ws_loop())
        except Exception:
            logging.error("Kalshi feed thread crashed", exc_info=True)
        finally:
            self._loop.close()

    async def _ws_loop(self):
        backoff = 1.0
        max_backoff = 60.0

        while not self._stop_event.is_set():
            try:
                headers = self._create_ws_headers()
                async with websockets.connect(
                    KALSHI_WS_URL,
                    additional_headers=headers,
                    ping_interval=30,
                    ping_timeout=10,
                ) as ws:
                    self._ws = ws
                    with self._lock:
                        self._connected = True
                        # R2 / P1-5: reset Phase 2 disable state on
                        # every reconnect. Sticky-within-session is
                        # a safety choice (a transient mid-session
                        # blip shouldn't toggle behavior repeatedly).
                        # Sticky-across-reconnect is a bug — fresh
                        # session = fresh sids = let primary path
                        # re-prove the contract. Without this, an
                        # outage that trips disable would degrade
                        # the bot forever.
                        if self._get_snapshot_disabled:
                            logging.info(
                                "WS_GET_SNAPSHOT_RE_ENABLE — fresh "
                                "WS session, re-arming primary "
                                "snapshot path.")
                        self._get_snapshot_disabled = False
                        self._get_snapshot_disabled_logged = False
                        self._get_snapshot_consecutive_failed_sweeps = 0
                        # R3 / P1-A + P1-B: stale Phase 2 dicts/lists
                        # from the prior session must NOT survive a
                        # reconnect. Otherwise:
                        #   - old `_snapshot_request_pending` entries
                        #     time out 5s into new session, falsely
                        #     incrementing the failed-sweep counter
                        #     (until it disables for non-Kalshi-
                        #     contract reasons),
                        #   - old `_pending_snapshot_requests` list
                        #     entries get sent as redundant
                        #     get_snapshots after the natural
                        #     reconnect re-subscribe already produced
                        #     a snapshot,
                        #   - old recovery deadlines fire spurious
                        #     WS_RESUB_STUCK warnings 30s into new
                        #     session.
                        self._snapshot_request_pending.clear()
                        self._pending_snapshot_requests.clear()
                        self._force_resub_recovery_deadline.clear()
                        self._force_resub_recovery_warned.clear()
                    backoff = 1.0
                    # Prime the watchdog timestamps so the grace window starts now.
                    _now = time.time()
                    self._ws_connect_ts = _now
                    self._ws_last_msg_ts = _now
                    logging.info(f"kalshi_ws_connected: url={KALSHI_WS_URL}")

                    # Subscribe to fills channel (all markets)
                    await ws.send(json.dumps({
                        "id": 1,
                        "cmd": "subscribe",
                        "params": {"channels": ["fill"]},
                    }))
                    logging.debug("kalshi_ws_subscribe: channel=fill")

                    # Re-subscribe to any tickers that were active before reconnect
                    with self._lock:
                        resub_tickers = list(self._subscribed_tickers)
                    for ticker in resub_tickers:
                        await self._send_ob_subscribe(ws, ticker)

                    # P0 FIX (2026-04-24 18:00 UTC): drain pending subs on a
                    # timer, independent of incoming messages. Previously subs
                    # only flushed inside the `async for raw in ws:` loop body,
                    # which blocks indefinitely when Kalshi stops delivering
                    # messages (symptom: subscribe_ticker calls accumulate in
                    # _pending_subscribes and never reach ws.send, so Kalshi
                    # never knows to send us snapshots — full chicken-and-egg
                    # deadlock). See kb/failures/ws-subscription-deadlock.md.
                    async def _drain_loop():
                        while not self._stop_event.is_set():
                            try:
                                await asyncio.sleep(2.0)
                                await self._process_pending_subs(ws)
                            except asyncio.CancelledError:
                                raise
                            except Exception:
                                logging.debug(
                                    "pending-sub drain iteration failed",
                                    exc_info=True)
                    _drain_task = asyncio.create_task(_drain_loop())

                    # Silence watchdog (2026-04-24 17:30 UTC 15M outage
                    # defense). Kalshi WS can stay "connected" (ping/pong
                    # healthy internally) while delivering zero protocol
                    # messages for minutes. When no fill/snapshot/delta/ack
                    # arrives for WS_SILENCE_TIMEOUT_SECONDS after the
                    # initial grace window, close the ws so the outer
                    # reconnect kicks in. See
                    # kb/failures/ws-15m-silence-2026-04-24.md.
                    async def _silence_watchdog():
                        while not self._stop_event.is_set():
                            try:
                                await asyncio.sleep(WS_WATCHDOG_CHECK_INTERVAL)
                                # Phase 2.6 R4 / A1+A2+A3: explicit
                                # reconnect request from B2 watchdog
                                # (stuck subscribe). Recovery path
                                # for sid-less stuck tickers — they
                                # cannot be drift-recovered without
                                # a fresh WS session.
                                if self._force_reconnect_requested:
                                    logging.error(
                                        "WS_FORCE_RECONNECT — "
                                        "reconnect requested by B2 "
                                        "stuck-subscribe watchdog. "
                                        "Closing WS for clean "
                                        "re-subscribe of all tickers.")
                                    try:
                                        await ws.close()
                                    except Exception:
                                        pass
                                    return
                                now = time.time()
                                if now - self._ws_connect_ts < WS_SILENCE_GRACE_SECONDS:
                                    continue
                                silent = now - self._ws_last_msg_ts
                                if silent > WS_SILENCE_TIMEOUT_SECONDS:
                                    with self._lock:
                                        _sub_count = len(self._subscribed_tickers)
                                    logging.error(
                                        "WS_SILENCE_WATCHDOG: no msg in %.0fs "
                                        "(timeout=%ds subs=%d) — forcing reconnect",
                                        silent, WS_SILENCE_TIMEOUT_SECONDS,
                                        _sub_count)
                                    try:
                                        await ws.close()
                                    except Exception:
                                        pass
                                    return
                            except asyncio.CancelledError:
                                raise
                            except Exception:
                                logging.debug(
                                    "silence watchdog iteration failed",
                                    exc_info=True)
                    _watchdog_task = asyncio.create_task(_silence_watchdog())

                    try:
                        # Message loop with periodic subscribe/unsubscribe processing
                        async for raw in ws:
                            if self._stop_event.is_set():
                                break
                            self._handle_message(raw)
                            # Process pending subscriptions
                            await self._process_pending_subs(ws)
                    finally:
                        for _bg_task in (_drain_task, _watchdog_task):
                            _bg_task.cancel()
                            try:
                                await _bg_task
                            except (asyncio.CancelledError, Exception):
                                pass

            except asyncio.CancelledError:
                self._cleanup_session_state()
                break
            except Exception as e:
                # R4 / F1: cleanup MUST run before the backoff
                # sleep. Otherwise during the up-to-60s wait,
                # is_connected returns True and stale
                # `_orderbooks` from the prior session is served
                # to scan as live data. Pre-R3 this happened
                # inline here; R3 moved it to `finally:` (which
                # fires AFTER except, i.e., AFTER the sleep) and
                # silently regressed the exact bug R3/P0-A
                # claimed to fix.
                self._cleanup_session_state()
                jitter = backoff * random.uniform(0, 0.25)
                wait = backoff + jitter
                logging.warning(
                    f"kalshi_ws_disconnected: reason={e} reconnect_backoff={wait:.1f}s"
                )
                try:
                    await asyncio.wait_for(
                        self._stop_event.wait(), timeout=wait
                    )
                    break
                except asyncio.TimeoutError:
                    pass
                backoff = min(backoff * 2, max_backoff)
            else:
                # R3 / P0-A + R4 / F1: graceful close path
                # (silence watchdog ws.close(), server-initiated
                # close, async-with normal exit). Without this,
                # stale `_orderbooks` from the prior session
                # would survive into the next iteration and be
                # served as live data for ~ORDERBOOK_CACHE_TTL
                # seconds before fresh snapshots replaced it —
                # the exact drift class Phase 2 was built to
                # prevent.
                self._cleanup_session_state()

        with self._lock:
            self._connected = False
        self._ws = None
        logging.info("Kalshi feed stopped")

    async def _send_ob_subscribe(self, ws, ticker: str):
        # Phase 2.6: unique command id per subscribe so the
        # type=subscribed response can be matched back to this
        # ticker (see _handle_message subscribed branch). Pre-2.6
        # we used static id=2 for all subscribes, making
        # response-matching impossible.
        with self._lock:
            cmd_id = self._next_msg_id
            self._next_msg_id += 1
            self._outstanding_subscribes[cmd_id] = ticker
            self._outstanding_subscribe_ts[cmd_id] = (
                time.monotonic())
        _payload = {
            "id": cmd_id,
            "cmd": "subscribe",
            "params": {
                "channels": ["orderbook_delta"],
                "market_tickers": [ticker],
            },
        }
        # Phase 2.9: raw-out trace BEFORE send so we see what was
        # attempted even if ws.send raises.
        self._log_raw_out(_payload)
        try:
            await ws.send(json.dumps(_payload))
        except Exception:
            # Phase 2.6 R-review A3: send failure leaves the cmd_id
            # registered in _outstanding_subscribes forever — orphan.
            # Pop it so a future retry doesn't get stuck waiting on
            # a response that will never come.
            with self._lock:
                self._outstanding_subscribes.pop(cmd_id, None)
                self._outstanding_subscribe_ts.pop(cmd_id, None)
            logging.warning(
                "kalshi_ws_subscribe send FAILED: ticker=%s id=%s",
                ticker, cmd_id, exc_info=True)
            raise
        logging.debug(
            f"kalshi_ws_subscribe: ticker={ticker} "
            f"id={cmd_id} channel=orderbook_delta")

    async def _send_ob_unsubscribe(self, ws, ticker: str):
        """Phase 2.10: surgical single-ticker removal via
        `update_subscription` with `action: delete_markets`.

        Pre-2.10 we sent `cmd: unsubscribe` with `sids: [sid]`,
        which Kalshi interprets as "cancel the entire
        subscription" — i.e., remove ALL tickers on that channel
        sid (~100 tickers). This was a latent disaster, masked
        only because Phase 2.6's per-ticker sid model didn't
        populate sids for most tickers (we only learned them
        from `type=subscribed`, which fires for the first 2
        subscribes only). After Phase 2.10 binds sids correctly
        from `type=ok`, the OLD unsubscribe schema would nuke
        the channel on every drift firing. Switch to:

            {"cmd": "update_subscription",
             "params": {"sid": <channel_sid>,
                        "action": "delete_markets",
                        "market_tickers": [ticker]}}

        Surgical removal — leaves the rest of the channel intact.

        If sid is unknown (subscribe in flight), we cannot send
        delete_markets without it. Skip; the late-unsub path in
        the ack handler will queue the proper delete_markets
        once the sid lands.
        """
        with self._lock:
            sid = self._ticker_to_sid.get(ticker)
        if sid is None:
            logging.warning(
                "kalshi_ws_unsubscribe SKIPPED: ticker=%s has no "
                "known sid (subscribe in flight?) — skipping",
                ticker)
            return
        with self._lock:
            cmd_id = self._next_msg_id
            self._next_msg_id += 1
        _payload = {
            "id": cmd_id,
            "cmd": "update_subscription",
            "params": {
                "sid": sid,
                "action": "delete_markets",
                "market_tickers": [ticker],
            },
        }
        # Phase 2.9: raw-out trace.
        self._log_raw_out(_payload)
        try:
            await ws.send(json.dumps(_payload))
        except Exception:
            # Send failed — leave sid map intact, caller may retry.
            logging.warning(
                "kalshi_ws_unsubscribe send FAILED: ticker=%s "
                "sid=%s", ticker, sid, exc_info=True)
            raise
        # Phase 2.6 R-review A2 / Phase 2.10: pop the sid mapping
        # AT SEND TIME. Note: with channel-shared sids (Phase 2.10),
        # the sid value itself is still valid (other tickers use
        # it), so popping just clears OUR map — Kalshi keeps the
        # channel subscription alive for the remaining tickers.
        with self._lock:
            cur = self._ticker_to_sid.get(ticker)
            if cur == sid:
                self._ticker_to_sid.pop(ticker, None)
        logging.debug(
            f"kalshi_ws_unsubscribe: ticker={ticker} sid={sid} "
            f"id={cmd_id} (via update_subscription/delete_markets)")

    async def _send_ob_get_snapshot(self, ws, ticker: str):
        """Phase 2.7: request a fresh snapshot WITHOUT bouncing the
        subscription. Per Kalshi WS error code list, get_snapshot
        requires BOTH a subscription ID (sid) AND at least one
        market identifier (market_tickers).

            {"cmd": "update_subscription",
             "params": {"sid": <int>,
                        "action": "get_snapshot",
                        "market_tickers": [<ticker>]}}

        History:
          - Phase 2.5 sent only `market_tickers` → code=7
            "Unknown subscription ID"
          - Phase 2.6 sent only `sid` → code=14
            "Market Ticker required"
          - Phase 2.7 sends BOTH (the actual schema)

        Caller (force_resubscribe) is responsible for ensuring a
        sid is known before queueing this. If the sid disappeared
        between queue and send (unsubscribe race), we pop the
        pending tracker and skip — fallback isn't required since
        the ticker is no longer subscribed.

        If Kalshi rejects/ignores this command,
        _check_snapshot_timeouts falls back to unsub+resub after
        WS_SNAPSHOT_REQUEST_TIMEOUT_S.
        """
        with self._lock:
            sid = self._ticker_to_sid.get(ticker)
        if sid is None:
            # Race: ticker was unsubscribed (or sid never learned)
            # between queue and drain. Clear pending tracker so the
            # timeout doesn't fire spuriously.
            with self._lock:
                self._snapshot_request_pending.pop(ticker, None)
            logging.warning(
                "kalshi_ws_get_snapshot: ticker=%s has no sid in "
                "_ticker_to_sid at send time — skipping", ticker)
            return
        # Phase 2.7 R-review A1: use unique cmd_id per call.
        # Pre-fix all get_snapshot used static id=4, which collapsed
        # all per-ticker errors into a single (id=4, code) tuple in
        # the error-frame dedup at _ws_error_frame_seen — losing
        # per-call visibility.
        with self._lock:
            cmd_id = self._next_msg_id
            self._next_msg_id += 1
        _payload = {
            "id": cmd_id,
            "cmd": "update_subscription",
            "params": {
                "sid": sid,
                "action": "get_snapshot",
                "market_tickers": [ticker],
            },
        }
        # Phase 2.9: raw-out trace.
        self._log_raw_out(_payload)
        await ws.send(json.dumps(_payload))
        logging.info(
            "kalshi_ws_get_snapshot: ticker=%s sid=%d id=%d "
            "(Phase 2.7 cache reset)",
            ticker, sid, cmd_id)

    async def _process_pending_subs(self, ws):
        # Phase 2: check for snapshot-request timeouts FIRST. Any
        # ticker whose get_snapshot didn't yield an orderbook_snapshot
        # within WS_SNAPSHOT_REQUEST_TIMEOUT_S falls back to unsub+resub
        # (which gets queued into _pending_unsubscribes/_pending_subscribes
        # by _check_snapshot_timeouts itself).
        timed_out = self._check_snapshot_timeouts()
        if timed_out:
            logging.warning(
                "WS_SNAPSHOT_TIMEOUT — falling back to unsub+resub "
                "for: %s", ", ".join(timed_out))

        with self._lock:
            subs = list(self._pending_subscribes)
            self._pending_subscribes.clear()
            unsubs = list(self._pending_unsubscribes)
            self._pending_unsubscribes.clear()
            snap_reqs = list(self._pending_snapshot_requests)
            self._pending_snapshot_requests.clear()

        # R1 / A4 [P0] ordering: UNSUBSCRIBES FIRST, THEN subscribes,
        # THEN snapshot requests. The fallback path (timeout → unsub
        # + resub) queues a ticker into BOTH _pending_unsubscribes AND
        # _pending_subscribes; if subs ran first we'd send subscribe
        # before unsubscribe, leaving the ticker permanently
        # unsubscribed. (Pre-fix: subs went first → resub bug.)
        for ticker in unsubs:
            try:
                await self._send_ob_unsubscribe(ws, ticker)
            except Exception:
                logging.debug(f"Failed to unsubscribe from {ticker}", exc_info=True)

        for ticker in subs:
            try:
                await self._send_ob_subscribe(ws, ticker)
            except Exception:
                logging.debug(f"Failed to subscribe to {ticker}", exc_info=True)

        for ticker in snap_reqs:
            try:
                await self._send_ob_get_snapshot(ws, ticker)
            except Exception:
                logging.warning(
                    "Failed to send get_snapshot for %s", ticker,
                    exc_info=True)
                # R3 / P1-C: send-failure must NOT leave the
                # pending tracker set — otherwise the timeout
                # path will count this transport-layer failure
                # as a Kalshi-contract failure, inflating the
                # consecutive-failed-sweeps counter and tripping
                # the disable for non-Kalshi reasons.
                with self._lock:
                    self._snapshot_request_pending.pop(ticker, None)

    def _handle_subscribe_ack(
        self,
        response_id: Optional[int],
        sid_value: Optional[int],
        ack_kind: str,
    ) -> None:
        """Phase 2.10: shared handler for both `type=subscribed`
        (channel-establishment) and `type=ok` (subsequent sub
        acks). Kalshi sends type=subscribed only for the first
        subscribe per channel; everything after is type=ok. Both
        carry the channel sid in the envelope and echo our
        cmd_id, so they're handled identically.

        Args:
            response_id: cmd_id echoed back from Kalshi.
            sid_value: the channel sid (shared across all tickers
                on that channel).
            ack_kind: "WS_SUBSCRIBED" or "WS_OK" — log prefix.
        """
        with self._lock:
            ticker = (
                self._outstanding_subscribes.pop(response_id, None)
                if response_id is not None else None)
            if response_id is not None:
                self._outstanding_subscribe_ts.pop(
                    response_id, None)
            if (ticker is not None and sid_value is not None
                    and ticker in self._subscribed_tickers):
                self._ticker_to_sid[ticker] = sid_value
                _log_ok = (
                    "%s ticker=%s sid=%s id=%s",
                    ack_kind, ticker, sid_value, response_id)
                _log_late = None
            elif (ticker is not None and sid_value is not None
                    and ticker in self._pending_late_unsubscribes):
                # Phase 2.6 R4 / A8: subscribe completed AFTER an
                # unsubscribe_ticker call. Queue an immediate
                # unsubscribe with the now-known sid (drain uses
                # delete_markets per Phase 2.10 to surgically
                # remove just this ticker).
                self._ticker_to_sid[ticker] = sid_value
                self._pending_late_unsubscribes.discard(ticker)
                if ticker not in self._pending_unsubscribes:
                    self._pending_unsubscribes.append(ticker)
                _log_ok = None
                _log_late = (
                    "WS_LATE_UNSUBSCRIBE ticker=%s sid=%s id=%s "
                    "(via %s) — subscribe completed after "
                    "unsubscribe_ticker; queued late unsub.",
                    ticker, sid_value, response_id, ack_kind)
            else:
                _log_ok = None
                _log_late = None
                _log_unmatched = (
                    "kalshi_ws_ack(%s): unmatched id=%s sid=%s "
                    "ticker=%s",
                    ack_kind, response_id, sid_value, ticker)
        if _log_ok is not None:
            logging.info(*_log_ok)
        elif _log_late is not None:
            logging.warning(*_log_late)
        else:
            logging.debug(*_log_unmatched)

    # Phase 2.9 — raw WS frame logging helpers.
    _RAW_LOG_NON_DATA_TYPES = frozenset(
        {"subscribed", "unsubscribed", "ok", "error"})

    def _should_log_raw_in(self, msg_type: Optional[str]) -> bool:
        """True if we should log this incoming frame raw.
        Always log command-response types (low volume, high
        diagnostic value). Otherwise log only within the first
        WS_RAW_LOG_DURATION_S after WS connect (initial burst)."""
        if msg_type in self._RAW_LOG_NON_DATA_TYPES:
            return True
        ts = self._ws_connect_ts
        if ts <= 0.0:
            return False
        return (time.time() - ts) < WS_RAW_LOG_DURATION_S

    def _raw_log_budget_ok(self) -> bool:
        """R-review A3: hard cap on HIGH-VOLUME raw log lines
        (orderbook_delta/snapshot bulk frames) per session.
        Non-data command-response types and outgoing frames are
        EXEMPT — they are bounded in volume and constitute the
        actual diagnostic signal we cannot afford to suppress.

        R2-review A1: pre-fix the cap was applied uniformly,
        meaning chatty deltas could exhaust the budget in ~6s
        and silently suppress the very `type=subscribed` ack
        the diagnostic depends on.
        """
        with self._lock:
            if self._raw_log_count >= WS_RAW_LOG_MAX_PER_SESSION:
                if not self._raw_log_capped_logged:
                    self._raw_log_capped_logged = True
                    _emit = True
                else:
                    _emit = False
            else:
                self._raw_log_count += 1
                _emit = None
        if _emit is True:
            logging.info(
                "WS_RAW_CAPPED — reached %d raw log lines this "
                "session, suppressing further high-volume frames "
                "until next reconnect. Command-response types "
                "(subscribed/unsubscribed/ok/error) and outgoing "
                "frames remain logged (separate exempt budget).",
                WS_RAW_LOG_MAX_PER_SESSION)
        return _emit is None

    def _log_raw_out(self, payload: Dict) -> None:
        """Log outgoing WS frame. Always called and ALWAYS logs —
        outgoing volume is naturally bounded (~100/session,
        primarily subscribe/unsubscribe at startup or reconnect).
        EXEMPT from the high-volume cap so the OUT side of the
        diagnostic is never silenced."""
        try:
            raw = json.dumps(payload)
        except Exception:
            raw = repr(payload)
        if len(raw) > WS_RAW_LOG_TRUNCATE:
            raw = raw[:WS_RAW_LOG_TRUNCATE] + "...[truncated]"
        logging.info("WS_RAW_OUT %s", raw)

    def _log_raw_in(
        self, raw: str, msg_type: Optional[str] = None,
    ) -> None:
        """Log incoming WS frame. Command-response types
        (subscribed, unsubscribed, ok, error) are EXEMPT from the
        cap — they're low-volume and constitute the diagnostic
        signal. Bulk data types (orderbook_delta/snapshot) ARE
        cap-gated to prevent runaway growth."""
        is_command_response = (
            msg_type in self._RAW_LOG_NON_DATA_TYPES)
        if not is_command_response:
            if not self._raw_log_budget_ok():
                return
        if len(raw) > WS_RAW_LOG_TRUNCATE:
            raw = raw[:WS_RAW_LOG_TRUNCATE] + "...[truncated]"
        logging.info("WS_RAW_IN %s", raw)

    def _handle_message(self, raw: str):
        # Watchdog: any message from the server (subscribe ack, heartbeat,
        # fill, orderbook event, even an error we ignore) proves the WS
        # session is healthy. Updated at every incoming frame regardless
        # of dispatch outcome. Paired with _ws_silence_watchdog in
        # _ws_loop which force-reconnects if this stays stale.
        self._ws_last_msg_ts = time.time()
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return

        # Phase 2.9: raw incoming frame log (gated by type +
        # time window). Goes BEFORE the dispatch so we see what
        # was actually received even if downstream parsing throws.
        _msg_type = data.get("type")
        if self._should_log_raw_in(_msg_type):
            self._log_raw_in(raw, msg_type=_msg_type)

        # WS sequence-gap detector. Kalshi WS messages carry (sid, seq) per
        # subscription; seq should be +1 per message within a sid. Any other
        # delta means dropped/reordered/duplicate messages. Diagnostic only —
        # remove after H3 hypothesis (WS message loss) is confirmed/rejected.
        sid = data.get("sid")
        seq = data.get("seq")
        if sid is not None and seq is not None:
            prev = self._ws_last_seq.get(sid)
            if prev is not None and seq != prev + 1:
                if self._ws_seq_gap_logs < self._ws_seq_gap_max_logs:
                    ticker = (data.get("msg") or {}).get("market_ticker", "?")
                    logging.warning(
                        "WS_SEQ_GAP sid=%s ticker=%s type=%s expected_seq=%d "
                        "actual_seq=%d gap=%d",
                        sid, ticker, data.get("type", "?"), prev + 1, seq,
                        seq - prev - 1)
                    self._ws_seq_gap_logs += 1
            self._ws_last_seq[sid] = seq

        msg_type = data.get("type")

        if msg_type == "fill":
            self._handle_fill(data)
        elif msg_type == "orderbook_snapshot":
            self._handle_ob_snapshot(data)
        elif msg_type == "orderbook_delta":
            self._handle_ob_delta(data)
        elif msg_type == "subscribed":
            # Phase 2.6: sid capture from `type=subscribed`. Per
            # Kalshi docs:
            #   {"id": <cmd_id>, "type": "subscribed",
            #    "msg": {"channel": "orderbook_delta", "sid": N}}
            # Empirically (Phase 2.9 raw logs): Kalshi sends
            # type=subscribed only for the FIRST subscribe to a
            # channel per session (channel-establishment). All
            # subsequent subscribes get type=ok instead. The `sid`
            # in both is the channel sid — shared across all
            # tickers on that channel.
            sub_msg = data.get("msg") or {}
            sid_value = (
                sub_msg.get("sid") if isinstance(sub_msg, dict)
                else None)
            self._handle_subscribe_ack(
                response_id=data.get("id"),
                sid_value=sid_value,
                ack_kind="WS_SUBSCRIBED")
        elif msg_type == "ok":
            # Phase 2.10: Kalshi's actual ack for ~99% of subscribes.
            # Empirically (Phase 2.9 raw logs):
            #   {"id": <cmd_id>, "type": "ok", "sid": N, "seq": M,
            #    "msg": {"market_tickers": [<cumulative list>]}}
            # The envelope `sid` is the channel sid; same handling
            # as type=subscribed for cmd_id matching + late-unsub.
            # Pre-2.10 we ignored type=ok → 230 stuck subscribes
            # in 12 min → reconnect loop.
            self._handle_subscribe_ack(
                response_id=data.get("id"),
                sid_value=data.get("sid"),
                ack_kind="WS_OK")
        elif msg_type == "unsubscribed":
            # Phase 2.6 R-review A2: confirmation-only. Sid was
            # already popped from _ticker_to_sid at send time in
            # `_send_ob_unsubscribe`. We don't search-by-value
            # here (fragile) — just log for traceability.
            logging.debug(
                "kalshi_ws_unsubscribed: sid=%s id=%s",
                data.get("sid"), data.get("id"))
        elif msg_type == "error":
            # Phase 2.5: Kalshi error frame. Pre-fix this branch
            # didn't exist; errors were silently dropped. R5 found
            # we'd been getting `invalid_params` rejections of
            # `update_subscription/get_snapshot` for hours without
            # any visibility. Log at WARNING with payload so
            # future command rejections are immediately visible.
            # Greppable prefix: `WS_ERROR_FRAME`.
            #
            # R-review Phase 2.5 P1-2: dedup repeat errors (a
            # broken contract hits us once per ticker per sweep
            # = 4×/5min); cap raw payload to 500 chars to avoid
            # log-spam blowup.
            err_msg = data.get("msg") or {}
            err_code = (
                err_msg.get("code") if isinstance(err_msg, dict)
                else "?")
            err_text = (
                err_msg.get("msg") if isinstance(err_msg, dict)
                else err_msg)
            err_id = data.get("id")
            err_key = (err_id, err_code)
            # Phase 2.6 R-review A4: if this error is a response
            # to one of our subscribes, pop the outstanding
            # entry so we don't leak a cmd_id → ticker mapping
            # forever waiting for a response that will never
            # come. The id field IS the cmd_id we sent.
            if err_id is not None:
                with self._lock:
                    orphan_ticker = (
                        self._outstanding_subscribes.pop(err_id, None))
                    self._outstanding_subscribe_ts.pop(err_id, None)
                if orphan_ticker is not None:
                    logging.warning(
                        "WS_SUBSCRIBE_ERROR ticker=%s id=%s — "
                        "popped orphan outstanding entry",
                        orphan_ticker, err_id)
            if err_key not in self._ws_error_frame_seen:
                self._ws_error_frame_seen.add(err_key)
                raw_str = repr(data)
                if len(raw_str) > 500:
                    raw_str = raw_str[:500] + "...[truncated]"
                logging.warning(
                    "WS_ERROR_FRAME id=%s code=%s msg=%s raw=%s",
                    data.get("id"), err_code, err_text, raw_str)
            else:
                # Suppress repeats; log a debug counter for visibility.
                logging.debug(
                    "WS_ERROR_FRAME (suppressed repeat) id=%s code=%s",
                    data.get("id"), err_code)
        # Other unknown types (subscription_updated, ok, etc.) are
        # quietly ignored — they don't carry data we need today.
        # If we need them later, add explicit branches.

    def _handle_fill(self, data: Dict):
        """Process a fill notification from the WebSocket."""
        try:
            msg = data.get("msg", {})
            fill_info = {
                "order_id": msg.get("order_id"),
                "ticker": msg.get("ticker"),
                "side": msg.get("side"),
                "action": msg.get("action"),
                "count": msg.get("count"),
                "yes_price": msg.get("yes_price"),
                "no_price": msg.get("no_price"),
                "trade_id": msg.get("trade_id"),
                "ts": time.time(),
            }
            with self._lock:
                self._recent_fills.append(fill_info)
        except Exception:
            logging.warning("Failed to parse WS fill message", exc_info=True)

    @staticmethod
    def _normalize_fp_levels(fp_arr) -> List[List[int]]:
        """Convert Kalshi [dollar_str, fp_qty_str] FP format → [int_cents, int_qty].

        Input:  [["0.9600", "54.00"], ["0.9500", "100"]]  (from *_dollars_fp)
        Output: [[96, 54], [95, 100]]                     (internal cents format)

        Tolerates None/[] and skips malformed entries without raising — parsing
        errors at level granularity shouldn't blow away an otherwise-valid
        snapshot.
        """
        if not fp_arr:
            return []
        out: List[List[int]] = []
        for entry in fp_arr:
            if not (isinstance(entry, (list, tuple)) and len(entry) >= 2):
                continue
            try:
                price_cents = int(round(float(entry[0]) * 100))
                qty = int(round(float(entry[1])))
            except (ValueError, TypeError):
                continue
            out.append([price_cents, qty])
        return out

    def _handle_ob_snapshot(self, data: Dict):
        """Replace cached orderbook with full snapshot.

        Contract: Kalshi 2026 sends {market_ticker, yes_dollars_fp, no_dollars_fp}.
        Legacy path (yes/no cents arrays) kept for resilience — fires a loud
        warning so the drift is noticed.
        """
        try:
            msg = data.get("msg", {})
            ticker = msg.get("market_ticker")
            if not ticker:
                return

            # One-shot schema probe (remove after first post-deploy verification).
            if not self._snapshot_schema_probed:
                logging.info(
                    "WS_SCHEMA_PROBE_SNAPSHOT ticker=%s keys=%s",
                    ticker, sorted(msg.keys()))
                self._snapshot_schema_probed = True

            # Preferred: Kalshi 2026 schema (yes_dollars_fp / no_dollars_fp).
            if "yes_dollars_fp" in msg or "no_dollars_fp" in msg:
                yes_levels = self._normalize_fp_levels(msg.get("yes_dollars_fp"))
                no_levels = self._normalize_fp_levels(msg.get("no_dollars_fp"))
            # Legacy: pre-2026 yes/no cents arrays. Fire a loud warning.
            elif "yes" in msg or "no" in msg:
                logging.warning(
                    "WS_SCHEMA_LEGACY_SNAPSHOT ticker=%s: legacy yes/no keys "
                    "(expected yes_dollars_fp/no_dollars_fp)", ticker)
                yes_levels = list(msg.get("yes") or [])
                no_levels = list(msg.get("no") or [])
            elif set(msg.keys()).issubset({"market_id", "market_ticker"}):
                # Empty-book snapshot: Kalshi omits yes_dollars_fp/no_dollars_fp
                # when both sides have zero resting orders. Observed on every
                # new 15M window open (~384 ERRORs/day pre-fix). Initialize
                # empty so subsequent deltas apply against a known zero state.
                yes_levels, no_levels = [], []
            else:
                raise OrderbookSchemaError(
                    f"snapshot {ticker}: no yes_dollars_fp/no_dollars_fp or "
                    f"yes/no keys (got {sorted(msg.keys())})")

            # Phase 2.6: do NOT learn ticker→sid from envelope sid.
            # The Phase 2.5 design read envelope sid as the
            # subscription id, but Kalshi rejected the resulting
            # update_subscription/unsubscribe with code=7 "Unknown
            # subscription ID". Sids are now learned from the
            # `type=subscribed` response in _handle_message
            # (authoritative source per Kalshi docs).
            envelope_sid = data.get("sid")
            with self._lock:
                # R2 / P1-3: drop snapshots for tickers we've
                # already unsubscribed from. Race window: T1
                # subscribed and unsubscribed in the same drain
                # cycle (window settles immediately after add).
                # Kalshi may still send a snapshot in flight from
                # the brief subscribe; without this guard, the
                # snapshot would zombie-write into `_orderbooks`
                # for a ticker the bot considers gone, leaking
                # forever (no path will pop it).
                if ticker not in self._subscribed_tickers:
                    return
                # Phase 2.6 R2 / B1: detect orphan-sid leak. If
                # we have a known sid for this ticker AND the
                # envelope sid differs, it means we previously
                # sent unsubscribe (which popped the local sid),
                # got a NEW subscribe (which captured sid_v2 via
                # type=subscribed), but Kalshi still streams
                # under sid_v1 — a permanent leak we cannot
                # unsubscribe (we don't know sid_v1's value
                # anymore, only sid_v2). Surface this so the
                # Phase 2.7 mitigation can be designed.
                expected_sid = self._ticker_to_sid.get(ticker)
                if (envelope_sid is not None
                        and expected_sid is not None
                        and envelope_sid != expected_sid):
                    leak_key = (ticker, envelope_sid, expected_sid)
                    if leak_key not in self._ws_orphan_sid_seen:
                        self._ws_orphan_sid_seen.add(leak_key)
                        logging.warning(
                            "WS_ORPHAN_SID ticker=%s "
                            "envelope_sid=%s expected_sid=%s — "
                            "Kalshi may be streaming on a leaked "
                            "subscription we cannot unsubscribe. "
                            "Will self-heal on next WS reconnect.",
                            ticker, envelope_sid, expected_sid)
                self._orderbooks[ticker] = {
                    "yes": yes_levels,
                    "no": no_levels,
                    "ts": time.time(),
                }
                # Phase 2: clear pending get_snapshot request for
                # this ticker (if any). Snapshot fulfilled — no need
                # for unsub+resub fallback.
                snapshot_fulfilled = False
                if ticker in self._snapshot_request_pending:
                    del self._snapshot_request_pending[ticker]
                    snapshot_fulfilled = True
                # R1 / A1 + R2 / P0-2: any successful snapshot
                # resets the failed-sweep counter (proves the
                # primary path is healthy). We don't auto-RE-enable
                # `_get_snapshot_disabled` mid-session — that
                # requires a WS reconnect (R2 / P1-5) so a
                # transient mid-session blip doesn't toggle
                # behavior repeatedly.
                if snapshot_fulfilled:
                    self._get_snapshot_consecutive_failed_sweeps = 0
                # R1 / A5: cache repopulated → clear recovery state.
                self._force_resub_recovery_deadline.pop(ticker, None)
                self._force_resub_recovery_warned.pop(ticker, None)
            if snapshot_fulfilled:
                logging.info(
                    "WS_SNAPSHOT_OK %s — fresh snapshot received "
                    "(get_snapshot fulfilled)", ticker)
        except OrderbookSchemaError as e:
            logging.error("WS_SCHEMA_ERROR snapshot: %s", e)
        except Exception:
            logging.warning("Failed to parse WS OB snapshot", exc_info=True)

    def _handle_ob_delta(self, data: Dict):
        """Apply incremental delta to cached orderbook.

        Contract: Kalshi 2026 sends a SINGLE update per delta message:
        {market_ticker, price_dollars, delta_fp, side} where delta_fp is additive
        (positive = qty added, negative = qty removed). Legacy schema grouped
        deltas by side (yes/no arrays) — kept as fallback with warning.
        """
        try:
            msg = data.get("msg", {})
            ticker = msg.get("market_ticker")
            if not ticker:
                return

            if not self._delta_schema_probed:
                logging.info(
                    "WS_SCHEMA_PROBE_DELTA ticker=%s keys=%s",
                    ticker, sorted(msg.keys()))
                self._delta_schema_probed = True

            # Preferred: Kalshi 2026 single-update schema.
            if ("price_dollars" in msg and "delta_fp" in msg
                    and "side" in msg):
                self._apply_fp_delta(ticker, msg)
            # Legacy: pre-2026 side-grouped arrays.
            elif "yes" in msg or "no" in msg:
                logging.warning(
                    "WS_SCHEMA_LEGACY_DELTA ticker=%s: legacy yes/no keys "
                    "(expected price_dollars/delta_fp/side)", ticker)
                self._apply_legacy_delta(ticker, msg)
            else:
                raise OrderbookSchemaError(
                    f"delta {ticker}: no price_dollars/delta_fp/side or "
                    f"yes/no keys (got {sorted(msg.keys())})")

            # Phase 2.6: do NOT learn ticker→sid from envelope sid.
            # See _handle_ob_snapshot for rationale. Sids are now
            # learned from `type=subscribed` responses
            # (authoritative source).
        except OrderbookSchemaError as e:
            logging.error("WS_SCHEMA_ERROR delta: %s", e)
        except Exception:
            logging.warning("Failed to apply WS OB delta", exc_info=True)

    def _apply_fp_delta(self, ticker: str, msg: Dict):
        """Apply Kalshi 2026 single-update delta. Caller holds no lock."""
        side = msg["side"]
        if side not in ("yes", "no"):
            raise OrderbookSchemaError(f"delta {ticker}: unknown side {side!r}")
        try:
            price_cents = int(round(float(msg["price_dollars"]) * 100))
            delta = int(round(float(msg["delta_fp"])))
        except (ValueError, TypeError) as e:
            raise OrderbookSchemaError(
                f"delta {ticker}: unparseable price/delta: {e}")

        with self._lock:
            # R3 / P1-E: zombie-cache guard — drop deltas for
            # tickers we've already unsubscribed from. Without
            # this, an in-flight delta from the prior subscription
            # would create a NEW _orderbooks entry for an
            # unsubscribed ticker (line below: "Delta arrived
            # before snapshot — initialize empty, apply"), leaking
            # zombie state forever. P1-3 closed this for
            # snapshots; deltas have an even larger race window
            # because they arrive constantly.
            if ticker not in self._subscribed_tickers:
                return
            ob = self._orderbooks.get(ticker)
            if ob is None:
                # Delta arrived before snapshot — initialize empty, apply.
                ob = {"yes": [], "no": [], "ts": time.time()}
                self._orderbooks[ticker] = ob

            levels = list(ob.get(side) or [])
            existing_idx = -1
            existing_qty = 0
            for i, lvl in enumerate(levels):
                if self._level_price(lvl) == price_cents:
                    existing_idx = i
                    existing_qty = self._level_qty(lvl)
                    break

            new_qty = existing_qty + delta
            if self._delta_probe_count < self._delta_probe_max:
                logging.info(
                    "WS_DELTA_PROBE #%d %s side=%s price=%d¢ delta_fp=%+d "
                    "existing_qty=%d new_qty=%d n_levels_side=%d",
                    self._delta_probe_count + 1, ticker, side, price_cents,
                    delta, existing_qty, new_qty, len(levels))
                self._delta_probe_count += 1
            if new_qty < 0:
                logging.warning(
                    "WS delta underflow %s %s @%d¢: existing=%d delta=%d "
                    "(clamping to 0)",
                    ticker, side, price_cents, existing_qty, delta)
                new_qty = 0

            if new_qty == 0:
                if existing_idx >= 0:
                    levels.pop(existing_idx)
            elif existing_idx >= 0:
                levels[existing_idx] = [price_cents, new_qty]
            else:
                levels.append([price_cents, new_qty])

            ob[side] = levels
            ob["ts"] = time.time()

    def _apply_legacy_delta(self, ticker: str, msg: Dict):
        """Apply pre-2026 side-grouped delta schema. Fallback only."""
        with self._lock:
            # R3 / P1-E: zombie-cache guard, same as _apply_fp_delta.
            if ticker not in self._subscribed_tickers:
                return
            ob = self._orderbooks.get(ticker)
            if ob is None:
                self._orderbooks[ticker] = {
                    "yes": list(msg.get("yes") or []),
                    "no": list(msg.get("no") or []),
                    "ts": time.time(),
                }
                return
            for side in ("yes", "no"):
                delta_levels = msg.get(side) or []
                if not delta_levels:
                    continue
                existing = {self._level_price(l): l for l in ob.get(side, [])}
                for level in delta_levels:
                    price = self._level_price(level)
                    qty = self._level_qty(level)
                    if qty == 0:
                        existing.pop(price, None)
                    else:
                        existing[price] = level
                ob[side] = list(existing.values())
            ob["ts"] = time.time()

    @staticmethod
    def _level_price(level) -> int:
        if isinstance(level, (list, tuple)) and len(level) >= 1:
            return int(level[0])
        if isinstance(level, dict):
            return int(level.get("price", 0))
        return 0

    @staticmethod
    def _level_qty(level) -> int:
        if isinstance(level, (list, tuple)) and len(level) >= 2:
            return int(level[1])
        if isinstance(level, dict):
            return int(level.get("quantity", 0))
        return 0
