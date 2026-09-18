#!/usr/bin/env bash
# Confirm every metric the rules and dashboards depend on actually exists, by name, in
# this Prometheus.
#
# vLLM renames series between versions and DCGM field names differ by driver build. A
# renamed metric does not error: the recording rule evaluates to nothing, the dashboard
# panel is blank, and the alert never fires. That failure looks exactly like "the system
# is healthy", which is why this runs before any measurement rather than after.
set -uo pipefail

PROM=${PROM:-localhost:9090}

NEEDED=(
  # consumed by observability/rules/recording.yaml
  vllm:time_to_first_token_seconds_bucket
  vllm:time_per_output_token_seconds_bucket
  vllm:generation_tokens_total
  vllm:request_success_total
  vllm:request_failure_total
  # consumed by dashboards and by bench/scripts/analyze.py
  vllm:num_requests_running
  vllm:num_requests_waiting
  vllm:gpu_cache_usage_perc
  vllm:num_preemptions_total
  vllm:prompt_tokens_total
  # hardware layer
  DCGM_FI_DEV_GPU_UTIL
  DCGM_FI_DEV_FB_USED
  DCGM_FI_DEV_SM_CLOCK
  DCGM_FI_DEV_POWER_USAGE
  DCGM_FI_DEV_GPU_TEMP
  DCGM_FI_PROF_PIPE_TENSOR_ACTIVE
)

if ! curl -sf "http://$PROM/-/ready" >/dev/null 2>&1; then
  echo "cannot reach Prometheus at $PROM" >&2
  echo "run: kubectl -n monitoring port-forward svc/kps-kube-prometheus-stack-prometheus 9090:9090" >&2
  exit 1
fi

missing=0
for m in "${NEEDED[@]}"; do
  n=$(curl -sG "http://$PROM/api/v1/query" --data-urlencode "query=$m" \
      | jq '.data.result | length' 2>/dev/null || echo 0)
  if [ "${n:-0}" -gt 0 ]; then
    printf '  ok       %s\n' "$m"
  else
    printf '  MISSING  %s\n' "$m"
    missing=$((missing + 1))
  fi
done

echo
if [ "$missing" -eq 0 ]; then
  echo "all $((${#NEEDED[@]})) metrics present"
  exit 0
fi

cat >&2 <<TXT
$missing metric(s) missing.

Find what this vLLM build calls them:
  curl -s "http://$PROM/api/v1/label/__name__/values" | jq -r '.data[]' | grep -i <keyword>

Then record the correct name in docs/metric-names.md together with the vLLM image tag,
and update observability/rules/*.yaml. Do not start measuring until this is clean --
a missing series is indistinguishable from a healthy one on a dashboard.
TXT
exit 1
