"""D1.5 — `ops/kalshi-collector.service` systemd unit contract.

Ticket `86b9ypna4` (2026-05-16, REQUIRES-APPROVAL discipline tier).

Pins the structural shape of the NEW systemd unit that ships the
Data Corpus collector to production: isolation knobs (Nice,
MemoryMax, MemorySwapMax, LimitNOFILE; CPUAffinity retired
2026-05-19 per ticket 86ba12rv6 — see
``test_service_no_cpu_affinity_pinning``), lifecycle directives
(Restart, RestartSec, Type), identity (User, WorkingDirectory),
config seam (EnvironmentFile), entrypoint (ExecStart →
collector-start.sh), and standard wiring (network-online,
multi-user, journal logging).

L99 PARANOID lesson: pin every load-bearing directive at day-1 so a
future edit that loosens any isolation knob fires at the contract
gate instead of waiting for an adversarial round (or worse, a
production OOM / resource-contention incident).

The numeric values here are authoritative — they are the 3 D0.3 §12
operator decisions resolved at D1.5 kickoff:
  1. ``Restart=on-failure`` + ``RestartSec=10s`` (NOT ``Restart=always``
     with bot's 30s) — collector-specific lifecycle posture.
  2. ``Nice=10`` — I/O-bound, not real-time (D0.3 §12 item #2).
  3. ``EnvironmentFile=/home/botuser/.env.collector`` (NOT shared with
     bot's repo-rooted ``.env``) — D0.3 §6 isolation strengthened at
     D1.5 (the D1.1 stub used the shared file; this unit moves to the
     dedicated home-rooted env file so credential rotation for the
     collector key cannot disturb the bot).
"""
from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
UNIT_FILE = REPO_ROOT / "ops" / "kalshi-collector.service"


def _read_unit() -> str:
    assert UNIT_FILE.exists(), (
        f"{UNIT_FILE.relative_to(REPO_ROOT)} missing — D1.5 ships this "
        "file as the source of truth for the on-VPS collector systemd "
        "unit. Run `bash ops/install.sh` on the VPS after this lands."
    )
    return UNIT_FILE.read_text()


def _directive(text: str, key: str) -> str | None:
    """Return the value half of a ``Key=Value`` directive (first match)."""
    m = re.search(rf"^{re.escape(key)}=(.*)$", text, re.M)
    return m.group(1).strip() if m else None


def test_unit_file_exists_and_has_required_sections():
    text = _read_unit()
    assert re.search(r"^\[Unit\]", text, re.M), (
        "[Unit] section missing — file is not a valid systemd unit."
    )
    assert re.search(r"^\[Service\]", text, re.M), (
        "[Service] section missing — file is not a valid systemd unit."
    )
    assert re.search(r"^\[Install\]", text, re.M), (
        "[Install] section missing — `systemctl enable` would fail."
    )


def test_unit_after_and_wants_network_online():
    text = _read_unit()
    assert _directive(text, "After") == "network-online.target", (
        "After= must be network-online.target so the collector waits "
        "for IPv4 before attempting WS/REST to Kalshi."
    )
    assert _directive(text, "Wants") == "network-online.target", (
        "Wants= must be network-online.target to actually activate the "
        "network-online dependency (After= alone is ordering-only)."
    )


def test_service_type_is_simple():
    text = _read_unit()
    assert _directive(text, "Type") == "simple", (
        "Type=simple — collector forks no children at boot; systemd "
        "considers the unit started as soon as ExecStart spawns."
    )


def test_service_user_is_botuser():
    text = _read_unit()
    assert _directive(text, "User") == "botuser", (
        "User=botuser — collector runs under the same non-root user as "
        "the bot; matches the kalshi-bot.service convention so process "
        "permissions are uniform."
    )


def test_service_working_directory_is_repo_root():
    text = _read_unit()
    assert (
        _directive(text, "WorkingDirectory")
        == "/home/botuser/kalshi-bot-repo"
    ), (
        "WorkingDirectory must be the cloned repo root so relative "
        "paths in collector-start.sh resolve consistently with the bot."
    )


def test_service_environment_file_is_dedicated_collector_env():
    """EnvironmentFile must be /home/botuser/.env.collector — separate
    from the bot's /home/botuser/kalshi-bot-repo/.env.

    Why dedicated: rotating ``KALSHI_COLLECTOR_KEY_ID`` (D0.3 §12 item
    #3) does NOT need to touch the bot's `.env`; deploys that
    `git reset --hard origin/main` don't wipe the collector's
    credentials (since they live outside the repo); a compromised
    collector env doesn't leak the bot's API key.
    """
    text = _read_unit()
    assert (
        _directive(text, "EnvironmentFile")
        == "/home/botuser/.env.collector"
    ), (
        "EnvironmentFile must be /home/botuser/.env.collector (D0.3 §6 "
        "isolation, strengthened at D1.5). The D1.1 stub used the "
        "shared bot .env; D1.5 moves to a dedicated home-rooted env "
        "file so credential rotation cannot disturb the bot."
    )


def test_service_execstart_calls_collector_start_sh():
    text = _read_unit()
    assert (
        _directive(text, "ExecStart")
        == "/home/botuser/kalshi-bot-repo/collector-start.sh"
    ), (
        "ExecStart must invoke /home/botuser/kalshi-bot-repo/"
        "collector-start.sh (parallel to start.sh for the bot). "
        "Direct `python -m collector` would bypass the venv-activate + "
        "fail-fast posture documented at start.sh."
    )


def test_service_restart_on_failure_with_10s_backoff():
    """``Restart=on-failure`` + ``RestartSec=10s``.

    D0.3 §6's original example said ``Restart=always`` + ``RestartSec=30``
    (mirrored from kalshi-bot.service). D1.5 resolves operator
    decision: ``on-failure`` is the correct posture because a clean
    `systemctl stop` should NOT trigger a restart (operator may want
    to halt collection deliberately for credential rotation); a
    non-zero exit (PEM-missing, env-var-missing, fatal exception)
    SHOULD restart. 10s backoff avoids burning Kalshi API quota on
    rapid restart loops while still recovering within a minute of a
    transient blip.
    """
    text = _read_unit()
    assert _directive(text, "Restart") == "on-failure", (
        "Restart=on-failure (NOT always) — clean systemctl stop must "
        "halt the collector without auto-restart."
    )
    assert _directive(text, "RestartSec") in ("10", "10s"), (
        "RestartSec must be 10s (or bare 10). Faster than bot's 30s "
        "because collector restart is non-trading-critical; slower than "
        "1s to avoid restart-storm burning API quota."
    )


def test_service_no_cpu_affinity_pinning():
    """``CPUAffinity`` directive MUST be absent.

    RETIRED 2026-05-19 (ticket 86ba12rv6, umbrella 86ba12rf0). At
    754K+ tickers the single-vCPU pin saturated at 90% CPU on 1 core
    while vCPU-0 sat idle, driving ~33% sustained frame drop. The
    Nice=10 polite-background posture (`test_service_nice_is_10`)
    remains the load-bearing guarantee that bot scan latency cannot
    be starved by the collector; the kernel scheduler now floats both
    processes across both vCPUs. A future re-add of CPUAffinity must
    document why the scaling argument has reversed AND update
    `kb/decisions/collector-queue-saturation-fix-plan.md` Phase 2.
    """
    text = _read_unit()
    assert _directive(text, "CPUAffinity") is None, (
        "CPUAffinity directive must NOT be present — retired per "
        "ticket 86ba12rv6 (single-vCPU pin caused 33% frame drop "
        "at 754K-ticker universe). Re-adding requires RCA + plan "
        "doc update; see kb/decisions/collector-queue-saturation-"
        "fix-plan.md."
    )


def test_service_nice_is_10():
    """``Nice=10`` (NOT -19). Collector is I/O-bound, not real-time."""
    text = _read_unit()
    assert _directive(text, "Nice") == "10", (
        "Nice=10 — D0.3 §12 operator decision #2. -19 would steal "
        "cycles from the bot during synchronous zstd flushes; 0 would "
        "treat the collector as a peer of the bot; 10 is 'polite "
        "background'."
    )


def test_service_memory_high_is_1600m():
    """``MemoryHigh=1600M`` soft cgroup throttle before MemoryMax kill.

    Originally added 2026-05-20 (umbrella ``86ba12rf0``) at 400M to
    surface memory pressure on the 2GB VPS before the hard SIGKILL at
    512M. Raised to 1600M on 2026-05-20 (ticket ``86ba1h0cb``) in
    lockstep with the MemoryMax raise from 512M → 2048M, preserving
    the 78% MemoryHigh-of-MemoryMax ratio. The 4× scaling matches the
    DigitalOcean droplet resize from s-2vcpu-2gb → s-4vcpu-8gb the
    same day.

    Trade-off: throttling under MemoryHigh slows the collector
    (intentionally) — drain rate drops, queues fill faster, drops
    increase. Strictly preferable to a hard SIGKILL that loses all
    in_flight chunks (the B0/B1 salvage path recovers most, but not
    all, and adds boot latency).
    """
    text = _read_unit()
    assert _directive(text, "MemoryHigh") == "1600M", (
        "MemoryHigh=1600M — D0.3 §6 isolation enhancement (ticket "
        "86ba12rf0) raised in lockstep with MemoryMax 2048M (ticket "
        "86ba1h0cb). Soft throttle at 78% of MemoryMax provides a "
        "graceful-degradation warning before the hard SIGKILL at "
        "2048M. Removing this directive without a replacement memory-"
        "pressure surface is a regression."
    )


def test_service_memory_max_is_2048m():
    """``MemoryMax=2048M`` kernel-kills the collector before it OOMs the 8GB box.

    Raised from 512M → 2048M on 2026-05-20 (ticket ``86ba1h0cb``) in
    lockstep with the droplet resize s-2vcpu-2gb → s-4vcpu-8gb the
    same day. The 512M cap was sized against D0.2 §3.2's measured
    47 MB/conn × 7-8 conns = ~376 MB Python footprint on the 2GB box
    (~136 MB writer/uploader headroom). Post-resize universe-growth
    measurement at 705K tickers (2026-05-20, vs. D0.2's ~74K baseline
    + the runbook's 386K estimate) drove peak cgroup memory to 513M
    during the universal-mode boot, hitting the 512M cap. The 2048M
    cap on an 8GB physical box leaves ~5.5 GB physical headroom
    (bot + 4 collectors all at cap ~3 GB committed vs 8GB physical).
    """
    text = _read_unit()
    assert _directive(text, "MemoryMax") == "2048M", (
        "MemoryMax=2048M — D0.3 §6 isolation, raised 2026-05-20 "
        "(ticket 86ba1h0cb) from 512M in lockstep with the 8GB droplet "
        "resize. The 8GB VPS has 0 swap; OOM is still the failure mode "
        "but with 4× the headroom of the prior 2GB box."
    )


def test_service_memory_swap_max_is_zero():
    """``MemorySwapMax=0`` — explicit even though system swap is 0.

    Survives a future 'operator adds swap to make some other thing
    work' change. The collector should NEVER fall through to swap;
    OOM-kill + journalctl-loud is the intended failure surface, so
    operator alerting fires on the right signal.
    """
    text = _read_unit()
    assert _directive(text, "MemorySwapMax") == "0", (
        "MemorySwapMax=0 — D0.3 §6 isolation. Prevents the collector "
        "from silently paging if the operator ever adds swap."
    )


def test_service_limit_nofile_is_4096():
    """``LimitNOFILE=4096`` — generous over WS + rotation + rclone needs.

    6 conns × ~10 fds + 3 channels × 6 conns × 2 (current + rotating)
    + rclone ≤32 fd + headroom. Default 1024 would be tight under the
    6-conn × 3-channel layout.
    """
    text = _read_unit()
    assert _directive(text, "LimitNOFILE") == "4096", (
        "LimitNOFILE=4096 — D0.3 §6 isolation. Default 1024 is tight "
        "for 6 conns × 3 channels × rotation + rclone."
    )


def test_service_logs_to_journal():
    text = _read_unit()
    assert _directive(text, "StandardOutput") == "journal", (
        "StandardOutput=journal — collector logs visible via "
        "`journalctl -u kalshi-collector`, same as the bot."
    )
    assert _directive(text, "StandardError") == "journal", (
        "StandardError=journal — keep stderr in the same stream as "
        "stdout so timeline correlation works."
    )


def test_install_wantedby_multi_user():
    text = _read_unit()
    assert _directive(text, "WantedBy") == "multi-user.target", (
        "WantedBy=multi-user.target — enables the unit at boot, "
        "matching kalshi-bot.service."
    )


def test_unit_does_not_use_restart_always():
    """Negative pin: the D0.3 §6 example used ``Restart=always`` (bot's
    pattern). D1.5 operator-decided ``on-failure`` instead. Pin the
    decision so a future copy-paste from the bot unit can't silently
    flip the posture back.
    """
    text = _read_unit()
    assert "Restart=always" not in text, (
        "Restart=always detected — D1.5 deliberately chose on-failure "
        "(see test_service_restart_on_failure_with_10s_backoff). A "
        "future edit that flips this back to `always` must explicitly "
        "update the decision in kb/decisions/data-corpus-architecture.md."
    )
