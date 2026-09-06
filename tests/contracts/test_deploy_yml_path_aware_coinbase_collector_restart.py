"""D2.5 — `.github/workflows/deploy.yml` SSH script MUST path-aware-restart
kalshi-coinbase-collector (ticket 86b9znq4w, 2026-05-18).

Mirror of `test_deploy_yml_path_aware_collector_restart.py` (D1.5.2 Kalshi-
side pin) for the Coinbase-side delta. Pre-D2.5 deploy.yml restarts
kalshi-bot + kalshi-collector but NEVER kalshi-coinbase-collector — every
Coinbase code change would require operator-issued manual restart. The
D2.5 block closes the manual-restart-tax class for the Coinbase side
exactly as D1.5.2 closed it for the Kalshi side.

Pins (mirror of D1.5.2 set):
  1. SSH script contains `systemctl restart kalshi-coinbase-collector`.
  2. Block gates on `systemctl is-active kalshi-coinbase-collector`
     (opt-out for operator-stopped unit).
  3. Path regex covers the 6 D2.5 affecting prefixes: `coinbase_wire/`,
     `collector/coinbase_archiver.py`, `collector/coinbase_main_loop.py`,
     `ops/kalshi-coinbase-collector.service`, `coinbase-collector-start.sh`,
     `requirements.txt`.
  4. Coinbase restart appears AFTER both the bot restart AND the Kalshi
     collector restart — the dependency direction is bot → kalshi-collector
     → kalshi-coinbase-collector (bot is primary, both collectors are
     secondary bronze-recorders).
  5. Diff base is `${{ github.event.before }}` (NOT `HEAD~1`).
  6. Null-SHA sentinel fallback to always-restart.
  7. `git diff` exit code captured + handled (not silently masked).
  8. `set +e` wraps the `git diff` substitution (so failing `$()` doesn't
     trip `set -euo pipefail` and abort the whole deploy script).
  9. Operator opt-out path emits a log line.
  10. Restart uses `sudo -n` (non-interactive).
  11. `git diff --name-only` (NOT bare `git diff`).

Independent diff capture (NOT reuse of COLLECTOR_DIFF_OUT from the Kalshi
block) is a deliberate design choice — the two blocks have zero shared
state so a future edit to one cannot regress the other.
"""
from __future__ import annotations

from pathlib import Path


_DEPLOY_YML = (
    Path(__file__).resolve().parents[2] / ".github" / "workflows" / "deploy.yml"
)


def _ssh_script_lines() -> list[str]:
    """Extract the SSH script body lines from deploy.yml.

    Mirror of helpers in sibling deploy.yml contract tests — kept
    file-local because the two D1.5.x and D2.5 tests evolve
    independently and shared mutation would tighten the coupling.
    """
    text = _DEPLOY_YML.read_text()
    lines = text.splitlines()
    in_script = False
    script_indent = None
    out: list[str] = []
    for line in lines:
        if "script: |" in line:
            in_script = True
            script_indent = len(line) - len(line.lstrip())
            continue
        if not in_script:
            continue
        if not line.strip():
            out.append("")
            continue
        line_indent = len(line) - len(line.lstrip())
        if line_indent <= (script_indent or 0):
            break
        out.append(line)
    assert out, "deploy.yml SSH script body could not be extracted."
    return out


def _line_index_containing(lines: list[str], needle: str) -> int:
    """First non-comment line containing the substring. -1 if absent."""
    for i, line in enumerate(lines):
        if line.lstrip().startswith("#"):
            continue
        if needle in line:
            return i
    return -1


def test_deploy_yml_ssh_script_contains_coinbase_collector_restart():
    """The SSH script MUST contain `systemctl restart kalshi-coinbase-collector`.

    Without this, deploy.yml never auto-restarts the Coinbase collector
    and the manual-restart tax (closed for Kalshi side by D1.5.2)
    re-opens for Coinbase changes.
    """
    script = _ssh_script_lines()
    idx = _line_index_containing(
        script, "systemctl restart kalshi-coinbase-collector",
    )
    assert idx >= 0, (
        "deploy.yml SSH script missing `systemctl restart "
        "kalshi-coinbase-collector`. Without this, every Coinbase code "
        "change requires operator manual restart. Add a path-aware "
        "block that fires when any of {coinbase_wire/**, "
        "collector/coinbase_archiver.py, collector/coinbase_main_loop.py, "
        "ops/kalshi-coinbase-collector.service, coinbase-collector-start.sh, "
        "requirements.txt} changed."
    )


def test_deploy_yml_coinbase_collector_restart_gated_by_systemctl_is_active():
    """The Coinbase collector restart MUST be gated by `systemctl is-active
    kalshi-coinbase-collector` so an operator-stopped unit is NOT
    auto-restarted by deploy.yml.
    """
    script = _ssh_script_lines()
    has_gate = any(
        ("is-active" in line and "kalshi-coinbase-collector" in line)
        and not line.lstrip().startswith("#")
        for line in script
    )
    assert has_gate, (
        "deploy.yml Coinbase collector restart MUST be gated by "
        "`systemctl is-active kalshi-coinbase-collector` so an operator-"
        "stopped collector is NOT restarted by auto-deploy. Pattern: "
        "`if systemctl is-active --quiet kalshi-coinbase-collector; "
        "then sudo -n /bin/systemctl restart kalshi-coinbase-collector; fi`."
    )


def test_deploy_yml_coinbase_collector_restart_path_set_covers_all_6_prefixes():
    """The path regex MUST cover ALL 6 D2.5 affecting prefixes.

    Missing any one creates a silent-skip class — exactly the manual-
    restart-tax bug D1.5.2 closed for Kalshi side and D2.5 closes for
    Coinbase side.
    """
    script_text = "\n".join(_ssh_script_lines())
    required_prefixes = [
        "coinbase_wire/",
        "collector/coinbase_archiver.py",
        "collector/coinbase_main_loop.py",
        "ops/kalshi-coinbase-collector.service",
        "coinbase-collector-start.sh",
        "requirements.txt",
    ]
    missing = [p for p in required_prefixes if p not in script_text]
    assert not missing, (
        f"deploy.yml Coinbase collector restart path set MISSING prefixes "
        f"{missing}. All 6 of {required_prefixes} must appear in the "
        f"regex/grep pattern. Missing any one creates a silent-skip class."
    )


def test_deploy_yml_coinbase_collector_restart_runs_after_kalshi_collector_restart():
    """The Coinbase collector restart MUST appear AFTER both the bot
    restart AND the Kalshi collector restart.

    Dependency direction: bot is primary; both collectors are secondary
    bronze-recorders; the Coinbase collector is the newest addition.
    Ordering keeps the most-load-bearing units restarting first; a
    Coinbase restart failure cannot hijack the bot or Kalshi collector
    restart paths.
    """
    script = _ssh_script_lines()
    bot_idx = _line_index_containing(script, "systemctl restart kalshi-bot")
    # Kalshi-collector restart line: distinguish from kalshi-coinbase-collector
    # by requiring the substring NOT to be followed by '-coinbase'.
    kalshi_collector_idx = -1
    coinbase_collector_idx = -1
    for i, line in enumerate(script):
        if line.lstrip().startswith("#"):
            continue
        if "systemctl restart kalshi-collector" in line and "kalshi-coinbase-collector" not in line:
            if kalshi_collector_idx == -1:
                kalshi_collector_idx = i
        if "systemctl restart kalshi-coinbase-collector" in line:
            if coinbase_collector_idx == -1:
                coinbase_collector_idx = i

    assert bot_idx >= 0, (
        "deploy.yml SSH script missing `systemctl restart kalshi-bot`."
    )
    assert kalshi_collector_idx >= 0, (
        "deploy.yml SSH script missing kalshi-collector restart line "
        "(D1.5.2 invariant). The D2.5 ordering pin requires both Kalshi "
        "collector and Coinbase collector restart blocks to be present."
    )
    assert coinbase_collector_idx >= 0, (
        "kalshi-coinbase-collector restart line missing (covered by "
        "sister test)."
    )
    assert bot_idx < kalshi_collector_idx < coinbase_collector_idx, (
        f"Ordering invariant violated. Expected bot ({bot_idx}) < "
        f"kalshi-collector ({kalshi_collector_idx}) < "
        f"kalshi-coinbase-collector ({coinbase_collector_idx}). "
        f"Reversed ordering risks restart hijack on the earlier unit."
    )


def test_deploy_yml_coinbase_collector_restart_handles_null_sha_initial_push():
    """The script MUST detect the 40-zero sentinel and fall back to
    always-restart (R1-M1 D1.5.2 lesson carried forward).
    """
    script_text = "\n".join(_ssh_script_lines())
    null_sha = "0" * 40
    # The null-SHA literal MUST appear at least twice now (D1.5.2 + D2.5
    # blocks). Pre-D2.5 it appeared once; post-D2.5 it MUST appear in
    # the Coinbase block too. Count occurrences to defend the post-
    # promotion invariant.
    occurrences = script_text.count(null_sha)
    assert occurrences >= 2, (
        f"deploy.yml contains null-SHA sentinel only {occurrences} times; "
        f"expected ≥ 2 (D1.5.2 Kalshi block + D2.5 Coinbase block, each "
        f"with their own independent null-SHA fallback). Bumping to 1 "
        f"means the Coinbase block dropped the null-SHA check; bumping "
        f"to 0 means both did."
    )


def test_deploy_yml_coinbase_collector_restart_handles_git_diff_failure():
    """The Coinbase block MUST capture + inspect `git diff` exit code.

    Mirror of R1-M2 (D1.5.2) for the Coinbase side — pattern: `OUT=$(git
    diff ... 2>&1); EXIT=$?; if [ $EXIT -ne 0 ]; then ... fallback ...`.
    """
    script_text = "\n".join(_ssh_script_lines())
    # The Coinbase block uses COINBASE_DIFF_EXIT (Kalshi uses
    # COLLECTOR_DIFF_EXIT). Defend that the Coinbase-specific exit var
    # is present so a future refactor that DROPS the Coinbase block
    # cannot pass solely on the Kalshi block's exit-capture.
    assert "COINBASE_DIFF_EXIT" in script_text, (
        "deploy.yml MUST have a Coinbase-specific exit-code capture "
        "variable (e.g., COINBASE_DIFF_EXIT). Otherwise a future "
        "refactor that drops the Coinbase block could still pass this "
        "test on the Kalshi block's COLLECTOR_DIFF_EXIT alone."
    )


def test_deploy_yml_coinbase_collector_restart_diff_uses_set_minus_e_wrap():
    """The Coinbase `git diff` substitution MUST run with `set +e`
    (mirror of R2-C1 D1.5.2 lesson).

    Without `set +e`, under `set -euo pipefail`, a failing `$()` aborts
    the WHOLE deploy script and the fallback path is unreachable.
    """
    script = _ssh_script_lines()
    diff_idx = -1
    for i, line in enumerate(script):
        if line.lstrip().startswith("#"):
            continue
        if "COINBASE_DIFF_OUT=$(git diff" in line:
            diff_idx = i
            break
    assert diff_idx >= 0, (
        "Couldn't locate `COINBASE_DIFF_OUT=$(git diff` capture line — "
        "structure changed since D2.5 ship or block was renamed."
    )
    set_plus_e_before = any(
        "set +e" in script[i]
        for i in range(max(0, diff_idx - 5), diff_idx)
    )
    assert set_plus_e_before, (
        f"Coinbase git diff capture at line {diff_idx} is NOT preceded "
        f"by `set +e` — under `set -euo pipefail`, the failing-"
        f"substitution case would abort the script. Pattern: `set +e; "
        f"COINBASE_DIFF_OUT=$(...); COINBASE_DIFF_EXIT=$?; set -e`."
    )
    set_minus_e_after = any(
        "set -e" in script[i] and "set +e" not in script[i]
        for i in range(diff_idx, min(len(script), diff_idx + 5))
    )
    assert set_minus_e_after, (
        f"Coinbase git diff capture at line {diff_idx} is NOT followed "
        f"by `set -e` re-enable within 5 lines — `set +e` leaks beyond "
        f"the intended scope."
    )


def test_deploy_yml_coinbase_collector_restart_logs_operator_opt_out():
    """When kalshi-coinbase-collector is stopped (operator opt-out), the
    Coinbase block MUST log the skip — same observability invariant as
    the Kalshi side (R1-N3 D1.5.2 closure).
    """
    script_text = "\n".join(_ssh_script_lines())
    # Look for a log line that mentions kalshi-coinbase-collector AND
    # uses a stop-intent phrase. Mirrors the Kalshi-side phrasing.
    has_optout_log = any(
        ("operator stopped" in line.lower() or "preserve operator intent" in line.lower())
        and "coinbase" in line.lower()
        and not line.lstrip().startswith("#")
        for line in script_text.splitlines()
    )
    assert has_optout_log, (
        "deploy.yml MUST log the skip when kalshi-coinbase-collector is "
        "stopped (operator opt-out path). Phrase must mention 'coinbase' "
        "to distinguish from the Kalshi block's opt-out log."
    )


def test_deploy_yml_coinbase_collector_restart_uses_sudo_dash_n():
    """The Coinbase restart MUST use `sudo -n` (non-interactive).

    Mirror of R3-C1 (D1.5.2) for the Coinbase side. appleboy/ssh-action
    has no tty; bare `sudo` either hangs or errors with `sudo: a
    terminal is required`. `sudo -n` fails LOUD if sudoers NOPASSWD
    has not been extended to include kalshi-coinbase-collector — the
    operator pre-deploy action documented in ops/CLAUDE.md.
    """
    script = _ssh_script_lines()
    restart_line = None
    for line in script:
        if line.lstrip().startswith("#"):
            continue
        if "restart kalshi-coinbase-collector" in line and "is-active" not in line:
            restart_line = line
            break
    assert restart_line is not None, (
        "Could not locate the kalshi-coinbase-collector restart command "
        "line (sister tests should already have flagged this)."
    )
    assert "sudo -n" in restart_line, (
        f"deploy.yml kalshi-coinbase-collector restart line "
        f"({restart_line.strip()!r}) MUST use `sudo -n` (non-interactive). "
        f"Bare `sudo` either hangs or errors in appleboy/ssh-action. "
        f"Pattern: `sudo -n /bin/systemctl restart kalshi-coinbase-collector`."
    )


def test_deploy_yml_coinbase_collector_uses_git_diff_name_only():
    """The Coinbase path-aware detection MUST use `git diff --name-only`
    (mirror of the Kalshi-side pin).

    Without `--name-only`, the diff output includes hunk metadata that
    could false-positive the path regex.
    """
    script = _ssh_script_lines()
    # The Coinbase-specific capture line.
    has_diff_name_only = any(
        "COINBASE_DIFF_OUT=$(git diff --name-only" in line
        and not line.lstrip().startswith("#")
        for line in script
    )
    assert has_diff_name_only, (
        "deploy.yml Coinbase collector restart MUST use `git diff "
        "--name-only` for path detection (mirror of D1.5.2 Kalshi pin). "
        "Pattern: `COINBASE_DIFF_OUT=$(git diff --name-only ...)`."
    )


def test_deploy_yml_coinbase_unit_drift_guard_precedes_restart():
    """2026-05-30: when the Coinbase UNIT FILE itself changed, deploy.yml
    MUST verify the on-VPS installed unit matches the deploy-commit unit
    BEFORE restarting — and FAIL LOUD (pointing at ops/install.sh) on drift.

    deploy.yml does NOT cp units / daemon-reload (that is install.sh's job),
    so a bare `systemctl restart` after a MemoryMax/cap change would run the
    unit against its STALE on-disk cap. For the 9-asset corpus bump
    (256M→384M, 2026-05-30) that means restarting the then-9-product collector
    against the old 256M cap → OOM-kill window. This guard mirrors the
    kalshi-bot unit-drift check (`systemctl cat` + `diff` + fail-with-recovery)
    and refuses to restart against a stale unit. No new sudoers needed (read +
    compare + exit 1).
    """
    script = _ssh_script_lines()
    cat_idx = _line_index_containing(script, "systemctl cat kalshi-coinbase-collector")
    restart_idx = _line_index_containing(
        script, "systemctl restart kalshi-coinbase-collector"
    )
    assert cat_idx >= 0, (
        "deploy.yml MUST read the installed Coinbase unit via `systemctl cat "
        "kalshi-coinbase-collector` to drift-check it before restart. Without "
        "this, a cap/MemoryMax change restarts against the STALE on-disk unit."
    )
    assert restart_idx >= 0, "Coinbase restart line missing."
    assert cat_idx < restart_idx, (
        "The unit-drift guard (`systemctl cat`) MUST appear BEFORE the "
        "`systemctl restart kalshi-coinbase-collector` line — otherwise the "
        "stale-cap restart fires before the guard can abort it."
    )
    joined = "\n".join(
        l for l in script if not l.lstrip().startswith("#")
    )
    assert "ops/kalshi-coinbase-collector.service" in joined, (
        "The drift guard must compare against the deploy-commit "
        "ops/kalshi-coinbase-collector.service."
    )
    assert "install.sh" in joined, (
        "The drift-guard failure message MUST point the operator at "
        "`bash ops/install.sh` for recovery (cp unit + daemon-reload)."
    )
