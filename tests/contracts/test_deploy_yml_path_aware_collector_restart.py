"""D1.5.2 — `.github/workflows/deploy.yml` SSH script MUST path-aware-restart
kalshi-collector (ticket 86b9zjtya, 2026-05-17).

Pre-D1.5.2 deploy.yml UNCONDITIONALLY restarts kalshi-bot and NEVER restarts
kalshi-collector — every collector code change required an operator-issued
`sudo systemctl restart kalshi-collector`. Bit us 3x in this session
(D1.3-fu1 ws_max_size, D1.4-fu REST status filter, D1.3-fu3 ping_timeout).

D1.5.2 adds a path-aware kalshi-collector restart block AFTER the bot
restart: detect changed paths via `git diff --name-only
${{ github.event.before }} ${{ github.sha }}`, restart iff any changed
path matches the affecting set, AND iff collector is currently active
(opt-out via VPS-side `systemctl stop`).

This file pins the new block's shape. Drift would re-open the manual-
restart-tax class.

Pins:
  1. The SSH script contains a path-aware collector restart block
     (identifiable by `systemctl restart kalshi-collector` reference).
  2. The block gates on `systemctl is-active kalshi-collector` (opt-out
     for operator-stopped collector).
  3. The path regex covers all 5 documented prefixes: `collector/`,
     `kalshi_wire/`, `ops/kalshi-collector.service`, `collector-start.sh`,
     `requirements.txt`.
  4. The collector restart appears AFTER the bot restart so a bot-only
     deploy can't have its bot restart hijacked by a collector restart bug.
  5. The diff base is `${{ github.event.before }}` (the prior deploy's
     HEAD), not `HEAD~1` (which is misleading post `git reset --hard`).
"""
from __future__ import annotations

from pathlib import Path


_DEPLOY_YML = (
    Path(__file__).resolve().parents[2] / ".github" / "workflows" / "deploy.yml"
)


def _ssh_script_lines() -> list[str]:
    """Extract the SSH script body lines from deploy.yml.

    Mirrors the helper in test_deploy_yml_pip_install_step.py — kept
    file-local (not shared) because the two D1.5.x tests evolve
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


def _all_line_indices_containing(lines: list[str], needle: str) -> list[int]:
    """All non-comment line indices containing the substring."""
    return [
        i for i, line in enumerate(lines)
        if needle in line and not line.lstrip().startswith("#")
    ]


def test_deploy_yml_ssh_script_contains_collector_restart():
    """The SSH script MUST contain `systemctl restart kalshi-collector`.

    Without this, deploy.yml never auto-restarts the collector and the
    manual-restart tax (D1.5.2 RCA evidence: 3 collector-affecting Bits
    in one session) continues. See
    `kb/decisions/d1-5-2-path-aware-restart-plan.md`.
    """
    script = _ssh_script_lines()
    idx = _line_index_containing(script, "systemctl restart kalshi-collector")
    assert idx >= 0, (
        "deploy.yml SSH script missing `systemctl restart kalshi-collector`. "
        "Every collector code change requires operator manual restart without "
        "this. Add a path-aware block AFTER the bot restart that fires when "
        "any of {collector/**, kalshi_wire/**, ops/kalshi-collector.service, "
        "collector-start.sh, requirements.txt} changed. See "
        "`kb/decisions/d1-5-2-path-aware-restart-plan.md`."
    )


def test_deploy_yml_collector_restart_gated_by_systemctl_is_active():
    """The collector restart MUST be gated by `systemctl is-active
    kalshi-collector` (opt-out for operator-stopped collector).

    If operator stops collector intentionally (controlled maintenance),
    deploy.yml MUST respect that and skip the restart. Without the gate,
    every deploy unwinds the operator's stop-intent.
    """
    script = _ssh_script_lines()
    # Look for the is-active gate anywhere in the script. Bash idioms
    # accept both `systemctl is-active kalshi-collector` and the more
    # defensive `systemctl is-active --quiet kalshi-collector`.
    has_gate = any(
        ("is-active" in line and "kalshi-collector" in line)
        and not line.lstrip().startswith("#")
        for line in script
    )
    assert has_gate, (
        "deploy.yml collector restart MUST be gated by "
        "`systemctl is-active kalshi-collector` so an operator-stopped "
        "collector is NOT restarted by auto-deploy. Pattern: `if systemctl "
        "is-active kalshi-collector >/dev/null 2>&1; then sudo systemctl "
        "restart kalshi-collector; fi`."
    )


def test_deploy_yml_collector_restart_uses_github_event_before_for_diff_base():
    """The diff base for path detection MUST be `${{ github.event.before }}`,
    NOT `HEAD~1`.

    After `git reset --hard ${{ github.sha }}` (which the script does
    earlier), `HEAD~1` is the prior commit on the current branch, but
    `${{ github.event.before }}` is the prior SHA from the push event
    (the deploy that ACTUALLY ran last). For a series of fast pushes,
    these can differ — the push event's `before` is authoritative.

    Sentinel handling: `${{ github.event.before }}` is `0000000...` on
    the initial push to a new branch. The script should fall back to
    "always restart" in that case (safer than skip).
    """
    script = _ssh_script_lines()
    has_before = any(
        "github.event.before" in line and not line.lstrip().startswith("#")
        for line in script
    )
    assert has_before, (
        "deploy.yml collector restart MUST use `${{ github.event.before }}` "
        "as the diff base, NOT `HEAD~1`. After `git reset --hard`, HEAD~1 "
        "is the prior commit on main but `github.event.before` is the SHA "
        "the prior push deployed — authoritative for 'what changed since "
        "last deploy'."
    )


def test_deploy_yml_collector_restart_path_set_covers_all_5_documented_prefixes():
    """The path regex MUST cover ALL 5 documented affecting prefixes.

    Missing any one would create a class of changes that silently skip
    the collector restart — exactly the manual-restart-tax bug D1.5.2
    closes.
    """
    script_text = "\n".join(_ssh_script_lines())
    required_prefixes = [
        "collector/",
        "kalshi_wire/",
        "ops/kalshi-collector.service",
        "collector-start.sh",
        "requirements.txt",
    ]
    missing = [p for p in required_prefixes if p not in script_text]
    assert not missing, (
        f"deploy.yml collector restart path set MISSING prefixes {missing}. "
        f"All 5 of {required_prefixes} must appear in the regex/grep "
        f"pattern. Missing any one creates a silent-skip class. See "
        f"`kb/decisions/d1-5-2-path-aware-restart-plan.md` § Fix."
    )


def test_deploy_yml_collector_restart_runs_after_bot_restart():
    """The collector restart MUST appear AFTER the bot restart line.

    Bot restart is the unconditional always-fire step that this Bit
    intentionally does NOT touch. Putting the collector block BEFORE
    bot restart risks one of two regressions:
      (a) collector restart errors → script exits via set -euo pipefail
          → bot never restarts that deploy.
      (b) future refactor of the collector block accidentally captures
          the bot restart inside its conditional.
    Putting collector AFTER bot makes the dependency direction explicit
    and matches the operator's mental model (bot is primary, collector
    is the bronze-recorder sibling).
    """
    script = _ssh_script_lines()
    bot_restart_idx = _line_index_containing(script, "systemctl restart kalshi-bot")
    collector_restart_idx = _line_index_containing(
        script, "systemctl restart kalshi-collector"
    )
    assert bot_restart_idx >= 0, (
        "deploy.yml SSH script missing `systemctl restart kalshi-bot` — "
        "file structure has changed since D1.5.2 planning."
    )
    assert collector_restart_idx >= 0, (
        "collector restart line missing (covered by sister test)."
    )
    assert bot_restart_idx < collector_restart_idx, (
        f"Bot restart (script line {bot_restart_idx}) MUST come BEFORE "
        f"collector restart (line {collector_restart_idx}). Reversed ordering "
        f"risks bot-restart hijack on collector-restart errors + couples the "
        f"two restart contracts."
    )


def test_deploy_yml_collector_restart_handles_null_sha_initial_push():
    """`${{ github.event.before }}` is 40 zeroes on the initial push to
    a new branch / after force-push history rewrite. The script MUST
    detect this sentinel and fall back to "always restart" (safer than
    silent-skip — preserves the plan doc's Verification §4 fail-safe
    intent and matches the test docstring's stated behavior).

    R1-M1 (D1.5.2 adv round 1) closure: the original implementation
    silently skipped on null SHA because `git diff 0000... HEAD` fails
    and the `|| true` swallowed it. The fix is an explicit sentinel
    check BEFORE the diff command.
    """
    script_text = "\n".join(_ssh_script_lines())
    # 40 zeroes — git's null-tree sentinel.
    null_sha = "0" * 40
    assert null_sha in script_text, (
        "deploy.yml MUST contain the null-SHA sentinel (40 zeroes) so the "
        "initial-push / force-push history-rewrite case explicitly falls "
        "back to always-restart rather than silently skipping. Pattern: "
        "`if [ \"${{ github.event.before }}\" = \"0000000000000000000000000000000000000000\" ]; then`. "
        "See kb/decisions/d1-5-2-path-aware-restart-plan.md § Risk register."
    )


def test_deploy_yml_collector_restart_handles_git_diff_failure():
    """If `git diff` fails for a non-null SHA (unreachable parent, gc'd
    history, network blip), the script MUST log the failure and fall
    back to always-restart rather than silently masking with `|| true`.

    R1-M2 (D1.5.2 adv round 1) closure: capture the diff command's exit
    code separately and inspect it. Pattern: `OUT=$(git diff ... 2>&1);
    EXIT=$?; if [ $EXIT -ne 0 ]; then ... fallback ... fi`.
    """
    script_text = "\n".join(_ssh_script_lines())
    has_exit_capture = (
        "COLLECTOR_DIFF_EXIT" in script_text
        or " $? " in script_text
        or "-ne 0" in script_text
        or "exit=" in script_text
    )
    assert has_exit_capture, (
        "deploy.yml MUST capture and inspect `git diff` exit code (e.g., "
        "`OUT=$(git diff ... 2>&1); EXIT=$?; if [ $EXIT -ne 0 ]; then "
        "log + fallback`) rather than silently masking failure with `|| "
        "true`. Without this, an unreachable-parent / gc'd-history case "
        "silently skips the collector restart. See R1-M2."
    )


def test_deploy_yml_collector_restart_diff_uses_set_minus_e_wrap():
    """The `git diff` substitution MUST run with `set +e` so a failing
    `$()` doesn't trigger `set -e` and abort the entire deploy script.

    R2-C1 (D1.5.2 adv round 2) closure: pure structural fix without
    `set +e` made the diff-failure fallback path UNREACHABLE — under
    `set -euo pipefail` (line 82), `VAR=$(failing-cmd)` aborts BEFORE
    the next `$?` capture line runs. The fallback log + restart never
    fire on real diff failure.

    Pattern required (in order, near the `git diff` line):
      set +e
      COLLECTOR_DIFF_OUT=$(git diff ... 2>&1)
      COLLECTOR_DIFF_EXIT=$?
      set -e
    """
    script = _ssh_script_lines()
    # Locate the git-diff capture line.
    diff_idx = -1
    for i, line in enumerate(script):
        if line.lstrip().startswith("#"):
            continue
        if "COLLECTOR_DIFF_OUT=$(git diff" in line:
            diff_idx = i
            break
    assert diff_idx >= 0, (
        "Couldn't locate `COLLECTOR_DIFF_OUT=$(git diff` capture line — "
        "structure changed since R2-C1."
    )
    # Look BACKWARDS for `set +e` in the prior 5 lines.
    set_plus_e_before = any(
        "set +e" in script[i]
        for i in range(max(0, diff_idx - 5), diff_idx)
    )
    assert set_plus_e_before, (
        f"git diff capture at script line {diff_idx} is NOT preceded by "
        f"`set +e` — under `set -euo pipefail`, the failing-substitution "
        f"case would abort the script and the fallback path is unreachable. "
        f"R2-C1 closure: add `set +e` before the substitution + `set -e` "
        f"after the `$?` capture. See "
        f"kb/decisions/d1-5-2-path-aware-restart-plan.md § R1-M2 + R2-C1."
    )
    # Look FORWARDS for `set -e` (re-enable) in the next 5 lines.
    set_minus_e_after = any(
        "set -e" in script[i] and "set +e" not in script[i]
        for i in range(diff_idx, min(len(script), diff_idx + 5))
    )
    assert set_minus_e_after, (
        f"git diff capture at script line {diff_idx} is NOT followed by "
        f"`set -e` re-enable within 5 lines — `set +e` leaks beyond the "
        f"intended scope, weakening fail-loud semantics for the rest of "
        f"the deploy script."
    )


def test_deploy_yml_collector_restart_logs_operator_opt_out():
    """When collector is stopped (operator opt-out), the script MUST log
    the skip rather than silently doing nothing. Observability invariant:
    every "restart would have fired" path MUST emit a recognizable log
    line so operators reading deploy logs can confirm the opt-out is in
    effect (vs the path-detection silently missing collector changes).

    R1-N3 (D1.5.2 adv round 1) closure: pin the `else` branch's log line.
    """
    script_text = "\n".join(_ssh_script_lines())
    # The log message contains a distinctive marker phrase.
    has_optout_log = (
        "operator stopped" in script_text.lower()
        or "preserve operator intent" in script_text.lower()
    )
    assert has_optout_log, (
        "deploy.yml MUST log the skip when kalshi-collector is stopped "
        "(operator opt-out path). Look for phrase like 'operator stopped' "
        "or 'preserve operator intent' in the else branch."
    )


def test_deploy_yml_collector_restart_uses_sudo_dash_n():
    """The `systemctl restart kalshi-collector` call MUST use `sudo -n`
    (non-interactive), matching the bot-restart pattern.

    R3-C1 (D1.5.2 adv round 3) closure: bare `sudo systemctl restart
    kalshi-collector` would either hang (waiting for password on a
    non-existent tty) or error with "sudo: a terminal is required to
    read the password" — appleboy/ssh-action has no tty. `sudo -n`
    fails IMMEDIATELY with exit 1 + a recognizable error message if
    sudoers NOPASSWD has not been extended for kalshi-collector.

    Mirrors the bot restart at deploy.yml:~134:
        sudo systemctl restart kalshi-bot  (currently)
        sudo -n /bin/systemctl restart kalshi-bot  (ideal)

    Sudoers prerequisite: `/etc/sudoers.d/botuser-systemctl-restart`
    must include `/bin/systemctl restart kalshi-collector` (operator-
    managed; see feedback_vps_sudoers_collector_gap_may17). Operator
    extended this 2026-05-17 mid-D1.3-fu1 session.
    """
    script = _ssh_script_lines()
    # Locate the restart-kalshi-collector line (non-comment, not is-active).
    restart_line = None
    for line in script:
        if line.lstrip().startswith("#"):
            continue
        if "restart kalshi-collector" in line and "is-active" not in line:
            restart_line = line
            break
    assert restart_line is not None, (
        "Could not locate the kalshi-collector restart command line "
        "(sister tests should already have flagged this)."
    )
    assert "sudo -n" in restart_line, (
        f"deploy.yml kalshi-collector restart line ({restart_line.strip()!r}) "
        f"MUST use `sudo -n` (non-interactive). Bare `sudo` in appleboy/"
        f"ssh-action (no tty) either hangs on password prompt or errors "
        f"with `sudo: a terminal is required` — both abort the deploy "
        f"silently. `sudo -n` fails loud-and-fast if NOPASSWD sudoers "
        f"hasn't been extended to include kalshi-collector. Pattern: "
        f"`sudo -n /bin/systemctl restart kalshi-collector`."
    )


def test_deploy_yml_collector_restart_uses_git_diff_name_only():
    """The path-aware detection MUST use `git diff --name-only` (the
    explicit, file-list-only flag) — not `git log --stat` or `git show`,
    which produce more output and risk regex false-positives on metadata.
    """
    script = _ssh_script_lines()
    has_diff_name_only = any(
        ("git diff --name-only" in line or "diff --name-only" in line)
        and not line.lstrip().startswith("#")
        for line in script
    )
    assert has_diff_name_only, (
        "deploy.yml collector restart MUST use `git diff --name-only` for "
        "path detection. Other forms (`git log`, `git show`, `git diff` "
        "without `--name-only`) include metadata that could false-positive "
        "the path regex."
    )
