"""Bit 9.1 path-A++ relocation: pure leaf helper for raw-API JSONL journal.

Pre-relocation: `_append_raw_api_journal` lived at `bot/_impl.py:282` with 3
callers (1 in OrderExecutor, 2 in SettlementTracker). Both consumer classes
extract in Sprint 9 (Bits 9.1 + 9.2); relocating once eliminates the
late-binding need that path-A would have introduced and avoids 2 new
`.importlinter` carve-outs (`executor-no-impl-toplevel` + `settlement-no-impl-toplevel`).

Per L79 default for bounded callee surface (1 def, 3 callers in 2 consumer
classes that BOTH extract this sprint): path-A++ wins decisively.

Per the bot/helpers/ leaf-package convention (Bit 3.2): this submodule is
NOT star-imported by `bot/helpers/__init__.py` — consumers explicit-import
via `from bot.helpers.raw_api_journal import append_raw_api_journal`. Mirrors
the `validators` / `breakers` submodule-private convention. This keeps the
public name from leaking into `bot._impl.__dict__` via the
`from bot.helpers import *` star-cascade — preserves
`tests/contracts/public_api.json` byte-stability per L81.

bot/_impl.py imports the public name with the L81 underscore alias:
    `from bot.helpers.raw_api_journal import append_raw_api_journal as _append_raw_api_journal`
which keeps the SettlementTracker callers at lines 6413/6655 working
byte-identical until Bit 9.2 retires them.
"""
import datetime
import json
import logging
from datetime import timezone

from bot.constants import RAW_API_JOURNAL_PATH


def append_raw_api_journal(entry: dict) -> None:
    """Append one JSON line to the raw-API journal. Never raises."""
    try:
        entry["ts"] = datetime.datetime.now(timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%S.%fZ"
        )
        with open(RAW_API_JOURNAL_PATH, "a") as f:
            f.write(json.dumps(entry, default=str) + "\n")
    except Exception as e:
        logging.warning("raw_api_journal write failed: %s", e)
