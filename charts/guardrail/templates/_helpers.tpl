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
{{- end -}}
