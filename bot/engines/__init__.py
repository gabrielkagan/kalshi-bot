"""Bit 6.1 + 6.2 + 6.3: Sprint 6 — math-layer engine classes.

Subpackage hosting the math-layer engines that consume feeds + fetchers
and emit per-asset volatility / probability / calibration estimates.
The Sprint 6 modularization track moved three classes here from
``bot/_impl.py``:

- ``VolatilityEngine`` (``volatility`` submodule, Bit 6.1) — Realized
  Kernel + Deribit DVOL volatility engine with adaptive jump detection
  and EGARCH variance-space blending. Byte-for-byte move.
- ``ProbabilityEngine`` (``probability`` submodule, Bit 6.2 + Bit 6.3
  path-B) — Student-t / NIG win-probability CDF + adaptive calibration
  cascade. Bit 6.2 originally added a ``from bot import _impl as
  _bot_impl`` late-binding pattern inside ``compute()`` and
  ``counterfactual_prob()`` because the mutable ``_CALIBRATION_ENGINE``
  singleton + ``_resolve_cal_engine`` resolver lived in ``bot/_impl.py``
  BELOW the line-109 engines re-export (top-level import would have
  ImportErrored or captured a stale ``None``). **Bit 6.3 path-B
  (2026-05-10) lifted that late-binding** by relocating the singleton
  + resolver to ``bot.engines.calibration`` (a leaf — does NOT import
  ``bot._impl``). ``probability.py`` now uses top-level
  ``from bot.engines import calibration as _cal_state`` plus
  ``_cal_state.X`` attribute access; the matching ``.importlinter``
  ``bot.engines.probability -> bot._impl`` ignore_imports carve-out
  (Pillar 2) was removed in the same commit.
- ``CalibrationEngine`` (``calibration`` submodule, Bit 6.3,
  2026-05-10) — adaptive 3-method calibrator (Platt / Beta / BLR) plus
  STC-aware Platt and shadow temperature scaling. **Byte-for-byte class
  body** with NO late-binding deviation; free-variable analysis returned
  zero suspect hits — the class itself never reads
  ``_CALIBRATION_ENGINE`` / ``_CAL_REGISTRY`` / ``_resolve_cal_engine``.
  **Bit 6.3 path-B** also relocated the calibration runtime state into
  this same module: ``_CALIBRATION_ENGINE: Optional[CalibrationEngine]``
  singleton, ``_CAL_REGISTRY: Dict[str, CalibrationEngine]`` dict, and
  the helpers ``_derive_subtype`` / ``_derive_asset_filter`` /
  ``_resolve_cal_engine`` all live alongside the class (annotations are
  bare-typed since the class is in scope; no quoted forward refs
  needed). The singleton is mutated by ``MainLoop.__init__`` (3
  instantiation sites) and read by callers OUTSIDE the class —
  ``OpportunityScanner.scan()`` and downstream sites.

``bot/_impl.py`` does ``from bot.engines import VolatilityEngine,
ProbabilityEngine, CalibrationEngine`` plus
``from bot.engines import calibration as _cal_state`` so:

- the runtime class construction in ``MainLoop.__init__`` resolves
  (``self.vol = VolatilityEngine(...)``,
  ``self.calibration = CalibrationEngine()``);
- the ``vol: VolatilityEngine`` type annotation on
  ``OpportunityScanner.__init__`` resolves;
- the bare-name ``ProbabilityEngine.X(...)`` call sites in
  ``bot/_impl.py`` resolve;
- ``MainLoop.__init__`` mutates the singleton via
  ``_cal_state._CALIBRATION_ENGINE = self.calibration`` and the
  registry via ``_cal_state._CAL_REGISTRY[k] = engine``;
- scan / settlement code reads the singleton + resolver via
  ``_cal_state._CALIBRATION_ENGINE`` and
  ``_cal_state._resolve_cal_engine(...)``.

Sprint 6 closes here; subsequent sprints (7+) will host their own
subpackages (state, scanners, etc.).
"""

from bot.engines.calibration import CalibrationEngine  # noqa: F401
from bot.engines.probability import ProbabilityEngine  # noqa: F401
from bot.engines.volatility import VolatilityEngine  # noqa: F401
