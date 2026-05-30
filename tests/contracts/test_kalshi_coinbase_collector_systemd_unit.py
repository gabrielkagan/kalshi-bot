"""D2.5 — ``ops/kalshi-coinbase-collector.service`` systemd unit contract.

Ticket `86b9znq4w` (2026-05-18, REQUIRES-APPROVAL discipline tier).

Pins the structural shape of the SECOND collector systemd unit that
ships the Coinbase Exchange WS bronze archiver to production as a
separate process from the Kalshi-side ``kalshi-collector.service``
(D1.5, ticket 86b9ypna4). Mirror of
``test_kalshi_collector_systemd_unit.py`` adapted for the per-unit
deltas captured at D2.5 kickoff (Option B — separate unit per the
operator-decided full isolation posture).

Per-unit deltas vs ``kalshi-collector.service`` (D1.5):
  - Different ExecStart wrapper (``coinbase-collector-start.sh``).
  - Different EnvironmentFile (``/home/botuser/.env.coinbase-collector``
    — DEDICATED env file, NOT shared with the Kalshi side). Isolation
    parity with the Kalshi-side D1.5 dedicated-env convention.
  - NO ``CPUAffinity`` directive. Same posture as kalshi-collector
    post-retirement 2026-05-19 (ticket 86ba12rv6). The 2-vCPU VPS lets
    the kernel scheduler float all collector tenants across both
    vCPUs; Nice=10 (collectors) vs Nice=0 (bot) gates priority when
    CPU is contended. Coinbase single-conn light load is fine
    wherever it lands.
  - ``MemoryMax=384M`` (vs Kalshi's 2048M post-2026-05-20 raise;
    originally 256M at D2.5 ship, 512M at the very first cut).
    Coinbase single-conn × 9 products × 5 channels (ADA+BCH added
    2026-05-30 for the 9-asset corpus) is well under Kalshi's 7-conn
    × 21K-subs steady-state footprint; bumped 256M → 384M when the
    product count went 7 → 9 (measured 7-product peak 229M → ~295M
    projected at 9).
  - ``LimitNOFILE=512`` (vs Kalshi's 4096). 1 conn × 5 channels ×
    rotation + rclone needs ~30 fds; 512 gives ample headroom.

Same as Kalshi:
  - ``Restart=on-failure`` + ``RestartSec=10s`` lifecycle posture.
  - ``MemorySwapMax=0`` — same 2 GB / 0-swap VPS posture.
  - ``Nice=10`` polite-background I/O-bound posture.
  - ``User=botuser`` + ``WorkingDirectory=/home/botuser/kalshi-bot-repo``
    + journal logging + Wants/After network-online + Type=simple +
    WantedBy=multi-user.target.

L99 PARANOID lesson: pin every load-bearing directive at day-1 so a
future edit that loosens any isolation knob fires at the contract
gate instead of waiting for an adversarial round (or worse, a
production OOM / resource-contention incident).
"""
from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
UNIT_FILE = REPO_ROOT / "ops" / "kalshi-coinbase-collector.service"


def _read_unit() -> str:
    assert UNIT_FILE.exists(), (
        f"{UNIT_FILE.relative_to(REPO_ROOT)} missing — D2.5 ships this "
        "file as the source of truth for the on-VPS Coinbase collector "
        "systemd unit. Run `bash ops/install.sh` on the VPS after this lands."
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
        "for IPv4 before attempting WS to Coinbase."
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
        "User=botuser — Coinbase collector runs under the same non-root "
        "user as the bot + Kalshi collector; uniform process permissions."
    )


def test_service_working_directory_is_repo_root():
    text = _read_unit()
    assert (
        _directive(text, "WorkingDirectory")
        == "/home/botuser/kalshi-bot-repo"
    ), (
        "WorkingDirectory must be the cloned repo root so relative "
        "paths in coinbase-collector-start.sh resolve consistently."
    )


def test_service_environment_file_is_dedicated_coinbase_env():
    """EnvironmentFile must be /home/botuser/.env.coinbase-collector —
    separate from BOTH the bot's repo-rooted .env AND the Kalshi
    collector's .env.collector.

    Why dedicated: the Option B isolation posture (operator-decided at
    D2.5 kickoff) requires the Coinbase collector to have its own
    credential surface. While D2.1.5 narrowed scope to public-only
    channels (no auth needed today), the env file still carries
    deploy-tunable knobs (COINBASE_BRONZE_ROOT, RCLONE_REMOTE,
    S3_BUCKET) and ANY future private-channel Bit will land HMAC
    creds here without touching the Kalshi collector's env.
    """
    text = _read_unit()
    assert (
        _directive(text, "EnvironmentFile")
        == "/home/botuser/.env.coinbase-collector"
    ), (
        "EnvironmentFile must be /home/botuser/.env.coinbase-collector "
        "(D2.5 isolation — separate from Kalshi's .env.collector and "
        "bot's repo-rooted .env). Lives outside the repo so deploys "
        "cannot wipe Coinbase-side knobs."
    )


def test_service_execstart_calls_coinbase_collector_start_sh():
    text = _read_unit()
    assert (
        _directive(text, "ExecStart")
        == "/home/botuser/kalshi-bot-repo/coinbase-collector-start.sh"
    ), (
        "ExecStart must invoke /home/botuser/kalshi-bot-repo/"
        "coinbase-collector-start.sh (parallel to start.sh + "
        "collector-start.sh). Direct `python -m collector.coinbase_main_loop` "
        "would bypass the venv-activate + fail-fast posture documented "
        "at start.sh."
    )


def test_service_restart_on_failure_with_10s_backoff():
    """``Restart=on-failure`` + ``RestartSec=10s`` — same lifecycle
    posture as kalshi-collector (D1.5). A clean ``systemctl stop`` must
    NOT trigger a restart (operator may halt collection deliberately
    for env-file rotation); non-zero exits SHOULD restart after 10s.
    """
    text = _read_unit()
    assert _directive(text, "Restart") == "on-failure", (
        "Restart=on-failure (NOT always) — clean systemctl stop must "
        "halt the collector without auto-restart."
    )
    assert _directive(text, "RestartSec") in ("10", "10s"), (
        "RestartSec must be 10s (or bare 10). Same as kalshi-collector."
    )


def test_service_has_no_cpu_affinity_directive():
    """No ``CPUAffinity=`` — D2.5 operator-decided posture.

    Same posture as kalshi-collector post-retirement 2026-05-19 (ticket
    86ba12rv6). The 2-vCPU VPS lets the kernel scheduler float all
    collector tenants across both vCPUs; Nice=10 (collectors) vs
    Nice=0 (bot) gates priority when CPU is contended. Coinbase
    single-conn light load is fine wherever it lands.

    Negative pin (assert absent) so a future copy-paste from a hypothetical
    re-introduced affinity directive can't silently introduce
    ``CPUAffinity=`` here.
    """
    text = _read_unit()
    cpu_affinity = _directive(text, "CPUAffinity")
    assert cpu_affinity is None, (
        f"CPUAffinity directive found (value={cpu_affinity!r}); D2.5 "
        f"deliberately omits CPUAffinity so the kernel can float "
        f"Coinbase across either vCPU. If a future Bit ADDS pinning, "
        f"update this test in the same commit and document the choice "
        f"in kb/decisions/data-corpus-architecture.md."
    )


def test_service_nice_is_10():
    """``Nice=10`` — same I/O-bound polite-background posture as the
    Kalshi collector."""
    text = _read_unit()
    assert _directive(text, "Nice") == "10", (
        "Nice=10 — same posture as kalshi-collector. -19 would steal "
        "cycles from the bot during synchronous zstd flushes; 0 would "
        "treat the collector as a peer of the bot; 10 is 'polite "
        "background'."
    )


def test_service_memory_max_is_384m():
    """``MemoryMax=384M`` kernel-kills before OOMing the 8GB box.

    Kalshi collector raised from 512M → 2048M on 2026-05-20 (ticket
    86ba1h0cb) in lockstep with the s-2vcpu-2gb → s-4vcpu-8gb droplet
    resize. The Coinbase cap was 256M for single-conn × 7 products ×
    5 channels (post-D2.5 level2_batch promotion); bumped 256M → 384M
    on 2026-05-30 when ADA-USD + BCH-USD were added for the 9-asset
    corpus (the collector now collects 9 products; the bot still
    trades 7). Measured peak was 229M/256M (90%) at 7 products; 9
    products projects ~295M, so 384M restores headroom. Still far
    below Kalshi's 7-conn × ~21K-subs steady-state. Host has 5.4G
    free, so the per-unit cap is the only constraint. 384M leaves room
    for the worker queue (10K items × ~1KB envelope ≈ 10MB) + zstd
    compression buffer + rclone upload overhead.
    """
    text = _read_unit()
    assert _directive(text, "MemoryMax") == "384M", (
        "MemoryMax=384M — bumped from 256M on 2026-05-30 for the 9-asset "
        "corpus (ADA+BCH added). Coinbase single-conn is still "
        "structurally lighter than the Kalshi side. Sized against "
        "measured 7-product peak (229M) scaled to 9 products (~295M) + "
        "worker queue + zstd buffer + rclone overhead."
    )


def test_service_memory_swap_max_is_zero():
    """``MemorySwapMax=0`` — same as Kalshi collector. Survives a
    future 'operator adds swap' change; OOM-kill is the intended
    failure surface."""
    text = _read_unit()
    assert _directive(text, "MemorySwapMax") == "0", (
        "MemorySwapMax=0 — same posture as kalshi-collector. Prevents "
        "the Coinbase collector from silently paging if the operator "
        "ever adds swap."
    )


def test_service_limit_nofile_is_512():
    """``LimitNOFILE=512`` — 1 conn × 5 channels × rotation + rclone.

    Smaller than Kalshi's 4096 because Coinbase is structurally
    single-conn. The fd budget: 5 channels × 2 (in-flight + rotating
    outbox) + rclone process ≤32 + WS conn + headroom ≈ 50 fd
    typical; 512 gives ~10× headroom.
    """
    text = _read_unit()
    assert _directive(text, "LimitNOFILE") == "512", (
        "LimitNOFILE=512 — D2.5 isolation. Smaller than Kalshi's 4096 "
        "because Coinbase is single-conn. Default 1024 would actually "
        "be fine; 512 is a deliberate tighter bound to surface "
        "fd-leak regressions early."
    )


def test_service_logs_to_journal():
    text = _read_unit()
    assert _directive(text, "StandardOutput") == "journal", (
        "StandardOutput=journal — collector logs visible via "
        "`journalctl -u kalshi-coinbase-collector`, same convention "
        "as the bot + Kalshi collector."
    )
    assert _directive(text, "StandardError") == "journal", (
        "StandardError=journal — keep stderr in the same stream as "
        "stdout so timeline correlation works."
    )


def test_install_wantedby_multi_user():
    text = _read_unit()
    assert _directive(text, "WantedBy") == "multi-user.target", (
        "WantedBy=multi-user.target — enables the unit at boot, "
        "matching kalshi-bot.service + kalshi-collector.service."
    )


def test_unit_does_not_use_restart_always():
    """Negative pin: kalshi-bot.service uses ``Restart=always``; both
    collector units use ``on-failure`` per the D1.5 operator decision.
    Pin the decision so a future copy-paste from the bot unit cannot
    silently flip the posture back.
    """
    text = _read_unit()
    assert "Restart=always" not in text, (
        "Restart=always detected — D2.5 (mirroring D1.5) deliberately "
        "chose on-failure (see test_service_restart_on_failure_with_10s_backoff). "
        "A future edit that flips this back to `always` must explicitly "
        "update the decision in kb/decisions/data-corpus-architecture.md."
    )
