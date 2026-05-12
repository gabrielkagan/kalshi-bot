"""Generate the public API snapshot for the bot package.

Run as::

    python3 scripts/audit/dump_public_api.py
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

# Bump recursion limit BEFORE any griffe machinery initializes (must happen
# pre-`import griffe` so any griffe-internal recursive caching honors it).
# griffe's expression iterator can recurse deeper than Python's default
# 1000-frame limit on complex annotations (e.g., nested Tuple/Union/Generic
# chains in extracted modules). 15K is empirically sufficient for the current
# bot package surface and low enough to avoid OS-level stack exhaustion on
# macOS. L94 workarounds remain in place at import sites that need them; this
# is an orthogonal bump for the iterator depth.
sys.setrecursionlimit(max(sys.getrecursionlimit(), 15000))

# Bit 11.2 (2026-05-12): relocated to scripts/audit/; 3-level dirname.
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import griffe  # noqa: E402

SNAPSHOT_PATH = PROJECT_ROOT / "tests" / "contracts" / "public_api.json"
PACKAGE_NAME = "bot"

# Submodules whose contents are intentionally NOT recursed into in the
# main static walk. Bit 9.3-iii.c (2026-05-11) DELETED bot/_impl.py — it
# is no longer in this list (the module doesn't exist; nothing to skip).
# bot._thread_env stays here because it's an OMP-pinning side-effect
# loader, not a public-API surface.
#
# Membership check is prefix-based (see _is_in_skipped).
SKIPPED_SUBMODULES = ("bot._thread_env",)

# Layer 2 (Bit 9.3-iii.c RETIRED) — historically auto-derived from
# ``griffe.load("bot._impl").classes`` to capture top-level public
# CLASSES inside bot/_impl.py. Bit 9.3-iii.c deleted bot/_impl.py, so
# there are no `_impl` canonical classes left to capture; the canonical
# homes of every extracted class are already covered by Layer 1 (griffe
# static walk of bot.* submodules). The _walk_impl_canonical_classes
# function is retired to a no-op (see below).


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
    """Layer 2 — RETIRED in Bit 9.3-iii.c (2026-05-11).

    Pre-retirement: captured signatures for every locally-defined public class
    in bot/_impl.py via griffe.load("bot._impl").classes. The shim provided a
    self-maintaining snapshot of the residual class surface during the
    multi-Bit modularization track.

    Post-retirement: bot/_impl.py was deleted, so griffe.load("bot._impl")
    raises ModuleNotFoundError. Every extracted class has a canonical home
    under bot.<canonical_module> and is covered by Layer 1 (the main static
    walk). This function is now a no-op kept only to preserve the
    public_api.json schema shape during the Sprint 9 milestone transition.
    The __impl_canonical_classes__ meta-key is intentionally absent from
    the regenerated snapshot.
    """
    return None  # no-op post-Bit-9.3-iii.c


def _probe_runtime_proxy_attrs(out: dict[str, Any]) -> None:
    """Layer 3 — RETIRED in Bit 9.3-iii.b (2026-05-11).

    Pre-retirement: snapshotted public names accessible via ``getattr(bot, name)``
    that the ``_BotProxy`` in ``bot/__init__.py`` forwarded to ``bot._impl``'s
    namespace (which included ``from bot.config import *`` and ``from bot.constants
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
    """Single-layer snapshot of bot's public API. Deterministic across runs.

    Layer 1: griffe static walk of bot.* submodules.
    Layer 2: (RETIRED Bit 9.3-iii.c, 2026-05-11) — bot/_impl.py was DELETED;
             the residual-class layer has no source to walk. _walk_impl_canonical_classes
             is a no-op.
    Layer 3: (RETIRED Bit 9.3-iii.b, 2026-05-11) — runtime proxy probe; the
             _BotProxy was retired so getattr(bot, name) no longer falls through.
             _probe_runtime_proxy_attrs is a no-op.
    """
    # Recursion limit bump moved to module-top (see comment near the
    # sys.setrecursionlimit call above the griffe import) — must run BEFORE
    # any griffe machinery initializes to avoid OS-level stack exhaustion.

    out: dict[str, Any] = {}

    package = griffe.load(PACKAGE_NAME)
    _walk(package, PACKAGE_NAME, out)

    _walk_impl_canonical_classes(out)  # no-op (Bit 9.3-iii.c)
    _probe_runtime_proxy_attrs(out)    # no-op (Bit 9.3-iii.b)

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
