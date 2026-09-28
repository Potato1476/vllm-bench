#!/usr/bin/env bash
# Run a k6 scenario from inside the cluster and bring the probes back out.
#
# Same script, same scenarios, same probe schema as bench/scripts/k6_load.sh -- only the
# place the traffic originates changes. Use this for anything above a few req/s; see the
# comment at the top of k8s/bench/k6-job.yaml for why a laptop cannot offer that load
# without becoming the thing being measured.
#
#   make load-incluster SCENARIO=ramp
#   make load-incluster SCENARIO=steady RPS=10 DURATION=15m
set -euo pipefail

cd "$(dirname "$0")/../.."

SCENARIO="${SCENARIO:-smoke}"
NS="${NS:-llm-serving}"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
SESSION="${SESSION:-${STAMP}-${SCENARIO}-incluster}"
OUT_DIR="${OUT_DIR:-results/probe}"
JOB=k6-load

die() { printf 'loi: %s\n' "$*" >&2; exit 1; }
say() { printf '\n\033[1m%s\033[0m\n' "$*"; }

[ -f bench/datasets/attacks-v1.jsonl ] || die "thieu bo tan cong. Chay 'make attacks-build'"
[ -f data/xanhsm_retrieval_mock/eval/retrieval_eval.jsonl ] \
  || die "thieu bo 144 cau hoi vang. Chay 'make rag-data'"

# The gateway is reached by Service DNS, so this path does not depend on the ingress
# being published or on today's home IP address at all.
BASE_URL="${BASE_URL:-http://litellm-private.$NS.svc.cluster.local:4000}"

say "k6 $SCENARIO trong cluster -> $BASE_URL"
echo "  session  $SESSION"

# See the long comment in k6_load.sh: a capacity run against a 144-question corpus is
# served entirely from the semantic cache once earlier runs have filled it, and the
# engine never sees a request.
case "$SCENARIO" in
  ramp|slo|steady|soak)
    if [ "${SKIP_CACHE_CLEAR:-0}" != "1" ]; then
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

# --- ship the code and the data in --------------------------------------------------
# Rebuilt every run rather than kept: the cluster is destroyed nightly, and a ConfigMap
# that survived an edit to moc.js would run yesterday's test under today's name.
kubectl -n "$NS" delete job "$JOB" --ignore-not-found --wait=true >/dev/null
for cm in k6-script k6-lib k6-agents k6-attacks k6-gold k6-config; do
  kubectl -n "$NS" delete configmap "$cm" --ignore-not-found >/dev/null
done

kubectl -n "$NS" create configmap k6-script --from-file=moc.js=bench/k6/moc.js >/dev/null
kubectl -n "$NS" create configmap k6-lib --from-file=bench/k6/lib >/dev/null
kubectl -n "$NS" create configmap k6-agents --from-file=agents.json=bench/agents.json >/dev/null
kubectl -n "$NS" create configmap k6-attacks \
  --from-file=attacks-v1.jsonl=bench/datasets/attacks-v1.jsonl >/dev/null
kubectl -n "$NS" create configmap k6-gold \
  --from-file=retrieval_eval.jsonl=data/xanhsm_retrieval_mock/eval/retrieval_eval.jsonl >/dev/null

# Tunables travel as a ConfigMap so the Job manifest stays static and reviewable. The
# master key is NOT here -- the Job reads it from the existing Secret via secretKeyRef,
# so it never passes through a ConfigMap, a file or this process's arguments.
kubectl -n "$NS" create configmap k6-config \
  --from-literal=BASE_URL="$BASE_URL" \
  --from-literal=SCENARIO="$SCENARIO" \
  --from-literal=SESSION="$SESSION" \
  ${RPS:+--from-literal=RPS="$RPS"} \
  ${DURATION:+--from-literal=DURATION="$DURATION"} \
  ${MODEL:+--from-literal=MODEL="$MODEL"} \
  ${MAX_TOKENS:+--from-literal=MAX_TOKENS="$MAX_TOKENS"} \
  ${ATTACK_MIX:+--from-literal=ATTACK_MIX="$ATTACK_MIX"} \
  ${CACHE_MIX:+--from-literal=CACHE_MIX="$CACHE_MIX"} \
  ${RAMP_LEVELS:+--from-literal=RAMP_LEVELS="$RAMP_LEVELS"} \
  ${LEVEL_SECONDS:+--from-literal=LEVEL_SECONDS="$LEVEL_SECONDS"} \
  ${DRAIN_SECONDS:+--from-literal=DRAIN_SECONDS="$DRAIN_SECONDS"} \
  ${REQUEST_TIMEOUT:+--from-literal=REQUEST_TIMEOUT="$REQUEST_TIMEOUT"} \
  ${MAX_VUS:+--from-literal=MAX_VUS="$MAX_VUS"} \
  ${ITERATIONS:+--from-literal=ITERATIONS="$ITERATIONS"} >/dev/null

kubectl -n "$NS" apply -f k8s/bench/k6-job.yaml >/dev/null
echo "  cho pod khoi dong..."
kubectl -n "$NS" wait --for=condition=ready pod -l app=k6-load --timeout=180s >/dev/null \
  || die "pod k6 khong khoi dong duoc. kubectl -n $NS describe job/$JOB"

# --- collect ------------------------------------------------------------------------
mkdir -p "$OUT_DIR"
PROBES="$OUT_DIR/$SESSION.jsonl"
RAW="$OUT_DIR/$SESSION.raw"

finish() {
  [ -f "$RAW" ] || return 0
  grep '^{' "$RAW" > "$PROBES" 2>/dev/null || : > "$PROBES"
  rm -f "$RAW"
}
trap finish EXIT INT TERM

# -f streams, so a long ramp is watchable and a Ctrl-C still leaves every probe taken up
# to that point on disk. Non-JSON lines -- k6's messages and the summary -- are echoed
# live and filtered out of the probe file afterwards.
kubectl -n "$NS" logs -f "job/$JOB" 2>/dev/null | tee "$RAW" | grep -v '^{' || true

# THEN READ THE LOG AGAIN, FROM THE TOP.
#
# The stream above is for watching, not for collecting, and treating it as collection
# lost most of a 15-minute ramp: the home IP changed mid-run, `kubectl logs -f` died at
# the 20 req/s level, and the run looked like it had crashed there. It had not -- the Job
# ran to completion inside the cluster and exited Succeeded, and the 35 and 50 req/s
# levels existed the whole time in the pod's log. 2102 probes were collected out of 8401.
#
# Any interruption between a laptop and the API server -- an address change, a sleep, a
# dropped connection -- silently truncates a measurement that cost GPU minutes. So once
# the Job is finished, fetch the whole log non-streaming and keep whichever copy has more
# probe records.
if kubectl -n "$NS" wait --for=condition=complete "job/$JOB" --timeout=120s >/dev/null 2>&1 \
   || kubectl -n "$NS" wait --for=condition=failed "job/$JOB" --timeout=10s >/dev/null 2>&1; then
  FULL="$OUT_DIR/$SESSION.full"
  if kubectl -n "$NS" logs "job/$JOB" > "$FULL" 2>/dev/null; then
    streamed=$(grep -c '^{' "$RAW" 2>/dev/null || echo 0)
    complete=$(grep -c '^{' "$FULL" 2>/dev/null || echo 0)
    if [ "$complete" -gt "$streamed" ]; then
      echo "  stream bi dut: lay lai tu log job ($streamed -> $complete probe)"
      mv "$FULL" "$RAW"
    else
      rm -f "$FULL"
    fi
  fi
fi
finish

COUNT=$(wc -l < "$PROBES" | tr -d ' ')
STATE=$(kubectl -n "$NS" get job "$JOB" -o jsonpath='{.status.succeeded}' 2>/dev/null)
cat <<EOF

  job ket thuc (succeeded=${STATE:-0})
  $COUNT probe -> $PROBES

  Khoang tin cay va phan ra theo muc tai:
    make availability PROBES=$PROBES

  Job va ConfigMap duoc giu lai de xem log; lan chay sau se tao lai.
EOF
