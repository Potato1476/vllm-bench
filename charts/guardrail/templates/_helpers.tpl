{{- define "guardrail.validate" -}}
{{- if not .Values.image.repository -}}
{{- fail "image.repository is empty -- use `make guardrail-up`, which reads the ECR URL from terraform output" -}}
{{- end -}}
{{- if not .Values.image.tag -}}
{{- fail "image.tag is empty -- use `make guardrail-up` or set it explicitly" -}}
{{- end -}}
{{- if lt (int .Values.replicaCount) 1 -}}
{{- fail "replicaCount must be at least 1" -}}
{{- end -}}
{{- if and .Values.semanticCache.enabled (gt (int .Values.replicaCount) 1) (ne .Values.semanticCache.redis.mode "external") -}}
{{- fail "multiple guardrail replicas with cache enabled require semanticCache.redis.mode=external" -}}
{{- end -}}
{{- if and .Values.semanticCache.enabled (eq .Values.semanticCache.redis.mode "external") (not .Values.semanticCache.redis.existingSecret.name) -}}
{{- fail "semanticCache.redis.existingSecret.name is required for external Redis" -}}
{{- end -}}
{{- if and .Values.semanticCache.enabled (not (has .Values.semanticCache.redis.mode (list "sidecar" "external"))) -}}
{{- fail "semanticCache.redis.mode must be sidecar or external" -}}
{{- end -}}
{{- end -}}
