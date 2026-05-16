"""Kalshi WS client — D1.2 implementation target.

D0.3 §5 contract: this is NOT ``bot/feeds/kalshi.py``. The bot's WS
feed is tangled with caching, NBBO dedup, fill events, and bot-state
machinery that bronze must NOT have. This module reimplements the
minimum required for capture — subscribe / update_subscription /
frame ingestion / per-conn auth.

Auth pattern: RSA-PSS-SHA256 (~20 LOC, mirrors ``bot/kalshi_client.py:60``
shape) — duplicated here per D0.3 §5 paragraph 6 ("v1 declines the first
cross-package coupling"; re-evaluate at D1.5 if drift becomes a problem).
"""
