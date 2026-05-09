"""Generate the public API snapshot for the bot package.

Run as::

    python3 scripts/dump_public_api.py
    # or: make api-snapshot-regen

Writes ``tests/contracts/public_api.json``. Consumed by
``tests/contracts/test_public_api_snapshot.py``.

What this captures (three layers):

1. **Static walk of public submodules under ``bot.*``** (e.g. ``bot.engines.*``,
   ``bot.feeds.*``, ``bot.helpers.*``, ``bot.constants``). Records every
   public name with its full signature (parameters + returns for functions,
   bases + public methods for classes, annotation for attributes).

2. **Static walk of top-level public CLASSES inside ``bot/_impl.py``**
   (``MainLoop``, ``OpportunityScanner``, ``OrderExecutor``, ``StateManager``,
   ``CalibrationEngine``, ``OrderFlowEngine``, ``KalshiOrderFlowTracker``,
   ``SettlementTracker``, ``KalshiClient``, ``Logger``, etc. — the canonical,
   load-bearing classes still resident in the legacy monolith). The rest of
   ``bot/_impl.py`` (constants, helpers, residual functions) is skipped to
   keep the snapshot focused.

3. **Runtime proxy probe** — imports ``bot`` and ``bot._impl`` at runtime,
   enumerates every public name accessible via ``getattr(bot, name)`` (the
   ``_BotProxy`` exposes ``bot._impl``'s namespace including ``from config
   import *`` and ``from bot.constants import *`` resolutions). Captures
   what griffe (static) cannot see: dynamic attribute access. A removed
   star-import or a renamed proxy attribute surfaces here as a removed
   list entry.

What this DOES NOT cover (deliberate scope):
- ``bot/_impl.py`` source content patterns (validator-binding lines, no-leaked-
  funcdef guards). These remain the job of the per-Bit ``test_*_extraction.py``
  files; the snapshot is *additive*, not a replacement.
- Runtime identity (``bot.X is bot.engines.foo.X``) — covered by the existing
  identity tests in test_*_extraction.py files.
- Behavioral correctness — covered by unit/integration tests.

Determinism: every dict is sorted by key; every list is sorted; griffe is
pinned tightly (1.14.x) in pyproject.toml dev extras to prevent
cross-version skew. Output is byte-identical across runs given identical
source + identical griffe minor version.

Pillar 1 of the testing-foundation-sprint. See parent ticket 86b9ve0wa
and child 86b9ve0xt.
"""

from __future__ import annotations

import inspect
import json
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import griffe  # noqa: E402

SNAPSHOT_PATH = PROJECT_ROOT / "tests" / "contracts" / "public_api.json"
PACKAGE_NAME = "bot"

# Submodules whose contents are intentionally NOT recursed into in the
# main static walk. Top-level public CLASSES inside bot._impl are still
# captured (see _walk_impl_canonical_classes) — those ARE the public
# surface today, until extraction moves them out.
#
# Membership check is prefix-based (see _is_in_skipped) — `bot._impl` and
# `bot._impl.OrderFlowEngine` both match.
SKIPPED_SUBMODULES = ("bot._impl", "bot._thread_env")

# These public top-level CLASSES are still resident in bot/_impl.py as of
# Sprint 6 in progress. Until extraction completes, they're load-bearing
# public surface (called from bot/__main__.py, mocked in ~94 test sites).
# The snapshot pins their signatures here even though we skip the rest
# of bot._impl.
#
# When a class extracts (e.g. Bit 6.3 moves CalibrationEngine to
# bot/engines/calibration.py), remove it from this list — it'll then
# show up under its new canonical path via the main static walk.
IMPL_CANONICAL_CLASSES = (
    "MainLoop",
    "OpportunityScanner",
    "OrderExecutor",
    "StateManager",
    "CalibrationEngine",
    "OrderFlowEngine",
    "KalshiOrderFlowTracker",
    "SettlementTracker",
)


def _is_in_skipped(path: str | None) -> bool:
    """Prefix-match against SKIPPED_SUBMODULES.

    Catches both `bot._impl` (the module) and `bot._impl.OrderFlowEngine`
    (an attribute path). Plain `in` would only match the exact string.
    """
    if not path:
        return False
    for skipped in SKIPPED_SUBMODULES:
        if path == skipped or path.startswith(skipped + "."):
            return True
    return False


def _is_public_name(name: str) -> bool:
    return not name.startswith("_")


def _stringify(expr: Any) -> str | None:
    if expr is None:
        return None
    return str(expr)


def _signature_parameter(param: Any) -> dict[str, Any]:
    return {
        "name": param.name,
        "kind": str(param.kind.value) if param.kind else None,
        "annotation": _stringify(param.annotation),
        "default": _stringify(param.default),
    }


def _signature_function(fn: Any) -> dict[str, Any]:
    return {
        "kind": "function",
        "parameters": [_signature_parameter(p) for p in fn.parameters],
        "returns": _stringify(fn.returns),
    }


def _signature_attribute(attr: Any) -> dict[str, Any]:
    return {
        "kind": "attribute",
        "annotation": _stringify(attr.annotation),
    }


def _signature_class(cls: Any) -> dict[str, Any]:
    members: dict[str, Any] = {}
    for name, member in sorted(cls.members.items()):
        if not _is_public_name(name) and name != "__init__":
            continue
        target = _resolve(member)
        if target is None:
            continue
        if target.is_function:
            members[name] = _signature_function(target)
        elif target.is_attribute:
            members[name] = _signature_attribute(target)
    return {
        "kind": "class",
        "bases": sorted(_stringify(b) or "" for b in cls.bases),
        "members": members,
    }


def _resolve(member: Any) -> Any | None:
    """Resolve aliases to their final target. Returns None if unresolvable."""
    if not getattr(member, "is_alias", False):
        return member
    try:
        return member.final_target
    except (griffe.AliasResolutionError, griffe.CyclicAliasError):
        return None


def _is_external_alias(member: Any, target: Any) -> bool:
    """Alias whose target lives outside the bot.* namespace.

    These are stdlib / third-party re-imports (e.g. ``import requests`` in
    bot/notifier.py creates an alias ``bot.notifier.requests`` → ``requests``).
    They're implementation detail, not public contract — filtering them
    drops ~170 noise entries and prevents internal-import refactors from
    triggering snapshot regeneration.
    """
    if not getattr(member, "is_alias", False):
        return False
    target_path = getattr(target, "path", None) or str(getattr(member, "target_path", ""))
    return not target_path.startswith("bot.") and target_path != "bot"


def _walk(module: Any, qualname: str, out: dict[str, Any]) -> None:
    if _is_in_skipped(qualname):
        return
    out[qualname] = {"kind": "module"}
    for name, member in sorted(module.members.items()):
        if not _is_public_name(name):
            continue
        full = f"{qualname}.{name}"

        if getattr(member, "is_alias", False):
            # Check target path BEFORE resolving — stdlib aliases (e.g.
            # ``from typing import Dict``) raise AliasResolutionError on
            # ``final_target`` access because typing/os/etc. aren't griffe-
            # loaded. The unresolved-alias fallback would then record them
            # as snapshot entries — pollution. Filter on the static path
            # string up front.
            target_path_str = str(getattr(member, "target_path", ""))
            if not target_path_str.startswith("bot.") and target_path_str != "bot":
                continue
            target = _resolve(member)
            if target is None:
                out[full] = {
                    "kind": "unresolved-alias",
                    "target_path": target_path_str,
                }
                continue
        else:
            target = member

        if _is_in_skipped(getattr(target, "path", None)):
            continue

        if target.is_module:
            _walk(target, full, out)
        elif target.is_class:
            out[full] = _signature_class(target)
        elif target.is_function:
            out[full] = _signature_function(target)
        elif target.is_attribute:
            out[full] = _signature_attribute(target)


def _walk_impl_canonical_classes(out: dict[str, Any]) -> None:
    """Capture signatures for the load-bearing classes still in bot/_impl.py.

    These are NOT covered by ``_walk`` (which skips bot._impl) but ARE the
    public surface today — Bit-6.3+ extractions will move them out one by
    one, and IMPL_CANONICAL_CLASSES tracks the moving boundary.
    """
    impl = griffe.load("bot._impl")
    classes: dict[str, Any] = {}
    for name in IMPL_CANONICAL_CLASSES:
        member = impl.members.get(name)
        if member is None:
            classes[name] = {"kind": "MISSING — class no longer in bot._impl"}
            continue
        target = _resolve(member)
        if target is None or not target.is_class:
            classes[name] = {"kind": f"unexpected: {type(member).__name__}"}
            continue
        classes[name] = _signature_class(target)
    out["__impl_canonical_classes__"] = classes


def _probe_runtime_proxy_attrs(out: dict[str, Any]) -> None:
    """Snapshot the set of public names accessible via ``getattr(bot, name)``.

    The ``_BotProxy`` in ``bot/__init__.py`` forwards attribute access to
    ``bot._impl``'s namespace (which includes ``from config import *`` and
    ``from bot.constants import *`` star-imports). griffe (static) cannot
    see this dynamic resolution; this runtime probe does.

    Captures the failure mode: an extraction Bit silently drops a star-import
    in bot/_impl.py, breaking ~94 ``mock.patch("bot.X")`` sites — the affected
    names disappear from this list, snapshot diffs, CI fails.
    """
    # Importing bot triggers heavy initialization (numpy, cryptography, etc.)
    # via bot._impl — that's the cost of running this probe. Acceptable for
    # a CI gate. ~10-20s on Mac.
    import bot  # noqa: F401  (triggers proxy setup)
    import bot._impl as impl

    # Candidate names: everything public in bot._impl that's not a module
    # import. We want classes, functions, attributes — including everything
    # surfaced by `from config import *` and `from bot.constants import *`.
    candidates = []
    for name in dir(impl):
        if name.startswith("_"):
            continue
        try:
            value = getattr(impl, name)
        except AttributeError:
            continue
        # Skip bare module re-imports (`import os`, `import requests`).
        # These are implementation detail; if they're the names @patch
        # relies on, the test patches into the module not the proxy.
        if isinstance(value, ModuleType):
            continue
        candidates.append(name)

    # Verify proxy access actually works for each candidate. Anything that
    # errors here is a proxy bug worth surfacing.
    accessible: list[str] = []
    inaccessible: list[dict[str, str]] = []
    for name in sorted(candidates):
        try:
            getattr(bot, name)
            accessible.append(name)
        except Exception as exc:  # noqa: BLE001
            inaccessible.append({"name": name, "error": f"{type(exc).__name__}: {exc}"})

    out["__bot_proxy_attrs__"] = sorted(accessible)
    if inaccessible:
        out["__bot_proxy_inaccessible__"] = sorted(
            inaccessible, key=lambda d: d["name"]
        )


def dump_bot_public_api() -> dict[str, Any]:
    """Three-layer snapshot of bot's public API. Deterministic across runs.

    Layer 1: griffe static walk of bot.* submodules (excluding bot._impl).
    Layer 2: griffe static walk of bot._impl canonical classes.
    Layer 3: runtime probe of bot.X proxy-accessible attrs.
    """
    out: dict[str, Any] = {}

    package = griffe.load(PACKAGE_NAME)
    _walk(package, PACKAGE_NAME, out)

    _walk_impl_canonical_classes(out)
    _probe_runtime_proxy_attrs(out)

    return out


def main() -> None:
    snapshot = dump_bot_public_api()
    SNAPSHOT_PATH.parent.mkdir(parents=True, exist_ok=True)
    SNAPSHOT_PATH.write_text(
        json.dumps(snapshot, indent=2, sort_keys=True) + "\n"
    )
    static_entries = sum(
        1 for k in snapshot if not k.startswith("__")
    )
    proxy_attrs = len(snapshot.get("__bot_proxy_attrs__", []))
    canonical = len(snapshot.get("__impl_canonical_classes__", {}))
    print(f"Wrote snapshot: {SNAPSHOT_PATH}")
    print(f"  Static entries: {static_entries}")
    print(f"  _impl canonical classes: {canonical}")
    print(f"  Runtime proxy attrs: {proxy_attrs}")


if __name__ == "__main__":
    main()
