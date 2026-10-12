# Hợp đồng xuất bản model — dành cho bên ghi

Tài liệu này mô tả **chính xác** những gì bước `publish` trong CI phải tạo ra trên S3 để
nền tảng serving của DA#51 chấp nhận và tự đưa model ra production.

Đọc xong là implement được, không cần biết gì về repo `vllm-bench`.

| | |
|---|---|
| **Dành cho** | Nhóm Deployment Quality Gate (bước `publish` trong pipeline CI) |
| **Liên hệ** | Nguyễn Gia Bảo — DA#51 |
| **Công cụ kiểm** | `bench/scripts/check_model.py` trong repo công khai [`Potato1476/vllm-bench`](https://github.com/Potato1476/vllm-bench) |

---

## 0. Vì sao có hợp đồng này

Phía DA#51 **không có người duyệt**. Một version model qua hết các cổng kiểm là tự lên
production, không ai bấm nút. Nên các quy tắc dưới đây không phải khuyến nghị — chúng là
điều kiện để CD nhận model, và thiếu bất kỳ điều nào thì **mọi model sẽ bị từ chối**.

Đổi lại, hợp đồng cố tình **không quan tâm model từ đâu ra**: tải về, fine-tune, tự huấn
luyện, mua. CD chỉ biết hợp đồng này và không biết gì khác về pipeline của bên ghi.

---

## 1. Bố cục trên S3

```
s3://<bucket>/models/<tên-model>/<version>/
├── config.json                       bắt buộc
├── tokenizer.json                    (hoặc tokenizer.model, hoặc vocab.json)
├── generation_config.json            tuỳ chọn
├── model-00001-of-0000N.safetensors  CHỈ safetensors
├── model.safetensors.index.json
├── manifest.json                     xem §2
├── manifest.bundle                   chữ ký, xem §3
└── _READY                            file rỗng, GHI SAU CÙNG
```

### Quy tắc

**`<tên-model>` và `<version>`** phải khớp `^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$`.

**`<version>` không bao giờ được ghi đè.** Rollback phải trả về **đúng bộ byte đã chạy**;
ghi đè một version làm cho `deploy/state.yaml` vẫn trỏ đúng tên nhưng nội dung đã khác —
và rollback khôi phục một thứ không ai chọn. Bucket đã bật versioning nên vẫn cứu được,
nhưng đừng dựa vào đó.

> **Đề xuất:** đặt `<version>` theo **commit sha của nguồn** (ví dụ Hugging Face), không
> theo ngày. `moc-7b/db1f81ad4b8c` tái lập được; `moc-7b/2026-10-12` thì không.

**`_READY` phải được ghi SAU CÙNG, sau khi mọi file khác đã lên xong.**
S3 không có thư mục nguyên tử, và một bộ trọng số 5–15 GiB mất vài phút để tải lên. CD dò
S3 định kỳ; thiếu quy tắc này nó sẽ bốc một version đang ghi dở, rồi báo lỗi về model
trong khi nguyên nhân là một cuộc đua trong pipeline.

**Chỉ nhận `.safetensors`.** Mọi file `.bin`, `.pt`, `.pth`, `.ckpt`, `.pkl`, `.pickle`,
`.h5`, `.msgpack` đều bị **từ chối cứng**. Đó là định dạng torch pickle, và unpickle chạy
bất kỳ `__reduce__` nào file yêu cầu — **nạp một model như vậy là thực thi nó**. Trọng số
có thể đi qua nhiều tay trước khi tới đây, nên đây là điều kiện không thương lượng.

**File không có trong `manifest.json` cũng bị từ chối.** Chữ ký chỉ phủ manifest, nên một
file không được khai là nội dung **chưa ký** nằm ngay trong thư mục engine sẽ đọc.

---

## 2. `manifest.json`

```json
{
  "name": "moc-7b",
  "version": "db1f81ad4b8c",
  "kind": "full",
  "served_name": "qwen2.5-7b",
  "producer": "quality-gate-ci",
  "source": "hugging-quants/Meta-Llama-3.1-8B-Instruct-AWQ-INT4 @ db1f81ad4b8c...",
  "files": [
    {
      "path": "model-00001-of-00002.safetensors",
      "size": 4681234944,
      "sha256": "5b29ed6f80e422c3ae7d40dc76fd8983501bbd7489cf84a72a5c5fe20bcf2813"
    }
  ]
}
```

| Trường | Bắt buộc | Ghi chú |
|---|---|---|
| `name` | ✅ | **phải khớp `<tên-model>` trong đường dẫn** |
| `version` | ✅ | **phải khớp `<version>` trong đường dẫn** |
| `served_name` | ✅ | tên model mà người gọi dùng trong request, vd `qwen2.5-7b` |
| `kind` | — | `full` (mặc định) hoặc `lora` |
| `files` | ✅ | **mọi file** trong thư mục trừ `manifest.json`, `manifest.bundle`, `_READY` |
| `files[].path` | ✅ | tương đối, **không** bắt đầu bằng `/`, **không** chứa `..` |
| `files[].size` | ✅ | byte |
| `files[].sha256` | ✅ | 64 ký tự hex |
| `producer` | — | tên bên ghi, để truy vết |
| `source` | — | mô tả tự do model từ đâu ra; CD không đọc để quyết định gì |

### Vì sao `name`/`version` phải khớp đường dẫn

Chữ ký **không** chứng minh điều này. Một manifest đã ký là lời khẳng định về **một**
version; chép nguyên nó sang thư mục của version khác thì nó **vẫn ký hợp lệ**, mọi hash
vẫn khớp, model vẫn nạp và vẫn phục vụ bình thường.

Thiếu kiểm tra này, người chỉ có quyền **ghi** S3 — không cần quyền ký — có thể đặt trọng
số tháng trước vào version hôm nay, và CD sẽ promote một bản lùi như một bản nâng cấp,
im lặng và vĩnh viễn.

---

## 3. Chữ ký — `manifest.bundle`

Ký `manifest.json` bằng **cosign v3** với khoá **AWS KMS** riêng của bên ghi.

```bash
cosign sign-blob \
  --key awskms:///alias/model-signer-quality-gate \
  --bundle manifest.bundle \
  --yes \
  manifest.json
```

### Lưu ý về cosign v3

`--output-signature` và `--tlog-upload` **đã bị gỡ** ở v3. Phải dùng `--bundle`, và nó
ghi ra một file JSON chứa chữ ký cùng siêu dữ liệu xác minh. DA#51 đã dính lỗi này một lần
— pipeline chết **sau khi đã băm xong 5,34 GiB**.

Ký bằng `--key` và không có signing config thì **không có lời gọi mạng nào** — không gì
lên Rekor. Đó là chủ ý: với trọng số nội bộ, một bản ghi công khai *"tổ chức này vừa ký
artefact digest X lúc Y"* là lộ thông tin không mua lại được gì.

### Việc cần làm một lần — phía bên ghi

```bash
KEY=$(aws kms create-key --key-spec ECC_NIST_P256 --key-usage SIGN_VERIFY \
        --description "Quality Gate CI: model signer" \
        --query KeyMetadata.KeyId --output text)
aws kms create-alias --alias-name alias/model-signer-quality-gate --target-key-id "$KEY"

cosign public-key --key awskms:///alias/model-signer-quality-gate > quality-gate.pub
```

Gửi file `quality-gate.pub` cho DA#51. Nó sẽ được commit vào `deploy/signers/` và thêm
vào danh sách `allowed_signers`.

**Khoá công khai nằm trong git là có chủ ý:** thêm một bên được phép đưa model vào
production là thay đổi phải qua review, không phải một lệnh ai đó chạy trên máy mình.

Khoá bí mật không bao giờ rời KMS. Quyền ký là quyền IAM `kms:Sign` trên đúng một khoá,
và CloudTrail ghi lại mọi lần dùng.

---

## 4. Tự kiểm trước khi ghi `_READY`

Repo công khai, chạy được ngay:

```bash
git clone https://github.com/Potato1476/vllm-bench.git
cd vllm-bench

python3 bench/scripts/check_model.py \
  --source ./build/moc-7b \
  --expect-name moc-7b \
  --expect-version db1f81ad4b8c \
  --verify-hashes \
  --json
```

Thoát `0` là qua. Chỉ phụ thuộc Python 3.10+ và `cosign` trên PATH.

**Chạy nó ở cuối bước `publish`, trước khi ghi `_READY`.** Cổng phía CD chạy cùng bộ kiểm
này mười lăm phút sau, trong một pipeline không ai nhìn, và từ chối của nó tới dưới dạng
một GitHub issue. Cùng lỗi đó bắt tại chỗ là một dòng trên terminal của người vừa gây ra.

Nếu tiện, có thể dùng luôn `bench/scripts/publish_model.py` — nó làm trọn: băm, kiểm, ký,
upload, đọc lại từ S3 đối chiếu, rồi mới ghi `_READY`.

```bash
python3 bench/scripts/publish_model.py \
  --from ./build/moc-7b --name moc-7b --version db1f81ad4b8c \
  --served-name qwen2.5-7b --signer quality-gate --bucket <bucket>
```

---

## 5. Các cổng phía DA#51 — để biết thứ gì sẽ bị từ chối

| Cổng | Kiểm gì | Khi nào |
|---|---|---|
| **Hợp lệ** | chữ ký và bên ký được phép; `_READY` có; `name`/`version` khớp đường dẫn; mọi file trong manifest có trên S3 đúng kích thước; chỉ safetensors; không có file lạ | CI, không cần GPU |
| **Tương thích** | `config.json` khai kiến trúc nằm trong danh sách đã chạy thử; có tokenizer; trọng số + KV cache vừa card 24 GB | CI, không cần GPU |
| **Nạp** | băm lại sha256 **từng file trên ổ của node** sau khi sync, so với manifest | mỗi lần pod khởi động |
| **Đánh giá** | 144 câu hỏi vàng, bộ tấn công, đo tải | trên 1 GPU riêng |
| **Canary** | 25% lưu lượng thật trong 15 phút | trên cụm |

Kiến trúc hiện được chấp nhận: `Qwen2ForCausalLM`, `Qwen3ForCausalLM`,
`LlamaForCausalLM`, `MistralForCausalLM`. Kiến trúc khác **không phải bị đánh giá là
kém** — chỉ là chưa ai cho nó qua cổng đánh giá trên bản vLLM đang ghim, nên đường tự động
dừng lại và cần người quyết định. Mở rộng bằng một PR sau khi đã chạy thử.

Cổng **Nạp** đáng nói riêng: nó là lần kiểm **có hiệu lực**. Các cổng phía trước chứng minh
bucket đúng *tại thời điểm kiểm*; cổng này chứng minh **đúng bộ byte mà GPU sắp mở** là bộ
đã ký, trên mỗi lần pod khởi động. Nó cũng là thứ duy nhất chặn giữa việc một version bị
ghi đè tại chỗ và việc bản ghi đè đó được phục vụ.

---

## 6. Thông báo qua SQS

CD hiện **dò S3 mỗi 15 phút**. Một tin nhắn SQS rút độ trễ xuống còn vài giây.

```json
{
  "bucket": "vllm-bench-artifacts-<account>",
  "prefix": "models/moc-7b/db1f81ad4b8c",
  "name": "moc-7b",
  "version": "db1f81ad4b8c",
  "producer": "quality-gate-ci",
  "published_at": "2026-10-12T09:14:00Z"
}
```

### Quy tắc quan trọng nhất

**Tin nhắn là gợi ý, `_READY` trên S3 là sự thật.** Nhận tin thì CD đi đọc S3 và chạy lại
toàn bộ cổng kiểm từ đầu.

Hệ quả, và đây là lý do thiết kế như vậy:

* **Mất một tin nhắn không làm mất một model.** Lịch dò 15 phút vẫn bắt được.
* **Nhận trùng tin nhắn không gây rollout hai lần.** Máy trạng thái từ chối rollout thứ
  hai trên một track đang bận.
* **Một tin nhắn giả không đưa được gì lên production.** Nó chỉ khiến CD đi đọc S3; không
  có `_READY` và chữ ký hợp lệ thì không có gì xảy ra.

Gửi tin **sau** khi `_READY` đã ghi xong. Gửi trước thì CD đọc một version chưa hoàn tất.

---

## 7. Quyền IAM cần cấp

Phía DA#51 sẽ cấp cho role của CI:

| Cấp | Phạm vi |
|---|---|
| `s3:PutObject`, `s3:AbortMultipartUpload` | **chỉ** `arn:aws:s3:::<bucket>/models/*` |
| `s3:ListBucket` | `<bucket>`, giới hạn prefix `models/` |

| **Không** cấp | Lý do |
|---|---|
| `s3:DeleteObject` trên mọi prefix | version không được xoá hay ghi đè |
| `runs/*` | bằng chứng đo nghiệm thu |
| `datasets/*`, `audit/*` | dữ liệu kiểm thử và nhật ký |

Phía bên ghi cấp cho DA#51: quyền đọc và xoá tin trên SQS queue
(`sqs:ReceiveMessage`, `sqs:DeleteMessage`, `sqs:GetQueueAttributes`).

---

## 8. Danh sách kiểm trước buổi làm việc chung

- [ ] Tạo khoá KMS `alias/model-signer-quality-gate`, gửi `quality-gate.pub` cho DA#51
- [ ] Chốt quy ước đặt `<tên-model>` và `<version>`
- [ ] Chạy `check_model.py --verify-hashes` trên một bộ trọng số thật, thoát `0`
- [ ] Xác nhận `_READY` được ghi **sau cùng** trong bước `publish`
- [ ] Chốt ai tạo SQS queue và cấp quyền hai chiều
- [ ] Gửi ARN của IAM role CI để DA#51 viết bucket policy

---

## 9. Ba lỗi DA#51 đã gặp, để bên ghi khỏi gặp lại

**Cờ cosign cũ.** v3 bỏ `--output-signature` và `--tlog-upload`. Lỗi nổ ra sau khi đã băm
xong 5,34 GiB. Hãy ghim bộ cờ bằng test.

**Bí danh KMS chưa tồn tại.** `cosign` báo lỗi KMS 400 và **cắt mất** phần nguyên nhân;
gọi thẳng `aws kms get-public-key --key-id alias/...` mới thấy `NotFoundException`.

**Lần xuất bản bị gián đoạn chặn chính lần thử lại của nó.** Nếu coi *bất kỳ file nào*
dưới prefix là "version đã tồn tại" thì lần chạy lại bị từ chối với thông báo "version đã
xuất bản" — sai, và đúng là câu khó nghe nhất vào sáng hôm sau. Hãy phân biệt theo `_READY`:
có `_READY` là đã xuất bản, chỉ có file lẻ là rác của lần chạy dở, xoá và làm lại được.
