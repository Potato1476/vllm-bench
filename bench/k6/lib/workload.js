// What to send, to whom, and as which agent.
//
// THE PROMPTS ARE THE REAL ONES.
//
// bench/datasets/*.jsonl holds synthetic prompts padded to an exact token count. Those
// are right for `vllm bench serve`, which measures the engine and needs a controlled
// prompt length. They are wrong here, because everything this suite exists to measure
// -- retrieval, the guardrail's ten stages, grounding, the semantic cache -- is a
// function of what the question actually says. A padded prompt retrieves nothing, cites
// nothing, and caches against nothing, so a load test built on one would report the
// latency of a pipeline with its middle switched off.
//
// So the questions come from the 144 gold queries in the retrieval eval set, and the
// attacks from the 343-sample adversarial suite the guardrail is scored against. Both
// are already versioned and checksummed by the repo.

import { SharedArray } from 'k6/data';

// Paths are relative to THIS file, and the in-cluster Job mounts the datasets at the
// same relative layout for exactly that reason -- open() resolves at init time and
// cannot read an environment variable.
const GOLD_PATH = '../../../data/xanhsm_retrieval_mock/eval/retrieval_eval.jsonl';
const ATTACK_PATH = '../../datasets/attacks-v1.jsonl';
const AGENTS_PATH = '../../agents.json';

function jsonl(text) {
  return text
    .split('\n')
    .filter((line) => line.trim().length > 0)
    .map((line) => JSON.parse(line));
}

export const gold = new SharedArray('gold', () => jsonl(open(GOLD_PATH)));

export const attacks = new SharedArray('attacks', () =>
  jsonl(open(ATTACK_PATH)).filter(
    (row) =>
      // fold B only. Fold A is the set the guardrail's rules were written against, so
      // blocking it proves nothing about generalisation; fold B was held out. Under load
      // we are asking whether saturation degrades detection, and a held-out set is the
      // only sample where a drop is attributable to load rather than to memorisation.
      row.fold === 'B' &&
      // placement 'user' only, and leaving this out made the first live run report
      // "attacks blocked 0.00%" -- a security regression that was not one.
      //
      // Half of fold B is placement 'document': INDIRECT injection, where the malicious
      // text is supposed to reach the model inside a RETRIEVED PASSAGE, not inside the
      // question. Sent as a user message it is a different attack -- often a harmless
      // sounding one -- so not refusing it is correct behaviour being scored as a failure.
      //
      // A load generator can only control the user message. Poisoning the corpus is a
      // different experiment and it already has an owner: `make defenses-eval`. Mixing
      // the two here would make the safety number unattributable to either.
      row.placement === 'user',
  ),
);

// --- agents -------------------------------------------------------------------------
// The roster is bench/agents.json, so a load test cannot drift from what the gateway is
// provisioned for. The WEIGHTS are not in that file and are placeholders, for the same
// reason the names there are: nobody has given us the real traffic mix yet. They are
// shaped by corpus size -- moc-daily is described as the bulk of the corpus -- so the
// heaviest agent is at least the one with the most material to answer from.
//
// Confirm with the mentor before quoting a per-agent latency figure as representative.
const AGENT_WEIGHTS = {
  'moc-daily': 30,
  'moc-analytics': 20,
  'moc-datadict': 15,
  'moc-kb': 15,
  'moc-playbook': 10,
  'moc-service': 5,
  'moc-governance': 5,
};

export const agents = new SharedArray('agents', () => {
  const roster = JSON.parse(open(AGENTS_PATH)).agents;
  const out = [];
  for (const agent of roster) {
    const weight = AGENT_WEIGHTS[agent.id] || 1;
    out.push({
      id: agent.id,
      weight,
      // Honour the per-agent model allow-list. A virtual key scoped to qwen2.5-1.5b that
      // is sent qwen2.5-7b gets a 400 from LiteLLM, and a run full of 400s reads as an
      // outage when it is really the load generator asking for something the key was
      // never entitled to.
      models: agent.models && agent.models.length ? agent.models : ['qwen2.5-7b'],
    });
  }
  return out;
});

// Cumulative weights, built once per VU rather than per iteration.
const cumulative = [];
let total = 0;
for (const agent of agents) {
  total += agent.weight;
  cumulative.push({ agent, upto: total });
}

export function pickAgent(rand) {
  const point = rand * total;
  for (const entry of cumulative) {
    if (point < entry.upto) return entry.agent;
  }
  return cumulative[cumulative.length - 1].agent;
}

// --- request construction -------------------------------------------------------------

// A deterministic pseudo-random stream per VU. Math.random() in k6 is seeded per VU
// anyway, but making the draw explicit means a repeat run with the same VU count sends
// the same mix, which is what makes two ramps comparable.
export function rng(seed) {
  let state = (seed * 2654435761) >>> 0;
  return function next() {
    state = (state * 1664525 + 1013904223) >>> 0;
    return state / 4294967296;
  };
}

/**
 * Choose one request.
 *
 * `cacheMix` is the fraction of benign requests that deliberately repeat a question
 * already asked in this run. It is a first-class parameter rather than something left to
 * chance because the semantic cache moves latency by more than an order of magnitude: a
 * run that happens to repeat itself measures the cache, and a run that never repeats
 * measures a system nobody deploys. Neither number is wrong; blending them is.
 *
 * Repeats are drawn from a small hot set, which is how real traffic concentrates -- a
 * handful of definitions get asked all day.
 */
export function pickRequest(next, opts) {
  const attackMix = opts.attackMix || 0;
  const cacheMix = opts.cacheMix || 0;
  const hotSize = opts.hotSize || 12;

  if (attackMix > 0 && next() < attackMix && attacks.length > 0) {
    const row = attacks[Math.floor(next() * attacks.length)];
    return {
      kind: 'attack',
      text: row.text,
      id: row.sample_id,
      family: row.family,
      mustCite: [],
    };
  }

  const repeat = cacheMix > 0 && next() < cacheMix;
  const pool = repeat ? Math.min(hotSize, gold.length) : gold.length;
  const row = gold[Math.floor(next() * pool)];
  return {
    kind: repeat ? 'repeat' : 'benign',
    text: row.query,
    id: row.query_id,
    family: row.query_type,
    mustCite: row.must_cite || [],
  };
}

export function buildBody(request, model, maxTokens) {
  return JSON.stringify({
    model,
    max_tokens: maxTokens,
    // Non-streaming on purpose. The guardrail buffers the whole answer before emitting
    // SSE (docs/metric-names.md says so), so streaming here would measure the same
    // end-to-end time while making every response harder to parse. Real TTFT is an
    // engine measurement and belongs to `vllm bench serve`, not to this suite.
    stream: false,
    messages: [{ role: 'user', content: request.text }],
  });
}
