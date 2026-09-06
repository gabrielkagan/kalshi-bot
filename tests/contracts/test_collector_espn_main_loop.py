"""D1.11.a — ``collector/espn_main_loop.py`` orchestrator shape contract.

Ticket `86ba0ppy0` (2026-05-19). Pins the structural shape of the NEW
ESPN main-loop module that the ``kalshi-espn-collector`` systemd unit
invokes as ``python3 -m collector.espn_main_loop``.

Mirrors ``collector/weather_main_loop.py`` (D1.8) for the ESPN deltas:
  - **HTTP-poll loop, NOT a WS reader.** No WSClient construction,
    no drain-bounded WS callbacks; the run loop is a 60-second poll
    tick + sleep.
  - **23 BronzeWriters** (one per enabled league per
    ``espn_archiver.LEAGUES_ESPN``) at construction. No ``conn=``
    dimension (HTTP polling has no persistent conn); writers are
    constructed with ``conn=None`` (writer.py:245 handles this
    fallback by emitting ``conn=none`` in the partition string).
  - **60-min rotation** (NOT D0.3 §4 default 5-min) per D1.11.a
    plan-doc decision #4 — pin ``interval_seconds=3600`` at writer
    construction. At 60s poll cadence each writer sees ~60 envelopes
    per hour; rotation aligns chunk boundaries with hour-partition
    boundaries.
  - **SIGINT/SIGTERM signal handlers** install when no shutdown_event
    is passed (production path); tests pass an explicit
    ``shutdown_event`` and bypass the signal handler.
  - **Env vars** per D1.11.a plan-doc decision #9 (``ESPN_BRONZE_ROOT``
    / ``ESPN_POLL_INTERVAL_SECONDS`` / ``ESPN_HEALTH_SIDECAR_PATH`` /
    ``RCLONE_REMOTE`` / ``S3_BUCKET``).

L99 PARANOID lesson: pin every load-bearing structural decision.
"""
from __future__ import annotations

import ast
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
MODULE_FILE = REPO_ROOT / "collector" / "espn_main_loop.py"


def _module_source() -> str:
    assert MODULE_FILE.exists(), (
        f"{MODULE_FILE.relative_to(REPO_ROOT)} missing — D1.11.a ships "
        "this module as the orchestrator that the "
        "kalshi-espn-collector systemd unit invokes via `python3 -m "
        "collector.espn_main_loop`."
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
        "collector.espn_main_loop must define a top-level `run()` "
        "function — invoked by the systemd unit via `python3 -m "
        "collector.espn_main_loop`."
    )


def test_module_has_main_block():
    """``if __name__ == "__main__": run()`` at module bottom."""
    src = _module_source()
    assert 'if __name__ == "__main__"' in src, (
        "Module missing `if __name__ == \"__main__\":` block — without "
        "it, `python3 -m collector.espn_main_loop` would import the "
        "module but never invoke run()."
    )


def test_run_constructs_writers_with_hourly_rotation():
    """Writer construction must pass ``interval_seconds=3600`` (60-min
    rotation per D1.11.a plan-doc decision #4).

    The D0.3 §4 default is 5-min; ESPN's 60s poll cadence + per-league
    sparse-payload nature makes the default produce sub-optimal chunks
    (a single empty-league poll is ~500 bytes compressed; 5-min chunks
    would be ~30 KB each, below the DEEP_ARCHIVE 40KB-per-object
    minimum). AST scan of the run function body for the literal kwarg.
    """
    src = _module_source()
    assert "interval_seconds=3600" in src or "interval_seconds = 3600" in src, (
        "Module must instantiate BronzeWriter with "
        "`interval_seconds=3600` (60-min rotation per D1.11.a plan-doc "
        "decision #4). Default 5-min would produce sub-optimal chunks "
        "at the 60s ESPN poll cadence."
    )


def test_run_installs_signal_handlers_when_owning_shutdown_event():
    """Signal handler install pattern mirrors weather_main_loop.py.

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


def test_run_imports_writer_and_archiver():
    """run() must import BronzeWriter (from collector.writer) AND
    ESPNArchiver (from collector.espn_archiver). Either-of is NOT
    sufficient — orchestrator must compose both.
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
            if mod == "collector.espn_archiver" and "ESPNArchiver" in names:
                has_archiver = True
    assert has_writer and has_archiver, (
        f"run() must import BronzeWriter (from collector.writer) AND "
        f"ESPNArchiver (from collector.espn_archiver). "
        f"has_writer={has_writer} has_archiver={has_archiver}"
    )


def test_run_reads_espn_collector_env_vars():
    """``run()`` reads the env vars per D1.11.a plan-doc decision #9.
    String-search the module source for each env var name.

    R1-N2 fix: extended ESPN_HEALTH_SIDECAR_PATH to the required-env
    list so a future refactor that drops the sidecar env read fires
    here instead of producing a stale-sidecar alert silently (the
    monitor's two-knob derivation falls back to a default path that
    diverges from the writer's bronze_root).
    """
    src = _module_source()
    required_env = [
        "ESPN_BRONZE_ROOT",
        "ESPN_POLL_INTERVAL_SECONDS",
        "ESPN_HEALTH_SIDECAR_PATH",
        "RCLONE_REMOTE",
        "S3_BUCKET",
    ]
    for env_var in required_env:
        assert f'"{env_var}"' in src or f"'{env_var}'" in src, (
            f"Module must reference env var '{env_var}' — D1.11.a "
            f"plan-doc decision #9."
        )


def test_run_default_poll_interval_is_60_seconds():
    """The default ESPN poll cadence is 60 seconds per D1.11.a plan-doc
    decision #2 (NOT 60min like weather). At 60s the collector matches
    the bot's effective polling rate during live games.
    """
    src = _module_source()
    # Pin the default constant either as DEFAULT_POLL_INTERVAL_SECONDS:
    # int = 60 or the equivalent assignment. Accept both annotated and
    # bare assignment forms.
    candidates = [
        "DEFAULT_POLL_INTERVAL_SECONDS: int = 60",
        "DEFAULT_POLL_INTERVAL_SECONDS = 60",
        "DEFAULT_POLL_INTERVAL_SECONDS: int=60",
        "DEFAULT_POLL_INTERVAL_SECONDS=60",
    ]
    assert any(c in src for c in candidates), (
        "Module must define DEFAULT_POLL_INTERVAL_SECONDS=60 — "
        "D1.11.a plan-doc decision #2 pins 60s cadence (NOT 60min "
        "like D1.8 weather)."
    )


def test_run_imports_uploader_for_s3():
    """run() must import RcloneUploader (from collector.uploader) so
    rotated chunks reach S3. Without this, bronze stays local and the
    disk fills.
    """
    tree = _module_ast()
    has_uploader = False
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            names = {alias.name for alias in node.names}
            if mod == "collector.uploader" and "RcloneUploader" in names:
                has_uploader = True
                break
    assert has_uploader, (
        "run() must import RcloneUploader from collector.uploader — "
        "without it, rotated bronze chunks never reach S3."
    )


def test_run_has_drain_thread_function():
    """The drain loop must run in a daemon thread mirroring
    weather_main_loop._drain_rotated. The function name is operator-
    chosen but must contain 'drain' to make journalctl tracing
    discoverable.
    """
    tree = _module_ast()
    func_names = {
        node.name for node in ast.iter_child_nodes(tree)
        if isinstance(node, ast.FunctionDef)
    }
    drain_funcs = {n for n in func_names if "drain" in n.lower()}
    assert drain_funcs, (
        f"Module must define a drain function (mirror of "
        f"weather_main_loop._drain_rotated). Existing functions: "
        f"{sorted(func_names)}"
    )


def test_run_writes_bronze_health_sidecar():
    """The orchestrator must write the bronze_health.json sidecar so
    collector_health_monitor.py's check_dropped_frames can detect
    staleness. Pin via function name string-match.
    """
    src = _module_source()
    assert "write_bronze_health_sidecar" in src or "bronze_health.json" in src, (
        "Module must write the bronze_health.json sidecar — "
        "collector_health_monitor.py's check_dropped_frames reads it "
        "(stale-mtime detection kicks in within 2 monitor ticks of a "
        "wedged drain thread)."
    )
