# vllm-bench

Nền tảng serving LLM dùng chung cho các copilot MOC: vLLM đa mô hình trên AWS EKS, một
gateway tương thích OpenAI, guardrail đọc nội dung ở cả hai chiều, và bộ đo tải trả lời
được bằng số liệu tự đo chứ không bằng ước lượng.

Đề tài **DA#51** · Nguyễn Gia Bảo, Nguyễn Lê Minh · Mentor: Phạm Duy Tùng

---

## Kết quả đo được

Cập nhật 05/10/2026. Phương pháp và dữ liệu thô: [`docs/acceptance-report.md`](docs/acceptance-report.md).

| | Tiêu chí | Kết quả | |
|---|---|---|---|
| **TC1a** | p95 < 3s ở 50 req/s | **p95 2047ms ở 50 req/s**, 15.001 request, 4× A10G | ✅ |
| **TC1b** | Uptime ≥ 99,5% | cận dưới 99,5946% (n=12.001) — chưa phủ 14 ngày | ⚠️ |
| **TC2** | Chi phí/1k token giảm ≥30% | đạt từ ~2,25 req/s duy trì; hoà vốn ~1,5 | ⚠️ |
| **TC3** | Chặn ≥95% injection/PII | 100% offline (294 mẫu), 100% live (129 mẫu) | ✅ |
| **TC4** | ≥5 DA chạy trên nền tảng | 0/7 đề án đã tích hợp | ⚠️ |

**TC1a** đo trực tiếp ở đúng mức tải đề bài, không ngoại suy: 5 phút, cache ngữ nghĩa
**tắt có chủ ý** để đo engine chứ không đo Redis, 0 lỗi. Cấu hình là `MODE=solo-a` —
cả bốn card phục vụ 7B. Phục vụ đồng thời 1.5B thì còn ít card hơn cho 7B và trần
thông lượng thấp hơn tương ứng.

**TC1b** đã chứng minh *tỷ lệ*, chưa chứng minh *thời lượng*. Phán quyết đọc theo cận
dưới Clopper-Pearson chứ không theo điểm ước lượng. Đề bài yêu cầu pilot 2 tuần; chạy
liên tục 2 tuần tốn ~338 USD trên ngân sách 200, nên
[`bench/scripts/availability.py`](bench/scripts/availability.py) tách tiêu chí thành ba
mệnh đề và chỉ mệnh đề thứ ba cần thời gian — mà nó cần **phủ lịch**, không cần uptime
liên tục. 14 phiên hằng ngày phủ 14 ngày *và* 14 lần triển khai.

**TC2** là một đường cong, không phải một con số: tự vận hành trả tiền **thời gian thuê**,
API trả tiền **token**, nên tỷ số giữa chúng chỉ là hàm của mức sử dụng.
Xem [`reports/images/tc2-savings.png`](reports/images/tc2-savings.png).

**TC4** đếm **người dùng**, không đếm cơ chế. Đề bài ghi nền tảng này phục vụ
DA#19/#20/#32/#39/#41/#44/#45, nên tiêu chí cần ít nhất 5 đội trong số đó thật sự gọi
vào. Key, hạn mức và client mẫu đã sẵn sàng; phần còn lại không nằm ở code.

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

### Hai profile guardrail

| Tên model | Truy hồi | Bắt buộc trích dẫn | Chặn injection · che PII |
|---|---|---|---|
| `qwen2.5-7b`, `qwen2.5-1.5b` | ✅ | ✅ | ✅ |
| `qwen2.5-7b-plain`, `qwen2.5-1.5b-plain` | ❌ | ❌ | ✅ |

Copilot MOC cần trích dẫn; một đề án làm phân loại hay trích xuất thì không có tài liệu
nào để dẫn và sẽ bị chặn trên mọi request. Profile đi theo **tên model** chứ không theo
trường trong request, vì LiteLLM đã cưỡng chế sẵn danh sách model mỗi key được gọi — ranh
giới phân quyền có sẵn thành ranh giới profile, không phát sinh cơ chế mới để làm sai.

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
make vllm-up MODE=solo-a  # 4 card cho 7B — cấu hình đã đạt TC1a
make litellm-up
make agent-keys           # virtual key cho từng đề án, từ bench/agents.json
```

Chạy pilot với người thật:

```bash
make webui-up PILOT_KEY=sk-...   # chat UI, key từ `make agent-keys`
make tunnel-up                   # URL HTTPS cố định, không đổi khi dựng lại cụm
```

Bắn tải — ở trên vài req/s **bắt buộc** chạy trong cụm, vì đường truyền từ laptop nằm
thẳng trong p95:

```bash
make load-incluster SCENARIO=slo  MODEL=qwen2.5-7b   # tiêu chí đề bài, đạt/không đạt
make load-incluster SCENARIO=ramp MODEL=qwen2.5-7b   # tìm điểm gãy
```

Kết thúc phiên:

```bash
make webui-export         # câu hỏi của pilot CHỈ nằm ở đây, xuất trước khi huỷ
make lab-down             # xuất số liệu lên S3, hạ node về 0, rồi destroy
```

> **Chi phí.** Bốn node GPU là ~4 USD/giờ. `lab-down` xuất số đo lên S3 **trước** khi xoá
> gì — chạy nó, đừng gõ `terraform destroy` tay. Ngân sách đề tài là 200 USD.

---

## Cấu trúc repo

| | |
|---|---|
| `terraform/` | ba tầng: `core`, `data`, `cluster` |
| `charts/` | Helm chart: vllm, litellm, guardrail, webui, tunnel |
| `services/llm_pipeline/` | guardrail — OpenAI-compatible, RAG, kiểm tra hai chiều |
| `guardrails/`, `prompt/`, `rag/` | phát hiện tấn công, dựng prompt, truy hồi |
| `bench/` | bộ k6, dataset, script phân tích |
| `bench/agents_sim/` | bảy consumer **mô phỏng** — không phải bằng chứng TC4 |
| `clients/python/` | client mẫu cho các đề án dùng nền tảng |
| `observability/` | recording rule và dashboard Grafana |
| `k8s/` | ingress, GPU operator, tracing |
| `docs/`, `reports/` | tài liệu, báo cáo tiến độ theo tuần |

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
python -m pytest tests/ -q   # 119 ca: pipeline, guardrail, cache, tracing, client
make guardrails-test         # kiểm thử hành vi PII, injection, policy, grounding, cache
make attacks-score           # chấm bộ đối kháng. FOLD=B tách kỹ thuật chưa từng thấy
make pii-verify              # 5 kiểm tra độc lập rằng PII thật sự bị che
make rag-eval                # truy hồi trên 144 câu hỏi vàng
make finops-plot             # vẽ lại đường cong TC2
```

> Ba lỗi trong **chính phương pháp đo** đã được tìm ra và sửa, mỗi lỗi đều báo một con số
> sai mà không báo lỗi ở đâu: cache ngữ nghĩa chiếm 98,7% lưu lượng của một lần đo engine,
> congestion collapse kéo availability xuống 7,26% mà không có một lỗi máy chủ nào, và
> engine nguội làm p95 lệch 9 lần. Chi tiết trong [`reports/tuan3.md`](reports/tuan3.md).
