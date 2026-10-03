{{- define "webui.validate" -}}
{{- if not .Values.image.repository -}}
{{- fail "image.repository is empty" -}}
{{- end -}}
{{- if not .Values.image.tag -}}
{{- fail "image.tag is empty -- pin it, a moving tag makes two pilot sessions incomparable" -}}
{{- end -}}
{{- if not .Values.openai.baseUrl -}}
{{- fail "openai.baseUrl is empty -- point it at the LiteLLM service, not at the guardrail" -}}
{{- end -}}
{{- if not .Values.openai.existingSecret.name -}}
{{- fail "openai.existingSecret.name is required -- run `make webui-secret`" -}}
{{- end -}}
{{- /*
     Refuse the combination that silently discards everything else in values.yaml.

     With persistentConfig true, Open WebUI reads its ConfigVar settings once into the
     database and ignores the environment afterwards. Every feature toggle below would
     then be decoration: the pod spec would show titleGeneration disabled while the
     running app kept generating titles, and the resulting refusals would land in the
     same metric as the guardrail's real ones. Fail loudly instead.
*/ -}}
{{- if .Values.persistentConfig -}}
{{- $wanted := list -}}
{{- range $k, $v := .Values.features -}}
{{- if not $v -}}{{- $wanted = append $wanted $k -}}{{- end -}}
{{- end -}}
{{- if $wanted -}}
{{- fail (printf "persistentConfig=true makes these feature toggles unenforceable after the first boot: %s. Set persistentConfig=false, or stop setting them here and change them in the admin UI." (join ", " $wanted)) -}}
{{- end -}}
{{- end -}}
{{- if and (gt (int .Values.replicaCount) 1) .Values.persistence.enabled -}}
{{- fail "more than one replica cannot share one sqlite volume -- use a single replica, or move to DATABASE_URL on Postgres first" -}}
{{- end -}}
{{- end -}}
