{{/* Model keys that exist in the matching vLLM mode. */}}
{{- define "litellm.activeModels" -}}
{{- if eq .Values.mode "shared" -}}
a b
{{- else if eq .Values.mode "solo-a" -}}
a
{{- else if eq .Values.mode "solo-b" -}}
b
{{- else -}}
{{- fail (printf "mode must be shared, solo-a or solo-b; got %q" .Values.mode) -}}
{{- end -}}
{{- end -}}

{{/* Fail during render, before an invalid release reaches the cluster. */}}
{{- define "litellm.validate" -}}
{{- if not .Values.existingSecret.name -}}
{{- fail "existingSecret.name is required; create it with `make litellm-secret`" -}}
{{- end -}}
{{- if lt (int .Values.replicaCount) 1 -}}
{{- fail "replicaCount must be at least 1" -}}
{{- end -}}
{{- if and (gt (int .Values.replicaCount) 1) (not .Values.redis.enabled) -}}
{{- fail "multiple LiteLLM replicas require shared Redis (redis.enabled=true)" -}}
{{- end -}}
{{- if and (gt (int .Values.replicaCount) 1) (not .Values.database.disableSchemaUpdate) -}}
{{- fail "multiple LiteLLM replicas require database.disableSchemaUpdate=true and a one-time migration before rollout" -}}
{{- end -}}
{{- if and .Values.redis.enabled (not .Values.redis.existingSecret.name) -}}
{{- fail "redis.existingSecret.name is required when Redis is enabled" -}}
{{- end -}}
{{- if and (eq .Values.service.type "NodePort") (not .Values.service.nodePort) -}}
{{- fail "service.nodePort is required when service.type=NodePort" -}}
{{- end -}}
{{- end -}}
