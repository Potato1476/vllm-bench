# LiteLLM, Aurora và cách sử dụng Makefile

Tài liệu này mô tả phần LiteLLM gateway và Aurora PostgreSQL đã được bổ sung vào repo,
đồng thời giải thích vai trò của `Makefile`. Các thay đổi mới chỉ nằm trong source code;
chưa có `terraform apply` hoặc triển khai Kubernetes nào được thực hiện.

## 1. Makefile dùng để làm gì?

`Makefile` là một danh sách lệnh tắt có tên. Mỗi lệnh tắt được gọi là một **target**.
Nó không phải tài nguyên AWS và cũng không tự chạy khi mở repo. Chỉ khi người dùng chạy
`make <target>` thì các câu lệnh bên dưới target đó mới được thực thi.

Ví dụ:

```make
data-plan:
	terraform -chdir=terraform/data plan
```

Khi chạy:

```bash
make data-plan
```

Make thực hiện theo thứ tự:

1. Chạy `terraform plan` trong thư mục `terraform/data`.
2. Không tạo tài nguyên vì `terraform plan` chỉ tính và hiển thị thay đổi dự kiến.

Mục đích của Makefile trong repo là giữ các lệnh dài, thứ tự triển khai và tham số chung
ở một nơi. Nhờ đó không cần nhớ lại toàn bộ lệnh Terraform, Helm, Kubernetes và AWS CLI.

### Cú pháp thường gặp

| Cú pháp | Ý nghĩa |
|---|---|
| `NAME ?= value` | Dùng giá trị mặc định nếu người dùng chưa truyền biến môi trường. |
| `export AWS_PROFILE` | Truyền biến này xuống Terraform và AWS CLI. |
| `target: dependency` | Chạy `dependency` thành công trước rồi mới chạy `target`. |
| Dòng bắt đầu bằng tab | Câu lệnh shell thuộc target. |
| `@command` | Chạy nhưng không in nguyên câu lệnh ra terminal. |
| `-command` | Nếu lệnh lỗi thì tiếp tục; thường dùng khi tài nguyên có thể chưa tồn tại. |
| `$$name` | Biến của shell; phải viết hai dấu `$` vì Make dùng một dấu `$` cho biến của nó. |

Ví dụ truyền tham số:

```bash
make litellm-diff MODE=solo-a
make gpu n=1
```

`MODE` và `n` là biến Make chỉ có hiệu lực cho lần chạy đó.

### Chọn AWS account

Makefile không hard-code AWS profile hoặc account ID. AWS CLI và Terraform sử dụng
profile từ môi trường hoặc cấu hình mặc định của máy. Trước khi thao tác, có thể chọn
profile và tự kiểm tra identity:

```bash
export AWS_PROFILE=vinai
aws sts get-caller-identity
```

Biến môi trường trên chỉ có hiệu lực trong shell hiện tại và không được ghi vào
Makefile.

## 2. Phân loại các target quan trọng

### Chỉ kiểm tra hoặc xem trước

Các target sau không tạo tài nguyên hạ tầng:

| Target | Tác dụng |
|---|---|
| `make help` | Liệt kê target và mô tả ngắn. |
| `make fmt` | Chuẩn hóa định dạng file Terraform trong repo. |
| `make validate` | Kiểm tra cú pháp/cấu hình Terraform, không kết nối backend. |
| `make lint` | Chạy TFLint cho ba tầng Terraform. |
| `make core-plan` | Xem thay đổi dự kiến của VPC, S3, ECR và budget. |
| `make data-plan` | Xem thay đổi dự kiến của Aurora. |
| `make plan` | Xem thay đổi dự kiến của EKS. |
| `make vllm-diff` | Render manifest vLLM, không cài vào cluster. |
| `make guardrail-diff` | Render service guardrail, không cài vào cluster. |
| `make litellm-diff` | Render manifest LiteLLM, không cài vào cluster. |

`plan` vẫn đọc state và metadata AWS, nhưng không tạo/sửa/xóa tài nguyên.

### Tạo hoặc cập nhật tài nguyên

Chỉ chạy các target này sau khi đã đọc plan tương ứng:

| Target | Thay đổi được tạo |
|---|---|
| `make core-up` | Apply tầng core: VPC, subnet, S3, ECR và budget. |
| `make data-up` | Apply tầng data: Aurora PostgreSQL và tài nguyên liên quan. |
| `make lab-up` | Apply tầng cluster: EKS và node groups. |
| `make vllm-up` | Cài/cập nhật vLLM bằng Helm. |
| `make guardrail-image` | Build và push pipeline guardrail vào ECR. |
| `make guardrail-up` | Cài/cập nhật OpenAI-compatible guardrail service. |
| `make litellm-up` | Cài guardrail, tạo Secret rồi cài/cập nhật LiteLLM. |
| `make monitoring-up` | Cài monitoring và các ServiceMonitor/rule. |
| `make ingress-up` | Công bố endpoint qua ingress của lab. |

Không target Terraform nào dùng `-auto-approve`. Terraform vẫn yêu cầu xác nhận trước
khi apply.

### Xóa hoặc hạ tài nguyên

Các target dưới đây có khả năng làm mất tài nguyên hoặc dữ liệu trong cluster:

| Target | Tác dụng |
|---|---|
| `make litellm-down` | Gỡ LiteLLM và xóa namespace `llm-serving`, gồm Secret trong namespace. |
| `make vllm-down` | Gỡ vLLM và xóa namespace `inference`. |
| `make monitoring-down` | Gỡ monitoring và PVC của monitoring. |
| `make lab-down` | Snapshot dữ liệu đo rồi hủy tầng EKS. Có bước xác nhận. |
| `make core-down` | Hủy VPC, ECR và artifacts bucket. Chỉ dành cho teardown cuối cùng. |

Không có target `data-down` vì Aurora chứa trạng thái LiteLLM và cần được bảo vệ khỏi
việc xóa nhầm. Aurora còn có `deletion_protection = true` và final snapshot mặc định.

## 3. Kiến trúc đã triển khai trong source code

Luồng request của phần hiện tại:

```text
Client
  -> Ingress/NLB
  -> LiteLLM gateway :4000
  -> Guardrail service :8080
     (RAG + input checks -> generation -> output checks)
  -> vLLM OpenAI-compatible API :8000
  -> model A hoặc model B

LiteLLM
  -> Aurora PostgreSQL :5432
     (virtual keys, quota, budget và spend history)
```

Redis và response cache đang được bỏ qua theo yêu cầu. Vì không có Redis để chia sẻ
router/quota state giữa nhiều pod, chart giới hạn LiteLLM ở `replicaCount: 1`.

### LiteLLM Helm chart

Chart mới nằm ở `charts/litellm/` và bao gồm:

- Image LiteLLM được pin version thay vì dùng moving tag.
- Ba mode tương ứng với vLLM: `shared`, `solo-a`, `solo-b`.
- Route cả hai model tới guardrail service; guardrail mới chọn `vllm-a` hoặc `vllm-b`.
- OpenAI-compatible endpoint trên port `4000`.
- Lab dùng ClusterIP sau ingress-nginx, tránh đụng HTTPS NodePort `30443` của controller.
- Production có thể override NodePort `30443` khi internal NLB trỏ trực tiếp LiteLLM.
- Bearer authentication bằng `LITELLM_MASTER_KEY`.
- Database URL và salt được đọc từ Kubernetes Secret, không ghi trong Helm values.
- Readiness, liveness, startup probes và giới hạn tài nguyên.
- Prometheus callback để xuất metric.
- Pod chạy trên node có label `workload=tooling`.

### Guardrail service

`services/llm_pipeline/` đóng gói pipeline từ nhánh main thành một OpenAI-compatible HTTP
hop. LiteLLM không còn gọi thẳng vLLM:

1. `pipeline.prepare` chặn injection, che PII, truy hồi, áp policy và dựng prompt.
2. Service chọn vLLM backend theo model và truyền `cache_salt`.
3. `pipeline.finalise` kiểm citation/grounding và PII đầu ra.
4. Chỉ câu trả lời đã đạt output checks mới được trả về LiteLLM.

Access level hiện là policy cố định của deployment (`internal-demo`), không đọc từ body
hoặc header do client gửi để tránh tự nâng quyền. Việc ánh xạ virtual key đã xác thực sang
access level riêng cho từng agent vẫn cần triển khai trước production.

Request streaming được buffer ở upstream rồi phát SSE sau khi kiểm tra xong. Nếu phát
từng token ngay khi vLLM sinh, hệ thống không thể thu hồi token chứa PII hoặc citation
bịa đã gửi cho client.

### Aurora PostgreSQL

Terraform mới nằm ở `terraform/data/`. Đây là tầng state riêng để EKS có thể bị hủy mà
database vẫn tồn tại.

Aurora được cấu hình như sau:

- Nằm trong cùng VPC do tầng `terraform/core` tạo.
- DB subnet group dùng các private subnet của VPC.
- `publicly_accessible = false`.
- Security Group chỉ nhận TCP `5432` từ CIDR các subnet chạy EKS application nodes.
- Aurora PostgreSQL mã hóa storage.
- Mặc định hai instance ở hai Availability Zone: một writer và một failover reader.
- Backup tự động 7 ngày.
- Deletion protection bật mặc định.
- Có final snapshot khi chủ động teardown.
- PostgreSQL logs được gửi tới CloudWatch Logs.
- Master password do RDS sinh và lưu trong Secrets Manager; password không nằm trong
  `terraform.tfvars`.

State của tầng data dùng key riêng:

```text
s3://vllm-bench-tfstate-mlops-lab/data/terraform.tfstate
```

### Kết nối LiteLLM với Aurora

Target `litellm-secret` thực hiện các bước:

1. Đọc endpoint, port, database name và secret ARN từ Terraform output của data tier.
2. Đọc username/password từ RDS-managed Secrets Manager secret.
3. URL-encode credential trước khi tạo connection string.
4. Tạo/cập nhật Kubernetes Secret `llm-serving/litellm-secrets`.
5. Giữ nguyên master key và salt cũ nếu Secret đã tồn tại.

Ứng dụng nhận các biến sau từ Secret:

```text
LITELLM_MASTER_KEY
LITELLM_SALT_KEY
DATABASE_URL
```

### Ingress và monitoring

Phần ingress đã thêm host `llm.*` trỏ tới service `litellm-private:4000`. Header
`Authorization: Bearer ...` được giữ nguyên để LiteLLM tự xác thực.

Monitoring đã bổ sung:

- `ServiceMonitor` cho LiteLLM.
- Prometheus recording rules cho request rate, error rate và latency.
- Alert khi LiteLLM không scrape được hoặc tỷ lệ lỗi tăng cao.
- Port-forward LiteLLM trên `localhost:4000` qua `make pf`.

Script `bench/scripts/smoke_litellm.sh` kiểm tra readiness, từ chối request không có key,
danh sách model, completion thường, streaming và endpoint metrics.

## 4. Danh sách file liên quan

| File/thư mục | Vai trò |
|---|---|
| `charts/litellm/` | Helm chart của LiteLLM gateway. |
| `charts/guardrail/` | Helm chart của guardrail service. |
| `services/llm_pipeline/` | HTTP adapter điều phối guardrail, retrieval và vLLM. |
| `terraform/data/` | Aurora PostgreSQL, subnet group, SG, log group và outputs. |
| `bench/scripts/smoke_litellm.sh` | Smoke test end-to-end cho gateway. |
| `k8s/monitoring/servicemonitor-litellm.yaml` | Cấu hình Prometheus scrape LiteLLM. |
| `k8s/ingress/` | Route ingress mới cho host `llm.*`. |
| `observability/rules/` | Recording rules và alerts cho LiteLLM. |
| `Makefile` | Các lệnh vận hành thống nhất. |
| `README.md` | Hướng dẫn tổng quan của repo. |

## 5. Trình tự triển khai sau khi được phê duyệt

Hiện chưa cần chạy các lệnh dưới đây. Khi muốn triển khai thật, trình tự an toàn là:

```bash
# 1. Chọn profile và xác nhận đúng account.
export AWS_PROFILE=vinai
aws sts get-caller-identity

# 2. Chỉ xem thay đổi Aurora.
make data-plan

# 3. Sau khi review plan, chủ động tạo Aurora.
make data-up

# 4. Build/push đúng phiên bản guardrail và render hai chart để kiểm tra.
make guardrail-image
make guardrail-diff
make litellm-diff MODE=shared

# 5. Cài guardrail + LiteLLM và chạy smoke test end-to-end.
make litellm-up MODE=shared
make litellm-smoke MODE=shared
```

Tài khoản `mlops_lab` cần quyền đọc/tạo RDS, EC2 networking liên quan, CloudWatch Logs,
Secrets Manager và quyền truy cập Terraform state S3. Trong lần kiểm tra read-only gần
nhất, identity đúng là account `043083732391` nhưng IAM chưa có
`rds:DescribeDBClusters`; quyền này cần được bổ sung trước khi plan/apply Aurora.

## 6. Trạng thái hiện tại

- Aurora chưa tồn tại theo xác nhận của người vận hành.
- Terraform cho Aurora đã được viết và `terraform validate` thành công.
- Redis và cache chưa được triển khai.
- Guardrail/RAG từ main đã được nối thành serving hop nhưng chưa build/push/deploy.
- Không có `terraform apply` nào được chạy.
- Không có Helm release hoặc Kubernetes resource mới nào được apply trong quá trình
  chuẩn bị source code này.
