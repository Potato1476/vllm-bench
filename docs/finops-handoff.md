# FinOps — bàn giao cho Minh, 07/10/2026

Mục tiêu hôm nay: **chốt số liệu TC2** (chi phí/1k token giảm ≥30% so với API ngoài) trên
cấu hình đang chạy thật — 4× A10G, EKS 1.35, Aurora `db.t3.medium`.

Phần dưới chia ba: thứ đã xác minh xong (dùng luôn), thứ cần đo trong phiên hôm nay, và
những cái bẫy đã làm số liệu sai ít nhất một lần.

---

## 1. Đã xác minh — dùng được ngay

### Chi tiêu thật từ đầu đề tài

Cost Explorer, 01/09 → 07/10/2026, **Usage gộp trước credit**:

| Dịch vụ | USD | % |
|---|---|---|
| EC2 Compute (chủ yếu GPU) | 60,32 | 69,5% |
| EKS | 22,81 | 26,3% |
| RDS (Aurora) | 1,72 | 2,0% |
| VPC | 0,64 | 0,7% |
| EC2 khác (EBS…) | 0,61 | 0,7% |
| S3 | 0,34 | 0,4% |
| Cost Explorer API | 0,33 | 0,4% |
| **Tổng** | **86,79** | |

Tài khoản có **credit trừ thẳng**, nên số ròng hiện 0. Con số cho TC2 là **Usage**, không
phải ròng: credit là cách trả tiền, không phải thuộc tính của nền tảng. Báo cáo tuần 3 ghi
"ước tính 25–30 USD" — số thật cao hơn nhiều.

### Phụ phí EKS extended support — 19,01 USD

Trong 22,81 USD tiền EKS, **19,01 là phụ phí** vì cụm chạy 1.31, đã hết hỗ trợ chuẩn từ
26/11/2025. Giá cụm thành 0,60 USD/giờ thay vì 0,10 — gấp 6. **Đã sửa:** cụm hôm nay là
1.35, và `terraform/cluster/version_guard.tf` chặn plan nếu phiên bản rơi ra khỏi hỗ trợ
chuẩn. Khi báo cáo nên tách khoản này ra: nó là **lỗi cấu hình**, không phải chi phí bản
chất của nền tảng.

### Đơn giá — Pricing API, 07/10, on-demand, us-east-1

| | USD/giờ |
|---|---|
| `g5.xlarge` (A10G) | 1,006 |
| `g6.xlarge` (L4) | 0,8048 |
| `m7i.large` (node CPU) | 0,1008 |
| Aurora `db.t3.medium` | 0,082 |
| EKS control plane (chuẩn) | 0,10 |

Cấu hình hôm nay: 4 GPU + 2 CPU + Aurora + EKS ≈ **4,39 USD/giờ**.

### Năng lực mỗi card (đạt SLO p95 < 3s, cache tắt)

| Card | req/s/card | Nguồn | req/s trên mỗi USD-giờ |
|---|---|---|---|
| A10G | 12,5 | 50 req/s trên 4 card, p95 2047ms, 15.001 request | **12,43** |
| L4 | 10,0 | ramp 8.642 probe, tuyến tính 1→4 card | **12,43** |

Hai card cho **cùng năng lực trên mỗi USD**. A10G không rẻ hơn ở quy mô lớn; nó chỉ cho
nhiều dư địa hơn trên mỗi card — thứ làm 50 req/s vừa trong hạn mức 4 card. Ở tải thấp L4
rẻ hơn vì giá giờ thấp hơn.

### Đường cong TC2 hiện tại (A10G)

```bash
make finops-plot                         # reports/images/tc2-savings.png
PYTHONPATH=. python3 bench/scripts/finops_curve.py
FINOPS_GPU=l4 PYTHONPATH=. python3 bench/scripts/finops_curve.py   # đối chiếu L4
```

Hoà vốn **~2 req/s duy trì**, đạt TC2 **~2,5 req/s duy trì**. Trục hoành là **trung bình
24/7**, không phải tải đỉnh.

---

## 2. Cần đo trong phiên hôm nay

Mô hình có bốn đầu vào. Hai cái đã đo ở trên; hai cái dưới đây quyết định kết quả và
**chưa ai đo trên A10G**.

### Token thật mỗi request

Mô hình đang giả định 1300 token vào / 45 ra. Trong Grafana → Explore (Prometheus), sau
một lần chạy tải:

```promql
sum(increase(litellm_input_tokens_metric[30m]))
  / sum(increase(litellm_proxy_total_requests_metric[30m]))

sum(increase(litellm_output_tokens_metric[30m]))
  / sum(increase(litellm_proxy_total_requests_metric[30m]))
```

Nếu khác xa 1300/45, sửa `PROMPT_TOKENS` / `OUTPUT_TOKENS` trong
`bench/scripts/finops_curve.py` rồi vẽ lại.

### Số lời gọi model trên mỗi request — biến nhạy nhất

Đổi từ 1 lên 3 lời gọi dịch điểm hoà vốn **gấp 3 lần** (1,5 → 0,5 req/s). Phần đo được
trên nền tảng là hệ số retry của guardrail:

```promql
sum(increase(guardrail_upstream_requests_total{outcome="success"}[30m]))
  / sum(increase(guardrail_requests_total[30m]))
```

Đang giả định 1,0175 (1,75% bị grounding chặn rồi sinh lại). Còn số lời gọi mà một copilot
agentic thật tạo ra cho mỗi câu hỏi của người dùng thì **không đo được** từ traffic mô
phỏng — ghi rõ trong báo cáo là giả định, và đưa ra cả đường `CALLS=1` lẫn `CALLS=3`.

### Chi phí của chính phiên hôm nay

Cost Explorer **trễ khoảng 24 giờ**, nên hôm nay tính bằng giờ-máy × đơn giá:

1. Ghi lại giờ bật 4 GPU và giờ tắt.
2. Chi phí phiên ≈ số giờ × 4,39 USD.
3. Ngày mai chạy `make cost` để đối soát với hoá đơn thật.

### Giá API ngoài

Mô hình so với `gpt-4o-mini` ở 0,15 / 0,60 USD cho 1M token vào/ra — **giá niêm yết chưa
xác nhận lại** từ lúc viết. Kiểm trên trang giá chính thức của OpenAI trước khi chốt, vì
toàn bộ phép so sánh đứng trên con số này.

---

## 3. Bẫy đã gặp

| Bẫy | Triệu chứng | Cách tránh |
|---|---|---|
| Credit che chi phí | Cost Explorer theo dịch vụ ra ~0 | lọc `RECORD_TYPE=Usage`; `make cost` đã sửa |
| Phụ phí extended support | EKS đắt gấp 6 mà không lỗi gì | guard trong `version_guard.tf` |
| Đọc tải đỉnh vào đường cong | phóng đại tiết kiệm ~3× | trục hoành là trung bình 24/7 |
| Cache ngữ nghĩa trong phép đo | p95 đẹp bất thường, 98,7% từ Redis | k6 gửi `X-Bypass-Cache` mặc định |
| Gọi Cost Explorer trong vòng lặp | mỗi request tốn 0,01 USD | gọi tay, không loop |

Cụm bị huỷ cuối phiên bằng `make lab-down` — lệnh đó xuất số liệu Prometheus lên S3
**trước** khi xoá. Đừng `terraform destroy` tay.
