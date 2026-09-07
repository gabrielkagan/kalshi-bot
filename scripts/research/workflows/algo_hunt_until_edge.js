export const meta = {
  name: 'algo-hunt-until-edge',
  description: 'Keep inventing NEW algorithm families and backtesting each through the local bronze corpus, round after round, until one survives the adversarial gate (a real tradeable edge) or the round cap is hit',
  phases: [
    { title: 'Round 1' },
    { title: 'Round 2' },
    { title: 'Round 3' },
    { title: 'Round 4' },
    { title: 'Round 5' },
    { title: 'Round 6' },
  ],
}

const REPO = '/Users/gabrielkagan/Documents/kalshi-bot'
const WD = '/tmp/edge_daily'
const ZOO = '/Users/gabrielkagan/Documents/kalshi-bot/scripts/research/algo_zoo'
const PER_ROUND = 6
const MAX_ROUNDS = budget.total ? 99 : 6

const DATA_BRIEF = `
LOCAL BRONZE CORPUS — already pulled, do NOT re-pull from S3. ~31h window (2026-05-30T10Z -> 05-31T17Z), crypto-15M.
- FRAMES = ${WD}/frames_crypto.jsonl  (4.4GB / 9.77M rows) kalshi_ws orderbook snapshots+deltas, 626 crypto-15M tickers, all 7 assets.
    line: {_wire_recv_ts,_source,_conn,_channel,_collector_seq,_raw:{type,msg:{...}}}; type orderbook_snapshot|orderbook_delta; NOT chrono-sorted on disk (sort by ticker+seq).
- TRADES = ${WD}/trades_crypto.jsonl  (339MB / 673K rows, _channel=trade) kalshi trade prints.
- SPOT   = ${WD}/coinbase_spot.jsonl  (77MB / 694K rows) coinbase_ws ticker, products BTC/ETH/SOL/XRP only (HYPE/DOGE/BNB have NO local coinbase mid). json.loads line then json.loads(_raw) -> {type,product_id,price,best_bid,best_ask,time}. Spot starts 05-30T21:00Z (first ~11h of frames have no spot).
- VENUE L2 = ${WD}/venue_pull/<source>/day=<NN>/hour=<HH>/conn=A/*.jsonl.zst  for kraken_ws, bitstamp_ws, gemini_ws (day=30 hr00-23, day=31 hr00-17). NOW LOCAL — enables real multi-venue work.
- DB     = ${WD}/state.db  (890MB, FRESH backup) full historical settled ledger Feb->May: settled_trades (entry_price_cents,count,pnl_cents,calibrated_prob,edge,kelly_f,side,asset,product_type,seconds_to_close,settled_at) + evaluated_opportunities (ticker,market_result,threshold). IN-WINDOW (5/30-31) outcomes are sparse/just-settling -> forecasting families should derive the 15M above/below outcome from the TERMINAL BOOK (book mid at close_epoch_from_ticker), not the DB. Sizing-replay families can use the full Feb-May ledger.

REUSE THE TESTED RECONSTRUCTION (read the modules for real signatures; do NOT hand-roll book parsing):
  cd ${REPO} ; sys.path.insert(0,'${REPO}') ; then import:
    scripts.research.kalshi_book_reconstruct: reliable_nbbo_at, nbbo_at, book_at, reliable_book_at, is_reliable, simulate_maker_bid_fill, KalshiBook
    scripts.research.phase1b_real_price_economics: load_frames_jsonl, load_outcomes_db, close_epoch_from_ticker
    scripts.research.phase1b_retail_flow: parse_trade
    scripts.research.mm_markout_evaluator: first_yes_bid_fill_ts, first_no_bid_fill_ts (honest maker-cross fill primitives)
    scripts.research.venue_book_reconstruct: VENUE_SYMBOLS, load_venue_frames, mid_at, KrakenBook/BitstampBook/GeminiBook
  reliable_nbbo_at / is_reliable REFUSE drifted books (the historical bronze has heavy delta-drift) — honor the refusal; never trade off a rejected book.

NON-NEGOTIABLE RULES (this hunt has KILLED ~15 candidates already — be your own assassin):
- Kalshi fees are real and price-dependent: ceil(0.07 * C * P * (1-P)) cents/contract. Include them. State maker-rebate assumptions (default 0).
- HONEST fill model: a resting maker order fills ONLY when a real trade print crosses it (use the mm_markout_evaluator primitives). A fill you got is usually a fill you regret (adverse selection) — model it.
- Bootstrap CI (>=1000 resamples; block/cluster bootstrap when serially correlated, clustered by ticker/window which is the true independent unit). NO CI -> verdict INCONCLUSIVE. A CI that straddles zero is NOT an edge.
- NO look-ahead: only data with timestamp <= decision time. Build signal-book and label-book independently. Watch _wire_recv_ts (arrival) vs event ts.
- A statistically-real signal that is NEGATIVE net of fees+fills is NO_EDGE, not EDGE. "Tradeable net of cost" is the ONLY meaning of EDGE here.
- If required inputs are absent locally -> DATA_GAP, name exactly what's missing; never fabricate.
- ~1.3 days of data -> wide CIs, humility. Subsample tickers/hours for a first pass; SAY n_samples and what you subsampled.
- Write your script to ${ZOO}/<name>.py (durable, in-repo). Put the path in script_path.
`

const TRIED_AND_DEAD = `
ALREADY TESTED (do NOT repropose these or trivial renamings — propose genuinely DIFFERENT mechanisms):
- avellaneda_stoikov_mm: NO_EDGE, -5.3c/ct, maker adverse selection on liquid books.
- glosten_milgrom_vpin: incomplete (toxicity-gated quoting) — a NEW, better-executed informed-flow-gating variant is allowed if mechanism differs.
- order_flow_imbalance (OFI): signal real (corr 0.033) but -1.5c/signal net of cost. NO_EDGE.
- microprice_fairvalue: gross directional +4.3pp at 10s REAL but NOT tradeable (no fee/fill survival), miscalibrated, horizon-cherry-picked. REFUTED.
- confidence_scaled_gate (LCB sizing): +$275 but CI straddles zero; misses 96-97c bleed. NO_EDGE.
- cvar_risk_averse_sizing: "gain" was EV-chasing/regime leakage. NO_EDGE.
- brownian_bridge_firstpassage: ties production Student-t. NO_EDGE.
- rough_vol_jump_diffusion: worse than plain EGARCH. NO_EDGE.
- cross_venue_lead_lag: data now local but not yet measured — a real cross-venue OR cross-asset (BTC-leads-alts, testable on local coinbase spot alone) lead-lag IS still open and welcome.
DEAD IDEAS from the broader hunt (don't re-derive): crossed-book risk-free arb (reconstruction drift artifact), stale-order pickoff (frozen-ghost books, untradeable), retail-flow "sell favorites" (sample-period direction artifact), info-edge spot-out-predicts-market (stale-quote artifact; on FRESH quotes the market wins), maker-mirage in the 60-89c middle (2.6% fill, -79c on fills).

WHERE EDGE PLAUSIBLY HIDES (the hunt's own conclusions — aim here): liquid majors are efficient; competition is thin in (a) the contested 60-89c MIDDLE, (b) the 1c/99c RAILS and queue-position/rebate economics there, (c) CROSS-ASSET structure (BTC microstructure leading ETH/SOL/XRP on the SAME local coinbase spot), (d) genuine multi-venue lead-lag (venue L2 now local), (e) settlement-second RTI convergence microstructure, (f) regime/time-of-day conditioning, (g) variance-risk-premium / vol-of-vol on 15M, (h) Hawkes/self-exciting trade intensity, (i) Kyle's-lambda price-impact, (j) anything microstructural NOT in the tried/dead lists. Favor TRADEABLE-net-of-fee tests over pure forecasting micro-facts (we've shown those don't monetize).
`

const PROPOSAL_SCHEMA = {
  type: 'object', additionalProperties: false, required: ['algorithms'],
  properties: {
    algorithms: {
      type: 'array',
      items: {
        type: 'object', additionalProperties: false,
        required: ['name', 'family', 'spec', 'why_might_have_edge', 'data_it_needs'],
        properties: {
          name: { type: 'string', description: 'snake_case, unique, not in the tried list' },
          family: { type: 'string' },
          spec: { type: 'string', description: 'concrete enough to implement: the mechanism, the metric, the fill/fee treatment' },
          why_might_have_edge: { type: 'string', description: 'why this could beat an efficient market where 15 prior ideas died' },
          data_it_needs: { type: 'string', description: 'which local files; flag if anything is missing' },
        },
      },
    },
  },
}

const RESULT_SCHEMA = {
  type: 'object', additionalProperties: false,
  required: ['name', 'family', 'data_available', 'verdict', 'metric_name', 'point_estimate',
             'ci_low', 'ci_high', 'fees_included', 'fill_model', 'n_samples', 'lookahead_risks',
             'script_path', 'one_line'],
  properties: {
    name: { type: 'string' }, family: { type: 'string' },
    data_available: { type: 'boolean' },
    verdict: { type: 'string', enum: ['EDGE', 'NO_EDGE', 'DATA_GAP', 'INCONCLUSIVE'] },
    metric_name: { type: 'string' }, point_estimate: { type: 'number' },
    ci_low: { type: 'number' }, ci_high: { type: 'number' },
    fees_included: { type: 'boolean' }, fill_model: { type: 'string' },
    n_samples: { type: 'number' }, lookahead_risks: { type: 'string' },
    script_path: { type: 'string' }, one_line: { type: 'string' },
  },
}

const VERDICT_SCHEMA = {
  type: 'object', additionalProperties: false, required: ['refuted', 'reasons', 'confidence'],
  properties: {
    refuted: { type: 'boolean' }, reasons: { type: 'string' },
    confidence: { type: 'string', enum: ['low', 'medium', 'high'] },
  },
}

const buildPrompt = (a) => `You are building and backtesting ONE algorithm family against a real local bronze corpus, then reporting an honest, CI-backed verdict.

ALGORITHM: ${a.name}  (family: ${a.family})
SPEC: ${a.spec}
WHY IT MIGHT HAVE EDGE: ${a.why_might_have_edge}
DATA IT NEEDS: ${a.data_it_needs}

${DATA_BRIEF}

Implement it as a python script at ${ZOO}/${a.name}.py, run it (cd ${REPO} so scripts.research imports resolve; sys.path.insert(0,'${REPO}') in the script too), iterate until it runs clean on the REAL local data, and report the structured result. Be ruthless about look-ahead, fees, and fill optimism. EDGE requires a tradeable, fee-net, CI-excludes-zero result — anything weaker is NO_EDGE/INCONCLUSIVE. If inputs are missing -> DATA_GAP. A ~1.3-day corpus means wide CIs and humility. You MUST end by returning the structured result object.`

const verifyPrompt = (a, res) => `Adversarially REFUTE this backtested edge claim. Default refuted=true unless it clearly survives. We have been burned ~15 times by replay edges that evaporated under exactly these checks.

CLAIM: ${a.name} reports verdict=EDGE.
  metric: ${res.metric_name} = ${res.point_estimate}  CI=[${res.ci_low}, ${res.ci_high}]
  fees_included=${res.fees_included}  fill_model=${res.fill_model}  n=${res.n_samples}
  self-reported look-ahead risks: ${res.lookahead_risks}
  script: ${res.script_path}

Read the script at ${res.script_path}. Hunt for: (1) look-ahead / future leakage (data at/after decision time); (2) fees omitted/understated (ceil(0.07*C*P*(1-P))); (3) optimistic fill model (fills that wouldn't happen / no adverse selection); (4) sample too small / CI crosses zero / multiple-comparison or horizon cherry-pick; (5) survivorship or reconstruction-drift (did it honor reliable_nbbo_at / is_reliable?); (6) is the "edge" actually tradeable net of cost, or just a gross/forecasting micro-fact? Return whether the edge is refuted.`

const strategistPrompt = (triedList, round) => `You are the strategy inventor for an automated edge hunt on Kalshi 15-minute crypto above/below markets. Your job: propose ${PER_ROUND} GENUINELY NEW, concrete, implementable algorithm families to backtest this round (round ${round}). Each must test a DISTINCT mechanism not already tried, aimed at where edge plausibly hides.

${DATA_BRIEF}

${TRIED_AND_DEAD}

NAMES ALREADY USED THIS HUNT (do not reuse): ${triedList.join(', ')}

Propose exactly ${PER_ROUND} algorithm families. For each: a unique snake_case name, the family, a concrete spec (mechanism + headline metric + how fees/fills are handled so it's a TRADEABLE test, not a gross micro-fact), why it might beat an efficient market, and the local data it needs. Prefer ideas that monetize net of fees over pure forecasting accuracy. Diversify across mechanisms (don't propose 6 forecasting tweaks). Be inventive — this is round ${round}, reach for the non-obvious. Return the structured proposal.`

// -------- the hunt loop -------------------------------------------------------
const tried = [
  'avellaneda_stoikov_mm', 'glosten_milgrom_vpin', 'order_flow_imbalance',
  'microprice_fairvalue', 'confidence_scaled_gate', 'cvar_risk_averse_sizing',
  'brownian_bridge_firstpassage', 'rough_vol_jump_diffusion', 'cross_venue_lead_lag',
]
const survivors = []
const allResults = []
let roundsRun = 0

for (let r = 1; r <= MAX_ROUNDS && survivors.length === 0; r++) {
  if (budget.total && budget.remaining() < 400_000) {
    log(`Budget low (${Math.round(budget.remaining() / 1000)}k left) — stopping after ${roundsRun} rounds.`)
    break
  }
  roundsRun = r
  const ph = 'Round ' + r
  phase(ph)

  const proposal = await agent(strategistPrompt(tried, r),
    { label: `strategize:r${r}`, phase: ph, schema: PROPOSAL_SCHEMA })
  const algos = ((proposal && proposal.algorithms) || []).slice(0, PER_ROUND)
  if (!algos.length) { log(`Round ${r}: strategist returned no algorithms — stopping.`); break }
  algos.forEach((a) => tried.push(a.name))
  log(`Round ${r}: testing ${algos.map((a) => a.name).join(', ')}`)

  const results = await pipeline(
    algos,
    (a) => agent(buildPrompt(a), { label: `build:${a.name}`, phase: ph, schema: RESULT_SCHEMA }),
    (res, a) => {
      if (!res || res.verdict !== 'EDGE') return res
      return agent(verifyPrompt(a, res), { label: `verify:${a.name}`, phase: ph, schema: VERDICT_SCHEMA })
        .then((v) => ({ ...res, verified: v }))
    }
  )

  const clean = results.filter(Boolean)
  clean.forEach((c) => allResults.push({ round: r, ...c }))
  const found = clean.filter((x) => x.verdict === 'EDGE' && x.verified && !x.verified.refuted)
  const refuted = clean.filter((x) => x.verdict === 'EDGE' && x.verified && x.verified.refuted)
  survivors.push(...found)
  log(`Round ${r} done: ${clean.length} ran | ${found.length} SURVIVED gate | ${refuted.length} refuted | ${clean.filter((x) => x.verdict === 'NO_EDGE').length} no-edge | ${clean.filter((x) => x.verdict === 'DATA_GAP').length} data-gap`)
  if (found.length) log(`🟢 SURVIVOR(S) FOUND in round ${r}: ${found.map((f) => f.name).join(', ')} — halting hunt.`)
}

return {
  rounds_run: roundsRun,
  survivors: survivors.map((r) => ({ name: r.name, round: r.round, metric: r.metric_name, est: r.point_estimate, ci: [r.ci_low, r.ci_high], one_line: r.one_line })),
  total_tested: allResults.length,
  refuted: allResults.filter((r) => r.verdict === 'EDGE' && r.verified && r.verified.refuted).map((r) => ({ name: r.name, round: r.round, why: r.verified.reasons })),
  no_edge: allResults.filter((r) => r.verdict === 'NO_EDGE').map((r) => ({ name: r.name, round: r.round, one_line: r.one_line })),
  data_gaps: allResults.filter((r) => r.verdict === 'DATA_GAP').map((r) => ({ name: r.name, round: r.round, one_line: r.one_line })),
  inconclusive: allResults.filter((r) => r.verdict === 'INCONCLUSIVE').map((r) => ({ name: r.name, round: r.round, one_line: r.one_line })),
  found_edge: survivors.length > 0,
}
