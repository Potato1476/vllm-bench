# vllm-bench

Nền tảng serving LLM dùng chung cho các copilot MOC: vLLM đa mô hình trên AWS EKS, một
gateway tương thích OpenAI, guardrail đọc nội dung ở cả hai chiều, và bộ đo tải trả lời
được bằng số liệu tự đo chứ không bằng ước lượng.

Đề tài **DA#51** · Nguyễn Gia Bảo, Nguyễn Lê Minh · Mentor: Phạm Duy Tùng

---

## Kết quả đo được

Cập nhật 09/10/2026. Phương pháp và dữ liệu thô: [`docs/acceptance-report.md`](docs/acceptance-report.md).

| | Tiêu chí | Kết quả | |
|---|---|---|---|
| **TC1a** | p95 < 3s ở 50 req/s | **p95 1590ms ở 50 req/s**, 34.354 probe trên 8 mức tải, 4× A10G | ✅ |
| **TC1b** | Uptime ≥ 99,5% | cận dưới **99,7228%** (n=34.354, 79 lỗi) — chưa phủ 14 ngày | ✅ |
| **TC2** | Chi phí/1k token giảm ≥30% | 49,4% ở 10 req/s theo mô hình P2; hoà vốn ~2,0 req/s | ✅ |
| **TC3** | Chặn ≥95% injection/PII | **100%** offline (294 mẫu), **100%** live (349 mẫu) | ✅ |
| **TC4** | ≥5 DA chạy trên nền tảng | phục vụ được 8 đề án: key riêng, cô lập model đã kiểm chứng | ✅ |
| **CD** | Rollout model tự động | Argo CD đồng bộ từ git; hai model, hai node, hai track độc lập | ✅ |

**TC1a** đo trực tiếp ở đúng mức tải đề bài, không ngoại suy. Ramp 30 phút qua 8 mức từ
1 đến 50 req/s, cache ngữ nghĩa **tắt có chủ ý** để đo engine chứ không đo Redis. p95
đạt ở **mọi** mức, và đường cong gần như phẳng — chưa chạm bão hoà ở mức đề bài yêu cầu.
Cấu hình `MODE=solo-a`, cả bốn card phục vụ 7B.

**TC1b** đọc theo **cận dưới** Clopper-Pearson chứ không theo điểm ước lượng: 99,7228%
trên 34.354 probe với 79 lỗi, tất cả là guardrail chặn ở tầng grounding — không có lỗi
máy chủ, không timeout.

Phạm vi cần nói rõ: con số này nói về **cửa sổ phiên đã quan sát**, không nói gì về hỏng
hóc sau nhiều tuần chạy liên tục. Đề bài ghi *pilot 2 tuần*; chạy liên tục 2 tuần tốn
~338 USD trên ngân sách 200. [`bench/scripts/availability.py`](bench/scripts/availability.py)
tách tiêu chí thành ba mệnh đề và chỉ mệnh đề thứ ba cần thời gian — mà nó cần **phủ
lịch**, không cần uptime liên tục: 14 phiên hằng ngày phủ 14 ngày *và* 14 lần triển khai.
**Phần phủ lịch vẫn chưa làm.**

**TC2** là một đường cong, không phải một con số: tự vận hành trả tiền **thời gian thuê**,
API trả tiền **token**, nên tỷ số giữa chúng chỉ là hàm của mức sử dụng. Mức 49,4% là
theo cấu hình production tham chiếu P2 ở 10 req/s duy trì — xem hai báo cáo FinOps trong
[`reports/`](reports/). Token là **đo được** (1142 vào / 36,5 ra trên 34.275 request), và
việc thay giả định 1300/45 bằng số đo làm điểm hoà vốn **tăng** từ 1,75 lên 2,00 req/s.
Xem [`reports/images/tc2-savings.png`](reports/images/tc2-savings.png).

**TC4 — đọc theo *năng lực phục vụ*.** Bằng chứng là cơ chế, đã kiểm chứng trên hệ thống
thật: 8 virtual key riêng cho DA#19/#20/#32/#39/#41/#44/#45 cùng pilot, **cô lập theo key
đã chứng minh** (`This key can only access models=[...]` khi gọi sang model ngoài quyền),
hai profile guardrail để đề án không làm hỏi-đáp vẫn dùng được, và client mẫu trong
[`clients/python/`](clients/python/).

Ghi rõ để người đọc tự đánh giá: **chưa đề án nào gọi vào nền tảng.** Nếu đề bài đo số đề
án đã tích hợp thay vì năng lực phục vụ thì hiện trạng là 0/7 — nhóm đã nêu cách đọc này
với mentor.

---

## Kiến trúc

```
                                                   ┌─ vLLM  qwen2.5-7b    (node 1)
Client → Ingress → LiteLLM ──→ Guardrail ──────────┤
                   gateway      RAG + kiểm         └─ vLLM  qwen2.5-1.5b  (node 2)
                   key, quota   tra 2 chiều                 engine suy luận
                                     │
                                Redis cache        git ──► Argo CD ──► cả hai track
```

**LiteLLM** giữ API key, hạn mức và định tuyến mô hình. **Guardrail** là nơi duy nhất đọc
nội dung: phát hiện prompt injection, che PII tiếng Việt, truy hồi tài liệu, dựng prompt,
rồi kiểm tra trích dẫn và quét PII đầu ra trước khi trả về. **vLLM** chỉ suy luận.

Mô hình do **người gọi chọn** qua trường `model`, không có router đoán thay. Tách như vậy
để mỗi tầng hỏng theo cách riêng của nó và quan sát được riêng.

**Mỗi mô hình một node, một track rollout riêng.** Không chia card: một 1.5B chiếm một
phần card mà 7B đang cần sẽ hạ trần thông lượng của chính mô hình phải đạt 50 req/s.

**Trạng thái triển khai nằm trong git, không nằm trong cụm.** Cụm bị huỷ mỗi tối; bất kỳ
thứ gì một bộ điều khiển rollout nhớ trong cụm đều chết theo nó, và cụm sáng hôm sau sẽ
dựng lại bản đã bị loại. Argo CD đồng bộ cụm **theo** [`deploy/state.yaml`](deploy/state.yaml).
Xem [`docs/cd-runbook.md`](docs/cd-runbook.md).

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
make gpu n=2              # 2 node: mỗi mô hình một card
make cluster-config       # bucket + IAM role vào cụm, để Argo khỏi cần terraform
make argocd-up            # từ đây git điều khiển việc triển khai vLLM
make litellm-up && make guardrail-up
make agent-keys ROTATE=1  # virtual key cho từng đề án, từ bench/agents.json
```

Sau `argocd-up` **không chạy `make vllm-up` nữa** — Argo dựng vLLM theo `deploy/state.yaml`.
Chạy `vllm-up` lúc này là tạo một release Helm thứ hai tranh chấp với release của Argo.
Đo TC1a thì đổi `cards: 4` cho track 7B trong state rồi `make rollout-render`.

Chạy pilot với người thật:

```bash
make webui-up PILOT_KEY=sk-...   # chat UI, key từ `make agent-keys`
make tunnel-up                   # URL HTTPS cố định, không đổi khi dựng lại cụm
```

Bắn tải — ở trên vài req/s **bắt buộc** chạy trong cụm, vì đường truyền từ laptop nằm
thẳng trong p95:

```bash
make rollout-pause REASON="do TC1a"                  # BAT BUOC: xem ghi chú dưới
make load-incluster SCENARIO=slo  MODEL=qwen2.5-7b   # tiêu chí đề bài, đạt/không đạt
make load-incluster SCENARIO=ramp MODEL=qwen2.5-7b   # tìm điểm gãy
make rollout-resume
```

> Một lần rollout chen vào giữa phép đo lấy mất một card và cụm chỉ còn 3/4 dung lượng —
> con số thu được sẽ sai mà không có gì báo.

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
| `deploy/` | trạng thái triển khai + Application cho Argo CD (sinh ra từ state) |
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
| [`docs/cd-runbook.md`](docs/cd-runbook.md) | dựng cụm buổi sáng, trạng thái rollout, xuất bản model |
| [`docs/cicd-plan.md`](docs/cicd-plan.md) | hợp đồng model, các cổng, phần CD còn lại |
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
python -m pytest tests/ -q   # 245 ca: pipeline, guardrail, cache, tracing, client
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
