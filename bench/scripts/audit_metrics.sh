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
  vllm:inter_token_latency_seconds_bucket
  vllm:e2e_request_latency_seconds_bucket
  vllm:generation_tokens_total
  vllm:request_success_total
  # consumed by dashboards and by bench/scripts/analyze.py
  vllm:num_requests_running
  vllm:num_requests_waiting
  vllm:kv_cache_usage_perc
  vllm:num_preemptions_total
  vllm:prompt_tokens_total
  # hardware layer
  DCGM_FI_DEV_GPU_UTIL
  DCGM_FI_DEV_FB_USED
  DCGM_FI_DEV_SM_CLOCK
  DCGM_FI_DEV_POWER_USAGE
  DCGM_FI_DEV_GPU_TEMP
  DCGM_FI_PROF_PIPE_TENSOR_ACTIVE
  DCGM_FI_PROF_DRAM_ACTIVE
  # gateway layer -- the only place a per-agent or error-rate number can come from,
  # because vLLM sees one undifferentiated stream and exports no failure counter.
  # These names come from LiteLLM's documentation, not from a running proxy, which is
  # exactly the situation that produced four wrong vLLM names in week 1.
  litellm_proxy_total_requests_metric_total
  litellm_proxy_failed_requests_metric_total
  litellm_request_total_latency_metric_bucket
  litellm_output_tokens_metric_total
  # Guardrail seeds bounded zero-valued series, so these must exist even when idle.
  guardrail_requests_total
  guardrail_request_duration_seconds_bucket
  guardrail_processing_duration_seconds_bucket
  guardrail_stage_duration_seconds_bucket
  guardrail_upstream_requests_total
  guardrail_upstream_duration_seconds_bucket
  guardrail_documents_retrieved_bucket
  guardrail_grounding_verdicts_total
  guardrail_grounding_overlap_ratio_bucket
  guardrail_pii_findings_total
  guardrail_injection_detections_total
  guardrail_documents_dropped_total
  guardrail_citations_total
  guardrail_build_info
)

# These labelled series are created only after the matching request/DB operation. Their
# absence is reported, but does not fail an otherwise healthy idle installation.
AFTER_TRAFFIC=(
  litellm_llm_api_latency_metric_bucket
  litellm_llm_api_time_to_first_token_metric_bucket
  litellm_request_queue_time_seconds_bucket
  litellm_overhead_latency_metric_bucket
  litellm_deployment_total_requests_total
  litellm_deployment_success_responses_total
  litellm_deployment_failure_responses_total
  litellm_deployment_latency_per_output_token_bucket
  litellm_deployment_state
  litellm_input_tokens_metric_total
  litellm_total_tokens_metric_total
  litellm_spend_metric_total
  litellm_in_flight_requests
  litellm_postgres_latency_bucket
  litellm_postgres_total_requests_total
  litellm_postgres_failed_requests_total
)

# Labels matter as much as names here: a rule that aggregates `by (end_user)` on a metric
# that has no end_user label returns one merged series instead of one per agent, silently,
# and the dashboard shows a single line that looks plausible.
NEEDED_LABELS=(
  "litellm_proxy_total_requests_metric_total end_user"
  "litellm_proxy_total_requests_metric_total requested_model"
  "litellm_proxy_total_requests_metric_total team"
  "litellm_request_total_latency_metric_bucket end_user"
  "litellm_request_total_latency_metric_bucket team"
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

optional_missing=0
for m in "${AFTER_TRAFFIC[@]}"; do
  n=$(curl -sG "http://$PROM/api/v1/query" --data-urlencode "query=$m" \
      | jq '.data.result | length' 2>/dev/null || echo 0)
  if [ "${n:-0}" -gt 0 ]; then
    printf '  ok       %s\n' "$m"
  else
    printf '  WAITING  %s (requires matching traffic/config)\n' "$m"
    optional_missing=$((optional_missing + 1))
  fi
done

# Labels matter as much as names: aggregation by a missing label silently produces one
# merged series. Check both the acceptance-criterion identity (end_user) and the stable
# virtual-key grouping (team).
echo
echo "nhan (label) can co:"
label_missing=0
for entry in "${NEEDED_LABELS[@]}"; do
  metric=${entry%% *}; label=${entry##* }
  got=$(curl -sG "http://$PROM/api/v1/series" --data-urlencode "match[]=$metric" \
        | python3 -c "import sys,json
try: d=json.load(sys.stdin)['data']
except Exception: d=[]
print('yes' if any('$label' in s for s in d) else 'no')" 2>/dev/null)
  if [ "$got" = "yes" ]; then
    printf '  OK      %s{%s}\n' "$metric" "$label"
  else
    printf '  MISSING %s{%s}\n' "$metric" "$label"
    label_missing=$((label_missing + 1))
  fi
done

echo
if [ "$missing" -eq 0 ] && [ "$label_missing" -eq 0 ]; then
  echo "all $((${#NEEDED[@]})) always-on metrics and required labels present"
  if [ "$optional_missing" -gt 0 ]; then
    echo "$optional_missing traffic-dependent metric(s) not observed yet; run: make litellm-smoke"
  fi
  exit 0
fi

cat >&2 <<TXT
$missing metric(s) and $label_missing required label(s) missing.

Inspect the names and labels exported by the running vLLM/LiteLLM/guardrail builds:
  curl -s "http://$PROM/api/v1/label/__name__/values" | jq -r '.data[]' | grep -i <keyword>

Then record the correct contract in docs/metric-names.md together with the relevant image
tag and update observability/rules/*.yaml. Do not start measuring until this is clean --
a missing series or label is indistinguishable from a healthy merged series on a dashboard.
TXT
exit 1
