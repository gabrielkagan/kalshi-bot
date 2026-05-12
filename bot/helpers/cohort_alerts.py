"""Cohort alert triggers — Phase 1 P1.3 (Money Printer Roadmap).

Pure functions that decide whether a cohort's nightly aggregation row
should fire a BLEED or CALIBRATION Telegram alert, plus a cooldown
predicate to throttle re-alerts.

Consumed by `bot.helpers.cohort_attribution.compute_next_alert_state`
via the parameter-injection contract (`run_aggregation(conn, *,
alerts_module=None)`): P1.1 ships with `alerts_module=None` graceful
fallback; this module activates the firing path once `import
bot.helpers.cohort_alerts` succeeds in the cron-driven script.

Thresholds (design doc § Alert design):

- BLEED (single-day):
  n_30d ≥ 50, wilson95_hi_30d < 0.92, cf_pnl_30d_dollars < -50,
  cell_block_stage == 'candidate'.

- CALIBRATION (3-day persistence; empirically committed via 73d
  backfill-replay 2026-05-12 — see kb design doc § False-positive
  considerations):
  n_30d ≥ 30, abs(cal_gap_30d) > 0.05, persistence_days ≥ 3.

- Cooldown: 23h per cohort. 23h not 24h — design § Telegram payload
  format rationale (cron jitter slack).

Design doc: kb/decisions/cohort-measurement-design-may12.md (LOCAL).
Ticket: ClickUp 86b9x3kkb (P1.3).
"""
from __future__ import annotations

import datetime as _dt
import logging
from typing import Any, Dict, Optional

# ── Trigger thresholds (single source of truth) ──────────────────────────────

BLEED_MIN_N_30D = 50
BLEED_WILSON95_HI_CEIL = 0.92  # strict <
BLEED_CF_PNL_FLOOR_DOLLARS = -50.0  # strict <
BLEED_REQUIRED_STAGE = "candidate"

CAL_MIN_N_30D = 30
CAL_ABS_GAP_FLOOR = 0.05  # strict >
CAL_MIN_PERSISTENCE_DAYS = 3  # empirically committed via 73d backfill-replay

COOLDOWN_HOURS_DEFAULT = 23  # strict < to block, ≥ to expire


# ── Triggers ─────────────────────────────────────────────────────────────────


def should_fire_bleed_alert(
    *,
    n: int,
    wilson95_hi: float,
    cf_pnl_30d_dollars: float,
    cell_block_stage: str,
) -> bool:
    """Single-day BLEED check.

    Fires only on 'candidate' stage — already-blocked cohorts route through
    a different filter_stage value and shouldn't trigger noise; the
    cell-block IS the resolution.
    """
    if cell_block_stage != BLEED_REQUIRED_STAGE:
        return False
    if n < BLEED_MIN_N_30D:
        return False
    if wilson95_hi >= BLEED_WILSON95_HI_CEIL:
        return False
    if cf_pnl_30d_dollars >= BLEED_CF_PNL_FLOOR_DOLLARS:
        return False
    return True


def should_fire_calibration_alert(
    *,
    n: int,
    abs_cal_gap: float,
    persistence_days: int,
) -> bool:
    """CALIBRATION trigger with 3-day persistence.

    Caller (the aggregator's `compute_next_alert_state`) computes
    `abs(cal_gap_30d)` and `persistence_days` (count of consecutive
    immediate-prior cohort_dates with `abs(cal_gap_30d) > 0.05`).
    """
    if n < CAL_MIN_N_30D:
        return False
    if abs_cal_gap <= CAL_ABS_GAP_FLOOR:
        return False
    if persistence_days < CAL_MIN_PERSISTENCE_DAYS:
        return False
    return True


# ── Cooldown ─────────────────────────────────────────────────────────────────


def cooldown_active(
    *,
    last_alert_time: Optional[_dt.datetime],
    now: _dt.datetime,
    hours: int = COOLDOWN_HOURS_DEFAULT,
) -> bool:
    """Return True iff a prior alert fired within the cooldown window.

    `last_alert_time is None` → no prior alert → not in cooldown.
    Boundary: a gap of EXACTLY `hours` hours expires the cooldown
    (strict `<` to block, `≥` to expire) — matches the design pin
    `test_cooldown_active_exact_23h_boundary`.
    """
    if last_alert_time is None:
        return False
    elapsed = now - last_alert_time
    return elapsed < _dt.timedelta(hours=hours)


# ── Telegram payload formatters ──────────────────────────────────────────────


_DASHBOARD_URL = "https://gabrielkagan.github.io/kalshi-bot/"


def _format_price_band(price_band_5c: int) -> str:
    """5¢ band → human label. price_band=17 → '85-89'."""
    lo = price_band_5c * 5
    return f"{lo}-{lo + 4}"


def _format_stc_band(stc_band_60s: int) -> str:
    """60s band → human label.

    band=0 → '0-60', band=8 → '480-540', band=10 → '600-660' (last finite),
    band=11 → '660+' (tail, captures everything ≥ 660s — design § Storage
    `stc_band_60s` semantics).
    """
    if stc_band_60s >= 11:
        return "660+"
    lo = stc_band_60s * 60
    return f"{lo}-{lo + 60}"


def format_bleed_payload(cohort_row: Dict[str, Any]) -> str:
    """Telegram payload for a BLEED firing.

    Expects a dict-shaped cohort_attribution_daily row (or compatible
    keyword projection). Format mirrors the design doc § Telegram
    payload format sample.
    """
    asset = cohort_row["asset"]
    strategy = cohort_row["strategy"]
    pband = _format_price_band(int(cohort_row["price_band_5c"]))
    sband = _format_stc_band(int(cohort_row["stc_band_60s"]))
    stage = cohort_row.get("cell_block_stage", "candidate")
    n = int(cohort_row.get("n_30d") or 0)
    wr = cohort_row.get("wr_30d")
    wilson_hi = cohort_row.get("wilson95_hi_30d")
    cf_pnl = cohort_row.get("cf_pnl_30d_dollars")
    cohort_date = cohort_row.get("cohort_date", "")
    wr_str = f"{wr:.3f}" if wr is not None else "n/a"
    wilson_str = f"{wilson_hi:.3f}" if wilson_hi is not None else "n/a"
    cf_str = f"${cf_pnl:.2f}" if cf_pnl is not None else "n/a"
    return (
        f"\U0001f7e5 BLEED: {asset} {strategy} {pband} {sband}s ({stage})\n"
        f"   n={n}  wr={wr_str}  wilson95_hi={wilson_str}  cf_30d={cf_str}\n"
        f"   Cohort date: {cohort_date}\n"
        f"   Dashboard: {_DASHBOARD_URL}"
    )


def format_calibration_payload(cohort_row: Dict[str, Any]) -> str:
    """Telegram payload for a CALIBRATION firing."""
    asset = cohort_row["asset"]
    strategy = cohort_row["strategy"]
    pband = _format_price_band(int(cohort_row["price_band_5c"]))
    sband = _format_stc_band(int(cohort_row["stc_band_60s"]))
    stage = cohort_row.get("cell_block_stage", "candidate")
    n = int(cohort_row.get("n_30d") or 0)
    mean_cal_prob = cohort_row.get("mean_cal_prob_30d")
    wr = cohort_row.get("wr_30d")
    cal_gap = cohort_row.get("cal_gap_30d")
    persistence = int(cohort_row.get("persistence_days") or 0)
    cohort_date = cohort_row.get("cohort_date", "")
    cal_str = f"{mean_cal_prob:.3f}" if mean_cal_prob is not None else "n/a"
    wr_str = f"{wr:.3f}" if wr is not None else "n/a"
    if cal_gap is None:
        gap_str = "n/a"
    else:
        sign = "+" if cal_gap >= 0 else "-"
        gap_str = f"{sign}{abs(cal_gap) * 100:.1f}pp"
    return (
        f"\U0001f7e1 CAL drift: {asset} {strategy} {pband} {sband}s ({stage})\n"
        f"   n={n}  cal_prob={cal_str}  realized_wr={wr_str}  gap={gap_str}\n"
        f"   Persistence: {persistence} days  Cohort date: {cohort_date}\n"
        f"   Dashboard: {_DASHBOARD_URL}"
    )


def cohort_dedup_key(cohort_row: Dict[str, Any], kind: str) -> str:
    """Stable dedup key for `TelegramNotifier.send(dedup_key=...)`.

    `kind` is 'bleed' or 'cal' — separates the two alert families so a
    cohort firing CAL doesn't suppress its BLEED, and vice versa.
    Underscore-joined cohort key; dedup-window in TelegramNotifier is
    60s by design, well below the 23h cohort cooldown.
    """
    return "cohort_alert_{kind}_{asset}_{ptype}_{strat}_{pb}_{sb}_{stage}".format(
        kind=kind,
        asset=cohort_row["asset"],
        ptype=cohort_row["product_type"],
        strat=cohort_row["strategy"],
        pb=cohort_row["price_band_5c"],
        sb=cohort_row["stc_band_60s"],
        stage=cohort_row.get("cell_block_stage", "candidate"),
    )


def emit_alert(
    cohort_row: Dict[str, Any],
    *,
    kind: str,
) -> bool:
    """Best-effort Telegram emit via the canonical singleton.

    Returns True iff the message was handed off to the notifier
    (`enabled` AND not deduped within 60s); False if the singleton is
    unset (boot ordering / test harness) or disabled.

    Reaches the singleton via `import bot.notifier as _telegram_state`
    + module-attribute access (Bit 8.1 path-A++) so mutation freshness
    from `MainLoop.__init__` is preserved across module loads. Falls
    back gracefully if `bot.notifier` is unavailable (e.g. partial test
    harness) — never raises into the nightly cron.
    """
    try:
        import bot.notifier as _telegram_state
    except Exception:
        return False
    notifier = getattr(_telegram_state, "_TELEGRAM", None)
    if notifier is None or not getattr(notifier, "enabled", False):
        return False
    if kind == "bleed":
        payload = format_bleed_payload(cohort_row)
    elif kind == "cal":
        payload = format_calibration_payload(cohort_row)
    else:
        logging.warning(
            "[COHORT_ALERTS] emit_alert called with unknown kind=%r; "
            "expected 'bleed' or 'cal'", kind,
        )
        return False
    try:
        notifier.send(payload, dedup_key=cohort_dedup_key(cohort_row, kind))
    except Exception:
        return False
    return True
