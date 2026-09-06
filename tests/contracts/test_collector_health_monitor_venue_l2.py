"""B2a-1 — ``scripts/ops/collector_health_monitor.py`` 6th tier:
``kalshi-venue-l2-collector``.

Ticket `86ba1zf5j`. The post-D1.11.a monitor has 5 tiers:
  - kalshi-collector (`d1_6`)        — WS collector, 4 checks (5 post-86bbvdcat: + boot_state)
  - kalshi-coinbase-collector (`d2_5`) — WS collector, 4 checks
  - kalshi-bot (`b3_fu3`)            — bot, 1 check
  - kalshi-weather-collector (`d1_8`) — HTTP poll, 3 checks
  - kalshi-espn-collector (`d1_11`)  — HTTP poll, 3 checks

B2a-1 adds a 6th: kalshi-venue-l2-collector with dedup prefix `b2a`.
It is a WS collector (3 persistent venue conns), so its check subset is
the FULL four — disk + ws_reconnects + collector_active + dropped_frames
— UNLIKE the HTTP-poll weather/espn tiers (which omit ws_reconnects).

The ws_reconnects check MUST pass the venue-l2 disconnect marker
(``venue_l2_ws_disconnected``); the Kalshi-default ``kalshi_ws_disconnected``
substring would never match the recorder's journal lines, producing an
always-OK false negative during a sustained reconnect storm.
"""
from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
MONITOR_FILE = REPO_ROOT / "scripts" / "ops" / "collector_health_monitor.py"


def _src() -> str:
    return MONITOR_FILE.read_text()


def _block(src: str, name: str) -> str:
    idx = src.find(f"{name} = [")
    if idx == -1:
        idx = src.find(f"{name}=[")
    assert idx != -1, f"missing `{name} = [...]` block"
    depth = 0
    for i in range(idx, min(len(src), idx + 4000)):
        if src[i] == "[":
            depth += 1
        elif src[i] == "]":
            depth -= 1
            if depth == 0:
                return src[idx:i + 1]
    return src[idx:]


def test_venue_l2_unit_constant_defined():
    src = _src()
    assert "VENUE_L2_COLLECTOR_UNIT" in src
    assert '"kalshi-venue-l2-collector"' in src


def test_venue_l2_tier_added_with_b2a_prefix():
    src = _src()
    assert '"kalshi-venue-l2-collector"' in src
    assert '"b2a"' in src, (
        "main() dispatch missing 'b2a' dedup prefix — without a per-tier "
        "prefix the venue-l2 alerts would collide with other tiers."
    )


def test_venue_l2_checks_include_all_four():
    """WS collector → full check set (disk + ws_reconnects +
    collector_active + dropped_frames)."""
    block = _block(_src(), "venue_l2_checks")
    for check_name in (
        '"disk"',
        '"ws_reconnects"',
        '"collector_active"',
        '"dropped_frames"',
    ):
        assert check_name in block, (
            f"venue_l2_checks block missing {check_name} — venue-l2 is a "
            "WS collector and needs the full 4-check set."
        )


def test_venue_l2_ws_reconnects_uses_venue_marker():
    """The ws_reconnects check must pass the venue-l2 disconnect marker
    (NOT the Kalshi default, which would never match the recorder's logs).

    The marker is wired via the module-level VENUE_L2_WS_DISCONNECT_MARKER
    constant (single source of truth). Pin BOTH: the constant equals the
    expected literal AND the check block references it."""
    src = _src()
    assert 'VENUE_L2_WS_DISCONNECT_MARKER = "venue_l2_ws_disconnected"' in src, (
        "module must define VENUE_L2_WS_DISCONNECT_MARKER = "
        '"venue_l2_ws_disconnected" — must match the journal marker the '
        "recorder emits (collector/venue_l2_archiver.DISCONNECT_LOG_MARKER)."
    )
    block = _block(src, "venue_l2_checks")
    assert (
        "VENUE_L2_WS_DISCONNECT_MARKER" in block
        or "venue_l2_ws_disconnected" in block
    ), (
        "venue_l2 ws_reconnects check must pass "
        "log_marker=VENUE_L2_WS_DISCONNECT_MARKER (= 'venue_l2_ws_disconnected'); "
        "the default 'kalshi_ws_disconnected' substring would never match "
        "the venue-l2 recorder's journal lines (always-OK false negative)."
    )


def test_venue_l2_path_constants_defined():
    src = _src()
    assert (
        "VENUE_L2_BRONZE_ROOT" in src
        or "/var/lib/kalshi-venue-l2-collector" in src
    )
