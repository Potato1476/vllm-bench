# Metrics của LiteLLM và Guardrail

Tài liệu này là hợp đồng telemetry giữa image đang chạy, Prometheus recording rules và
dashboard Grafana. Phạm vi gồm LiteLLM `v1.90.2` và Guardrail service của repository này.

Luồng thu thập:

```text
LiteLLM /metrics ─┐
                  ├─> Prometheus ─> recording rules ─> Grafana / alerts
Guardrail /metrics┘
```

Grafana không nhận metric trực tiếp từ ứng dụng. Hai `ServiceMonitor` đọc `/metrics`,
Prometheus lưu chuỗi thời gian, sau đó dashboard truy vấn raw metric hoặc recording
metric trong `observability/rules/recording.yaml`.

## 1. Quy ước

### Loại metric

| Loại | Cách sử dụng |
|---|---|
| Counter | Chỉ tăng. Dùng `rate(...[5m])` để tính tốc độ hoặc tỷ lệ lỗi. |
| Gauge | Giá trị tức thời, ví dụ số request đang chạy. |
| Histogram | Phân bố quan sát. Dùng `_bucket` và `histogram_quantile()` để tính p50/p95/p99. |

Python Prometheus client tự thêm:

- `_total` vào sample của Counter;
- `_bucket`, `_sum`, `_count` vào sample của Histogram.

Ví dụ, metric được LiteLLM khai báo là `litellm_spend_metric` nhưng tên sample cần query
là `litellm_spend_metric_total`.

### Quy tắc label

Metric không được chứa raw prompt, raw response, giá trị PII, document ID hoặc
`request_id`. Các giá trị này vừa nhạy cảm vừa tạo cardinality không giới hạn.

Guardrail chỉ dùng các label hữu hạn. Cấu hình LiteLLM lọc bỏ `client_ip`, `user_agent`
và raw `user`. `end_user` phục vụ acceptance metric theo agent nhưng bị giới hạn 100
series/metric và TTL một giờ; `team` là chiều ổn định để phân bổ theo DA/virtual key.
Trên LiteLLM `v1.90.2`, trường OpenAI `user` không tự điền `end_user`; attribution theo
agent cần virtual key/Aurora có end-user identity tương ứng.

## 2. Guardrail raw metrics

### 2.1 Request và lỗi

#### `guardrail_requests_total`

| Thuộc tính | Giá trị |
|---|---|
| Type | Counter |
| Labels | `outcome`, `stage`, `model` |

Đếm mọi request completion kết thúc tại Guardrail.

`outcome`:

| Giá trị | Ý nghĩa |
|---|---|
| `allowed` | Request và câu trả lời vượt qua toàn bộ kiểm tra. |
| `refused` | Guardrail chủ động từ chối vì policy hoặc safety. Đây không phải lỗi hệ thống. |
| `error` | Request không hợp lệ hoặc Guardrail/upstream gặp lỗi. |

`stage` là điểm kết thúc request:

| Stage | Ý nghĩa |
|---|---|
| `none` | Request thành công. |
| `injection` | Input chứa prompt injection và bị chặn. |
| `retrieval` | Không còn tài liệu phù hợp sau retrieval và policy. |
| `known_answer` | Context truy hồi làm detector phát hiện mô hình bị điều hướng. |
| `grounding` | Câu trả lời thiếu căn cứ hoặc có citation giả. |
| `pii_egress` | Câu trả lời chứa PII và bị chặn trước khi trả ra ngoài. |
| `request` | JSON, model hoặc request contract không hợp lệ. |
| `upstream` | vLLM lỗi, timeout hoặc không truy cập được. |
| `internal` | Lỗi không dự kiến trong Guardrail service. |

`model` chỉ nhận model có trong `MODEL_ROUTES_JSON`; model tùy ý từ caller được gom vào
`unknown` để tránh tạo series vô hạn.

Ví dụ request rate:

```promql
sum by (model, outcome) (rate(guardrail_requests_total[5m]))
```

#### `guardrail_in_flight_requests`

| Thuộc tính | Giá trị |
|---|---|
| Type | Gauge |
| Labels | Không có |

Số request completion đang được Guardrail xử lý trong process. Giá trị tăng kéo dài cùng
với latency thường cho thấy pod Guardrail hoặc upstream đang bị nghẽn.

#### `guardrail_build_info`

| Thuộc tính | Giá trị |
|---|---|
| Type | Gauge, luôn bằng `1` |
| Labels | `version`, `policy_version`, `corpus_version` |

Gắn lineage của image, policy và corpus vào dữ liệu benchmark. Metric này cũng được dùng
để phát hiện trường hợp target còn `up=1` nhưng application metrics bị mất.

#### `guardrail_trace_spans_total`

| Thuộc tính | Giá trị |
|---|---|
| Type | Counter |
| Labels | `result` = `exported` \| `dropped` \| `failed` |

Chỉ xuất hiện khi tracing được bật (`OTEL_EXPORTER_OTLP_ENDPOINT` có giá trị).

Exporter trong `services/llm_pipeline/tracing.py` cố tình nuốt mọi lỗi kết nối: một
Tempo chết không được phép làm chậm request. Hệ quả là tracing hỏng **hoàn toàn im
lặng** — không log, không lỗi, chỉ là trace không bao giờ xuất hiện. Ba counter này là
cách duy nhất để thấy điều đó từ dashboard.

- `failed` tăng → không gửi được tới Tempo (Tempo chưa chạy, sai endpoint, network).
- `dropped` tăng → hàng đợi đầy, exporter không theo kịp. Đáng lo hơn `failed` vì trace
  *vẫn* có, nhưng đã thành mẫu thiên lệch: request chậm là request dễ bị bỏ nhất.

Hai alert tương ứng nằm trong nhóm `tracing` của `observability/rules/alerts.yaml`.

### 2.2 Latency

#### `guardrail_request_duration_seconds`

| Thuộc tính | Giá trị |
|---|---|
| Type | Histogram |
| Labels | `outcome`, `model` |

Thời gian end-to-end của Guardrail hop, từ lúc nhận request đến lúc chuẩn bị response.
Giá trị này **bao gồm** thời gian gọi vLLM.

#### `guardrail_processing_duration_seconds`

| Thuộc tính | Giá trị |
|---|---|
| Type | Histogram |
| Labels | `outcome`, `model` |

Thời gian Guardrail tự xử lý, được tính bằng tổng thời gian trừ thời gian chờ vLLM. Đây
là metric dùng để kiểm tra ngân sách Guardrail p95 dưới 150 ms.

#### `guardrail_upstream_duration_seconds`

| Thuộc tính | Giá trị |
|---|---|
| Type | Histogram |
| Labels | `model` |

Thời gian Guardrail chờ HTTP call tới vLLM. So sánh metric này với
`guardrail_processing_duration_seconds` để phân biệt inference chậm với Guardrail chậm.

#### `guardrail_stage_duration_seconds`

| Thuộc tính | Giá trị |
|---|---|
| Type | Histogram |
| Labels | `stage` |

Đo thời gian từng bước nội bộ:

| Stage | Công việc được đo |
|---|---|
| `injection_user` | Quét prompt injection trực tiếp. |
| `pii_ingress` | Tìm và redact PII đầu vào. |
| `canonicalise` | Chuẩn hóa query và scope. |
| `retrieval` | BM25/hybrid retrieval. |
| `policy` | Lọc quyền truy cập và tài liệu hết hiệu lực. |
| `injection_document` | Quét injection trong context truy hồi. |
| `known_answer` | Detector bổ sung, nếu được bật. |
| `prompt` | Xây system/user prompt và cache salt. |
| `grounding` | Kiểm tra citation và lexical grounding. |
| `pii_egress` | Quét PII trong câu trả lời. |
| `restore` | Khôi phục placeholder do chính caller cung cấp. |

Ví dụ p95 từng stage:

```promql
histogram_quantile(
  0.95,
  sum by (le, stage) (rate(guardrail_stage_duration_seconds_bucket[5m]))
)
```

### 2.3 Upstream vLLM

#### `guardrail_upstream_requests_total`

| Thuộc tính | Giá trị |
|---|---|
| Type | Counter |
| Labels | `model`, `outcome="success|error"` |

Đếm call từ Guardrail tới vLLM. Request bị input guardrail từ chối sẽ không tăng metric
này vì chưa gọi inference.

### 2.4 PII và prompt injection

#### `guardrail_pii_findings_total`

| Thuộc tính | Giá trị |
|---|---|
| Type | Counter |
| Labels | `direction`, `kind`, `action` |

Đếm finding PII mà không xuất giá trị thật.

- `direction="ingress"`, `action="redact"`: PII từ caller đã được thay placeholder.
- `direction="egress"`, `action="block"`: PII trong output đã làm response bị chặn.
- `kind`: `email`, `cccd`, `phone`, `plate`, `cmnd`, `tax_id`, `passport`.

#### `guardrail_injection_detections_total`

| Thuộc tính | Giá trị |
|---|---|
| Type | Counter |
| Labels | `source`, `action` |

- `source="user"`, `action="block"`: chặn toàn bộ request do injection trực tiếp.
- `source="document"`, `action="drop"`: loại document nhiễm injection nhưng tiếp tục
  request bằng context an toàn còn lại.

### 2.5 Retrieval, policy và grounding

#### `guardrail_documents_retrieved`

| Thuộc tính | Giá trị |
|---|---|
| Type | Histogram |
| Labels | `model` |

Số document thực sự được đưa vào prompt cho mỗi request, sau policy và document injection
screening. Đây không phải tổng số candidate ban đầu của retrieval.

#### `guardrail_documents_dropped_total`

| Thuộc tính | Giá trị |
|---|---|
| Type | Counter |
| Labels | `reason` |

| Reason | Ý nghĩa |
|---|---|
| `access` | Caller không có quyền xem document. |
| `stale` | Document deprecated/draft bị loại theo policy hiện hành. |
| `injection` | Document chứa chỉ dẫn nguy hiểm. |

#### `guardrail_grounding_verdicts_total`

| Thuộc tính | Giá trị |
|---|---|
| Type | Counter |
| Labels | `verdict="ok|flag|block"` |

- `ok`: citation hợp lệ và overlap đạt yêu cầu.
- `flag`: response được phép trả nhưng overlap thấp hoặc có câu khẳng định thiếu citation.
- `block`: không có citation bắt buộc hoặc citation không nằm trong context.

#### `guardrail_grounding_overlap_ratio`

| Thuộc tính | Giá trị |
|---|---|
| Type | Histogram |
| Labels | `verdict` |

Tỷ lệ từ nội dung của answer xuất hiện trong evidence được trích dẫn, miền `[0,1]`. Đây
chỉ là tín hiệu screening, không phải điểm factuality: câu “tăng 5%” và “giảm 5%” có thể
có overlap giống nhau.

#### `guardrail_citations_total`

| Thuộc tính | Giá trị |
|---|---|
| Type | Counter |
| Labels | `kind="valid|fabricated|missing"` |

Đếm citation hợp lệ, citation không có trong context và citation bị thiếu. Một request có
thể tăng counter nhiều lần nếu answer chứa nhiều citation hoặc nhiều câu thiếu citation.

## 3. LiteLLM raw metrics

Các tên dưới đây đã được đối chiếu với source của image pin `v1.90.2`.

### 3.1 Traffic và lỗi phía caller

#### `litellm_proxy_total_requests_metric_total`

| Thuộc tính | Giá trị |
|---|---|
| Type | Counter |
| Labels giữ lại | `end_user`, `requested_model`, `team`, `status_code`, `route` |

Tổng request mà LiteLLM nhận. Đây là mẫu số cho request rate và caller-facing error
ratio vì nó bao gồm request chưa bao giờ tới Guardrail/vLLM.

#### `litellm_proxy_failed_requests_metric_total`

| Thuộc tính | Giá trị |
|---|---|
| Type | Counter |
| Labels giữ lại | `end_user`, `requested_model`, `team`, `exception_status`, `exception_class`, `route` |

Đếm response thất bại do authentication, quota, routing, timeout, Guardrail hoặc vLLM.
Kết hợp với total request để tính error ratio.

### 3.2 Latency và concurrency

| Metric | Type | Labels chính | Ý nghĩa |
|---|---|---|---|
| `litellm_request_total_latency_metric` | Histogram | `end_user`, `requested_model`, `model`, `team` | End-to-end từ khi request vào LiteLLM đến khi hoàn tất post-call processing. |
| `litellm_llm_api_latency_metric` | Histogram | `end_user`, `requested_model`, `model`, `team` | Thời gian LiteLLM chờ API upstream. Với kiến trúc này upstream là toàn bộ Guardrail → vLLM → output guardrail. |
| `litellm_llm_api_time_to_first_token_metric` | Histogram | `end_user`, `requested_model`, `model`, `team` | TTFT mà LiteLLM quan sát từ upstream. Guardrail hiện buffer toàn bộ answer trước khi phát SSE, nên metric này không phải TTFT token thật của vLLM. |
| `litellm_request_queue_time_seconds` | Histogram | `end_user`, `requested_model`, `model`, `team` | Thời gian request nằm trong queue trước khi được xử lý. |
| `litellm_overhead_latency_metric` | Histogram | `model_group`, `litellm_model_name`, `api_provider` | Overhead riêng của LiteLLM, không gồm thời gian upstream; đơn vị được export là giây. |
| `litellm_in_flight_requests` | Gauge | Không có | Số HTTP request đang chạy trong LiteLLM worker/pod. |

Để lấy histogram sample trong PromQL, thêm `_bucket`, ví dụ
`litellm_request_total_latency_metric_bucket`.

### 3.3 Routing và deployment

| Metric | Type | Ý nghĩa |
|---|---|---|
| `litellm_deployment_total_requests_total` | Counter | Tổng call mà router gửi tới deployment. |
| `litellm_deployment_success_responses_total` | Counter | Call deployment thành công. |
| `litellm_deployment_failure_responses_total` | Counter | Call deployment lỗi; có `exception_status` và `exception_class`. |
| `litellm_deployment_state` | Gauge | `0`: healthy, `1`: partial outage, `2`: complete outage. |
| `litellm_deployment_latency_per_output_token` | Histogram | Latency trên mỗi output token của deployment. |

Các counter deployment giữ `requested_model`, `litellm_model_name`, `team`; failure giữ
thêm loại lỗi. State giữ `litellm_model_name` và `api_provider`.

### 3.4 Token và spend

| Metric | Type | Ý nghĩa |
|---|---|---|
| `litellm_input_tokens_metric_total` | Counter | Tổng input token. |
| `litellm_output_tokens_metric_total` | Counter | Tổng output token. |
| `litellm_total_tokens_metric_total` | Counter | Input + output token. |
| `litellm_spend_metric_total` | Counter | Spend do LiteLLM tính từ model pricing. |

Trong `v1.90.2`, token metrics có cả `requested_model` và `model`, nhưng spend có `model`
và không có `requested_model`. Vì vậy phép tính cost/1.000 token phải join theo `model`:

```promql
1000
* sum by (model) (rate(litellm_spend_metric_total[1h]))
/
clamp_min(sum by (model) (rate(litellm_total_tokens_metric_total[1h])), 1)
```

Spend chỉ có ý nghĩa khi LiteLLM có pricing cho model nội bộ. Chi phí GPU/EKS/CPU thực tế
vẫn phải được tính riêng trong báo cáo benchmark.

### 3.5 Aurora/PostgreSQL

Các metric này được tạo bởi `service_callback: ["prometheus_system"]`:

| Metric | Type | Ý nghĩa |
|---|---|---|
| `litellm_postgres_latency` | Histogram | Latency các thao tác PostgreSQL/Aurora. |
| `litellm_postgres_total_requests_total` | Counter | Tổng thao tác PostgreSQL. |
| `litellm_postgres_failed_requests_total` | Counter | Thao tác PostgreSQL lỗi, có `error_class` và `function_name`. |

Dashboard và rules query histogram qua `litellm_postgres_latency_bucket`.

## 4. Recording metrics

Recording rule giúp dashboard không phải tính lại quantile trên raw buckets ở mỗi lần
refresh.

### LiteLLM

| Recording metric | Ý nghĩa |
|---|---|
| `litellm:request_rate:rate1m` | Request/giây theo model. |
| `litellm:error_ratio:rate2m` | Tỷ lệ request thất bại phía caller. |
| `litellm:e2e_seconds:p50` | E2E median. |
| `litellm:e2e_seconds:p95` | E2E p95 dùng cho SLO. |
| `litellm:e2e_seconds:p99` | E2E tail latency p99. |
| `litellm:llm_api_seconds:p95` | Upstream API p95 mà LiteLLM quan sát. |
| `litellm:ttft_seconds:p95` | Upstream TTFT p95 mà LiteLLM quan sát. |
| `litellm:queue_seconds:p95` | Queue latency p95. |
| `litellm:overhead_seconds:p95` | LiteLLM-only overhead p95. |
| `litellm:deployment_error_ratio:rate2m` | Tỷ lệ deployment call lỗi. |
| `litellm:deployment_seconds_per_output_token:p95` | Deployment latency/output token p95. |
| `litellm:input_tokens:rate1m` | Input token/giây. |
| `litellm:output_tokens:rate1m` | Output token/giây. |
| `litellm:postgres_error_ratio:rate5m` | Tỷ lệ thao tác PostgreSQL lỗi. |
| `litellm:postgres_seconds:p95` | PostgreSQL latency p95. |
| `agent:request_rate:rate1m` | Request/giây theo `end_user` và model. |
| `agent:error_ratio:rate5m` | Tỷ lệ lỗi theo `end_user`. |
| `agent:e2e_seconds:p95` | E2E p95 theo `end_user`. |
| `agent:output_tokens:rate5m` | Output token/giây theo `end_user`. |

### Guardrail

| Recording metric | Ý nghĩa |
|---|---|
| `guardrail:request_rate:rate1m` | Request/giây theo model và outcome. |
| `guardrail:block_ratio:rate5m` | Tỷ lệ request bị policy/safety từ chối. |
| `guardrail:error_ratio:rate5m` | Tỷ lệ lỗi hệ thống/request. |
| `guardrail:e2e_seconds:p95` | Tổng Guardrail hop p95, gồm vLLM. |
| `guardrail:processing_seconds:p95` | Guardrail-only p95, không gồm vLLM. |
| `guardrail:upstream_seconds:p95` | Thời gian chờ vLLM p95. |
| `guardrail:stage_seconds:p95` | p95 từng stage Guardrail. |
| `guardrail:upstream_error_ratio:rate5m` | Tỷ lệ call Guardrail → vLLM lỗi. |

### Chi phí platform

| Recording metric | Ý nghĩa |
|---|---|
| `platform:cost_usd_per_hour` | Giá cố định theo giờ của EKS control plane, tooling node và GPU node. |
| `platform:output_tokens_per_hour` | Output token/giờ từ vLLM. |
| `platform:cost_usd_per_1k_output_tokens` | Chi phí platform trên 1.000 output token. |
| `platform:breakeven_output_tokens_per_hour` | Throughput cần đạt để hòa vốn với giá API tham chiếu. |

## 5. Cách đọc dashboard

Dashboard `observability/dashboards/gateway-guardrail.json` được đọc từ trên xuống:

1. **Caller SLI:** người dùng đang thấy bao nhiêu traffic, lỗi và latency?
2. **Latency decomposition:** thời gian nằm ở LiteLLM, Guardrail processing hay vLLM?
3. **Guardrail decisions:** request bị block/redact/drop vì lý do nào?
4. **Dependencies:** router/deployment và Aurora có khỏe không?
5. **Usage:** token, spend và phân bổ theo `team`/model.

Một số cách diễn giải:

- E2E cao, Guardrail processing thấp, upstream cao: điều tra vLLM/queue/GPU.
- E2E cao, processing cao, retrieval stage cao: điều tra CPU/index/corpus Guardrail.
- LiteLLM overhead hoặc queue cao nhưng Guardrail thấp: gateway pod đang nghẽn.
- Error ratio cao nhưng Guardrail error thấp: điều tra auth, quota hoặc LiteLLM routing.
- Refusal ratio cao nhưng error ratio thấp: hệ thống vẫn khỏe; traffic đang vi phạm policy
  hoặc bộ rule có false positive.
- PostgreSQL latency/error cao: virtual key, budget và spend accounting có thể ảnh hưởng
  trực tiếp tới request path.

## 6. Kiểm tra sau triển khai

```bash
make litellm-smoke
make audit-metrics
```

Kiểm tra target trước:

```promql
up{namespace="llm-serving", service=~"litellm-private|guardrail"}
```

Guardrail seed zero-valued series khi khởi động nên metric của nó phải hiện cả lúc idle.
Phần lớn LiteLLM series chỉ được tạo sau request hoặc database operation đầu tiên;
`audit-metrics` báo `WAITING` thay vì fail cho các metric phụ thuộc traffic.

Tên vLLM/DCGM vẫn phụ thuộc runtime. Sau mỗi lần đổi image tag, phải chạy audit trên live
cluster trước khi dùng số liệu benchmark.
