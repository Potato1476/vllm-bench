{{- define "tunnel.validate" -}}
{{- if not .Values.image.tag -}}
{{- fail "image.tag is empty -- pin it; cloudflared ships weekly and `latest` makes two sessions incomparable" -}}
{{- end -}}
{{- if not .Values.existingSecret.name -}}
{{- fail "existingSecret.name is required -- run `make tunnel-secret TUNNEL_TOKEN=...`" -}}
{{- end -}}
{{- if lt (int .Values.replicaCount) 1 -}}
{{- fail "replicaCount must be at least 1" -}}
{{- end -}}
{{- end -}}
