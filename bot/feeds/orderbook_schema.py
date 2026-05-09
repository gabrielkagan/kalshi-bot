"""OrderbookSchemaError — exception raised when a Kalshi WS orderbook
message violates the expected wire contract.

Extracted from bot/_impl.py in Sprint 4 Bit 4.5a (2026-05-08). The
class is a 3-line exception type with one purpose: fail LOUD at the
ingest boundary when Kalshi silently renames wire fields. Precedents:
- Mar 2026 REST orderbook_fp migration -> 37-day sports outage
- Apr 2026 WS orderbook_snapshot/delta migration -> 5+ weeks of silent
  95%-NULL bid-side feature data

Currently consumed exclusively by the ``KalshiFeed`` class (still in
``bot/_impl.py``); Bit 4.5b will move ``KalshiFeed`` into this same
subpackage and reroute the import locally. Contract tests in
``tests/test_kalshi_ws_contracts.py`` pin the expected schema.
"""

from __future__ import annotations


class OrderbookSchemaError(Exception):
    """Raised when a Kalshi WS orderbook message violates the expected wire contract.

    Purpose: fail LOUD at the ingest boundary when Kalshi silently renames wire
    fields (precedent: Mar 2026 REST orderbook_fp migration — 37-day sports
    outage; Apr 2026 WS orderbook_snapshot/delta migration — 5+ weeks of silent
    95%-NULL bid-side feature data). Contract tests in
    tests/test_kalshi_ws_contracts.py pin the expected schema.
    """
    pass
