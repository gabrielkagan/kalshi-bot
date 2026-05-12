#!/usr/bin/env python3
"""Documentation drift detection — compares config values in code vs docs.

Extracts "facts" from the codebase (bot/constants.py, bot/config.py, market_config.py, etc.)
and compares them against claims in documentation files. Reports DRIFT when a
doc claims a value that differs from the code.

Usage:
    python3 scripts/doc_drift_check.py              # full report
    python3 scripts/doc_drift_check.py --quiet       # only drift, exit code
    python3 scripts/doc_drift_check.py --telegram    # send drift alert via Telegram

Exit code: 1 if any DRIFT detected, 0 if clean.
"""

import argparse
import json
import os
import re
import subprocess
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# Bit 11.2 (2026-05-12): file relocated from `scripts/doc_drift_check.py`
# to `scripts/audit/doc_drift_check.py`. The repo-root anchor must walk
# 3 levels (audit/ → scripts/ → repo/) instead of the pre-Bit-11.2 2.
REPO_ROOT = Path(__file__).resolve().parent.parent.parent

# Source files to extract facts from
# Sprint 12 Bit 12.1 (2026-05-12): `config.py` → `bot/config.py`. Stale
# `bot/_impl.py` entry (deleted in 9.3-iii.c) dropped in same atomic commit.
SOURCE_FILES = [
    "bot/constants.py", "bot/config.py", "market_config.py", "bot/models.py",  # Sprint 10.5b (2026-05-11): models relocated; Sprint 12 Bit 12.1 (2026-05-12): config.py relocated to bot/config.py
    "bot/engines/spx_engine.py", "bot/engines/weather_engine.py", "bot/engines/sports_engine.py",  # Sprint 10.1b/c/d sibling-reorg (2026-05-11): all 3 main engines relocated
    "bot/shadows/fifteenm_shadow.py", "bot/shadows/hourly_alt_shadow.py",  # Sprint 10.2 (2026-05-11)
    # R-p7-deploy-r11 R5: cal_mlp constants live here. Without this entry,
    # changing SIGMA_WINSOR_ABS_CAP / GLOBAL_MIN_ENTRY_PRICE etc. without
    # updating agent_docs/config_reference.md passes drift-check silently.
    "scripts/cal_mlp/features.py",
]

# Documentation files to check
DOC_FILES = [
    "README.md", "whitepaper.md", "whitepaper_investor.md", "CLAUDE.md",
    # Bit 1.3 (Sprint 1 modularization): AGENTS.md is a symlink to CLAUDE.md.
    # Today the scan is redundant (same file via symlink, no false positives).
    # If a future Bit flips the GO/NO-GO to two-file mode (Phase α1 option 2,
    # `kb/decisions/repo-modularization-plan-may05.md` line 891), this entry
    # is what catches divergence between the portable and Claude-specific
    # surfaces. Plan reference: line 897 ("doc_drift_check.py extended to
    # detect symlink target drift"). Pinned by
    # tests/unit/test_agents_md_symlink.py::test_doc_drift_check_includes_agents_md.
    "AGENTS.md",
    # R-p7-deploy-r11 R6: agent_docs/config_reference.md mirrors many
    # of the same constants; without it in DOC_FILES the drift-check
    # is structurally blind to the file the rule explicitly points
    # operators to update.
    "agent_docs/config_reference.md",
]

# Constants to extract via regex from Python source files.
# Format: (constant_name, human_label, optional_file_override)
SIMPLE_CONSTANTS = [
    ("OBSERVATION_MODE", "Observation mode"),
    ("MIN_ENTRY_PRICE", "Global min entry price"),
    ("BTC_MIN_ENTRY_PRICE", "BTC min entry price"),
    ("ETH_MIN_ENTRY_PRICE", "ETH min entry price"),
    ("XRP_MIN_ENTRY_PRICE", "XRP min entry price"),
    ("MAX_ENTRY_PRICE", "Max entry price"),
    ("MAX_SECONDS_BEFORE_CLOSE", "Max seconds before close"),
    ("STC_SHADOW_THRESHOLD", "STC shadow threshold"),
    ("DIRECT_TAKER_THRESHOLD", "Direct taker threshold"),
    ("SOL_TAKER_FIRST", "SOL taker-first"),
    ("XRP_15M_SHADOW", "XRP 15M shadow"),
    ("HOURLY_OBSERVATION_ONLY", "Hourly observation only"),
    ("SPX_HOURLY_OBSERVATION_ONLY", "SPX hourly observation only"),
    ("WEATHER_OBSERVATION_ONLY", "Weather observation only"),
    ("SPORTS_OBSERVATION_ONLY", "Sports observation only"),
    ("HOURLY_CALIBRATION_ENABLED", "Hourly calibration enabled"),
    ("WEATHER_NO_SIDE_LIVE", "Weather NO-side live"),
    ("MARKET_BLEND_W", "Market blend weight"),
    ("MAX_RISK_PER_TRADE", "Max risk per trade"),
    ("XRP_MAX_RISK_PER_TRADE", "XRP max risk per trade"),
    ("BTC_MAX_RISK_PER_TRADE", "BTC max risk per trade"),
    ("SOL_MIN_EDGE", "SOL min edge"),
    ("HOURLY_MAX_RISK_PER_TRADE", "Hourly max risk per trade"),
    ("SPX_HOURLY_MAX_RISK_PER_TRADE", "SPX max risk per trade"),
    ("SPX_HOURLY_MIN_ENTRY_PRICE", "SPX min entry price"),
    ("SPX_HOURLY_MARKET_BLEND_W", "SPX market blend weight"),
    ("SPX_HOURLY_KELLY_FRACTION", "SPX Kelly fraction"),
    ("SPX_HOURLY_BANKROLL_FRACTION", "SPX bankroll fraction"),
    ("HOURLY_KELLY_FRACTION", "Hourly Kelly fraction"),
    ("WEATHER_KELLY_FRACTION", "Weather Kelly fraction"),
    ("WEATHER_MAX_RISK_PER_TRADE", "Weather max risk per trade"),
    ("HOURLY_MIN_STC_ENTRY", "Hourly min STC entry"),
    ("HOURLY_MAX_STC_ENTRY", "Hourly max STC entry"),
    ("HOURLY_MARKET_BLEND_W", "Hourly market blend weight"),
    ("HOURLY_TEMPERATURE_T", "Hourly temperature T"),
    ("LOW_STC_SIZING_CAP", "Low-STC sizing cap"),
    ("LOW_STC_SIZING_CAP_THRESHOLD", "Low-STC sizing cap threshold"),
    ("ESCALATION_WAIT_LONG", "Escalation wait long"),
    ("BTC_ESCALATION_WAIT_OVERRIDE", "BTC escalation wait override"),
    ("ESCALATION_WAIT_MEDIUM", "Escalation wait medium"),
    ("ESCALATION_WAIT_SHORT", "Escalation wait short"),
    ("DECIDED_CONTRACT_Z_T1", "Decided contract Z T1"),
    ("DECIDED_CONTRACT_Z_T2", "Decided contract Z T2"),
    ("DECIDED_CONTRACT_MIN_PRICE", "Decided contract min price"),
    ("DECIDED_CONTRACT_T2_MAX_PRICE", "Decided contract T2 max price"),
    ("DECIDED_CONTRACT_RISK", "Decided contract risk"),
    ("ADDON_ENABLED", "Addon enabled"),
    ("DIP_ADDON_ENABLED", "Dip addon enabled"),
    ("DIP_ADDON_SHADOW_MODE", "Dip addon shadow mode"),
    ("DRAWDOWN_HALF_THRESHOLD", "Drawdown half threshold"),
    ("DRAWDOWN_QUARTER_THRESHOLD", "Drawdown quarter threshold"),
    ("DRAWDOWN_HALT_THRESHOLD", "Drawdown halt threshold"),
    # R-p7-deploy-r11 R6: cal_mlp constants. Source = scripts/cal_mlp/features.py
    # (added to SOURCE_FILES). Adding here so a future bump to any of these
    # without corresponding doc update is caught by `doc_drift_check.py`.
    ("SIGMA_WINSOR_ABS_CAP", "Sigma winsor abs cap (cal_mlp)"),
    ("RAW_PROB_CLIP_EPS", "Raw prob clip eps (cal_mlp)"),
    # R-bleed-1 R3-MED4: bleed-cell numerics. Source = bot/_impl.py.
    ("TM98_HIGHPRICE_BLEED_BLOCK_PRICE_LO", "TM-98 bleed block price lo"),
    ("TM98_HIGHPRICE_BLEED_BLOCK_PRICE_HI", "TM-98 bleed block price hi"),
    ("TM98_HIGHPRICE_BLEED_BLOCK_STC_LO_S", "TM-98 bleed block STC lo seconds"),
    ("TM98_HIGHPRICE_BLEED_BLOCK_STC_HI_S", "TM-98 bleed block STC hi seconds"),
    ("SOL_TAKER_LOWPRICE_BLEED_BLOCK_PRICE_LO", "SOL TAKER bleed block price lo"),
    ("SOL_TAKER_LOWPRICE_BLEED_BLOCK_PRICE_HI", "SOL TAKER bleed block price hi"),
    ("SOL_TAKER_LOWPRICE_BLEED_BLOCK_STC_LO_S", "SOL TAKER bleed block STC lo seconds"),
    ("SOL_TAKER_LOWPRICE_BLEED_BLOCK_STC_HI_S", "SOL TAKER bleed block STC hi seconds"),
]


# ---------------------------------------------------------------------------
# Fact extraction
# ---------------------------------------------------------------------------

def extract_constant(name: str, source_lines: Dict[str, List[str]]) -> Optional[str]:
    """Extract a constant value from source files using regex."""
    # Match: CONSTANT = value  or  CONSTANT = os.environ.get(..., "1") == "1"
    pattern = re.compile(
        rf'^\s*{re.escape(name)}\s*=\s*(.+?)(?:\s*#.*)?$'
    )
    for filename, lines in source_lines.items():
        for line in lines:
            m = pattern.match(line)
            if m:
                raw = m.group(1).strip()
                # Handle os.environ.get patterns — extract the default
                env_match = re.match(
                    r'os\.environ\.get\([^,]+,\s*["\'](.+?)["\']\)\s*==\s*["\'](.+?)["\']',
                    raw,
                )
                if env_match:
                    return str(env_match.group(1) == env_match.group(2))
                # Clean trailing comments that slipped through
                raw = re.sub(r'\s*#.*$', '', raw)
                return raw
    return None


def normalize_value(raw: str) -> str:
    """Normalize a Python literal to a comparable string."""
    raw = raw.strip().rstrip(',')
    # Remove quotes
    if (raw.startswith('"') and raw.endswith('"')) or \
       (raw.startswith("'") and raw.endswith("'")):
        raw = raw[1:-1]
    # Normalize booleans
    if raw in ('True', 'true'):
        return 'True'
    if raw in ('False', 'false'):
        return 'False'
    # Normalize numbers — remove trailing .0 for ints
    try:
        f = float(raw)
        if f == int(f) and '.' not in raw:
            return str(int(f))
        return raw
    except (ValueError, OverflowError):
        pass
    return raw


def get_line_count() -> int:
    """Returns 0 post-Bit-9.3-iii.c (bot/_impl.py was deleted).
    Retained as a no-op so the call site at L258 + downstream BOT_LINE_COUNT
    consumers (whitepaper template) don't break — `if lc:` falls through to skip
    the fact emission. Out-of-scope cleanup tracked in followup."""
    return 0


def get_test_count() -> Optional[int]:
    """Get pytest test count."""
    try:
        result = subprocess.run(
            [sys.executable, "-m", "pytest", "--collect-only", "-q"],
            capture_output=True, text=True, timeout=30,
            cwd=str(REPO_ROOT),
        )
        # Look for "N tests collected" or "N test collected"
        for line in result.stdout.splitlines():
            m = re.search(r'(\d+)\s+tests?\s+collected', line)
            if m:
                return int(m.group(1))
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass
    return None


def count_weather_cities() -> int:
    """Count weather cities from bot/engines/weather_engine.py."""
    we_path = REPO_ROOT / "bot" / "engines" / "weather_engine.py"  # Sprint 10.1c (2026-05-11)
    if not we_path.exists():
        return 0
    with open(we_path) as f:
        content = f.read()
    return len(re.findall(r'"series_ticker":\s*"KXHIGH', content))


def count_sports_leagues() -> int:
    """Count sports leagues from bot/engines/sports_data.py or bot/engines/sports_engine.py.

    Sprint 10.1a-d (2026-05-11): sports_data + sports_engine both relocated to
    bot/engines/. Master plan L2225-2238 Sprint 10 engines/ sub-moves complete."""
    for fname in ("bot/engines/sports_data.py", "bot/engines/sports_engine.py"):
        path = REPO_ROOT / fname
        if not path.exists():
            continue
        with open(path) as f:
            content = f.read()
        # Count league entries in LR tables or LEAGUE configs
        leagues = set(re.findall(r'"league":\s*"([^"]+)"', content))
        if leagues:
            return len(leagues)
    return 0


def extract_all_facts(source_lines: Dict[str, List[str]]) -> Dict[str, Any]:
    """Extract all facts from the codebase."""
    facts = {}

    # Simple constants
    for name, label in SIMPLE_CONSTANTS:
        raw = extract_constant(name, source_lines)
        if raw is not None:
            facts[name] = {
                "label": label,
                "value": normalize_value(raw),
                "raw": raw,
            }

    # Structural facts — `get_line_count()` is a no-op post-Bit-9.3-iii.c
    # (bot/_impl.py deleted). `lc` is always 0; the block is reachable but
    # falls through. Kept for output-schema stability against any downstream
    # consumer of BOT_LINE_COUNT.
    lc = get_line_count()
    if lc:
        facts["BOT_LINE_COUNT"] = {
            "label": "bot.* combined line count (deprecated; was bot/_impl.py pre-9.3-iii.c)",
            "value": str(lc),
            "raw": str(lc),
        }

    tc = get_test_count()
    if tc is not None:
        facts["TEST_COUNT"] = {
            "label": "Test count",
            "value": str(tc),
            "raw": str(tc),
        }

    wc = count_weather_cities()
    if wc:
        facts["WEATHER_CITY_COUNT"] = {
            "label": "Weather city count",
            "value": str(wc),
            "raw": str(wc),
        }

    return facts


# ---------------------------------------------------------------------------
# Document scanning
# ---------------------------------------------------------------------------

def build_doc_patterns(facts: Dict[str, Any]) -> List[Tuple[str, str, re.Pattern, str]]:
    """Build regex patterns to find fact claims in documentation.

    Returns list of (fact_key, label, pattern, extract_group_hint).
    """
    patterns = []

    for key, info in facts.items():
        val = info["value"]
        label = info["label"]

        # 1. Explicit constant name mention: "CONSTANT = value" or "CONSTANT=value"
        #    or "| CONSTANT | value |" (markdown table)
        #    Use boundary check to prevent MARKET_BLEND_W matching SPX_HOURLY_MARKET_BLEND_W
        pat_name = re.compile(
            rf'(?:^|[^A-Z_]){re.escape(key)}(?:[^A-Z_]|$)\s*[=|]\s*[`|]?\s*(\S+)',
        )
        patterns.append((key, label, pat_name, "explicit_name"))

        # 2. For numeric values, build contextual patterns
        if key == "BOT_LINE_COUNT":
            # Match patterns like "~14,000 lines" or "~14000 lines" or "(14,043 lines"
            pat = re.compile(r'~?([\d,]+)\s*lines', re.IGNORECASE)
            patterns.append((key, label, pat, "line_count"))

        elif key == "TEST_COUNT":
            pat = re.compile(r'([\d,]+)\s*tests?', re.IGNORECASE)
            patterns.append((key, label, pat, "test_count"))

        elif key == "WEATHER_CITY_COUNT":
            pat = re.compile(r'(\d+)\s*(?:US\s+)?cit(?:y|ies)', re.IGNORECASE)
            patterns.append((key, label, pat, "city_count"))

    return patterns


def scan_doc_for_claims(
    doc_path: Path,
    patterns: List[Tuple[str, str, re.Pattern, str]],
) -> List[Dict[str, Any]]:
    """Scan a document for claims matching our patterns.

    Returns list of {fact_key, label, doc_file, line_num, claimed_value, pattern_type}.
    """
    if not doc_path.exists():
        return []

    with open(doc_path) as f:
        lines = f.readlines()

    claims = []
    doc_name = doc_path.name

    for line_num, line in enumerate(lines, 1):
        for fact_key, label, pattern, ptype in patterns:
            for m in pattern.finditer(line):
                claimed = m.group(1).strip().rstrip('|').strip().strip('`').strip()
                if not claimed:
                    continue
                claims.append({
                    "fact_key": fact_key,
                    "label": label,
                    "doc_file": doc_name,
                    "line_num": line_num,
                    "claimed_value": claimed,
                    "pattern_type": ptype,
                    "_line_text": line,
                })

    return claims


# ---------------------------------------------------------------------------
# Comparison
# ---------------------------------------------------------------------------

def values_match(code_val: str, doc_val: str, fact_key: str) -> bool:
    """Compare a code value against a doc claim, with tolerance for formatting."""
    # Normalize both
    cv = code_val.strip().lower()
    dv = doc_val.strip().lower()

    # Direct match
    if cv == dv:
        return True

    # Numeric comparison with tolerance for comma formatting
    try:
        cn = float(cv.replace(",", ""))
        dn = float(dv.replace(",", ""))
        if cn == dn:
            return True
        # For line counts, allow ~500 tolerance (docs say "~14,000" for 14,043)
        if fact_key == "BOT_LINE_COUNT":
            return abs(cn - dn) <= 500
        # For test counts, allow ±20 tolerance
        if fact_key == "TEST_COUNT":
            return abs(cn - dn) <= 20
    except (ValueError, OverflowError):
        pass

    # Boolean normalization
    truthy = {'true', '1', 'yes', 'enabled', 'live'}
    falsy = {'false', '0', 'no', 'disabled', 'shadow', 'observation'}
    if cv in truthy and dv in truthy:
        return True
    if cv in falsy and dv in falsy:
        return True

    return False


def compare_facts_to_claims(
    facts: Dict[str, Any],
    all_claims: List[Dict[str, Any]],
) -> Tuple[List[Dict], List[Dict], List[str]]:
    """Compare extracted facts against document claims.

    Returns (drifts, matches, missing_keys).
    """
    drifts = []
    matches = []
    seen_keys = set()

    for claim in all_claims:
        key = claim["fact_key"]
        if key not in facts:
            continue
        seen_keys.add(key)

        code_val = facts[key]["value"]
        doc_val = claim["claimed_value"]

        if values_match(code_val, doc_val, key):
            matches.append({
                **claim,
                "code_value": code_val,
            })
        else:
            # Filter out false positives from overly broad patterns
            if _is_false_positive(claim, code_val):
                continue
            drifts.append({
                **claim,
                "code_value": code_val,
            })

    missing = [k for k in facts if k not in seen_keys]
    return drifts, matches, missing


def _is_false_positive(claim: Dict, code_val: str) -> bool:
    """Filter out obvious false positive matches."""
    ptype = claim["pattern_type"]
    doc_val = claim["claimed_value"]

    # Line count: ignore matches that are clearly not about bot/_impl.py
    # (e.g., "4096" in Telegram message truncation)
    if ptype == "line_count":
        try:
            n = int(doc_val.replace(",", ""))
            if n < 1000 or n > 50000:
                return True
        except ValueError:
            return True

    # Test count: ignore small numbers that aren't test counts
    if ptype == "test_count":
        try:
            n = int(doc_val.replace(",", ""))
            if n < 50:
                return True
        except ValueError:
            return True

    # City count: ignore small numbers
    if ptype == "city_count":
        try:
            n = int(doc_val)
            if n < 3:
                return True
        except ValueError:
            return True

    # Explicit name matches: if the claimed value is clearly a different
    # type (e.g., a description string, not a number), skip it
    if ptype == "explicit_name":
        # Skip if the "value" is a word, not a number/bool
        if re.match(r'^[a-zA-Z]{3,}', doc_val) and doc_val.lower() not in (
            'true', 'false', 'none', 'set()',
        ):
            return True
        # Skip instructional text like "Set OBSERVATION_MODE = True to run..."
        line_text = claim.get("_line_text", "")
        if re.search(r'\b(set|to run|to enable|to disable)\b', line_text, re.IGNORECASE):
            return True

    # STC_SHADOW_THRESHOLD: ignore trailing parentheses/brackets
    if "claimed_value" in claim:
        cleaned = re.sub(r'[)\]}]+$', '', doc_val)
        if cleaned != doc_val and values_match(code_val, cleaned, claim["fact_key"]):
            return True

    return False


# ---------------------------------------------------------------------------
# Deduplication
# ---------------------------------------------------------------------------

def dedupe_results(items: List[Dict]) -> List[Dict]:
    """Deduplicate results — keep one per (fact_key, doc_file) pair,
    preferring explicit_name matches over contextual ones."""
    best = {}
    priority = {"explicit_name": 0, "line_count": 1, "test_count": 1, "city_count": 1}
    for item in items:
        k = (item["fact_key"], item["doc_file"])
        p = priority.get(item["pattern_type"], 2)
        if k not in best or p < priority.get(best[k]["pattern_type"], 2):
            best[k] = item
    return list(best.values())


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

def format_report(
    drifts: List[Dict],
    matches: List[Dict],
    missing: List[str],
    facts: Dict[str, Any],
) -> str:
    """Format the drift report."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    lines = [
        "=== Documentation Drift Report ===",
        f"Generated: {now}",
        "",
    ]

    if drifts:
        lines.append(f"DRIFT DETECTED ({len(drifts)} issues):")
        for d in sorted(drifts, key=lambda x: (x["doc_file"], x["line_num"])):
            lines.append(
                f"  \u274c {d['doc_file']}:{d['line_num']}: "
                f"{d['label']} — doc says {d['claimed_value']!r}, "
                f"code says {d['code_value']!r}"
            )
        lines.append("")

    if matches:
        lines.append(f"MATCHES ({len(matches)} verified):")
        for m in sorted(matches, key=lambda x: (x["doc_file"], x["fact_key"])):
            lines.append(
                f"  \u2705 {m['doc_file']}: {m['label']}={m['code_value']} \u2713"
            )
        lines.append("")

    if missing:
        lines.append(f"NOT MENTIONED IN DOCS ({len(missing)} facts):")
        for k in sorted(missing):
            if k in facts:
                lines.append(f"  \u2139\ufe0f  {facts[k]['label']}: {facts[k]['value']}")
        lines.append("")

    if drifts:
        lines.append(f"RESULT: {len(drifts)} drift(s) found — docs need updating")
    else:
        lines.append("RESULT: All checked values match \u2714")

    return "\n".join(lines)


def format_telegram_alert(drifts: List[Dict]) -> str:
    """Format a compact Telegram alert for drift."""
    lines = ["*Doc Drift Alert*\n"]
    for d in drifts[:10]:  # Cap at 10 to stay under 4096 chars
        lines.append(
            f"• `{d['doc_file']}:{d['line_num']}` "
            f"{d['label']}: doc=`{d['claimed_value']}` code=`{d['code_value']}`"
        )
    if len(drifts) > 10:
        lines.append(f"\n...and {len(drifts) - 10} more")
    lines.append(f"\nRun `python3 scripts/doc_drift_check.py` for full report.")
    return "\n".join(lines)


def send_telegram(message: str) -> bool:
    """Send drift alert via Telegram."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")
    if not token or not chat_id:
        print("WARNING: TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID not set, skipping alert",
              file=sys.stderr)
        return False

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    # Escape underscores for Markdown
    escaped = message.replace("_", "\\_")
    body = json.dumps({
        "chat_id": chat_id,
        "text": escaped[:4096],
        "parse_mode": "Markdown",
    }).encode()
    req = urllib.request.Request(url, data=body,
                                headers={"Content-Type": "application/json"})
    try:
        urllib.request.urlopen(req, timeout=10)
        return True
    except Exception as e:
        print(f"WARNING: Telegram send failed: {e}", file=sys.stderr)
        return False


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Documentation drift detection")
    parser.add_argument("--quiet", action="store_true",
                        help="Only show drift, suppress matches")
    parser.add_argument("--telegram", action="store_true",
                        help="Send Telegram alert if drift found")
    parser.add_argument("--report-file", type=str, default=None,
                        help="Write report to file (default: DOC_DRIFT_REPORT.txt)")
    args = parser.parse_args()

    # 1. Read source files
    source_lines: Dict[str, List[str]] = {}
    for fname in SOURCE_FILES:
        path = REPO_ROOT / fname
        if path.exists():
            with open(path) as f:
                source_lines[fname] = f.readlines()

    # 2. Extract facts from code
    facts = extract_all_facts(source_lines)
    if not facts:
        print("ERROR: No facts extracted from codebase", file=sys.stderr)
        sys.exit(2)

    # 3. Build search patterns
    patterns = build_doc_patterns(facts)

    # 4. Scan all documentation files
    all_claims = []
    for doc_name in DOC_FILES:
        doc_path = REPO_ROOT / doc_name
        # Bit 1.3 (Sprint 1 modularization): skip symlinks. AGENTS.md is a
        # symlink to CLAUDE.md today (option 1, plan line 890); scanning
        # both would emit two separate `doc_file` rows for the same
        # physical file (dedupe_results would NOT collapse them — it keys
        # on (fact_key, doc_file) and `doc_name = doc_path.name` returns
        # different names for the symlink and its target — so reports
        # would inflate and confuse drift triage). When a future Bit flips
        # to two-file mode (plan line 891), AGENTS.md becomes a regular
        # file and this skip falls through naturally, activating the
        # DOC_FILES entry's divergence-detection purpose. Pinned by
        # tests/unit/test_agents_md_symlink.py::test_doc_drift_check_includes_agents_md.
        if doc_path.is_symlink():
            continue
        claims = scan_doc_for_claims(doc_path, patterns)
        all_claims.extend(claims)

    # 5. Compare
    drifts, matches, missing = compare_facts_to_claims(facts, all_claims)

    # 6. Deduplicate
    drifts = dedupe_results(drifts)
    matches = dedupe_results(matches)

    # 7. Report
    if args.quiet and not drifts:
        print("Documentation drift check: CLEAN")
        sys.exit(0)

    if args.quiet:
        for d in drifts:
            print(
                f"DRIFT: {d['doc_file']}:{d['line_num']}: "
                f"{d['label']} — doc={d['claimed_value']!r}, code={d['code_value']!r}"
            )
        sys.exit(1)

    report = format_report(drifts, matches, missing, facts)
    print(report)

    # Write report file
    report_path = args.report_file or str(REPO_ROOT / "DOC_DRIFT_REPORT.txt")
    if drifts:
        with open(report_path, "w") as f:
            f.write(report)

    # Telegram alert
    if args.telegram and drifts:
        alert = format_telegram_alert(drifts)
        send_telegram(alert)

    sys.exit(1 if drifts else 0)


if __name__ == "__main__":
    main()
