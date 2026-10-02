// Load and stress the WHOLE platform: ingress -> LiteLLM -> guardrail -> vLLM.
//
// WHY THIS EXISTS WHEN `vllm bench serve` ALREADY DOES LOAD GENERATION
//
// bench/runner/run_bench.py says plainly that this project does not implement a load
// generator, because `vllm bench serve` already computes goodput, TTFT, TPOT and ITL
// against the engine. That is still true, and nothing here replaces it.
//
// It measures the engine. It talks to vLLM directly, so it cannot see:
//
//   - the gateway. Auth, virtual-key lookup, budget accounting and routing are on the
//     request path, and Aurora is on the request path behind them. Their cost shows up
//     in the caller's latency and in nobody's engine metric.
//   - the guardrail's ten stages. Retrieval, PII screening, injection detection,
//     prompt canonicalisation, grounding. This is most of the code in the repo and
//     `vllm bench serve` routes around all of it.
//   - the semantic cache, which changes latency by an order of magnitude and cannot
//     exist in a benchmark that sends padded synthetic prompts.
//   - whether the platform stays CORRECT under load. An engine benchmark has no notion
//     of a request that should have been refused. "Does injection detection still hold
//     at saturation" is not a question it can be asked.
//   - per-agent behaviour with seven tenants sharing one GPU.
//
// Those are the acceptance criteria. So: `vllm bench serve` sizes the engine, this sizes
// the platform, and the two are never compared to each other.
//
// SECOND JOB: THIS IS THE AVAILABILITY PROBE SOURCE.
//
// Every request emits a record in the schema bench/scripts/availability.py reads,
// tagged with the offered rate. That module's per-load breakdown and Clopper-Pearson
// intervals were written for exactly this input. It is why criteria 1 and 3 -- "p95 < 3s
// at 50 req/s" and "uptime >= 99.5%" -- are one experiment and not two.
//
// OPEN LOOP, NOT CLOSED.
//
// Every scenario uses an arrival-rate executor, never constant-vus. A closed loop offers
// less work as the server slows, so it cannot produce a queue and cannot find a knee: it
// measures the server's own pace and calls it capacity. When the client fails to keep up
// k6 records dropped_iterations, and that number is reported rather than swallowed --
// load that was never offered must not be counted as load the platform survived.
//
//   SCENARIO=smoke        12 requests, one VU. Run this before anything that costs GPU.
//   SCENARIO=ramp         the knee finder: staged arrival rates with drain between.
//   SCENARIO=steady       one rate, long enough for an availability claim.
//   SCENARIO=soak         low rate, long run: cache behaviour and drift within a session.
//   SCENARIO=agents       seven tenants at once, weighted.
//   SCENARIO=adversarial  normal load with 20% attacks: does safety survive saturation?

import http from 'k6/http';
import exec from 'k6/execution';
import { buildBody, pickAgent, pickRequest, rng } from './lib/workload.js';
import { classify, emitProbe } from './lib/verdict.js';

// --- configuration --------------------------------------------------------------------
const BASE_URL = (__ENV.BASE_URL || '').replace(/\/+$/, '');
const MASTER_KEY = __ENV.MASTER_KEY || '';
const SCENARIO = __ENV.SCENARIO || 'smoke';
const SESSION = __ENV.SESSION || `k6-${SCENARIO}`;
const MODEL_PIN = __ENV.MODEL || '';
const MAX_TOKENS = Number(__ENV.MAX_TOKENS || 192);

// 20s, against an SLO of 3s. Not generous -- deliberate. A request the platform is still
// holding at nearly seven times its latency budget has failed from the caller's side
// whatever it eventually returns, and letting VUs block on it past that point converts a
// capacity measurement into a memory problem: at 50 req/s an unbounded wait needs
// thousands of live VUs on a node that has four cores.
const TIMEOUT = __ENV.REQUEST_TIMEOUT || '20s';
const TIMEOUT_S = Number(String(TIMEOUT).replace(/[^0-9.]/g, '')) || 20;

// Per-agent virtual keys as a JSON object, supplied in the environment and never written
// to a file. Absent, every request uses the master key and carries X-Agent-Id, which is
// what actually populates the end_user label (charts/litellm/templates/configmap.yaml
// explains why the OpenAI `user` body field does not). Supplying real keys additionally
// exercises budgets and per-key model allow-lists.
const AGENT_KEYS = __ENV.AGENT_KEYS ? JSON.parse(__ENV.AGENT_KEYS) : {};

const RAMP_LEVELS = (__ENV.RAMP_LEVELS || '1,2,5,10,20,35,50')
  .split(',')
  .map((value) => Number(value.trim()))
  .filter((value) => value > 0);
const LEVEL_SECONDS = Number(__ENV.LEVEL_SECONDS || 90);

// The engine must be idle when a level starts. run_bench.py blocks on Prometheus for
// this; k6 cannot read Prometheus, so it waits instead. A level that begins while the
// previous one is still draining measures the previous level's tail, which is how a knee
// gets smeared into a gentle slope that looks like headroom.
const DRAIN_SECONDS = Number(__ENV.DRAIN_SECONDS || 45);

// A warm-up level that is MEASURED AND THEN THROWN AWAY, because the drain above only
// separates levels from each other and nothing separated the FIRST level from a cold
// engine.
//
// What that cost: the four-L4 ramp reported p95 1665ms at 10 req/s against 2190ms at
// 40 -- the lowest load looking worse than four times the load, which reads as a broken
// measurement and took a per-10s breakdown to explain. The first seconds of a run pay for
// CUDA graph capture, an empty prefix cache (82.6% hit once warm) and torch autotuning,
// and at 10 req/s those seconds are a large share of the level.
//
// It runs as its own scenario rather than as a lower first level, because every metric is
// tagged by scenario name: `served_latency{scenario:warmup}` is simply a different series,
// so no threshold, no report line and no probe aggregate can pick it up by accident. That
// is the difference between discarding data and hoping nobody averages it in.
const WARMUP_SECONDS = Number(__ENV.WARMUP_SECONDS || 60);
const WARMUP_RPS = Number(__ENV.WARMUP_RPS || 3);

// Which scenarios send X-Bypass-Cache, i.e. which ones are measuring the engine rather
// than the platform-with-cache. Overridable so the cached path can still be measured on
// purpose: BYPASS_CACHE=0 restores the old behaviour, and the cache_hit thresholds below
// then report what it costs.
const CAPACITY_SCENARIOS = ['ramp', 'slo', 'steady', 'warmup'];
const BYPASS_CACHE = __ENV.BYPASS_CACHE !== undefined && __ENV.BYPASS_CACHE !== ''
  ? __ENV.BYPASS_CACHE === '1' || __ENV.BYPASS_CACHE === 'true'
  : CAPACITY_SCENARIOS.includes(SCENARIO);

const RPS = Number(__ENV.RPS || 2);
const DURATION = __ENV.DURATION || '10m';
const ATTACK_MIX = Number(__ENV.ATTACK_MIX || 0);

// The `slo` scenario defaults differ from every other scenario, and each difference is
// there to stop the run from flattering itself:
//
//   50 req/s   the acceptance criterion, verbatim, not a level on a curve.
//   no cache   cache hits are already excluded from served_latency, so they cannot move
//              the p95 -- but they DO remove work from the GPU, so a cache mix makes the
//              platform look like it sustained an offered rate it never really served.
//              Testing the criterion means testing it against cold traffic.
//   more VUs   at 50 req/s an open loop needs rate x latency VUs in flight. The 600 that
//              is right for a ramp covers 12s of latency here and would start dropping
//              iterations -- i.e. quietly offering less than 50 req/s -- exactly when
//              the system is struggling and the number matters most.
const CACHE_MIX = Number(
  __ENV.CACHE_MIX !== undefined && __ENV.CACHE_MIX !== ''
    ? __ENV.CACHE_MIX
    : SCENARIO === 'slo'
      ? 0
      : 0.15,
);
const MAX_VUS = Number(__ENV.MAX_VUS || (SCENARIO === 'slo' ? 1500 : 600));

// --- scenario construction --------------------------------------------------------------
// rate x latency is how many VUs an open-loop executor needs in flight. Sized against the
// timeout rather than the expected latency, because the interesting part of a ramp is
// where latency is nowhere near expected.
function vus(rate) {
  const ceiling = Math.max(4, Math.ceil(rate * TIMEOUT_S));
  const capped = Math.min(ceiling, MAX_VUS);
  return { pre: Math.max(4, Math.min(Math.ceil(rate * 4), capped)), max: capped };
}

function arrival(name, rate, duration, startTime, extra) {
  const size = vus(rate);

  // k6 requires an INTEGER rate, so a fractional one is expressed by stretching timeUnit
  // instead: 0.5 req/s becomes 1 per 2s. Without this, `SCENARIO=soak` could not start at
  // all -- its default is 0.5 req/s, and k6 rejected the options with
  //
  //     json: cannot unmarshal number 0.5 into Go struct field ... rate of type int64
  //
  // pointed at a line of unrelated TLS config, so `make load-soak` has never once run.
  // Found by inspecting every scenario rather than by running the one being used today;
  // a target that cannot start is not visible until something tries to start it.
  let unitSeconds = 1;
  let perUnit = rate;
  if (!Number.isInteger(rate)) {
    unitSeconds = Math.ceil(1 / (rate - Math.floor(rate) || 1));
    perUnit = Math.round(rate * unitSeconds);
    if (perUnit < 1) { perUnit = 1; unitSeconds = Math.round(1 / rate); }
  }

  return Object.assign(
    {
      executor: 'constant-arrival-rate',
      rate: perUnit,
      timeUnit: `${unitSeconds}s`,
      duration,
      startTime,
      preAllocatedVUs: size.pre,
      maxVUs: size.max,
      // Let in-flight requests finish rather than counting them as errors at the
      // boundary. One timeout's worth is enough by construction.
      gracefulStop: TIMEOUT,
      exec: 'request',
    },
    extra || {},
  );
}

const scenarios = {};
// Scenario name -> the rate it offered, so each probe can be tagged with the load it was
// taken under. availability.py groups by exactly this field.
const OFFERED = {};
// Filled by the ramp branch, which is the only place its level names exist.
const rampCacheThresholds = {};

if (SCENARIO === 'smoke') {
  scenarios.smoke = {
    executor: 'per-vu-iterations',
    vus: 1,
    iterations: Number(__ENV.ITERATIONS || 12),
    maxDuration: '5m',
    exec: 'request',
  };
  OFFERED.smoke = 0;
} else if (SCENARIO === 'ramp') {
  let offset = 0;
  if (WARMUP_SECONDS > 0 && WARMUP_RPS > 0) {
    scenarios.warmup = arrival('warmup', WARMUP_RPS, `${WARMUP_SECONDS}s`, '0s');
    OFFERED.warmup = WARMUP_RPS;
    offset += WARMUP_SECONDS + DRAIN_SECONDS;
  }
  for (const rate of RAMP_LEVELS) {
    const name = `ramp_${String(rate).replace('.', '_')}`;
    scenarios[name] = arrival(name, rate, `${LEVEL_SECONDS}s`, `${offset}s`);
    OFFERED[name] = rate;
    // One per level, because the level names are not known until here.
    //
    // LATENCY thresholds stay off the ramp on purpose -- its upper levels are MEANT to
    // fail and a red threshold there would report a successful knee-finding run as a
    // broken one. A cache threshold is the opposite case: a ramp served from Redis has
    // not found a knee at all, it has measured Redis, and the run is invalid rather than
    // informative. `slo` had this guard and the ramp did not, which is how a ramp that
    // was 99.8% cache hits -- 38 of 22,686 requests reaching the engine -- came back with
    // a pretty curve and no complaint.
    //
    // With X-Bypass-Cache now sent for capacity scenarios this should read 0%, so the
    // threshold doubles as a check that the header is understood: an older guardrail
    // image ignores it silently and the only symptom is a number that looks too good.
    rampCacheThresholds[`cache_hit{scenario:${name}}`] = ['rate<0.25'];
    offset += LEVEL_SECONDS + DRAIN_SECONDS;
  }
} else if (SCENARIO === 'adversarial') {
  scenarios.adversarial = arrival('adversarial', RPS, DURATION, '0s');
  OFFERED.adversarial = RPS;
} else if (SCENARIO === 'soak') {
  scenarios.soak = arrival('soak', Number(__ENV.RPS || 0.5), DURATION, '0s');
  OFFERED.soak = Number(__ENV.RPS || 0.5);
} else if (SCENARIO === 'slo') {
  // The acceptance criterion as written: "p95 < 3s at 50 req/s". One rate, held, with
  // thresholds that make the run pass or fail rather than describe a curve.
  //
  // Five minutes at 50 req/s is 15,000 requests. That is far more than the 600 needed to
  // bound the failure rate below 0.5%, and the surplus is deliberate: a criterion this
  // load-bearing should not rest on the minimum sample that technically clears the bar,
  // and five minutes is long enough for a queue to build and for the KV cache to reach
  // steady state. Twelve seconds would also give 600 samples and would measure a burst.
  const rate = Number(__ENV.RPS || 50);
  // The warm-up belongs here MORE than it belongs on the ramp, and it was only on the
  // ramp. `slo` is the scenario whose exit code is quoted as the acceptance verdict, and
  // it was the one measuring a cold engine.
  //
  // Measured, same four cards and same pod placement, the only difference being whether
  // the engines had served anything yet:
  //
  //     cold   p95 18650ms   availability 80.56%   5654 of 15000 offered
  //     warm   p95  2047ms   availability 99.71%  15001 of 15000 offered
  //
  // A factor of nine, from an empty prefix cache (82.6% hit once warm) and CUDA graph
  // capture. Reporting the cold run would have failed TC1a and sent us to ask for a GPU
  // to fix a problem that did not exist.
  //
  // Its own scenario, so `served_latency{scenario:slo}` and every threshold see only the
  // measured window. Set WARMUP_SECONDS=0 to measure a cold start deliberately.
  let start = 0;
  if (WARMUP_SECONDS > 0 && WARMUP_RPS > 0) {
    scenarios.warmup = arrival('warmup', WARMUP_RPS, `${WARMUP_SECONDS}s`, '0s');
    OFFERED.warmup = WARMUP_RPS;
    start = WARMUP_SECONDS + DRAIN_SECONDS;
  }
  scenarios.slo = arrival('slo', rate, __ENV.DURATION || '5m', `${start}s`);
  OFFERED.slo = rate;
} else if (SCENARIO === 'agents') {
  scenarios.agents = arrival('agents', RPS, DURATION, '0s');
  OFFERED.agents = RPS;
} else {
  scenarios.steady = arrival('steady', RPS, DURATION, '0s');
  OFFERED.steady = RPS;
}

// Attack share: explicit per scenario rather than a single global default, because the
// two questions want opposite settings. A capacity ramp wants none -- refusals return
// before inference and would flatter the throughput number. A safety run wants a lot.
const attackMix =
  __ENV.ATTACK_MIX !== undefined && __ENV.ATTACK_MIX !== ''
    ? ATTACK_MIX
    : SCENARIO === 'adversarial'
      ? 0.2
      : SCENARIO === 'smoke'
        ? 0.25
        : 0;

export const options = {
  scenarios,
  // Trust the ingress certificate story of the lab; there is no TLS on the node port.
  insecureSkipTLSVerify: true,
  // k6's default trend stats stop at p(95) and omit the sample count. Both matter here:
  // the SLO table in docs/OVERVIEW.md is written in p90/p95, and a percentile quoted
  // without its n is not a measurement. p(99) is included because the tail is where a
  // queue first becomes visible.
  summaryTrendStats: ['min', 'med', 'p(90)', 'p(95)', 'p(99)', 'max', 'count'],
  thresholds: {
    // Safety is the one threshold that aborts. Every other failure mode here is a number
    // worth finishing the run to measure; an injection getting through at load is a
    // finding that makes the rest of the run irrelevant, and continuing to hammer a
    // pipeline that has stopped refusing things just spends GPU proving it again.
    attack_blocked: [
      { threshold: 'rate>=0.95', abortOnFail: true, delayAbortEval: '60s' },
    ],
    // Scoped to `steady`, and absent from `ramp` on purpose. The ramp is EXPECTED to
    // fail at its upper levels -- that is what it is for -- and a red threshold there
    // would report a successful knee-finding run as a broken one.
    // The brief, verbatim, as pass/fail. `slo` is the only scenario whose thresholds are
    // the acceptance criterion rather than a sanity check, so it is the one whose exit
    // code can be quoted.
    'served_latency{scenario:slo}': ['p(95)<3000'],
    'availability{scenario:slo}': ['rate>=0.995'],
    // Load that was never offered must not count as load the platform survived. At
    // 50 req/s this is the threshold most likely to fire, and it fires on the load
    // generator, not on the platform -- re-run with more MAX_VUS or more client CPU.
    'dropped_iterations{scenario:slo}': ['count<1'],
    // The backstop for a run that never reached the GPU. With a 144-question corpus and a
    // warm semantic cache the whole ramp can be served from Redis: guardrail at 3.2 req/s,
    // vLLM at 0.0, every engine panel correctly reading zero, and a latency figure two
    // orders of magnitude too good. Nothing in the run looks wrong at the time.
    //
    // A capacity scenario that is mostly cache hits has measured the cache. Fail it.
    'cache_hit{scenario:slo}': ['rate<0.25'],
    'availability{scenario:steady}': ['rate>=0.995'],
    'served_latency{scenario:steady}': ['p(95)<3000'],
    'answer_grounded{scenario:steady}': ['rate>=0.90'],
    // A dropped iteration in the availability run means the offered rate was never
    // offered, so the denominator is wrong and the result cannot be quoted. Re-run with
    // a larger MAX_VUS or from a machine that can keep up.
    'dropped_iterations{scenario:steady}': ['count<1'],
    'cache_hit{scenario:steady}': ['rate<0.25'],
    ...rampCacheThresholds,
  },
};

// --- the request ------------------------------------------------------------------------
let next = null;

export function request() {
  if (next === null) next = rng(exec.vu.idInTest || 1);

  const scenarioName = exec.scenario.name;
  const rps = OFFERED[scenarioName] || 0;

  // Every scenario draws from the weighted roster, so end_user is always populated and
  // the per-agent dashboard has data from any run. `agents` differs only in that it is
  // the run whose point IS the mix; the others get attribution for free.
  const agent = pickAgent(next());
  const model = MODEL_PIN || agent.models[Math.floor(next() * agent.models.length)];
  const draw = pickRequest(next, { attackMix, cacheMix: CACHE_MIX });

  const key = AGENT_KEYS[agent.id] || MASTER_KEY;
  const response = http.post(`${BASE_URL}/v1/chat/completions`, buildBody(draw, model, MAX_TOKENS), {
    headers: {
      'Content-Type': 'application/json',
      Authorization: `Bearer ${key}`,
      // The header, not the body field. See the ConfigMap comment.
      'X-Agent-Id': agent.id,
      // Capacity scenarios answer "what can the ENGINE do", so they must not be answered
      // from Redis. Clearing the cache beforehand does not achieve that: the corpus is
      // 36 base questions with 4 paraphrases, the semantic threshold is 0.96, and the
      // paraphrases match each other -- so a cleared cache refills from the corpus within
      // a couple of hundred requests. Measured: 15,000 requests at 50 req/s reached the
      // engine 191 times.
      //
      // `soak` is deliberately absent. Cache behaviour over a long session is what that
      // scenario is for, and bypassing the cache there would remove its subject.
      ...(BYPASS_CACHE ? { 'X-Bypass-Cache': '1' } : {}),
    },
    timeout: TIMEOUT,
    tags: { rps: String(rps), kind: draw.kind },
    // Refusals are 4xx and are expected; letting k6 mark them as unexpected responses
    // would poison http_req_failed, which the summary reads.
    responseCallback: http.expectedStatuses({ min: 200, max: 499 }),
  });

  const tags = { rps: String(rps), kind: draw.kind, agent: agent.id };
  const result = classify(draw, response, tags);
  emitProbe(result, response, {
    target: BASE_URL,
    rps,
    session: SESSION,
    kind: draw.kind,
    agent: agent.id,
    model,
  });
}

// --- summary ------------------------------------------------------------------------
// k6's default summary is replaced rather than extended, because most of it is about
// HTTP and the questions this project has to answer are not. The full metric set still
// goes to the JSON file; what is printed is the acceptance criteria.
// A Rate with no observations reports {rate: 0, passes: 0, fails: 0}, so printing
// `values.rate` directly renders "0.00%" for a metric nothing was ever measured against.
// On attack_blocked that is the most alarming number this report can produce, and a
// capacity ramp -- which deliberately sends no attacks -- produces it every time. Read
// passes+fails and say n/a, because "no attacks were sent" and "no attacks were blocked"
// must never look the same.
function pct(metric) {
  if (!metric || !metric.values) return '     n/a';
  const n = (metric.values.passes || 0) + (metric.values.fails || 0);
  if (n === 0) return '     n/a';
  return `${(metric.values.rate * 100).toFixed(2)}% (n=${n})`;
}

function ms(trend, stat) {
  if (!trend || trend.values === undefined) return 'n/a';
  const value = trend.values[stat];
  return value === undefined ? 'n/a' : `${Math.round(value)}ms`;
}

export function handleSummary(data) {
  const m = data.metrics;
  const lines = [];
  const add = (line) => lines.push(line);

  add('');
  add(`  === ${SCENARIO} (${SESSION}) ===`);
  add('');
  add('  Latency is reported per class. A cache hit skips the GPU and a refusal skips');
  add('  inference, so one blended percentile would be a function of the test config.');
  add('');
  add(`    served   p50 ${ms(m.served_latency, 'med')}  p90 ${ms(m.served_latency, 'p(90)')}  ` +
      `p95 ${ms(m.served_latency, 'p(95)')}  p99 ${ms(m.served_latency, 'p(99)')}  ` +
      `n=${m.served_latency ? m.served_latency.values.count : 0}`);
  add(`    cached   p50 ${ms(m.cached_latency, 'med')}  p95 ${ms(m.cached_latency, 'p(95)')}  ` +
      `n=${m.cached_latency ? m.cached_latency.values.count : 0}`);
  add(`    refused  p50 ${ms(m.refused_latency, 'med')}  p95 ${ms(m.refused_latency, 'p(95)')}  ` +
      `n=${m.refused_latency ? m.refused_latency.values.count : 0}`);
  add(`    guardrail overhead p95 ${ms(m.guardrail_seconds, 'p(95)')}`);
  add('');

  // Throughput and answer length, side by side, because one is the other divided by the
  // third: req/s = tok/s / tokens-per-answer. Printing them apart invites quoting a
  // req/s figure without saying what length of answer produced it, and at these answer
  // lengths that ratio moves the number by nearly 2x.
  const outTok = m.output_tokens ? m.output_tokens.values : null;
  if (outTok && outTok.count > 0) {
    const served = m.served_latency ? m.served_latency.values.count : 0;
    add(`    throughput     ${outTok.rate.toFixed(1)} token/s ra` +
        `  (tong ${outTok.count}, ${served} request sinh moi)`);
    add(`    do dai tra loi  p50 ${Math.round(m.answer_tokens.values.med)}` +
        `  p90 ${Math.round(m.answer_tokens.values['p(90)'])}` +
        `  p95 ${Math.round(m.answer_tokens.values['p(95)'])}` +
        `  max ${Math.round(m.answer_tokens.values.max)} token`);
    add('      (gia dinh SLO dung 144 dap an vang: p50 32 / p90 44 / max 54.');
    add('       Lech nhieu o day thi du doan latency trong OVERVIEW.md khong con dung.)');
    if (m.input_tokens && m.input_tokens.values.count > 0) {
      add(`    prompt vao     ${Math.round(m.input_tokens.values.count / Math.max(served, 1))}` +
          ` token/request trung binh`);
    }
    add('');
  }
  add(`    availability    ${pct(m.availability)}`);
  add(`    attacks blocked ${pct(m.attack_blocked)}`);
  add(`    answers cited   ${pct(m.answer_grounded)}`);
  add(`    cache hit       ${pct(m.cache_hit)}`);
  add('');

  const dropped = m.dropped_iterations ? m.dropped_iterations.values.count : 0;
  const started = m.iterations ? m.iterations.values.count : 0;
  add(`    requests sent  ${started}`);
  if (dropped > 0) {
    add(`    DROPPED        ${dropped} -- the client could not offer the configured rate.`);
    add('      Every level where this is non-zero measured LESS load than it claims.');
    add('      Raise MAX_VUS, or run the generator somewhere with more CPU.');
  }
  for (const [name, metric] of [
    ['false refusals', m.false_refusals],
    ['attacks answered', m.attacks_answered],
    ['empty 200s', m.empty_answers],
    ['throttled (429)', m.throttled_requests],
  ]) {
    const count = metric ? metric.values.count : 0;
    if (count > 0) add(`    ${name}: ${count}`);
  }
  add('');
  add('  Per-load breakdown and the confidence interval come from the probe file:');
  add('    make availability PROBES=<probe file>');
  add('');

  const out = { stdout: lines.join('\n') };
  if (__ENV.SUMMARY_JSON) out[__ENV.SUMMARY_JSON] = JSON.stringify(data, null, 2);
  return out;
}
