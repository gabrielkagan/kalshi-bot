"""Supabase asset-registry parity with the weather engine.

Failure mode: `supabase_sync._WEATHER_ASSETS` silently drifts from
`weather_engine.WEATHER_CITIES`. Weather trades emit `asset = f"{city_code}_TEMP"`
(weather_engine.py ~L751). If the Supabase `assets` table is missing a row for any
such code, the FK `trades_asset_fkey` rejects every batch that contains one — the
rowid watermark never advances and trade sync freezes for all product types.

Past incidents:
- 2026-04-12..18: 486 trades dropped because weather assets were never registered
  (commit 3af5c58 added `_register_assets()`).
- 2026-04-20: Philadelphia was registered as `PHIL_TEMP` while the bot emits
  `PHI_TEMP` — 171 trades stuck, dashboard Daily P&L chart missing 04-19 bar.
  See kb/failures/supabase-fk-silent-drop.md.

This test pins the two sources together so the next typo fails in CI.
"""

import os
import sys

import pytest
import bot.engines  # noqa: F401

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)


class TestSupabaseWeatherAssetParity:
    def test_every_weather_city_is_registered(self):
        """Every city code in weather_engine must have a matching `{code}_TEMP`
        entry in supabase_sync._WEATHER_ASSETS — otherwise the first-ever
        settlement for that city FK-fails the sync batch."""
        from bot.engines.weather_engine import WEATHER_CITIES  # Sprint 10.1c sibling-reorg (2026-05-11)
        from supabase_sync import SupabaseSyncer

        expected = {f"{code}_TEMP" for code in WEATHER_CITIES}
        registered = {symbol for symbol, _name, _ticker in SupabaseSyncer._WEATHER_ASSETS}

        missing = expected - registered
        assert not missing, (
            f"Weather city codes not registered in supabase_sync._WEATHER_ASSETS: {sorted(missing)}. "
            f"Every city in weather_engine.WEATHER_CITIES must have a matching `{{code}}_TEMP` "
            f"row in _WEATHER_ASSETS, or trade sync will FK-fail on first settlement."
        )

    def test_no_orphan_registered_assets(self):
        """Every registered weather asset must correspond to a real city in
        weather_engine — catches copy-paste errors and typos like PHIL vs PHI."""
        from bot.engines.weather_engine import WEATHER_CITIES  # Sprint 10.1c sibling-reorg (2026-05-11)
        from supabase_sync import SupabaseSyncer

        expected = {f"{code}_TEMP" for code in WEATHER_CITIES}
        registered = {symbol for symbol, _name, _ticker in SupabaseSyncer._WEATHER_ASSETS}

        orphans = registered - expected
        assert not orphans, (
            f"Registered weather assets with no matching city in weather_engine.WEATHER_CITIES: "
            f"{sorted(orphans)}. Likely a typo; the bot will never emit this symbol."
        )

    def test_registered_series_tickers_match_weather_engine(self):
        """Each registered asset's series_ticker must match the weather_engine
        definition — defends against renames on one side."""
        from bot.engines.weather_engine import WEATHER_CITIES  # Sprint 10.1c sibling-reorg (2026-05-11)
        from supabase_sync import SupabaseSyncer

        for symbol, _name, series_ticker in SupabaseSyncer._WEATHER_ASSETS:
            city_code = symbol.replace("_TEMP", "")
            if city_code not in WEATHER_CITIES:
                continue  # caught by test_no_orphan_registered_assets
            expected_ticker = WEATHER_CITIES[city_code]["series_ticker"]
            assert series_ticker == expected_ticker, (
                f"{symbol} registered with series_ticker={series_ticker!r} but "
                f"weather_engine says {expected_ticker!r}"
            )
