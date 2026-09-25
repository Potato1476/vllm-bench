# BÁO CÁO TIẾN ĐỘ DỰ ÁN – TUẦN 2

* **Dự án:** Nền tảng Serving LLM nội bộ tích hợp Guardrail & Observability
* **Thành viên:** Nguyễn Gia Bảo, Nguyễn Lê Minh
* **Thời gian báo cáo:** Tuần 2

---

## Đối chiếu với mục tiêu đề ra trong báo cáo tuần 1

Nhóm đã hoàn thành **mục tiêu trọng tâm của kế hoạch tuần 2 nêu ở báo cáo tuần 1**: dựng nền tảng hạ tầng, hình thành đường xử lý từ gateway qua guardrail đến vLLM, thiết lập giám sát và có kết quả kiểm thử bước đầu. Các phép đo tải lớn và tiêu chí nghiệm thu toàn hệ thống được tiếp tục trong kế hoạch tuần 3.

## 1. Dựng hạ tầng và hoàn thiện full pipeline

Nhóm đã dựng hạ tầng và hoàn thiện toàn bộ pipeline từ client đến mô hình và quay lại client. Mã nguồn hiện bao phủ các lớp hạ tầng, gateway, guardrail, truy hồi, serving và giám sát. Các thành phần đã được cấu hình và nối với nhau ở mức ứng dụng; trạng thái triển khai trên AWS cần được kiểm chứng riêng trong từng phiên lab.

### 1.1. Hạ tầng và quy trình vận hành

* **Tách hạ tầng thành ba tầng Terraform:** `core` quản lý mạng, bucket artifacts, ECR và cảnh báo ngân sách; `data` quản lý Aurora PostgreSQL; `cluster` quản lý EKS và các node group CPU/GPU. Cách tách này cho phép hủy cụm tính toán sau phiên đo mà vẫn giữ dữ liệu, model weights và kết quả thử nghiệm.
* **Chuẩn hóa quy trình lab:** Bổ sung các lệnh dựng/hạ cụm, bật/tắt node GPU, triển khai từng dịch vụ, kiểm tra tài nguyên còn sót và theo dõi chi phí trong `Makefile`. Trước khi hạ cụm, quy trình xuất số liệu Prometheus lên S3 và đưa node group về 0 để tránh EC2 tự khởi tạo lại sau một lần hủy dở.
* **Đóng gói workload bằng Helm:** Tạo chart cho vLLM, LiteLLM và guardrail. Chart vLLM hỗ trợ hai mô hình, chế độ dùng chung GPU và chế độ chạy riêng từng mô hình. Model weights được đồng bộ từ S3; cấu hình serving có Prefix Caching và Chunked Prefill. Bộ kiểm tra khi render chart từ chối cấu hình phân bổ bộ nhớ GPU không khả thi.
* **Điều chỉnh cấu hình hai mô hình trên L4:** Trọng số FP16 của mô hình 7B và 1.5B không phù hợp để chạy chung theo mức chia bộ nhớ đã chọn. Nhóm đã chuẩn bị trọng số AWQ 4-bit trên S3 và chuyển chế độ dùng chung sang AWQ; các phép đo trên AWQ và FP16 sẽ được ghi tách biệt để tránh so sánh sai.

### 1.2. Luồng xử lý yêu cầu đầu cuối

Luồng đã hiện thực trong repository là **Client → Ingress → LiteLLM → dịch vụ Guardrail/RAG → vLLM → kiểm tra đầu ra → Client**. LiteLLM chịu trách nhiệm API key, định tuyến mô hình và cấu hình quota qua Aurora. Dịch vụ guardrail tập trung các bước cần đọc nội dung, gồm kiểm tra đầu vào, truy hồi, dựng prompt và kiểm tra câu trả lời trước khi phát ra ngoài. vLLM chỉ thực hiện suy luận.

Ở đầu vào, pipeline phát hiện prompt injection, che PII tiếng Việt và chuẩn hóa câu hỏi. Tầng RAG truy hồi tài liệu bằng BM25, có nhánh kết hợp dense retrieval khi dịch vụ embedding sẵn sàng; metadata về quyền truy cập và hiệu lực tài liệu được áp dụng trước khi đưa chunk vào prompt. Nội dung tài liệu truy hồi cũng được quét để loại chỉ dẫn độc hại. Ở đầu ra, hệ thống kiểm tra trích dẫn/căn cứ, quét PII rồi mới khôi phục các placeholder thuộc chính yêu cầu của người gọi.

Nhóm đã bổ sung Redis response cache cạnh dịch vụ guardrail. Cache chỉ lưu câu trả lời đã qua kiểm tra đầu ra; request chứa PII không được đưa vào cache. Khóa cache tách theo mô hình, quyền truy cập và phiên bản policy/corpus, đồng thời hỗ trợ xóa theo tài liệu khi nguồn thay đổi. Đây là lớp cache câu trả lời, tách với Prefix Caching của vLLM.

### 1.3. Quan sát và khả năng tái lập

Prometheus, Grafana và DCGM được cấu hình để theo dõi độ trễ, thông lượng token, hàng đợi, KV cache và tài nguyên GPU. Dashboard cho gateway/guardrail hiển thị kết quả ở phía người gọi cùng thời gian xử lý từng tầng. Nhóm cũng thêm trace theo các bước của một request qua Tempo để xác định nơi phát sinh độ trễ hoặc nơi một yêu cầu bị chặn. Các script audit metric giúp phát hiện chỉ số bị thiếu hoặc đổi tên trước khi dùng dashboard làm cơ sở đánh giá.

Hệ thống dữ liệu thử nghiệm gồm corpus tiếng Việt, mock warehouse, tập câu hỏi truy hồi và các bộ dữ liệu tải có độ dài token được cố định. Bộ đo lưu metadata của mỗi lần chạy và số liệu phiên lab lên S3, tạo cơ sở để lặp lại phép thử và so sánh cấu hình.

## 2. Kiểm thử bước đầu và kết quả ghi nhận

### 2.1. Kiểm tra chức năng và an toàn

* **Kiểm thử cục bộ:** Chạy `python3 -m unittest discover -s tests -q`; bộ kiểm thử gồm 49 ca, trong đó 44 ca chạy thành công và 5 ca được bỏ qua. Các ca kiểm tra bao phủ pipeline, guardrail, cache, tracing, truy hồi dense và công cụ đo availability. Đây là kiểm thử mã cục bộ, chưa thay thế kiểm thử tích hợp trên EKS.
* **Bộ kiểm thử đối kháng:** Chạy bộ chấm trên 343 mẫu, gồm 294 mẫu tấn công và 49 câu hỏi hợp lệ. Kết quả hiện tại là **294/294 mẫu tấn công bị chặn** và **0/49 câu hợp lệ bị chặn nhầm**. Bộ chấm đánh giá từng họ tấn công tại lớp chịu trách nhiệm; với PII ở đầu ra, nó dùng câu trả lời kiểm thử dựng sẵn, không phải câu trả lời do mô hình sinh. Do đó con số này phản ánh độ phủ của bộ lọc trên tập dữ liệu hiện có, chưa phải tỷ lệ an toàn đầu cuối trong vận hành.
* **Kiểm tra hợp đồng API:** Repository có các bài smoke và bộ Postman để kiểm tra xác thực, định tuyến, completion, streaming, guardrail và cache trên môi trường cloud. Dịch vụ guardrail đã được kiểm tra cục bộ với API tương thích OpenAI. Streaming hiện đợi toàn bộ câu trả lời được kiểm tra rồi mới phát ra; cần tính đặc điểm này khi đo thời gian nhận token đầu tiên từ góc nhìn client.

### 2.2. Đo hiệu năng ban đầu

Phiên ramp đầu tiên trên một GPU L4 với mô hình FP16 ghi nhận thông lượng tăng từ **66 lên 468 token/s** khi tăng số luồng đồng thời từ 4 lên 32. Hàng đợi vẫn bằng 0 tại các mức này; TTFT p95 tăng từ khoảng **0,24 s lên 0,43 s**, còn ITL p99 từ **0,075 s lên 0,085 s**. Vì chưa hình thành hàng đợi, đây là dữ liệu nền để nhận diện giới hạn phần cứng, chưa phải thông lượng cực đại của hệ thống.

Các chỉ số phần cứng cho thấy băng thông bộ nhớ ở khoảng **94%**, trong khi mức sử dụng tensor core chỉ **6–35%**. Nhận định ban đầu là giai đoạn sinh token đang chịu giới hạn băng thông bộ nhớ. Đây là lý do nhóm chuẩn bị AWQ 4-bit và bổ sung cấu hình đo riêng, nhưng **chưa có kết quả benchmark AWQ** để khẳng định mức cải thiện thực tế.

Trên tập truy hồi thử nghiệm, áp dụng policy metadata giúp tránh đưa tài liệu hết hiệu lực vào ngữ cảnh của các câu hỏi hiện hành. Chuẩn hóa câu hỏi làm nDCG tăng từ **0,508 lên 0,570** trên bộ eval đang dùng. Các số liệu này giúp kiểm tra từng khâu của pipeline; chất lượng câu trả lời cuối cùng vẫn cần đo khi dịch vụ embedding, gateway và mô hình chạy cùng nhau.

### 2.3. Giới hạn hiện tại

Pipeline đã có đầy đủ các lớp trong mã nguồn, nhưng các tiêu chí nghiệm thu về **p95 dưới 3 giây ở 50 req/s**, **uptime tối thiểu 99,5%** và **chi phí/token thấp hơn API ngoài** chưa được chứng minh. Mức tải thực đo mới đạt đỉnh khoảng **1,98 req/s**; dashboard theo từng agent còn phụ thuộc vào virtual key/nhãn agent trên gateway. Một số nhánh, như dense retrieval qua TEI và kiểm tra nâng cao bằng mô hình phụ, cần phiên triển khai và đo riêng.

---

## 3. Kế hoạch Triển khai Tuần 3

Trọng tâm tuần 3 là **stress test toàn bộ pipeline bằng k6 ở nhiều mức tải**, tăng dần đến mục tiêu **50 req/s**. Nhóm sẽ dùng cùng bộ câu hỏi, giới hạn token và cấu hình mô hình cho các lần chạy để kết quả giữa các mức tải có thể so sánh; ghi riêng số liệu của từng cấu hình GPU và mô hình.

### Nguyễn Gia Bảo

* **Chuẩn bị môi trường kiểm thử:** Dựng vLLM, guardrail và các thành phần phụ thuộc trên EKS; kiểm tra trạng thái pod, model weights và đường gọi API đầu cuối trước khi tạo tải. Chuẩn bị cấu hình AWQ dùng chung GPU và các chế độ chạy riêng để so sánh.
* **Xây dựng kịch bản k6:** Tạo bài stress test gửi yêu cầu qua gateway với tốc độ đến được kiểm soát; tăng tải theo các mốc dự kiến **1, 5, 10, 20, 35 và 50 req/s**, giữ mỗi mốc đủ lâu để quan sát trạng thái ổn định, rồi thực hiện một đợt tải đột biến. Ghi lại số request thực phát, số request hoàn thành và lỗi ở từng mốc.
* **Phân tích năng lực serving:** Đối chiếu độ trễ p95/p99, thông lượng token, hàng đợi, mức dùng KV cache và tài nguyên GPU để xác định điểm bắt đầu bão hòa. Tổng hợp kết quả AWQ so với mốc FP16 đã đo, đồng thời ước lượng cấu hình cần thiết để phục vụ 50 req/s nếu một GPU chưa đạt.

### Nguyễn Lê Minh

* **Kiểm thử gateway và guardrail dưới tải:** Hoàn thiện đường đi LiteLLM → guardrail → vLLM qua ingress; kiểm tra API key, quota, định tuyến, cache và phản hồi lỗi khi nhiều client gửi đồng thời. Phân biệt lỗi do giới hạn quota/rate limit với lỗi do hệ thống quá tải trong kết quả k6.
* **Giám sát và đối soát số liệu:** Kiểm tra Prometheus, Grafana và tracing trong suốt các đợt stress test; đối chiếu số request và độ trễ ở k6 với metric của gateway, guardrail và vLLM. Xuất snapshot số liệu từng phiên lên S3 để có thể phân tích lại sau khi hạ cụm.
* **Tổng hợp báo cáo kiểm thử:** Lập bảng kết quả theo từng mức req/s, gồm tỷ lệ thành công, lỗi, độ trễ p95/p99, thông lượng và tình trạng tài nguyên. Đánh giá riêng mục tiêu **p95 dưới 3 giây tại 50 req/s** dựa trên số đo thực tế, nêu rõ giới hạn hệ thống và các thay đổi cần thử ở tuần tiếp theo.
