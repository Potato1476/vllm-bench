# BÁO CÁO TIẾN ĐỘ DỰ ÁN – TUẦN 4

* **Dự án:** Nền tảng Serving LLM nội bộ tích hợp Guardrail & Observability
* **Nhóm thực hiện:** DA#51 (Nguyễn Gia Bảo, Nguyễn Lê Minh)
* **Thời gian báo cáo:** Tuần 4 (05/10 – 09/10/2026)

---

## TỔNG QUAN TIẾN ĐỘ

Trong Tuần 4, nhóm đã hoàn thành toàn diện các hạng mục kỹ thuật cốt lõi theo đề bài: **Xây dựng luồng CD GitOps đa mô hình**, **Đo kiểm năng lực serving toàn cụm đạt trần tải 50 req/s**, **Cấp phát hạ tầng Gateway phục vụ đồng thời các đề án**, **Triển khai giao diện Chatbot nghiệp vụ**, và **Hoàn thành báo cáo FinOps/TCO thực tế**.

### Bảng đối chiếu mục tiêu & Tiêu chí nghiệm thu

| Tiêu chí | Nội dung yêu cầu | Trạng thái Tuần 4 | Kết quả thực tế đạt được |
|---|---|---|---|
| **TC1a** | p95 latency < 3.000 ms tại 50 req/s toàn cụm | **ĐẠT** | **1.590 ms** tại 50 req/s (dư 47% ngân sách latency) |
| **TC1b** | Availability toàn cụm ≥ 99,5% | **ĐẠT** | **99,72%** (cận dưới 95% CI trên 34.354 probe) |
| **TC2** | Tiết kiệm chi phí ≥ 30% so với Cloud API | **ĐẠT** | Hoàn thành 2 báo cáo FinOps; tiết kiệm **49,4% – 54,3%** tại 10 req/s |
| **TC3** | Chặn tấn công ≥ 95%, chặn nhầm ≤ 2% | **ĐẠT** | Chặn tấn công **100%**, chặn nhầm **0%** trên cả offline và live cluster |
| **TC4** | Serving đồng thời ≥ 5 đề án DA | **ĐẠT** | Cấp phát và quản lý 8 Virtual Key riêng biệt cho 7 đề án DA + Pilot qua LiteLLM Gateway |
| **CD & Multi-model** | Luồng CD tự động hóa & phục vụ đa mô hình | **ĐẠT** | Argo CD App-of-Apps quản lý đồng thời Qwen2.5-7B và Qwen2.5-1.5B trên cụm 4 node GPU |

---

## 1. CƠ CHẾ CD VÀ ROLLOUT MÔ HÌNH MỚI (ARGO CD & GITOPS)

Luồng CD được thiết kế theo nguyên lý GitOps: file `deploy/state.yaml` trong Git là nguồn sự thật duy nhất (Single Source of Truth), Argo CD tự động phát hiện và đồng bộ trạng thái triển khai xuống cụm Kubernetes.

### 1.1. Các bước trong chu trình Rollout mô hình
Quá trình đưa một phiên bản mô hình mới ra phục vụ diễn ra an toàn qua 5 giai đoạn:

1. **`idle` (Trạng thái ổn định):** Phiên bản chính (`stable`) đang phục vụ 100% lưu lượng truy cập.
2. **`evaluating` (Đánh giá độc lập):** Triển khai mô hình ứng viên (`candidate`) trên một endpoint nội bộ riêng, hoàn toàn không nhận traffic production. Chạy các bài test tự động về độ chính xác, an toàn Guardrail và độ trễ. Nếu không đạt, hủy ứng viên và quay về `idle`.
3. **`canary` (Thử nghiệm lưu lượng nhỏ):** Mở một tỷ lệ nhỏ lưu lượng thật (ví dụ 10% – 25%) sang mô hình mới để kiểm chứng hành vi thực tế.
4. **`promoting` (Cập nhật chính thức):** Khi canary đạt chuẩn, mô hình ứng viên được thăng cấp thành bản `stable`. Argo CD thực hiện rolling update để thay thế phiên bản cũ.
5. **`watching` (Giám sát hậu kiểm):** Theo dõi chặt chẽ các chỉ số trong một khoảng thời gian sau khi roll xong; nếu phát sinh sự cố, có thể rollback về phiên bản trước đó ngay lập tức.

### 1.2. Cơ chế an toàn khi nạp mô hình
* **Chống mã độc:** Bắt buộc dùng `safetensors`, từ chối nạp các file Torch Pickle (`.bin`, `.pt`, `.ckpt`).
* **Xác thực toàn vẹn:** File `manifest.json` lưu trữ mã băm SHA-256 từng file, được ký số nội bộ qua AWS KMS (khóa ECC P-256).
* **Kiểm tra lúc nạp (Init Container):** Init Container tự động tính toán lại SHA-256 ngay trên ổ NVMe cục bộ trước khi bàn giao cho vLLM; từ chối chạy nếu phát hiện bất kỳ sai lệch nào.

### 1.3. Phục vụ đa mô hình trên cụm
* Argo CD quản lý theo kiến trúc App-of-Apps: Ứng dụng `root` điều phối đồng thời 2 ứng dụng độc lập `vllm-qwen2-5-7b-stable` và `vllm-qwen2-5-1-5b-stable`.
* Phân bổ trên 4 node GPU riêng biệt giúp phục vụ song song cả 2 mô hình mà không tranh chấp bộ nhớ GPU, đảm bảo tổng trần tải phục vụ của toàn cụm đạt 50 req/s.

---

## 2. QUẢN LÝ GATEWAY & PHÂN QUYỀN ĐA ĐỀ ÁN (TC4 - LITELLM)

Hạ tầng Gateway đã hoàn tất cấu hình phục vụ đồng thời cho các nhóm đề án khác nhau với chính sách kiểm soát truy cập và định mức rõ ràng.

### 2.1. Cấp phát Virtual Keys cho các đề án
* **Giao diện quản trị tập trung:** Triển khai LiteLLM Dashboard (`/ui`), cung cấp trang quản lý trực quan danh sách API Key tương tự Google AI Studio / OpenAI Platform.
* **Cấp phát đầy đủ 8 Virtual Key:**
  * 7 nhóm đề án: `da19`, `da20`, `da32`, `da39`, `da41`, `da44`, `da45`.
  * 1 tài khoản pilot nội bộ: `moc-da-pilot`.
* **Chính sách quản lý (RBAC & Quota):**
  * **Team:** Tập trung toàn bộ vào nhóm `moc-shared`.
  * **Phân quyền Model:** Cấu hình danh sách mô hình được phép gọi (`qwen2.5-7b`, `qwen2.5-1.5b`).
  * **Hạn mức (Budget):** Thiết lập hạn mức $10.0 cho mỗi đề án, đảm bảo cách ly ngân sách và chống lạm dụng tài nguyên.
  * **Bảo mật:** LiteLLM lưu trữ bản hash của khóa; toàn bộ chuỗi khóa bí mật bản rõ (`sk-...`) được bảo vệ trong Kubernetes Secret `llm-serving/agent-keys` để phục vụ bàn giao.

---

## 3. GIAO DIỆN CHATBOT NGHIỆP VỤ (OPEN WEBUI)

Triển khai giao diện Chat UI phục vụ người dùng cuối là các chuyên viên phân tích dữ liệu (GreenSM).

* **Tích hợp Gateway:** Kết nối trực tiếp vào LiteLLM qua tài khoản `moc-da-pilot`.
* **Lựa chọn đa mô hình:** Dropdown trên giao diện cho phép chuyển đổi linh hoạt giữa `qwen2.5-7b` và `qwen2.5-1.5b`.
* **Tự động hóa định danh quản trị:** Cấu hình hook `lifecycle.postStart` trong Helm Chart giúp tự động seed tài khoản admin `admin@da51.lab` từ Secret ngay khi khởi động pod; người dùng chỉ cần đăng nhập trực tiếp mà không phải qua màn hình thiết lập ban đầu.
* **Tối ưu hóa Guardrail:** Tắt toàn bộ các tác vụ background sinh ngầm của giao diện (sinh tiêu đề, tag, gợi ý câu hỏi) nhằm giữ tính trong sạch cho dữ liệu kiểm toán của pipeline Guardrail.

---

## 4. NĂNG LỰC SERVING & BENCHMARK THỰC TẾ (TC1a, TC1b, TC3)

Đo kiểm toàn diện đường cong hiệu năng toàn cụm từ 1 đến 50 req/s.

### 4.1. Cấu hình kiểm thử
* **Quy mô đo:** Ramp-up 30 phút, 8 mức tải từ 1 đến 50 req/s, tổng cộng **34.354 probe request**.
* **Phần cứng:** Cụm 4× NVIDIA A10G (EC2 `g5.xlarge`).
* **Điều kiện biên:** Vô hiệu hóa Semantic Cache để đo kiểm sức tải thực tế của inference engine và tầng Guardrail.

### 4.2. Kết quả đo kiểm chi tiết

| Tải mục tiêu (req/s) | Request phục vụ | p50 (ms) | **p95 (ms)** | p99 (ms) | Availability | Đánh giá TC1a |
|---|---|---|---|---|---|---|
| 1 | 171 | 587 | 1.036 | 1.444 | 100,00% | Đạt |
| 10 | 1.700 | 563 | 1.082 | 2.007 | 99,94% | Đạt |
| 20 | 3.388 | 540 | 1.881 | 6.522 | 99,71% | Đạt |
| 30 | 5.086 | 556 | 1.180 | 1.827 | 99,71% | Đạt |
| 40 | 6.787 | 629 | 1.660 | 5.412 | 99,81% | Đạt |
| 45 | 7.631 | 641 | 1.466 | 2.450 | 99,74% | Đạt |
| **50** | **8.486** | **694** | **1.590** | **2.549** | **99,82%** | **ĐẠT** |

**Kết luận kỹ thuật:**
1. **TC1a:** Tại mức tải tối đa 50 req/s của cụm, latency **p95 đạt 1.590 ms** (thấp hơn nhiều so với ngưỡng quy định 3.000 ms, còn dư 47% ngân sách độ trễ).
2. **TC1b:** Availability toàn cụm đạt **99,7228%** (tính theo cận dưới một phía 95% Confidence Interval trên toàn bộ 34.354 probe), vượt ngưỡng 99,5%. 100% các request từ chối đều do tầng Guardrail grounding chặn đúng quy tắc nghiệp vụ (tỷ lệ 0,23%), không có lỗi hệ thống 5xx hay timeout.
3. **TC3 (Guardrail an toàn):** Chặn chính xác **100%** các mẫu tấn công đối kháng, tỷ lệ chặn nhầm (False Positive) là **0%** trên cả 294 mẫu kiểm thử offline và 349 mẫu kiểm thử trực tiếp trên cụm.

---

## 5. BÁO CÁO FINOPS & TỐI ƯU HÓA TCO (TC2)

Đã hoàn thành 2 báo cáo phân tích chi phí chi tiết lưu trong repository:

### 5.1. Chi phí vận hành thực tế AWS (`reports/aws-continuous-cost-2026-10-07.md`)
* **Tổng chi phí giai đoạn 01/09 – 06/10/2026:** **86,79 USD** (chi phí sử dụng thực tế trước credit trên toàn tài khoản AWS).
* **Đơn giá tài nguyên thực tế đối soát từ hóa đơn:**
  * `g5.xlarge` (A10G): 1,006 USD/giờ.
  * `g6.xlarge` (L4): 0,8048 USD/giờ.
  * `m7i.large` (CPU): 0,1008 USD/giờ.

### 5.2. Dự toán TCO Production 24/7 (`reports/production-cost-estimation-tco-2026-10-05.md`)
* **Cấu hình tham chiếu sản xuất P2 (Multi-AZ):** 2 GPU L4 + 4 CPU node + Aurora Multi-AZ + Redis HA. Chi phí hạ tầng cố định: **2.263,01 USD/tháng**.
* **So sánh chi phí tại mức tải trung bình 10 req/s 24/7:**

| Phương án | Chi phí / Ngày | Chi phí / Tháng (30 ngày) | So với Tự host |
|---|---|---|---|
| **Tự host Qwen 7B (Cấu hình P2)** | **76,47 USD** | **2.294,01 USD** | **Gốc chuẩn** |
| Qwen2.5-7B qua Cloud API (Phala) | 167,24 USD | 5.017,28 USD | Đắt hơn 118% (Tự host tiết kiệm **54,3%**) |
| GPT-4o mini (OpenAI API) | 233,49 USD | 7.004,67 USD | Đắt hơn 205% (Tự host tiết kiệm **67,2%**) |
| Gemini 3.5 Flash-Lite (Google API) | 480,08 USD | 14.402,46 USD | Đắt hơn 527% (Tự host tiết kiệm **84,1%**) |

* **Điểm hòa vốn (Break-even):** Đạt tại mức tải khoảng **3,00 – 3,43 req/s** (~259.000 request/ngày). Khi tải thực tế từ 10 req/s trở lên, giải pháp tự host đạt tỷ lệ tiết kiệm **49,4% – 54,3%**, vượt xa ngưỡng yêu cầu 30% của TC2.

---

## 6. KẾ HOẠCH TUẦN 5

Trọng tâm của Tuần 5 là **kết hợp luồng CI hoàn chỉnh** và **viết tài liệu đặc tả, bàn giao hệ thống**:

### 6.1. Phân công công việc

* **Nguyễn Gia Bảo:**
  * Phối hợp với nhóm Deployment Quality Gate để kết nối tầng CI: Tích hợp quét lỗ hổng container image (Trivy), kiểm tra cấu hình Kubernetes (Conftest), ký image với Cosign và áp dụng policy kiểm soát an ninh container Kyverno (`ML-001`).
  * Soạn thảo tài liệu đặc tả kỹ thuật hệ thống (System Specification Document) mô tả chi tiết kiến trúc Serving, cơ chế Guardrail pipeline, luồng GitOps CD và mô hình bảo mật Model Contract.

* **Nguyễn Lê Minh:**
  * Soạn thảo tài liệu bàn giao và hướng dẫn tích hợp cho các nhóm đề án: Cung cấp tài liệu API Reference, code mẫu tích hợp (Python, cURL, LangChain) và quy trình yêu cầu cấp/quản lý Virtual Key.
  * Hoàn thiện tài liệu vận hành hạ tầng (Runbook): Hướng dẫn giám sát hệ thống qua Grafana, vận hành cụm vLLM đa mô hình và quy trình sao lưu/phục hồi dữ liệu.

### 6.2. Ngân sách & Tài nguyên
* Chi tiêu lũy kế hiện tại: **86,79 USD** / 200,00 USD ngân sách AWS được cấp.
* Đảm bảo duy trì chi phí tối ưu trong tuần tới bằng việc bật tắt cụm linh hoạt theo lịch làm việc và bàn giao.
