"""D1.3 — subscription_manager body contract (ticket 86b9ypn72, 2026-05-16).

D1.3 is the Bit where bronze actually flows: ``collector/subscription_manager.py``
gains a planner that assigns tier-tagged market tickers across N WS connections
and assembles subscribe-frame batches respecting Kalshi's WS message-size cap.

What this file pins:

  1. ``SubscriptionManager`` exists with the documented constructor shape.
  2. ``assign()`` returns N ``ConnPlan`` instances (1-per-conn) regardless of
     whether any tickers exist (empty plans for empty-tier inputs).
  3. Round-robin within-tier assignment: tickers from each tier fan out across
     conns so a single conn's drop affects all tiers, not just one. Stable
     ordering so D1.3's first-bronze-flow is deterministic on replay.
  4. ``build_subscribe_frames`` produces ``{"id", "cmd": "subscribe", "params":
     {"channels": [<one>], "market_tickers": [<batch>]}}`` — single-channel per
     frame, mirrors the bot's ``_send_ob_subscribe`` shape (bot/feeds/kalshi.py).
  5. Batch-size cap honored — N tickers split into ceil(N/batch_size) frames
     per channel.
  6. cmd_id allocation is monotone within a single ``build_subscribe_frames``
     call; the returned ``cmd_id_to_channel`` map covers every frame's id.
  7. Empty market_tickers ⇒ zero subscribe frames (defensive: no empty subs).
  8. Default channels are the D0.3 §3 set: ``orderbook_delta`` + ``trade`` +
     ``market_lifecycle_v2``. ``fill`` is intentionally absent — collector
     is read-only and never trades.
"""
from __future__ import annotations

import pytest

from collector.subscription_manager import (
    CHANNELS_DEFAULT,
    DEFAULT_BATCH_SIZE,
    ConnPlan,
    SubscriptionManager,
)


# ─── 1. Constants ────────────────────────────────────────────────────────────


def test_channels_default_is_d0_3_set():
    """D0.3 §3 partition scheme: ``orderbook_delta`` + ``trade`` +
    ``market_lifecycle_v2``. ``fill`` is bot-only (private order events) and
    MUST NOT appear in the collector default — the collector has no private
    key with order-write scope and shouldn't be subscribing to it.
    """
    assert "orderbook_delta" in CHANNELS_DEFAULT
    assert "trade" in CHANNELS_DEFAULT
    assert "market_lifecycle_v2" in CHANNELS_DEFAULT
    assert "fill" not in CHANNELS_DEFAULT, (
        "collector default channels must NOT include `fill` — collector is "
        "read-only, has no order-write API key, and bronze tape is observation "
        "only. The `fill` channel is bot-only via bot/feeds/kalshi.py."
    )


def test_default_batch_size_within_kalshi_ws_envelope_budget():
    """Kalshi WS message-size limit is not publicly documented; the bot
    sends one ticker per subscribe. The collector batches ~1000 tickers per
    frame (~30KB JSON envelope at ~30 char/ticker) — well under any plausible
    WS limit while keeping the subscribe-burst budget bounded.

    If a future Kalshi-side limit shrinks, lower DEFAULT_BATCH_SIZE here and
    the planner's batching shrinks atomically.
    """
    assert 1 <= DEFAULT_BATCH_SIZE <= 5000, (
        f"DEFAULT_BATCH_SIZE={DEFAULT_BATCH_SIZE} outside plausible range "
        f"[1, 5000]. >5000 risks Kalshi WS message-size errors; <1 is invalid."
    )


# ─── 2. SubscriptionManager construction ─────────────────────────────────────


def test_subscription_manager_rejects_zero_conn_count():
    with pytest.raises(ValueError, match="conn_count"):
        SubscriptionManager(
            tickers_by_tier={1: ["X"]}, conn_count=0,
        )


def test_subscription_manager_rejects_negative_batch_size():
    with pytest.raises(ValueError, match="batch_size"):
        SubscriptionManager(
            tickers_by_tier={1: ["X"]}, conn_count=1, batch_size=0,
        )


def test_subscription_manager_rejects_empty_channels():
    with pytest.raises(ValueError, match="channels"):
        SubscriptionManager(
            tickers_by_tier={1: ["X"]}, conn_count=1, channels=(),
        )


# ─── 3. assign() returns one plan per conn ───────────────────────────────────


def test_assign_returns_one_plan_per_conn():
    """conn_count=3 ⇒ exactly 3 ConnPlan instances, each with a unique
    conn_id chosen from a deterministic letter sequence."""
    mgr = SubscriptionManager(
        tickers_by_tier={1: ["A", "B", "C", "D", "E", "F"]},
        conn_count=3,
    )
    plans = mgr.assign()
    assert len(plans) == 3
    conn_ids = [p.conn_id for p in plans]
    assert len(set(conn_ids)) == 3, (
        f"conn_ids must be unique; got {conn_ids}"
    )


def test_assign_conn_ids_are_uppercase_letters_in_order():
    """D0.3 §3 partition uses ``conn=<X>`` where X is A/B/C/D/E/F. The
    planner emits conn_ids in that sequence so partition paths align with
    the spec without per-conn naming clashes.
    """
    mgr = SubscriptionManager(tickers_by_tier={1: ["A"]}, conn_count=4)
    plans = mgr.assign()
    assert [p.conn_id for p in plans] == ["A", "B", "C", "D"]


def test_assign_empty_tickers_still_returns_one_plan_per_conn():
    """Empty tier dict ⇒ N empty plans (not N=0). Operators may boot the
    collector before D1.4's REST snapshot populates the ticker file; the
    WS still connects per conn (auth/handshake healthy) and waits.
    """
    mgr = SubscriptionManager(tickers_by_tier={}, conn_count=2)
    plans = mgr.assign()
    assert len(plans) == 2
    assert plans[0].market_tickers == ()
    assert plans[1].market_tickers == ()


def test_assign_round_robins_tickers_across_conns():
    """6 tickers + 3 conns ⇒ 2 tickers per conn, dealt round-robin so
    a single-conn loss spreads the data-density hit evenly across tier
    rather than concentrating it.
    """
    tickers = ["T1", "T2", "T3", "T4", "T5", "T6"]
    mgr = SubscriptionManager(tickers_by_tier={1: tickers}, conn_count=3)
    plans = mgr.assign()
    # Round-robin: T1→A, T2→B, T3→C, T4→A, T5→B, T6→C.
    assert sorted(plans[0].market_tickers) == ["T1", "T4"]
    assert sorted(plans[1].market_tickers) == ["T2", "T5"]
    assert sorted(plans[2].market_tickers) == ["T3", "T6"]


def test_assign_round_robins_within_each_tier_independently():
    """Tier ordering is preserved in input → round-robin happens per-tier
    so T1 markets get distributed evenly BEFORE T2 etc. (T1 first so a
    partial-cap scenario favors highest-value tier.)
    """
    mgr = SubscriptionManager(
        tickers_by_tier={1: ["A", "B"], 2: ["C", "D"]},
        conn_count=2,
    )
    plans = mgr.assign()
    # T1: A→conn0, B→conn1; T2: C→conn0, D→conn1.
    assert sorted(plans[0].market_tickers) == ["A", "C"]
    assert sorted(plans[1].market_tickers) == ["B", "D"]


def test_assign_is_deterministic_on_repeat_calls():
    """Same inputs ⇒ same outputs. Replay-debugging (and bronze
    reconstruction during a recovery) requires deterministic conn
    assignment so a chunk_id maps stably back to which conn captured it.
    """
    inputs = dict(
        tickers_by_tier={1: ["X", "Y", "Z"], 2: ["P", "Q"]},
        conn_count=2,
    )
    plans_a = SubscriptionManager(**inputs).assign()
    plans_b = SubscriptionManager(**inputs).assign()
    for a, b in zip(plans_a, plans_b):
        assert a.conn_id == b.conn_id
        assert a.market_tickers == b.market_tickers


def test_assign_plan_channels_default_to_d0_3_set():
    """Each ConnPlan carries the channel list it will subscribe to. Default
    is the D0.3 §3 3-channel set unless the manager was constructed with
    a custom channels tuple.
    """
    mgr = SubscriptionManager(tickers_by_tier={1: ["A"]}, conn_count=1)
    plans = mgr.assign()
    assert tuple(plans[0].channels) == tuple(CHANNELS_DEFAULT)


# ─── 4. build_subscribe_frames ───────────────────────────────────────────────


def test_build_subscribe_frames_one_frame_per_channel_within_batch():
    """5 tickers + 3 channels at batch_size=1000 ⇒ 3 subscribe frames
    (one per channel). Each frame has ``cmd: subscribe`` and exactly ONE
    channel in ``params.channels`` so subscribe-ack ``sid`` can be mapped
    back to a specific channel via cmd_id correlation.
    """
    plan = ConnPlan(
        conn_id="A",
        market_tickers=("T1", "T2", "T3", "T4", "T5"),
        channels=("orderbook_delta", "trade", "market_lifecycle_v2"),
    )
    frames, cmd_id_map = SubscriptionManager.build_subscribe_frames(
        plan, cmd_id_start=100, batch_size=1000,
    )
    assert len(frames) == 3
    channels_seen = {f["params"]["channels"][0] for f in frames}
    assert channels_seen == {"orderbook_delta", "trade", "market_lifecycle_v2"}
    for f in frames:
        assert f["cmd"] == "subscribe"
        assert len(f["params"]["channels"]) == 1, (
            "Each subscribe frame must specify exactly ONE channel — multi-"
            "channel-per-frame breaks the cmd_id→channel mapping that the "
            "BronzeArchiver sid-binding logic depends on."
        )
        assert sorted(f["params"]["market_tickers"]) == ["T1", "T2", "T3", "T4", "T5"]


def test_build_subscribe_frames_batches_by_batch_size():
    """2500 tickers + 1 channel at batch_size=1000 ⇒ 3 frames
    (1000 + 1000 + 500) on that channel. cmd_id_map covers all 3 frames.
    """
    tickers = tuple(f"T{i}" for i in range(2500))
    plan = ConnPlan(
        conn_id="A", market_tickers=tickers,
        channels=("orderbook_delta",),
    )
    frames, cmd_id_map = SubscriptionManager.build_subscribe_frames(
        plan, cmd_id_start=1, batch_size=1000,
    )
    assert len(frames) == 3
    batch_sizes = [len(f["params"]["market_tickers"]) for f in frames]
    assert sorted(batch_sizes) == [500, 1000, 1000]
    assert len(cmd_id_map) == 3
    # Every frame's cmd_id must appear in the map keyed to its channel.
    for f in frames:
        cmd_id = f["id"]
        assert cmd_id in cmd_id_map
        assert cmd_id_map[cmd_id] == f["params"]["channels"][0]


def test_build_subscribe_frames_cmd_ids_are_unique_monotonic_starting_from_start():
    plan = ConnPlan(
        conn_id="A", market_tickers=("T1", "T2"),
        channels=("orderbook_delta", "trade"),
    )
    frames, _ = SubscriptionManager.build_subscribe_frames(
        plan, cmd_id_start=5000, batch_size=10,
    )
    ids = [f["id"] for f in frames]
    assert ids == [5000, 5001], (
        f"cmd_ids must be monotone starting at cmd_id_start; got {ids}"
    )


def test_build_subscribe_frames_empty_tickers_returns_zero_frames():
    plan = ConnPlan(
        conn_id="A", market_tickers=(),
        channels=("orderbook_delta", "trade"),
    )
    frames, cmd_id_map = SubscriptionManager.build_subscribe_frames(
        plan, cmd_id_start=1, batch_size=1000,
    )
    assert frames == []
    assert cmd_id_map == {}


def test_build_subscribe_frames_payload_shape_matches_bot_feeds_kalshi():
    """The subscribe-frame schema MUST mirror bot/feeds/kalshi.py's
    ``_send_ob_subscribe``: ``{"id", "cmd": "subscribe", "params":
    {"channels": [<one>], "market_tickers": [...]}}``.

    Drift here (extra fields, renamed keys, nesting changes) would mean
    Kalshi's server interprets the collector's frames differently from the
    bot's — exactly the "two sides of the same coin" symmetry the
    2026-05-16 §5 AMENDMENT was created to defend.
    """
    plan = ConnPlan(
        conn_id="A", market_tickers=("T1",),
        channels=("orderbook_delta",),
    )
    frames, _ = SubscriptionManager.build_subscribe_frames(
        plan, cmd_id_start=1, batch_size=1000,
    )
    f = frames[0]
    assert set(f.keys()) == {"id", "cmd", "params"}
    assert set(f["params"].keys()) == {"channels", "market_tickers"}
    assert f["cmd"] == "subscribe"
    assert f["params"]["channels"] == ["orderbook_delta"]
    assert f["params"]["market_tickers"] == ["T1"]
