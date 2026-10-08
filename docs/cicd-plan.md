# Kế hoạch CI/CD — model và image tự lên production

Đề tài DA#51 · bản nháp 08/10/2026 · Nguyễn Gia Bảo

**Mục tiêu.** Hai thứ thay đổi trên nền tảng serving đều phải tự đi tới production, qua cùng các
cổng kiểm tra, và tự quay về khi hỏng:

- **Model:** một version trọng số mới xuất hiện trên S3, đúng hợp đồng ở §4.1. Model đó do đâu
  mà có — tự huấn luyện, fine-tune, mua, hay tải về — không phải việc của CD. CD chỉ kiểm nó đúng
  hợp đồng, rồi đưa lên.
- **Image:** code của guardrail đổi, hoặc một image upstream (vLLM, LiteLLM…) có bản mới. CI tự
  build, quét, ký, rồi CD đưa lên.

**Không có bước duyệt tay.** Version nào qua hết cổng thì tự lên production. Ba thứ thay chỗ
người duyệt: các cổng tự động, bước tự theo dõi rồi tự quay về sau khi promote, và một công tắc
dừng khẩn (§4.3).

---

## 1. Hiện trạng

**Chưa có CI lẫn CD.** Repo không có `.github/workflows`. Mọi thứ chạy bằng `make` từ laptop.

### Model

`charts/vllm/values.yaml` khai mỗi model bằng một `s3Prefix`. Init container sync prefix đó sang
NVMe, rồi vLLM nạp từ đó. **Đổi model = đổi một prefix S3** — CD chỉ cần quyết định prefix nào
được dùng.

Bốn checkpoint đang có trên S3 (Qwen2.5 7B/1.5B, bản thường và AWQ) được đưa lên bằng tay để
demo. Chúng không có manifest hay chữ ký, nên CD sẽ không nhận chúng nguyên trạng; bản dùng cho
demo được ghi lại theo hợp đồng (§7).

### Image

| Image | Nguồn | Build thế nào | Tag |
|---|---|---|---|
| guardrail | `services/llm_pipeline/Dockerfile` | `make guardrail-image`, trên laptop | `src-<hash nội dung mã nguồn>` |
| bench-runner | `bench/runner/Dockerfile` | `make runner-image`, trên laptop | git short sha |
| vLLM | upstream `vllm/vllm-openai` | — | `v0.29.0` |
| LiteLLM | upstream `ghcr.io/berriai/litellm` | — | `v1.90.2` |
| Open WebUI | upstream | — | `v0.6.40` |
| cloudflared, aws-cli | upstream | — | ghim tag |

Đã có sẵn trong `terraform/core/storage.tf`: hai repo ECR để **tag bất biến**, bật **quét khi
push**, có lifecycle policy. Nhưng kết quả quét của ECR chỉ để xem, không chặn gì. Image upstream
ghim theo tag chứ chưa ghim theo digest: tag có thể bị đẩy lại, digest thì không.

### Ràng buộc

- **Cụm bị huỷ mỗi tối.** Production là cụm đang chạy; mỗi lần dựng lại phải lên đúng bản đã
  promote gần nhất.
- **API EKS chỉ mở cho vài dải IP**, nên runner GitHub không gọi `kubectl`/`helm` vào cụm được.
- **Tối đa 4 GPU.** Trong lúc rollout model chỉ có thể là 3 pod bản cũ + 1 pod ứng viên.
- **Node tooling đã chật** (67–73% CPU ngày 07/10). Argo CD cần node tooling thứ ba.
- Không secret, token hay ARN nào nằm trong file.

---

## 2. Luồng tổng thể

```
 NGUỒN THAY ĐỔI                     GITHUB ACTIONS                         EKS (Argo CD kéo từ git)
 ──────────────                     ──────────────                         ────────────────────────
 Bất kỳ bên ghi model nào
   publish_model.py ─► S3 ────────► watch-models.yml (15 phút/lần)
   models/<tên>/<version>/            thấy _READY mới → kiểm trước GPU
   manifest + chữ ký + _READY         (chữ ký, đủ file, định dạng, vừa GPU)  ┐
                                                                             │
 PR sửa code guardrail ──────────►  pr.yml: test, build, quét             │
          │ merge                                                            ├─► rollout.yml
          ▼                                                                  │   mở PR sửa deploy/state.yaml
 merge vào main ─────────────────►  images.yml: build → quét → ký → ECR    ┘   → check xanh → tự merge ──► Argo đồng bộ
                                                                                         ┌─ model / vLLM: ứng viên 1 GPU
 Renovate: image upstream mới ───►  PR bump digest → pr.yml quét                         │   → đánh giá → canary 25%
                                                                                         │   → cuốn từng pod → theo dõi
                                                                                         └─ guardrail: rolling update
                                                                                             → smoke test
                                    rollout.yml đọc báo cáo trên S3  ◄──── Job trong cụm ghi runs/rollout/<id>/
                                      đạt → bước kế    trượt → quay về + mở issue
```

---

## 3. Quyết định thiết kế chung

**C1. GitHub Actions làm CI.** Repo public nên runner chuẩn miễn phí. Xác thực AWS bằng OIDC
(`aws-actions/configure-aws-credentials`), không có access key tĩnh.

**C2. Argo CD kéo trạng thái từ git, thay vì CI đẩy vào cụm.**

- Runner GitHub không với tới API EKS.
- Cụm dựng lại mỗi tối: Argo CD đọc git là lên lại đúng bản đang chạy.
- CI không giữ credential nào của cụm.

**C3. Git giữ trạng thái rollout, và mọi thay đổi đi qua PR.** `deploy/state.yaml` chỉ do
`rollout.yml` sửa. Mỗi lần chuyển bước, bot mở một PR; PR chạy check kiểm schema của
`deploy/state.yaml`, rồi **tự merge** khi check xanh. `main` được branch protection bảo vệ: không
ai — kể cả bot — được đẩy thẳng vào. Lịch sử PR chính là nhật ký rollout.

```yaml
# deploy/state.yaml
qwen2.5-7b:
  stable:    { weights: moc-7b/2026-11-02-r3, engine: vllm/vllm-openai@sha256:… }
  candidate: { weights: moc-7b/2026-11-20-r1, engine: vllm/vllm-openai@sha256:… }
  phase: canary              # idle | evaluating | canary | promoting | watching
  canary_weight: 25
  previous:  { … }           # để quay về sau promote
guardrail:
  image: <ECR>/vllm-bench/guardrail@sha256:…
  previous: <ECR>/vllm-bench/guardrail@sha256:…
```

**Đơn vị rollout của phần GPU là cặp (trọng số, engine).** Đổi một trong hai đều đi qua cùng các
cổng, vì nâng vLLM cũng đổi câu trả lời và độ trễ y như đổi model.

*Vì sao không dùng Argo Rollouts:* Rollouts giữ việc huỷ canary trong cụm. Cụm bị xoá thì cụm mới
không biết bản đó đã bị loại, và sẽ dựng thẳng bản bị loại làm stable. Muốn tránh thì vẫn phải sửa
git — nên để git làm luôn từ đầu.

**C4. Bot mở PR bằng GitHub App, không bằng `GITHUB_TOKEN`.** PR do `GITHUB_TOKEN` mở **không kích
hoạt workflow nào** — GitHub làm vậy để chặn vòng lặp — nên check bắt buộc không bao giờ chạy và
auto-merge chờ mãi. Token của một GitHub App (`actions/create-github-app-token`) thì kích hoạt
check bình thường. App chỉ có quyền `contents` và `pull_requests` trên repo này; khoá riêng của
App nằm trong secret của GitHub Actions, không nằm trong file nào.

Cách `rollout.yml` lái quy trình:

- một lượt chạy lái cả quy trình: mở PR, chờ merge, chờ báo cáo trên S3, mở PR bước kế (giới hạn
  6 giờ một job);
- lịch 15 phút một lần tiếp tục rollout dở dang, ví dụ khi cụm đang tắt;
- `concurrency` đảm bảo mỗi lúc chỉ có một rollout;
- các workflow khác bỏ qua thay đổi chỉ nằm trong `deploy/state.yaml` (`paths-ignore`), để PR của
  bot không kích hoạt build image hay bộ test nặng.

Mỗi bước thêm khoảng 1–2 phút cho check và merge — không đáng kể so với 20 phút đánh giá và 15 phút
canary, kể cả khi phải quay về.

---

## 4. Luồng model — trọng số từ S3

### 4.1 Hợp đồng cho bên ghi model

CD nhận một version khi và chỉ khi nó có đúng dạng này:

```
s3://<bucket>/models/<tên-model>/<version>/
  config.json, tokenizer*, generation_config.json
  *.safetensors                 # CHỈ safetensors
  manifest.json                 # xem dưới
  manifest.bundle               # chữ ký (bundle cosign v3) của bên ghi
  _READY                        # ghi CUỐI CÙNG
```

```json
{
  "name": "moc-7b",
  "version": "2026-11-20-r1",
  "kind": "full",
  "served_name": "qwen2.5-7b",
  "producer": "training-pipeline",
  "source": "<mô tả tự do: lần huấn luyện nào, từ đâu ra>",
  "dtype": "float16",
  "max_model_len": 8192,
  "files": [ { "path": "model-00001-of-00004.safetensors", "size": 3945441440, "sha256": "…" } ]
}
```

Lý do từng điều:

- **Chỉ safetensors.** `.bin`, `.pt`, `.pkl` là pickle — nạp một file pickle là chạy code trong nó.
  Không biết model đi qua những tay nào, nên đây là điều kiện cứng.
- **`_READY` ghi cuối.** Một bộ 15 GB mất nhiều phút để ghi. Thiếu `_READY` nghĩa là đang ghi dở;
  CD không đụng tới.
- **`version` không bao giờ ghi đè.** Rollback phải ra đúng bộ trọng số cũ. S3 không tự cấm ghi đè,
  nhưng nếu có ai ghi đè thì sha256 lúc nạp sẽ lệch và pod từ chối khởi động (§4.3).
- **`name` và `version` trong manifest phải khớp đường dẫn.** Không có điều này, ai có quyền ghi S3
  có thể chép một bộ trọng số cũ đã ký hợp lệ sang đường dẫn version mới, và CD sẽ "nâng cấp" lên
  một bản cũ.
- **Chữ ký trên manifest.** sha256 chứng minh file khớp manifest; chữ ký chứng minh manifest do đúng
  bên được phép ghi ra. Cách ký ở §4.6.
- **`source` là mô tả tự do**, để người đọc issue biết model từ đâu ra. CD không đọc trường này để
  quyết định gì.
- **`kind: lora`** cho trường hợp fine-tune bằng LoRA: thư mục chứa adapter, vLLM nạp bằng
  `--enable-lora`. Cùng hợp đồng, cùng cổng.

### 4.2 Công cụ ghi model: `publish_model.py`

Bên ghi model gọi một lệnh ở cuối pipeline của họ, từ một thư mục trên máy hoặc một prefix S3 có
sẵn:

1. chạy `check_model.py` (cùng các kiểm tra như cổng Hợp lệ và Tương thích ở §4.3), để lỗi lộ ra ở
   phía bên ghi chứ không phải mười lăm phút sau ở phía CD;
2. đẩy file lên `models/<tên>/<version>/`, từ chối nếu version đó đã tồn tại;
3. băm sha256 từng file, ghi `manifest.json`;
4. ký manifest bằng khoá KMS của bên ghi;
5. ghi `_READY`.

Dùng công cụ này là tiện, không bắt buộc. CD chỉ kiểm hợp đồng, không kiểm ai đã ghi bằng công cụ
gì.

### 4.3 Tự phát hiện và các cổng

`models/catalog.yaml` khai model nào được theo dõi — do người sửa qua PR:

```yaml
paused: false                    # công tắc dừng khẩn cho MỌI rollout, model lẫn image

qwen2.5-7b:
  watch: models/moc-7b/          # nơi bên ghi đặt các version mới
  auto_rollout: true             # qua hết cổng là lên production
  allowed_signers: [training-pipeline]
```

`watch-models.yml` chạy 15 phút một lần, liệt kê `_READY` mới dưới các thư mục được theo dõi. Có
nhiều version mới thì lấy version mới nhất, bỏ các bản cũ hơn.

**Công tắc dừng khẩn.** `paused: true` làm `watch-models.yml` và `rollout.yml` dừng ở bước kế tiếp:
rollout đang chạy đứng nguyên chỗ, không rollout mới nào bắt đầu. Để trong git để việc dừng cũng
có lịch sử. Dùng khi cần đo tải, khi demo, hoặc khi production có gì lạ chưa rõ nguyên nhân.

| Cổng | Ở đâu | Kiểm gì | Trượt thì |
|---|---|---|---|
| Hợp lệ | CI, không cần GPU | chữ ký đúng và bên ký có trong `allowed_signers`; `_READY` có; `name`/`version` khớp đường dẫn; mọi file trong manifest có trên S3, đúng kích thước; chỉ safetensors | bỏ qua version, mở issue |
| Tương thích | CI, không cần GPU | `config.json` có kiến trúc nằm trong danh sách đã thử với bản vLLM đang ghim; có tokenizer; trọng số + KV cache cho `max_model_len` vừa 24 GB của A10G | như trên |
| Nạp | init container | sync sang NVMe, băm sha256 từng file, so manifest | pod không lên → bỏ ứng viên |
| Đánh giá | Job trong cụm | 144 câu vàng, bộ tấn công, k6 `steady` ~12 req/s, chỉ vào ứng viên | bỏ ứng viên, mở issue |
| Canary | Job trong cụm | guardrail chia 75/25 trong 15 phút; so hai bản theo metric có nhãn `model_version` | `weight: 0`, bỏ ứng viên |
| Promote | Argo | cuốn 3 pod stable sang bản mới, từng pod một | quay về `previous` |
| Theo dõi | Job trong cụm | 60 phút sau promote: availability, p95, tỷ lệ trích dẫn, chặn oan trên traffic thật | tự quay về `previous`, mở issue |

Hai cổng đầu rẻ và chạy trước khi tốn GPU. Cổng Nạp là lần kiểm sha256 có hiệu lực: nó chứng minh
đúng thứ GPU sắp nạp khớp manifest, và bắt được cả trường hợp ai đó ghi đè file sau khi CI đã kiểm.

**Mỗi lần promote đều mở một issue** ghi version cũ, version mới, `source` và đường dẫn báo cáo,
rồi tự đóng khi bước Theo dõi đạt. Không ai duyệt trước, nên ít nhất mọi người phải biết sau.

**Canary chia ở guardrail, không chia bằng pod.** `MODEL_ROUTES_JSON` mở rộng để một tên model trỏ
tới hai upstream có trọng số. Đổi tỷ lệ không khởi động lại pod GPU nào, và guardrail biết câu trả
lời nào do bản nào sinh, nên đo được **chất lượng theo phiên bản** ngay trong canary. Trước canary,
ứng viên có tên riêng `qwen2.5-7b-candidate` mà chỉ key đánh giá được gọi.

### 4.4 Ngưỡng — so với bản đang chạy

Mốc đo ngày 07/10/2026: ramp 30 phút, 34.354 probe, bỏ qua cache.

| Chỉ số | Bản đang chạy | Cổng đánh giá | Cổng canary và theo dõi |
|---|---|---|---|
| p95 | 1590 ms ở 50 req/s, 4 card | < 3000 ms ở ~12 req/s, 1 card | không tệ hơn stable quá 20% |
| Có trích dẫn | 100% | ≥ 95% trên 144 câu vàng | ≥ 95% |
| Chặn oan ở grounding | 0,23% | không tệ hơn quá khoảng tin cậy | như bên trái |
| Chặn tấn công | 100% (349 mẫu) | ≥ 95% | — |
| Availability, cận dưới 95% | 99,72% | — | ≥ 99,5% |
| Token ra trung bình | 36,5 | chỉ báo cáo — ảnh hưởng chi phí | chỉ báo cáo |

144 câu vàng là ít; chênh 1–2 câu không tách được khỏi nhiễu. Cổng gồm một sàn tuyệt đối cộng điều
kiện "không tệ hơn quá khoảng tin cậy", cùng cách `availability.py` đang đọc theo cận dưới.

Model học từ dữ liệu nội bộ có thể nhớ và nhả lại dữ liệu đó. Cổng đánh giá nên có thêm một bộ câu
dò PII; guardrail đã quét PII ở đầu ra, nhưng một model nhả PII thường xuyên vẫn không nên lên
production.

### 4.5 Rollback

| Hỏng ở đâu | Quay về thế nào | Mất bao lâu |
|---|---|---|
| Hợp lệ, tương thích, nạp, đánh giá | bỏ ứng viên; người dùng chưa từng thấy bản mới | tức thì |
| Canary | `weight: 0`, guardrail rollout lại | vài giây |
| Sau promote | `stable = previous`; từng pod sync, băm, nạp lại | tới 20 phút (giới hạn startupProbe) |

Quay về sau promote xảy ra tự động khi bước Theo dõi trượt, hoặc bằng tay bằng cách revert PR
promote. Không đủ GPU để giữ bản cũ song song sau khi promote, nên promote cuốn từng pod
(`maxUnavailable: 1`, `maxSurge: 0`). Trong lúc cuốn, ứng viên vẫn phục vụ phần của nó, nên dung
lượng không tụt dưới 3/4.

### 4.6 Ký manifest — Cosign với khoá AWS KMS

**Mỗi bên ghi model có một khoá KMS bất đối xứng riêng** (ECC NIST P-256).

- Ký: `cosign sign-blob --key awskms:///alias/<bí danh khoá> --tlog-upload=false manifest.json`
- Kiểm trong CI: `cosign verify-blob` với khoá công khai tương ứng, bỏ qua tlog vì không đẩy lên.

| Bên ghi | Khoá | Ai được ký |
|---|---|---|
| Pipeline huấn luyện (khi có) | `alias/model-signer-training` | IAM role của pipeline đó |
| Bên ghi bản demo | `alias/model-signer-demo` | role của người chạy demo; xoá sau khi có bên ghi thật |

Thêm một bên ghi mới là: tạo khoá, thêm khoá công khai vào repo, thêm tên vào `allowed_signers`.
CD không đổi gì.

**Vì sao chọn cách này:**

- **Khoá bí mật không rời KMS.** Không có file khoá nào để lộ, để chép, hay để quên xoay vòng.
- **Quyền ký là quyền IAM `kms:Sign` trên đúng khoá đó.** Cấp, thu hồi, kiểm toán đều qua IAM và
  CloudTrail; mỗi lần ký để lại một dòng ghi ai ký, lúc nào.
- **Không đẩy lên log công khai.** `--tlog-upload=false` bỏ qua Rekor, log công khai của Sigstore.
  Với model nội bộ, công bố "vừa ký một model có digest X lúc Y" là lộ thông tin không cần thiết.
- **Bên ghi chạy trong AWS thì chỉ cần một IAM role**, không thêm hạ tầng danh tính nào.
- **Mỗi bên một khoá.** CD biết chính xác bên nào ký. Thu hồi một bên là tắt khoá đó và gỡ khoá công
  khai khỏi repo, không ảnh hưởng bên khác.

Khoá công khai nằm trong repo ở `deploy/signers/<bên>.pub` — khoá công khai không phải bí mật, và để
trong git thì việc thêm hay gỡ một bên ký cũng đi qua PR. Khoá KMS tạo bằng Terraform ở
`terraform/core`, tầng sống qua mọi lần huỷ cụm.

**Xoay vòng khoá.** KMS chỉ tự xoay vòng khoá đối xứng. Với khoá bất đối xứng: tạo khoá mới, thêm
khoá công khai mới vào `deploy/signers/`, chuyển bên ghi sang khoá mới, giữ khoá công khai cũ tới
khi không còn version nào do khoá cũ ký đang được dùng.

**Phương án đã cân nhắc:**

| Phương án | Vì sao không chọn cho model |
|---|---|
| Cosign keyless (Sigstore) | ghi lên log công khai; phụ thuộc Fulcio và Rekor đang chạy đúng lúc rollout; bên ghi phải có danh tính OIDC mà Sigstore chấp nhận |
| Cặp khoá Cosign để trong secret | khoá bí mật nằm ở dạng file, phải tự chép, cất và xoay vòng; lộ một lần là phải thay ở mọi nơi |
| Chỉ dựa vào quyền ghi S3 | chứng minh *ai được ghi*, không chứng minh *ai đã ghi*; một role có quyền ghi bị lạm dụng là đủ đưa model lên production |

Image vẫn ký keyless (§5.1), vì hoàn cảnh khác: image build từ repo public, nên bản ghi công khai là
điều mong muốn chứ không phải rò rỉ.

---

## 5. Luồng image

### 5.1 Image tự build: guardrail, bench-runner

| Khi | Workflow | Các bước | Chặn khi |
|---|---|---|---|
| Mỗi PR | `pr.yml` | test (143 unit test, `guardrails-test`, `attacks-score`, `pii-verify`, `rag-eval`); build không đẩy; Trivy quét mã nguồn và Dockerfile | test hỏng; cấu hình Dockerfile nguy hiểm |
| Merge vào `main` | `images.yml` | build bằng buildx có cache; Trivy quét image; tạo SBOM; ký Cosign keyless theo digest; đẩy ECR; mở PR ghi digest mới vào `deploy/state.yaml` | CVE CRITICAL/HIGH **có bản vá**; quét hỏng |
| Sau khi Argo đồng bộ | Job smoke trong cụm | một phần câu vàng + mẫu tấn công qua guardrail mới | revert về `previous`, mở issue |

- **Giữ tag `src-<hash nội dung>` đang có.** Cùng mã nguồn cho cùng tag; ECR đã để tag bất biến, nên
  tag đã tồn tại thì bỏ qua bước build — merge chỉ sửa tài liệu không build lại image.
- **Deploy theo digest, không theo tag.** `deploy/state.yaml` ghi `@sha256:…`.
- **Ký keyless:** chữ ký gắn với danh tính OIDC của workflow trên repo này, không có khoá bí mật nào
  phải giữ.
- Đổi guardrail không đụng GPU: rolling update xong trong vài chục giây. Nhánh này không cần ứng
  viên, đánh giá dài hay canary; các bài test nặng đã chạy ở PR.
- **Ở bản sau**, có thể thêm kiểm chữ ký lúc admission (Sigstore policy-controller hoặc Kyverno),
  để image không đi qua CI thì không chạy được trong cụm.

### 5.2 Image upstream: vLLM, LiteLLM, Open WebUI, cloudflared, aws-cli

- **Renovate** (miễn phí cho repo public) mở PR khi có bản mới, và ghim theo `tag@sha256:digest`.
- `pr.yml` quét image mới bằng Trivy, chặn theo cùng ngưỡng như trên.
- **Bản mới của vLLM** đi theo luồng model ở §4: đổi engine thì đổi câu trả lời và độ trễ, nên phải
  qua đánh giá và canary.
- **LiteLLM, Open WebUI, cloudflared, aws-cli:** rolling update rồi smoke test như guardrail.

---

## 6. Thay đổi trong repo

| File | Thay đổi |
|---|---|
| `bench/scripts/publish_model.py` (mới) | công cụ cho bên ghi model: kiểm, đẩy, băm, ghi manifest, ký, ghi `_READY` |
| `bench/scripts/check_model.py` (mới) | các cổng Hợp lệ và Tương thích; dùng chung cho CI và cho bên ghi |
| `charts/vllm` | Deployment `stable` + `candidate`, mỗi bên có trọng số và engine riêng; số pod stable = 4 trừ số ứng viên; init container băm và so manifest; tên phục vụ của ứng viên |
| `services/llm_pipeline/app.py` | `MODEL_ROUTES_JSON` nhận nhiều upstream có trọng số; nhãn `model_version` trên metric; test |
| `charts/guardrail` và các chart khác | image sinh từ `deploy/state.yaml`, theo digest |
| `models/catalog.yaml`, `deploy/state.yaml` | mới |
| `deploy/signers/*.pub` | khoá công khai của các bên được phép ký model |
| `deploy/argocd/` | Application cho `vllm`, `guardrail`; chart khác chuyển dần |
| `k8s/rollout/` | Job đánh giá, canary, smoke, theo dõi; ghi báo cáo lên `runs/rollout/<id>/` |
| `.github/workflows/` | `pr.yml`, `images.yml`, `watch-models.yml`, `rollout.yml` |
| `renovate.json` | theo dõi image upstream, ghim digest |
| `terraform/core` | OIDC provider của GitHub + role cho Actions (đọc `models/`, đọc `runs/rollout/`, đẩy ECR); role IRSA cho Job rollout; khoá KMS ký model, key policy chỉ cho đúng role của bên ghi `kms:Sign` |
| `terraform/cluster/variables.tf` | `cpu_desired = 3` mặc định — `desired_size` chỉ có hiệu lực lúc tạo node group |
| `Makefile` | `lab-up` cài Argo CD và các Application; các target `*-secret` vẫn chạy trước, vì secret không nằm trong git |
| GitHub App (cấu hình trên GitHub, không phải file) | quyền `contents` + `pull_requests`; khoá riêng trong secret của Actions; branch protection trên `main` bắt buộc PR + check |

---

## 7. Lộ trình

| Giai đoạn | Việc | Công |
|---|---|---|
| P1 | Hợp đồng model; `check_model.py`, `publish_model.py`; khoá KMS; init container băm; ghi lại một checkpoint demo theo hợp đồng | 2 ngày |
| P2 | `pr.yml`, `images.yml`; OIDC + role trong `terraform/core` | 1,5 ngày |
| P3 | Argo CD trong `lab-up`; Application; `deploy/state.yaml` | 1,5 ngày |
| P4 | chart stable/candidate; route có trọng số và nhãn `model_version` trong guardrail | 1,5 ngày |
| P5 | Job đánh giá, canary, smoke, theo dõi sau promote | 2 ngày |
| P6 | `watch-models.yml`, `rollout.yml`: máy trạng thái, PR qua GitHub App và tự merge, tự revert, issue mỗi lần promote, công tắc `paused`; branch protection | 2 ngày |
| P7 | Renovate, ghim digest cho mọi image upstream | 0,5 ngày |
| P8 | demo đầu-cuối | 1 ngày |

**Tổng:** khoảng 12 ngày công.

### Kịch bản demo

Bản đang chạy: 7B FP16. Bản mới: 7B AWQ, lấy từ checkpoint demo có sẵn trên S3 và ghi lại bằng
`publish_model.py` vào `models/moc-7b/<version>/`, ký bằng `alias/model-signer-demo`. Với CD, nó
không khác gì một model do công ty tự huấn luyện.

1. **Model tự lên:** chạy `publish_model.py`. Không chạm tay vào gì nữa: trong vòng 15 phút CD phát
   hiện → kiểm → đánh giá → canary 25% → promote → theo dõi. Lịch sử PR của `deploy/state.yaml` và
   Grafana cho thấy từng bước.
2. **Ca âm, không tốn GPU:** một version có file `.bin`, hoặc ký bằng khoá không có trong
   `allowed_signers` → bị chặn ở cổng Hợp lệ.
3. **Ca âm toàn vẹn:** ghi đè một file sau khi CI đã kiểm → init container từ chối khởi động.
4. **Ca âm chất lượng:** ghi bản 1.5B dưới `served_name: qwen2.5-7b` → đánh giá trượt vì tỷ lệ trích
   dẫn tụt → tự bỏ ứng viên, mở issue kèm báo cáo.
5. **Image:** sửa code guardrail → PR xanh → merge → build, quét, ký, đẩy → rolling update → smoke
   đạt. Ca âm: một thay đổi làm smoke trượt → tự revert về digest cũ.
6. Sáng hôm sau dựng lại cụm: lên thẳng các bản đã promote.

---

## 8. Chi phí

- GitHub Actions và Renovate: miễn phí, vì repo public.
- Một lần rollout model cần cụm đang chạy với 4 GPU khoảng 45 phút, chưa kể 60 phút theo dõi chạy
  trên traffic thường: **~3,4 USD** ở mức 4,49 USD/giờ. Rollout chờ tới khi cụm bật, không tự bật
  cụm.
- Rollout image guardrail: không tốn GPU.
- Node tooling thứ ba: **+~0,10 USD/giờ** khi cụm chạy.
- KMS: khoảng 1 USD/tháng mỗi khoá ký; phí mỗi lần ký không đáng kể.
- ECR: đã có lifecycle policy dọn image cũ.

---

## 9. Rủi ro

| Rủi ro | Hệ quả | Giảm thiểu |
|---|---|---|
| Không có người duyệt trước khi lên production | bản tệ theo kiểu cổng không đo được vẫn lên | bước Theo dõi 60 phút tự quay về; issue mỗi lần promote; `paused` để dừng khẩn |
| Đang rollout model chỉ còn 3/4 dung lượng | đo TC1a lúc đó ra số sai | bật `paused` trước khi đo; k6 từ chối chạy khi `phase` khác `idle` |
| Cụm tắt giữa chừng | rollout treo | lịch 15 phút tiếp tục khi cụm lên |
| Bên ghi không theo hợp đồng | CD bỏ qua version, model không lên | bên ghi chạy `check_model.py` trước khi ghi `_READY`; issue nói rõ trượt điều kiện nào |
| Trọng số dạng pickle | chạy code tuỳ ý khi nạp | chỉ nhận safetensors |
| Ai đó ghi đè version đã có | rollback ra bản khác | băm lúc nạp phát hiện; bật S3 versioning để còn bản gốc |
| Model nhả dữ liệu nội bộ | lộ PII qua câu trả lời | bộ câu dò PII trong cổng đánh giá, cộng quét PII đầu ra của guardrail |
| 144 câu vàng không tách được chênh lệch nhỏ | cổng cho qua bản hơi tệ hơn | ghi cỡ mẫu; sàn tuyệt đối; canary và theo dõi đo thêm trên traffic thật |
| Rollback sau promote mất tới 20 phút | gián đoạn | canary là nơi bắt lỗi chính; promote cuốn từng pod |
| PR của bot không chạy check | auto-merge chờ mãi, rollout treo | mở PR bằng token GitHub App (C4) |
| Lộ khoá riêng của GitHub App | kẻ khác mở và merge PR sửa `deploy/state.yaml` | App chỉ có quyền trên repo này; check bắt buộc vẫn chạy; thu hồi khoá trong cài đặt App |
| Lộ quyền `kms:Sign` | ký được model giả | key policy chỉ cho một role; CloudTrail ghi mọi lần ký; tắt khoá là chặn ngay |
| Renovate mở quá nhiều PR | nhiễu | gom theo nhóm, lịch hằng tuần |
| Node tooling hết CPU | Argo `Pending` | node thứ ba mặc định |

---

## 10. Đã chốt (08/10/2026)

| Câu hỏi | Quyết định |
|---|---|
| Model lấy từ đâu? | **Không giới hạn.** CD chỉ nhận version đúng hợp đồng §4.1 trên S3 |
| Tự lên production hay chờ người bấm? | **Tự lên** khi qua hết cổng; thay người duyệt bằng bước Theo dõi, issue mỗi lần promote, công tắc `paused` |
| Có bước canary không? | **Có**, 25% trong 15 phút |
| Ký manifest model bằng gì? | **Cosign + khoá AWS KMS**, mỗi bên ghi một khoá, không đẩy lên log công khai (§4.6) |
| Bot sửa `main` thế nào? | **Mở PR rồi tự merge** khi check xanh, bằng token GitHub App (C3, C4) |
