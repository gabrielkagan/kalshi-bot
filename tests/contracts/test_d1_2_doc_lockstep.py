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
    # D1.3-fu4 R2-M3: the worker-thread contract test file is the new
    # canonical pin for the BronzeArchiver._on_frame asyncio→worker
    # split. Adding to TRACKED_DOCS so the L99 ratchet covers it
    # (closes the asymmetric-coverage gap the R2 reviewer flagged).
    REPO_ROOT / "tests" / "contracts" / "test_bronze_archiver_worker_thread.py",
    # D1.6 fu R1-M5: monitor + sidecar narrative surfaces. The 144 LOC
    # added to collector_health_monitor.py + the new contract test file
    # carry the dropped-frames/STALE/SCHEMA alert documentation; close
    # the L99 lockstep gap at ship time (not as a future-Bit followup).
    REPO_ROOT / "scripts" / "ops" / "collector_health_monitor.py",
    REPO_ROOT / "tests" / "contracts" / "test_bronze_health_sidecar.py",
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
    # D2.1.5 (ticket 86b9zkpny, 2026-05-17): the coinbase_wire body
    # surfaces + their pinning contract tests. coinbase_wire/ mirrors
    # the kalshi_wire/ TRACKED_DOCS coverage pattern so any future Bit
    # that narrows / extends the Coinbase wire library is caught by the
    # L99 ratchet at ship time.
    REPO_ROOT / "coinbase_wire" / "__init__.py",
    REPO_ROOT / "coinbase_wire" / "auth.py",
    REPO_ROOT / "coinbase_wire" / "ws_client.py",
    REPO_ROOT / "tests" / "contracts" / "test_coinbase_wire_no_bot_imports.py",
    REPO_ROOT / "tests" / "contracts" / "test_coinbase_wire_no_collector.py",
    REPO_ROOT / "tests" / "contracts" / "test_coinbase_wire_ws_client.py",
    REPO_ROOT / "tests" / "contracts" / "test_coinbase_wire_auth.py",
    REPO_ROOT / "tests" / "contracts" / "test_coinbase_wire_envelope.py",
    # D2.2 (ticket 86b9zkppk, 2026-05-17): the new CoinbaseArchiver
    # consumer + its 3 contract test files. Adding to TRACKED_DOCS so
    # the L99 ratchet covers any future drift on the Coinbase-bronze
    # consumer surface.
    REPO_ROOT / "collector" / "coinbase_archiver.py",
    REPO_ROOT / "tests" / "contracts"
        / "test_coinbase_archiver_on_session_start.py",
    REPO_ROOT / "tests" / "contracts"
        / "test_coinbase_archiver_worker_thread.py",
    REPO_ROOT / "tests" / "contracts"
        / "test_coinbase_archiver_skip_ack.py",
    # D2.3 (ticket 86b9zkppt, 2026-05-17): bot/feeds/coinbase.py
    # refactored to consume coinbase_wire.WSClient + its 2 new pinning
    # tests (the AST contract guard + the wire-equivalence differential).
    # Adding to TRACKED_DOCS so the L99 ratchet covers any future drift
    # on the Coinbase bot-side consumer surface.
    REPO_ROOT / "bot" / "feeds" / "coinbase.py",
    REPO_ROOT / "tests" / "contracts"
        / "test_bot_feeds_coinbase_delegates_to_wire.py",
    REPO_ROOT / "tests" / "equivalence"
        / "test_coinbase_wire_differential.py",
    # D2.3 R2-M1: agent_docs/config_reference.md described
    # SPOT_BUFFER_PERSIST_INTERVAL_S as flushing "async via
    # asyncio.to_thread" — the deleted pre-D2.3 mechanism. The R2-M1
    # fix retracted the asyncio.to_thread claim to "dedicated sampler
    # daemon thread"; adding this file to TRACKED_DOCS so the L99
    # ratchet catches any future drift on the persist-mechanism
    # narrative.
    REPO_ROOT / "agent_docs" / "config_reference.md",
    # D2.5 (ticket 86b9znq4w, 2026-05-18): the Coinbase-collector
    # systemd surface — NEW unit + wrapper + orchestrator + contract
    # tests + deploy.yml path-aware restart block. Adding to
    # TRACKED_DOCS so any future Bit that touches the Coinbase
    # deploy posture fires the L99 ratchet on sister-doc drift.
    REPO_ROOT / "ops" / "kalshi-coinbase-collector.service",
    REPO_ROOT / "coinbase-collector-start.sh",
    REPO_ROOT / "collector" / "coinbase_main_loop.py",
    REPO_ROOT / "tests" / "contracts"
        / "test_kalshi_coinbase_collector_systemd_unit.py",
    REPO_ROOT / "tests" / "contracts"
        / "test_collector_coinbase_main_loop.py",
    REPO_ROOT / "tests" / "contracts"
        / "test_deploy_yml_path_aware_coinbase_collector_restart.py",
    # D2.5 deploy.yml block landed in this file too — already tracked
    # by sister D1.5.x lockstep, but the explicit add here documents
    # the D2.5 dependency.
    REPO_ROOT / ".github" / "workflows" / "deploy.yml",
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


# ─── D1.3-fu4 (worker-thread decouple, 2026-05-17, ticket 86b9zk4hz) ────────
#
# L99 PARANOID-at-day-1: when fu4 pivots BronzeArchiver._on_frame from
# inline writer.write to worker-thread dispatch, retract the prior
# phrasings so sister docs cannot re-introduce them via copy-paste from
# a git blame. Same R1-M4 lesson as the D1.4 + D1.5 patterns above.

STALE_PATTERNS_POST_D1_3_FU4: list[str] = [
    # The fu4-PROVED-INSUFFICIENT D1.3-fu3 forward-tense "proper fix" prose.
    "proper fix is to decouple the bronze write from the asyncio loop",
    # Pre-fu4 KB / plan-doc framing — "loop blocks > 30s" framed as a
    # future-to-fix rather than a past-pivot. (Past-tense framing
    # "the loop USED TO block > 30s" or "the loop blocked past the
    # keepalive-ping-timeout window" is legitimate post-fix narrative
    # and won't substring-match these literals.)
    "Loop blocks > 30s",
    "loop blocks > 30s",
    # Pre-fu4 stopgap-still-stands phrasing.
    "ping_timeout stopgap PROVED INSUFFICIENT",  # past, but framed open-loop
    "82 × 1011 errors in 32 min, collector self-restart",
    "the collector is in a 1011 reconnect cycle every ~30 min until D1.3-fu4 lands",
    "until D1.3-fu4 lands",
    "until D1.3-fu4 ships",
    "D1.3-fu4 target",
    "D1.3-fu4 will",
    "after D1.3-fu4 lands",
    "future D1.3-fu4",
    # Pre-fu4 D1.6 health-monitor commentary about cadence.
    "Expected initial WS-reconnect alert cadence ~288/day",
    "~288 WS-reconnect alerts/day",
    # Pre-fu4 architectural framing — synchronous writer dispatch claim.
    "BronzeArchiver writes synchronously on the asyncio thread",
    # Pre-fu4 WSClient.stop() fire-and-forget claim (the bug C1 retracted).
    "WSClient.stop is fire-and-forget",
    "wire.stop is fire-and-forget",
    # R2-M1 narrowing — the broader "wire.stop guarantees no callbacks
    # after return" claim is also wrong (R2-M1 narrowed the claim to
    # "joins the asyncio thread with bounded timeout").
    "wire.stop guarantees no callbacks after return",
    # R2-m2 cleanup: the 3 dead-regex patterns from R1
    # (substring-with-.*) removed entirely rather than kept as
    # documented-but-never-matching anti-patterns — they were
    # misleading to a future maintainer assuming regex semantics.
]


@pytest.mark.parametrize("pattern", STALE_PATTERNS_POST_D1_3_FU4)
def test_no_post_d1_3_fu4_stale_forward_looking_phrase(pattern: str):
    """No tracked doc should still say a D1.3-fu4-pending phrase after
    D1.3-fu4 shipped.

    L99 PARANOID-at-day-1 ratchet extension for D1.3-fu4 (worker-thread
    decouple of BronzeArchiver._on_frame + WSClient.stop join_timeout
    kwarg). Same lesson as D1.4 / D1.5 R1-M4: encode retracted prose at
    ship time so sister-doc drift cannot re-introduce it.

    NOTE: pattern matching is plain substring (not regex). R1 included
    3 patterns with ``.*`` wildcards which (per the substring matcher)
    would never fire — those were removed in R2-m2 cleanup. To match a
    range of phrasings, add multiple literal-substring entries.
    """
    findings: list[str] = []
    for doc in TRACKED_DOCS:
        for lineno, line in _scan(doc, pattern):
            findings.append(f"{doc.relative_to(REPO_ROOT)}:{lineno}: {line}")
    assert not findings, (
        f"Stale D1.3-fu4-pending phrasing detected (pattern {pattern!r}):\n"
        + "\n".join(findings)
        + "\n\nL99 lesson (D1.2 R3, reaffirmed D1.3/D1.4/D1.5): lockstep "
        "ratchets must have PARANOID pattern coverage from day-1. If "
        "THIS pattern is a legitimate forward-looking phrase for a "
        "subsequent Bit, narrow it (add a qualifier that won't match "
        "historical D1.3-fu4 prose)."
    )


def test_d1_3_fu4_shipped_status_in_at_least_one_tracked_doc():
    """Positive assertion: at least one tracked doc explicitly marks
    D1.3-fu4 as SHIPPED. Catches the inverse failure mode where
    staleness patterns pass (no D1.3-fu4 mention at all) but the docs
    haven't been updated to claim D1.3-fu4 SHIPPED.
    """
    shipped_re = re.compile(
        r"D1\.3-fu4\s+SHIPPED|D1\.3-fu4.*shipped|shipped.*D1\.3-fu4",
        re.IGNORECASE,
    )
    matched_docs: list[str] = []
    for doc in TRACKED_DOCS:
        if not doc.is_file():
            continue
        if shipped_re.search(doc.read_text()):
            matched_docs.append(str(doc.relative_to(REPO_ROOT)))
    assert matched_docs, (
        "No tracked doc claims D1.3-fu4 SHIPPED — staleness ratchets "
        "clean but nothing affirms the ship. Update at least one of:\n  "
        + "\n  ".join(str(d.relative_to(REPO_ROOT)) for d in TRACKED_DOCS)
    )


# ─── D1.3-fu5 (skip ack enqueue, 2026-05-17, ticket 86b9zky3u) ──────────────
#
# fu5 closes the OOM-via-large-ack class that fu4 accidentally opened. Ack
# frames bind sid synchronously then RETURN — they no longer flow through
# the bronze write path. RCA + reasoning:
# `kb/failures/collector-oom-via-ack-queue-may17.md`.
#
# Retracted claims to encode per L99 ratchet:

STALE_PATTERNS_POST_D1_3_FU5: list[str] = [
    # The retracted pre-fu5 claim: ack frames flow through the write
    # path / land in _unrouted partition.
    "ack frames go to _unrouted",
    "acks go to _unrouted",
    "ack flows through the data path",
    "ack ALSO routes through the data path",
    "ack itself is part of the wire trace",
    "_unrouted partition receives ack",
    # Pre-fu5 _on_frame docstring claim that all frames share the same
    # 3-step path (sid lookup + seq alloc + enqueue). Post-fu5 acks short-
    # circuit after the bind.
    "Three steps execute synchronously here (must, for correctness)",
    # Pre-fu5 narrative: "bronze captures every frame".
    "bronze captures every frame",
    "Bronze captures every frame",
    # Pre-fu5 _unrouted purpose claim.
    "unrouted bytes go to a separate path for silver QA",
    # Pre-fu5 claim that the ack-write path was load-bearing.
    "the ack ALSO routes through the data path (we still write it to bronze",
    # The OOM regression itself (forward-tense narrative if anything
    # paraphrases the pre-fix state).
    "OOM-via-large-ack class will be addressed",
    "cgroup OOM kill from large-ack queue is unresolved",
    "subscribe-ack queue OOM unresolved",
    # R1-M1 retracts: sister-test comment phrasings that pinned the
    # pre-fu5 ack-writes-to-bronze behavior. Encode the retracted
    # strings so a future paraphrase cannot drift them back via copy-
    # paste from a git blame.
    "subscribe-acks ARE written to bronze",
    "subscribe-ack itself routes to writers[None]",
    "subscribe-ack itself went to writers[None]",
    "subscribe-ack that bound sid=42 also went through the queue",
    "the bind ack adds 1",
]


@pytest.mark.parametrize("pattern", STALE_PATTERNS_POST_D1_3_FU5)
def test_no_post_d1_3_fu5_stale_forward_looking_phrase(pattern: str):
    """No tracked doc should still say a D1.3-fu5-pending phrase after
    D1.3-fu5 shipped. L99 PARANOID-at-day-1 ratchet for the fu5
    semantic flip (ack frames removed from bronze write path).
    """
    findings: list[str] = []
    for doc in TRACKED_DOCS:
        for lineno, line in _scan(doc, pattern):
            findings.append(f"{doc.relative_to(REPO_ROOT)}:{lineno}: {line}")
    assert not findings, (
        f"Stale D1.3-fu5-pending phrasing detected (pattern {pattern!r}):\n"
        + "\n".join(findings)
        + "\n\nPer L99 + L106 (new with this Bit): when a Bit changes a "
        "data-flow contract, sister-doc retracts of the OLD contract MUST "
        "ship same-Bit to prevent paraphrase drift."
    )


def test_d1_3_fu5_shipped_status_in_at_least_one_tracked_doc():
    """Positive assertion: at least one tracked doc explicitly marks
    D1.3-fu5 as SHIPPED. Same pattern as fu4 sister check.
    """
    shipped_re = re.compile(
        r"D1\.3-fu5\s+SHIPPED|D1\.3-fu5.*shipped|shipped.*D1\.3-fu5",
        re.IGNORECASE,
    )
    matched_docs: list[str] = []
    for doc in TRACKED_DOCS:
        if not doc.is_file():
            continue
        if shipped_re.search(doc.read_text()):
            matched_docs.append(str(doc.relative_to(REPO_ROOT)))
    assert matched_docs, (
        "No tracked doc claims D1.3-fu5 SHIPPED — staleness ratchets "
        "clean but nothing affirms the ship. Update at least one of:\n  "
        + "\n  ".join(str(d.relative_to(REPO_ROOT)) for d in TRACKED_DOCS)
    )


# ─── D2.1.5 (coinbase_wire body, 2026-05-17, ticket 86b9zkpny) ──────────────
#
# D2.1.5 lands the bodies of coinbase_wire/auth.py + coinbase_wire/ws_client.py
# that D2.1 (PR #66) shipped as empty stubs. Two semantic flips this ratchet
# encodes per L99:
#
#   1. D2.1 forecast was "HMAC-SHA256 auth body lands at D2.1.5"; D2.1.5
#      NARROWED to public-channels-only per operator scope decision. Sister
#      docs that paraphrase the original forecast (HMAC sign() / make_ws_headers()
#      as the D2.1.5 deliverable) are stale post-ship — those functions exist
#      as NotImplementedError stubs reserved for a future private-channel Bit.
#   2. D2.1 scaffolding framed __init__.py with empty `__all__` and the
#      stub modules as "instantiating WSClient will fail with
#      NotImplementedError". Post-D2.1.5 WSClient is instantiable; Frame +
#      build_envelope + WSClient are re-exported at the package top-level.
#
# Additional retracts: HYPE-included claims (D2.1 generic "5 product set"
# silently assumed all 6 live assets are Coinbase-listed; D2.1.5 docstring
# explicitly scopes HYPE out and files the follow-up).

STALE_PATTERNS_POST_D2_1_5: list[str] = [
    # Pre-ship "stub / deferred" framing — false post-D2.1.5.
    "D2.1 ships SCAFFOLDING ONLY",
    "Body deferred to D2.1.5",
    "body deferred to D2.1.5",
    "instantiating WSClient will fail with NotImplementedError",
    "calling any function here will fail with NotImplementedError",
    "Removed at D2.1.5 when",
    "D2.1 stub: re-exports intentionally empty",
    "__all__: list[str] = []",
    # D2.1 forecast that D2.1.5 would ship HMAC — narrowed at kickoff.
    "D2.1.5: ``auth.py`` body (Coinbase HMAC-SHA256",
    "D2.1.5 will populate this with",
    "D2.1.5 will land the body",
    "D2.1.5 (auth + ws_client implementation)",
    "lands the body (HMAC auth",
    "(HMAC auth + WSClient body)",
    "HMAC auth + WSClient body",
    # Pre-ship forward-looking framing.
    "until D2.1.5 lands",
    "until D2.1.5 ships",
    "after D2.1.5 lands",
    "future D2.1.5",
    "D2.1.5 target",
    "D2.1.5 will",
    # R1 Mn4: bare "D2.1.5 lands" (without preceding "until" / "after")
    # was a coverage gap — add it. Catches "Bodies are stubs at D2.1;
    # D2.1.5 lands the implementations" and "D2.1.5 lands the body".
    "D2.1.5 lands the body",
    "D2.1.5 lands the implementations",
    # Pre-ship claim that scaffolding modules are "empty stub".
    "empty stub `coinbase_wire/__init__.py`",
    "empty stub modules (docstrings + the test guards)",
    # R1 C2: HYPE-on-Coinbase contradiction. The R1 prose claimed HYPE
    # is not a Coinbase-listed product / is a Hyperliquid token / D2.1.5
    # covers only 5/6 assets. bot/constants.py:717 explicitly verified
    # HYPE-USD live on Coinbase Exchange 2026-05-10; the R1 fix switched
    # the wire library to Coinbase Exchange WS so HYPE-USD is in scope.
    # Encode the retracted phrasings so sister docs cannot carry them
    # forward via paraphrase.
    "HYPE is NOT a Coinbase-listed product",
    "HYPE is not a Coinbase-listed product",
    "HYPE (Hyperliquid token)",
    "Coinbase doesn't list HYPE-USD",
    "Coinbase does not list HYPE-USD",
    "5/6 of the bot's live assets",
    "5/6 assets",
    "covers 5/6 of the bot",
    "BTC / ETH / SOL / XRP / DOGE",   # 5-asset list missing HYPE
    "BTC-USD, ETH-USD, SOL-USD, XRP-USD, DOGE-USD",
    # R1 C1: Advanced Trade endpoint claim. The R1 fix switched the
    # wire library to Coinbase Exchange WS — Advanced Trade is a
    # different API surface not targeted by D2.1.5. Patterns intentionally
    # narrow (NOT bare "Coinbase Advanced Trade" / "advanced-trade-ws...")
    # because the rewritten coinbase_wire docstrings legitimately mention
    # Advanced Trade as a disambiguation contrast ("this is NOT Advanced
    # Trade"); the ratchet must catch RETRACTED CLAIMS specifically, not
    # the contrasting-prose that prevents future drift back.
    'DEFAULT_WS_URL = "wss://advanced-trade-ws',
    "targets ``wss://advanced-trade-ws",
    "targets wss://advanced-trade-ws",
    "D2.1.5 targets Coinbase Advanced Trade",
    "D2.1.5 uses Coinbase Advanced Trade",
    "endpoint per their public docs. Sourcing here",  # old ws_client.py code-comment
    "Coinbase requires per-channel subscribe frames",
    "Coinbase requires a separate subscribe message per channel",
    "you cannot batch multiple channels in one frame",
    "you cannot batch multiple channels in one subscribe",
    # Per-connection sequence_num claim — Exchange WS uses per-product
    # `sequence` not per-connection `sequence_num`. The R4 reviewer
    # caught two new paraphrases that escape the previous patterns —
    # encoded below for L99 PARANOID coverage.
    "per-connection monotonic across all subscribed channels",
    "per-connection monotonic across all channels",
    "per-connection ``sequence_num``",
    "Coinbase's sequence_num is per-connection monotonic",
    "single global counter",
    # R4 M1: short-form paraphrase missing "monotonic" prefix; appeared
    # in test_coinbase_wire_ws_client.py docstring line 32.
    "per-connection across all channels",
    "per-connection across all channels rather than per-sid",
    # R4 M1: split-line paraphrase that line-by-line scanner CAN catch
    # in the line that holds the prefix.
    "per-connection monotonic across all",
    # R4 M2: Frame.channel-is-dispatch-key overclaim. Production
    # ws_client.py sets Frame.channel=None and uses msg_type as the
    # dispatch key.
    "``channel`` is the dispatch key",
    "channel is the dispatch key",
    "Frame.channel is the dispatch key",
    # `channels` singular signature (R1 fix flipped to plural).
    "build_public_subscribe_message(channel:",
    "build_public_subscribe_message(channel,",
    "build_public_subscribe_message(channel=",
    # Old default channel set (Advanced Trade names). The narrower
    # entries below catch flips back to the wrong channel-name flavor
    # even when the exact 6-element tuple changes.
    "DEFAULT_CHANNELS = (level2, market_trades, ticker, heartbeats, status, candles)",
    "DEFAULT_CHANNELS = (level2, market_trades, ticker, heartbeats, status,",
    "level2 / market_trades / ticker / heartbeats / status / candles",
    "(level2, market_trades, ticker, heartbeats, status, candles)",
    # Narrower patterns covering the post-R2 `level2_batch`-trim retract
    # of the 5-channel set (R2 M4 fix). If a sister doc reintroduces
    # `level2_batch` to the DEFAULT_CHANNELS tuple BEFORE D2.2 verifies
    # reachability, catch the regression at lockstep.
    "(level2_batch, matches, ticker, heartbeat, status)",
    "level2_batch | level2,",       # batch-vs-bare flip catch
    "Captures every load-bearing alpha signal — level2_batch",
    # R3 M1: SLASH-FORM coverage of the 5-channel paraphrase. R2 fix
    # only encoded the parens-comma form; R3 reviewer caught 4 surfaces
    # still describing the pre-R2 scope as "level2_batch / matches /
    # ticker / heartbeat / status". The slash-form is a different
    # paraphrase that must be caught independently.
    "level2_batch / matches / ticker / heartbeat / status",
    "(level2_batch / matches / ticker / heartbeat / status)",
    # R3 M2: WIRE-SHAPE 5-element JSON-literal retract. The pre-R3
    # auth.py example showed `"channels": ["level2_batch", "matches",
    # "ticker", "heartbeat", "status"]` as the implied default. R3
    # rewrites it to "any subset" + explicit 4-channel default.
    '["level2_batch", "matches", "ticker", "heartbeat", "status"]',
    '"channels": ["level2_batch", "matches", "ticker", "heartbeat"',  # multi-line
    # R3 reaffirms the L99 meta-ratchet: every paraphrase encoded above
    # was a R-N reviewer find — slash, parens-comma, JSON-literal.
    # Future paraphrases (XML-attr style, comma-separated bare list,
    # etc.) join here.
    #
    # NOTE on the `snapshot / l2update → level2_batch` patterns: those
    # pre-D2.5 entries retracted the FORWARD-LOOKING claim that the
    # dispatch table WOULD add those mappings "when reachability lands."
    # Post-D2.5 (ticket 86b9znq4w, 2026-05-18) the mappings DID land
    # (R0 spike confirmed reachability; bundle shipped same-Bit), so
    # the patterns are no longer false claims. Removed from this list
    # at D2.5 ship; sister-doc paraphrases in arrow-form now describe
    # the legitimate post-D2.5 dispatch shape.
    # R1 C1: candles channel claims. Coinbase Exchange WS has no
    # candles channel (Advanced Trade does, with a default 5-minute
    # granularity that's not consumer-configurable per the R1 RCA).
    # Drop all candles claims from sister docs.
    "candles 1m",
    "candles (1m",
    "candles 5m",
    "candles + market_trades",
    "smallest granularity Coinbase supports",
    "Coinbase WS candles subscribe takes one granularity",
    # M2: integration-test-file claim. The test docstring claimed
    # "behavior is pinned by tests/integration/test_coinbase_wire_ws_loop.py
    # added in this Bit alongside the body" — file does not exist. R1
    # fix retracts the claim and defers behavioral coverage to D2.2
    # (mirrors kalshi_wire pattern). Encode the false claim.
    "tests/integration/test_coinbase_wire_ws_loop.py",
    "added in this Bit alongside the body",
    # M3: WSClient-as-bronze-surface overclaim retracted in coinbase_wire
    # only. Kalshi side shares the phrasing (kalshi_wire/__init__.py:61);
    # that's out-of-scope for D2.1.5 and tracked separately. The narrower
    # pattern below catches the specific coinbase_wire docstring text that
    # was rewritten ("the envelope helpers + Frame dataclass + WSClient
    # are the canonical bronze surface" → "Frame + build_envelope are the
    # canonical bronze data surface; WSClient is the transport class").
    # Kept out of patterns because no precise substring distinguishes
    # the coinbase_wire phrasing from the kalshi_wire one.
    # R1 Frame.channel claim retract — Coinbase Exchange WS frames
    # don't carry a top-level `channel` field; Frame.channel is None
    # at the wire layer for D2.1.5.
    "``parsed[\"channel\"]`` if present",
    "channel: ``parsed[\"channel\"]``",
]


@pytest.mark.parametrize("pattern", STALE_PATTERNS_POST_D2_1_5)
def test_no_post_d2_1_5_stale_forward_looking_phrase(pattern: str):
    """No tracked doc should still say a D2.1.5-pending phrase after
    D2.1.5 shipped.

    L99 PARANOID-at-day-1 ratchet extension for D2.1.5 (coinbase_wire
    body landing + HMAC→public-only scope narrowing + HYPE-out
    decision). Same lesson as D1.4 / D1.5 / D1.3-fu4 / D1.3-fu5
    R1-M4 — encode retracted prose at ship time so sister-doc drift
    cannot re-introduce it via paraphrase from a git blame.
    """
    findings: list[str] = []
    for doc in TRACKED_DOCS:
        for lineno, line in _scan(doc, pattern):
            findings.append(f"{doc.relative_to(REPO_ROOT)}:{lineno}: {line}")
    assert not findings, (
        f"Stale D2.1.5-pending phrasing detected (pattern {pattern!r}):\n"
        + "\n".join(findings)
        + "\n\nL99 lesson (D1.2 R3, reaffirmed through D1.3-fu5): when a "
        "Bit narrows or changes a forecast, sister-doc retracts of the "
        "original forecast MUST ship same-Bit to prevent paraphrase drift."
    )


def test_d2_1_5_shipped_status_in_at_least_one_tracked_doc():
    """Positive assertion: at least one tracked doc explicitly marks
    D2.1.5 as SHIPPED. Catches the inverse failure mode where staleness
    patterns pass (no D2.1.5 mention at all) but the docs haven't been
    updated to claim D2.1.5 SHIPPED.
    """
    shipped_re = re.compile(
        r"D2\.1\.5\s+SHIPPED|D2\.1\.5.*shipped|shipped.*D2\.1\.5|"
        r"D2\.1\.5\s+BODY\s+SHIPPED",
        re.IGNORECASE,
    )
    matched_docs: list[str] = []
    for doc in TRACKED_DOCS:
        if not doc.is_file():
            continue
        if shipped_re.search(doc.read_text()):
            matched_docs.append(str(doc.relative_to(REPO_ROOT)))
    assert matched_docs, (
        "No tracked doc claims D2.1.5 SHIPPED — staleness ratchets "
        "clean but nothing affirms the ship. Update at least one of:\n  "
        + "\n  ".join(str(d.relative_to(REPO_ROOT)) for d in TRACKED_DOCS)
    )


# ─── D2.2 (coinbase_archiver body, 2026-05-17, ticket 86b9zkppk) ────────────
#
# D2.2 wires ``collector/coinbase_archiver.py`` — the consumer that turns
# coinbase_wire.WSClient into bronze-in-S3 for the Coinbase side. Mirror
# of the Kalshi-side BronzeArchiver shape, adapted for Exchange WS's
# single-conn + msg_type-dispatch model. The Bit applies the D1.3-fu4
# worker-thread decouple + D1.3-fu5 skip-ack-enqueue lessons FROM DAY 1.
#
# Forward-looking phrases that become FALSE post-D2.2-ship — encode per
# L99 PARANOID-at-day-1 so sister-doc drift cannot reintroduce them via
# paraphrase from a git blame.

STALE_PATTERNS_POST_D2_2: list[str] = [
    # Pre-D2.2 "D2.2 wires the consumer" framing (CLAUDE.md +
    # agent_docs/bot_layout.md surfaces).
    "D2.2 wires `collector/coinbase_archiver.py`",
    "D2.2 wires ``collector/coinbase_archiver.py``",
    "D2.2 wires the `collector/coinbase_archiver.py`",
    "D2.2 wires the ``collector/coinbase_archiver.py``",
    "D2.2 wires the consumer",
    # Pre-D2.2 "body not yet consumed" framing.
    "Body NOT yet consumed; D2.2",
    "body NOT yet consumed; D2.2",
    "body not yet consumed; D2.2",
    # "D2.2 (the archiver) is the right place" — pre-ship deferral.
    "D2.2 (the archiver) is the right place",
    "the D2.2 archiver (next Bit)",
    "D2.2 archiver (next Bit)",
    "D2.2 archiver (which lands next)",
    # Pre-D2.2 "lands at D2.2" forward-tense.
    "lands at D2.2",
    "land at D2.2",
    # Pre-D2.2 "the consumer (D2.2 archiver) will land".
    "the consumer (D2.2 archiver) will land",
    "(D2.2 archiver) will land",
    # Generic forward-tense D2.2 framings.
    "D2.2 target",
    "D2.2 will add",
    "D2.2 will wire",
    "D2.2 will ship",
    "D2.2 will land",
    "D2.2 will verify",
    "until D2.2 lands",
    "until D2.2 ships",
    "until D2.2 wires",
    "after D2.2 lands",
    "future D2.2",
    "D2.2 (next Bit)",
    # Pre-D2.2 "D2.2 archiver to verify-then-extend" pre-ship language.
    "D2.2 archiver to verify-then-extend",
    # The D2.1.5 module-docstring pre-D2.2 framing in coinbase_wire.auth:
    # "D2.2 archiver to verify-then-extend ... before bronze goes silent"
    # — post-D2.2 ship the archiver IS the place where that verification
    # would happen (no longer a forward-looking deferral).
    "the D2.2 archiver to verify-then-extend",
    # R1-M2 paraphrase coverage: the parenthetical "(post-D2.2)" form +
    # "the future ``collector/coinbase_archiver.py``" paraphrases that
    # escaped the literal-substring matcher on R1's first cut. Same L99
    # meta-ratchet lesson as the D1.4 R2-M1 / D1.5 R3-MN2 retracts —
    # encode the regressing phrasings at ship time so a sister doc that
    # paraphrases via git-blame copy-paste fires the ratchet at lockstep.
    "(post-D2.2)",
    "the future ``collector/coinbase_archiver",
    "the future `collector/coinbase_archiver",
    "future ``collector/coinbase_archiver.py`` at D2.2",
    "the future Coinbase bronze archiver",
    "future Coinbase bronze archiver",
    # R2-M1 / R2-M2 retract: the R1-M1 fix correctly flipped the FUNCTION
    # docstring + the code, but the DELEGATION CLAIM survived in the
    # MODULE docstring of coinbase_archiver.py + agent_docs/bot_layout.md
    # + the contract test module docstring. Same paraphrase-drift class
    # as the D1.5 R3 "match-what-the-audit-observed" retract — when
    # retracting a multi-surface claim, encode the retracted phrase as
    # STALE so it cannot survive in higher-level descriptions of the
    # same code. Per the R1-M1 retract: on_session_start does NOT
    # delegate to the WSClient default; it builds the payload itself.
    "delegates to the WSClient default",
    "delegates to the wire library's _default_on_session_start",
    "delegates to the wire library's ``_default_on_session_start``",
    "delegates to the wire layer",
    "accepts the WSClient default",
    "accepts that default; per-subscribe customization",
    "calls the underlying WSClient's default subscribe path",
    "doesn't second-guess the channel/product set",
    "consumer accepts the WSClient default",
    # R3-M1 + R3-N1 retract: the ASCII architecture diagram in
    # coinbase_archiver.py + the test-file docstring/error-message
    # described the on_session_start path as "default subscribe" /
    # "default-subscribe path" — both phrasings paraphrase the same
    # retracted delegation claim. Third consecutive round catching a
    # survivor of the R1-M1 retract — encode the surviving paraphrases
    # so future drift cannot re-introduce them via copy-paste from a
    # git blame or visual ASCII-diagram recall.
    "──► default subscribe",
    "the default subscribe never dispatches",
    "the default-subscribe path never fires",
    "default-subscribe path",
]


@pytest.mark.parametrize("pattern", STALE_PATTERNS_POST_D2_2)
def test_no_post_d2_2_stale_forward_looking_phrase(pattern: str):
    """No tracked doc should still say a D2.2-pending phrase after D2.2
    shipped.

    L99 PARANOID-at-day-1 ratchet extension for D2.2
    (collector/coinbase_archiver.py body landing). Same lesson as
    D1.3-fu4 / D1.3-fu5 / D2.1.5 — when a Bit narrows or fulfills a
    forecast, sister-doc retracts of the original forecast MUST ship
    same-Bit to prevent paraphrase drift.
    """
    findings: list[str] = []
    for doc in TRACKED_DOCS:
        for lineno, line in _scan(doc, pattern):
            findings.append(f"{doc.relative_to(REPO_ROOT)}:{lineno}: {line}")
    assert not findings, (
        f"Stale D2.2-pending phrasing detected (pattern {pattern!r}):\n"
        + "\n".join(findings)
        + "\n\nPer L99 + L106: when a Bit fulfills a forecast (D2.2 wires "
        "the consumer that D2.1.5 deferred), sister-doc retracts of the "
        "OLD forecast MUST ship same-Bit to prevent paraphrase drift."
    )


def test_d2_2_shipped_status_in_at_least_one_tracked_doc():
    """Positive assertion: at least one tracked doc explicitly marks
    D2.2 as SHIPPED. Catches the inverse failure mode where staleness
    patterns pass (no D2.2 mention at all) but the docs haven't been
    updated.
    """
    shipped_re = re.compile(
        r"D2\.2\s+SHIPPED|D2\.2.*shipped|shipped.*D2\.2",
        re.IGNORECASE,
    )
    matched_docs: list[str] = []
    for doc in TRACKED_DOCS:
        if not doc.is_file():
            continue
        if shipped_re.search(doc.read_text()):
            matched_docs.append(str(doc.relative_to(REPO_ROOT)))
    assert matched_docs, (
        "No tracked doc claims D2.2 SHIPPED — staleness ratchets "
        "clean but nothing affirms the ship. Update at least one of:\n  "
        + "\n  ".join(str(d.relative_to(REPO_ROOT)) for d in TRACKED_DOCS)
    )


# ─── D2.3 (bot/feeds/coinbase.py → coinbase_wire consumer, 2026-05-17,
#          ticket 86b9zkppt) ──────────────────────────────────────────────────
#
# D2.3 refactors ``bot/feeds/coinbase.py`` to consume
# ``coinbase_wire.ws_client.WSClient`` (parallel of the D1.1.5 Phase 3b
# refactor that did the same for ``bot/feeds/kalshi.py`` on the Kalshi
# side). Bot becomes a wire-library consumer: 3 sync callbacks
# (``_on_session_start`` / ``_on_frame`` / ``_on_session_end``) + a
# dedicated sampler daemon thread that replaces the pre-D2.3 asyncio
# ``_snapshot_loop`` coroutine.
#
# Forward-looking phrases that become FALSE post-D2.3-ship — encode per
# L99 PARANOID-at-day-1 so sister-doc drift cannot reintroduce them via
# paraphrase from a git blame.

STALE_PATTERNS_POST_D2_3: list[str] = [
    # Pre-D2.3 "future refactor" framing (appears in coinbase_wire
    # docstrings + agent_docs/bot_layout.md + CLAUDE.md).
    "future D2.3 refactor",
    "the future D2.3 refactor",
    "future D2.3",
    "until D2.3 lands",
    "until D2.3 ships",
    "until D2.3 refactors",
    "after D2.3 lands",
    "after D2.3 ships",
    "D2.3 target",
    "D2.3 will",
    "D2.3 (next Bit)",
    "D2.3 (the bot-side refactor)",
    # Present-tense forecast verbs that become past-tense post-ship.
    # Narrow + literal so a sister doc that paraphrases via git-blame
    # copy-paste fires the ratchet (same L99 lesson as D2.2 R1-M2).
    "D2.3 refactors `bot/feeds/coinbase.py`",
    "D2.3 refactors ``bot/feeds/coinbase.py``",
    "D2.3 refactors the bot",
    # Stub-fossil claims about the pre-D2.3 CoinbaseFeed shape.
    "`_swallow_persist_exception` moved to `bot/feeds/coinbase.py`",
    "_swallow_persist_exception moved to bot/feeds/coinbase.py",
    "hosts the `_swallow_persist_exception` done-callback helper",
    "hosts the _swallow_persist_exception done-callback helper",
    "alongside `CoinbaseFeed._snapshot_loop`",
    "alongside CoinbaseFeed._snapshot_loop",
    "CoinbaseFeed._snapshot_loop",
    # Pre-D2.3 CoinbaseFeed asyncio-internal claims (the bot used to own
    # its own asyncio event loop + websockets.connect; post-D2.3 the
    # wire owns those).
    "CoinbaseFeed runs an asyncio event loop in a daemon thread",
    "CoinbaseFeed._ws_loop",
    "CoinbaseFeed._handle_message",
    "CoinbaseFeed._run_thread",
    # Pre-D2.3 "bot owns WS" framing.
    "bot/feeds/coinbase.py owns its own WS",
    "bot/feeds/coinbase.py opens its own WS",
    "CoinbaseFeed opens its own WS connection",
    # The D2.2 R1-M1 retracted-delegation paraphrase class extended for
    # the bot side. Same defensive pattern as the D2.2 "delegates to the
    # WSClient default" retract — applies symmetrically to CoinbaseFeed:
    # the consumer MUST build its subscribe payload via the public
    # ``coinbase_wire.auth.build_public_subscribe_message`` helper, not
    # by reaching into the wire's private ``_default_on_session_start``.
    "CoinbaseFeed delegates to the WSClient default",
    "bot/feeds/coinbase.py delegates to the WSClient default",
    "CoinbaseFeed accepts the WSClient default",
    # R2-M1 retract: the pre-D2.3 persist mechanism was ``asyncio.to_thread(
    # self.persist_buffer)`` invoked from inside the asyncio _snapshot_loop
    # coroutine. Post-D2.3 the dedicated sampler daemon thread runs
    # persist directly — no asyncio dependency. Encode the retracted
    # descriptions so sister-doc drift cannot reintroduce the false
    # mechanism claim via paraphrase from a git blame.
    "Async via `asyncio.to_thread`",
    "Async via ``asyncio.to_thread``",
    "async via `asyncio.to_thread`",
    "async via ``asyncio.to_thread``",
    "asyncio.to_thread(self.persist_buffer)",
    "asyncio.to_thread persist",
    "asyncio.to_thread done-callback",
    "fire-and-forget via asyncio.to_thread",
    "off-loop via asyncio.to_thread",
]


@pytest.mark.parametrize("pattern", STALE_PATTERNS_POST_D2_3)
def test_no_post_d2_3_stale_forward_looking_phrase(pattern: str):
    """No tracked doc should still say a D2.3-pending phrase after D2.3
    shipped.

    L99 PARANOID-at-day-1 ratchet extension for D2.3 (bot/feeds/coinbase.py
    refactored to consume coinbase_wire.WSClient). Same lesson as D1.4 /
    D1.5 / D1.3-fu4 / D1.3-fu5 / D2.1.5 / D2.2 — when a Bit fulfills a
    forecast, sister-doc retracts of the original forecast MUST ship
    same-Bit to prevent paraphrase drift.
    """
    findings: list[str] = []
    for doc in TRACKED_DOCS:
        for lineno, line in _scan(doc, pattern):
            findings.append(f"{doc.relative_to(REPO_ROOT)}:{lineno}: {line}")
    assert not findings, (
        f"Stale D2.3-pending phrasing detected (pattern {pattern!r}):\n"
        + "\n".join(findings)
        + "\n\nPer L99 + L106: when a Bit fulfills a forecast (D2.3 "
        "refactors bot/feeds/coinbase.py to consume coinbase_wire), "
        "sister-doc retracts of the OLD forecast MUST ship same-Bit "
        "to prevent paraphrase drift."
    )


def test_d2_3_shipped_status_in_at_least_one_tracked_doc():
    """Positive assertion: at least one tracked doc explicitly marks
    D2.3 as SHIPPED. Catches the inverse failure mode where staleness
    patterns pass (no D2.3 mention at all) but the docs haven't been
    updated.
    """
    shipped_re = re.compile(
        r"D2\.3\s+SHIPPED|D2\.3.*shipped|shipped.*D2\.3",
        re.IGNORECASE,
    )
    matched_docs: list[str] = []
    for doc in TRACKED_DOCS:
        if not doc.is_file():
            continue
        if shipped_re.search(doc.read_text()):
            matched_docs.append(str(doc.relative_to(REPO_ROOT)))
    assert matched_docs, (
        "No tracked doc claims D2.3 SHIPPED — staleness ratchets "
        "clean but nothing affirms the ship. Update at least one of:\n  "
        + "\n  ".join(str(d.relative_to(REPO_ROOT)) for d in TRACKED_DOCS)
    )


# ─── D2.5 (ticket 86b9znq4w, 2026-05-18) ─────────────────────────────────
#
# D2.5 SHIPPED the Coinbase-collector systemd surface (NEW
# ops/kalshi-coinbase-collector.service + coinbase-collector-start.sh +
# collector/coinbase_main_loop.py + ops/install.sh N=2→N=3 extension +
# deploy.yml path-aware kalshi-coinbase-collector restart block +
# scripts/ops/collector_health_monitor.py dual-tier dispatch). Same
# L99 PARANOID-at-day-1 ratchet pattern: every pre-D2.5 forward-looking
# phrase about the systemd unit / installer / deploy.yml restart goes
# stale at ship time. Retracts ride same-Bit to prevent paraphrase
# drift from a git blame.
#
# D2.5 ALSO bundled the `level2_batch` promotion (R0 reachability spike
# at D2.5 kickoff confirmed public access). Some patterns previously in
# STALE_PATTERNS_POST_D2_1_5 specifically retracted the "D2.1.5
# deferred level2_batch to a followup" framing — those CONTINUE to be
# stale (D2.1.5 DID defer; that fact didn't change). New post-D2.5
# patterns retract a DIFFERENT class: the forward-looking phrasing
# about WHEN the promotion would land + "deferred pending in-archiver
# reachability verification" status framing that's no longer accurate
# post-D2.5.

STALE_PATTERNS_POST_D2_5: list[str] = [
    # Pre-D2.5 forward-looking framing for the systemd unit.
    "D2.5 target",
    "D2.5 will",
    "D2.5 will add",
    "D2.5 will ship",
    "D2.5 will deploy",
    "D2.5 will install",
    "D2.5 will write",
    "D2.5 will wire",
    "until D2.5 lands",
    "until D2.5 ships",
    "after D2.5 lands",
    "future D2.5",
    "D2.5 (next Bit)",
    "D2.5 (the systemd unit)",
    "D2.5 implementation target",
    # NOTE: "D2.5 ships the systemd unit" is grammatically ambiguous
    # (present-tense could read either as forward-looking "will ship"
    # OR as completed-action narrative "now-ships post-merge"). Pre-
    # ship sister docs used it in the forward-looking sense; post-
    # ship narrative uses it in the completed-action sense. The
    # ambiguity made this pattern unsafe as an L99 retract — it would
    # fire on legitimate post-ship descriptions. Removed from the
    # pattern list at D2.5 ship; pre-ship surfaces were retracted via
    # explicit "D2.5 SHIPPED ..." past-tense rewrite (see
    # collector/__init__.py docstring + ops/CLAUDE.md "D2.5 Coinbase
    # collector deploy" section).
    # Pre-D2.5 level2_batch-deferral framing (forward-looking status
    # claims that are FALSE post-D2.5 promotion). The HISTORICAL
    # "D2.1.5 deferred level2_batch" framing is still true and is
    # NOT retracted here — only the forward-looking pieces.
    "level2_batch deferred pending in-archiver reachability",
    "level2_batch deferred pending",
    "level2_batch is intentionally OMITTED at D2.1.5",
    "intentionally OMITTED from D2.1.5 defaults",  # R1-M1 paraphrase variant
    "level2_batch ... pending in-archiver reachability verification",
    "pending coinbase_wire's reachability promotion",
    "A followup ticket promotes level2_batch",
    "A followup ticket promotes `level2_batch`",
    "A followup ticket adds ``level2_batch`` to defaults",  # R1-M1 paraphrase
    "extend the dispatch dict + coinbase_wire.DEFAULT_CHANNELS together when that lands",
    "level2_batch lands once that channel is promoted",
    "level2_batch lands once",
    "lands in a followup once subscribe-success",  # R1-M2 paraphrase
    "is reserved for an in-archiver",  # R1-M2 paraphrase
    "verified-public subset at D2.1.5",  # R1-M2 paraphrase
    # R1-M8 ("is deferred" verb-be form — present-tense, forward-
    # looking, false post-D2.5). The PAST-tense form "was deferred
    # pending in-archiver reachability" is HISTORICAL legitimate
    # narrative (D2.1.5's actual state) and is NOT a stale claim — do
    # NOT add the "was deferred" variant here or it will false-positive
    # on legitimate "D2.1.5 originally deferred / was deferred at
    # D2.1.5 / D2.5 promoted what D2.1.5 had deferred" history sentences.
    "is deferred pending in-archiver reachability",
    # R1-M3 — pre-D2.5 NotImplementedError narrative listing the
    # 4-channel subscribe set as the current state.
    "D2.1.5 is public-only (default subscribe set: ticker / matches / heartbeat / status)",
    # R1 4-channel-default arithmetic class (8 surfaces — see R1 report).
    # Pure-substring patterns covering the phrasing variants found in
    # production source + sister docs + contract tests + error strings.
    # NOTE: keep these narrow — broad patterns like "4-channel" alone
    # would false-positive against legitimate HISTORICAL paragraphs
    # ("D2.1.5 originally shipped 4-channel"). Each pattern below names
    # the wire's CURRENT default as 4-channel, which post-D2.5 is wrong.
    "wire's 4-channel default",
    "wire's 4-channel ``DEFAULT_CHANNELS``",
    "wire's 4-channel DEFAULT_CHANNELS",
    "wider 4-channel set for bronze",
    "wider 4-channel ``DEFAULT_CHANNELS``",
    "defaults to a 4-channel set (ticker",
    "DEFAULT_CHANNELS (ticker + matches + heartbeat + status)",
    "wire library's 4-channel set",
    "(4 public Coinbase Exchange WS channels",
    "ships 4 verified-public channels (",
    "the verified-public subset at D2.1.5",
    "default subscribe set is the 4",
    # The mock-server enumeration "all 4 channels" tied to a
    # current-default claim.
    "the mock server emits all 4 channels regardless",
    "emits all 4 channels regardless of what the consumer subscribes",
    # R2-M1/M2: line-wrapped docstring drift. The L99 _scan helper is
    # LINE-BY-LINE — a multi-line phrasing like
    #
    #   When None, the wire library's
    #   default (ticker + matches + heartbeat + status) is
    #   used.
    #
    # would NEVER match a single-line substring pattern like
    # "wire's default (ticker + matches + heartbeat + status)" because
    # no single line contains the full pattern. The fix is per-line
    # fragment patterns covering the surviving wrapped variants. Each
    # fragment must be specific enough to NOT false-positive on
    # post-D2.5 "default (ticker + matches + heartbeat + status +
    # level2_batch) is" (which is a legitimate post-tense narrative).
    "default (ticker + matches + heartbeat + status) is",
    "Default covers the 4",
    "public Coinbase Exchange WS channels D2.1.5 subscribes",
    # R0 was the spike result — no longer a future-looking "we should run a spike"
    # paraphrase. Retract paraphrases that frame the spike as pending.
    "R0 spike pending",
    "R0 reachability spike pending",
    # Pre-D2.5 systemd-shape forward-looking framing.
    "Coinbase collector systemd unit deferred",
    "Coinbase collector systemd unit (deferred)",
    "kalshi-coinbase-collector unit (next Bit)",
    "kalshi-coinbase-collector unit (future Bit)",
    # Pre-D2.5 install.sh / health-monitor framing.
    "ops/install.sh will extend",
    "collector_health_monitor.py will extend",
    "dual-tier dispatch deferred",
    "dual-tier dispatch (future Bit)",
    # Pre-D2.5 sudoers-extension framing.
    "sudoers extension for kalshi-coinbase-collector deferred",
]


@pytest.mark.parametrize("pattern", STALE_PATTERNS_POST_D2_5)
def test_no_post_d2_5_stale_forward_looking_phrase(pattern: str):
    """No tracked doc should still say a D2.5-pending phrase after D2.5
    shipped.

    L99 PARANOID-at-day-1 ratchet extension for D2.5 (Coinbase-collector
    systemd unit + path-aware deploy.yml restart + health-monitor dual-
    tier dispatch + level2_batch promotion bundle). Same lesson as D1.4
    / D1.5 / D1.3-fu4 / D1.3-fu5 / D2.1.5 / D2.2 / D2.3 — when a Bit
    fulfills a forecast, sister-doc retracts of the original forecast
    MUST ship same-Bit to prevent paraphrase drift.
    """
    findings: list[str] = []
    for doc in TRACKED_DOCS:
        for lineno, line in _scan(doc, pattern):
            findings.append(f"{doc.relative_to(REPO_ROOT)}:{lineno}: {line}")
    assert not findings, (
        f"Stale D2.5-pending phrasing detected (pattern {pattern!r}):\n"
        + "\n".join(findings)
        + "\n\nPer L99 + L106: when a Bit fulfills a forecast (D2.5 "
        "ships the Coinbase-collector systemd unit + promotes "
        "level2_batch + extends deploy.yml + extends "
        "collector_health_monitor.py to dual-tier), sister-doc retracts "
        "of the OLD forecast MUST ship same-Bit to prevent paraphrase "
        "drift."
    )


def test_d2_5_shipped_status_in_at_least_one_tracked_doc():
    """Positive assertion: at least one tracked doc explicitly marks
    D2.5 as SHIPPED. Catches the inverse failure mode where staleness
    patterns pass (no D2.5 mention at all) but the docs haven't been
    updated.
    """
    shipped_re = re.compile(
        r"D2\.5\s+SHIPPED|D2\.5.*shipped|shipped.*D2\.5",
        re.IGNORECASE,
    )
    matched_docs: list[str] = []
    for doc in TRACKED_DOCS:
        if not doc.is_file():
            continue
        if shipped_re.search(doc.read_text()):
            matched_docs.append(str(doc.relative_to(REPO_ROOT)))
    assert matched_docs, (
        "No tracked doc claims D2.5 SHIPPED — staleness ratchets "
        "clean but nothing affirms the ship. Update at least one of:\n  "
        + "\n  ".join(str(d.relative_to(REPO_ROOT)) for d in TRACKED_DOCS)
    )


# ─── D1.3-fu4-oom-closure (staggered reconnect, 2026-05-19, ticket 86b9zk4hz REUSED) ──
#
# D1.3-fu4-oom-closure pivots `_replan_for_archivers` from tight-loop
# dispatch (all 7 archivers' `request_reconnect()` within <100ms) to
# staggered dispatch (`_RECONNECT_STAGGER_SECONDS`=20s between
# iterations). Closes the residual OOM-restart class (84 cgroup OOM-kills
# 2026-05-17 → 2026-05-19) that the D1.3-fu4 worker-thread decouple
# (2026-05-17, SAME ticket 86b9zk4hz REUSED — see
# `kb/decisions/d1-3-fu4-oom-closure-plan.md` ticket-reuse preamble)
# did NOT close. The `-oom-closure` suffix is the structural
# disambiguator from the 2026-05-17 worker-thread scope.

STALE_PATTERNS_POST_D1_3_FU4_OOM_CLOSURE: list[str] = [
    # The pre-Bit `_replan_for_archivers` phrasing — "on every archiver"
    # in the tight-loop sense. The post-Bit phrasing must add the
    # stagger semantics (the agent_docs/bot_layout.md R1-M2 amendment
    # is the canonical reference). Future revival via copy-paste from
    # a pre-Bit git blame must fire.
    "request_reconnect()` on every archiver.",
    "request_reconnect() on every archiver.",
    # Pre-Bit framing of the reconnect storm as "fire-and-forget so
    # order doesn't matter" — retracted; order matters for memory
    # pressure reasons (concurrent ack-parse heap peak).
    "request_reconnect is fire-and-forget so order doesn't matter",
    # Pre-Bit framing of the OOM mechanism as queue-overflow rather
    # than concurrent ack-parse heap pressure.
    "OOM was caused by queue overflow",
    "OOM was caused by write_queue overflow",
    # Pre-Bit claim that D1.3-fu5 skip-ack-enqueue closed the OOM-via-ack
    # class outright — it closed the ack-IN-QUEUE class, but the
    # ack-PARSE-on-asyncio-thread peak across 7 concurrent conns
    # remained open until D1.3-fu4-oom-closure.
    "fu5 skip-ack-enqueue closed the OOM-via-ack class",
    "D1.3-fu5 closed the OOM-via-ack class",
    # Pre-Bit "iterate as fast as possible" framing of _replan_for_archivers.
    "_replan_for_archivers should iterate as fast as possible",
    "iterate as fast as possible to minimize subscription drift",
    # Pre-Bit "All archivers reconnect concurrently on REST refresh"
    # framing — retracted; staggered.
    "All archivers reconnect concurrently on REST refresh",
    "all 7 archivers reconnect concurrently",
    # Pre-Bit "OOM-restart loop is unrecoverable" / "D1.3-fu4 worker-
    # thread decouple closes the OOM" overclaim — fu4 (2026-05-17)
    # closed the 1011 class but not the OOM class; only
    # D1.3-fu4-oom-closure (2026-05-19) closes the OOM-restart loop.
    "D1.3-fu4 worker-thread decouple closes the OOM",
    "D1.3-fu4 closes the OOM-restart loop",
]


@pytest.mark.parametrize(
    "pattern", STALE_PATTERNS_POST_D1_3_FU4_OOM_CLOSURE,
)
def test_no_post_d1_3_fu4_oom_closure_stale_forward_looking_phrase(
    pattern: str,
):
    """No tracked doc should still say a D1.3-fu4-oom-closure-pending
    phrase after the Bit shipped.

    L99 PARANOID-at-day-1 ratchet extension for D1.3-fu4-oom-closure
    (staggered reconnect in `_replan_for_archivers`). Same lesson as
    D1.3-fu4 / D1.4 / D1.5 R1-M4: encode retracted prose at ship
    time so sister-doc drift cannot re-introduce it via copy-paste
    from a pre-Bit git blame.

    NOTE: pattern matching is plain substring (not regex). To match a
    range of phrasings, add multiple literal-substring entries.
    """
    findings: list[str] = []
    for doc in TRACKED_DOCS:
        for lineno, line in _scan(doc, pattern):
            findings.append(f"{doc.relative_to(REPO_ROOT)}:{lineno}: {line}")
    assert not findings, (
        f"Stale D1.3-fu4-oom-closure-pending phrasing detected "
        f"(pattern {pattern!r}):\n"
        + "\n".join(findings)
        + "\n\nL99 lesson (D1.2 R3, reaffirmed D1.3/D1.4/D1.5/D1.3-fu4): "
        "lockstep ratchets must have PARANOID pattern coverage from "
        "day-1. If THIS pattern is a legitimate forward-looking "
        "phrase for a subsequent Bit, narrow it (add a qualifier that "
        "won't match historical D1.3-fu4-oom-closure prose)."
    )


def test_d1_3_fu4_oom_closure_shipped_status_in_at_least_one_tracked_doc():
    """Positive assertion: at least one tracked doc explicitly marks
    D1.3-fu4-oom-closure as SHIPPED. Catches the inverse failure mode
    where staleness patterns pass (no mention at all) but the docs
    haven't been updated to claim D1.3-fu4-oom-closure SHIPPED.

    The `-oom-closure` suffix is the structural disambiguator from the
    2026-05-17 D1.3-fu4 worker-thread decouple ship (same ticket
    `86b9zk4hz`, different scope).
    """
    shipped_re = re.compile(
        r"D1\.3-fu4-oom-closure\s+SHIPPED|"
        r"D1\.3-fu4-oom-closure.*shipped|"
        r"shipped.*D1\.3-fu4-oom-closure",
        re.IGNORECASE,
    )
    matched_docs: list[str] = []
    for doc in TRACKED_DOCS:
        if not doc.is_file():
            continue
        if shipped_re.search(doc.read_text()):
            matched_docs.append(str(doc.relative_to(REPO_ROOT)))
    assert matched_docs, (
        "No tracked doc claims D1.3-fu4-oom-closure SHIPPED — "
        "staleness ratchets clean but nothing affirms the ship. "
        "Update at least one of:\n  "
        + "\n  ".join(str(d.relative_to(REPO_ROOT)) for d in TRACKED_DOCS)
    )
