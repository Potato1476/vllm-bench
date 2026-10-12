# Đối chiếu với bộ quy tắc của team Deployment Quality Gate

**Đo ngày 12/10/2026** · gate ghim tại commit `6ff9d3b2daf5eb1ccfc05118681e704c67bb8c4d`
(08/10/2026 — repo gate **chưa có tag nào**, nên bắt buộc ghim theo SHA).

Nguồn: [`kdvh-gsm-vsf-group/deployment-quality-gate`](https://gitlab.com/kdvh-gsm-vsf-group/deployment-quality-gate),
repo công khai. DA#51 **tái dùng dữ liệu policy** và **tự viết runner** bằng GitHub Actions —
`ci/consumer/gate.gitlab-ci.yml` của gate gắn cứng runner tag `aws-cloud`, `$CI_JOB_TOKEN`,
Alpine và GitLab Container Registry, nên không dùng lại được. Lý do đầy đủ:
[`docs/cicd-plan.md`](cicd-plan.md) §5.

Đây là **số đo**, không phải phỏng đoán: mỗi dòng dưới đây do `conftest`, `kyverno apply`
hoặc `trivy` in ra. Chạy lại bằng `make gate-check` (cần docker, không cần cụm).

> **Trạng thái 12/10/2026 sau khi sửa: 0 vi phạm chặn.**
> Từ 25 vi phạm xuống còn 0 phải sửa và 20 có ngoại lệ khai trong
> [`security/gate-exceptions.yaml`](../security/gate-exceptions.yaml), mỗi ngoại lệ kèm lý
> do, người chịu trách nhiệm và **ngày hết hạn**. Hết hạn thì check đỏ lại — đó là điểm
> khác biệt giữa một ngoại lệ và việc tắt quy tắc đi. Xem hạn còn lại:
> `make gate-exceptions`.

---

## 0. Mức áp dụng — đọc từ `security/muc-rule.json` của gate

| Nhóm | Rule | Nghĩa |
|---|---|---|
| **hard gate** | IMG-001, K8S-001, K8S-002, K8S-003, K8S-007, ML-002 | luôn BLOCK, **không file ghi đè nào hạ xuống được** |
| **block** | 25 rule, gồm IMG-002…009, IMG-011, K8S-004…006, K8S-008…015, ML-001, ML-003…006 | chặn merge |
| **warn** | IMG-010, K8S-016, ML-007 | báo, không chặn |
| **info** | K8S-017 | chỉ thu số liệu |

Tổng 31/35 BLOCK. **K8S-016 (replicas ≥ 2) chỉ là `warn`** — gate ghi rõ lý do: cụm kind của
họ một node nên không kiểm chứng được. Đây là may mắn cho DA#51: quota GPU 16 vCPU không cho
chạy 2 replica mỗi model, nên nếu rule này lên block thì DA#51 **không thể** đáp ứng.

---

## 1. Kết quả — tổng hợp

| Hạng mục | Công cụ | Lần đo đầu | Sau khi sửa |
|---|---|---|---|
| IMG-001 quét secret toàn repo | Trivy 0.74.0 | sạch | **sạch** |
| Chiều nghịch của máy quét secret | Trivy 0.74.0 | đạt | **đạt** — bắt hết key giả, kể cả trong `tests/`, `*.example.yaml`, `*.md` |
| IMG-002 CVE của phụ thuộc | Trivy 0.74.0 | — | **0 CVE CRITICAL/HIGH có bản vá** |
| IMG-004/005/ML-007 Dockerfile | Conftest v0.70.0 | 4 block | **4 block, đều có ngoại lệ** |
| Chiều nghịch của Conftest | Conftest v0.70.0 | đạt | **đạt** — fixture của gate ra đúng `IMG-004, IMG-005, ML-007` |
| Policy Kyverno trên manifest + Job | Kyverno CLI 1.19.1 | 54 pass / 25 fail | **100 pass / 17 fail**, 0 cái nào không có ngoại lệ |

Kết quả "sạch" của IMG-001 **chỉ đáng tin vì chiều nghịch chạy trước và đạt**. Repo này từng
có `scan_secrets.sh` báo "sạch" khi quét 0 file; thứ tự đó là để bài học không lặp lại.

---

## 2. Dockerfile — 4 vi phạm block

| Rule | File | Vi phạm | Xử lý |
|---|---|---|---|
| IMG-004 | `services/llm_pipeline/Dockerfile` | base `python:3.12.11-slim` không có trong `security/base-image-allowlist.yaml` (allowlist chỉ có `python:3.11-slim`) | **cần gate đổi allowlist** |
| IMG-005 | `services/llm_pipeline/Dockerfile` | không pin digest | **sửa được** |
| IMG-004 | `bench/runner/Dockerfile` | base `vllm/vllm-openai:v0.29.0` không có trong allowlist | **cần gate đổi allowlist** |
| IMG-005 | `bench/runner/Dockerfile` | không pin digest | **sửa được** |
| ML-007 | cả hai file | **đạt** — không nhúng trọng số vào image | — |

Hai vi phạm IMG-004 không sửa được một phía. Allowlist hiện chỉ cho `python:3.11-slim`:

- **`python:3.12-slim`** — hạ về 3.11 là sai hướng: bộ test của repo này chạy trên 3.12, và
  `rollout-check.yml` dùng `actions/setup-python` 3.12. Đường đúng là thêm `python:3.12-slim`
  vào allowlist qua MR có số đo CVE, đúng quy trình `docs/base-image-survey.md` của gate.
- **`vllm/vllm-openai`** — sẽ không bao giờ nằm trong một allowlist chung. `bench/runner` build
  FROM image engine để dùng cùng CUDA runtime. Cần một ngoại lệ có thời hạn, hoặc tách runner
  ra khỏi phạm vi IMG-004.

ML-007 đạt là một điểm đáng nói với họ: DA#51 **không** đóng trọng số vào image, nên rule
"image nhúng model phải có nhãn truy nguyên" không áp dụng — truy nguyên nằm ở `manifest.json`
đã ký, mạnh hơn một LABEL.

---

## 3. Manifest Kubernetes — 25 vi phạm

`g` = guardrail, `v` = cả hai Deployment vLLM, `j` = Job rollout.

| Rule | Mức | Ở đâu | Nguyên nhân đo được | Xử lý |
|---|---|---|---|---|
| K8S-011 | block | g, v | thiếu `app.kubernetes.io/name` và `/version` | **sửa được**, dễ nhất |
| ML-006 | block | v | initContainer `fetch-weights` thiếu `limits.cpu` và `limits.ephemeral-storage` | **sửa được — rủi ro thật** |
| ML-005 | block | v | container thiếu `limits.ephemeral-storage`, trong khi volume `weights` là `emptyDir` nhận 5–15 GiB | **sửa được — rủi ro thật** |
| K8S-008 | block | g, v, j | container thiếu `limits.cpu` | **sửa được** |
| K8S-010 | block | g | **sidecar `redis`** chỉ có `readinessProbe`, thiếu `livenessProbe` | **sửa được** |
| K8S-014 | block | g | guardrail chạy bằng ServiceAccount **`default`** (chart không đặt `serviceAccountName`) | **sửa được** |
| K8S-014 | block | v, j | không đặt `automountServiceAccountToken: false` | **cần kiểm trên cụm** (xem dưới) |
| K8S-006 | block | v | container không `drop: ALL` capabilities | **sửa được** |
| K8S-004 | block | v | không đặt `runAsNonRoot: true` | **chưa kết luận được** |
| K8S-013 | block | g, v, j | không đặt `readOnlyRootFilesystem: true` | **chưa kết luận được** |
| K8S-016 | warn | g, v | `replicas: 1` | **không sửa** — quota GPU; chỉ warn |
| **ML-001** | block | — | **không nổ** | **cần gate đổi policy** |

### Hai thứ rủi ro thật, sửa được ngay — ML-005 và ML-006

Volume `weights` là `emptyDir` và nhận **5–15 GiB trọng số** mà **không có trần**. Đặc tả
ML-006 của gate viết đúng tình huống này: *"một model lớn bất thường có thể ép node evict pod
của người khác"*. Rule của họ bắt được một rủi ro có thật trong chart của DA#51, không phải
thủ tục giấy tờ. Đây là hai vi phạm nên sửa trước tiên.

### Ba thứ chưa kết luận được khi chưa có cụm

- **K8S-004 `runAsNonRoot`** trên pod vLLM. Image `vllm/vllm-openai` ở nhiều bản chạy bằng
  root và có ràng buộc truy cập thiết bị GPU. **Không được khẳng định sửa được khi chưa thử
  trên node A10G thật.** Kết luận đúng có thể là xin ngoại lệ có thời hạn.
- **K8S-013 `readOnlyRootFilesystem`** trên pod vLLM. vLLM ghi cache biên dịch và cache
  HuggingFace khi khởi động; bật cờ này mà không mount đủ `emptyDir` vào các đường đó sẽ làm
  pod không lên. Phải thử trên cụm.
- **K8S-014 `automountServiceAccountToken: false`** trên pod vLLM. Pod này dùng **IRSA** để
  đọc S3. Token của IRSA là một projected volume riêng do webhook của EKS tiêm, nên về lý
  thuyết tắt automount không ảnh hưởng — nhưng **chưa kiểm chứng trên cụm này**, và nếu sai
  thì init container không tải được trọng số. Không bật mù.

Ba cái trên là lý do mục này tồn tại: một bản sửa đoán bừa trên chart vLLM không hỏng ở CI,
nó hỏng ở pod không khởi động lúc 3 giờ sáng trên cụm không ai xem.

### ML-001 không nổ — và đó mới là vấn đề

ML-001 ("initContainer tải model phải đúng nguồn và có verify checksum") **không xuất hiện
trong 25 vi phạm**. Không phải vì DA#51 đạt, mà vì policy không nhận ra initContainer của
DA#51 là một bước tải model: nó nhận diện bằng volume tên `model-cache` **hoặc** biến môi
trường `*_MODEL_ID`, còn DA#51 dùng volume `weights` và `S3_BUCKET`/`S3_MODEL_URI`/
`VERIFY_MANIFEST`.

Kết quả: **gate báo xanh về model provenance trong khi không kiểm gì** — đúng cái bẫy mà
template của chính gate cảnh báo (*"đoán sai thì job xanh trong khi chẳng quét gì"*).

Việc đúng **không phải** uốn chart DA#51 cho giống `rag-service`. Cơ chế của DA#51 mạnh hơn:
`manifest.json` ký bằng cosign + khoá KMS, và init container **băm lại từng file trên NVMe mỗi
lần pod khởi động** — so với kiểu ghim `EMBEDDING_MODEL_REVISION` theo commit Hugging Face thì
nó chứng minh được nhiều hơn. Đề xuất gửi gate: [`docs/gate-ml-001-de-xuat.md`](gate-ml-001-de-xuat.md).

---

## 4. Việc chia theo ai làm

| Ai | Việc | Trạng thái |
|---|---|---|
| **DA#51** | K8S-011 nhãn `app.kubernetes.io/*`; ML-006 `limits.cpu` + `limits.ephemeral-storage` cho initContainer; ML-005 `limits.ephemeral-storage` cho container và Job; K8S-008 `limits.cpu` cho guardrail, redis và Job; K8S-010 `livenessProbe` cho sidecar redis; K8S-014 ServiceAccount riêng cho guardrail; K8S-006 `drop: ALL` trên vLLM | **đã sửa** |
| **DA#51, cần cụm** | K8S-004 và K8S-013 trên pod vLLM; K8S-014 với IRSA | ngoại lệ tới 09/11 |
| **DA#51, sửa được** | IMG-005 pin digest hai Dockerfile | ngoại lệ tới 26/10 — pin sau khi IMG-004 chốt, để không phải pin hai lần |
| **Cần gate đổi** | IMG-004 thêm `python:3.12-slim` kèm số đo CVE; xử lý `vllm/vllm-openai`. ML-001 thêm nhánh nhận diện luồng manifest-đã-ký | ngoại lệ tới 09/11 |
| **Không làm** | K8S-016 replicas ≥ 2 — quota GPU không cho, và gate đang để `warn` | ngoại lệ tới 31/12 |
| **Ngoại lệ có chủ ý, không phải chưa làm** | K8S-008 `limits.cpu` trên container vLLM — CFS throttling làm tăng đúng TTFT mà nền tảng này đo, và p95 1590 ms được đo khi không có limit | cần chốt với gate |

---

## 5. Chạy lại

```bash
make gate-check        # clone gate o SHA ghim, quet, cham. Can docker.
make gate-exceptions   # ngoai le nao sap het han
```

`make gate-check` dùng **đúng** script và **đúng** phiên bản công cụ mà
[`.github/workflows/gate.yml`](../.github/workflows/gate.yml) dùng, nên "xanh ở local"
và "xanh trên CI" có cùng nghĩa. SHA của gate nằm một chỗ duy nhất:
[`security/gate-ref.txt`](../security/gate-ref.txt).

Trivy trên máy có thể là bản khác (ở đây là 0.75.0 so với 0.74.0 gate ghim); vì vậy cả hai
đường đều chạy công cụ **qua docker image đã ghim**, không dùng bản cài sẵn — IMG-003 so với
một baseline CVE, và hai bản Trivy khác nhau cho hai tập CVE khác nhau.
