"""Regression: settlement alerts dropped when message body contained an
unbalanced Markdown control character.

B4 (commit bdfc90e2, 2026-05-18) added a `KALSHI_DELTA=...` divergence
tag to the settlement Telegram alert. The notifier was sending with
`parse_mode: "Markdown"`, and the single `_` inside `KALSHI_DELTA`
made Telegram return 400 Bad Request — every WIN settlement that
tripped divergence (which is most of them, because the Kalshi
balance API lags the settlement event) had its alert silently
dropped.

Fix: drop `parse_mode` from `bot/notifier.TelegramNotifier._post`.
No call site formats markdown — alerts are plain text.
"""
from unittest.mock import patch

from bot.notifier import TelegramNotifier


def test_post_does_not_set_parse_mode():
    """B4 regression: parse_mode must NOT be set, so unbalanced `_`/`*`/`` ` ``
    in alert payloads (like `KALSHI_DELTA=...`) don't 400 the Telegram API.
    """
    n = TelegramNotifier("tok", "chat")
    with patch("bot.notifier.requests.post") as mock_post:
        n._post("WIN HYPE 56ct @97c +$1.68 ⚠️KALSHI_DELTA=+$0.00 (expected +$56.00)", silent=False)
    payload = mock_post.call_args.kwargs["json"]
    assert "parse_mode" not in payload, (
        "parse_mode must not be set — unbalanced `_` in alert bodies "
        "(e.g. B4 KALSHI_DELTA tag) trip Telegram Markdown parser → 400"
    )
