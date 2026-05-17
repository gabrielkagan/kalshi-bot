"""Per-tier WS subscription assignment + subscribe-frame batching — D1.3
(ticket 86b9ypn72, 2026-05-16).

D1.2 (`86b9ypn66`) shipped bronze data plumbing — writer + uploader +
main_loop + BronzeArchiver.run() body — but the WS connects without
sending any subscribe frames, so no data flows. D1.3 is the Bit where
bronze actually populates: the SubscriptionManager assigns tier-tagged
market tickers across N WS connections and builds subscribe-frame
batches that BronzeArchiver dispatches in its ``on_session_start``
callback.

Design (per D0.3 §3 + §6 + the D1.3 pickup-prompt):

- **Per-conn assignment** — N WS connections (default-deploy basis: 7-8
  conns at D0.2's 10K-subs-per-conn TESTED floor; consolidate to 6 conns
  × 15K post-F1 NFL Sunday peak-load soak). Tickers distributed
  round-robin within each tier so a single-conn loss spreads the data-
  density hit evenly across every tier instead of dropping one whole
  tier on the floor.
- **Per-channel subscribe frames** — one frame per (conn, channel,
  batch) tuple. Single-channel-per-frame is REQUIRED so the cmd_id
  echoed back in ``type=subscribed``/``type=ok`` acks resolves to a
  unique channel — multi-channel-per-frame would collapse the
  sid→channel mapping the BronzeArchiver depends on.
- **Batch size** — ``DEFAULT_BATCH_SIZE = 1000`` tickers per frame
  (~30KB JSON envelope at ~30 char/ticker). 1000 keeps the subscribe-
  burst latency bounded at session start while staying well clear of
  the WS frame limit.
- **Byte budget (D1.3-fu2)** — ``DEFAULT_MAX_FRAME_BYTES = 900_000``
  inner cap on the ``json.dumps``'d frame. Kalshi enforces a ~1 MiB
  receive-side limit (the source of the 2026-05-17 1009 close storm
  that D1.3-fu1 addressed on OUR receive side via kalshi_wire's
  ``ws_max_size`` lift); ``build_subscribe_frames`` byte-checks each
  frame before emit so a future schema-driven ticker-name growth (or a
  larger per-conn density) can't push an OUTGOING subscribe past
  Kalshi's cap.

Per D0.2 §F2 (followup): the reconnect handler should recognize Kalshi
WS error code 25 and shed least-active subs before reconnect — that's
captured by the kalshi_wire silence watchdog + reconnect path; D1.3's
planner is stateless w.r.t. error frames.

Channel set (D0.3 §3): ``orderbook_delta`` + ``trade`` +
``market_lifecycle_v2``. ``fill`` is explicitly bot-only (private order
events) and MUST NOT appear in the collector default — the collector has
no order-write API key.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Dict, List, Mapping, Optional, Sequence, Tuple


# D0.3 §3 channel set. ``fill`` is bot-only (private order events) — see
# bot/feeds/kalshi.py:790 for the bot-side subscribe site. Collector
# is read-only and never subscribes to fill.
CHANNELS_DEFAULT: Tuple[str, ...] = (
    "orderbook_delta",
    "trade",
    "market_lifecycle_v2",
)

# D0.3 §3 conn-id letters (A/B/C/D/E/F per spec example). Hard-cap at
# the alphabet length so the planner can't produce duplicate conn_ids
# even if a future operator misconfigures conn_count > 26.
_CONN_ID_LETTERS: str = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"

# Outer count cap for build_subscribe_frames; bot/feeds/kalshi.py sends
# one ticker per subscribe. The collector batches up to this many tickers
# per frame (~30 char/ticker × 1000 = ~30KB JSON envelope — comfortably
# under Kalshi's ~1 MiB WS receive cap). Tunable via
# SubscriptionManager(batch_size=...). The byte-budget guard
# (DEFAULT_MAX_FRAME_BYTES, D1.3-fu2) is the load-bearing protection
# against the receive cap; this count cap bounds burst latency.
DEFAULT_BATCH_SIZE: int = 1000

# D1.3-fu2 (ticket 86b9zjx3x, 2026-05-17). Defense-in-depth byte budget
# applied per outgoing subscribe frame. 900_000 ≈ 150 KB headroom under
# the 1 MiB receive-side limit Kalshi enforces (the same limit D1.3-fu1
# raised on OUR receive side via kalshi_wire's ws_max_size kwarg —
# this is the symmetric protection for OUR send side against Kalshi's
# receive cap).
#
# Today's worst-case at batch_size=1000 + ~42 char/ticker is ~46 KB, well
# under this budget. The guard exists for future-proofing: longer ticker
# names, larger event_ticker prefixes, or per-conn density growth could
# push a count-batched frame past 1 MiB and trigger a Kalshi-side 1009
# close. Pack-by-bytes prevents that class of failure regardless of how
# the count batcher is tuned.
DEFAULT_MAX_FRAME_BYTES: int = 900_000


@dataclass(frozen=True)
class ConnPlan:
    """The planner's per-conn output: which conn gets which tickers on
    which channels.

    Tuple-typed market_tickers + channels for value-equality + hashability
    in test fixtures. Empty market_tickers is legal (operator boot with
    no COLLECTOR_TICKERS_FILE and a transient REST fetch failure, or
    Kalshi legitimately reports zero open markets — the D1.4 refresher
    repopulates on the next interval).
    """
    conn_id: str
    market_tickers: Tuple[str, ...]
    channels: Tuple[str, ...] = field(default_factory=lambda: CHANNELS_DEFAULT)


class SubscriptionManager:
    """Tier-aware per-conn subscribe-frame planner.

    Stateless w.r.t. WS sessions — call ``assign()`` once at boot, then
    ``build_subscribe_frames(plan, cmd_id_start=N)`` per (conn, plan) to
    materialize the actual payloads BronzeArchiver dispatches on
    ``on_session_start``.

    Reconnect-safe: the assignment is deterministic on input, so a
    crashed conn that reconnects gets the same ticker set without any
    cross-conn coordination.
    """

    def __init__(
        self,
        *,
        tickers_by_tier: Mapping[object, Sequence[str]],
        conn_count: int,
        channels: Sequence[str] = CHANNELS_DEFAULT,
        batch_size: int = DEFAULT_BATCH_SIZE,
    ) -> None:
        if conn_count < 1:
            raise ValueError(
                f"conn_count must be ≥ 1 (got {conn_count}). The planner "
                "always emits at least one ConnPlan; an empty deploy is "
                "expressed by passing an empty tickers_by_tier dict."
            )
        if conn_count > len(_CONN_ID_LETTERS):
            raise ValueError(
                f"conn_count={conn_count} exceeds the {len(_CONN_ID_LETTERS)}-"
                "letter conn-id namespace. Extend _CONN_ID_LETTERS or "
                "consolidate conns per D0.2 §5.2 (15K-per-conn target)."
            )
        if batch_size < 1:
            raise ValueError(
                f"batch_size must be ≥ 1 (got {batch_size}). Each subscribe "
                "frame carries at least one market ticker."
            )
        channels_t = tuple(channels)
        if not channels_t:
            raise ValueError(
                "channels must contain at least one channel. Default is "
                f"the D0.3 §3 set: {CHANNELS_DEFAULT}."
            )
        self._tickers_by_tier = dict(tickers_by_tier)
        self._conn_count = conn_count
        self._channels = channels_t
        self._batch_size = batch_size

    def assign(self) -> List[ConnPlan]:
        """Round-robin tickers across N conns, within each tier.

        Tier iteration order is the input dict's iteration order (Python
        3.7+ preserves insertion order). Inside each tier, tickers are
        dealt to conns round-robin (ticker[i] → conn[i % N]). The result
        is N ConnPlan instances with stable conn_ids ``A``, ``B``, … —
        even when ``tickers_by_tier`` is empty (so the operator boot path
        still spins up N WS conns ready to accept subscribes — useful
        for boots that race the first D1.4 REST refresh tick).
        """
        bins: List[List[str]] = [[] for _ in range(self._conn_count)]
        for tier_tickers in self._tickers_by_tier.values():
            for i, ticker in enumerate(tier_tickers):
                bins[i % self._conn_count].append(ticker)
        plans: List[ConnPlan] = []
        for idx, bucket in enumerate(bins):
            plans.append(ConnPlan(
                conn_id=_CONN_ID_LETTERS[idx],
                market_tickers=tuple(bucket),
                channels=tuple(self._channels),
            ))
        return plans

    @staticmethod
    def build_subscribe_frames(
        plan: ConnPlan,
        *,
        cmd_id_start: int,
        batch_size: int = DEFAULT_BATCH_SIZE,
        max_frame_bytes: int = DEFAULT_MAX_FRAME_BYTES,
    ) -> Tuple[List[Dict], Dict[int, str]]:
        """Materialize per-channel subscribe frames + cmd_id→channel map.

        Returns:
            (frames, cmd_id_to_channel) where ``frames`` is the list of
            payloads BronzeArchiver dispatches via ``WSClient.send_frame``
            on session_start, and ``cmd_id_to_channel`` maps every
            frame's cmd_id back to the single channel it subscribed —
            consumed by ``BronzeArchiver._handle_subscribe_ack`` to bind
            sid→channel when Kalshi echoes the cmd_id in
            ``type=subscribed``/``type=ok``.

        Frame schema mirrors ``bot/feeds/kalshi.py::_send_ob_subscribe``
        (single-channel-per-frame is REQUIRED for cmd_id→channel
        resolution to work).

        Batching has TWO caps (D1.3-fu2):
          * ``batch_size`` — outer count cap (default 1000).
          * ``max_frame_bytes`` — inner byte cap on the json.dumps'd
            payload (default 900_000 ≈ 150 KB headroom under the 1 MiB
            WS receive limit Kalshi enforces). Defense-in-depth against
            future ticker-name growth: even if a count-batched frame
            would otherwise cross 1 MiB, the byte-budget check forces a
            split before the append.

        A single ticker whose own payload exceeds ``max_frame_bytes``
        raises ``ValueError`` rather than silently dropping the ticker
        or emitting an oversized frame — the operator needs to see this
        loud + early because it indicates either a Kalshi schema change
        (multi-KB ticker names) or a config typo.
        """
        if batch_size < 1:
            raise ValueError(
                f"batch_size must be ≥ 1 (got {batch_size}); each frame "
                "carries at least one market ticker."
            )
        if max_frame_bytes < 1:
            raise ValueError(
                f"max_frame_bytes must be ≥ 1 (got {max_frame_bytes}); "
                "each subscribe frame carries at least one market ticker "
                "and a non-empty envelope."
            )
        if not plan.market_tickers:
            return [], {}
        frames: List[Dict] = []
        cmd_id_to_channel: Dict[int, str] = {}
        next_id = cmd_id_start
        tickers = list(plan.market_tickers)
        for channel in plan.channels:
            # Greedy byte-aware pack within the outer ``batch_size`` cap.
            # ``current`` is the in-progress ticker list for the next
            # frame on this channel; we measure the would-be frame at
            # each append and finalize-then-start-new on overflow.
            current: List[str] = []
            for ticker in tickers:
                proposed = current + [ticker]
                proposed_frame = {
                    "id": next_id,
                    "cmd": "subscribe",
                    "params": {
                        "channels": [channel],
                        "market_tickers": proposed,
                    },
                }
                proposed_bytes = len(
                    json.dumps(proposed_frame).encode("utf-8")
                )
                # Pathological: a single ticker that ALONE blows the
                # budget. ``not current`` means we're attempting the
                # very first ticker of a new frame; if even that
                # overflows, no smaller frame can hold this ticker so
                # we raise (rather than silently drop).
                if proposed_bytes > max_frame_bytes:
                    if not current:
                        raise ValueError(
                            f"single ticker on channel={channel!r} produces "
                            f"a frame of {proposed_bytes} bytes, exceeding "
                            f"max_frame_bytes={max_frame_bytes}. Either the "
                            "ticker name is anomalously large (Kalshi schema "
                            "change?) or max_frame_bytes is misconfigured."
                        )
                    # Finalize ``current``, start a new frame holding
                    # just this ticker.
                    frames.append({
                        "id": next_id,
                        "cmd": "subscribe",
                        "params": {
                            "channels": [channel],
                            "market_tickers": list(current),
                        },
                    })
                    cmd_id_to_channel[next_id] = channel
                    next_id += 1
                    current = [ticker]
                    continue
                current.append(ticker)
                # Outer count cap: finalize and start fresh when we hit
                # batch_size, identical to the pre-D1.3-fu2 behavior so
                # callers passing only ``batch_size`` see the same shape.
                if len(current) >= batch_size:
                    frames.append({
                        "id": next_id,
                        "cmd": "subscribe",
                        "params": {
                            "channels": [channel],
                            "market_tickers": list(current),
                        },
                    })
                    cmd_id_to_channel[next_id] = channel
                    next_id += 1
                    current = []
            # Flush the trailing partial batch (if any) on this channel.
            if current:
                frames.append({
                    "id": next_id,
                    "cmd": "subscribe",
                    "params": {
                        "channels": [channel],
                        "market_tickers": list(current),
                    },
                })
                cmd_id_to_channel[next_id] = channel
                next_id += 1
        return frames, cmd_id_to_channel
