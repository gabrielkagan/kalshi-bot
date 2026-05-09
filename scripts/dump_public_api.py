"""Generate the public API snapshot for the bot package.

Run as::

    python3 scripts/dump_public_api.py

Writes ``tests/contracts/public_api.json``. Consumed by
``tests/contracts/test_public_api_snapshot.py``.

Design:
- Walks every public (non-underscore) submodule under ``bot.*``.
- Skips ``bot._impl`` and ``bot._thread_env`` — implementation detail, not
  part of the public contract. Symbols re-exported from those modules via
  ``bot/__init__.py`` are still captured as alias entries under ``bot.<name>``
  with their resolved signatures, so a lost re-export surfaces as a removed
  snapshot entry.
- For each public name, records: kind, full signature (parameters + returns
  for functions; bases + public methods for classes; annotation for
  attributes). Aliases resolve to their target's signature, but are keyed
  under the alias path.

Stable ordering: every dict is sorted by key; every list is sorted. Output
is deterministic across runs given identical source.

Pillar 1 of the testing-foundation-sprint. See parent ticket 86b9ve0wa
and child 86b9ve0xt.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import griffe  # noqa: E402

SNAPSHOT_PATH = PROJECT_ROOT / "tests" / "contracts" / "public_api.json"
PACKAGE_NAME = "bot"

# Submodules whose contents are intentionally NOT recursed into.
# Reachable public symbols are still captured as alias entries when
# re-exported via bot/__init__.py.
SKIPPED_SUBMODULES = frozenset({"bot._impl", "bot._thread_env"})


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
    if qualname in SKIPPED_SUBMODULES:
        return
    out[qualname] = {"kind": "module"}
    for name, member in sorted(module.members.items()):
        if not _is_public_name(name):
            continue
        full = f"{qualname}.{name}"

        if getattr(member, "is_alias", False):
            target = _resolve(member)
            if target is None:
                out[full] = {
                    "kind": "unresolved-alias",
                    "target_path": str(member.target_path),
                }
                continue
        else:
            target = member

        target_path = getattr(target, "path", None)
        if target_path in SKIPPED_SUBMODULES:
            continue

        if target.is_module:
            _walk(target, full, out)
        elif target.is_class:
            out[full] = _signature_class(target)
        elif target.is_function:
            out[full] = _signature_function(target)
        elif target.is_attribute:
            out[full] = _signature_attribute(target)


def dump_bot_public_api() -> dict[str, Any]:
    """Load bot via griffe, walk its public surface, return a deterministic dict."""
    package = griffe.load(PACKAGE_NAME)
    out: dict[str, Any] = {}
    _walk(package, PACKAGE_NAME, out)
    return out


def main() -> None:
    snapshot = dump_bot_public_api()
    SNAPSHOT_PATH.parent.mkdir(parents=True, exist_ok=True)
    SNAPSHOT_PATH.write_text(
        json.dumps(snapshot, indent=2, sort_keys=True) + "\n"
    )
    print(f"Wrote snapshot: {SNAPSHOT_PATH}")
    print(f"Entries: {len(snapshot)}")


if __name__ == "__main__":
    main()
