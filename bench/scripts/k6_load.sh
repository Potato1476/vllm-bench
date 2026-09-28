#!/usr/bin/env bash
# Run one k6 scenario against the platform and leave behind an availability probe file.
#
# Everything here is plumbing around `k6 run`, and the plumbing is the point: without it
# a load test is a command with six environment variables that somebody has to remember,
# an endpoint that changes every session, a master key that lives in a Secret, and an
# output stream that mixes probe records into the progress bar.
#
#   make load-smoke                       # 12 requests, no GPU cost worth counting
#   make load-ramp                        # the knee finder
#   make load-steady RPS=2 DURATION=10m   # the availability claim
#   make load-adversarial                 # does safety hold at load
#
# Probes land in results/probe/<session>.jsonl and are read by:
#   make availability PROBES=results/probe/
set -euo pipefail

cd "$(dirname "$0")/../.."

SCENARIO="${SCENARIO:-smoke}"
NS="${NS:-llm-serving}"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
SESSION="${SESSION:-${STAMP}-${SCENARIO}}"
OUT_DIR="${OUT_DIR:-results/probe}"
SUMMARY_DIR="${SUMMARY_DIR:-results/k6}"

say() { printf '\n\033[1m%s\033[0m\n' "$*"; }
die() { printf 'loi: %s\n' "$*" >&2; exit 1; }

command -v k6 >/dev/null 2>&1 || die "chua cai k6. macOS: brew install k6"

# --- datasets -------------------------------------------------------------------------
# open() runs in k6's init context and aborts the whole run if a file is missing, so
# check here where the message can say what to do about it. The attack suite is
# gitignored because it is regenerated deterministically from a seed.
[ -f data/xanhsm_retrieval_mock/eval/retrieval_eval.jsonl ] \
  || die "thieu bo 144 cau hoi vang. Chay 'make rag-data'"
if [ ! -f bench/datasets/attacks-v1.jsonl ]; then
  # Not fatal for a pure capacity run, fatal for anything measuring safety: a missing
  # attack set makes attack_blocked vacuously 100%, which is the most dangerous possible
  # way for this to fail.
  case "$SCENARIO" in
    adversarial|smoke) die "thieu bo tan cong. Chay 'make attacks-build'" ;;
    *) echo "  canh bao: chua co bench/datasets/attacks-v1.jsonl; khong do duoc safety" ;;
  esac
fi

# --- where to send it -------------------------------------------------------------------
# Same discovery as bench/scripts/trace_request.sh: prefer a port-forward, because it is
# authenticated by IAM and does not depend on the ingress being published or on today's
# home IP address. Neither address is written down in this file.
if [ -n "${BASE_URL:-}" ]; then
  VIA="BASE_URL"
elif curl -fsS --max-time 2 http://127.0.0.1:4000/health/readiness >/dev/null 2>&1; then
  BASE_URL="http://127.0.0.1:4000"; VIA="port-forward"
else
  IP=$(kubectl -n monitoring get ingress grafana -o jsonpath='{.spec.rules[0].host}' 2>/dev/null \
        | sed 's/^grafana\.//; s/\.nip\.io$//')
  [ -n "$IP" ] || die "khong tim thay duong vao. Chay 'make pf' hoac 'make ingress-up'"
  BASE_URL="http://llm.$IP.nip.io:30080"; VIA="ingress"
fi

# A port-forward is a single TCP proxy through one kubectl process on a laptop. It is the
# right default for smoke and for a couple of requests per second; it is not a load path.
# Past a few req/s it becomes the bottleneck and the run measures kubectl.
RATE_HINT="${RPS:-0}"
case "$SCENARIO" in ramp) RATE_HINT="${RAMP_MAX:-50}" ;; esac
if [ "$VIA" = "port-forward" ] && [ "${RATE_HINT%%.*}" -gt 5 ] 2>/dev/null; then
  cat >&2 <<'EOF'

  CANH BAO: dang gui tai cao qua port-forward.
  kubectl port-forward la mot proxy TCP don luong tren may ban -- tren vai req/s no se
  tro thanh nut co chai va phep do se do chinh kubectl, khong do nen tang.
  Dung 'make load-incluster' (Job chay tren node tooling) cho cac muc tai cao.

EOF
fi

# --- the cache, before a capacity run ---------------------------------------------------
# A capacity measurement must reach the engine, and with this corpus it silently does not.
# The gold set is 144 questions; a few earlier runs put all of them in the semantic cache,
# and a ramp then draws from a pool that is entirely cached. Measured during exactly that:
#
#     guardrail        3.229 req/s     <- traffic arriving
#     vLLM             0.000 req/s     <- engine idle
#     semantic cache   100% hit
#
# Every Grafana engine panel correctly read zero, the run reported latency in the tens of
# milliseconds, and it was measuring Redis. README.md warns about this -- "bạn đang đo
# cache chứ không đo engine" -- and the warning is not enough, because nothing about the
# run looks wrong until you compare two rates that no dashboard puts side by side.
#
# Clearing is not sufficient on its own either: within one ramp the same 144 questions
# repeat and re-cache. CAPACITY_SCENARIOS therefore expect the cache to be OFF, and this
# only removes what an earlier session left behind. k6 also thresholds cache_hit for the
# `slo` scenario as a backstop.
case "$SCENARIO" in
  ramp|slo|steady|soak)
    if [ "${SKIP_CACHE_CLEAR:-0}" != "1" ] && [ -z "${BASE_URL:-}" ]; then
      echo "  xoa semantic cache truoc phep do dung luong..."
      # timeout, because `kubectl exec` into this pod intermittently hangs forever, and
      # without a bound it hangs the measurement instead of the shell -- a 15-minute ramp
      # that never starts and prints nothing, which is exactly how this was found.
      # Clearing the cache is a precaution; failing to clear it must not cost the run.
      timeout 60 kubectl -n "$NS" exec deploy/guardrail -c guardrail -- \
        python -m services.llm_pipeline.cache_admin clear 2>/dev/null \
        | sed 's/^/    /' || echo "    (bo qua: khong xoa duoc trong 60s)"
    fi
    ;;
esac

MASTER_KEY="${MASTER_KEY:-$(kubectl -n "$NS" get secret litellm-secrets \
  -o jsonpath='{.data.LITELLM_MASTER_KEY}' 2>/dev/null | base64 -d || true)}"
[ -n "$MASTER_KEY" ] || die "khong doc duoc LITELLM_MASTER_KEY tu secret litellm-secrets"

mkdir -p "$OUT_DIR" "$SUMMARY_DIR"
PROBES="$OUT_DIR/$SESSION.jsonl"
SUMMARY="$SUMMARY_DIR/$SESSION.json"
# k6 writes its OWN log lines to --log-output as well as ours, so the raw stream is not
# valid JSONL. It matters more than it sounds: the line it adds is
# "thresholds on metrics 'attack_blocked' have been crossed", which appears only on the
# run where safety failed -- the one run whose probe file must not be malformed.
#
# So the raw stream is durable and the .jsonl is derived from it, in a trap, so that a
# session killed by lab-down still leaves a clean probe file behind.
RAW="$OUT_DIR/$SESSION.raw"
finish() {
  [ -f "$RAW" ] || return 0
  grep '^{' "$RAW" > "$PROBES" 2>/dev/null || : > "$PROBES"
  if grep -qv '^{' "$RAW" 2>/dev/null; then
    echo
    echo "  k6 ghi chu:"
    grep -v '^{' "$RAW" | sed 's/^/    /'
  fi
  rm -f "$RAW"
}
trap finish EXIT INT TERM

say "k6 $SCENARIO -> $BASE_URL ($VIA)"
echo "  session  $SESSION"
echo "  probes   $PROBES"

# --- run ----------------------------------------------------------------------------
# Three output streams, kept apart:
#   --log-output=file  console.log, i.e. one probe record per line, written as it happens
#                      so a run that is killed mid-session still leaves its probes behind
#   stdout             handleSummary()
#   stderr             the progress bar, live
#
# MASTER_KEY is passed with -e so it never reaches a file or the process list of anything
# but k6 itself.
set +e
k6 run \
  --log-format=raw \
  --log-output="file=$RAW" \
  -e "BASE_URL=$BASE_URL" \
  -e "MASTER_KEY=$MASTER_KEY" \
  -e "SCENARIO=$SCENARIO" \
  -e "SESSION=$SESSION" \
  -e "SUMMARY_JSON=$SUMMARY" \
  ${RPS:+-e "RPS=$RPS"} \
  ${DURATION:+-e "DURATION=$DURATION"} \
  ${MODEL:+-e "MODEL=$MODEL"} \
  ${MAX_TOKENS:+-e "MAX_TOKENS=$MAX_TOKENS"} \
  ${ATTACK_MIX:+-e "ATTACK_MIX=$ATTACK_MIX"} \
  ${CACHE_MIX:+-e "CACHE_MIX=$CACHE_MIX"} \
  ${RAMP_LEVELS:+-e "RAMP_LEVELS=$RAMP_LEVELS"} \
  ${LEVEL_SECONDS:+-e "LEVEL_SECONDS=$LEVEL_SECONDS"} \
  ${DRAIN_SECONDS:+-e "DRAIN_SECONDS=$DRAIN_SECONDS"} \
  ${REQUEST_TIMEOUT:+-e "REQUEST_TIMEOUT=$REQUEST_TIMEOUT"} \
  ${AGENT_KEYS:+-e "AGENT_KEYS=$AGENT_KEYS"} \
  ${MAX_VUS:+-e "MAX_VUS=$MAX_VUS"} \
  ${ITERATIONS:+-e "ITERATIONS=$ITERATIONS"} \
  bench/k6/moc.js
STATUS=$?
set -e

# Normally, not only from the trap: the count printed below has to read the filtered
# file, and the trap does not run until after that. `finish` is idempotent.
finish

# 99 is k6's "a threshold failed" exit. That is a result, not a crash: the safety
# threshold aborting is the suite working exactly as designed, and the probe file is
# still valid and still worth analysing. Anything else is a failure to measure.
case "$STATUS" in
  0) ;;
  99) echo; echo "  mot threshold khong dat (exit 99) -- xem phan tren. Probe van dung." ;;
  *) echo; echo "  k6 thoat voi ma $STATUS; probe co the khong day du." >&2 ;;
esac

COUNT=$(wc -l < "$PROBES" | tr -d ' ')
cat <<EOF

  $COUNT probe -> $PROBES
  summary JSON  -> $SUMMARY

  Khoang tin cay va phan ra theo muc tai:
    make availability PROBES=$PROBES

  Gop nhieu phien lai (14 phien phu 14 ngay lich):
    make availability PROBES=$OUT_DIR/
EOF
exit 0
