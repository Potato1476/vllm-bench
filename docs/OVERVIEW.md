# DA#51 — Bản đồ dự án

Tài liệu này để bạn định vị được mình đang ở đâu trong repo và chạy được thứ mình cần,
không đi sâu vào lý do từng quyết định. Phần lý do nằm trong `README.md` (573 dòng) và
trong chính các file mã — mọi module đều mở đầu bằng một khối giải thích **vì sao** nó
được viết như vậy. Khi tài liệu này và mã mâu thuẫn nhau thì tin vào mã.

---

## 1. Đề tài và tiêu chí nghiệm thu

Một nền serving LLM nội bộ dùng chung cho ≥7 agent copilot MOC, có guardrail và đo lường
được. Ngân sách AWS **200 USD**, 6 tuần.

| Tiêu chí | Trạng thái |
|---|---|
| p95 < 3s ở 50 req/s | đo được p95; **chưa chạm 50 req/s** (đỉnh 1,98) |
| Uptime ≥ 99,5%, pilot 2 tuần | **chưa có gì** — cần probe ngoài cụm |
| Chi phí/1k token giảm ≥30% so với API ngoài | **chưa có metric nào** |
| Chặn ≥95% bộ test prompt injection / PII leak | guardrail đã có; **bộ test chưa đủ** (7 mẫu) |
| Dashboard latency/chi phí/token **theo agent** | chưa có nhãn `agent` — cần gateway |

Đọc bảng này trước khi bắt tay vào bất cứ việc gì: bốn trên năm dòng còn trống.

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

```
câu hỏi người dùng
  │
  │  guardrails/pipeline.py  — 10 bước, thứ tự là thiết kế
  │
  ├─ 1  injection.inspect(source="user")     chặn thì dừng luôn
  ├─ 2  pii_vi.redact                        che TRƯỚC khi truy hồi và TRƯỚC khi ghi log
  ├─ 3  canonical.canonicalise               cắt cụm dẫn nhập, tách dấu hiệu phạm vi
  ├─ 4  retrieve.hybrid                      BM25 (+ dense khi có)
  ├─ 5  policy.apply                         quyền: bỏ hẳn. hiệu lực: giữ lại + báo
  ├─ 6  injection.inspect(source="document") bỏ chunk bị nhiễm, giữ phần còn lại
  ├─ 7  prompt.build                         prefix ổn định + hàng rào + cache_salt
  │
  │  vLLM sinh câu trả lời
  │
  ├─ 8  grounding.check                      trích dẫn bịa → chặn
  ├─ 9  pii_vi.scan(outbound)                số định danh lọt ra → chặn
  └─ 10 khôi phục placeholder                chỉ trả lại giá trị do chính người đó nhập
```

Hai chỗ đặt sai thứ tự là lỗi phổ biến, nên nói trước:

**Che PII đứng TRƯỚC truy hồi.** Nếu chỉ che trên đường gửi vào model thì giá trị gốc vẫn
nằm trong truy vấn tìm kiếm, trong log request và trong trace — ba bản sao nằm ngoài model
mà không ai coi là vấn đề của LLM.

**Quét injection trên tài liệu đứng SAU policy.** Quét 50 ứng viên để bảo vệ 5 cái sống
sót là lãng phí gấp 10 lần.

---

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
make guardrails-test     # 26 kiểm tra hành vi
make rag-eval            # đo truy hồi trên 144 truy vấn vàng
make rag-eval-nopolicy   # cho thấy lớp metadata đáng giá bao nhiêu
make rag-data            # kéo corpus từ s3://.../datasets/v1/
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
| Nhánh dense + rerank | **chưa ai làm** |
| Gateway LiteLLM (nhãn agent, đếm lỗi, định tuyến) | **chưa ai làm** |
| Probe uptime ngoài cụm (Lambda) | Minh, chưa làm |
| Panel chi phí/1k token | chưa ai làm |
| Bộ test tấn công ~200 mẫu tiếng Việt | chưa ai làm |

Thứ tự đề nghị: **dense + rerank** trước (nó mở khoá hai nhóm truy vấn đang hỏng), rồi
**gateway** (vì guardrail sẽ cắm vào đó, và nó mang theo nhãn `agent` mà đề bài đòi), rồi
mới tới phần đo chi phí và uptime.

Một câu nên hỏi mentor sớm, vì nó quyết định toàn bộ bài toán sizing: **câu trả lời trung
bình của MOC dài bao nhiêu token?** Với ITL hiện tại, ràng buộc "p95 < 3s" chỉ đủ cho
khoảng 43 token đầu ra. Nếu thực tế là 300 token thì SLO đó không đạt được với bất kỳ số
GPU nào, và biết sớm thì hơn.
