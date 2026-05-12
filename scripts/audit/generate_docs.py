#!/usr/bin/env python3
"""Unified doc generation orchestrator.

Runs the full pipeline: extract config → fetch stats → render all templates.
Designed to run locally (with state.db) or in CI (with pre-fetched stats JSON).

Usage:
    # Local (has state.db):
    python3 scripts/audit/generate_docs.py

    # CI / remote (stats pre-fetched):
    python3 scripts/audit/generate_docs.py --stats-json whitepaper_stats.json

    # Extract config only (no rendering):
    python3 scripts/audit/generate_docs.py --config-only

    # Check freshness without rendering:
    python3 scripts/audit/generate_docs.py --check-only
"""

import json
import os
import subprocess
import sys

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
# Bit 11.2 (2026-05-12): relocated to scripts/audit/; need 2 ".." levels.
REPO_DIR = os.path.join(SCRIPT_DIR, "..", "..")

EXTRACT_CONFIG = os.path.join(SCRIPT_DIR, "extract_config.py")
GENERATE_STATS = os.path.join(SCRIPT_DIR, "generate_whitepaper_stats.py")
BUILD_WHITEPAPER = os.path.join(SCRIPT_DIR, "build_whitepaper.py")
CHECK_FRESHNESS = os.path.join(SCRIPT_DIR, "check_docs_freshness.py")

CONFIG_JSON = os.path.join(REPO_DIR, "config.json")
STATS_JSON = os.path.join(REPO_DIR, "whitepaper_stats.json")


def run_step(name, cmd):
    """Run a subprocess step, printing status."""
    print(f"  [{name}] ...", end=" ", flush=True)
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print("FAILED")
        print(f"    stderr: {result.stderr.strip()}")
        return False
    print("OK")
    return True


def main():
    args = sys.argv[1:]
    config_only = "--config-only" in args
    check_only = "--check-only" in args
    stats_json_flag = "--stats-json" in args

    print("Doc generation pipeline")
    print("=" * 40)

    # Step 1: Extract config from bot/_impl.py
    print("\n1. Extracting config from bot/_impl.py...")
    result = subprocess.run(
        [sys.executable, EXTRACT_CONFIG],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        print(f"  FAILED: {result.stderr.strip()}")
        sys.exit(1)
    with open(CONFIG_JSON, "w") as f:
        f.write(result.stdout)
    print(f"  Saved to {CONFIG_JSON}")

    if config_only:
        print("\n--config-only: stopping after config extraction.")
        return

    # Step 2: Generate stats (skip if --stats-json provided or stats file exists)
    print("\n2. Generating whitepaper stats...")
    if stats_json_flag:
        idx = args.index("--stats-json")
        if idx + 1 < len(args):
            src = args[idx + 1]
            if os.path.exists(src) and os.path.abspath(src) != os.path.abspath(STATS_JSON):
                import shutil
                shutil.copy2(src, STATS_JSON)
                print(f"  Using provided stats from {src}")
            elif os.path.exists(src):
                print(f"  Stats file already at {STATS_JSON}")
            else:
                print(f"  ERROR: {src} not found")
                sys.exit(1)
        else:
            print("  ERROR: --stats-json requires a path argument")
            sys.exit(1)
    elif os.path.exists(os.path.join(REPO_DIR, "state.db")):
        result = subprocess.run(
            [sys.executable, GENERATE_STATS],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            print(f"  FAILED: {result.stderr.strip()}")
            sys.exit(1)
        with open(STATS_JSON, "w") as f:
            f.write(result.stdout)
        print(f"  Saved to {STATS_JSON}")
    elif os.path.exists(STATS_JSON):
        print(f"  No state.db found; using existing {STATS_JSON}")
    else:
        print("  WARNING: No state.db and no whitepaper_stats.json — rendering with empty stats")
        with open(STATS_JSON, "w") as f:
            json.dump({}, f)

    if check_only:
        print("\n3. Running freshness check...")
        if os.path.exists(CHECK_FRESHNESS):
            result = subprocess.run(
                [sys.executable, CHECK_FRESHNESS],
                capture_output=True, text=True,
            )
            print(result.stdout)
            if result.returncode != 0:
                print(result.stderr)
            sys.exit(result.returncode)
        else:
            print("  check_docs_freshness.py not found, skipping")
        return

    # Step 3: Render all templates
    print("\n3. Rendering templates...")
    result = subprocess.run(
        [sys.executable, BUILD_WHITEPAPER],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        print(f"  FAILED: {result.stderr.strip()}")
        sys.exit(1)
    # Print each rendered file
    for line in result.stdout.strip().split("\n"):
        print(f"  {line}")
    if result.stderr.strip():
        for line in result.stderr.strip().split("\n"):
            print(f"  WARNING: {line}")

    # Step 4: Run freshness check
    print("\n4. Checking freshness...")
    if os.path.exists(CHECK_FRESHNESS):
        result = subprocess.run(
            [sys.executable, CHECK_FRESHNESS],
            capture_output=True, text=True,
        )
        print(result.stdout.rstrip())
        if result.returncode != 0:
            print(f"\n  Freshness check found issues (see above)")
            sys.exit(1)
    else:
        print("  check_docs_freshness.py not found, skipping")

    print("\nDone.")


if __name__ == "__main__":
    main()
