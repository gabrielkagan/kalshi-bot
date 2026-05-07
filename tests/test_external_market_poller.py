"""TDD for P7 (R-p7-deploy-r11): external market data poller.

Background: per kb/concepts/calibrator-data-hygiene-apr29.md (Section B in
the deep-dive report), the cal_mlp v3 K=2 train (June 22) needs ~30-50
days of history on three high-signal series we currently don't capture:
  - Binance perpetuals funding rate (free REST, 8h cycle)
  - Binance perpetuals open interest (free REST, ~1m updates)
  - Deribit BTC DVOL index (free REST, ~5m updates)

Starting polling NOW lets v3 train on 50d of data by June 22. Without it,
v3 ships missing the highest-value forward-looking features.

This poller runs as a separate process (NOT bot/_impl.py — keeps blast radius
small) writing to a new `external_market_data` table.
"""
import json
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / 'scripts' / 'external_market_poller.py'


def _import_module():
    """Import the poller module without running its main().

    R-p7-deploy-r11 R6 (HIGH): do NOT re-import on every call — that wipes
    the fixture's per-test `_LAST_SUCCESS_PERSIST_PATH` override and
    causes `state/external_poller_state.json` to leak into the repo.
    Cache the module after first import; subsequent calls return the
    same instance, preserving fixture mutations.
    """
    if str(REPO / 'scripts') not in sys.path:
        sys.path.insert(0, str(REPO / 'scripts'))
    if 'external_market_poller' not in sys.modules:
        import external_market_poller  # noqa: F401  type: ignore
    return sys.modules['external_market_poller']


def _make_db(tmp_path: Path) -> Path:
    db = tmp_path / 'state.db'
    sqlite3.connect(db).close()
    return db


@pytest.fixture(autouse=True)
def _reset_module_state(tmp_path_factory):
    """R-p7-deploy-r11 R4 (LOW) + R5 + R6: module-level state must be saved
    and restored per test or mutation leaks across tests, producing
    ordering-dependent flakes. Covers _LAST_SUCCESS_TS, _PROCESS_START_TS,
    `_stop` flag, AND _LAST_SUCCESS_PERSIST_PATH (R6 H-1 — without
    overriding the path, every test that calls poll_once writes
    `state/external_poller_state.json` to the REPO ROOT, leaks the
    timestamp into git, and races other test runs)."""
    mod = _import_module()
    saved_last = dict(getattr(mod, '_LAST_SUCCESS_TS', {}))
    saved_start = getattr(mod, '_PROCESS_START_TS', None)
    saved_start_mono = getattr(mod, '_PROCESS_START_MONO', None)
    saved_stop = getattr(mod, '_stop', False)
    saved_persist_path = getattr(mod, '_LAST_SUCCESS_PERSIST_PATH', None)
    mod._LAST_SUCCESS_TS = {}
    # Per-test isolated path — destroyed when tmp_path_factory cleans up.
    test_state_dir = tmp_path_factory.mktemp('poller_state')
    mod._LAST_SUCCESS_PERSIST_PATH = str(test_state_dir / 'external_poller_state.json')
    yield
    mod._LAST_SUCCESS_TS = saved_last
    if saved_start is not None:
        mod._PROCESS_START_TS = saved_start
    if saved_start_mono is not None:
        mod._PROCESS_START_MONO = saved_start_mono
    mod._stop = saved_stop
    if saved_persist_path is not None:
        mod._LAST_SUCCESS_PERSIST_PATH = saved_persist_path


def test_module_imports():
    """The poller module must import cleanly with stdlib only."""
    mod = _import_module()
    assert hasattr(mod, 'main')
    assert hasattr(mod, 'init_schema')
    # R2: switched from Binance (geoblocked from US) to OKX.
    assert hasattr(mod, 'poll_okx_funding')
    assert hasattr(mod, 'poll_okx_open_interest')
    assert hasattr(mod, 'poll_deribit_dvol')
    assert hasattr(mod, 'insert_observation')


def test_init_schema_creates_table(tmp_path: Path):
    """init_schema must create external_market_data with the agreed PK."""
    mod = _import_module()
    db = _make_db(tmp_path)
    conn = sqlite3.connect(db)
    mod.init_schema(conn)
    cols = {c[1]: c[2] for c in conn.execute(
        'PRAGMA table_info(external_market_data)'
    ).fetchall()}
    assert 'source' in cols
    assert 'symbol' in cols
    assert 'ts' in cols
    assert 'value' in cols
    # PK should be (source, symbol, ts) so re-polling the same time bucket
    # is idempotent (INSERT OR REPLACE / IGNORE doesn't dupe rows).
    pks = sorted(c[1] for c in conn.execute(
        'PRAGMA table_info(external_market_data)'
    ).fetchall() if c[5] > 0)
    assert pks == ['source', 'symbol', 'ts'], (
        f"PK should be (source, symbol, ts), got {pks}"
    )


def test_insert_observation_idempotent(tmp_path: Path):
    """Re-inserting the same (source, symbol, ts) must not duplicate or crash."""
    mod = _import_module()
    db = _make_db(tmp_path)
    conn = sqlite3.connect(db)
    mod.init_schema(conn)
    now = int(time.time())
    mod.insert_observation(conn, 'binance_funding', 'BTCUSDT', now, 0.00012, '{}')
    mod.insert_observation(conn, 'binance_funding', 'BTCUSDT', now, 0.00012, '{}')
    n = conn.execute(
        "SELECT COUNT(*) FROM external_market_data WHERE source='binance_funding' AND symbol='BTCUSDT'"
    ).fetchone()[0]
    assert n == 1, f"expected 1 row (idempotent insert), got {n}"


def test_poll_okx_funding_parses_correctly():
    """Stub the HTTP layer; verify OKX funding-rate parser."""
    mod = _import_module()
    sample = {
        "code": "0",
        "data": [{
            "instId": "BTC-USDT-SWAP",
            "fundingRate": "0.00012345",
            "ts": "1714386000000",
        }],
        "msg": "",
    }
    with patch.object(mod, '_http_get_json', return_value=sample):
        result = mod.poll_okx_funding(['BTC-USDT-SWAP'])
    assert result == [('okx_funding', 'BTC-USDT-SWAP', pytest.approx(0.00012345), 1714386000000)]


def test_poll_okx_open_interest_parses_correctly():
    """OKX OI endpoint returns {code, data:[{instId, oi, ts}]}."""
    mod = _import_module()
    sample = {
        "code": "0",
        "data": [{
            "instId": "ETH-USDT-SWAP",
            "oi": "150000.5",
            "ts": "1714386000000",
        }],
        "msg": "",
    }
    with patch.object(mod, '_http_get_json', return_value=sample):
        result = mod.poll_okx_open_interest(['ETH-USDT-SWAP'])
    assert result == [('okx_oi', 'ETH-USDT-SWAP', pytest.approx(150000.5), 1714386000000)]


def test_poll_okx_handles_api_error_response():
    """OKX returns code != '0' on errors. The poller should skip
    rather than poison the row with a bogus value."""
    mod = _import_module()
    sample = {"code": "50001", "msg": "rate-limited", "data": []}
    with patch.object(mod, '_http_get_json', return_value=sample):
        result = mod.poll_okx_funding(['BTC-USDT-SWAP'])
    assert result == []


def test_poll_deribit_dvol_parses_correctly():
    """Deribit DVOL endpoint returns {result: {index_price}}.
    Verify parsing extracts index_price as the DVOL value."""
    mod = _import_module()
    sample = {
        "jsonrpc": "2.0",
        "result": {
            "index_price": 56.7,
            "estimated_delivery_price": 56.7,
        },
    }
    with patch.object(mod, '_http_get_json', return_value=sample):
        result = mod.poll_deribit_dvol(['btc_dvol'])
    # Value is the index_price; ts is poll time (within a few seconds of now).
    assert len(result) == 1
    src, sym, val, ts = result[0]
    assert src == 'deribit_dvol'
    assert sym == 'btc_dvol'
    assert val == pytest.approx(56.7)
    assert abs(ts - int(time.time() * 1000)) < 5000  # within 5s


def test_poll_handles_http_error_gracefully():
    """Network errors should produce zero results, not crash. Each polled
    symbol fails independently so a transient error on one doesn't poison
    the whole batch."""
    mod = _import_module()
    with patch.object(mod, '_http_get_json', side_effect=Exception('network down')):
        result = mod.poll_okx_funding(['BTC-USDT-SWAP'])
    assert result == []


def test_cron_mode_persists_last_success_across_runs(tmp_path: Path):
    """R-p7-deploy-r11 R5 (HIGH): without disk persistence, every cron
    --once invocation is a fresh process, _PROCESS_START_TS resets, and
    the grace period blanket-suppresses NEVER alerts forever. Real
    outages of a never-seen source go unalarmed indefinitely.

    Fix: persist _LAST_SUCCESS_TS per source to a JSON file alongside
    the DB. Load on startup. After two cron passes (one success, one
    failure with stale last-success), the staleness alert must fire.
    """
    mod = _import_module()
    db = _make_db(tmp_path)
    conn = sqlite3.connect(db)
    mod.init_schema(conn)
    persist_path = str(tmp_path / 'external_poller_state.json')
    # Module must accept a path override for testability AND persist + load.
    assert hasattr(mod, '_load_last_success_ts'), (
        "external_market_poller must expose _load_last_success_ts to read "
        "persisted last-success timestamps from disk on cron startup."
    )
    assert hasattr(mod, '_persist_last_success_ts'), (
        "external_market_poller must expose _persist_last_success_ts to "
        "save last-success after each successful poll pass."
    )
    # Run 1: simulate prior cron success that wrote disk state.
    import time as _t
    written_state = {
        'okx_funding': _t.time() - 7200,  # 2h ago
        'okx_oi': _t.time() - 7200,       # 2h ago
        'deribit_dvol': _t.time() - 7200,  # 2h ago
    }
    import json
    with open(persist_path, 'w') as f:
        json.dump(written_state, f)
    # Reset module state (fresh cron process).
    mod._LAST_SUCCESS_TS = {}
    mod._PROCESS_START_TS = float(_t.time())
    # Load disk state.
    mod._load_last_success_ts(persist_path)
    # Now _LAST_SUCCESS_TS should reflect 2h-old timestamps.
    assert 'deribit_dvol' in mod._LAST_SUCCESS_TS
    assert mod._LAST_SUCCESS_TS['deribit_dvol'] < _t.time() - 3600


def test_persist_load_roundtrip(tmp_path: Path):
    """R-p7-deploy-r11 R6 (HIGH): the H1 fix's value lives in the
    write→read round-trip. The earlier test only proved load works
    given a hand-written file; it doesn't catch a regression where
    `_persist_last_success_ts` writes the wrong format/key/dtype.

    This test exercises the full round-trip:
      1. Set _LAST_SUCCESS_TS in memory
      2. Call _persist_last_success_ts()
      3. Wipe in-memory state
      4. Call _load_last_success_ts()
      5. Assert restored == original
    """
    mod = _import_module()
    # R7 fix: don't redundantly override _LAST_SUCCESS_PERSIST_PATH — the
    # autouse fixture already pointed it at a per-test tmp dir. Reading it
    # via the module attr keeps the fixture's invariant intact.
    persist_path = mod._LAST_SUCCESS_PERSIST_PATH
    original = {
        'okx_funding': 1714386000.123,
        'okx_oi': 1714386060.456,
        'deribit_dvol': 1714386120.789,
    }
    mod._LAST_SUCCESS_TS = dict(original)
    mod._persist_last_success_ts()
    # Wipe in-memory; the fixture would do this on test exit, but we want
    # a clean state for the load.
    mod._LAST_SUCCESS_TS = {}
    mod._load_last_success_ts()
    assert mod._LAST_SUCCESS_TS == pytest.approx(original)
    # Sanity: the file actually lives at the fixture-controlled path.
    assert Path(persist_path).exists()


def test_persist_load_roundtrip_across_processes(tmp_path: Path):
    """R-p7-deploy-r11 R6/R7 (MED-2): the H1 fix's whole purpose is to
    survive process death (cron --once = fresh process every run).
    Same-process round-trip is a weaker test — a regression that swaps
    `_load_last_success_ts` to read a memoized in-process backup
    instead of the file would still pass.

    This test runs the persist + load in TWO separate Python processes
    via subprocess, with the disk file as the only communication channel.
    """
    persist_path = str(tmp_path / 'cross_proc_state.json')
    repo = REPO

    # Process 1: write
    write_script = (
        "import sys; sys.path.insert(0, %r); "
        "import external_market_poller as m; "
        "m._LAST_SUCCESS_PERSIST_PATH = %r; "
        "m._LAST_SUCCESS_TS = {'okx_funding': 1714386000.123, "
        "'okx_oi': 1714386060.456, 'deribit_dvol': 1714386120.789}; "
        "m._persist_last_success_ts()"
    ) % (str(repo / 'scripts'), persist_path)
    res1 = subprocess.run(
        [sys.executable, '-c', write_script],
        capture_output=True, text=True, timeout=10,
    )
    assert res1.returncode == 0, (
        f"writer process failed: stdout={res1.stdout} stderr={res1.stderr}"
    )
    assert Path(persist_path).exists(), 'writer did not produce the file'

    # Process 2: read (fresh interpreter — proves we're not leaning on
    # any in-memory cache).
    read_script = (
        "import sys, json; sys.path.insert(0, %r); "
        "import external_market_poller as m; "
        "m._LAST_SUCCESS_PERSIST_PATH = %r; "
        "assert m._LAST_SUCCESS_TS == {}, 'fresh process should start empty'; "
        "m._load_last_success_ts(); "
        "print(json.dumps(m._LAST_SUCCESS_TS))"
    ) % (str(repo / 'scripts'), persist_path)
    res2 = subprocess.run(
        [sys.executable, '-c', read_script],
        capture_output=True, text=True, timeout=10,
    )
    assert res2.returncode == 0, (
        f"reader process failed: stdout={res2.stdout} stderr={res2.stderr}"
    )
    import json
    restored = json.loads(res2.stdout.strip().splitlines()[-1])
    assert restored == pytest.approx({
        'okx_funding': 1714386000.123,
        'okx_oi': 1714386060.456,
        'deribit_dvol': 1714386120.789,
    })


def test_persist_handles_oserror_without_exception(tmp_path: Path):
    """R-p7-deploy-r11 R6 (MEDIUM): `_persist_last_success_ts` must
    NOT raise on disk-full / permission-denied / mid-write OSError.
    The next poll pass retries; the bot keeps polling.

    Mock `os.replace` to raise; verify the call returns cleanly,
    no orphan tmp file remains, and _LAST_SUCCESS_TS is unchanged.
    """
    mod = _import_module()
    # R7 fix: rely on fixture's _LAST_SUCCESS_PERSIST_PATH override.
    persist_path = mod._LAST_SUCCESS_PERSIST_PATH
    tmp_path = Path(persist_path).parent
    mod._LAST_SUCCESS_TS = {'okx_funding': 100.0}
    import os as _os
    real_replace = _os.replace
    def boom(*args, **kwargs):
        raise OSError(28, 'no space left on device')
    with patch.object(_os, 'replace', side_effect=boom):
        mod._persist_last_success_ts()  # must not raise
    # Disk file does NOT exist (write was attempted, replace failed,
    # tmp was unlinked).
    assert not Path(persist_path).exists()
    # No orphan tmp.
    leftovers = list(tmp_path.glob(f'{Path(persist_path).name}.tmp-*'))
    assert leftovers == [], f"orphan tmp files: {leftovers}"
    # In-memory state preserved.
    assert mod._LAST_SUCCESS_TS == {'okx_funding': 100.0}


def test_cron_mode_NEVER_alarm_fires_when_disk_state_absent_and_grace_elapsed(tmp_path: Path):
    """R-p7-deploy-r11 R5 (HIGH): grace must be tied to first-seen
    timestamp on disk OR process start, whichever is older. If a fresh
    process starts and disk has no record (new bot deploy), AFTER the
    grace period elapses, NEVER alerts MUST fire — that's the whole
    point of the alert.
    """
    mod = _import_module()
    db = _make_db(tmp_path)
    conn = sqlite3.connect(db)
    mod.init_schema(conn)
    # No disk state. Process started long ago (well past grace).
    import time as _t
    mod._LAST_SUCCESS_TS = {}
    mod._PROCESS_START_TS = _t.time() - 7200  # 2h ago
    mod._PROCESS_START_MONO = _t.monotonic() - 7200  # 2h ago (monotonic)
    # All polls fail — staleness must report all sources as NEVER.
    with patch.object(mod, '_http_get_json', side_effect=Exception('down')):
        import logging as L
        with patch.object(L, 'warning') as mock_warn:
            mod.poll_once(conn)
    # Must have at least one staleness warning containing NEVER. The
    # warning is `logging.warning('per-source staleness: %s', joined)` —
    # NEVER lives in the formatted-args (positional 2), not the format
    # string. Compare the whole call args tuple.
    staleness_with_never = [
        c for c in mock_warn.call_args_list
        if c and c[0] and 'staleness' in str(c[0][0])
        and 'NEVER' in str(c[0])
    ]
    assert len(staleness_with_never) >= 1, (
        f"After grace period elapses with no disk state, NEVER alarm MUST "
        f"fire — otherwise misconfigured sources go silent forever. Got: "
        f"{mock_warn.call_args_list}"
    )


def test_cron_mode_does_not_warn_NEVER_within_grace_period(tmp_path: Path):
    """R-p7-deploy-r11 R4 (HIGH): in --once cron mode, _LAST_SUCCESS_TS
    starts EMPTY because it's module-level state in a fresh process.
    Without a grace period, the first cron run that hits a transient
    error reports every source as 'NEVER' stale and emits a WARNING.
    Cron runs every 5-60 min → false alarm spam.

    The poller must require process-start uptime >= STALE_THRESHOLD
    seconds before reporting 'NEVER'. Within the grace period, log
    a debug-level note instead.
    """
    mod = _import_module()
    db = _make_db(tmp_path)
    conn = sqlite3.connect(db)
    mod.init_schema(conn)
    # Reset module state to simulate fresh cron process.
    mod._LAST_SUCCESS_TS = {}
    mod._PROCESS_START_TS = float(__import__('time').time())  # NOW = process start
    # All polls fail; this would otherwise spew NEVER alarms.
    with patch.object(mod, '_http_get_json', side_effect=Exception('down')):
        import logging as L
        with patch.object(L, 'warning') as mock_warn:
            mod.poll_once(conn)
    # Warnings about per-source staleness must NOT fire because we're
    # within the grace period.
    staleness_calls = [
        c for c in mock_warn.call_args_list
        if c and c[0] and 'staleness' in str(c[0][0])
    ]
    assert not staleness_calls, (
        f"Cron mode within grace period must NOT emit per-source staleness "
        f"warning. Got: {mock_warn.call_args_list}"
    )


def test_per_source_staleness_warning_when_no_obs(tmp_path: Path):
    """R-p7-deploy-r11 R3-M3: when a source returns no observations
    for >15 minutes, the poller must surface a WARNING-level
    staleness summary so cron-log inspection catches sustained
    outages (e.g., Deribit weekly maintenance) without scraping
    individual per-symbol WARNING lines.

    Force _LAST_SUCCESS_TS for a source to a stale value, run a poll
    pass with all sources erroring, and verify the staleness summary
    fires.
    """
    mod = _import_module()
    db = _make_db(tmp_path)
    conn = sqlite3.connect(db)
    mod.init_schema(conn)
    # Pre-populate _LAST_SUCCESS_TS so deribit_dvol looks 1h stale.
    import time as _t
    mod._LAST_SUCCESS_TS = {
        'okx_funding': _t.time(),       # fresh
        'okx_oi': _t.time(),            # fresh
        'deribit_dvol': _t.time() - 3600,  # 1h stale
    }
    # R4: ensure the process-start grace period has elapsed so the
    # staleness check actually fires. R5 (LOW): grace check now uses
    # monotonic clock — set both wall + mono.
    mod._PROCESS_START_TS = _t.time() - 7200  # 2h ago, well past grace
    mod._PROCESS_START_MONO = _t.monotonic() - 7200
    # Mock all polls to fail (so this pass produces no obs and doesn't
    # update _LAST_SUCCESS_TS).
    with patch.object(mod, '_http_get_json', side_effect=Exception('down')):
        import logging as L
        with patch.object(L, 'warning') as mock_warn:
            mod.poll_once(conn)
    # Assert one of the warning calls mentions per-source staleness.
    staleness_calls = [
        c for c in mock_warn.call_args_list
        if c and c[0] and 'staleness' in str(c[0][0])
    ]
    assert len(staleness_calls) >= 1, (
        f"Expected per-source staleness warning to fire when deribit_dvol is "
        f"3600s stale; got warnings: {mock_warn.call_args_list}"
    )


def test_main_one_pass_writes_observations(tmp_path: Path):
    """End-to-end: main(--once) executes one poll pass and writes rows
    to the DB. Mock the HTTP layer; verify rows land."""
    mod = _import_module()
    db = _make_db(tmp_path)

    funding_resp = {
        "code": "0",
        "data": [{
            "instId": "BTC-USDT-SWAP",
            "fundingRate": "0.0001",
            "ts": "1714386000000",
        }],
    }
    oi_resp = {
        "code": "0",
        "data": [{
            "instId": "BTC-USDT-SWAP",
            "oi": "100000.0",
            "ts": "1714386000000",
        }],
    }
    dvol_resp = {
        "jsonrpc": "2.0", "result": {"index_price": 50.0},
    }

    def mock_http(url, *_a, **_kw):
        if 'funding-rate' in url:
            return funding_resp
        if 'open-interest' in url:
            return oi_resp
        if 'get_index_price' in url:
            return dvol_resp
        raise ValueError(f'unexpected url: {url}')

    with patch.object(mod, '_http_get_json', side_effect=mock_http):
        rc = mod.main(['--db', str(db), '--once'])
    assert rc == 0
    conn = sqlite3.connect(db)
    rows = conn.execute(
        "SELECT source, symbol FROM external_market_data ORDER BY source, symbol"
    ).fetchall()
    sources = {r[0] for r in rows}
    assert 'okx_funding' in sources
    assert 'okx_oi' in sources
    assert 'deribit_dvol' in sources
