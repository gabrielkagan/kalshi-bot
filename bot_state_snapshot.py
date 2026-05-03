"""Phase H-2 — Bot microstate forward-capture helper.

Builds a single JSON-serializable dict capturing the bot's *internal* state
at decision-tick time. Stamped into `evaluated_opportunities.bot_state_snapshot_json`
on every insert by `StateManager.insert_evaluated_opportunity` (integration
shipped in a separate commit; this module is the pure helper).

Design constraints:
  - **Defensive on every field.** A missing attribute, a thread race, or a
    None-typed cache must NEVER raise out of this helper — the insert path
    cannot tolerate exceptions. Each field is wrapped in try/except → None.
  - **Bounded growth.** Per-asset dicts are restricted to the canonical
    `ASSETS` list (4 entries today). No unbounded user-keyed accumulation.
  - **JSON-safe.** Floats are filtered for NaN/Inf before return (json.dumps
    with allow_nan=False is the contract; values that violate it become None).
  - **No PII / no secrets.** Field whitelist is closed. No iteration over
    `vars(bot_self)`. No balance amounts, no API keys, no order IDs.
  - **Cheap.** Target <1ms typical, <5ms worst case (tested). Reads in-memory
    state only — no SQL, no network, no disk.

Schema (returned dict; matches `bot_state_snapshot_json` JSON blob):
  {
    "schema_version": 1,                 # bumped on any breaking field change
    "scan_iter": int | None,             # current scan iteration counter
    "scan_dt_ms": float | None,          # elapsed ms in current scan loop
    "active_cooldowns": [str, ...],      # per-asset loss cooldowns active now
    "api_error_counts": {asset: int},    # per-asset consecutive api_error count (15M-only by default)
    "ws_cache_age_ms": {asset: float},   # per-asset NBBO cache age in ms (best-ticker, 15M-only by default)
    "open_positions_count": int | None,  # total positions across all assets (60s-cached, NOT decision-exact)
    "lock_wait_ms": float | None,        # BEGIN-IMMEDIATE wait time in ms (set by caller)
  }

Provenance map (where each field comes from in bot.py — line numbers as of
the H-2 design read 2026-05-02; offsets may drift on rebases):

  scan_iter            → MainLoop._scan_iter (NEW counter — see Acceptance
                         in design doc; not present today, increment in
                         scan() body when integrating)
  scan_dt_ms           → derived from time.perf_counter() - MainLoop._scan_loop_start
                         (bot.py:11401). At insert time, snapshot the
                         elapsed-so-far reading.
  active_cooldowns     → list of asset strings from MainLoop._cooldown_assets
                         (bot.py:11193). Set populated by LOSS_COOLDOWN
                         logic (bot.py:11187-11206). Confirmed: bot.py
                         currently writes a `set` of asset strings,
                         already-filtered by SQL `julianday()` window —
                         no expiry timestamps stored. If a future refactor
                         changes this to a dict-of-expiries, the helper
                         applies an `if expiry > now` filter defensively.
  api_error_counts     → MainLoop.executor._ticker_api_errors (bot.py:18889).
                         Aggregated by asset (parsed from ticker prefix).
                         Bounded: per-asset, only ASSETS keys returned.
                         Default product_type_filter='15m' restricts to
                         `KX{ASSET}15M-...` tickers; hourly KXBTCD-... and
                         legacy KXBTC-... tickers are EXCLUDED so they
                         don't conflate the 15M bucket. Caller can override
                         via product_type_filter kwarg.
  ws_cache_age_ms      → derived from MainLoop.kalshi_feed._orderbooks (bot.py:5567).
                         Each entry has {"yes": ..., "no": ..., "ts": <epoch>}
                         (bot.py:7099, 7236). Confirmed: bot.py writes
                         `time.time()` (epoch wall-clock seconds), so this
                         helper uses `time.time() - ts` to compute age.
                         For each asset, take age of the freshest entry
                         across that asset's tickers, in ms. Same
                         product_type_filter as api_error_counts applies.
                         Sanity bound: ages > 86_400_000 ms (24h) are
                         omitted (almost certainly a clock-domain bug or
                         fixture artifact).
  open_positions_count → cached at 60s resolution via MainLoop._compute_bot_state_features
                         (bot.py:10898 — `if now - cache.get("ts", 0) < 60`).
                         **NOT decision-tick exact**; v2 training must treat
                         this as an "approximate state" feature, not a
                         precise gate condition.
  lock_wait_ms         → measured by the caller (insert site in bot.py)
                         using the BEGIN IMMEDIATE timing pattern (see
                         "lock_wait_ms semantics" below). Wall-clock delay
                         in ms between issuing `BEGIN IMMEDIATE` and the
                         lock being granted by SQLite. This module accepts
                         it as an optional kwarg.

lock_wait_ms semantics — MANDATED PATTERN
==========================================

The integration commit MUST measure lock_wait_ms using BEGIN IMMEDIATE
timing. SQLite's BEGIN IMMEDIATE acquires the write lock synchronously
(blocking up to PRAGMA busy_timeout=10000ms) BEFORE any data statement
runs, so the wait time is cleanly attributable to lock contention and
not to insert work:

    t0 = time.perf_counter()
    conn.execute("BEGIN IMMEDIATE")
    lock_wait_ms = (time.perf_counter() - t0) * 1000.0
    conn.execute(<the eval-opp INSERT statement>, params)
    conn.execute("COMMIT")

Definition (binding for v2 training): `lock_wait_ms` = "wall-clock delay
between issuing BEGIN IMMEDIATE and the lock being granted, in
milliseconds." Other patterns (timing the full INSERT, timing a
post-execute checkpoint) ARE NOT EQUIVALENT and will produce different
distributions. Operators MUST NOT mix patterns across deploy windows;
do that and v2 calibrator features become apples-to-oranges.

Bound: returned dict has at most ~8 keys × O(ASSETS) inner values. Total
serialized size ~300-500 bytes per row.
"""

from __future__ import annotations

import math
import time
from typing import Any, Dict, List, Optional


# Schema version. Bump on any breaking field rename / removal / type change.
# v2 calibrator training pins on this so a silent format flip can't ship
# without an explicit version bump + downstream parser update.
_SCHEMA_VERSION = 1


# Sentinel used by the (rare) NaN/Inf paths. JSON-serializing None is portable.
_NAN_INF_REPLACEMENT = None


# Hardcoded asset whitelist mirrors bot.py ASSETS. Pinned here so the helper
# stays usable in tests without importing all of bot.py. If bot.py adds an
# asset, update this list AND the test that asserts the contract.
_DEFAULT_ASSETS = ("BTC", "ETH", "SOL", "XRP")


# Sanity bound on ws_cache_age_ms. Anything older than 24h is almost
# certainly a clock-domain mismatch (e.g. caller wrote time.monotonic())
# or a stale test fixture — omit so it doesn't poison v2 features.
_WS_CACHE_AGE_MAX_MS = 86_400_000.0


# Per-product-type ticker substring tokens. _ticker_to_asset uses these
# to restrict aggregation to ONE product_type at a time, preventing
# hourly KXBTCD-... tickers from being counted into the 15M BTC bucket.
# Layout: product_type → required substring after the asset code.
_PRODUCT_TYPE_TOKENS = {
    "15m": "15M",     # KXBTC15M-..., KXETH15M-..., etc.
    "hourly": "D",    # KXBTCD-..., KXETHD-..., etc.
}


def _safe(fn):
    """Run a zero-arg callable; on ANY exception return None.

    Defensive: every field-extractor in this module is wrapped in `_safe`
    so a missing attribute or race-during-iteration doesn't break the
    insert path.
    """
    try:
        return fn()
    except Exception:
        return None


def _scrub_float(v: Any) -> Any:
    """Replace NaN/Inf with None so json.dumps(..., allow_nan=False) succeeds.

    Returns the value unchanged if it's not a problematic float.
    """
    if isinstance(v, float) and (math.isnan(v) or math.isinf(v)):
        return _NAN_INF_REPLACEMENT
    return v


def _ticker_to_asset(ticker: str, assets: tuple,
                     product_type_filter: str = "15m") -> Optional[str]:
    """Best-effort parse: `KXBTC15M-...` → `BTC`. Returns None if unmatched.

    `product_type_filter` restricts which Kalshi market family is recognized:
      - '15m' (default): only KX{ASSET}15M-... tickers match
      - 'hourly':        only KX{ASSET}D-... tickers match
      - None / unknown:  no product-type restriction (legacy behavior; matches
                         any KX{ASSET}* ticker — caller accepts conflation)

    This restriction is the fix for adversarial round-3 critique #4: hourly
    KXBTCD-... and 15M KXBTC15M-... both start with "KXBTC", so without the
    token check, hourly api_errors / ws_cache entries would be conflated
    into the 15M BTC bucket.
    """
    if not isinstance(ticker, str):
        return None
    upper = ticker.upper()
    token = _PRODUCT_TYPE_TOKENS.get(product_type_filter) if product_type_filter else None
    for a in assets:
        # Match either bare prefix or 'KX'+asset prefix; covers Kalshi's
        # ticker conventions (KXBTC, KXETH, KXSOL, KXXRP) without false-
        # positives since assets are 3-letter codes.
        for prefix in (a, "KX" + a):
            if not upper.startswith(prefix):
                continue
            if token is None:
                return a  # no product-type restriction
            # Require the product-type token to follow the asset code,
            # before any '-' separator. e.g. for 15m, "KXBTC15M-..." must
            # contain "15M" between "KXBTC" and the first "-".
            tail = upper[len(prefix):]
            head = tail.split("-", 1)[0]  # part before first dash
            if token == "D":
                # Hourly markers are exactly 'D' (single char). 15M tickers
                # have head='15M' which would NOT equal 'D' — good.
                if head == "D":
                    return a
            elif token in head:
                return a
    return None


def _extract_scan_iter(bot_self: Any) -> Optional[int]:
    val = getattr(bot_self, "_scan_iter", None)
    if isinstance(val, int):
        return val
    return None


def _extract_scan_dt_ms(bot_self: Any) -> Optional[float]:
    """Compute (now - _scan_loop_start) * 1000 in ms, defensively."""
    start = getattr(bot_self, "_scan_loop_start", None)
    if start is None or not isinstance(start, (int, float)):
        return None
    try:
        dt = time.perf_counter() - start
    except Exception:
        return None
    if dt < 0 or dt > 600:
        # Sanity bound: a scan should never run >10 minutes; if it does,
        # the value is almost certainly a stale reference from a prior loop.
        return None
    return _scrub_float(round(dt * 1000.0, 3))


def _extract_active_cooldowns(bot_self: Any, assets: tuple) -> List[str]:
    """Snapshot of currently-cooled-down assets. Bounded to len(assets).

    Handles three concrete shapes:
      1. set/list of asset strings (CURRENT bot.py shape, bot.py:11202)
         — no expiry filter needed because the SQL refresh already filters
         by the LOSS_COOLDOWN_SECONDS time window.
      2. dict of {asset: expiry_epoch_seconds} — defensive future-shape;
         we keep only entries where expiry > now (epoch seconds).
      3. dict of {asset: any_other_value} — treated like a set (use keys).

    The output list is ALWAYS the intersection of the input with the
    canonical `assets` tuple, sorted. This bounds iteration to len(assets),
    no matter how large the input is (adversarial round-3 critique #5:
    10K junk strings in a set would otherwise iterate first under hash
    order before our break-on-len triggered).
    """
    cd = getattr(bot_self, "_cooldown_assets", None)
    if cd is None:
        return []
    # Dict-of-expiries case: filter to non-expired entries first.
    if isinstance(cd, dict):
        now = time.time()
        try:
            keys: List[str] = []
            for k, v in list(cd.items()):
                # If value is a number (epoch seconds), treat as expiry.
                if isinstance(v, (int, float)):
                    if v > now:
                        keys.append(k)
                else:
                    # Non-numeric value → fall back to set semantics.
                    keys.append(k)
            valid = set(keys)
        except Exception:
            return []
    else:
        try:
            valid = set(cd)
        except Exception:
            return []
    # Bound iteration by len(assets), not len(valid). This is the
    # adversarial-round-3 fix: even if `valid` has 10K junk strings,
    # `set(assets)` has 4, and the intersection is at most 4.
    return sorted(valid & set(assets))


def _extract_api_error_counts(bot_self: Any, assets: tuple,
                              product_type_filter: str = "15m") -> Dict[str, int]:
    """Aggregate executor._ticker_api_errors by asset prefix.

    Returns {asset: total_consecutive_errors} for known assets only,
    SCOPED to whichever product_type_filter the caller passes (default '15m').

    Why scoped: Phase H-2 forward-captures decision-tick state for the
    15M bot. Hourly KXBTCD-... tickers must NOT inflate the 15M BTC
    api_error count or v2 features become product-type-conflated.
    Caller can pass product_type_filter='hourly' or None to override.

    Unknown-asset tickers (or wrong-product-type) are silently skipped
    (bounded output).
    """
    executor = getattr(bot_self, "executor", None)
    if executor is None:
        return {}
    err_map = getattr(executor, "_ticker_api_errors", None)
    if not isinstance(err_map, dict):
        return {}
    try:
        items = list(err_map.items())
    except Exception:
        return {}
    by_asset: Dict[str, int] = {a: 0 for a in assets}
    for ticker, count in items:
        a = _ticker_to_asset(ticker, assets, product_type_filter=product_type_filter)
        if a is None:
            continue
        try:
            by_asset[a] += int(count)
        except (TypeError, ValueError):
            continue
    return by_asset


def _extract_ws_cache_age_ms(bot_self: Any, assets: tuple,
                             product_type_filter: str = "15m") -> Dict[str, float]:
    """Per-asset freshest-NBBO age in ms.

    Walks bot_self.kalshi_feed._orderbooks (ticker → {"yes": ..., "no": ...,
    "ts": epoch}). For each asset, finds the MIN age (= freshest) across
    that asset's tickers. Older / missing → entry omitted (None-equivalent).

    CONTRACT: `ts` MUST be `time.time()` (epoch wall-clock seconds), as
    written by bot.py:7099 / 7236 / 7265. This helper computes age via
    `time.time() - ts`. If a future refactor switches the writer to
    `time.monotonic()` or `time.perf_counter()`, ages will be nonsensical
    (potentially negative or wildly large). The 24h sanity bound below
    catches gross mismatches; the unit test pin (test_bot_state_snapshot.py)
    enforces the contract end-to-end.

    Same product_type_filter as api_error_counts: default '15m' restricts
    to KX{ASSET}15M-... tickers so hourly orderbooks don't conflate.
    """
    feed = getattr(bot_self, "kalshi_feed", None)
    if feed is None:
        return {}
    obs = getattr(feed, "_orderbooks", None)
    if not isinstance(obs, dict):
        return {}
    try:
        items = list(obs.items())
    except Exception:
        return {}
    now_epoch = time.time()
    youngest: Dict[str, float] = {}
    for ticker, ob in items:
        if not isinstance(ob, dict):
            continue
        ts = ob.get("ts")
        if not isinstance(ts, (int, float)):
            continue
        a = _ticker_to_asset(ticker, assets, product_type_filter=product_type_filter)
        if a is None:
            continue
        age_ms = (now_epoch - ts) * 1000.0
        if age_ms < 0:
            # Future-dated — clock skew or test fixture; clamp to 0.
            age_ms = 0.0
        if age_ms > _WS_CACHE_AGE_MAX_MS:
            # >24h old — almost certainly a clock-domain mismatch
            # (writer used time.monotonic()) or a stale test fixture.
            # Omit so v2 features don't ingest garbage.
            continue
        # Keep the freshest per asset.
        cur = youngest.get(a)
        if cur is None or age_ms < cur:
            youngest[a] = age_ms
    # Round + scrub.
    return {a: _scrub_float(round(v, 1)) for a, v in youngest.items()
            if _scrub_float(v) is not None}


def _extract_open_positions_count(bot_self: Any) -> Optional[int]:
    """Count of open/pending positions.

    NOTE ON STALENESS: this value is sourced from `_compute_bot_state_features`
    which CACHES at 60s resolution (bot.py:10898 — `if now - cache.get("ts", 0) < 60`).
    The returned count is therefore APPROXIMATE — up to 60 seconds stale.
    It is NOT a decision-tick-exact reading. v2 training that uses this
    feature must treat it as a "rough state" signal, not as a precise gate
    condition. (If decision-tick exactness is needed, a separate Phase H
    sub-task can wire an uncached path — out of scope for H-2.)

    Two-tier resolution:
      1. Read `bot_self._open_positions_count_cache` if present (caller-
         provided cached count — avoids per-insert SQL hit). The integration
         commit in bot.py is expected to populate this from
         `MainLoop._compute_bot_state_features` (60s cache — see bot.py:10898).
      2. Fall back to `state.get_open_positions()` if no cache. Defensive
         against DB exceptions (test stubs may raise).

    Returns None if neither path yields a value.

    NOTE: The fallback is acceptable in tests but should NOT be the hot
    path in production. Round-1 adversarial finding: 50 inserts/scan ×
    SQL round-trip = scan-budget burn. Bot.py must wire the cache.
    """
    cached = getattr(bot_self, "_open_positions_count_cache", None)
    if isinstance(cached, int) and cached >= 0:
        return cached
    state = getattr(bot_self, "state", None) or getattr(bot_self, "_state", None)
    if state is None:
        return None
    getter = getattr(state, "get_open_positions", None)
    if not callable(getter):
        return None
    try:
        positions = getter()
    except Exception:
        return None
    try:
        return int(len(positions))
    except (TypeError, ValueError):
        return None


def compute_bot_state_snapshot(
    bot_self: Any,
    *,
    lock_wait_ms: Optional[float] = None,
    assets: Optional[tuple] = None,
    product_type_filter: str = "15m",
) -> Dict[str, Any]:
    """Build the bot microstate snapshot dict.

    Args:
      bot_self: the MainLoop instance (or a duck-typed test stub). Every
        attribute access is defensive — passing an empty object returns a
        dict with all-None / empty values rather than raising.
      lock_wait_ms: caller-measured DB-lock wait time for the insert this
        snapshot is about to be attached to. Per the docstring at the top
        of this module, this MUST be measured via the BEGIN IMMEDIATE
        timing pattern. The helper does not enforce the unit but applies
        `round(., 3)` so the JSON blob is consistent with the other ms
        floats. Pass None if not measured (column will be None).
      assets: override the canonical asset list. Defaults to BTC/ETH/SOL/XRP.
        Tests can pass a smaller tuple to validate bounded behavior.
      product_type_filter: restricts api_error_counts and ws_cache_age_ms
        aggregation to one Kalshi market family. Default '15m' is the
        Phase H-2 use case (15M decision-tick capture). Pass 'hourly' to
        scope to KXBTCD-... or None to disable filtering. The
        active_cooldowns and open_positions_count fields are NOT scoped
        (cooldowns are per-asset across all product types; positions are
        a global count).

    Returns:
      JSON-safe dict (no NaN/Inf, no objects, all keys are strings).
      Always includes the 8 keys defined in the schema (some may be None
      or empty containers). schema_version is always 1 for this version.
    """
    # Round-4 critique #1: validate product_type_filter early. An unknown
    # value (e.g. typo "15min", "1h", "weekly") would silently skip the
    # token check in `_ticker_to_asset`, since `token = None` and the
    # function falls back to "no product-type restriction" — conflating
    # 15M and hourly buckets without operator awareness. Raising forces
    # the caller to use one of the supported scopes.
    if product_type_filter not in ("15m", "hourly", None):
        raise ValueError(
            f"product_type_filter must be one of {{'15m', 'hourly', None}}, "
            f"got {product_type_filter!r}"
        )

    if assets is None:
        assets = _DEFAULT_ASSETS

    # lock_wait_ms: round to 3 decimals for consistency with scan_dt_ms
    # (other ms float in the blob). Adversarial round-3 critique #7.
    if lock_wait_ms is not None:
        scrubbed_lock_wait = _scrub_float(lock_wait_ms)
        if isinstance(scrubbed_lock_wait, float):
            scrubbed_lock_wait = round(scrubbed_lock_wait, 3)
    else:
        scrubbed_lock_wait = None

    snapshot: Dict[str, Any] = {
        "schema_version": _SCHEMA_VERSION,
        "scan_iter": _safe(lambda: _extract_scan_iter(bot_self)),
        "scan_dt_ms": _safe(lambda: _extract_scan_dt_ms(bot_self)),
        "active_cooldowns": _safe(lambda: _extract_active_cooldowns(bot_self, assets)) or [],
        "api_error_counts": _safe(lambda: _extract_api_error_counts(
            bot_self, assets, product_type_filter=product_type_filter)) or {},
        "ws_cache_age_ms": _safe(lambda: _extract_ws_cache_age_ms(
            bot_self, assets, product_type_filter=product_type_filter)) or {},
        "open_positions_count": _safe(lambda: _extract_open_positions_count(bot_self)),
        "lock_wait_ms": scrubbed_lock_wait,
    }
    return snapshot
