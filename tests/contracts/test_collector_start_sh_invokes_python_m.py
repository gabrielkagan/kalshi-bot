"""D1.5 — ``collector-start.sh`` body contract.

Ticket `86b9ypna4` (2026-05-16, REQUIRES-APPROVAL discipline tier).

The wrapper is invoked by ``ops/kalshi-collector.service``'s
``ExecStart`` directive. It mirrors ``start.sh``'s posture (fail-fast,
venv-activate, env-source, exec into python) — pinned here so a
future edit that drops `set -e`, points at the wrong venv, or
sources the bot's `.env` instead of the dedicated `.env.collector`
fires at the contract gate.

The D1.1 stub shipped a body that sourced the SHARED bot `.env`.
D1.5 strengthens isolation per the unit's
``EnvironmentFile=/home/botuser/.env.collector`` directive — the
wrapper sources the dedicated collector env file too (belt-and-
suspenders: systemd loads it via EnvironmentFile; bash sources it
for any future codepath that invokes collector-start.sh outside
systemd, mirroring start.sh's belt-and-suspenders pattern).
"""
from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
COLLECTOR_START = REPO_ROOT / "collector-start.sh"


def _read() -> str:
    assert COLLECTOR_START.exists(), (
        f"{COLLECTOR_START.relative_to(REPO_ROOT)} missing — D1.1 "
        "shipped this wrapper stub; D1.5 strengthens its body to "
        "match the new env-file isolation."
    )
    return COLLECTOR_START.read_text()


def test_collector_start_is_executable():
    assert COLLECTOR_START.exists()
    mode = COLLECTOR_START.stat().st_mode
    assert mode & 0o100, (
        f"{COLLECTOR_START.relative_to(REPO_ROOT)} is not executable. "
        "systemd's ExecStart needs the +x bit; fix with `chmod +x`."
    )


def test_starts_with_shebang():
    text = _read()
    assert text.startswith("#!"), (
        "Missing shebang — systemd's ExecStart invokes the file "
        "directly, so the shebang line is what selects the interpreter."
    )
    first_line = text.splitlines()[0]
    assert "bash" in first_line, (
        f"Shebang {first_line!r} doesn't invoke bash. start.sh uses "
        "`#!/bin/bash`; collector-start.sh must match for consistent "
        "set -eo pipefail semantics."
    )


def test_set_eo_pipefail_enabled():
    """``set -eo pipefail`` makes the wrapper fail-fast.

    Without ``-e``: a `source` failure on a missing `.env.collector`
    silently continues and the next `exec python3 -m collector` runs
    against the system python without dependencies.
    Without ``pipefail``: piped sources mask the exit code of the left
    side.
    """
    text = _read()
    assert re.search(r"^set\s+-eo\s+pipefail\b", text, re.M), (
        "Missing `set -eo pipefail` at column 0. Without it, a "
        "venv-activate failure or missing .env.collector silently "
        "falls through to system python."
    )


def test_cd_to_repo_root():
    text = _read()
    assert re.search(
        r"^cd\s+/home/botuser/kalshi-bot-repo\b", text, re.M
    ), (
        "Missing `cd /home/botuser/kalshi-bot-repo`. systemd's "
        "WorkingDirectory= directive sets it too, but the explicit cd "
        "is belt-and-suspenders for invocations outside systemd "
        "(mirrors start.sh's pattern)."
    )


def test_sources_venv_activate():
    text = _read()
    assert re.search(
        r"^source\s+/home/botuser/kalshi-bot-repo/venv/bin/activate\b",
        text,
        re.M,
    ), (
        "Missing `source venv/bin/activate`. Without the venv, "
        "`python3 -m collector` runs against the system interpreter "
        "and fails on the first `import websocket` (or similar)."
    )


def test_sources_dedicated_collector_env_file():
    """Must source `/home/botuser/.env.collector` (NOT the bot's
    repo-rooted `.env`).

    The D1.1 stub sourced the bot's `.env`; D1.5 moves to the
    dedicated home-rooted env file so credential rotation for the
    collector key cannot disturb the bot's runtime env. Mirrors the
    unit's EnvironmentFile= directive (belt-and-suspenders).
    """
    text = _read()
    assert re.search(
        r"^source\s+/home/botuser/\.env\.collector\b", text, re.M
    ), (
        "Missing `source /home/botuser/.env.collector`. D1.5 "
        "deliberately uses a dedicated env file outside the repo so "
        "credential rotation doesn't require a redeploy."
    )
    # Negative pin: must NOT also source the bot's .env (would re-
    # introduce the very coupling D1.5 is removing).
    assert not re.search(
        r"^source\s+/home/botuser/kalshi-bot-repo/\.env\b", text, re.M
    ), (
        "collector-start.sh still sources the bot's "
        "/home/botuser/kalshi-bot-repo/.env. D1.5 moves the collector "
        "to /home/botuser/.env.collector exclusively — sourcing both "
        "would mean a rotated bot key still leaks into the collector "
        "process env."
    )


def test_exec_python_m_collector():
    """``exec python3 [flags...] -m collector`` — NOT `-m bot`, NOT a bare script.

    P1-A-fu2 (ticket 86ba1qgbp, 2026-05-20) loosened the python3-to-`-m`
    portion to allow short flags (e.g., `-O`) between. The strict
    pin on the `-O` flag itself lives in the sister test
    `tests/contracts/test_collector_start_sh_uses_O_flag.py::
    test_python3_invoked_with_O_flag`. This test continues to pin:
      - exec semantics (bash → python via PID 1 replacement)
      - module invocation (`-m collector`)
      - no `-m bot` (sacred-boundary)
    """
    text = _read()
    assert re.search(
        r"^exec\s+python3(\s+-\w+)*\s+-m\s+collector\b", text, re.M
    ), (
        "Missing `exec python3 [flags...] -m collector`. The `exec` is "
        "what makes python3 replace bash as PID 1 of the systemd cgroup "
        "— without it, signals from systemd hit bash, not python."
    )
    # Negative pin: must NOT invoke the bot.
    assert not re.search(
        r"^exec\s+python3(\s+-\w+)*\s+-m\s+bot\b", text, re.M
    ), (
        "collector-start.sh execs `python3 -m bot` — that's start.sh's "
        "entrypoint, not the collector's. Sacred-boundary violation "
        "(would launch the trading bot from the collector unit)."
    )
