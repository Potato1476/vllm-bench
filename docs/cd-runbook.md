# CD — chạy từ đầu trên cụm mới

Phần đã dựng xong của [`docs/cicd-plan.md`](cicd-plan.md). Tài liệu này là thứ cần đọc khi
dựng cụm buổi sáng.

---

## Dựng cụm, theo thứ tự

```bash
make lab-up                 # EKS, 3 node tooling; GPU la g5.xlarge (A10G)
make kubeconfig
make monitoring-up          # GPU operator, Prometheus, Grafana
make gpu n=4
make cluster-config         # bucket + role vao cum, de Argo khong can terraform
make argocd-up              # Argo CD + app-of-apps; tu day git dieu khien viec trien khai
```

Sau `argocd-up`, **không chạy `make vllm-up` nữa**. Argo CD đọc
`deploy/argocd/apps/` và tự dựng vLLM đúng bản ghi trong `deploy/state.yaml`. Chạy
`vllm-up` lúc này là tạo một release Helm thứ hai tranh chấp với release của Argo.

```bash
make argocd-status          # dong bo chua, khoe khong, state.yaml dang noi gi
make litellm-up             # gateway van cai bang make
make guardrail-up
```

Theo dõi lần đồng bộ đầu:

```bash
kubectl -n argocd get applications -w
kubectl -n inference get pods -w      # cho 20 phut: pod sync trong so roi bam lai
```

---

## Trạng thái nằm ở đâu

`deploy/state.yaml` là **nguồn sự thật duy nhất** cho thứ đang chạy. Cụm bị huỷ mỗi tối;
bất kỳ thứ gì chỉ tồn tại trong cụm đều chết theo nó, nên sáng hôm sau cụm sẽ lên đúng bản
ghi trong file này.

```bash
python3 bench/scripts/rollout_state.py validate   # hoac: make rollout-check
```

`deploy/argocd/apps/*.yaml` được **sinh ra** từ state. Sửa tay sẽ bị PR check bắt:

```bash
make rollout-render         # sinh lai sau khi sua state.yaml
make rollout-check          # kiem lech -- chay trong CI
```

### Năm giai đoạn

| phase | Nghĩa là gì | Chiếm mấy card |
|---|---|---|
| `idle` | chỉ có bản đang chạy | 4 stable |
| `evaluating` | ứng viên lên 1 card; guardrail chỉ đưa tên `<model>-candidate` tới nó | 3 + 1 |
| `canary` | guardrail chia `canary_weight`% traffic thật của `<model>` sang ứng viên | 3 + 1 |
| `promoting` | ứng viên đã thành stable, pod đang cuốn | 4 |
| `watching` | đã cuốn xong, còn trong cửa sổ quay về được | 4 |

---

## Trước khi đo tải — bắt buộc

```bash
make rollout-pause REASON="do TC1a 50 req/s"
# ... chay k6 ...
make rollout-resume
```

Một lần rollout chen vào giữa phép đo làm cụm **chỉ còn 3/4 dung lượng**, và con số TC1a
thu được sẽ sai mà không có gì báo. `paused` nằm trong git nên lần tạm dừng cũng có lịch sử.

---

## Rollout tự động — không ai bấm gì

Bên ghi đặt một version đúng hợp đồng vào thư mục được theo dõi
([`deploy/catalog.yaml`](../deploy/catalog.yaml)). Từ đó:

```
 S3: models/qwen2.5-7b/<version>/_READY
        │  rollout.yml, 15 phút/lần (hoặc ngay khi có tin SQS)
        ▼
 cổng CPU: chữ ký, đủ file, safetensors, vừa card ── trượt ─► rejected + issue
        │ PR → rollout-check xanh → tự merge → Argo
        ▼
 evaluating  Job rollout-eval: 144 câu vàng, 154 tấn công, 12 req/s ── trượt ─► rejected
        │ PR
        ▼
 canary 25%  Job rollout-canary: 15 phút, so 2 bản theo metric model_version ── trượt ─► rejected
        │ PR  (mở issue "promoted")
        ▼
 promoting   pod stable cuốn từng cái sang bản mới; controller chờ 25 phút
        │ PR
        ▼
 watching    Job rollout-watch: 60 phút, availability ≥ 99,5%, p95 ≤ 3 s ── trượt ─► quay về previous
        │ PR  (đóng issue)
        ▼
 idle
```

**Ai làm gì:**

| Thành phần | Chạy ở đâu | Việc |
|---|---|---|
| `rollout_controller.py` | GitHub Actions (`rollout.yml`) | đọc S3, quyết định, sửa `deploy/state.yaml`, mở PR |
| `rollout-check.yml` | GitHub Actions, trên mọi PR | state hợp lệ, file sinh ra khớp, chart render được, test |
| Argo CD | trong cụm | đồng bộ `deploy/argocd/apps/` và `deploy/rollout/` |
| `rollout_jobs.py` | Job trong cụm, namespace `benchmark` | đo, ghi báo cáo `runs/rollout/<id>/<bước>.json` |
| guardrail | trong cụm | đọc bảng route từ ConfigMap `guardrail-routes`, mỗi 5 giây |

**Controller không bao giờ huỷ ứng viên vì im lặng.** Nó không nhìn thấy cụm, nên không
phân biệt được "Job đang chạy" với "cụm đang tắt buổi tối". Hạn giờ nằm trong Job; Job
luôn để lại báo cáo, kể cả khi trượt vì hết giờ hay vì lỗi. Im lặng quá 6 giờ chỉ được ghi
là `stalled` trong tóm tắt lần chạy, cho người xem.

**Thử luồng bằng track 1.5B, không phải 7B.** Cả hai track đều bật `auto_rollout`, nhưng
1.5B là track rẻ và nhanh hơn nhiều: trọng số AWQ 1,5 GiB nên pod ứng viên lên trong vài
phút thay vì tới nửa giờ, và bước đánh giá chạy xong nhanh hơn. Đó là track nên nhắm khi
bên ghi model muốn kiểm bắt tay, hoặc khi thử lại luồng sau một thay đổi.

Bốn node GPU khi cả hai track chạy, **~4,49 USD/giờ** — đúng trần quota 16 vCPU. Muốn một
buổi thử rẻ hơn thì hạ `cards` của 7B về `1`: một card **vẫn hợp lệ khi track đang idle**
(máy trạng thái chỉ từ chối `cards < 2` lúc có ứng viên), nên 1 + 2 = 3 node,
**~3,4 USD/giờ**, mà track 1.5B vẫn rollout bình thường.

**Mỗi version chỉ được thử một lần.** `watermark` trong state ghi thời điểm `_READY` của
version mới nhất đã xét; mọi thứ cũ hơn bị bỏ qua mãi mãi, kể cả khi bị loại. Muốn thử lại
một bản đã trượt: xuất bản nó thành version mới.

### Xem nó đang làm gì

```bash
make argocd-status                     # phase tung track
make rollout-jobs                      # Job dang chay + log Job moi nhat
make rollout-step                      # lan chay ke se lam gi (khong ghi gi)
make rollout-report ID=<id> KIND=eval  # bao cao Job da ghi
```

`<id>` có trong tên Job (`rollout-eval-<id>`) và trong PR của bot.

### Cài đặt một lần

1. `terraform -chdir=terraform/core apply` — tạo role OIDC chỉ-đọc cho Actions.
2. Tạo một GitHub App trên repo: quyền **Contents: write**, **Pull requests: write**.
   Cài vào repo. Lấy App ID và private key.
3. Repo → Settings → Secrets and variables → Actions:

   | Loại | Tên | Giá trị |
   |---|---|---|
   | Secret | `ROLLOUT_ROLE_ARN` | `terraform -chdir=terraform/core output -raw github_rollout_role_arn` |
   | Secret | `ARTIFACTS_BUCKET` | `terraform -chdir=terraform/core output -raw artifacts_bucket_name` |
   | Secret | `ROLLOUT_APP_PRIVATE_KEY` | private key của App |
   | Secret | `MODEL_QUEUE_URL` | tuỳ chọn, URL SQS của bên ghi |
   | Variable | `ROLLOUT_APP_ID` | App ID |
   | Variable | `AWS_REGION` | `us-east-1` |

   Bucket và role để ở **Secret** chứ không phải Variable chỉ để được che trong log: log
   Actions của repo công khai là công khai, và cả hai đều chứa account ID.
4. Settings → General → bật **Allow auto-merge**. Settings → Branches → bảo vệ `main`:
   bắt buộc PR, bắt buộc check **`rollout-check`**.

Thiếu bước 3 thì `rollout.yml` tự bỏ qua, không báo lỗi. Thiếu bước 4 thì bot vẫn chạy
nhưng tự chờ check rồi mới merge — không bao giờ merge khi check chưa xanh.

---

## Đưa một model mới lên

```bash
make model-publish FROM=./build/moc-7b NAME=moc-7b \
     VERSION=2026-11-20-r1 SERVED=qwen2.5-7b SIGNER=training-pipeline
```

Lệnh này tự chạy các cổng kiểm **trước khi** upload, băm từng file, ký manifest, upload,
đọc lại từ S3 để đối chiếu, rồi mới ghi `_READY`. Trượt ở bước nào cũng dừng và không ghi
`_READY` — version đó coi như không tồn tại với CD.

Kiểm một version đã có trên S3:

```bash
make model-check SOURCE=s3://<bucket>/models/moc-7b/2026-11-20-r1/
```

### Cần có trước

**`cosign` và một khoá KMS.** Chưa có khoá nào được tạo. Mỗi bên ghi model cần một khoá
riêng — xem [`deploy/signers/README.md`](../deploy/signers/README.md):

```bash
brew install cosign
aws kms create-key --key-spec ECC_NIST_P256 --key-usage SIGN_VERIFY
aws kms create-alias --alias-name alias/model-signer-training-pipeline --target-key-id <id>
cosign public-key --key awskms:///alias/model-signer-training-pipeline \
  > deploy/signers/training-pipeline.pub
```

rồi mở PR thêm khoá công khai đó. **Khoá công khai trong git là có chủ ý**: thêm một bên
được phép đưa model vào production là thay đổi phải qua review.

---

## Checkpoint cũ chưa có manifest

Bốn checkpoint đang nằm trên S3 được đưa lên bằng tay trước khi có hợp đồng model, nên
không có manifest để đối chiếu. `deploy/state.yaml` khai điều đó **ra mặt**:

```yaml
stable:
  weights: models/Qwen2.5-7B-Instruct-AWQ
  verify: false
  reason: "checkpoint dua len tay truoc khi co hop dong model; chua co manifest"
```

`verify: false` **bắt buộc kèm `reason`**, vì nó tắt lớp kiểm duy nhất chứng minh trọng số
trên đĩa đúng là bản đã ký. Không có dòng đó thì state không hợp lệ.

Bỏ ngoại lệ này bằng cách xuất bản lại checkpoint theo hợp đồng rồi trỏ `weights` sang
version mới. Từ lúc đó, **mỗi lần pod khởi động đều băm lại và đối chiếu**.

---

## Tầng node theo loại model

Mỗi model chạy trên node group riêng, không chia card với model khác.

| tier | node group | card | giá | dùng cho |
|---|---|---|---|---|
| `main` | `gpu` | g5.xlarge — A10G 24GB | 1,006 USD/h | 7B, 4 node |
| `light` | `gpu-light` | g4dn.xlarge — T4 16GB | 0,526 USD/h | 1.5B, **0 node** |

Khai trong `deploy/state.yaml`:

```yaml
qwen2.5-1.5b:
  tier: light
  stable: { weights: models/..., engine: vllm/vllm-openai:v0.29.0 }
  phase: idle
```

Track `light` tự nhận `nodeSelector` và toleration cho taint `tier=light`, và lấy 1 card
chứ không phải 4.

### Vì sao `light` đang để 0 node

**Quota G là 16 vCPU**, mỗi `.xlarge` ăn 4 — tức **tối đa 4 node GPU, bất kể loại nào**.
Cả 4 đang cần cho 7B: 12,5 req/s mỗi card × 4 = 50 req/s, vừa đúng ngưỡng TC1a. Bật node
nhẹ lên bây giờ là lấy mất một card của 7B và trần tụt còn **37,5 req/s** — trượt TC1a.

Mở khoá bằng cách xin nâng quota lên 20 vCPU, rồi đặt `gpu_light_desired = 1`. Thêm
**0,526 USD/giờ**.

Đây cũng là lý do `mode: shared` không phải hình dạng đúng cho việc này: nó cắt 25% một
card đang phải đạt 50 req/s, trong khi một card T4 riêng rẻ hơn một nửa và không mượn của ai.

---

## Khi hỏng

| Triệu chứng | Nguyên nhân thường gặp |
|---|---|
| Application `OutOfSync` mãi | `make rollout-check` để xem file sinh ra có lệch state không |
| Pod `Init:Error` | init container báo hash lệch hoặc thiếu manifest — xem log, **đừng tắt `verify`** |
| Pod `Pending`, `Insufficient cpu` | node tooling hết chỗ; `cpu_desired` phải là 3 |
| `ImagePullBackOff` trên engine | digest nối sai dấu, xem `image:` trong Application |
| Argo không dọn ứng viên đã bỏ | root app phải bật `prune`; kiểm `deploy/argocd/root.yaml` |
| Job rollout `CreateContainerConfigError` | thiếu ConfigMap `cluster-config` ở namespace `benchmark` — chạy `make cluster-config` |
| Job báo `AccessDenied` khi ghi báo cáo | ServiceAccount `bench-runner` chưa gắn role — `make cluster-config` |
| Canary trượt "khong request nao toi ung vien" | guardrail chưa thấy bảng route mới: ConfigMap `guardrail-routes` có trong `llm-serving` không? guardrail có được cài lại sau khi thêm mount không (`make guardrail-up`)? |
| `guardrail_route_reload_failures_total` tăng | file route sai; guardrail giữ bảng cũ. Xem log guardrail |
| Bot không mở PR mới | PR trước của nhánh `rollout/bot` chưa merge — xem check của nó |
| Lần chạy ghi `stalled` | Job chưa để lại báo cáo sau 6 giờ: cụm tắt, hoặc Job chết — `make rollout-jobs` |

Quay về bản trước bằng tay (bước theo dõi tự làm việc này khi trượt):

```bash
python3 bench/scripts/rollout_state.py rollback --track qwen2.5-7b
make rollout-render && git checkout -b rollback && git commit -am "rollback" && git push
# mo PR -- main duoc bao ve, ke ca voi nguoi
```

Argo nhận thay đổi trong vòng 3 phút. Mỗi pod phải sync và băm lại nên **mất tới 20 phút**.

---

## Chưa làm

Chạy được rồi: hợp đồng model, các cổng CPU, kiểm lúc nạp, máy trạng thái, Argo CD,
guardrail chia traffic có trọng số, ba Job đánh giá / canary / theo dõi, controller và hai
workflow.

Chưa có:

- **Chưa chạy đầu-cuối trên cụm thật.** Mọi bước đã qua test, controller đã đọc S3 thật,
  Job đã chạy trong đúng image `amazon/aws-cli` — nhưng một vòng đầy đủ cần cụm 4 GPU
  khoảng 2 giờ (xem §8 của plan).
- **Cài đặt GitHub** ở mục trên: App, secret, branch protection. Làm trên giao diện GitHub,
  không phải file.
- **CD cho image guardrail** (`images.yml` + smoke test) và Renovate cho image upstream.
  Rollout ở đây chỉ đổi trọng số; đổi engine vLLM vẫn sửa tay `engine:` trong state.
- **Quay về sau promote mất tới 20 phút**, vì pod phải sync và băm lại trọng số cũ.
