"""Fire-and-forget Telegram alerts via Bot API.

Bit 4.2 (Sprint 4): extracted verbatim from bot/_impl.py. Pure leaf —
stdlib + `requests` only, no `bot.constants` deps, no helpers.
Constructed exactly once in `MainLoop.__init__` at runtime.

Bit 8.1 path-A++ (2026-05-10): the module-level singleton `_TELEGRAM`
relocated from `bot/_impl.py` to here, alongside the class it
references. Both `bot/_impl.py` and `bot/scanner/__init__.py` reach it
via `import bot.notifier as _telegram_state` plus
`_telegram_state._TELEGRAM` module-attribute access — the
module-attribute access pattern (parallel to the Bit 6.3 path-B
`_cal_state._CALIBRATION_ENGINE` pattern) preserves mutation freshness
across consumers because every reader goes through the module reference,
NOT a captured-by-value binding. The plain `from bot.notifier import
_TELEGRAM` form would NOT propagate runtime mutation — `MainLoop.__init__`
writes via `_telegram_state._TELEGRAM = self.telegram` (drops the
previous `global _TELEGRAM` declaration) and readers in BOTH bot/_impl.py
and bot/scanner/__init__.py see the rebinding immediately via the
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

    def _post(self, text: str, silent: bool):
        try:
            requests.post(self._url, json={
                "chat_id": self._chat_id,
                "text": text,
                "parse_mode": "Markdown",
                "disable_notification": silent,
            }, timeout=5)
        except Exception as e:
            logging.warning(f"Telegram send failed: {e}")


# Module-level singleton — populated by MainLoop.__init__ at runtime.
# Relocated from bot/_impl.py per Bit 8.1 path-A++ (2026-05-10).
# Reach via `bot.notifier._TELEGRAM` (or alias-import in bot/_impl.py +
# bot/scanner/__init__.py) to preserve mutation freshness.
_TELEGRAM: Optional["TelegramNotifier"] = None
