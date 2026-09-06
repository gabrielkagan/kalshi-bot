"""86bbvqhyr (2026-09-06) — `.github/workflows/deploy.yml` SSH script MUST
path-aware-restart kalshi-espn-collector.

Found at R4 of the ESPN-403 fix's adversarial gate: deploy.yml restarted
kalshi-bot + kalshi-collector + kalshi-coinbase-collector but NEVER
kalshi-espn-collector, so the collector half of a fix that touches
`collector/espn_archiver.py` would keep running the OLD process (old
User-Agent, no `espn_http_status_1h` sidecar key) while the deploy log
looked green — the same "looks deployed, isn't" class the ticket exists
to close. Mirror of `test_deploy_yml_path_aware_coinbase_collector_restart.py`
(D2.5) with the ESPN delta.

Pins (mirror of the D2.5 set):
  1. SSH script contains `systemctl restart kalshi-espn-collector`.
  2. Block gates on `systemctl is-active kalshi-espn-collector`.
  3. Path regex covers the 8 affecting paths: `collector/espn_archiver.py`,
     `collector/espn_main_loop.py`, `collector/writer.py`,
     `collector/uploader.py`, `kalshi_wire/ws_client.py` (build_envelope),
     `ops/kalshi-espn-collector.service`, `espn-collector-start.sh`,
     `requirements.txt`.
  4. ESPN restart appears AFTER the bot + Kalshi collector restarts.
  5-11. Same diff-base / null-SHA / exit-code / set +e / opt-out log /
     sudo -n / --name-only pins as D2.5, on the independent ESPN_* vars.
  12. Unit-drift guard precedes the restart (stale on-disk unit → FAIL).
VPS sudoers NOPASSWD for `/bin/systemctl restart kalshi-espn-collector`
was verified present on 2026-09-06 (read-only `sudo -n -l`).
"""
from __future__ import annotations

from pathlib import Path


_DEPLOY_YML = (
    Path(__file__).resolve().parents[2] / ".github" / "workflows" / "deploy.yml"
)


def _ssh_script_lines() -> list[str]:
    """Extract the SSH script body lines from deploy.yml.

    Mirror of helpers in sibling deploy.yml contract tests — kept
    file-local because the two D1.5.x and 86bbvqhyr tests evolve
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


def test_deploy_yml_ssh_script_contains_espn_collector_restart():
    """The SSH script MUST contain `systemctl restart kalshi-espn-collector`.

    Without this, deploy.yml never auto-restarts the ESPN collector
    and the manual-restart tax (closed for Kalshi side by D1.5.2)
    re-opens for ESPN changes.
    """
    script = _ssh_script_lines()
    idx = _line_index_containing(
        script, "systemctl restart kalshi-espn-collector",
    )
    assert idx >= 0, (
        "deploy.yml SSH script missing `systemctl restart "
        "kalshi-espn-collector`. Without this, every ESPN code "
        "change requires operator manual restart. Add a path-aware "
        "block that fires when any of {espn_wire/**, "
        "collector/espn_archiver.py, collector/espn_main_loop.py, "
        "ops/kalshi-espn-collector.service, espn-collector-start.sh, "
        "requirements.txt} changed."
    )


def test_deploy_yml_espn_collector_restart_gated_by_systemctl_is_active():
    """The ESPN collector restart MUST be gated by `systemctl is-active
    kalshi-espn-collector` so an operator-stopped unit is NOT
    auto-restarted by deploy.yml.
    """
    script = _ssh_script_lines()
    has_gate = any(
        ("is-active" in line and "kalshi-espn-collector" in line)
        and not line.lstrip().startswith("#")
        for line in script
    )
    assert has_gate, (
        "deploy.yml ESPN collector restart MUST be gated by "
        "`systemctl is-active kalshi-espn-collector` so an operator-"
        "stopped collector is NOT restarted by auto-deploy. Pattern: "
        "`if systemctl is-active --quiet kalshi-espn-collector; "
        "then sudo -n /bin/systemctl restart kalshi-espn-collector; fi`."
    )


def test_deploy_yml_espn_collector_restart_path_set_covers_all_8_prefixes():
    """The path regex MUST cover ALL 8 86bbvqhyr affecting prefixes.

    Missing any one creates a silent-skip class — exactly the manual-
    restart-tax bug D1.5.2 closed for Kalshi side and 86bbvqhyr closes for
    ESPN side.
    """
    script_text = "\n".join(_ssh_script_lines())
    required_prefixes = [
        "collector/espn_archiver.py",
        "collector/espn_main_loop.py",
        "collector/writer.py",
        "collector/uploader.py",
        "kalshi_wire/ws_client.py",
        "ops/kalshi-espn-collector.service",
        "espn-collector-start.sh",
        "requirements.txt",
    ]
    missing = [p for p in required_prefixes if p not in script_text]
    assert not missing, (
        f"deploy.yml ESPN collector restart path set MISSING prefixes "
        f"{missing}. All 8 of {required_prefixes} must appear in the "
        f"regex/grep pattern. Missing any one creates a silent-skip class."
    )


def test_deploy_yml_espn_collector_restart_runs_after_kalshi_collector_restart():
    """The ESPN collector restart MUST appear AFTER both the bot
    restart AND the Kalshi collector restart.

    Dependency direction: bot is primary; both collectors are secondary
    bronze-recorders; the ESPN collector is the newest addition.
    Ordering keeps the most-load-bearing units restarting first; a
    ESPN restart failure cannot hijack the bot or Kalshi collector
    restart paths.
    """
    script = _ssh_script_lines()
    bot_idx = _line_index_containing(script, "systemctl restart kalshi-bot")
    # Kalshi-collector restart line: distinguish from kalshi-espn-collector
    # by requiring the substring NOT to be followed by '-espn'.
    kalshi_collector_idx = -1
    espn_collector_idx = -1
    for i, line in enumerate(script):
        if line.lstrip().startswith("#"):
            continue
        if "systemctl restart kalshi-collector" in line and "kalshi-espn-collector" not in line:
            if kalshi_collector_idx == -1:
                kalshi_collector_idx = i
        if "systemctl restart kalshi-espn-collector" in line:
            if espn_collector_idx == -1:
                espn_collector_idx = i

    assert bot_idx >= 0, (
        "deploy.yml SSH script missing `systemctl restart kalshi-bot`."
    )
    assert kalshi_collector_idx >= 0, (
        "deploy.yml SSH script missing kalshi-collector restart line "
        "(D1.5.2 invariant). The 86bbvqhyr ordering pin requires both Kalshi "
        "collector and ESPN collector restart blocks to be present."
    )
    assert espn_collector_idx >= 0, (
        "kalshi-espn-collector restart line missing (covered by "
        "sister test)."
    )
    assert bot_idx < kalshi_collector_idx < espn_collector_idx, (
        f"Ordering invariant violated. Expected bot ({bot_idx}) < "
        f"kalshi-collector ({kalshi_collector_idx}) < "
        f"kalshi-espn-collector ({espn_collector_idx}). "
        f"Reversed ordering risks restart hijack on the earlier unit."
    )


def test_deploy_yml_espn_collector_restart_handles_null_sha_initial_push():
    """The script MUST detect the 40-zero sentinel and fall back to
    always-restart (R1-M1 D1.5.2 lesson carried forward).
    """
    script_text = "\n".join(_ssh_script_lines())
    null_sha = "0" * 40
    # The null-SHA literal MUST appear at least twice now (D1.5.2 + 86bbvqhyr
    # blocks). Pre-86bbvqhyr it appeared once; post-86bbvqhyr it MUST appear in
    # the ESPN block too. Count occurrences to defend the post-
    # promotion invariant.
    occurrences = script_text.count(null_sha)
    assert occurrences >= 2, (
        f"deploy.yml contains null-SHA sentinel only {occurrences} times; "
        f"expected ≥ 2 (D1.5.2 Kalshi block + 86bbvqhyr ESPN block, each "
        f"with their own independent null-SHA fallback). Bumping to 1 "
        f"means the ESPN block dropped the null-SHA check; bumping "
        f"to 0 means both did."
    )


def test_deploy_yml_espn_collector_restart_handles_git_diff_failure():
    """The ESPN block MUST capture + inspect `git diff` exit code.

    Mirror of R1-M2 (D1.5.2) for the ESPN side — pattern: `OUT=$(git
    diff ... 2>&1); EXIT=$?; if [ $EXIT -ne 0 ]; then ... fallback ...`.
    """
    script_text = "\n".join(_ssh_script_lines())
    # The ESPN block uses ESPN_DIFF_EXIT (Kalshi uses
    # COLLECTOR_DIFF_EXIT). Defend that the ESPN-specific exit var
    # is present so a future refactor that DROPS the ESPN block
    # cannot pass solely on the Kalshi block's exit-capture.
    assert "ESPN_DIFF_EXIT" in script_text, (
        "deploy.yml MUST have a ESPN-specific exit-code capture "
        "variable (e.g., ESPN_DIFF_EXIT). Otherwise a future "
        "refactor that drops the ESPN block could still pass this "
        "test on the Kalshi block's COLLECTOR_DIFF_EXIT alone."
    )


def test_deploy_yml_espn_collector_restart_diff_uses_set_minus_e_wrap():
    """The ESPN `git diff` substitution MUST run with `set +e`
    (mirror of R2-C1 D1.5.2 lesson).

    Without `set +e`, under `set -euo pipefail`, a failing `$()` aborts
    the WHOLE deploy script and the fallback path is unreachable.
    """
    script = _ssh_script_lines()
    diff_idx = -1
    for i, line in enumerate(script):
        if line.lstrip().startswith("#"):
            continue
        if "ESPN_DIFF_OUT=$(git diff" in line:
            diff_idx = i
            break
    assert diff_idx >= 0, (
        "Couldn't locate `ESPN_DIFF_OUT=$(git diff` capture line — "
        "structure changed since 86bbvqhyr ship or block was renamed."
    )
    set_plus_e_before = any(
        "set +e" in script[i]
        for i in range(max(0, diff_idx - 5), diff_idx)
    )
    assert set_plus_e_before, (
        f"ESPN git diff capture at line {diff_idx} is NOT preceded "
        f"by `set +e` — under `set -euo pipefail`, the failing-"
        f"substitution case would abort the script. Pattern: `set +e; "
        f"ESPN_DIFF_OUT=$(...); ESPN_DIFF_EXIT=$?; set -e`."
    )
    set_minus_e_after = any(
        "set -e" in script[i] and "set +e" not in script[i]
        for i in range(diff_idx, min(len(script), diff_idx + 5))
    )
    assert set_minus_e_after, (
        f"ESPN git diff capture at line {diff_idx} is NOT followed "
        f"by `set -e` re-enable within 5 lines — `set +e` leaks beyond "
        f"the intended scope."
    )


def test_deploy_yml_espn_collector_restart_logs_operator_opt_out():
    """When kalshi-espn-collector is stopped (operator opt-out), the
    ESPN block MUST log the skip — same observability invariant as
    the Kalshi side (R1-N3 D1.5.2 closure).
    """
    script_text = "\n".join(_ssh_script_lines())
    # Look for a log line that mentions kalshi-espn-collector AND
    # uses a stop-intent phrase. Mirrors the Kalshi-side phrasing.
    has_optout_log = any(
        ("operator stopped" in line.lower() or "preserve operator intent" in line.lower())
        and "espn" in line.lower()
        and not line.lstrip().startswith("#")
        for line in script_text.splitlines()
    )
    assert has_optout_log, (
        "deploy.yml MUST log the skip when kalshi-espn-collector is "
        "stopped (operator opt-out path). Phrase must mention 'espn' "
        "to distinguish from the Kalshi block's opt-out log."
    )


def test_deploy_yml_espn_collector_restart_uses_sudo_dash_n():
    """The ESPN restart MUST use `sudo -n` (non-interactive).

    Mirror of R3-C1 (D1.5.2) for the ESPN side. appleboy/ssh-action
    has no tty; bare `sudo` either hangs or errors with `sudo: a
    terminal is required`. `sudo -n` fails LOUD if sudoers NOPASSWD
    has not been extended to include kalshi-espn-collector — the
    operator pre-deploy action documented in ops/CLAUDE.md.
    """
    script = _ssh_script_lines()
    restart_line = None
    for line in script:
        if line.lstrip().startswith("#"):
            continue
        if "restart kalshi-espn-collector" in line and "is-active" not in line:
            restart_line = line
            break
    assert restart_line is not None, (
        "Could not locate the kalshi-espn-collector restart command "
        "line (sister tests should already have flagged this)."
    )
    assert "sudo -n" in restart_line, (
        f"deploy.yml kalshi-espn-collector restart line "
        f"({restart_line.strip()!r}) MUST use `sudo -n` (non-interactive). "
        f"Bare `sudo` either hangs or errors in appleboy/ssh-action. "
        f"Pattern: `sudo -n /bin/systemctl restart kalshi-espn-collector`."
    )


def test_deploy_yml_espn_collector_uses_git_diff_name_only():
    """The ESPN path-aware detection MUST use `git diff --name-only`
    (mirror of the Kalshi-side pin).

    Without `--name-only`, the diff output includes hunk metadata that
    could false-positive the path regex.
    """
    script = _ssh_script_lines()
    # The ESPN-specific capture line.
    has_diff_name_only = any(
        "ESPN_DIFF_OUT=$(git diff --name-only" in line
        and not line.lstrip().startswith("#")
        for line in script
    )
    assert has_diff_name_only, (
        "deploy.yml ESPN collector restart MUST use `git diff "
        "--name-only` for path detection (mirror of D1.5.2 Kalshi pin). "
        "Pattern: `ESPN_DIFF_OUT=$(git diff --name-only ...)`."
    )


def test_deploy_yml_espn_unit_drift_guard_precedes_restart():
    """2026-05-30: when the ESPN UNIT FILE itself changed, deploy.yml
    MUST verify the on-VPS installed unit matches the deploy-commit unit
    BEFORE restarting — and FAIL LOUD (pointing at ops/install.sh) on drift.

    deploy.yml does NOT cp units / daemon-reload (that is install.sh's job),
    so a bare `systemctl restart` after a MemoryMax/cap change would run the
    unit against its STALE on-disk cap. For the 9-asset corpus bump
    (256M→384M, 2026-05-30) that means restarting the now-9-product collector
    against the old 256M cap → OOM-kill window. This guard mirrors the
    kalshi-bot unit-drift check (`systemctl cat` + `diff` + fail-with-recovery)
    and refuses to restart against a stale unit. No new sudoers needed (read +
    compare + exit 1).
    """
    script = _ssh_script_lines()
    cat_idx = _line_index_containing(script, "systemctl cat kalshi-espn-collector")
    restart_idx = _line_index_containing(
        script, "systemctl restart kalshi-espn-collector"
    )
    assert cat_idx >= 0, (
        "deploy.yml MUST read the installed ESPN unit via `systemctl cat "
        "kalshi-espn-collector` to drift-check it before restart. Without "
        "this, a cap/MemoryMax change restarts against the STALE on-disk unit."
    )
    assert restart_idx >= 0, "ESPN restart line missing."
    assert cat_idx < restart_idx, (
        "The unit-drift guard (`systemctl cat`) MUST appear BEFORE the "
        "`systemctl restart kalshi-espn-collector` line — otherwise the "
        "stale-cap restart fires before the guard can abort it."
    )
    joined = "\n".join(
        l for l in script if not l.lstrip().startswith("#")
    )
    assert "ops/kalshi-espn-collector.service" in joined, (
        "The drift guard must compare against the deploy-commit "
        "ops/kalshi-espn-collector.service."
    )
    assert "install.sh" in joined, (
        "The drift-guard failure message MUST point the operator at "
        "`bash ops/install.sh` for recovery (cp unit + daemon-reload)."
    )
