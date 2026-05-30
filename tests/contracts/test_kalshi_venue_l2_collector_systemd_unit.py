"""B2a-1 — ``ops/kalshi-venue-l2-collector.service`` systemd unit contract.

Ticket `86ba1zf5j` (REQUIRES-APPROVAL — deploy starts the ~14d bronze
accumulation clock for the B2a RMSE validation gate).

SIXTH systemd unit on the bot VPS (after kalshi-bot, kalshi-collector,
kalshi-coinbase-collector, kalshi-weather-collector, kalshi-espn-collector).
Mirror of ``test_kalshi_coinbase_collector_systemd_unit.py`` adapted for
the per-unit deltas of the multi-venue lean L2 recorder:

  - Different ExecStart wrapper (``venue-l2-collector-start.sh``).
  - Different EnvironmentFile (``/home/botuser/.env.venue-l2-collector``).
  - ``MemoryMax=512M`` (vs Coinbase 384M, was 256M pre-2026-05-30). Three concurrent WS conns +
    periodic large Gemini full-book snapshots + the synchronous zstd
    compress-whole-in-flight memory spike at rotation. 512M with headroom
    on the s-4vcpu-8gb box.
  - NO ``CPUAffinity`` (same posture as coinbase/weather/espn — kernel
    floats it; Nice=10 gates priority).
"""
from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
UNIT_FILE = REPO_ROOT / "ops" / "kalshi-venue-l2-collector.service"


def _read_unit() -> str:
    assert UNIT_FILE.exists(), (
        f"{UNIT_FILE.relative_to(REPO_ROOT)} missing — B2a-1 ships this "
        "file as the source of truth for the on-VPS venue-L2 collector "
        "systemd unit. Run `bash ops/install.sh` on the VPS after this lands."
    )
    return UNIT_FILE.read_text()


def _directive(text: str, key: str) -> str | None:
    m = re.search(rf"^{re.escape(key)}=(.*)$", text, re.M)
    return m.group(1).strip() if m else None


def test_unit_has_required_sections():
    text = _read_unit()
    assert re.search(r"^\[Unit\]", text, re.M)
    assert re.search(r"^\[Service\]", text, re.M)
    assert re.search(r"^\[Install\]", text, re.M)


def test_after_and_wants_network_online():
    text = _read_unit()
    assert _directive(text, "After") == "network-online.target"
    assert _directive(text, "Wants") == "network-online.target"


def test_service_type_simple_and_user_botuser():
    text = _read_unit()
    assert _directive(text, "Type") == "simple"
    assert _directive(text, "User") == "botuser"


def test_working_directory_is_repo_root():
    text = _read_unit()
    assert _directive(text, "WorkingDirectory") == "/home/botuser/kalshi-bot-repo"


def test_environment_file_is_dedicated_venue_l2_env():
    text = _read_unit()
    assert (
        _directive(text, "EnvironmentFile")
        == "/home/botuser/.env.venue-l2-collector"
    ), (
        "EnvironmentFile must be /home/botuser/.env.venue-l2-collector — "
        "separate from the bot's .env and the other collectors' env files "
        "(D0.3 §6 isolation; lives outside the repo so deploys cannot wipe "
        "it)."
    )


def test_execstart_calls_venue_l2_start_sh():
    text = _read_unit()
    assert (
        _directive(text, "ExecStart")
        == "/home/botuser/kalshi-bot-repo/venue-l2-collector-start.sh"
    )


def test_restart_on_failure_10s():
    text = _read_unit()
    assert _directive(text, "Restart") == "on-failure"
    assert _directive(text, "RestartSec") in ("10", "10s")
    assert "Restart=always" not in text, (
        "Restart=always detected — collectors deliberately use on-failure "
        "so a clean systemctl stop halts collection without auto-restart."
    )


def test_no_cpu_affinity():
    text = _read_unit()
    assert _directive(text, "CPUAffinity") is None, (
        "B2a-1 deliberately omits CPUAffinity (kernel floats it across "
        "vCPUs; Nice=10 gates priority). Mirror coinbase/weather/espn."
    )


def test_nice_is_10():
    text = _read_unit()
    assert _directive(text, "Nice") == "10"


def test_memory_max_is_512m():
    text = _read_unit()
    assert _directive(text, "MemoryMax") == "512M", (
        "MemoryMax=512M — 3 WS conns + periodic large Gemini full-book "
        "snapshots + the synchronous zstd compress-whole-in-flight spike "
        "at rotation. Heavier than coinbase single-conn (384M)."
    )


def test_memory_swap_max_zero():
    text = _read_unit()
    assert _directive(text, "MemorySwapMax") == "0"


def test_limit_nofile_512():
    text = _read_unit()
    assert _directive(text, "LimitNOFILE") == "512"


def test_logs_to_journal():
    text = _read_unit()
    assert _directive(text, "StandardOutput") == "journal"
    assert _directive(text, "StandardError") == "journal"


def test_install_wantedby_multi_user():
    text = _read_unit()
    assert _directive(text, "WantedBy") == "multi-user.target"
