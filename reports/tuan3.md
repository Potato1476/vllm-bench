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

Trọng tâm tuần 4 là **FinOps** và **đưa nền tảng vào tay người dùng thật**: một nhóm Data Analyst của GreenSM dùng nó cho công việc của họ, thay vì nền tảng chỉ được gọi bằng công cụ đo.

### 4.1. Hiện trạng phần tương thích OpenAI

Phần lõi đã tương thích và đã kiểm chứng trên hệ thống thật: `/v1/models`, `/v1/chat/completions` ở cả chế độ thường lẫn streaming, xác thực Bearer, và mã lỗi theo chuẩn. Một ứng dụng viết bằng SDK OpenAI chỉ cần đổi `base_url` và `api_key`.

Năm giới hạn còn lại, và chúng định hình việc chọn người dùng thí điểm:

* **Chưa có TLS.** Endpoint hiện là HTTP thuần. Analyst đăng nhập bằng mật khẩu, nên đây là điều kiện chặn chứ không phải việc làm đẹp.
* **Địa chỉ không ổn định.** URL gắn với IP công khai của node và đổi sau mỗi lần dựng lại cụm.
* **Function calling không được hỗ trợ,** và đây là lựa chọn thiết kế chứ không phải lỗi: guardrail đầu ra kiểm tra trích dẫn và PII trên văn bản, mà một tool call không mang văn bản nào. Hệ thống từ chối rõ ràng thay vì trả về kết quả chưa được kiểm tra.
* **Streaming không chạy chữ.** Guardrail đệm toàn bộ câu trả lời rồi mới phát SSE, vì không thể thu hồi một PII hay một trích dẫn bịa đã gửi đi. Hệ quả là thời gian tới token đầu tiên bằng thời gian sinh cả câu trả lời — công cụ đo không quan tâm, người dùng thật sẽ báo đó là lag.
* **Chỉ phục vụ chat completions.** Các endpoint `/v1/embeddings` và `/v1/completions` chưa đi qua guardrail.

### 4.2. Ràng buộc lớn nhất: corpus hiện là dữ liệu giả lập

`data/xanhsm_retrieval_mock/manifest.json` ghi rõ không có chính sách hay con số nào trong bộ dữ liệu đại diện cho dữ liệu nội bộ thật của Xanh SM, và **720 trên 798 bản ghi là báo cáo vận hành sinh tự động** theo seed, cho các thành phố có thật.

Tầng grounding không phát hiện được điều đó, và đây không phải khiếm khuyết của nó: nó kiểm câu trả lời có dựa trên tài liệu được cung cấp hay không, chứ không có khái niệm tài liệu đó có đúng hay không. Một câu hỏi về doanh thu một thành phố sẽ nhận một con số, trích dẫn đúng một mã tài liệu có thật trong corpus, qua sạch mọi lớp kiểm tra — và là số bịa. Chính trích dẫn là thứ tạo ra lòng tin, nên một câu trả lời sai mà tự tin còn tệ hơn không trả lời: analyst mang con số đó vào báo cáo thật là đã bị nền tảng dẫn sai.

Biện pháp đã triển khai trong tuần: mọi câu trả lời được **chèn cảnh báo dữ liệu giả lập ở tầng guardrail**, tất định, tại mọi đường phục vụ kể cả cache và streaming. Không đặt trong system prompt, vì sinh văn bản là ngẫu nhiên, và một biện pháp bảo vệ báo cáo thật của đồng nghiệp thì không được phép ngẫu nhiên.

Cần nói rõ thêm: **thay corpus không phải là việc đổi file**. Guardrail hiện ghim access level theo cấu hình triển khai cho mọi caller, nên tài liệu nội bộ thật sẽ đọc được bằng bất kỳ virtual key nào, cho tới khi ánh xạ key → access level được thực thi.

### 4.3. Hai pilot, và tuần 4 làm cái thứ nhất

* **Pilot A — DA dùng thật, nhưng đề bài là đánh giá nền tảng.** Mọi câu trả lời kèm cảnh báo dữ liệu giả lập. Đo ba thứ: độ trễ dưới traffic người thật, tỷ lệ guardrail chặn oan trên câu hỏi thật (đối chiếu với 0% trên 144 câu gold), và tỷ lệ câu hỏi corpus không trả lời được.
* **Pilot B — có giá trị nghiệp vụ thật.** Cần tài liệu GreenSM thật, ánh xạ key → access level, và index lại. Phụ thuộc vào việc xin được tài liệu nên khởi động ngay ngày đầu, nhưng không đặt trong phạm vi tuần 4.

Sản phẩm chính của Pilot A là **danh sách tài liệu cần xin cho Pilot B**, có bằng chứng thay vì phỏng đoán. Để đo được điều đó, tuần này đã bổ sung counter `guardrail_answers_declined_total`: trước đó một câu trả lời kiểu "tài liệu không đề cập điều này" đi qua grounding với verdict `ok` và được đếm là thành công, **không phân biệt được với một câu trả lời hữu ích**. Counter này không chứa chữ nào của câu hỏi — đó chính là điều kiện để nó bật trong khi audit log phải tắt, vì câu hỏi của người thật có thể mang số điện thoại khách hàng hoặc tên tài xế.

### 4.4. Ngân sách và hình dạng phiên

Traffic người thật vào khoảng 0,01 req/s, nên dung lượng card không còn là ràng buộc: **một node g6.xlarge (L4) là đủ**, và rẻ hơn A10G khoảng 20%. Tổng hạ tầng khoảng **1,31 USD/giờ**.

Ngân sách còn lại dưới 40 USD, nên pilot được bố trí thành **ba buổi 4 giờ, hủy cụm giữa các buổi — khoảng 16 USD**, chừa lại ~24 USD cho nghiệm thu tuần 5–6. Analyst **không cầm API key**: chat UI giữ key ở phía server, nên việc Aurora bị hủy mỗi tối chỉ là một thao tác làm mới cấu hình UI chứ không làm gián đoạn người dùng.

### Nguyễn Gia Bảo

* **Hoàn thiện FinOps:** dựng lại đường cong TC2 bằng số đo A10G thay cho L4 — 12,5 req/s mỗi card ở 1,006 USD/giờ, thay cho 10 req/s ở 0,8048 USD/giờ — ghi chi phí thật theo từng phiên, và xác nhận giá niêm yết của API ngoài dùng để so sánh.

* **Làm cho hạn mức chi tiêu có hiệu lực:** chưa có `input_cost_per_token`/`output_cost_per_token` nào được cấu hình cho model self-hosted, nên LiteLLM tính spend bằng 0 và **`budget_usd` của cả bảy agent hiện không ràng buộc được gì**. Đặt hai giá trị này cũng chính là việc làm TC2 tính được bên trong LiteLLM thay vì trên bảng tính.

* **Chạy Pilot A và lập danh sách tài liệu thiếu:** ba buổi, thu tỷ lệ decline, tỷ lệ chặn oan trên câu hỏi thật, và phân phối câu hỏi thật so với bộ gold. Ghi lại mọi chỗ hành vi khác với OpenAI mà người dùng gặp phải.

* **Tách nút thắt gateway khỏi nút thắt GPU:** ở 50 req/s, LiteLLM đạt 0,96 core và thông lượng engine đạt 98% mức bão hòa cùng lúc, nên một lần chạy không phân biệt được hai nguyên nhân. Dùng profile 3 replica để đo lại: nếu p95 cải thiện thì gateway là ràng buộc, nếu không thì GPU.

### Nguyễn Lê Minh

* **Đưa endpoint lên mức dùng được thật:** bổ sung TLS và một địa chỉ ổn định không đổi theo từng phiên, rồi nới allowlist cho dải IP egress của văn phòng GreenSM. Việc cuối cần hỏi bộ phận IT nên có thời gian chờ không tự kiểm soát được — khởi động sớm.

* **Triển khai chat UI cho analyst:** giao diện tương thích OpenAI, giữ virtual key ở phía server, có tài khoản người dùng riêng để mỗi người xem được lịch sử của chính mình. Lịch sử hội thoại nằm ở đây chứ không nằm trong log hạ tầng, vì đó là nơi người gõ câu hỏi nhìn thấy và kiểm soát được nội dung của mình.

* **Triển khai profile HA 3 replica:** bổ sung Terraform cho Redis dùng chung, kiểm chứng hạn mức và router state được chia sẻ giữa các pod sau khi chủ động xóa một pod. Đây cũng là điều kiện để endpoint chịu được một lần cập nhật mà không đứt với người đang dùng.
