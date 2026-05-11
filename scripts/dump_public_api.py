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

2. **Static walk of locally-defined public classes inside ``bot/_impl.py``** —
   auto-derived from ``griffe.load("bot._impl").classes`` filtered to
   ``is_alias=False``. Post-Bit-9.3.5 (2026-05-10), this list is EMPTY —
   Sprint 9 closing sister leaf moved the final two classes
   (``OrderFlowEngine`` + ``KalshiOrderFlowTracker``) to ``bot/order_flow.py``,
   joining all prior extractions (``MainLoop`` → bot.main_loop in Bit 9.3,
   ``SettlementTracker`` → bot.settlement in Bit 9.2, ``OrderExecutor`` →
   bot.executor in Bit 9.1, ``OpportunityScanner`` → bot.scanner in Bit 8.1,
   ``StateManager`` → bot.state in Bit 7.1, ``CalibrationEngine`` →
   bot.engines.calibration in Bit 6.3, ``ProbabilityEngine`` → bot.engines.probability
   in Bit 6.2, ``VolatilityEngine`` → bot.engines.volatility in Bit 6.1,
   plus all Sprint 4 leaf classes). All re-exports back into bot._impl
   have ``is_alias=True`` and are filtered out — they appear under their
   canonical paths in Layer 1. Auto-derive is self-maintaining:
   extracted classes naturally drop out of Layer 2; new classes added to
   ``_impl.py`` (none anticipated post-Sprint-9) would naturally appear.

3. **Runtime proxy probe** — RETIRED in Bit 9.3-iii.b (2026-05-11).
   Pre-retirement: imported ``bot`` and ``bot._impl`` at runtime,
   enumerated every public name accessible via ``getattr(bot, name)`` (the
   ``_BotProxy`` exposed ``bot._impl``'s namespace including ``from config
   import *`` and ``from bot.constants import *`` resolutions). Captured
   what griffe (static) couldn't see: dynamic attribute access. Post-retirement
   the proxy is gone — ``getattr(bot, name)`` no longer falls through, and
   Layer 1 (griffe static walk of bot.* submodules) already covers every
   name's canonical home. ``_probe_runtime_proxy_attrs`` is now a no-op; the
   ``__bot_proxy_attrs__`` key is absent from regenerated snapshots.

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

# Layer 2 (canonical _impl classes) is auto-derived from
# ``griffe.load("bot._impl").classes`` rather than a hardcoded allowlist.
# Rationale: a hardcoded list creates a "drift laundering" loophole —
# extracting CalibrationEngine but forgetting to update the list would
# leave a MISSING sentinel that ``make api-snapshot-regen`` silently
# commits, masking the regression. Auto-derive is self-maintaining:
# extracted classes disappear naturally; new public classes appear
# automatically. (R2-M1 fix.)


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
    """Capture signatures for every locally-defined public class in bot/_impl.py.

    These are NOT covered by ``_walk`` (which skips bot._impl) but ARE the
    public surface today. Auto-derived from griffe's parse so the list is
    self-maintaining: extracted classes (Bit 6.3+ moves) naturally
    disappear; new classes added to _impl naturally appear.

    Uses ``impl.classes`` (locally defined) rather than ``impl.members``
    (which would include aliased imports like ``from bot.engines import
    VolatilityEngine``).
    """
    impl = griffe.load("bot._impl")
    classes: dict[str, Any] = {}
    for name, cls in sorted(impl.classes.items()):
        if not _is_public_name(name):
            continue
        # Filter out aliased re-export shims: when an extraction Bit moves
        # a class out of _impl.py and adds ``from bot.engines.foo import X``
        # as backward-compat, griffe surfaces it in impl.classes with
        # is_alias=True. We only want classes still LOCALLY defined here.
        # Already-extracted classes (Logger, KalshiClient, VolatilityEngine,
        # etc.) appear in Layer 1 under their canonical paths.
        if getattr(cls, "is_alias", False):
            continue
        classes[name] = _signature_class(cls)
    out["__impl_canonical_classes__"] = classes


def _probe_runtime_proxy_attrs(out: dict[str, Any]) -> None:
    """Layer 3 — RETIRED in Bit 9.3-iii.b (2026-05-11).

    Pre-retirement: snapshotted public names accessible via ``getattr(bot, name)``
    that the ``_BotProxy`` in ``bot/__init__.py`` forwarded to ``bot._impl``'s
    namespace (which included ``from config import *`` and ``from bot.constants
    import *`` star-imports). griffe (static) couldn't see that dynamic resolution;
    the runtime probe could.

    Post-retirement: the proxy is gone, ``getattr(bot, name)`` no longer falls
    through, and a probe would either (a) produce ``__bot_proxy_attrs__: []`` +
    a 615-entry ``__bot_proxy_inaccessible__`` dict of stale AttributeErrors, or
    (b) need to walk a different surface entirely. Neither adds value — Layer 1
    (griffe static walk of bot.* submodules) already covers every name's canonical
    home. This function is now a no-op kept only to preserve the public_api.json
    schema shape during the transition to Bit 9.3-iii.c (DELETE bot/_impl.py).
    """
    return None  # no-op post-Bit-9.3-iii.b


def dump_bot_public_api() -> dict[str, Any]:
    """Two-layer snapshot of bot's public API. Deterministic across runs.

    Layer 1: griffe static walk of bot.* submodules (excluding bot._impl).
    Layer 2: griffe static walk of bot._impl canonical classes.
    Layer 3: (RETIRED Bit 9.3-iii.b) — runtime proxy probe — no longer applicable
             post-proxy-retirement. _probe_runtime_proxy_attrs is a no-op until
             Bit 9.3-iii.c deletes bot/_impl.py + this scaffolding entirely.
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
