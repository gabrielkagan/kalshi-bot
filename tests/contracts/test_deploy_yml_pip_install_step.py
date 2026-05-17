"""D1.5.1 — `.github/workflows/deploy.yml` SSH script MUST install pip
requirements before service restart (ticket 86b9zjtxn, 2026-05-17).

Pre-D1.5.1 the deploy.yml SSH script did `git reset --hard ${{ github.sha }}`
→ syntax check → `sudo systemctl restart kalshi-bot`, with NO `pip install`
in between. Any commit that adds a runtime Python dep silently breaks the
next service restart with `ModuleNotFoundError`. Bit us 2026-05-17 mid-D1.5
collector deploy on `zstandard` — collector entered `Restart=on-failure`
crashloop until operator manually ran `pip install -r requirements.txt`
on the VPS.

D1.5.1 fix inserts a pip-install step between `git reset --hard` and
the syntax check, using the bot's venv (`~/kalshi-bot-repo/venv/bin/pip`)
so the install lands in the SHARED bot+collector venv per D0.3 §6.

This file pins the step exists with correct ordering. Drift would
re-open the dep-drift bug class.

Pins:
  1. The SSH script contains `pip install -r requirements.txt`.
  2. The pip-install line appears AFTER `git reset --hard` (so deps
     match the deploy commit, not the prior commit).
  3. The pip-install line appears BEFORE `systemctl restart kalshi-bot`
     (so the restart can't crashloop on missing deps).
  4. The pip-install line appears BEFORE the syntax check (so an
     `import ast`-style syntax check on a file that pulls in a new
     dep doesn't itself ModuleNotFoundError).
  5. The pip command uses the bot venv's pip explicitly
     (`~/kalshi-bot-repo/venv/bin/pip` or equivalent `venv/bin/pip`
     path) — bare `pip` resolves to system Python 3.11 on the VPS,
     bypassing the venv where the bot runs.
"""
from __future__ import annotations

from pathlib import Path


_DEPLOY_YML = (
    Path(__file__).resolve().parents[2] / ".github" / "workflows" / "deploy.yml"
)


def _read_deploy_yml() -> str:
    """Single source of file content for all tests in this module."""
    return _DEPLOY_YML.read_text()


def _ssh_script_lines() -> list[str]:
    """Extract the SSH script body lines from deploy.yml.

    Returns the raw script content (lines after `script: |` up to the
    next top-level YAML key). Preserves ordering and line numbers within
    the script.
    """
    text = _read_deploy_yml()
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
        # Script body must be at GREATER indent than the `script:` key.
        # First non-blank script line establishes the body indent; any
        # subsequent line dedented to ≤ script_indent ends the block.
        line_indent = len(line) - len(line.lstrip())
        if line_indent <= (script_indent or 0):
            break
        out.append(line)
    assert out, (
        "deploy.yml SSH script body could not be extracted — file structure "
        "may have changed since D1.5.1. Check `.github/workflows/deploy.yml` "
        "for the `script: |` anchor."
    )
    return out


def _line_index_containing(lines: list[str], needle: str) -> int:
    """Return the index of the first NON-COMMENT line containing the substring.

    Skips lines whose first non-whitespace char is ``#`` (bash/yaml comments)
    so a future doc-comment that mentions the substring (e.g. ``# operator
    manually ran pip install -r requirements.txt``) does NOT mask the
    actual command being removed. R1-M2 (D1.5.1 adv round 1) closed the
    comment-as-fossil class for this locator.

    Returns -1 if no non-comment line matches. Uses substring match
    (not regex) for robustness against indentation / quoting differences.
    """
    for i, line in enumerate(lines):
        if line.lstrip().startswith("#"):
            continue
        if needle in line:
            return i
    return -1


def test_deploy_yml_ssh_script_contains_pip_install_requirements():
    """The SSH script MUST contain `pip install -r requirements.txt`.

    Without this, any commit adding a Python dep silently crashloops the
    service on the next restart. RCA + lesson: see
    `kb/decisions/d1-5-1-deploy-yml-pip-install-plan.md`.
    """
    script = _ssh_script_lines()
    idx = _line_index_containing(script, "pip install -r requirements.txt")
    assert idx >= 0, (
        "deploy.yml SSH script missing `pip install -r requirements.txt`. "
        "Any commit adding a Python dep to requirements.txt will silently "
        "crashloop the service on the next restart (ModuleNotFoundError). "
        "Add the step between `git reset --hard ${{ github.sha }}` and "
        "`sudo systemctl restart kalshi-bot`. See "
        "`kb/decisions/d1-5-1-deploy-yml-pip-install-plan.md`."
    )


def test_deploy_yml_pip_install_runs_after_git_reset_hard():
    """The pip-install step MUST come AFTER `git reset --hard ${{ github.sha }}`.

    Running pip BEFORE the SHA pin would install deps from the PRIOR
    commit's requirements.txt — the inverse of what the operator
    expects. The pip install must target the new deploy commit's
    requirements.txt.
    """
    script = _ssh_script_lines()
    reset_idx = _line_index_containing(script, "git reset --hard")
    pip_idx = _line_index_containing(script, "pip install -r requirements.txt")
    assert reset_idx >= 0, (
        "deploy.yml SSH script missing `git reset --hard` — file structure "
        "may have changed since D1.5.1 planning."
    )
    assert pip_idx >= 0, "pip install step missing (covered by sister test)."
    assert pip_idx > reset_idx, (
        f"pip install step (line {pip_idx} of SSH script) MUST come AFTER "
        f"`git reset --hard` (line {reset_idx}). Pre-reset, the working tree "
        f"is still on the PRIOR commit's requirements.txt — installing then "
        f"would target the wrong deps."
    )


def test_deploy_yml_pip_install_runs_before_systemctl_restart():
    """The pip-install step MUST come BEFORE `systemctl restart kalshi-bot`.

    Restart-before-install reproduces the exact bug D1.5.1 closes:
    crashloop on missing dep. Restart MUST see the new venv state.
    """
    script = _ssh_script_lines()
    pip_idx = _line_index_containing(script, "pip install -r requirements.txt")
    restart_idx = _line_index_containing(script, "systemctl restart kalshi-bot")
    assert pip_idx >= 0, "pip install step missing (covered by sister test)."
    assert restart_idx >= 0, (
        "deploy.yml SSH script missing `systemctl restart kalshi-bot` — "
        "file structure may have changed since D1.5.1 planning."
    )
    assert pip_idx < restart_idx, (
        f"pip install step (line {pip_idx} of SSH script) MUST come BEFORE "
        f"`systemctl restart kalshi-bot` (line {restart_idx}). Restart-first "
        f"reproduces the D1.5.1 dep-drift crashloop."
    )


def test_deploy_yml_pip_install_runs_before_syntax_check():
    """Defensive: pip install MUST come BEFORE the `import ast` syntax check.

    A syntax check on a file that pulls in a new dep at module-import
    time (e.g., a new `import zstandard` in a top-level module that
    `bot/scanner/__init__.py` transitively imports) would itself fire
    `ModuleNotFoundError` before pip even runs. Order matters even
    though the syntax check is "just" `python3 -c "import ast..."`.
    """
    script = _ssh_script_lines()
    pip_idx = _line_index_containing(script, "pip install -r requirements.txt")
    syntax_idx = _line_index_containing(script, "import ast")
    assert pip_idx >= 0, "pip install step missing (covered by sister test)."
    if syntax_idx < 0:
        # Syntax-check step is optional/movable; if it's gone, the
        # invariant trivially holds. Don't fail.
        return
    assert pip_idx < syntax_idx, (
        f"pip install step (line {pip_idx} of SSH script) MUST come BEFORE "
        f"the `python3 -c \"import ast...\"` syntax check (line {syntax_idx}). "
        f"A syntax check on a file with a new top-level import would itself "
        f"ModuleNotFoundError if the dep isn't installed yet."
    )


def test_deploy_yml_pip_install_uses_venv_pip_not_system_pip():
    """The pip command MUST target the bot's venv, not system Python.

    `appleboy/ssh-action` runs the script directly via SSH — does NOT
    source `~/.bashrc` or auto-activate the venv. Bare `pip` would
    resolve to the system Python 3.11 install on the VPS, NOT the bot's
    venv at `~/kalshi-bot-repo/venv/bin/python`. Installing into the
    wrong site-packages means the bot still ModuleNotFoundErrors on
    restart even though pip succeeded.

    Acceptable forms:
      - `~/kalshi-bot-repo/venv/bin/pip install -r requirements.txt`
      - `/home/botuser/kalshi-bot-repo/venv/bin/pip install ...`
      - `source venv/bin/activate && pip install ...`
      - `venv/bin/pip install ...` (since `cd ~/kalshi-bot-repo` is line 1)
    """
    script = _ssh_script_lines()
    pip_idx = _line_index_containing(script, "pip install -r requirements.txt")
    assert pip_idx >= 0, "pip install step missing (covered by sister test)."
    pip_line = script[pip_idx]

    venv_path_marker = "venv/bin/pip" in pip_line
    activate_marker = (
        "venv/bin/activate" in pip_line
        or any("venv/bin/activate" in s for s in script[max(0, pip_idx - 3):pip_idx])
    )
    assert venv_path_marker or activate_marker, (
        f"pip install line ({pip_line.strip()!r}) appears to use bare `pip`. "
        f"appleboy/ssh-action runs the script via SSH without sourcing "
        f"~/.bashrc — bare `pip` resolves to system Python 3.11, NOT the "
        f"bot's venv at `~/kalshi-bot-repo/venv/`. Use "
        f"`~/kalshi-bot-repo/venv/bin/pip install -r requirements.txt` (or "
        f"source the venv first). See "
        f"`kb/decisions/d1-5-1-deploy-yml-pip-install-plan.md` § Fix."
    )
