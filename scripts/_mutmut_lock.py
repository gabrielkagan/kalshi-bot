"""Cross-platform exclusive-lock wrapper for mutmut + tier-test recipes.

Ticket 86b9vgh1a (Pillar 5 followup) of testing-foundation-sprint
(parent 86b9ve0wa).

## Why

`make test-mutmut` runs `mutmut run` which mutates
`bot/engines/{volatility,probability}.py` in-place during the ~1-2h
baseline. If `make test-equivalence` / `test-integration` runs
concurrently it will see the mutated source and surface false test
failures — wasting an hour of agent or operator time chasing a
non-bug. The prior mitigation was a `tests/CLAUDE.md` prose warning
("Don't run `make test-mutmut` in parallel with the test tiers"),
which is honor-system; this script enforces the same contract
structurally.

## Why not flock(1)

`flock(1)` is Linux-native (util-linux). macOS ships no `flock` in
`/usr/bin/` and the dev box is macOS, so a `flock --nonblock
--exclusive <path> -- <cmd>` recipe would error out with `flock:
command not found` (or worse, silently no-op behind a `|| true`).
This script uses `fcntl.flock(LOCK_EX | LOCK_NB)` which is Python
stdlib on both darwin and Linux.

## Behavior

* CLI shape:

    python scripts/_mutmut_lock.py acquire <lockfile> -- <cmd> [args...]

* Opens `<lockfile>` with `O_CREAT` so the file appears on first
  invocation. The lockfile contents are NEVER read or written — fcntl
  locks the FD, not the path. The file is left on disk; gitignored
  via `.mutmut.lock` entry.

* Calls `fcntl.flock(fd, LOCK_EX | LOCK_NB)`. The LOCK_NB flag is
  load-bearing: without it, contending invocations BLOCK indefinitely
  rather than fail-fast, which masks the concurrency bug. We want
  loud failure.

* On contention:
    - Prints a clear, operator-actionable error to stderr.
    - Exits with status 2 (distinguishable from the inner command's
      typical 0/1).

* On acquisition:
    - Execs the inner command via `os.execvp` so the parent process
      exits cleanly (signals propagate naturally; PID accounting
      stays sane). NOTE: on Linux exec keeps the file descriptors
      open across the exec, so the kernel-level fcntl lock is
      preserved by the child process until it exits. Same on
      darwin.
    - If `os.execvp` itself fails (e.g., binary not found on PATH),
      prints to stderr and exits 127 (matches POSIX `command not
      found` convention).

* Lock is released automatically when the (exec'd) process exits —
  fcntl locks are tied to the open file description; closing the FD
  (which `exit` does for all FDs) releases the lock.

## Self-test

    python scripts/_mutmut_lock.py --self-test

Runs a quick acceptance test: spawns a 0.5s holder, attempts a
contender, asserts the contender exits non-zero with a clear
message.
"""

from __future__ import annotations

import fcntl
import os
import sys
from typing import List, NoReturn


_EXIT_LOCK_HELD = 2
_EXIT_EXEC_FAILED = 127
_EXIT_USAGE = 64  # EX_USAGE per /usr/include/sysexits.h


def _usage() -> NoReturn:
    sys.stderr.write(
        "usage: python _mutmut_lock.py acquire <lockfile> -- <cmd> [args...]\n"
        "       python _mutmut_lock.py --self-test\n"
    )
    sys.exit(_EXIT_USAGE)


def _acquire(lockfile: str, command: List[str]) -> NoReturn:
    """Acquire exclusive lock on `lockfile`, then exec `command`.

    The lockfile is opened with O_CREAT|O_RDWR so it appears on
    first invocation. The mode 0o644 mirrors typical Unix sentinel-
    file conventions; an existing file's mode is preserved
    (O_CREAT does not chmod existing files).
    """
    if not command:
        sys.stderr.write(
            "ERROR: no command to exec — usage:\n"
            "  python _mutmut_lock.py acquire <lockfile> -- <cmd> [args...]\n"
        )
        sys.exit(_EXIT_USAGE)
    # Open the lockfile FD. O_RDWR is broader than needed (we never
    # read/write the file) but matches the kernel-call shape most
    # operators expect to see in `lsof` output if they go debugging
    # a hung lock.
    fd = os.open(lockfile, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        # EWOULDBLOCK / EAGAIN — another process holds the lock.
        # fcntl raises BlockingIOError (subclass of OSError) on
        # contention; catch the broader OSError so any fcntl
        # error surfaces cleanly.
        sys.stderr.write(
            f"ERROR: another mutmut/tier-test invocation holds the "
            f"lock at {lockfile!r} (fcntl.flock LOCK_NB rejected: {exc}).\n"
            f"\n"
            f"Ticket 86b9vgh1a: concurrent `make test-mutmut` + `make "
            f"test-equivalence` / `test-integration` would race against\n"
            f"mutmut's in-place mutation of bot/engines/. Wait for the "
            f"holder to finish, OR check `lsof {lockfile}` to identify\n"
            f"the holder if you believe it's stuck.\n"
        )
        os.close(fd)
        sys.exit(_EXIT_LOCK_HELD)
    # Write our PID to the lockfile for debuggability — purely
    # advisory; the kernel-level fcntl lock is what enforces
    # mutual exclusion. Operators running `cat .mutmut.lock` can
    # see which PID to wait on.
    try:
        os.ftruncate(fd, 0)
        os.write(fd, f"{os.getpid()}\n".encode())
    except OSError:
        # Best-effort; never fail the lock acquisition because the
        # advisory write hiccupped.
        pass
    # PEP 446 — since Python 3.4, `os.open` sets FD_CLOEXEC by
    # default on all new file descriptors. If we don't clear it,
    # `os.execvp` closes the FD as part of the exec, which
    # IMMEDIATELY releases the fcntl lock — a contender spawned
    # 0.1s later would acquire it cleanly, defeating the entire
    # guard. Confirmed by hand on macOS 24.5.0 / Python 3.9.6
    # before this fix was added.
    #
    # Clear the close-on-exec flag so the FD survives execvp;
    # the fcntl lock is associated with the FD, so the lock is
    # then held by the (post-exec) inner process until it exits
    # and the kernel reaps its FDs.
    try:
        flags = fcntl.fcntl(fd, fcntl.F_GETFD)
        fcntl.fcntl(fd, fcntl.F_SETFD, flags & ~fcntl.FD_CLOEXEC)
    except OSError as exc:
        # If we can't clear CLOEXEC, the lock would be released on
        # exec — bail loudly. Don't silently exec into the inner
        # command without the guarantee we promised.
        sys.stderr.write(
            f"ERROR: failed to clear FD_CLOEXEC on lockfile fd: {exc}. "
            f"Refusing to exec without the lock guarantee.\n"
        )
        os.close(fd)
        sys.exit(_EXIT_EXEC_FAILED)
    # Hand off to the inner command. execvp inherits the FD (and
    # therefore the fcntl lock) until the inner process exits.
    # Use execvp (not execv) so `mutmut` / `python3` / `true` etc.
    # resolve against PATH.
    try:
        os.execvp(command[0], command)
    except OSError as exc:
        sys.stderr.write(
            f"ERROR: failed to exec {command[0]!r}: {exc}\n"
        )
        # FD will be closed by interpreter exit, which releases
        # the lock — good. The contender won't see a stuck lock
        # from a typo'd exec target.
        sys.exit(_EXIT_EXEC_FAILED)
    # Unreachable — execvp either replaces the process or raises.
    sys.exit(_EXIT_EXEC_FAILED)


def _self_test() -> int:
    """Lightweight acceptance: spawn a 0.5s holder, attempt
    contender, assert contender fails-fast with a usable error.

    Returns 0 on pass, non-zero on regression. Used by the test
    harness AND by operators sanity-checking a fresh checkout.
    """
    import subprocess
    import tempfile
    import time

    with tempfile.TemporaryDirectory() as td:
        lockfile = os.path.join(td, "test.lock")
        holder = subprocess.Popen(
            [
                sys.executable,
                os.path.abspath(__file__),
                "acquire",
                lockfile,
                "--",
                "sh",
                "-c",
                "sleep 0.5",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        # Brief wait for holder to acquire the FD lock. fcntl.flock
        # is synchronous so once the holder process exists past
        # `os.execvp`, the lock is held — but `subprocess.Popen`
        # returns BEFORE that point. 0.1s is enough on modern
        # hardware; 0.2s for headroom on slow CI.
        time.sleep(0.2)
        contender = subprocess.run(
            [
                sys.executable,
                os.path.abspath(__file__),
                "acquire",
                lockfile,
                "--",
                "true",
            ],
            capture_output=True,
            text=True,
            timeout=5,
        )
        holder.communicate(timeout=5)

    if holder.returncode != 0:
        sys.stderr.write(
            f"SELF-TEST FAIL: holder exited {holder.returncode} (expected 0).\n"
        )
        return 1
    if contender.returncode != _EXIT_LOCK_HELD:
        sys.stderr.write(
            f"SELF-TEST FAIL: contender exited {contender.returncode} "
            f"(expected {_EXIT_LOCK_HELD}).\n"
            f"  stderr: {contender.stderr!r}\n"
        )
        return 1
    if "lock" not in contender.stderr.lower():
        sys.stderr.write(
            f"SELF-TEST FAIL: contender stderr lacks 'lock' keyword — "
            f"operator-actionability regression.\n"
            f"  stderr: {contender.stderr!r}\n"
        )
        return 1
    sys.stdout.write("SELF-TEST OK: lock contention rejected as expected.\n")
    return 0


def main(argv: List[str]) -> NoReturn:
    if len(argv) < 2:
        _usage()
    if argv[1] == "--self-test":
        sys.exit(_self_test())
    if argv[1] != "acquire":
        _usage()
    if len(argv) < 4 or argv[3] != "--":
        _usage()
    lockfile = argv[2]
    command = argv[4:]
    _acquire(lockfile, command)


if __name__ == "__main__":
    main(sys.argv)
