"""Reader for the day-partitioned reliable-NBBO cache built by
`scripts.research.build_nbbo_cache` (GENHUNT 2026-06-11 §5 infrastructure).

Every future evaluator consumes a sealed frames day in 3 lines:

    from scripts.research import nbbo_cache
    for day, book in nbbo_cache.load_days("~/kalshi-research-data/fairvalue"):
        ...  # book[ticker] -> reliable NBBO timeline, no frames re-stream

SCHEMA (must stay lock-step with build_nbbo_cache's docstring):
  <corpus>/frames_nbbo/day=<YYYY-MM-DD>.pkl.zst
    Logical content: {ticker: list[(ts_epoch_float, yes_bid_c_or_None,
                                    yes_ask_c_or_None)]}
    — EXACTLY `early_exit_backtest.reliable_nbbo_timeline` over that ticker's
    day frames (snapshot-anchored, drift-refusing, never-crossed NBBO;
    unanchored/crossed points are absent; prices are cents floats, None for an
    empty side). Physical encoding: zstd-compressed concatenated pickle
    records, one `(ticker, timeline)` tuple each, EOF-terminated; `load_day`
    reassembles the dict.
  <corpus>/frames_nbbo/.done_<YYYY-MM-DD>
    Day-keyed completion marker (JSON funnel stats). A cached day is valid IFF
    its marker exists — NEVER trust mtimes (rclone preserves source mtimes;
    that trap silently truncated a strategy corpus on 2026-06-10).
"""
from __future__ import annotations

import io
import os
import pickle
import subprocess
from scripts.research.zstd_stream import assert_zstd_ok  # noqa: E402  (repo root on sys.path above)


def _cache_dir(corpus: str) -> str:
    return os.path.join(os.path.expanduser(corpus), "frames_nbbo")


def available_days(corpus: str) -> list:
    """Sorted YYYY-MM-DD days whose cache is COMPLETE (.done_<day> marker AND
    day=<day>.pkl.zst both present)."""
    d = _cache_dir(corpus)
    if not os.path.isdir(d):
        return []
    return sorted(
        name[len(".done_"):] for name in os.listdir(d)
        if name.startswith(".done_")
        and os.path.exists(os.path.join(d, f"day={name[len('.done_'):]}.pkl.zst"))
    )


def load_day(corpus: str, day: str) -> dict:
    """{ticker: [(ts_epoch_float, yes_bid_c_or_None, yes_ask_c_or_None), ...]}
    for one cached day. Raises FileNotFoundError if the day's .done marker is
    absent (an unmarked file may be a truncated partial — refuse it)."""
    d = _cache_dir(corpus)
    done = os.path.join(d, f".done_{day}")
    path = os.path.join(d, f"day={day}.pkl.zst")
    if not os.path.exists(done):
        raise FileNotFoundError(
            f"{done} missing — day {day} not sealed in the NBBO cache "
            f"(run scripts.research.build_nbbo_cache)")
    proc = None
    _zlib_path = None  # set when the zstandard branch is taken (see below)
    try:
        import zstandard
        fh = open(path, "rb")
        stream = io.BufferedReader(
            zstandard.ZstdDecompressor().stream_reader(fh))
        # ticket 86bbvrx1t: this branch is PREFERRED over the Popen fallback
        # and the library raises NOTHING on a truncated frame. Record the path
        # so the finally block can assert frame completion; the sibling Popen
        # branch is already covered by assert_zstd_ok.
        _zlib_path = path
    except ImportError:
        proc = subprocess.Popen(["zstd", "-dc", path], stdout=subprocess.PIPE,
                                bufsize=1 << 20)
        fh, stream = None, proc.stdout
    out = {}
    try:
        while True:
            try:
                tk, tl = pickle.load(stream)
            except EOFError:
                break
            out[tk] = tl
    finally:
        stream.close()
        if fh is not None:
            fh.close()
        if _zlib_path is not None:
            # ticket 86bbvrx1t: re-decode to confirm the frame terminated.
            # decompressobj.eof is the only reliable signal — byte counts do
            # not work, since a truncated file IS fully consumed.
            import zstandard as _z
            _d = _z.ZstdDecompressor().decompressobj()
            with open(_zlib_path, "rb") as _vfh:
                while True:
                    _c = _vfh.read(1 << 20)
                    if not _c:
                        break
                    _d.decompress(_c)
            if not _d.eof:
                raise RuntimeError(
                    f"{_zlib_path}: zstd frame did NOT terminate — the NBBO "
                    f"cache day is TRUNCATED and its timelines are partial.")
        if proc is not None:
            proc.wait()
            # ticket 86bbvrx1t: the unpickle loop exits only on EOFError, i.e.
            # a true EOF, so a non-zero zstd exit means the cache file was
            # TRUNCATED and this day's timelines are silently partial.
            assert_zstd_ok(proc, path, exhausted=True, require_nonempty=False)
    return out


def load_days(corpus: str, days=None):
    """Iterate (day, {ticker: timeline}) over `days` (default: every available
    day, ascending). One day in memory at a time."""
    for day in (days if days is not None else available_days(corpus)):
        yield day, load_day(corpus, day)
