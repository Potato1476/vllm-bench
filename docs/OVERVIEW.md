# DA#51 — Bản đồ dự án

Tài liệu này để bạn định vị được mình đang ở đâu trong repo và chạy được thứ mình cần,
không đi sâu vào lý do từng quyết định. Phần lý do nằm trong `README.md` (573 dòng) và
trong chính các file mã — mọi module đều mở đầu bằng một khối giải thích **vì sao** nó
được viết như vậy. Khi tài liệu này và mã mâu thuẫn nhau thì tin vào mã.

Tài liệu chi tiết cho từng mảng:

| | |
|---|---|
| [`GUARDRAILS.md`](GUARDRAILS.md) | Mô hình mối đe doạ, ba lớp phòng thủ, bộ test 343 mẫu chia hai nửa, và số đo trên nửa giữ lại |
| [`TRACING.md`](TRACING.md) | Xem một request cụ thể đi qua 10 stage: Tempo, span model, và vì sao span không chứa nội dung |
| [`metric-names.md`](metric-names.md) | Từng metric của LiteLLM và guardrail: type, label, ý nghĩa |
| [`diagrams/request_flow.png`](diagrams/request_flow.png) | Sơ đồ luồng một request, cả nhánh cache HIT lẫn MISS |

---

## 1. Đề tài và tiêu chí nghiệm thu

Một nền serving LLM nội bộ dùng chung cho ≥7 agent copilot MOC, có guardrail và đo lường
được. Ngân sách AWS **200 USD**, 6 tuần.

| Tiêu chí | Trạng thái |
|---|---|
| p95 < 3s ở 50 req/s | **chưa chạm 50 req/s** (đỉnh 1,98). Xem §1a — ở độ dài trả lời thật, AWQ đạt 3s còn FP16 thì không |
| Uptime ≥ 99,5%, pilot 2 tuần | **chưa có gì**, và mâu thuẫn ngân sách: 2 tuần liên tục kèm GPU tốn ~338 USD, vượt cả 200 |
| Chi phí/1k token giảm ≥30% so với API ngoài | rule đã có (`platform:cost_usd_per_1k_output_tokens`); hiện **đắt gấp 3** vì thiếu tải |
| Chặn ≥95% bộ test prompt injection / PII leak | **100% trên 294 mẫu tấn công**, chặn nhầm 0% trên 49 câu hỏi thật — **đạt** |
| Dashboard latency/chi phí/token **theo agent** | chưa có nhãn `agent` — cần Aurora + virtual key |

### 1a. Câu trả lời dài bao nhiêu — và vì sao nó quyết định SLO

Đây là biến quan trọng nhất mà dự án từng bỏ trống, vì **độ dài câu trả lời là số hạng duy
nhất trong ngân sách p95 mà nền tảng tự chọn**. TTFT cố định, tốc độ decode cố định; thêm
GPU làm tăng thông lượng chứ không làm giảm ITL.

Đo trên 144 câu trả lời vàng trong `data/xanhsm_retrieval_mock/eval/retrieval_eval.jsonl`
— chính là đáp án tham chiếu viết cho corpus này:

```
p50 = 32 token     p90 = 44 token     max = 54 token
vi du: "Chỉ tính trip COMPLETED, có completed_at, distance > 0,2 km và không phải test/fraud."
```

Hợp lý, vì tài liệu nguồn chỉ dài 127–223 token và câu trả lời đúng là **nêu điều kiện
được hỏi**, không chép lại tài liệu.

Hệ quả, với TTFT 0,24s:

| Độ dài | FP16 (ITL 0,075s) | AWQ (ITL ~0,035s) |
|---|---|---|
| p50 = 32 | 2,64s ✅ | 1,36s ✅ |
| p90 = 44 | 3,54s ❌ | 1,78s ✅ |
| max = 54 | 4,29s ❌ | 2,13s ✅ |

**Ở độ dài thật, AWQ đạt mục tiêu 3s còn FP16 thì không.** Trước đây con số này trông vô
vọng chỉ vì hồ sơ tải giả định 250–300 token — nặng gấp 6–9 lần công việc thật.

Ba thay đổi đi kèm phát hiện này:

1. `prompt/build.py` nay **yêu cầu trả lời ngắn gọn 1–3 câu**. Nằm trong stable prefix nên
   gần như miễn phí về cache. Thiếu nó thì model tự do viết dài và SLO mất vì lan man.
2. `DEFAULT_MAX_TOKENS=192` trong guardrail — chặn cứng khi client không tự đặt. Đặt rộng
   so với nhu cầu vì đây là lưới chống sinh vô hạn, không phải công cụ ép ngắn; cắt sát
   quá sẽ chặt cụt trích dẫn, mà trích dẫn cụt là lỗi grounding.
3. Hồ sơ benchmark **`moc`** (4000 vào / 48 ra) phản ánh hình dạng thật. **Thêm chứ không
   thay** `rag`, vì thay sẽ âm thầm đổi ý nghĩa của mọi phép đo cũ.

Vẫn nên hỏi mentor để xác nhận, nhưng giờ câu hỏi đã có số liệu làm nền chứ không còn bỏ
ngỏ: *"đáp án tham chiếu của bọn em dài 32–54 token; câu trả lời MOC thật có cùng cỡ đó
không?"*

---

## 2. Bản đồ repo

```
terraform/core/        VPC, S3 bucket, ECR, cảnh báo ngân sách.  Tạo 1 lần, MIỄN PHÍ
terraform/cluster/     EKS + node group.                         TÍNH TIỀN THEO GIỜ
charts/vllm/           Helm chart chạy vLLM, tham số hoá 2 model
k8s/monitoring/        kube-prometheus-stack + GPU Operator (chỉ DCGM)
k8s/ingress/           ingress-nginx trên NodePort, mở URL ra ngoài
observability/         PrometheusRule + dashboard Grafana (dạng code)
bench/                 bộ tạo tải, dataset, script vận hành
rag/                   truy hồi: BM25 tiếng Việt, RRF, policy metadata, bộ đo
guardrails/            PII, prompt injection, kiểm tra căn cứ, pipeline ghép lại
prompt/                chuẩn hoá câu hỏi + dựng prompt cho prefix cache
data/                  corpus và warehouse tổng hợp  (Minh)
tests/                 kiểm tra hành vi của guardrails
```

**Hai tầng Terraform là chi tiết quan trọng nhất về chi phí.** `core` chứa những thứ miễn
phí và phải sống suốt 6 tuần (bucket đựng trọng số model, dataset, kết quả đo). `cluster`
chứa mọi thứ tính tiền theo giờ và **bị xoá mỗi tối**. Đừng bao giờ chạy `make core-down`
trừ tuần 6 — nó xoá luôn bucket.

---

## 3. Một request đi qua những gì

![Luồng một request](diagrams/request_flow.png)

Dựng lại bằng `make diagrams`. Nguồn: `docs/diagrams/request_flow.py`.

Sơ đồ vẽ **đúng những gì đang chạy trên EKS**, và đánh số trùng khớp với các mốc trong
`guardrails/pipeline.py` — nên đọc sơ đồ rồi mở thẳng mã ra đối chiếu được. Hai thứ **cố ý không có** vì chưa nối vào serving: semantic cache ở Redis và
known-answer detection. Dense **đã nối** vào `app.py` nhưng cần dựng TEI mới bật được
(`make embeddings-up`); thiếu nó thì truy hồi lùi về BM25.

### 3.0 Ba pod, và pod nào sở hữu cái gì

| Pod / namespace | Sở hữu | Nguyên tắc |
|---|---|---|
| `ingress-nginx` | TLS, định tuyến theo host, lọc theo IP | Không chạm nội dung |
| `llm-serving` — LiteLLM | API key, quota, router, nhãn agent | Không chạm nội dung |
| `llm-serving` — Guardrail | Bước 1–7 và 8–10, cộng truy hồi | **Mọi bước đọc nội dung đều ở đây** |
| `inference` — vLLM | Sinh văn bản | Chỉ sinh, không quyết định gì |

Ranh giới là *"ai được đọc nội dung câu hỏi"*. LiteLLM định tuyến theo metadata — khoá
API, quota, model đích — và không cần biết người dùng hỏi gì. Mọi bước cần đọc nội dung
nằm trong pod guardrail, nên chỉ có **một nơi để audit và một codebase để sửa** khi thêm
luật.

**Truy hồi cũng ở pod guardrail.** Lý do là bước 6: quét injection trên tài liệu phải đọc
chính các chunk vừa truy hồi. Tách hai bước luôn chạy cùng nhau nghĩa là chuyển 5 chunk ×
~900 ký tự qua mạng hai lần mỗi request.

### 3.0a Mười bước

**Trước khi sinh**

**1 · Chặn injection, nguồn = người dùng.** Gấp né tránh (zero-width, fullwidth, giãn chữ,
**không dấu**) rồi so 10 luật HIGH + 6 luật MEDIUM ở cả dạng có dấu và không dấu. **BLOCK
thì dừng hẳn** — không truy hồi, không GPU.

**2 · Che PII.** `0912345678` → `[PHONE_1]`, giữ ánh xạ cho bước 10. **Đặt trước truy hồi,
không phải trước model**: che muộn thì giá trị gốc vẫn nằm trong truy vấn tìm kiếm, trong
log và trong trace — ba bản sao ngoài model mà không ai coi là vấn đề của LLM.

**3 · Chuẩn hoá câu hỏi.** NFC, cắt cụm dẫn nhập (`"Cho tôi biết: "`), tách dấu hiệu phạm
vi. Chạy **trên văn bản đã che** để câu hỏi đưa vào truy hồi và câu hỏi đưa vào model là
một. Đo được: chuẩn hoá trước đưa tỉ lệ cặp diễn đạt lấy ra cùng tập chunk từ 18,5% lên
100%.

Một chi tiết cố ý: `"theo định nghĩa hiện hành"` bị cắt như khung, nhưng `"theo định nghĩa
CŨ"` được **giữ lại thành dấu hiệu phạm vi** và chuyển xuống bước 5 để mở khoá tài liệu
`deprecated`. Câu hỏi về lịch sử là câu hỏi hợp lệ.

**4 · Truy hồi.** BM25 trên unigram + bigram âm tiết, hợp nhất với dense bằng RRF **khi
có dịch vụ embedding**. Đường dense đã nối sẵn trong `app.py`: đặt `DENSE_INDEX_PATH` và
`DENSE_ENDPOINT` là bật, thiếu một trong hai thì lặng lẽ lùi về thuần BM25 — một embedder
chết phải làm câu trả lời kém đi, không được làm hỏng request.

**5 · Policy metadata.** Quyền: `access_level` không đủ → **bỏ hẳn, chỉ đếm**. Hiệu lực:
`status ≠ active` → **giữ lại và gắn lời nhắc** vào system prompt.

**6 · Chặn injection, nguồn = tài liệu.** Cùng bộ luật, mức độ nâng lên: câu ra lệnh cho
trợ lý là bình thường từ người dùng, là tấn công khi nằm trong tài liệu. **Bỏ chunk nhiễm,
giữ phần còn lại** — một tài liệu nhiễm không được phép giết mọi câu hỏi chạm tới nó.

Đặt **sau** bước 5 vì quét 50 ứng viên để bảo vệ 5 cái sống sót là gấp 10 lần việc cần làm.

**6b · Known-answer detection — có mã, mặc định tắt, chưa nối vào serving.** Chèn khoá 7
ký tự kèm chỉ dẫn *"lặp lại khoá này và bỏ qua văn bản bên dưới"*; model không trả về khoá
nghĩa là dữ liệu đã làm nó chệch hướng. Nó **không nhìn vào văn bản** nên bắt được tấn công
bằng bất kỳ ngôn ngữ nào mà luật không có mẫu. Giá: **một lần sinh thêm**, prefill lại toàn
bộ ngữ cảnh, và không dùng chung prefix cache với request chính. Chi tiết:
[`GUARDRAILS.md`](GUARDRAILS.md) §3.3.

**7 · Dựng prompt.** Prefix ổn định lên trước, datamarking cho nội dung chunk, `cache_salt`
theo `agent|access_level`. Chi tiết ở §3.1 và §3.2.

**vLLM sinh câu trả lời** — prefix cache KV nằm **bên trong** engine này.

**Sau khi sinh**

**8 · Kiểm căn cứ.** Trích dẫn `[MÃ]` không có trong ngữ cảnh → **chặn, chính xác tuyệt
đối**. Khẳng định không kèm nguồn → chặn. Lời từ chối thuần → cho qua, không cần nguồn.

**9 · Quét PII đầu ra.** Số định danh trong câu trả lời → chặn, bất kể nó từ đâu ra.

**10 · Khôi phục placeholder.** `[PHONE_1]` → `0912345678`, **chỉ những giá trị chính
người này đã nhập**.

**Xem mười bước này chạy trên một request cụ thể:** `make trace` bắn một request rồi in
link mở đúng trace của nó trong Grafana, nơi mỗi bước ở trên là một span, và bước nào
chặn request thì span đó tô đỏ. Span mang quyết định và con số, **không mang nội dung** —
chi tiết và lý do ở [`TRACING.md`](TRACING.md).

### 3.0b Ở đâu thì dừng, và dừng kiểu gì

| Bước | Phản ứng | Vì sao |
|---|---|---|
| 1 · injection người dùng | từ chối cả request | không có gì đáng cứu |
| 5 · quyền | bỏ im lặng, chỉ đếm | nêu tên đã là rò rỉ về việc cái gì tồn tại |
| 5 · hiệu lực | giữ lại **và nói ra** | người dùng cần biết định nghĩa đã đổi |
| 6 · injection tài liệu | bỏ chunk, giữ phần còn lại | một tài liệu nhiễm không được giết mọi câu hỏi chạm tới nó |
| 6b · known-answer | từ chối cả request | kiểm trên ngữ cảnh gộp nên không biết chunk nào |
| 8 · trích dẫn bịa | chặn câu trả lời | |
| 9 · PII đầu ra | chặn câu trả lời | |

### 3.0c Semantic cache — chưa có, và năm quy tắc phải theo khi thêm

Sơ đồ không có Redis vì nó chưa được nối. Khi thêm, đây là năm điều kiện để nó không
thành lỗ hổng. Ghi lại ở đây vì bốn trong năm chỉ lộ ra khi đã vẽ xong luồng.

**Cache câu trả lời khác hẳn prefix cache KV.** Redis lưu **câu trả lời hoàn chỉnh** và
một lần trúng bỏ qua cả truy hồi lẫn GPU; prefix cache lưu **khối KV** bên trong vLLM và
chỉ giúp request đã tới được đó.

**1. Chuẩn hoá trước khi dựng key.** `"Cho tôi biết: X"` và `"X"` là cùng một câu hỏi.
Trong thứ tự hiện tại, bước 3 đã đứng trước chỗ cache sẽ nằm, nên điều kiện này có sẵn.

**2. Key phải chứa `access_level`.** Câu trả lời trong cache được tính từ tài liệu mà
người gọi đầu tiên được phép đọc. Phục vụ nó cho người ít quyền hơn là rò chính những tài
liệu đó qua bản tóm tắt. Cùng lập luận với `cache_salt` của vLLM, chỉ ở tầng trên — và
Redis không tự làm giúp.

**3. Che PII trước khi chạm cache.** Bước 2 đã đứng trước, nên cũng có sẵn.

**4. Request có PII thì KHÔNG cache.** Hệ quả ngược của quy tắc 3, và là chỗ dễ sai nhất:

```
A hỏi  "tra cứu chuyến của 0912345678"  →  "tra cứu chuyến của [PHONE_1]"
B hỏi  "tra cứu chuyến của 0987654321"  →  "tra cứu chuyến của [PHONE_1]"   ← TRÙNG KEY
```

B nhận câu trả lời tính từ dữ liệu của A, rồi bước 10 thay `[PHONE_1]` bằng số của B — rò
dữ liệu **đến tay B khoác chính thông tin của B**, và không chỗ nào trông sai. Đưa giá trị
đã che vào key thì hết trùng, nhưng lại nhét dữ liệu định danh trở vào key, đúng thứ việc
che sinh ra để tránh.

**5. Cache hit không được đi vòng qua bước 8–9.** Giải bằng **bất biến** chứ không bằng
kiểm lại: chỉ ghi vào Redis **sau bước 9**, nên một lần trúng an toàn nhờ thứ đã được cho
vào. Kiểm lại mỗi lần trúng thì vứt đi phần lớn độ trễ mà cache được mua về.

**Còn một điểm chưa có lời giải:** câu trả lời trong cache tính từ corpus tại thời điểm T.
Khi một tài liệu chuyển `deprecated`, mọi entry dựa trên nó thành sai mà không ai biết.
Cần TTL hoặc xoá theo `document_id`.

### 3.1 Prompt được dựng như thế nào, và cache nằm ở đâu

vLLM băm KV cache theo **chuỗi block**: mỗi block gồm hash của block cha cộng token id
trong block, và **chỉ block đầy mới được cache** (mặc định 16 token). Hệ quả duy nhất cần
nhớ: **hai request dùng chung phần tính toán đúng bằng đoạn token trùng nhau tính từ vị
trí 0.** Một token khác ở vị trí 3 là vứt toàn bộ phía sau.

Nên prompt được xếp theo thứ tự **ít đổi nhất lên trước**:

```
┌─ system ──────────────────────────────────────────────────────┐
│ Luật cơ bản                        giống hệt mọi request      │  ← cache
│ Luật spotlight + ký tự đánh dấu    cố định theo phiên         │  ← cache
│ Lời nhắc tài liệu hết hiệu lực     chỉ khi bước 5 giữ lại gì đó    │  ← cache
│ ## Dữ liệu tham khảo                                           │
│ [METRIC-REV-001]                   ← MÃ NẰM NGOÀI vùng đánh dấu│
│ Gross^Booking^Value^và^doanh^thu…  ← nội dung đã datamark      │
│ [METRIC-TRIP-001]                  sắp theo document_id,       │
│ Định^nghĩa^chuyến^hoàn^thành…      KHÔNG theo điểm số          │
└────────────────────────────────────────────────────────────────┘
┌─ user ─────────────────────────────────────────────────────────┐
│ Câu hỏi đã chuẩn hoá               luôn khác → đặt cuối cùng   │
└────────────────────────────────────────────────────────────────┘
cache_salt = sha256("<agent>|<access_level>")[:16]
```

**Vì sao sắp chunk theo `document_id` chứ không theo điểm.** Hai cách diễn đạt của cùng
một câu hỏi thường lấy ra cùng tập chunk nhưng khác thứ tự. Sắp theo điểm thì prefix vỡ
ngay ở chunk đầu; sắp theo id thì hai prompt giống nhau đến tận câu hỏi. Đo được: **+0,9%**
— nhỏ, vì chỉ 18,5% cặp lấy ra cùng tập. Bước 2 mới là đòn bẩy thật, kéo con số đó lên
100% trên bộ eval.

**Vì sao mã tài liệu nằm ngoài vùng đánh dấu.** Model phải nhắc lại `[METRIC-REV-001]`
nguyên văn để bước 8 đối chiếu được. Datamark nó thành `[METRIC-REV-001]` có dấu chen vào
là không trích dẫn nào khớp được nữa.

**`cache_salt` là kiểm soát bảo mật, không phải nút tinh chỉnh.** vLLM trộn salt vào hash
của block đầu tiên, nên chỉ request cùng salt mới dùng lại KV của nhau. Hai người khác
quyền truy cập **không bao giờ** chia sẻ KV suy ra từ tài liệu chỉ một người được đọc.
Salt sinh từ `agent|access_level`, không từ id người dùng — salt theo người dùng an toàn
tương đương nhưng giết sạch cache.

### 3.2 Datamarking: nó là gì và tốn bao nhiêu

Mọi khoảng trắng trong dữ liệu truy hồi bị thay bằng một ký tự đánh dấu, và system prompt
nói cho model biết điều đó có nghĩa gì. Lấy từ Hines et al. (arXiv:2403.14720), biến thể
**datamarking** — bài báo nói rõ **không nên** chỉ dùng delimiting.

```
gốc      Gross Booking Value và doanh thu thuần
đánh dấu Gross^Booking^Value^và^doanh^thu^thuần
```

Ý tưởng: tín hiệu xuất xứ xuất hiện **liên tục** trong văn bản, không chỉ ở hai đầu, nên
model khó nhầm dữ liệu thành chỉ dẫn hơn. Bài báo đo được ASR giảm từ >50% xuống <2%.

Chi phí, đo bằng chính tokenizer của engine trên corpus này:

| marker | token | tỉ lệ | va chạm |
|---|---|---|---|
| U+E000 (bài báo khuyến nghị) | 522.425 | **1,75x** | 0 |
| `^` (đang dùng) | 377.361 | **1,26x** | 0 |
| `|` | 381.528 | 1,28x | 2.880 |

Số **ký tự** gần như không đổi, nên ai đo ký tự sẽ kết luận datamarking miễn phí. Chi phí
nằm hoàn toàn ở tokenisation: U+E000 ngoài từ điển BPE nên rơi xuống byte-level. Với ngữ
cảnh 5 chunk: **1.964 → 3.273 token**. Trên card mà nút thắt là băng thông bộ nhớ, đó là
thời gian prefill và KV cache mà batch không dùng được.

`^` được chọn tự động vì nó rẻ hơn và **không xuất hiện lần nào** trong corpus này —
kiểm tại thời điểm dựng index, không phải giả định. Corpus tương lai có mã nguồn hay LaTeX
thì nó tự rơi về U+E000.

## 4. Vòng đời một phiên làm việc

```bash
make lab-up            # dựng cụm, GPU vẫn ở 0        ~15 phút
make kubeconfig
make monitoring-up     # Prometheus, Grafana, DCGM
make gpu n=1           # BẬT GPU — từ giây này tốn 0,80 USD/giờ
make vllm-up           # MODE=shared | solo-a | solo-b
make smoke CLASS=a     # 7 kiểm tra, phải pass hết
make audit-metrics     # tên metric có khớp engine thật không

# ... làm việc ...

make lab-down          # tự xuất số liệu lên S3 rồi mới xoá   ~15 phút
```

**Nhìn `make gpu n=1` như nhìn công tắc tiền.** Chi phí khi mọi thứ chạy:

```
EKS control plane   0,10 USD/giờ
m7i.large (tooling) 0,10
g6.xlarge (GPU)     0,80   ← 80% hoá đơn
                    ────
                    1,01 USD/giờ
```

Sau `lab-down` là **0 USD/giờ**. Một ngày làm việc ~7 giờ tốn khoảng 7 USD.

Muốn xem dashboard: `make ingress-up` rồi `make ingress-url`. Mật khẩu: `make creds`.
Ingress mở cổng 30080 chỉ cho IP của người chạy lệnh; thêm người khác bằng
`make ingress-up EXTRA_CIDRS=<ip>/32`.

---

## 5. Phần RAG và guardrail chạy được ngay, không cần cụm

Toàn bộ chạy trên CPU, không tải model, không cần AWS:

```bash
make guardrails-test        # 30 kiểm tra hành vi
make rag-eval               # đo truy hồi trên 144 truy vấn vàng
make rag-eval-nopolicy      # cho thấy lớp metadata đáng giá bao nhiêu
make attacks-score FOLD=B   # chấm guardrail trên nửa bộ test GIỮ LẠI
make rag-data               # kéo corpus từ s3://.../datasets/v1/

Cần torch (một lần: `make dense-env`):

```bash
make dense-build         # mã hoá corpus, ~42s trên MPS
make dense-ablation      # lexical vs dense vs gộp, tách theo loại truy vấn
```
```

Nhánh **dense chưa có ai hiện thực**. `rag/retrieve.py` đã định nghĩa interface
`DenseRanker`; chỗ đúng để chạy nó là slot time-slicing thứ hai của con L4, cạnh vLLM.

---

## 6. Số liệu đã đo được

Đây là tài sản thật của dự án tính đến lúc này. Chuỗi thời gian đầy đủ nằm ở
`s3://<artifacts>/runs/20260921T102259Z-w1-ramp/`.

### Phần cứng — nghẽn ở đâu

```
luồng đồng thời    4      8     16     32
token/s           66    131    258    468      ← còn tăng gần tuyến tính
hàng đợi           0      0      0      0      ← chưa bao giờ hình thành
TTFT p95        0,24s  0,24s  0,24s  0,43s
ITL p99        0,075s 0,075s 0,075s 0,085s
```

```
GPU_UTIL (thô)        100%   ← vô dụng, bão hoà ở tải tầm thường
băng thông bộ nhớ      94%   ← ĐÂY là nghẽn
tensor core          6-35%   ← rảnh phần lớn thời gian
```

**Nghẽn là băng thông bộ nhớ, không phải tính toán.** Mỗi bước decode phải đọc toàn bộ
15,23 GB trọng số; L4 có 300 GB/s, nên sàn vật lý là **51 ms/token**. SLO 40ms **không
đạt được với FP16** ở bất kỳ số GPU nào. AWQ 4-bit đưa sàn xuống ~24ms.

Thêm một trần nữa: L4 bị **chặn công suất ở 72W**, xung tụt 2040 → 1400 MHz khi có tải.
Không phải quá nhiệt (70°C, ngưỡng ~85°C). Khi so với L40S (350W) nhớ ghi lại biến này.

### Trọng số AWQ — đã có trên S3, chưa đo

Hệ quả trực tiếp của đoạn trên: trọng số 4-bit đã được publish, và `mode: shared` lần đầu
tiên chạy được.

| | FP16 | AWQ 4-bit |
|---|---|---|
| Qwen2.5-7B-Instruct | 14,2 GiB | **5,2 GiB** |
| Qwen2.5-1.5B-Instruct | 2,9 GiB | **1,5 GiB** |
| Tổng | 17,1 GiB | **6,7 GiB** |
| Vừa `mode: shared` trên L4 22,5 GiB? | **Không** | Có, còn ~13 GiB cho KV cache |

Cả hai đều là bản AWQ **chính chủ Qwen**, apache-2.0 — không phải bản cộng đồng tái lượng
tử hoá, để lineage của một con số benchmark chỉ gồm một repo và một giấy phép.

`mode: shared` nằm trong `values.yaml` như trạng thái bình thường của lab và **chưa từng
chạy một lần nào**, vì model A cần 14,2 GiB còn phần card của nó là 14,6 GiB — phép tính
mà không ai thực hiện cho tới khi engine thử. Giờ `vllm.validate` làm phép nhân đó lúc
render và từ chối kèm số liệu, thay vì để hỏng thành lỗi cấp phát giữa cửa sổ đo.

**Hai thứ cần đo, chưa đo:**

1. **ITL.** Sàn vật lý FP16 là 51 ms/token, tức **SLO 40ms không đạt được ở bất kỳ số GPU
   nào**. AWQ đưa sàn xuống ~24ms. Đây không chỉ là chuyện nhét vừa hai model — nó là thứ
   quyết định SLO lớp A có khả thi hay không. Chạy `make smoke CLASS=a` rồi ramp lại.
2. **Chất lượng.** Lượng tử hoá 4-bit mất một phần độ chính xác, và bộ 144 truy vấn vàng
   cùng bộ tấn công 298 mẫu là công cụ sẵn có để định lượng. Đừng báo cáo throughput AWQ
   mà không kèm con số này.

Trọng số FP16 **vẫn giữ nguyên trên S3**. `QUANT=none` cho các lần chạy `solo-a`/`solo-b`
sinh ra con số sizing production — throughput từ trọng số 4-bit không so sánh được với
FP16, nên giữ cả hai mới có cái để đối chiếu.

### RAG — lớp metadata

```
              nDCG@10   R@10    dính bẫy
có policy      0,508    0,586     0,000
không policy   0,501    0,584     0,542
```

Corpus có 4 tài liệu `deprecated`/`draft` nói **đúng chủ đề** với đáp án đúng. Tắt policy
thì 54% truy vấn nạp định nghĩa hết hiệu lực vào ngữ cảnh, trong khi nDCG chỉ nhích 0,007.
Không mô hình nhúng nào phân biệt được — khác biệt nằm ở metadata, không nằm ở ngữ nghĩa.

### RAG — chuẩn hoá truy vấn

```
             R@10    nDCG    MRR   | trùng tập chunk  prefix chung
gốc          0,586   0,508   0,624 |      18,5%          45,1%
chuẩn hoá    0,616   0,570   0,734 |     100,0%         100,0%
```

Con số 100% là **cận trên**, không phải ước lượng production: bộ eval sinh biến thể bằng
ba mẫu dẫn nhập cố định. Phần tổng quát hoá được là nDCG +0,062 và MRR +0,110.

Chỗ yếu nhất hiện tại, cần nhánh dense để cứu:

```
loại truy vấn   R@1     R@10
retrieval      0,693   0,932   ← BM25 làm tốt
hybrid         0,177   0,425
reasoning      0,000   0,471   ← BM25 mù hoàn toàn
```

---

## 7. Bẫy đã biết — đọc trước khi mất hai giờ

**`kubectl` treo rồi timeout, không báo lỗi quyền.** ISP đổi IP (xảy ra 4 lần trong một
tuần). EKS chặn ở tầng mạng nên triệu chứng giống cụm hỏng. Sửa: `make fix-cidr`.

**Docker Desktop cướp `kubectl` context.** Bật Docker lên là `~/.kube/config` bị ghi đè,
`current-context` nhảy sang `docker-desktop`. Triệu chứng: `namespaces "monitoring" not
found`. Kiểm tra `kubectl config current-context` trước tiên.

**`terraform apply -var gpu_desired=1` không làm gì cả.** Module EKS đặt
`ignore_changes = [scaling_config[0].desired_size]`. Phải dùng `make gpu n=1` (đi qua AWS
CLI). Đây cũng là lý do đổi ngoài Terraform không gây drift.

**`terraform destroy` để lại rác tính tiền.** Volume EBS và load balancer do controller
trong cụm tạo ra, không do Terraform. `make lab-down` xoá chúng trước, theo đúng thứ tự.
Đừng chạy `terraform destroy` tay.

**Tên metric của vLLM đổi giữa các phiên bản.** v0.29 đổi 3 tên và xoá 1. Chạy
`make audit-metrics` sau mỗi lần nâng cấp engine, trước khi tin bất kỳ con số nào.

**EKS console không xem được Pod/Node.** Không phải lỗi RBAC. Console gọi Kubernetes API
từ hạ tầng AWS, mà IP đó không nằm trong `publicAccessCidrs`. Dùng `kubectl`.

**Mạng rớt giữa `terraform destroy`** để lại khoá state trên S3. Gỡ bằng
`terraform -chdir=terraform/cluster force-unlock <ID>` rồi chạy lại.

---

## 8. Ai làm gì, và việc tiếp theo

| Mảng | Trạng thái |
|---|---|
| Hạ tầng, serving, giám sát, bộ đo | xong |
| RAG + guardrail (BM25, PII, injection, grounding, cache) | xong, chạy CPU |
| Dataset tổng hợp (warehouse + corpus truy hồi) | Minh, xong |
| Nhánh dense (BM25 + embedding, RRF) | xong; **đã nối vào serving**, cần dựng TEI mới bật được |
| Bộ test tấn công 343 mẫu, chia hai nửa | xong — **100%** chặn, 0% chặn nhầm |
| Trọng số AWQ 4-bit + `mode: shared` | trọng số đã lên S3, chart đã chuyển, **chưa đo** |
| Trace request (Tempo, span theo từng stage) | xong |
| Rerank | **chưa ai làm** |
| Gateway LiteLLM (nhãn agent, đếm lỗi, định tuyến) | **chưa ai làm** |
| Probe uptime ngoài cụm (Lambda) | Minh, chưa làm |
| Panel chi phí/1k token | rule đã có, thiếu tải để có số |
| Bộ test tấn công ~200 mẫu tiếng Việt | chưa ai làm |

Thứ tự đề nghị: **dense + rerank** trước (nó mở khoá hai nhóm truy vấn đang hỏng), rồi
**gateway** (vì guardrail sẽ cắm vào đó, và nó mang theo nhãn `agent` mà đề bài đòi), rồi
mới tới phần đo chi phí và uptime.

Một câu nên hỏi mentor sớm, vì nó quyết định toàn bộ bài toán sizing: **câu trả lời trung
bình của MOC dài bao nhiêu token?** Với ITL đo được trên FP16, ràng buộc "p95 < 3s" chỉ đủ
cho khoảng 43 token đầu ra. Nếu thực tế là 300 token thì con số đó không cứu được bằng
cách thêm GPU — thêm GPU tăng thông lượng, không giảm ITL.

Trọng số AWQ vừa publish (§6) thay đổi chính phép tính này, vì nghẽn là băng thông bộ nhớ
và 4-bit cắt số byte đọc mỗi token đi khoảng bốn lần. **Đo lại ITL trên AWQ trước khi trả
lời mentor** — nhưng vẫn hỏi câu đó sớm, vì nếu đáp án là 300 token thì nó định hình lại
cả SLO lẫn cách chọn model, chứ không chỉ chọn lượng tử hoá.
