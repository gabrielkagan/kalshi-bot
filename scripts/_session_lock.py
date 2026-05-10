"""Sprint PSC Bit P5.1 — parallel-Claude-session lock primitive.

Why this exists
---------------
The kalshi-bot repo is touched by multiple concurrent Claude Code sessions
(parent orchestrator + per-Bit worktree workers). Without coordination they
collide on shared write surfaces:

  * MEMORY.md (the user's auto-memory file) — concurrent appends produce
    corrupt index entries.
  * origin/main — both sessions push the same Bit, second push fails or
    (worse) ships divergent code.
  * agent_docs/* + kb/* — the same finding gets written twice.
  * bot/_impl.py — two extraction Bits each rebase on top of stale main.

This module provides the lock primitive that all subsequent PSC hooks
(P5.2 PreToolUse, P5.3 git-pre-commit, P5.6 memory-write) consume to
serialize per-target-path writes across sessions on a single host.

Design
------
Mirrors mclaude (github.com/AnastasiyaW/mclaude):

  * O_CREAT | O_EXCL atomic create of a JSON lockfile.
  * Daemon heartbeat thread updates `last_heartbeat` every 30 s.
  * Lockfile with `last_heartbeat` older than 180 s is reclaimable;
    reclaim is logged to .claude/locks/reclaim.log for forensic audit.
  * Malformed JSON → quarantined sidecar (.quarantine), reclaim audited.

Stdlib-only by deliberate choice (mirrors scripts/_state_db_snapshot.py).
No `bot.*` imports — this is leaf infrastructure.

iCloud caveat
-------------
The repo lives under iCloud Drive. iCloud occasionally creates collision
suffixes like `foo 2.lock` next to `foo.lock` when files are touched on
two devices in quick succession. The lock primitive is robust to this:

  * We acquire by O_CREAT|O_EXCL on the *canonical* flat-encoded path.
    iCloud never inserts characters into our chosen filename — it only
    creates siblings — so the canonical name is stable.
  * Stale-reclaim only inspects the canonical path. iCloud-suffixed
    siblings are ignored, never honored as if they were authoritative.
  * No glob-walk over the lock directory — we resolve directly by name.

Path-flatten contract
---------------------
target_path "bot/_impl.py"  ↔  lockfile name "bot__SLASH___impl.py.lock"
target_path "bot/scanner/__init__.py"
            ↔  "bot__SLASH__scanner__SLASH____init__.py.lock"

The `__SLASH__` token was chosen because it cannot appear in a real
POSIX path (uppercase + double-underscore convention) and survives
iCloud's filename quirks. Round-trip is preserved by `flatten_target_path`
+ `unflatten_target_path`, which together form a bijection over the
accepted input space: `flatten` rejects (a) the literal contiguous
marker, (b) any component starting with `SLASH__`, and (c) any component
ending with `__SLASH` — eliminating both direct collision and
boundary-straddling collision (R1 M1 + R4 M1).
"""
from __future__ import annotations

import errno
import json
import os
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Dict, Optional

# ---------------------------------------------------------------------------
# Tunables
# ---------------------------------------------------------------------------

#: Heartbeat tick interval. mclaude uses 30s. Reducible by tests via
#: monkeypatch (NOT meant to be changed at runtime by callers).
HEARTBEAT_INTERVAL_S: float = 30.0

#: Stale threshold. Lockfile with `last_heartbeat` older than this is
#: reclaimable. Set to 6× HEARTBEAT_INTERVAL_S so a momentary GC pause
#: or paging stall doesn't trigger spurious reclaim.
STALE_THRESHOLD_S: float = 180.0

#: Filename token that stands in for "/" in target paths.
_SLASH_MARKER: str = "__SLASH__"

#: Required keys in a well-formed lockfile JSON. Anything missing → malformed.
_REQUIRED_KEYS = frozenset(
    {"pid", "session_id", "target_path", "started_at", "last_heartbeat"}
)

# ---------------------------------------------------------------------------
# Test hooks (overrides). Production reads from env / repo-relative defaults.
# ---------------------------------------------------------------------------

_LOCK_ROOT_OVERRIDE: Optional[Path] = None
_RECLAIM_LOG_OVERRIDE: Optional[Path] = None


def _repo_root() -> Path:
    """Best-effort *common* repo root, worktree-aware.

    Two Claude sessions — one on the main checkout, one inside
    `.claude/worktrees/<name>/` — must resolve to the SAME lock root,
    otherwise their lockfiles never collide and the primitive is useless
    for our actual topology (parent orchestrator + per-Bit worktree
    workers; see `kb/decisions/parallel-session-coordination-may09.md`).

    Algorithm:
      1. Walk up looking for any `.git` entry.
      2. If `.git` is a **file** (worktree pointer), parse its first
         line — `gitdir: <path-to-.git/worktrees/<name>/>` — and resolve
         the common git-dir as that path's parent's parent
         (`<repo>/.git/worktrees/<name>/` → `<repo>`).
      3. If `.git` is a **directory**, that's the main checkout —
         return the dir containing it.
      4. Fall back to the parent of the `scripts/` dir if `.git` is
         absent (e.g. extracted tarball, CI). R1 minor #1 (out of scope).
    """
    here = Path(__file__).resolve().parent
    for cand in (here, *here.parents):
        git_entry = cand / ".git"
        if not git_entry.exists():
            continue
        if git_entry.is_dir():
            # Main checkout. cand is the repo root.
            return cand
        if git_entry.is_file():
            # Worktree pointer file. First line: "gitdir: <absolute-path>".
            # That path is `<main-repo>/.git/worktrees/<worktree-name>/`,
            # whose parent.parent is the main checkout root.
            try:
                first_line = git_entry.read_text(
                    encoding="utf-8", errors="replace"
                ).splitlines()[0]
            except (OSError, IndexError):
                # Unreadable / empty — fall through to walk-up fallback.
                return cand
            if first_line.startswith("gitdir:"):
                gitdir_str = first_line[len("gitdir:"):].strip()
                gitdir = Path(gitdir_str)
                # R2 M7 — git 2.48+ supports
                # `git config --global worktree.useRelativePaths true`, which
                # makes `git worktree add` write a RELATIVE gitdir (e.g.
                # `gitdir: ../../.git/worktrees/wt1`). Per git-worktree(1):
                # "if gitdir is a relative path, it is relative to the
                # location of the worktree's .git file." Without resolving
                # against `git_entry.parent`, `Path('../../..').exists()`
                # silently succeeds against CWD (returning whatever happens
                # to live two levels above CWD), which produces a wrong-
                # but-existent lock-root — exactly the silent-divergence
                # bug C2 was meant to fix. Resolve here so the rest of the
                # logic works on the canonical absolute path.
                if not gitdir.is_absolute():
                    gitdir = (git_entry.parent / gitdir).resolve()
                # `<repo>/.git/worktrees/<name>` → parents[1] = `<repo>/.git`,
                # parents[2] = `<repo>`. Use parents[2] for the common root.
                # Guard against unexpected gitdir layout: if parents[2] does
                # not exist, fall back to cand.
                try:
                    common_root = gitdir.parents[1].parent
                except IndexError:
                    return cand
                if common_root.exists():
                    return common_root
            return cand
    return here.parent


def _lock_root() -> Path:
    if _LOCK_ROOT_OVERRIDE is not None:
        return Path(_LOCK_ROOT_OVERRIDE)
    env = os.environ.get("KALSHI_SESSION_LOCK_ROOT")
    if env:
        return Path(env)
    return _repo_root() / ".claude" / "locks" / "active-work"


def _reclaim_log_path() -> Path:
    if _RECLAIM_LOG_OVERRIDE is not None:
        return Path(_RECLAIM_LOG_OVERRIDE)
    env = os.environ.get("KALSHI_SESSION_LOCK_RECLAIM_LOG")
    if env:
        return Path(env)
    return _repo_root() / ".claude" / "locks" / "reclaim.log"


# ---------------------------------------------------------------------------
# Path-flatten helpers
# ---------------------------------------------------------------------------


def flatten_target_path(target_path: str) -> str:
    """Encode a relative POSIX path into a single-segment lockfile basename.

    Refuses absolute paths, traversal (`..`), and empty components — those
    inputs would either escape the lock dir or produce ambiguous round-trips.

    Refuses inputs that already contain the literal `__SLASH__` token, since
    `flatten("a__SLASH__b/c")` and `flatten("a/b/c")` would otherwise collide
    on the same basename (R1 M1 — injectivity).

    Also refuses inputs where any component ENDS with ``__SLASH`` or STARTS
    with ``SLASH__`` (R4 M1 — boundary-straddling injectivity). The R1 M1
    rule only rejected the contiguous literal ``__SLASH__`` token, but two
    inputs whose components straddle that token across a `/` boundary still
    collide once joined::

        flatten('a__SLASH/b')  → 'a__SLASH__SLASH__b'
        flatten('a/SLASH__b')  → 'a__SLASH__SLASH__b'   # COLLISION

    Neither input contains the contiguous token, yet
    ``_SLASH_MARKER.join(parts)`` reproduces it across the join. Rejecting
    components that touch the marker boundary closes the gap. The two
    practical sub-checks (`startswith("SLASH__")` and `endswith("__SLASH")`)
    cover every non-empty proper suffix/prefix of the marker because any
    longer overlap subsumes one of these — e.g. ``_SLASH_`` ending matches
    ``__SLASH`` ending; ``LASH__`` starting matches ``SLASH__`` starting.

    Refuses NUL bytes and other ASCII control chars, which would crash inside
    `os.open` with `ValueError: embedded null byte` (R1 M2).
    """
    if not target_path:
        raise ValueError("target_path must be non-empty")
    if target_path.startswith("/"):
        raise ValueError(f"absolute paths not allowed: {target_path!r}")
    # Reject NUL + other ASCII control characters (0x00-0x1f + 0x7f) — they
    # either crash os.open or render the lockfile name un-greppable.
    for ch in target_path:
        if ord(ch) < 0x20 or ord(ch) == 0x7F:
            raise ValueError(
                f"control character {ord(ch):#04x} not allowed in target_path: "
                f"{target_path!r}"
            )
    # Reject literal __SLASH__ in the input — would collide with the encoded
    # form of a sibling path containing a real "/" at the same position.
    if _SLASH_MARKER in target_path:
        raise ValueError(
            f"target_path contains reserved slash-marker token "
            f"{_SLASH_MARKER!r}: {target_path!r}"
        )
    parts = target_path.split("/")
    if any(p == "" for p in parts):
        raise ValueError(f"empty path component in {target_path!r}")
    if any(p in (".", "..") for p in parts):
        raise ValueError(f"traversal not allowed: {target_path!r}")
    # R4 M1 — reject components whose suffix/prefix would straddle the
    # _SLASH_MARKER across a `/` boundary in the joined output. Without
    # this guard, `flatten('a__SLASH/b')` and `flatten('a/SLASH__b')` both
    # yield `'a__SLASH__SLASH__b'` and unflatten is non-bijective.
    for comp in parts:
        if comp.endswith("__SLASH") or comp.startswith("SLASH__"):
            raise ValueError(
                f"path component {comp!r} straddles slash-marker boundary "
                f"in {target_path!r}"
            )
    return _SLASH_MARKER.join(parts)


def unflatten_target_path(flat: str) -> str:
    """Inverse of `flatten_target_path`."""
    return flat.replace(_SLASH_MARKER, "/")


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class LockHeldError(RuntimeError):
    """Raised when a lock is held by another (live) session.

    `held_by` carries the parsed JSON metadata of the holder so callers
    can render a useful diagnostic ("X is editing bot/_impl.py — pid 1234,
    started 12s ago").
    """

    def __init__(self, held_by: Dict[str, Any]):
        self.held_by = held_by
        super().__init__(
            f"lock held by pid={held_by.get('pid')!r} "
            f"session={held_by.get('session_id')!r} "
            f"target={held_by.get('target_path')!r}"
        )


# ---------------------------------------------------------------------------
# Stale + quarantine helpers
# ---------------------------------------------------------------------------


def _now() -> float:
    return time.time()


def _read_lockfile_metadata(path: Path) -> Optional[Dict[str, Any]]:
    """Parse JSON metadata. Returns None on any read/parse error or if
    required keys are missing — caller treats None as "malformed"."""
    try:
        raw = path.read_text(encoding="utf-8")
    except (FileNotFoundError, OSError):
        return None
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    if not _REQUIRED_KEYS.issubset(data.keys()):
        return None
    # Type sanity. last_heartbeat must be numeric, target_path str.
    try:
        float(data["last_heartbeat"])
        float(data["started_at"])
    except (TypeError, ValueError):
        return None
    if not isinstance(data.get("target_path"), str):
        return None
    return data


def is_stale(path: Path, now: Optional[float] = None) -> bool:
    """True iff lockfile exists, parses cleanly, and last_heartbeat is
    older than STALE_THRESHOLD_S. Malformed/missing → False (callers
    handle malformed via the quarantine path, not the stale path)."""
    meta = _read_lockfile_metadata(path)
    if meta is None:
        return False
    # Use explicit None check, not `or`: now=0.0 is a valid (if absurd)
    # caller-supplied clock value and must not silently fall through to
    # _now().
    eff_now = _now() if now is None else now
    return eff_now - float(meta["last_heartbeat"]) > STALE_THRESHOLD_S


def _audit_log(line: str) -> None:
    """Append a single audit line to the reclaim log. Best-effort: a log
    write failure must NEVER block the actual lock acquire."""
    log_path = _reclaim_log_path()
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(line.rstrip("\n") + "\n")
    except OSError:
        # Audit is non-load-bearing for correctness. Swallow + continue.
        pass


def _quarantine_malformed(path: Path) -> None:
    """Move a malformed lockfile aside so a fresh acquire can proceed.

    Sidecars are named `<flat>.<unix-millis>.quarantine` so multiple
    quarantines on the same target path don't clobber each other.
    """
    if not path.exists():
        return
    # Stamp = unix-millis + reclaimer pid. Two sessions racing to
    # quarantine the same path produce distinct sidecars rather than
    # one clobbering the other.
    stamp = f"{int(_now() * 1000)}.{os.getpid()}"
    sidecar = path.with_name(f"{path.stem}.{stamp}.quarantine")
    try:
        os.replace(str(path), str(sidecar))
    except OSError:
        # If we can't move it, try to unlink. Last-ditch.
        try:
            path.unlink()
        except OSError:
            return
    _audit_log(
        json.dumps(
            {
                "event": "QUARANTINE",
                "ts": _now(),
                "path": str(path),
                "sidecar": str(sidecar),
                "reclaimer_pid": os.getpid(),
            }
        )
    )


def _reclaim_stale(path: Path, prior_meta: Dict[str, Any]) -> None:
    """Unlink a stale lockfile and emit an audit-log entry."""
    try:
        path.unlink()
    except FileNotFoundError:
        pass  # Someone else won the race; that's fine, our acquire retries.
    except OSError:
        return
    _audit_log(
        json.dumps(
            {
                "event": "RECLAIM_STALE",
                "ts": _now(),
                "path": str(path),
                "stale_since": prior_meta.get("last_heartbeat"),
                "stale_pid": prior_meta.get("pid"),
                "stale_session_id": prior_meta.get("session_id"),
                "reclaimer_pid": os.getpid(),
            }
        )
    )


# ---------------------------------------------------------------------------
# SessionLock
# ---------------------------------------------------------------------------


class SessionLock:
    """Per-target-path mutex lockfile.

    Usage::

        with SessionLock("bot/_impl.py").acquire():
            do_protected_work()

    Or explicitly::

        lock = SessionLock("bot/_impl.py")
        lock.acquire_raw()
        try:
            ...
        finally:
            lock.release()
    """

    def __init__(self, target_path: str, *, claude_session_marker: str = ""):
        # Validate + flatten eagerly so misuse fails at construction.
        self._flat = flatten_target_path(target_path)
        self.target_path = target_path
        self._claude_session_marker = claude_session_marker or os.environ.get(
            "CLAUDE_SESSION_ID", ""
        )
        self._session_id = uuid.uuid4().hex
        self._held = False
        self._heartbeat_thread: Optional[threading.Thread] = None
        self._heartbeat_stop = threading.Event()
        # `metadata` is the in-memory view of what we wrote — populated on
        # successful acquire. Useful for diagnostics + tests.
        self.metadata: Dict[str, Any] = {}

    @property
    def lockfile_path(self) -> Path:
        return _lock_root() / f"{self._flat}.lock"

    # ------------------------------------------------------------------
    # Acquire / release
    # ------------------------------------------------------------------

    def _build_metadata(self) -> Dict[str, Any]:
        now = _now()
        return {
            "pid": os.getpid(),
            "session_id": self._session_id,
            "target_path": self.target_path,
            "started_at": now,
            "last_heartbeat": now,
            "claude_session_marker": self._claude_session_marker,
        }

    def _try_create(self) -> bool:
        """Attempt one atomic-write lockfile create. Returns True on success.

        On lockfile-already-exists returns False (caller decides whether to
        inspect for stale/malformed and retry).

        R1 C1 — TOCTOU fix
        ------------------
        Previously this used O_CREAT|O_EXCL on the canonical lockfile name,
        wrote+fsync'd, then closed. The window between `os.open` succeeding
        and `os.write` completing left the lockfile observable as a 0-byte
        empty file. A peer doing `acquire_raw` against the same target
        could in that window:

          1. See EEXIST on its own O_EXCL attempt.
          2. `_read_lockfile_metadata` → JSONDecodeError on empty file → None.
          3. Conclude "malformed", call `_quarantine_malformed`, which
             `os.replace`s the (still-being-written) file out from under us.
          4. Retry O_EXCL — now succeeds (file is gone) — and BOTH sessions
             think they hold the lock.

        Fix: write+fsync to a per-pid+per-session tmp file in the same
        directory, then `os.rename` (POSIX-atomic) the tmp file onto the
        canonical lockfile name. Atomicity properties:

          * Peers see no file at the canonical path (acquire wins) OR see
            a fully-written, parseable lockfile (acquire blocks cleanly).
          * Never see a 0-byte/partial canonical lockfile.
          * `os.rename` to an existing destination on POSIX is documented
            as atomic-and-overwriting; we want overwrite to FAIL, so we
            link()+unlink() instead — `os.link(tmp, canonical)` raises
            FileExistsError if canonical exists; the tmp is then unlinked.
            This matches the O_CREAT|O_EXCL semantics on the canonical
            name without the partial-write window.

        Mirrors the `_tick_heartbeat` tmp+rename pattern, but the rename
        target must NOT pre-exist on creation (whereas heartbeat OVERWRITES
        intentionally). Hence link/unlink, not rename.
        """
        path = self.lockfile_path
        path.parent.mkdir(parents=True, exist_ok=True)
        # Tmp name includes pid + session_id[:8] so concurrent creators
        # don't collide on the same tmp file.
        tmp = path.with_name(
            f"{path.name}.tmp.{os.getpid()}.{self._session_id[:8]}"
        )
        meta = self._build_metadata()
        payload = json.dumps(meta).encode("utf-8")
        try:
            # O_CREAT|O_EXCL on the *tmp* path: harmless if a prior
            # interrupted attempt left a stale tmp behind (unlink + retry).
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
            try:
                fd = os.open(str(tmp), flags, 0o600)
            except FileExistsError:
                # Stale tmp from a previous crashed attempt by this same
                # (pid, session_id) prefix. Unlink + one retry.
                try:
                    tmp.unlink()
                except FileNotFoundError:
                    pass
                fd = os.open(str(tmp), flags, 0o600)
            try:
                os.write(fd, payload)
                try:
                    os.fsync(fd)
                except OSError:
                    pass
            finally:
                os.close(fd)
            # Atomic publish: link tmp → canonical. link() fails with
            # FileExistsError if canonical already exists, giving us the
            # O_EXCL semantics WITHOUT the partial-write race.
            try:
                os.link(str(tmp), str(path))
            except FileExistsError:
                return False
            except OSError as e:
                if e.errno == errno.EEXIST:
                    return False
                raise
            finally:
                # tmp is now hardlinked to canonical (or link failed).
                # Either way, drop the tmp name; canonical retains the
                # inode via the hardlink. If link failed, this is just
                # cleanup of the failed-publish tmp.
                try:
                    tmp.unlink()
                except FileNotFoundError:
                    pass
            self.metadata = meta
            return True
        except OSError:
            # Best-effort cleanup of tmp on unexpected error.
            try:
                tmp.unlink()
            except (FileNotFoundError, OSError):
                pass
            raise

    def acquire_raw(self) -> "SessionLock":
        """Acquire without context-manager wrapping. Caller MUST `release()`.

        Tries up to 3 passes, accommodating a stale or malformed lockfile
        on the first pass. After 3 failed passes raise LockHeldError —
        the holder is genuinely live.
        """
        if self._held:
            raise RuntimeError(
                f"SessionLock already held by this instance for "
                f"{self.target_path!r}"
            )
        for _ in range(3):
            if self._try_create():
                self._held = True
                self._start_heartbeat()
                return self
            # Lockfile exists. Inspect.
            existing = self.lockfile_path
            meta = _read_lockfile_metadata(existing)
            if meta is None:
                # Malformed / corrupt → quarantine + retry.
                _quarantine_malformed(existing)
                continue
            if is_stale(existing):
                _reclaim_stale(existing, meta)
                continue
            # Live, well-formed lock — caller blocked.
            raise LockHeldError(meta)
        # Three retries exhausted. Final read to give caller best-effort
        # held-by metadata.
        meta = _read_lockfile_metadata(self.lockfile_path) or {
            "pid": None,
            "session_id": None,
            "target_path": self.target_path,
            "last_heartbeat": None,
            "started_at": None,
        }
        raise LockHeldError(meta)

    def acquire(self) -> "_LockContext":
        """Context-manager-friendly acquire. ``with lock.acquire(): ...``"""
        self.acquire_raw()
        return _LockContext(self)

    def release(self) -> None:
        """Release. Idempotent; safe to call multiple times or pre-acquire."""
        if not self._held:
            return
        self._stop_heartbeat()
        try:
            self.lockfile_path.unlink()
        except FileNotFoundError:
            # Already gone — possibly reclaimed by a peer that judged us
            # stale. Not an error; just log it for forensic audit.
            _audit_log(
                json.dumps(
                    {
                        "event": "RELEASE_MISSING",
                        "ts": _now(),
                        "path": str(self.lockfile_path),
                        "session_id": self._session_id,
                        "pid": os.getpid(),
                    }
                )
            )
        except OSError:
            pass
        finally:
            self._held = False

    # ------------------------------------------------------------------
    # Heartbeat thread
    # ------------------------------------------------------------------

    def _start_heartbeat(self) -> None:
        self._heartbeat_stop.clear()
        t = threading.Thread(
            target=self._heartbeat_loop,
            name=f"SessionLock-heartbeat-{self._flat}",
            daemon=True,
        )
        self._heartbeat_thread = t
        t.start()

    def _stop_heartbeat(self) -> None:
        self._heartbeat_stop.set()
        t = self._heartbeat_thread
        if t is not None and t is not threading.current_thread():
            t.join(timeout=max(2 * HEARTBEAT_INTERVAL_S, 1.0))
        self._heartbeat_thread = None

    def _heartbeat_loop(self) -> None:
        # Wait FIRST, then tick. The lockfile already has a fresh
        # last_heartbeat from acquire() — re-writing it immediately would
        # (1) race with any callers about to manipulate the file before
        # the first interval elapses, and (2) waste a write. wait() returns
        # True if the event is set during the wait — bail promptly so
        # release() isn't blocked behind a 30 s interval.
        while True:
            if self._heartbeat_stop.wait(HEARTBEAT_INTERVAL_S):
                return
            try:
                self._tick_heartbeat()
            except OSError:
                # File gone (reclaimed by peer)? Loop will exit on next
                # release(). Don't crash the daemon thread.
                pass

    def _tick_heartbeat(self) -> None:
        # Race window note: between our session_id-match check and the
        # os.replace below, a peer who judged us stale could (a) unlink
        # the file, then (b) start writing their own. Our os.replace
        # would then clobber theirs. Acceptable degradation, because:
        #   * a peer only reclaims if our last_heartbeat is >180s old;
        #   * if we're ticking, our heartbeat is by definition fresh,
        #     so no live peer would have judged us stale at that moment;
        #   * the tail-risk window is the tens of microseconds between
        #     read and rename — essentially nil in practice.
        # If pathological behavior emerges later, fix is to use a
        # filesystem-level advisory lock (fcntl) on the lockfile during
        # the rename; deferred as out-of-scope for P5.1.
        path = self.lockfile_path
        if not path.exists():
            return
        meta = _read_lockfile_metadata(path)
        if meta is None or meta.get("session_id") != self._session_id:
            # Either malformed or another session reclaimed us — leave
            # alone; the heartbeat is no longer our authority to touch.
            return
        meta["last_heartbeat"] = _now()
        # Atomic update: write tmp + rename. Tmp name includes both pid
        # and session_id so two locks held by the same process for the
        # same target (impossible by acquire_raw's invariant, but be
        # defensive) could not collide on the tmp file.
        tmp = path.with_name(
            f"{path.name}.tmp.{os.getpid()}.{self._session_id[:8]}"
        )
        try:
            tmp.write_text(json.dumps(meta), encoding="utf-8")
            os.replace(str(tmp), str(path))
            self.metadata = meta
        except OSError:
            # Best-effort. If rename fails, peer lock-holders will see us
            # as stale eventually; that's the correct degradation.
            try:
                tmp.unlink()
            except OSError:
                pass


class _LockContext:
    """Trivial ctx-manager wrapper around an already-acquired SessionLock.

    Returning the lock itself from `__enter__` lets callers introspect
    metadata while inside the `with` block.
    """

    def __init__(self, lock: SessionLock):
        self._lock = lock

    def __enter__(self) -> SessionLock:
        return self._lock

    def __exit__(self, exc_type, exc, tb) -> None:
        self._lock.release()
