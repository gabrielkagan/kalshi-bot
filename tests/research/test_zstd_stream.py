"""Tests for the shared checked zstd reader (ticket 86bbvrx1t).

The two load-bearing cases are the truncation test (which must RAISE) and the
early-break test (which must NOT raise). The second is what stops the fix from
breaking every early-exit consumer — a naive `if rc != 0: raise` passes the first
test and fails the second, which is precisely why the naive fix is wrong.
"""

from __future__ import annotations

import shutil
import subprocess

import pytest

from scripts.research.zstd_stream import (
    ZstdTruncatedError,
    assert_zstd_ok,
    checked_stream_lines,
)

pytestmark = pytest.mark.skipif(
    shutil.which("zstd") is None, reason="zstd binary not installed"
)


def _write_zst(path, n_lines):
    raw = path.with_suffix("")
    raw.write_text("".join(f'{{"i":{i}}}\n' for i in range(n_lines)))
    subprocess.run(["zstd", "-q", "-f", str(raw), "-o", str(path)], check=True)
    return path


def test_reads_a_good_file_completely(tmp_path):
    f = _write_zst(tmp_path / "good.jsonl.zst", 500)
    assert len(list(checked_stream_lines(str(f)))) == 500


def test_raises_on_a_truncated_stream(tmp_path):
    """A frame cut in half: zstd exits non-zero after emitting a prefix.

    This is the exact shape that silently produced 6.0M of 20.5M lines in the
    Track E build that motivated this module.
    """
    f = _write_zst(tmp_path / "trunc.jsonl.zst", 200_000)
    data = f.read_bytes()
    f.write_bytes(data[: len(data) // 2])

    with pytest.raises(ZstdTruncatedError) as exc:
        list(checked_stream_lines(str(f)))
    assert "TRUNCATED" in str(exc.value)


def test_does_not_raise_when_the_caller_breaks_early(tmp_path):
    """Early break kills zstd with SIGPIPE — legitimate, must NOT raise.

    Guards against the naive `if rc != 0: raise` regression.
    """
    f = _write_zst(tmp_path / "big.jsonl.zst", 200_000)
    seen = 0
    for _ in checked_stream_lines(str(f)):
        seen += 1
        if seen == 5:
            break
    assert seen == 5


def test_raises_on_a_file_that_yields_nothing(tmp_path):
    f = _write_zst(tmp_path / "empty.jsonl.zst", 0)
    with pytest.raises(ZstdTruncatedError) as exc:
        list(checked_stream_lines(str(f)))
    assert "ZERO lines" in str(exc.value)


def test_nonempty_guard_can_be_disabled(tmp_path):
    f = _write_zst(tmp_path / "empty2.jsonl.zst", 0)
    assert list(checked_stream_lines(str(f), require_nonempty=False)) == []


def test_plain_uncompressed_file_streams_too(tmp_path):
    f = tmp_path / "plain.jsonl"
    f.write_text('{"i":1}\n{"i":2}\n')
    assert len(list(checked_stream_lines(str(f)))) == 2


def test_bytes_mode_yields_bytes(tmp_path):
    f = _write_zst(tmp_path / "b.jsonl.zst", 3)
    out = list(checked_stream_lines(str(f), decode=False))
    assert all(isinstance(x, bytes) for x in out) and len(out) == 3


def test_assert_zstd_ok_is_a_noop_on_a_non_exhausted_read(tmp_path):
    """The pipeline primitive must tolerate SIGPIPE on an abandoned read."""
    f = _write_zst(tmp_path / "p.jsonl.zst", 50_000)
    z = subprocess.Popen(["zstd", "-dc", str(f)], stdout=subprocess.PIPE)
    assert z.stdout is not None
    z.stdout.readline()
    z.stdout.close()
    z.wait()
    assert_zstd_ok(z, str(f), exhausted=False)  # must not raise


def test_assert_zstd_ok_raises_for_an_exhausted_failed_read(tmp_path):
    f = _write_zst(tmp_path / "q.jsonl.zst", 200_000)
    data = f.read_bytes()
    f.write_bytes(data[: len(data) // 2])
    z = subprocess.Popen(
        ["zstd", "-dc", str(f)], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL
    )
    assert z.stdout is not None
    n = sum(1 for _ in z.stdout)
    z.stdout.close()
    z.wait()
    with pytest.raises(ZstdTruncatedError):
        assert_zstd_ok(z, str(f), exhausted=True, n_lines=n)
