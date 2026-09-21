{{/*
Which model keys run in the current mode.
  shared -> both;  solo-a -> only a;  solo-b -> only b
*/}}
{{- define "vllm.activeModels" -}}
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

{{/*
gpu-memory-utilization for one model under the current mode.
*/}}
{{- define "vllm.gpuMemory" -}}
{{- $root := .root -}}
{{- $key := .key -}}
{{- if eq $root.Values.mode "shared" -}}
{{- index $root.Values.gpuMemory.shared $key -}}
{{- else -}}
{{- index $root.Values.gpuMemory.solo $key -}}
{{- end -}}
{{- end -}}

{{/*
Resource block for one model under the current mode.
*/}}
{{- define "vllm.resources" -}}
{{- $root := .root -}}
{{- $m := index $root.Values.models .key -}}
{{- if eq $root.Values.mode "shared" -}}
{{- toYaml $m.resources.shared -}}
{{- else -}}
{{- toYaml $m.resources.solo -}}
{{- end -}}
{{- end -}}

{{/*
Refuse a configuration that cannot physically start, rather than letting it fail as an
OOM twenty minutes into a measurement window.
*/}}
{{- define "vllm.validate" -}}
{{- if not .Values.artifactsBucket -}}
{{- fail "artifactsBucket is empty -- run through `make vllm-up`, which reads it from terraform output" -}}
{{- end -}}
{{- if not .Values.roleArn -}}
{{- fail "roleArn is empty -- run through `make vllm-up`, which reads it from terraform output" -}}
{{- end -}}
{{- if eq .Values.mode "shared" -}}
{{- $cpu := addf (float64 .Values.models.a.resources.shared.requests.cpu) (float64 .Values.models.b.resources.shared.requests.cpu) -}}
{{- if gt $cpu .Values.nodeAllocatableCpu -}}
{{- fail (printf "shared-mode CPU requests total %v, more than the %v a g6.xlarge leaves after daemonsets. The second pod will sit Pending with 'Insufficient cpu'." $cpu .Values.nodeAllocatableCpu) -}}
{{- end -}}
{{- $sum := addf .Values.gpuMemory.shared.a .Values.gpuMemory.shared.b -}}
{{- if ge $sum 1.0 -}}
{{- fail (printf "gpuMemory.shared.a + gpuMemory.shared.b = %v, which is >= 1.0. Two vLLM instances on one card would OOM each other; leave headroom for the CUDA context." $sum) -}}
{{- end -}}
{{- end -}}
{{- end -}}
