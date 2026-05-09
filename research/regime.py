"""Regime cutoff filter — drops rows with evaluation_time < cutoff.

Per CLAUDE.md "performance analysis filters to current config regime."
Pre-regime data is misleading: live behavior changed materially when
the bleed-cell blocks went LIVE on 2026-04-30T16:16Z.

Default cutoff matches scripts/alpha_audit.REGIME_CUTOFFS[0]; pass
None / "none" to opt out (sweep callers should NEVER do this without
a stderr warning — see research/eval.py).
"""
from __future__ import annotations

from datetime import datetime
from typing import Iterable, Iterator, Optional


REGISTERED_DEFAULT_CUTOFF = datetime.fromisoformat("2026-04-30T16:16:00")


def parse_cutoff(value: Optional[str]) -> Optional[datetime]:
    """Parse an ISO-8601 cutoff string. Returns None if value is
    None or the literal string 'none' (case-insensitive)."""
    if value is None:
        return None
    if value.strip().lower() == "none":
        return None
    return datetime.fromisoformat(value)


def apply_regime_cutoff(
    records: Iterable,
    cutoff: Optional[datetime],
) -> Iterator:
    """Drop records with evaluation_time < cutoff. None cutoff is
    a passthrough (caller must surface the no-cutoff warning)."""
    if cutoff is None:
        yield from records
        return
    for r in records:
        if r.evaluation_time >= cutoff:
            yield r
