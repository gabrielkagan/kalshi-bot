"""bot/shadows/ — shadow / observation-only engines (Sprint 10.2 sibling-reorg, 2026-05-11).

Per master plan L2225-2238 Sprint 10 row: "shadows/ — fifteenm, hourly_alt,
spx_harrv". Three shadow engines that run alongside production engines but
DO NOT trade — they write to dedicated `*_shadow_signals` tables for offline
calibration analysis (Brier scores, edge-distribution diagnostics, etc.).

Submodule contents (filenames preserved from pre-move repo-root location):
  - `fifteenm_shadow.py`  — 15M shadow variants A1/A2/A3/A4
  - `hourly_alt_shadow.py` — hourly alternate sim (MM + HAR-RV)
  - `spx_harrv_shadow.py` — SPX HAR-RV shadow

Each is independently importable: `from bot.shadows.fifteenm_shadow import
FifteenMShadowEngine, FIFTEENM_SHADOW_ENABLED` (mirrors bot/engines/
sibling-reorg pattern from Sprint 10.1).

No package-level re-exports — callers reach each shadow directly via the
submodule path. Mirrors the 10.1 precedent (bot/engines/__init__.py also
focused on its 3 main engine classes; sports_data + sports_engine + etc.
are reached via direct submodule imports).
"""
