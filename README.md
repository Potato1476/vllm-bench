# vllm-bench

Đo năng lực và giám sát một dịch vụ suy luận vLLM chạy trên AWS EKS. Mục tiêu là trả lời
được ba câu hỏi bằng số liệu tự đo: một GPU NVIDIA L4 24 GB chạy Qwen2.5-7B-Instruct
(FP16) phục vụ được bao nhiêu request mỗi giây khi 95% request vẫn đạt ngưỡng TTFT p99,
điểm nghẽn nằm ở tài nguyên nào (GPU compute, bộ nhớ KV cache, hay CPU tiền/hậu xử lý),
và chi phí trên mỗi 1 triệu token là bao nhiêu. Repo này chứa hạ tầng Terraform, manifest
Kubernetes, bộ tạo tải và cấu hình quan sát của dự án.

Đây là môi trường **lab** của đề tài DA#51 (LLM Serving & Guardrails cho copilot MOC):
Terraform ba tầng, Aurora PostgreSQL, LiteLLM gateway, serving vLLM, tầng giám sát và
bộ đo. Phần
guardrail chạy thành một OpenAI-compatible service riêng giữa LiteLLM và vLLM.

## Yêu cầu công cụ

| Công cụ | Phiên bản tối thiểu | Ghi chú |
|---|---|---|
| Terraform | 1.6 | `required_version >= 1.6` trong `versions.tf` |
| AWS CLI | 2.13 | cần lệnh `aws eks get-token` cho provider `kubernetes` |
| kubectl | 1.30 | lệch tối đa một minor so với control plane 1.31 |
| tflint | 0.53 | plugin `aws` 0.48.0 tải bằng `tflint --init` |
| pre-commit | 3.5.0 | ghim trong `.pre-commit-config.yaml` |
| Python | 3.11 | target của ruff; `make datasets` cần `pip install transformers` |
| Helm | 3.14 | dùng từ Tuần 2 trở đi |

Tài khoản AWS phải có quyền tạo VPC, EKS, IAM role và EC2, và region đích phải thực sự
có họ máy GPU đã chọn — không phải region nào cũng có `g5`.

## Bootstrap backend

State nằm trên S3, khoá bằng `use_lockfile` (S3-native locking, có từ Terraform 1.10) nên
**không cần DynamoDB**. Khối `backend` không đọc được biến, nên bucket phải tạo **trước**
lần `init` đầu tiên và tên phải điền literal vào
[core/versions.tf:17-24](terraform/core/versions.tf#L17-L24).

```bash
export REGION=us-east-1
export BUCKET=vllm-bench-tfstate-mlops-lab   # phải duy nhất toàn cầu

# us-east-1 KHÔNG được truyền --create-bucket-configuration; region khác thì bắt buộc.
aws s3api create-bucket --bucket "$BUCKET" --region "$REGION"

aws s3api put-bucket-versioning --bucket "$BUCKET" \
    --versioning-configuration Status=Enabled
aws s3api put-public-access-block --bucket "$BUCKET" \
    --public-access-block-configuration \
    BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true
aws s3api put-bucket-encryption --bucket "$BUCKET" \
    --server-side-encryption-configuration \
    '{"Rules":[{"ApplyServerSideEncryptionByDefault":{"SSEAlgorithm":"AES256"},"BucketKeyEnabled":true}]}'
```

Bật versioning không phải thủ tục cho có: đó là đường lùi duy nhất nếu một lần `apply`
hỏng làm rách state. Tên bucket cố ý không chứa account ID, vì giá trị trong khối
`backend` không nội suy được nên nó sẽ nằm vĩnh viễn trong git.

Bucket `vllm-bench-tfstate-mlops-lab` đã được tạo và kiểm chứng (versioning bật, chặn
public access, mã hoá SSE-S3 mặc định) — chỉ chạy lại các lệnh trên nếu bạn dựng dự án
này ở một tài khoản khác.

Danh tính chạy bootstrap cần tối thiểu `s3:CreateBucket`, `s3:PutBucketVersioning`,
`s3:PutBucketPublicAccessBlock`, `s3:PutEncryptionConfiguration`. Bản thân `terraform
apply` còn cần quyền rộng hơn nhiều — xem mục "Bẫy đã biết".

Sau đó copy tfvars mẫu:

```bash
cp terraform/core/terraform.tfvars.example    terraform/core/terraform.tfvars
cp terraform/data/terraform.tfvars.example    terraform/data/terraform.tfvars
cp terraform/cluster/terraform.tfvars.example terraform/cluster/terraform.tfvars
```

## Thứ tự chạy

Lần đầu tiên trong đời repo:

```bash
make hooks        # cài git hook, chạy một lượt toàn repo
make validate     # init -backend=false rồi validate — không chạm AWS
make lint         # tflint
make init         # init thật, kết nối backend S3 (sau khi đã bootstrap)
make core-up      # tầng core: VPC, bucket, ECR, budget — chạy MỘT LẦN ở tuần 1
make data-plan    # chỉ xem thay đổi Aurora, không tạo tài nguyên
make data-up      # tầng data: Aurora PostgreSQL writer + reader — chỉ chạy khi đã duyệt plan
make lab-up       # tầng cluster: EKS + node group — mỗi đầu phiên
make kubeconfig   # trỏ kubectl vào cụm
```

Lần `apply` đầu tiên cần hai lượt, vì provider `kubernetes` được cấu hình từ output của
module EKS và không thể cấu hình khi cụm chưa tồn tại:

```bash
terraform -chdir=terraform/cluster apply -target=module.eks
terraform -chdir=terraform/cluster apply
```

Điều khiển node GPU trong ngày làm việc:

```bash
make gpu n=1       # kéo node group inference lên 1 node
make gpu n=0     # thu về 0 — chạy cuối mỗi ngày
```

`REGION` và `CLUSTER` đọc từ môi trường, có mặc định trong `Makefile`. AWS CLI và
Terraform sử dụng profile từ môi trường hoặc cấu hình AWS mặc định của máy; Makefile
không hard-code tên profile hay account ID.

Không target nào dùng `-auto-approve`. `make lab-down` bắt gõ `yes` sau khi đọc
checklist; `make core-down` bắt gõ `DESTROY-CORE` vì nó xoá cả bucket artifacts.

## Biến Terraform

| Biến | Tầng | Mặc định | Ý nghĩa |
|---|---|---|---|
| `region` | core | `us-east-1` | Giá GPU thấp nhất, capacity G dễ có nhất. |
| `budget_emails` | core | **không có** | Nhận cảnh báo 50/100/150/180 USD. Rỗng thì `apply` bị từ chối. |
| `budget_limit_usd` | core | `200` | Trần ngân sách tháng. |
| `instance_class` | data | `db.t4g.medium` | Kích thước writer/reader Aurora PostgreSQL. |
| `instance_count` | data | `2` | Một writer và một failover reader; đặt `1` chỉ cho lab không HA. |
| `backup_retention_days` | data | `7` | Số ngày giữ automated backup. |
| `deletion_protection` | data | `true` | Chặn xoá nhầm database stateful. |
| `cluster_name` | cluster | `da51-lab` | Tên cụm EKS. |
| `k8s_version` | cluster | `1.31` | Phải còn **standard support**: extended support đội phí cụm từ 0,10 lên 0,60 USD/giờ. |
| `allowed_cidrs` | cluster | **không có** | Danh sách /32 được gọi API endpoint. Cố ý không có mặc định. |
| `cpu_instance_type` | cluster | `m7i.large` | Gateway, guardrail, Prometheus, Grafana. |
| `gpu_instance_type` | cluster | `g6.xlarge` | L4 24 GB, 0,8048 USD/giờ. Ada nên có FP8. |
| `gpu_desired` | cluster | `0` | 0 ngoài giờ đo, 1 khi làm việc, 3 cho phiên kiểm chứng scale. |
| `gpu_l40s_instance_type` | cluster | `g6e.xlarge` | L40S 48 GB, 1,861 USD/giờ. |
| `gpu_l40s_desired` | cluster | `0` | Chỉ lên 1 trong phiên so sánh 4 giờ ở tuần 2. |
| `gpu_vcpu_quota` | cluster | `16` | Quota vCPU họ G. `plan` bị chặn nếu cấu hình vượt. |

Output đáng chú ý của data tier: `aurora_writer_endpoint`, `aurora_reader_endpoint` và
`master_user_secret_arn`. Password do RDS sinh/rotate trong Secrets Manager, không nằm
trong tfvars hay Terraform state. Output cluster gồm `kubeconfig_command`,
`bench_runner_role_arn` (gắn vào annotation của service account `benchmark:bench-runner`),
`vllm_role_arn` (gắn vào `inference:vllm`), và `artifacts_bucket_name`.

Tên bucket artifacts là `<project>-artifacts-<account-id>`, do tầng core quản lý. Lấy tên
thật ra biến môi trường thay vì gõ tay:

```bash
export ARTIFACTS=$(terraform -chdir=terraform/core output -raw artifacts_bucket_name)
```

Trọng số model phải được đẩy lên trước phiên đầu tiên:

```bash
make model-fetch          # resumable, retries, verifies sizes, then uploads
```

Or by hand. `huggingface-cli` was removed; the CLI is now `hf`. `HF_HUB_DISABLE_XET=1`
matters: HuggingFace's Xet backend drops the connection partway through a 15 GB transfer
often enough that an unattended download usually fails, with a `CAS Client Error` from
`us.aws.cdn.hf.co` deep in a traceback. Plain HTTPS is slower per connection and survives.

```bash
HF_HUB_DISABLE_XET=1 hf download Qwen/Qwen2.5-7B-Instruct \
    --local-dir /tmp/qwen7b --max-workers 4

# --exclude ".cache/*" is not optional: `hf` writes lock and resume metadata into
# .cache/huggingface/ inside --local-dir, and without the filter every session's
# initContainer syncs that junk back down along with the weights.
aws s3 sync /tmp/qwen7b "s3://$ARTIFACTS/models/Qwen2.5-7B-Instruct/" \
    --exclude ".cache/*"

# Confirm what landed: expect 4 safetensors shards and ~15.2 GB total.
aws s3 ls --summarize --human-readable --recursive \
    "s3://$ARTIFACTS/models/Qwen2.5-7B-Instruct/" | tail -5
```

Two notes on this repo specifically. It contains no `*.pth` and no `original/` directory,
so the exclusions carried over from Llama-style download recipes do nothing here. And the
`hf` CLI takes **one pattern per `--exclude` flag** -- writing
`--exclude "*.pth" "original/*"` silently reinterprets the second pattern as a filename to
download rather than to skip.

Set `HF_TOKEN` first if the download is rate-limited; unauthenticated pulls are throttled.

Re-running resumes: `hf download` skips files that are already complete, so a failure
halfway through costs only what is still missing. Do not trust exit code 0 on its own --
`make model-fetch` compares every file against the sizes the Hub reports, because a
truncated shard uploads to S3 without complaint and only surfaces much later as an
unhelpful load error inside vLLM.

**If the home connection makes this painful**, do the transfer inside AWS instead. The
tooling node pulls from HuggingFace at cloud bandwidth and writes to S3 in-region, so the
same 15 GB takes minutes rather than an hour, and it costs nothing extra because the node
is already running for the session.

## Quy trình phiên làm việc

Cụm EKS là thứ dùng xong thì bỏ. Ngân sách 200 USD không đủ để nó chạy liên tục 5 tuần,
nên mô hình vận hành là **3 phiên mỗi tuần, mỗi phiên khoảng 10 giờ**, hết phiên huỷ sạch
cụm và node. S3, ECR, Aurora và Secrets Manager sống qua các phiên. Aurora vẫn tính phí
khi EKS đã bị huỷ; theo dõi bằng `make cost` và chỉ giữ nó khi cần bảo toàn key/quota.

**Đầu phiên:**

```bash
make lab-up   # apply với gpu_desired=0
make kubeconfig
# cài lại Prometheus, Grafana, GPU Operator (task sau)
make gpu n=1          # chỉ khi thật sự sắp đo
```

**Cuối phiên:**

```bash
make gpu n=0        # trước tiên, ngay khi đo xong
# xuất chuỗi thời gian Prometheus ra S3  <-- BẮT BUỘC
make cost            # liếc xem tiêu bao nhiêu
make lab-down     # in checklist, bắt gõ `yes`, rồi destroy
```

### Xuất Prometheus ra S3 là bắt buộc, không phải tuỳ chọn

Huỷ cụm là huỷ luôn PVC của Prometheus, và **toàn bộ lịch sử chuỗi thời gian mất theo**.
Số liệu tổng hợp của mỗi lần chạy thì vẫn còn trên S3, nhưng phần dùng để *giải thích*
một kết quả — GPU utilisation theo thời gian, độ sâu hàng đợi, tỉ lệ hit KV cache, áp lực
bộ nhớ — thì không. Khi đó, muốn trả lời "vì sao TTFT p99 vọt lên ở mức tải này" bạn phải
chạy lại cả bộ thí nghiệm, tức là tốn thêm giờ GPU cho dữ liệu lẽ ra đã có.

Vì vậy `make lab-down` bắt gõ `yes` sau khi đọc checklist. Đó là chỗ duy nhất trong
quy trình mà một phím bấm vô ý làm mất dữ liệu không mua lại được bằng gì ngoài tiền GPU.

### Ghi chú cho task Helm sau này

Prometheus mặc định giữ dữ liệu 15 ngày trên PV khá lớn — vô nghĩa ở đây, vì cụm không
sống quá 10 giờ. Khi viết values Helm, đặt `retention: 7d` và PV 20 GiB. Phần này **chưa**
làm trong repo, chỉ ghi lại để không quên.


## Triển khai vLLM

```bash
make lab-up   # cụm lên, GPU vẫn 0
make kubeconfig
make gpu n=1          # dựng node g6.xlarge (~3-4 phút)
make vllm-up         # render manifest rồi apply
make smoke           # chứng minh nó trả lời đúng trước khi đo bất cứ thứ gì
```

**Điều kiện tiên quyết:** trọng số model phải có sẵn trên S3, nếu không initContainer sẽ
lỗi. Làm một lần, xem mục "Bootstrap backend".

### ARN và tên bucket không nằm trong repo

`make vllm-up` đọc chúng từ `terraform output` rồi truyền vào Helm bằng `--set`. Không
commit giá trị thật vì IRSA role được tạo lại theo mỗi cụm, nên ARN trong git vừa lộ
account ID vừa cũ đi trong im lặng. Chart từ chối render nếu hai giá trị đó rỗng.

### Trọng số: S3 là nguồn, PVC là bộ đệm

`initContainer` sync trọng số từ S3 vào **emptyDir trên NVMe instance store**, đánh dấu
bằng `/weights/.sync-complete` nên container khởi động lại thì bỏ qua.

`g6.xlarge` và `g6e.xlarge` mỗi con có 250 GB NVMe **miễn phí**, và node group đặt
`localStorage.strategy: RAID0` qua nodeadm để emptyDir nằm trên đó. So với PVC gp3 thì
nó được cả ba mặt: không tốn tiền EBS, đọc nhanh hơn nhiều khi vLLM nạp model, và
**không ghim AZ** — volume EBS sẽ trói node GPU vào một AZ, điều rất rủi ro khi capacity
họ G khan hiếm.

Đánh đổi: trọng số được sync lại khi **node** bị thay, chứ không phải khi cụm bị xoá.
Trong một phiên, container restart vẫn giữ cache; nhưng **rollout thì mất** — nên ma
trận tham số engine ở tuần 4 phải tính 1–3 phút sync cho mỗi bước.

Nếu emptyDir rơi nhầm xuống ổ gốc EBS thay vì NVMe, triệu chứng chỉ là "sync chậm đi" —
không ai đi điều tra. Vì vậy initContainer in `df -h /weights` sau khi sync xong.

vLLM chạy với `HF_HUB_OFFLINE=1`. Nếu bản sync thiếu file, pod chết ngay thay vì lặng lẽ
tải bù từ HuggingFace — một lần tải bù là một phép đo dùng model khác lần trước.

### Khác biệt so với Runbook

| Runbook | Repo này | Vì sao |
|---|---|---|
| `vllm/vllm-openai:v0.8.5` | `v0.29.0` | tag cũ đã lỗi thời, v0.29.0 là bản thật hiện có |
| Tải weights từ HuggingFace + HF token | Sync từ S3, không cần token | nhanh hơn, và mỗi phiên dùng đúng một bộ byte |
| PVC `hf-cache` 50Gi | emptyDir trên NVMe | NVMe có sẵn 250 GB miễn phí, nhanh hơn, không ghim AZ |
| `limits.cpu: "3"` | không đặt CPU limit | node đã taint, limit không cô lập gì mà chỉ gây CFS throttling làm TTFT đo được cao hơn thật |

## Triển khai LiteLLM gateway

Tài liệu chi tiết về các thay đổi, Aurora và cách đọc/sử dụng Makefile nằm tại
[`docs/litellm-aurora-implementation.md`](docs/litellm-aurora-implementation.md).

Gateway dùng cùng `MODE` với chart vLLM và lắng nghe cổng `4000`. Profile lab dùng
`ClusterIP` sau ingress-nginx; production có thể override sang NodePort `30443` cho
contract NLB trong sơ đồ. API yêu cầu Bearer key; master key và salt được sinh vào
Secret trong cluster, không đi qua Git hoặc Helm values.

```bash
make guardrail-image            # build/push pipeline đã test vào ECR (sau core apply)
make litellm-up                 # cài guardrail + LiteLLM, MODE=shared mặc định
make litellm-smoke              # auth + route + completion + streaming end-to-end
make creds                      # xem lại master key của lab
make litellm-diff MODE=solo-b   # render an toàn, không chạm cluster
```

Aurora PostgreSQL cung cấp virtual key, quota và spend tracking. `make litellm-up` lấy
credential từ data tier; `DATABASE_URL` chỉ là đường override khi cần. Target giữ nguyên
master/salt key đang có khi chạy lại nên không vô tình rotate key:

```bash
make data-up
make litellm-up
```

Luồng lab là LiteLLM → guardrail service → vLLM. Guardrail chạy `prepare` trước khi sinh
và `finalise` trước khi phát câu trả lời; request streaming vì vậy được buffer cho đến khi
output checks pass. Redis và response cache tạm thời nằm ngoài phạm vi; chart cố ý từ chối
`replicaCount > 1` để quota/router state không bị chia tách giữa các pod.

## Bộ đo

```bash
make datasets          # sinh 3 dataset + manifest checksum, đẩy lên S3
make datasets-check    # đối chiếu checksum trước mỗi phiên đo
make runner-image      # build + push runner lên ECR
```

### Bộ tạo tải là `vllm bench serve`, không phải mã tự viết

`bench/runner/run_bench.py` **không** tự tạo tải. vLLM v0.29 đã có sẵn `vllm bench serve`,
và nó đã tính đúng chỉ số trung tâm của dự án — **request goodput**, tỉ lệ request đạt
đồng thời ngưỡng TTFT và TPOT — cùng với ITL percentile, arrival Poisson, warm-up, và
chi tiết từng request.

Viết lại thứ đó bằng vài trăm dòng asyncio sẽ đặt các lỗi tinh vi vào **client** thay vì
vào engine, và khiến số đo không so sánh được với bất kỳ kết quả vLLM nào đã công bố.

`run_bench.py` bọc quanh nó và thêm ba thứ vLLM không làm:

1. **Chờ engine rảnh.** Bắt đầu đo khi sequence của lần chạy trước còn đang thoát là đo
   phần đuôi của lần trước. Nó hỏi Prometheus tới khi KV-cache về dưới 5% và không còn
   request nào chạy. Nếu không chờ được thì vẫn chạy, nhưng ghi `started_from_idle:false`
   vào kết quả để sau này loại ra được.
2. **Lineage.** Một con số JSON là vô dụng ở tuần 5 nếu không kèm tham số engine, tag
   image, phiên bản **và checksum** dataset, git commit.
3. **Đẩy lên S3 ngay.** Cụm bị huỷ cuối mỗi phiên; thứ gì còn trong pod là mất.

### Dataset: độ dài token là thiết kế thí nghiệm

Ba profile, cố định và công bố kèm checksum:

| Profile | prompt | output | Ép engine ở đâu |
|---|---|---|---|
| `chat` | 500 | 300 | decode chiếm phần lớn |
| `rag` | 4000 | 250 | prefill và KV-cache |
| `code` | 1500 | 800 | thời gian nằm trong engine lâu nhất |

Trên L4 24 GB, tài nguyên khan hiếm là KV-cache, mà KV-cache tiêu theo **số token đang
giữ**. Nên capacity của prompt 4000 token và prompt 500 token lệch nhau hơn một bậc. Để
độ dài trôi theo văn bản nguồn là biến thí nghiệm thành ngẫu nhiên.

Định dạng đầu ra đúng chuẩn `--dataset-name custom` của vLLM: mỗi dòng một JSON có khoá
`prompt` và `output_tokens`. Các khoá thừa (`id`, `profile`, `prompt_tokens`) là của ta và
vLLM bỏ qua.

**Mỗi prompt phân kỳ trong ~10 ký tự đầu.** Dòng đầu mang một id ngẫu nhiên riêng và thứ
tự câu bị xáo trộn theo từng mẫu. Lý do không phải thẩm mỹ: vLLM cache KV theo **tiền tố**
prompt. Nếu mọi prompt bắt đầu bằng cùng một đoạn mở đầu, phần lớn request được phục vụ từ
cache, TTFT sụp xuống, và bạn đang đo cache chứ không đo engine.

## Hai model, và chế độ đo

`charts/vllm/` là Helm chart nhận N model. Biến `MODE` quyết định model nào chạy và card
được chia thế nào:

| `MODE` | Chạy gì | `gpu-memory-utilization` | Dùng để |
|---|---|---|---|
| `shared` | cả hai model trên 1 GPU | A 0,65 · B 0,25 | **mặc định của lab và của pilot** |
| `solo-a` | chỉ lớp A, trọn card | A 0,90 | **đo X_A** cho việc tính số GPU production |
| `solo-b` | chỉ lớp B, trọn card | B 0,90 | đo X_B |

```bash
make vllm-up MODE=solo-a     # đo X_A
make vllm-up MODE=shared     # quay lại cấu hình pilot
make vllm-diff MODE=solo-b   # xem trước, không apply
```

### Vì sao cần `solo-*`

Production (§2.7) cho **mỗi model một node group riêng**, nên model 7B độc chiếm một card.
Lab thì dùng chung để tiết kiệm credit. Hai hình dạng khác nhau:

| | KV cache còn lại | Request lớp A đồng thời |
|---|---|---|
| 7B trọn card (`solo-a`) | 14,1 GiB | **203** |
| 7B dùng chung (`shared`) | 8,5 GiB | **123** |

Lớp A mất khoảng **40% năng lực** khi dùng chung, vì mỗi GiB cấp cho model nhỏ lấy thẳng
từ KV cache của model lớn. Nếu lấy X_A đo ở chế độ `shared` rồi đưa vào công thức

```
N = ceil(35 / (0,7 × X_A)) + ceil(15 / (0,7 × X_B))
```

thì N tăng gần gấp rưỡi và bạn đề xuất mua thừa GPU cho một kiến trúc không ai triển khai.

Phiên so sánh L4 với L40S ở tuần 2 **bắt buộc** chạy `solo-*` trên cả hai card — nếu không
bạn đang so hai khối lượng công việc khác nhau, mà đó là căn cứ duy nhất để chọn phần cứng
production.

### Chart từ chối cấu hình không chạy được

```
gpuMemory.shared.a + gpuMemory.shared.b = 1.05, which is >= 1.0.
Two vLLM instances on one card would OOM each other.
```

Tổng vượt 1,0 là model thứ hai sẽ OOM model thứ nhất — nhưng chỉ sau khi node GPU đã chạy
được 20 phút. Chart chặn ngay lúc render. Nó cũng từ chối `mode` sai và `artifactsBucket`
hay `roleArn` rỗng.

### Nhãn `model_class` — thứ làm hai model tách được ra

ServiceMonitor sao nhãn `model-class` của Service vào mọi chuỗi chỉ số. Không có nó,
`sum by (le)` sẽ gộp histogram của model 7B với model 1,5B và cho ra một p99 **không mô tả
model nào cả**.

vLLM vốn đã gắn `model_name`, nhưng chuỗi đó đổi mỗi khi đổi checkpoint; `model_class` là
`a` hoặc `b` mãi mãi. Bảng SLO đặt ngưỡng **khác nhau theo lớp**, nên alert cần một nhãn
không xê dịch:

| Alert | Lớp | Ngưỡng |
|---|---|---|
| `VllmSloBurnFastClassA` | A | lỗi > 2% **và** TTFT p95 > 800ms |
| `VllmSloBurnFastClassB` | B | lỗi > 2% **và** E2E p95 > 1,5s |
| `VllmInterTokenLatencyClassA` | A | TBT p99 > 60ms |

## Tầng giám sát

```bash
make monitoring-up      # GPU Operator (chỉ DCGM) + kube-prometheus-stack + rules
make pf                 # port-forward 9090 / 3000 / 8000
make audit-metrics      # BẮT BUỘC trước khi đo bất cứ thứ gì
```

`make monitoring-up` tự sinh mật khẩu Grafana vào Secret `grafana-admin` nếu chưa có và
in ra **đúng một lần**. Không có mật khẩu nào trong repo.

### Vì sao `audit-metrics` là cổng chặn, không phải bước tuỳ chọn

vLLM đổi tên chuỗi chỉ số giữa các phiên bản, và tên field của DCGM khác nhau theo bản
driver. Một chỉ số bị đổi tên **không báo lỗi**: recording rule trả về rỗng, panel trống,
alert không bao giờ kích hoạt. Trên dashboard nó trông y hệt "hệ thống khoẻ mạnh".

Các rule trong `observability/rules/` viết theo tên trong Runbook, **chưa** đối chiếu với
engine v0.29.0 đang chạy. Chạy `make audit-metrics` một lần sau khi vLLM lên, sửa mọi
dòng `MISSING`, ghi tên đúng vào `docs/metric-names.md` kèm tag image — rồi mới đo.

### Dashboard ba lớp

`observability/dashboards/vllm-3layer.json`, nạp tự động bởi `make monitoring-up` (hoặc
`make dashboards` riêng) qua ConfigMap có label `grafana_dashboard`.

Cách đọc là **từ trên xuống**, và đó là toàn bộ thiết kế:

| Hàng | Trả lời câu hỏi |
|---|---|
| Service level | Người gọi API có chấp nhận được không? TTFT p99, TBT p99, tok/s, tỉ lệ lỗi — kèm ngưỡng SLO tô màu |
| Engine state | Con số đó đến từ đâu? `waiting` rời khỏi 0 là điểm bão hoà; KV-cache và preemption nói vì sao |
| Hardware | Tài nguyên nào cạn? Khoảng cách giữa GPU util và tensor-core activity phân biệt nghẽn tính toán với nghẽn băng thông bộ nhớ |

Một con số độ trễ ở hàng 1 **không có ý nghĩa** cho tới khi hàng 2 cho biết nó có phải do
xếp hàng hay không. Vì vậy dashboard bật shared crosshair: rê chuột trên một đỉnh TTFT là
thấy ngay độ sâu hàng đợi và trạng thái GPU tại đúng thời điểm đó.

Ba panel đáng chú ý:

- **Requests: running vs waiting** — panel quan trọng nhất. `waiting` còn ở 0 nghĩa là
  engine hấp thụ hết tải; lúc nó nhấc khỏi 0 chính là capacity.
- **GPU utilisation vs tensor-core activity** — *khoảng cách* giữa hai đường mới là thông
  tin. Util gần 100% mà tensor thấp là nghẽn băng thông bộ nhớ ở pha decode, tăng batch
  không cứu được. Cả hai cùng cao mới thật sự là nghẽn tính toán.
- **Preemptions per second** — khác 0 nghĩa là engine đang đuổi sequence đang chạy. Số đo
  độ trễ tại mức tải đó không dùng được, vì request bị đuổi phải tính lại từ đầu.

Datasource là **biến template**, không hard-code UID. Cụm được dựng lại mỗi phiên nên UID
của datasource đổi theo; hard-code là dashboard trắng trơn ở phiên thứ hai.

Sửa dashboard trong UI để khám phá thì được, nhưng phải export ngược về
`observability/dashboards/` — nếu không nó chết cùng cụm.

### Ngân sách CPU của node tooling

Một `m7i.large` — **2 vCPU**, khoảng 1930m khả dụng:

| Thành phần | CPU request |
|---|---|
| guardrail (đường tới hạn, ngân sách 150ms) | **cần request tường minh** |
| kube-prometheus-stack + GPU Operator | ~600m |
| DaemonSet của kube-system | ~200m |
| còn lại | ~1100m |

Bộ tạo tải **không** chạy trên node này — nó có `t3.large` riêng, đúng theo kế hoạch, để
một client bị bóp CPU không bị nhầm thành engine bão hoà.

Nhưng 2 vCPU vẫn rất chật cho LiteLLM + Postgres + guardrail (có classifier chạy CPU) +
Prometheus + Grafana. Guardrail nằm **trên đường tới hạn** với ngân sách 150ms, mà
Prometheus nén dữ liệu theo từng đợt và ngốn CPU. Một đợt nén trùng burst tải sẽ đẩy p95
của guardrail vượt ngân sách, và nó sẽ trông như guardrail chậm chứ không như tranh CPU.
Đặt `requests` CPU tường minh cho guardrail, và cân nhắc `m7i.xlarge` ngay từ đầu.

Prometheus **không** đặt CPU limit, cùng lý do như vLLM: bị throttle thì nó bỏ lỡ scrape,
và khoảng trống rơi đúng vào dữ liệu dùng để giải thích kết quả đo.

### Khác biệt so với Runbook

| Runbook | Repo này | Vì sao |
|---|---|---|
| Tự viết `servicemonitor-dcgm.yaml` | Dùng ServiceMonitor của chart | Service của DCGM do operator tạo lúc chạy, tên port không đọc được từ repo; đặt sai tên port thì target im lặng không xuất hiện |
| `retention: 30d`, PV 100Gi | `7d`, PV 20Gi | cụm sống ~10 giờ mỗi phiên; chuỗi thời gian đã xuất ra S3 |
| `adminPassword` viết thẳng trong values | Secret sinh ngẫu nhiên | không để mật khẩu trong git |
| `alertmanager.config` chỉ có `inhibit_rules` | Chép đủ default + thêm 1 rule | ghi đè key này thay thế **toàn bộ** block, mất hết default |

### Bẫy EBS mồ côi

Volume EBS cấp động bị xoá bởi EBS CSI controller khi PVC biến mất. `terraform destroy`
xoá cụm — và xoá luôn controller — mà **không** xoá PVC trước. Volume sống sót ở trạng
thái detached và vẫn bị tính tiền.

Sau khi chuyển trọng số sang NVMe, PVC duy nhất còn lại là Prometheus 20Gi. Nhưng một
volume mỗi phiên × 3 phiên/tuần × 6 tuần vẫn là 360 GiB mồ côi nếu không dọn. `make lab-down` giờ
tự chạy `cleanup-volumes` **trước** khi destroy, rồi chạy `make orphans` sau đó để đối
chiếu. Chạy `make orphans` bất cứ lúc nào để kiểm tra.

## Kiến trúc mạng: hồ sơ tiết kiệm, không phải mẫu production

Không có NAT Gateway, và node chạy ở **public subnet** với IP công khai. NAT Gateway tính
khoảng 0,06 USD mỗi giờ bất kể có lưu lượng hay không; trong 5 tuần khoản đó lớn hơn mọi
mục chi khác trừ giờ GPU, mà việc duy nhất nó làm ở đây là cho node tải image.

Cái giá phải trả là thật: ENI của node thành thứ Internet chạm tới được, và hàng rào duy
nhất còn lại là security group của node. Hai chỗ siết bù lại:

- `cluster_endpoint_public_access_cidrs = [var.allowed_cidr]` — API endpoint chỉ nhận
  đúng một CIDR. Biến này **cố ý không có mặc định** để không thể quên.
- `node_security_group_additional_rules = {}` — không mở thêm gì ngoài các rule mà module
  EKS tự tạo cho node ↔ control plane và node ↔ node.

Chấp nhận được vì cụm bị huỷ cuối mỗi phiên, không chứa dữ liệu production, và chỉ tồn
tại khoảng 10 giờ mỗi lần. **Đừng bê nguyên mô hình này sang production** — ở đó câu trả
lời đúng là private subnet cộng NAT Gateway hoặc VPC endpoint.

Khai báo private subnet vẫn được giữ trong `network.tf` dù không dùng, để quay lại chỉ là
sửa hai dòng chứ không phải đánh số lại toàn bộ dải địa chỉ. S3 Gateway Endpoint vẫn giữ
(miễn phí giờ) nhưng đã chuyển sang gắn vào **public** route table, vì đó mới là nơi node
ở — để nguyên ở private route table thì nó không phục vụ gì cả.

## Kiến trúc: hai node group

Node CPU (`tooling`) chạy Prometheus, Grafana và bộ tạo tải; node GPU (`inference`) chỉ
chạy vLLM và bị taint `nvidia.com/gpu=true:NoSchedule`. Lý do không gộp: máy GPU chỉ có
4 vCPU, nếu bộ tạo tải chạy cùng máy thì nó tranh CPU với engine và làm TTFT đo được cao
hơn thực tế. Sai số đó tăng theo tải, nên nó **bẻ cong** đường cong kết quả chứ không chỉ
dịch cả đường lên — nghĩa là không thể trừ đi sau khi đo xong.

## Bẫy đã biết

- **GPU Operator xung đột driver.** AMI `AL2023_x86_64_NVIDIA` đã cài sẵn driver NVIDIA,
  container toolkit và device plugin. Khi cài NVIDIA GPU Operator lên trên, bắt buộc đặt
  `driver.enabled=false`, `toolkit.enabled=false`, `devicePlugin.enabled=false`. Bỏ qua
  bước này thì Operator cài đè driver thứ hai và node mất khả năng nhìn thấy GPU.
- **Luôn chạy `make gpu n=0` khi hết ngày làm việc.** Giờ GPU là khoản chi lớn nhất của
  dự án, và phần lớn thời gian là phân tích chứ không phải chạy tải. Một node `g6.xlarge`
  quên tắt qua cuối tuần tốn nhiều hơn cả tuần chạy đo thật.
- **`disk_size` không có tác dụng.** Module EKS v20 luôn dựng launch template riêng, nên
  input `disk_size` của node group bị bỏ qua. Kích thước ổ gốc phải đặt qua
  `block_device_mappings` — đó là lý do hai node group đều khai báo khối này.
- **Không có Interface endpoint cho ECR/STS/Logs.** Mỗi endpoint tính phí theo giờ theo
  AZ; với khối lượng của dự án 5 tuần thì đi qua NAT rẻ hơn. Chỉ Gateway endpoint cho S3
  được tạo, vì loại này miễn phí. Xem lại nếu lượng pull image vượt vài trăm GB.
- **Module EKS mặc định tạo KMS key và CloudWatch log group; cả hai đã bị tắt.** CMK cho
  Secret tốn 1 USD/tháng và `destroy` không xoá nó — key vào hàng chờ 30 ngày mà vẫn tính
  tiền, nên với 3 phiên/tuần bạn tích luỹ hàng chục key. Control plane log thì ingestion
  tính theo GB và log group sống sót qua `destroy` với retention 90 ngày. Hệ quả kèm theo:
  policy `vllm-bench-eks-admin` cố ý không có `kms:*` và `logs:*` — nếu bật lại hai tính
  năng này thì phải cấp thêm quyền, nếu không `apply` sẽ chết giữa chừng.
- **IP nhà thay đổi là bạn mất đường vào cụm — và việc này xảy ra thường xuyên.**
  `allowed_cidrs` ghim đúng một /32. Khi ISP cấp IP mới, mọi thứ chạm vào Kubernetes API
  server sẽ **treo rồi timeout**, không báo lỗi quyền, vì EKS chặn ở tầng mạng. Triệu
  chứng trông hệt như cụm hỏng hoặc mạng hỏng. Cách sửa:

  ```bash
  make fix-cidr    # đọc IP hiện tại, sửa tfvars, apply
  ```

  Lưu ý các lệnh Terraform tạo hạ tầng vẫn chạy bình thường khi IP sai, vì chúng gọi AWS
  API chứ không gọi API server của cụm. Thứ đầu tiên lộ ra vấn đề thường là
  `kubernetes_storage_class_v1` ở cuối lần apply — hoặc `kubectl` treo.

  Nới CIDR rộng hơn thường vô ích: nhà mạng ở Việt Nam xoay IP qua nhiều dải `/8` khác
  nhau, không phải trong cùng một khối.
- **Không dùng `gpu_capacity_type = "SPOT"` từ tuần 3.** Bị thu hồi máy giữa một bộ thí
  nghiệm là mất cả bộ, và chạy lại tốn nhiều giờ GPU hơn khoản chiết khấu spot tiết kiệm
  được. SPOT chỉ hợp lý ở tuần 1–2 khi đang dựng và thử.
- **Quyền IAM là thứ chặn bạn trước tiên, không phải mã Terraform.** Stack này tạo VPC,
  NAT gateway, cụm EKS, managed node group và nhiều IAM role. Một user chỉ có quyền đọc
  sẽ qua được `validate` và `plan` một phần, rồi chết ở `apply`. Kiểm tra nhanh trước khi
  mất thời gian: `aws ec2 create-vpc --cidr-block 10.99.0.0/16 --dry-run` phải trả về
  `DryRunOperation` (nghĩa là có quyền), không phải `UnauthorizedOperation`.
- **`AmazonEKSClusterPolicy` KHÔNG cấp quyền gọi API EKS.** Tên gây hiểu nhầm: đó là
  policy gắn vào *service role* mà EKS assume, không phải cho danh tính chạy Terraform.
  AWS không có managed policy nào cấp `eks:*` cho user — cả 13 policy có chữ "EKS" đều là
  service-role policy. Phải tự tạo customer-managed policy với `eks:*`. Kiểm tra bằng
  `aws eks list-clusters`; lưu ý IAM lan truyền chậm, vừa gắn policy xong có thể vẫn
  `AccessDeniedException` trong ~30 giây đầu.
- **Quota GPU mặc định là 0.** Tài khoản mới có quota "Running On-Demand G and VT
  instances" bằng 0 vCPU, mà `g6.xlarge` cần 4. Nếu không xin tăng trước, `apply` sẽ tạo
  xong VPC và cụm EKS (~15 phút) rồi mới chết ở node group `inference`. Kiểm tra:
  `aws service-quotas get-service-quota --service-code ec2 --quota-code L-DB2E81BA \`
  `--query 'Quota.Value'`. Xin tăng qua Service Quotas console; yêu cầu GPU thường mất
  vài giờ đến vài ngày, nên làm việc này **trước** mọi thứ khác.
- **StorageClass mặc định của EKS là gp2.** `storage.tf` tạo `gp3` và đặt làm mặc định.
  Nếu áp dụng lên cụm đã có sẵn, phải gỡ annotation default khỏi `gp2` trước, nếu không
  cụm có hai default class và scheduler chọn ngẫu nhiên.
