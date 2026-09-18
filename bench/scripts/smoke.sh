#!/usr/bin/env bash
# Smoke test for the vLLM deployment.
#
# The point is to separate "the cluster is broken" from "the measurement is bad" before
# any benchmarking starts. Every check below must pass before a single number is worth
# recording.
set -euo pipefail

NS=${NS:-inference}
SVC=${SVC:-vllm-svc}
PORT=${PORT:-8000}
MODEL=${MODEL:-qwen2.5-7b}
OUT=${OUT:-reports}

pf_pid=""
cleanup() { [ -n "$pf_pid" ] && kill "$pf_pid" 2>/dev/null || true; }
trap cleanup EXIT

say() { printf '\n=== %s\n' "$*"; }
fail() { printf 'FAIL: %s\n' "$*" >&2; exit 1; }

say "0. pod is ready"
kubectl -n "$NS" wait --for=condition=ready pod -l app=vllm-server --timeout=1800s \
  || fail "vLLM pod never became ready -- check: kubectl -n $NS describe pod -l app=vllm-server"

kubectl -n "$NS" port-forward "svc/$SVC" "${PORT}:8000" >/dev/null 2>&1 &
pf_pid=$!
for _ in $(seq 1 30); do
  curl -sf "localhost:${PORT}/health" >/dev/null 2>&1 && break
  sleep 1
done
curl -sf "localhost:${PORT}/health" >/dev/null || fail "port-forward never came up"

say "1. model is loaded and served under the expected name"
got=$(curl -s "localhost:${PORT}/v1/models" | jq -r '.data[].id')
echo "  served model: $got"
[ "$got" = "$MODEL" ] || fail "expected model name '$MODEL', got '$got'"

say "2. the GPU is actually the one we think it is"
kubectl -n "$NS" exec deploy/vllm-server -- nvidia-smi \
  --query-gpu=name,memory.used,memory.total --format=csv,noheader \
  || fail "nvidia-smi failed inside the container"

say "3. non-streaming completion returns real content"
body=$(curl -s "localhost:${PORT}/v1/chat/completions" \
  -H 'Content-Type: application/json' \
  -d "{\"model\":\"$MODEL\",
       \"messages\":[{\"role\":\"user\",\"content\":\"Explain KV-Cache in two sentences.\"}],
       \"max_tokens\":100}")
txt=$(echo "$body" | jq -r '.choices[0].message.content // empty')
[ -n "$txt" ] || fail "empty completion; raw response: $body"
echo "  ${txt:0:160}"

say "4. streaming returns many chunks, not one"
chunks=$(curl -sN "localhost:${PORT}/v1/chat/completions" \
  -H 'Content-Type: application/json' \
  -d "{\"model\":\"$MODEL\",
       \"messages\":[{\"role\":\"user\",\"content\":\"Count from 1 to 20.\"}],
       \"max_tokens\":120,\"stream\":true}" | grep -c '^data: ' || true)
echo "  SSE chunks: $chunks"
# One chunk means the server buffered the whole answer. TBT would then be
# unmeasurable and every per-token latency number would be fiction.
[ "$chunks" -gt 10 ] || fail "only $chunks SSE chunks -- streaming is not per-token"

say "5. measure TTFT on a single request"
ttft=$(curl -sN -o /dev/null -w '%{time_starttransfer}' "localhost:${PORT}/v1/chat/completions" \
  -H 'Content-Type: application/json' \
  -d "{\"model\":\"$MODEL\",
       \"messages\":[{\"role\":\"user\",\"content\":\"Say hello.\"}],
       \"max_tokens\":16,\"stream\":true}")
echo "  time to first byte: ${ttft}s"

say "6. metrics endpoint exposes the vllm: series"
mkdir -p "$OUT"
curl -s "localhost:${PORT}/metrics" | grep '^vllm:' | cut -d'{' -f1 | sort -u \
  > "$OUT/w1-metric-names.txt"
n=$(wc -l < "$OUT/w1-metric-names.txt" | tr -d ' ')
echo "  $n distinct vllm: metrics -> $OUT/w1-metric-names.txt"
[ "$n" -gt 5 ] || fail "only $n vllm: metrics found -- week 2 dashboards will have nothing to read"

for m in vllm:time_to_first_token_seconds vllm:gpu_cache_usage_perc vllm:num_requests_waiting; do
  grep -q "^$m" "$OUT/w1-metric-names.txt" \
    && echo "  ok      $m" \
    || echo "  MISSING $m  (name may have changed in this vLLM version -- record it in docs/metric-names.md)"
done

printf '\nall smoke checks passed\n'
