"""D1.8 — ``collector/weather_archiver.py`` class + envelope shape contract.

Ticket `86ba0duck` (2026-05-18). Pins the structural shape of the NEW
weather-archiver module — first non-WS bronze source. Mirrors the
``CoinbaseArchiver`` (D2.2) contract shape adapted for HTTP polling:

  - **No WS callbacks.** Weather is HTTP poll, not WebSocket. No
    ``on_session_start`` / ``on_frame`` / ``on_session_end`` —
    instead a single ``poll_once(target_date)`` method per cycle.
  - **4 channels:** ``ensemble_gfs`` / ``ensemble_ecmwf`` /
    ``forecast_hrrr`` / ``archive_observed`` (next-day verification).
  - **Source name:** ``open_meteo`` (NOT ``nws_hrrr`` — D0.3 §1
    slot reservation was aspirational; the bot polls Open-Meteo's
    aggregated HRRR via ``OPEN_METEO_FORECAST_URL`` with
    ``models=ncep_hrrr_conus``, NOT NWS direct).
  - **Envelope routes through ``kalshi_wire.build_envelope``** —
    single source of truth per the 2026-05-16 §5 AMENDMENT.
  - **Non-200 responses are bronze.** Open-Meteo 429 storms get
    written as envelopes with the diagnostic shape (``http_status``
    + ``error``); D3.x silver can model the quota dynamics.

L99 PARANOID lesson: contract tests pin the shape day-1 so a future
Bit that loosens an isolation invariant fires here rather than at
adversarial review.
"""
from __future__ import annotations

import ast
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
MODULE_FILE = REPO_ROOT / "collector" / "weather_archiver.py"


def _module_source() -> str:
    assert MODULE_FILE.exists(), (
        f"{MODULE_FILE.relative_to(REPO_ROOT)} missing — D1.8 ships this "
        "module as the weather bronze archiver. See "
        "kb/decisions/d1-8-weather-bronze-plan.md for the design."
    )
    return MODULE_FILE.read_text()


def _module_ast() -> ast.Module:
    return ast.parse(_module_source())


def test_module_has_no_bot_imports():
    """No ``bot.*`` imports — D0.3 §10 isolation contract.

    Pinned structurally by the ``collector-no-bot`` import-linter
    contract (Contract 7); this is AST defense-in-depth so a refactor
    catches the regression at the contract tier BEFORE lint-imports
    runs.
    """
    tree = _module_ast()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert not alias.name.startswith(("bot.", "bot ")), (
                    f"`import {alias.name}` forbidden — D0.3 §10 "
                    f"bot-isolation contract."
                )
                assert alias.name != "bot", (
                    "`import bot` forbidden — D0.3 §10 bot-isolation."
                )
        elif isinstance(node, ast.ImportFrom):
            mod = node.module
            assert mod is not None, "relative imports forbidden in this module"
            assert not mod.startswith("bot."), (
                f"`from {mod} import …` forbidden — D0.3 bot-isolation."
            )
            assert mod != "bot", (
                "`from bot import …` forbidden — D0.3 bot-isolation."
            )


def test_weather_archiver_class_exists():
    """The module must export a ``WeatherArchiver`` class — the
    production seam that ``collector.weather_main_loop.run`` instantiates."""
    tree = _module_ast()
    class_names = {
        node.name for node in ast.iter_child_nodes(tree)
        if isinstance(node, ast.ClassDef)
    }
    assert "WeatherArchiver" in class_names, (
        "WeatherArchiver class missing — D1.8 ships this class as the "
        f"public API. Existing classes: {sorted(class_names)}"
    )


def test_module_defines_default_channels_tuple_with_four_entries():
    """``DEFAULT_CHANNELS`` constant must be defined at module level with
    the 4 channels: ``ensemble_gfs`` / ``ensemble_ecmwf`` /
    ``forecast_hrrr`` / ``archive_observed``.

    Pinning the constant at module level (not class-level) so D3.x silver
    parsers can import it without instantiating the archiver.
    """
    tree = _module_ast()
    # Find module-level assignment to DEFAULT_CHANNELS.
    default_channels_node = None
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "DEFAULT_CHANNELS":
                    default_channels_node = node
                    break
        elif isinstance(node, ast.AnnAssign):
            if isinstance(node.target, ast.Name) and node.target.id == "DEFAULT_CHANNELS":
                default_channels_node = node
                break
    assert default_channels_node is not None, (
        "DEFAULT_CHANNELS module-level constant missing — D1.8 pins the "
        "4-channel set: ensemble_gfs / ensemble_ecmwf / forecast_hrrr / "
        "archive_observed. Silver D3.x reads this constant when "
        "enumerating per-channel parsers."
    )
    # Extract the tuple/list literal value.
    value_node = (
        default_channels_node.value
        if isinstance(default_channels_node, ast.AnnAssign)
        else default_channels_node.value
    )
    assert isinstance(value_node, (ast.Tuple, ast.List)), (
        f"DEFAULT_CHANNELS must be a tuple or list literal, got "
        f"{type(value_node).__name__}"
    )
    channel_strs = {
        elt.value for elt in value_node.elts
        if isinstance(elt, ast.Constant) and isinstance(elt.value, str)
    }
    expected = {"ensemble_gfs", "ensemble_ecmwf", "forecast_hrrr", "archive_observed"}
    assert channel_strs == expected, (
        f"DEFAULT_CHANNELS mismatch: got {sorted(channel_strs)}, "
        f"expected {sorted(expected)}. Adding a channel without "
        f"updating downstream silver D3.x parsers would silently drop "
        f"the new data — keep this contract narrow."
    )


def test_module_defines_open_meteo_source_constant():
    """The bronze ``_source`` string must be ``open_meteo`` (NOT
    ``nws_hrrr`` per the D1.8 plan-doc decision #1).

    Pinned as a module-level constant so writer + archiver
    construction can't drift.
    """
    src = _module_source()
    assert '"open_meteo"' in src or "'open_meteo'" in src, (
        "Module must use the string literal 'open_meteo' as the bronze "
        "_source. The bot polls Open-Meteo's aggregated HRRR — NOT NWS "
        "direct — so 'nws_hrrr' (the D0.3 §1 slot reservation) would be "
        "misleading. See kb/decisions/d1-8-weather-bronze-plan.md #1."
    )
    assert '"nws_hrrr"' not in src and "'nws_hrrr'" not in src, (
        "Module must NOT use 'nws_hrrr' as the _source — that would "
        "claim bronze contains NWS-direct data when it actually "
        "contains Open-Meteo aggregations."
    )


def test_module_imports_build_envelope_from_kalshi_wire():
    """Envelope construction routes through ``kalshi_wire.ws_client.build_envelope``
    — single source of truth per D0.3 §2 + the 2026-05-16 §5 AMENDMENT.

    Without this routing, a parallel envelope-construction site in the
    weather module would drift from kalshi_wire's invariants (6 fields,
    ISO-8601 UTC µs ``_wire_recv_ts``, ``_raw`` as string).
    """
    tree = _module_ast()
    found = False
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.module == "kalshi_wire.ws_client":
                if any(alias.name == "build_envelope" for alias in node.names):
                    found = True
                    break
        elif isinstance(node, ast.Import):
            if any(alias.name == "kalshi_wire.ws_client" for alias in node.names):
                found = True
                break
    assert found, (
        "Module must import `build_envelope` from "
        "`kalshi_wire.ws_client` (single source of truth per D0.3 §2 + "
        "2026-05-16 §5 AMENDMENT). Inline envelope construction would "
        "drift from the 6-field contract."
    )


def test_module_imports_only_allowed_external_deps():
    """The only external dependency for weather polling is ``requests``
    (already a bot dependency). No new packages.

    Negative pin so a future Bit that introduces a heavyweight
    dependency (e.g., ``xarray`` for GRIB2 parsing, ``netcdf4``,
    ``pygrib``) fires here. Weather bronze stores raw Open-Meteo JSON,
    NOT GRIB2 — there's no parsing tier in bronze.
    """
    tree = _module_ast()
    forbidden = {"xarray", "netcdf4", "pygrib", "cfgrib", "pandas", "numpy"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                top = alias.name.split(".")[0]
                assert top not in forbidden, (
                    f"`import {alias.name}` forbidden in weather bronze — "
                    f"bronze stores raw JSON only; GRIB2/scientific parsing "
                    f"lives in silver D3.x (and even then, the bot uses "
                    f"Open-Meteo's JSON aggregation, not GRIB2)."
                )
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                top = node.module.split(".")[0]
                assert top not in forbidden, (
                    f"`from {node.module} import …` forbidden in weather "
                    f"bronze — bronze stores raw JSON only."
                )


def test_weather_archiver_has_poll_once_method():
    """``WeatherArchiver.poll_once`` is the per-cycle public API —
    one call per scheduled poll tick fetches all cities × all channels
    and dispatches to BronzeWriters.
    """
    tree = _module_ast()
    archiver_cls = next(
        (node for node in ast.iter_child_nodes(tree)
         if isinstance(node, ast.ClassDef) and node.name == "WeatherArchiver"),
        None,
    )
    assert archiver_cls is not None, "WeatherArchiver class missing"
    method_names = {
        node.name for node in archiver_cls.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    assert "poll_once" in method_names, (
        f"WeatherArchiver.poll_once method missing — D1.8 main loop "
        f"calls this per scheduled tick. Existing methods: "
        f"{sorted(method_names)}"
    )


def test_weather_archiver_no_async_methods():
    """Weather archiver uses synchronous ``requests``, NOT asyncio.

    Pin per CLAUDE.md anti-pattern: 'Don't add async. Synchronous +
    threading for WS feeds is the design.' Weather is HTTP polling
    — single-threaded calls with explicit sleeps between cities
    (the existing bot poller's pattern). asyncio would be a
    surprising new dependency surface.
    """
    tree = _module_ast()
    for node in ast.walk(tree):
        assert not isinstance(node, ast.AsyncFunctionDef), (
            f"Async function `{getattr(node, 'name', '?')}` forbidden — "
            f"weather collector is synchronous (CLAUDE.md anti-pattern)."
        )


def test_module_has_envelope_diagnostic_field_set():
    """The diagnostic envelope wrapper inside ``_raw`` carries the 5
    diagnostic fields per the D1.8 plan-doc 'Bronze record shape'
    section: ``city_code`` + ``target_date`` + ``model`` (or channel)
    + ``http_status`` + ``elapsed_ms`` (plus the verbatim ``response``).

    Pinned as string-search in the module source so the silver D3.x
    parser knows what keys to dispatch on.
    """
    src = _module_source()
    required_keys = ["city_code", "http_status", "elapsed_ms"]
    for key in required_keys:
        assert f'"{key}"' in src or f"'{key}'" in src, (
            f"Diagnostic envelope field '{key}' not found in module "
            f"source. The D1.8 plan-doc 'Bronze record shape' pins "
            f"this — silver D3.x parsers dispatch on these keys."
        )
