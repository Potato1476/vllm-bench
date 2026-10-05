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

### 2.3. Đa agent (TC4) — **chưa đạt, và trước đó nhóm đã đọc sai tiêu chí**

Cơ chế kỹ thuật đã sẵn sàng. Tầng Aurora dựng lại được, virtual key cấp được, và **cô lập theo key đã kiểm chứng trên hệ thống thật**: một key bị giới hạn mô hình nhận `This key can only access models=['qwen2.5-1.5b']` khi gọi sang mô hình khác. Nhãn `end_user` trong Prometheus đến từ **HTTP header `X-Agent-Id`**, không phải trường `user` trong body — LiteLLM v1.90.2 không đọc trường đó — và dashboard độ trễ, chi phí, token theo từng key đã có dữ liệu.

Nhưng **tiêu chí không đo cơ chế, nó đo người dùng**. Đề bài ghi nền tảng này là *"nền tảng cho DA#19/#20/#32/#39/#41/#44/#45"*, nên **"≥5 DA chạy trên nền tảng" nghĩa là ít nhất 5 trong 7 đề án đó thật sự gọi vào**. Bảy agent mà nhóm từng báo cáo (`moc-analytics`, `moc-datadict`…) là tên nhóm tự đặt theo các nhóm tài liệu trong corpus, không phải roster thật. Cấp key không làm tiêu chí này đạt.

**Hiện trạng: 0/7 đề án đã tích hợp.** `bench/agents.json` đã đổi sang đúng bảy mã đề án, và `clients/python/` được viết để một đội tích hợp trong vài phút.

Có một trở ngại kỹ thuật phải xử lý trước khi mời họ: nền tảng hiện **chỉ phục vụ một dạng workload** — hỏi đáp có trích dẫn trên corpus MOC. `finalise()` luôn chạy kiểm tra grounding, nên một đề án làm phân loại, tóm tắt hay trích xuất sẽ bị chặn ở tầng này trên gần như mọi request. Thứ có giá trị với các đề án khác là **chặn PII, chặn prompt injection và hạ tầng serving có đo đạc**, chứ không phải RAG. Hướng xử lý là gắn **profile guardrail theo từng virtual key**: copilot MOC giữ grounding bắt buộc đúng như đề bài yêu cầu, đề án khác dùng profile chỉ gồm PII và injection.


## 3. Giới hạn hiện tại

* **TC2 (chi phí/1k token) là một đường cong theo tải, chưa phải một con số.** Hòa vốn ở khoảng 1,5 req/s duy trì 24/7; đạt mức giảm 30% ở khoảng 2,25 req/s. Giá API ngoài dùng để so sánh là giá niêm yết, cần xác nhận.
* **Uptime 99,5% đã chứng minh trong một phiên đo, chưa phủ 14 ngày pilot.** Cần chuỗi probe theo lịch.
* **Profile HA 3 replica đã có nhưng chưa triển khai được:** cần Redis dùng chung, và chưa có Terraform cho ElastiCache.
* **Chi phí tuần 3 ước tính 25–30 USD** theo giá giờ và thời lượng phiên (4 node GPU ở 3,22–4,02 USD/giờ).

---

## 4. Kế hoạch Triển khai Tuần 4

Trọng tâm tuần 4 là **FinOps** và **đưa nền tảng vào tay người dùng thật**: một nhóm Data Analyst dùng thử trên giao diện chat, và các đề án khác bắt đầu tích hợp — thay vì nền tảng chỉ được gọi bằng công cụ đo. Lưu ý phạm vi: dữ liệu là mô phỏng, nên pilot đánh giá **nền tảng**, không phải thay thế công cụ tra cứu trong công việc.

### 4.1. Hiện trạng phần tương thích OpenAI

Phần lõi đã tương thích và đã kiểm chứng trên hệ thống thật: `/v1/models`, `/v1/chat/completions` ở cả chế độ thường lẫn streaming, xác thực Bearer, và mã lỗi theo chuẩn. Một ứng dụng viết bằng SDK OpenAI chỉ cần đổi `base_url` và `api_key`.

Năm giới hạn còn lại, và chúng định hình việc chọn người dùng thí điểm:

* **Chưa có TLS.** Endpoint hiện là HTTP thuần. Analyst đăng nhập bằng mật khẩu, nên đây là điều kiện chặn chứ không phải việc làm đẹp.
* **Địa chỉ không ổn định.** URL gắn với IP công khai của node và đổi sau mỗi lần dựng lại cụm.
* **Function calling không được hỗ trợ,** và đây là lựa chọn thiết kế chứ không phải lỗi: guardrail đầu ra kiểm tra trích dẫn và PII trên văn bản, mà một tool call không mang văn bản nào. Hệ thống từ chối rõ ràng thay vì trả về kết quả chưa được kiểm tra.
* **Streaming không chạy chữ.** Guardrail đệm toàn bộ câu trả lời rồi mới phát SSE, vì không thể thu hồi một PII hay một trích dẫn bịa đã gửi đi. Hệ quả là thời gian tới token đầu tiên bằng thời gian sinh cả câu trả lời — công cụ đo không quan tâm, người dùng thật sẽ báo đó là lag.
* **Chỉ phục vụ chat completions.** Các endpoint `/v1/embeddings` và `/v1/completions` chưa đi qua guardrail.

### 4.2. Ràng buộc lớn nhất: corpus là dữ liệu mô phỏng, và sẽ luôn như vậy

`data/xanhsm_retrieval_mock/manifest.json` ghi rõ không có chính sách hay con số nào trong bộ dữ liệu đại diện cho dữ liệu nội bộ thật của Xanh SM, và **720 trên 798 bản ghi là báo cáo vận hành sinh tự động** theo seed, cho các thành phố có thật.

Tầng grounding không phát hiện được điều đó, và đây không phải khiếm khuyết của nó: nó kiểm câu trả lời có dựa trên tài liệu được cung cấp hay không, chứ không có khái niệm tài liệu đó có đúng hay không. Một câu hỏi về doanh thu một thành phố sẽ nhận một con số, trích dẫn đúng một mã tài liệu có thật trong corpus, qua sạch mọi lớp kiểm tra — và là số bịa. Chính trích dẫn là thứ tạo ra lòng tin, nên một câu trả lời sai mà tự tin còn tệ hơn không trả lời: analyst mang con số đó vào báo cáo thật là đã bị nền tảng dẫn sai.

Biện pháp đã triển khai trong tuần: mọi câu trả lời được **chèn cảnh báo dữ liệu giả lập ở tầng guardrail**, tất định, tại mọi đường phục vụ kể cả cache và streaming. Không đặt trong system prompt, vì sinh văn bản là ngẫu nhiên, và một biện pháp bảo vệ báo cáo thật của đồng nghiệp thì không được phép ngẫu nhiên.

**Dữ liệu mô phỏng là trạng thái vĩnh viễn của đề tài này, không phải giai đoạn tạm.** Nhóm không có quyền truy cập tài liệu nội bộ của công ty, nên sẽ không có bước thay corpus bằng tài liệu thật. Mọi biện pháp đi kèm — dòng nhắc nguồn dữ liệu trong từng câu trả lời, banner trong giao diện — là cố định chứ không phải tạm thời.

Điều đó cũng gỡ hai ràng buộc từng được nêu: ánh xạ key → access level không còn chặn việc gì (vẫn là lỗ hổng cần ghi nhận khi bàn giao, nhưng không phải việc tuần 4), và việc đưa endpoint qua Cloudflare Tunnel là chấp nhận được lâu dài, vì không có tài liệu nội bộ nào đi qua đó.

### 4.3. Pilot đánh giá nền tảng

Pilot là **đánh giá nền tảng**, không phải công cụ tra cứu sự thật. Mọi câu trả lời kèm cảnh báo nguồn dữ liệu. Đo ba thứ: độ trễ dưới traffic người thật, tỷ lệ guardrail chặn oan trên câu hỏi thật (đối chiếu với 0% trên 144 câu gold), và tỷ lệ câu hỏi corpus không trả lời được.

Chỉ số thứ ba đo bằng counter `guardrail_answers_declined_total` bổ sung tuần này. Trước đó một câu trả lời kiểu "tài liệu không đề cập điều này" đi qua grounding với verdict `ok` và được đếm là thành công, **không phân biệt được với một câu trả lời hữu ích** — nên tỷ lệ câu hỏi nằm ngoài tầm phủ của corpus là con số vô hình. Counter không chứa chữ nào của câu hỏi, và đó chính là điều kiện để nó bật trong khi audit log phải tắt, vì câu hỏi của người thật có thể mang số điện thoại khách hàng hoặc tên tài xế. Kết quả cho biết corpus mô phỏng cần mở rộng về hướng nào để phủ được thứ người dùng thật sự hỏi.

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
