# BÁO CÁO TIẾN ĐỘ DỰ ÁN – TUẦN 3

* **Dự án:** Nền tảng Serving LLM nội bộ tích hợp Guardrail & Observability
* **Thành viên:** Nguyễn Gia Bảo, Nguyễn Lê Minh
* **Thời gian báo cáo:** Tuần 3 (25/09 – 02/10/2026)

---

## Đối chiếu với mục tiêu đề ra trong báo cáo tuần 2

Kế hoạch tuần 3 là **stress test toàn bộ pipeline bằng k6 ở nhiều mức tải, tăng dần đến 50 req/s**. Mục tiêu này đã hoàn thành, và **tiêu chí p95 dưới 3 giây tại 50 req/s đã đạt bằng số đo thực tế**: 15.001 request trong 5 phút, p95 2047 ms, availability 99,71%, không lỗi.

Tuần 3 cũng phát sinh một nhóm kết quả không có trong kế hoạch: **ba lỗi trong chính phương pháp đo**, mỗi lỗi đều khiến hệ thống báo một con số sai mà không báo lỗi ở đâu. Nhóm đánh giá đây là phần quan trọng nhất của tuần, vì nó quyết định các số liệu trước đó được đọc như thế nào.

## 1. Năng lực serving và tiêu chí TC1

### 1.1. Cấu hình đo

Hai cấu hình GPU đã được đo trong tuần, cùng nằm trong hạn mức 16 vCPU của tài khoản:

* **4× g6.xlarge (NVIDIA L4, 24 GB, 300 GB/s):** mỗi node một engine vLLM, mô hình Qwen2.5-7B-Instruct-AWQ.
* **4× g5.xlarge (NVIDIA A10G, 24 GB, 600 GB/s):** cùng mô hình và cùng bộ câu hỏi.

Card L40S (`g6e.xlarge`) đã được dự kiến nhưng **không khả dụng**: phép dò bằng một instance đơn cho `InsufficientInstanceCapacity` ở cả hai Availability Zone của VPC. Đây là giới hạn năng lực của AWS, không phải lỗi cấu hình, và hướng "2 card L40S đủ 50 req/s" nêu ở báo cáo nghiệm thu hiện không thực hiện được.

### 1.2. Đường cong dung lượng

Trên **4× L4**, scaling theo số card là tuyến tính ở mức 10 req/s mỗi card, giữ nguyên từ 1 lên 4 card. Hệ thống đạt 40 req/s ở p95 2190 ms, và ở 50 req/s thì p95 lên 4424 ms — vượt ngưỡng nhưng vẫn phục vụ gần đủ và không phát sinh lỗi.

Trên **A10G**, phép ramp với bước 5 req/s cho kết quả sau (3 card dành cho mô hình 7B):

| Tải chào (req/s) | Availability | p50 | p95 | p99 | TC1a |
|---|---|---|---|---|---|
| 10 | 99,89% | 560 ms | 1037 ms | 1586 ms | đạt |
| 20 | 99,72% | 622 ms | 1139 ms | 1700 ms | đạt |
| 30 | 99,67% | 726 ms | 1500 ms | 2366 ms | đạt |
| 40 | 99,83% | 843 ms | 2029 ms | 3098 ms | đạt |
| 50 | 99,66% | 1408 ms | 8518 ms | 12048 ms | trượt |
| 60 | 96,73% | 7888 ms | 17223 ms | 19386 ms | trượt |

Điểm bão hòa nằm giữa 40 và 50 req/s, tương ứng **13,3 req/s mỗi card A10G** so với 10 req/s mỗi card L4. Băng thông bộ nhớ gấp đôi chỉ đổi thành 1,33 lần dung lượng đạt SLO, trong khi thông lượng token thô tăng 1,7 lần (743 so với 438 token/s mỗi card). Phần chênh lệch là cái giá của việc giữ p95 dưới ngưỡng.

### 1.3. TC1a đạt với bốn card dành riêng cho mô hình 7B

Khi chuyển toàn bộ bốn card sang mô hình 7B, hệ thống **đạt tiêu chí đề bài**:

```
50 req/s, 5 phút, 15.001 request, 4× A10G
served p95       2047 ms   < 3000 ms     ĐẠT
availability     99,71%
lỗi                   0
semantic cache        0%  (bỏ qua có chủ đích để đo engine)
```

Đây là số đo trực tiếp ở đúng mức tải đề bài yêu cầu, không phải ngoại suy từ mức thấp hơn.

## 2. Guardrail, độ sẵn sàng và đa agent

### 2.1. Độ sẵn sàng (TC1b)

Availability ban đầu chỉ đạt 97,4%: khoảng 2% câu hỏi hợp lệ bị tầng grounding chặn, nguyên nhân là mô hình **bịa mã tài liệu** — trích dẫn một mã có thật trong corpus nhưng không nằm trong các chunk được cung cấp. Cách xử lý là cho mô hình một lần sinh thứ hai khi grounding chặn, thay vì nới lỏng điều kiện kiểm tra. Kết quả trên 12.001 request ở 40 req/s: điểm 99,6917%, **cận dưới một phía 95% là 99,5946%**, vượt ngưỡng 99,5%.

Phán quyết được đọc theo cận dưới chứ không theo điểm ước lượng, vì một lần chạy không lỗi với cỡ mẫu nhỏ vẫn tương thích với độ sẵn sàng thật thấp hơn nhiều.

### 2.2. An toàn (TC3)

Bộ đối kháng chặn **100% trên 294 mẫu offline** và **100% trên 129 mẫu held-out** chạy qua hệ thống thật. Tỷ lệ chặn nhầm câu hỏi hợp lệ là 0 trên tập offline. Tuần 3 cũng sửa một chỉ số sai trong chính bộ đo: metric `attack_blocked` từng báo 79% do một lớp mẫu tấn công được gửi sai kênh — tỷ lệ thật là 100%, và việc một chỉ số an toàn báo thấp hơn thực tế vẫn là lỗi cần sửa.

### 2.3. Đa agent (TC4)

Tầng Aurora đã được dựng lại và **7 virtual key** được cấp cho 7 agent trong `bench/agents.json`, kèm team dùng chung hạn mức 3000 RPM. Cô lập theo key đã được kiểm chứng trên hệ thống thật: key của `moc-datadict` bị từ chối khi gọi mô hình 7B với thông báo `This key can only access models=['qwen2.5-1.5b']`.

Nhãn `end_user` trong Prometheus đến từ **HTTP header `X-Agent-Id`**, không phải trường `user` trong body — LiteLLM v1.90.2 không đọc trường đó. Dashboard độ trễ, chi phí và token theo từng agent đã có dữ liệu.

### 2.4. Đánh đổi giữa TC1a và TC4

Hai tiêu chí này **không cùng đạt được trong hạn mức 16 vCPU**:

| | 4 card cho 7B | 3 card 7B + 1 card 1.5B |
|---|---|---|
| TC1a (p95 < 3s @ 50 req/s) | **đạt** — 2047 ms | trượt ở 50; đạt ở 40 |
| TC4 (≥5 agent chạy được) | **trượt** — 4/7 agent | **đạt** — 7/7 agent |
| Cụm đa mô hình (phạm vi đề bài) | không | có |

Ba agent `moc-datadict`, `moc-service`, `moc-daily` chỉ được phép dùng mô hình 1.5B. Khi bỏ card 1.5B, các agent này **không hoạt động** — đã kiểm chứng bằng request thật, trả về lỗi phân giải tên service. Số agent chạy được giảm từ 7 xuống 4, dưới ngưỡng 5 của TC4.

**Đề nghị mentor xác nhận:** ba agent nêu trên có được phép dùng mô hình 7B hay không. Nếu được, cấu hình bốn card đạt đồng thời cả năm tiêu chí trong hạn mức hiện tại. Nếu việc gán mô hình 1.5B là quyết định chi phí có chủ đích, cần nâng hạn mức lên 20 vCPU.

## 3. Ba lỗi trong phương pháp đo

Nhóm xếp phần này riêng vì mỗi lỗi đều **báo thành công trong khi kết quả sai**, và vì chúng quyết định cách đọc các số liệu của những tuần trước.

* **Phép đo đo cache thay vì đo hệ thống.** Một lần chạy 50 req/s cho p95 9 ms ở panel gateway trong khi vLLM báo 4656 ms. Nguyên nhân: 14.809 trong 15.000 request là cache hit, engine chỉ nhận 191 request. Xóa cache trước khi chạy không giải quyết được, vì tập eval chỉ có 36 câu gốc với 4 biến thể mỗi câu và ngưỡng tương đồng là 0,96 — các biến thể trùng cache của nhau, nên cache tự nạp lại từ chính tập dữ liệu trong khoảng 4 giây đầu. Giải pháp là một header `X-Bypass-Cache` bỏ qua cả tra cứu và ghi, cộng ngưỡng `cache_hit` cho từng mức tải của phép ramp.

* **Timeout của gateway dài gấp 15 lần của client gây sụp đổ do nghẽn.** Ở 50 req/s, 13.178 trong 14.210 request timeout ở đúng 20.000 ms, **không một lỗi server nào**, availability 7,26%, thông lượng 127 token/s trên ba card — khoảng một phần mười mức một card L4 từng đạt. Client bỏ cuộc ở 20 giây còn LiteLLM giữ request tới 300 giây rồi thử lại một lần, nên engine liên tục sinh những câu trả lời không còn ai nhận. Sửa hai giá trị cấu hình (`timeoutSeconds` 300 → 20, `retries` 1 → 0) đưa availability từ 7,26% lên 99,78% trên **cùng phần cứng**.

* **Engine nguội làm sai lệch kết quả 9 lần.** Cùng bốn card, cùng vị trí pod, khác duy nhất ở chỗ engine đã phục vụ request nào chưa: p95 18.650 ms và availability 80,56% khi nguội, so với 2047 ms và 99,71% khi đã ấm. Nguyên nhân là prefix cache rỗng (82,6% hit khi ấm) và chi phí dựng CUDA graph. Nếu báo cáo lần chạy nguội, kết luận sẽ là bốn card không đủ và nhóm sẽ đi xin GPU thứ năm cho một vấn đề không tồn tại.

Ngoài ba lỗi trên, tuần 3 còn sửa: bộ tạo tải chạy chung node với gateway nó đang đo (node 2 vCPU, guardrail 1,1 core + LiteLLM 1,4 core + k6 1 core); bốn ngưỡng cấu hình chặn việc mở rộng số GPU, trong đó ba ngưỡng thất bại trong im lặng; và anti-affinity thiếu khiến bốn engine có thể bị dồn lên hai card do GPU Operator quảng bá time-slicing 2 slot mỗi card.

## 4. Giới hạn hiện tại

* **TC1a đạt ở cấu hình không đạt TC4**, và ngược lại. Đây là giới hạn hạn mức vCPU, không phải giới hạn kỹ thuật.
* **TC2 (chi phí/1k token) là một đường cong theo tải, chưa phải một con số.** Hòa vốn ở khoảng 1,5 req/s duy trì 24/7; đạt mức giảm 30% ở khoảng 2,25 req/s. Giá API ngoài dùng để so sánh là giá niêm yết, cần xác nhận.
* **Uptime 99,5% đã chứng minh trong một phiên đo, chưa phủ 14 ngày pilot.** Cần chuỗi probe theo lịch.
* **Profile HA 3 replica đã có nhưng chưa triển khai được:** cần Redis dùng chung, và chưa có Terraform cho ElastiCache.
* **Chi phí tuần 3 ước tính 25–30 USD** theo giá giờ và thời lượng phiên (4 node GPU ở 3,22–4,02 USD/giờ). Cost Explorer hiện báo gần 0 do credit bù, nên đây là ước lượng chứ không phải số hóa đơn.

---

## 5. Kế hoạch Triển khai Tuần 4

Trọng tâm tuần 4 là **chốt cấu hình nghiệm thu** và chuyển từ các phép đo đơn lẻ sang bằng chứng phủ thời gian cho pilot.

### Nguyễn Gia Bảo

* **Chốt cấu hình đạt đồng thời TC1a và TC4:** sau khi mentor xác nhận việc gán mô hình cho ba agent, đo lại cấu hình được chọn ở cả ba mức 10, 25 và 50 req/s; chụp dashboard và lưu snapshot số liệu lên S3 cho từng mức.
* **Tách nút thắt gateway khỏi nút thắt GPU:** ở 50 req/s, LiteLLM đạt 0,96 core và thông lượng engine đạt 98% mức bão hòa cùng lúc, nên một lần chạy không phân biệt được. Dùng profile 3 replica của Minh để đo lại; nếu p95 cải thiện thì gateway là ràng buộc, nếu không thì GPU.
* **Hoàn thiện FinOps:** ghi chi phí thật theo từng phiên, dựng lại đường cong TC2 với số đo của A10G thay vì L4, và xác nhận giá API ngoài dùng để so sánh.

### Nguyễn Lê Minh

* **Triển khai profile HA 3 replica:** bổ sung Terraform cho Redis dùng chung, dựng và kiểm chứng rollout, xác nhận hạn mức và router state được chia sẻ giữa các pod sau khi chủ động xóa một pod.
* **Chuỗi probe phủ thời gian cho TC1b:** thiết lập probe định kỳ ghi kết quả ra ngoài cụm để availability được tính trên nhiều ngày thay vì một phiên, phục vụ yêu cầu pilot hai tuần.
* **Đối soát số liệu và tổng hợp báo cáo:** dựng bảng kết quả theo từng mức tải cho báo cáo nghiệm thu, đối chiếu số liệu k6 với metric của gateway, guardrail và vLLM, và tách rõ panel độ trễ có trộn cache với panel chỉ tính request được engine phục vụ.
