# Monitor session handoff — 2026-09-07 00:25Z

Written by the outgoing master-monitor session. The pickup prompt is at the bottom.

## Your job

You are the **master monitor** for a fleet of parallel Claude sessions doing edge research
on the Kalshi bot. You do not run the tracks; you shepherd them. Concretely:

- Sweep every session periodically. Ask for: current phase, ETA, blocked-on. Three lines.
- **Route findings between tracks.** This is the highest-value thing you do — tracks cannot
  see each other, and several results tonight only became meaningful when cross-referenced.
- **File tickets for sessions whose ClickUp MCP is down** (several are; yours may work).
- Surface decisions to Gabe. Never merge, push, deploy, or terminate without his word.
- Keep `agent_docs/external_cli_delegation.md` current — it is the durable lessons file.

## Standing rules from Gabe

- **The bot is OFF and stays off** until there is a real edge. `GLOBAL_LIVE_TRADING=False`,
  last settled trade 2026-06-18. Nothing any track produces turns it on.
- **At most two heavy corpus jobs concurrently.** Stagger; do not kill in-flight work.
- **No push/merge/deploy without explicit approval.** He approved #180 and #181 specifically.
- **Open a PR only after the 2-consecutive-zero adversarial gate clears** — PR creation and
  every push to an open PR fires paid CI.
- Grok may author code and open PRs (it wrote #182). The review gate is NOT waived by that;
  it is a property of the change, not the author.

## Live state at handoff

**Bot**: `active`, VPS on `f1c4a14`, not trading.

**EC2**: `kalshi-research-trackb` (c7g.4xlarge) RUNNING — Track B's smoke day.
`kalshi-research-probe` STOPPED by me (on-demand, so reversible; EBS + 74 GB corpus intact;
restart when Track A's gate v2 clears). The sports filter box finished 30/30 and is gone.

**Mac**: 49 GB free (90%), load ~10. iCloud move done except the main repo — see Open items.

**Open PRs**: #183 (Kalshi create-order V2, fixes a 410 on the deprecated v1 endpoint —
matters whenever the bot trades again), #77 (old spike, SHIP-WITH-CAVEATS).

**Branch `algo-zoo-edge-hunt`**: 12 unpushed commits, all mine, all zstd/doc work.

## Track status

| Track | Session | State |
|---|---|---|
| A — 97-day probe | `kalshi-bot-ff` | R45/R46; "probe gate clear v2" pending. Box stopped awaiting it. |
| B — 9 mechanisms | `kalshi-bot-fc` | pipeline v12; 97-day batch blocked on ITS OWN preflight pending venue resync |
| C — retail behavior | `kalshi-bot-7f` | R19; **8 consecutive rounds with zero numeric errors** |
| D — universe map | `kalshi-bot-24` | queue sims running |
| E — maker markout | `kalshi-bot-04` | result final, gate 0/2, running own rounds |
| H — college football | `kalshi-bot-7b` | R5; **verdict went to ZERO cells** |
| algo-zoo re-run | new session | just started; pre-registration first |
| fleet checker | `kalshi-bot-9d` | not a track |

## What is actually known about the money printer

**No tradeable edge. Every measured effect is smaller than the ~3c round-trip cost.**

The one WELL-RESOLVED number of the day (Track E): **quoting both sides LOSES** — touch 5s
at −1.37c/contract, **8.6x its own noise floor**, monotonically worse at longer lifetimes.
That kills the "quote, don't take" cost layer. Its transferable form: on this venue measure
the SPREAD of two legs, never the legs — the joint bootstrap is far tighter than either side,
because quoting both cancels the tape.

**RETRACTED 2026-09-07 00:38Z by Track H itself — do not cite this.** The claim as written
was: "Track H independently reached the same shape: every cell with a large settlement markout
marks flat-to-negative five minutes later; the near-certainty edge at 85-100c is a
settlement-horizon artifact a maker never keeps." Track H's own R5 reviewer falsified it on
their artifact. The counterexample mixed two fill models, and there are cells with settlement
+16.97c whose forward mark is +5.05c with a game-clustered lower bound ABOVE zero. It may also
be a category error: settlement markout IS the P&L for a hold-to-settlement maker, so a
five-minute mark-to-market on a subsample cannot show the settlement number is unrealisable.
**Track E therefore has NO corroboration from Track H**; the -1.37c result stands on its own
instrument. What survives is a question to Track E, not a finding from Track H: does its
forward-mark instrument match its holding period?

**Favourite-longshot bias is real in DIRECTION but not established as a replication.**
Track D: 53 of 73 band curves slope the FLB way against 36.5 expected, sign test p = 0.0001 —
but NOT ONE survives BH correction. Real, everywhere, too small to establish anywhere.
Track C's +3.4c and Track D's +0.83/+0.96 on the SAME family are a 4x gap = a discrepancy
needing band-edge/horizon/weighting reconciliation, NOT agreement. **I over-claimed this
earlier; do not re-inflate it.** FLB also REVERSES in sports intraday, so it is not universal.

**Two live candidates nobody has killed**: Track E's DOGE 90-99 YES 1c-inside 5s at +4.01c
[+2.22,+5.67] (declining to claim absence, carried to 97 days), and the **post-only reactive
maker** variant — quote only after a print, never sitting in the pre-sweep queue. That is the
one maker configuration untested and the only one whose mechanism survives the queue result.

## Open items

1. **Main repo out of iCloud.** 47 of 50 dirs moved; the main repo remains and is STILL
   generating corruption (a fleet checker caught iCloud mid-act creating a 36-character
   filename inside `.git`). 78 processes have cwd there, so it needs a quiet fleet. Gabe has
   approved the move. Two dirs with literal `" 2"` in their names also remain.
2. **12 unpushed commits** on `algo-zoo-edge-hunt`. Gabe has not been asked to push them.
3. **Track B's 97-day batch** needs the venue resync (its symlink layer, not the data —
   kraken's 32 linked days are exactly the 32 STANDARD-tier days; restored Deep Archive days
   never entered the layer the resolver reads).
4. **2 zstd readers** still unguarded, both owned by live sessions and notified.
5. Tickets filed tonight: `86bbvrx1t` (zstd, 3 variants), `86bbvu94g` (state.db restore),
   `86bbvucck` (book reconstruction), `86bbvt12r` (fifa, SHIPPED), `86bbvt307`,
   `86bbvrunn`, `86bbvrv7a`, `86bbvrv86` (closed), `86bbvrv96`.

## Read these before doing anything

- `agent_docs/external_cli_delegation.md` — the durable lessons file. Long, worth it.
- `kb/decisions/algo-zoo-rerun-pickup-sep06.md` — the re-run's context and stale-cache trap.
- `kb/decisions/session-resume-sep05-model-first-97-day-corpus.md` — program doc + scoreboard.

## Lessons the fleet earned today (apply these, do not re-learn them)

- **UNDERPOWERED is not NULL.** No track may publish "no effect" without the minimum
  detectable effect beside it. One "null" tonight was a real −8.1c effect the instrument
  could not resolve.
- **The asymmetry test** (Track E): *for any standard you adopt, find an instance where
  applying it would have made your result look BETTER. If there are none, you are not
  applying a standard, you are selecting evidence.* Every act of scepticism feels like rigour,
  which is why one-directional rigour is invisible from inside.
- **You cannot spend an argument twice in opposite directions.** If you argued a bar cannot
  detect a tradeable effect at this n, you may not cite that bar as evidence of absence.
- **Every correction points the same way** (Track D): the uncorrected procedure produces MORE
  and LARGER findings. Treat a correction that SHRINKS your result as confirmation it was
  right; distrust a finding that survived every fix unchanged.
- **"A comparison that returns a confident answer to a question you did not ask"** (Track B) —
  eight instances today: `day=` matching day-of-MONTH, object-count vs byte parity, a
  convenience sample quoted as a population, raw vs Kish effective N, mirror cells counted
  twice, a guard reading the wrong unit, an unevaluable clause skipped rather than reported
  UNRESOLVED, and a "game-clustered" test that defaulted one clause to day clustering.
- **Relay the artifact PATH, never a summary.** I broke this twice today and both times
  another session caught it.
- **Silence is not evidence.** Assert expected COUNTS, never exit codes or absence of error.
- **`py_compile` does not resolve imports.** A commit ADDING a module needs
  `git archive HEAD | tar -x` then an actual import. I shipped 12 broken files by skipping this.

---

## PICKUP PROMPT (paste verbatim into a fresh session)

> You are the master monitor for the Kalshi edge-research fleet. Read
> `kb/decisions/monitor-handoff-sep07.md` first — it has the full state, the track roster,
> the standing rules from Gabe, and the lessons the fleet earned today.
>
> Start by running `ListAgents` and sweeping every `kalshi-bot-*` session for: current phase,
> ETA, blocked-on — three lines each, no narrative. Then report to Gabe: what changed, what
> needs a decision from him, and honest ETAs.
>
> Your highest-value function is routing findings between tracks — they cannot see each other,
> and several results only became meaningful when cross-referenced. Your second is filing
> tickets for sessions whose ClickUp MCP is down.
>
> Standing rules: the bot is OFF and stays off; at most two heavy corpus jobs at once; never
> merge, push, deploy or terminate without Gabe's explicit word; a PR opens only after its
> 2-consecutive-zero gate clears because CI costs money.
>
> Two things you should verify yourself rather than take from this doc: that the bot is still
> not trading, and which EC2 boxes are billing. Both were true at 00:25Z and both can change.
