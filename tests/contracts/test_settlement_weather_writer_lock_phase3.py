"""Ticket 86ba1xdwp — settlement weather Phase 3 writer-lock invariant.

Pins the structural invariant from `kb/decisions/settlement-weather-writer-storm-plan-may21.md`:
the weather settlement phase in `bot/settlement.py::SettlementTracker._poll_evaluated_opportunities`
must NOT call `_wx_eng._model.update_bias(...)` from inside the same for-loop body
that also calls `_wx_eng._fetcher.fetch_observed_high(...)`.

The pre-fix shape (mechanically buggy):

    for (opp_id, ticker, row) in _weather_updates:
        ...
        _obs_high = _wx_eng._fetcher.fetch_observed_high(_wx_city, _market_date)   # HTTP, 1-5s
        if _obs_high is not None:
            self._state.conn.execute("UPDATE evaluated_opportunities ...")          # auto-BEGIN tx
            _wx_dirty = True
            ...
            _wx_eng._model.update_bias(_wx_city, _obs_high, forecast_mean, ...)     # ← STILL in tx;
                                                                                    #   _save_bias's
                                                                                    #   separate conn
                                                                                    #   times out 10s
    if _wx_dirty:
        self._state.conn.commit()                                                   # tx finally closes

The post-fix shape (Phase 3a HTTP-only / Phase 3b DB-only):

    _wx_observations = []
    for (opp_id, ticker, row) in _weather_updates:                                   # Phase 3a
        ...
        _obs_high = _wx_eng._fetcher.fetch_observed_high(_wx_city, _market_date)
        if _obs_high is not None:
            _wx_observations.append((opp_id, ticker, _wx_city, _market_date,
                                     _obs_high, forecast_mean))

    for (opp_id, ticker, _wx_city, _market_date, _obs_high, forecast_mean) in _wx_observations:  # Phase 3b
        try:
            self._state.conn.execute("UPDATE evaluated_opportunities ...")
            self._state.conn.commit()                                                # per-row release
            if forecast_mean:
                _wx_eng._model.update_bias(_wx_city, _obs_high, forecast_mean, ...)
        except Exception as e:
            ...

Failure modes pinned:

  1. **Single-loop regression** — A future maintainer collapses the two loops back into
     one to "simplify"; this test FAILS because `fetch_observed_high` and `update_bias`
     end up in the same loop body again.

  2. **Missing per-row commit** — A future maintainer removes the per-row commit in
     Phase 3b to "save commits"; this test FAILS because no `self._state.conn.commit()`
     remains inside the Phase 3b loop body.

  3. **`_wx_dirty` resurrection** — A future maintainer reintroduces an after-loop
     commit pattern; this test FAILS on the `_wx_dirty` symbol check.

Storm evidence on production (`ubuntu-s-1vcpu-1gb-nyc3-01`):
  2026-05-20 11:04-11:07 UTC + 2026-05-21 11:04-11:07 UTC — both daily slots
  cascaded 10-30s `status=fail` writes across weather_engine save_bias /
  market_obs_snapshotter / fifteenm_shadow / phantom_reconcile.
"""
from __future__ import annotations

import ast
from pathlib import Path


_REPO_ROOT = Path(__file__).resolve().parents[2]
_SETTLEMENT_PATH = _REPO_ROOT / "bot" / "settlement.py"


def _find_method(tree: ast.Module, class_name: str, method_name: str) -> ast.FunctionDef:
    for cls in ast.walk(tree):
        if isinstance(cls, ast.ClassDef) and cls.name == class_name:
            for item in cls.body:
                if isinstance(item, ast.FunctionDef) and item.name == method_name:
                    return item
    raise AssertionError(
        f"Method {class_name}.{method_name} not found in {_SETTLEMENT_PATH}"
    )


def _calls_attribute_chain(node: ast.AST, chain_suffix: tuple) -> bool:
    """Return True if `node`'s subtree contains a Call whose .func attribute chain
    ends with `chain_suffix`.  e.g. chain_suffix=('_fetcher', 'fetch_observed_high')
    matches `<anything>._fetcher.fetch_observed_high(...)`."""
    for sub in ast.walk(node):
        if not isinstance(sub, ast.Call):
            continue
        func = sub.func
        attrs = []
        while isinstance(func, ast.Attribute):
            attrs.append(func.attr)
            func = func.value
        attrs.reverse()
        if len(attrs) >= len(chain_suffix) and tuple(attrs[-len(chain_suffix):]) == chain_suffix:
            return True
    return False


def _calls_method(node: ast.AST, attr_name: str) -> bool:
    """True if any Call in `node`'s subtree has .func.attr == attr_name."""
    for sub in ast.walk(node):
        if isinstance(sub, ast.Call) and isinstance(sub.func, ast.Attribute):
            if sub.func.attr == attr_name:
                return True
    return False


def _commits_state_conn(node: ast.AST) -> bool:
    """True if subtree contains `self._state.conn.commit()` (or `_state.conn.commit()`)."""
    for sub in ast.walk(node):
        if not isinstance(sub, ast.Call):
            continue
        func = sub.func
        if not isinstance(func, ast.Attribute) or func.attr != "commit":
            continue
        # func.value should be `self._state.conn` (Attribute "conn" on Attribute "_state")
        val = func.value
        if isinstance(val, ast.Attribute) and val.attr == "conn":
            inner = val.value
            if isinstance(inner, ast.Attribute) and inner.attr == "_state":
                return True
    return False


def _names_referenced(node: ast.AST) -> set:
    """All ast.Name.id strings appearing in `node`'s subtree."""
    out: set = set()
    for sub in ast.walk(node):
        if isinstance(sub, ast.Name):
            out.add(sub.id)
    return out


def test_weather_phase3_uses_two_separate_loops() -> None:
    """Phase 3a (HTTP fetch) and Phase 3b (DB writes + update_bias) are SEPARATE for-loops.

    Pin: at least one for-loop in `_poll_evaluated_opportunities` contains
    `fetch_observed_high` but NOT `update_bias`, AND at least one OTHER for-loop
    contains `update_bias` but NOT `fetch_observed_high`. This rules out the
    pre-fix shape where both were in the same loop body.
    """
    tree = ast.parse(_SETTLEMENT_PATH.read_text())
    method = _find_method(tree, "SettlementTracker", "_poll_evaluated_opportunities")

    fetch_only_loops = []
    update_only_loops = []
    mixed_loops = []
    for for_node in ast.walk(method):
        if not isinstance(for_node, ast.For):
            continue
        has_fetch = _calls_attribute_chain(for_node, ("_fetcher", "fetch_observed_high"))
        has_update_bias = _calls_method(for_node, "update_bias")
        if has_fetch and has_update_bias:
            mixed_loops.append(for_node)
        elif has_fetch:
            fetch_only_loops.append(for_node)
        elif has_update_bias:
            update_only_loops.append(for_node)

    assert not mixed_loops, (
        f"Pre-fix regression: at least one for-loop in "
        f"SettlementTracker._poll_evaluated_opportunities calls BOTH "
        f"_fetcher.fetch_observed_high AND update_bias in the same body "
        f"(line {mixed_loops[0].lineno}). The mechanical bug from ticket "
        f"86ba1xdwp re-opened: writer lock held across HTTP latency. "
        f"Split into Phase 3a (HTTP-only) + Phase 3b (DB-only) per "
        f"kb/decisions/settlement-weather-writer-storm-plan-may21.md."
    )

    assert fetch_only_loops, (
        "Expected at least one for-loop in _poll_evaluated_opportunities that calls "
        "_fetcher.fetch_observed_high without update_bias (Phase 3a — HTTP-only). "
        "Did the weather settlement phase get removed entirely?"
    )

    # Phase 3b can be absent ONLY if there are no HTTP observations to drain.
    # In normal shape, the post-fix design has both phases.
    assert update_only_loops, (
        "Expected at least one for-loop in _poll_evaluated_opportunities that calls "
        "update_bias without _fetcher.fetch_observed_high (Phase 3b — DB-only). "
        "Did Phase 3b get folded back into Phase 3a?"
    )


def test_weather_phase3b_commits_per_row() -> None:
    """Phase 3b loop body MUST call `self._state.conn.commit()` per iteration.

    Without per-row commit, the auto-BEGIN tx accumulates across update_bias calls,
    re-opening the cascade. The plan-doc fix releases the writer lock immediately
    after each UPDATE so the next iteration's separate-conn `_save_bias` can grab
    the lock cleanly.
    """
    tree = ast.parse(_SETTLEMENT_PATH.read_text())
    method = _find_method(tree, "SettlementTracker", "_poll_evaluated_opportunities")

    found_phase3b_commit = False
    for for_node in ast.walk(method):
        if not isinstance(for_node, ast.For):
            continue
        if not _calls_method(for_node, "update_bias"):
            continue
        if _calls_attribute_chain(for_node, ("_fetcher", "fetch_observed_high")):
            continue
        # This is the Phase 3b loop; assert it commits per row.
        if _commits_state_conn(for_node):
            found_phase3b_commit = True
            break

    assert found_phase3b_commit, (
        "Phase 3b for-loop in SettlementTracker._poll_evaluated_opportunities "
        "must contain `self._state.conn.commit()` inside the loop body so the "
        "writer lock is released between rows. Without per-row commit, the "
        "settlement weather cascade (ticket 86ba1xdwp) re-opens — each save_bias "
        "call would still race against an open shared-conn tx."
    )


def test_wx_dirty_after_loop_commit_retired() -> None:
    """The pre-fix `_wx_dirty` accumulator + after-loop `if _wx_dirty: commit()`
    pattern is retired. If `_wx_dirty` is still referenced in
    `_poll_evaluated_opportunities`, the fix is incomplete or has regressed.
    """
    tree = ast.parse(_SETTLEMENT_PATH.read_text())
    method = _find_method(tree, "SettlementTracker", "_poll_evaluated_opportunities")
    names = _names_referenced(method)
    assert "_wx_dirty" not in names, (
        "`_wx_dirty` accumulator should have been removed when Phase 3 was "
        "two-phased per kb/decisions/settlement-weather-writer-storm-plan-may21.md. "
        "If you intentionally kept it, the after-loop commit pattern likely "
        "regressed the writer-lock-held-across-HTTP bug from ticket 86ba1xdwp."
    )
