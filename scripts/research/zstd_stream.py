"""Checked zstd streaming — the ONE place research code decompresses bronze.

WHY THIS MODULE EXISTS (ticket 86bbvrx1t, 2026-09-06)
-----------------------------------------------------
The idiom this replaces is:

    proc = subprocess.Popen(["zstd", "-dc", path], stdout=subprocess.PIPE)
    for raw in proc.stdout:
        ...
    proc.wait()                      # <-- return value discarded

If the decompressor dies mid-file, `for raw in proc.stdout` simply STOPS. The loop
ends, the caller sees an ordinary end-of-iteration, and the run reports success
having read a PREFIX of the day. Nothing raises. Nothing logs. A short day looks
exactly like a quiet day.

An audit on 2026-09-06 found 21 of 28 research scripts doing exactly this. The
measured instance (Track E maker-markout): a build reported DONE with 7,050 windows
against 7,511 expected, because day 06-01 streamed 6,026,544 of 20,502,366 lines and
06-04 streamed 13.3M of 24.9M. The FILES WERE FINE — `zstd -t` passed on all of them,
mtimes unchanged. Only the discarded exit code. As that session put it: "217 windows
instead of 672 on one day does not announce itself in a pooled average."

That is why this is the most dangerous member of the silent-failure family: the other
instances produced something visibly wrong; this one produces something PLAUSIBLE.

THE SUBTLETY THAT MAKES THE NAIVE FIX WRONG
--------------------------------------------
`if proc.returncode != 0: raise` breaks every early-exit consumer. When a caller
`break`s out of the loop (or the generator is closed/garbage-collected), the pipe is
closed and zstd dies of SIGPIPE with a non-zero code — legitimately, by design.

The distinguishing state is whether the read reached EOF. Only an EXHAUSTED read can
be a truncation. Hence the `exhausted` flag, set solely by the `for...else` / the
statement after the loop, and checked as `exhausted and rc != 0`.

DIRECTION OF BIAS (why this matters more for nulls than for positives)
-----------------------------------------------------------------------
Truncation drops the END of a file, so the loss is never uniform. Any analysis whose
events cluster in the tail — game hours, window closes, settlement — loses
disproportionately the part carrying the signal, and the bias runs TOWARD a null.
A null computed over a silently-truncated corpus is the failure mode this guards.

`zstd -t` PROVES NOTHING ABOUT A READ. Track E's 06-01 passed integrity with all
20.5M lines present while the build had consumed 6.0M of them. Never substitute an
integrity check for a consumed-line count.

USAGE
-----
Plain case — replaces the whole idiom::

    from scripts.research.zstd_stream import checked_stream_lines
    for line in checked_stream_lines(path):
        ...

Pipeline case (`zstd -dc | grep`), where grep may legitimately exit 1 on no matches
but zstd must still have exited 0::

    z = subprocess.Popen(["zstd", "-dc", path], stdout=subprocess.PIPE)
    g = subprocess.Popen(filter_cmd, stdin=z.stdout, stdout=subprocess.PIPE)
    z.stdout.close()
    exhausted = False
    try:
        for raw in g.stdout:
            ...
        exhausted = True
    finally:
        g.stdout.close(); g.wait()
        assert_zstd_ok(z, path, exhausted=exhausted)

Reference for the shape: `scripts/research/power_analysis_probe.py` (one of the 7
files that already got this right) and `maker_markout_scale.checked_stream_lines`.
"""

from __future__ import annotations

import subprocess
from typing import Iterator, Optional

__all__ = ["checked_stream_lines", "assert_zstd_ok", "ZstdTruncatedError"]


class ZstdTruncatedError(RuntimeError):
    """A zstd stream that was read to EOF but whose decompressor exited non-zero.

    Distinct from a plain RuntimeError so callers that legitimately tolerate a bad
    day (a corpus-repair tool, say) can catch precisely this and nothing else.
    """


def assert_zstd_ok(
    proc: subprocess.Popen,
    path: str,
    *,
    exhausted: bool,
    n_lines: Optional[int] = None,
    require_nonempty: bool = True,
) -> None:
    """Raise if a fully-read zstd stream came from a failed decompressor.

    Call this AFTER `proc.wait()` (or let it wait). Safe to call on a
    non-exhausted read: it is a no-op, because an early `break` kills zstd with
    SIGPIPE and that is expected rather than an error.

    Args:
        proc: the `zstd -dc` process. Its `returncode` is read; `wait()` is called
            if the process has not been reaped yet.
        path: the file, for the error message.
        exhausted: True ONLY if the consumer read the stream to EOF.
        n_lines: lines actually yielded, included in the message when known.
        require_nonempty: also raise when an exhausted read yielded zero lines.
            zstd can exit 0 having written nothing (empty/degenerate file), and a
            silently empty day is the same class of failure as a truncated one.
    """
    rc = proc.returncode
    if rc is None:
        rc = proc.wait()
    if not exhausted:
        # Caller broke out early: zstd died of SIGPIPE by design. Not an error.
        return
    if rc != 0:
        seen = f" after {n_lines:,} lines" if n_lines is not None else ""
        raise ZstdTruncatedError(
            f"zstd exited {rc} while decompressing {path}{seen} — the stream was "
            f"TRUNCATED. Do not trust any result or cache built from this read. "
            f"(`zstd -t` may still pass on this file: integrity of the FILE is not "
            f"evidence about the READ.)"
        )
    if require_nonempty and n_lines == 0:
        raise ZstdTruncatedError(
            f"{path} yielded ZERO lines with a clean zstd exit — an empty day is "
            f"not silently acceptable; assert the expected count upstream."
        )


def checked_stream_lines(
    path: str,
    *,
    decode: bool = True,
    require_nonempty: bool = True,
    skip_blank: bool = True,
) -> Iterator:
    """Stream lines from a `.jsonl.zst` (or plain file), raising on a short read.

    Never buffers the whole file — multi-GB frame days are the normal input.

    Args:
        path: `.zst` files stream through `zstd -dc`; anything else is opened
            directly, so callers can pass either without branching.
        decode: yield `str` (utf-8, errors replaced). False yields raw `bytes`,
            which is meaningfully faster when the consumer greps before parsing.
        require_nonempty: raise if a fully-read file yielded no lines.
        skip_blank: drop whitespace-only lines (they are not data, and counting
            them would weaken the non-empty guard).

    Raises:
        ZstdTruncatedError: the read reached EOF but zstd exited non-zero, or the
            file yielded nothing. A caller that `break`s early never triggers this.
    """
    if not path.endswith(".zst"):
        with open(path, "rb") as fh:
            for raw in fh:
                if skip_blank and not raw.strip():
                    continue
                yield raw.decode("utf-8", "replace") if decode else raw
        return

    proc = subprocess.Popen(
        ["zstd", "-dc", path], stdout=subprocess.PIPE, bufsize=1 << 20
    )
    assert proc.stdout is not None
    n = 0
    exhausted = False  # set ONLY on a true EOF — see module docstring
    try:
        for raw in proc.stdout:
            if skip_blank and not raw.strip():
                continue
            n += 1
            yield raw.decode("utf-8", "replace") if decode else raw
        exhausted = True
    finally:
        try:
            proc.stdout.close()
        except Exception:
            pass
        proc.wait()
        assert_zstd_ok(
            proc, path, exhausted=exhausted, n_lines=n,
            require_nonempty=require_nonempty,
        )
