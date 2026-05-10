"""Phase 1 — Prevention #3 from the Apr 24 PM.

Make every silent-continue in scan() observable. Three known smoking guns
caused recurring 15M scan-silence outages over Apr 24-25:

  - bot/_impl.py:8793  `if not prob_result.tradeable:` non-z_score reason
  - bot/_impl.py:9512  `if not prob_with_market.tradeable:` non-z_score reason
  - bot/_impl.py:8832  `low_probability` for 15M (JSONL only, no DB row)

Each path silently continues with no `insert_rejection` row, leaving
the scan-productive watchdog blind (it only counts DB rows). Apr 24
fix #2 wired no_orderbook/no_best_ask paths but missed these.

This test file pins the new behavior:
  1. EVERY path through `if not <prob>.tradeable:` writes a DB row,
     not just z_score/refusing reasons.
  2. The 15M low_probability filter writes an insert_rejection (or
     insert_evaluated_opportunity), not just JSONL.
  3. AST tripwire: any future `continue` inside an `if not ...tradeable:`
     branch must be preceded by an unconditional `insert_rejection`.

Why this is the durable fix:
  - Each silent path is a future outage waiting. The Apr 24 PM's
    "Prevention #3" called for an AST audit; never done; we've had
    3 recurrences in 2 days.
  - With every continue writing a row, future outages immediately
    surface in the DB with the exact `rejection_reason`.
  - The scan-productive watchdog becomes 100% reliable.
"""

import ast
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

BOT_PY = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bot/scanner/__init__.py")


def _scan_fn():
    """Return the OpportunityScanner.scan FunctionDef AST node."""
    with open(BOT_PY) as f:
        tree = ast.parse(f.read())
    for cls in ast.walk(tree):
        if (isinstance(cls, ast.ClassDef)
                and cls.name == "OpportunityScanner"):
            for fn in cls.body:
                if (isinstance(fn, ast.FunctionDef)
                        and fn.name == "scan"):
                    return fn
    return None


class TestTradeableFalseAlwaysWritesRow(unittest.TestCase):
    """For every `if not <something>.get('tradeable'):` block in scan()
    whose body ends in `continue`, the body must contain an
    unconditional (i.e., not gated by 'reason' content) call to
    `insert_rejection` or `insert_evaluated_opportunity`.

    Pre-fix: `if 'z_score' in reason or 'refusing' in reason: insert_rejection(...)`
    only fires on those reasons. Other reasons (`'invalid inputs'`,
    `'sigma_move is zero'`) silently continue.
    Post-fix: insert_rejection fires regardless of reason; the reason
    string is recorded in `rejection_reason`."""

    def test_all_tradeable_false_blocks_have_unconditional_db_write(self):
        scan = _scan_fn()
        self.assertIsNotNone(scan, "scan() not found")

        # Find every `if not <expr>.get('tradeable'):` pattern in scan body.
        targets = []
        for node in ast.walk(scan):
            if not isinstance(node, ast.If):
                continue
            test = node.test
            # Match `not <X>.get("tradeable")`
            if (isinstance(test, ast.UnaryOp)
                    and isinstance(test.op, ast.Not)
                    and isinstance(test.operand, ast.Call)
                    and isinstance(test.operand.func, ast.Attribute)
                    and test.operand.func.attr == "get"
                    and len(test.operand.args) >= 1
                    and isinstance(test.operand.args[0], ast.Constant)
                    and test.operand.args[0].value == "tradeable"):
                targets.append(node)

        self.assertGreaterEqual(
            len(targets), 2,
            f"Expected at least 2 `if not <X>.get('tradeable')` blocks "
            f"in scan() (the two prob_result and prob_with_market "
            f"sites). Found {len(targets)}.")

        for if_node in targets:
            # Body must end with `continue` (these are bail-out blocks).
            ends_with_continue = (
                isinstance(if_node.body[-1], ast.Continue))
            if not ends_with_continue:
                continue  # not a bail-out path; skip
            # Walk the if-body and look for an UNCONDITIONAL (not nested
            # inside another If) call to insert_rejection or
            # insert_evaluated_opportunity.
            body_src = ast.unparse(if_node.body)
            has_call = (
                "insert_rejection(" in body_src
                or "insert_evaluated_opportunity(" in body_src)
            self.assertTrue(
                has_call,
                f"`if not <X>.get('tradeable')` block at line "
                f"{if_node.lineno} ends in `continue` but contains "
                f"no insert_rejection / insert_evaluated_opportunity "
                f"call. This is a silent-continue path — every future "
                f"outage triggered by this code goes invisible. "
                f"Wire a DB row.")

            # Stronger: the call must be UNCONDITIONAL — i.e., not
            # solely inside an inner `if "z_score" in reason or "refusing" in reason:`
            # gate. Strategy: find every insert_rejection call; for each,
            # check that its enclosing branch chain doesn't contain a
            # conditional that filters by the `reason` string.
            unconditional_found = False
            for sub in ast.walk(if_node):
                if not isinstance(sub, ast.Call):
                    continue
                if not (isinstance(sub.func, ast.Attribute)
                        and sub.func.attr in (
                            "insert_rejection",
                            "insert_evaluated_opportunity")):
                    continue
                # Trace ancestors back to if_node and check if any
                # ancestor is a sub-If whose test mentions "reason" and
                # uses string membership (e.g., 'z_score' in reason).
                parents_chain = []
                # Build parents within if_node
                parent_map = {}
                for p in ast.walk(if_node):
                    for c in ast.iter_child_nodes(p):
                        parent_map[id(c)] = p
                cur = sub
                gated_by_reason = False
                while cur is not if_node:
                    parent = parent_map.get(id(cur))
                    if parent is None:
                        break
                    if isinstance(parent, ast.If) and parent is not if_node:
                        cond_src = ast.unparse(parent.test)
                        if "reason" in cond_src and (
                                "z_score" in cond_src
                                or "refusing" in cond_src):
                            gated_by_reason = True
                            break
                    cur = parent
                if not gated_by_reason:
                    unconditional_found = True
                    break

            self.assertTrue(
                unconditional_found,
                f"`if not <X>.get('tradeable')` block at line "
                f"{if_node.lineno} has insert_rejection/eval_opp calls "
                f"BUT all of them are gated by `reason` content "
                f"(z_score/refusing). Reasons like 'invalid inputs' "
                f"or 'sigma_move is zero' fall through silently. "
                f"Move at least one DB-write call OUT of the reason "
                f"gate so it fires for every tradeable=False case.")


class TestLowProbability15mWritesDbRow(unittest.TestCase):
    """The 15M `low_probability` filter (cal_prob < min_prob_needed)
    must call `insert_rejection` (or `insert_evaluated_opportunity`)
    so the watchdog can see it. Pre-fix only logged JSONL via
    `log_opportunity` — DB blind."""

    def test_low_probability_filter_writes_db_for_15m(self):
        scan = _scan_fn()
        self.assertIsNotNone(scan, "scan() not found")

        # Find: `if cal_prob < min_prob_needed and not _pcfg.observation_only:`
        target = None
        for node in ast.walk(scan):
            if not isinstance(node, ast.If):
                continue
            test_src = ast.unparse(node.test)
            if ("cal_prob" in test_src
                    and "min_prob_needed" in test_src
                    and "observation_only" in test_src):
                target = node
                break
        self.assertIsNotNone(
            target,
            "low_probability gate `if cal_prob < min_prob_needed and "
            "not _pcfg.observation_only:` not found in scan().")

        # Within target, the 15M branch (i.e., NOT inside the
        # hourly/spx/weather sub-If) must contain insert_rejection or
        # insert_evaluated_opportunity. The hourly/spx/weather branch
        # is allowlisted (those products use volume-control logging).
        # Strategy: walk target.body looking for the hourly/spx/weather
        # If; everything OUTSIDE that nested If is the 15M path.
        inner_hourly_if = None
        for stmt in ast.walk(target):
            if not isinstance(stmt, ast.If):
                continue
            if stmt is target:
                continue
            cond_src = ast.unparse(stmt.test)
            if ("hourly" in cond_src
                    and "spx" in cond_src
                    and "weather" in cond_src):
                inner_hourly_if = stmt
                break

        # Collect all insert_* calls inside the target block,
        # excluding any inside the inner hourly_if branch.
        def call_in_inner_hourly(call_node):
            if inner_hourly_if is None:
                return False
            for sub in ast.walk(inner_hourly_if):
                if sub is call_node:
                    return True
            return False

        has_15m_db_write = False
        for sub in ast.walk(target):
            if not isinstance(sub, ast.Call):
                continue
            if not (isinstance(sub.func, ast.Attribute)
                    and sub.func.attr in (
                        "insert_rejection",
                        "insert_evaluated_opportunity")):
                continue
            if call_in_inner_hourly(sub):
                continue
            has_15m_db_write = True
            break

        self.assertTrue(
            has_15m_db_write,
            "low_probability filter at line %d has no DB-write "
            "(insert_rejection/insert_evaluated_opportunity) call "
            "OUTSIDE the hourly/spx/weather branch. The 15M path "
            "silently continues — the Apr 25 outage class. Wire a "
            "DB row." % target.lineno)


class TestRejectionReasonsTaxonomyExpanded(unittest.TestCase):
    """The new wiring should use distinguishable rejection reasons.
    String-grep for the new reasons we expect to see in rejected_opportunities:

      - 'tradeable_false' or 'invalid_inputs' or 'sigma_move_zero' —
        the previously-silent tradeable=False reasons.
      - 'low_probability_15m' — the previously-JSONL-only path.

    The exact strings can vary; this test asserts at least SOME
    new descriptive reason appears in scan() that's not 'z_score'
    or 'refusing' or 'no_orderbook' or 'no_best_ask' (the existing
    reasons)."""

    def test_new_rejection_reasons_present(self):
        with open(BOT_PY) as f:
            src = f.read()
        # We expect either a new filter_stage / rejection_reason like
        # 'tradeable_false', 'invalid_inputs', 'sigma_move_zero',
        # 'low_probability_15m', or use of the dynamic reason from
        # prob_result['reason'] in insert_rejection unconditionally.
        # Easiest pin: the new behavior should reference the dynamic
        # `reason` variable in insert_rejection BEYOND the existing
        # z_score/refusing branch. Check that scan() body contains
        # at least one insert_rejection call where the reason argument
        # is the dynamic `reason` variable AND that call is NOT inside
        # an `if 'z_score' in reason` or similar gate.
        # (This is a soft check; the hard check is in the previous
        # tests.)
        self.assertTrue(
            "filter_stage='low_probability'" in src
            or 'filter_stage="low_probability"' in src
            or "tradeable_false" in src
            or "invalid_inputs" in src
            or 'rejection_reason=reason' in src.replace('\n', ''),
            "Expected at least one new descriptive rejection reason "
            "in bot/_impl.py corresponding to the previously-silent paths. "
            "Suggested values: 'low_probability', 'tradeable_false', "
            "'invalid_inputs'. The existing 'z_score'/'refusing'/"
            "'no_orderbook'/'no_best_ask' reasons are not enough — "
            "those are the wired ones; we want NEW reasons for the "
            "newly-wired paths.")


class TestRound1AdditionalSilentPaths(unittest.TestCase):
    """Round 1 review surfaced 3 more silent continues missed by the
    initial fix:
      - bot/_impl.py:8639  `if threshold is None: continue`
      - bot/_impl.py:8689  hourly/spx/weather NBBO out-of-range
      - bot/_impl.py:8711  weather `_wx_prob is None`

    All three now write `insert_rejection`. Pin the new reason
    strings so a future revert-to-silent shows up in tests."""

    def test_threshold_unparsable_path_writes_row(self):
        with open(BOT_PY) as f:
            src = f.read()
        self.assertIn(
            "threshold_unparsable", src,
            "Expected `threshold_unparsable` rejection_reason in "
            "scan() — covers Kalshi schema drift on floor_strike.")

    def test_price_out_of_range_early_writes_row(self):
        with open(BOT_PY) as f:
            src = f.read()
        self.assertIn(
            "price_out_of_range_early", src,
            "Expected `price_out_of_range_early` rejection_reason "
            "for the hourly/spx/weather NBBO range gate.")

    def test_weather_prob_none_writes_row(self):
        with open(BOT_PY) as f:
            src = f.read()
        self.assertIn(
            "weather_prob_none", src,
            "Expected `weather_prob_none` rejection_reason for the "
            "weather engine None case.")


class TestInsertFailuresLogWarning(unittest.TestCase):
    """Round 1 [P1-2]: insert_rejection failures around the new
    silent-continue wiring must log at WARNING level, not DEBUG.
    The whole point of the change is to surface failures — burying
    them at DEBUG defeats the purpose."""

    def test_no_debug_level_insert_rejection_failure_logs(self):
        with open(BOT_PY) as f:
            src = f.read()
        # We added these specific failure messages; assert they're
        # not at debug level.
        for failure_msg in (
                "tradeable_false insert_rejection failed",
                "tradeable_false (with_market) insert_rejection failed",
                "low_probability_15m insert_rejection failed",
                "threshold_unparsable insert_rejection failed",
                "price_out_of_range_early insert_rejection failed",
                "weather_prob_none insert_rejection failed",
        ):
            # Find the line that contains this failure message and
            # check the surrounding context for `logging.debug` (bad)
            # vs `logging.warning` (good).
            idx = src.find(f'"{failure_msg}"')
            self.assertGreater(
                idx, 0,
                f"Failure message not found: {failure_msg!r}")
            # Look at the line containing this message and adjacent
            # lines for the logging.X call.
            chunk_start = max(0, idx - 200)
            chunk = src[chunk_start:idx + len(failure_msg) + 50]
            self.assertNotIn(
                "logging.debug(", chunk,
                f"Failure log {failure_msg!r} appears to be at "
                f"DEBUG level. Round 1 [P1-2] requires WARNING — "
                f"these are the failures the whole change exists "
                f"to surface.")


class TestNewInsertSitesAreDeduped(unittest.TestCase):
    """Round 2 [A1]: the new insert_rejection sites must be wrapped
    in the existing `_eval_opp_seen` dedup to prevent commit-in-loop
    DB lock contention (CLAUDE.md PM-001).

    Per CLAUDE.md: 'Never commit inside a loop — always batch'. Even
    with INSERT OR IGNORE bounding row count, conn.execute() acquires
    the write lock on every call — sustained ~30/sec from the new
    paths during an outage would race supabase_sync's 165 reads/10s
    and recreate the Mar 9 2026 lock burst pattern."""

    def test_new_insert_sites_use_eval_opp_seen_dedup(self):
        """AST: every insert_rejection call we added must be inside
        an `if _dedup_key not in self._eval_opp_seen:` guard."""
        with open(BOT_PY) as f:
            tree = ast.parse(f.read())
        scan = None
        for cls in ast.walk(tree):
            if (isinstance(cls, ast.ClassDef)
                    and cls.name == "OpportunityScanner"):
                for fn in cls.body:
                    if isinstance(fn, ast.FunctionDef) and fn.name == "scan":
                        scan = fn
        self.assertIsNotNone(scan)

        # Find every insert_rejection call referenced by reason class
        # we added (so we don't accidentally grade the pre-existing
        # z_score path).
        new_reasons = {
            "threshold_unparsable",
            "price_out_of_range_early",
            "weather_prob_none",
            "low_probability_15m",
        }

        # Build parent map
        parents = {}
        for p in ast.walk(scan):
            for c in ast.iter_child_nodes(p):
                parents[id(c)] = p

        # Count and locate every insert_rejection call by its 4th
        # positional arg literal. R3 [F3]: assert count, not just
        # presence — silent removal of one site shouldn't be
        # vacuously OK.
        site_calls_by_reason = {}
        for sub in ast.walk(scan):
            if not isinstance(sub, ast.Call):
                continue
            if not (isinstance(sub.func, ast.Attribute)
                    and sub.func.attr == "insert_rejection"):
                continue
            if len(sub.args) < 4:
                continue
            arg = sub.args[3]
            if (isinstance(arg, ast.Constant)
                    and isinstance(arg.value, str)
                    and arg.value in new_reasons):
                site_calls_by_reason.setdefault(
                    arg.value, []).append(sub)
        self.assertEqual(
            set(site_calls_by_reason.keys()), new_reasons,
            f"Expected exactly the 4 new reason strings as "
            f"insert_rejection 4th-arg literals; got "
            f"{sorted(site_calls_by_reason.keys())}.")
        for reason in new_reasons:
            target_call = site_calls_by_reason[reason][0]
            self.assertIsNotNone(
                target_call,
                f"insert_rejection call for reason={reason!r} not found.")
            # Walk up parents looking for an `if X not in self._eval_opp_seen:`.
            cur = target_call
            found_dedup = False
            depth = 0
            while cur is not scan and depth < 15:
                p = parents.get(id(cur))
                if p is None:
                    break
                if isinstance(p, ast.If):
                    cond_src = ast.unparse(p.test)
                    if "_eval_opp_seen" in cond_src and (
                            " not in " in cond_src
                            or "not in" in cond_src):
                        found_dedup = True
                        break
                cur = p
                depth += 1
            self.assertTrue(
                found_dedup,
                f"insert_rejection for reason={reason!r} is not "
                f"wrapped in `if _dedup_key not in "
                f"self._eval_opp_seen:` guard. R2 [A1] regression "
                f"— without dedup, every scan tick fires the "
                f"insert and acquires the DB lock, hitting PM-001 "
                f"commit-in-loop pattern.")


if __name__ == "__main__":
    unittest.main()
