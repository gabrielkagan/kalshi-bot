"""Bit 4.4 — DeribitDVOLFetcher + CoinGlassFetcher extracted from bot/_impl.py
to bot/fetchers/{deribit,coinglass}.py via the bot/fetchers/__init__.py
subpackage.

Locks the contract between bot/_impl.py (which does
`from bot.fetchers import DeribitDVOLFetcher, CoinGlassFetcher` after the
KalshiClient import) and the new bot/fetchers/ subpackage. Mirrors
tests/test_kalshi_client_extraction.py (Bit 4.3) and
tests/test_helpers_extraction.py (Bit 3.2 — the prior subpackage).

L2 (Bit 3.0.5): tests call production directly. No reimplementing the
contract in test helpers.

L29 (Bit 4.1): doc-drift in agent_docs/bot_layout.md ships in the same
atomic commit (regen via the recipe in the doc preamble).

L31 (Bit 4.2.5.1): pre-flight grep for whole-file drift surfaces — the
two fetchers reference module-level constants that span bot/constants.py
(DERIBIT_DVOL_*, DVOL_*, COINGLASS_*) AND config.py (DVOL_ANNUALIZED_TO_5S).
Both import paths are pinned by tests below.

L32 (Bit 4.3): Plan-agent-style pre-emptive findings baked in — the
DVOL_ANNUALIZED_TO_5S constant lives in config.py (not bot/constants.py),
which would silently break if the new module imported only from
bot.constants.
"""
import ast
import importlib
import logging
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]


# ─── 1. Files exist + imports ───────────────────────────────────────────────


def test_fetchers_subpackage_init_exists():
    assert (REPO_ROOT / "bot" / "fetchers" / "__init__.py").is_file()


def test_deribit_module_exists():
    assert (REPO_ROOT / "bot" / "fetchers" / "deribit.py").is_file()


def test_coinglass_module_exists():
    assert (REPO_ROOT / "bot" / "fetchers" / "coinglass.py").is_file()


def test_subpackage_imports():
    importlib.import_module("bot.fetchers")


def test_subpackage_exports_both_classes():
    import bot.fetchers
    assert hasattr(bot.fetchers, "DeribitDVOLFetcher")
    assert hasattr(bot.fetchers, "CoinGlassFetcher")


# ─── 2. Identity preservation across re-export chain ────────────────────────


def test_deribit_identity_through_bot_impl():
    import pytest as _pytest_bit_iii_c_skip; _pytest_bit_iii_c_skip.skip("bot/_impl.py removed (Bit 9.3-iii.c) — re-export contract retired", allow_module_level=False)
    import bot.fetchers as bf
    import bot.fetchers.deribit as bfd
    assert b.DeribitDVOLFetcher is bf.DeribitDVOLFetcher is bfd.DeribitDVOLFetcher


def test_coinglass_identity_through_bot_impl():
    import pytest as _pytest_bit_iii_c_skip; _pytest_bit_iii_c_skip.skip("bot/_impl.py removed (Bit 9.3-iii.c) — re-export contract retired", allow_module_level=False)
    import bot.fetchers as bf
    import bot.fetchers.coinglass as bfc
    assert b.CoinGlassFetcher is bf.CoinGlassFetcher is bfc.CoinGlassFetcher


def test_deribit_identity_through_bot_proxy():
    import bot
    import bot.fetchers.deribit as bfd
    assert bot.fetchers.DeribitDVOLFetcher is bfd.DeribitDVOLFetcher


def test_coinglass_identity_through_bot_proxy():
    import bot
    import bot.fetchers.coinglass as bfc
    assert bot.fetchers.CoinGlassFetcher is bfc.CoinGlassFetcher


# ─── 3. Drift guards (AST + source-string) ──────────────────────────────────


@pytest.mark.parametrize("class_name", ["DeribitDVOLFetcher", "CoinGlassFetcher"])
def test_class_not_defined_in_bot_impl(class_name):
    """Future drift guard: catches "I'll just add it back to _impl.py".

    Mirrors test_kalshi_client_class_not_defined_in_bot_impl (Bit 4.3).
    """
    bot_impl = REPO_ROOT / "bot" / "_impl.py"
    if not bot_impl.exists() if hasattr(bot_impl, 'exists') else not __import__('os').path.exists(bot_impl): pytest.skip("bot/_impl.py removed (Bit 9.3-iii.c)")
    tree = ast.parse(bot_impl.read_text(), filename=str(bot_impl))
    classdefs = [
        node for node in ast.iter_child_nodes(tree)
        if isinstance(node, ast.ClassDef) and node.name == class_name
    ]
    assert classdefs == [], (
        f"{class_name} ClassDef found at module scope in bot/_impl.py "
        f"(line {classdefs[0].lineno if classdefs else '?'}). The class was "
        f"extracted to bot/fetchers/ in Bit 4.4 — re-introducing it breaks "
        f"the import chain and identity preservation."
    )


def test_bot_impl_imports_fetchers_subpackage():
    """bot/_impl.py must import both fetchers from bot.fetchers.

    AST-based to avoid false matches inside docstrings/comments.
    """
    bot_impl = REPO_ROOT / "bot" / "_impl.py"
    if not bot_impl.exists() if hasattr(bot_impl, 'exists') else not __import__('os').path.exists(bot_impl): pytest.skip("bot/_impl.py removed (Bit 9.3-iii.c)")
    tree = ast.parse(bot_impl.read_text(), filename=str(bot_impl))
    imported = set()
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "bot.fetchers":
            for alias in node.names:
                imported.add(alias.name)
    missing = {"DeribitDVOLFetcher", "CoinGlassFetcher"} - imported
    assert not missing, (
        f"bot/_impl.py is missing fetchers re-imports: {sorted(missing)}. "
        f"Without them, MainLoop construction + Optional[DeribitDVOLFetcher] "
        f"type annotation on VolatilityEngine.__init__ break."
    )


# ─── 4. Constants resolve at import time ────────────────────────────────────


DERIBIT_CONSTANTS = (
    "DERIBIT_DVOL_CURRENCIES",
    "DERIBIT_DVOL_URL",
    "DVOL_CACHE_TTL",
    "DVOL_FETCH_INTERVAL",
    "DVOL_HOURLY_AVG_MAXLEN",
    "DVOL_HOURLY_AVG_MIN",
    "DVOL_REQUEST_TIMEOUT",
)


@pytest.mark.parametrize("name", DERIBIT_CONSTANTS)
def test_deribit_constants_resolve_from_bot_constants(name):
    """All DVOL_* / DERIBIT_DVOL_* constants live in bot.constants per Bit 3.1.

    The deribit module imports them explicitly. Pin the locations.
    """
    import bot.constants
    import bot.fetchers.deribit as bfd
    bot_value = getattr(bot.constants, name)
    deribit_value = getattr(bfd, name)
    assert bot_value is deribit_value, (
        f"bot.fetchers.deribit.{name} ({deribit_value!r}) drifted from "
        f"bot.constants.{name} ({bot_value!r})."
    )


def test_dvol_annualized_to_5s_from_config():
    """DVOL_ANNUALIZED_TO_5S is one of the few constants that DID NOT move
    to bot/constants.py in Bit 3.1 — it remains in config.py because it's
    derived from VOL_RETURN_INTERVAL + SECONDS_PER_YEAR which also stayed
    there. The deribit module imports it directly from config.

    L31 (Bit 4.2.5.1): a future maintainer might assume all constants live
    in bot/constants.py and miss this. Pin the actual location.
    """
    import bot.fetchers.deribit as bfd
    import config
    assert bfd.DVOL_ANNUALIZED_TO_5S == config.DVOL_ANNUALIZED_TO_5S


COINGLASS_CONSTANTS = (
    "COINGLASS_API_URL",
    "COINGLASS_CACHE_TTL",
    "COINGLASS_FETCH_INTERVAL",
    "COINGLASS_REQUEST_TIMEOUT",
    "COINGLASS_SYMBOLS",
)


@pytest.mark.parametrize("name", COINGLASS_CONSTANTS)
def test_coinglass_constants_resolve_from_bot_constants(name):
    """All COINGLASS_* constants live in bot.constants per Bit 3.1."""
    import bot.constants
    import bot.fetchers.coinglass as bfc
    bot_value = getattr(bot.constants, name)
    cg_value = getattr(bfc, name)
    assert bot_value is cg_value, (
        f"bot.fetchers.coinglass.{name} ({cg_value!r}) drifted from "
        f"bot.constants.{name} ({bot_value!r})."
    )


# ─── 5. Method-presence pins ────────────────────────────────────────────────


DERIBIT_METHODS = ("__init__", "start", "stop", "get_dvol", "get_dvol_hourly_avg",
                   "_run", "_fetch_latest_dvol")


@pytest.mark.parametrize("method", DERIBIT_METHODS)
def test_deribit_method_present(method):
    from bot.fetchers.deribit import DeribitDVOLFetcher
    assert callable(getattr(DeribitDVOLFetcher, method, None))


COINGLASS_METHODS = ("__init__", "start", "stop", "get_funding_rate",
                     "_run", "_fetch_funding")


@pytest.mark.parametrize("method", COINGLASS_METHODS)
def test_coinglass_method_present(method):
    from bot.fetchers.coinglass import CoinGlassFetcher
    assert callable(getattr(CoinGlassFetcher, method, None))


# ─── 6. Module hygiene ──────────────────────────────────────────────────────


@pytest.mark.parametrize("submodule", ["deribit", "coinglass", "__init__"])
def test_no_forbidden_numerical_imports(submodule):
    """No numpy/scipy/torch/sklearn/pandas in fetcher modules. Bit 4.1/4.2/4.3
    precedent — numerical libs have OMP thread-count side effects (per
    bot/CLAUDE.md "Threading + numerical libraries") and must not load
    before bot._thread_env."""
    src_path = REPO_ROOT / "bot" / "fetchers" / f"{submodule}.py"
    src = src_path.read_text()
    forbidden = ("numpy", "scipy", "torch", "sklearn", "pandas")
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".", 1)[0]
                assert root not in forbidden, (
                    f"bot/fetchers/{submodule}.py imports {alias.name!r}."
                )
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                root = node.module.split(".", 1)[0]
                assert root not in forbidden, (
                    f"bot/fetchers/{submodule}.py imports from {node.module!r}."
                )


@pytest.mark.parametrize("submodule", ["deribit", "coinglass", "__init__"])
def test_no_circular_bot_impl_import(submodule):
    """Submodules MUST NOT import from bot._impl. Three forms checked
    (per Bit 4.1 R2 #1 hole: `from bot._impl ...`, `import bot._impl`,
    `from bot import _impl`).
    """
    src_path = REPO_ROOT / "bot" / "fetchers" / f"{submodule}.py"
    src = src_path.read_text()
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            assert node.module != "bot._impl", (
                f"bot/fetchers/{submodule}.py uses `from bot._impl import ...` — cycle."
            )
            if node.module == "bot":
                for alias in node.names:
                    assert alias.name != "_impl", (
                        f"bot/fetchers/{submodule}.py uses `from bot import _impl` — cycle."
                    )
        elif isinstance(node, ast.Import):
            for alias in node.names:
                assert alias.name != "bot._impl", (
                    f"bot/fetchers/{submodule}.py uses `import bot._impl` — cycle."
                )


# ─── 7. Root-logger handler regression (subprocess isolation) ───────────────


def test_fetchers_does_not_install_root_logger_handlers():
    """Importing bot.fetchers must NOT install handlers on the root
    logger. Mirrors Logger / TelegramNotifier / KalshiClient subprocess-
    isolation tests (Bit 4.1, 4.2, 4.3).
    """
    code = (
        "import logging\n"
        "before = list(logging.getLogger().handlers)\n"
        "import bot.fetchers  # noqa: F401\n"
        "after = list(logging.getLogger().handlers)\n"
        "added = [h for h in after if h not in before]\n"
        "if added:\n"
        "    print(f'ADDED: {added}')\n"
        "    raise SystemExit(1)\n"
        "print('clean')\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, (
        f"bot.fetchers added root-logger handlers at import time:\n"
        f"  stdout: {result.stdout}\n"
        f"  stderr: {result.stderr}"
    )


# ─── 8. Behavioral smoke (cache + lock state) ───────────────────────────────


def test_deribit_get_dvol_returns_none_for_unknown_asset():
    """Empty cache returns None. Verifies post-extraction class behavior."""
    from bot.fetchers.deribit import DeribitDVOLFetcher
    f = DeribitDVOLFetcher()
    assert f.get_dvol("BTC") is None
    assert f.get_dvol("UNKNOWN") is None


def test_deribit_get_hourly_avg_returns_none_when_below_min():
    """Insufficient samples (< DVOL_HOURLY_AVG_MIN) returns None."""
    from bot.fetchers.deribit import DeribitDVOLFetcher
    f = DeribitDVOLFetcher()
    assert f.get_dvol_hourly_avg("BTC") is None


def test_coinglass_get_funding_rate_returns_none_for_unknown_asset():
    """Empty cache returns None."""
    from bot.fetchers.coinglass import CoinGlassFetcher
    f = CoinGlassFetcher()
    assert f.get_funding_rate("BTC") is None


def test_coinglass_start_short_circuits_without_api_key(monkeypatch):
    """When COINGLASS_API_KEY is unset, start() must not spawn a thread.
    Pins the disabled-mode contract: a debug user without the API key
    should not hit network requests on startup.
    """
    monkeypatch.delenv("COINGLASS_API_KEY", raising=False)
    from bot.fetchers.coinglass import CoinGlassFetcher
    f = CoinGlassFetcher()
    f.start()
    assert f._thread is None


# ─── 9. Subpackage __init__ contract ────────────────────────────────────────


def test_subpackage_init_re_exports_match_submodule_classes():
    """bot.fetchers exports the same class objects as the submodules.
    A future maintainer might add a third fetcher and forget to wire
    the __init__.py re-export."""
    import bot.fetchers
    import bot.fetchers.deribit
    import bot.fetchers.coinglass
    assert bot.fetchers.DeribitDVOLFetcher is bot.fetchers.deribit.DeribitDVOLFetcher
    assert bot.fetchers.CoinGlassFetcher is bot.fetchers.coinglass.CoinGlassFetcher


# ─── 10. Annotation-consumer pin (R1 finding) ──────────────────────────────


def test_deribit_annotation_consumer_is_volatility_engine():
    """`Optional[DeribitDVOLFetcher]` annotation lives on
    `VolatilityEngine.__init__` (NOT `OrderFlowEngine.__init__` as a R1
    documentation drift incorrectly claimed). This test pins which class
    consumes the re-import — if a future bit moves the dvol_fetcher
    parameter to a different class without updating the breadcrumb (search
    for `Bit 4.4 leaf extraction` in bot/_impl.py), this trips so the
    comment doesn't rot.
    """
    # VolatilityEngine moved to bot/engines/volatility.py per Bit 6.1
    # (2026-05-09). Retarget the AST walk per L38 (Bit 4.5b).
    target_path = REPO_ROOT / "bot" / "engines" / "volatility.py"
    src = target_path.read_text()
    tree = ast.parse(src)

    # Find the VolatilityEngine class
    vol_engine = next(
        (
            node
            for node in ast.iter_child_nodes(tree)
            if isinstance(node, ast.ClassDef) and node.name == "VolatilityEngine"
        ),
        None,
    )
    assert vol_engine is not None, (
        "VolatilityEngine ClassDef missing from bot/engines/volatility.py "
        "(post-Bit-6.1 location)."
    )

    # Find its __init__
    init = next(
        (
            n
            for n in vol_engine.body
            if isinstance(n, ast.FunctionDef) and n.name == "__init__"
        ),
        None,
    )
    assert init is not None, "VolatilityEngine.__init__ missing."

    # Render the source segment and check it contains the annotation
    init_src = ast.get_source_segment(src, init) or ""
    assert "Optional[DeribitDVOLFetcher]" in init_src, (
        "VolatilityEngine.__init__ no longer annotates dvol_fetcher with "
        "Optional[DeribitDVOLFetcher]. Either the annotation was removed "
        "(then drop the `Bit 4.4 leaf extraction` breadcrumb in "
        "bot/_impl.py referencing it) or it moved to a different class "
        "(then update the breadcrumb to name that class)."
    )


def test_no_orderflowengine_dvol_fetcher_annotation():
    """Negative pin: OrderFlowEngine does NOT annotate any parameter as
    `Optional[DeribitDVOLFetcher]`. Documentation breadcrumbs that
    incorrectly cite OrderFlowEngine as the consumer would silently
    succeed without this guard.
    """
    # OrderFlowEngine moved to bot/order_flow.py per Bit 9.3.5
    # (2026-05-10). Retarget the AST walk per L38.
    order_flow = REPO_ROOT / "bot" / "order_flow.py"
    src = order_flow.read_text()
    tree = ast.parse(src)

    of_engine = next(
        (
            node
            for node in ast.iter_child_nodes(tree)
            if isinstance(node, ast.ClassDef) and node.name == "OrderFlowEngine"
        ),
        None,
    )
    assert of_engine is not None, (
        "OrderFlowEngine ClassDef missing from bot/order_flow.py "
        "(post-Bit-9.3.5 location)."
    )
    init = next(
        (
            n
            for n in of_engine.body
            if isinstance(n, ast.FunctionDef) and n.name == "__init__"
        ),
        None,
    )
    assert init is not None, "OrderFlowEngine.__init__ missing."
    init_src = ast.get_source_segment(src, init) or ""
    assert "Optional[DeribitDVOLFetcher]" not in init_src, (
        "OrderFlowEngine.__init__ now annotates a parameter as "
        "Optional[DeribitDVOLFetcher]. If intentional, update the "
        "`Bit 4.4 leaf extraction` breadcrumb in bot/_impl.py + "
        "bot/fetchers/__init__.py + agent_docs/bot_layout.md to mention "
        "OrderFlowEngine alongside VolatilityEngine."
    )
