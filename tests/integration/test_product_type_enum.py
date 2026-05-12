"""product_type enum contract (Tier 1 #2, Apr 24 2026).

Failure class: `product_type` is a stringly-typed enum used across
scan(), settlement routing, SQL queries, CalEngine dispatch, and engine
discovery. A typo or legacy value in any one branch silently dead-codes
the gate. Example: STC shadow gate checked `product_type is None`
while 15M windows had `product_type == "15m"` — the gate was dead for
weeks (98c954d Mar 1 2026).

The canonical source of truth is `market_config.MARKET_CONFIGS` — the
dataclass registry validated against bot/_impl.py at startup by
`validate_market_configs()`. This test asserts every string literal
compared against or assigned to `product_type` anywhere in production
code is a member of that set, and that the canonical set itself has
not silently drifted from {"15m", "hourly", "spx_hourly", "weather",
"sports"}.

Deliberate grep-based implementation — no refactor of scan(). The
CLAUDE.md anti-pattern "Don't refactor bot/_impl.py" plus the scan() edit
track record (dead-code STC shadow gate, nested-gate weather-no) made
the enum-registry refactor too risky. Grep is uglier but strictly
additive: the contract lives in tests, not in runtime code.

See kb/concepts/contract-testing.md Tier 1 #2.
"""

import os
import re
import sys

import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, PROJECT_ROOT)


# Files scanned for product_type literals. Engines + main bot + sync
# layers. Scripts/migrations are explicitly excluded — they handle legacy
# data where old product_type values (e.g., NULL) are expected.
PRODUCTION_FILES = [
    "bot/_impl.py",
    "analyst.py",
    "auditor.py",
    "bot/infra/capital_allocator.py",  # Sprint 10.5a (2026-05-11)
    "dashboard_snapshot.py",
    "bot/shadows/fifteenm_shadow.py",  # Sprint 10.2 (2026-05-11)
    "bot/shadows/hourly_alt_shadow.py",  # Sprint 10.2 (2026-05-11)
    "market_config.py",
    "researcher.py",
    "bot/engines/sports_engine.py",  # Sprint 10.1d (2026-05-11)
    "bot/engines/spx_engine.py",  # Sprint 10.1b sibling-reorg (2026-05-11)
    "bot/shadows/spx_harrv_shadow.py",  # Sprint 10.2 (2026-05-11)
    "supabase_sync.py",
    "watchdog.py",
    "bot/engines/weather_engine.py",  # Sprint 10.1c sibling-reorg (2026-05-11)
]

# Lightweight stability fence: if MARKET_CONFIGS gains or drops a key,
# this assertion fires first so the author must acknowledge every
# downstream consequence (SQL query filters, dashboard panels, audit
# scripts, CalEngine registry wiring).
EXPECTED_CANONICAL = frozenset({
    "15m", "hourly", "spx_hourly", "weather", "sports",
})

# Known non-canonical values tolerated during transition. Adding here
# requires a removal plan — this is not a general allowlist. Every entry
# must have a scheduled cleanup path documented below.
#
#   "dip_addon_shadow"   — bot/_impl.py:16857 passes this to
#       insert_evaluated_opportunity. `filter_stage` is already set to
#       the same tag at bot/_impl.py:16838, so the product_type column gets
#       duplicated context. Code path is gated by DIP_ADDON_ENABLED=False
#       (killed Mar 2026, 55.2% WR), so no new rows are generated.
#       Historical rows in state.db still carry this value. Fix: change
#       the bot/_impl.py:16857 write site to product_type="15m". Deferred to
#       avoid touching bot/_impl.py in Tier 1 scope.
LEGACY_PRODUCT_TYPES = frozenset({
    "dip_addon_shadow",
})


def _load_canonical():
    """The canonical product_type set lives in market_config.MARKET_CONFIGS.

    Using the dataclass registry as the source of truth means adding a
    new product_type to market_config auto-expands this contract.
    """
    from market_config import MARKET_CONFIGS
    return frozenset(MARKET_CONFIGS.keys())


# ───────────────── Extraction regexes ─────────────────
# Each regex returns the literal string operand as its single capture.
# Strict on literal form (no f-strings, no concatenation — verified via
# repo grep), permissive on surrounding structure (handles bare
# `product_type == X`, `window.get("product_type") == X`,
# `row["product_type"] == X`, etc.).

# Matches any comparison where one side anchors on `product_type` (bare
# name, string key in dict access, or string arg to .get()/attr). The
# `[\)\]]?` tolerates `)` or `]` closing a function call / subscript
# right before the operator.
_EQ_RE     = re.compile(
    r"""(?:product_type|["']product_type["'])\s*[\)\]]?\s*"""
    r"""(?:==|!=)\s*["']([^"']+)["']"""
)
# Reverse order: `"x" == product_type` / `"x" == w.get("product_type")`.
_EQ_REV_RE = re.compile(
    r"""["']([^"']+)["']\s*(?:==|!=)\s*"""
    r"""(?:product_type|["']product_type["'][\)\]]?)"""
)
_DICT_RE   = re.compile(r""""product_type"\s*:\s*["']([^"']+)["']""")
_KWARG_RE  = re.compile(r"""\bproduct_type\s*=\s*["']([^"']+)["']""")
_SQL_EQ_RE = re.compile(r"""product_type\s*=\s*'([^']+)'""", re.IGNORECASE)
# `product_type in ("a", "b")` / `w.get("product_type") in {"a", "b"}`.
_IN_RE     = re.compile(
    r"""(?:product_type|["']product_type["'])\s*[\)\]]?\s*"""
    r"""\s+in\s+[\(\{\[]\s*([^\)\}\]]+?)\s*[\)\}\]]"""
)
_STRING_LIT_RE = re.compile(r"""["']([^"']+)["']""")


def _extract_literals(source: str):
    """Yield (literal, match_offset) for every product_type literal."""
    for pattern in (_EQ_RE, _EQ_REV_RE, _DICT_RE, _KWARG_RE, _SQL_EQ_RE):
        for m in pattern.finditer(source):
            yield m.group(1), m.start()
    for m in _IN_RE.finditer(source):
        container_blob = m.group(1)
        for lit in _STRING_LIT_RE.findall(container_blob):
            yield lit, m.start()


# ───────────────── Tests ─────────────────

class TestCanonicalSetStable:
    """MARKET_CONFIGS keys cannot silently change without this test
    firing. Each product_type implies downstream wiring across
    settlement, SQL, CalEngine, dashboard, etc. — an addition or
    removal should force deliberate author acknowledgement.
    """

    def test_market_configs_matches_expected(self):
        canonical = _load_canonical()
        missing = EXPECTED_CANONICAL - canonical
        extra = canonical - EXPECTED_CANONICAL
        assert not (missing or extra), (
            f"market_config.MARKET_CONFIGS has drifted. "
            f"Missing from registry: {sorted(missing)}. "
            f"Extra in registry: {sorted(extra)}. "
            f"If this is intentional, update EXPECTED_CANONICAL in "
            f"tests/integration/test_product_type_enum.py AND verify all downstream "
            f"wiring: SQL filters, CalEngine registry, dashboard "
            f"panels, audit scripts, settlement router."
        )


class TestProductTypeLiteralsInCanonical:
    """Every string literal used in product_type comparison / assignment
    / SQL across production code must be in MARKET_CONFIGS.keys().

    Catches:
      - Typos (`"spx_hourl"`)
      - Legacy values left after a rename
      - Copy-paste from another project
      - Dead-code gates that compare against a value that never exists
    """

    def test_no_unknown_product_type_literals_in_production(self):
        canonical = _load_canonical() | LEGACY_PRODUCT_TYPES
        failures = []
        for fname in PRODUCTION_FILES:
            fpath = os.path.join(PROJECT_ROOT, fname)
            if not os.path.exists(fpath):
                continue
            with open(fpath) as f:
                source = f.read()
            for literal, offset in _extract_literals(source):
                if literal in canonical:
                    continue
                line = source.count("\n", 0, offset) + 1
                failures.append(
                    f"{fname}:{line} product_type literal {literal!r} "
                    f"not in canonical set {sorted(canonical)}"
                )
        assert not failures, (
            "\n".join(failures) +
            "\nIf the literal is intentional, add it to "
            "market_config.MARKET_CONFIGS (permanent) or "
            "LEGACY_PRODUCT_TYPES (time-bounded, with removal plan). "
            "If it's a typo, fix the typo. See "
            "kb/concepts/contract-testing.md Tier 1 #2."
        )


class TestCanonicalTypesHaveProductionCallsites:
    """Every canonical product_type must appear in at least one
    production file. A type that lives only in market_config with zero
    references elsewhere is either dead-coded (delete it) or
    orphan-wired (connect it) — the contract forces author to decide.
    """

    def test_every_canonical_type_used(self):
        canonical = _load_canonical()
        used = set()
        for fname in PRODUCTION_FILES:
            fpath = os.path.join(PROJECT_ROOT, fname)
            if not os.path.exists(fpath):
                continue
            with open(fpath) as f:
                source = f.read()
            for literal, _ in _extract_literals(source):
                if literal in canonical:
                    used.add(literal)

        unused = canonical - used
        assert not unused, (
            f"Canonical product_type(s) with zero production callsites: "
            f"{sorted(unused)}. Either remove from "
            f"market_config.MARKET_CONFIGS or wire up downstream usage."
        )
