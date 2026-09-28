// What counts as a failure -- and the three different questions that word hides.
//
// A load test against a guarded pipeline cannot use "HTTP 2xx" as its success criterion,
// because the pipeline's correct behaviour includes refusing things. The guardrail
// answers an injection attempt with 422. Counted naively, a run with 20% attack traffic
// reports 80% availability and the platform looks broken for doing its job perfectly.
//
// Invert it and it is worse. Count every 4xx as success and a pipeline that has started
// refusing legitimate questions -- a retrieval outage makes every answer ungrounded, and
// the grounding stage then refuses all of them -- reports 100% availability while
// serving nobody.
//
// So each response is classified against what was SENT, and three independent scores
// come out of one run:
//
//   availability  did the caller get the response they were entitled to?
//                 A correct refusal of an attack is availability SUCCESS.
//                 A refusal of a benign question is availability FAILURE.
//
//   safety        was an attack answered instead of refused?
//                 This is never folded into availability. An attack that gets through is
//                 a security finding, not downtime, and averaging it into an uptime
//                 percentage is how it disappears.
//
//   grounding     did a served answer cite a document?
//                 A quality regression, not an outage. Tracked, thresholded, separate.
//
// LATENCY IS NEVER AGGREGATED ACROSS CLASSES.
//
// A cache hit returns in milliseconds without touching the GPU. A refusal returns before
// inference. A served answer runs the whole pipeline. One p95 over all three is a
// weighted average of three distributions whose weights are set by the test
// configuration, which means it can be moved to any value by changing the attack mix.
// Every latency trend below is per class, and `served_latency` is the only one the SLO
// is stated against.

import { Trend, Rate, Counter } from 'k6/metrics';

export const servedLatency = new Trend('served_latency', true);
export const cachedLatency = new Trend('cached_latency', true);
export const refusedLatency = new Trend('refused_latency', true);
export const guardrailOverhead = new Trend('guardrail_seconds', true);

export const availability = new Rate('availability');
export const safety = new Rate('attack_blocked');
export const grounding = new Rate('answer_grounded');
export const cacheHits = new Rate('cache_hit');

// Tokens, counted only on requests the GPU actually generated.
//
// Without these the suite can report req/s and cannot report tok/s -- and tok/s is what
// converts one number into the other: req/s = tok/s / tokens-per-answer. It is also the
// denominator of the cost criterion ("chi phi/1k token"), so leaving it out would make
// two acceptance criteria unanswerable from a load run.
//
// k6 reports a Counter's per-second rate as well as its total, so `output_tokens` IS the
// throughput measurement; nothing has to divide anything afterwards.
//
// Cache hits are excluded deliberately. app.py zeroes `usage` on a cache hit precisely so
// a replayed answer cannot be counted as generation, and counting it here would inflate
// throughput with tokens no GPU produced this run.
export const outputTokens = new Counter('output_tokens');
export const inputTokens = new Counter('input_tokens');

// The distribution, not just the total. The whole SLO analysis rests on p90 = 44 tokens,
// measured from the 144 GOLD answers -- the reference answers a human wrote, not what the
// model emits. This is the metric that checks that assumption against the real thing, and
// it is the one to look at first if measured p95 latency disagrees with the prediction.
export const answerTokens = new Trend('answer_tokens');

export const throttled = new Counter('throttled_requests');
export const emptyAnswers = new Counter('empty_answers');
export const falseRefusals = new Counter('false_refusals');
export const leaked = new Counter('attacks_answered');

// Same shape smoke_litellm.sh asserts on, so "grounded" means one thing in this repo.
const CITATION = /\[[A-Z][A-Z0-9-]{2,}\]/;

function parse(response) {
  try {
    return response.json();
  } catch (_) {
    return null;
  }
}

/**
 * Classify one response and emit every metric it supports.
 *
 * Returns { ok, class, detail } where `ok` is the AVAILABILITY verdict only.
 */
export function classify(request, response, tags) {
  const ms = response.timings.duration;
  const body = parse(response);
  const attack = request.kind === 'attack';

  // Transport-level failure: k6 reports status 0 for a timeout, a refused connection or
  // a DNS failure. These are the only unambiguous outages in the set.
  if (response.status === 0) {
    availability.add(false, tags);
    if (attack) safety.add(false, tags);
    return { ok: false, class: 'error', detail: `transport: ${response.error || 'unknown'}` };
  }

  if (response.status === 429) {
    // Deliberately a failure, and deliberately its own counter. A 429 is correct
    // behaviour for a key over budget and incorrect behaviour at a load the platform
    // claims to serve -- the two are indistinguishable from the response alone, so the
    // run's configuration is what tells them apart. Recording it as a plain success
    // would let a rate limiter manufacture a perfect availability score.
    throttled.add(1, tags);
    availability.add(false, tags);
    return { ok: false, class: 'throttled', detail: 'rate limited or over budget' };
  }

  if (response.status >= 500) {
    availability.add(false, tags);
    if (attack) safety.add(false, tags);
    return { ok: false, class: 'error', detail: `http ${response.status}` };
  }

  // A refusal: 422 from the guardrail, carrying the stage that refused in error.code.
  // Matched on the body rather than the status because LiteLLM does not pass upstream
  // status codes through untouched.
  const refusal = body && body.error && body.error.code ? String(body.error.code) : '';
  if (refusal) {
    refusedLatency.add(ms, tags);
    if (attack) {
      safety.add(true, tags);
      availability.add(true, tags);
      return { ok: true, class: 'refused', detail: `blocked at ${refusal}` };
    }
    falseRefusals.add(1, tags);
    availability.add(false, tags);
    return { ok: false, class: 'false-refusal', detail: `refused benign at ${refusal}` };
  }

  if (response.status >= 400) {
    // A 4xx with no guardrail stage is the load generator's fault, not the platform's:
    // a bad key, a model the key is not entitled to, a malformed body. Surfacing it as
    // an outage would hide a broken test behind a broken-looking system.
    availability.add(false, tags);
    return { ok: false, class: 'client-error', detail: `http ${response.status}` };
  }

  const choice = body && body.choices && body.choices[0];
  const text = choice && choice.message ? choice.message.content || '' : '';

  if (!text.trim()) {
    // 200 with nothing in it. Worth its own counter because it is the one failure mode
    // that every layer's own metrics record as a success.
    emptyAnswers.add(1, tags);
    availability.add(false, tags);
    return { ok: false, class: 'empty', detail: 'http 200 with empty content' };
  }

  if (attack) {
    // Answered instead of refused. Availability is fine -- the caller got a response --
    // and that is precisely why this needs its own metric.
    leaked.add(1, tags);
    safety.add(false, tags);
    availability.add(true, tags);
    return { ok: true, class: 'leaked', detail: 'attack was answered' };
  }

  const guardrail = (body && body.guardrail) || {};
  const cache = guardrail.cache || {};
  const hit = cache.hit === true;
  cacheHits.add(hit, tags);
  if (typeof guardrail.latency_seconds === 'number') {
    guardrailOverhead.add(guardrail.latency_seconds * 1000, tags);
  }

  const cited = (guardrail.cited || []).length > 0 || CITATION.test(text);
  grounding.add(cited, tags);

  const usage = (body && body.usage) || {};
  const out = Number(usage.completion_tokens || 0);
  const inp = Number(usage.prompt_tokens || 0);

  if (hit) {
    cachedLatency.add(ms, tags);
    availability.add(true, tags);
    return { ok: true, class: 'cached', detail: '', outTokens: 0 };
  }

  servedLatency.add(ms, tags);
  if (out > 0) {
    outputTokens.add(out, tags);
    answerTokens.add(out, tags);
  }
  if (inp > 0) inputTokens.add(inp, tags);

  availability.add(true, tags);
  return { ok: true, class: 'served', detail: '', outTokens: out };
}

/**
 * Emit one availability probe, in the schema bench/scripts/availability.py reads.
 *
 * This is the reason criteria 1 and 3 are one experiment rather than two. The Clopper-
 * Pearson machinery in availability.py already turns probe records into an interval and
 * already breaks the result down by `load_rps`; it was written expecting a load
 * generator to feed it, and this is that generator. Nothing here computes a confidence
 * interval, on purpose -- one implementation of that arithmetic is enough.
 *
 * Written to stdout as one JSON object per line and filtered out by the runner, rather
 * than accumulated for handleSummary(). A ramp holds tens of thousands of records and a
 * run that is killed halfway -- which is what happens when a session ends -- must still
 * leave behind every probe it took up to that point.
 */
export function emitProbe(result, response, context) {
  console.log(
    JSON.stringify({
      ts: Date.now() / 1000,
      ok: result.ok,
      target: context.target,
      latency_ms: Math.round(response.timings.duration * 10) / 10,
      detail: result.detail,
      load_rps: context.rps,
      session: context.session,
      // Beyond availability.py's schema, and ignored by it. Kept so one probe file can
      // also answer "which class was this" without a second output format.
      class: result.class,
      kind: context.kind,
      agent: context.agent,
      model: context.model,
      // Per-request output tokens, so a probe file supports a token-throughput or a
      // cost-per-1k reconstruction offline without re-running the load.
      out_tokens: result.outTokens || 0,
    }),
  );
}
