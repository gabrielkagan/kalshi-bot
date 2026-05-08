"""Structured JSONL logging with fill deduplication.

Bit 4.1 (Sprint 4): extracted verbatim from bot/_impl.py. Pure leaf —
stdlib + bot.constants only, no helpers, no module-level instance,
no import-time side effects. Constructed exactly once in
MainLoop.__init__ at runtime.

Re-imported into bot/_impl.py as `from bot.logger import Logger` so the
type annotations on OpportunityScanner / OrderExecutor / SettlementTracker
(`logger: Logger`) resolve at class-body time.
"""
import datetime
import json
import logging
from datetime import timezone
from typing import Dict, Set

from bot.constants import (
    EXECUTION_JOURNAL,
    OPPORTUNITY_JOURNAL,
    ORDER_JOURNAL,
    PERFORMANCE_JOURNAL,
    REJECTION_JOURNAL,
    SCAN_JOURNAL,
    SETTLEMENT_JOURNAL,
    TRADE_JOURNAL,
)


class Logger:
    """Structured JSONL logging with fill deduplication."""

    def __init__(self):
        self._logged_fill_ids: Set[str] = set()

    def _write_entry(self, filepath: str, entry: Dict):
        entry["ts"] = datetime.datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        try:
            with open(filepath, "a") as f:
                f.write(json.dumps(entry, default=str) + "\n")
        except IOError as e:
            logging.error(f"Failed to write to {filepath}: {e}")

    def log_scan(self, data: Dict):
        self._write_entry(SCAN_JOURNAL, {"type": "scan", **data})

    def log_trade(self, data: Dict):
        self._write_entry(TRADE_JOURNAL, {"type": "trade", **data})

    def log_settlement(self, data: Dict):
        self._write_entry(SETTLEMENT_JOURNAL, {"type": "settlement", **data})

    def log_order(self, data: Dict):
        self._write_entry(ORDER_JOURNAL, {"type": "order", **data})

    def log_rejection(self, data: Dict):
        self._write_entry(REJECTION_JOURNAL, {"type": "rejection", **data})

    def log_opportunity(self, data: Dict):
        self._write_entry(OPPORTUNITY_JOURNAL, {"type": "opportunity", **data})

    def log_execution(self, data: Dict):
        self._write_entry(EXECUTION_JOURNAL, {"type": "execution", **data})

    def log_performance(self, data: Dict):
        self._write_entry(PERFORMANCE_JOURNAL, {"type": "performance", **data})

    def log_fill(self, fill: Dict) -> bool:
        """Log a fill, deduplicating by fill_id. Returns True if new."""
        fill_id = fill.get("fill_id", "")
        if fill_id in self._logged_fill_ids:
            return False
        self._logged_fill_ids.add(fill_id)
        self._write_entry(TRADE_JOURNAL, {"type": "fill", **fill})
        return True

    def load_logged_fill_ids(self):
        """Rebuild _logged_fill_ids from existing trade journal on startup."""
        try:
            with open(TRADE_JOURNAL, "r") as f:
                for line in f:
                    try:
                        entry = json.loads(line)
                        if entry.get("type") == "fill" and "fill_id" in entry:
                            self._logged_fill_ids.add(entry["fill_id"])
                    except json.JSONDecodeError:
                        continue
        except FileNotFoundError:
            pass
        logging.info(f"Loaded {len(self._logged_fill_ids)} previously logged fill IDs")
