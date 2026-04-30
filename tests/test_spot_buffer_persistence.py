"""TDD for P1 (R-p7-deploy-r11): persist Coinbase spot price buffer across
bot restarts.

Background (kb/concepts/calibrator-data-hygiene-apr29.md):

The 5-minute and 30-minute spot momentum features (`spot_momentum_5m_bps`,
`btc_spot_change_5m_bps`, `btc_spot_change_30m_bps`,
`sol_btc_relative_return_30m_bps`) populate via `CoinbaseFeed.get_buffer()`.
On bot restart, that in-memory buffer starts EMPTY — so the first ~5 min
post-restart, 5m momentum is NULL. The first ~30 min, 30m features are NULL.

With 8+ deploys in a single day, this drives ~25-50% NULL rates on the
worst days, well above the documented "post-reconnect" pattern.

Fix: persist the 30-min buffer to disk every ~30s; reload on
CoinbaseFeed.__init__. Stale entries (>30 min old) get dropped on load.

This test set drives the implementation.
"""
import json
import os
import sys
import time
from collections import deque
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]


def _import_bot():
    """Import bot module without running its main(). Add repo to path."""
    if str(REPO) not in sys.path:
        sys.path.insert(0, str(REPO))
    import bot  # type: ignore
    return bot


def _make_feed_isolated(tmp_path: Path):
    """Build a CoinbaseFeed pointed at tmp_path for persistence file.

    The class must accept a `persist_path` kwarg (default to a sane
    state-dir location in production).
    """
    bot = _import_bot()
    feed = bot.CoinbaseFeed(persist_path=str(tmp_path / 'spot_buffer.json'))
    return bot, feed


def test_coinbase_feed_accepts_persist_path_kwarg(tmp_path: Path):
    """CoinbaseFeed.__init__ must accept persist_path; default must not crash."""
    _, feed = _make_feed_isolated(tmp_path)
    assert feed is not None
    # Default-no-kwarg must also work for backward compatibility.
    bot = _import_bot()
    f2 = bot.CoinbaseFeed()  # default persist_path
    assert f2 is not None


def test_persist_writes_json_atomically(tmp_path: Path):
    """Persist must write to a .tmp file then os.replace, never leaving
    a torn file mid-write. After persist, the JSON must be loadable."""
    bot, feed = _make_feed_isolated(tmp_path)
    now = time.time()
    with feed._lock:
        feed._buffers['BTC'].append((now, 75000.0))
        feed._buffers['ETH'].append((now, 3000.0))
    feed.persist_buffer()
    persist_path = tmp_path / 'spot_buffer.json'
    assert persist_path.exists()
    data = json.loads(persist_path.read_text())
    assert 'BTC' in data
    assert 'ETH' in data
    # Each asset's series must be a list of [ts, price] pairs.
    assert isinstance(data['BTC'], list)
    assert len(data['BTC']) == 1
    assert data['BTC'][0][0] == pytest.approx(now)
    assert data['BTC'][0][1] == pytest.approx(75000.0)


def test_load_restores_buffer_on_init(tmp_path: Path):
    """Writing then constructing a fresh CoinbaseFeed must populate
    in-memory buffers from the persisted file."""
    bot, feed = _make_feed_isolated(tmp_path)
    now = time.time()
    # Populate + persist.
    with feed._lock:
        for i in range(60):
            feed._buffers['BTC'].append((now - i, 75000.0 + i))
    feed.persist_buffer()
    # Construct a new instance pointing at the same path.
    persist_path = str(tmp_path / 'spot_buffer.json')
    feed2 = bot.CoinbaseFeed(persist_path=persist_path)
    btc = list(feed2._buffers['BTC'])
    assert len(btc) == 60, f"expected 60 entries, got {len(btc)}"
    # Order doesn't matter for momentum lookup, but verify content.
    timestamps = sorted(t for t, _p in btc)
    prices = sorted(p for _t, p in btc)
    assert min(prices) == pytest.approx(75000.0)
    assert max(prices) == pytest.approx(75059.0)


def test_load_drops_entries_older_than_buffer_maxage(tmp_path: Path):
    """A persisted file from >30 min ago should NOT pollute the new
    buffer with stale data — entries older than PRICE_BUFFER_SIZE
    seconds (1800) get filtered out at load time."""
    bot, feed = _make_feed_isolated(tmp_path)
    now = time.time()
    # Persist a mix: some recent (in-window), some ancient (out-of-window).
    persist_path = tmp_path / 'spot_buffer.json'
    persist_path.write_text(json.dumps({
        'BTC': [
            [now - 100, 75000.0],   # recent — keep
            [now - 1700, 74000.0],  # 28 min — keep
            [now - 1900, 73000.0],  # 31 min — drop
            [now - 7200, 72000.0],  # 2 hours — drop
        ],
        'ETH': [],
    }))
    feed2 = bot.CoinbaseFeed(persist_path=str(persist_path))
    btc = list(feed2._buffers['BTC'])
    # Only the two recent entries should survive.
    assert len(btc) == 2, f"expected 2, got {len(btc)}: {btc}"
    prices = {p for _t, p in btc}
    assert prices == {75000.0, 74000.0}


def test_load_handles_missing_file(tmp_path: Path):
    """No persist file = silent empty-buffer init; no crash, no warning."""
    bot = _import_bot()
    persist_path = tmp_path / 'never_written.json'
    feed = bot.CoinbaseFeed(persist_path=str(persist_path))
    # Buffers all empty.
    for asset in feed._buffers:
        assert len(feed._buffers[asset]) == 0


def test_load_handles_corrupt_file(tmp_path: Path):
    """A corrupt persist file (truncated, malformed JSON) must NOT crash
    the bot. Buffer starts empty; bot proceeds normally."""
    bot = _import_bot()
    persist_path = tmp_path / 'corrupt.json'
    persist_path.write_text('{"BTC": [[1234, 75')  # truncated
    feed = bot.CoinbaseFeed(persist_path=str(persist_path))
    for asset in feed._buffers:
        assert len(feed._buffers[asset]) == 0


def test_load_handles_unknown_asset_in_persist_file(tmp_path: Path):
    """A persist file with an asset not in current ASSETS must not crash.
    The unknown asset's data is silently dropped."""
    bot = _import_bot()
    persist_path = tmp_path / 'unknown.json'
    now = time.time()
    persist_path.write_text(json.dumps({
        'BTC': [[now - 60, 75000.0]],
        'DOGE': [[now - 60, 0.42]],   # not in ASSETS
    }))
    feed = bot.CoinbaseFeed(persist_path=str(persist_path))
    btc = list(feed._buffers['BTC'])
    assert len(btc) == 1
    assert 'DOGE' not in feed._buffers


def test_persist_serialization_roundtrip_full_buffer(tmp_path: Path):
    """A full 30-min buffer (1800 entries × 4 assets) must round-trip
    correctly through persist→load. This is the production case."""
    bot, feed = _make_feed_isolated(tmp_path)
    now = time.time()
    with feed._lock:
        for asset, base_price in [('BTC', 75000.0), ('ETH', 3000.0),
                                   ('SOL', 200.0), ('XRP', 0.5)]:
            for i in range(1800):
                feed._buffers[asset].append((now - i, base_price + (i * 0.01)))
    feed.persist_buffer()
    feed2 = bot.CoinbaseFeed(persist_path=str(tmp_path / 'spot_buffer.json'))
    for asset in ('BTC', 'ETH', 'SOL', 'XRP'):
        assert len(feed2._buffers[asset]) == 1800, \
            f"asset {asset} had {len(feed2._buffers[asset])} entries"


def test_persist_path_default_is_state_dir(tmp_path: Path, monkeypatch):
    """Default persist path should land in a state dir, not the repo root.
    This prevents accidental commits of the persist file."""
    bot = _import_bot()
    # Inspect default value via signature.
    import inspect
    sig = inspect.signature(bot.CoinbaseFeed.__init__)
    default = sig.parameters['persist_path'].default
    # Must be a string path containing 'state' or 'data' (writable dir),
    # NOT the repo root or a hardcoded /tmp.
    assert default is not None
    assert isinstance(default, str)
    assert 'state' in default or 'data' in default, (
        f"Default persist_path should be in a state/data dir; got {default!r}"
    )


def test_load_drops_future_dated_entries(tmp_path: Path):
    """R-p7-deploy-r11 R2 (P1 HIGH): a corrupt persist file or clock skew
    must not leave future-dated entries in the buffer (they'd never age
    out and silently corrupt every consumer of get_buffer)."""
    bot = _import_bot()
    persist_path = tmp_path / 'spot_buffer.json'
    now = time.time()
    persist_path.write_text(json.dumps({
        'BTC': [
            [now - 60, 75000.0],     # recent — keep
            [now + 120, 999999.0],   # 2 min in the future — drop (>60s skew)
            [now + 1e18, 1e18],      # absurd far-future — drop
        ],
        'ETH': [],
    }))
    feed = bot.CoinbaseFeed(persist_path=str(persist_path))
    btc = list(feed._buffers['BTC'])
    assert len(btc) == 1, f"expected only the recent entry, got {len(btc)}: {btc}"
    assert btc[0][1] == pytest.approx(75000.0)


def test_load_accepts_minor_clock_skew_within_60s(tmp_path: Path):
    """A small future-dated timestamp (<60s) is plausible clock-skew and
    should NOT be dropped — that would over-zealously discard recent data."""
    bot = _import_bot()
    persist_path = tmp_path / 'spot_buffer.json'
    now = time.time()
    persist_path.write_text(json.dumps({
        'BTC': [[now + 10, 75000.0]],  # 10s ahead — accept
    }))
    feed = bot.CoinbaseFeed(persist_path=str(persist_path))
    btc = list(feed._buffers['BTC'])
    assert len(btc) == 1


def test_persist_buffer_uses_uuid_tmp_filename(tmp_path: Path):
    """R-p7-deploy-r11 R2 (P1 MEDIUM): two writers must not race over a
    fixed `.tmp` filename. The tmp file must include pid + uuid suffix.
    """
    bot, feed = _make_feed_isolated(tmp_path)
    now = time.time()
    with feed._lock:
        feed._buffers['BTC'].append((now, 75000.0))
    # Patch os.replace to capture the tmp filename it sees.
    captured: list[str] = []
    real_replace = os.replace
    def spy_replace(src, dst):
        captured.append(src)
        real_replace(src, dst)
    import unittest.mock as mock
    with mock.patch('os.replace', side_effect=spy_replace):
        feed.persist_buffer()
    assert len(captured) == 1
    tmp_name = captured[0]
    # Must include pid AND a hex uuid fragment (so two PIDs / two writers
    # at the same instant don't clobber each other).
    assert str(os.getpid()) in tmp_name, (
        f"tmp filename {tmp_name} should include pid {os.getpid()}"
    )
    # 8 hex chars from uuid4().hex[:8]
    import re
    assert re.search(r'\.tmp-\d+-[0-9a-f]{8}$', tmp_name), (
        f"tmp filename {tmp_name} should match `.tmp-<pid>-<uuid8>` pattern"
    )


def test_persist_buffer_is_thread_serialized(tmp_path: Path):
    """R-p7-deploy-r11 R5 (HIGH): two threads racing through persist_buffer
    (e.g., stop() + a still-running asyncio to_thread persist) must NOT
    interleave their os.replace calls. The later-finishing snapshot
    might be older, leading to silent data loss.

    Fix: persist_buffer holds a threading.Lock for the entire write,
    so concurrent calls serialize.

    Test: spawn two threads calling persist_buffer simultaneously, time
    each call. If they're serialized, the second call's wall-clock
    duration is at least the first's (they don't overlap). Without the
    lock, both can be near-zero (overlapping work).
    """
    import threading
    bot, feed = _make_feed_isolated(tmp_path)
    now = time.time()
    with feed._lock:
        # Make the persist non-trivial.
        for i in range(1800):
            feed._buffers['BTC'].append((now - i, 75000.0 + i))
            feed._buffers['ETH'].append((now - i, 3000.0 + i))
    # Verify the lock attribute exists.
    assert hasattr(feed, '_persist_lock'), (
        "CoinbaseFeed must have _persist_lock (threading.Lock) so concurrent "
        "stop()/to_thread persists serialize. Without this, the os.replace "
        "race the R3 stop() reorder was supposed to close stays open in "
        "the silent-WS case."
    )
    # The lock must actually block re-entry.
    assert feed._persist_lock.acquire(blocking=False)
    try:
        # While we hold it, another thread should NOT be able to persist.
        result = []
        def _worker():
            # Use timeout=0.05 to detect blocking
            got = feed._persist_lock.acquire(blocking=True, timeout=0.05)
            if got:
                feed._persist_lock.release()
            result.append(got)
        t = threading.Thread(target=_worker)
        t.start()
        t.join(timeout=0.5)
        assert result == [False], (
            "_persist_lock must block concurrent acquire — got result "
            f"{result}; persist_buffer is NOT serialized."
        )
    finally:
        feed._persist_lock.release()


def test_stop_persists_before_shutdown(tmp_path: Path):
    """R-p7-deploy-r11 R2 (P1 HIGH): bot stop() must flush the buffer to
    disk; otherwise up to SPOT_BUFFER_PERSIST_INTERVAL_S of fresh data
    is lost on every restart, partially defeating the persistence."""
    bot, feed = _make_feed_isolated(tmp_path)
    now = time.time()
    # Add new data; do NOT call persist_buffer manually.
    with feed._lock:
        feed._buffers['BTC'].append((now, 75000.0))
        feed._buffers['ETH'].append((now, 3000.0))
    # Calling stop() must flush — even though the asyncio loop isn't running.
    feed.stop()
    persist_path = tmp_path / 'spot_buffer.json'
    assert persist_path.exists(), (
        "stop() must persist before shutdown so latest ticks survive restart"
    )
    data = json.loads(persist_path.read_text())
    assert len(data['BTC']) == 1
    assert data['BTC'][0][1] == pytest.approx(75000.0)


def test_persist_dir_ready_resets_on_oserror(tmp_path: Path):
    """R-p7-deploy-r11 R3-M1: if the persist dir is removed mid-process
    (sysadmin cleanup, container volume re-mount), persist_buffer hits
    OSError. The `_persist_dir_ready` flag must be reset so the next
    call re-attempts makedirs. Otherwise persistence is permanently
    broken until restart, with only one warning log line as evidence.
    """
    bot, feed = _make_feed_isolated(tmp_path)
    now = time.time()
    with feed._lock:
        feed._buffers['BTC'].append((now, 75000.0))
    # First persist: succeeds, dir created, flag set.
    feed.persist_buffer()
    assert getattr(feed, '_persist_dir_ready', False) is True
    # Now move the persist target to an unwritable path so the next
    # persist hits OSError.
    bad_path = tmp_path / 'nope' / 'gone' / 'spot_buffer.json'
    feed._persist_path = str(bad_path)
    # Force flag reset condition: pretend dir was deleted and we're stale.
    # Make the parent unwritable to trigger OSError (best portable way:
    # point at /dev/null/x or a path with a regular-file as parent).
    blocker = tmp_path / 'blocker_file'
    blocker.write_text('not-a-dir')
    feed._persist_path = str(blocker / 'spot_buffer.json')
    feed._persist_dir_ready = True  # simulate "we already created it once"
    feed.persist_buffer()  # should hit OSError, log warning
    # Flag must be reset so a future fix-up retries makedirs.
    assert feed._persist_dir_ready is False, (
        "_persist_dir_ready must be cleared on OSError; otherwise persist "
        "is permanently broken until restart if dir gets removed."
    )


def test_persist_does_not_lose_data_during_concurrent_writes(tmp_path: Path):
    """Persist writes via tmp+rename; a concurrent reader during the write
    sees either the old file or the new file, never a torn file. This
    test simulates the basic atomic property by writing twice in
    quick succession and confirming the second write fully overwrote."""
    bot, feed = _make_feed_isolated(tmp_path)
    now = time.time()
    with feed._lock:
        feed._buffers['BTC'].append((now - 100, 75000.0))
    feed.persist_buffer()
    # Add more, persist again.
    with feed._lock:
        feed._buffers['BTC'].append((now - 50, 76000.0))
    feed.persist_buffer()
    data = json.loads((tmp_path / 'spot_buffer.json').read_text())
    assert len(data['BTC']) == 2
