"""D1.11.a — ``collector/espn_archiver.py`` class + envelope shape contract.

Ticket `86ba0ppy0` (2026-05-19). Pins the structural shape of the NEW
ESPN-archiver module — second non-WS bronze source after D1.8 weather.
Mirrors the ``WeatherArchiver`` (D1.8) contract shape adapted for the
ESPN deltas:

  - **HTTP-poll, no WS callbacks.** Single ``poll_once()`` method per
    cycle (60-second cadence; one HTTP call per enabled league).
  - **One channel per enabled non-esports league** (24 channels at
    D1.11.a ship: nba/nhl/mlb/wnba/nfl/ufc/atp/wta + 14 soccer
    leagues + 2 NCAA leagues; CSGO/LoL/Valorant/AFC-Intl have
    ``espn_league=None`` and are excluded). Channel name = the
    ESPN league slug (e.g. ``"nba"``, ``"eng.1"``,
    ``"uefa.champions"``).
  - **Source name:** ``espn`` (NEW slot — D0.3 §1 amended at D1.11.a
    ship).
  - **Envelope routes through ``kalshi_wire.build_envelope``** —
    single source of truth per the 2026-05-16 §5 AMENDMENT.
  - **Non-200 responses are bronze.** ESPN rate-limit responses (when
    they happen) get written as envelopes with the diagnostic shape
    (``http_status`` + ``error``); D3.x silver can model quota dynamics.

L99 PARANOID lesson: contract tests pin the shape day-1 so a future
Bit that loosens an isolation invariant fires here rather than at
adversarial review.
"""
from __future__ import annotations

import ast
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
MODULE_FILE = REPO_ROOT / "collector" / "espn_archiver.py"


def _module_source() -> str:
    assert MODULE_FILE.exists(), (
        f"{MODULE_FILE.relative_to(REPO_ROOT)} missing — D1.11.a ships "
        "this module as the ESPN bronze archiver. See "
        "kb/decisions/d1-11a-espn-bronze-plan.md for the design."
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


def test_espn_archiver_class_exists():
    """The module must export an ``ESPNArchiver`` class — the
    production seam that ``collector.espn_main_loop.run`` instantiates."""
    tree = _module_ast()
    class_names = {
        node.name for node in ast.iter_child_nodes(tree)
        if isinstance(node, ast.ClassDef)
    }
    assert "ESPNArchiver" in class_names, (
        "ESPNArchiver class missing — D1.11.a ships this class as the "
        f"public API. Existing classes: {sorted(class_names)}"
    )


def test_module_defines_leagues_espn_mapping():
    """``LEAGUES_ESPN`` constant must be defined at module level — a
    league_slug → sport mapping that mirrors the enabled+espn-eligible
    subset of ``bot.engines.sports_data.LEAGUES``.

    Pinning the constant at module level (not class-level) so silver
    D3.x parsers + tests can import it without instantiating the
    archiver.

    NOTE: this test only pins PRESENCE of the constant. The
    sister test ``test_leagues_espn_mirrors_bot_leagues`` below
    pins the DRIFT-CHECK against the bot's LEAGUES.
    """
    tree = _module_ast()
    leagues_node = None
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "LEAGUES_ESPN":
                    leagues_node = node
                    break
        elif isinstance(node, ast.AnnAssign):
            if isinstance(node.target, ast.Name) and node.target.id == "LEAGUES_ESPN":
                leagues_node = node
                break
    assert leagues_node is not None, (
        "LEAGUES_ESPN module-level constant missing — D1.11.a pins the "
        "league_slug → sport map at module scope so silver D3.x can "
        "enumerate per-channel parsers without instantiating ESPNArchiver."
    )


def test_leagues_espn_mirrors_bot_leagues():
    """Drift-check: every enabled+espn-eligible entry in the bot's
    ``LEAGUES`` must appear in the collector's ``LEAGUES_ESPN`` with the
    matching sport, and vice-versa.

    This is the WEATHER_CITIES lock-step pattern from D1.8 applied to
    the ESPN league set. When the bot enables/disables a league or adds
    a new one, THIS test fails RED and the operator must update both
    sides + restart the collector to pick up the new channel.

    The test imports from ``bot.engines.sports_data`` because the TEST
    file is allowed to import from bot (D0.3 §10 forbids the COLLECTOR
    PRODUCTION code from doing so; tests are unrestricted).
    """
    import bot.engines.sports_data as sd
    expected_pairs = {
        cfg.espn_league: cfg.espn_sport
        for series, cfg in sd.LEAGUES.items()
        if cfg.enabled and cfg.espn_league is not None
    }
    # Now import the collector-side constant.
    import importlib
    import sys
    sys.path.insert(0, str(REPO_ROOT))
    try:
        espn_archiver = importlib.import_module("collector.espn_archiver")
    finally:
        sys.path.pop(0)
    actual_pairs = dict(espn_archiver.LEAGUES_ESPN)
    assert expected_pairs == actual_pairs, (
        f"LEAGUES_ESPN drift from bot/engines/sports_data.LEAGUES.\n"
        f"In bot but NOT collector: "
        f"{ {k: v for k, v in expected_pairs.items() if k not in actual_pairs} }\n"
        f"In collector but NOT bot: "
        f"{ {k: v for k, v in actual_pairs.items() if k not in expected_pairs} }\n"
        f"Sport-mismatch: "
        f"{ {k: (expected_pairs.get(k), actual_pairs.get(k)) for k in expected_pairs if k in actual_pairs and expected_pairs[k] != actual_pairs[k]} }\n"
        f"To fix: update collector/espn_archiver.py LEAGUES_ESPN to match, "
        f"then restart kalshi-espn-collector on the VPS so the new channel "
        f"is registered as a BronzeWriter."
    )


def test_module_defines_espn_source_constant():
    """The bronze ``_source`` string must be ``espn`` (NEW slot at
    D1.11.a — D0.3 §1 amendment).

    Pinned as a module-level constant so writer + archiver
    construction can't drift.
    """
    src = _module_source()
    assert '"espn"' in src or "'espn'" in src, (
        "Module must use the string literal 'espn' as the bronze "
        "_source. D0.3 §1 amended at D1.11.a ship to add this slot. "
        "See kb/decisions/d1-11a-espn-bronze-plan.md #1."
    )


def test_module_imports_build_envelope_from_kalshi_wire():
    """Envelope construction routes through ``kalshi_wire.ws_client.build_envelope``
    — single source of truth per D0.3 §2 + the 2026-05-16 §5 AMENDMENT.

    Without this routing, a parallel envelope-construction site in the
    ESPN module would drift from kalshi_wire's invariants (6 fields,
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
    """The only external dependency for ESPN polling is ``requests``
    (already a bot dependency). No new packages.

    Negative pin so a future Bit that introduces a heavyweight
    dependency (e.g., ``pandas`` for response parsing) fires here.
    Bronze stores raw ESPN JSON, NOT parsed records — there's no
    parsing tier in bronze.
    """
    tree = _module_ast()
    forbidden = {"pandas", "numpy", "xarray", "lxml", "bs4"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                top = alias.name.split(".")[0]
                assert top not in forbidden, (
                    f"`import {alias.name}` forbidden in ESPN bronze — "
                    f"bronze stores raw JSON only; parsing lives in "
                    f"silver D3.x."
                )
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                top = node.module.split(".")[0]
                assert top not in forbidden, (
                    f"`from {node.module} import …` forbidden in ESPN "
                    f"bronze — bronze stores raw JSON only."
                )


def test_espn_archiver_has_poll_once_method():
    """``ESPNArchiver.poll_once`` is the per-cycle public API —
    one call per scheduled poll tick fetches all leagues and
    dispatches to BronzeWriters.
    """
    tree = _module_ast()
    archiver_cls = next(
        (node for node in ast.iter_child_nodes(tree)
         if isinstance(node, ast.ClassDef) and node.name == "ESPNArchiver"),
        None,
    )
    assert archiver_cls is not None, "ESPNArchiver class missing"
    method_names = {
        node.name for node in archiver_cls.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    assert "poll_once" in method_names, (
        f"ESPNArchiver.poll_once method missing — D1.11.a main loop "
        f"calls this per scheduled tick. Existing methods: "
        f"{sorted(method_names)}"
    )


def test_espn_archiver_no_async_methods():
    """ESPN archiver uses synchronous ``requests``, NOT asyncio.

    Pin per CLAUDE.md anti-pattern: 'Don't add async. Synchronous +
    threading for WS feeds is the design.' Mirrors D1.8 weather pattern.
    """
    tree = _module_ast()
    for node in ast.walk(tree):
        assert not isinstance(node, ast.AsyncFunctionDef), (
            f"Async function `{getattr(node, 'name', '?')}` forbidden — "
            f"ESPN collector is synchronous (CLAUDE.md anti-pattern)."
        )


def test_module_has_envelope_diagnostic_field_set():
    """The diagnostic envelope wrapper inside ``_raw`` carries the
    diagnostic fields per the D1.11.a plan-doc 'Bronze record shape'
    section: ``sport`` + ``league`` + ``http_status`` + ``elapsed_ms``
    + ``poll_ts`` (plus the verbatim ``response`` on success or
    ``error`` on failure).

    Pinned as string-search in the module source so the silver D3.x
    parser knows what keys to dispatch on.
    """
    src = _module_source()
    required_keys = ["sport", "league", "http_status", "elapsed_ms"]
    for key in required_keys:
        assert f'"{key}"' in src or f"'{key}'" in src, (
            f"Diagnostic envelope field '{key}' not found in module "
            f"source. The D1.11.a plan-doc 'Bronze record shape' pins "
            f"this — silver D3.x parsers dispatch on these keys."
        )


def test_module_pins_espn_base_url():
    """The bot's existing ESPN base URL must be reused verbatim.

    Pin per D1.11.a plan-doc §5 ('Independent poller — does NOT tee
    from bot/engines/sports_engine.py'). Drift between collector and
    bot's URLs would mean bronze captures a different surface than
    the bot's parser expects.

    The string is pinned, not the import — D0.3 §10 prevents importing
    from bot.*.
    """
    src = _module_source()
    assert "https://site.api.espn.com/apis/site/v2/sports" in src, (
        "Module must use ESPN_BASE='https://site.api.espn.com/apis/"
        "site/v2/sports' — same URL the bot polls in "
        "bot/engines/sports_engine.py:139. Drift would mean bronze "
        "captures a different endpoint than what the bot's existing "
        "parser handles."
    )


def test_module_pins_scoreboard_endpoint_suffix():
    """The endpoint suffix ``/scoreboard`` must appear in the module —
    D1.11.a captures the per-league scoreboard endpoint (NOT summary,
    NOT odds; those are D1.11.b + D1.11.c).
    """
    src = _module_source()
    assert "scoreboard" in src, (
        "Module must reference '/scoreboard' endpoint — D1.11.a is "
        "scoped to the per-league scoreboard channel only. /summary "
        "(D1.11.b) and /odds (D1.11.c) are followup Bits."
    )
