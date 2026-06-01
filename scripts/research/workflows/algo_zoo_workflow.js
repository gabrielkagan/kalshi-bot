export const meta = {
  name: 'algo-zoo-bronze-backtest',
  description: 'Build N untried algorithm families and backtest each through the 5/30+ bronze corpus; adversarially verify every positive edge before believing it',
  phases: [
    { title: 'Corpus', detail: 'verify pull complete + characterize local data + pull coinbase spot for the window' },
    { title: 'Build+Backtest', detail: 'one agent per algorithm family: implement + run against bronze, fees + fill-model + bootstrap CI' },
    { title: 'Verify', detail: 'adversarial skeptic on every EDGE verdict — refute look-ahead / fee / fill-optimism / sample-size' },
    { title: 'Synthesize', detail: 'rank survivors, write report' },
  ],
}

const REPO = '/Users/gabrielkagan/Documents/kalshi-bot'
const WD = '/tmp/edge_daily'
// ZOO = where each agent writes its algorithm script. DURABLE repo path (NOT /tmp)
// so generated code survives a reboot — /tmp is wiped on macOS restart and we lost
// the first run's scripts that way (2026-05-31 reboot). WD stays in /tmp (large,
// re-pullable bronze corpus); CODE must persist.
const ZOO = '/Users/gabrielkagan/Documents/kalshi-bot/scripts/research/algo_zoo'

const RESULT_SCHEMA = {
  type: 'object',
  additionalProperties: false,
  required: ['name', 'family', 'data_available', 'verdict', 'metric_name', 'point_estimate',
             'ci_low', 'ci_high', 'fees_included', 'fill_model', 'n_samples', 'lookahead_risks',
             'script_path', 'one_line'],
  properties: {
    name: { type: 'string' },
    family: { type: 'string' },
    data_available: { type: 'boolean', description: 'were the required inputs present locally' },
    verdict: { type: 'string', enum: ['EDGE', 'NO_EDGE', 'DATA_GAP', 'INCONCLUSIVE'] },
    metric_name: { type: 'string', description: 'e.g. settlement-markout-net-cents, brier-vs-baseline, counterfactual-pnl-usd' },
    point_estimate: { type: 'number' },
    ci_low: { type: 'number' },
    ci_high: { type: 'number' },
    fees_included: { type: 'boolean' },
    fill_model: { type: 'string', description: 'how fills were simulated, or N/A for forecasting-only' },
    n_samples: { type: 'number' },
    lookahead_risks: { type: 'string', description: 'honest self-assessment of look-ahead / leakage in this backtest' },
    script_path: { type: 'string' },
    one_line: { type: 'string' },
  },
}

const VERDICT_SCHEMA = {
  type: 'object',
  additionalProperties: false,
  required: ['refuted', 'reasons', 'confidence'],
  properties: {
    refuted: { type: 'boolean', description: 'true if the edge claim does NOT survive scrutiny' },
    reasons: { type: 'string' },
    confidence: { type: 'string', enum: ['low', 'medium', 'high'] },
  },
}

const DATA_BRIEF = `
LOCAL BRONZE CORPUS (already pulled, do NOT re-pull from S3):
- FRAMES = ${WD}/frames_crypto.jsonl  — kalshi_ws orderbook snapshots+deltas, crypto-15M only, 5/30 21:06Z onward.
    Each line: {_wire_recv_ts, _source, _conn, _channel, _collector_seq, _raw:{type, msg:{...}}}.
    type is "orderbook_snapshot" | "orderbook_delta"; msg carries yes/no book in cents/dollars_fp.
- TRADES = ${WD}/trades_crypto.jsonl  — kalshi_ws trade prints (ts, yes_price, taker_side).
- DB     = ${WD}/state.db  — outcomes: evaluated_opportunities (ticker -> market_result, threshold) and
    settled_trades (entry_price_cents, count, pnl_cents, calibrated_prob, edge, kelly_f, side, asset, product_type, seconds_to_close, settled_at).
- SPOT   = ${WD}/coinbase_spot.jsonl  — coinbase ticker mid for the window IF Phase 0 pulled it; may be ABSENT.

REUSE THE EXISTING, TESTED RECONSTRUCTION — do not hand-roll book parsing:
  cd ${REPO} and import:
    from scripts.research.kalshi_book_reconstruct import reliable_nbbo_at, nbbo_at, book_at, simulate_maker_bid_fill, KalshiBook
    from scripts.research.phase1b_retail_flow import parse_trade
    from scripts.research.phase1b_real_price_economics import load_frames_jsonl, load_outcomes_db, close_epoch_from_ticker
  reliable_nbbo_at REFUSES drifted books — honor it; never trade off a book it rejects.

RULES:
- Kalshi fees are real and price-dependent — include them. Maker rebates if any: state the assumption.
- Use an HONEST fill model (a resting maker order fills only if a real trade print crosses it).
- Compute a bootstrap CI (>=1000 resamples) on your headline metric. No CI -> verdict INCONCLUSIVE.
- If your required inputs are not present locally -> verdict DATA_GAP, say exactly what's missing, do NOT fabricate.
- Subsample tickers for a first-pass estimate if the full corpus is too big; SAY so in n_samples/one_line.
- Write your script to ${ZOO}/<name>.py so it's inspectable. Put the script path in script_path.
- Be your own skeptic in lookahead_risks. This corpus is ~1 day; tiny-sample humility is mandatory.
`

const ALGOS = [
  { name: 'avellaneda_stoikov_mm', family: 'market-making',
    spec: `Avellaneda-Stoikov optimal market making on the reconstructed Kalshi binary book. Compute a reservation price r = mid - q*gamma*sigma^2*(T-t) (q=inventory, gamma=risk aversion) and an optimal half-spread; post two-sided quotes that skew with inventory and time-to-expiry. Simulate fills against real trade prints, mark to settlement. Headline metric: per-contract settlement-markout net of fees, with CI. This implements "figure out fair value, quote a spread around it, skew against inventory/adverse-selection".` },
  { name: 'glosten_milgrom_vpin', family: 'market-making',
    spec: `Glosten-Milgrom adverse-selection-aware quoting + VPIN (volume-synchronized prob of informed trading). Estimate order-flow toxicity from the trade tape; widen/skew quotes when flow looks informed. Compare PnL of toxicity-aware quoting vs naive constant-spread quoting on the same books. Headline: net markout uplift (cents/contract) from toxicity gating, with CI. This is "harvest from regulars, don't give it back to quants".` },
  { name: 'order_flow_imbalance', family: 'signal',
    spec: `Order-Flow Imbalance (OFI) short-horizon predictor. From the book deltas compute OFI = signed changes in best bid/ask size; regress next 5-30s mid move on OFI. Then a simple tradeable rule (taker when |predicted move| > fee). Headline: out-of-sample net PnL per signal, or correlation + a tradeability check, with CI.` },
  { name: 'microprice_fairvalue', family: 'fair-value',
    spec: `Stoikov microprice as a fair-value estimator: microprice = weighted mid by opposite-side size. Test whether microprice predicts the next mid better than the simple mid (it should, microstructurally) and whether the gap (microprice - market mid) is a tradeable signal net of fees. Headline: Brier or directional-hit improvement of microprice over mid, with CI.` },
  { name: 'confidence_scaled_gate', family: 'sizing-gate',
    spec: `Alex's confidence rule, formalized. Using state.db settled_trades (calibrated_prob, edge, entry_price_cents, side, pnl_cents, count): replay the trades under a gate that only takes a trade when the LOWER confidence bound on true prob beats price: q_hat - z*sigma_q > p (use z=1.65). Estimate sigma_q per price-tier from realized calibration error (binomial SE within tier). Headline: counterfactual total PnL (USD) of gate-survivors vs actual, with CI from bootstrap over trades. Report which price tiers (esp 90-94c and 97-98c) get filtered.` },
  { name: 'cvar_risk_averse_sizing', family: 'sizing-gate',
    spec: `CVaR / risk-averse re-sizing. Using state.db settled_trades, re-size each historical trade to minimize 95% CVaR (expected shortfall) of the portfolio rather than maximize Kelly growth, given the realized outcome distribution per tier. Compare counterfactual PnL AND tail loss (worst 5%) vs the actual half-Kelly book. Headline: change in 95% CVaR and in total PnL, with CI. Directly tests "a few big losses outweigh many small gains".` },
  { name: 'brownian_bridge_firstpassage', family: 'forecasting',
    spec: `First-passage / Brownian-bridge terminal probability for the 15M above/below. Conditioning on current spot (SPOT file) and the strike, compute P(spot_T above strike) via a Brownian bridge / first-passage formulation instead of a terminal Student-t CDF. If SPOT is absent, attempt a book-implied-mid martingale variant; if neither is possible, DATA_GAP. Headline: Brier vs the production Student-t baseline on real outcomes (evaluated_opportunities), with CI.` },
  { name: 'rough_vol_jump_diffusion', family: 'forecasting',
    spec: `Rough-volatility (rough Bergomi flavor) and/or Merton jump-diffusion terminal probability for short-dated 15M markets. Needs SPOT returns. Estimate roughness/jump params on the window, produce terminal P(above strike), score Brier vs an EGARCH/Student-t baseline on real outcomes. If SPOT absent -> DATA_GAP with the exact requirement.` },
  { name: 'cross_venue_lead_lag', family: 'signal',
    spec: `Cross-venue lead-lag: does one venue (Coinbase) lead others (Kraken/Bitstamp/Gemini) by 100s of ms, giving a predictive signal usable to fade stale Kalshi quotes? Requires venue L2 bronze (kraken_ws/bitstamp_ws/gemini_ws) — check ${WD} and scripts.research.venue_book_reconstruct. If venue data not local -> DATA_GAP stating exactly which channels + the pull command, and characterize what the test WOULD measure.` },
]

phase('Corpus')
const corpus = await agent(
  `You are the corpus-readiness gate for an algorithm-backtesting fan-out.
${DATA_BRIEF}

DO THESE STEPS:
1) Confirm the bronze pull is finished: poll until the process pulling into ${WD} is gone AND ${WD}/frames_crypto.jsonl mtime is stable for ~15s (or a fresh ${WD}/report_*.txt exists newer than the frames file). Use: ps + ls -la in a short shell loop. Do NOT wait more than ~6 minutes; if it never settles, proceed and note it.
2) Characterize the corpus: row counts of frames_crypto.jsonl and trades_crypto.jsonl, the UTC time span (min/max _wire_recv_ts), distinct crypto tickers, and which _channel values appear. From state.db: count evaluated_opportunities and settled_trades rows in the 5/30 21:06Z -> now window.
3) Spot data: check if ${WD}/coinbase_spot.jsonl exists. If NOT, ATTEMPT a bounded pull of coinbase ticker bronze for the same window so forecasting agents have spot. Try:
     rclone lsf kalshi-restore:kalshi-bot-archive/bronze/coinbase_ws/ticker/ --max-depth 4 | tail
   then copy only 2026-05-30 (hour>=21) + 2026-05-31 partitions into ${WD}/cb_pull, zstd -dc, grep BTC/ETH/SOL/XRP product_ids, write to ${WD}/coinbase_spot.jsonl. Cap total at a few hundred MB; if it's too large or rclone path differs, SKIP and report spot as UNAVAILABLE with the exact path you tried.
4) Check venue L2 presence for the lead-lag agent: rclone lsf kalshi-restore:kalshi-bot-archive/bronze/ --dirs-only — report whether kraken_ws/bitstamp_ws/gemini_ws exist.

Return a concise plain-text readiness brief: corpus stats, spot AVAILABLE/UNAVAILABLE (+path), venue-L2 AVAILABLE/UNAVAILABLE, and any caveat the build agents must know. This text is passed verbatim to every downstream agent.`,
  { phase: 'Corpus', label: 'corpus-ready' }
)

log('Corpus ready — fanning out ' + ALGOS.length + ' algorithm families')

const results = await pipeline(
  ALGOS,
  (a) => agent(
    `You are building and backtesting ONE algorithm family against a real bronze corpus, then reporting an honest, CI-backed verdict.

ALGORITHM: ${a.name}  (family: ${a.family})
SPEC: ${a.spec}

${DATA_BRIEF}

CORPUS READINESS BRIEF (authoritative — trust this over your assumptions):
${corpus}

Implement it as a python script at ${ZOO}/${a.name}.py, run it (cd ${REPO} so the scripts.research imports resolve; the script should sys.path.insert(0,'${REPO}') itself too), iterate until it runs clean, and report the structured result. Be ruthless about look-ahead and fees. If inputs are missing, DATA_GAP — never fabricate numbers. A one-day corpus means wide CIs and humility.`,
    { phase: 'Build+Backtest', label: `build:${a.name}`, schema: RESULT_SCHEMA }
  ),
  (res, a) => {
    if (!res || res.verdict !== 'EDGE') return res
    return agent(
      `Adversarially REFUTE this backtested edge claim. Default to refuted=true unless it clearly survives.

CLAIM: ${a.name} reports verdict=EDGE.
  metric: ${res.metric_name} = ${res.point_estimate}  CI=[${res.ci_low}, ${res.ci_high}]
  fees_included=${res.fees_included}  fill_model=${res.fill_model}  n=${res.n_samples}
  self-reported look-ahead risks: ${res.lookahead_risks}
  script: ${res.script_path}

Read the script at ${res.script_path}. Hunt for: (1) look-ahead / future leakage (using data at or after the decision time), (2) fees omitted or understated, (3) optimistic fill model (assuming fills that wouldn't happen), (4) sample too small / CI crosses zero / multiple-comparisons, (5) survivorship or reconstruction-drift (did it honor reliable_nbbo_at?). We have been burned 4 times by replay edges that evaporated under exactly these. Return whether the edge is refuted.`,
      { phase: 'Verify', label: `verify:${a.name}`, schema: VERDICT_SCHEMA }
    ).then((v) => ({ ...res, verified: v }))
  }
)

phase('Synthesize')
const clean = results.filter(Boolean)
const survivors = clean.filter((r) => r.verdict === 'EDGE' && r.verified && !r.verified.refuted)
const refuted = clean.filter((r) => r.verdict === 'EDGE' && r.verified && r.verified.refuted)
const gaps = clean.filter((r) => r.verdict === 'DATA_GAP')

return {
  ran: clean.length,
  survivors: survivors.map((r) => ({ name: r.name, metric: r.metric_name, est: r.point_estimate, ci: [r.ci_low, r.ci_high], one_line: r.one_line })),
  refuted: refuted.map((r) => ({ name: r.name, why: r.verified.reasons })),
  no_edge: clean.filter((r) => r.verdict === 'NO_EDGE').map((r) => ({ name: r.name, one_line: r.one_line })),
  data_gaps: gaps.map((r) => ({ name: r.name, one_line: r.one_line })),
  inconclusive: clean.filter((r) => r.verdict === 'INCONCLUSIVE').map((r) => ({ name: r.name, one_line: r.one_line })),
  all: clean,
}
