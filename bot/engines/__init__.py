"""Bit 6.1: Sprint 6 — math-layer engine classes.

Subpackage hosting the math-layer engines that consume feeds + fetchers
and emit per-asset volatility / probability / calibration estimates.
The Sprint 6 modularization track moves three classes here byte-for-byte
from ``bot/_impl.py``:

- ``VolatilityEngine`` (``volatility`` submodule, Bit 6.1) — Realized
  Kernel + Deribit DVOL volatility engine with adaptive jump detection
  and EGARCH variance-space blending.
- ``ProbabilityEngine`` (``probability`` submodule, Bit 6.2 — pending) —
  Student-t / NIG win-probability CDF.
- ``CalibrationEngine`` (``calibration`` submodule, Bit 6.3 — pending) —
  three-layer calibrator (raw → cal → blended) with cal_mlp four-site
  feature lock-step.

``bot/_impl.py`` does ``from bot.engines import VolatilityEngine`` so
the runtime construction in ``MainLoop.__init__`` (search
``self.vol = VolatilityEngine``) and the ``vol: VolatilityEngine``
type annotation on ``OpportunityScanner.__init__`` resolve through the
proxy. Subsequent bits will extend this re-export list.
"""

from bot.engines.volatility import VolatilityEngine  # noqa: F401
