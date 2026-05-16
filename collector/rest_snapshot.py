"""Kalshi REST snapshot — D1.4 implementation target.

Periodic ``/events`` and ``/markets`` catalog refresh (single source of
truth for which markets exist + their metadata, NOT covered by the WS
``market_lifecycle_v2`` channel which is updates-only).

D0.3 §1 medallion layout — REST snapshots land under
``s3://kalshi-bot-archive/bronze/kalshi_rest/{events_snapshot,markets_snapshot}/...``
with the same JSONL.zst bronze envelope as WS captures (D0.3 §2),
``_conn=null`` and ``_channel`` carrying the REST endpoint stub.
"""
