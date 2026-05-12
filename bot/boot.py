"""Boot-time bindings that fire at production startup.

Sprint 9 Bit 9.3-iii.a (2026-05-11) — relocated from bot/_impl.py to break the
last structural dependency that bot/state.py and bot/main_loop.py had on
bot._impl. Clean leaf — imports only stdlib + bot.helpers.validators +
scripts/cal_mlp/integration. Zero references to bot._impl, bot.main_loop,
bot.state, bot.scanner, bot.executor, bot.settlement, bot.order_flow,
bot.orphan_db_watchdog.

## What lives here

Four module-level bindings that previously lived in bot/_impl.py at lines
347 / 358 / 359 / 380 plus the cal_mlp warmup boot log block (lines 416-432):

- `_HPSB_VALIDATOR_UNAVAILABLE_REASON: Optional[str]` — vestigial gate-state
  field per Bit 3.0.5 (registry-membership validator can't fail with
  FileNotFoundError; kept as None for the HPSB_GATE_STATE log consumer).
- `_HPSB_MISSING_BLEEDERS` — boot-time invocation of
  `_validate_high_price_stc_block_bleeder_strings()`.
- `_BLEED_BLOCK_MISSING_BLEEDERS` — boot-time invocation of
  `_validate_bleed_block_bleeder_strings()`.
- `compute_for_15m_main_path` — Phase 7 Edit 2 closure: static
  reimplementation of 15M main-path sizing for parity-assert (Bit 7.1 fu /
  Smell 4 makes this self-contained — closure imports its 11 dependent
  names directly from bot.constants + config). DO NOT use in production
  trading — only consumed by sizing_parity_assert at StateManager startup.
- cal_mlp warmup cache call + INFO boot log (`[CALMLP] enabled=N at boot,
  predictors_warmed=N/4`).

## Load-order guarantee

The production chain is `bot/__main__.py → bot._thread_env → bot.main_loop
→ bot.boot → integration → numpy/scipy/torch`. Because bot._thread_env is
bot/__main__.py's FIRST import (R-p7-deploy-r7), OMP_NUM_THREADS=1 is set
before any numerical C-extension caches its thread count. bot.boot's
`from integration import ...` reaches an environment where the pin is
already in place. See kb/failures/cal-mlp-torch-thread-contention-apr29.md
for the regression class this prevents.

## Timing semantics (post-Bit-9.3-iii.a)

cal_mlp warmup now fires at bot.boot module load, which happens during
bot.main_loop's import chain (top-level `from bot.boot import (...)`).
Pre-9.3-iii.a the warmup fired at bot._impl module load — which post-9.3-ii
was LAZY (triggered by first _BotProxy access). So 9.3-iii.a shifts warmup
EARLIER (from first proxy access to bot.main_loop module load), matching
the pre-9.3-ii semantics where bot._impl was top-imported by bot/__main__.py.
CALMLP_ENABLED env contract is preserved — `warmup_predictor_cache` itself
reads the env and short-circuits when disabled.

## Consumers

- bot/_impl.py — re-exports the 4 bindings via `from bot.boot import (...)`
  to preserve the existing `tests/contracts/public_api.json` proxy-attr
  surface byte-stable (the snapshot picks up the new bot.boot module +
  bot.boot.compute_for_15m_main_path + bot.state.compute_for_15m_main_path
  entries as intentional additions; no existing entries removed) until
  proxy retirement in Bit 9.3-iii.b/c.
- bot/main_loop.py — top-imports `_HPSB_MISSING_BLEEDERS` and
  `_HPSB_VALIDATOR_UNAVAILABLE_REASON` (replaces the Bit 9.3 method-body
  late-binding block at `MainLoop.__init__` lines 226-229).
- bot/state.py — top-imports `compute_for_15m_main_path` (replaces the
  Bit 7.1 `_get_compute_for_15m_main_path()` late-binding helper).

## bot.helpers.validators dependency

`_validate_high_price_stc_block_bleeder_strings` + `_validate_bleed_block_bleeder_strings`
live in `bot/helpers/validators.py` (Bit 3.2). Per the helpers-leaf
contract, bot.boot consuming bot.helpers.validators is the allowed
direction (helpers are leaf; consumers reach DOWN into helpers).
"""
import os
import sys

# R-p7-deploy-r7 CRITICAL — defense-in-depth: bot._thread_env MUST load BEFORE
# the `from integration import (...)` block below transitively loads
# numpy/scipy/torch; those C-extensions cache OpenBLAS thread count at load
# time, so OMP_NUM_THREADS=1 must be in os.environ before they import.
# Production-path chain bot/__main__.py → bot._thread_env → bot.main_loop →
# bot.boot ALREADY fires bot._thread_env first (via bot/__main__.py line 25);
# this defense-in-depth covers any code path that imports bot.boot directly
# (e.g., test-suite imports without going through bot/__main__.py). Postmortem:
# kb/failures/cal-mlp-torch-thread-contention-apr29.md.
#
# **Form**: `__import__()` runtime call rather than `import bot._thread_env`
# statement. griffe's static expression walker hits a RecursionError when it
# traverses a top-level `import bot._thread_env` inside a public bot.* module
# (true infinite alias-recursion in griffe's Alias.canonical_path chain — not
# a depth issue; bumping sys.setrecursionlimit doesn't help). The runtime
# `__import__()` form bypasses griffe's static walk while preserving the
# identical side-effect (OMP_NUM_THREADS=1 set before numpy loads). AST regression:
# tests/integration/test_cal_mlp_invariants.py::test_thread_env_imported_before_numerical_libs_in_bot_boot.
__import__("bot._thread_env")  # noqa: E402

import logging  # noqa: E402
from typing import Optional  # noqa: E402

# scripts/cal_mlp/ on sys.path so the bare `from integration import` resolves
# (mirrors bot/_impl.py:15 + bot/main_loop.py path manipulation). Placed AFTER
# bot._thread_env so OMP_NUM_THREADS=1 is set before integration.py later
# loads numpy/scipy/torch transitively.
sys.path.insert(
    0,
    os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "scripts",
        "cal_mlp",
    ),
)

from integration import (  # noqa: E402
    make_compute_for_15m_main_path,
    warmup_predictor_cache as _calmlp_warmup_cache,
)
from bot.helpers.validators import (  # noqa: E402
    _validate_high_price_stc_block_bleeder_strings,
    _validate_bleed_block_bleeder_strings,
)


# ─── Bleeder-validator boot-time bindings (Bit 3.0.5) ────────────────────────
# Vestigial post-Bit-3.0.5: registry-membership validator can't fail with a
# FileNotFoundError (no filesystem read). Kept as None for the HPSB_GATE_STATE
# log consumer ("validator_unavailable=no" output at MainLoop.__init__).
_HPSB_VALIDATOR_UNAVAILABLE_REASON: Optional[str] = None

# Runtime-registry membership check for HPSB bleeders.
_HPSB_MISSING_BLEEDERS = _validate_high_price_stc_block_bleeder_strings()

# Same for the BLEED_BLOCK family (Bit 3.0.5 sister binding).
_BLEED_BLOCK_MISSING_BLEEDERS = _validate_bleed_block_bleeder_strings()


# ─── Phase 7 Edit 2: 15M main-path sizing closure (parity-assert only) ───────
# DO NOT use in production trading — only consumed by sizing_parity_assert at
# startup. Bit 7.1 fu (Smell 4, ticket 86b9vhccw) made the closure
# self-contained: it imports its 11 dependent names directly from
# bot.constants + config inside its own body (the call-time imports below
# the `make_compute_for_15m_main_path()` definition in
# scripts/cal_mlp/integration.py). bot.boot just invokes it — no namespace
# capture, no globals() arg.
compute_for_15m_main_path = make_compute_for_15m_main_path()


# ─── cal_mlp warmup cache + boot log (R-p7-deploy-r9) ────────────────────────
# Predictor INSTANCES always constructed at integration.py module-import time
# (regardless of CALMLP_ENABLED). The `.warmup()` orchestration is gated on
# the env var — `warmup_predictor_cache` reads CALMLP_ENABLED and short-circuits
# warmup when 0. Per-call env check inside `annotate_evaluation_kwargs` and
# `annotate_evaluation_async_enqueue` ensures `predict()` never runs when env=0
# even if the cache IS warmed (kill-switch contract: R-p7-cleanroom#H2 +
# R-p7-coldboot#C-S2).
#
# Module-scoped logger (NOT bare `logging.info`) — pinned by
# tests/regression/test_no_basicconfig_in_bot_impl.py (similar test would
# exist for bot.boot).
_calmlp_enabled_at_boot, _calmlp_warmed = _calmlp_warmup_cache()
if _calmlp_enabled_at_boot:
    logging.getLogger(__name__).info(
        "[CALMLP] enabled=1 at boot, predictors_warmed=%d/4", _calmlp_warmed
    )
else:
    logging.getLogger(__name__).info(
        "[CALMLP] enabled=0 at boot — predictors constructed but not warmed; "
        "hot env flip to 1 will lazy-load on first scan tick"
    )
