# Delegating work to external CLIs (Grok, Codex)

**Why:** Claude usage is the scarce resource in this workspace (7+ concurrent kalshi sessions,
each running adversarial-review subagents). Mechanical, high-token, read-mostly work should go
to another vendor's CLI. A second vendor also gives genuinely independent review — a different
model reading the same artifacts catches different things, and "two vendors, zero CRITICALs"
is stronger evidence than two rounds from the same family.

Operator directive 2026-09-06: "feel free to delegate tasks to grok and codex cli so we use
less claude usage" + "make sure we engrain this in our harness and workflows". Operator holds a
**SuperGrok** subscription — lean on Grok first.

## Status (verified 2026-09-06)

| CLI | Binary | Auth | Works? |
|---|---|---|---|
| **Grok** | `/Users/gabrielkagan/.grok/bin/grok` (1.0.13) | operator's SuperGrok login | **PARTIAL** — see reliability note below |
| Codex | `/opt/homebrew/bin/codex` (0.153.4, `npm i -g @openai/codex`) | `~/.codex/auth.json`, ChatGPT-account mode | **NO** — every model id tried (gpt-5.x, gpt-5*-codex, o3, o4-mini, gpt-4.1, codex-mini-latest) returns `The '<model>' model is not supported when using Codex with a ChatGPT account`. Re-test after a CLI upgrade or with an `OPENAI_API_KEY` in `auth.json` instead of ChatGPT-account auth. |

## Grok invocation (the pattern that works)

```bash
grok -p "<prompt>"                      # single-turn headless, prints to stdout, exits
grok -p "$(cat prompt.txt)"             # long prompts: write to a file first
grok -p "..." --output-format json      # machine-readable
grok -p "..." --json-schema '{...}'     # structured output, implies --output-format json
grok --agent <name> / --agents <JSON>   # subagent definitions
grok -m <MODEL>                         # model override
```

Run it from the repo root (or pass `--cwd`); it reads files and runs tools on its own. It is
NOT sandboxed by Claude's permission system — treat it like any shell command: never hand it a
prompt that could write to `bot/`, push, or deploy. Read-and-report only.

Always run it **backgrounded** from Claude (`run_in_background: true`) — a review pass takes
minutes and blocks the session otherwise.

## What to delegate (do this by default)

1. **Second-opinion adversarial review.** Every findings doc / postmortem before it lands, and
   every load-bearing number. Runs in PARALLEL with the track's own gate rounds; it does not
   replace them. Prompt shape that works (see `kb/decisions/` for a real one):
   - state the domain and the claim being made,
   - rank the failure modes you want hunted (stats errors → underpowered-claimed-as-null →
     asserted-not-demonstrated filters → internal contradictions → known-caveat interactions),
   - demand severity + exact quote + why-wrong + specific fix per finding,
   - require an explicit "ZERO CRITICAL, ZERO MAJOR" line if it finds nothing.
2. **Doc-drift sweeps** after a constant/path/name change (the `make doc-drift` class of work).
3. **Log and corpus forensics** — "which hours have http_status != 200", "when did X start".
4. **Test-failure triage** on flaky CI (read the log, classify, propose).

## What NOT to delegate

**AMENDED 2026-09-06 ~23:5xZ.** The original rule read "anything that writes to
`bot/`, pushes, opens a PR, merges, or touches the VPS." The operator has since
directed Grok to author code and open PRs directly — PR #182
(`perf/scan-preloop-and-twap-breaker`, 12 files including `bot/executor.py`,
`bot/scanner/__init__.py`, `bot/feeds/kalshi.py`, `bot/twaplock.py`) is Grok's
work. That is the operator's call and this doc follows practice rather than
the other way round.

What the amendment does NOT change is the **gate**. `CLAUDE.md` requires
2-consecutive-zero adversarial review for `bot/` changes, and an external CLI
does not run that gate on itself. So the rule becomes:

- **Grok MAY author `bot/` changes and open PRs.**
- **A Grok-authored PR still needs a Claude-side adversarial pass before merge**,
  because the review discipline is a property of the change, not of the author.
  Merge authority stays with the operator either way.
- Still never delegate: merging, pushing to main, or anything touching the VPS.

Empirically, PR #182 was good work — its twaplock circuit breaker is a real
production bug fix (2,582 `tw-` api_errors against 2 fills ever, last fill
2026-06-17), and the diagnosis is one no static reading would catch: each 15M
window is a NEW ticker, so the executor's per-ticker `TICKER_API_ERROR_CAP`
resets every window and can never trip on a cross-ticker drip. A per-ticker cap
structurally cannot stop a per-ticker-renewing failure.

The one thing to hold Grok to is the same thing every track was held to today:
**provenance for load-bearing numbers.** "VPS 2026-09-06: 2582 tw- api_error,
2 fills ever" appears in a code comment with no recorded query. Three tracks had
to retract figures today for exactly that reason.

- Anything that pushes to main, merges, or touches the VPS.
- The **gate itself**. Per `CLAUDE.md` extraction-bit discipline the 2-consecutive-zero rounds
  are the track's own review; Grok is a parallel second opinion, cited as such.
- Judging Grok's own findings. A flag is a **hypothesis**: the owning track verifies it against
  the run artifacts before changing anything (three of eleven findings in Track C's R1–R5 were
  reviewer misreads the data did not support).

## Citing it honestly

A null from an external reviewer is weak evidence. Cite as:
`independent pass by <cli> <model+version> on <date>, prompt recorded at <path>, raised nothing`
— never as "validated by". It read the same artifacts, so it cannot catch an error upstream of
them; only a re-extract/second-way reproduction does that.

## Companion trap: `aws s3 sync` silently skips restored Deep Archive objects

Found 2026-09-06 on Track B's box. `aws s3 sync s3://.../bronze/kraken_ws/book /data/...`
copied ONLY the days that were still STANDARD tier. Every restored DEEP_ARCHIVE object
(`ongoing-request="false"`, valid expiry) was skipped — **exit code 0, no warning, no error**.
Result: kraken had 32 of ~97 days on disk and three venue-dependent mechanisms would have run
to completion reporting "no venue data" for two thirds of the corpus.

Fix: `--force-glacier-transfer` on every sync/cp that can touch restored bronze.

Verify coverage explicitly after any bronze sync — never trust exit 0:
```bash
ls -d /data/venue/<venue>/day=2026-* | wc -l     # against the expected day count
```
Same family as the ESPN 403 and the wedged writer: **a job that keeps succeeding while its
input silently disappears.** Check the count, not the status code.

## Companion trap 2: a restore pass has a shelf life — verify, don't trust the issuance count

Found 2026-09-06. A restore batch covering 05-30→08-05 for `kalshi_ws/trade` reported clean
(zero errors, per-day issued counts logged). Days later a consumer still hit
`Object in GLACIER, restore first` on **758 of 2,009 objects for 08-05** and 6 for 08-04 —
and, exactly as with the sync trap, the missing objects were the **>128 KB busy chunks**, so
the surviving day was biased toward quiet windows. Re-issuing found 738 + 756 objects
genuinely DEEP_ARCHIVE with no restore in flight.

Two candidate causes, both real risks:
- **Rolling lifecycle boundary.** The 30-day rule archives continuously. `08-05 + 30d = 09-04`,
  so days can cross into Deep Archive *after* a restore pass covering them.
- **Silent partial issuance.** An issuance count is not a completion proof.

Rule: after any restore, **re-list the range and assert zero `DEEP_ARCHIVE`-without-`Restore`
before declaring it ready** — and re-check before each consumer run, not once.

```bash
aws s3api list-objects-v2 --bucket B --prefix "$P" \
  --query 'Contents[?StorageClass==`DEEP_ARCHIVE`].Key' --output text | wc -l   # expect 0
```
Third member of the same family (with the ESPN 403 and the wedged writer): **an operation that
reports success while its data silently goes missing, biased toward the busiest samples.**


## Grok reliability note (2026-09-06, two sessions' experience)

Mixed, and you must check for empty output before trusting a "clean" result.

- **Master monitor**: one full adversarial review of a findings doc succeeded and returned two
  substantive MAJORs (one a real methodological error six internal rounds had missed). Required
  `--permission-mode bypassPermissions` — without it `grok -p` HANGS silently at 0% CPU waiting
  on a tool-permission prompt it cannot display in headless mode. That is the first thing to
  check if it produces nothing.
- **ESPN-fix session**: the same CLI answered a trivial prompt but returned EMPTY output on the
  real review prompt across four invocations, including an isolated sandbox with
  `--always-approve`. No error, just nothing.

Practical rules: (1) always background it and check the byte count of the output file — an empty
result is a FAILURE, never a "clean review"; (2) if it hangs, suspect the permission prompt;
(3) if it returns empty repeatedly, fall back rather than burn rounds on it; (4) never let a
Grok null substitute for a track's own gate round.

## The day's unifying failure mode: silence is not evidence

2026-09-06 produced five instances of ONE bug shape across five different layers. Learn the
shape, not the instances:

| Layer | What reported success | What was actually true |
|---|---|---|
| `aws s3 sync` | exit 0, no warning | every restored DEEP_ARCHIVE object silently skipped (32 of 97 days present) |
| S3 restore batch | per-day issued counts, zero errors | 758 of 2,009 objects still archived days later — biased to the >128 KB busy chunks |
| ESPN collector | chunk counts normal, units active | every row an HTTP 403; 33 days of no data across 24 leagues |
| `box_batch.sh` | printed `BOX_BATCH_DONE` | `zoo_aggregate` tracebacked; aggregate.txt held only the traceback |
| A research query | printed "no trade prints at checkpoint" | the ROW SET was empty; the true figure was 46-82% having prints |

General form: **an empty or well-formed-but-vacuous result read as a positive finding.** The
defence is the same everywhere and costs one line: assert the expected COUNT, never the exit
code or the absence of an error.

```bash
[ "$(ls -d "$d"/day=2026-* | wc -l)" -ge "$EXPECTED" ] || { echo "COVERAGE SHORT"; exit 1; }
```
In analysis code: distinguish "no rows matched" from "rows matched and the value is zero" — they
must not print the same sentence.

## Two-vendor review: the empirical case (2026-09-06)

Every finding produced today went through a track's own adversarial rounds AND, where the CLI
worked, an independent Grok pass. The independent pass was not redundant:

- **Track C**: 6 internal rounds passed a "clean holdout" that shared 9 of its 12 days with the
  estimation sample. Grok caught it. Track C accepted and rewrote.
- **Track B**: its headline "no tradeable seam" was measuring the wrong population — 109 of 153
  rows priced ≥99¢ on already-decided windows, so the result was just the 1¢ fee, and the
  near-money band had the OPPOSITE sign. Grok caught it; Track B reproduced every number and
  changed the conclusion to INCONCLUSIVE.
- **Track H**: Claude adv-review and Grok independently found **5 CRITICALs each** on the same
  first results set. Entire set withdrawn before publication.

Lesson: a track reviewing its own work converges on internal consistency, not on correctness.
A reviewer that did not build the artifact asks different questions. Budget for it.

### Failure modes worth pre-registering against (all observed today)
- **as-of book samples with no max age** — median book 3.6 DAYS stale behind a calibration table.
- **print-conditioned staleness guards** — structurally blind inside a data gap, because the
  prints are missing there too. Use an independent age check.
- **clustering on the wrong unit** — GAME/SPREAD/TOTAL are three tickers but ONE game; clustering
  on ticker overstated precision ~3x.
- **applying a criterion stricter than the one pre-registered** — Track H's headline "0 of 45
  cells" was false in the CONSERVATIVE direction; one cell survived the criterion as written.
  Re-read the pre-registration against what the code enforces. The bias runs both ways.

## Timing lessons (2026-09-06) — measure over a full period, not a snapshot

Three separate tracks produced a wrong conclusion from too short an observation window:

- **A periodic anomaly asserted from 2 observations.** A 2,192-ticker subscribe burst was called
  "one-off stale-boot" (1 point), then "hourly at :40" (2 points), then correctly "fires when
  `tracked` is stale" (3 points — the third was 60, not 2,192). The discriminating event was a
  REST refresh COMPLETING at 19:51:38 after 4,692 s. Rule: **on a periodic system, do not assert
  a pattern until you have observed a full period beyond the suspected cause.**
- **A corpus counted while it was still downloading.** "4 game days" was an artifact of download
  timing, not of the data. Snapshot a completed input set before analysing it.
- **Clustering on a unit shorter than the event.** Day-clustering split a 4-day golf tournament
  into 4 "independent" clusters (anti-conservative); ticker-clustering split one football game's
  GAME/SPREAD/TOTAL into 3 (also anti-conservative, ~3x). The fix that survives both: compute
  BOTH candidate units and let the **wider interval and larger p** decide.

Common root: **the measurement window was shorter than the period of the thing being measured.**
Before quoting a rate, a frequency, or a cluster count, state the period of the underlying
process and confirm the window exceeds it.

## Relay rule: pass the artifact PATH, never a summary of findings

2026-09-06, master monitor's own error. A Grok review artifact was 86 lines; it was read with
`tail -30`, and the resulting relay said "0 CRITICAL, 2 MAJOR". **Line 1 of the file read
"CRITICAL (1), MAJOR (4)"** and line 5 was the CRITICAL — a finding that reversed the receiving
track's entire verdict from "null" to "real effect, underpowered". The track recorded the relayed
count as fact rather than opening the file, and three findings including the CRITICAL never
reached it.

Rules:
1. **Relay the path, not the content.** `/path/to/artifact.out (86 lines)` plus one line of
   context. The owning track reads it.
2. **Never state a findings count you did not read in full.** Check line count before quoting a
   tally: `wc -l` then read the head — reviewers put the summary at the TOP.
3. **A relayed summary is a lossy re-derivation with no provenance** — downstream cannot tell
   that it omitted something, which is exactly the silent-incompleteness failure this doc is
   otherwise about. The relay pattern is a hazard in itself, not just its content.

## UNDERPOWERED is not NULL — report the MDE or the finding does not stand

2026-09-06. Four of five research tracks published a headline that an independent reviewer
overturned, and the single most common root was **an underpowered instrument read as evidence
of absence**. This is now a publication requirement, not advice.

**Rule: no track may report "no effect", "calibrated", "nothing survived", or "at the null
rate" without the minimum detectable effect beside it**, computed at the same bar the verdict
used (`1.96*SE` for a raw CI, the Holm/BH-corrected bar where multiplicity applies, and
inflated by the clustering unit actually used). A cell whose MDE exceeds the tradeable
threshold is UNDERPOWERED and must be labelled as such — visually distinguishable from a true
null in any table a later reader will scan.

Measured MDEs from the day, as calibration for what these instruments can actually see:

| Track | Instrument | Realised MDE | Consequence |
|---|---|---|---|
| C | 12 UTC days, Holm bar | 3.1c at T-30; 7.2-8.2c at T-90/180/300 | its "null" was a −8.1c effect it could not resolve |
| B | one day, pre-registered <95c band | 12.7c at T-90 -> 39.5c at T-5 | the doc had claimed 2c: an order of magnitude too flattering |
| B | 97 days, T-30 near-money (19 rows/day) | ~2.8c row-level, ~1.3x worse day-clustered | may STILL be underpowered against a ~3c cost threshold |
| power probe (Jun) | middle bands, sigma ~45c | 6-15c per cell | 131-710 days/cell needed; the constraint is signal, not days |

Two corollaries that cost real verdicts today:
- **Report the pre-registered gate count AND the post-filter count side by side.** Correcting
  only the winners launders the selection; Track D reported cells selected from ~49,000
  intervals with no multiplicity control at all.
- **Compare the right quantity against chance.** Track C's "51 vs 48.6, i.e. the null rate"
  was wrong because the flagged set is a SUBSET of CI-excludes-zero; the honest comparison was
  171 vs 48.6 = **3.5x chance**. Check what your denominator actually contains.

A multiplicity bar over thousands of cells can lack power against a genuinely tradeable
effect. "Nothing survived" and "the instrument cannot see" are different sentences and must
not be printed the same way.

## Never build a second glob: validate through the resolver the consumer actually uses

2026-09-06, Track B. A venue corpus was physically on disk (151 GB of kraken) while the
`day=` symlink layer the resolver reads held only 32 of 97 days. Every coverage check that
counted files or symlinks with its own glob reported a number that had nothing to do with what
the pipeline would resolve at run time.

The fix that holds: a `preflight` subcommand that resolves every day for every venue **through
the same `_venue_day_chunks` resolver staging uses**, names the missing days, and exits non-zero
before the batch starts — with a deliberately awkward `ZOO_ALLOW_PARTIAL_VENUE=1` override.
Because it shares the resolver rather than reimplementing the lookup, it cannot drift from what
staging actually reads. A separate glob is a second source of truth, and a second source of
truth is the bug.

Companion trap found while writing that check: **shell tilde expansion fires only at the start
of a word**, so `ZOO_VENUE_ROOTS=/data/venue,~/other` silently yields a literal `~/other` that
resolves nothing. `expanduser` every element of a delimited path list. Same silent-partial-input
family as the rest of this document — and it appeared inside the very hour spent hardening
against that family, which is the point: the shape recurs faster than anyone patches instances
of it.

## A cost measured over partial input is a LOWER BOUND, not a measurement

2026-09-06, Track B, stated by the track and worth generalising. A smoke day exists to produce
a per-day cost to extrapolate a batch from. If part of the input silently failed to resolve,
the run skipped the work that input implies and the cost **under-books the batch in exactly the
direction that hurts** — you provision from a floor and discover it mid-run.

Label every extrapolation input as `measured` or `lower bound`, and say which artifact settled
it (here: `manifest.json`'s `venue_files_mb` map plus the harnesses' `skip_venue_lt2` diag).
Separately: outputs from a single smoke day are a timing artifact, never a result — the MDE
table above shows one day resolves 12.7-39.5c, so no mechanism number should be quoted from it
under any coverage outcome.

## Clock provenance: `_wire_recv_ts` is arrival, and arrival lags event time under load

2026-09-06, Track H, ticket `86bbvrv7a`. **CORRECTED 22:5xZ — the first figures quoted here
were a convenience sample, and the correction is itself the lesson (see below).**

Population-wide over 1.28M in-game samples: `recv_ts − delta ts` is **p50 1.4 s, p90 33.5 s,
p99 48.1 s, with 26% of samples exceeding 15 s**. It scales with load, so it is per-connection
write-queue backlog (WS -> worker -> drain -> writer), not clock skew: specific busy chunks
have a large median (08-29 20Z: 18.4 s; 08-29 21Z: 20.7 s) while quiet ones are near zero
(08-27 23Z: 0.1 s).

**RETRACTED: "p50 12-38 s on game hours."** That came from the first 20,000 frames of a handful
of busy hour files — a convenience sample quoted as a population, wrong by an order of magnitude
at the median. It had already been broadcast to three tracks and written into a ticket before the
population figure existed. Any correction sized off the retracted median must be re-derived from
the distribution.

**RETRACTED: "~22% of anchored books refused as crossed."** The real crossed-book refusal rate is
**0.19-0.86%**. The 22% was the share of books WIDER than a 20-cent quotable cap — a wide market,
not a reconstruction failure. So the reconstruction damage is small; the materiality that actually
bites is the arrival-clock lag, not book corruption.

Consequence for every consumer: any latency or markout number computed on the arrival clock is
wrong by 5-35 s **precisely in the busy hours that matter**, and right in the quiet hours where
nothing is at stake. Sample by delta `ts` / trade `ts_ms`; date snapshots as
`recv − last observed lag on that conn`.

This also **falsifies the 2026-05-28 LOW-materiality verdict** on collector drops, which rested
on orderbook bronze being unconsumed until D3.1. Five tracks consume it now. When a decision
doc's premise is "nothing reads this yet", that premise expires — re-check it before relying on
the conclusion.

## The zstd exit code: 21 of 28 research scripts read partial days silently

2026-09-06, ticket `86bbvrx1t`. Found by Track E as the SIXTH instance of its own
silent-truncation class, then audited repo-wide and found to be the norm rather than the
exception.

Research scripts stream bronze with `subprocess.Popen(["zstd","-dc",path], stdout=PIPE)` and
iterate the pipe. **If the decompressor dies mid-file the generator just stops** — the loop
ends normally, the caller sees ordinary end-of-iteration, and the run reports success on a
prefix of the day. `p.wait()` is usually called; its return value is usually thrown away.

Track E's measured instance: build reported DONE with 7,050 windows against 7,511 expected,
because 06-01 streamed 6,026,544 of 20,502,366 lines and 06-04 streamed 13.3M of 24.9M. The
files were fine — integrity checks passed, mtimes unchanged. Only the discarded exit code.
Its summary is the reason this one outranks the other five: *"217 windows instead of 672 on
one day does not announce itself in a pooled average."* The first five defects produced
something visibly wrong; this one produced something **plausible**.

The correct fix distinguishes EOF from SIGPIPE, because an early `break` kills zstd by design
and naive checking makes every early-exit path throw. Reference implementation is in-tree at
`scripts/research/power_analysis_probe.py` (one of the 7 clean files):

```python
    try:
        for line in p.stdout:
            ...
            if stop_hour is not None and ts >= stop_hour:
                break          # zstd dies of SIGPIPE here — expected, not an error
            yield ...
        else:
            exhausted = True   # only a true EOF sets this
    finally:
        p.stdout.close(); p.wait()
    if exhausted and p.returncode != 0:
        raise RuntimeError(f"zstd -dc {path} exited {p.returncode}: corrupt or truncated")
```

Route every call site through ONE shared checked reader rather than patching 21 sites — a
second implementation is how this drifts back (same rule as never-build-a-second-glob above).
Record per-file line counts in the run marker so a short read is visible after the fact, not
only at the instant it happens.

**Retrospective consequence, unresolved:** the `genhunt/` and `algo_zoo/` families are among
the 21, and they produced the 41-mechanism ZERO-edge result that project memory records as
durable. Partial reads would bias toward finding nothing. This does not overturn that result —
it makes it a hypothesis with a mechanism, and it should be re-run after the fix before any of
its nulls are cited again.

## "A comparison that returns a confident answer to a question you did not ask"

2026-09-06, Track B's phrasing for a family that produced FOUR separate near-misses in one day.
Each one ran cleanly, returned a definite result, and answered a subtly different question than
the one asked:

| The check | What it looked like it compared | What it actually compared |
|---|---|---|
| `day=` partition overlap | two corpora's DATES | day-of-MONTH only — matched June 3 against August 3, manufacturing a 12-day "overlap" between wholly disjoint corpora (05-30..06-10 vs 08-05..09-04). Nearly deleted 29 GB of single-copy data. |
| `ZOO_VENUE_ROOTS=/data,~/other` | two path roots | one root and a literal `~/other` — tilde expands only at the START of a word |
| venue `day=` symlink count | data on disk | the SYMLINK layer — 151 GB present, 32 of 100 days linked |
| object-count parity local vs S3 | that the mirror is complete | that the mirror has the same NUMBER of files — count parity cannot detect a TRUNCATED upload. Use total BYTES. |

The defence is to state the question in words first, then check that the comparison's units are
the units in the question. A confident answer is not evidence the question was right, and none
of these four produced an error, a warning, or an empty result.

## Know your tool's success signal — exit codes, not output matching

2026-09-06, Track D, and it is the mirror image of the zstd bug above. Verifying 17 files by
grepping `zstd -t` output for "OK" reported **all 17 corrupt**: that build prints
`<file>: N bytes` and signals success via EXIT CODE. A 17-of-17 false positive, manufactured by
the checking tool, and nearly acted on.

Note the symmetry: the original bug was exit codes being IGNORED, this one was exit codes not
being CONSULTED. Both are the same misunderstanding of where the signal lives.

Related, from the same session, and now the standard for any corpus writer:
- **`.part` + rename-on-success.** A file under its final name is then complete BY
  CONSTRUCTION — strictly stronger than checking completeness afterwards.
- **Marker-gated readers that PRINT their skips.** A verifier that globbed `day=*.jsonl*`
  without checking the `.done` marker would have fed a truncated day into two published sims.
  Silent exclusion and silent inclusion are the same bug wearing different clothes.

Second confirmed live instance of the truncation class that day: `day=2026-08-19.jsonl.zst` at
34 MB where the day is ~160 MB, under its final name, decompressing cleanly to a prefix. The
first was a 6.0M-of-20.5M-line read. Two in one day retires the argument that this is theoretical.

## Unstated denominators

Twice in one day, in two different tracks' tooling, a ratio was reported whose denominator was
not what a reader would assume:
- Flagged cells are a SUBSET of CI-excludes-zero, so "51 vs 48.6, i.e. the null rate" was the
  wrong comparison; the right one was 171 vs 48.6 = **3.5x chance**. This reversed a verdict.
- "cells able to prove an effect: k/m" where `m` is every Holm-tested cell — including cells
  with too few days to EVER be powered, which are legitimately in the Holm bar (alpha/m) but can
  never appear in `k`.

Whenever printing `k/m`, print what `m` contains.

## Every correction pointed the same way

2026-09-06, Track D, and it is the single most useful sentence anyone produced that day:

> All four defects pointed the same way — the uncorrected procedure produced MORE and LARGER
> findings, and every correction removed them. The cells left standing are the ones that never
> looked interesting.

That is not a coincidence, it is the expected direction. Selection, multiplicity, clustering on
the wrong unit, mirror-image duplicate cells, and raw-vs-effective counts all inflate the TOP of
a ranked table, which is the only part anyone reads. So a research pipeline's errors are
systematically biased toward manufacturing an edge, and a track that has not yet found its
defects is a track whose headline is probably too good.

Practical consequence: **treat "this correction made my result smaller" as confirmation the
correction was right, and "my finding survived every fix unchanged" as the thing to distrust.**

### The Kish effective count — raw N is not N

Track D's `sports_futures` cell reported **49 contests and ranked second overall**. Over 90% of
its contracts came from ONE tournament (a single winner); pooling sibling series added cluster
KEYS without adding independent settlements. The few-cluster guard read 49 and waved it through.

Fix: Kish's effective sample size, `(Σw)² / Σw²`, which collapses to under 2 there and equals
the raw count when weights are equal. It now drives both the few-cluster guard and the t degrees
of freedom. Effect: group verdicts 92 -> 40, series 913 -> 414, and every World Cup / futures
cell left the top of the table.

**Print the effective count beside the raw count wherever both exist** — `49 (effective 1.8)`
makes the defect impossible to reintroduce silently.

### Mirror cells are one finding, not two

Complementary sides of a binary market appear as two cells and rank as two independent findings.
Track D's top two cells (−32¢ and +28¢) were the SAME 21 knockout matches seen from opposite
sides — one directional statement, "favourites lost in 21 matches", counted twice. Multiplicity
correction cannot see this: it assumes cells are distinct tests, and these are the same test with
the sign flipped.

Collapse complementary sides BEFORE ranking, and state how many cells that removed. The
decomposition is also useful in its own right: for complementary prints the two markouts sum to
minus the spread rather than to zero, so `(t+m)/2` is the half-spread both sides pay and
`(t−m)/2` is how far that side sits from the fair midpoint.

## Favourite-longshot bias: real in direction, NOT established as a cross-track replication

**This section was rewritten 2026-09-06 23:3xZ after the monitor over-claimed it.** The first
version said "three independent sightings, two market families, one phenomenon." Track D
checked that against its own corpus and disputed it. The corrected version is below; the
over-claim is left on the record because the mistake is instructive — a shared DIRECTION across
tracks is a pattern match, and calling it a replication skips the step where magnitudes,
band edges and horizons have to reconcile.

### What is actually established (Track D, internally, no cross-track appeal needed)

The right test is not a cell, it is the SHAPE of the band curve: for each family x horizon with
8+ populated bands, Spearman between band rank and taker markout. FLB predicts positive. That is
**73 quasi-independent curves**.

- **53 of 73 slope the FLB way against 36.5 expected — sign test p = 0.0001.**
- **NOT ONE curve survives BH correction** for having looked at 73 of them; the smallest p is
  7.9e-04 against a q/m bar of 6.8e-04.

Both halves are the finding: **the effect is real, it is everywhere, and it is too small to
establish anywhere in particular.** A sign test over many weak tests is not defeated by the
multiplicity that defeats each one individually. This is a stronger claim than any single cell.

### What is NOT established — an open discrepancy, not agreement

Track C reports +3.4c at 85-95c on crypto 15-minute. Track D's same-family bands give **+0.83c
(81-90) and +0.96c (91-95)** at 5m-1h — roughly a QUARTER of that, with both cells marked
`calibrated` and intervals covering zero. Track D's 91-95 band **FLIPS to −0.80c inside the last
five minutes.**

A shared direction with a **4x magnitude gap is one hypothesis and one discrepancy, not a
replication.** The two tracks already found they use different band edges (81-90/91-95 vs 85-95),
which alone could explain part of it. Band bounds, horizon and weighting must be reconciled
before any joint claim.

Track H's three cells rest on **4, 5 and 1 losing games**. One losing game is a single cluster —
that would not clear Track D's few-cluster guard and should not clear anyone's. Do not carry it
as supporting evidence.

### It is NOT universal

Game lines and totals **REVERSE intraday**: `sports_game` at 5m-1h and 1-6h, and `sports_total`
at 1-6h, all slope the wrong way at nominal significance. **"Kalshi exhibits favourite-longshot
bias" is FALSE as stated** — the family and horizon must be attached to the claim.

### What it is worth

At 1-2c it sits under the ~3c round-trip cost. Track D independently found the venue is not
systematically miscalibrated in any way a maker could harvest at scale. **A skew input for
quoting you are already doing, never an entry. Nothing here turns the bot on.**

## The asymmetry test: are you applying a standard, or selecting evidence?

Track E, 2026-09-07, after catching itself doing the thing:

> For any standard you adopt, go find an instance where applying it would have
> made your result look **BETTER**. If there are none, you are not applying a
> standard, you are selecting evidence.

The instance that produced it: Track E spent an entire Bit arguing that day-clustering
is the honest unit, then quoted the WINDOW-cluster interval — the weaker one — to dismiss
its own best near-miss cell (BTC 40-59 NO touch 300s, +5.01c/contract over 704 windows,
day-cluster 99% CI [+0.62,+9.30]). It had policed the direction that would PROMOTE a cell
and not the direction that would KILL one.

Why this is hard to catch from inside: **every individual act of scepticism feels like
rigour.** Killing your own cell feels more honest than keeping it, so the asymmetric
application never trips the internal alarm that a too-good result does. Conservatism is
not a direction-free virtue — applied in one direction only, it is just a different way
of choosing your answer.

This is the self-directed member of the family this document keeps returning to. A
comparison that answers a narrower question than the one asked (the `day=` overlap, the
convenience-sample median, raw-vs-effective N) is that error pointed at the data; the
asymmetry is the same error pointed at yourself.

Companion, from the same session and the same day: **prefer an instrument that RE-MEASURES
over a constant that was measured once.** A cited number is a claim about a population you
did not observe; a measured one travels with the data. Track D's verifier measures
arrival-minus-event lag in whatever corpus it is handed and prints the distribution beside
the result, so when the relayed lag figure was retracted, nothing it had built moved.
