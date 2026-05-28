"""D1.6 — collector health monitor contract pins (ticket 86b9zk4we, 2026-05-17).

NEW `scripts/ops/collector_health_monitor.py` standalone CLI runs via
cron on the VPS. Pre-D1.6 the collector had NO Telegram alert surface
for disk pressure, WS conn-loss, or service-down events (D0.3 §6
enumerated these as known-unalerted classes).

D1.6 closes these via 3 check functions that return Optional[str]
alert messages, dispatched through the existing
`bot.notifier.TelegramNotifier` (no re-implementation of the Telegram
client).

This file pins the public-API shape + threshold defaults + canonical
alert phrasings. Drift would silently reopen any of the 3 alert
classes.

Pins:
  1. The 3 check functions exist with the documented signatures.
  2. Default thresholds match D1.6 plan (80%, 5min/10count).
  3. Alert messages include diagnostic context (paths, counts, recovery hints).
  4. The monitor's `main()` imports `TelegramNotifier` from `bot.notifier`
     and does NOT re-implement the Telegram client.
  5. `main()` returns 0 even on alert-fire (cron convention).
"""
from __future__ import annotations

import inspect
from unittest.mock import MagicMock, patch


def test_check_disk_signature():
    """`check_disk(path=..., threshold_pct=...)` exists with documented kwargs."""
    from scripts.ops.collector_health_monitor import check_disk
    sig = inspect.signature(check_disk)
    assert "path" in sig.parameters
    assert "threshold_pct" in sig.parameters
    # Default threshold = 80% per D1.6 plan (matches D0.3 §6 disk-pressure floor).
    assert sig.parameters["threshold_pct"].default == 80, (
        f"check_disk default threshold_pct should be 80 (per D1.6 plan); "
        f"got {sig.parameters['threshold_pct'].default}"
    )


def test_check_disk_silent_below_threshold():
    """When disk usage is below threshold, return None (no alert)."""
    from scripts.ops.collector_health_monitor import check_disk
    # Use a forced-high threshold so even a full disk wouldn't trip.
    result = check_disk(threshold_pct=200)
    assert result is None, f"Expected None below threshold; got {result!r}"


def test_check_disk_alerts_above_threshold():
    """When disk usage exceeds threshold, return alert string with usage details."""
    from scripts.ops.collector_health_monitor import check_disk
    # Forced-low threshold (0%) so any disk usage trips it.
    result = check_disk(threshold_pct=0)
    assert result is not None, "Expected alert at threshold=0 (any usage trips)"
    assert "DISK ALERT" in result, f"Alert message missing 'DISK ALERT': {result!r}"
    assert "%" in result, f"Alert message missing usage percentage: {result!r}"
    assert "threshold" in result.lower(), (
        f"Alert message should mention threshold for operator context: {result!r}"
    )


def test_check_ws_reconnects_signature():
    """`check_ws_reconnects(window_min=..., threshold_count=..., unit=..., log_marker=...)` exists.

    D2.5 R2-C1 added the `log_marker` kwarg so the dual-tier dispatch
    can target the Coinbase wire's distinct `coinbase_ws_disconnected`
    marker. Pre-R2-C1 the substring filter was hardcoded to
    `kalshi_ws_disconnected`, silently never-matching Coinbase logs.
    """
    from scripts.ops.collector_health_monitor import check_ws_reconnects
    sig = inspect.signature(check_ws_reconnects)
    assert "window_min" in sig.parameters
    assert "threshold_count" in sig.parameters
    assert "unit" in sig.parameters
    assert "log_marker" in sig.parameters, (
        "check_ws_reconnects missing `log_marker` kwarg added at D2.5 "
        "R2-C1. Without it, the Coinbase tier's reconnect-storm alert "
        "is silently broken — the hardcoded substring filter would "
        "never match Coinbase wire logs."
    )
    # Per D1.6 plan: 10 disconnects per 5 min = ~2/min cadence trips alert.
    assert sig.parameters["window_min"].default == 5
    assert sig.parameters["threshold_count"].default == 10
    assert sig.parameters["log_marker"].default == "kalshi_ws_disconnected", (
        "log_marker default must be Kalshi-tier value so pre-D2.5 "
        "callers (and tests) preserve their existing behavior."
    )


def test_check_ws_reconnects_silent_when_journalctl_unavailable():
    """When journalctl is absent or errors, return None (don't alert-spam)."""
    from scripts.ops.collector_health_monitor import check_ws_reconnects
    # Use a unit name unlikely to exist in any test environment; journalctl
    # may return empty output (count=0 < threshold) or fail (returns None).
    # Either way: None or non-alert string.
    result = check_ws_reconnects(unit="definitely-not-a-real-unit-d1-6-test")
    assert result is None, (
        f"Expected None when no disconnects found; got {result!r}. "
        f"Alert-spam on absent unit would breach plan §Risk register."
    )


def test_check_ws_reconnects_alert_includes_class_breakdown():
    """Verify alert string format would include 1006/1009/1011 class
    breakdown when disconnects are above threshold.

    Uses subprocess mock to inject a synthetic journalctl response.
    """
    from scripts.ops import collector_health_monitor as mod
    fake_journal = "\n".join(
        ["kalshi_ws_disconnected: reason=sent 1011 (internal error)..."] * 15
    )
    with patch.object(mod.subprocess, "check_output", return_value=fake_journal):
        result = mod.check_ws_reconnects(threshold_count=10)
    assert result is not None, "Expected alert with 15 disconnects > threshold 10"
    assert "RECONNECT STORM" in result, (
        f"Alert message missing 'RECONNECT STORM' marker: {result!r}"
    )
    assert "1011" in result, (
        f"Alert message must include 1011 class count: {result!r}"
    )


def test_check_collector_active_signature():
    """`check_collector_active(unit=...)` exists with documented default."""
    from scripts.ops.collector_health_monitor import check_collector_active
    sig = inspect.signature(check_collector_active)
    assert "unit" in sig.parameters
    assert sig.parameters["unit"].default == "kalshi-collector"


def test_check_collector_active_silent_when_systemctl_absent():
    """systemctl absent (test env) → None, not crash."""
    from scripts.ops.collector_health_monitor import check_collector_active
    # In test env, systemctl IS usually present but no kalshi-collector
    # unit exists → exit 3 (inactive) → alert WOULD fire. To make this
    # test environment-independent, use a unit name unlikely to be active.
    result = check_collector_active(unit="definitely-not-a-real-unit-d1-6-test")
    # Either None (systemctl absent) or an alert string about the unit
    # being not-active. Both are valid; the test pins the function doesn't
    # CRASH.
    assert result is None or isinstance(result, str), (
        f"Expected None or alert string; got {type(result).__name__}"
    )


def test_main_uses_bot_notifier_telegram_notifier():
    """`main()` MUST import `TelegramNotifier` from `bot.notifier` and use
    it for alert dispatch — NOT re-implement the Telegram client.

    Reuses the bot's battle-tested notifier (dedup + threading +
    Markdown formatting). Re-implementation risks divergence (different
    dedup semantics, missing error handling).
    """
    from scripts.ops import collector_health_monitor as mod
    src = inspect.getsource(mod)
    # Either import-form is acceptable.
    has_import = (
        "from bot.notifier import TelegramNotifier" in src
        or "import bot.notifier" in src
    )
    assert has_import, (
        "scripts/ops/collector_health_monitor.py MUST import TelegramNotifier "
        "from bot.notifier (no re-implementation). See D1.6 plan § Scope."
    )


def test_main_returns_zero_per_cron_convention():
    """`main()` MUST return 0 even when alerts fire (cron convention —
    alerts go via Telegram, not exit code, so cron doesn't email the
    operator's spool on every transient threshold breach).
    """
    from scripts.ops import collector_health_monitor as mod
    # Mock TelegramNotifier so we don't actually try to send.
    with patch.object(mod, "__name__", "scripts.ops.collector_health_monitor"):
        with patch("bot.notifier.TelegramNotifier") as MockNotifier:
            MockNotifier.return_value = MagicMock()
            rc = mod.main()
    assert rc == 0, f"main() must return 0 per cron convention; got {rc}"


# ─── D2.5 dual-tier dispatch (ticket 86b9znq4w, 2026-05-18) ─────────────


def test_coinbase_tier_constants_exist():
    """D2.5 extends the monitor to poll kalshi-coinbase-collector as a
    SECOND tier alongside kalshi-collector. The 4 Coinbase-side
    constants must be defined at module scope so a future refactor
    cannot silently lose Coinbase coverage by renaming + leaving the
    kalshi side stranded.
    """
    from scripts.ops import collector_health_monitor as mod
    assert mod.COINBASE_BRONZE_ROOT == "/var/lib/kalshi-coinbase-collector", (
        f"COINBASE_BRONZE_ROOT must point at the Coinbase-side bronze "
        f"mount per D2.5 Option B isolation; got "
        f"{mod.COINBASE_BRONZE_ROOT!r}."
    )
    assert mod.COINBASE_COLLECTOR_UNIT == "kalshi-coinbase-collector", (
        f"COINBASE_COLLECTOR_UNIT must match the systemd unit name; "
        f"got {mod.COINBASE_COLLECTOR_UNIT!r}."
    )
    assert mod.COINBASE_SIDECAR_PATH == (
        "/var/lib/kalshi-coinbase-collector/bronze_health.json"
    ), (
        f"COINBASE_SIDECAR_PATH must point at the Coinbase-side "
        f"bronze_health.json written by collector.coinbase_main_loop; "
        f"got {mod.COINBASE_SIDECAR_PATH!r}."
    )
    assert mod.COINBASE_MONITOR_STATE_PATH == (
        "/var/lib/kalshi-coinbase-collector/monitor_state.json"
    ), (
        f"COINBASE_MONITOR_STATE_PATH must be Coinbase-side (separate "
        f"from Kalshi monitor_state.json to keep dropped-frames "
        f"accumulation independent); got "
        f"{mod.COINBASE_MONITOR_STATE_PATH!r}."
    )


def test_main_resolves_coinbase_sidecar_path_via_env_var():
    """R4-M2 + R5-M1 regression pin: `main()` MUST mirror the writer's
    two-knob derivation EXACTLY when resolving the Coinbase sidecar
    path. The writer side
    (``collector/coinbase_main_loop.py::run``):

      1. ``COINBASE_HEALTH_SIDECAR_PATH`` env var when set, else
      2. ``bronze_root.parent / "bronze_health.json"`` (where
         ``bronze_root`` comes from ``COINBASE_BRONZE_ROOT`` env var
         or default), else
      3. canonical hardcoded path.

    Pre-R4-M2 only step (1) was env-aware on the reader side. R5-M1
    found step (2) still hardcoded — an operator who relocated bronze
    via ``COINBASE_BRONZE_ROOT`` alone would silently break the
    monitor. Post-R5-M1 both knobs are mirrored.

    AST scan checks for BOTH env-var references on the reader side.
    Accepts either ``os.environ.get`` or ``os.getenv`` (functionally
    equivalent stdlib forms; R5-N1 broadening to avoid false-fail on
    refactor).
    """
    from scripts.ops import collector_health_monitor as mod
    src = inspect.getsource(mod)
    has_env_resolver = "os.environ.get(" in src or "os.getenv(" in src
    assert has_env_resolver, (
        "main() must resolve env vars at runtime via os.environ.get or "
        "os.getenv. Pure-constant resolution would re-introduce the "
        "writer/reader path drift that R4-M2 + R5-M1 closed."
    )
    assert '"COINBASE_HEALTH_SIDECAR_PATH"' in src, (
        "main() must resolve COINBASE_HEALTH_SIDECAR_PATH env var with "
        "COINBASE_SIDECAR_PATH fallback (R4-M2). Without it, the "
        "writer + monitor would reference different paths when an "
        "operator relocates the sidecar."
    )
    assert '"COINBASE_BRONZE_ROOT"' in src, (
        "main() must ALSO mirror the writer's bronze-root-derived "
        "sidecar fallback (R5-M1). Without reading COINBASE_BRONZE_ROOT "
        "on the reader side, an operator who relocates ONLY the bronze "
        "root (no HEALTH_SIDECAR_PATH override) would silently break "
        "the monitor — writer derives `<new-root>/bronze_health.json`, "
        "reader keeps the hardcoded /var/lib/... path."
    )


def test_main_passes_coinbase_log_marker_to_check_ws_reconnects():
    """R2-C1 regression pin: `main()` MUST pass
    `log_marker="coinbase_ws_disconnected"` to the Coinbase-tier
    `check_ws_reconnects` invocation.

    Pre-R2-C1 the hardcoded `"kalshi_ws_disconnected"` substring filter
    would silently never match Coinbase logs, producing always-OK signals
    even during a sustained Coinbase reconnect storm. AST scan over the
    module source confirms the kwarg is wired.
    """
    from scripts.ops import collector_health_monitor as mod
    src = inspect.getsource(mod)
    assert 'log_marker="coinbase_ws_disconnected"' in src, (
        "main() must pass `log_marker=\"coinbase_ws_disconnected\"` to "
        "the Coinbase-tier check_ws_reconnects call. Without it, the "
        "Coinbase reconnect-storm alert is silently broken (the hardcoded "
        "kalshi_ws_disconnected substring filter never matches Coinbase "
        "wire logs)."
    )


def test_main_polls_both_collectors_with_distinct_dedup_keys():
    """`main()` MUST dispatch checks for BOTH kalshi-collector AND
    kalshi-coinbase-collector, with DIFFERENT dedup-key prefixes so
    a Kalshi alert does NOT dedup-suppress an in-flight Coinbase alert
    (their underlying mount points are structurally separate per the
    Option B isolation posture).

    Captures notifier.send() invocations to assert the per-tier dedup
    prefix is correctly applied. The check functions return None in
    the test env (no journalctl / no sidecar / no real disk pressure),
    so we force one alert per tier via patches and inspect the dedup
    keys.
    """
    from scripts.ops import collector_health_monitor as mod

    mock_notifier = MagicMock()
    sent_calls: list[tuple] = []

    def _capture_send(message, dedup_key=None):
        sent_calls.append((message, dedup_key))

    mock_notifier.send.side_effect = _capture_send

    # Force one alert per check by stubbing the 4 check functions to
    # return a fixed alert string. We do this at module level so BOTH
    # tier dispatches see the alert (the lambdas in main() call the
    # module-level functions).
    def _alert(*args, **kwargs):
        return "FORCED-ALERT-FOR-TEST"

    with patch("bot.notifier.TelegramNotifier", return_value=mock_notifier):
        with patch.object(mod, "check_disk", _alert), \
             patch.object(mod, "check_ws_reconnects", _alert), \
             patch.object(mod, "check_collector_active", _alert), \
             patch.object(mod, "check_dropped_frames", _alert):
            mod.main()

    # Post-B2a-1 (2026-05-28, ticket 86ba1zf5j): 4 checks × 3 WS-collector
    # tiers (Kalshi + Coinbase + Venue-L2) + 3 checks × 2 HTTP-poll tiers
    # (Weather + ESPN) + bot tier OK = 18.
    assert len(sent_calls) == 18, (
        f"Expected 18 alert dispatches (4×3 WS-collector + 3×2 "
        f"HTTP-poll-collector + 0 bot); got {len(sent_calls)}. Dispatch "
        f"loop may have lost a tier."
    )

    dedup_keys = [k for _, k in sent_calls]
    kalshi_keys = [k for k in dedup_keys if k and k.startswith("d1_6_")]
    coinbase_keys = [k for k in dedup_keys if k and k.startswith("d2_5_")]
    weather_keys = [k for k in dedup_keys if k and k.startswith("d1_8_")]
    espn_keys = [k for k in dedup_keys if k and k.startswith("d1_11_")]
    venue_l2_keys = [k for k in dedup_keys if k and k.startswith("b2a_")]
    assert len(kalshi_keys) == 4
    assert len(coinbase_keys) == 4
    assert len(weather_keys) == 3
    assert len(espn_keys) == 3, (
        f"Expected 3 dedup keys with `d1_11_` prefix (ESPN side, same "
        f"HTTP-poll subset NO ws_reconnects); got {len(espn_keys)}: "
        f"{espn_keys}. The D1.11.a ESPN-tier dispatch was lost or "
        f"its dedup-key prefix regressed."
    )
    assert len(venue_l2_keys) == 4, (
        f"Expected 4 dedup keys with `b2a_` prefix (Venue-L2 side, FULL "
        f"WS subset INCL ws_reconnects); got {len(venue_l2_keys)}: "
        f"{venue_l2_keys}. The B2a-1 venue-L2-tier dispatch was lost or "
        f"its dedup-key prefix regressed."
    )

    kalshi_check_names = {k.removeprefix("d1_6_") for k in kalshi_keys}
    coinbase_check_names = {k.removeprefix("d2_5_") for k in coinbase_keys}
    venue_l2_check_names = {k.removeprefix("b2a_") for k in venue_l2_keys}
    assert kalshi_check_names == coinbase_check_names
    # Venue-L2 is a WS collector → same FULL 4-check set as Kalshi/Coinbase.
    assert venue_l2_check_names == kalshi_check_names, (
        f"Venue-L2-tier check set mismatch. Got: {venue_l2_check_names}; "
        f"expected (Kalshi WS set): {kalshi_check_names}. The B2a-1 "
        f"venue-L2 tier runs 3 persistent WS conns so it must include "
        f"ws_reconnects (unlike the HTTP-poll weather/ESPN tiers)."
    )
    expected_http_checks = {"disk", "collector_active", "dropped_frames"}
    weather_check_names = {k.removeprefix("d1_8_") for k in weather_keys}
    espn_check_names = {k.removeprefix("d1_11_") for k in espn_keys}
    assert weather_check_names == expected_http_checks
    assert espn_check_names == expected_http_checks, (
        f"ESPN-tier check set mismatch. Got: {espn_check_names}; "
        f"expected: {expected_http_checks}. The D1.11.a ESPN tier "
        f"subset (NO ws_reconnects) must mirror D1.8 weather subset."
    )


# ── RCA-F boot-grace pins (umbrella `86ba12rf0`, ticket `86ba12xr6`) ─────────


def test_check_dropped_frames_accepts_unit_and_boot_grace_kwargs():
    """``check_dropped_frames`` exposes ``unit`` + ``boot_grace_seconds`` kwargs.

    Both are required so the caller (the main() per-tier dispatch loop)
    can pass the per-collector unit name + grace-period override. Pinned
    so a future signature refactor that drops them silently disables
    the boot-grace surface.
    """
    import inspect
    from scripts.ops.collector_health_monitor import check_dropped_frames

    sig = inspect.signature(check_dropped_frames)
    assert "unit" in sig.parameters, (
        "check_dropped_frames must accept `unit` kwarg so the dispatch "
        "loop can pass per-collector unit names (kalshi-collector, "
        "kalshi-coinbase-collector, etc.)."
    )
    assert "boot_grace_seconds" in sig.parameters, (
        "check_dropped_frames must accept `boot_grace_seconds` kwarg "
        "so the boot-grace window is tunable (default 1200s = 20 min)."
    )


def test_default_boot_grace_seconds_is_1200():
    """``DEFAULT_BOOT_GRACE_SECONDS = 1200`` matches the ~17-min boot window.

    Boot sequence per umbrella ticket: salvage (1min) + REST snapshot
    (10 min for 754K-ticker pagination) + per-conn wire-up (60s × 7
    conns ≈ 7 min) ≈ 17 min. 1200s (20 min) gives a small safety margin
    above the observed worst-case + accounts for boot-time jitter under
    CPU load.
    """
    from scripts.ops.collector_health_monitor import DEFAULT_BOOT_GRACE_SECONDS
    assert DEFAULT_BOOT_GRACE_SECONDS == 1200, (
        f"DEFAULT_BOOT_GRACE_SECONDS=1200 covers the observed ~17-min "
        f"boot window + safety margin; got {DEFAULT_BOOT_GRACE_SECONDS}. "
        f"A tighter value would re-introduce STALE false-positives "
        f"during boot; a looser value would mask real wedged-drain "
        f"events during the grace window."
    )


def test_check_dropped_frames_suppresses_stale_alert_during_boot_grace(
    tmp_path, monkeypatch,
):
    """STALE alert is SUPPRESSED when the collector unit's uptime is below
    ``boot_grace_seconds`` — the sidecar's staleness is explained by the
    in-progress boot.

    Without this suppression, every collector restart fires a STALE
    Telegram alert at the next monitor tick (~5 min after the prior
    sidecar's last write). On 2026-05-19 this happened 3+ times in 5h
    (umbrella `86ba12rf0`) — pure false-positive noise.
    """
    import time as _time
    from scripts.ops import collector_health_monitor as mod

    # Stale sidecar (mtime well past the 120s threshold).
    sidecar = tmp_path / "bronze_health.json"
    state = tmp_path / "monitor_state.json"
    sidecar.write_text('{"schema_version": 1, "total_dropped_frames": 0}')
    old_mtime = _time.time() - 600  # 10 min ago
    os_utime_supports_ns = True
    try:
        import os
        os.utime(sidecar, (old_mtime, old_mtime))
    except OSError:
        os_utime_supports_ns = False

    # Patch the uptime helper to report the unit as freshly-booted
    # (uptime well below the grace window).
    monkeypatch.setattr(
        mod, "_collector_uptime_seconds",
        lambda _unit: 300.0,  # 5 min — below the 1200s grace
    )

    result = mod.check_dropped_frames(
        sidecar_path=sidecar, state_path=state,
        unit="kalshi-collector", boot_grace_seconds=1200,
    )
    assert result is None, (
        f"STALE alert should be SUPPRESSED during boot grace (uptime=300s "
        f"< boot_grace_seconds=1200s); got alert={result!r}. The grace "
        f"closes the false-positive class flagged by ticket 86ba12xr6."
    )


def test_check_dropped_frames_fires_stale_alert_after_boot_grace(
    tmp_path, monkeypatch,
):
    """STALE alert FIRES when the collector unit has been up longer than
    the grace window AND the sidecar is stale — distinguishes real
    wedged-drain from in-progress-boot.
    """
    import os
    import time as _time
    from scripts.ops import collector_health_monitor as mod

    sidecar = tmp_path / "bronze_health.json"
    state = tmp_path / "monitor_state.json"
    sidecar.write_text('{"schema_version": 1, "total_dropped_frames": 0}')
    old_mtime = _time.time() - 600
    os.utime(sidecar, (old_mtime, old_mtime))

    # Unit up for 30 min — well past the 20-min grace.
    monkeypatch.setattr(
        mod, "_collector_uptime_seconds",
        lambda _unit: 1800.0,
    )

    result = mod.check_dropped_frames(
        sidecar_path=sidecar, state_path=state,
        unit="kalshi-collector", boot_grace_seconds=1200,
    )
    assert result is not None, (
        "STALE alert MUST fire post-grace when the sidecar is "
        "actually stale — otherwise wedged-drain events would silently "
        "go un-alerted forever."
    )
    assert "STALE" in result


def test_check_dropped_frames_fires_stale_alert_when_uptime_unknown(
    tmp_path, monkeypatch,
):
    """When ``_collector_uptime_seconds`` returns None (systemctl absent /
    test env), the grace check fail-opens and the STALE alert fires
    normally.

    Fail-open posture: if we can't confirm we're in the grace window,
    treat as "post-grace" and alert. Better to ALERT on a real wedged-
    drain in a test/devbox where systemctl is missing than to silently
    skip the alert.
    """
    import os
    import time as _time
    from scripts.ops import collector_health_monitor as mod

    sidecar = tmp_path / "bronze_health.json"
    state = tmp_path / "monitor_state.json"
    sidecar.write_text('{"schema_version": 1, "total_dropped_frames": 0}')
    old_mtime = _time.time() - 600
    os.utime(sidecar, (old_mtime, old_mtime))

    # Helper returns None — uptime unknown.
    monkeypatch.setattr(
        mod, "_collector_uptime_seconds",
        lambda _unit: None,
    )

    result = mod.check_dropped_frames(
        sidecar_path=sidecar, state_path=state,
    )
    assert result is not None, (
        "When uptime is unknown, the grace check must fail-OPEN "
        "(treat as post-grace) so a real wedged-drain still alerts."
    )
    assert "STALE" in result


def test_main_passes_per_tier_unit_to_check_dropped_frames():
    """Every tier dispatcher MUST pass ``unit=<tier_unit>`` to
    check_dropped_frames so the boot-grace check queries the right
    systemd unit's ``ActiveEnterTimestamp``.

    R2-M1 fix (2026-05-20): without this regression guard, a future
    refactor that drops the ``unit=`` kwarg from any tier's closure
    would silently re-introduce R1-M1 cross-coupling (Coinbase /
    Weather / ESPN STALE alerts suppressed during Kalshi's 20-min boot
    window instead of their own).
    """
    import inspect
    from scripts.ops import collector_health_monitor as mod
    src = inspect.getsource(mod.main)

    # Each tier's check_dropped_frames invocation must include
    # unit=<TIER>_COLLECTOR_UNIT (or DEFAULT_COLLECTOR_UNIT for Kalshi).
    # We grep the source rather than mock-recording calls because the
    # dispatcher closures are lambdas that resolve at call time —
    # easier to pin the literal source than monkey-patch the lookup.
    expected_unit_kwargs = [
        "unit=DEFAULT_COLLECTOR_UNIT",
        "unit=COINBASE_COLLECTOR_UNIT",
        "unit=WEATHER_COLLECTOR_UNIT",
        "unit=ESPN_COLLECTOR_UNIT",
    ]
    for kwarg in expected_unit_kwargs:
        assert kwarg in src, (
            f"Expected {kwarg!r} in main()'s tier dispatcher source — "
            f"missing it would cross-couple the boot-grace check to the "
            f"wrong tier's unit (R1-M1 / R2-M1 regression class)."
        )


def test_check_dropped_frames_schema_alert_fires_during_boot_grace(
    tmp_path, monkeypatch,
):
    """SCHEMA-mismatch alert MUST fire during boot grace.

    R1-C1 fix (2026-05-20): the boot-grace branch must skip ONLY the
    STALE alert. SCHEMA + DROPS checks below read the file's CONTENT
    (not its mtime) and would silently regress observability for 20-
    min windows if short-circuited.

    Setup: stale sidecar with WRONG schema_version, unit freshly booted
    (uptime within grace). Expect: SCHEMA alert fires (NOT silent).
    """
    import os
    import time as _time
    from scripts.ops import collector_health_monitor as mod

    sidecar = tmp_path / "bronze_health.json"
    state = tmp_path / "monitor_state.json"
    # Schema-mismatch sidecar (schema_version=99, not 1).
    sidecar.write_text('{"schema_version": 99, "total_dropped_frames": 0}')
    old_mtime = _time.time() - 600  # 10 min ago (also triggers STALE)
    os.utime(sidecar, (old_mtime, old_mtime))

    # Unit freshly booted — STALE would be skipped by grace.
    monkeypatch.setattr(
        mod, "_collector_uptime_seconds",
        lambda _unit: 300.0,  # 5 min — within grace
    )

    result = mod.check_dropped_frames(
        sidecar_path=sidecar, state_path=state,
        unit="kalshi-collector", boot_grace_seconds=1200,
    )
    assert result is not None, (
        "SCHEMA alert MUST fire during boot grace — the grace skips "
        "ONLY the STALE alert. R1-C1 regression guard: a short-circuit "
        "`return None` inside the grace branch would silently disable "
        "SCHEMA detection for 20-min windows."
    )
    assert "SCHEMA" in result, (
        f"Expected SCHEMA alert; got {result!r}. The grace-branch fix "
        f"must let control flow continue to the SCHEMA + DROPS checks."
    )


def test_collector_uptime_seconds_returns_none_for_invalid_unit():
    """``_collector_uptime_seconds(unit)`` returns None for a unit that
    doesn't exist, NOT an exception.

    The grace check fail-opens on None — silent-crash semantics inside
    the helper would propagate to the cron caller, which (per
    `feedback_monitor_the_monitor`) must always exit 0.
    """
    from scripts.ops.collector_health_monitor import _collector_uptime_seconds
    result = _collector_uptime_seconds("definitely-not-a-real-unit-86ba12xr6")
    assert result is None, (
        f"_collector_uptime_seconds must return None for invalid units; "
        f"got {result!r}. The cron-caller relies on None-safe behavior "
        f"so check_dropped_frames can fail-open to STALE alerts."
    )
