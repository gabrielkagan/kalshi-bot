# Pickup: re-run the algo-zoo edge hunt on a corrected reader + 113-day corpus

**Created 2026-09-06 ~23:5xZ by the master-monitor session. Paste the block at the
bottom into a FRESH session.**

## Why this re-run exists (two independent reasons, either alone justifies it)

### 1. The corpus was 1.3 days. We now have ~113.

`kb/findings/algo-zoo-edge-hunt-41-mechanisms-may31.md` says so in its own words:

> Zero produced a tradeable edge **on that 1.3-day corpus**.
> "…every real signal is already priced" — is NOT supported by this work and is **withdrawn**.
> **1.3 days cannot** [support the efficiency claim].

So the "market is efficient" reading was already retracted BY THE AUTHORS. What survived
was a much narrower statement: 41 mechanisms found nothing in 1.3 days. Project memory
(`finding_algo_zoo_edge_hunt_41_mechanisms_may31.md`) nonetheless carries the advice
*"Don't re-run; gate future hunts on new DATA not new ideas."* **The new data now exists** —
that instruction's own precondition is met.

Bronze coverage verified 2026-09-06 by listing S3 day partitions:

| source | days |
|---|---|
| `kalshi_ws/orderbook_delta` | 113 |
| `kalshi_ws/trade` | 113 |
| `kalshi_ws/market_lifecycle_v2` | 113 |
| `coinbase_ws/ticker` | 112 |
| `coinbase_ws/level2_batch` | 112 |
| `kraken_ws/book`, `bitstamp_ws/order_book`, `gemini_ws/l2` | 102 each |

That is ~87x the corpus the null was computed on.

### 2. The read path under ~38 of the 41 mechanisms silently truncated

Ticket `86bbvrx1t`. `phase1b_real_price_economics._zst_lines` — imported by 38 algo_zoo
scripts — was:

```python
raw = subprocess.run(["zstd", "-dc", path], capture_output=True).stdout.decode()
```

The exit code is discarded. On a truncated file zstd writes a PREFIX to stdout and exits
non-zero; `.stdout` hands back the prefix with no complaint.

**Direction of bias matters: truncation drops the END of a file**, so any analysis whose
events cluster in the tail (window closes, settlement) loses the part carrying signal, and
the bias runs TOWARD a null. Fixed in `598b3f46` via `run_zstd_checked()`; it now raises.

Two confirmed live instances of the class that day: a build reported DONE with 7,050 windows
vs 7,511 expected (one day streamed 6.0M of 20.5M lines), and a sports pull left a 34 MB file
where the day was ~160 MB, under its final name, decompressing cleanly to a prefix.

**Caveat, stated honestly:** it is NOT established that any specific algo_zoo run actually
read a truncated file. The scripts logged no per-file row counts, so the cheap discriminator
(compare logged counts against a checked re-read) is IMPOSSIBLE for them. That is precisely
why the re-run is the only way to settle it.

## What is NOT being claimed

- Not "the 41 mechanisms were wrong." They may well have been right.
- Not "there is an edge." Three other tracks measured effects of 1-2c, all UNDER the ~3c
  round-trip cost.
- The June power probe found the binding constraint is **information per window**, not days
  or model class. More corpus may change nothing. That is a real possibility and the re-run
  should be able to report it cleanly.

## STOP: quarantine the stale label-map caches before your first run

**Added 2026-09-07 00:1xZ. This would have silently poisoned the re-run.**

`phase1b._zst_lines` was fixed at commit `598b3f46`, **2026-09-06 23:52:48Z**. Any label
map cached BEFORE that timestamp was produced by the truncating reader and may be SHORT —
missing determined windows, i.e. under-coverage, which biases toward NULL.

Four such caches existed and have been moved to
`~/kalshi-research-data/_QUARANTINE_pre_zstd_fix/` (moved, not deleted — they are evidence):
`_tmp_genhunt_lifecycle.pkl`, `_tmp_genhunt04_determined.pkl`,
`_tmp_genhunt05_determined.pkl`, `_tmp_genhunt11_determined.pkl`. All dated 2026-06-11.

**The trap, and it is subtle** (found by the Track A session): cache fingerprints key on
`(file count, bytes, newest mtime)` of the lifecycle tree. **The TREE did not change when
the CODE did.** So a stale cache passes its own freshness check and is served silently, and
your run inherits the short label map with no signal. A byte-count check does not detect it,
for the same reason `decompressobj.eof` is the only reliable signal on the library variant:
a truncated file IS fully consumed; only the frame is incomplete.

**Before your first run:**
1. `ls ~/kalshi-research-data/_QUARANTINE_pre_zstd_fix/` — confirm nothing you need is only there.
2. Search for any other `*.pkl` cache older than 2026-09-06 23:52:48Z that a mechanism might load.
3. Fold the LOADER'S SOURCE HASH into any cache fingerprint you build, so a code fix
   invalidates the cache exactly once. Track A is doing this for its probe's
   `--determined-cache`; copy that pattern rather than inventing one.
4. Do NOT accept `/data/out/determined_97.pkl` from the probe box as a shortcut — if it
   predates the fix it carries the same defect.

## Pre-registration (write this BEFORE looking at any output)

State, in the findings doc, before the first result is read:
1. The verdict criterion — exactly what counts as an edge, net of fees, and the CI form.
2. The multiplicity plan across however many mechanisms are run.
3. The clustering unit (day vs window vs contest) and why.
4. **The MDE per cell.** A null without a stated minimum detectable effect is not a finding —
   this is the single most repeated lesson of 2026-09-06 (four of five tracks had a headline
   overturned; one "null" turned out to be a real -8.1c effect the instrument could not resolve).

## Hard-won lessons that apply directly (read before running anything)

`agent_docs/external_cli_delegation.md` — the whole file, but especially:
- **UNDERPOWERED is not NULL.** Report the MDE beside every non-result.
- **Every correction points the same way.** Track D: *"All four defects pointed the same way —
  the uncorrected procedure produced MORE and LARGER findings, and every correction removed
  them. The cells left standing are the ones that never looked interesting."* Treat a
  correction that shrinks your result as confirmation it was right.
- **Kish effective count.** Raw N lies when contracts cluster. `(Σw)²/Σw²`. Track D had a cell
  reporting 49 contests that was effectively <2, ranked second overall.
- **Mirror cells are one finding, not two.** Complementary sides of a binary market are the
  same test with the sign flipped; multiplicity correction cannot see it.
- **Silence is not evidence** — assert expected COUNTS, never exit codes.

## Environment facts

- **Corpus**: `/tmp/edge_daily` from the first run is GONE (`/tmp` wiped). Re-pull with
  `scripts/research/pull_crypto_corpus.sh` (takes `<END> <START>` args). Pull to a
  NON-`/tmp` path this time.
- **Disk**: ~55 GiB free on the Mac. A 113-day multi-source pull will NOT fit naively —
  stream-and-delete per day, or subset deliberately and say which days and why.
- **Load**: Gabe's standing rule is **at most two heavy corpus jobs concurrently**. Other
  tracks are running. Check before launching, and stagger.
- **Reader**: use `scripts/research/zstd_stream.py` — `checked_stream_lines`,
  `run_zstd_checked`, `checked_zstandard_lines`, `assert_zstd_ok`. Do NOT write a new one.
- **All in-repo readers are now guarded** as of `f750c743`. One remains: `maker_markout_scale.py`,
  which is the Track E session's and already carries its own checked reader. If you write a NEW
  reader, route it through `zstd_stream.py` — do not add a fourth variant.
- **The bot is OFF and stays off.** Nothing here turns it on.
- **Do not run the multi-agent workflows** (`scripts/research/workflows/*.js`) without Gabe
  explicitly asking — they spawn dozens of agents and cost a lot.

## Pickup prompt (paste verbatim into a fresh session)

> Re-run the algo-zoo edge hunt. Read `kb/decisions/algo-zoo-rerun-pickup-sep06.md` first —
> it has the full context, the pre-registration requirements, and the environment facts.
>
> Short version: the original 41-mechanism "zero edge" result ran on a **1.3-day corpus**
> (the finding doc withdraws its own efficiency claim on exactly that basis), and the shared
> read path under ~38 of those mechanisms was silently truncating files in a way that biases
> toward nulls. Both are now fixed: bronze has ~113 days, and the reader raises on truncation
> as of commit `598b3f46`.
>
> Your job is to determine whether the null survives an honest re-test. Start by deciding —
> and writing down — how many mechanisms and how many days you can actually run given ~55 GiB
> free disk and a two-concurrent-heavy-jobs limit. A well-powered re-run of 5 mechanisms beats
> a corpus-starved re-run of 41; the first version of this work already proved that.
>
> Pre-register the verdict criterion, multiplicity plan, clustering unit and per-cell MDE
> BEFORE looking at any output. Report the MDE beside every null. If the answer is still "no
> edge", say so with the MDE attached so we know what we could have seen.
>
> Use `scripts/research/zstd_stream.py` for every read. Do not write a new reader. Do not run
> the multi-agent workflows. The bot stays off.
