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
{{- if gt (int .Values.replicaCount) 1 -}}
{{- fail "this phase has no Redis; replicaCount must stay at 1 so quota/router state is not split across pods" -}}
{{- end -}}
{{- if and (eq .Values.service.type "NodePort") (not .Values.service.nodePort) -}}
{{- fail "service.nodePort is required when service.type=NodePort" -}}
{{- end -}}
{{- end -}}
