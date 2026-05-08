"""Bit 3.2: Time/regime feature helpers (Tier 4), extracted from bot/_impl.py."""
import datetime
from datetime import timezone
from typing import Dict, Optional

from bot.constants import FOMC_ANNOUNCEMENT_DATES, CPI_RELEASE_DATES

def compute_time_regime_features(eval_time_iso: Optional[str] = None) -> Dict[str, Optional[int]]:
    """Compute Tier 4 (time/regime) features for a timestamp.

    Returns dict with: hour_of_day_utc, day_of_week (Sun=0), is_weekend (0/1),
    minutes_since_us_open (negative if pre-open), is_fomc_day (0/1),
    is_cpi_day (0/1). Safe for None/malformed inputs.
    """
    null_result = {
        "hour_of_day_utc": None, "day_of_week": None, "is_weekend": None,
        "minutes_since_us_open": None, "is_fomc_day": None, "is_cpi_day": None,
    }
    try:
        from zoneinfo import ZoneInfo
    except ImportError:
        return null_result

    if eval_time_iso is None:
        now_utc = datetime.datetime.now(timezone.utc)
    else:
        # Handle 'Z' suffix (Python ISO parsing needs +00:00)
        s = eval_time_iso.replace("Z", "+00:00")
        try:
            now_utc = datetime.datetime.fromisoformat(s)
        except (ValueError, TypeError):
            return null_result
        if now_utc.tzinfo is None:
            now_utc = now_utc.replace(tzinfo=timezone.utc)

    try:
        hour_of_day_utc = now_utc.hour
        # weekday(): Mon=0..Sun=6. Convert to Sun=0, Mon=1, ..., Sat=6.
        day_of_week = (now_utc.weekday() + 1) % 7
        is_weekend = 1 if day_of_week in (0, 6) else 0

        # Minutes since US market open (9:30 AM ET). Handles DST automatically.
        et = now_utc.astimezone(ZoneInfo("America/New_York"))
        us_open = et.replace(hour=9, minute=30, second=0, microsecond=0)
        minutes_since_us_open = int((et - us_open).total_seconds() / 60)

        date_str = now_utc.strftime("%Y-%m-%d")
        is_fomc_day = 1 if date_str in FOMC_ANNOUNCEMENT_DATES else 0
        is_cpi_day = 1 if date_str in CPI_RELEASE_DATES else 0

        return {
            "hour_of_day_utc": hour_of_day_utc,
            "day_of_week": day_of_week,
            "is_weekend": is_weekend,
            "minutes_since_us_open": minutes_since_us_open,
            "is_fomc_day": is_fomc_day,
            "is_cpi_day": is_cpi_day,
        }
    except Exception:
        return null_result
