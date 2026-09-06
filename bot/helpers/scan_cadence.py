"""15M vs slow-product scan cadence.

Weather/hourly/SPX share OpportunityScanner.scan() with 15M. They skip
WS and REST-fallback; evaluating them every 1s produced 5–8s
SCAN_BODY_SLOW (2026-09-06). 15M stays on the fast tick; these types
evaluate every SLOW_PRODUCT_SCAN_INTERVAL_S.

See kb/failures/scan-body-5-8s-collecting-mode-sep06.md.
"""
from __future__ import annotations

from typing import Optional

from bot.constants import SLOW_PRODUCT_TYPES


def include_window_this_tick(
    product_type: Optional[str], slow_due: bool
) -> bool:
    """True if this window should run its scan body on this tick."""
    if product_type in SLOW_PRODUCT_TYPES:
        return bool(slow_due)
    return True


def slow_scan_due(now: float, last_ts: float, interval: float) -> bool:
    return (now - last_ts) >= interval
