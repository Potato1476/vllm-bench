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
| `evaluating` | ứng viên có tên riêng, không nhận traffic thật | 3 + 1 |
| `canary` | ứng viên vào pool chính ở `canary_weight`% | 3 + 1 |
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

Quay về bản trước:

```bash
python3 bench/scripts/rollout_state.py rollback --track qwen2.5-7b
make rollout-render && git commit -am "rollback" && git push
```

Argo nhận thay đổi trong vòng 3 phút. Mỗi pod phải sync và băm lại nên **mất tới 20 phút**.

---

## Chưa làm

Chạy được rồi: hợp đồng model, các cổng, kiểm lúc nạp, máy trạng thái, Argo CD đồng bộ.

Chưa có — vẫn phải làm tay:

- **Job đánh giá và Job canary.** Hiện chuyển phase bằng tay qua
  `rollout_state.py advance`. Chưa có gì tự chấm ứng viên.
- **`.github/workflows/`.** Chưa có CI. `make rollout-check` là bài kiểm sẽ chạy ở đó.
- **Định tuyến có trọng số trong guardrail.** `canary_weight` đã có trong state và trong
  Application, nhưng guardrail chưa biết chia traffic theo nó — nên phase `canary` hiện tại
  chỉ là 1 trong 4 pod, tức 25% theo số pod chứ không theo tỷ lệ đặt được.
- **Job theo dõi 60 phút sau promote.**
