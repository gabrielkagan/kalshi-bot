"""Data-Integrity E.1 — monitor_watchdog contract pins (ticket 86ba0xq51, 2026-05-19).

NEW `scripts/ops/monitor_watchdog.py` standalone CLI runs via cron on the
VPS every 10 min. Stats each cron monitor's log file mtime, alerts on stale
via the existing `bot.notifier.TelegramNotifier` (no re-implementation of
the Telegram client).

Closes the silent-monitor-death class:
- data_health DEAD 2026-05-17→19 (A.1 fixed root cause; E.1 prevents class)
- collector_health DEAD same window (separate fix in 86ba0jvka)
- quiet_market LIKELY DEAD (open ticket 86ba0k557)

This file pins the public-API shape + default thresholds + dedup-key
convention + production-runtime smoke (no TypeError when invoked without
test-seam notifier). Drift would silently reopen the silent-monitor-death
class.
"""
from __future__ import annotations

import inspect
import os
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest


def _import_watchdog():
    """Import the watchdog module; add scripts/ops to sys.path on demand."""
    repo = Path(__file__).resolve().parents[2]
    ops_dir = repo / "scripts" / "ops"
    if str(ops_dir) not in sys.path:
        sys.path.insert(0, str(ops_dir))
    import monitor_watchdog
    return monitor_watchdog


def test_watched_monitor_dataclass_shape():
    """`WatchedMonitor` is a frozen dataclass with (name, log_path, max_stale_minutes)."""
    mod = _import_watchdog()
    fields = {f.name for f in mod.dataclasses.fields(mod.WatchedMonitor)}
    assert fields == {"name", "log_path", "max_stale_minutes"}, (
        f"WatchedMonitor fields should be (name, log_path, max_stale_minutes); "
        f"got {fields}"
    )
    # Frozen = configuration is immutable post-creation.
    wm = mod.WatchedMonitor("foo", "/tmp/foo.log", 60)
    with pytest.raises(Exception):  # dataclasses.FrozenInstanceError on Py3.9+
        wm.name = "bar"  # type: ignore[misc]


def test_default_config_covers_verified_cron_monitors():
    """`WATCHED_MONITORS` includes exactly the 4 verified cron monitors that
    use `>> <log_path> 2>&1` redirection (data_health, quiet_market,
    collector_health, phantom_reconcile) — and nothing else.

    Each must correspond to a real cron line on the VPS; adding entries for
    non-existent cron lines would Telegram-storm MISSING alerts on every
    tick (R1-M2 from the adv-review chain — researcher/analyst/auditor are
    bot-runtime classes, not cron scripts; watchdog's cron line has no log
    redirect). The verified set is the only safe watchlist until a future
    Bit promotes one of the excluded monitors with an actual cron-line
    redirect.
    """
    mod = _import_watchdog()
    names = {m.name for m in mod.WATCHED_MONITORS}
    expected = {
        "data_health",        # A.1 root cause; cron */30, >> /tmp/data_health.log
        "quiet_market",       # 86ba0k557; cron */15, >> /tmp/quiet_market.log
        "collector_health",   # 86ba0jvka root cause; cron */5, >> ~/collector_health.log
        "phantom_reconcile",  # Stage B precondition; cron 7 *, >> ~/phantom_reconcile.log
    }
    assert names == expected, (
        f"WATCHED_MONITORS must equal {expected}; got {names}. "
        f"R1-M2: any entry without a real cron-line redirect on the VPS "
        f"will produce false-positive MISSING alerts on every cron tick. "
        f"Promoting researcher/analyst/auditor/watchdog requires bundling "
        f"a cron-line edit in the same Bit."
    )


def test_check_log_freshness_fresh_returns_none(tmp_path: Path) -> None:
    """File mtime within window → None (no alert)."""
    mod = _import_watchdog()
    fresh = tmp_path / "fresh.log"
    fresh.write_text("just-now\n")
    monitor = mod.WatchedMonitor("fresh_test", str(fresh), 60)
    result = mod.check_log_freshness(monitor)
    assert result is None, f"Expected None for fresh log; got {result!r}"


def test_check_log_freshness_stale_returns_alert(tmp_path: Path) -> None:
    """File mtime older than threshold → non-empty alert string."""
    mod = _import_watchdog()
    stale = tmp_path / "stale.log"
    stale.write_text("ancient\n")
    # Backdate mtime to 2 hours ago.
    two_hours_ago = time.time() - (2 * 3600)
    os.utime(stale, (two_hours_ago, two_hours_ago))
    monitor = mod.WatchedMonitor("stale_test", str(stale), 60)  # max_stale=60 min
    result = mod.check_log_freshness(monitor)
    assert result is not None, "Expected alert string for stale log"
    assert "stale_test" in result, "Alert must include monitor name"
    assert "STALE" in result, "Alert text must indicate staleness"
    assert "threshold" in result.lower(), (
        "Alert must include threshold context for operator diagnosis"
    )


def test_check_log_freshness_missing_returns_alert(tmp_path: Path) -> None:
    """File doesn't exist → non-empty alert string indicating absence."""
    mod = _import_watchdog()
    nonexistent = tmp_path / "does_not_exist.log"
    monitor = mod.WatchedMonitor(
        "missing_test", str(nonexistent), 60)
    result = mod.check_log_freshness(monitor)
    assert result is not None, "Expected alert string for missing log"
    assert "missing_test" in result
    assert "MISSING" in result, "Alert must indicate file absence"


def test_check_log_freshness_expands_tilde(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Log paths with `~` must expand to HOME via os.path.expanduser.
    Defends against the class where `~/some.log` is treated as a literal
    relative directory `~`. Uses monkeypatch for proper test isolation
    (R1-m2: prior version leaked HOME mutation on KeyboardInterrupt).
    """
    mod = _import_watchdog()
    fresh = tmp_path / "fresh.log"
    fresh.write_text("home-relative\n")
    monkeypatch.setenv("HOME", str(tmp_path))
    monitor = mod.WatchedMonitor("home_test", "~/fresh.log", 60)
    result = mod.check_log_freshness(monitor)
    assert result is None, (
        f"Expected None for fresh ~/fresh.log; got {result!r}. "
        f"Tilde expansion may be broken."
    )


def test_main_sends_alert_with_correct_dedup_key(tmp_path: Path) -> None:
    """`main()` sends alerts via TelegramNotifier.send with
    `dedup_key=f'monitor_watchdog_{name}'` per the 60-second in-process
    dedup convention (cron-tick resets reduce dedup to per-tick). Drift
    in the dedup key shape would Telegram-storm operators (one alert per
    monitor per cron tick, max len(monitors)/tick) on a chronically-dead
    monitor — bounded but only if the key shape is correct.
    """
    mod = _import_watchdog()
    stale = tmp_path / "stale.log"
    stale.write_text("ancient\n")
    os.utime(stale, (time.time() - 7200, time.time() - 7200))  # 2h ago
    monitors = (mod.WatchedMonitor("test_dedup", str(stale), 60),)

    fake_notifier = MagicMock()
    # disks=() — ticket 86bbvd50a added a real-filesystem usage check to
    # main(); pin the log-freshness dispatch in isolation from the host's
    # disk (tests/contracts/test_monitor_watchdog_disk.py pins the disk path).
    rc = mod.main(monitors=monitors, notifier=fake_notifier, disks=())
    assert rc == 0, "main() must return 0 even on alert fire (cron convention)"
    fake_notifier.send.assert_called_once()
    _, kwargs = fake_notifier.send.call_args
    assert kwargs.get("dedup_key") == "monitor_watchdog_test_dedup", (
        f"dedup_key must be 'monitor_watchdog_{{name}}'; "
        f"got {kwargs.get('dedup_key')!r}"
    )


def test_main_returns_zero_when_all_fresh(tmp_path: Path) -> None:
    """Happy path — all monitors fresh, main() returns 0, notifier untouched."""
    mod = _import_watchdog()
    fresh = tmp_path / "fresh.log"
    fresh.write_text("now\n")
    monitors = (mod.WatchedMonitor("test_ok", str(fresh), 60),)
    fake_notifier = MagicMock()
    # disks=() — see test_main_sends_alert_with_correct_dedup_key.
    rc = mod.main(monitors=monitors, notifier=fake_notifier, disks=())
    assert rc == 0
    fake_notifier.send.assert_not_called()


def test_main_signature_accepts_notifier_seam():
    """`main()` accepts a `notifier` kwarg for test injection. Drift here
    would mean tests can't run without live Telegram creds (fail-CI class).
    """
    mod = _import_watchdog()
    sig = inspect.signature(mod.main)
    assert "notifier" in sig.parameters, (
        "main() must accept a `notifier` test-seam kwarg"
    )
    # Default is None → production code lazy-imports TelegramNotifier.
    assert sig.parameters["notifier"].default is None


def test_main_does_not_reimplement_telegram_client():
    """The watchdog must REUSE `bot.notifier.TelegramNotifier` rather than
    re-implementing the Telegram client. Drift here = config duplication
    (TELEGRAM_BOT_TOKEN env var read twice, two dedup stores, etc.).

    Verifies BOTH (a) the bot.notifier import lives INSIDE the `main`
    function body (lazy import — keeps test collection free of bot
    dep tree, matches collector_health_monitor pattern) AND (b) no
    alternative HTTP client (requests / httpx / aiohttp / urllib3 /
    urllib.request) is imported at module scope (R1-M4 widened deny-list).
    """
    import ast
    src = Path(__file__).resolve().parents[2] / "scripts" / "ops" / "monitor_watchdog.py"
    tree = ast.parse(src.read_text())

    # (a) bot.notifier import must be inside FunctionDef named "main".
    main_func = None
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == "main":
            main_func = node
            break
    assert main_func is not None, "monitor_watchdog.py must define `main()`"

    has_lazy_import = False
    for inner in ast.walk(main_func):
        if isinstance(inner, ast.ImportFrom) and inner.module == "bot.notifier":
            if any(a.name == "TelegramNotifier" for a in inner.names):
                has_lazy_import = True
                break
    assert has_lazy_import, (
        "`from bot.notifier import TelegramNotifier` must live INSIDE "
        "main() (lazy import). A module-top import would break the "
        "documented 'test collection free of bot dep tree' property. "
        "See scripts/ops/collector_health_monitor.py for the canonical pattern."
    )

    # Belt-and-suspenders: also confirm the import is NOT at module top
    # (catches a refactor that adds module-top + leaves main() import).
    for node in tree.body:
        if isinstance(node, ast.ImportFrom) and node.module == "bot.notifier":
            raise AssertionError(
                "`from bot.notifier import ...` found at MODULE TOP — "
                "must be lazy (inside main())."
            )

    # (b) widened deny-list for alternative HTTP clients (R1-M4).
    BANNED_HTTP_LIBS = {"requests", "httpx", "aiohttp", "urllib3",
                        "urllib.request"}
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            for a in n.names:
                assert a.name not in BANNED_HTTP_LIBS, (
                    f"monitor_watchdog.py must not import `{a.name}` "
                    f"directly — alternative HTTP clients are a "
                    f"Telegram-re-implementation smell. Route through "
                    f"bot.notifier.TelegramNotifier."
                )
        elif isinstance(n, ast.ImportFrom):
            assert n.module not in BANNED_HTTP_LIBS, (
                f"monitor_watchdog.py must not import from `{n.module}` "
                f"— alternative HTTP clients are a Telegram-re-implementation "
                f"smell. Route through bot.notifier.TelegramNotifier."
            )


def test_main_production_path_does_not_crash_without_notifier_seam(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R1-C1 regression: previously `main()` constructed
    `TelegramNotifier()` with no args, which crashes with TypeError
    because the constructor requires (bot_token, chat_id). This would
    have crashed on every cron tick in production.

    Now main() must read TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID from env
    and pass positionally; with empty env values, TelegramNotifier
    silently no-ops via its `.enabled` flag.

    This test exercises the PRODUCTION code path (no `notifier=` kwarg).
    It must NOT raise TypeError. It may print to stdout; we ignore output.
    """
    mod = _import_watchdog()
    # Empty Telegram creds — production-no-Telegram scenario.
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "")
    # Single fresh log so we don't fire any alerts in this test.
    fresh = tmp_path / "fresh.log"
    fresh.write_text("now\n")
    monitors = (mod.WatchedMonitor("smoke", str(fresh), 60),)
    # Call main() WITHOUT notifier= kwarg — exercises the production
    # path that R1-C1 said would TypeError-crash.
    rc = mod.main(monitors=monitors)
    assert rc == 0, (
        "main() production path must exit 0 (R1-C1 regression: was "
        "TypeError-crashing on every cron tick because TelegramNotifier() "
        "was instantiated with no args)."
    )


def test_main_production_path_reads_telegram_env_vars(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R1-C1+M3 regression: docstring documents reading TELEGRAM_BOT_TOKEN
    and TELEGRAM_CHAT_ID from env; verify the body actually does. Spoof
    env to known values, capture the lazy TelegramNotifier constructor
    via monkeypatch, assert it receives the spoofed values.
    """
    mod = _import_watchdog()
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "fake-token-XYZ")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "fake-chat-42")

    captured = {}

    class FakeNotifier:
        def __init__(self, bot_token, chat_id):
            captured["bot_token"] = bot_token
            captured["chat_id"] = chat_id
            self.enabled = False

        def send(self, *args, **kwargs):
            pass

    # Monkeypatch the lazy import target so main() picks up FakeNotifier.
    import bot.notifier
    monkeypatch.setattr(bot.notifier, "TelegramNotifier", FakeNotifier)

    fresh = tmp_path / "fresh.log"
    fresh.write_text("now\n")
    monitors = (mod.WatchedMonitor("env_test", str(fresh), 60),)
    rc = mod.main(monitors=monitors)

    assert rc == 0
    assert captured.get("bot_token") == "fake-token-XYZ", (
        f"main() must pass TELEGRAM_BOT_TOKEN env to constructor; "
        f"got bot_token={captured.get('bot_token')!r}"
    )
    assert captured.get("chat_id") == "fake-chat-42", (
        f"main() must pass TELEGRAM_CHAT_ID env to constructor; "
        f"got chat_id={captured.get('chat_id')!r}"
    )
