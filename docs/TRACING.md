# Tracing request

Tài liệu cho người vận hành platform: cách xem một request cụ thể đã đi qua những đâu,
bị chặn ở bước nào, và chậm ở bước nào.

---

## 1. Vấn đề mà metrics không giải được

Prometheus trả lời được "p95 latency của guardrail là bao nhiêu" và "có bao nhiêu request
bị chặn vì injection". Nó **không** trả lời được:

> Request tôi vừa bắn lúc nãy — cái bị từ chối ấy — nó chết ở đâu, và tại sao?

Lý do là metrics gộp. `guardrail_stage_duration_seconds` biết stage `retrieval` có p95 là
80ms, nhưng không biết *request nào* mất 80ms, cũng không nối được stage đó với stage
`grounding` của **cùng một request**. Khi pipeline có 10 stage nối tiếp cộng thêm 2 hop
mạng (LiteLLM → guardrail → vLLM), thông tin bị mất chính là thứ cần nhất khi debug.

Trace giải quyết đúng chỗ đó: mỗi request là một cây span, mỗi stage là một span con, và
nhìn vào biểu đồ thác nước là thấy ngay bước nào ăn hết thời gian, bước nào tô đỏ.

---

## 2. Dùng gì, xem ở đâu

| | |
|---|---|
| **Sinh trace** | `services/llm_pipeline/tracing.py` (tự viết, stdlib), LiteLLM OTEL callback |
| **Giao thức** | OTLP/HTTP + JSON |
| **Lưu trữ** | Grafana Tempo, single binary, `k8s/tracing/tempo.yaml` |
| **Xem ở đâu** | **Grafana → Explore → datasource Tempo** |

**Không mở thêm hostname, không thêm mật khẩu, không thêm cổng trên node.** Tempo chỉ có
ClusterIP; Grafana truy vấn nó từ bên trong cluster. Đây là lý do chính chọn Tempo thay
vì Jaeger — Jaeger có UI riêng, nghĩa là host thứ sáu trên ingress đang chạy `PUBLIC=1`.

### Vì sao không dùng Langfuse / Phoenix

Hai công cụ này chuyên cho LLM và nhìn hấp dẫn hơn: chúng hiển thị nguyên văn prompt và
câu trả lời, kèm chấm điểm. Đó cũng chính là lý do loại chúng.

Stage 2 của pipeline tồn tại để **bóc PII ra khỏi đúng đoạn text đó**. Đẩy nó vào một
trace backend là dựng lại chỗ rò rỉ ở nơi thứ ba, sau request log và sau cache key. Cùng
lý do với dòng comment đã có sẵn trong `services/llm_pipeline/app.py`:

```python
def log_message(self, fmt: str, *args: Any) -> None:
    # Never log request bodies: they may contain the PII step 2 exists to redact.
```

### Vì sao tự viết exporter thay vì dùng OpenTelemetry SDK

Image guardrail cố ý chỉ dùng stdlib — `class Metrics` trong `app.py` cũng là một
Prometheus registry tự viết vì cùng lý do. SDK của OpenTelemetry cộng exporter OTLP là
khoảng 20 MB dependency để gửi vài trăm byte JSON mỗi request, trên một node đã ba lần
cạn CPU. Khả năng tương thích với Tempo, LiteLLM và vLLM xảy ra ở **tầng giao thức**, nên
một encoder đúng là đủ.

Cạm bẫy: OTLP/JSON **không phải** proto3 JSON thông thường. Spec ghi đè quy tắc
bytes→base64, bắt buộc `traceId`/`spanId` là hex thường, còn số 64-bit vẫn là string. Sai
một trong hai thì Tempo **nhận payload rồi không tìm thấy span** — hỏng theo kiểu tệ nhất,
vì collector không báo lỗi gì. `tests/test_tracing.py` ghim chặt cách mã hoá này.

---

## 3. Một trace trông như thế nào

```
POST /v1/chat/completions                          (LiteLLM, span gốc)
└── POST /v1/chat/completions                      (guardrail, SERVER)
    ├── guardrail.injection_user          1   chặn chỉ dẫn ghi đè trong câu hỏi
    ├── guardrail.pii_ingress             2   bóc PII khỏi câu hỏi
    ├── guardrail.canonicalise            3   chuẩn hoá để dùng chung prefix cache
    ├── guardrail.retrieval               4   BM25
    ├── guardrail.policy                  5   access level + tài liệu hết hiệu lực
    ├── guardrail.injection_document      6   chặn injection nằm trong tài liệu
    ├── guardrail.prompt                  7   dựng prompt + datamark + cache_salt
    ├── vllm qwen2.5-7b                       (CLIENT) ← gần như toàn bộ thời gian ở đây
    ├── guardrail.grounding               8   trích dẫn có thật không
    ├── guardrail.pii_egress              9   PII rò ra trong câu trả lời
    └── guardrail.restore               10   trả lại placeholder
```

Thứ tự này khớp đúng `guardrails/pipeline.py`. Span stage được sinh ra từ **observer** mà
Minh đã thêm ở commit `e7f42ea` (`observer=_observe_stage`), nên `pipeline.py` **không
phải import gì liên quan tới tracing** — các stage của nó vẫn gọi được từ harness đánh giá
offline, nơi không có collector và cũng không cần.

### Stage bị chặn được tô đỏ

Khi request bị từ chối, span của đúng stage gây ra chuyện đó mang status `ERROR`. Nhìn vào
là thấy ngay, không cần đọc log.

Có một điểm dễ hỏng: refusal đặt tên stage là `injection`, còn observer đo nó dưới tên
`injection_user`. Bảng `_REFUSAL_TO_SPAN` trong `app.py` nối hai tên đó lại. Nếu ai xoá đi,
trace sẽ hiện một request bị từ chối mà **không span nào bị đánh dấu** — test
`test_a_refusal_marks_the_stage_that_blocked_it` tồn tại để bắt đúng trường hợp này.

---

## 4. Cách dùng

### Bật lên

```bash
make tracing-up          # cài Tempo vào namespace monitoring
```

Datasource Tempo được khai báo trong `k8s/monitoring/kps-values.yaml`, nên lần đầu cần
`make monitoring-up` để Grafana nhận. Guardrail và LiteLLM bật tracing theo mặc định của
chart; nếu đang chạy sẵn thì restart:

```bash
kubectl -n llm-serving rollout restart deploy/guardrail deploy/litellm
```

### Bắn một request và mở trace của nó

```bash
make trace
make trace Q='Doanh thu tháng trước là bao nhiêu?'
make trace MODEL=qwen2.5-1.5b
```

Lệnh này in kết quả **và in thẳng URL mở đúng trace đó trong Grafana**. Đây mới là phần
quan trọng: tracing chỉ hữu ích nếu khoảng cách từ "tôi vừa bắn request" đến "tôi đang
nhìn nó làm gì" là một cú click.

### Tự bắn bằng curl

Trace id trả về trong header `X-Trace-Id`, kể cả khi request bị từ chối (4xx) — vì đó
chính là request người ta muốn mở trace nhất:

```bash
curl -i -X POST "$BASE/v1/chat/completions" \
  -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
  -d '{"model":"qwen2.5-7b","messages":[{"role":"user","content":"..."}]}' \
  | grep -i x-trace-id
```

Nó cũng nằm trong body, ở `guardrail.trace_id`, để bench runner giữ lại được.

### Nối vào trace có sẵn

Gửi header `traceparent` theo chuẩn W3C thì guardrail nối tiếp trace đó thay vì mở trace
mới — đây là cách LiteLLM, guardrail và vLLM nằm chung **một** trace chứ không phải ba
trace rời rạc.

### Kiểm tra xem có thật sự chạy không

```bash
make tracing-check
```

Đọc cả hai đầu: số span guardrail đã gửi, và số span Tempo đã nhận. Cần thiết vì tracing
hỏng hoàn toàn im lặng (xem mục 6).

---

## 5. Span chứa gì, và cố ý không chứa gì

**Quy tắc: span mang quyết định, định danh và con số. Span không bao giờ mang nội dung
prompt, nội dung tài liệu, hay nội dung câu trả lời.**

Thuộc tính trên span gốc:

| Thuộc tính | Ví dụ |
|---|---|
| `guardrail.outcome` | `allowed` \| `refused` \| `error` |
| `guardrail.terminal_stage` | `none` \| `injection` \| `grounding` … |
| `guardrail.agent`, `guardrail.model` | `moc-analytics`, `qwen2.5-7b` |
| `rag.documents.retrieved` / `.denied` / `.stale` / `.dropped_injection` | `5` / `2` / `0` / `1` |
| `guardrail.pii.ingress.count` / `.kinds` | `1` / `cccd` |
| `guardrail.grounding.verdict` / `.overlap` | `ok` / `0.6231` |
| `guardrail.citations.fabricated` | `0` |
| `prompt.cache_salt` | `a3f1…` |
| `llm.usage.prompt_tokens` / `.completion_tokens` | `812` / `96` |

Chú ý `guardrail.pii.ingress.kinds`: ghi **loại** PII đã bóc (`cccd`), không ghi giá trị.
Biết rằng một CCCD đã bị che là toàn bộ giá trị chẩn đoán; biết CCCD nào thì là xoá bỏ
chính việc che đó.

Quy tắc này được ép bằng cấu trúc chứ không phải bằng quy ước: `Span.set()` chỉ nhận kiểu
nguyên thuỷ, nên không thể vô tình gắn nguyên một chunk đã truy xuất vào span. Và
`test_no_span_ever_carries_the_content_it_was_asked_to_redact` bắn một request chứa CCCD
hợp lệ rồi **quét toàn bộ byte đã export** để chắc chắn số đó không có mặt.

### Cảnh báo về LiteLLM

Callback OTEL của LiteLLM **mặc định đính nguyên văn prompt vào span**. Đã kiểm chứng
trong mã nguồn v1.90.2:

```python
# litellm/integrations/opentelemetry.py:1751
attrs["gen_ai.prompt"] = msg["content"]
```

Thứ chặn nó là `litellm_settings.turn_off_message_logging: true`, vốn đã được đặt sẵn
trong `charts/litellm/templates/configmap.yaml`. **Không bao giờ bật `"otel"` trong
`callbacks` mà thiếu cờ này.** ConfigMap có comment ghi rõ điều đó ngay tại chỗ.

---

## 6. Vận hành

### Chi phí

Một pod, `requests: 50m CPU / 192Mi`, nằm trên node tooling. Không có EBS volume, không có
load balancer, **không phát sinh chi phí AWS nào**. Chart Helm `grafana/tempo` đã bị đánh
dấu `deprecated`, còn `tempo-distributed` tách thành 6 thành phần và không vừa node
m7i.large — nên ở đây là manifest tự viết, giống cách đã làm với
`k8s/monitoring/servicemonitor-dcgm.yaml`.

### Trace không sống qua `lab-down`

Storage là `emptyDir`. Có chủ đích:

- Trace dùng để trả lời "tôi vừa bắn request xong, nó đi đâu" — đọc sau vài phút. Bản ghi
  lâu dài của một phiên là metric snapshot mà `make snapshot` đẩy lên S3 trước khi huỷ.
- PVC nghĩa là thêm một EBS volume, mà dự án này **đã từng bị tính tiền cho volume mồ
  côi** sống lâu hơn cluster sinh ra nó.
- Backend S3 thì chạy được, nhưng cần IRSA role, bucket policy và sửa Terraform tầng
  cluster — cho dữ liệu có vòng đời một buổi chiều.

Muốn đổi sau này: sửa `storage.trace.backend` thành `s3` trong `k8s/tracing/tempo.yaml` và
gán annotation role cho ServiceAccount. Không phải sửa gì khác.

### Tracing hỏng im lặng — và cách phát hiện

Exporter nuốt mọi lỗi kết nối, có chủ đích: Tempo chết không được phép làm chậm serving.
Hệ quả là khi hỏng, **không có log, không có lỗi** — chỉ là trace không bao giờ xuất hiện,
thường phát hiện ra giữa lúc đang debug việc khác.

Vì vậy có `guardrail_trace_spans_total{result=exported|dropped|failed}` và hai alert trong
nhóm `tracing` của `observability/rules/alerts.yaml`. Đáng chú ý là `dropped` **đáng lo
hơn** `failed`: hàng đợi đầy nghĩa là trace vẫn có nhưng đã thành mẫu thiên lệch — request
chậm chính là request dễ bị bỏ nhất.

---

## 7. Phần chưa xong

**vLLM chưa được xác minh.** Guardrail đã gửi header `traceparent` sang vLLM, và vLLM có
cờ `--otlp-traces-endpoint`, nhưng **chưa kiểm chứng được image
`vllm/vllm-openai:v0.29.0` có sẵn `opentelemetry-sdk` hay không** — cần cluster chạy mới
kiểm tra được. Nếu thiếu, engine bỏ qua header và không có gì hỏng.

Hiện tại thời gian của engine **vẫn hiện trong waterfall**, đo từ phía guardrail bằng span
`vllm <model>`. Cái thiếu là chi tiết bên trong engine (thời gian nằm hàng đợi, prefill so
với decode).

Khi cluster lên, kiểm tra bằng:

```bash
kubectl -n inference exec deploy/vllm-a -- python3 -c "import opentelemetry; print('co')"
```

Nếu có, thêm vào `extraArgs` của chart vLLM (chart đã hỗ trợ sẵn, không cần sửa gì):

```yaml
extraArgs:
  - --otlp-traces-endpoint=http://tempo.monitoring.svc.cluster.local:4317
```

Việc còn lại:

- Exemplar nối từ biểu đồ Prometheus sang trace (một cú click từ "p95 tăng" sang "đây là
  request chậm"). Cần Prometheus bật `--enable-feature=exemplar-storage`.
- Service graph của Tempo cần metrics-generator, hiện đang tắt để tiết kiệm bộ nhớ.
