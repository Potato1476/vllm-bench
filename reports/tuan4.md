# BÁO CÁO TIẾN ĐỘ DỰ ÁN – TUẦN 4

* **Dự án:** Nền tảng Serving LLM nội bộ tích hợp Guardrail & Observability
* **Thành viên:** Nguyễn Gia Bảo, Nguyễn Lê Minh
* **Thời gian báo cáo:** Tuần 4 (05/10 – 09/10/2026)

---

## Đối chiếu với mục tiêu đề ra trong báo cáo tuần 3

Kế hoạch tuần 4 là **FinOps** và **đưa nền tảng vào tay người dùng thật**. Kết quả tách làm hai nửa rõ rệt.

Phần FinOps **đã xong và các con số đã đổi**, theo hướng khắt khe hơn. Phần người dùng thật **chưa đạt**: TC4 vẫn 0/7, và lý do không nằm ở kỹ thuật.

Tuần 4 phát sinh một khối công việc lớn không có trong kế hoạch: **luồng CD**. Mentor của nhóm Deployment Quality Gate nêu rằng không có luồng CD đưa model mới ra phục vụ thì các cổng kiểm tra phía trước không có chỗ tác động. Nhóm đánh giá nhận xét này đúng và đã dựng phần đó trong tuần. Phần CI không dựng lại ở đây mà sẽ phối hợp với nhóm Deployment Quality Gate, nhóm đã có sẵn tầng quét và ký image.

Tuần này cũng là tuần tìm ra **nhiều lỗi nhất từ đầu dự án** — mười bốn lỗi, phần lớn thuộc loại báo một kết quả sai mà không báo lỗi ở đâu. Mục 4 liệt kê đầy đủ.

---

## 1. Năng lực serving — đo lại đầy đủ hơn tuần 3

### 1.1. Cấu hình và quy mô phép đo

Ramp 30 phút, 8 mức tải từ 1 đến 50 req/s, **34.354 probe**, cache ngữ nghĩa tắt có chủ ý, 4× A10G ở chế độ `solo-a`. Tuần 3 đo 15.001 request ở một mức tải duy nhất; tuần này đo cả đường cong.

### 1.2. TC1a — đạt ở mọi mức tải

| req/s | served | p50 (ms) | **p95 (ms)** | p99 (ms) | availability |
|---|---|---|---|---|---|
| 1 | 171 | 587 | 1036 | 1444 | 100,00% |
| 10 | 1700 | 563 | 1082 | 2007 | 99,94% |
| 20 | 3388 | 540 | 1881 | 6522 | 99,71% |
| 30 | 5086 | 556 | 1180 | 1827 | 99,71% |
| 40 | 6787 | 629 | 1660 | 5412 | 99,81% |
| 45 | 7631 | 641 | 1466 | 2450 | 99,74% |
| **50** | **8486** | **694** | **1590** | **2549** | **99,82%** |

p95 tại 50 req/s là **1590 ms** trên ngưỡng 3000 — còn dư 47% ngân sách, và **tốt hơn con số 2047 ms của tuần 3**. Nguyên nhân nhiều khả năng là hai sửa đổi trong tuần: bỏ ký tự đánh dấu `^` khỏi câu trả lời, và câu trả lời ngắn lại.

Đáng chú ý hơn con số: **đường cong gần như phẳng từ 1 đến 50 req/s**. Hệ thống chưa chạm điểm bão hoà ở mức tải mà đề bài yêu cầu.

### 1.3. TC1b — đạt, đọc theo cận dưới

**99,7228%** là cận dưới một phía 95% trên 34.354 probe với 79 lỗi. Riêng tại mức 50 req/s: 8501 probe, 15 lỗi, cận dưới **99,7091%**. Cả hai vượt ngưỡng 99,5%.

Công cụ cảnh báo rằng ba mức tải thấp có cận dưới **dưới** ngưỡng. Đây là **thiếu mẫu chứ không phải kém hơn**: 171 probe không thể chứng minh 99,5% ngay cả khi không có lỗi nào — cần tối thiểu 598 probe ở mức đó.

**79 lỗi đều là một loại duy nhất: guardrail chặn ở tầng grounding.** Không có lỗi máy chủ, không timeout, không lỗi mạng. Tỷ lệ chặn oan 0,23%, và nó tăng theo tải (3 req/s: 1 lần → 45 req/s: 20 lần). Đây là hành vi đúng, nhưng từ góc nhìn người dùng vẫn là mất khả dụng.

Phạm vi kết luận không đổi so với tuần 3: nó nói về **cửa sổ phiên đã quan sát**, không nói gì về hỏng hóc sau nhiều tuần chạy liên tục. Phần phủ 14 ngày lịch vẫn chưa làm.

### 1.4. TC3 — đạt, cả offline lẫn trên hệ thống thật

| | mẫu | chặn | chặn nhầm |
|---|---|---|---|
| Bộ offline | 294 | **100%** | **0%** (kể cả 34 câu gần giống tấn công) |
| Trên hệ thống | 349 | **100%** | 0 |

Lần chạy đối kháng trên hệ thống đạt availability 100% và 100% câu trả lời có trích dẫn.

### 1.5. Một chỉ số mới

**127 câu trả lời thoái thác** — model từ chối vì corpus không phủ được câu hỏi. Đây là chỉ số lần đầu đo được, và nó là đầu vào trực tiếp cho danh sách tài liệu cần bổ sung.

---

## 2. FinOps — Nguyễn Lê Minh

Hai báo cáo đã hoàn thành và nằm trong repo.

### 2.1. Chi phí vận hành thực tế — [`reports/aws-continuous-cost-2026-10-07.md`](aws-continuous-cost-2026-10-07.md)

Đối soát từ AWS Billing và AWS Price List, giai đoạn 01/09–06/10/2026:

* **86,79 USD** chi phí `Usage` trước credit cho toàn tài khoản — EC2 Compute 60,32; EKS 22,81; RDS 1,72; VPC 0,64; EBS 0,61; S3 0,34; Cost Explorer API 0,33.
* Đơn giá đã đối soát từ chính hoá đơn, không lấy từ bảng giá: `g5.xlarge` 1,006 USD/giờ, `g6.xlarge` 0,8048, `m7i.large` 0,1008.
* Nếu giữ nguyên cấu hình 4 GPU A10G và chạy **liên tục**: **107,21 USD/ngày**, **3.216,25 USD/30 ngày**.

Báo cáo nêu rõ một giới hạn quan trọng: **không được lấy chi phí lịch sử chạy lab theo giờ làm việc để suy ra chi phí production 24/7**. Cụm chỉ bật vào 8 ngày trong giai đoạn đó, và tag `project` chưa dùng được để phân bổ trong Cost Explorer, nên 86,79 USD xác nhận **đơn giá**, không phải tổng chi phí riêng của DA#51.

### 2.2. TCO production 24/7 — [`reports/production-cost-estimation-tco-2026-10-05.md`](production-cost-estimation-tco-2026-10-05.md)

Cấu hình tham chiếu **P2**: 2 GPU L4 + 4 CPU node + 2 Aurora instance + Redis có replica, trên 2 AZ. Chi phí giữ cấu hình **2.263,01 USD/30 ngày**.

Tại **10 req/s trung bình 24/7**:

| Phương án | Ngày | Tháng 30 ngày |
|---|---|---|
| **Tự host Qwen 7B, 2× L4 (P2)** | **76,47 USD** | **2.294,01 USD** |
| Qwen2.5-7B / OpenRouter–Phala | 167,24 | 5.017,28 |
| GPT-4o mini / OpenAI | 233,49 | 7.004,67 |
| Gemini 3.5 Flash-Lite / Google | 480,08 | 14.402,46 |
| Llama 3.1 8B / OpenRouter–DeepInfra | 64,11 | 1.923,24 |

Điểm hoà vốn so với Qwen API: **khoảng 3,00 req/s**, tương đương 258.856 request/ngày. Tại 10 req/s, tự host rẻ hơn **2.723,27 USD/30 ngày**, bằng **54,3%** tổng chi phí hệ thống.

Báo cáo giữ ba điều kiện đọc số mà nhóm đánh giá là phần giá trị nhất của nó:

* **Dòng Llama rẻ hơn nhưng chưa xác nhận tương đương chất lượng.** Giá rẻ hơn không tự động là phương án thay thế phù hợp.
* **Hoà vốn Llama 14,67 req/s vượt khả năng P2 giữ tải khi mất một AZ** (~10 req/s), nên không được quảng bá con số đó như một lời hứa.
* **Tỷ lệ tiết kiệm phải ghi rõ mẫu số** là toàn hệ thống hay riêng inference; hai tỷ lệ không thay thế nhau.

### 2.3. Token đã đo lại — các con số trên cần cập nhật

Cả hai báo cáo dùng **giả định 1300 token vào / 45 token ra**. Tuần này đã đo thật trên **34.275 request đã phục vụ**, đọc từ trường `usage` do API trả về:

| | giả định | **đo được 07/10** |
|---|---|---|
| Token vào | 1300 | **1142** |
| Token ra (trung bình) | 45 | **36,5** |
| Hệ số gọi lại | 1,0175 | **1,0168** — khớp, giữ nguyên |

Dùng trung bình chứ không dùng trung vị, vì chi phí tuyến tính theo token còn phân phối đầu ra bị lệch (p50 38, p90 65, max 192).

**Điều này làm TC2 khó hơn, không dễ hơn.** Ít token mỗi request nghĩa là API bên ngoài rẻ hơn mỗi request, còn tự host vẫn trả tiền **thời gian thuê máy** chứ không trả theo token — nên tự host cần nhiều tải hơn mới thắng:

| Theo mô hình P2 của Minh | công bố (1300/45) | **tính lại (1142/36,5)** |
|---|---|---|
| Chi phí API mỗi 1.000 model call | 0,1466 USD | **0,1282 USD** (−12,6%) |
| Điểm hoà vốn | ~3,00 req/s | **~3,43 req/s** |
| Tiết kiệm tại 10 req/s | 2.723,27 USD (54,3%) | **~2.236 USD (49,4%)** |

Các số tính lại là **ước tính từ chính các tiểu mục đã công bố của Minh**, không phải chạy lại toàn bộ mô hình; việc tính lại chính thức thuộc tuần 5. Kết luận không đổi: **TC2 vẫn đạt ở 10 req/s** với biên 49,4% so với ngưỡng 30%, nhưng biên hẹp lại.

Trên mô hình lab nhỏ hơn của nhóm (4 card, không có dự phòng AZ), cùng phép thay token làm hoà vốn dịch từ 1,75 lên **2,00 req/s** và ngưỡng tiết kiệm 30% từ 2,45 lên **2,85 req/s**.

---

## 3. Luồng CD — khối công việc lớn nhất của tuần

### 3.1. Vì sao làm

Mentor nhóm Deployment Quality Gate nêu: nếu không có luồng CD đưa model mới ra phục vụ thì các cổng chất lượng phía trước không có chỗ tác động. Nhóm đồng ý và dựng phần CD trong tuần.

**Phạm vi phân chia rõ:** DA#51 làm **luồng CD** — hợp đồng model, các cổng kiểm, kiểm lúc nạp, máy trạng thái rollout và Argo CD. **Phần CI sẽ phối hợp với nhóm Deployment Quality Gate**, nhóm đã có sẵn tầng quét Trivy, Conftest, ký Cosign và policy Kyverno. Dựng lại những thứ đó ở đây là trùng việc.

### 3.2. Hợp đồng model

Một phiên bản model là `models/<tên>/<version>/` gồm safetensors, `manifest.json` có sha256 từng file, chữ ký trên manifest, và `_READY` ghi **sau cùng**. Model do đâu mà có — tự huấn luyện, fine-tune, mua, tải về — **cố ý không nằm trong hợp đồng**. CD chỉ biết hợp đồng.

Bốn điều từ chối, mỗi điều có lý do cụ thể:

* **Chỉ nhận safetensors.** File `.bin`/`.pt`/`.ckpt` là torch pickle, và nạp một pickle là chạy code trong nó.
* **`name` và `version` phải khớp đường dẫn.** Một manifest đã ký là lời khẳng định về **một** version; chép sang thư mục khác thì nó vẫn ký hợp lệ và mọi hash vẫn khớp. Thiếu kiểm tra này, người chỉ có quyền **ghi** S3 (không cần quyền ký) có thể đặt trọng số tháng trước vào version hôm nay, và CD promote một bản lùi như một bản nâng cấp.
* **File không có trong manifest bị từ chối.** Chữ ký chỉ phủ manifest, nên file không được khai là nội dung chưa ký nằm ngay trong thư mục engine sẽ đọc.
* **Kích thước trọng số được tính từ file**, không đọc từ cấu hình khai tay — con số khai tay chỉ đúng chừng nào còn người ngồi viết nó.

**Thứ tự kiểm quan trọng và không phải thứ tự hiển nhiên:** manifest là dữ liệu **không đáng tin cho tới khi chữ ký được xác minh**. Phân tích nó trước nghĩa là đối chiếu số liệu của kẻ tấn công với file của kẻ tấn công, và chúng sẽ khớp.

### 3.3. Kiểm lại lúc nạp

Init container băm lại từng file sau khi sync xuống NVMe và từ chối khởi động nếu lệch hoặc thiếu manifest. Cổng kiểm phía trước chứng minh bucket đúng *tại thời điểm kiểm*; bước này chứng minh **đúng thứ GPU sắp mở**, trên mỗi lần pod khởi động. Nó cũng là thứ duy nhất chặn giữa việc một version bị ghi đè tại chỗ và việc bản ghi đè được phục vụ.

Đã chạy thử trong chính image sẽ dùng: file nguyên vẹn qua, sửa một byte trượt, xoá manifest trượt.

### 3.4. Trạng thái triển khai nằm trong git

`deploy/state.yaml` là nguồn sự thật duy nhất cho thứ đang chạy, và Argo CD đồng bộ cụm theo nó. Lý do: **cụm bị huỷ mỗi tối**. Bất kỳ thứ gì một bộ điều khiển rollout nhớ trong cụm — rằng ứng viên này đã bị loại, rằng đợt roll đang dở — đều chết theo nó, và cụm sáng hôm sau sẽ dựng lại bản đã bị loại. Đây cũng là lý do không dùng Argo Rollouts: nó giữ đúng trí nhớ đó ở nơi không sống sót.

Năm giai đoạn: `idle` → `evaluating` → `canary` → `promoting` → `watching`. Chuyển bước chỉ đi theo các cạnh đã khai, và `previous` được ghi lại tại đúng khoảnh khắc bản cũ còn tồn tại.

### 3.5. Ký bằng AWS KMS

Mỗi bên ghi model có một khoá KMS riêng (ECC P-256). Khoá bí mật không bao giờ rời KMS; quyền ký là quyền IAM `kms:Sign` trên đúng một khoá; CloudTrail ghi mọi lần dùng. Không đẩy lên log công khai Rekor — với trọng số huấn luyện trên dữ liệu nội bộ, công bố "tổ chức này vừa ký một artefact có digest X lúc Y" là lộ thông tin không mua lại được gì.

Khoá công khai nằm trong git có chủ ý: thêm một bên được phép đưa model vào production là thay đổi phải qua review.

### 3.6. Đã dựng, chưa chạy

Tới cuối tuần, phần đã dựng: hợp đồng model, các cổng, kiểm lúc nạp, máy trạng thái, Argo CD đồng bộ, hai track model.

**Chưa có và vẫn làm tay:** Job đánh giá, Job canary, Job theo dõi sau promote, và định tuyến có trọng số trong guardrail. Chuyển giai đoạn hiện vẫn gõ lệnh.

---

## 4. Mười bốn lỗi tìm ra trong tuần

Nhóm ghi lại đầy đủ vì phần lớn thuộc cùng một loại: **báo một kết quả sai mà không báo lỗi ở đâu**.

### 4.1. Lỗi làm sai lệch kết quả đo

**Bộ đo gọi model không được phục vụ.** Một lần chạy đối kháng báo **46% câu hỏi lành bị chặn** và availability 62,8%. Guardrail không làm gì sai — nó chưa bao giờ nhận những request đó. k6 bốc model ngẫu nhiên từ danh sách quyền của key, mà cụm chỉ phục vụ 7B, nên LiteLLM trả 400 và k6 đếm 400 trên traffic lành là "chặn oan". **795 trên 1801 request không bao giờ tới nền tảng.**

Đây **tệ hơn một lần chạy hỏng**: một sai lệch cấu hình được báo cáo thành một lỗi an toàn, đúng vào chỉ số mà pilot sẽ đọc. Đã sửa: bộ đo hỏi `/v1/models` trước khi có VU nào chạy và dừng hẳn nếu sắp gọi model không tồn tại.

**Cấu hình trong git không tái lập được số liệu trong git.** `gpu_instance_type` mặc định là `g6.xlarge` (L4) trong khi mọi kết quả báo cáo đều đo trên A10G. Nó chạy đúng chỉ vì một file `terraform.tfvars` bị gitignore trên một máy. Bốn L4 phục vụ **40 req/s** so với ngưỡng 50, và quota không cho thêm node thứ năm — nên ai clone repo rồi dựng lại sẽ **trượt TC1a 20% mà không có gì báo**.

### 4.2. Lỗi trả lời sai loại câu hỏi

**Bộ định tuyến warehouse đẩy câu hỏi định nghĩa sang SQL.** `"GBV được tính như thế nào?"` trả về `gbv_vnd=15323000` thay vì định nghĩa kèm trích dẫn. Không có lỗi nào; câu trả lời **sai loại** nhưng rất tự tin.

Đáng ghi hơn là cách nó được tìm ra: **bản sửa đầu tiên đo trên bảy câu do chính người sửa chọn và đã qua.** Đo lại trên **cả 144 câu gold** thì **60 câu (42%)** vẫn sai đường. Gốc rễ là bộ định tuyến khớp **danh từ trần** (`doanh thu`, `chuyến`, `tài xế`) vốn có trong **mọi** câu hỏi metric-catalog. Đã đổi sang khớp **động từ tổng hợp** hoặc **mốc thời gian cụ thể**: 0/144 sai đường.

### 4.3. Lỗi trong chính luồng CD vừa dựng

* **Ba release tranh một Deployment.** Mọi track render `mode: solo-a` nên đều tạo `vllm-a`. Trùng tên là nửa ồn ào; **trùng selector là nửa nguy hiểm** — Service của track này bắt pod của track kia, và câu hỏi gửi Qwen được Llama trả lời, không lỗi nào.
* **Tên Service không được chứa dấu chấm**, mà tên model thì có. Deployment nhận dấu chấm, Service thì không — nên nửa release render xong rồi mới hỏng.
* **Cổng kiểm tại chỗ của publisher không kiểm gì** mà vẫn báo qua. Dấu hiệu duy nhất là khối thông tin rỗng.
* **`promoting` bị bắt buộc phải có ứng viên**, nhưng promote làm ứng viên *trở thành* bản chính — nên trạng thái ghi ra không đọc lại được. Chỉ lộ ra khi test đi trọn một vòng qua file.
* **Một lần xuất bản bị gián đoạn chặn chính lần thử lại của nó**, với thông báo "version đã tồn tại" — sai, và đúng là câu khó nghe nhất vào sáng hôm sau.
* **Cờ `cosign` đã lỗi thời.** v3 bỏ hai cờ mà code đang truyền. Lỗi nổ ra **sau khi đã băm xong 5,34 GiB**. Nay bộ cờ được ghim bằng test.
* **Ingress vLLM trỏ tới Service không còn tồn tại.**

### 4.4. Lỗi nhỏ nhưng im lặng

* **`make agent-key` với tên gõ sai** in ra rỗng và thoát 0. Ai gán vào biến môi trường sẽ nhận key rỗng rồi gặp 401 và tưởng key bị thu hồi.
* **Cột "đã tiêu" luôn bằng 0** cho mọi key. Không phải chưa tiêu gì, mà là **chưa cấu hình giá token** nên LiteLLM tính spend = 0 và **không hạn mức nào trong `agents.json` có thể chặn được**. Một cột luôn bằng 0 đọc như "chưa dùng", không đọc như "không đo được".

---

## 5. Một hiểu nhầm của chính nhóm, cần đính chính

Báo cáo tuần 1 mô tả hạng mục bàn giao số 2 là *"Hệ thống vLLM phục vụ đồng thời nhiều mô hình **trên cùng một GPU** (mô hình lớn 7–8B và mô hình nhỏ 1.5–4B)"*.

Đối chiếu lại với đề bài: đề bài chỉ ghi **"Cụm vLLM đa mô hình, batching, cache"**. Không nói hai mô hình, không nói chung một GPU, không nói cỡ tham số.

Nhóm đã tự diễn giải chữ "đa mô hình" thành một ràng buộc chặt hơn đề bài, rồi thiết kế quanh ràng buộc tự đặt đó. Hệ quả cụ thể: `mode: shared` — chia một card cho hai model — được coi là bắt buộc, trong khi nó làm giảm trần thông lượng của chính mô hình phải đạt 50 req/s.

**Hướng đã chọn lại:** hai mô hình, **mỗi mô hình một node riêng**, mỗi cái một track rollout độc lập. Cách này đáp ứng "đa mô hình" như đề bài viết, và tránh việc mô hình nhỏ ăn vào phần của mô hình lớn.

---

## 6. Giới hạn hiện tại

* **TC4 vẫn 0/7.** Cơ chế đã đủ: key, hạn mức, client mẫu, hai profile guardrail, trang quản lý key. Phần còn lại không nằm ở code.
* **Chưa có TLS**, endpoint vẫn là HTTP thuần.
* **Địa chỉ chưa ổn định**, gắn với IP node và đổi sau mỗi lần dựng lại. Cloudflare Tunnel đã có chart nhưng chưa nối.
* **Hạn mức token chưa có hiệu lực** cho tới khi cấu hình giá token trong LiteLLM.
* **TC1b chưa phủ 14 ngày lịch.**
* **Quota GPU 16 vCPU = tối đa 4 node.** Mọi cấu hình phục vụ thêm một mô hình trên node riêng đều phải lấy bớt card của mô hình 7B, và trần tụt xuống 37,5 req/s. Mở khoá bằng cách xin nâng quota lên 20 vCPU.

---

## 7. Kế hoạch Tuần 5

Trọng tâm: **đóng TC4** và **tự động hoá nốt luồng CD**.

### Nguyễn Gia Bảo

* **TC4.** Bàn giao key và hướng dẫn tích hợp cho các đề án, mục tiêu ≥5 đề án gọi thật vào nền tảng. Đây là việc phối hợp, không phải việc code.
* **Hoàn tất CD.** Job đánh giá, Job canary, Job theo dõi sau promote, và định tuyến có trọng số trong guardrail.
* **Phối hợp CI với nhóm Deployment Quality Gate.** Chốt đường `include` gate của nhóm đó, và thống nhất policy `ML-001` kiểm trường nào để init container khai checksum đúng chỗ.
* **Địa chỉ ổn định và TLS** qua Cloudflare Tunnel — hiện là điều kiện chặn với mọi đề án ở mạng khác.

### Nguyễn Lê Minh

* **Cấu hình giá token trong LiteLLM**, để hạn mức có hiệu lực và TC2 tính được ngay trong gateway.
* **Tính lại TCO với token 1142/36,5** — mục 2.3. Cập nhật điểm hoà vốn và tỷ lệ tiết kiệm trong cả hai báo cáo chi phí.
* Tiếp tục phần warehouse: tính năng trả lời câu hỏi dữ liệu bằng SQLite read-only đã vào trong tuần.

### Ngân sách

Chi tiêu tới nay **86,79 USD** trên hạn mức 200 (đối soát trong báo cáo mục 2.1). Phiên demo dùng **2 node GPU** thay vì 4 — khoảng **2,4 USD/giờ** gồm cả tooling và Aurora, bằng một nửa cấu hình đo tải.
