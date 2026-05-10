"""Phase H-2 — Bot microstate forward-capture helper tests.

Tests `bot_state_snapshot.compute_bot_state_snapshot`.

The helper is a pure dict-builder: no SQL, no network. Every assertion
here uses lightweight stub objects that duck-type the MainLoop attributes
the helper reads.

Master plan: kb/decisions/shadow-coverage-phase-h-data-recovery-may02.md
Design doc:  kb/decisions/phase-h2-bot-microstate-fwd-may02.md
"""

from __future__ import annotations

import json
import math
import os
import sys
import time

import pytest


PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

# Helper module under test (moved to repo root in H-2 step 2 to match
# the H-3a pattern — bot/_impl.py imports as a top-level module).
from bot_state_snapshot import (  # noqa: E402
    compute_bot_state_snapshot,
)


# ── Test stubs ──────────────────────────────────────────────────────────

class _StubExecutor:
    def __init__(self, api_errors=None):
        self._ticker_api_errors = api_errors or {}


class _StubFeed:
    def __init__(self, orderbooks=None):
        self._orderbooks = orderbooks if orderbooks is not None else {}


class _StubState:
    def __init__(self, n_positions=0, raise_on_call=False):
        self._n_positions = n_positions
        self._raise = raise_on_call

    def get_open_positions(self, asset=None):
        if self._raise:
            raise RuntimeError("simulated DB lock timeout")
        return [{"ticker": f"X{i}"} for i in range(self._n_positions)]


def _make_stub(*,
               scan_iter=42,
               scan_loop_start=None,
               cooldown_assets=None,
               api_errors=None,
               orderbooks=None,
               n_positions=3):
    """Build a duck-typed MainLoop stand-in."""
    class _Stub:
        pass
    s = _Stub()
    s._scan_iter = scan_iter
    s._scan_loop_start = scan_loop_start if scan_loop_start is not None else time.perf_counter()
    s._cooldown_assets = cooldown_assets if cooldown_assets is not None else set()
    s.executor = _StubExecutor(api_errors=api_errors)
    s.kalshi_feed = _StubFeed(orderbooks=orderbooks)
    s.state = _StubState(n_positions=n_positions)
    return s


# ── Tests ──────────────────────────────────────────────────────────────


def test_snapshot_returns_dict():
    """Helper always returns a dict (never None, never raises)."""
    bot = _make_stub()
    snap = compute_bot_state_snapshot(bot)
    assert isinstance(snap, dict)


def test_snapshot_is_json_serializable():
    """Output must round-trip through json.dumps with allow_nan=False.

    This is the actual SQLite serialization contract: insert site will call
    json.dumps(snapshot) before INSERT. allow_nan=False is enforced because
    NaN/Inf in JSON is non-standard and breaks downstream consumers
    (Postgres jsonb column on supabase mirror rejects NaN).
    """
    bot = _make_stub(
        api_errors={"KXBTC15M-FOO": 2, "KXETH15M-BAR": 1},
        orderbooks={
            "KXBTC15M-A": {"yes": [], "no": [], "ts": time.time() - 0.5},
            "KXETH15M-B": {"yes": [], "no": [], "ts": time.time() - 1.2},
        },
        cooldown_assets={"SOL"},
    )
    snap = compute_bot_state_snapshot(bot, lock_wait_ms=12.5)
    blob = json.dumps(snap, allow_nan=False)
    parsed = json.loads(blob)
    assert parsed["scan_iter"] == snap["scan_iter"]
    # Round-trip preserves nested structure.
    assert parsed["api_error_counts"] == snap["api_error_counts"]


def test_snapshot_handles_missing_attrs():
    """A bare object with no attrs must NOT raise — every field → None/empty.

    This is the production-safety contract: insert path cannot tolerate
    exceptions from this helper. If a refactor renames an attribute,
    behavior degrades to NULL, never crashes the insert.
    """
    class _Bare:
        pass
    snap = compute_bot_state_snapshot(_Bare())
    assert snap["scan_iter"] is None
    assert snap["scan_dt_ms"] is None
    assert snap["active_cooldowns"] == []
    assert snap["api_error_counts"] == {}
    assert snap["ws_cache_age_ms"] == {}
    assert snap["open_positions_count"] is None
    assert snap["lock_wait_ms"] is None


def test_snapshot_includes_required_keys():
    """All 8 schema keys must be present on every call, no exceptions.

    Downstream (v2 calibrator training) joins on key presence; missing
    keys would silently zero-fill features. This guards the contract.
    """
    REQUIRED_KEYS = {
        "schema_version",
        "scan_iter",
        "scan_dt_ms",
        "active_cooldowns",
        "api_error_counts",
        "ws_cache_age_ms",
        "open_positions_count",
        "lock_wait_ms",
    }
    # Even on a totally bare stub, all 8 keys present.
    snap = compute_bot_state_snapshot(object())
    assert set(snap.keys()) == REQUIRED_KEYS, (
        f"Schema drift: got {set(snap.keys())}, want {REQUIRED_KEYS}"
    )
    # schema_version is always 1 for this version. v2 calibrator training
    # MUST pin on this so a silent format flip can't ship without bump.
    assert snap["schema_version"] == 1


def test_snapshot_no_unbounded_growth():
    """Lists/dicts must be bounded by the canonical asset count.

    Adversarial: caller inserts 10K api_error tickers (e.g. cleanup bug
    in upstream) — snapshot must NOT echo all of them, only fold by
    asset prefix. Same for cooldowns and ws_cache_age.
    """
    huge_api_errors = {f"KXBTC15M-{i}": 1 for i in range(5000)}
    huge_obs = {f"KXETH15M-{i}": {"ts": time.time() - 0.1, "yes": [], "no": []}
                for i in range(5000)}
    huge_cooldowns = set(f"NOTREAL{i}" for i in range(5000))
    huge_cooldowns.update({"BTC", "ETH"})  # plus 2 valid

    bot = _make_stub(
        api_errors=huge_api_errors,
        orderbooks=huge_obs,
        cooldown_assets=huge_cooldowns,
    )
    snap = compute_bot_state_snapshot(bot)

    # api_error_counts: at most len(ASSETS) keys (one per canonical asset).
    # T1 (2026-05-10): ASSETS now 6 (added HYPE/DOGE for shadow observation).
    from config import ASSETS
    assert len(snap["api_error_counts"]) <= len(ASSETS)
    assert len(snap["ws_cache_age_ms"]) <= len(ASSETS)
    assert len(snap["active_cooldowns"]) <= len(ASSETS)
    assert set(snap["active_cooldowns"]).issubset(set(ASSETS))


def test_snapshot_handles_nan_inf():
    """NaN/Inf in float fields must not break json.dumps(allow_nan=False).

    Adversarial: a float field receives NaN (e.g. divide-by-zero in
    upstream timing code) or +Inf (overflow). json.dumps with allow_nan=False
    would raise ValueError. Helper must scrub to None instead.
    """
    bot = _make_stub()
    # Pass NaN explicitly via the lock_wait_ms parameter.
    snap_nan = compute_bot_state_snapshot(bot, lock_wait_ms=float("nan"))
    snap_inf = compute_bot_state_snapshot(bot, lock_wait_ms=float("inf"))
    snap_neginf = compute_bot_state_snapshot(bot, lock_wait_ms=float("-inf"))

    for s in (snap_nan, snap_inf, snap_neginf):
        # Must be scrubbed to None.
        assert s["lock_wait_ms"] is None
        # Strict-JSON serialization must succeed.
        blob = json.dumps(s, allow_nan=False)
        assert "NaN" not in blob and "Infinity" not in blob


def test_snapshot_no_pii():
    """Snapshot must NOT echo balance amounts, API keys, order IDs, or ticker IDs.

    Whitelist approach: assert that the returned key set is exactly the 7
    schema keys. No accidental introspection of `vars(bot_self)`. Also
    sanity-check that no nested value contains substrings that would
    indicate API key leakage.
    """
    # Stub that injects "secret-looking" attributes — helper must ignore them.
    class _LeakyStub:
        def __init__(self):
            self.api_key = "sk_live_REDACTED_ABC123"
            self.account_balance_cents = 999_999_99
            self.private_key_pem = "-----BEGIN PRIVATE KEY-----"
            self.recent_order_ids = ["ord_1", "ord_2"]
            # Plus the legitimate attrs.
            self._scan_iter = 1
            self._scan_loop_start = time.perf_counter()
            self._cooldown_assets = set()
            self.executor = _StubExecutor()
            self.kalshi_feed = _StubFeed()
            self.state = _StubState()

    snap = compute_bot_state_snapshot(_LeakyStub())
    blob = json.dumps(snap)
    # No secret-looking strings should appear anywhere in the serialization.
    for forbidden in ("sk_live", "BEGIN PRIVATE", "ord_1", "999999",
                      "api_key", "private_key", "account_balance"):
        assert forbidden not in blob, (
            f"PII leak: {forbidden!r} found in snapshot blob"
        )
    # Keys are EXACTLY the 8 whitelisted ones (incl. schema_version).
    assert set(snap.keys()) == {
        "schema_version",
        "scan_iter", "scan_dt_ms", "active_cooldowns",
        "api_error_counts", "ws_cache_age_ms",
        "open_positions_count", "lock_wait_ms",
    }


def test_compute_under_5ms():
    """Helper must be cheap — target <1ms typical, hard limit <20ms p95.

    Insert path runs at scan rate (~1 Hz × N candidates). A 5ms helper
    cost across 4 candidates would add 20ms to scan latency. Bot's scan
    budget is ~1.5s (SCAN_LOOP_SLOW threshold) so this is non-trivial.

    Round-3 fix: bumped p95 threshold 5ms→20ms with samples 100→1000.
    Shared CI runners produced flaky failures at 5ms; 20ms still catches
    accidental O(n²) regressions or SQL-on-call without false-positives.
    """
    # Realistic-ish stub: 4 assets × 5 tickers each in orderbooks.
    obs = {}
    for a in ("BTC", "ETH", "SOL", "XRP"):
        for i in range(5):
            obs[f"KX{a}15M-{i}"] = {"ts": time.time() - i * 0.1, "yes": [], "no": []}
    bot = _make_stub(
        api_errors={f"KX{a}15M-{i}": (i % 3) for a in ("BTC", "ETH") for i in range(5)},
        orderbooks=obs,
        cooldown_assets={"SOL"},
    )
    # Warm-up.
    compute_bot_state_snapshot(bot)
    # Time 1000 calls; report median per-call.
    samples = []
    for _ in range(1000):
        t0 = time.perf_counter()
        compute_bot_state_snapshot(bot, lock_wait_ms=1.0)
        samples.append((time.perf_counter() - t0) * 1000.0)
    samples.sort()
    p50 = samples[len(samples) // 2]
    p95 = samples[int(len(samples) * 0.95)]
    # 20ms bound — CI runners are slow. We mainly want to catch
    # accidental O(n²) over orderbooks or accidental SQL-on-call regressions.
    assert p95 < 20.0, f"Snapshot p95={p95:.2f}ms exceeds 20ms budget (p50={p50:.2f}ms)"


# ── Bonus regression tests ─────────────────────────────────────────────


def test_snapshot_state_get_open_positions_raises_returns_none():
    """If state.get_open_positions raises (e.g. DB locked), don't propagate.

    Adversarial round 2 finding: the only field that calls SQL must be
    isolated. Verifies the defensive wrapper.
    """
    class _Stub:
        pass
    s = _Stub()
    s._scan_iter = 1
    s._scan_loop_start = time.perf_counter()
    s._cooldown_assets = set()
    s.executor = _StubExecutor()
    s.kalshi_feed = _StubFeed()
    s.state = _StubState(raise_on_call=True)
    snap = compute_bot_state_snapshot(s)
    assert snap["open_positions_count"] is None


def test_snapshot_uses_cached_open_positions_count_when_set():
    """Round-1 adversarial fix: helper prefers a cached count over SQL.

    bot/_impl.py is expected to populate `_open_positions_count_cache` from its
    existing 60s-cached `_compute_bot_state_features` so the helper does
    not issue 50 SELECTs per scan tick.
    """
    bot = _make_stub(n_positions=99)  # state.get_open_positions would say 99
    bot._open_positions_count_cache = 7  # but cache says 7
    snap = compute_bot_state_snapshot(bot)
    # Cache wins — no SQL fallback when cache is set.
    assert snap["open_positions_count"] == 7


def test_snapshot_unbounded_growth_positive_case():
    """Round-1 adversarial fix: bound is ≤4 BUT we must also assert valid
    inputs survive the filter. Otherwise the bound test would still pass
    on a buggy filter that drops everything.
    """
    bot = _make_stub(
        api_errors={"KXBTC15M-1": 3, "KXETH15M-2": 1},
        orderbooks={
            "KXSOL15M-X": {"ts": time.time() - 0.05, "yes": [], "no": []},
            "KXXRP15M-Y": {"ts": time.time() - 0.10, "yes": [], "no": []},
        },
        cooldown_assets={"BTC", "SOL"},
    )
    snap = compute_bot_state_snapshot(bot)
    # Valid inputs survive.
    assert snap["api_error_counts"].get("BTC") == 3
    assert snap["api_error_counts"].get("ETH") == 1
    assert "SOL" in snap["ws_cache_age_ms"]
    assert "XRP" in snap["ws_cache_age_ms"]
    assert set(snap["active_cooldowns"]) == {"BTC", "SOL"}


def test_snapshot_ws_cache_keeps_freshest_per_asset():
    """When two tickers map to one asset, pick the FRESHEST (smallest age)."""
    now = time.time()
    obs = {
        "KXBTC15M-OLD": {"ts": now - 10.0, "yes": [], "no": []},  # 10s old
        "KXBTC15M-NEW": {"ts": now - 0.2, "yes": [], "no": []},   # 200ms old
    }
    bot = _make_stub(orderbooks=obs)
    snap = compute_bot_state_snapshot(bot)
    # Should report the 200ms entry, not the 10s one.
    assert "BTC" in snap["ws_cache_age_ms"]
    assert snap["ws_cache_age_ms"]["BTC"] < 1000.0


# ── Round-3 adversarial regression tests ──────────────────────────────


def test_active_cooldowns_filters_expired():
    """Round-3 critique #2: dict-of-expiries shape — filter expired entries.

    Defensive future-shape: if a refactor changes _cooldown_assets from
    a `set[str]` to `dict[str, expiry_epoch]`, the helper must filter
    out entries whose expiry has already passed (epoch seconds vs
    `time.time()`). Otherwise stale cooldowns leak into v2 features
    until the next sweep runs.

    Current bot/_impl.py shape (bot/_impl.py:11193) is a `set` already filtered by
    SQL `julianday()` window — no expiry timestamps. This test pins the
    defensive code path against future structure changes.
    """
    now = time.time()
    cooldowns_dict = {
        "BTC": now + 60.0,    # active (expires in 60s)
        "ETH": now - 10.0,    # EXPIRED 10s ago
        "SOL": now + 3600.0,  # active (expires in 1h)
        # XRP not in dict
    }
    bot = _make_stub(cooldown_assets=cooldowns_dict)
    snap = compute_bot_state_snapshot(bot)
    # ETH must be filtered out (expired). BTC and SOL should remain.
    assert set(snap["active_cooldowns"]) == {"BTC", "SOL"}
    assert "ETH" not in snap["active_cooldowns"]


def test_active_cooldowns_bounded_by_assets_not_input():
    """Round-3 critique #5: 10K junk strings must NOT iterate before valid.

    With a set of 10K hash-arbitrary-ordered junk strings + 2 valid
    ones, the prior implementation's `if len(out) >= len(assets): break`
    could break on junk before reaching the 2 valid entries. The fix
    rewrites to `sorted(set(snap) & set(assets))`, which is bounded by
    `len(assets)` regardless of input size.
    """
    junk = {f"NOTREAL_{i}_xyz" for i in range(10_000)}
    junk.update({"BTC", "XRP"})  # 2 valid
    bot = _make_stub(cooldown_assets=junk)
    snap = compute_bot_state_snapshot(bot)
    # MUST find both valid entries despite 10K junk noise.
    assert set(snap["active_cooldowns"]) == {"BTC", "XRP"}


def test_ws_cache_age_excludes_24h_old():
    """Round-3 critique #1: clock-domain-mismatch sanity bound.

    If a future bot/_impl.py refactor accidentally writes `time.monotonic()`
    instead of `time.time()` to the orderbook `ts` field, ages computed
    via `time.time() - ts` would be wildly wrong (potentially many
    decades). The 24h sanity bound omits any entry with age > 86_400_000ms
    so v2 calibrator features don't ingest garbage.
    """
    now = time.time()
    obs = {
        "KXBTC15M-OK":     {"ts": now - 0.5, "yes": [], "no": []},          # 500ms — fine
        "KXETH15M-WAYOLD": {"ts": now - (48 * 3600), "yes": [], "no": []},  # 48h — too old
        "KXSOL15M-MONO":   {"ts": time.monotonic(), "yes": [], "no": []},    # monotonic — bogus age
    }
    bot = _make_stub(orderbooks=obs)
    snap = compute_bot_state_snapshot(bot)
    # BTC stays (fresh), ETH dropped (>24h), SOL dropped (monotonic
    # is typically a small number relative to epoch → huge negative
    # diff → clamped to 0 → kept; or huge positive → dropped).
    # The bound makes ETH always omitted regardless.
    assert "BTC" in snap["ws_cache_age_ms"]
    assert "ETH" not in snap["ws_cache_age_ms"]


def test_ticker_to_asset_excludes_hourly_when_filtered_15m():
    """Round-3 critique #4: hourly KXBTCD-... must NOT count into 15M BTC.

    Adversarial: caller has both 15M (KXBTC15M-...) and hourly (KXBTCD-...)
    tickers in api_errors / orderbooks. Without product_type_filter,
    both get folded into the BTC bucket — conflating decision-tick state
    across product types.
    """
    bot = _make_stub(
        api_errors={
            "KXBTC15M-1": 5,   # 15M — should count
            "KXBTCD-2": 100,   # hourly — should NOT count under default filter
            "KXETH15M-3": 2,   # 15M — should count
            "KXETHD-4": 50,    # hourly — should NOT count
        },
    )
    snap = compute_bot_state_snapshot(bot)
    # Default product_type_filter='15m' excludes hourly entries.
    assert snap["api_error_counts"].get("BTC") == 5
    assert snap["api_error_counts"].get("ETH") == 2


def test_ticker_to_asset_can_scope_to_hourly():
    """Round-3 critique #4 (positive case): explicit 'hourly' filter.

    Caller can override product_type_filter='hourly' to scope api_errors
    and ws_cache_age_ms to KX{ASSET}D-... tickers instead.
    """
    bot = _make_stub(
        api_errors={
            "KXBTC15M-1": 5,    # 15M — excluded under hourly filter
            "KXBTCD-2": 100,    # hourly — included
            "KXETHD-3": 50,     # hourly — included
        },
    )
    snap = compute_bot_state_snapshot(bot, product_type_filter="hourly")
    assert snap["api_error_counts"].get("BTC") == 100
    assert snap["api_error_counts"].get("ETH") == 50


def test_lock_wait_ms_rounded_to_3_decimals():
    """Round-3 critique #7: lock_wait_ms precision matches scan_dt_ms.

    scan_dt_ms is rounded to 3 decimals. Without symmetric rounding on
    lock_wait_ms, the JSON blob mixes precision (e.g. 12.345 vs
    12.34567890123). Apply round(., 3) for consistency.
    """
    bot = _make_stub()
    snap = compute_bot_state_snapshot(bot, lock_wait_ms=12.34567890123)
    assert snap["lock_wait_ms"] == 12.346


def test_compute_raises_on_unknown_product_type_filter():
    """Round-4 critique #1: validate product_type_filter at entry.

    Unknown values (e.g. typo "15min", "1h", "weekly") would silently
    fall back to no-filter behavior in `_ticker_to_asset` (token=None),
    conflating 15M and hourly buckets without operator awareness. The
    explicit ValueError forces the caller to use a supported scope.

    Whitelist: {'15m', 'hourly', None}. Anything else raises.
    """
    bot = _make_stub()
    # Each of these unknown values must raise.
    for bad in ("15min", "1h", "weekly", "minute", "", "15M", "Hourly", 0):
        with pytest.raises(ValueError, match="product_type_filter"):
            compute_bot_state_snapshot(bot, product_type_filter=bad)
    # Sanity: the three valid values DO NOT raise.
    for good in ("15m", "hourly", None):
        snap = compute_bot_state_snapshot(bot, product_type_filter=good)
        assert isinstance(snap, dict)


def test_default_assets_matches_bot_py():
    """Round-4 critique #2: drift guard — `_DEFAULT_ASSETS` must mirror
    bot/_impl.py's canonical asset list (sourced from `config.py:ASSETS`).

    bot/_impl.py imports `ASSETS` via `from config import *` (bot/_impl.py:44), so
    the canonical literal lives in `config.py`. AST-parse `config.py` to
    extract its `ASSETS = [...]` literal at module scope and assert that
    the helper's `_DEFAULT_ASSETS` tuple matches.

    Why AST not import: importing `bot` would pull in requests / websocket /
    cryptography and is slow — for a literal-equality check we just walk
    the AST of `config.py` (the upstream-of-bot single source of truth).

    If `ASSETS` ever grows from 4 → 5 (e.g. add DOGE), this test fires
    BEFORE production data has wrong/missing per-asset microstate buckets.
    """
    import ast as _ast

    from bot_state_snapshot import _DEFAULT_ASSETS

    config_path = os.path.join(PROJECT_ROOT, "config.py")
    with open(config_path) as fh:
        tree = _ast.parse(fh.read())

    canonical = None
    for node in tree.body:
        # Look for `ASSETS = <literal>` at module scope.
        if isinstance(node, _ast.Assign):
            for target in node.targets:
                if isinstance(target, _ast.Name) and target.id == "ASSETS":
                    canonical = _ast.literal_eval(node.value)
                    break
        if canonical is not None:
            break

    assert canonical is not None, (
        "Canonical ASSETS literal not found in config.py at module scope. "
        "If it moved, update this drift guard to point at the new home."
    )
    # Coerce both to tuples for comparison (config.py uses a list).
    assert tuple(_DEFAULT_ASSETS) == tuple(canonical), (
        f"_DEFAULT_ASSETS drift: helper has {_DEFAULT_ASSETS!r}, "
        f"config.py has {canonical!r}. Update _DEFAULT_ASSETS in "
        "bot_state_snapshot.py to match."
    )


def test_schema_version_present_and_pinned_to_1():
    """Round-3 critique #8: schema_version field on every snapshot.

    v2 calibrator training MUST pin on this so a silent format flip
    can't ship without an explicit version bump + downstream parser
    update. Always present, always int, currently == 1.
    """
    bot = _make_stub()
    snap = compute_bot_state_snapshot(bot)
    assert "schema_version" in snap
    assert isinstance(snap["schema_version"], int)
    assert snap["schema_version"] == 1
    # Bare-stub case still has it.
    snap_bare = compute_bot_state_snapshot(object())
    assert snap_bare["schema_version"] == 1
