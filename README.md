# vllm-bench

Nền tảng serving LLM dùng chung cho các copilot MOC: vLLM đa mô hình trên AWS EKS, một
gateway tương thích OpenAI, guardrail đọc nội dung ở cả hai chiều, và bộ đo tải trả lời
được bằng số liệu tự đo chứ không bằng ước lượng.

Đề tài **DA#51** · Nguyễn Gia Bảo, Nguyễn Lê Minh · Mentor: Phạm Duy Tùng

---

## Kết quả đo được

Cập nhật 02/10/2026. Phương pháp và dữ liệu thô: [`docs/acceptance-report.md`](docs/acceptance-report.md).

| | Tiêu chí | Kết quả | |
|---|---|---|---|
| **TC1a** | p95 < 3s ở 50 req/s | 40 req/s @ p95 2029ms trên 3× A10G | ⚠️ |
| **TC1b** | Uptime ≥ 99,5% | cận dưới 99,5946% ở 40 req/s (n=12.001) | ✅ |
| **TC2** | Chi phí/1k token giảm ≥30% | đường cong theo tải; hoà vốn ~1,5 req/s | ⚠️ |
| **TC3** | Chặn ≥95% injection/PII | 100% offline (294 mẫu), 100% live (129 mẫu) | ✅ |
| **TC4** | ≥5 DA chạy trên nền tảng | 0/7 đề án đã tích hợp; key và client đã sẵn | ⚠️ |

TC1a và TC2 không phải bài toán kỹ thuật chưa giải được — chúng là **quyết định về tài
nguyên**. TC1a thiếu một GPU trong hạn mức 16 vCPU; TC2 là ngưỡng tải chứ không phải một
con số cố định.

---

## Kiến trúc

```
Client → Ingress → LiteLLM ──→ Guardrail ──→ vLLM
                   gateway      RAG + kiểm       engine
                   key, quota   tra 2 chiều      suy luận
                                     │
                                Redis cache
```

**LiteLLM** giữ API key, hạn mức và định tuyến mô hình. **Guardrail** là nơi duy nhất đọc
nội dung: phát hiện prompt injection, che PII tiếng Việt, truy hồi tài liệu, dựng prompt,
rồi kiểm tra trích dẫn và quét PII đầu ra trước khi trả về. **vLLM** chỉ suy luận.

Mô hình do **người gọi chọn** qua trường `model`, không có router đoán thay. Tách như vậy
để mỗi tầng hỏng theo cách riêng của nó và quan sát được riêng.

Hạ tầng chia **ba tầng Terraform** — `core` (mạng, S3, ECR), `data` (Aurora), `cluster`
(EKS, node group) — để huỷ cụm tính toán mỗi tối mà không mất dữ liệu hay bằng chứng đo.

---

## Bắt đầu nhanh

```bash
make help                 # mọi lệnh, kèm mô tả
make lab-up               # dựng tầng cluster, GPU vẫn ở 0
make kubeconfig
make monitoring-up        # GPU operator, Prometheus, Grafana
make gpu n=4              # bật node GPU khi đã sẵn sàng đo
make vllm-up MODE=split REPLICAS_A=3 REPLICAS_B=1
make litellm-up
make webui-up PILOT_KEY=sk-...   # chat UI cho pilot, key lấy từ `make agent-keys`
make ingress-up           # publish UI, giới hạn theo IP của bạn
```

Bắn tải — ở trên vài req/s **bắt buộc** chạy trong cụm, vì đường truyền từ laptop nằm
thẳng trong p95:

```bash
make load-incluster SCENARIO=slo  MODEL=qwen2.5-7b   # tiêu chí đề bài, đạt/không đạt
make load-incluster SCENARIO=ramp MODEL=qwen2.5-7b   # tìm điểm gãy
```

Kết thúc phiên:

```bash
make lab-down             # xuất số liệu lên S3, hạ node về 0, rồi destroy
```

> **Chi phí.** Bốn node GPU là ~4 USD/giờ. `lab-down` xuất số đo lên S3 **trước** khi xoá
> gì — chạy nó, đừng gõ `terraform destroy` tay. Ngân sách đề tài là 200 USD.

---

## Cấu trúc repo

| | |
|---|---|
| `terraform/` | ba tầng: `core`, `data`, `cluster` |
| `charts/` | Helm chart cho vllm, litellm, guardrail |
| `services/llm_pipeline/` | guardrail — OpenAI-compatible, RAG, kiểm tra hai chiều |
| `guardrails/`, `prompt/`, `rag/` | phát hiện tấn công, dựng prompt, truy hồi |
| `bench/` | bộ k6, dataset, script phân tích |
| `bench/agents_sim/` | bảy consumer **mô phỏng** — không phải bằng chứng TC4 |
| `clients/python/` | client mẫu cho các đề án dùng nền tảng |
| `observability/` | recording rule và dashboard Grafana |
| `k8s/` | ingress, GPU operator, tracing |
| `docs/` | báo cáo, runbook, kiến trúc |
| `reports/` | báo cáo tiến độ theo tuần |

---

## Tài liệu

| | |
|---|---|
| [`docs/acceptance-report.md`](docs/acceptance-report.md) | kết quả nghiệm thu, phương pháp bên cạnh từng con số |
| [`docs/runbook.md`](docs/runbook.md) | thao tác vận hành, biến Terraform, **bẫy đã gặp** |
| [`docs/OVERVIEW.md`](docs/OVERVIEW.md) | kiến trúc và lý do các lựa chọn kỹ thuật |
| [`docs/GUARDRAILS.md`](docs/GUARDRAILS.md) | mô hình đe doạ và từng tầng kiểm tra |
| [`docs/k6-load-testing.md`](docs/k6-load-testing.md) | thiết kế bộ đo và cách đọc kết quả |
| [`docs/ha-serving.md`](docs/ha-serving.md) | profile 3 replica với Redis dùng chung |
| [`docs/completion-plan.md`](docs/completion-plan.md) | việc còn lại tới nghiệm thu |
| [`docs/pilot-guide.html`](docs/pilot-guide.html) | trang phát cho analyst trước buổi dùng thử |
| [`clients/python/README.md`](clients/python/README.md) | hướng dẫn tích hợp cho DA#19/20/32/39/41/44/45 |

Đọc `docs/runbook.md` **trước** lần dựng đầu tiên. Phần "Bẫy đã biết" ở cuối file ghi
những thứ chỉ lộ ra sau khi đã mất vài giờ — quota GPU mặc định bằng 0, IP nhà đổi làm
treo `kubectl` mà không báo lỗi quyền, GPU Operator cài đè driver của AMI.

---

## Kiểm thử

```bash
python -m pytest tests/ -q   # 119 ca: pipeline, guardrail, cache, tracing, availability
make guardrails-test         # kiểm thử hành vi PII, injection, policy, grounding, cache
make attacks-score           # chấm bộ đối kháng. FOLD=B tách kỹ thuật chưa từng thấy
make pii-verify              # 5 kiểm tra độc lập rằng PII thật sự bị che
make rag-eval                # truy hồi trên 144 câu hỏi vàng
```
