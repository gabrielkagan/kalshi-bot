"""Bit 6.1 + 6.2: Sprint 6 — math-layer engine classes.

Subpackage hosting the math-layer engines that consume feeds + fetchers
and emit per-asset volatility / probability / calibration estimates.
The Sprint 6 modularization track moves three classes here from
``bot/_impl.py``:

- ``VolatilityEngine`` (``volatility`` submodule, Bit 6.1) — Realized
  Kernel + Deribit DVOL volatility engine with adaptive jump detection
  and EGARCH variance-space blending. Byte-for-byte move.
- ``ProbabilityEngine`` (``probability`` submodule, Bit 6.2) — Student-t
  / NIG win-probability CDF + adaptive calibration cascade. Almost
  byte-for-byte; the two methods that touch the mutable
  ``_CALIBRATION_ENGINE`` singleton and ``_resolve_cal_engine``
  function in ``bot/_impl.py`` use ``from bot import _impl as _bot_impl``
  late-binding to avoid the partial-module circular-import failure
  (``bot._impl`` re-exports this class BEFORE those names are defined).
- ``CalibrationEngine`` (``calibration`` submodule, Bit 6.3 — pending) —
  three-layer calibrator (raw → cal → blended) with cal_mlp four-site
  feature lock-step.

``bot/_impl.py`` does ``from bot.engines import VolatilityEngine,
ProbabilityEngine`` so the runtime construction in ``MainLoop.__init__``
(search ``self.vol = VolatilityEngine``), the ``vol: VolatilityEngine``
type annotation on ``OpportunityScanner.__init__``, and the 19
bare-name ``ProbabilityEngine.X(...)`` call sites in ``bot/_impl.py``
all resolve through the proxy. Subsequent bits will extend this
re-export list.
"""

from bot.engines.probability import ProbabilityEngine  # noqa: F401
from bot.engines.volatility import VolatilityEngine  # noqa: F401
