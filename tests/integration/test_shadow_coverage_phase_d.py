"""Phase D (Shadow Coverage Expansion): cal_mlp_request_id on shadow rows.

Background:
  Pre-Phase-D, `_calmlp_annotate_async` at bot/_impl.py was called ONCE per
  scan-loop iteration AFTER the per-asset floor check + after several
  shadow-stage inserts/queue snapshots had already fired. Two consequences:

  1. Cross-ticker contamination: `_shadow_diag` is window-scoped (created
     once per scan window, reused per market). The annotate function never
     CLEARS prior `cal_mlp_request_id` keys. So iteration N+1 inherited
     iteration N's stale uuid until N+1's annotate overwrote it. Inserts
     happening BEFORE annotate in iteration N+1 carried iteration N's uuid.

  2. Many shadow stages (low_price_shadow, floor_raise_shadow,
     overnight_lp_shadow, weekend_discount_shadow, no_side queues, etc.)
     either fire BEFORE the annotate call, or had cal_mlp_* explicitly
     STRIPPED from their queue snapshots — so their rows had NULL
     cal_mlp_request_id and the post-hoc daemon never predicted on them.

Phase D fixes both:
  - Reset cal_mlp_* keys at TOP of each market iteration (eliminates leak).
  - Move the annotate call EARLIER in the iteration (after best_ask
    validation, before any _shadow_diag-splat insert).
  - Remove the explicit `not k.startswith('cal_mlp_')` strip patterns from
    queue snapshots so shadow rows propagate the uuid.

Master plan: kb/decisions/shadow-coverage-expansion-may01.md (Phase D).
"""

import os
import re
import sys

import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, PROJECT_ROOT)

BOT_PATH = os.path.join(PROJECT_ROOT, "bot/_impl.py")
SCANNER_PATH = os.path.join(PROJECT_ROOT, "bot", "scanner", "__init__.py")


def _read_bot():
    """Bit 8.1 (2026-05-10): OpportunityScanner moved to
    bot/scanner/__init__.py. Phase D's annotate site, scan for-loop,
    queue-snapshot strip patterns, and `_shadow_diag` reset all live
    in scanner now. Concat both files so the audit survives the move."""
    src = ""
    if os.path.exists(BOT_PATH):
        with open(BOT_PATH) as f:
            src = f.read()
    if os.path.isfile(SCANNER_PATH):
        with open(SCANNER_PATH) as f:
            src += "\n" + f.read()
    return src


class TestPhaseDStripPatternsRemoved:
    """Post-Phase-D, only TWO `_shadow_diag.items() if not k.startswith('cal_mlp_')`
    strip patterns are legitimate:

    1. TM96 cal_mlp gate (around bot/_impl.py:12920): computes its own
       `_tm96_diag_clean` and splats `_shadow_diag` MINUS cal_mlp_* keys
       to avoid double-stamping the per-gate prediction.

    2. tradeable_false (with_market) `insert_rejection` post-annotate:
       this rejection site fires AFTER the new early annotate, but
       `insert_rejection`'s signature does NOT accept cal_mlp_* kwargs.
       Stripping at the splat keeps the rejection row writeable.

    The two queue-snapshot strips (originally lines ~12501 and ~13175)
    are GONE — Phase D propagates cal_mlp_request_id through shadow
    queue snapshots so the post-hoc daemon predicts on shadow rows.

    Master plan: kb/decisions/shadow-coverage-expansion-may01.md."""

    def test_only_legitimate_strips_remain(self):
        src = _read_bot()
        pattern = r"_shadow_diag\.items\(\)\s*\n?\s*if\s+not\s+k\.startswith\(['\"]cal_mlp_['\"]\)"
        matches = list(re.finditer(pattern, src))
        assert len(matches) == 2, (
            f"Expected 2 strip patterns (TM96 gate + tradeable_false "
            f"insert_rejection post-annotate); found {len(matches)}. "
            f"Phase D removed the queue-snapshot strips at the original "
            f"lines ~12501 and ~13175 so shadow rows propagate "
            f"cal_mlp_request_id."
        )
        # Verify each remaining strip is in a justified context. Strip 1
        # = TM96 (`_tm96_diag_clean` or `tm96_calmlp_gate_blocked` within
        # ~4000 chars before the strip). Strip 2 = tradeable_false
        # with_market (`insert_rejection` and `prob_with_market` within
        # ~1500 chars before the strip).
        contexts_found = {"tm96": False, "tradeable_false": False}
        for m in matches:
            before = src[max(0, m.start() - 4000):m.start()]
            if "_tm96_diag_clean" in before or "tm96_calmlp_gate_blocked" in before:
                contexts_found["tm96"] = True
            elif "insert_rejection" in before[-1500:] and "prob_with_market" in before[-1500:]:
                contexts_found["tradeable_false"] = True
        assert all(contexts_found.values()), (
            f"One or both strip patterns is NOT in a justified context: "
            f"{contexts_found}. Phase D may have removed the wrong strip "
            f"or introduced a strip in an unintended location."
        )


class TestPhaseDIterationReset:
    """Every market iteration must clear cal_mlp_* keys from _shadow_diag
    BEFORE any insert/queue snapshot in that iteration. Prevents
    cross-ticker uuid contamination (the pre-Phase-D bug)."""

    def test_for_loop_resets_cal_mlp_keys(self):
        """The body of `for mkt in window["markets"]:` must contain a
        statement that clears cal_mlp_* keys from _shadow_diag near the
        top — before raw_prob_pre is computed."""
        src = _read_bot()
        # Locate the scan for-loop.
        loop_match = re.search(r'for mkt in window\["markets"\]:', src)
        assert loop_match, "scan for-loop not found in bot/_impl.py"
        loop_start = loop_match.end()
        # Take a generous slice to cover the iteration prologue (200 lines
        # is enough; raw_prob_pre is set within ~200 lines of loop start).
        # Cap by next "def " at outer indent to not bleed into other methods.
        loop_body = src[loop_start:loop_start + 12000]
        # Must reset cal_mlp_* keys via a delete-by-prefix construct.
        # Accept either an explicit list-comprehension delete pattern or
        # a helper-function call. Canonical forms:
        #   for _k in [k for k in _shadow_diag if k.startswith("cal_mlp_")]: del _shadow_diag[_k]
        #   _reset_cal_mlp_diag(_shadow_diag)
        # We only require that "cal_mlp_" appears in a context that
        # clears the key, not just stamps.
        candidates = [
            r"del\s+_shadow_diag\[",                      # del _shadow_diag[k] form
            r"_shadow_diag\.pop\(",                       # _shadow_diag.pop("cal_mlp_*")
            r"_reset_cal_mlp_diag\s*\(\s*_shadow_diag",  # helper-fn form
        ]
        # Require BOTH the delete/pop construct AND the cal_mlp_ string
        # within ~150 chars of each other (ensures the delete is
        # cal_mlp_-targeted, not an unrelated pop).
        found = False
        for pat in candidates:
            for m in re.finditer(pat, loop_body):
                ctx_start = max(0, m.start() - 200)
                ctx_end = min(len(loop_body), m.end() + 200)
                ctx = loop_body[ctx_start:ctx_end]
                if "cal_mlp_" in ctx:
                    found = True
                    break
            if found:
                break
        assert found, (
            "Phase D: top-of-iteration reset of cal_mlp_* keys not found "
            "in scan for-loop. Add either an explicit `del`-by-prefix "
            "loop or a `_reset_cal_mlp_diag(_shadow_diag)` helper call "
            "near the start of `for mkt in window[\"markets\"]:` to "
            "prevent cross-ticker uuid contamination."
        )


class TestPhaseDInsertRejectionStripsCalMlp:
    """Phase D adversarial review HIGH-1 regression: any `insert_rejection`
    call AFTER the new early annotate site that splats `**_shadow_diag`
    must STRIP cal_mlp_* keys, because `insert_rejection`'s signature does
    NOT accept cal_mlp_*. Without the strip, `**_shadow_diag` raises
    TypeError and the rejection row is silently lost."""

    def test_post_annotate_insert_rejection_strips_cal_mlp(self):
        src = _read_bot()
        # Locate the new early annotate site.
        annotate_match = re.search(
            r"_calmlp_annotate_async\(\s*\n?\s*_shadow_diag,\s*raw_prob=raw_prob_pre",
            src,
        )
        assert annotate_match, (
            "New early annotate call not found — search for "
            "`_calmlp_annotate_async(_shadow_diag, raw_prob=raw_prob_pre, ...)`"
        )
        annotate_idx = annotate_match.end()
        # Locate scan-loop end (next outer-indent `def `). Bot.py uses
        # 4-space indents inside MainLoop methods; method def at 4 spaces.
        loop_end_match = re.search(r"\n    def\s+", src[annotate_idx:])
        loop_end = annotate_idx + (loop_end_match.start() if loop_end_match else len(src))
        section = src[annotate_idx:loop_end]
        # Find every insert_rejection( inside this section.
        for rej_match in re.finditer(r"insert_rejection\s*\(", section):
            # Take the next ~2000 chars after the call to capture its body.
            body_start = rej_match.start()
            body_end = min(len(section), body_start + 2000)
            body = section[body_start:body_end]
            # If this insert_rejection splats **_shadow_diag, the splat
            # MUST strip cal_mlp_*. Either via dict-comprehension filter
            # at this exact call, or via extending insert_rejection's
            # signature (in which case the test below should be updated).
            if "**_shadow_diag" in body:
                assert "cal_mlp_" in body and "startswith" in body, (
                    f"Phase D: insert_rejection at offset {body_start} splats "
                    f"`**_shadow_diag` post-annotate without stripping cal_mlp_*. "
                    f"This silently TypeErrors and loses the rejection row. Add "
                    f"a `**{{k:v for k,v in _shadow_diag.items() "
                    f"if not k.startswith('cal_mlp_')}}` strip OR extend "
                    f"insert_rejection to accept cal_mlp_* params."
                )


class TestPhaseDAnnotatePosition:
    """The `_calmlp_annotate_async(_shadow_diag, ...)` call must be
    positioned EARLIER than the first `insert_evaluated_opportunity` call
    that splats `**_shadow_diag`. Pre-Phase-D it was at ~line 12331,
    AFTER queue snapshots at ~12000. Phase D moves it to fire BEFORE
    those snapshots."""

    def test_annotate_called_before_first_eval_opp_shadow_splat(self):
        src = _read_bot().splitlines()
        # Find the LAST _calmlp_annotate_async call inside the scan loop
        # (there may be more than one if NO-side gets its own).
        # We expect at least one annotate BEFORE the price_out_of_range
        # insert at line ~11928.
        scan_loop_idx = None
        first_eval_opp_splat_idx = None
        last_annotate_before_splat_idx = None
        for i, line in enumerate(src):
            if scan_loop_idx is None and 'for mkt in window["markets"]:' in line:
                scan_loop_idx = i
                continue
            if scan_loop_idx is None:
                continue
            if first_eval_opp_splat_idx is None:
                # Match an insert_evaluated_opportunity call followed
                # within ~50 lines by **_shadow_diag splat.
                if "insert_evaluated_opportunity(" in line:
                    window = "\n".join(src[i:i + 60])
                    if "**_shadow_diag" in window:
                        first_eval_opp_splat_idx = i
            if "_calmlp_annotate_async(" in line:
                if first_eval_opp_splat_idx is None:
                    last_annotate_before_splat_idx = i
        assert scan_loop_idx is not None, "scan for-loop not found"
        assert first_eval_opp_splat_idx is not None, (
            "no insert_evaluated_opportunity with **_shadow_diag splat "
            "found inside scan for-loop"
        )
        assert last_annotate_before_splat_idx is not None, (
            f"Phase D: no `_calmlp_annotate_async(...)` call found in "
            f"the scan for-loop BEFORE the first insert_evaluated_opportunity "
            f"that splats **_shadow_diag (at line {first_eval_opp_splat_idx + 1}). "
            f"Move the annotate call to fire after best_ask validation but "
            f"before any shadow insert/queue snapshot."
        )


class TestPhaseDAnnotateRuntimeIsolation:
    """Phase D adversarial review LOW-2 regression: runtime test that the
    pop-at-top-of-iteration semantically prevents the cross-ticker uuid
    leak. Calls `_calmlp_annotate_async` directly on a shared dict
    (mimicking the per-window `_shadow_diag` reuse) with iter-1 stamping
    a uuid and iter-2 hitting the raw_prob_null skip path. Without the
    pop, iter-2's row would carry iter-1's uuid."""

    def test_pop_prevents_uuid_leak(self):
        """Simulate two iterations: iter-1 succeeds (uuid stamped),
        iter-2 hits raw_prob=None skip path. The Phase D pop-at-top-
        of-iter must clear iter-1's uuid before iter-2's annotate call."""
        from scripts.cal_mlp.integration import (
            annotate_evaluation_async_enqueue as _annotate,
        )

        class _StubPredictor:
            pass

        diag = {"egarch_sigma": 0.5}  # window-scoped non-cal_mlp keys
        # Iter 1: success path — uuid stamped.
        _annotate(
            diag, raw_prob=0.85, ticker="A", side="yes",
            entry_price_cents=70, row_features={},
            predictor=_StubPredictor(), db_path="/tmp/x",
        )
        assert "cal_mlp_request_id" in diag
        iter1_uuid = diag["cal_mlp_request_id"]
        assert iter1_uuid

        # Iter 2 prologue: Phase D's pop-at-top-of-iter clears cal_mlp_*.
        for _cmk in [
            "cal_mlp_request_id", "cal_mlp_skipped_reason",
            "cal_mlp_p_mean", "cal_mlp_p_std",
            "cal_mlp_final_lo", "cal_mlp_final_hi",
            "cal_mlp_train_id",
        ]:
            diag.pop(_cmk, None)
        assert "cal_mlp_request_id" not in diag

        # Iter 2: raw_prob=None → annotate sets skipped_reason and
        # does NOT stamp uuid. Without the pop above, the dict would
        # still carry iter-1's uuid AND iter-2's skipped_reason.
        _annotate(
            diag, raw_prob=None, ticker="B", side="yes",
            entry_price_cents=70, row_features={},
            predictor=_StubPredictor(), db_path="/tmp/x",
        )
        assert diag.get("cal_mlp_skipped_reason") == "raw_prob_null"
        assert "cal_mlp_request_id" not in diag, (
            f"Cross-ticker uuid leak: iter-2 row carries iter-1's "
            f"uuid {diag.get('cal_mlp_request_id')!r}. The pop-at-top-"
            f"of-iter at bot/_impl.py around line 11272 must clear cal_mlp_*."
        )


class TestPhaseDQueueSnapshotsCarryRequestId:
    """The shadow queue snapshots (low_price_shadow, no_side_shadow,
    overnight_lp_shadow, weekend_discount_shadow, etc.) must NOT strip
    cal_mlp_* keys when copying _shadow_diag — so the queued items carry
    the iteration's cal_mlp_request_id and the eventual insert writes it
    to the row."""

    def test_queue_shadow_diag_copies_are_unfiltered(self):
        """Every `_shadow_diag.copy()` or dict-copy splat of `_shadow_diag`
        in queue snapshots must NOT have a cal_mlp_*-stripping filter."""
        src = _read_bot()
        # Find queue-snapshot patterns.
        snapshot_re = re.compile(
            r'"_shadow_diag":\s*(\{[^{}]*\}|_shadow_diag\.copy\(\))'
        )
        bad = []
        for m in snapshot_re.finditer(src):
            snapshot = m.group(1)
            if "cal_mlp_" in snapshot and "not k.startswith" in snapshot:
                line_no = src[:m.start()].count("\n") + 1
                bad.append(f"line {line_no}: {snapshot[:80]}")
        assert not bad, (
            "Phase D: queue snapshots are still filtering cal_mlp_* "
            "keys. Remove the `if not k.startswith('cal_mlp_')` from "
            "these snapshots:\n" + "\n".join(bad)
        )
