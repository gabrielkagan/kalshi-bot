"""Fire-and-forget Telegram alerts via Bot API.

Bit 4.2 (Sprint 4): extracted verbatim from bot/_impl.py. Pure leaf —
stdlib + `requests` only, no `bot.constants` deps, no helpers, no
module-level instance, no import-time side effects. Constructed
exactly once in `MainLoop.__init__` at runtime; the module-level
singleton `_TELEGRAM` lives in `bot/_impl.py` (instance, not class).

Re-imported into bot/_impl.py as `from bot.notifier import TelegramNotifier`
so the runtime construction at `MainLoop.__init__` resolves. The
`Optional["TelegramNotifier"]` forward-ref on `_TELEGRAM` is a string
annotation that no caller currently evaluates (no `typing.get_type_hints`
consumer in-tree as of Bit 4.2 R2), so the import is justified solely by
the runtime construction.
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
