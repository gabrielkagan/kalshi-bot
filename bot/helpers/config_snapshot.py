"""Per-decision config_snapshot — ticket 86b9zkp8p (2026-05-17).

RCA: For "retrain at any moment", regime-filter of training data needs
deterministic reproducibility. Today it requires `git log market_config.py
bot/constants.py bot/main_loop.py bot/scanner/__init__.py bot/executor.py`
+ correlating with `evaluation_time`. This is fragile:
  - Mid-day env-var flips (kill switches like WEATHER_NO_SIDE_LIVE=1)
    leave no git trace.
  - Multi-config-change days require manual disambiguation.
  - Operator runtime mutations are invisible.

Goal: every `evaluated_opportunities` + `rejected_opportunities` row carries
a FK to a `config_snapshots` row that captures EXACTLY which config produced
the decision. Replay = look up snapshot -> restore the config -> re-run.

Phase-1 captures the snapshot once at `MainLoop.__init__`. Mid-day mutation
re-capture is deferred to a Phase-2 followup ticket.

Helper-leaf rule (.importlinter `helpers-leaf`): this module must NOT import
bot.main_loop / bot.scanner / bot.state / bot.config. Stdlib only -- the three
config files are read as FILE CONTENTS via SHA256, not as Python modules.
This both keeps the leaf rule satisfied AND captures the on-disk text exactly
(comments + formatting included) which is what a human reading `git show`
on a snapshot row will want to compare against.

Schema-chain pairing (see bot/CLAUDE.md "config_snapshot_id schema chain"):
  - bot/state.py -- CREATE TABLE config_snapshots, ALTER TABLE for the FK col,
    insert_evaluated_opportunity + insert_rejection signature + SQL
  - bot/main_loop.py -- MainLoop.__init__ calls persist_config_snapshot
  - bot/scanner/__init__.py -- every insert call passes config_snapshot_id
  - tests/contracts/test_config_snapshot.py -- full chain pin
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
from typing import Dict


# -- Tracked env-var flags ---------------------------------------------------
#
# These are flags that runtime-mutate scanner/executor behavior WITHOUT a
# git commit. Adding here = include in the hash (rotating the hash on flip).
# Removing here = silently exclude. Default is OMIT-when-unset to keep legacy
# environments hash-stable when a new flag is added; only the live setting
# rotates the hash.
_TRACKED_ENV_FLAGS = (
    "CALMLP_ENABLED",
    "WEATHER_NO_SIDE_LIVE",
    "HOURLY_NO_SIDE_LIVE",
    "BRACKET_NO_ENABLED",
    "MEXC_FEED_ENABLED",
    "BINANCE_FEED_ENABLED",
    "BAND_CALIBRATION_DISABLED_CELLS",
)


# Resolved on disk relative to repo root. Resolution at module-load time
# (not call time) so monkeypatching the module attribute in tests works.
# Path computed via __file__ -> repo root (parent of bot/).
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_CONSTANTS_PATH = os.path.join(_REPO_ROOT, "bot", "constants.py")
_CONFIG_PATH = os.path.join(_REPO_ROOT, "bot", "config.py")
_MARKET_CONFIG_PATH = os.path.join(_REPO_ROOT, "market_config.py")


def _sha256_file(path: str) -> str:
    """SHA256 the file at `path`. Missing file -> empty-content sha256.

    Missing-file semantics: returning the sha256 of empty bytes is a stable,
    deterministic sentinel that distinguishes "file missing" from "file
    exists with content" without raising at hash time. In the unlikely event
    a config file is moved/renamed outside this module's awareness, the
    config_hash will rotate to a value reproducible across all hosts.
    """
    try:
        with open(path, "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()
    except (FileNotFoundError, IsADirectoryError, PermissionError):
        return hashlib.sha256(b"").hexdigest()


def _env_flags_json() -> str:
    """JSON of tracked env-var flags (sorted keys, omit-unset for hash stability)."""
    snapshot = {
        flag: os.environ[flag]
        for flag in _TRACKED_ENV_FLAGS
        if flag in os.environ
    }
    # sort_keys ensures byte-identical output across Python dict iteration
    # ordering changes; matters for the cross-host reproducibility guarantee.
    return json.dumps(snapshot, sort_keys=True)


def _git_head_sha() -> str:
    """Best-effort `git rev-parse HEAD`. Missing git or no commits -> "unknown".

    Run with a 2s timeout so a hung subprocess can never block MainLoop init.
    Captures stderr to avoid polluting the boot log when git is unavailable.
    """
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=_REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=2.0,
            check=False,
        )
        if proc.returncode == 0:
            out = proc.stdout.strip()
            if out:
                return out
        return "unknown"
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return "unknown"


def compute_config_snapshot() -> Dict[str, str]:
    """Compute the canonical 6-field snapshot bundle + composite hash.

    Returns a dict with keys:
      - config_hash: sha256 over "constants_sha|config_sha|market_sha|env_json|git_sha"
      - captured_at: ISO-8601 UTC timestamp
      - git_head_sha: best-effort; "unknown" on failure
      - constants_sha: sha256 of bot/constants.py file bytes
      - config_sha: sha256 of bot/config.py file bytes
      - market_config_sha: sha256 of market_config.py file bytes
      - env_flags_json: sorted-key JSON of tracked env-var flags (set ones only)

    Stability contract: identical inputs -> identical config_hash, byte-exact.
    Cross-host determinism -- two operators running the same commit + same env
    should get the same hash.
    """
    import datetime  # local import -- keep module body stdlib-clean

    constants_sha = _sha256_file(_CONSTANTS_PATH)
    config_sha = _sha256_file(_CONFIG_PATH)
    market_sha = _sha256_file(_MARKET_CONFIG_PATH)
    env_json = _env_flags_json()
    git_sha = _git_head_sha()
    composite = "|".join((constants_sha, config_sha, market_sha, env_json, git_sha))
    config_hash = hashlib.sha256(composite.encode("utf-8")).hexdigest()
    captured_at = datetime.datetime.now(datetime.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%S.%fZ"
    )
    return {
        "config_hash": config_hash,
        "captured_at": captured_at,
        "git_head_sha": git_sha,
        "constants_sha": constants_sha,
        "config_sha": config_sha,
        "market_config_sha": market_sha,
        "env_flags_json": env_json,
    }


def persist_config_snapshot(conn) -> int:
    """Persist (or look up) the current config snapshot and return its row id.

    INSERT OR IGNORE on the UNIQUE config_hash, then SELECT id. Identical
    inputs -> SAME id every call (no row duplication). This lets MainLoop call
    once per restart AND a future Phase-2 followup re-call per scan tick on
    mid-day mutation without growing the table on every tick.

    Caller owns the connection; we do not close it. Commits to flush the
    INSERT -- keeps the helper independent of the caller's tx posture.
    """
    bundle = compute_config_snapshot()
    conn.execute(
        """
        INSERT OR IGNORE INTO config_snapshots
            (config_hash, captured_at, git_head_sha,
             constants_sha, config_sha, market_config_sha, env_flags_json)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            bundle["config_hash"],
            bundle["captured_at"],
            bundle["git_head_sha"],
            bundle["constants_sha"],
            bundle["config_sha"],
            bundle["market_config_sha"],
            bundle["env_flags_json"],
        ),
    )
    conn.commit()
    row = conn.execute(
        "SELECT id FROM config_snapshots WHERE config_hash=?",
        (bundle["config_hash"],),
    ).fetchone()
    if row is None:
        # Defensive: this can only happen if the INSERT was silently dropped
        # AND no prior row existed -- should be unreachable. Raising is
        # safer than returning a fake id (would break the FK invariant
        # at the call sites in scanner).
        raise RuntimeError(
            f"persist_config_snapshot: row lookup failed after insert "
            f"(config_hash={bundle['config_hash'][:8]}...)"
        )
    # sqlite3.Row supports both index and key access; fall back to [0].
    try:
        return int(row["id"])
    except (TypeError, IndexError):
        return int(row[0])
