"""bot.ai — AI-driven analysis + audit + reporting helpers.

Telegram-driven entrypoints (NOT VPS-crontab-runtime — they ARE invoked
via VPS cron, but as standalone scripts, not via systemd/start.sh):

  - `analyst.py`     — Claude API loss analysis + news sentiment.
  - `auditor.py`     — Hourly deterministic health checks → Telegram alerts.
  - `researcher.py`  — 3×/day performance reports → Telegram.

Each module reads `state.db` read-only (auditor.py also writes
`auditor_state.db`; researcher.py also writes `researcher_state.db`).
Path anchors resolve to the repository root via the standard
`Path(__file__).resolve().parent.parent.parent` chain (3 levels:
ai/ → bot/ → repo/) — see Sprint 10 Bit 10.3 (2026-05-12) for the
relocation rationale.

These modules are NOT part of the bot runtime hot path (no import
into `bot.main_loop`, `bot.scanner`, etc.). Adding back-edges from
hot-path modules would create a cycle hazard and should be avoided.
"""
