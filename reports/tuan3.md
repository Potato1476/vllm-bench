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


## 3. Giới hạn hiện tại

* **TC2 (chi phí/1k token) là một đường cong theo tải, chưa phải một con số.** Hòa vốn ở khoảng 1,5 req/s duy trì 24/7; đạt mức giảm 30% ở khoảng 2,25 req/s. Giá API ngoài dùng để so sánh là giá niêm yết, cần xác nhận.
* **Uptime 99,5% đã chứng minh trong một phiên đo, chưa phủ 14 ngày pilot.** Cần chuỗi probe theo lịch.
* **Profile HA 3 replica đã có nhưng chưa triển khai được:** cần Redis dùng chung, và chưa có Terraform cho ElastiCache.
* **Chi phí tuần 3 ước tính 25–30 USD** theo giá giờ và thời lượng phiên (4 node GPU ở 3,22–4,02 USD/giờ).

---

## 4. Kế hoạch Triển khai Tuần 4

Trọng tâm tuần 4 là **FinOps** và **đưa nền tảng vào sử dụng thật**: xuất mô hình qua API tương thích OpenAI, rồi kiểm chứng bằng cách cắm API key vào một dự án đang chạy thay vì chỉ gọi bằng công cụ đo.

### 4.1. Hiện trạng phần tương thích OpenAI

Phần lõi đã tương thích và đã kiểm chứng trên hệ thống thật: `/v1/models`, `/v1/chat/completions` ở cả chế độ thường lẫn streaming, xác thực Bearer, và mã lỗi theo chuẩn. Một ứng dụng viết bằng SDK OpenAI chỉ cần đổi `base_url` và `api_key`.

Bốn điểm chưa sẵn sàng cho dự án thật, cần xử lý trong tuần 4:

* **Chưa có TLS.** Endpoint hiện là HTTP thuần, nghĩa là API key đi qua mạng ở dạng đọc được. Đây là điều kiện chặn với bất kỳ tích hợp thật nào, không phải việc làm đẹp.
* **Địa chỉ không ổn định.** URL gắn với IP công khai của node và đổi sau mỗi lần dựng lại cụm, trong khi cụm bị hủy mỗi tối để tiết kiệm chi phí. Một dự án thật không thể phụ thuộc vào địa chỉ như vậy.
* **Function calling không được hỗ trợ,** và đây là lựa chọn thiết kế chứ không phải lỗi: guardrail đầu ra kiểm tra trích dẫn và PII trên văn bản, mà một tool call không mang văn bản nào. Hệ thống từ chối rõ ràng thay vì trả về kết quả chưa được kiểm tra. Cần xác nhận dự án thí điểm có dùng tính năng này không **trước khi** chọn dự án.
* **Chỉ phục vụ chat completions.** Các endpoint `/v1/embeddings` và `/v1/completions` chưa đi qua guardrail.

### Nguyễn Gia Bảo

* **Hoàn thiện FinOps:** dựng lại đường cong TC2 bằng số đo A10G thay cho L4 — 12,5 req/s mỗi card ở 1,006 USD/giờ, thay cho 10 req/s ở 0,8048 USD/giờ — ghi chi phí thật theo từng phiên, và xác nhận giá niêm yết của API ngoài dùng để so sánh. Kết quả cần là một bảng trả lời được "ở mức tải nào thì tự vận hành rẻ hơn", không phải một con số phần trăm đơn lẻ.
* **Chốt cấu hình nghiệm thu:** sau khi mentor xác nhận việc gán mô hình cho ba agent hiện chỉ dùng 1.5B, đo lại cấu hình được chọn ở ba mức 10, 25 và 50 req/s; lưu dashboard và snapshot số liệu lên S3 cho từng mức.
* **Tách nút thắt gateway khỏi nút thắt GPU:** ở 50 req/s, LiteLLM đạt 0,96 core và thông lượng engine đạt 98% mức bão hòa cùng lúc, nên một lần chạy không phân biệt được hai nguyên nhân. Dùng profile 3 replica để đo lại: nếu p95 cải thiện thì gateway là ràng buộc, nếu không thì GPU.

### Nguyễn Lê Minh

* **Đưa endpoint lên mức dùng được thật:** bổ sung TLS và một địa chỉ ổn định không đổi theo từng phiên, rồi xác định khung giờ phục vụ mà dự án thí điểm có thể dựa vào — hoặc chấp nhận cụm chạy liên tục trong tuần tích hợp và tính chi phí tương ứng.
* **Tích hợp vào một dự án thật:** chọn một ứng dụng đang chạy, cấp cho nó một virtual key riêng, và chỉ đổi `base_url` cùng `api_key` chứ không sửa mã nguồn. Ghi lại mọi chỗ hành vi khác với OpenAI: định dạng lỗi, cách đếm token, hành vi streaming, và các tính năng bị guardrail từ chối.
* **Triển khai profile HA 3 replica:** bổ sung Terraform cho Redis dùng chung, kiểm chứng hạn mức và router state được chia sẻ giữa các pod sau khi chủ động xóa một pod. Đây cũng là điều kiện để endpoint chịu được một lần cập nhật mà không đứt với dự án đang dùng.
