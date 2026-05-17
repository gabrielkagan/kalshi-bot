"""Layer 3.5 of orphan prevention — mid-session detection via the
existing 2-min `ops/watchdog.py` cron.

Layer 3 (`bot/_impl.py:detect_orphan_db_holders`) runs at bot startup. If
an orphan H-4 backfill spawns DURING bot uptime (operator manually
triggers `gh workflow run h4_backfill.yml -f source=...` mid-day,
script orphans itself), Layer 3 won't catch it until the next bot
restart. This file extends `ops/watchdog.py` (already runs every 2 min
via systemd cron) with the same orphan check, so mid-session
orphans are detected within ≤2 min.

Adversarial-review C-8 from the Layer 3 review.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))


def test_watchdog_check_orphan_db_holders_returns_no_offenders_clean(
    monkeypatch, tmp_path,
):
    """Healthy state: lsof shows only the bot process and the
    watchdog itself. No orphan alerts."""
    import ops.watchdog as watchdog
    db = tmp_path / "state.db"
    db.touch()

    sent: list = []
    monkeypatch.setattr(
        watchdog, "send_telegram",
        lambda msg: sent.append(msg),
    )
    # `lsof` returns the bot PID + the watchdog's own PID, neither of
    # which is in the orphan positive-list.
    monkeypatch.setattr(
        watchdog, "_run_lsof_for_db",
        lambda path: [12345, os.getpid()],
    )
    monkeypatch.setattr(
        watchdog, "_get_pid_cmdline",
        lambda pid: "venv/bin/python3 bot/_impl.py" if pid == 12345 else "ops/watchdog.py",
    )

    msg = watchdog.check_orphan_db_holders(str(db))
    assert msg is None, f"healthy state must return None; got {msg!r}"
    assert sent == [], f"no Telegram alert when healthy; got {sent}"


def test_watchdog_check_orphan_db_holders_alerts_on_h4_backfill(
    monkeypatch, tmp_path,
):
    """Orphan H-4c (cryptocompare_news_backfill) PID holding state.db
    → alert returned for caller to send."""
    import ops.watchdog as watchdog
    db = tmp_path / "state.db"
    db.touch()

    monkeypatch.setattr(
        watchdog, "_run_lsof_for_db",
        lambda path: [99999],  # only the orphan
    )
    monkeypatch.setattr(
        watchdog, "_get_pid_cmdline",
        lambda pid: "python3 scripts/backfill/cryptocompare_news_backfill.py --db state.db",
    )
    # Liveness probe: pretend the orphan is alive (real os.kill on
    # fake PID would raise ProcessLookupError → no alert).
    monkeypatch.setattr(os, "kill", lambda pid, sig: None)

    msg = watchdog.check_orphan_db_holders(str(db))
    assert msg is not None, "must return alert text for orphan"
    assert "ORPHAN" in msg.upper() or "orphan" in msg
    assert "99999" in msg
    assert "cryptocompare_news_backfill" in msg


def test_watchdog_check_orphan_db_holders_skips_legitimate_processes(
    monkeypatch, tmp_path,
):
    """Same positive-list as Layer 3 — must NOT alert on the bot's
    own state.db conn or on the watchdog itself or on bot/ai/auditor.py /
    audit_cron.py / dashboard_snapshot.py."""
    import ops.watchdog as watchdog
    db = tmp_path / "state.db"
    db.touch()

    legit_pids = {
        2001: "venv/bin/python3 bot/_impl.py",
        2002: "venv/bin/python3 ops/watchdog.py",
        2003: "venv/bin/python3 bot/ai/auditor.py",
        2004: "venv/bin/python3 scripts/audit/audit_cron.py --db state.db",
        2005: "venv/bin/python3 dashboard_snapshot.py",
    }
    monkeypatch.setattr(
        watchdog, "_run_lsof_for_db",
        lambda path: list(legit_pids.keys()),
    )
    monkeypatch.setattr(
        watchdog, "_get_pid_cmdline",
        lambda pid: legit_pids.get(pid, ""),
    )

    msg = watchdog.check_orphan_db_holders(str(db))
    assert msg is None, (
        f"must NOT alert on legitimate cron processes (alert fatigue "
        f"prevention from adversarial-review C-1); got: {msg!r}"
    )


def test_watchdog_check_orphan_db_holders_handles_lsof_missing(
    monkeypatch, tmp_path,
):
    """If `lsof` is not installed, the check must return None
    silently (defense-in-depth that has zero observability of its
    own health is acceptable here because Layer 3 in bot/_impl.py emits
    its own one-shot Telegram if lsof is missing)."""
    import ops.watchdog as watchdog
    db = tmp_path / "state.db"
    db.touch()

    def _lsof_fail(path):
        raise FileNotFoundError("lsof not found")

    monkeypatch.setattr(watchdog, "_run_lsof_for_db", _lsof_fail)
    msg = watchdog.check_orphan_db_holders(str(db))
    assert msg is None, "must not crash watchdog if lsof missing"


def test_watchdog_main_calls_check_orphan_db_holders(monkeypatch):
    """Regression: watchdog.main() must call
    check_orphan_db_holders so the 2-min cron actually exercises
    the new check. Verified via AST scan."""
    import ast
    src = (PROJECT_ROOT / "ops" / "watchdog.py").read_text()
    tree = ast.parse(src)
    main_fn = None
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "main":
            main_fn = node
            break
    assert main_fn is not None, "watchdog.main() not found"
    found = False
    for node in ast.walk(main_fn):
        if isinstance(node, ast.Call):
            fn = node.func
            name = (
                fn.attr if isinstance(fn, ast.Attribute)
                else fn.id if isinstance(fn, ast.Name)
                else None
            )
            if name == "check_orphan_db_holders":
                found = True
                break
    assert found, (
        "watchdog.main() must call check_orphan_db_holders "
        "(Layer 3.5 of orphan prevention; adversarial-review C-8 "
        "from Layer 3 review)"
    )
