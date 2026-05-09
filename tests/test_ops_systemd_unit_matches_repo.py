"""Regression tests for ops/ systemd unit + install.sh consistency.

Bit 2.0.5.2 of repo modularization plan
(kb/decisions/repo-modularization-plan-may05.md). Pins:
- ops/kalshi-bot.service exists and parses as a systemd unit.
- ops/install.sh runs `systemctl daemon-reload` AFTER `cp` (otherwise
  the install is a silent no-op — systemd keeps the cached unit).
- ops/install.sh targets the canonical destination
  /etc/systemd/system/kalshi-bot.service.
- The unit's ExecStart routes through start.sh (current chain) or
  `python -m bot` (post-Bit-2.1a future state). Anything else (e.g.,
  `python3 bot/_impl.py` direct) repeats the Bit 2.1a incident class.
- .github/workflows/deploy.yml contains the pre-deploy drift check
  introduced by Bit 2.0.5.2, and that check runs BEFORE
  `git reset --hard` so an aborted deploy leaves the VPS untouched.
"""
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
UNIT_FILE = REPO_ROOT / "ops" / "kalshi-bot.service"
INSTALL_SH = REPO_ROOT / "ops" / "install.sh"
DEPLOY_YML = REPO_ROOT / ".github" / "workflows" / "deploy.yml"
EXPECTED_DST = "/etc/systemd/system/kalshi-bot.service"


def test_ops_unit_file_exists():
    """ops/kalshi-bot.service exists and parses as a systemd unit."""
    assert UNIT_FILE.exists(), (
        f"{UNIT_FILE.relative_to(REPO_ROOT)} missing. Bit 2.0.5.1 of repo "
        "modularization plan ships this file as the source of truth for "
        "the on-VPS systemd unit."
    )
    text = UNIT_FILE.read_text()
    assert re.search(r"^\[Unit\]", text, re.M), (
        "[Unit] section missing — file is not a valid systemd unit."
    )
    assert re.search(r"^\[Service\]", text, re.M), (
        "[Service] section missing — file is not a valid systemd unit."
    )
    assert re.search(r"^ExecStart=", text, re.M), (
        "ExecStart= directive missing — systemd would refuse the unit."
    )


def test_ops_install_sh_invokes_systemctl_daemon_reload():
    """install.sh must run `systemctl daemon-reload` AFTER `cp`.

    Order matters: cp without daemon-reload is a silent no-op (systemd
    keeps using the cached unit until reload). A future edit that flips
    the order would brick the install.

    Anchored at column 0 (re.M `^`) so a comment containing the phrase
    `sudo cp` or `systemctl daemon-reload` mid-line can't satisfy the
    ordering assertion (R1 M3: install.sh has both phrases in comment
    bodies, which a substring search would match before the real lines).
    """
    assert INSTALL_SH.exists(), f"{INSTALL_SH.relative_to(REPO_ROOT)} missing."
    text = INSTALL_SH.read_text()
    cp_match = re.search(r"^sudo\s+cp\b", text, re.M)
    assert cp_match, (
        "install.sh missing `sudo cp` line at column 0 — without it, the "
        "unit never lands at /etc/systemd/system/."
    )
    reload_match = re.search(r"^sudo\s+systemctl\s+daemon-reload\b", text, re.M)
    assert reload_match, (
        "install.sh missing `sudo systemctl daemon-reload` line at column "
        "0 — without it, systemd keeps using the cached unit, so the cp "
        "is a silent no-op."
    )
    assert reload_match.start() > cp_match.start(), (
        "install.sh runs `sudo systemctl daemon-reload` BEFORE `sudo cp`. "
        "That ordering reloads stale unit content, leaving the new "
        "unit at /etc/ ineffective until the next reload."
    )


def test_ops_install_sh_targets_correct_dst():
    """install.sh must target /etc/systemd/system/kalshi-bot.service."""
    assert INSTALL_SH.exists()
    text = INSTALL_SH.read_text()
    assert EXPECTED_DST in text, (
        f"install.sh doesn't reference {EXPECTED_DST!r}. The canonical "
        f"systemd unit destination is hardcoded by systemd; a typo here "
        f"would copy the file somewhere systemd doesn't read."
    )


def test_ops_unit_execstart_calls_start_sh_or_python_m_bot():
    """ExecStart must route through start.sh or `python -m bot`.

    Anything else (e.g., `python3 bot/_impl.py` direct) repeats the Bit 2.1a
    incident class — the on-VPS systemd unit bypasses the documented
    sacred-rule chain in CLAUDE.md.
    """
    assert UNIT_FILE.exists()
    text = UNIT_FILE.read_text()
    m = re.search(r"^ExecStart=(.*)$", text, re.M)
    assert m, "ExecStart= directive missing in unit file."
    cmd = m.group(1).strip()
    via_start_sh = bool(re.search(r"(?:^|/)start\.sh(?:\s|$)", cmd))
    via_python_m_bot = bool(re.search(r"-m\s+bot(?:\s|$)", cmd))
    assert via_start_sh or via_python_m_bot, (
        f"ExecStart={cmd!r} doesn't route through start.sh or `python -m "
        f"bot`. Bit 2.1a postmortem (kb/failures/bit-2.1a-systemd-mismatch-"
        f"may06.md): direct `python3 bot/_impl.py` invocation bypasses the "
        f"documented sacred-rule chain in CLAUDE.md and is the original "
        f"incident class this Bit's safety net catches."
    )


def test_pre_deploy_check_in_deploy_yml():
    """deploy.yml must contain the Bit 2.0.5.2 pre-deploy drift check.

    Pins the gate's presence so a future workflow edit can't silently
    delete it. Asserts:
    - `systemctl cat kalshi-bot` is invoked (the on-VPS side).
    - `ops/kalshi-bot.service` is referenced (the repo side).
    - The comparison uses `git show <sha>:ops/kalshi-bot.service` to
      pin against the deploy-commit file, NOT the working-tree file
      (which at this point in the script is the PRIOR commit's content
      — `git checkout main` doesn't advance HEAD before reset --hard).
    - The check precedes `git reset --hard <sha>` so an aborted deploy
      leaves the VPS on the prior commit (untouched).
    """
    assert DEPLOY_YML.exists(), f"{DEPLOY_YML.relative_to(REPO_ROOT)} missing."
    text = DEPLOY_YML.read_text()
    assert "systemctl cat kalshi-bot" in text, (
        "deploy.yml missing `systemctl cat kalshi-bot` (Bit 2.0.5.2 "
        "pre-deploy drift check). Without this step, on-VPS systemd "
        "unit drift goes undetected until manual `ssh + systemctl cat` "
        "— exactly the gap the Bit 2.1a postmortem flagged."
    )
    assert "ops/kalshi-bot.service" in text, (
        "deploy.yml drift check must reference `ops/kalshi-bot.service` "
        "(the source-of-truth file)."
    )
    # R1 M2 + R2 M1: the drift check must compare against the deploy
    # commit's ops/kalshi-bot.service via `git show ${{ github.sha }}:...`
    # — NOT the working-tree file (still the prior commit's content here).
    # Anchor against the FULL command form (path included) so the
    # assertion can't be satisfied by the explanatory comment alone:
    # the comment uses `:...` (ellipsis), the command uses the full path.
    assert re.search(
        r"git\s+show\s+\$\{\{\s*github\.sha\s*\}\}:ops/kalshi-bot\.service",
        text,
    ), (
        "deploy.yml drift check must use `git show ${{ github.sha }}:"
        "ops/kalshi-bot.service` (the deploy-commit file). The naive "
        "`<(... ops/kalshi-bot.service)` form compares against the "
        "working-tree file, which at this point is the PRIOR commit's "
        "content (R1 M2: `git checkout main` doesn't advance HEAD if "
        "local main was at the prior reset --hard target)."
    )
    # Ordering: diff step must precede the actual `git reset --hard <sha>`
    # command, NOT a `git reset --hard` substring that appears in a comment
    # (a stricter `find` would false-positive on commentary above the diff).
    # Match the literal command form by anchoring on the GitHub Actions
    # expression `${{` that follows the sha.
    diff_idx = text.find("systemctl cat kalshi-bot")
    reset_match = re.search(r"^\s*git\s+reset\s+--hard\s+\$\{\{", text, re.M)
    assert diff_idx != -1 and reset_match is not None, (
        "deploy.yml is missing one of the expected anchor strings — the "
        "workflow may have been refactored in a way that needs this test "
        "updated alongside."
    )
    assert diff_idx < reset_match.start(), (
        "deploy.yml drift check is AFTER `git reset --hard <sha>`. The "
        "check must run BEFORE reset --hard so an aborted deploy leaves "
        "the VPS on the prior commit (untouched)."
    )


def test_test_unit_tier_invokes_systemd_unit_test():
    """make test-unit (Pillar 5 rename of test-fast) must run this test file.

    Mirrors Bit 1.3 / 1.4 / 1.5 cadence: every dev-tooling invariant
    test self-pins into the fast tier so a future Makefile edit can't
    silently drop it.

    Pillar 5 (86b9ve11y) renamed test-fast → test-unit and moved the
    file list into a `UNIT_FILES` Make variable. The file may appear
    in any of: test-unit recipe, test-fast recipe (legacy alias), or
    UNIT_FILES variable body — accept all three forms.
    """
    makefile = REPO_ROOT / "Makefile"
    assert makefile.exists(), "Makefile missing at repo root."
    text = re.sub(r"\\\n", " ", makefile.read_text())
    fragments = []
    for target in ("test-unit", "test-fast"):
        m = re.search(
            rf"^{target}:[^\n]*\n(?:[ \t]*\n)*((?:[ \t]+[^\n]*\n?)+)",
            text,
            re.M,
        )
        if m:
            fragments.append(m.group(1))
    var_match = re.search(
        r"^UNIT_FILES\s*[:?]?=\s*([^\n]+)$",
        text,
        re.M,
    )
    if var_match:
        fragments.append(var_match.group(1))
    assert fragments, (
        "Makefile has neither a `test-unit:` nor a `test-fast:` target "
        "with a recipe body, and no `UNIT_FILES` variable."
    )
    combined = "\n".join(fragments)
    assert "test_ops_systemd_unit_matches_repo.py" in combined, (
        "Unit tier (test-unit/test-fast/UNIT_FILES) doesn't invoke "
        "tests/test_ops_systemd_unit_matches_repo.py. Per Bits 1.3-1.5 "
        "cadence, every dev-tooling invariant test self-pins into the "
        "unit tier so a future edit can't silently drop it. Combined "
        f"surface: {combined!r}"
    )
