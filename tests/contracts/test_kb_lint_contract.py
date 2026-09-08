"""Contract pins for `.claude/skills/kb-lint/kb_lint.py`.

kb-lint drives `/kb-evolve`, which performs unrecoverable renames and deletes
against an untracked corpus. Three adversarial review rounds found defects that
a test would have caught cheaply — a gate that failed open on a bad `--repo`, a
classifier that would have stripped the range off `config_changes_mar21_27`, and
a separator rule that swallowed ordinary snake_case. These pin the behaviours
those rounds established so the next edit can't quietly undo them.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
LINT = REPO / ".claude" / "skills" / "kb-lint" / "kb_lint.py"

# Built from today: a hardcoded date silently ages past --stale-days and turns
# 60 filler files into FM-UPDATED-OLD findings, breaking every absence assertion.
FM = (f"---\nstatus: active\nupdated: {dt.date.today().isoformat()}\n"
      "tags: [x]\n---\n## Related\n")


def run(repo: Path, *args):
    return subprocess.run([sys.executable, str(LINT), "--repo", str(repo), *args],
                          capture_output=True, text=True)


def corpus(tmp_path: Path, files: dict[str, str], n_filler: int = 60) -> Path:
    """A corpus above kb-lint's MIN_FILES floor, plus the named files."""
    root = tmp_path / "repo"
    (root / "kb" / "concepts").mkdir(parents=True)
    for i in range(n_filler):
        (root / "kb" / "concepts" / f"filler{i}.md").write_text(FM)
    for rel, body in files.items():
        f = root / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(body)
    return root


def codes(repo: Path) -> dict[str, list[dict]]:
    r = run(repo, "--json")
    out: dict[str, list[dict]] = {}
    for f in json.loads(r.stdout)["findings"]:
        out.setdefault(f["code"], []).append(f)
    return out


def test_lint_script_exists():
    assert LINT.exists(), f"{LINT} missing — the kb-lint skill is broken"


# --- exit contract: 0 clean / 1 ERROR findings / 2 could not run -------------

def test_exit_2_when_kb_missing(tmp_path):
    """A wrong --repo must never report perfect health. Regression: the gate
    originally returned 0 findings and exit 0 on a nonexistent path."""
    (tmp_path / "empty").mkdir()
    assert run(tmp_path / "empty").returncode == 2


def test_exit_2_when_corpus_implausibly_small(tmp_path):
    assert run(corpus(tmp_path, {}, n_filler=3)).returncode == 2


def test_exit_0_on_clean_corpus(tmp_path):
    assert run(corpus(tmp_path, {})).returncode == 0


def test_exit_1_on_error_finding(tmp_path):
    root = corpus(tmp_path, {"kb/findings/a.md": FM + "[[nope-not-real]]\n"})
    assert run(root).returncode == 1


@pytest.mark.parametrize("flag", ["--quiet", "--json"])
def test_exit_contract_identical_across_output_modes(tmp_path, flag):
    root = corpus(tmp_path, {"kb/findings/a.md": FM + "[[nope-not-real]]\n"})
    assert run(root, flag).returncode == 1


# --- conflict classification -------------------------------------------------

def test_underscore_is_not_a_conflict_separator(tmp_path):
    """Sync clients use a space. An underscore rule eats snake_case titles and
    date ranges like config_changes_mar21_27."""
    root = corpus(tmp_path, {"kb/concepts/tier_2.md": FM,
                             "kb/concepts/config_changes_mar21_27.md": FM})
    found = codes(root)
    for code in ("DUPE-ORPHAN", "DUPE-AMBIGUOUS", "DUPE-IDENTICAL", "DUPE-NAMED"):
        assert not [f for f in found.get(code, []) if "tier_2" in f["file"]
                    or "mar21_27" in f["file"]], f"snake_case misclassified as {code}"


def test_lone_conflict_shaped_file_is_ambiguous_not_orphan(tmp_path):
    """No canonical, no link evidence, no batch: shape alone proves nothing."""
    root = corpus(tmp_path, {"kb/decisions/roadmap-may12-phase 2.md": FM})
    found = codes(root)
    assert "DUPE-AMBIGUOUS" in found
    assert "DUPE-ORPHAN" not in found, "a lone shaped file must never be auto-renamable"


def test_identical_copy_is_flagged_for_deletion(tmp_path):
    root = corpus(tmp_path, {"kb/decisions/x.md": FM, "kb/decisions/x 2.md": FM})
    assert "DUPE-IDENTICAL" in codes(root)


def test_divergent_copy_is_an_error(tmp_path):
    root = corpus(tmp_path, {"kb/decisions/x.md": FM,
                             "kb/decisions/x 2.md": FM + "different\n"})
    found = codes(root)
    assert "DUPE-DIVERGENT" in found
    assert found["DUPE-DIVERGENT"][0]["severity"] == "ERROR"


def test_sync_batch_makes_orphans_actionable(tmp_path):
    """Sync corruption is a BULK event. Link evidence alone has ~3% recall on a
    real event, because the notes that rot are the ones nothing links to."""
    files = {f"kb/decisions/note{i} 2.md": FM for i in range(6)}
    files.update({f"kb/failures/f{i} 2.md": FM for i in range(6)})
    files.update({f"kb/findings/g{i} 2.md": FM for i in range(6)})
    found = codes(corpus(tmp_path, files))
    assert len(found.get("DUPE-ORPHAN", [])) == 18, "batch evidence failed to fire"
    assert "DUPE-AMBIGUOUS" not in found


def test_empty_copy_is_never_renamed(tmp_path):
    """Renaming a 0-byte copy onto a canonical name shadows real content."""
    root = corpus(tmp_path, {"kb/decisions/ghost 2.md": ""})
    found = codes(root)
    assert "DUPE-EMPTY" in found
    assert "delete" in found["DUPE-EMPTY"][0]["fix"]


def test_link_evidence_marks_a_real_title_do_not_rename(tmp_path):
    root = corpus(tmp_path, {"kb/decisions/spec phase 2.md": FM,
                             "kb/findings/ref.md": FM + "[[spec phase 2]]\n"})
    found = codes(root)
    assert "DUPE-NAMED" in found
    assert "DUPE-ORPHAN" not in found


def test_a_copy_does_not_vouch_for_itself(tmp_path):
    """Self-links and rot-citing-rot are not evidence."""
    root = corpus(tmp_path, {"kb/decisions/selfy 2.md": FM + "[[selfy]]\n"})
    assert "DUPE-ORPHAN" not in codes(root)


# --- link resolution ---------------------------------------------------------

def test_dotted_stem_path_link_resolves(tmp_path):
    """with_suffix('.md') truncates bit-4.2.5.2-x to bit-4.2.5.md."""
    root = corpus(tmp_path, {"kb/decisions/bit-4.2.5.2-shipped.md": FM,
                             "kb/findings/l.md": FM + "[[decisions/bit-4.2.5.2-shipped]]\n"})
    found = codes(root)
    assert "LINK-BROKEN" not in found
    assert "LINK-MISPATHED" not in found


def test_wrong_path_is_mispathed_not_silently_resolved(tmp_path):
    """Bare-stem fallback on an explicit path is a false negative in a gate."""
    root = corpus(tmp_path, {"kb/concepts/thing.md": FM,
                             "kb/findings/l.md": FM + "[[decisions/thing.md]]\n"})
    assert "LINK-MISPATHED" in codes(root)


def test_links_in_code_fences_are_ignored(tmp_path):
    body = FM + "```\n[[not-a-real-link]]\n```\n`[[also-not-real]]`\n"
    root = corpus(tmp_path, {"kb/findings/doc.md": body})
    assert "LINK-BROKEN" not in codes(root)


def test_missing_memory_store_is_announced(tmp_path):
    """The memory-store suppression is load-bearing; failing silently turns
    valid memory-slug links into bogus ERRORs."""
    assert "MEM-STORE-MISSING" in codes(corpus(tmp_path, {}))


# --- frontmatter -------------------------------------------------------------

def test_inactive_is_not_active(tmp_path):
    root = corpus(tmp_path, {"kb/concepts/old.md":
                             "---\nstatus: inactive\nupdated: 2019-01-01\ntags: [radioactive]\n"
                             "---\n## Related\n"})
    assert "FM-UPDATED-OLD" not in codes(root)


@pytest.mark.parametrize("value", ["unknown", "2026-02-30"])
def test_unparseable_updated_is_reported(tmp_path, value):
    """An unparseable date must not read as 'fresh'."""
    root = corpus(tmp_path, {"kb/concepts/d.md":
                             f"---\nstatus: active\nupdated: {value}\ntags: [x]\n---\n"
                             "## Related\n"})
    assert "FM-UNPARSEABLE-DATE" in codes(root)


# --- skills ------------------------------------------------------------------

def test_routed_skill_without_a_directory_is_dead(tmp_path):
    root = corpus(tmp_path, {})
    (root / "CLAUDE.md").write_text("## Skill routing\n\n| x | `/ghost-skill` |\n")
    assert "SKILL-MISSING" in codes(root)


def test_conflict_copy_shadowing_a_skill_is_dead(tmp_path):
    root = corpus(tmp_path, {})
    (root / "CLAUDE.md").write_text("## Skill routing\n\n| x | `/rotted` |\n")
    d = root / ".claude" / "skills" / "rotted"
    d.mkdir(parents=True)
    (d / "SKILL 2.md").write_text("---\nname: rotted\n---\n")
    assert "SKILL-ROT" in codes(root)


# --- doomed-set propagation --------------------------------------------------
# Adversarial round 4 found that reverting the doomed-set fix left the suite
# green: DUPE-INBOUND-LINK escalates only if the target is actually scheduled to
# change, so every class kb-evolve deletes must land in `doomed`.

@pytest.mark.parametrize("kind,files", [
    ("identical", {"kb/decisions/x.md": FM, "kb/decisions/x 2.md": FM}),
    ("divergent", {"kb/decisions/x.md": FM, "kb/decisions/x 2.md": FM + "other\n"}),
    ("empty", {"kb/decisions/x 2.md": ""}),
])
def test_inbound_link_to_a_doomed_copy_is_an_error(tmp_path, kind, files):
    files = dict(files)
    files["kb/findings/cite.md"] = FM + "[[x 2]]\n"
    found = codes(corpus(tmp_path, files))
    errs = [f for f in found.get("DUPE-INBOUND-LINK", []) if f["severity"] == "ERROR"]
    assert errs, f"link to a doomed {kind} copy must be ERROR, not WARN"


def test_inbound_link_to_a_safe_copy_is_only_a_warning(tmp_path):
    """A conflict-shaped file nothing will touch must not flip the exit code."""
    root = corpus(tmp_path, {"kb/concepts/keep.md": FM,
                             "kb/decisions/keep 2.md": FM + "distinct\n",
                             "kb/findings/cite.md": FM + "[[keep 2]]\n"})
    found = codes(root)
    assert not [f for f in found.get("DUPE-INBOUND-LINK", []) if f["severity"] == "ERROR"]


def test_chain_members_are_all_doomed(tmp_path):
    root = corpus(tmp_path, {"kb/decisions/p 2.md": FM + "a\n",
                             "kb/decisions/p 3.md": FM + "b\n",
                             "kb/findings/cite.md": FM + "[[p 2]]\n"})
    found = codes(root)
    assert "DUPE-CHAIN" in found
    assert [f for f in found.get("DUPE-INBOUND-LINK", []) if f["severity"] == "ERROR"]


# --- evidence ladder ordering ------------------------------------------------

def test_full_name_link_evidence_outranks_batch_evidence(tmp_path):
    """A sync event elsewhere in the tree says nothing about a file that real
    docs cite by its full name. Batch may promote AMBIGUOUS, never NAMED."""
    files = {f"kb/decisions/rot{i} 2.md": FM for i in range(5)}
    files.update({f"kb/failures/rot{i} 2.md": FM for i in range(5)})
    files.update({f"kb/findings/rot{i} 2.md": FM for i in range(5)})
    files["kb/decisions/roadmap-phase 2.md"] = FM
    for i in range(3):
        files[f"kb/concepts/citer{i}.md"] = FM + "[[roadmap-phase 2]]\n"
    found = codes(corpus(tmp_path, files))
    named = [f for f in found.get("DUPE-NAMED", []) if "roadmap-phase 2" in f["file"]]
    orphan = [f for f in found.get("DUPE-ORPHAN", []) if "roadmap-phase 2" in f["file"]]
    assert named and not orphan, "batch evidence must not override full-name link evidence"


def test_single_stray_link_cannot_downgrade_a_divergence(tmp_path):
    """The copy holding the only instance of a finding must not be reclassified
    as 'a distinct document' because one doc happened to link the rotted name."""
    root = corpus(tmp_path, {"kb/decisions/plan.md": FM,
                             "kb/decisions/plan 2.md": FM + "unique content\n",
                             "kb/log.md": FM + "see [[plan 2]] for the numbers\n"})
    found = codes(root)
    assert "DUPE-DIVERGENT" in found, "one stray link must not hide a divergence"
    assert run(root).returncode == 1


def test_empty_copy_beside_intact_canonical_is_not_a_merge_request(tmp_path):
    root = corpus(tmp_path, {"kb/decisions/plan.md": FM, "kb/decisions/plan 2.md": ""})
    found = codes(root)
    assert "DUPE-EMPTY" in found
    assert "DUPE-DIVERGENT" not in found, "there is nothing to merge from a 0-byte file"


def test_sync_batch_threshold_is_not_trivially_low(tmp_path):
    """Two shaped files in one directory is not a sync event."""
    root = corpus(tmp_path, {"kb/decisions/a 2.md": FM, "kb/decisions/b 2.md": FM})
    found = codes(root)
    assert "DUPE-AMBIGUOUS" in found
    assert "DUPE-ORPHAN" not in found


def test_a_few_shaped_files_across_dirs_is_not_yet_a_sync_event(tmp_path):
    """Isolates the FILE-count threshold from the directory threshold: 3 files
    across 3 dirs clears the dir bar but not the file bar, so a lowered
    SYNC_BATCH_MIN_FILES is caught here."""
    root = corpus(tmp_path, {"kb/decisions/a 2.md": FM,
                             "kb/failures/b 2.md": FM,
                             "kb/findings/c 2.md": FM})
    found = codes(root)
    assert "DUPE-AMBIGUOUS" in found
    assert "DUPE-ORPHAN" not in found, "3 files is below the sync-event threshold"


def test_sync_event_can_be_forced_for_incremental_cleanup(tmp_path):
    """Fixing part of a batch drops the count below threshold; the override
    stops that stranding the remainder as never-touchable."""
    root = corpus(tmp_path, {"kb/decisions/a 2.md": FM, "kb/decisions/b 2.md": FM})
    r = run(root, "--json", "--sync-event")
    found = {}
    for f in json.loads(r.stdout)["findings"]:
        found.setdefault(f["code"], []).append(f)
    assert "DUPE-ORPHAN" in found and "DUPE-AMBIGUOUS" not in found


# --- index integrity ---------------------------------------------------------

def test_index_entry_with_a_wrong_path_is_diagnosed_not_called_missing(tmp_path):
    """Telling an operator to 'create the file' when it exists elsewhere makes
    them duplicate a real doc."""
    root = corpus(tmp_path, {"kb/concepts/real.md": FM})
    (root / "kb" / "_index.md").write_text("- [[decisions/real.md]] - x\n")
    found = codes(root)
    assert "INDEX-MISPATHED" in found
    assert "INDEX-DANGLING" not in found


def test_index_near_miss_hint_is_offered(tmp_path):
    root = corpus(tmp_path, {"kb/concepts/my_doc.md": FM})
    (root / "kb" / "_index.md").write_text("- [[my-doc]] - x\n")
    found = codes(root)
    hinted = [f for f in found.get("INDEX-DANGLING", []) if "did you mean" in f["message"]]
    assert hinted, "separator drift should offer the corrected slug"


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads through mode 000")
def test_unreadable_index_does_not_report_clean(tmp_path):
    """An index that cannot be read empties both the index checks and the
    curated tier — that must not look like health."""
    root = corpus(tmp_path, {"kb/concepts/real.md": FM})
    idx = root / "kb" / "_index.md"
    idx.write_text("- [[concepts/real.md]] - x\n")
    idx.chmod(0o000)
    try:
        assert run(root).returncode == 1
    finally:
        idx.chmod(0o644)


# --- path handling -----------------------------------------------------------

def test_repo_path_is_absolute_in_output(tmp_path):
    """A relative --repo mislocates the memory store, turning valid memory-slug
    links into bogus ERRORs."""
    root = corpus(tmp_path, {})
    r = subprocess.run([sys.executable, str(LINT), "--repo", "repo", "--json"],
                       capture_output=True, text=True, cwd=tmp_path)
    assert Path(json.loads(r.stdout)["repo"]).is_absolute()


def test_inbound_link_path_matches_on_components_not_string_suffix(tmp_path):
    """'kb/devops/runbook 2.md'.endswith('ops/runbook 2.md') is True — a raw
    suffix test blames the wrong file."""
    root = corpus(tmp_path, {"kb/ops/runbook.md": FM,
                             "kb/devops/runbook 2.md": FM,
                             "kb/devops/runbook.md": FM,
                             "kb/findings/cite.md": FM + "[[ops/runbook 2]]\n"})
    found = codes(root)
    assert not [f for f in found.get("DUPE-INBOUND-LINK", []) if f["severity"] == "ERROR"]


def test_rot_does_not_vouch_for_rot_across_files(tmp_path):
    """Two conflict copies citing each other's canonical names is evidence that
    lives entirely inside the rot. Distinct from the single-file case above."""
    root = corpus(tmp_path, {"kb/decisions/aye 2.md": FM + "[[bee]]\n",
                             "kb/decisions/bee 2.md": FM + "[[aye]]\n"})
    found = codes(root)
    assert not found.get("DUPE-ORPHAN"), "conflict copies must not be counted as linkers"


def test_empty_wikilink_is_not_reported_as_a_broken_reference(tmp_path):
    root = corpus(tmp_path, {"kb/findings/a.md": FM + "[[   ]]\n"})
    assert "LINK-BROKEN" not in codes(root)


# --- R5 regressions ----------------------------------------------------------

def test_one_doc_linking_twice_is_one_witness(tmp_path):
    """linkers() must count DISTINCT documents. A single log.md citing a rotted
    copy twice reaching the plurality bar re-opens the data-loss hole."""
    root = corpus(tmp_path, {
        "kb/decisions/plan.md": FM,
        "kb/decisions/plan 2.md": FM + "content held only here\n",
        "kb/log.md": FM + "see [[plan 2]] and again [[plan 2]]\n"})
    found = codes(root)
    assert "DUPE-DIVERGENT" in found, "one doc linking twice must not hide a divergence"
    assert run(root).returncode == 1


def test_aliases_and_anchors_from_one_doc_are_one_witness(tmp_path):
    root = corpus(tmp_path, {
        "kb/decisions/plan.md": FM,
        "kb/decisions/plan 2.md": FM + "content held only here\n",
        "kb/log.md": FM + "[[plan 2|alias]] and [[plan 2#sec]]\n"})
    assert "DUPE-DIVERGENT" in codes(root)


def test_batch_evidence_does_not_override_full_name_links_beside_a_canonical(tmp_path):
    """Both SKILL.mds promise batch evidence never overrides link evidence. It
    must hold on the canon-exists fork too, not just the orphan fork."""
    files = {f"kb/decisions/rot{i} 2.md": FM for i in range(5)}
    files.update({f"kb/failures/rot{i} 2.md": FM for i in range(5)})
    files.update({f"kb/findings/rot{i} 2.md": FM for i in range(5)})
    files["kb/concepts/tier.md"] = FM
    files["kb/concepts/tier 2.md"] = FM + "a genuinely distinct document\n"
    for i in range(3):
        files[f"kb/strategies/cite{i}.md"] = FM + "[[tier 2]]\n"
    found = codes(corpus(tmp_path, files))
    assert [f for f in found.get("DUPE-NAMED", []) if "tier 2" in f["file"]], \
        "a batch event must not doom a doc that real docs cite by full name"
    assert not [f for f in found.get("DUPE-DIVERGENT", []) if "tier 2" in f["file"]]


def test_forced_sync_event_does_not_claim_evidence_it_lacks(tmp_path):
    """The hand-confirm rests on the printed reason. A forced verdict must say
    it was forced, and must be distinguishable in --json."""
    root = corpus(tmp_path, {"kb/decisions/a 2.md": FM})
    r = run(root, "--json", "--sync-event")
    payload = json.loads(r.stdout)
    assert payload["stats"]["conflict_copies"]["batch_event_forced"] is True
    orphans = [f for f in payload["findings"] if f["code"] == "DUPE-ORPHAN"]
    assert orphans and "forced via --sync-event" in orphans[0]["message"]
    assert "NOT met" in orphans[0]["message"]
    assert "batch evidence met" not in orphans[0]["message"]


def test_nested_conflict_directory_is_caught(tmp_path):
    """A conflict dir that contains no .md directly was invisible."""
    root = corpus(tmp_path, {"kb/notes 2/sub/a.md": FM})
    assert "DUPE-DIR" in codes(root)


def test_missing_skill_routing_section_is_reported(tmp_path):
    """A renamed heading silently made every skill 'loadable'."""
    root = corpus(tmp_path, {})
    (root / "CLAUDE.md").write_text("# Project\n\n## Something Else\n\n`/ghost`\n")
    assert "SKILL-ROUTING-MISSING" in codes(root)


def test_unreadable_file_reports_exactly_one_io_error(tmp_path):
    root = corpus(tmp_path, {"kb/decisions/x.md": FM, "kb/decisions/x 2.md": FM + "d\n"})
    bad = root / "kb" / "decisions" / "x 2.md"
    bad.chmod(0o000)
    try:
        if os.geteuid() == 0:
            pytest.skip("root reads through mode 000")
        errs = codes(root).get("IO-ERROR", [])
        assert len([f for f in errs if "x 2.md" in (f["file"] or "")]) == 1
    finally:
        bad.chmod(0o644)


# --- R6 regressions ----------------------------------------------------------

def test_files_inside_a_conflict_directory_are_not_witnesses(tmp_path):
    """A sync client duplicates whole directories, which is how it manufactures
    a second independent-looking witness. kb/journal 2/log.md has a normal stem,
    so a file-stem-only filter counts it and the plurality bar collapses."""
    root = corpus(tmp_path, {
        "kb/decisions/plan.md": FM,
        "kb/decisions/plan 2.md": FM + "content held only here\n",
        "kb/journal/log.md": FM + "[[plan 2]]\n",
        "kb/journal 2/log.md": FM + "[[plan 2]]\n"})
    found = codes(root)
    assert "DUPE-DIVERGENT" in found, "a shadow-tree witness must not silence a divergence"


def test_shadow_witness_cannot_authorize_a_rename(tmp_path):
    root = corpus(tmp_path, {
        "kb/decisions/roadmap-phase 2.md": FM,
        "kb/notes 2/stale.md": FM + "[[roadmap-phase]]\n"})
    found = codes(root)
    assert not [f for f in found.get("DUPE-ORPHAN", []) if "roadmap-phase" in f["file"]], \
        "a witness inside a conflict directory must not evidence a lost original"


def test_conflict_dir_file_is_not_offered_as_a_canonical(tmp_path):
    root = corpus(tmp_path, {"kb/decisions/plan 2.md": FM,
                             "kb/notes 2/plan.md": FM})
    cross = codes(root).get("DUPE-CROSS-STORE", [])
    assert not cross, "a doomed shadow file must not be presented as the canonical to compare"


def test_files_under_a_conflict_dir_are_doomed(tmp_path):
    """R4 established that every class kb-evolve removes lands in `doomed`."""
    # the conflict dir must sit inside a CURATED tree, or the hygiene checks
    # would skip its contents anyway and the assertion proves nothing
    root = corpus(tmp_path, {"kb/concepts/notes 2/a.md": "no frontmatter, no Related\n"})
    found = codes(root)
    assert "DUPE-DIR" in found
    assert not found.get("NO-FRONTMATTER"), "files in a doomed dir need no hygiene backfill"
    assert not found.get("NO-RELATED")


def test_nested_conflict_dirs_report_once(tmp_path):
    root = corpus(tmp_path, {"kb/notes 2/sub 2/a.md": FM})
    dirs = codes(root).get("DUPE-DIR", [])
    assert len(dirs) == 1, "merging the outer directory subsumes the inner one"


def test_forced_sync_event_states_the_real_arithmetic(tmp_path):
    """Saying 'below the bar' when the count meets it is the same class of lie
    as claiming evidence you lack — the hand-confirm rests on this sentence."""
    files = {f"kb/decisions/a{i} 2.md": FM for i in range(6)}
    files.update({f"kb/failures/b{i} 2.md": FM for i in range(6)})
    files.update({f"kb/findings/c{i} 2.md": FM for i in range(6)})
    root = corpus(tmp_path, files)
    payload = json.loads(run(root, "--json", "--sync-event").stdout)
    assert payload["stats"]["conflict_copies"]["batch_event_detected"] is True
    msg = [f for f in payload["findings"] if f["code"] == "DUPE-ORPHAN"][0]["message"]
    assert "NOT met" not in msg, "18 files across 3 dirs meets the bar; don't claim otherwise"
    assert "MET" in msg


def test_suppressed_batch_mode_is_visible(tmp_path):
    """A stale --no-sync-event in a wrapper would silently strand real rot."""
    files = {f"kb/decisions/a{i} 2.md": FM for i in range(6)}
    files.update({f"kb/failures/b{i} 2.md": FM for i in range(6)})
    files.update({f"kb/findings/c{i} 2.md": FM for i in range(6)})
    payload = json.loads(run(corpus(tmp_path, files), "--json", "--no-sync-event").stdout)
    stats = payload["stats"]["conflict_copies"]
    assert stats["batch_event_suppressed"] is True and stats["batch_event_detected"] is True


def test_empty_wikilink_in_index_is_malformed_not_dangling(tmp_path):
    root = corpus(tmp_path, {})
    (root / "kb" / "_index.md").write_text("- [[  ]] - x\n")
    found = codes(root)
    assert "LINK-MALFORMED" in found
    assert "INDEX-DANGLING" not in found


def test_cross_store_canonical_is_reported(tmp_path):
    root = corpus(tmp_path, {"kb/findings/foo.md": FM, "kb/decisions/foo 2.md": FM + "d\n"})
    assert "DUPE-CROSS-STORE" in codes(root)


def test_index_membership_confers_curated_status(tmp_path):
    """decisions/ is not a curated dir; only the index entry makes it one."""
    root = corpus(tmp_path, {"kb/decisions/curated.md": "no frontmatter here\n"})
    (root / "kb" / "_index.md").write_text("- [[decisions/curated.md]] - x\n")
    assert "NO-FRONTMATTER" in codes(root)


def test_stale_days_threshold_is_honoured(tmp_path):
    recent = (dt.date.today() - dt.timedelta(days=30)).isoformat()
    root = corpus(tmp_path, {"kb/concepts/old.md":
                             f"---\nstatus: active\nupdated: {recent}\ntags: [x]\n---\n"
                             "## Related\n"})
    assert "FM-UPDATED-OLD" not in codes(root), "30d is inside the 180d default"
    r = run(root, "--json", "--stale-days", "5")
    assert "FM-UPDATED-OLD" in {f["code"] for f in json.loads(r.stdout)["findings"]}


def test_unreadable_claude_md_is_reported(tmp_path):
    if os.geteuid() == 0:
        pytest.skip("root reads through mode 000")
    root = corpus(tmp_path, {})
    cm = root / "CLAUDE.md"
    cm.write_text("## Skill routing\n\n`/x`\n")
    cm.chmod(0o000)
    try:
        assert "IO-ERROR" in codes(root)
    finally:
        cm.chmod(0o644)


# --- summary block (non-JSON stdout) ----------------------------------------
# R7 found that every stdout assertion in this file went through json.loads, so
# the human-readable summary — the only thing --quiet prints, and what an
# operator actually reads before authorizing a bulk rename — was unpinned.

def batch_files(n_per_dir: int = 6) -> dict:
    f = {f"kb/decisions/a{i} 2.md": FM for i in range(n_per_dir)}
    f.update({f"kb/failures/b{i} 2.md": FM for i in range(n_per_dir)})
    f.update({f"kb/findings/c{i} 2.md": FM for i in range(n_per_dir)})
    return f


def test_banner_reflects_detection_not_the_override(tmp_path):
    """A forced run on a corpus below the bar must NOT claim a detected event."""
    out = run(corpus(tmp_path, {"kb/decisions/a 2.md": FM}), "--quiet", "--sync-event").stdout
    assert "[SYNC EVENT detected]" not in out, "forcing must not fabricate a detection"
    assert "forced via --sync-event" in out


def test_batch_orphan_message_does_not_invent_shadow_dirs(tmp_path):
    """The DUPE-DIR disclaimer is only true when shaped_all > shaped. On a
    flat batch (the 2026-09-06 replay shape) it would tell the operator the
    rename set is not the rename set."""
    msgs = [f["message"] for f in json.loads(
        run(corpus(tmp_path, batch_files()), "--json").stdout)["findings"]
            if f["code"] == "DUPE-ORPHAN"]
    assert msgs
    assert all("batch evidence met" in m for m in msgs)
    assert not any("shadow-dir copies" in m for m in msgs)


def test_banner_shows_when_detected_even_if_suppressed(tmp_path):
    out = run(corpus(tmp_path, batch_files()), "--quiet", "--no-sync-event").stdout
    assert "[SYNC EVENT detected]" in out
    assert "suppressed via --no-sync-event" in out


def test_suppressed_run_does_not_deny_the_signal_it_reports(tmp_path):
    """The summary saying 'SYNC EVENT detected' while every finding says 'no
    sync-batch signal' is the same class of lie as claiming absent evidence."""
    r = run(corpus(tmp_path, batch_files()), "--json", "--no-sync-event")
    msgs = [f["message"] for f in json.loads(r.stdout)["findings"]
            if f["code"] == "DUPE-AMBIGUOUS"]
    assert msgs
    assert not any("no sync-batch signal" in m for m in msgs)
    assert any("SUPPRESSED via --no-sync-event" in m for m in msgs)


def test_shadow_dirs_are_counted_in_the_summary(tmp_path):
    """Every other ERROR class has a counter; a lone DUPE-DIR printed 1 ERROR
    with nothing in the breakdown explaining it."""
    out = run(corpus(tmp_path, {"kb/notes 2/a.md": FM}), "--quiet").stdout
    assert "shadow dirs 1" in out


# --- coverage gaps R7 enumerated --------------------------------------------

def test_many_shaped_files_in_one_directory_is_not_a_sync_event(tmp_path):
    """Isolates the DIRECTORY half of the threshold from the file half."""
    root = corpus(tmp_path, {f"kb/decisions/a{i} 2.md": FM for i in range(12)})
    found = codes(root)
    assert "DUPE-AMBIGUOUS" in found
    assert "DUPE-ORPHAN" not in found, "12 files in one dir is not a sync event"


def test_canonical_name_link_evidence_marks_a_lost_original(tmp_path):
    """The step-4 `lost` branch produces the tool's most consequential advice
    ('likely mv') and had no test at all."""
    root = corpus(tmp_path, {"kb/decisions/plan 2.md": FM,
                             "kb/findings/ref.md": FM + "[[plan]]\n"})
    found = codes(root)
    assert "DUPE-ORPHAN" in found
    assert "evidence the original was lost" in found["DUPE-ORPHAN"][0]["message"]


def test_orphan_is_doomed_so_its_inbound_links_escalate(tmp_path):
    root = corpus(tmp_path, {"kb/decisions/plan 2.md": FM,
                             "kb/findings/ref.md": FM + "[[plan]]\n",
                             "kb/findings/cite.md": FM + "[[plan 2]]\n"})
    errs = [f for f in codes(root).get("DUPE-INBOUND-LINK", []) if f["severity"] == "ERROR"]
    assert errs, "a rename candidate must escalate links pointing at it"


def test_batch_orphan_inbound_link_is_an_error(tmp_path):
    """The batch-orphan doomed.add is the common rename path (219/221 in the
    2026-09-06 replay). Deleting only that add left the suite green while
    inbound links to batch orphans dropped from ERROR to WARN — kb-evolve
    then bulk-mvs without rewriting referrers."""
    files = batch_files()
    # Cite from another batch orphan so the link is not named-evidence
    # (rot must not vouch for rot) but inbound still sees a doomed target.
    files["kb/failures/b0 2.md"] = FM + "[[a0 2]]\n"
    errs = [f for f in codes(corpus(tmp_path, files)).get("DUPE-INBOUND-LINK", [])
            if f["severity"] == "ERROR"]
    assert errs, "a batch-orphan rename candidate must escalate links pointing at it"


def test_exactly_two_linkers_meets_the_documented_bar(tmp_path):
    """Both SKILL.mds say the bar is two; the prior test used three."""
    root = corpus(tmp_path, {"kb/decisions/plan.md": FM,
                             "kb/decisions/plan 2.md": FM + "distinct\n",
                             "kb/findings/c1.md": FM + "[[plan 2]]\n",
                             "kb/findings/c2.md": FM + "[[plan 2]]\n"})
    found = codes(root)
    assert "DUPE-NAMED" in found and "DUPE-DIVERGENT" not in found


def test_memory_store_slugs_resolve(tmp_path, monkeypatch):
    """Suppression #1 had only its absence warning pinned, never its effect."""
    home = tmp_path / "home"
    root = corpus(tmp_path, {"kb/findings/a.md": FM + "[[feedback_a_memory_slug]]\n"})
    mem = home / ".claude" / "projects" / str(root).replace("/", "-") / "memory"
    mem.mkdir(parents=True)
    (mem / "feedback_a_memory_slug.md").write_text("x\n")
    env = dict(os.environ, HOME=str(home))
    r = subprocess.run([sys.executable, str(LINT), "--repo", str(root), "--json"],
                       capture_output=True, text=True, env=env)
    found = {f["code"] for f in json.loads(r.stdout)["findings"]}
    assert "LINK-BROKEN" not in found and "MEM-STORE-MISSING" not in found


def test_memory_store_hit_is_not_evidence_a_kb_original_was_lost(tmp_path):
    """A [[roadmap]] link that resolves in the memory store must not print
    'the original was lost' / 'likely mv' for kb/decisions/roadmap 2.md."""
    home = tmp_path / "home"
    root = corpus(tmp_path, {"kb/decisions/roadmap 2.md": FM,
                             "kb/findings/ref.md": FM + "[[roadmap]]\n"})
    mem = home / ".claude" / "projects" / str(root).replace("/", "-") / "memory"
    mem.mkdir(parents=True)
    (mem / "roadmap.md").write_text("x\n")
    env = dict(os.environ, HOME=str(home))
    r = subprocess.run([sys.executable, str(LINT), "--repo", str(root), "--json"],
                       capture_output=True, text=True, env=env)
    payload = json.loads(r.stdout)
    codes_found = {f["code"] for f in payload["findings"]}
    assert "DUPE-ORPHAN" not in codes_found
    assert "DUPE-AMBIGUOUS" in codes_found
    msgs = " ".join(f["message"] for f in payload["findings"] if f["code"] == "DUPE-AMBIGUOUS")
    assert "memory store" in msgs
    assert "likely mv" not in msgs


def test_mandated_skill_outside_the_routing_table_is_still_checked(tmp_path):
    """/ticket lives in Critical rules, not the routing table. Scanning the
    whole file for `/name` would also match `/scoreboard`; the allowlist is
    the bounded fix."""
    root = corpus(tmp_path, {})
    (root / "CLAUDE.md").write_text(
        "## Skill routing\n\n| x | `/status` |\n\n"
        "## Other\nfiles a ticket via `/ticket`.\n")
    found = codes(root)
    assert "SKILL-MISSING" in found
    assert any("ticket" in f["message"] for f in found["SKILL-MISSING"])
    assert not any("scoreboard" in f["message"] for f in found.get("SKILL-MISSING", []))


def test_broken_link_separator_hint_is_not_deletable(tmp_path):
    root = corpus(tmp_path, {"kb/findings/a.md": FM + "[[my-doc]]\n",
                             "kb/findings/my_doc.md": FM})
    msgs = " ".join(f["message"] for f in codes(root).get("LINK-BROKEN", []))
    assert "did you mean [[my_doc]]" in msgs


def test_chain_inside_a_shadow_dir_is_not_a_second_verdict(tmp_path):
    root = corpus(tmp_path, {"kb/notes 2/p 2.md": FM + "a\n",
                             "kb/notes 2/p 3.md": FM + "b\n"})
    found = codes(root)
    assert "DUPE-DIR" in found
    assert "DUPE-CHAIN" not in found


def test_skill_with_unparseable_frontmatter_is_dead(tmp_path):
    root = corpus(tmp_path, {})
    (root / "CLAUDE.md").write_text("## Skill routing\n\n| x | `/broken` |\n")
    d = root / ".claude" / "skills" / "broken"
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text("# no frontmatter at all\n")
    assert "SKILL-FRONTMATTER" in codes(root)


def test_missing_claude_md_does_not_pass_every_skill(tmp_path):
    assert "SKILL-ROUTING-MISSING" in codes(corpus(tmp_path, {}))


def test_index_links_are_not_double_reported(tmp_path):
    root = corpus(tmp_path, {})
    (root / "kb" / "_index.md").write_text("- [[decisions/ghost.md]] - x\n")
    found = codes(root)
    assert "INDEX-DANGLING" in found
    assert "LINK-BROKEN" not in found, "an index link must be reported once, not twice"


def test_failures_and_decisions_require_their_extra_frontmatter(tmp_path):
    root = corpus(tmp_path, {"kb/failures/f.md": FM, "kb/decisions/d.md": FM})
    (root / "kb" / "_index.md").write_text("- [[failures/f.md]] - x\n- [[decisions/d.md]] - y\n")
    msgs = " ".join(f["message"] for f in codes(root).get("FM-FIELDS", []))
    assert "severity" in msgs and "date" in msgs


def test_curated_scoping_keeps_session_notes_out(tmp_path):
    """The curation policy is load-bearing: applied to every file these checks
    fire on most of the corpus and get muted."""
    root = corpus(tmp_path, {"kb/decisions/note.md": "no frontmatter, no Related\n"})
    found = codes(root)
    assert "NO-FRONTMATTER" not in found and "NO-RELATED" not in found


# --- severity partition ------------------------------------------------------
# The exit code is the gate an operator and /kb-evolve act on, and it derives
# purely from severity. Adversarial round 8 downgraded all eight ERROR codes to
# WARN and the whole suite stayed green — a "stop, merge by hand" run would have
# reported clean. Severity is pinned per-code here.

ERROR_CASES = {
    "DUPE-DIVERGENT": {"kb/decisions/x.md": FM, "kb/decisions/x 2.md": FM + "diff\n"},
    "DUPE-CHAIN": {"kb/decisions/p 2.md": FM + "a\n", "kb/decisions/p 3.md": FM + "b\n"},
    "DUPE-DIR": {"kb/notes 2/a.md": FM},
    "LINK-BROKEN": {"kb/findings/a.md": FM + "[[definitely-absent]]\n"},
}


@pytest.mark.parametrize("code", sorted(ERROR_CASES))
def test_error_codes_are_error_severity(tmp_path, code):
    found = codes(corpus(tmp_path, ERROR_CASES[code]))
    assert code in found, f"{code} did not fire on its fixture"
    assert found[code][0]["severity"] == "ERROR", f"{code} must gate the exit code"


def test_index_error_codes_are_error_severity(tmp_path):
    root = corpus(tmp_path, {"kb/concepts/real.md": FM})
    (root / "kb" / "_index.md").write_text("- [[ghost.md]] - x\n- [[decisions/real.md]] - y\n")
    found = codes(root)
    for code in ("INDEX-DANGLING", "INDEX-MISPATHED"):
        assert code in found and found[code][0]["severity"] == "ERROR"


@pytest.mark.parametrize("code,setup", [
    ("SKILL-MISSING", None),
    ("SKILL-ROT", "rot"),
    ("SKILL-FRONTMATTER", "fm"),
])
def test_skill_error_codes_are_error_severity(tmp_path, code, setup):
    root = corpus(tmp_path, {})
    (root / "CLAUDE.md").write_text("## Skill routing\n\n| x | `/target` |\n")
    d = root / ".claude" / "skills" / "target"
    if setup == "rot":
        d.mkdir(parents=True)
        (d / "SKILL 2.md").write_text("---\nname: target\n---\n")
    elif setup == "fm":
        d.mkdir(parents=True)
        (d / "SKILL.md").write_text("# no frontmatter\n")
    found = codes(root)
    assert code in found and found[code][0]["severity"] == "ERROR"


def test_a_severity_downgrade_flips_the_exit_code(tmp_path):
    """Why the pins above matter: severity alone decides stop-vs-clean."""
    root = corpus(tmp_path, {"kb/decisions/x.md": FM, "kb/decisions/x 2.md": FM + "diff\n"})
    assert run(root).returncode == 1


# --- checks that were invisibly deletable ------------------------------------

def test_curated_article_without_related_is_flagged(tmp_path):
    """NO-RELATED had only absence assertions, so deleting the check entirely
    left the suite green and made those assertions unfalsifiable."""
    root = corpus(tmp_path, {"kb/concepts/lonely.md":
                             "---\nstatus: active\nupdated: 2026-09-01\ntags: [x]\n---\n"
                             "body with no heading\n"})
    assert "NO-RELATED" in codes(root)


def test_related_must_be_a_heading_not_a_word(tmp_path):
    root = corpus(tmp_path, {"kb/concepts/loose.md":
                             "---\nstatus: active\nupdated: 2026-09-01\ntags: [x]\n---\n"
                             "see the Related work above\n"})
    assert "NO-RELATED" in codes(root), "a bare mention must not satisfy the check"


@pytest.mark.parametrize("name", ["c copy.md", "c (2).md", "c  2.md"])
def test_documented_conflict_shapes_are_recognized(tmp_path, name):
    """kb-evolve promises ' copy.md' and ' (2).md'; narrowing the regex to
    digits-only left the suite green."""
    root = corpus(tmp_path, {"kb/decisions/c.md": FM, f"kb/decisions/{name}": FM})
    assert "DUPE-IDENTICAL" in codes(root), f"{name} not recognized as a conflict copy"


def test_strategies_is_a_curated_dir(tmp_path):
    root = corpus(tmp_path, {"kb/strategies/s.md": "no frontmatter\n"})
    assert "NO-FRONTMATTER" in codes(root)


def test_index_itself_is_not_linted_as_an_article(tmp_path):
    root = corpus(tmp_path, {})
    (root / "kb" / "_index.md").write_text("# Index\n")
    assert not [f for f in codes(root).get("NO-RELATED", []) if "_index" in f["file"]]


def test_bom_does_not_hide_frontmatter(tmp_path):
    root = corpus(tmp_path, {})
    (root / "kb" / "concepts" / "bom.md").write_bytes(
        b"\xef\xbb\xbf" + FM.encode())
    assert not [f for f in codes(root).get("NO-FRONTMATTER", []) if "bom" in f["file"]]


def test_kb_research_index_is_read(tmp_path):
    root = corpus(tmp_path, {"kb-research/bot/note.md": FM})
    (root / "kb-research" / "_index.md").write_text("- [[ghost-in-research.md]] - x\n")
    assert "INDEX-DANGLING" in codes(root)


def test_missing_kb_dir_is_caught_even_with_a_full_research_store(tmp_path):
    """Isolates the kb/ guard from the MIN_FILES floor."""
    root = tmp_path / "repo"
    (root / "kb-research").mkdir(parents=True)
    for i in range(60):
        (root / "kb-research" / f"f{i}.md").write_text(FM)
    assert run(root).returncode == 2


def test_duplicated_store_is_reported(tmp_path):
    root = corpus(tmp_path, {})
    (root / "kb 2" / "decisions").mkdir(parents=True)
    (root / "kb 2" / "decisions" / "a.md").write_text(FM)
    found = codes(root)
    assert "DUPE-STORE" in found and found["DUPE-STORE"][0]["severity"] == "ERROR"


def test_inbound_link_repeated_in_one_doc_is_reported_once(tmp_path):
    root = corpus(tmp_path, {"kb/decisions/x.md": FM, "kb/decisions/x 2.md": FM,
                             "kb/findings/cite.md": FM + "[[x 2]] [[x 2]] [[x 2]]\n"})
    assert len(codes(root).get("DUPE-INBOUND-LINK", [])) == 1


def test_shadow_file_gets_one_verdict_not_two(tmp_path):
    """A conflict-shaped file inside a conflict dir was told both 'merge the
    directory' and 'never bulk-rename this file'."""
    root = corpus(tmp_path, {"kb/notes 2/b 2.md": FM})
    found = codes(root)
    assert "DUPE-DIR" in found
    assert "DUPE-AMBIGUOUS" not in found


def test_summary_counters_track_the_findings(tmp_path):
    """A summary reading '0 broken' above a list of LINK-BROKEN findings is the
    same contradiction class as the sync-banner one."""
    root = corpus(tmp_path, {"kb/findings/a.md": FM + "[[ghost-one]]\n[[ghost-two]]\n"})
    stats = json.loads(run(root, "--json").stdout)["stats"]
    assert stats["broken_links"] == 2


def test_index_entry_count_covers_both_link_forms(tmp_path):
    """Path-form and bare-name entries are tracked in different structures; the
    reported count must not silently follow only one of them."""
    root = corpus(tmp_path, {"kb/concepts/a.md": FM, "kb/concepts/b.md": FM})
    (root / "kb" / "_index.md").write_text("- [[concepts/a.md]] - x\n- [[b.md]] - y\n")
    stats = json.loads(run(root, "--json").stdout)["stats"]
    assert stats["index_entries"] == 2


# --- every reported number must track the findings ---------------------------
# Adversarial round 9 zeroed 14 of 17 stats values and every summary-line number
# with the suite green. /kb-evolve authorizes unrecoverable renames off these.

COUNTER_TO_CODE = {
    "broken_links": "LINK-BROKEN",
    "mispathed_links": "LINK-MISPATHED",
    "index_dangling": ("INDEX-DANGLING", "INDEX-MISPATHED"),
    "inbound_conflict_links": "DUPE-INBOUND-LINK",
    "no_related": "NO-RELATED",
    "no_frontmatter": "NO-FRONTMATTER",
    "frontmatter_incomplete": "FM-FIELDS",
    "stale_active": "FM-UPDATED-OLD",
}


def rich_corpus(tmp_path) -> Path:
    """One corpus exercising every counted class at once."""
    old = (dt.date.today() - dt.timedelta(days=400)).isoformat()
    files = {
        "kb/concepts/thing.md": FM,
        "kb/concepts/nofm.md": "no frontmatter at all\n",
        "kb/concepts/norel.md": f"---\nstatus: active\nupdated: {old}\ntags: [x]\n---\nbody\n",
        "kb/failures/f.md": FM,
        "kb/decisions/x.md": FM,
        "kb/decisions/x 2.md": FM,
        "kb/findings/links.md": (FM + "[[ghost-one]]\n[[ghost-two]]\n"
                                "[[decisions/thing.md]]\n[[x 2]]\n"),
    }
    root = corpus(tmp_path, files)
    (root / "kb" / "_index.md").write_text(
        "- [[failures/f.md]] - x\n- [[ghost-entry.md]] - y\n"
        "- [[decisions/thing.md]] - z\n")
    return root


@pytest.mark.parametrize("counter", sorted(COUNTER_TO_CODE))
def test_stat_counter_matches_its_findings(tmp_path, counter):
    payload = json.loads(run(rich_corpus(tmp_path), "--json").stdout)
    want = COUNTER_TO_CODE[counter]
    wanted = want if isinstance(want, tuple) else (want,)
    actual = len([f for f in payload["findings"] if f["code"] in wanted])
    assert actual > 0, f"corpus does not exercise {wanted} — this assertion would be 0==0"
    assert payload["stats"][counter] == actual, (
        f"{counter} says {payload['stats'][counter]} but {actual} findings were emitted")


def every_class_corpus(tmp_path) -> Path:
    """One copy of every conflict class at once. Seven shaped files in a single
    directory keeps it under the sync-batch bar, so `ambiguous` stays ambiguous."""
    return corpus(tmp_path, {
        "kb/decisions/x.md": FM,          "kb/decisions/x 2.md": FM,           # identical
        "kb/decisions/y.md": FM,          "kb/decisions/y 2.md": FM + "d\n",   # divergent
        "kb/decisions/z 2.md": FM,        "kb/findings/ref.md": FM + "[[z]]\n",  # orphan
        "kb/decisions/amb 2.md": FM,                                            # ambiguous
        "kb/decisions/nm 2.md": FM,       "kb/findings/n1.md": FM + "[[nm 2]]\n",  # named
        "kb/decisions/e 2.md": "",                                              # empty
        "kb/decisions/cs 2.md": FM,       "kb/findings/cs.md": FM,              # cross-store
        "kb/decisions/ch 2.md": FM + "a\n", "kb/decisions/ch 3.md": FM + "b\n",  # chain
    })


def test_conflict_breakdown_matches_its_findings(tmp_path):
    """Adversarial round 10 found 6 of these 7 assertions were 0 == 0 on the
    previous corpus. The non-zero guard makes that impossible to reintroduce."""
    payload = json.loads(run(every_class_corpus(tmp_path), "--json").stdout)
    c = payload["stats"]["conflict_copies"]
    findings = payload["findings"]
    pairs = [("identical", "DUPE-IDENTICAL"), ("divergent", "DUPE-DIVERGENT"),
             ("orphan", "DUPE-ORPHAN"), ("ambiguous", "DUPE-AMBIGUOUS"),
             ("named", "DUPE-NAMED"), ("empty", "DUPE-EMPTY"),
             ("cross_store", "DUPE-CROSS-STORE")]
    zero = [k for k, _ in pairs if c[k] == 0]
    assert not zero, f"corpus does not exercise {zero} — those assertions would be 0==0"
    assert c["chain"] >= 2, "corpus does not exercise chain — that assertion would be 0==0"
    for key, code in pairs:
        assert c[key] == len([f for f in findings if f["code"] == code]), key
    assert len([f for f in findings if f["code"] == "DUPE-CHAIN"]) == 1
    assert c["total"] == sum(c[k] for k, _ in pairs) + c["chain"]


def test_file_counts_are_not_transposed(tmp_path):
    root = corpus(tmp_path, {"kb-research/bot/only.md": FM})
    stats = json.loads(run(root, "--json").stdout)["stats"]
    assert stats["kb_research_files"] == 1
    assert stats["kb_files"] > 1, "kb/ and kb-research/ counts must not be swapped"


def test_header_counts_cannot_be_zeroed(tmp_path):
    """curated_articles and routed_skills are the only numbers --quiet prints
    for those lines; both were 0==0 until a fixture produced them."""
    root = corpus(tmp_path, {})
    (root / "CLAUDE.md").write_text("## Skill routing\n\n| x | `/ok` |\n")
    d = root / ".claude" / "skills" / "ok"
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text("---\nname: ok\ndescription: x\n---\n")
    stats = json.loads(run(root, "--json").stdout)["stats"]
    assert stats["curated_articles"] >= 60
    assert stats["routed_skills"] == 1


def test_summary_prints_the_real_numbers(tmp_path):
    """--quiet prints nothing but these lines, and they are what a human reads
    before authorizing a bulk rename."""
    root = corpus(tmp_path, {"kb/findings/a.md": FM + "[[ghost-one]]\n[[ghost-two]]\n"})
    out = run(root, "--quiet").stdout
    assert "2 broken" in out
    assert "61 kb/" in out and "0 kb-research/" in out
    assert "2 ERROR" in out


def test_dead_skills_appear_in_the_summary(tmp_path):
    root = corpus(tmp_path, {})
    (root / "CLAUDE.md").write_text("## Skill routing\n\n| x | `/ghost-skill` |\n")
    out = run(root, "--quiet").stdout
    assert "DEAD: ghost-skill" in out and "all loadable" not in out


def test_findings_are_printed_in_default_mode(tmp_path):
    root = corpus(tmp_path, {"kb/findings/a.md": FM + "[[ghost-one]]\n"})
    out = run(root).stdout
    assert "LINK-BROKEN" in out and "ghost-one" in out


# --- curated-tier membership (the R8-MN10 fix) -------------------------------

def test_path_form_index_entry_curates_only_the_cited_file(tmp_path):
    """A bare-basename match pulled unrelated same-named session notes into the
    curated tier and emitted hygiene findings on them."""
    root = corpus(tmp_path, {"kb/decisions/dup.md": "no frontmatter\n",
                             "kb/findings/dup.md": "no frontmatter\n"})
    (root / "kb" / "_index.md").write_text("- [[decisions/dup.md]] - x\n")
    flagged = {f["file"] for f in codes(root).get("NO-FRONTMATTER", [])}
    assert "kb/decisions/dup.md" in flagged
    assert "kb/findings/dup.md" not in flagged


def test_bare_name_index_entry_curates_by_leaf(tmp_path):
    root = corpus(tmp_path, {"kb/decisions/bnote.md": "no frontmatter\n"})
    (root / "kb" / "_index.md").write_text("- [[bnote]] - x\n")
    assert "kb/decisions/bnote.md" in {f["file"] for f in codes(root).get("NO-FRONTMATTER", [])}


def test_index_entry_for_a_nonexistent_file_curates_nothing(tmp_path):
    root = corpus(tmp_path, {"kb/decisions/note.md": "no frontmatter\n"})
    (root / "kb" / "_index.md").write_text("- [[decisions/absent.md]] - x\n")
    assert not codes(root).get("NO-FRONTMATTER")


# --- guards that were invisibly deletable ------------------------------------

def test_non_article_files_are_exempt_inside_a_curated_dir(tmp_path):
    """The prior test for this never reached the guard: _index.md is not curated
    to begin with, so the exemption was never evaluated."""
    root = corpus(tmp_path, {"kb/concepts/log.md": "no frontmatter\n",
                             "kb/concepts/dashboard.md": "no frontmatter\n",
                             "kb/concepts/normal.md": "no frontmatter\n"})
    flagged = {f["file"] for f in codes(root).get("NO-FRONTMATTER", [])}
    assert flagged == {"kb/concepts/normal.md"}


def test_empty_wikilink_in_a_normal_doc_is_malformed(tmp_path):
    root = corpus(tmp_path, {"kb/findings/a.md": FM + "[[   ]]\n"})
    assert "LINK-MALFORMED" in codes(root)


def test_a_legitimate_kb_prefixed_sibling_is_not_a_duplicated_store(tmp_path):
    """`kb notes 2/` is a conflict copy of `kb notes/`, which is not a store —
    calling it DUPE-STORE would advise merging it into the KB."""
    root = corpus(tmp_path, {})
    (root / "kb notes 2").mkdir()
    (root / "kb notes 2" / "a.md").write_text(FM)
    assert "DUPE-STORE" not in codes(root)


_OLD = (dt.date.today() - dt.timedelta(days=400)).isoformat()
WARN_CASES = {
    "DUPE-ORPHAN": {"kb/decisions/plan 2.md": FM,
                    "kb/findings/ref.md": FM + "[[plan]]\n"},
    "DUPE-IDENTICAL": {"kb/decisions/x.md": FM, "kb/decisions/x 2.md": FM},
    "DUPE-EMPTY": {"kb/decisions/e 2.md": ""},
    "DUPE-AMBIGUOUS": {"kb/decisions/amb 2.md": FM},
    "DUPE-NAMED": {"kb/decisions/nm 2.md": FM,
                   "kb/findings/n1.md": FM + "[[nm 2]]\n"},
    "DUPE-CROSS-STORE": {"kb/findings/foo.md": FM,
                         "kb/decisions/foo 2.md": FM + "d\n"},
    "DUPE-INBOUND-LINK": {"kb/decisions/keep 2.md": FM,
                          "kb/findings/cite.md": FM + "[[keep 2]]\n"},
    "LINK-MISPATHED": {"kb/concepts/thing.md": FM,
                       "kb/findings/links.md": FM + "[[decisions/thing.md]]\n"},
    "NO-RELATED": {"kb/concepts/n.md":
                   "---\nstatus: active\nupdated: 2026-09-01\ntags: [x]\n---\nbody\n"},
    "NO-FRONTMATTER": {"kb/concepts/nofm.md": "no frontmatter at all\n"},
    "FM-FIELDS": {"kb/concepts/inc.md":
                  "---\nstatus: active\ntags: [x]\n---\n## Related\n"},
    "FM-UNPARSEABLE-DATE": {"kb/concepts/d.md":
                            "---\nstatus: active\nupdated: unknown\ntags: [x]\n---\n"
                            "## Related\n"},
    "FM-UPDATED-OLD": {"kb/concepts/old.md":
                       f"---\nstatus: active\nupdated: {_OLD}\ntags: [x]\n---\n"
                       "## Related\n"},
    "LINK-MALFORMED": {"kb/findings/a.md": FM + "[[   ]]\n"},
    "MEM-STORE-MISSING": {},
    "SKILL-ROUTING-MISSING": {},
}


@pytest.mark.parametrize("code", sorted(WARN_CASES))
def test_warn_codes_stay_warn(tmp_path, code):
    """The muting direction was pinned; the noise direction was not. Upgrading
    any WARN code to ERROR would make a hygiene-only corpus exit 1 — and
    until R10 only three of them were pinned."""
    found = codes(corpus(tmp_path, WARN_CASES[code]))
    assert code in found, f"{code} did not fire on its fixture"
    assert found[code][0]["severity"] == "WARN", f"{code} must stay WARN"


def test_non_index_io_error_stays_warn(tmp_path):
    if os.geteuid() == 0:
        pytest.skip("root reads through mode 000")
    root = corpus(tmp_path, {})
    cm = root / "CLAUDE.md"
    cm.write_text("## Skill routing\n\n`/x`\n")
    cm.chmod(0o000)
    try:
        found = codes(root)
        assert "IO-ERROR" in found and found["IO-ERROR"][0]["severity"] == "WARN"
    finally:
        cm.chmod(0o644)


def test_shadow_copies_still_count_as_sync_evidence(tmp_path):
    """Shadow-directory copies are excluded from classification and from the
    reported total — but they are among the strongest evidence a sync client
    ran, so they must still count toward the batch threshold. Excluding them
    from both would let a directory-duplicating event fall below the bar."""
    files = {f"kb/notes 2/s{i}.md": FM for i in range(8)}
    files.update({f"kb/notes 2/inner{i} 2.md": FM for i in range(8)})
    files.update({"kb/decisions/a 2.md": FM, "kb/failures/b 2.md": FM,
                  "kb/findings/c 2.md": FM})
    root = corpus(tmp_path, files)
    payload = json.loads(run(root, "--json").stdout)
    c = payload["stats"]["conflict_copies"]
    assert c["batch_event_detected"] is True, "shadow copies must count as evidence"
    assert c["orphan"] == 3, "the 3 loose copies should be actionable orphans"
    assert c["total"] == 3, "the reported total covers only classified files"
    assert c["shaped_all"] == 11, "8 shadow-shaped + 3 loose"
    assert c["dirs_hit"] == 4
    out = run(root, "--quiet").stdout
    assert "3 classified / 11 shaped" in out
    msgs = [f["message"] for f in payload["findings"] if f["code"] == "DUPE-ORPHAN"]
    assert msgs and all("not this rename set" in m for m in msgs)


def test_canon_fork_named_is_counted(tmp_path):
    """`counts['named'] += 1` appears on both forks; only the orphan fork was
    exercised, so the canon-fork counter could be dropped unnoticed."""
    root = corpus(tmp_path, {"kb/decisions/plan.md": FM,
                             "kb/decisions/plan 2.md": FM + "distinct\n",
                             "kb/findings/c1.md": FM + "[[plan 2]]\n",
                             "kb/findings/c2.md": FM + "[[plan 2]]\n"})
    payload = json.loads(run(root, "--json").stdout)
    named = [f for f in payload["findings"] if f["code"] == "DUPE-NAMED"]
    assert len(named) == 1
    assert payload["stats"]["conflict_copies"]["named"] == 1


def test_shadow_dirs_count_toward_directory_diversity(tmp_path):
    """Isolates the dirs_hit half: the loose copies sit in ONE directory, so the
    >=3-directory bar is met only by counting the duplicated directories. A sync
    event that duplicated whole trees must not fall below the bar."""
    files = {f"kb/decisions/a{i} 2.md": FM for i in range(10)}
    files["kb/notes 2/x 2.md"] = FM
    files["kb/other 2/y 2.md"] = FM
    payload = json.loads(run(corpus(tmp_path, files), "--json").stdout)
    c = payload["stats"]["conflict_copies"]
    assert c["batch_event_detected"] is True, "duplicated dirs are directory diversity"
    assert c["orphan"] == 10 and c["ambiguous"] == 0
