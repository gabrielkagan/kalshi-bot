"""Collector run loop — D1.2 implementation target.

D1.1 (ticket 86b9ypn49, 2026-05-16) ships this as scaffolding only.
``run()`` raises ``NotImplementedError`` so ``python -m collector`` is
diagnosable as "scaffolding present, body pending D1.2" rather than
silent-no-op.

Future shape (per D0.3 §5):
- spawn one WS connection per tier per
  ``collector/subscription_manager.py``
- per-frame: ``collector/writer.py`` appends to the in-flight JSONL
  buffer file with the 6-field bronze envelope (D0.3 §2)
- on rotation trigger (5min OR 100MB, D0.3 §4):
  ``collector/uploader.py`` zstd-compresses + rclones + verifies +
  deletes-local per D0.3 §7
- ``collector/rest_snapshot.py`` periodically pulls /events catalog
  refresh (D1.4)
"""


def run() -> None:
    raise NotImplementedError(
        "collector/main_loop.run() is a D1.1 scaffolding stub. "
        "Real implementation lands at D1.2 (ticket 86b9ypn5q). "
        "See kb/decisions/data-corpus-architecture.md §5 for the "
        "process-shape lock."
    )
