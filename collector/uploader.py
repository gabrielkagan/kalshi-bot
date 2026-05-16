"""rclone upload + verify + delete-local — D1.2 implementation target.

D0.3 §7 contract (load-bearing — mirrors the existing
``scripts/ops/journal_archives_s3_sync.py`` precedent from commit
``635afbf``):

  1. fsync the in-flight JSONL
  2. zstd-compress level 6 to tmp/
  3. atomic ``os.replace`` to outbox/
  4. ``rclone copy --checksum --immutable`` to S3
  5. verify ``rclone size`` matches local stat
  6. ONLY THEN delete the local outbox + in-flight files

KEEP-local posture on rclone non-zero / size mismatch. ``--immutable``
guards against silent overwrites (returns exit-6 on divergence).
``copy`` NOT ``sync`` — sync would mirror-delete S3 objects when local
files age out (catastrophic).

NOTE on disk-pressure failure mode (D0.3 §6 last bullet): sustained
upload-stall accumulation can fill the bot VPS root volume in <1 hour.
D1.6 must alert on the FIRST rclone non-zero, not wait on a passive
``df`` watermark.
"""
