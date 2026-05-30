"""Information-edge test helpers — TDD-first.

Does a live-spot signal at T-15s rank the settlement outcome BETTER than Kalshi's
own price (the lag-edge question)? Methodology-critical helpers pinned here:
no-look-ahead spot lookup + AUC + Brier.

Parent: kb/decisions/settlement-convergence-worklist.md (information-edge corner)
"""

from __future__ import annotations

import pytest

from scripts.research import phase1b_information_edge as ie


def test_spot_at_or_before_no_lookahead():
    series = [(100.0, 10.0), (105.0, 11.0), (110.0, 12.0)]
    assert ie.spot_at_or_before(series, 107.0) == 11.0     # most recent <= 107
    assert ie.spot_at_or_before(series, 110.0) == 12.0     # exact
    assert ie.spot_at_or_before(series, 104.9) == 10.0
    assert ie.spot_at_or_before(series, 99.0) is None       # before first
    # staleness guard: reject if nearest tick is too old
    assert ie.spot_at_or_before(series, 200.0, max_staleness_s=30) is None
    assert ie.spot_at_or_before(series, 120.0, max_staleness_s=30) == 12.0


def test_auc_perfect_and_inverted():
    # perfectly separating score -> AUC 1.0
    assert ie.auc([0.1, 0.2, 0.8, 0.9], [0, 0, 1, 1]) == pytest.approx(1.0)
    # perfectly anti-correlated -> AUC 0.0
    assert ie.auc([0.9, 0.8, 0.2, 0.1], [0, 0, 1, 1]) == pytest.approx(0.0)
    # random/tied -> 0.5
    assert ie.auc([0.5, 0.5, 0.5, 0.5], [0, 1, 0, 1]) == pytest.approx(0.5)


def test_auc_handles_ties_midrank():
    # one tie across the class boundary -> 0.5 contribution
    a = ie.auc([0.3, 0.5, 0.5, 0.7], [0, 0, 1, 1])
    assert 0.5 < a < 1.0


def test_brier():
    assert ie.brier([1.0, 0.0], [1, 0]) == pytest.approx(0.0)
    assert ie.brier([0.5, 0.5], [1, 0]) == pytest.approx(0.25)
