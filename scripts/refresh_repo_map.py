#!/usr/bin/env python3
"""Sprint 13 Bit 13.3 (2026-05-11) — auto-generate agent_docs/repository_map.md.

Walks the `bot/` package, extracts each module's top-level classes +
functions + LOC via AST, and writes a hierarchical map. Agentic-eng
utility — future sessions get a fresh repo map on demand via
`make refresh-map` without needing to grep around.

The output is REGENERATABLE — running again produces the same file
(modulo timestamp + commit-hash header line). DO NOT edit
`agent_docs/repository_map.md` manually; edit this script instead.

Stdlib-only (ast + pathlib + subprocess). No bot/* imports.
"""
from __future__ import annotations

import ast
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Tuple

REPO_ROOT = Path(__file__).resolve().parent.parent
BOT_DIR = REPO_ROOT / "bot"
OUTPUT_FILE = REPO_ROOT / "agent_docs" / "repository_map.md"


def _git_short_sha() -> str:
    """Best-effort: return current HEAD short SHA. Empty if not in git."""
    try:
        out = subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=REPO_ROOT, stderr=subprocess.DEVNULL,
        ).decode().strip()
        return out
    except (subprocess.CalledProcessError, FileNotFoundError):
        return ""


def _extract_module(py_file: Path) -> Tuple[int, List[str], List[str]]:
    """Return (loc, classes, functions) for a Python module.

    LOC = total lines including blanks/comments (rough proxy for module
    size — matches `wc -l` semantics, which is the convention in
    bot/CLAUDE.md + existing session-resume docs).

    Classes + functions: TOP-LEVEL ONLY (nested defs are ignored — the
    map is meant to be a navigation aid, not a full symbol table).
    """
    try:
        text = py_file.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        return 0, [], []
    loc = text.count("\n") + (0 if text.endswith("\n") or not text else 1)
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return loc, [], []
    classes: List[str] = []
    functions: List[str] = []
    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            classes.append(node.name)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            # Skip underscore-prefixed private functions in the map —
            # they're implementation details, not navigation anchors.
            if not node.name.startswith("_"):
                functions.append(node.name)
    return loc, sorted(classes), sorted(functions)


def _walk_bot() -> List[Tuple[Path, int, List[str], List[str]]]:
    """Walk bot/ and yield (relpath, loc, classes, functions) for each .py.

    Filters out iCloud-conflict files (` <digit>.py`) and `__pycache__`."""
    rows: List[Tuple[Path, int, List[str], List[str]]] = []
    for root, dirs, files in os.walk(BOT_DIR):
        dirs[:] = [d for d in dirs if d != "__pycache__"]
        # Sort for deterministic output across runs (regen-stability).
        dirs.sort()
        files.sort()
        for f in files:
            if not f.endswith(".py"):
                continue
            # iCloud conflict pattern: " 2.py", " 3.py", etc.
            if any(part for part in f.rsplit(".py", 1)[0].split() if part.isdigit()):
                continue
            full = Path(root) / f
            rel = full.relative_to(REPO_ROOT)
            loc, classes, functions = _extract_module(full)
            rows.append((rel, loc, classes, functions))
    return rows


def _format_module(rel: Path, loc: int, classes: List[str], functions: List[str]) -> str:
    """Render a single module's entry in the map."""
    name = rel.name
    indent_depth = len(rel.parts) - 1  # bot/X.py = 1, bot/engines/X.py = 2
    indent = "  " * indent_depth
    out = [f"{indent}- `{name}` ({loc} LOC)"]
    if classes:
        out.append(f"{indent}  - classes: {', '.join(classes)}")
    if functions:
        # Cap public-function list at 6 to keep map readable; full
        # surface is in tests/contracts/public_api.json (Pillar 1).
        shown = functions[:6]
        suffix = f" (+ {len(functions) - 6} more)" if len(functions) > 6 else ""
        out.append(f"{indent}  - functions: {', '.join(shown)}{suffix}")
    return "\n".join(out)


def _group_by_dir(rows: List[Tuple[Path, int, List[str], List[str]]]) -> List[str]:
    """Group rows by their parent directory + render with dir headers."""
    current_dir: Path | None = None
    out: List[str] = []
    for rel, loc, classes, functions in rows:
        parent = rel.parent
        if parent != current_dir:
            if current_dir is None or parent != current_dir:
                # Insert directory header.
                depth = len(parent.parts) - 1  # bot = 0
                dir_indent = "  " * depth
                if depth == 0:
                    out.append(f"\n### `{parent}/`\n")
                else:
                    out.append(f"{dir_indent}- `{parent.name}/`")
                current_dir = parent
        out.append(_format_module(rel, loc, classes, functions))
    return out


def main() -> int:
    if not BOT_DIR.is_dir():
        print(f"ERROR: {BOT_DIR} not found. Run from repo root.", file=sys.stderr)
        return 1

    rows = _walk_bot()
    if not rows:
        print(f"ERROR: no .py files found under {BOT_DIR}", file=sys.stderr)
        return 1

    total_loc = sum(loc for _, loc, _, _ in rows)
    total_classes = sum(len(c) for _, _, c, _ in rows)
    total_functions = sum(len(f) for _, _, _, f in rows)

    sha = _git_short_sha()
    sha_tag = f" (HEAD: `{sha}`)" if sha else ""
    timestamp = datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    lines: List[str] = [
        "# Repository Map (auto-generated)",
        "",
        "**DO NOT EDIT MANUALLY.** Regenerate with `make refresh-map` "
        f"(invokes `scripts/refresh_repo_map.py`).",
        "",
        f"Generated: {timestamp}{sha_tag}.",
        "",
        "## Summary",
        f"- Modules: {len(rows)} .py files under `bot/`",
        f"- Total LOC: {total_loc:,}",
        f"- Top-level classes: {total_classes}",
        f"- Public top-level functions: {total_functions} "
        f"(underscore-prefixed private functions excluded from this map)",
        "",
        "Public-surface snapshot is in `tests/contracts/public_api.json` "
        "(Pillar 1); this map is the navigation-aid complement.",
        "",
        "## Module tree",
    ]
    lines.extend(_group_by_dir(rows))
    lines.append("")

    content = "\n".join(lines)
    OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_FILE.write_text(content, encoding="utf-8")
    print(f"Wrote {OUTPUT_FILE} ({len(rows)} modules, {total_loc} LOC).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
