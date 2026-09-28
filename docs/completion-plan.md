# Kế hoạch hoàn thành — Đề tài 51

**Cập nhật:** 28/09/2026 (tuần 3/6) · **Còn lại:** ~3,5 tuần
**Người viết:** Nguyễn Gia Bảo · **Đọc cùng:** `docs/OVERVIEW.md`, `docs/k6-load-testing.md`

Mọi con số dưới đây là **đo được trên hệ thống thật**, không phải ước tính. Chỗ nào chưa đo thì ghi rõ là chưa đo.

---

## 1. Vị trí hiện tại

| | Tiêu chí | Trạng thái | Số đo |
|---|---|---|---|
| **TC3** | Chặn ≥95% injection/PII | ✅ đạt | injection **100%** (294 mẫu offline, và dưới tải); PII **4/4** có bằng chứng trên hệ thống chạy thật |
| **TC4** | ≥5 DA chạy trên nền tảng | ✅ đạt | 7 virtual key, `end_user` vào Prometheus, `agent:*` có 7–8 series |
| **TC1b** | Uptime ≥99,5% | ✅ **đạt** | cận dưới 95% một phía **99,6352%** ≥ 99,5% (n=3601). Nhờ cơ chế thử lại khi grounding chặn: **cứu 56/63** |
| **TC1a** | p95 <3s @ 50 req/s | ❌ chưa đạt | 1× L4 đạt **10 req/s @ p95 1886ms**; ở 50 req/s sụp còn 2,9 req/s |
| **TC2** | Chi phí/1k token −30% | ⬜ chưa đo | rule `platform:cost_usd_per_1k_output_tokens` sẵn sàng, thiếu tải bền |

**Đường cong dung lượng**, 1× L4, qwen2.5-7b AWQ, cache tắt, 8642 probe:

```
chào    phục vụ     p50       p95      SLO 3s
   1      1,0/s    944ms    1586ms       ✅
   2      2,0/s    900ms    1539ms       ✅
   5      4,9/s   1037ms    1638ms       ✅
  10      9,8/s   1128ms    1886ms       ✅   ← điểm vận hành
  20     18,2/s   6967ms   11935ms       ❌   ← điểm gãy
  35      9,3/s  12065ms   19144ms       ❌   thông lượng đi lùi
  50      2,9/s  16343ms   19547ms       ❌
```

Đầu phiên hôm nay con số này là **1 req/s**. Toàn bộ mức tăng gấp 10 đến từ một đoạn văn bản trong `prompt/build.py`: câu trả lời dài gấp 2–3 lần giả định SLO, và decode chi phối thời gian.

---

## 2. Việc còn lại, theo thứ tự chặn nhau

### V1 — Đo L40S ⭐ *phép đo quan trọng nhất còn lại*

> **CHẶN (28/09): AWS hết hàng g6e.xlarge ở us-east-1a.**
> ```
> InsufficientInstanceCapacity - We currently do not have sufficient
> g6e.xlarge capacity in the Availability Zone you requested (us-east-1a)
> ```
> `g6e.xlarge` **có** ở us-east-1b/1c/1d, nhưng node group `gpu-l40s` bị ghim vào
> **một subnet duy nhất** (`subnet-0eec561fbdcebacf7`, us-east-1a), nên ASG không thể
> thử AZ khác. Đây là lỗi cấu hình sẽ tái diễn với mọi loại GPU khan hiếm.
>
> **Việc cần làm trước:** sửa `terraform/cluster/eks.tf` cho node group GPU dùng subnet
> ở nhiều AZ (core tier đã có sẵn 2 private + 2 public subnet), apply, rồi thử lại.
> Ước tính 20 phút. Làm đầu phiên sau.

Quyết định TC1a có khả thi trong quota hay không.

```
g6.xlarge    L4    4 vCPU   300 GB/s   ~$0,80/h
g6e.xlarge   L40S  4 vCPU   864 GB/s   ~$1,86/h    ← cùng vCPU
quota: 16 vCPU = 4 GPU, bất kể loại nào
```

Decode bị chặn bởi băng thông bộ nhớ, L40S gấp **2,9×**. Nếu hệ số giữ được thì **2 card đủ 50 req/s** — nằm gọn trong quota, không cần xin tăng.

```bash
make gpu-l40s n=1
make vllm-up MODE=solo-a QUANT=awq        # ghim lên node L40S
make load-incluster SCENARIO=ramp MODEL=qwen2.5-7b
```

> ~1 GPU-giờ. **Đo thật, đừng nhân băng thông rồi tin.** Prefill, KV cache và guardrail đều không scale theo cùng hệ số.

**Nếu L40S cho ≥25 req/s** → TC1a khả thi với 2 card, làm tiếp V2.
**Nếu <13 req/s** → phải xin tăng quota, và đó là việc có thời gian chờ — làm ngay đầu tuần 4.

### V2 — Chạy TC1a đúng nguyên văn

Sau V1, dựng đủ số GPU rồi chạy tiêu chí y như đề bài:

```bash
make gpu-l40s n=2        # hoặc số V1 chỉ ra
make load-incluster SCENARIO=slo MODEL=qwen2.5-7b
```

`SCENARIO=slo` là 50 req/s, 5 phút, 15.000 request, và **threshold của nó chính là tiêu chí** — exit code trích thẳng vào báo cáo được.

> ~0,5 GPU-giờ. Cần V1 xong trước.

### V3 — Chốt TC1b

Cơ chế thử-lại khi grounding chặn đã deploy. Cần xác nhận nó đưa availability qua 99,5%.

```bash
make load-incluster SCENARIO=steady RPS=5 DURATION=10m MODEL=qwen2.5-7b
make availability PROBES=results/probe/
```

Theo dõi hai counter: `guardrail_grounding_retry_total` và `_rescued_total` — tỉ lệ giữa chúng cho biết cơ chế đáng giá bao nhiêu.

**Nếu vẫn dưới 99,5%:** nguyên nhân là chất lượng sinh của 7B AWQ (bịa mã tài liệu), không phải guardrail. Hai hướng: thử lại 2 lần, hoặc đo xem FP16 có ít bịa hơn AWQ không — **chưa ai đo chi phí độ chính xác của lượng tử hoá 4-bit**, và nó là mục còn nợ từ tuần 2.

> ~0,3 GPU-giờ/lần đo.

### V4 — Đo TC2

Bài toán **hiệu suất sử dụng**, không phải chọn model. Chia $/giờ cho 2 req/s hay 10 req/s là hai thế giới khác nhau.

```bash
make load-incluster SCENARIO=steady RPS=10 DURATION=20m MODEL=qwen2.5-7b
# rồi đọc platform:cost_usd_per_1k_output_tokens trên Grafana
```

Cần thêm: **bảng giá API ngoài để đối chiếu**, tính cả token vào (prompt ~1200 token/request là đáng kể). Ghi rõ đang so với model nào.

> ~0,5 GPU-giờ. Không phụ thuộc V1.

### V5 — Gom bằng chứng calendar cho TC1b

Mỗi phiên làm việc chạy một lần, gộp lại cuối dự án:

```bash
make load-incluster SCENARIO=steady RPS=2 DURATION=10m
make availability PROBES=results/probe/      # gộp mọi phiên
```

14 phiên phủ 14 ngày lịch — **và 14 lần triển khai**, thứ một lần chạy liên tục 2 tuần không phủ được, trong khi triển khai mới là nơi sự cố thật sinh ra.

> ~0,2 GPU-giờ/phiên. Bắt đầu ngay từ phiên sau.

### V6 — Đẩy log lên S3 mỗi phiên

```bash
make audit-export HOURS=12 LABEL=<viec-gi-do>
```

**Chạy nhiều lần trong phiên, không chỉ lúc tắt máy.** `kubectl logs` chỉ đọc được phần container còn giữ; Kubernetes xoay vòng log ở ~10MB, và một ramp 8600 request vượt ngưỡng đó — bản ghi sớm nhất sẽ mất.

> Nên đưa vào `lab-down` như `make snapshot` đã làm. Chưa làm.

---

## 3. Cần mentor quyết

**1. `agents.json` gán cứng `qwen2.5-1.5b` cho ba agent** (`moc-datadict`, `moc-service`, `moc-daily` — agent nặng nhất). Đo được: 1.5B **trượt grounding 133/144 câu**, kể cả câu đơn giản nhất. Ép trích dẫn ở tầng decode cứu được (8/8 trích dẫn hợp lệ) nhưng phủ nội dung chỉ 66% trên câu tra cứu và 15–20% trên câu suy luận. **Ba lựa chọn:** cấp 7B cho mọi agent / xây router theo loại câu hỏi / nới grounding cho lớp B (không khuyến nghị — bỏ chính TC3).

**2. "50 req/s là chỉ tiêu cho cả fleet hay cho một GPU?"** Quyết định này đổi hẳn V1–V2. Nếu là fleet thì số hiện tại đã gần đạt.

**3. Tên 7 agent trong `bench/agents.json` là placeholder**, suy từ các nhóm tài liệu có thật trong corpus. Cần roster thật trước khi trích số per-agent vào báo cáo.

**4. Có mẫu log truy vấn MOC thật không?** Mọi kết luận về tỉ lệ cache hit và về việc phân tầng model có lãi hay không đều phụ thuộc tỉ lệ câu hỏi đơn giản thật, mà hiện ta chỉ có tỉ lệ của bộ eval (31%) — vốn là lựa chọn thiết kế của người viết bộ test, không phải đo traffic.

---

## 4. Rủi ro

| Rủi ro | Dấu hiệu | Xử lý |
|---|---|---|
| **IP nhà đổi liên tục** | Hôm nay đổi **5 lần**, qua 4 dải. Mỗi lần mất cả Grafana lẫn kubectl, và đã **giết một ramp 15 phút** | Đã mở `0.0.0.0/0` + mật khẩu. Nếu cần khoá lại thì phải chấp nhận chạy `fix-cidr` liên tục |
| **Log xoay vòng mất dữ liệu** | Export chỉ được 11/8600 bản ghi khi chạy sai thời điểm | V6: export nhiều lần/phiên. Bền hơn: Fluent Bit đẩy liên tục (chưa làm) |
| **Chất lượng AWQ chưa đo** | Model bịa mã tài liệu, có lần chèn ký tự CJK và tự sinh lượt `user` | Nợ từ tuần 2. Cần chạy 144 câu vàng trên FP16 vs AWQ |
| **Ngân sách** | 200 USD | `make cost` mỗi phiên. Kế hoạch trên tốn <3 GPU-giờ ≈ 5 USD |

---

## 5. Thứ tự đề xuất

| Tuần | Việc |
|---|---|
| **4 đầu tuần** | V1 (L40S) → nếu không đủ thì **xin tăng quota ngay**, việc này có thời gian chờ |
| **4** | V3 (chốt TC1b) · V4 (TC2) · bắt đầu V5 mỗi phiên |
| **5** | V2 (TC1a nguyên văn) · đo chất lượng AWQ vs FP16 · trả lời câu hỏi mentor |
| **6** | Gộp V5 → báo cáo availability · viết báo cáo cuối · tắt tầng `core` |

**Ràng buộc thật không phải tiền mà là số phiên làm việc còn lại.** Toàn bộ kế hoạch dưới 3 GPU-giờ; V5 cần *nhiều phiên khác ngày*, nên bắt đầu sớm là thứ duy nhất không bù lại được.
