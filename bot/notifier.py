"""Fire-and-forget Telegram alerts via Bot API.

Bit 4.2 (Sprint 4): extracted verbatim from bot/_impl.py. Pure leaf —
stdlib + `requests` only, no `bot.constants` deps, no helpers.
Constructed exactly once in `MainLoop.__init__` at runtime.

Bit 8.1 path-A++ (2026-05-10): the module-level singleton `_TELEGRAM`
relocated from `bot/_impl.py` to here, alongside the class it
references. FIVE consumers reach it via `import bot.notifier as
_telegram_state` plus `_telegram_state._TELEGRAM` module-attribute
access (Bit 9.3-ii atomic update, 2026-05-10 — the orphan-DB Layer-3
watchdog block relocated from bot/_impl.py to NEW bot/orphan_db_watchdog.py;
that NEW module REPLACES bot/_impl.py as the 5th consumer — bot/_impl.py
post-Bit-9.3-ii has zero `_telegram_state._TELEGRAM` consumers; net stays at 5):
  - `bot/orphan_db_watchdog.py` (Bit 9.3-ii, 2026-05-10) — for the orphan-DB
    Layer-3 watchdog helpers (`_alert_orphan_db_holder` + the
    `detect_orphan_db_holders` lsof-not-found Telegram alert branch;
    transitively called from `MainLoop.startup()` in bot/main_loop.py
    via `from bot.orphan_db_watchdog import detect_orphan_db_holders`
    + `detect_orphan_db_holders(DB_PATH)`)
  - `bot/main_loop.py` (search anchor: `import bot.notifier as _telegram_state`) — for MainLoop reads (Bit 9.3, 2026-05-10) + the singleton WRITE at `MainLoop.__init__` (`_telegram_state._TELEGRAM = self.telegram`)
  - `bot/scanner/__init__.py` (search anchor: ``import bot.notifier as _telegram_state``) — for OpportunityScanner reads
  - `bot/executor.py` (Bit 9.1, 2026-05-10) — for OrderExecutor reads
    (19 read sites in the class body)
  - `bot/settlement.py` (Bit 9.2, 2026-05-10) — for SettlementTracker reads
    (4 read sites in the class body)
The module-attribute access pattern (parallel to the Bit 6.3 path-B
`_cal_state._CALIBRATION_ENGINE` pattern) preserves mutation freshness
across consumers because every reader goes through the module reference,
NOT a captured-by-value binding. The plain `from bot.notifier import
_TELEGRAM` form would NOT propagate runtime mutation — `MainLoop.__init__`
(now in `bot/main_loop.py`) writes via `_telegram_state._TELEGRAM = self.telegram`
(drops the previous module-level `global` rebind declaration) and readers
in all five consumer modules see the rebinding immediately via the
module-attribute lookup. The relocation closes a laundered-namespace
coupling that would have forced ~86 `@patch("bot._TELEGRAM", ...)`
patch-target retargets to either `bot.scanner._TELEGRAM` (path-A
late-binding) or `bot.notifier._TELEGRAM` (path-A++); the latter is
strictly cleaner because the singleton lives next to its class.
"""
import logging
import threading
import time
from typing import Dict, Optional

import requests


class TelegramNotifier:
    """Fire-and-forget Telegram alerts via Bot API."""

    def __init__(self, bot_token: str, chat_id: str):
        self._url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
        self._chat_id = chat_id
        self.enabled = bool(bot_token and chat_id)
        self._dedup: Dict[str, float] = {}

    def send(self, message: str, silent: bool = False, dedup_key: Optional[str] = None):
        if not self.enabled:
            return
        if dedup_key:
            now = time.time()
            if dedup_key in self._dedup and now - self._dedup[dedup_key] < 60:
                return
            self._dedup[dedup_key] = now
        text = message[:4096]
        threading.Thread(target=self._post, args=(text, silent), daemon=True).start()

    def send_sync(self, message: str, silent: bool = False,
                  dedup_key: Optional[str] = None) -> bool:
        """Synchronous variant of `send` — performs the HTTP POST inline
        and returns True iff Telegram returned 2xx.

        Added 2026-05-19 for ``scripts/ops/phantom_reconcile_monitor.py``
        to close R2-N1: the fire-and-forget ``send()`` spawns a daemon
        thread and returns immediately; the cron wrapper then records
        "sent" in its on-disk dedup sidecar. If the daemon dies before
        the HTTP POST completes (SIGTERM during shutdown, network
        failure post-record), the sidecar suppresses the next retry
        and the alert is silently lost. ``send_sync`` lets the caller
        gate sidecar record on actual 2xx delivery.

        Hot-path callers (bot/scanner, bot/executor, bot/settlement,
        bot/main_loop) MUST keep ``send`` — they cannot block on a 5s
        HTTP timeout inside the scan tick. ``send_sync`` is for
        cron-tier scripts where the process exits anyway after one
        round.
        """
        if not self.enabled:
            return False
        if dedup_key:
            now = time.time()
            if dedup_key in self._dedup and now - self._dedup[dedup_key] < 60:
                return False
            self._dedup[dedup_key] = now
        text = message[:4096]
        try:
            resp = requests.post(self._url, json={
                "chat_id": self._chat_id,
                "text": text,
                "disable_notification": silent,
            }, timeout=5)
            return 200 <= getattr(resp, "status_code", 0) < 300
        except Exception as e:
            logging.warning(f"Telegram send_sync failed: {e}")
            return False

    def _post(self, text: str, silent: bool):
        # No parse_mode: Telegram's Markdown parser 400s on unbalanced
        # `_` / `*` / `` ` `` in alert payloads. B4's `KALSHI_DELTA=…`
        # tag has a single underscore and was dropping every WIN
        # settlement alert (2026-05-18). No call site formats markdown.
        try:
            requests.post(self._url, json={
                "chat_id": self._chat_id,
                "text": text,
                "disable_notification": silent,
            }, timeout=5)
        except Exception as e:
            logging.warning(f"Telegram send failed: {e}")


# Module-level singleton — populated by MainLoop.__init__ at runtime
# (MainLoop now lives in bot/main_loop.py post-Bit-9.3, 2026-05-10).
# Relocated from bot/_impl.py per Bit 8.1 path-A++ (2026-05-10).
# Reach via `bot.notifier._TELEGRAM` (or alias-import in bot/orphan_db_watchdog.py +
# bot/main_loop.py + bot/scanner/__init__.py + bot/executor.py +
# bot/settlement.py — 5 consumers post-Bit-9.3-ii) to preserve mutation freshness.
_TELEGRAM: Optional["TelegramNotifier"] = None
