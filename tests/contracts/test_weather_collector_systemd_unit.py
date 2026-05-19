"""D1.8 — ``ops/kalshi-weather-collector.service`` systemd unit contract.

Ticket `86ba0duck` (2026-05-18, REQUIRES-APPROVAL discipline tier).
Pins the structural shape of the FOURTH systemd unit on the bot VPS
(after kalshi-bot, kalshi-collector D1.5, kalshi-coinbase-collector D2.5).
Mirror of ``test_kalshi_coinbase_collector_systemd_unit.py`` adapted
for D1.8 deltas.

Per-unit deltas vs ``kalshi-coinbase-collector.service`` (D2.5):
  - Different ExecStart wrapper (``weather-collector-start.sh``).
  - Different EnvironmentFile (``/home/botuser/.env.weather-collector``).
  - ``MemoryMax=128M`` (vs Coinbase's 256M). Weather is HTTP-polled
    at 60-min cadence — 4 cycles × 19 cities × 4 channels per cycle ≈
    very light memory footprint. 128M provides ~5× headroom over
    measured baseline.
  - ``LimitNOFILE=512`` (same as Coinbase). Light fd budget: 4
    writers × 2 rotation files + rclone process + headroom.

Same as D2.5:
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
UNIT_FILE = REPO_ROOT / "ops" / "kalshi-weather-collector.service"


def _read_unit() -> str:
    assert UNIT_FILE.exists(), (
        f"{UNIT_FILE.relative_to(REPO_ROOT)} missing — D1.8 ships this "
        "file as the source of truth for the on-VPS weather collector "
        "systemd unit. Run `bash ops/install.sh` on the VPS after this lands."
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


def test_service_environment_file_is_dedicated_weather_env():
    """``EnvironmentFile=/home/botuser/.env.weather-collector`` —
    separate from bot's .env, Kalshi's .env.collector, Coinbase's
    .env.coinbase-collector. Mirrors D1.5/D2.5 isolation pattern.
    """
    text = _read_unit()
    assert (
        _directive(text, "EnvironmentFile")
        == "/home/botuser/.env.weather-collector"
    ), (
        "EnvironmentFile must be /home/botuser/.env.weather-collector "
        "(D1.8 isolation — separate from bot/Kalshi/Coinbase env files). "
        "Lives outside the repo so deploys cannot wipe weather knobs."
    )


def test_service_execstart_calls_weather_collector_start_sh():
    text = _read_unit()
    assert (
        _directive(text, "ExecStart")
        == "/home/botuser/kalshi-bot-repo/weather-collector-start.sh"
    )


def test_service_restart_on_failure_with_10s_backoff():
    text = _read_unit()
    assert _directive(text, "Restart") == "on-failure", (
        "Restart=on-failure (NOT always) — clean systemctl stop must "
        "halt the weather collector without auto-restart."
    )
    assert _directive(text, "RestartSec") in ("10", "10s")


def test_service_has_no_cpu_affinity_directive():
    """No ``CPUAffinity=`` — mirrors D2.5 posture.

    With 4 tenants on a 2-vCPU box (bot implicit vCPU-0, Kalshi
    pinned vCPU-1, Coinbase + Weather both unpinned), additional
    pins would over-constrain the kernel scheduler. Weather's
    HTTP-poll load is the lightest of all 4; floats safely.
    """
    text = _read_unit()
    cpu_affinity = _directive(text, "CPUAffinity")
    assert cpu_affinity is None, (
        f"CPUAffinity directive found (value={cpu_affinity!r}); D1.8 "
        f"deliberately omits CPUAffinity so the kernel can float the "
        f"weather collector across either vCPU."
    )


def test_service_nice_is_10():
    text = _read_unit()
    assert _directive(text, "Nice") == "10"


def test_service_memory_max_is_128m():
    """``MemoryMax=128M`` — half of Coinbase's 256M cap.

    Weather is HTTP-polled at 60-min cadence × 19 cities × 4 channels
    per cycle. Each cycle's working set is ~1.3KB × 76 = ~100KB; the
    writer's in-flight memory is bounded by the rotation cadence (60-min)
    × 4 channels ≈ 0.4MB. 128M provides ~300× headroom over measured
    working set — generous to absorb retry buffers + zstd compression.
    """
    text = _read_unit()
    assert _directive(text, "MemoryMax") == "128M", (
        "MemoryMax=128M — D1.8 isolation. Weather HTTP-poll is the "
        "lightest of all 4 collector tiers; 128M provides ample "
        "headroom over the ~0.4MB measured working set."
    )


def test_service_memory_swap_max_is_zero():
    text = _read_unit()
    assert _directive(text, "MemorySwapMax") == "0"


def test_service_limit_nofile_is_512():
    """``LimitNOFILE=512`` — same as Coinbase. 4 writers × 2 rotation
    files + rclone subprocess + HTTP keep-alive sockets ≈ 30 fd typical;
    512 gives ~15× headroom.
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
    """Negative pin: weather collector uses ``on-failure``, NOT ``always``."""
    text = _read_unit()
    assert "Restart=always" not in text, (
        "Restart=always detected — D1.8 (mirroring D1.5+D2.5) uses "
        "on-failure. A clean systemctl stop must halt without auto-restart."
    )
