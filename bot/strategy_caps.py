"""Live-small COMBINED risk rails shared by longshot + twaplock (Bit T-1).

Single source of truth for the plan-doc requirement
(kb/decisions/longshot-twap-live-small-plan.md): "$20/day cap, BOTH
strategies combined, realized+marked -> same-day auto-disable" and
"3 consecutive losing days -> auto-disable both". The Bit L-1 ship was
per-strategy (R2-MN4 note); Bit T-1 retargeted
``LongshotEngine._refresh_disabled`` here and ``TwaplockEngine`` consumes
the same functions, so the two engines' disable latches can never see
different numbers (``feedback_modular_easy_north_star`` — one chokepoint,
not two near-copies of the same SQL).

Surface:

* ``combined_realized_day_pnl_cents(conn, day_iso)`` — fee-inclusive
  realized PnL for one UTC day summed across
  ``strategy IN LIVE_SMALL_STRATEGIES``. Raises on DB failure — each
  engine keeps its own fail-OPEN posture (preserve the last latch state;
  see the R1-MN3 asymmetry note in bot/longshot.py).
* ``combined_marked_loss_cents()`` — sum over the per-strategy mark
  providers registered by each engine at construction
  (``register_mark_provider``). A provider exception contributes 0
  (fail-open, same direction as longshot's ``_marked_open_loss_cents``
  query-failure posture: the realized term still applies).
* ``combined_daily_cap_hit(conn, today_iso)`` — ``(hit, realized_cents,
  marked_cents)`` against ``LIVE_SMALL_DAILY_LOSS_CAP_DOLLARS`` (read
  live via module-attribute access — runtime kill-switch pattern).
* ``combined_consecutive_losing_days(conn, today, n_disable, reset_date)``
  — completed COMBINED losing days strictly before ``today``, capped at
  ``n_disable``; ``reset_date`` (LIVE_SMALL_STREAK_RESET_UTC_DATE) ends
  the walk (operator re-enable).

Clean leaf: stdlib + ``bot.constants`` only.
"""
from __future__ import annotations

import datetime
import logging
from typing import Callable, Dict, Optional, Tuple

import bot.constants as C

# The strategies whose realized+marked PnL share the combined live-small
# rails. settled_trades.strategy carries these literals (longshot:
# bot/longshot.py LONGSHOT_STRATEGY; twaplock: bot/twaplock.py
# TWAPLOCK_STRATEGY — both pass through models.strategy_to_group
# unchanged).
LIVE_SMALL_STRATEGIES: Tuple[str, ...] = ("longshot", "twaplock")

# strategy -> zero-arg callable returning that engine's marked full-loss
# cents on OPEN positions (longshot: sold side currently ITM; twaplock:
# bought side currently OTM). Engines register at construction; a
# re-registration under the same key replaces the old provider (fresh
# engine instances in tests / restarts overwrite stale closures).
_mark_providers: Dict[str, Callable[[], int]] = {}


def register_mark_provider(strategy: str, provider: Callable[[], int]) -> None:
    """Register (or replace) the marked-open-loss provider for a strategy."""
    _mark_providers[strategy] = provider


def combined_marked_loss_cents() -> int:
    """Sum of all registered mark providers; a failing provider counts 0.

    Fail-open by design: marks are a same-day tightening on top of the
    realized term, and positions settle within minutes anyway — a missing
    mark must not flip a healthy engine into a disable latch.
    """
    total = 0
    for strategy, provider in list(_mark_providers.items()):
        try:
            total += int(provider() or 0)
        except Exception:
            logging.warning("live-small mark provider failed for %s",
                            strategy, exc_info=True)
    return total


def combined_realized_day_pnl_cents(conn, day_iso: str) -> int:
    """Fee-inclusive realized PnL (cents) for ``day_iso`` summed across
    LIVE_SMALL_STRATEGIES. Raises on DB failure (caller decides posture)."""
    placeholders = ",".join("?" for _ in LIVE_SMALL_STRATEGIES)
    row = conn.execute(
        "SELECT COALESCE(SUM(pnl_cents - COALESCE(fee_cents, 0)), 0) "
        f"FROM settled_trades WHERE strategy IN ({placeholders}) "
        "AND substr(settled_at, 1, 10) = ?",
        (*LIVE_SMALL_STRATEGIES, day_iso)).fetchone()
    return int(row[0] or 0)


def _day_pnl_or_none(conn, day_iso: str) -> Optional[int]:
    """SUM (NULL when no rows) for the streak walk — None ends a streak."""
    placeholders = ",".join("?" for _ in LIVE_SMALL_STRATEGIES)
    row = conn.execute(
        "SELECT SUM(pnl_cents - COALESCE(fee_cents, 0)) "
        f"FROM settled_trades WHERE strategy IN ({placeholders}) "
        "AND substr(settled_at, 1, 10) = ?",
        (*LIVE_SMALL_STRATEGIES, day_iso)).fetchone()
    return row[0] if row else None


def combined_daily_cap_hit(conn, today_iso: str) -> Tuple[bool, int, int]:
    """(hit, realized_cents, marked_cents) vs the combined daily cap.

    ``hit`` is True when ``realized - marked <= -cap``. Reads
    ``LIVE_SMALL_DAILY_LOSS_CAP_DOLLARS`` live (module-attribute access)
    so a constants flip takes effect on the next decision. Raises on DB
    failure (each engine preserves its last latch state — fail-open)."""
    realized = combined_realized_day_pnl_cents(conn, today_iso)
    marked = combined_marked_loss_cents()
    cap_cents = int(round(C.LIVE_SMALL_DAILY_LOSS_CAP_DOLLARS * 100))
    return (realized - marked <= -cap_cents, realized, marked)


def combined_consecutive_losing_days(conn, today: datetime.date, *,
                                     n_disable: int,
                                     reset_date: str = "") -> int:
    """Completed COMBINED losing days strictly before ``today``.

    Walks back day-by-day (capped at ``n_disable``); a day with no
    live-small activity (SUM NULL) or non-negative PnL ends the streak;
    days on/before ``reset_date`` are ignored (operator re-enable via
    LIVE_SMALL_STREAK_RESET_UTC_DATE). Raises on DB failure."""
    reset = (reset_date or "").strip()
    streak = 0
    for back in range(1, n_disable + 1):
        day_iso = (today - datetime.timedelta(days=back)).isoformat()
        if reset and day_iso <= reset:
            break
        day_pnl = _day_pnl_or_none(conn, day_iso)
        if day_pnl is None or day_pnl >= 0:
            break
        streak += 1
    return streak
