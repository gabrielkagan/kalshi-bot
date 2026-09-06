"""D2.5 — ``collector/coinbase_main_loop.py`` orchestrator shape contract.

Ticket `86b9znq4w` (2026-05-18). Pins the structural shape of the
NEW Coinbase-side main-loop module that the
``kalshi-coinbase-collector`` systemd unit invokes as
``python3 -m collector.coinbase_main_loop``.

Mirrors ``collector/main_loop.py`` (Kalshi side, D1.2-D1.4) for the
Coinbase deltas:
  - **Single-conn.** Coinbase Exchange WS is single-connection by
    design (D2.2). No SubscriptionManager / no per-conn fan-out / no
    ConnPlan iteration.
  - **No REST refresher.** Coinbase product_ids are static at the
    repo's ``coinbase_wire.ws_client.DEFAULT_PRODUCT_IDS`` constant
    (BTC/ETH/SOL/XRP/HYPE/DOGE/BNB); changes are repo-commit events,
    NOT hourly REST polls. No RestSnapshotRefresher wiring.
  - **5-channel default.** Post-D2.5 R0 reachability spike confirms
    level2_batch is publicly subscribable; default channel set is
    ``ticker`` + ``matches`` + ``heartbeat`` + ``status`` +
    ``level2_batch`` per the bundled promotion.
  - **No PEM / no RSA-PSS auth.** Public channels only at D2.5; no
    load_private_key call, no eager-PEM-load posture.
  - **Same drain thread + uploader pattern** as Kalshi side; single
    BronzeWriter per channel (no per-conn dimension at single-conn).

L99 PARANOID lesson: contract tests pin the shape so a future Bit
that loosens an isolation invariant (e.g., importing bot.* into the
loop, or removing the SIGINT handler) fires at the contract gate
rather than at adversarial review (or production).
"""
from __future__ import annotations

import ast
import inspect
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
MODULE_FILE = REPO_ROOT / "collector" / "coinbase_main_loop.py"


def _module_source() -> str:
    assert MODULE_FILE.exists(), (
        f"{MODULE_FILE.relative_to(REPO_ROOT)} missing — D2.5 ships "
        "this module as the orchestrator that the kalshi-coinbase-"
        "collector systemd unit invokes via `python3 -m "
        "collector.coinbase_main_loop`."
    )
    return MODULE_FILE.read_text()


def _module_ast() -> ast.Module:
    return ast.parse(_module_source())


def test_module_imports_only_from_collector_coinbase_wire_and_stdlib():
    """No ``bot.*`` imports — pinned structurally by the
    ``collector-no-bot`` import-linter contract (Contract 7,
    .importlinter:170). This test is the AST defense-in-depth so a
    future refactor catches the regression at the contract tier
    BEFORE lint-imports runs.

    Allowed deps: ``coinbase_wire``, ``collector.*`` siblings,
    stdlib, ``zstandard`` (transitive via ``collector.writer``).
    """
    tree = _module_ast()
    forbidden_prefixes = ("bot.", "bot ")
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert not alias.name.startswith(forbidden_prefixes), (
                    f"`import {alias.name}` forbidden — D0.3 §10 "
                    f"bot-isolation contract: collector.* must not "
                    f"reach into bot/. (pinned by import-linter "
                    f"contract `collector-no-bot`)"
                )
        elif isinstance(node, ast.ImportFrom):
            assert node.module is not None
            assert not node.module.startswith("bot."), (
                f"`from {node.module} import …` forbidden — D0.3 "
                f"bot-isolation contract."
            )
            assert node.module != "bot", (
                "`from bot import …` forbidden — D0.3 bot-isolation."
            )


def test_module_has_run_function():
    """The systemd entrypoint shim ``__main__`` block must call
    ``run()`` at module load. ``run()`` is the load-bearing public API.
    """
    tree = _module_ast()
    has_run = any(
        isinstance(node, ast.FunctionDef) and node.name == "run"
        for node in ast.iter_child_nodes(tree)
    )
    assert has_run, (
        "collector.coinbase_main_loop must define a top-level `run()` "
        "function — the systemd unit entrypoint via `python3 -m "
        "collector.coinbase_main_loop` (with `if __name__ == \"__main__\": "
        "run()` at module bottom)."
    )


def test_module_has_main_block():
    """``if __name__ == "__main__": run()`` block at module bottom.

    Mirrors ``collector/__main__.py`` but inline-in-loop rather than
    via a separate ``__main__.py`` because we invoke this module
    directly (``python3 -m collector.coinbase_main_loop``), not the
    package.
    """
    source = _module_source()
    assert 'if __name__ == "__main__"' in source, (
        "Module missing `if __name__ == \"__main__\":` block — without "
        "it, `python3 -m collector.coinbase_main_loop` would import "
        "the module but never invoke run()."
    )


def test_run_reads_required_env_vars():
    """``run()`` must read the 4 D2.5-required env vars via
    ``os.environ.get`` (with defaults) or explicit lookups:

      - COINBASE_BRONZE_ROOT
      - COINBASE_HEALTH_SIDECAR_PATH (optional, defaults under bronze root)
      - RCLONE_REMOTE (shared convention with Kalshi side)
      - S3_BUCKET (shared convention with Kalshi side)

    Static AST scan — checks the literal env-var names appear in the
    module source. Stricter than a runtime call assertion (which
    would need a mock-environ harness).
    """
    source = _module_source()
    required_env_vars = [
        "COINBASE_BRONZE_ROOT",
        "RCLONE_REMOTE",
        "S3_BUCKET",
    ]
    for var_name in required_env_vars:
        assert var_name in source, (
            f"D2.5 env var {var_name!r} not referenced in "
            f"collector/coinbase_main_loop.py. Required for the .env."
            f"coinbase-collector wiring."
        )


def test_run_wires_coinbase_archiver():
    """The orchestrator must instantiate ``CoinbaseArchiver`` from
    ``collector.coinbase_archiver`` — the D2.2 ship is the consumer
    that D2.5 wires to a process.
    """
    source = _module_source()
    assert "CoinbaseArchiver" in source, (
        "collector.coinbase_main_loop must instantiate "
        "CoinbaseArchiver. Without this wiring, the systemd unit "
        "would boot a no-op process and no bronze would flow."
    )
    assert "from collector.coinbase_archiver" in source, (
        "CoinbaseArchiver import must come from "
        "collector.coinbase_archiver (the D2.2 module)."
    )


def test_run_uses_bronze_writer():
    """Per-channel BronzeWriter allocation. The orchestrator builds
    writers and hands them to the archiver via ``writers_by_channel``."""
    source = _module_source()
    assert "BronzeWriter" in source, (
        "collector.coinbase_main_loop must allocate BronzeWriter "
        "instances per channel. The CoinbaseArchiver dispatches to "
        "these writers via the writers_by_channel dict."
    )
    assert "writers_by_channel" in source, (
        "CoinbaseArchiver constructor takes writers_by_channel as a "
        "keyword arg — pinned by AST contract on the archiver itself."
    )


def test_collector_uses_own_11_product_list_decoupled_from_wire_default():
    """The COLLECTOR collects 11 assets; the bot trades 7.

    Added 2026-05-30: ADA + BCH; 2026-09-05 (ticket 86bbvdc8y): NEAR + ZEC —
    Kalshi 15M series the bot does NOT trade (KXNEAR15M / KXZEC15M live since
    2026-06-30; NEAR-USD + ZEC-USD verified online on Coinbase Exchange
    2026-09-05). The SHARED ``coinbase_wire.ws_client.DEFAULT_PRODUCT_IDS`` is
    consumed by BOTH ``bot/feeds/coinbase.py`` AND this collector, so it
    stays bot-aligned at 7. The collector defines its OWN 11-product list
    (``COLLECTOR_PRODUCT_IDS``) and passes it EXPLICITLY to CoinbaseArchiver
    — decoupling collection from live trading without a ``bot.*`` import.
    """
    from coinbase_wire.ws_client import DEFAULT_PRODUCT_IDS
    from collector.coinbase_main_loop import COLLECTOR_PRODUCT_IDS

    # Wire default stays bot-aligned at 7 (decouple guarantee).
    assert len(DEFAULT_PRODUCT_IDS) == 7
    for corpus_only in ("ADA-USD", "BCH-USD", "NEAR-USD", "ZEC-USD"):
        assert corpus_only not in DEFAULT_PRODUCT_IDS

    # Collector list = the 7 + ADA + BCH + NEAR + ZEC (corpus-max, 11 assets).
    assert set(COLLECTOR_PRODUCT_IDS) == {
        "BTC-USD", "ETH-USD", "SOL-USD", "XRP-USD",
        "HYPE-USD", "DOGE-USD", "BNB-USD", "ADA-USD", "BCH-USD",
        "NEAR-USD", "ZEC-USD",
    }
    # Superset of the wire default — never silently drops a bot product.
    assert set(DEFAULT_PRODUCT_IDS).issubset(set(COLLECTOR_PRODUCT_IDS))

    # The orchestrator wires the collector list, NOT the wire default.
    source = _module_source()
    assert "product_ids=COLLECTOR_PRODUCT_IDS" in source, (
        "run() must pass product_ids=COLLECTOR_PRODUCT_IDS to "
        "CoinbaseArchiver — the 11-asset collector list, not the "
        "bot-aligned 7-product wire DEFAULT_PRODUCT_IDS."
    )


def test_run_uses_rclone_uploader():
    """The orchestrator must wire ``RcloneUploader`` for S3 upload of
    rotated chunks. Same pattern as the Kalshi side."""
    source = _module_source()
    assert "RcloneUploader" in source, (
        "collector.coinbase_main_loop must instantiate RcloneUploader. "
        "Without this, rotated bronze chunks would accumulate locally "
        "until the disk fills (the 384M MemoryMax + bounded /var/lib "
        "would crash the unit)."
    )


def test_run_installs_sigterm_handler():
    """SIGTERM handler so ``systemctl stop kalshi-coinbase-collector``
    triggers graceful shutdown (drain + close + final sweep) rather
    than abrupt-kill.

    Same posture as collector.main_loop.run (D1.2 ship pattern):
    install signal handler that sets a shutdown_event the main thread
    waits on.
    """
    source = _module_source()
    assert "SIGTERM" in source, (
        "collector.coinbase_main_loop must install a SIGTERM handler "
        "for graceful shutdown. Without it, systemctl stop would "
        "deliver SIGTERM with no handler → Python default → abrupt "
        "exit → possible torn writes."
    )
    assert "SIGINT" in source, (
        "collector.coinbase_main_loop must install a SIGINT handler "
        "for Ctrl-C development workflows (same pattern as "
        "collector.main_loop)."
    )


def test_run_does_not_import_rest_snapshot():
    """No RestSnapshotRefresher wiring — Coinbase product_ids are
    static at the ``coinbase_wire.ws_client.DEFAULT_PRODUCT_IDS``
    constant. Product additions are repo-commit events, not hourly
    REST polls (the bot's ``COINBASE_PRODUCTS`` constant similarly
    requires a commit + deploy).

    AST scan over actual import statements so a doc-string narrative
    that NEGATES the import ("No RestSnapshotRefresher wiring") does
    not self-trigger the regression.
    """
    tree = _module_ast()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.module and "rest_snapshot" in node.module:
                imported = ", ".join(alias.name for alias in node.names)
                assert False, (
                    f"`from {node.module} import {imported}` forbidden — "
                    f"Coinbase product_ids are static; no hourly REST "
                    f"refresh on the Coinbase side."
                )
            if node.module is not None:
                for alias in node.names:
                    assert alias.name != "RestSnapshotRefresher", (
                        f"`from {node.module} import RestSnapshotRefresher` "
                        f"forbidden — D2.5 explicitly omits Coinbase REST "
                        f"refresh."
                    )
        elif isinstance(node, ast.Import):
            for alias in node.names:
                assert "rest_snapshot" not in alias.name, (
                    f"`import {alias.name}` forbidden — Coinbase product_ids "
                    f"are static; no REST refresh."
                )


def test_run_does_not_load_private_key():
    """No PEM loading — D2.5 ships public channels only (per D2.1.5
    auth scope). The presence of load_private_key would imply HMAC
    private-channel work was bundled here without updating the
    auth.py stubs.
    """
    source = _module_source()
    assert "load_private_key" not in source, (
        "collector.coinbase_main_loop must NOT load a private key. "
        "D2.5 ships public channels only; HMAC private-channel "
        "support is deferred to a future Bit (with corresponding "
        "auth.py body landing)."
    )


def test_module_writes_health_sidecar():
    """The orchestrator must write a bronze_health.json sidecar for
    the cron-driven ``scripts/ops/collector_health_monitor.py`` to
    poll. Schema v1 (same as Kalshi); separate file per Option B
    isolation (defaults to ``COINBASE_HEALTH_SIDECAR_PATH`` env →
    ``/var/lib/kalshi-coinbase-collector/bronze_health.json``).
    """
    source = _module_source()
    assert "bronze_health" in source or "COINBASE_HEALTH_SIDECAR_PATH" in source, (
        "collector.coinbase_main_loop must write the bronze_health.json "
        "sidecar (positive observability for D1.6 fu monitor + D2.5 "
        "coinbase parity). Either reference the env var "
        "COINBASE_HEALTH_SIDECAR_PATH or the bronze_health filename."
    )


def test_run_signature_includes_shutdown_event():
    """``run()`` must accept ``shutdown_event`` kwarg for testability
    — caller can drive shutdown from a test harness without
    signal-handler pollution. Same shape as
    ``collector.main_loop.run``.
    """
    # Import lazily so the test discovers the file's existence before
    # exercising the signature.
    import importlib
    spec = importlib.util.find_spec("collector.coinbase_main_loop")
    assert spec is not None, (
        "collector.coinbase_main_loop spec not findable — "
        "module-create check should have failed first."
    )
    mod = importlib.import_module("collector.coinbase_main_loop")
    sig = inspect.signature(mod.run)
    assert "shutdown_event" in sig.parameters, (
        "run() signature missing `shutdown_event` kwarg — tests cannot "
        "drive shutdown without polluting signal handlers."
    )
