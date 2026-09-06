#!/bin/bash
# Journal rotation for kalshi-bot — hour-stamped, fail-closed (ticket 86bbvd50a).
#
# Tracked under ops/ since 2026-09-05. Source of truth for the VPS cron:
#   0 */4 * * * /bin/bash /home/botuser/kalshi-bot-repo/ops/rotate_journals.sh >> /home/botuser/kalshi-bot-repo/journal_archives/rotation.log 2>&1
# ops/install.sh validates this file (executable + `bash -n`) on every
# install; the crontab itself stays operator-owned (see ops/CLAUDE.md
# "Journal rotation").
#
# History: the previous, VPS-local, untracked copy named archives
# `<journal>_<UTC date>.jsonl` (date-only stamp). When PR #64 (2026-05-17) moved the cron
# from daily to every-4h, every run after the first of the day hit
# `zstd: ... already exists; not overwritten`, left the raw copy behind,
# and the NEXT run's `cp` clobbered it: 436 intermediate 4-hour chunks
# per journal (opportunity 52.0 GB raw, scan 41.5 GB raw) were destroyed
# over 109 days and 230 orphan raws (30.2 GB) filled the disk on
# 2026-09-04. See kb/failures/vps-disk-full-journal-rotation-collision-sep05.md.
#
# Invariants (pinned by tests/contracts/test_rotate_journals_sh.py):
#   - the archive stem carries the UTC HOUR: <journal>_YYYY-MM-DDTHH.jsonl.zst
#   - FAIL CLOSED before touching the live file: an existing archive name
#     → ERROR + skip (no mv); a failed mv → live untouched; a failed
#     compress → raw kept and retried next run. Any error → exit 1 AND
#     `errors=N` on the Done line (the watchdog reads it — see below).
#   - atomic `mv`: the bot appends with open/close per write (bot/logger.py
#     for opportunity/scan/rejection, bot/shadows/fifteenm_shadow.py for the
#     shadow journal — both open with mode "a" per line; no held handle), so
#     renaming the live file loses nothing — the next append creates a
#     fresh live file. (The previous copy-then-truncate DROPPED every line
#     written between `cp` finishing and the truncate — seconds on a
#     multi-GB journal — and transiently doubled the largest journal on a
#     disk being defended at 85%.) ROTATE_ARCHIVE_DIR must be on the same
#     filesystem as the live journals for the rename to be atomic (the
#     default is).
#   - leftover raws: any `<journal>_<stamp>.jsonl` still uncompressed in
#     the archive dir (an earlier failed compress, OR a legacy date-only
#     raw from the pre-2026-09-05 script) is retried FIRST on every run;
#     a raw whose compressed twin exists is left for manual triage and
#     counted as an error. The final
#     "Done." line carries `errors=N`; monitor_watchdog.py alerts on N>0
#     and on a stale rotation.log (R1-M3 — cron ignores exit codes under
#     the `>> rotation.log 2>&1` redirect).
#
# Env seams (defaults = production):
#   ROTATE_REPO_DIR              live journals dir
#   ROTATE_ARCHIVE_DIR           archive dir (synced to S3 by
#                                kalshi-journal-archives-sync.timer)
#   ROTATE_MIN_SIZE_BYTES        rotate only files >= this (10 MiB)
#   ROTATE_LOCAL_RETENTION_DAYS  `find -mtime +N`: prune local archives at
#                                least N+1 full days old (14 → ≥15 d; the live
#                                VPS value; S3 keeps the long-term copy)
#   ROTATE_JOURNALS              space-separated journal file names
#   ROTATE_STAMP                 archive stamp override (test seam)
set -u

REPO_DIR="${ROTATE_REPO_DIR:-/home/botuser/kalshi-bot-repo}"
ARCHIVE_DIR="${ROTATE_ARCHIVE_DIR:-$REPO_DIR/journal_archives}"
MIN_SIZE="${ROTATE_MIN_SIZE_BYTES:-$((10 * 1024 * 1024))}"
LOCAL_RETENTION_DAYS="${ROTATE_LOCAL_RETENTION_DAYS:-14}"
STAMP="${ROTATE_STAMP:-$(date -u +%Y-%m-%dT%H)}"
JOURNALS="${ROTATE_JOURNALS:-opportunity_journal.jsonl rejection_journal.jsonl scan_journal.jsonl fifteenm_shadow_journal.jsonl}"

# zstd default level: ~4x better than gzip on this repetitive JSONL and a
# ~1.5 MB memory window (measured 2026-05-10: 4.5G -> 89M). Falls back to
# gzip if zstd is missing.
if command -v zstd >/dev/null 2>&1; then
    COMPRESS_CMD=(zstd -q --rm)
    COMPRESS_EXT="zst"
else
    echo "WARN zstd not installed; falling back to gzip"
    COMPRESS_CMD=(gzip)
    COMPRESS_EXT="gz"
fi

file_size() {
    stat -c%s "$1" 2>/dev/null || stat -f%z "$1" 2>/dev/null
}

device_id() {
    # -L: follow symlinks so a symlinked archive dir on another fs is caught.
    stat -L -c%d "$1" 2>/dev/null || stat -L -f%d "$1" 2>/dev/null
}

# The rename below is only atomic (and only loss-free) on ONE filesystem;
# a cross-filesystem `mv` degrades to copy+unlink and reopens the drop
# window. Refuse loudly rather than silently regress. The refusal still
# ends with the `Done. … errors=N` footer the watchdog regex reads.
if [ ! -d "$REPO_DIR" ]; then
    echo "ERROR journals dir $REPO_DIR does not exist — refusing to rotate"
    echo "Done. Disk free: n/a errors=1"
    exit 1
fi
if ! mkdir -p "$ARCHIVE_DIR"; then
    echo "ERROR cannot create archive dir $ARCHIVE_DIR"
    echo "Done. Disk free: n/a errors=1"
    exit 1
fi
if [ "$(device_id "$REPO_DIR")" != "$(device_id "$ARCHIVE_DIR")" ]; then
    echo "ERROR $ARCHIVE_DIR is not on the same filesystem as $REPO_DIR — mv would not be atomic; refusing to rotate"
    echo "Done. Disk free: n/a errors=1"
    exit 1
fi

errors=0

# Retry-compress leftovers from an earlier failed compress (raw archives
# never sync to S3 — the S3 sync excludes *.jsonl).
# Matches BOTH the hour-stamped shape and the legacy date-only shape
# (`<journal>_YYYY-MM-DD.jsonl`) the pre-2026-09-05 script left behind on
# every collision — those must surface as errors, not sit unsynced forever.
for leftover in "$ARCHIVE_DIR"/*_????-??-??*.jsonl; do
    [ -e "$leftover" ] || continue
    if [ -e "$leftover.zst" ] || [ -e "$leftover.gz" ]; then
        echo "ERROR leftover raw $leftover has a compressed twin — leaving both for manual triage"
        errors=$((errors + 1))
        continue
    fi
    if "${COMPRESS_CMD[@]}" "$leftover"; then
        echo "RECOVERED leftover raw archive: $leftover -> $leftover.$COMPRESS_EXT"
    else
        echo "ERROR compress of leftover $leftover failed again — raw kept"
        errors=$((errors + 1))
    fi
done

for journal in $JOURNALS; do
    filepath="$REPO_DIR/$journal"
    if [ ! -f "$filepath" ]; then
        continue
    fi

    size=$(file_size "$filepath")
    size=${size:-0}
    if [ "$size" -lt "$MIN_SIZE" ]; then
        echo "SKIP $journal (${size} bytes < ${MIN_SIZE} threshold)"
        continue
    fi

    archive="$ARCHIVE_DIR/${journal%.jsonl}_${STAMP}.jsonl"

    # Fail CLOSED before touching the live file. A same-stamp rerun (or a
    # cron cadence tighter than the stamp granularity) must never clobber
    # or silently drop a chunk.
    for existing in "$archive" "$archive.zst" "$archive.gz"; do
        if [ -e "$existing" ]; then
            echo "ERROR $journal: $existing already exists — refusing to rotate; live file NOT truncated (same-hour rerun? cron cadence vs stamp granularity?)"
            errors=$((errors + 1))
            continue 2
        fi
    done

    # Atomic rename (same filesystem). A failed mv leaves the live file
    # exactly as it was — nothing to clean up.
    if ! mv "$filepath" "$archive"; then
        echo "ERROR $journal: mv to $archive failed (permissions / cross-filesystem?) — live file untouched"
        errors=$((errors + 1))
        continue
    fi

    if ! "${COMPRESS_CMD[@]}" "$archive"; then
        echo "ERROR $journal: compress of $archive failed — raw archive kept on disk (not synced to S3 until compressed)"
        errors=$((errors + 1))
        continue
    fi

    orig_mb=$((size / 1024 / 1024))
    out_size=$(file_size "${archive}.${COMPRESS_EXT}")
    out_mb=$((${out_size:-0} / 1024 / 1024))
    echo "ROTATED $journal: ${orig_mb}MB -> ${archive}.${COMPRESS_EXT} (${out_mb}MB compressed)"
done

# Local retention: S3 (kalshi-journal-archives-sync.timer, rclone copy —
# never deletes remote) holds the long-term copy.
find "$ARCHIVE_DIR" \( -name "*.gz" -o -name "*.zst" \) -mtime +"$LOCAL_RETENTION_DAYS" -delete -print 2>/dev/null | while read -r f; do
    echo "DELETED old archive: $f"
done

echo "Done. Disk free: $(df -hP "$ARCHIVE_DIR" | tail -1 | awk '{print $4}') errors=$errors"
if [ "$errors" -ne 0 ]; then
    exit 1
fi
exit 0
