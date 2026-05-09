"""Typed schema + structural parser for Track A proposals.

The DSL surface (autoresearch-design-may05.md):

  thesis: "natural-language hypothesis"
  filters:
    asset: ["BTC", "ETH", "SOL", "XRP"]
    yes_ask_cents: [80, 99]
    edge_threshold: 0.015
    stc_seconds: [60, 300]
    hour_of_day_utc: [0, 1, 2, ..., 23]
    day_of_week: ["mon", ..., "sun"]
    price_tier: ["<80", "80-89", "90-97", "98-99"]
    regime: ["normal", "high_vol", ...]
  sizing:
    rule: "kelly_capped"
    fraction: 0.25
    max_contracts: 50
    stc_scaler: false
  exclusions:
    filter_stage_blocks: ["TM98_97_98C_2_5MIN_BLEED", ...]

`parse_proposal()` does STRUCTURAL validation only — type/enum/range
checks. Semantic checks (kill switches, non-degeneracy, constraint
references) live in `research.dsl.validate.validate()`.

Loads YAML via a `yaml.SafeLoader` subclass (`_DupKeyDetectingLoader`)
that adds dup-key rejection on top of safe-load semantics. `yaml.load(
…, Loader=_DupKeyDetectingLoader)` is equivalent to `yaml.safe_load`
plus dup-key check — the unsafe constructor table is NOT inherited.
PyYAML 6.x is a Mac soft-dep of the autoresearch tree (research/ is
Mac-only by design); the import is at module top so a missing PyYAML
fails loud rather than silently degrading.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional, Tuple, Union

import yaml


# R1-finding-6: cap proposal text size to bound parser cost. 1MB is
# ~5000× the largest legitimate Qwen-emitted proposal in the design
# (~1KB per autoresearch-design-may05.md). Prevents DoS via runaway
# samples without rejecting anything realistic.
MAX_PROPOSAL_BYTES = 1_000_000


# R1-finding-5: PyYAML's default safe_load silently takes the LAST
# value on duplicate keys. Qwen could emit `asset: ["BTC"], asset: ["ETH"]`
# and the validator never sees the conflict. We subclass SafeLoader
# and reject duplicates at construction time so the structural parser
# catches them.
class _DupKeyDetectingLoader(yaml.SafeLoader):
    pass


def _construct_mapping_no_dups(loader, node, deep=False):
    keys = []
    for key_node, _value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in keys:
            raise yaml.constructor.ConstructorError(
                "while constructing a mapping", node.start_mark,
                f"duplicate key {key!r}", key_node.start_mark,
            )
        keys.append(key)
    return yaml.SafeLoader.construct_mapping(loader, node, deep=deep)


_DupKeyDetectingLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_mapping_no_dups,
)


# ── Registered enum values (canonical) ────────────────────────────────

# Crypto-15M assets the bot trades. Sport assets exist in vol_regime
# but are not 15M-DSL-eligible; caller supplies registered_assets via
# Constraints if expanded later.
ASSETS = frozenset({"BTC", "ETH", "SOL", "XRP"})

# Day-of-week enum (lowercase 3-letter). datetime.strftime("%a").lower()-compatible.
DAYS_OF_WEEK = frozenset({"mon", "tue", "wed", "thu", "fri", "sat", "sun"})

# Price-tier strings — match research.holdouts.price_tier (Ph1a).
PRICE_TIERS = frozenset({"<80", "80-89", "90-97", "98-99"})

# Sizing rule registry — extensible by adding to this set + a sizer impl.
SIZING_RULES = frozenset({"kelly_capped", "fixed", "scaled"})

# Schema-allowed top-level keys.
TOP_LEVEL_KEYS = frozenset({"thesis", "filters", "sizing", "exclusions"})

# Schema-allowed sub-keys per block.
FILTER_KEYS = frozenset({
    "asset", "yes_ask_cents", "edge_threshold", "stc_seconds",
    "hour_of_day_utc", "day_of_week", "price_tier", "regime",
})
SIZING_KEYS = frozenset({"rule", "fraction", "max_contracts", "stc_scaler"})
EXCLUSION_KEYS = frozenset({"filter_stage_blocks"})


class SchemaError(ValueError):
    """Raised on structural parse failure. Message identifies the
    specific key + reason."""


@dataclass(frozen=True)
class ParsedFilters:
    """All filter axes are optional. Omitted axis = "no filter on
    this dimension". Tuples are used (not lists) so ParsedProposal
    is hashable for Ph1c.2 anti-collusion dedup.

    NB: dedup is first-seen-order at parse time (per R2-finding-2),
    NOT sorted. Ph1c.2 anti-collusion that wants order-invariant
    equality should canonicalize via `tuple(sorted(t))` before
    hashing — see kb/decisions/auto-research-phase-1c-1-plan-may09.md
    "decisions to confirm".
    """
    asset: Optional[Tuple[str, ...]] = None
    yes_ask_cents: Optional[Tuple[int, int]] = None
    edge_threshold: Optional[float] = None
    stc_seconds: Optional[Tuple[int, int]] = None
    hour_of_day_utc: Optional[Tuple[int, ...]] = None
    day_of_week: Optional[Tuple[str, ...]] = None
    price_tier: Optional[Tuple[str, ...]] = None
    regime: Optional[Tuple[str, ...]] = None

    def is_empty(self) -> bool:
        """True iff EVERY filter axis is None (no filtering at all).
        Used by validate() to enforce non-degeneracy."""
        return all(getattr(self, k) is None for k in FILTER_KEYS)


@dataclass(frozen=True)
class ParsedSizing:
    rule: str
    fraction: float
    max_contracts: int
    stc_scaler: bool


@dataclass(frozen=True)
class ParsedExclusions:
    filter_stage_blocks: Tuple[str, ...] = ()


@dataclass(frozen=True)
class ParsedProposal:
    thesis: Optional[str]
    filters: ParsedFilters
    sizing: ParsedSizing
    exclusions: ParsedExclusions


def _expect_dict(value: Any, key: str) -> dict:
    if not isinstance(value, dict):
        raise SchemaError(
            f"{key!r}: expected mapping, got {type(value).__name__}"
        )
    return value


def _check_unknown_keys(value: dict, allowed: frozenset, where: str) -> None:
    extra = set(value.keys()) - allowed
    if extra:
        raise SchemaError(
            f"{where}: unknown key(s) {sorted(extra)}; "
            f"allowed: {sorted(allowed)}"
        )


def _expect_int(value: Any, key: str) -> int:
    # Reject bool — `isinstance(True, int)` is True in Python; we
    # don't want booleans coerced to 0/1 in numeric slots.
    if isinstance(value, bool) or not isinstance(value, int):
        raise SchemaError(
            f"{key!r}: expected int, got {type(value).__name__}"
        )
    return value


def _expect_float(value: Any, key: str) -> float:
    if isinstance(value, bool):
        raise SchemaError(f"{key!r}: expected float, got bool")
    if isinstance(value, int):
        return float(value)
    if not isinstance(value, float):
        raise SchemaError(
            f"{key!r}: expected float, got {type(value).__name__}"
        )
    # R1-finding-1: reject NaN/Inf. NaN comparisons always return False
    # so subsequent range/sign guards (e.g., `edge_threshold < 0`) silently
    # let it through, producing a "valid proposal that trades nothing"
    # downstream because every replay row's `mid_price >= NaN` is False.
    if not math.isfinite(value):
        raise SchemaError(
            f"{key!r}: must be finite, got {value}"
        )
    return value


def _expect_bool(value: Any, key: str) -> bool:
    if not isinstance(value, bool):
        raise SchemaError(
            f"{key!r}: expected bool, got {type(value).__name__}"
        )
    return value


def _expect_str(value: Any, key: str) -> str:
    if not isinstance(value, str):
        raise SchemaError(
            f"{key!r}: expected str, got {type(value).__name__}"
        )
    return value


def _expect_min_max_pair(value: Any, key: str,
                        lo: int, hi: int) -> Tuple[int, int]:
    """`[min, max]` int tuple within `[lo, hi]`. min <= max not enforced
    here (semantic check belongs to validate.py)."""
    if not isinstance(value, list) or len(value) != 2:
        raise SchemaError(
            f"{key!r}: expected [min, max] 2-element list, "
            f"got {value!r}"
        )
    a = _expect_int(value[0], f"{key}[0]")
    b = _expect_int(value[1], f"{key}[1]")
    if not (lo <= a <= hi and lo <= b <= hi):
        raise SchemaError(
            f"{key!r}: values must be in [{lo}, {hi}], got [{a}, {b}]"
        )
    return (a, b)


def _dedup_preserve_order(items):
    """Drop duplicates while preserving first-seen order.

    R2-finding-2: list fields like `filters.asset`, `filters.regime`,
    `exclusions.filter_stage_blocks`, `hour_of_day_utc`, `day_of_week`,
    `price_tier` are SEMANTIC SETS. A proposal with `regime: ["a","a",...]`
    × 10,000 should not produce a 10,000-element ParsedFilters tuple
    that downstream code iterates over — both pre-replay validation
    and replay loops do redundant work for no gain. Dedup at parse
    time is free defense-in-depth and keeps Qwen output canonical.
    """
    seen = set()
    out = []
    for x in items:
        if x in seen:
            continue
        seen.add(x)
        out.append(x)
    return out


def _expect_subset(value: Any, key: str,
                   allowed: frozenset, item_type: type) -> Tuple:
    if not isinstance(value, list):
        raise SchemaError(
            f"{key!r}: expected list, got {type(value).__name__}"
        )
    out = []
    for i, x in enumerate(value):
        if item_type is int:
            x = _expect_int(x, f"{key}[{i}]")
        elif item_type is str:
            x = _expect_str(x, f"{key}[{i}]")
        if x not in allowed:
            raise SchemaError(
                f"{key}[{i}]={x!r} not in allowed set {sorted(allowed)}"
            )
        out.append(x)
    return tuple(_dedup_preserve_order(out))


def _expect_int_subset_range(value: Any, key: str,
                             lo: int, hi: int) -> Tuple[int, ...]:
    if not isinstance(value, list):
        raise SchemaError(
            f"{key!r}: expected list, got {type(value).__name__}"
        )
    out = []
    for i, x in enumerate(value):
        x = _expect_int(x, f"{key}[{i}]")
        if not (lo <= x <= hi):
            raise SchemaError(
                f"{key}[{i}]={x} out of range [{lo}, {hi}]"
            )
        out.append(x)
    return tuple(_dedup_preserve_order(out))


def _parse_filters(raw: dict) -> ParsedFilters:
    raw = _expect_dict(raw, "filters")
    _check_unknown_keys(raw, FILTER_KEYS, "filters")

    asset = (
        _expect_subset(raw["asset"], "filters.asset", ASSETS, str)
        if "asset" in raw else None
    )
    yes_ask_cents = (
        _expect_min_max_pair(raw["yes_ask_cents"],
                             "filters.yes_ask_cents", 1, 99)
        if "yes_ask_cents" in raw else None
    )
    edge_threshold = (
        _expect_float(raw["edge_threshold"], "filters.edge_threshold")
        if "edge_threshold" in raw else None
    )
    if edge_threshold is not None and edge_threshold < 0:
        raise SchemaError(
            f"filters.edge_threshold: must be >= 0, got {edge_threshold}"
        )
    stc_seconds = (
        _expect_min_max_pair(raw["stc_seconds"],
                             "filters.stc_seconds", 0, 3600)
        if "stc_seconds" in raw else None
    )
    hour_of_day_utc = (
        _expect_int_subset_range(raw["hour_of_day_utc"],
                                 "filters.hour_of_day_utc", 0, 23)
        if "hour_of_day_utc" in raw else None
    )
    day_of_week = (
        _expect_subset(raw["day_of_week"], "filters.day_of_week",
                       DAYS_OF_WEEK, str)
        if "day_of_week" in raw else None
    )
    price_tier = (
        _expect_subset(raw["price_tier"], "filters.price_tier",
                       PRICE_TIERS, str)
        if "price_tier" in raw else None
    )
    # `regime` enum is caller-supplied (registered set varies by
    # caller context — see validate.Constraints.registered_regimes).
    # At parse time we accept any list[str] and let validate.py reject
    # unregistered values. Dedup'd per R2-finding-2.
    regime = None
    if "regime" in raw:
        rraw = raw["regime"]
        if not isinstance(rraw, list):
            raise SchemaError(
                f"filters.regime: expected list, got {type(rraw).__name__}"
            )
        regime = tuple(_dedup_preserve_order(
            _expect_str(x, f"filters.regime[{i}]")
            for i, x in enumerate(rraw)
        ))
    return ParsedFilters(
        asset=asset, yes_ask_cents=yes_ask_cents,
        edge_threshold=edge_threshold, stc_seconds=stc_seconds,
        hour_of_day_utc=hour_of_day_utc, day_of_week=day_of_week,
        price_tier=price_tier, regime=regime,
    )


def _parse_sizing(raw: dict) -> ParsedSizing:
    raw = _expect_dict(raw, "sizing")
    _check_unknown_keys(raw, SIZING_KEYS, "sizing")
    for required in ("rule", "fraction", "max_contracts", "stc_scaler"):
        if required not in raw:
            raise SchemaError(
                f"sizing: required key {required!r} missing"
            )
    rule = _expect_str(raw["rule"], "sizing.rule")
    if rule not in SIZING_RULES:
        raise SchemaError(
            f"sizing.rule: {rule!r} not in {sorted(SIZING_RULES)}"
        )
    fraction = _expect_float(raw["fraction"], "sizing.fraction")
    if not (0 < fraction <= 1):
        raise SchemaError(
            f"sizing.fraction: must be in (0, 1], got {fraction}"
        )
    max_contracts = _expect_int(raw["max_contracts"], "sizing.max_contracts")
    if max_contracts < 1:
        raise SchemaError(
            f"sizing.max_contracts: must be >= 1, got {max_contracts}"
        )
    stc_scaler = _expect_bool(raw["stc_scaler"], "sizing.stc_scaler")
    return ParsedSizing(
        rule=rule, fraction=fraction,
        max_contracts=max_contracts, stc_scaler=stc_scaler,
    )


def _parse_exclusions(raw: Optional[dict]) -> ParsedExclusions:
    if raw is None:
        return ParsedExclusions()
    raw = _expect_dict(raw, "exclusions")
    _check_unknown_keys(raw, EXCLUSION_KEYS, "exclusions")
    blocks: Tuple[str, ...] = ()
    if "filter_stage_blocks" in raw:
        rraw = raw["filter_stage_blocks"]
        if not isinstance(rraw, list):
            raise SchemaError(
                f"exclusions.filter_stage_blocks: expected list, "
                f"got {type(rraw).__name__}"
            )
        # Dedup per R2-finding-2 (semantic set).
        blocks = tuple(_dedup_preserve_order(
            _expect_str(x, f"exclusions.filter_stage_blocks[{i}]")
            for i, x in enumerate(rraw)
        ))
    return ParsedExclusions(filter_stage_blocks=blocks)


def parse_proposal(source: Union[str, Path]) -> ParsedProposal:
    """Parse YAML source into ParsedProposal. Raises SchemaError on
    any structural error.

    `source` may be a YAML string or a `Path` to a YAML file. The
    Path branch is for golden-file tests; production proposers
    should pass strings (Qwen emits to memory).

    R1-finding-3: path-not-found raises SchemaError (not bare
    FileNotFoundError) so Ph1c.2 fast-reject can route it through
    the same exception channel as parse failures.
    """
    if isinstance(source, Path):
        try:
            with open(source, "r") as fh:
                text = fh.read()
        except (OSError, UnicodeDecodeError) as exc:
            # R2-finding-5: cover binary-file paths too. UnicodeDecodeError
            # isn't an OSError subclass, so a JPEG passed as a proposal
            # path would escape as a non-SchemaError without this.
            raise SchemaError(f"path {source}: {exc}") from exc
    else:
        text = source

    # R2-finding-1: count BYTES not chars. `len(text)` is Python
    # char count which under-counts UTF-8 multi-byte chars by 2-3×.
    # The constant name advertises byte semantics; honor it.
    encoded_size = len(text.encode("utf-8"))
    if encoded_size > MAX_PROPOSAL_BYTES:
        raise SchemaError(
            f"proposal text too large: {encoded_size} bytes "
            f"(cap {MAX_PROPOSAL_BYTES})"
        )

    try:
        raw = yaml.load(text, Loader=_DupKeyDetectingLoader)
    except yaml.YAMLError as exc:
        raise SchemaError(f"YAML parse failure: {exc}") from exc

    if not isinstance(raw, dict):
        raise SchemaError(
            f"proposal must be a top-level mapping, got "
            f"{type(raw).__name__}"
        )
    _check_unknown_keys(raw, TOP_LEVEL_KEYS, "<top-level>")

    if "filters" not in raw:
        raise SchemaError("proposal: required key 'filters' missing")
    if "sizing" not in raw:
        raise SchemaError("proposal: required key 'sizing' missing")

    thesis = None
    if "thesis" in raw:
        thesis = _expect_str(raw["thesis"], "thesis")

    filters = _parse_filters(raw["filters"])
    sizing = _parse_sizing(raw["sizing"])
    exclusions = _parse_exclusions(raw.get("exclusions"))

    return ParsedProposal(
        thesis=thesis, filters=filters,
        sizing=sizing, exclusions=exclusions,
    )
