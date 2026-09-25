#!/usr/bin/env bash
# Send one request through the gateway and print the link that opens its trace.
#
# The point of this script is the last line it prints. Tracing is only useful if the gap
# between "I sent a request" and "I am looking at what it did" is a click, and without
# this that gap is: read the trace id out of a response header, open Grafana, pick
# Explore, pick the Tempo datasource, switch the query type to TraceQL, paste. Grafana
# encodes all of that in a URL, so this builds it.
#
#   make trace                                 # default question, through LiteLLM
#   make trace Q='câu hỏi khác'
#   make trace MODEL=qwen2.5-1.5b
#   make trace DIRECT=1                        # skip the gateway, call the guardrail
#
# Works against `make pf` (localhost) or against the published ingress; it discovers
# which by asking the cluster, so neither address is written down here.
set -euo pipefail

MODEL="${MODEL:-qwen2.5-7b}"
Q="${Q:-Một chuyến xe được tính là hoàn thành khi đáp ứng điều kiện nào?}"
MAX_TOKENS="${MAX_TOKENS:-120}"
AGENT="${AGENT:-moc-analytics}"

say() { printf '\n\033[1m%s\033[0m\n' "$*"; }

# --- where to send it -------------------------------------------------------------
# Prefer a port-forward if one is already running: it is authenticated by IAM and does
# not depend on the ingress being published or on today's home IP address.
if curl -fsS --max-time 2 http://127.0.0.1:4000/health/readiness >/dev/null 2>&1; then
  BASE="http://127.0.0.1:4000"; VIA="port-forward"
elif curl -fsS --max-time 2 http://127.0.0.1:8080/health >/dev/null 2>&1 && [ "${DIRECT:-0}" = "1" ]; then
  BASE="http://127.0.0.1:8080"; VIA="port-forward (guardrail truc tiep)"
else
  IP=$(kubectl -n monitoring get ingress grafana -o jsonpath='{.spec.rules[0].host}' 2>/dev/null \
        | sed 's/^grafana\.//; s/\.nip\.io$//')
  [ -n "$IP" ] || { echo "khong tim thay duong vao. Chay 'make pf' hoac 'make ingress-up'"; exit 1; }
  BASE="http://llm.$IP.nip.io:30080"; VIA="ingress"
fi

if [ "${DIRECT:-0}" = "1" ] && [ "$BASE" != "http://127.0.0.1:8080" ]; then
  # The guardrail has no Service published through the ingress, by design -- it is
  # reached only by LiteLLM. DIRECT therefore needs the port-forward.
  echo "DIRECT=1 can 'make pf' dang chay (cong 8080)"; exit 1
fi

KEY=$(kubectl -n llm-serving get secret litellm-secrets \
       -o jsonpath='{.data.LITELLM_MASTER_KEY}' 2>/dev/null | base64 -d || true)

say "gui request qua $VIA -> $BASE"
echo "  model  $MODEL"
echo "  agent  $AGENT"

BODY=$(python3 -c '
import json, sys
print(json.dumps({
    "model": sys.argv[1],
    "user": sys.argv[2],
    "max_tokens": int(sys.argv[3]),
    "messages": [{"role": "user", "content": sys.argv[4]}],
}, ensure_ascii=False))' "$MODEL" "$AGENT" "$MAX_TOKENS" "$Q")

HEADERS=$(mktemp)
trap 'rm -f "$HEADERS"' EXIT

# --fail is deliberately absent. A refused request -- an injection attempt, a grounding
# failure -- is a 4xx and is exactly the case somebody wants to open the trace for, so
# the body and the trace id still have to be read out of it.
RESPONSE=$(curl -sS -D "$HEADERS" --max-time 300 \
  -X POST "$BASE/v1/chat/completions" \
  ${KEY:+-H "Authorization: Bearer $KEY"} \
  -H "Content-Type: application/json" \
  --data-binary "$BODY")

STATUS=$(awk 'NR==1{print $2}' "$HEADERS")
# Both spellings, because LiteLLM does not pass upstream headers through untouched: it
# re-emits them with an `llm_provider-` prefix. Matching only `x-trace-id` finds the id
# when talking to the guardrail directly and silently finds nothing through the gateway,
# which is the path everybody actually uses.
TRACE=$(grep -iE '^(llm_provider-)?x-trace-id:' "$HEADERS" \
        | tail -1 | tr -d '\r' | awk '{print $2}')

say "HTTP $STATUS"
python3 - "$RESPONSE" <<'PY'
import json, sys
try:
    body = json.loads(sys.argv[1])
except ValueError:
    print(sys.argv[1][:500]); raise SystemExit
if "error" in body:
    error = body["error"]
    print(f"  tu choi tai stage: {error.get('code')}")
    print(f"  ly do            : {error.get('message')}")
    for line in error.get("detail") or []:
        print(f"    - {line}")
else:
    guardrail = body.get("guardrail", {})
    usage = body.get("usage", {})
    print(f"  verdict   {guardrail.get('verdict')}")
    print(f"  cited     {', '.join(guardrail.get('cited') or []) or '(khong)'}")
    print(f"  latency   {guardrail.get('latency_seconds')}s")
    print(f"  tokens    in={usage.get('prompt_tokens')} out={usage.get('completion_tokens')}")
    content = body["choices"][0]["message"]["content"]
    print("\n  --- tra loi ---")
    for line in content.splitlines():
        print(f"  {line}")
PY

if [ -z "$TRACE" ]; then
  cat <<'EOF'

  Khong co X-Trace-Id trong response.
    - guardrail chua bat tracing?   kubectl -n llm-serving set env deploy/guardrail --list | grep OTEL
    - hoac chua chay 'make tracing-up'
EOF
  exit 0
fi

# --- the link ---------------------------------------------------------------------
GIP=$(kubectl -n monitoring get ingress grafana -o jsonpath='{.spec.rules[0].host}' 2>/dev/null \
       | sed 's/^grafana\.//; s/\.nip\.io$//')
if [ -n "$GIP" ]; then
  GRAFANA="http://grafana.$GIP.nip.io:30080"
else
  GRAFANA="http://127.0.0.1:3000"
fi

# Grafana's Explore state is a JSON blob in the query string. `uid: tempo` is fixed by
# k8s/monitoring/kps-values.yaml, so the link does not have to look it up.
LINK=$(python3 -c '
import json, sys, urllib.parse
state = {"datasource": "tempo", "queries": [{"refId": "A", "queryType": "traceql",
         "query": sys.argv[2]}], "range": {"from": "now-1h", "to": "now"}}
print(sys.argv[1] + "/explore?schemaVersion=1&panes=" +
      urllib.parse.quote(json.dumps({"trace": state})) + "&orgId=1")' "$GRAFANA" "$TRACE")

say "trace id  $TRACE"
cat <<EOF

  Mo trong Grafana (dang nhap admin -- 'make creds'):

$LINK

  Neu link khong mo duoc: Grafana -> Explore -> datasource Tempo -> TraceQL -> dan id tren.
  Span se hien theo thu tu: LiteLLM -> guardrail (10 stage) -> vLLM.
EOF
