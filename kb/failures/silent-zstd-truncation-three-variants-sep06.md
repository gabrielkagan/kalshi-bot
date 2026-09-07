# Silent zstd truncation: one bug, three spellings, ~28 sites

**Date**: 2026-09-06 → 09-07 · **Tickets**: `86bbvrx1t` (research), `86bbvu94g` (state.db restore)
**Status**: fixed in the working tree; `zstd_stream.py` + tests committed, the per-script guards
live in untracked research files (see "The commit mistake" below).

## What happened

Research code decompresses bronze by shelling out to `zstd` or using the `zstandard` library.
Across **three different spellings** of that, the failure signal was discarded, so a decompressor
that died mid-file produced a **silent PREFIX** and the run reported success.

Three confirmed LIVE instances, found by three different sessions, none of them looking for it:

| Where | Symptom |
|---|---|
| Track E maker-markout build | reported DONE with 7,050 windows vs 7,511 expected — one day streamed **6,026,544 of 20,502,366 lines**, another 13.3M of 24.9M |
| Track D sports-books pull | `day=2026-08-19.jsonl.zst` at **34 MB where the day is ~160 MB**, under its FINAL name, decompressing cleanly to a prefix |
| Track B zoo pipeline | deliberately truncated archive read **7,646 of 20,000 lines** — old pipeline `rc=0`, new `rc=1` |

In all three the FILES were fine. `zstd -t` passed. Only the read was short.

## The three variants

**1. `Popen` + `proc.wait()` with the return code discarded.**
```python
proc = subprocess.Popen(["zstd","-dc",path], stdout=subprocess.PIPE)
for raw in proc.stdout: ...
proc.wait()                      # <- value thrown away
```
The loop just ENDS. The caller sees ordinary end-of-iteration.

**2. `subprocess.run(...).stdout`.** Same bug, different spelling — and the one that mattered
most, because it was `phase1b_real_price_economics._zst_lines`, **the read path under ~38 of the
41 algo_zoo mechanisms**. On truncation zstd writes a prefix to stdout and exits non-zero;
`.stdout` hands back the prefix.

**3. The `zstandard` LIBRARY, which raises nothing at all.** No subprocess, so no exit code to
forget. Measured: `stream_reader` over a half-truncated file yielded **44,617 lines and raised
nothing**. `copy_stream` wrote **2,228,224 of 5,000,000 bytes and returned success**.

## Why the obvious checks do not work

- **`zstd -t` proves nothing about a READ.** Track E's 06-01 passed integrity with all 20.5M
  lines present while the build had consumed 6.0M of them.
- **Byte counts do not detect variant 3.** A truncated file is *fully consumed*; only the frame
  is incomplete. Measured `consumed == size` on both good and truncated inputs.
  `ZstdDecompressionObj.eof` is the only reliable signal — True only when a frame terminated.
- **A naive `if rc != 0: raise` breaks every early-exit consumer.** Closing the pipe sends
  SIGPIPE and zstd exits non-zero legitimately. The distinguishing state is whether the read
  reached EOF, hence the `exhausted` flag throughout the fix.
- **Shell pipelines mask it.** `zstd -dc f | awk ...` reports the LAST command's status, so a
  dying zstd is reported as awk's success. Needs `set -o pipefail` + `executable=/bin/bash`.
- **`py_compile` passes on all of it.** It does not resolve imports or run anything.

## Why it matters more for nulls than for positives

**Truncation drops the END of a file**, so the loss is never uniform. Any analysis whose events
cluster in the tail — window closes, settlement, game hours — loses disproportionately the part
carrying signal, and **the bias runs TOWARD a null**. This corpus mostly produces nulls, which is
exactly the population this defect would manufacture.

Two consequences that are still OPEN:
1. The **41-mechanism ZERO-edge result** ran through variant 2. It is not established that any
   specific run read a truncated file — those scripts logged no per-file row counts, so the cheap
   discriminator (compare logged counts to a checked re-read) is IMPOSSIBLE for them. Only a
   re-run settles it. See `kb/decisions/algo-zoo-rerun-pickup-sep06.md`.
2. **Cached label maps built before `598b3f46` (2026-09-06 23:52:48Z)** came from the truncating
   reader. The trap: cache fingerprints key on the DATA tree, which did not change when the CODE
   did — so a stale cache passes its own freshness check and is served silently. Four quarantined
   to `~/kalshi-research-data/_QUARANTINE_pre_zstd_fix/`.

## The one that was not research

`scripts/ops/_state_db_snapshot.py::decompress` used `copy_stream`, on the **state.db restore
path**. A backup truncated in S3 decompressed to a **partial database with a success return**, and
SQLite will often open a truncated file. Every other instance corrupts an analysis; this one
corrupts a restore, and it only matters at the moment the real database is already gone. Ticket
`86bbvu94g`. Fixed with a chunked `decompressobj` loop asserting `eof`.

## The fix

`scripts/research/zstd_stream.py` — one module, four entry points:
- `checked_stream_lines(path)` — the common line reader
- `run_zstd_checked(path)` — replaces `subprocess.run(...).stdout`
- `checked_zstandard_lines(path)` — library variant, asserts `decompressobj.eof`
- `checked_zstd_byte_stream(path)` — context manager for pickle consumers
- `assert_zstd_ok(proc, path, exhausted=)` — primitive for `zstd | grep` pipelines

15 tests. One **pins the hazard itself** — asserting the raw library returns a silent partial read
— so nobody later "simplifies" the guard away believing the library protects them.

## The process failure, which is the real lesson

**I declared this fixed twice before it was.** First audit grepped for `Popen`, found 21 sites,
fixed 18, and reported the class closed. It had found ONE SPELLING. Variant 2 was discovered only
because a re-run scoping question made me look at what `algo_zoo` actually imports; variant 3 only
because I tested whether the library raises. Both times the report was confident and wrong.

The generalisable failure: **I searched for the shape of the bug I had already seen, then concluded
absence from a search that could not have found the others.** That is the same error this fleet
kept hitting all day in the data — a comparison that returns a confident answer to a question you
did not ask — pointed at a codebase instead.

The defence that would have worked, and which the fix now encodes: **enumerate the ways a thing can
be spelled BEFORE searching, and test the assumption that a library or tool reports its own
failures.** One five-line experiment against a deliberately truncated file would have found all
three variants in the first pass.

## The commit mistake

Fixing this, I ran `git add <file>` on every script I had edited. Sixteen were UNTRACKED local
research scripts. Committing them added modules whose sibling dependencies are still untracked, so
on a clean `git archive HEAD` tree **12 of 18 added files raised ImportError**. Caught by the Track
C session's R19 on one file; the real scope was 16. Reverted via `git rm --cached`, which un-tracks
while preserving every guard in the working tree.

Two lessons: **stage by explicit intent, never by "what did I touch"** — in a tree several sessions
are writing to, that derives your file set from other people's churn. And **a commit ADDING a module
needs an import test on a CLEAN tree**, not a compile check.

## Related

- `agent_docs/external_cli_delegation.md` — "silence is not evidence" family, eight instances
- `kb/failures/espn-403-user-agent-silent-outage-sep06.md` — same shape at the HTTP layer
- `86bbvucck` — book reconstruction, compounds with this in the same direction (fewer windows)
