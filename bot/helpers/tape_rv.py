"""Trailing tape realized-vol — EXACT parity with the validated backtests.

Bit V.1 (2026-06-12, gates the live-small re-arm). Postmortem:
``kb/failures/vol-engine-beta-dvol-deflation-jun12.md`` — ``blended_rv``
for non-BTC/ETH assets is, in most regimes, ``clamp(beta, 0.5, 3.0) ×
BTC_DVOL`` rather than a realized-vol estimate, so 10/11 day-one
live-small fills failed the validated entry rule under honest vol.
Lesson L-VOL-1: **estimator parity is a contract** — if a backtest
conditions on estimator X, the live gate must compute X (same formula,
shared helper, AST-pinned; the vol twin of the cal_mlp
feature-transform lock-step rule).

Formula provenance (function-name anchors, not line numbers):

- ``scripts/research/genhunt/02_longshot_tick_floor.py::_rv`` (+ its
  ``_at`` staleness read) — the validated longshot entry rule's
  ``rv_5s``: sample the last-known price on a ``step_s`` grid over
  ``[now - window_s, now]`` with a per-grid-point staleness guard
  (``STALE_S = 30.0`` in that script), take per-step log returns,
  SAMPLE stdev.
- ``scripts/research/genhunt/02b_longshot_fillable_validation.py::_rv_pure``
  (+ its ``_at`` staleness read; ``STALE_S = 30.0`` there too) — the
  TRACKED in-repo citation (neither 02 nor fairvalue_extract is
  committed; 02b is). 02's fill-validation sibling carrying the SAME
  grid/staleness/sample-stdev formula, restructured to a pure
  recompute: 02's memo-cache keyed ``(id(tl), t // 5)`` while the
  value depended on exact ``t``, making results call-order-dependent
  (see the "signal" section comment in 02b) — ``_rv_pure`` recomputes
  per call and is bit-stable by construction, exactly like this
  helper.
- ``scripts/research/fairvalue_extract.py::_realized_vol`` — same grid
  construction with two NAMED divergences (R1-MN3): (a) no staleness
  guard (its ``_spot_at`` has none; the guard here is the 02 superset,
  and 02 is the script the kill criteria were evaluated on); (b)
  non-positive-price handling — fairvalue_extract ABORTS (``if s is
  None or s <= 0: return None`` at the first non-positive grid
  sample), whereas 02 PAIR-SKIPS (filters out log returns whose
  DENOMINATOR ``samples[i-1]`` is non-positive and would raise an
  uncaught ``ValueError`` on a non-positive numerator over a positive
  denominator). This helper follows 02's pair-skip, with a defensive
  ``None`` in place of 02's uncaught ``ValueError`` (see step 2 of the
  function docstring).

**Sample (not population) stdev**: both research scripts divide the
squared deviations by ``len(rets) - 1``
(``var = sum((r - m) ** 2 for r in rets) / (len(rets) - 1)`` in
``fairvalue_extract._realized_vol`` and in ``02``'s ``_rv``). This
helper MATCHES that — pinned by
``tests/contracts/test_tape_rv_estimator_parity.py::test_trailing_rv300_uses_sample_stdev_n_minus_1``.

Helpers-leaf, strictly: stdlib-only imports (not even ``bot.constants``)
so research scripts can import it without dragging bot config. The
default kwargs ARE the research constants (``VOL_WIN_S=300.0`` /
``VOL_STEP_S=5.0`` / ``STALE_S=30.0`` in 02; ``VOL_WINDOW_S`` /
``VOL_STEP_S`` in fairvalue_extract).

Consumer: ``bot/scanner/__init__.py`` computes this once per asset per
scan tick from ``CoinbaseFeed.get_buffer(asset)`` (the lock-held
snapshot of the 1-second-resolution ``PRICE_BUFFER_SIZE`` rolling
buffer), memoizes it per tick, stashes it at the 15M seam as the tape
half of the ATOMIC ``StateManager._scan_vol_pair_cache[asset] =
(raw_blended_rv, tape_rv300)`` pair (V.3-R1-M1), and the
longshot/twaplock overlays price off ``max(blended_rv, rv300)``.

**Two-layer staleness reality (R1-M1 fix round).** The per-grid-point
guard in this helper operates on BUFFER time and therefore covers only
buffer-SHAPE gaps: warmup (short buffer), a stalled/dead sampler thread,
or a persisted-buffer reload hole. It does NOT cover a frozen WS feed at
runtime — ``CoinbaseFeed._sampler_loop`` re-stamps the last-known price
with a fresh ``time.time()`` every 1s unconditionally (step-hold), so a
frozen feed presents a gapless buffer of flat, freshly-stamped samples
and this guard can never fire on it (rv300 instead reads LOW off the
flat segment). The research timeline had no re-stamping sampler, so in
the backtest this ONE guard covered both classes. Live, the second
class is covered at the consumer seam: ``scan()`` gates the
``_scan_vol_pair_cache`` write on the Bit-S.1 EVENT-time staleness
signal (``StateManager._scan_spot_staleness_cache``), treating rv300 as
None when the spot is unmeasured or more than ``TAPE_RV_MAX_STALENESS_S``
event-seconds stale — restoring the backtest's abstention. Pinned by
``tests/contracts/test_tape_rv_estimator_parity.py``
``::test_scan_gates_tape_rv_on_event_time_staleness`` +
``tests/integration/test_tape_rv_parity.py::TestEventTimeStalenessGate``.
"""
from __future__ import annotations

import bisect
import math
from typing import Optional, Sequence, Tuple

# The research scripts' staleness horizon (``STALE_S = 30.0`` in
# scripts/research/genhunt/02_longshot_tick_floor.py) — single source of
# truth shared by this helper's BUFFER-time guard (default kwarg below)
# and the scanner seam's EVENT-time gate (R1-M1; see module docstring).
# bot/constants.py::LONGSHOT_MAX_SPOT_STALENESS_SECONDS mirrors the same
# 30.0 for the engine-level gate (lockstep-pinned; constants.py cannot
# import this module without inverting the helpers-leaf layering).
TAPE_RV_MAX_STALENESS_S = 30.0


def trailing_rv300(
    samples: Sequence[Tuple[float, float]],
    now: float,
    *,
    window_s: float = 300.0,
    step_s: float = 5.0,
    max_staleness_s: float = TAPE_RV_MAX_STALENESS_S,
) -> Optional[float]:
    """Stdev of trailing per-``step_s`` log returns over ``window_s``.

    ``samples`` is a chronologically ordered sequence of ``(ts, price)``
    pairs — the ``CoinbaseFeed.get_buffer(asset)`` shape (1s cadence;
    ordering is a PRECONDITION, matching the sorted timelines the
    research scripts build; it is not re-sorted here).

    Mechanics (parity with ``02_longshot_tick_floor._rv``):

    1. For each grid point ``tt = now - j * step_s`` (``j = 0 ..
       window_s // step_s``, i.e. 61 points for the defaults), take the
       last sample at-or-before ``tt`` (last-price sampling via
       ``bisect_right``). If no such sample exists, or it is more than
       ``max_staleness_s`` older than ``tt``, return ``None`` — the
       guard applies at EVERY grid point (so a too-short buffer, a
       stale newest sample, or a mid-buffer BUFFER-time gap all yield
       ``None``, never a fabricated number). NOTE: this is a
       buffer-SHAPE guard only — a frozen-but-resampled live feed is
       invisible to it and is handled by the scanner seam's event-time
       gate (see the module docstring "Two-layer staleness reality").
    2. Chronological per-step log returns (pairs whose denominator is
       non-positive are skipped, as in 02; a non-positive numerator
       returns ``None`` — defensive vs. 02's uncaught ``ValueError``,
       unreachable on a real price tape).
    3. ``None`` if fewer than 10 valid returns.
    4. SAMPLE stdev: mean-centered squared deviations divided by
       ``len(rets) - 1`` (see module docstring for the citation).

    Returns the per-``step_s`` realized vol (dimensionless log-return
    stdev per 5s step for the defaults — the same surface as the
    engines' ``blended_rv``), or ``None`` for "no honest estimate".
    """
    if not samples:
        return None
    secs = [s[0] for s in samples]
    px = [s[1] for s in samples]
    k = int(window_s // step_s)
    grid = []
    for j in range(k + 1):
        tt = now - j * step_s
        i = bisect.bisect_right(secs, tt) - 1
        if i < 0 or (tt - secs[i]) > max_staleness_s:
            return None
        grid.append(px[i])
    grid.reverse()
    try:
        rets = [
            math.log(grid[i] / grid[i - 1])
            for i in range(1, len(grid))
            if grid[i - 1] > 0
        ]
    except (ValueError, TypeError):
        # Non-positive/garbage numerator price — no honest estimate.
        return None
    if len(rets) < 10:
        return None
    m = sum(rets) / len(rets)
    var = sum((r - m) ** 2 for r in rets) / (len(rets) - 1)
    return math.sqrt(var)
