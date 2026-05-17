"""D1.2 + D1.3 + D1.4 + D1.5 sister-doc lockstep — break the prose-drift cycle proactively (L97 + L99).

Tickets `86b9ypn66` (D1.2) + `86b9ypn72` (D1.3) + `86b9ypn8r` (D1.4)
+ `86b9ypna4` (D1.5), all 2026-05-16.
Created post-D1.2 R2 adversarial review which surfaced 4 MAJOR findings
all in the same drift class: tracked sister docs still saying
"D1.2-D1.5" / "raises NotImplementedError" / "land at D1.2" after D1.2
had shipped. **D1.3 extends the ratchet with PARANOID pattern coverage
per the D1.2 L99 lesson** — broader TRACKED_DOCS + STALE_PATTERNS at
day one (rather than waiting for an adversarial round to surface a
blind spot, then patching reactively).

L97 from P2.3 live-promotion (8 adv rounds for narrative-prose drift) +
the parent agent's recommendation: rather than depend on grep
discipline at each sub-Bit's adversarial round, pin the structural
invariant as a contract test. If a future Bit reintroduces stale
forward-looking phrasing, this test fires immediately at the contract
tier gate (~5s budget) instead of being caught at R2 or later.

Patterns checked (D1.2 carry + D1.3 additions):

D1.2 patterns (false after D1.2 SHIPPED):
- ``land at D1.2-D1.5``: false after D1.2 shipped — should be D1.3-D1.5
  or D1.3-D1.4 (whichever the surface narrows to).
- ``raises NotImplementedError`` + ``D1.2``: false now; D1.2 is the body
  ship.
- ``D1.2 target``: implies D1.2 hasn't shipped.

D1.3 patterns (false after D1.3 SHIPPED):
- ``D1.3 target``: implies D1.3 hasn't shipped (subscription_manager body).
- ``D1.3 will add``: forward-looking; D1.3 already shipped.
- ``D1.3 will generalize``: ditto for main_loop multi-conn.
- ``D1.3's acceptance criterion`` (forward-tense): D1.3 met the criterion.
- ``until D1.3 lands``: D1.3 landed.
- ``until D1.3 subscription_manager``: D1.3 shipped subscription_manager.
- ``no subscribe frames are sent``: false post-D1.3 (on_session_start dispatches).
- ``BronzeArchiver does NOT send any subscribe frames``: same.
- ``rest_snapshot.py, subscription_manager.py``: stale enumeration —
  post-D1.3 only rest_snapshot remains.
- ``land at D1.3-D1.4``: false; only D1.4 remains.
- ``land at D1.3 (``: forward-looking gerund.

If you're shipping D1.4+ and a NEW forward-looking phrase needs to land
("D1.4-D1.5"), update this test's allow-list AT THE SAME TIME. The test
is a ratchet, not a permanent ban.

If you're updating closeout docs in `kb/decisions/` that contain
narrative-history-only references to the old forward-looking phrases,
add the closeout path to ``HISTORICAL_NARRATIVE_PATHS`` below — but
prefer rephrasing the closeout to use past-tense (e.g., "the original
D1.3 target was…") so the ratchet stays load-bearing.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]

# Tracked sister docs that describe the collector/ + kalshi_wire/
# architecture. Adding a new doc to this list extends the ratchet.
# R3 (D1.2) expanded coverage to include sister test files + kalshi_wire/
# source after R2's set proved too narrow (4 blind spots surfaced).
# D1.3 adds: new D1.3 contract tests + collector/subscription_manager.py
# (whose body lands at D1.3 — historical narrative referencing "D1.3
# target" must flip).
# SELF-EXCLUSION: this test file (`tests/contracts/test_d1_2_doc_lockstep.py`)
# is DELIBERATELY NOT in TRACKED_DOCS. Its STALE_PATTERNS_POST_D1_{2,3,4,5}
# lists contain Python string literals of every retracted phrase the
# ratchet hunts — those literals would self-trigger the parametrized
# scan_doc_for_pattern test if this file were included. A future
# contributor who adds this file to TRACKED_DOCS would see ~100
# spurious failures with no obvious cause. The exclusion is structural,
# not coincidental.
TRACKED_DOCS: list[Path] = [
    REPO_ROOT / "CLAUDE.md",
    REPO_ROOT / "CONTRIBUTING.md",
    REPO_ROOT / "README.md",
    REPO_ROOT / "README.template.md",
    REPO_ROOT / "agent_docs" / "bot_layout.md",
    REPO_ROOT / "collector" / "__init__.py",
    REPO_ROOT / "collector" / "__main__.py",
    REPO_ROOT / "collector" / "main_loop.py",
    REPO_ROOT / "collector" / "ws_connection.py",
    REPO_ROOT / "collector" / "writer.py",
    REPO_ROOT / "collector" / "uploader.py",
    REPO_ROOT / "collector" / "subscription_manager.py",
    REPO_ROOT / "collector" / "rest_snapshot.py",
    REPO_ROOT / "kalshi_wire" / "__init__.py",
    REPO_ROOT / "kalshi_wire" / "auth.py",
    REPO_ROOT / "kalshi_wire" / "ws_client.py",
    REPO_ROOT / "tests" / "contracts" / "test_collector_no_bot_imports.py",
    REPO_ROOT / "tests" / "contracts" / "test_kalshi_wire_no_collector.py",
    REPO_ROOT / "tests" / "contracts" / "test_kalshi_wire_no_bot.py",
    REPO_ROOT / "tests" / "contracts" / "test_collector_ws_consumes_wire.py",
    REPO_ROOT / "tests" / "contracts" / "test_collector_subscription_manager.py",
    REPO_ROOT / "tests" / "contracts" / "test_bronze_archiver_on_session_start.py",
    REPO_ROOT / "tests" / "contracts" / "test_collector_rest_snapshot.py",
    REPO_ROOT / "tests" / "integration" / "test_collector_main_loop_wireup.py",
    REPO_ROOT / "tests" / "integration" / "test_collector_rest_snapshot_refresh_cycle.py",
    # D1.5: systemd-deploy surface. ops/kalshi-collector.service +
    # collector-start.sh + the contract tests pin the deploy shape.
    REPO_ROOT / "ops" / "kalshi-collector.service",
    REPO_ROOT / "ops" / "kalshi-bot.service",
    REPO_ROOT / "ops" / "install.sh",
    REPO_ROOT / "ops" / "CLAUDE.md",
    REPO_ROOT / "collector-start.sh",
    REPO_ROOT / "tests" / "contracts" / "test_kalshi_collector_systemd_unit.py",
    REPO_ROOT / "tests" / "contracts" / "test_collector_start_sh_invokes_python_m.py",
    REPO_ROOT / "tests" / "unit" / "test_ops_systemd_unit_matches_repo.py",
    # Ticket 86b9xgz66 (Path C, post-D1.5 follow-up): the §3 writer-IAM
    # template and the writer-creds docstring both echoed the now-retracted
    # "PutObject ONLY (no Delete/Get/List)" claim. The rclone HeadObject
    # quirk requires s3:GetObject + s3:ListBucket; the live policy already
    # had those Actions, so verbatim re-run of the pre-Path-C template
    # against the live bucket would silently DROP the Get/List grants
    # (same REPLACE-semantics class as the §2 lifecycle clobber). Adding
    # both surfaces to TRACKED_DOCS so the ratchet covers the writer-IAM
    # narrative going forward.
    REPO_ROOT / "scripts" / "STATE_DB_BACKUP_SETUP.md",
    REPO_ROOT / "scripts" / "ops" / "state_db_s3_backup.py",
    # R1 N6: the operator-facing installer also carried a stale
    # "writer IAM only has PutObject (no Delete)" comment that the
    # initial Path C sweep missed. Add to TRACKED_DOCS so the ratchet
    # catches future drift on this surface.
    REPO_ROOT / "scripts" / "ops" / "setup_state_db_backup_timer.sh",
]

# Patterns that are FALSE post-D1.2 SHIPPED. If any tracked doc above
# contains one of these literal substrings, the doc is stale.
# R3 (D1.2) added: "lands at D1.2", "at D1.2 the", "after D1.2 lands",
# "future D1.2", "until D1.2" — broader coverage of the same drift class.
STALE_PATTERNS_POST_D1_2: list[str] = [
    "land at D1.2-D1.5",
    "D1.2-D1.5 per the Data Corpus",  # README/template specific
    "D1.2 target",
    "implementations land at D1.2-D1.5",
    "lands at D1.2",
    "at D1.2 the",
    "after D1.2 lands",
    "future D1.2",
    "until D1.2",
]

# Patterns that are FALSE post-D1.3 SHIPPED. PARANOID coverage at day 1
# per L99 lesson — broader than the minimal set we'd patch reactively.
STALE_PATTERNS_POST_D1_3: list[str] = [
    "D1.3 target",
    "D1.3 will add",
    "D1.3 will generalize",
    "until D1.3 lands",
    "until D1.3 subscription_manager",
    "after D1.3 lands",
    "future D1.3",
    "lands at D1.3",
    "land at D1.3-D1.5",  # narrows to D1.4 post-D1.3
    "land at D1.3-D1.4",  # narrows to D1.4 post-D1.3
    "land at D1.3 (",
    # First-bronze-flow gating narrative — D1.3 closed this:
    "no subscribe frames are sent",
    "BronzeArchiver does NOT send any subscribe frames",
    "the WS connects but no data frames flow",
    "the WS connects but no data flows",
    "First-bronze-flow is D1.3's acceptance criterion",  # past-tense now
    "first-bronze-flow is D1.3's acceptance criterion",  # case variant
    # Stale enumeration — post-D1.3 only rest_snapshot is left.
    "rest_snapshot.py, subscription_manager.py",
    "rest_snapshot.py and subscription_manager.py",
    "subscription_manager.py, rest_snapshot.py",
    # Stale single-conn no-tier narrative — D1.3 generalized to multi-conn.
    "single-conn no-tier shape",
]

# Patterns that are FALSE post-D1.4 SHIPPED. PARANOID coverage at day 1
# per L99 lesson — extending the pattern set proactively rather than
# letting R-N rounds discover blind spots.
#
# Note: phrases that exclusively reference D1.5+ work (systemd deploy,
# operator decisions, etc.) MUST NOT be added here — those are still
# legitimately forward-looking after D1.4. Only patterns that became
# false at the moment D1.4 shipped go here.
STALE_PATTERNS_POST_D1_4: list[str] = [
    "D1.4 target",
    "D1.4 will add",
    "D1.4 will replace",
    "D1.4 (rest_snapshot, ",  # only-D1.5 unblocked phrasing post-D1.4
    "land at D1.4 (",
    "until D1.4 lands",
    "until D1.4 REST snapshot",
    "until D1.4 subscription_manager",  # belt-and-suspenders — was D1.3
    "after D1.4 lands",
    "future D1.4",
    "lands at D1.4",
    "D1.4 implementation target",
    "D1.4 replaces this",
    # Stub-fossil module docstring (rest_snapshot.py had a stub pre-D1.4).
    "REST-fallback redundancy for catalog refresh",
    "D1.4 (REST snapshot, 86b9ypn8r)  ← NEXT",  # pickup-chain phrasing
    # Stale single-file-pending narrative — post-D1.4 the rest_snapshot
    # body shipped; the "only rest_snapshot remains" framing is no
    # longer accurate as the pending-Bit description.
    "only rest_snapshot remains",
    "only D1.4 remains",
    # R1-M3 (D1.4 R1 adv finding): the original PARANOID set used
    # literal substring matching but missed parenthetical forms like
    # ``D1.4 (REST snapshot) will replace`` and ``before D1.4 REST
    # snapshot populates``. Extending coverage with the specific
    # phrases the R1 reviewer found across 3 collector source files.
    "D1.4 (REST snapshot) will replace",
    "D1.4 (REST snapshot) will populate",
    "D1.4 (REST snapshot) will add",
    "before D1.4 REST snapshot",
    "once D1.4 REST snapshot",
    "wait for D1.4 REST snapshot",
    "D1.4 REST snapshot populates",
    "D1.4 REST snapshot lands",
    # Phrases that explicitly tag the *production-default* as still being
    # the file seam are stale post-D1.4 (REST is the new default).
    "operator points COLLECTOR_TICKERS_FILE at",
    "COLLECTOR_TICKERS_FILE is the only seam",
    "no production file at it",
    # R2-M1/R2-Mn1/R3-M1 meta-pattern: when R1-M4 retracted the
    # "lock-protected against in-flight on_frame dispatch" overclaim at
    # the canonical docstring, sister surfaces (CLAUDE.md + bot_layout.md
    # + _replan_for_archivers docstring + the test-file module docstring)
    # echoed the now-retracted claim. Encode the retracted phrases as
    # ratchet patterns so a future Bit reintroducing them fires at the
    # contract tier instead of waiting for an adversarial round.
    "lock-protected atomic swap",
    "lock-protected against in-flight on_frame dispatch",
    # R3-M2 echo of R1-M2 retract: test/doc surfaces saying fetch
    # returns "empty ticker list / empty / partial map" on failure
    # paths are stale post-R1-M2 (fetch returns None on failure).
    "returns an empty ticker list",
    "empty page returns an empty",
    "return an empty / partial",
    "return an empty or partial",
    "return an empty/partial",
    # D1.4-fu (ticket ``86b9zjqhn``, commit ``0c19a0a``) — the original
    # D1.4 ship read ``row.get("status") != "open"`` and dropped 100%
    # of markets in production because Kalshi's response body labels
    # trading-active markets ``status="active"`` (the query-param vocab
    # and response-field vocab differ; the collector booted with
    # subscribes=0 and empty bronze on the first bronze day-zero run).
    # Fix accepted both ``open`` and ``active`` in the filter. Encode
    # the retracted "response status is open" / "filter responses by
    # status='open'" framings so a future sister-doc claim or copy-
    # paste regression at the response-handling site fires the ratchet
    # before R-N rounds discover it. Ticket ``86b9zjtf2`` (D1.4-fu
    # NIT-2 closure) — L99 meta-ratchet retroactive encoding for the
    # L97 vocab-drift class.
    'row.get("status") != "open"',
    "row.get('status') != 'open'",
    'response rows have status="open"',
    "response rows have status='open'",
    "response rows arrive with status=\"open\"",
    "response rows arrive with status='open'",
    "filter rows where status=\"open\"",
    "filter rows where status='open'",
    "response field is status=\"open\"",
    "response field is status='open'",
    'response-field is ``status="open"``',
    "Kalshi response body labels trading-active markets status=\"open\"",
    "Kalshi response body labels trading-active markets status='open'",
    "response body uses ``status=\"open\"``",
    "response-field vocab is status=\"open\"",
    "response-field vocab is status='open'",
    # Note: the legitimate query-parameter phrasings ``status=open`` (URL
    # query-string form) and ``params["status"] = "open"`` (Python kwarg
    # form) remain TRUE post-D1.4-fu (the QUERY is still status=open;
    # only the RESPONSE-side filter widened). Those phrasings are NOT
    # added here — narrowing the patterns above to the response-handling
    # context keeps the ratchet load-bearing.
]


# Patterns that are FALSE post-D1.5 SHIPPED. L99 PARANOID-at-day-1
# pattern coverage for D1.5 (systemd deploy: ops/kalshi-collector.service
# + collector-start.sh body refresh + install.sh multi-unit + F6
# bucket-policy/lifecycle extension + dedicated .env.collector).
#
# What's legitimately STILL forward-looking after D1.5:
# - D1.6+ health monitoring (shutil.disk_usage ≥80% used, conn-loss alert)
# - D1.7+ lifecycle hygiene
# - D1.8+ silver backfill
# - D1.9-D1.13 multi-source
# - D2.x silver/gold ETL
# Patterns explicitly tagging those phases MUST NOT be added here.
STALE_PATTERNS_POST_D1_5: list[str] = [
    "D1.5 target",
    "D1.5 will add",
    "D1.5 will install",
    "D1.5 will ship",
    "D1.5 will write",
    "D1.5 will wire",
    "until D1.5 lands",
    "until D1.5 systemd",
    "until D1.5 deploys",
    "after D1.5 lands",
    "future D1.5",
    "lands at D1.5",
    "land at D1.5",
    "D1.5 implementation target",
    "D1.5 unit ops/kalshi-collector.service",  # forward-looking from D1.1 stub
    "D1.5 (systemd deploy unit) remains",  # pre-ship status phrase
    "D1.5 (systemd deploy) remains pending",  # pre-ship status phrase
    # Note: do NOT add the longer "remains pending per the Data Corpus
    # architecture decision" — the shorter pattern below ("pending per
    # the Data Corpus architecture") subsumes it. R1-M4 trimmed the
    # redundancy so parametrize doesn't generate a dead test case.
    # The pre-D1.5 collector-start.sh stub claimed "SAME .env file as
    # the bot"; D1.5 moved to dedicated .env.collector. Encode the
    # retracted claim so it cannot drift back via copy-paste.
    "SAME .env file as the bot",
    "source /home/botuser/kalshi-bot-repo/.env",  # bot's repo-rooted env
    "D1.1 stub: this script is not yet operator-installed on the VPS",
    # D0.3 §12 operator decisions — post-D1.5 they are RESOLVED, not
    # pending. The 3 specific phrases each retract to a SHIPPED claim.
    "D0.3 §12 item #3 pending",
    # NOTE: do NOT add "D0.3 §12 item #3 pending operator decision" —
    # subsumed by the line above. R6 minor cleanup.
    "pending per the Data Corpus architecture",
    "operator decisions still pending",
    "three D0.3 §12 operator decisions",
    # F6 bucket-policy extension: pre-D1.5 it was a MINOR finding; at
    # D1.5 kickoff it promotes to MAJOR; post-ship it is RESOLVED.
    "F6 from D0.1 is a D1.5 blocker",
    "F6 from D0.1 (refresh STATE_DB_BACKUP_SETUP.md",
    # The D1.4 closeout's "first-bronze-in-S3 acceptance criterion:
    # within 30 minutes of `systemctl start kalshi-collector`" is the
    # D1.5 acceptance criterion specifically — post-ship it is past-
    # tense, but the verbatim phrase is fine to retain in CLOSEOUT
    # narrative under HISTORICAL_NARRATIVE_PATHS if needed.
    # R2 retracts (D1.5 R3 adv: encode retracted phrases per L99
    # meta-ratchet so a future Bit cannot drift them back via
    # copy-paste from a git blame).
    "the collector/ package files below are unchanged at D1.5",
    "Two rules:",  # §2 lifecycle narrative — now "Six rules"
    # R3-C1 retract: the "rules match what the audit observed" claim
    # was factually false because daily/ cadence diverged from the §2
    # template. Encode the retracted overclaim.
    "the 4 pre-D1.5 rules above",  # paraphrase superset
    "match what the audit observed on the live bucket",  # the false claim
    # R3-M1 retract: ops/CLAUDE.md said "re-run the same AWS-CLI steps
    # against the bucket — they are idempotent" for ALL of §1-§5. After
    # R3, §2 specifically requires GET-merge-PUT; the broad "all
    # idempotent" framing was misleading.
    "On an existing pre-D1.5 bucket: re-run the same AWS-CLI steps against the bucket — they are idempotent",
    # R4 retract: §12's "re-run §1-§5 verbatim — No hand-extension is
    # required" prose contradicted §2's GET-merge-PUT requirement.
    "re-run the AWS-CLI steps from those sections verbatim against your bucket",
    "the templates are idempotent (`put-bucket-lifecycle-configuration` / `put-bucket-policy` / `put-user-policy` all REPLACE",
    "No hand-extension is required",
    # R5 retract: ops/CLAUDE.md:87 had a sister-paragraph echo of the
    # R4-M1 retracted "verbatim — idempotent" claim. Same class as the
    # §12 retract, different surface.
    "re-run §1-§5 verbatim against it — the AWS-CLI templates are idempotent",
    # R7 retract: stale forward-tense "stub" / "will exec" / "D1.5
    # wires" phrasing in test_collector_no_bot_imports.py docstrings.
    # Post-D1.5 the wrapper is no longer a stub and the unit has wired
    # the ExecStart (past tense).
    "collector-start.sh`` stub exists",  # the test-list bullet phrasing
    "shell wrapper stub",
    "D1.5 systemd unit will exec it",
    "D1.5 wires the unit",
    "D1.5 (systemd unit, requires-approval) wires",
    # Ticket 86b9xgz66 (Path C post-D1.5 follow-up) — writer-IAM
    # rclone-quirk retracts. The pre-Path-C §3 template + sister-doc
    # narrative claimed the writer policy granted `s3:PutObject` only
    # (no Delete/Get/List). The live policy already had Get + List
    # (rclone HeadObject quirk), so verbatim re-run of the old template
    # would drop those grants. Post-Path-C: §3 template adds Get/List;
    # §5 reader-IAM Condition.StringLike is dropped (cosmetic — the 6
    # prefixes are the entire content set). Encode the retracted claims
    # as STALE patterns so sister docs cannot drift them back via
    # copy-paste from a git blame.
    "PutObject ONLY, no Delete/Get/List",  # § header form (line 222 pre-fix)
    "PutObject ONLY",  # terse form
    # R1 N4: hyphenated parenthetical form. The Path C R1 review found
    # `expire-install-probes` rule narrative at §2 still echoed
    # "(PutObject-only)" — same drift class, different surface form.
    "PutObject-only",
    "PutObject only)",  # closing-paren parenthetical
    "writer IAM only has PutObject",  # setup_state_db_backup_timer.sh form
    "s3:PutObject` only (no Delete/Get/List)",  # ops/CLAUDE.md:40 form
    "writer creds have `s3:PutObject` only",  # ops/CLAUDE.md:40
    "writer IAM grants `s3:PutObject` only",  # §2b RCA note (line 160 pre-fix)
    "s3:PutObject only. NO Delete, NO Get, NO List",  # docstring form
    "NO Delete, NO Get, NO List",  # docstring terse
    "no Delete/Get/List",  # generic
    # The "paranoia" claim that a compromised VPS "can't delete or read
    # prior snapshots" — post-Path-C the "or read" half is false (writer
    # can Get/List for HeadObject). Paranoia narrows to "no Delete"
    # (preserved by §3 + §2b Deny defense-in-depth).
    # NOTE: the `\n`-bearing multi-line form was removed (R7 NIT
    # cleanup) — per `_scan` line-by-line semantics it could never
    # match; the single-line variant below covers the load-bearing form.
    "can't delete or read prior snapshots",
    # The pre-Path-C §12 verify one-liner expected a 12-element Condition
    # StringLike array for the reader-IAM ListBucketAndVersions Sid.
    # Post-Path-C the Condition is dropped entirely; the verify checks
    # absence-of-Condition instead.
    "12-element array covering daily/, daily/*, journals/, journals/*",
    "A half-extended Condition that omits bronze/* makes",
    # Pre-Path-C the §3 template Sid was "PutObjectsOnly" — now a lie
    # (the Sid includes Get + List). Replaced by the structured Sids
    # below (PutAndHeadObject + ListBucketForRclone).
    "PutObjectsOnly Resource is a 6-element array",  # §12 verify form
    "\"Sid\": \"PutObjectsOnly\"",  # template-JSON form (escaped quotes)
    # R1 M3 retract: D1.5 was a partial close of F6 (Resource gap only);
    # Path C closes the rclone-Actions gap. "F6 closed by D1.5" claims
    # are factually wrong — the joint close belongs to D1.5 + Path C.
    "Ticket `86b9xgz66` (F6 from D0.1) is **CLOSED** by D1.5",
    "F6 from D0.1 — RESOLVED at D1.5",
    # R1 M5 retract: the per-prefix "All five must enumerate"
    # closing-summary at §12 — Path C added 2 more one-liners (now 7)
    # AND 2 of them check bucket-root Resource (not per-prefix).
    "All five must enumerate",
    # R1 M4 retract: the "6 prefixes are the bucket's entire content
    # set" justification ignored `_install_check/` (the install-probe
    # prefix that exists, has objects, but auto-expires @ 7d via the
    # `expire-install-probes` lifecycle rule). Replaced with
    # `_install_check/`-aware framing.
    # NOTE: pattern strings with literal `\n` would never match the
    # line-by-line scan in `_scan`; the single-line variant below is
    # the load-bearing form. R2 N2 cleanup removed the dead `\n` form.
    "the 6 prefixes are the bucket's entire content set",  # §12 verify form
    "no objects exist outside them",
    # R2 M1 retract: per-section "closes F6 from D0.1" overclaims at
    # §2b (D1.5 §2b alone) + §3 (D1.5 + Path C §3 alone). The actual
    # F6 close (per `kb/findings/s3-existing-corpus-audit.md` §4-F6)
    # is D1.5 §2 + §2b alone — both surfaces belong to F6.
    "— closes F6 from D0.1 per",  # §2b form
    "ships. Closes F6 from D0.1.",  # §3 form (terminal sentence)
    # R3 M1 retract: F6 redefined from canonical 2-surface (§2 lifecycle
    # + §2b bucket-policy per s3-existing-corpus-audit.md §4-F6) to a
    # 4-surface gap including writer-IAM + reader-IAM. The canonical
    # sources (kb/findings/s3-existing-corpus-audit.md:240-291 +
    # kb/decisions/data-corpus-architecture.md:75,446) explicitly scope
    # F6 to §2/§2b template drift. Path C §3 (Actions) + D1.5 §3/§5
    # (Resource) are ADJACENT template-drift fixes in the same class
    # but outside the original F6 scope.
    "F6 is a 4-surface gap",
    "4-surface gap — lifecycle / bucket-policy Deny / writer-IAM / reader-IAM",
    # R4 MN1 cleanup: keep only the broader substrings; narrower forms
    # were subsumed (substring `in` semantics in `_scan` means the
    # broader pattern catches every longer variant). Retracted longer
    # forms: "D1.5 §2 + §2b + §5 + Path C §3 jointly close F6 in full",
    # "Together, D1.5 + Path C close F6 from D0.1",
    # "F6 from D0.1 — RESOLVED by D1.5 + Path C", "...— RESOLVED at...".
    "D1.5 §2 + §2b + §5 + Path C §3 jointly close F6",
    "Together, D1.5 + Path C close F6",
    "F6 from D0.1 — RESOLVED by",  # subsumes "by D1.5 + Path C" + "(...)" variants
    # R3 MN2 retract: the "AWS returns 403-instead-of-404 when caller
    # lacks BOTH GetObject and ListBucket" mechanism description was
    # technically imprecise — the 403-vs-404 disclosure rule is gated
    # by ListBucket alone, not the conjunction with GetObject. Replaced
    # with the two-disclosure-rule explanation (GetObject for existing
    # keys, ListBucket for non-existent keys).
    "AWS returns 403-instead-of-404 for HeadObject when the caller lacks both",
    "lacks both `s3:GetObject` and `s3:ListBucket` on the resource",
    "granting both unconditionally avoids the quirk",
    # R3 MN3 retract: the "marginal regression because a compromised
    # VPS already holds the live state.db" rationale undersold the
    # post-Path-C exfiltration widening (writer can now also read
    # historical journals/, market_obs/, daily/ snapshots). The new
    # prose acknowledges the strict widening + explains the load-
    # bearing immutability property survives.
    "accepted as a marginal regression because a compromised VPS already holds the live state.db",
    "accepted as marginal regression because",
    # R3 ops/CLAUDE.md:135 header retract — the "F6 ... RESOLVED by
    # D1.5 + Path C" overclaim. Replaced with "S3 bucket-side template
    # alignment (D1.5 + Path C)". The "F6 from D0.1 — RESOLVED by"
    # pattern above already subsumes the parenthetical (F6...) form.
    # R5 M1+M2 retract: sister-doc echoes of the R3 MN2 retracted
    # mechanism explanation crept back in via different phrasings
    # in state_db_s3_backup.py:49 + ops/CLAUDE.md:40-(a). The
    # 403-instead-of-404 disclosure rule is gated by `s3:ListBucket`
    # alone (not the conjunction with GetObject); lacking GetObject
    # returns 403 unconditionally regardless of key existence. Encode
    # the regressing phrasings explicitly so the ratchet catches the
    # drift class going forward.
    "403-instead-of-404 when the caller lacks s3:GetObject",
    "the collector cannot upload to ANY destination",
    # R5 Mn1 retract: pinning a Path-C-era Sid name (`PutAndHeadObject`)
    # to the pre-D1.5 state is anachronistic; pre-D1.5 the Sid was
    # `PutObjectsOnly`. Better to refer to the user-policy name
    # `s3-put-only` (stable across eras) or just describe the
    # Resource shape.
    "writer-IAM `PutAndHeadObject` Resource is the canonical pre-D1.5",
]


# The "raises NotImplementedError" mention is only allowed in
# tests/  + .md histories. In the live collector/ source files, no
# function body should still raise NotImplementedError post-D1.2.
# D1.3 carries this rule forward — subscription_manager.py body now
# exists, so any NIE in it is fresh staleness.
NIE_ALLOWED_FILES: set[str] = {
    # historical narrative — D1.1/D1.1.5 closeout docs may legitimately
    # reference the prior stub-with-NotImplementedError state.
    # Add here if a NEW historical doc is created.
}

# Paths in ``kb/decisions/`` that legitimately reference stale forward-
# looking phrases as historical narrative (closeout docs that quote what
# the pre-ship state was). Out of scope for this ratchet — they are not
# in TRACKED_DOCS, but listing here makes the boundary explicit. NEW
# closeouts should prefer past-tense phrasing ("the original D1.3 target
# was…") so the ratchet remains load-bearing.
HISTORICAL_NARRATIVE_PATHS: set[str] = set()


def _scan(path: Path, needle: str) -> list[tuple[int, str]]:
    if not path.is_file():
        return []
    hits: list[tuple[int, str]] = []
    for i, line in enumerate(path.read_text().splitlines(), start=1):
        if needle in line:
            hits.append((i, line.strip()))
    return hits


@pytest.mark.parametrize("pattern", STALE_PATTERNS_POST_D1_2)
def test_no_post_d1_2_stale_forward_looking_phrase(pattern: str):
    """No tracked doc should still say a D1.2-pending phrase after D1.2 shipped."""
    findings: list[str] = []
    for doc in TRACKED_DOCS:
        for lineno, line in _scan(doc, pattern):
            findings.append(f"{doc.relative_to(REPO_ROOT)}:{lineno}: {line}")
    assert not findings, (
        f"Stale D1.2-pending phrasing detected (pattern {pattern!r}):\n"
        + "\n".join(findings)
        + "\n\nL97 lesson: prose drift across sister docs is the most "
        "common adversarial-review finding. Update ALL tracked surfaces "
        "in the same commit as the body change."
    )


@pytest.mark.parametrize("pattern", STALE_PATTERNS_POST_D1_3)
def test_no_post_d1_3_stale_forward_looking_phrase(pattern: str):
    """No tracked doc should still say a D1.3-pending phrase after D1.3 shipped.

    PARANOID coverage at day-1 per L99 — extending the pattern set
    proactively rather than letting R-N rounds discover blind spots.
    """
    findings: list[str] = []
    for doc in TRACKED_DOCS:
        for lineno, line in _scan(doc, pattern):
            findings.append(f"{doc.relative_to(REPO_ROOT)}:{lineno}: {line}")
    assert not findings, (
        f"Stale D1.3-pending phrasing detected (pattern {pattern!r}):\n"
        + "\n".join(findings)
        + "\n\nL99 lesson (from D1.2 R3): lockstep ratchets must have "
        "PARANOID pattern coverage from day-1 to avoid reactive R-N "
        "round patches. If THIS pattern is a legitimate D1.4+ forward-"
        "looking phrase, narrow it (e.g., add a specific qualifier that "
        "won't match historical D1.3 prose)."
    )


def test_collector_and_kalshi_wire_source_files_have_no_notimplemented_post_d1_2():
    """Live collector/ + kalshi_wire/ source modules must not contain a
    function body that ``raise NotImplementedError``s — D1.2 shipped the
    bodies, D1.3 shipped subscription_manager. Tests under tests/contracts/
    are out of scope (they may legitimately reference NotImplementedError
    in docstring narrative)."""
    findings: list[str] = []
    nie_re = re.compile(r"raise\s+NotImplementedError")
    for doc in TRACKED_DOCS:
        if doc.suffix != ".py":
            continue
        if doc.name in NIE_ALLOWED_FILES:
            continue
        rel = str(doc.relative_to(REPO_ROOT))
        # Only check collector/ and kalshi_wire/ live sources; tests are
        # narrative and may reference NIE in docstrings.
        if not (rel.startswith("collector/") or rel.startswith("kalshi_wire/")):
            continue
        for lineno, line in enumerate(doc.read_text().splitlines(), start=1):
            if nie_re.search(line):
                findings.append(f"{rel}:{lineno}: {line.strip()}")
    assert not findings, (
        f"Live collector/kalshi_wire source still raises NotImplementedError post-D1.2:\n"
        + "\n".join(findings)
    )


@pytest.mark.parametrize("pattern", STALE_PATTERNS_POST_D1_4)
def test_no_post_d1_4_stale_forward_looking_phrase(pattern: str):
    """No tracked doc should still say a D1.4-pending phrase after D1.4 shipped.

    L99 PARANOID-at-day-1 ratchet extension for D1.4 (REST snapshot
    body + RestSnapshotRefresher + main_loop hourly refresh wiring).
    """
    findings: list[str] = []
    for doc in TRACKED_DOCS:
        for lineno, line in _scan(doc, pattern):
            findings.append(f"{doc.relative_to(REPO_ROOT)}:{lineno}: {line}")
    assert not findings, (
        f"Stale D1.4-pending phrasing detected (pattern {pattern!r}):\n"
        + "\n".join(findings)
        + "\n\nL99 lesson (D1.2 R3, reaffirmed D1.3): lockstep ratchets "
        "must have PARANOID pattern coverage from day-1. If THIS pattern "
        "is a legitimate D1.5+ forward-looking phrase, narrow it (e.g., "
        "add a qualifier that won't match historical D1.4 prose)."
    )


def test_d1_4_shipped_status_in_at_least_one_tracked_doc():
    """Positive assertion: at least one tracked doc explicitly marks D1.4
    as SHIPPED. Catches the inverse failure mode where staleness patterns
    pass (no D1.4 mention at all) but the docs haven't been updated."""
    shipped_re = re.compile(
        r"D1\.4\s+SHIPPED|D1\.4.*shipped|shipped.*D1\.4",
        re.IGNORECASE,
    )
    matched_docs: list[str] = []
    for doc in TRACKED_DOCS:
        if not doc.is_file():
            continue
        if shipped_re.search(doc.read_text()):
            matched_docs.append(str(doc.relative_to(REPO_ROOT)))
    assert matched_docs, (
        "No tracked doc claims D1.4 SHIPPED — staleness ratchets clean "
        "but nothing affirms the ship. Update at least one of:\n  "
        + "\n  ".join(str(d.relative_to(REPO_ROOT)) for d in TRACKED_DOCS)
    )


@pytest.mark.parametrize("pattern", STALE_PATTERNS_POST_D1_5)
def test_no_post_d1_5_stale_forward_looking_phrase(pattern: str):
    """No tracked doc should still say a D1.5-pending phrase after D1.5 shipped.

    L99 PARANOID-at-day-1 ratchet extension for D1.5 (systemd deploy:
    ops/kalshi-collector.service + collector-start.sh body refresh +
    install.sh multi-unit + F6 bucket-policy/lifecycle extension +
    dedicated .env.collector).
    """
    findings: list[str] = []
    for doc in TRACKED_DOCS:
        for lineno, line in _scan(doc, pattern):
            findings.append(f"{doc.relative_to(REPO_ROOT)}:{lineno}: {line}")
    assert not findings, (
        f"Stale D1.5-pending phrasing detected (pattern {pattern!r}):\n"
        + "\n".join(findings)
        + "\n\nL99 lesson (D1.2 R3, reaffirmed through D1.4): lockstep "
        "ratchets must have PARANOID pattern coverage from day-1. If "
        "THIS pattern is a legitimate D1.6+ forward-looking phrase, "
        "narrow it (e.g., add a qualifier that won't match historical "
        "D1.5 prose)."
    )


def test_d1_5_shipped_status_in_at_least_one_tracked_doc():
    """Positive assertion: at least one tracked doc explicitly marks D1.5
    as SHIPPED. Catches the inverse failure mode where staleness patterns
    pass (no D1.5 mention at all) but the docs haven't been updated."""
    shipped_re = re.compile(
        r"D1\.5\s+SHIPPED|D1\.5.*shipped|shipped.*D1\.5",
        re.IGNORECASE,
    )
    matched_docs: list[str] = []
    for doc in TRACKED_DOCS:
        if not doc.is_file():
            continue
        if shipped_re.search(doc.read_text()):
            matched_docs.append(str(doc.relative_to(REPO_ROOT)))
    assert matched_docs, (
        "No tracked doc claims D1.5 SHIPPED — staleness ratchets clean "
        "but nothing affirms the ship. Update at least one of:\n  "
        + "\n  ".join(str(d.relative_to(REPO_ROOT)) for d in TRACKED_DOCS)
    )


def test_d1_3_shipped_status_in_at_least_one_tracked_doc():
    """Positive assertion: at least one tracked doc explicitly marks D1.3
    as SHIPPED. Catches the inverse failure mode where staleness patterns
    pass (no D1.3 mention at all) but the docs haven't actually been
    updated to claim D1.3 SHIPPED.
    """
    shipped_re = re.compile(
        r"D1\.3\s+SHIPPED|D1\.3.*shipped|shipped.*D1\.3",
        re.IGNORECASE,
    )
    matched_docs: list[str] = []
    for doc in TRACKED_DOCS:
        if not doc.is_file():
            continue
        if shipped_re.search(doc.read_text()):
            matched_docs.append(str(doc.relative_to(REPO_ROOT)))
    assert matched_docs, (
        "No tracked doc claims D1.3 SHIPPED — staleness ratchets clean "
        "but nothing affirms the ship. Update at least one of:\n  "
        + "\n  ".join(str(d.relative_to(REPO_ROOT)) for d in TRACKED_DOCS)
    )
