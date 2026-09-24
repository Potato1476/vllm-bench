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
The S3 prefix holding the weights actually being served, which depends on quantization.
*/}}
{{- define "vllm.s3Prefix" -}}
{{- $m := index .root.Values.models .key -}}
{{- if eq .root.Values.quantization "awq" -}}
{{- if not $m.awq -}}
{{- fail (printf "quantization is awq but model %q declares no awq block" .key) -}}
{{- end -}}
{{- $m.awq.s3Prefix -}}
{{- else if eq .root.Values.quantization "none" -}}
{{- $m.s3Prefix -}}
{{- else -}}
{{- fail (printf "quantization must be none or awq; got %q" .root.Values.quantization) -}}
{{- end -}}
{{- end -}}

{{/*
How much GPU memory the served weights occupy, for the fit check.
*/}}
{{- define "vllm.weightsGiB" -}}
{{- $m := index .root.Values.models .key -}}
{{- if eq .root.Values.quantization "awq" -}}
{{- $m.awq.weightsGiB -}}
{{- else -}}
{{- $m.weightsGiB -}}
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
{{- $mem := addf (float64 (trimSuffix "Gi" .Values.models.a.resources.shared.requests.memory)) (float64 (trimSuffix "Gi" .Values.models.b.resources.shared.requests.memory)) -}}
{{- if gt $mem .Values.nodeAllocatableMemoryGiB -}}
{{- fail (printf "shared-mode memory requests total %v GiB, more than the %v GiB a g6.xlarge leaves after kubelet reservations. The second pod will sit Pending with 'Insufficient memory'." $mem .Values.nodeAllocatableMemoryGiB) -}}
{{- end -}}
{{- $sum := addf .Values.gpuMemory.shared.a .Values.gpuMemory.shared.b -}}
{{- if ge $sum 1.0 -}}
{{- fail (printf "gpuMemory.shared.a + gpuMemory.shared.b = %v, which is >= 1.0. Two vLLM instances on one card would OOM each other; leave headroom for the CUDA context." $sum) -}}
{{- end -}}
{{- end -}}

{{/*
Do the weights actually fit in the share of the card they are given?

This is the check that was missing. `mode: shared` sat in values.yaml as the documented
normal state of the lab and had never once run, because FP16 model A needs 14.2 GiB and
its 0.65 share of the card is 14.6 GiB -- arithmetic that nothing performed until the
engine tried it. The symptom is not a clear error either: vLLM loads the weights, then
computes a KV cache of near zero and either refuses to allocate or starts and serves one
sequence at a time. Doing the multiplication here turns twenty minutes of GPU time into a
render-time failure that names the fix.
*/}}
{{- $root := . -}}
{{- range $key := splitList " " (include "vllm.activeModels" .) -}}
{{- $gmu := float64 (include "vllm.gpuMemory" (dict "root" $root "key" $key)) -}}
{{- $weights := float64 (include "vllm.weightsGiB" (dict "root" $root "key" $key)) -}}
{{- $share := mulf $gmu $root.Values.gpuMemoryGiB -}}
{{- $kv := subf $share $weights -}}
{{- if lt $kv $root.Values.minKvCacheGiB -}}
{{- fail (printf "model %s does not fit: %s weights need %.1f GiB, but --gpu-memory-utilization=%v of a %.1f GiB card is %.1f GiB, leaving %.1f GiB for the KV cache (minimum %.1f). Fix by setting quantization: awq, or by raising gpuMemory for this model in mode %s."
    $key $root.Values.quantization $weights $gmu $root.Values.gpuMemoryGiB $share $kv $root.Values.minKvCacheGiB $root.Values.mode) -}}
{{- end -}}
{{- end -}}
{{- end -}}
