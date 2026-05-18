"""B5-fu3 (`86b9zygqc`) — L104/L107 sister-test slice-window cascade.

Walks every `candidates.append(...)` site in `bot/scanner/__init__.py`
and asserts each candidate-emitting path enforces a documented
stack-protection invariant against the post-B5 incident class:

    Strategy X fires a candidate while a different non-related
    strategy Y already has a Kelly-sized live position (or in-flight
    IOC retry, or same-tick candidate) on the same (ticker, side).

The B5 incident (`kb/failures/tm-stack-decided-may18.md`,
ticket 86b9zudg2) showed `terminal_momentum_98` stacking on top of an
in-flight `decided_t1` retry 22 seconds after the decided_t1 candidate
emitted in a prior scan tick. The pre-B5 TM gate scanned the per-tick
`candidates` list (empty for the prior tick) and the open-positions
table filtered to TM-same-price-group (no match for decided_*) —
both checks returned False, both candidates landed, the TM stack
doubled the real settlement loss.

L107 lesson (from the B5 postmortem, "Per-(ticker, side) entry-lock
is the right scope for stack gates"):

    Defensive pattern: any new strategy that submits IOC orders
    should be reviewed against the per-(ticker, side) lock — if it
    doesn't already check `not strategy.startswith("terminal_momentum")`
    (or the strategy-appropriate inverse), it can stack on top of TM
    itself.

L104 lesson (from the same postmortem, "Block-boundary anchor
brittleness — sister-test slice windows"): slice-from-marker patterns
in regression tests grow brittle as the underlying source code grows.
This contract test uses generous line-count windows (500 lines /
1500 lines for the main candidate path) with strategy-specific tokens
to mitigate the brittleness — a moderate-size gate refactor must
preserve the unique token names or update the registry.

This test extends the L107 invariant to ALL candidate-emitting paths
in the scanner, not just TM. Each `candidates.append(...)` site is
classified by its `strategy=` value and the surrounding gate block is
scanned for ONE of the three documented stack-protection mechanisms:

1. **Open-positions scan with a position-stack guard**: the enclosing
   block calls `self._state.get_open_positions()` AND filters by
   ticker — the strategy's gate sees existing live positions and
   either rejects or deflates sizing toward zero.

2. **Sizing-deflation by existing_exposure**: the enclosing block
   subtracts an `existing_exposure` (or per-strategy equivalent) from
   the candidate's position-size such that a non-zero existing
   exposure pushes the candidate into the `zero_sizing` rejection
   branch.

3. **Explicit exemption** (documented with a ClickUp ticket ID): the
   strategy is known to lack a stack-protection guard, the gap is
   tracked, and the exemption is bounded by a ticket reference. Any
   exemption without an open ticket fails the test.

Mechanism (1) is the canonical post-B5 fix shape for the TM intercept;
mechanism (2) is the main candidate path's implicit shape. The
exemption registry is the L104 "if you find an unguarded path that
IS new bug surface, file a ticket and document it here" escape hatch.

This is a CONTRACT test — it pins the gate-presence invariant in the
source AST. It does NOT exercise the gate at runtime; the live-trace
regression for the B5 incident itself is at
`tests/integration/test_tm_stack_decided_regression.py`.

If a future strategy is added that appends to `candidates` without a
stack-protection guard, this test will fail with a clear message
pointing the developer at either (a) adding a guard inline, or (b)
filing a ticket and adding the strategy to the exemption registry
with a reason.
"""
import ast
import os
import re
import sys
import unittest

REPO_ROOT = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
)
sys.path.insert(0, REPO_ROOT)

SCANNER_PATH = os.path.join(REPO_ROOT, "bot", "scanner", "__init__.py")


# ────────────────────────────────────────────────────────────────────
# Stack-protection registry
# ────────────────────────────────────────────────────────────────────
#
# For each strategy that appears as the `strategy=` value in a
# `candidates.append({...})` literal, declare ONE of:
#
#   - guard_tokens: a list of substring patterns; ALL must appear in
#     the block preceding the candidates.append site (within
#     PRECEDING_CONTEXT_LINES lines of the append call). These are
#     AST/source-text patterns, not semantic checks — the test is a
#     workflow ratchet, not a correctness verifier.
#
#   - exempt_ticket: a ClickUp ticket ID and a one-sentence reason
#     pinning a known-unguarded path to a tracked followup. The test
#     ACCEPTS this exemption but warns in the failure message that
#     the gap exists.
#
# A strategy with NEITHER fails the test. New strategies added to
# scanner that don't appear here also fail.
#
# Token patterns can be plain substrings (case-sensitive). They
# match against the source text of the preceding context block,
# with line-comments stripped.

GUARDED_STRATEGIES = {
    # LPNE — per-tick candidates + open-positions (any-strategy) guard
    "low_price_near_expiry": {
        "guard_tokens": [
            "_lpne_dc_overlap",  # per-tick candidates scan for decided_*
            "get_open_positions",  # open-positions query
            "_lpne_has_position",  # the position-stack short-circuit
        ],
    },
    # Terminal Momentum (post-B5 four-gate stack-protection)
    "terminal_momentum_*": {
        "guard_tokens": [
            "_tm_dc_overlap",  # per-tick candidates scan
            "_dc_retry_queue",  # cross-tick in-flight retry scan
            "get_open_positions",  # open-positions query
            "_tm_non_tm_position",  # B5 fix: per-(ticker, side) non-TM lock
        ],
    },
    # Decided Contract — per-window risk cap + ticker-exposure deflation
    "decided_t1": {"guard_tokens": ["_dc_existing_exposure", "get_open_positions"]},
    "decided_t1b": {"guard_tokens": ["_dc_existing_exposure", "get_open_positions"]},
    "decided_t2": {"guard_tokens": ["_dc_existing_exposure", "get_open_positions"]},
    "decided_t2_z25": {"guard_tokens": ["_dc_existing_exposure", "get_open_positions"]},
    "decided_t2_z2": {"guard_tokens": ["_dc_existing_exposure", "get_open_positions"]},
    # Bracket NO (weather) — per-ticker open-position guard.
    # (`_bn_existing` + `_bn_in_scan` are concurrent-cap protection
    # across-tickers, NOT per-(ticker, side) stack-protection — they
    # cap total bracket_no positions but allow a single same-ticker
    # stack. The stack-protection is `_bn_has_pos` at line ~5550.)
    "bracket_no": {
        "guard_tokens": [
            "get_open_positions",
            "_bn_has_pos",  # per-ticker open-position guard
        ],
    },
    # Weather NO live — open-positions guard on ticker
    "weather_no_live": {
        "guard_tokens": ["get_open_positions", "_wnl_has_pos"],
    },
    # Hourly NO live — open-positions guard on ticker
    "hourly_no_live": {
        "guard_tokens": ["get_open_positions", "_hno_has_pos"],
    },
    # Main candidate path (filter_stage="candidate" or "hourly_live") —
    # uses sizing-deflation by `existing_exposure` (open positions + resting
    # orders) which pushes the candidate into the zero_sizing rejection
    # branch when any non-zero exposure on the same ticker exists. This is
    # the implicit per-(ticker, *) entry-lock for the Kelly-sized path.
    "__main_candidate__": {
        "guard_tokens": [
            "existing_exposure",
            "get_open_positions",
            "get_resting_orders",
        ],
    },
}

# Strategies known to lack an open-position / candidates-scan stack-
# protection guard. Each MUST be paired with a tracked followup ticket.
# B5-fu3 surfaced these as part of the L104 sister-test cascade:
EXEMPT_STRATEGIES = {
    # weekend_discount: the `_wknd_dc_overlap` check is a z_score/price/STC
    # heuristic — it doesn't query open positions or scan the candidates
    # list, so a previously-filled non-DC position on the same ticker
    # (e.g., a Kelly-sized main candidate from an earlier tick) could in
    # principle be stacked-on by a subsequent weekend_discount candidate
    # at the next tick when the weekend discount band opens up. Same L104
    # class as the B5 TM incident but on the wknd intercept path. Filed
    # at B5-fu3 ship time as a tracked followup, NOT fixed in B5-fu3 (per
    # the pickup-prompt scope-limit discipline: "If any path is currently
    # stacking, that's a NEW BUG. Do NOT fix in this Bit. File a NEW
    # ticket and document the finding").
    "weekend_discount": {
        "ticket": "86ba05k5q",
        "reason": (
            "_wknd_dc_overlap is z/price/STC heuristic only — does NOT "
            "scan open positions or per-tick candidates for non-DC "
            "Kelly-sized entries on same (ticker, side). Same L104 "
            "class as B5 TM-stacks-on-decided. Followup filed."
        ),
    },
    "overnight_discount": {
        "ticket": "86ba05k5q",
        "reason": (
            "_ovn_dc_overlap is z/price/STC heuristic only — does NOT "
            "scan open positions or per-tick candidates for non-DC "
            "Kelly-sized entries on same (ticker, side). Same L104 "
            "class as B5 TM-stacks-on-decided. Followup filed."
        ),
    },
}

# How many lines BEFORE each `candidates.append(` call to scan for
# guard tokens. Chosen large enough to span the typical strategy gate
# block (LPNE ~25 lines, TM intercept ~200 lines, DC ~220 lines,
# wknd ~140 lines, main candidate path uses an exposure check ~1380
# lines upstream so it gets a wider window). 500 gives ~2x typical
# margin so a moderate-size gate refactor doesn't silently invalidate
# the guard check (L104 brittleness). If a gate genuinely grows past
# 500 lines, that's a code-smell signal worth refactoring; the
# explicit failure message tells the maintainer to widen the constant
# OR extract the gate into a helper function (which the AST walker
# would then need to follow into).
PRECEDING_CONTEXT_LINES = 500
MAIN_CANDIDATE_PRECEDING_LINES = 1500


def _scanner_source():
    with open(SCANNER_PATH, "r", encoding="utf-8") as f:
        return f.read()


def _strip_comments(src: str) -> str:
    """Strip line comments and triple-quoted strings so a comment
    mentioning a token doesn't trip the guard check.

    LINE-COUNT PRESERVING: replaces stripped content with blank
    characters (or blank lines for multi-line triple-quotes) so that
    line numbers in the output match the input. This is required so
    AST `lineno` values (1-based, against the original source) can be
    used to index into the stripped output's `splitlines()`.
    """
    def _blank_keep_newlines(match):
        # Replace match with the same number of newlines + spaces so
        # the resulting string has identical line/column shape.
        text = match.group(0)
        # Preserve newlines, replace everything else with spaces.
        return "".join("\n" if c == "\n" else " " for c in text)

    no_triple = re.sub(r'""".*?"""', _blank_keep_newlines, src, flags=re.DOTALL)
    no_triple = re.sub(r"'''.*?'''", _blank_keep_newlines, no_triple, flags=re.DOTALL)
    # Strip everything from `#` onward on each line but KEEP the line.
    stripped_lines = []
    for ln in no_triple.splitlines():
        idx = ln.find("#")
        stripped_lines.append(ln if idx < 0 else ln[:idx])
    return "\n".join(stripped_lines)


def _find_candidate_append_sites(src):
    """Return [(lineno, strategy_value), ...] for every
    `candidates.append({... "strategy": <value>, ...})` site in the
    scanner source.

    `strategy_value` is the raw AST source for the strategy literal
    (e.g., `"low_price_near_expiry"`, `f"terminal_momentum_{best_ask}"`,
    `_dc_strat`, `strategy`). The caller normalizes these to registry
    keys.
    """
    tree = ast.parse(src)
    sites = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        # Match `candidates.append(<dict>)`
        func = node.func
        if not (isinstance(func, ast.Attribute) and func.attr == "append"):
            continue
        if not (isinstance(func.value, ast.Name) and func.value.id == "candidates"):
            continue
        if len(node.args) != 1 or not isinstance(node.args[0], ast.Dict):
            continue
        dict_node = node.args[0]
        strategy_src = None
        for key, val in zip(dict_node.keys, dict_node.values):
            if isinstance(key, ast.Constant) and key.value == "strategy":
                strategy_src = ast.get_source_segment(src, val) or ""
                break
        sites.append((node.lineno, strategy_src or "<missing>"))
    return sites


def _normalize_strategy_key(strategy_src):
    """Map the AST source for a `strategy=` value to a registry key.

    Examples:
      `"low_price_near_expiry"`           -> `low_price_near_expiry`
      `f"terminal_momentum_{best_ask}"`   -> `terminal_momentum_*`
      `_dc_strat`                         -> `_dc_strat` (sentinel; the
                                            test expands this to each
                                            of the 5 DC tier strategies)
      `strategy`                           -> `__main_candidate__`
      `"bracket_no"`                       -> `bracket_no`
    """
    s = strategy_src.strip()
    # Plain string literal
    m = re.match(r'^["\']([\w_]+)["\']$', s)
    if m:
        return m.group(1)
    # f-string TM family
    if s.startswith('f"terminal_momentum_') or s.startswith("f'terminal_momentum_"):
        return "terminal_momentum_*"
    # _dc_strat sentinel — maps to the dict lookup at scanner line ~4398
    if s == "_dc_strat":
        return "_dc_strat"
    # bare `strategy` name (from evaluate_execution_strategy result)
    if s == "strategy":
        return "__main_candidate__"
    return "<unrecognized:" + s + ">"


class TestL104SingleDecisionPerWindow(unittest.TestCase):
    """For every `candidates.append(...)` site in scanner, assert the
    enclosing block has a documented stack-protection mechanism.
    """

    def test_every_candidate_append_site_has_stack_protection(self):
        src = _scanner_source()
        sites = _find_candidate_append_sites(src)
        self.assertGreater(
            len(sites), 0,
            "No candidates.append(...) sites found - AST walk likely broken "
            "(refresh anchors if scanner shape changed)."
        )
        no_comments_src = _strip_comments(src)
        no_comments_lines = no_comments_src.splitlines()

        problems = []
        seen_keys = set()

        for lineno, strategy_src in sites:
            key = _normalize_strategy_key(strategy_src)
            # Expand _dc_strat into each of the 5 DC tier entries.
            keys_to_check = (
                ["decided_t1", "decided_t1b", "decided_t2",
                 "decided_t2_z25", "decided_t2_z2"]
                if key == "_dc_strat"
                else [key]
            )
            for k in keys_to_check:
                seen_keys.add(k)

                # If this key is in EXEMPT_STRATEGIES, accept it but require
                # a tracked ticket reference.
                if k in EXEMPT_STRATEGIES:
                    exempt = EXEMPT_STRATEGIES[k]
                    if not exempt.get("ticket") or not exempt.get("reason"):
                        problems.append(
                            "line " + str(lineno) + " (strategy=" + repr(k) +
                            "): EXEMPT entry missing ticket or reason - "
                            "registry malformed"
                        )
                    continue

                if k not in GUARDED_STRATEGIES:
                    problems.append(
                        "line " + str(lineno) + " (strategy_src=" + repr(strategy_src) +
                        ", normalized=" + repr(k) + "): NEW candidate-emitting "
                        "strategy in bot/scanner/__init__.py without a stack-"
                        "protection registry entry. Either add it to "
                        "GUARDED_STRATEGIES with the guard tokens of its "
                        "enclosing gate block, or file a followup ticket "
                        "and add it to EXEMPT_STRATEGIES. See L104 in "
                        "kb/failures/tm-stack-decided-may18.md."
                    )
                    continue

                # Locate the preceding context block.
                window = (
                    MAIN_CANDIDATE_PRECEDING_LINES
                    if k == "__main_candidate__"
                    else PRECEDING_CONTEXT_LINES
                )
                # AST lineno is 1-based; convert to 0-based slice index.
                end_idx = lineno  # 0-based exclusive end -> includes line lineno-1
                start_idx = max(0, end_idx - window)
                context = "\n".join(no_comments_lines[start_idx:end_idx])

                required = GUARDED_STRATEGIES[k]["guard_tokens"]
                # Use word-boundary regex so a token like `existing_exposure`
                # does NOT silently match the substring inside an unrelated
                # `_dc_existing_exposure` token from a sister gate that
                # happens to fall within the line-count window.
                missing = [
                    t for t in required
                    if not re.search(r"\b" + re.escape(t) + r"\b", context)
                ]
                if missing:
                    problems.append(
                        "line " + str(lineno) + " (strategy=" + repr(k) +
                        "): enclosing gate block (lines " + str(start_idx + 1) +
                        "-" + str(lineno) + ") is missing required stack-"
                        "protection tokens: " + repr(missing) + ". Required "
                        "by L104 sister-test ratchet - every "
                        "candidates.append must enforce a single-decision-"
                        "per-(ticker, side) invariant via open-positions "
                        "scan OR sizing-deflation. If you intentionally "
                        "removed a guard, file a ticket and add this "
                        "strategy to EXEMPT_STRATEGIES."
                    )

        # Inventory check: ensure every registry entry actually corresponds
        # to a real site (catches stale registry entries that would silently
        # accept gate removal at non-existent sites).
        for k in GUARDED_STRATEGIES:
            if k not in seen_keys:
                problems.append(
                    "GUARDED_STRATEGIES entry " + repr(k) + " has no matching "
                    "candidates.append site in scanner - stale registry "
                    "entry (refresh if strategy was retired)."
                )
        for k in EXEMPT_STRATEGIES:
            if k not in seen_keys:
                problems.append(
                    "EXEMPT_STRATEGIES entry " + repr(k) + " has no matching "
                    "candidates.append site in scanner - stale registry "
                    "entry (refresh if strategy was retired)."
                )

        self.assertFalse(
            problems,
            "L104 sister-test sweep found stack-protection gaps:\n  - "
            + "\n  - ".join(problems),
        )

    def test_registry_has_exempt_tickets_filed(self):
        """Every EXEMPT_STRATEGIES entry MUST have a non-empty ticket
        reference and a non-empty reason. Empty fields fail.
        """
        for k, v in EXEMPT_STRATEGIES.items():
            self.assertTrue(
                v.get("ticket"),
                "EXEMPT_STRATEGIES[" + repr(k) + "] missing ticket field"
            )
            self.assertTrue(
                v.get("reason"),
                "EXEMPT_STRATEGIES[" + repr(k) + "] missing reason field"
            )

    def test_enumeration_covers_all_candidate_append_sites(self):
        """Self-test: the AST walk must find every textual
        `candidates.append(` in the scanner source. If a future strategy
        is added with a different append-syntax (e.g. via a helper
        function), the textual count drifts from the AST count and this
        test fires.
        """
        src = _scanner_source()
        ast_sites = _find_candidate_append_sites(src)
        # Textual count (allow whitespace between `candidates`/`.`/`append`)
        textual_count = len(re.findall(r"\bcandidates\s*\.\s*append\s*\(", src))
        self.assertEqual(
            len(ast_sites), textual_count,
            "AST walk found " + str(len(ast_sites)) + " candidates.append "
            "sites but textual grep found " + str(textual_count) + ". "
            "Either a new append-syntax slipped past the AST matcher "
            "(e.g., a helper function wrapping the call), or AST parsing "
            "failed on one of the dict literals. Refresh "
            "_find_candidate_append_sites to cover the new shape."
        )


if __name__ == "__main__":
    unittest.main()
