"""D1.11.a — ``ops/kalshi-espn-collector.service`` systemd unit contract.

Ticket `86ba0ppy0` (2026-05-19, REQUIRES-APPROVAL discipline tier).
Pins the structural shape of the FIFTH systemd unit on the bot VPS
(after kalshi-bot, kalshi-collector D1.5, kalshi-coinbase-collector
D2.5, kalshi-weather-collector D1.8). Mirror of
``test_weather_collector_systemd_unit.py`` adapted for D1.11.a deltas.

Per-unit deltas vs ``kalshi-weather-collector.service`` (D1.8):
  - Different ExecStart wrapper (``espn-collector-start.sh``).
  - Different EnvironmentFile (``/home/botuser/.env.espn-collector``).
  - ``MemoryMax=256M`` (vs Weather's 128M). ESPN polls 24 leagues at
    60s cadence via a sequential ``for league in self._leagues`` loop
    on a single ``requests.Session`` — peak in-flight is 1 response ×
    ~50 KB + 24 BronzeWriter chunk buffers × ~8 KB ≈ ~250 KB working
    set. 256M matched Coinbase's known-good precedent (Coinbase bumped
    to 384M on 2026-05-30 for the 9-asset corpus; ESPN unchanged) rather
    than right-sizing tightly so envelope-construction + json-decode
    transients are absorbed.
  - ``LimitNOFILE=512`` (same as Weather + Coinbase).

Same as D1.8:
  - ``NO CPUAffinity`` directive (kernel scheduler floats across vCPU 0/1).
  - ``Nice=10`` polite-background.
  - ``Restart=on-failure`` + ``RestartSec=10s``.
  - ``MemorySwapMax=0``.
  - ``User=botuser`` + ``WorkingDirectory`` + journal logging.
"""
from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
UNIT_FILE = REPO_ROOT / "ops" / "kalshi-espn-collector.service"


def _read_unit() -> str:
    assert UNIT_FILE.exists(), (
        f"{UNIT_FILE.relative_to(REPO_ROOT)} missing — D1.11.a ships "
        "this file as the source of truth for the on-VPS ESPN "
        "collector systemd unit. Run `bash ops/install.sh` on the VPS "
        "after this lands."
    )
    return UNIT_FILE.read_text()


def _directive(text: str, key: str) -> str | None:
    m = re.search(rf"^{re.escape(key)}=(.*)$", text, re.M)
    return m.group(1).strip() if m else None


def test_unit_file_exists_and_has_required_sections():
    text = _read_unit()
    assert re.search(r"^\[Unit\]", text, re.M)
    assert re.search(r"^\[Service\]", text, re.M)
    assert re.search(r"^\[Install\]", text, re.M)


def test_unit_after_and_wants_network_online():
    text = _read_unit()
    assert _directive(text, "After") == "network-online.target"
    assert _directive(text, "Wants") == "network-online.target"


def test_service_type_is_simple():
    text = _read_unit()
    assert _directive(text, "Type") == "simple"


def test_service_user_is_botuser():
    text = _read_unit()
    assert _directive(text, "User") == "botuser"


def test_service_working_directory_is_repo_root():
    text = _read_unit()
    assert (
        _directive(text, "WorkingDirectory")
        == "/home/botuser/kalshi-bot-repo"
    )


def test_service_environment_file_is_dedicated_espn_env():
    """``EnvironmentFile=/home/botuser/.env.espn-collector`` —
    separate from bot's .env, Kalshi's .env.collector, Coinbase's
    .env.coinbase-collector, Weather's .env.weather-collector.
    Mirrors D1.5/D2.5/D1.8 isolation pattern.
    """
    text = _read_unit()
    assert (
        _directive(text, "EnvironmentFile")
        == "/home/botuser/.env.espn-collector"
    ), (
        "EnvironmentFile must be /home/botuser/.env.espn-collector "
        "(D1.11.a isolation — separate from bot/Kalshi/Coinbase/Weather "
        "env files). Lives outside the repo so deploys cannot wipe "
        "ESPN knobs."
    )


def test_service_execstart_calls_espn_collector_start_sh():
    text = _read_unit()
    assert (
        _directive(text, "ExecStart")
        == "/home/botuser/kalshi-bot-repo/espn-collector-start.sh"
    )


def test_service_restart_on_failure_with_10s_backoff():
    text = _read_unit()
    assert _directive(text, "Restart") == "on-failure", (
        "Restart=on-failure (NOT always) — clean systemctl stop must "
        "halt the ESPN collector without auto-restart."
    )
    assert _directive(text, "RestartSec") in ("10", "10s")


def test_service_has_no_cpu_affinity_directive():
    """No ``CPUAffinity=`` — mirrors D2.5 + D1.8 posture.

    With 5 tenants on a 2-vCPU box (bot implicit vCPU-0, Kalshi
    pinned vCPU-1, Coinbase + Weather + ESPN all unpinned),
    additional pins would over-constrain the kernel scheduler.
    ESPN's HTTP-poll load floats safely across either vCPU.
    """
    text = _read_unit()
    cpu_affinity = _directive(text, "CPUAffinity")
    assert cpu_affinity is None, (
        f"CPUAffinity directive found (value={cpu_affinity!r}); D1.11.a "
        f"deliberately omits CPUAffinity so the kernel can float the "
        f"ESPN collector across either vCPU."
    )


def test_service_nice_is_10():
    text = _read_unit()
    assert _directive(text, "Nice") == "10"


def test_service_memory_max_is_256m():
    """``MemoryMax=256M`` — matched Coinbase's original cap, double Weather's 128M.

    ESPN polls 24 leagues sequentially (`for league in self._leagues`
    loop on a single `requests.Session`) at 60s cadence. Peak in-flight
    = 1 response × ~50 KB + 24 BronzeWriter chunk buffers × ~8 KB ≈
    ~250 KB working set. 256M matched Coinbase's known-good precedent
    (Coinbase bumped 256M→384M on 2026-05-30 for the 9-asset corpus;
    ESPN unchanged) rather than right-sizing tightly so
    envelope-construction + json-decode transients are absorbed
    without OOM-kill risk.
    """
    text = _read_unit()
    assert _directive(text, "MemoryMax") == "256M", (
        "MemoryMax=256M — D1.11.a isolation. Sequential per-league "
        "polling caps in-flight at 1 response × ~50 KB; 256M matched "
        "Coinbase's original cap (Coinbase now 384M) rather than "
        "right-sizing tightly."
    )


def test_service_memory_swap_max_is_zero():
    text = _read_unit()
    assert _directive(text, "MemorySwapMax") == "0"


def test_service_limit_nofile_is_512():
    """``LimitNOFILE=512`` — same as Coinbase + Weather. 24 writers ×
    2 rotation files + rclone subprocess + HTTP keep-alive sockets ≈
    60 fd typical; 512 gives ~8× headroom.
    """
    text = _read_unit()
    assert _directive(text, "LimitNOFILE") == "512"


def test_service_logs_to_journal():
    text = _read_unit()
    assert _directive(text, "StandardOutput") == "journal"
    assert _directive(text, "StandardError") == "journal"


def test_install_wantedby_multi_user():
    text = _read_unit()
    assert _directive(text, "WantedBy") == "multi-user.target"


def test_unit_does_not_use_restart_always():
    """Negative pin: ESPN collector uses ``on-failure``, NOT ``always``."""
    text = _read_unit()
    assert "Restart=always" not in text, (
        "Restart=always detected — D1.11.a (mirroring D1.5+D2.5+D1.8) "
        "uses on-failure. A clean systemctl stop must halt without "
        "auto-restart."
    )
