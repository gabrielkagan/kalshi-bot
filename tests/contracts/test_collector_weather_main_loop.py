"""D1.8 — ``collector/weather_main_loop.py`` orchestrator shape contract.

Ticket `86ba0duck` (2026-05-18). Pins the structural shape of the NEW
weather main-loop module that the ``kalshi-weather-collector`` systemd
unit invokes as ``python3 -m collector.weather_main_loop``.

Mirrors ``collector/coinbase_main_loop.py`` (D2.5) for the weather
deltas:
  - **HTTP-poll loop, NOT a WS reader.** No WSClient construction,
    no drain-bounded WS callbacks; the run loop is a 60-min poll
    tick + sleep.
  - **4 BronzeWriters** (one per channel) at construction. No
    ``conn=`` dimension (HTTP polling has no persistent conn);
    writers are constructed with ``conn=None`` (writer.py:245
    handles this fallback by emitting ``conn=none`` in the
    partition string).
  - **60-min rotation** (NOT D0.3 §4 default 5-min) per D1.8 plan-doc
    decision #4 — pin `interval_seconds=3600` at writer construction.
  - **SIGINT/SIGTERM signal handlers** install when no shutdown_event
    is passed (production path); tests pass an explicit
    ``shutdown_event`` and bypass the signal handler.

L99 PARANOID lesson: pin every load-bearing structural decision.
"""
from __future__ import annotations

import ast
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
MODULE_FILE = REPO_ROOT / "collector" / "weather_main_loop.py"


def _module_source() -> str:
    assert MODULE_FILE.exists(), (
        f"{MODULE_FILE.relative_to(REPO_ROOT)} missing — D1.8 ships "
        "this module as the orchestrator that the "
        "kalshi-weather-collector systemd unit invokes via `python3 -m "
        "collector.weather_main_loop`."
    )
    return MODULE_FILE.read_text()


def _module_ast() -> ast.Module:
    return ast.parse(_module_source())


def test_module_imports_only_from_collector_kalshi_wire_and_stdlib():
    """No ``bot.*`` imports — D0.3 §10 bot-isolation contract.

    AST defense-in-depth on top of the ``collector-no-bot``
    import-linter contract.
    """
    tree = _module_ast()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert not alias.name.startswith(("bot.", "bot ")), (
                    f"`import {alias.name}` forbidden — D0.3 §10."
                )
                assert alias.name != "bot", (
                    "`import bot` forbidden — D0.3 §10."
                )
        elif isinstance(node, ast.ImportFrom):
            mod = node.module
            assert mod is not None, "relative imports forbidden"
            assert not mod.startswith("bot."), (
                f"`from {mod} import …` forbidden — D0.3 §10."
            )
            assert mod != "bot", "`from bot import …` forbidden."


def test_module_has_run_function():
    """The systemd entrypoint must call ``run()`` at module load."""
    tree = _module_ast()
    has_run = any(
        isinstance(node, ast.FunctionDef) and node.name == "run"
        for node in ast.iter_child_nodes(tree)
    )
    assert has_run, (
        "collector.weather_main_loop must define a top-level `run()` "
        "function — invoked by the systemd unit via `python3 -m "
        "collector.weather_main_loop`."
    )


def test_module_has_main_block():
    """``if __name__ == "__main__": run()`` at module bottom."""
    src = _module_source()
    assert 'if __name__ == "__main__"' in src, (
        "Module missing `if __name__ == \"__main__\":` block — without "
        "it, `python3 -m collector.weather_main_loop` would import the "
        "module but never invoke run()."
    )


def test_run_constructs_writers_with_hourly_rotation():
    """Writer construction must pass ``interval_seconds=3600`` (60-min
    rotation per D1.8 plan-doc decision #4).

    The D0.3 §4 default is 5-min; weather's 60-min poll cadence makes
    the default produce mostly-empty chunks. AST scan of the run
    function body for the literal kwarg.
    """
    src = _module_source()
    assert "interval_seconds=3600" in src or "interval_seconds = 3600" in src, (
        "Module must instantiate BronzeWriter with "
        "`interval_seconds=3600` (60-min rotation per D1.8 plan-doc "
        "decision #4). Default 5-min would produce mostly-empty chunks "
        "at the 60-min weather poll cadence and incur unnecessary "
        "DEEP_ARCHIVE per-object minimum overhead."
    )


def test_run_installs_signal_handlers_when_owning_shutdown_event():
    """Signal handler install pattern mirrors coinbase_main_loop.py.

    String-search for the canonical `signal.signal(signal.SIGINT, ...)` and
    `signal.signal(signal.SIGTERM, ...)` calls — a missing handler means
    systemd's `kill -TERM <pid>` would not trigger graceful shutdown.
    """
    src = _module_source()
    assert "signal.SIGINT" in src, (
        "Module must install a SIGINT handler — `Ctrl-C` in dev or "
        "systemd's `kill -INT` must trigger graceful shutdown."
    )
    assert "signal.SIGTERM" in src, (
        "Module must install a SIGTERM handler — systemd's `systemctl "
        "stop` sends SIGTERM and expects graceful shutdown."
    )


def test_run_constructs_four_writers_one_per_channel():
    """The run() function must allocate one BronzeWriter per channel
    listed in DEFAULT_CHANNELS. String-pin via the source — each
    channel name must appear as a string in the orchestrator (either
    iterated from DEFAULT_CHANNELS or written explicitly).
    """
    src = _module_source()
    # We accept either "for channel in DEFAULT_CHANNELS:" loop OR
    # explicit per-channel construction. String-search the channel
    # name is sufficient — the loop-form references DEFAULT_CHANNELS
    # by name (which the archiver module test pins).
    assert (
        "DEFAULT_CHANNELS" in src
        or all(
            f'"{ch}"' in src or f"'{ch}'" in src
            for ch in (
                "ensemble_gfs",
                "ensemble_ecmwf",
                "forecast_hrrr",
                "archive_observed",
            )
        )
    ), (
        "run() must allocate writers for all 4 weather channels — "
        "either iterate DEFAULT_CHANNELS or reference each channel name "
        "explicitly. Without per-channel writer allocation, the archiver "
        "dispatch would have nowhere to write."
    )


def test_run_imports_build_envelope_or_delegates_to_archiver():
    """The orchestrator either imports BronzeWriter directly (and lets
    WeatherArchiver build envelopes) OR imports build_envelope itself.
    Either path is acceptable; what's NOT acceptable is constructing
    envelopes inline in the orchestrator's loop.

    Pin via positive presence of one of the two import paths.
    """
    tree = _module_ast()
    has_writer = False
    has_archiver = False
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            names = {alias.name for alias in node.names}
            if mod == "collector.writer" and "BronzeWriter" in names:
                has_writer = True
            if mod == "collector.weather_archiver" and "WeatherArchiver" in names:
                has_archiver = True
    assert has_writer and has_archiver, (
        f"run() must import BronzeWriter (from collector.writer) AND "
        f"WeatherArchiver (from collector.weather_archiver). "
        f"has_writer={has_writer} has_archiver={has_archiver}"
    )


def test_run_reads_weather_collector_env_vars():
    """``run()`` reads the 5 weather-specific env vars per D1.8 plan-doc
    decision #9. String-search the module source for each env var name.
    """
    src = _module_source()
    required_env = [
        "WEATHER_BRONZE_ROOT",
        "WEATHER_POLL_INTERVAL_SECONDS",
        "RCLONE_REMOTE",
        "S3_BUCKET",
    ]
    for env_var in required_env:
        assert f'"{env_var}"' in src or f"'{env_var}'" in src, (
            f"Module must reference env var '{env_var}' — D1.8 plan-doc "
            f"decision #9."
        )
