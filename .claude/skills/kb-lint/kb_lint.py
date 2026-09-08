#!/usr/bin/env python3
"""kb-lint — structural health check for kb/ + kb-research/.

Severity-tiered so the output stays actionable: applied naively, the
kb/_meta/MAINTENANCE.md checks fire on most of the corpus and get muted, and a
muted gate is worse than no gate.

Three rules keep the false-positive rate near zero:
  1. [[links]] resolve against kb/, kb-research/ AND the Claude memory
     store — kb docs legitimately link to memory files by slug.
  2. Links inside code fences / inline backticks are documentation
     examples, not references.
  3. Explicit paths are honored. [[concepts/foo.md]] does NOT resolve to
     kb/failures/foo.md — Obsidian would not resolve it either, so
     neither do we (that would be a false negative in a correctness gate).

FAILS CLOSED: a missing kb/ or an implausibly small corpus exits 2 rather
than reporting perfect health.

Usage:  kb_lint.py [--json] [--repo PATH] [--stale-days N] [--quiet]
                   [--sync-event | --no-sync-event]
Exit:   0 clean · 1 ERROR findings · 2 the lint itself could not run.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import subprocess
import sys
from pathlib import Path, PurePosixPath

FENCE_RE = re.compile(r"^(```)[\s\S]*?(?:^\1|\Z)|^(~~~)[\s\S]*?(?:^\2|\Z)", re.M)
INLINE_RE = re.compile(r"`[^`\n]*`")
LINK_RE = re.compile(r"\[\[([^\]|#]+?)(?:#[^\]|]*)?(?:\|[^\]]*)?\]\]")
FM_RE = re.compile(r"\A---\r?\n(.*?)\r?\n---\r?\n", re.S)
KEY_RE = re.compile(r"^([A-Za-z_]+)\s*:", re.M)
DATE_RE = re.compile(r"(\d{4})-(\d{2})-(\d{2})")
STATUS_RE = re.compile(r"^status\s*:\s*(.+)$", re.M)
UPDATED_RE = re.compile(r"^updated\s*:\s*(.+)$", re.M)
ROUTE_RE = re.compile(r"`/([a-z0-9][a-z0-9-]*)`")  # single segment: no "/" inside

# Sync-conflict shapes. macOS/iCloud/Dropbox all use a SPACE before the
# discriminator — never an underscore, which would swallow ordinary
# snake_case ("tier_2.md") and date ranges ("..._may19_20").
CONFLICT_RE = re.compile(r"^(?P<base>.+?) (?:\d+|copy|\(\d+\))$", re.I)

NON_ARTICLE = {"_index.md", "CLAUDE.md", "MAINTENANCE.md", "dashboard.md", "log.md"}
CURATED_DIRS = ("concepts", "strategies")
REQUIRED_FM = ("status", "updated", "tags")
MIN_FILES = 50
# Skills mandated in CLAUDE.md Critical / Interaction rules but absent from
# the routing table. Scanning the whole file for `/name` would also match
# `/scoreboard` `/odds` `/markets` in architecture prose, so this is an
# allowlist. kb-evolve cites /ticket and /pickup as the SKILL-ROT incident.
MANDATED_SKILLS = frozenset({"ticket", "pickup", "test-writer"})
# Sync clients corrupt in BULK. Many conflict-shaped files across several
# directories is itself evidence a sync event occurred — evidence that is
# corpus-internal and always available, unlike inbound links (only ~19% of
# files have any, and just ~5% under decisions/ where rot accumulates).
SYNC_BATCH_MIN_FILES = 10
SYNC_BATCH_MIN_DIRS = 3


def strip_code(text: str) -> str:
    return INLINE_RE.sub(" ", FENCE_RE.sub(" ", text))


def read(p: Path) -> str | None:
    """utf-8-sig strips a BOM (which otherwise defeats the frontmatter
    anchor). Returns None if the file moved mid-run — concurrent sessions
    edit kb/ while this runs, and one moving file must not abort the run."""
    try:
        return p.read_text(encoding="utf-8-sig", errors="replace")
    except OSError:
        return None


def conflict_base(stem: str) -> str | None:
    """The canonical stem a conflict copy claims, or None.

    Shape alone NEVER proves a sync artifact — "roadmap-phase 2" is a
    legitimate title. Classification below corroborates with link
    evidence; nothing here is sufficient to drive a rename on its own."""
    m = CONFLICT_RE.match(stem)
    return m.group("base").rstrip() if m else None


class Lint:
    def __init__(self, repo: Path, stale_days: int, sync_override: bool | None = None):
        self.repo, self.stale_days = repo, stale_days
        self.sync_override = sync_override
        self.kb, self.kbr = repo / "kb", repo / "kb-research"
        self.findings: list[dict] = []
        self.stats: dict[str, object] = {}

    def add(self, sev, code, msg, file=None, fix=None):
        self.findings.append({"severity": sev, "code": code, "message": msg,
                              "file": file, "fix": fix})

    def files_under(self, base: Path):
        if not base.exists():
            return []
        return sorted(p for p in base.rglob("*.md")
                      if not any(x.startswith(".") for x in p.relative_to(base).parts))

    def build(self):
        if not self.kb.is_dir():
            print(f"kb-lint: no kb/ under {self.repo} — refusing to report health", file=sys.stderr)
            sys.exit(2)
        self.files = self.files_under(self.kb) + self.files_under(self.kbr)
        if len(self.files) < MIN_FILES:
            print(f"kb-lint: only {len(self.files)} files under {self.repo} — expected "
                  f">={MIN_FILES}; wrong --repo? refusing to report health", file=sys.stderr)
            sys.exit(2)

        self.names, self.stems = {}, {}
        for p in self.files:
            self.names.setdefault(p.name, []).append(p)
            self.stems.setdefault(p.stem, []).append(p)
        mem = Path.home() / ".claude" / "projects" / str(self.repo).replace("/", "-") / "memory"
        self.mem_stems = {p.stem for p in mem.glob("*.md")} if mem.is_dir() else set()
        if not mem.is_dir():
            # SKILL.md calls this the #1 suppression; failing silently turns
            # every valid memory-slug link into a bogus ERROR.
            self.add("WARN", "MEM-STORE-MISSING",
                     f"no memory store at {mem} — links to memory slugs will report as "
                     f"broken. Is --repo canonical (not a worktree)?", None,
                     "run with --repo pointing at the primary checkout")


        # Index every link target once: stem -> [files linking it]. Feeds both
        # the inbound-link check and the conflict classifier's link evidence.
        # One read pass, cached: later checks must see the same bytes the link
        # index was built from, or a concurrent writer can desync them.
        self.text: dict[Path, str] = {}
        self.link_targets: dict[str, list[Path]] = {}
        for f in self.files:
            body = read(f)
            if body is None:
                if f not in (self.kb / "_index.md", self.kbr / "_index.md"):
                    self.add("WARN", "IO-ERROR", "unreadable — excluded from every check",
                             self.rel(f), "check permissions; findings for this file are absent")
                continue
            self.text[f] = body
            for m in LINK_RE.finditer(strip_code(body)):
                leaf = m.group(1).strip().split("/")[-1]
                stem = leaf[:-3] if leaf.endswith(".md") else leaf
                self.link_targets.setdefault(stem, []).append(f)

        self.indexes = [i for i in (self.kb / "_index.md", self.kbr / "_index.md") if i.exists()]
        self.index_cited: set[str] = set()
        self.index_cited_paths: set[Path] = set()
        self.stats["index_entries"] = 0   # links parsed, not membership-set size
        for idx in self.indexes:
            body = self.text.get(idx)
            if body is None:
                # An unreadable index silently empties the curated tier AND the
                # index checks. That is the same blindness the exit-2 floor
                # guards against, so it must not read as clean.
                self.add("ERROR", "IO-ERROR",
                         "index is unreadable — index integrity and the curated tier "
                         "cannot be evaluated", self.rel(idx), "fix permissions and re-run")
                continue
            for m in LINK_RE.finditer(strip_code(body)):
                tgt = m.group(1).strip()
                if not tgt:
                    continue
                self.stats["index_entries"] = self.stats.get("index_entries", 0) + 1
                leaf = tgt.split("/")[-1]
                leaf = leaf if leaf.endswith(".md") else leaf + ".md"
                if "/" in tgt:
                    # an explicit path cites ONE file; a bare name cites by leaf
                    want = PurePosixPath(tgt if tgt.endswith(".md") else tgt + ".md").parts
                    self.index_cited_paths.update(
                        q for q in self.stems.get(leaf[:-3], [])
                        if q.parts[-len(want):] == want)
                else:
                    self.index_cited.add(leaf)

        # Conflict-copy DIRECTORIES, computed before any check consults them: a
        # sync client duplicates whole directories, and every file inside one is
        # a shadow of the real tree. They must never count as evidence.
        self.conflict_dirs = {a for f in self.files for a in f.parents
                              if a != self.repo and conflict_base(a.name)
                              and (self.kb in a.parents or self.kbr in a.parents)}
        self.doomed: set[Path] = set()   # scheduled for rename OR deletion
        self.doomed.update(f for f in self.files if self.in_conflict_dir(f))

    # ---------- helpers ----------
    def rel(self, p: Path) -> str:
        return str(p.relative_to(self.repo))

    def in_conflict_dir(self, p: Path) -> bool:
        return any(a in self.conflict_dirs for a in p.parents)

    def is_curated(self, p: Path) -> bool:
        """Curated tier = kb/concepts, kb/strategies, or cited by either
        _index.md. Session notes are intentionally excluded (curation policy)."""
        try:
            if p.relative_to(self.kb).parts[0] in CURATED_DIRS:
                return True
        except ValueError:
            pass
        return p in self.index_cited_paths or p.name in self.index_cited

    def resolves(self, target: str, origin: Path) -> tuple[bool, str | None]:
        """-> (resolved, note). Honors explicit paths; bare names fall back
        to stem/basename lookup across both stores and the memory store."""
        t = target.strip()
        if not t:
            return False, None
        leaf = t.split("/")[-1]
        stem = leaf[:-3] if leaf.endswith(".md") else leaf

        if "/" in t:                       # explicit path — must actually match
            cands = [origin.parent / t, self.kb / t, self.kbr / t, self.repo / t]
            for c in cands:
                try:
                    c = c.resolve()
                except OSError:
                    continue
                if c.exists() or Path(str(c) + ".md").exists():
                    return True, None
            if stem in self.stems or leaf in self.names:
                where = (self.stems.get(stem) or self.names.get(leaf))[0]
                return False, f"a file named {leaf} exists at {self.rel(where)} — path is wrong"
            return False, None

        if stem in self.stems or leaf in self.names or stem in self.mem_stems:
            return True, None
        return False, None

    def near_miss(self, target: str) -> str | None:
        """Slug-separator drift is the common authoring error: memory files
        are underscored, and hyphenated links to them are silently dead."""
        stem = target.strip().split("/")[-1]
        stem = stem[:-3] if stem.endswith(".md") else stem
        for alt in (stem.replace("-", "_"), stem.replace("_", "-")):
            if alt != stem and (alt in self.mem_stems or alt in self.stems):
                return alt
        return None

    # ---------- checks ----------
    def check_conflicts(self):
        by_canon: dict[Path, list[Path]] = {}
        for p in self.files:
            base = conflict_base(p.stem)
            if base:
                by_canon.setdefault(p.with_name(base + ".md"), []).append(p)

        # Two different questions, two different sets:
        #   evidence — was there a sync event? Shadow-directory copies COUNT;
        #     they are among the strongest signs a sync client ran.
        #   reporting/classification — which files get a verdict? Shadow copies
        #     are excluded; the DUPE-DIR merge subsumes them.
        shaped_all = [p for v in by_canon.values() for p in v]
        shaped = [p for p in shaped_all if not self.in_conflict_dir(p)]
        dirs_hit = {p.parent for p in shaped_all}
        self.conflict_stems = {p.stem for p in shaped}
        # Batch evidence: a sync client corrupts in bulk, so many conflict-shaped
        # files spread across directories is itself evidence of a sync event.
        # Link evidence alone has ~3% recall on a real event, because the notes
        # that rot are the ones nothing links to.
        detected = (len(shaped_all) >= SYNC_BATCH_MIN_FILES
                    and len(dirs_hit) >= SYNC_BATCH_MIN_DIRS)
        self.batch_event_detected = detected
        self.batch_event = self.sync_override if self.sync_override is not None else detected

        counts = {"orphan": 0, "identical": 0, "divergent": 0, "ambiguous": 0,
                  "named": 0, "empty": 0, "cross_store": 0, "chain": 0}

        def linkers(stem: str) -> set[Path]:
            """DISTINCT docs linking `stem`, excluding conflict-shaped files —
            rot must not vouch for rot — that includes files inside a conflict
            DIRECTORY, which is how a sync client manufactures a second
            independent-looking witness. Deduped by file: one doc citing the same
            copy twice is one witness, not two, or a single log.md defeats the
            plurality bar that protects divergent content."""
            return {f for f in self.link_targets.get(stem, [])
                    if not conflict_base(f.stem) and not self.in_conflict_dir(f)}

        for canon, copies in sorted(by_canon.items()):
            copies = [c for c in copies if not self.in_conflict_dir(c)]
            if not copies:
                continue          # DUPE-DIR merge subsumes shadow-dir files
            if len(copies) > 1:
                counts["chain"] += len(copies)
                self.doomed.update(copies)
                self.add("ERROR", "DUPE-CHAIN",
                         f"{len(copies)} copies compete for {canon.name} "
                         f"(canonical {'exists' if canon.exists() else 'is MISSING'}): "
                         + ", ".join(c.name for c in copies),
                         self.rel(copies[0]),
                         "resolve by hand — a bulk rename would destroy all but one")
                continue

            p = copies[0]
            r = self.rel(p)
            if p not in self.text:        # unreadable; already reported in build()
                continue
            try:
                empty = p.stat().st_size == 0
            except OSError:
                self.add("WARN", "IO-ERROR", "unreadable at conflict-classification time", r)
                continue
            named = linkers(p.stem)

            # Ladder step 2: zero length outranks everything but byte-identity.
            # An empty copy has nothing to merge and must never be renamed.
            if empty and not canon.exists():
                counts["empty"] += 1
                self.doomed.add(p)
                self.add("WARN", "DUPE-EMPTY",
                         "0-byte copy and no canonical — renaming would shadow real content "
                         "with an empty file", r, "delete; do not rename")
                continue

            if canon.exists():
                try:
                    same = canon.read_bytes() == p.read_bytes()
                except OSError:
                    self.add("WARN", "IO-ERROR", "unreadable at conflict-classification time", r)
                    continue
                if same:
                    counts["identical"] += 1
                    self.doomed.add(p)
                    self.add("WARN", "DUPE-IDENTICAL", f"byte-identical to {canon.name}",
                             r, "safe to delete (re-verify with cmp at fix time)")
                elif empty:
                    counts["empty"] += 1
                    self.doomed.add(p)
                    self.add("WARN", "DUPE-EMPTY",
                             f"0-byte copy beside an intact {canon.name} — nothing to merge",
                             r, "delete; do not rename")
                elif len(named) >= 2:
                    # Divergence is a CONTENT-RISK statement. Only strong, plural
                    # link evidence may downgrade it — a single stray link to a
                    # rotted copy must never hide content held in only one place.
                    counts["named"] += 1
                    self.add("WARN", "DUPE-NAMED",
                             f"differs from {canon.name}, but {len(named)} docs link "
                             f"[[{p.stem}]] by its full name — likely a distinct document "
                             f"that merely shares a prefix", r,
                             "do NOT merge or delete; verify these are separate docs")
                else:
                    counts["divergent"] += 1
                    self.doomed.add(p)
                    note = (f" ({len(named)} doc links this name — check it is not a real "
                            f"title before merging)" if named else "")
                    self.add("ERROR", "DUPE-DIVERGENT",
                             f"differs from {canon.name} — content may exist in only one "
                             f"copy{note}", r, "diff and merge by hand, then delete the copy")
                continue

            twins = [q for q in self.stems.get(canon.stem, []) if not self.in_conflict_dir(q)]
            lost = linkers(canon.stem)
            if twins:
                counts["cross_store"] += 1
                self.add("WARN", "DUPE-CROSS-STORE",
                         f"canonical exists at {self.rel(twins[0])}", r,
                         "compare against that file; do not rename into place")
            elif lost and canon.stem in self.mem_stems:
                # The linkers resolve to the memory store (suppression #1).
                # That is not evidence a kb/ original was lost.
                counts["ambiguous"] += 1
                self.add("WARN", "DUPE-AMBIGUOUS",
                         f"no kb/ canonical; {len(lost)} doc(s) link [[{canon.stem}]] "
                         f"which resolves to the memory store — not evidence of a lost "
                         f"kb/ original", r,
                         "CONFIRM BY HAND; never bulk-rename this class")
            elif lost:
                counts["orphan"] += 1
                self.doomed.add(p)
                self.add("WARN", "DUPE-ORPHAN",
                         f"no canonical, but {len(lost)} doc(s) link [[{canon.stem}]] — "
                         f"evidence the original was lost", r,
                         f"likely mv to {canon.name} + re-point inbound links — CONFIRM BY HAND")
            elif named:
                # Ladder step 4 outranks step 5: real docs citing this file by its
                # FULL name is direct evidence of a real title, and a sync event
                # elsewhere in the tree says nothing about this file.
                counts["named"] += 1
                self.add("WARN", "DUPE-NAMED",
                         f"{len(named)} doc(s) link [[{p.stem}]] by its full name — evidence "
                         f"this is a legitimate title, not a copy", r,
                         "do NOT rename; likely a real doc whose title ends in a number")
            elif self.batch_event:
                counts["orphan"] += 1
                self.doomed.add(p)
                if self.sync_override:
                    why = (f"forced via --sync-event ({len(shaped_all)} shaped / "
                           f"{len(dirs_hit)} dirs, {len(shaped)} classified; the "
                           f">={SYNC_BATCH_MIN_FILES}/>={SYNC_BATCH_MIN_DIRS} bar is "
                           f"{'MET' if self.batch_event_detected else 'NOT met'})")
                else:
                    why = (f"batch evidence met ({len(shaped_all)} shaped files across "
                           f"{len(dirs_hit)} dirs; {len(shaped)} classified")
                    if len(shaped_all) > len(shaped):
                        why += (" — shadow-dir copies are DUPE-DIR, not this rename set")
                    why += ")"
                self.add("WARN", "DUPE-ORPHAN", f"no canonical; {why}", r,
                         f"likely mv to {canon.name} + re-point inbound links — "
                         f"CONFIRM BY HAND against the printed plan")
            else:
                counts["ambiguous"] += 1
                if self.sync_override is False and self.batch_event_detected:
                    why = (f"no canonical sibling, no link evidence, and batch evidence "
                           f"SUPPRESSED via --no-sync-event (the bar IS met: {len(shaped_all)} "
                           f"shaped files across {len(dirs_hit)} dirs)")
                else:
                    why = ("no canonical sibling, no link evidence, and no sync-batch "
                           "signal — filename shape alone cannot prove this is a sync artifact")
                self.add("WARN", "DUPE-AMBIGUOUS", why, r,
                         "CONFIRM BY HAND; never bulk-rename this class")

        for d in sorted(self.conflict_dirs):
            if any(a in self.conflict_dirs for a in d.parents):
                continue          # merging the outer directory subsumes this one
            self.add("ERROR", "DUPE-DIR",
                     "directory is a conflict copy; its files shadow the real tree and can "
                     "mask broken links", self.rel(d), "merge into the canonical directory")

        for sib in sorted(self.repo.glob("kb *")) + sorted(self.repo.glob("kb-research *")):
            if sib.is_dir() and conflict_base(sib.name) in ("kb", "kb-research"):
                self.add("ERROR", "DUPE-STORE",
                         "an entire KB store appears to be a sync conflict copy; nothing "
                         "inside it is linted", self.rel(sib),
                         "merge into the canonical store, then re-run")
        self.stats["conflict_dirs"] = len(self.conflict_dirs)
        self.stats["conflict_copies"] = dict(total=len(shaped),
                                             shaped_all=len(shaped_all),
                                             dirs_hit=len(dirs_hit),
                                             batch_event=self.batch_event,
                                             batch_event_detected=self.batch_event_detected,
                                             batch_event_forced=self.sync_override is True,
                                             batch_event_suppressed=self.sync_override is False,
                                             **counts)


    def check_inbound_links(self):
        """A link pointing AT a conflict copy breaks the moment that copy is
        renamed. Tier 1 must re-point these in the same pass."""
        seen: set[tuple[Path, str]] = set()
        n = 0
        for p, body in self.text.items():
            for m in LINK_RE.finditer(strip_code(body)):
                t = m.group(1).strip()
                leaf = t.split("/")[-1]
                stem = leaf[:-3] if leaf.endswith(".md") else leaf
                if stem not in self.conflict_stems or (p, t) in seen:
                    continue
                seen.add((p, t))
                same_stem = self.stems.get(stem, [])
                if "/" in t:
                    want = PurePosixPath(t if t.endswith(".md") else t + ".md").parts
                    targets = [q for q in same_stem if q.parts[-len(want):] == want]
                else:
                    targets = same_stem
                n += 1
                if any(q in self.doomed for q in targets):
                    self.add("ERROR", "DUPE-INBOUND-LINK",
                             f"[[{t}]] points at a copy scheduled for rename or deletion — "
                             f"this link dies with it", self.rel(p),
                             "re-point to the canonical name in the SAME pass as the fix")
                else:
                    self.add("WARN", "DUPE-INBOUND-LINK",
                             f"[[{t}]] names a conflict-shaped file that is not scheduled for "
                             f"any change — the link is fine unless that file moves",
                             self.rel(p), "no action unless you change the target")
        self.stats["inbound_conflict_links"] = n

    def check_links(self):
        broken = mispathed = 0
        for p, body in self.text.items():
            if p in self.indexes:      # reported by check_index; avoid double-counting
                continue
            for m in LINK_RE.finditer(strip_code(body)):
                t = m.group(1).strip()
                if not t:
                    self.add("WARN", "LINK-MALFORMED", "empty wikilink [[ ]]", self.rel(p),
                             "remove it or fill in a target")
                    continue
                ok, note = self.resolves(t, p)
                if ok:
                    continue
                if note:
                    mispathed += 1
                    self.add("WARN", "LINK-MISPATHED", f"[[{t}]] — {note}", self.rel(p),
                             "correct the path")
                    continue
                broken += 1
                hint = self.near_miss(t)
                self.add("ERROR", "LINK-BROKEN",
                         f"[[{t}]] resolves to nothing"
                         + (f" — did you mean [[{hint}]]?" if hint else ""),
                         self.rel(p),
                         f"re-point to {hint}" if hint else
                         "create it, fix the slug, or drop the link")
        self.stats["broken_links"] = broken
        self.stats["mispathed_links"] = mispathed

    def check_index(self):
        dangling = 0
        for idx in self.indexes:
            for m in LINK_RE.finditer(strip_code(self.text.get(idx, ""))):
                t = m.group(1).strip()
                if not t:
                    self.add("WARN", "LINK-MALFORMED", "empty wikilink [[ ]]", self.rel(idx),
                             "remove it or fill in a target")
                    continue
                ok, note = self.resolves(t, idx)
                if ok:
                    continue
                dangling += 1
                if note:                      # M5: keep the precise diagnosis
                    self.add("ERROR", "INDEX-MISPATHED", f"cites [[{t}]] — {note}",
                             self.rel(idx), "correct the path in the index entry")
                else:
                    hint = self.near_miss(t)
                    self.add("ERROR", "INDEX-DANGLING",
                             f"cites [[{t}]] which does not exist"
                             + (f" — did you mean [[{hint}]]?" if hint else ""),
                             self.rel(idx),
                             f"re-point to {hint}" if hint else
                             "create the file or remove the entry")
        self.stats["index_dangling"] = dangling

    def check_articles(self):
        no_fm = no_rel = bad_fm = stale = 0
        today = dt.date.today()
        for p in self.files:
            # DOOMED copies are scheduled for rename/deletion — don't ask anyone
            # to backfill frontmatter into a file that's about to disappear.
            # Non-doomed conflict-shaped files (NAMED / AMBIGUOUS / CROSS-STORE)
            # may be real documents and stay in scope.
            if p.name in NON_ARTICLE or p in self.doomed or not self.is_curated(p):
                continue
            r, text = self.rel(p), self.text.get(p)
            if text is None:
                continue
            if not re.search(r"^##+\s+Related", text, re.M):
                no_rel += 1
                self.add("WARN", "NO-RELATED", "curated article has no '## Related'", r,
                         "add cross-links per MAINTENANCE.md")
            m = FM_RE.match(text)
            if not m:
                no_fm += 1
                self.add("WARN", "NO-FRONTMATTER", "no YAML frontmatter", r,
                         "add status/updated/tags")
                continue
            fm = m.group(1)
            keys = set(KEY_RE.findall(fm))
            missing = [k for k in REQUIRED_FM if k not in keys]
            if "/failures/" in r and "severity" not in keys:
                missing.append("severity")
            if "/decisions/" in r and "date" not in keys:
                missing.append("date")
            if missing:
                bad_fm += 1
                self.add("WARN", "FM-FIELDS", "frontmatter missing: " + ", ".join(missing), r)

            st = STATUS_RE.search(fm)
            raw_status = st.group(1).split("#")[0].strip().strip("\"'").lower() if st else ""
            is_active = raw_status == "active"
            up = UPDATED_RE.search(fm)
            dm = DATE_RE.search(up.group(1)) if up else None
            if is_active and up and not dm:
                self.add("WARN", "FM-UNPARSEABLE-DATE",
                         f"status:active but updated is not a date: {up.group(1).strip()!r}", r)
            elif is_active and dm:
                try:
                    age = (today - dt.date(*map(int, dm.groups()))).days
                except ValueError:
                    self.add("WARN", "FM-UNPARSEABLE-DATE",
                             f"status:active but updated is not a valid date: "
                             f"{up.group(1).strip()!r}", r)
                    continue
                if age > self.stale_days:
                    stale += 1
                    self.add("WARN", "FM-UPDATED-OLD",
                             f"status:active but updated {age}d ago", r,
                             "re-verify, or mark superseded. NOTE: this checks metadata age, "
                             "not content drift — `make doc-drift` is the content check")
        self.stats.update(curated_articles=sum(1 for p in self.text if self.is_curated(p)
                                               and p.name not in NON_ARTICLE
                                               and p not in self.doomed),
                          no_related=no_rel, no_frontmatter=no_fm,
                          frontmatter_incomplete=bad_fm, stale_active=stale)

    def check_skills(self):
        """Every /skill routed in CLAUDE.md must have a loadable SKILL.md.
        Catches conflict-copy rot, missing dirs, and unparseable frontmatter."""
        sk = self.repo / ".claude" / "skills"
        claude_md = self.repo / "CLAUDE.md"
        routed: set[str] = set()
        if not claude_md.exists():
            self.add("WARN", "SKILL-ROUTING-MISSING",
                     "no CLAUDE.md — no skill is checked this run", None,
                     "expected at the repo root")
        else:
            text = read(claude_md)
            if text is None:
                self.add("WARN", "IO-ERROR",
                         "CLAUDE.md unreadable — no skill is checked this run", "CLAUDE.md",
                         "fix permissions and re-run")
            else:
                tbl = text.split("## Skill routing", 1)
                if len(tbl) > 1:
                    routed = {m.group(1) for m in ROUTE_RE.finditer(tbl[1].split("\n##", 1)[0])}
                else:
                    self.add("WARN", "SKILL-ROUTING-MISSING",
                             "CLAUDE.md has no '## Skill routing' section — no skill is checked",
                             "CLAUDE.md", "restore the heading or update this check")
                # Critical-rules skills are not in the table. Always check
                # the allowlist — requiring a CLAUDE.md mention left /pickup
                # invisible because it is not backtick-routed there.
                # Whole-file `/name` matching would also hit `/scoreboard`.
                routed |= set(MANDATED_SKILLS)
        dead = []
        for name in sorted(routed):
            d = sk / name
            f = d / "SKILL.md"
            if not d.is_dir():
                dead.append(name)
                self.add("ERROR", "SKILL-MISSING",
                         f"/{name} is routed in CLAUDE.md but .claude/skills/{name}/ does not exist",
                         "CLAUDE.md", "create the skill or drop it from the routing table")
            elif not f.exists():
                dead.append(name)
                alt = sorted(d.glob("SKILL *.md"))
                self.add("ERROR", "SKILL-ROT",
                         f"/{name} has no SKILL.md"
                         + (f" — only {alt[0].name}, a conflict copy" if alt else ""),
                         self.rel(f),
                         f"mv '{alt[0].name}' SKILL.md" if alt else "create SKILL.md")
            else:
                fm = FM_RE.match(read(f) or "")
                if not fm or "name:" not in fm.group(1):
                    dead.append(name)
                    self.add("ERROR", "SKILL-FRONTMATTER",
                             f"/{name} SKILL.md has no parseable name: frontmatter — "
                             "the skill will not load", self.rel(f), "add name: + description:")
        self.stats["routed_skills"] = len(routed)
        self.stats["dead_skills"] = dead

    def run(self):
        self.build()
        # Counted from the analysed set, not a fresh directory walk — with
        # concurrent writers a re-walk describes a different corpus than the
        # one every check ran against.
        self.stats["kb_files"] = sum(1 for p in self.files if self.kb in p.parents)
        self.stats["kb_research_files"] = sum(1 for p in self.files if self.kbr in p.parents)
        self.check_conflicts()
        self.check_inbound_links()
        self.check_links()
        self.check_index()
        self.check_articles()
        self.check_skills()
        return self


ORDER = {"ERROR": 0, "WARN": 1}


def main():
    ap = argparse.ArgumentParser(description="KB structural health check.")
    ap.add_argument("--repo", default=None,
                    help="repo root (default: git toplevel, else this script's repo)")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    ap.add_argument("--stale-days", type=int, default=180,
                    help="age at which a status:active doc is flagged (default 180)")
    ap.add_argument("--quiet", action="store_true", help="summary only")
    ap.add_argument("--sync-event", dest="sync", action="store_true", default=None,
                    help="force sync-event mode: treat conflict-shaped files as batch rot "
                         "even if the surviving count is below threshold. Use during an "
                         "INCREMENTAL cleanup — fixing part of a batch drops the count and "
                         "would otherwise strand the remainder as DUPE-AMBIGUOUS.")
    ap.add_argument("--no-sync-event", dest="sync", action="store_false",
                    help="force sync-event mode OFF")
    a = ap.parse_args()

    if a.repo:
        repo = Path(a.repo).resolve()
    else:
        try:
            repo = Path(subprocess.run(["git", "rev-parse", "--show-toplevel"],
                                       capture_output=True, text=True, check=True,
                                       cwd=Path(__file__).parent).stdout.strip())
        except Exception:
            repo = Path(__file__).resolve().parents[3]

    try:
        lint = Lint(repo, a.stale_days, sync_override=a.sync).run()
    except Exception as exc:
        # SystemExit is BaseException, so the exit-2 floor passes straight through;
        # a crash must never look like "clean" or "ERROR findings".
        print(f"kb-lint: aborted — {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    errs = [f for f in lint.findings if f["severity"] == "ERROR"]

    if a.json:
        print(json.dumps({"repo": str(repo), "stats": lint.stats,
                          "findings": lint.findings}, indent=2))
        return 1 if errs else 0

    s = lint.stats
    c = s["conflict_copies"]
    print(f"kb-lint — {s['kb_files']} kb/ + {s['kb_research_files']} kb-research/ files, "
          f"{s['curated_articles']} curated   [{repo}]")
    print(f"  conflict copies : {c['total']} classified / {c['shaped_all']} shaped"
          + (" [SYNC EVENT detected]" if c["batch_event_detected"] else "")
          + (" (batch mode forced via --sync-event)" if c["batch_event_forced"] else "")
          + (" (batch mode suppressed via --no-sync-event)" if c["batch_event_suppressed"] else "")
          + f"\n                    orphan {c['orphan']} / identical {c['identical']} / "
            f"divergent {c['divergent']} / ambiguous {c['ambiguous']} / named {c['named']} / "
            f"empty {c['empty']} / cross-store {c['cross_store']} / chains {c['chain']} / "
            f"shadow dirs {s['conflict_dirs']}")
    print(f"  links           : {s['broken_links']} broken, {s['mispathed_links']} mispathed, "
          f"{s['inbound_conflict_links']} pointing at conflict copies")
    print(f"  index           : {s['index_entries']} entries, {s['index_dangling']} dangling")
    print(f"  curated gaps    : {s['no_related']} no-Related, {s['no_frontmatter']} no-frontmatter, "
          f"{s['frontmatter_incomplete']} incomplete-fm, {s['stale_active']} old")
    print(f"  skills          : {s['routed_skills']} routed"
          + (f", DEAD: {', '.join(s['dead_skills'])}" if s["dead_skills"] else ", all loadable"))

    if not a.quiet:
        buckets: dict[str, list] = {}
        for f in sorted(lint.findings, key=lambda f: (ORDER[f["severity"]], f["code"])):
            buckets.setdefault(f"{f['severity']} {f['code']}", []).append(f)
        for key, group in buckets.items():
            print(f"\n{key}  ({len(group)})")
            for f in group[:10]:
                print(f"  {f['file']}\n      {f['message']}")
                if f.get("fix"):
                    print(f"      fix: {f['fix']}")
            if len(group) > 10:
                print(f"  … {len(group) - 10} more (--json for the full list)")

    print(f"\n{len(errs)} ERROR, {len(lint.findings) - len(errs)} WARN")
    return 1 if errs else 0


if __name__ == "__main__":
    sys.exit(main())
