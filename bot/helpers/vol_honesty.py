"""Continuous vol-honesty monitor — Bit V.3 (2026-06-12).

Postmortem ``kb/failures/vol-engine-beta-dvol-deflation-jun12.md``,
lesson **L-VOL-2: every model input with a cheap independent estimate
gets a continuous honesty monitor.** ``blended_rv`` for non-BTC/ETH
assets was ``clamp(beta, 0.5, 3.0) × BTC_DVOL`` for ~4 months; 17
adversarial-review rounds missed it because every in-scope artifact
agreed with the wrong number. Only an out-of-scope independent
measurement (the tape) could catch it — and nothing compared them.
This module is the "something that compares them".

Home choice: the engine-side throttled-diagnostic precedent is the
5-min VRP log in ``bot/engines/volatility.py`` (the ``_rk_last_summary``
per-asset throttle dict inside ``update()``). The honesty monitor does
NOT live there, deliberately: the tape estimate is the INDEPENDENT
measurement, and embedding the comparison inside the engine under audit
would re-create the exact in-scope-agreement blindness the postmortem
describes. It lives at the scanner's V.1 ``_strategy_vol`` seam instead
— the one place where the raw engine estimate (``blended_rv`` before
the ``max()`` selection) and the tape estimate (``trailing_rv300``)
are both in hand — packaged as this helpers-leaf class (stdlib +
``bot.constants`` only, same throttle-dict shape as the VRP log).

Data source: an in-memory deque of per-tick cache pairs rather than a
trailing DB query — the scanner already holds both values each tick, so
re-reading the rows it just wrote through ``insert_evaluated_opportunity``
every 60s would buy nothing but writer-lock contention on the shared
SQLite conn (kb/failures/database-contention.md). The persisted
``tape_rv300`` / ``raw_blended_rv`` columns (same Bit) serve the OFFLINE
soak queries (/live-small); this monitor serves the ONLINE alert.

Semantics:

- ``record(asset, raw_blended_rv, tape_rv300)`` is called once per
  asset per scan tick. Ticks where either estimate is missing (or the
  tape is non-positive) contribute NO sample — an asset with a dead
  tape can never breach (honest abstention; the seam's ``TAPE_RV_NONE``
  log covers that condition separately).
- Every ``CHECK_INTERVAL_S`` (60s) per asset: median of the ratio
  samples inside the trailing ``VOL_HONESTY_WINDOW_S``. Median, not
  mean — a single garbage tick must not trip a 10-minute verdict.
- Median outside ``[VOL_HONESTY_LOW, VOL_HONESTY_HIGH]`` →
  ``logging.warning`` with the ``VOL_HONESTY_BREACH`` signature (fires
  on every breaching 60s check), and a Telegram-ready message is
  RETURNED at most once per ``VOL_HONESTY_ALERT_THROTTLE_S`` per asset.
  The caller (scanner) owns the actual notifier send — keeps this
  module a clean helpers-leaf (no ``bot.notifier`` carve-out needed).
- ``MIN_RATIO_SAMPLES`` guards the median against a thin window
  (restart warmup, sparse scan cadence): fewer pairs → no verdict.

Pinned by ``tests/contracts/test_vol_honesty_instrumentation.py``.
"""
from __future__ import annotations

import logging
import statistics
import time
from collections import deque
from typing import Deque, Dict, Optional, Tuple

from bot.constants import (
    VOL_HONESTY_ALERT_THROTTLE_S,
    VOL_HONESTY_HIGH,
    VOL_HONESTY_LOW,
    VOL_HONESTY_WINDOW_S,
)

# Check cadence — matches the spec's "every 60s per asset" and the V.1
# TAPE_RV_NONE per-asset log throttle at the same seam.
CHECK_INTERVAL_S = 60.0

# Minimum ratio pairs inside the window before the median is a verdict.
# At the ~1-5s scan-tick cadence a healthy asset accumulates 100+ pairs
# per 10-min window; 5 only abstains during warmup / sparse coverage.
MIN_RATIO_SAMPLES = 5

# Defensive deque bound — time-pruning is the real limit (~600 samples
# at 1/s over the 600s window); the maxlen just caps pathological feeds.
_MAX_SAMPLES = 2048


class VolHonestyMonitor:
    """Per-asset trailing-median honesty check of raw engine vol vs tape.

    NOT thread-safe by design — constructed and driven exclusively by
    the scanner thread (same single-writer discipline as the scanner's
    other per-asset dicts, e.g. ``_tape_rv_none_log_ts``).
    """

    def __init__(
        self,
        *,
        low: float = VOL_HONESTY_LOW,
        high: float = VOL_HONESTY_HIGH,
        window_s: float = VOL_HONESTY_WINDOW_S,
        alert_throttle_s: float = VOL_HONESTY_ALERT_THROTTLE_S,
        check_interval_s: float = CHECK_INTERVAL_S,
        min_samples: int = MIN_RATIO_SAMPLES,
    ) -> None:
        self._low = low
        self._high = high
        self._window_s = window_s
        self._alert_throttle_s = alert_throttle_s
        self._check_interval_s = check_interval_s
        self._min_samples = min_samples
        # asset -> deque of (ts, raw_blended_rv / tape_rv300)
        self._samples: Dict[str, Deque[Tuple[float, float]]] = {}
        self._last_check: Dict[str, float] = {}
        self._last_alert: Dict[str, float] = {}

    def record(
        self,
        asset: str,
        raw_blended_rv: Optional[float],
        tape_rv300: Optional[float],
        now: Optional[float] = None,
    ) -> Optional[str]:
        """Feed one tick's estimate pair; return a Telegram message on a
        newly alertable breach, else None.

        WARN-log side effect: ``VOL_HONESTY_BREACH`` fires on EVERY
        breaching 60s check (journal-greppable soak evidence); the
        returned message is additionally throttled to one per
        ``alert_throttle_s`` per asset.
        """
        if now is None:
            now = time.time()
        dq = self._samples.get(asset)
        if dq is None:
            dq = deque(maxlen=_MAX_SAMPLES)
            self._samples[asset] = dq
        if (raw_blended_rv is not None and tape_rv300 is not None
                and tape_rv300 > 0):
            dq.append((now, raw_blended_rv / tape_rv300))
        # Prune outside the trailing window (also handles clock regressions
        # in tests: strictly time-based, no tick counting).
        cutoff = now - self._window_s
        while dq and dq[0][0] < cutoff:
            dq.popleft()
        # 60s per-asset check throttle. None-sentinel (not a 0.0 default):
        # "never checked" must mean "check now", independent of the clock's
        # epoch — a 0.0 default would conflate t=0 with "checked at t=0".
        _last_check = self._last_check.get(asset)
        if (_last_check is not None
                and now - _last_check < self._check_interval_s):
            return None
        self._last_check[asset] = now
        if len(dq) < self._min_samples:
            return None
        median = statistics.median(r for _, r in dq)
        if self._low <= median <= self._high:
            return None
        logging.warning(
            "VOL_HONESTY_BREACH: %s median(raw_blended_rv/tape_rv300)=%.3f "
            "over trailing %.0fs (n=%d) outside [%.2f, %.2f] — engine vol "
            "dishonest vs the tape (L-VOL-2, kb/failures/"
            "vol-engine-beta-dvol-deflation-jun12.md)",
            asset, median, self._window_s, len(dq), self._low, self._high,
        )
        # Same None-sentinel discipline: "never alerted" must alert NOW —
        # a 0.0 default would silently swallow every alert in the first
        # alert_throttle_s of any epoch-near-zero clock (caught RED by
        # test_monitor_deflation_breach_warns_and_returns_message).
        _last_alert = self._last_alert.get(asset)
        if (_last_alert is not None
                and now - _last_alert < self._alert_throttle_s):
            return None
        self._last_alert[asset] = now
        return (
            f"⚠️ VOL_HONESTY_BREACH {asset}: median raw_blended_rv/tape_rv300 "
            f"= {median:.3f} over the last {self._window_s / 60:.0f} min "
            f"(n={len(dq)}), outside [{self._low:.2f}, {self._high:.2f}]. "
            f"Engine vol is dishonest vs the tape — the beta×DVOL deflation "
            f"class (L-VOL-2). Live-small overlays already price off "
            f"max(blended_rv, rv300); main-pipeline raw_prob may be affected."
        )
