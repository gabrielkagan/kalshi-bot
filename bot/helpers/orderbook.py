"""Orderbook helpers — Sprint 10 sibling-reorg Bit 86b9vpp2z (2026-05-11).

Two pure-utility functions extracted from `OpportunityScanner` staticmethods
(`_convert_orderbook_fp` + `_best_yes_ask_cents`) so OrderExecutor can stop
going through the `_get_opportunity_scanner()` cycle-break helper.

Pre-Bit: 10 OrderExecutor call sites accessed these as `OpportunityScanner.X(...)`
via the late-binding `_get_opportunity_scanner()` helper (which existed solely
to break the bot.executor ↔ bot.scanner cycle Bit 9.1 created when retiring
the scanner-side `_get_order_executor()` helper). Path-B preservation: the
two `OpportunityScanner` staticmethods remain as 1-line delegates so the
~20 test sites that use `OpportunityScanner._X(...)` staticmethod-via-class
form keep working unchanged.

Clean leaf: no `bot.*` deps; stdlib `typing` only. Listed in
`.importlinter` `helpers-leaf` `forbidden_modules` enumeration via the
walk-based regression test.
"""
from __future__ import annotations

from typing import Dict, Optional


def best_yes_ask_cents(ob_data: Dict) -> Optional[int]:
    """Compute best YES ask = 100 - highest NO bid.

    Handles both legacy cents format and floating-point dollar format.
    """
    no_bids = ob_data.get("no", [])
    if not no_bids:
        return None

    best_no_bid = None
    for entry in no_bids:
        if isinstance(entry, (list, tuple)) and len(entry) >= 2:
            price = entry[0]
        elif isinstance(entry, dict):
            price = entry.get("price", 0)
        else:
            continue

        if isinstance(price, float) and price < 1.0:
            price_cents = round(price * 100)
        else:
            price_cents = int(price)

        if best_no_bid is None or price_cents > best_no_bid:
            best_no_bid = price_cents

    if best_no_bid is None or best_no_bid <= 0:
        return None

    return 100 - best_no_bid


def convert_orderbook_fp(ob_fp: Dict) -> Dict:
    """Convert orderbook_fp format to internal cents format.

    Input:  {"no_dollars": [["0.1100", "205.00"], ...], "yes_dollars": [...]}
    Output: {"no": [[11, 205], ...], "yes": [[77, 200], ...]}
    """
    result = {}
    for side in ("yes", "no"):
        entries = ob_fp.get(f"{side}_dollars") or []
        converted = []
        for entry in entries:
            if isinstance(entry, (list, tuple)) and len(entry) >= 2:
                price_cents = round(float(entry[0]) * 100)
                count = int(round(float(entry[1])))
                converted.append([price_cents, count])
        result[side] = converted
    return result
