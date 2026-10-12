# Đề xuất: ML-001 cần một nhánh nhận diện luồng manifest-đã-ký

**Gửi** nhóm Deployment Quality Gate (Hồ Trọng Hảo — chủ rule; Nguyễn Gia Thiều — chủ
`policies/kyverno/`)
**Từ** Nguyễn Gia Bảo, DA#51 — LLM Serving & Guardrails
**Ngày** 12/10/2026 · gate ở commit `6ff9d3b2`

---

## Vấn đề, nói ngắn

ML-001 **không nổ** trên pod vLLM của DA#51. Không phải vì DA#51 đạt, mà vì policy không
nhận ra initContainer của DA#51 là một bước tải model.

`policies/kyverno/ml/ml-001-model-source-checksum.yaml` nhận diện initContainer tải model
bằng volume tên `model-cache` **hoặc** biến môi trường `*_MODEL_ID` (theo ghi chú trạng thái
28/09 trong `docs/ml-001-ml-006-dac-ta.md`). Pod vLLM của DA#51 dùng volume `weights` và các
biến `S3_BUCKET`, `S3_MODEL_URI`, `VERIFY_MANIFEST`.

Kết quả đo được: trong 25 vi phạm mà `kyverno apply` tìm thấy trên manifest của DA#51,
**không có ML-001**. Cổng báo xanh về model provenance trong khi **không kiểm gì** — đúng
cái bẫy mà template của chính gate cảnh báo: *"Gate KHÔNG đoán đường dẫn: đoán sai thì job
xanh trong khi chẳng quét gì."*

Đây là loại sai tệ hơn một vi phạm. Một vi phạm thì có người sửa; một rule không nổ thì nằm
trong báo cáo như một dòng đã đạt.

---

## Vì sao DA#51 không nên đổi chart cho khớp

Đổi tên volume thành `model-cache` và thêm một biến `*_MODEL_ID` sẽ làm ML-001 nổ và DA#51
sẽ trượt điều kiện 4 ("phải chạy `app.model_fetch`"). Nhưng điều đó **hạ chất lượng thật**,
vì hai cơ chế không tương đương:

| | `rag-service` | DA#51 |
|---|---|---|
| Nguồn | Hugging Face lúc pod khởi động | S3, theo hợp đồng `docs/model-publishing-contract.md` |
| Ghim bản nào | `EMBEDDING_MODEL_REVISION` = commit SHA | prefix `models/<tên>/<version>/`, version không bao giờ được ghi đè |
| Toàn vẹn | checksum ghim bằng `pin_model_checksums.py` | `manifest.json` có sha256 **từng file**, **ký bằng cosign + khoá KMS** |
| Kiểm khi nào | lúc tải | lúc tải, **và băm lại từng file trên NVMe mỗi lần pod khởi động** |
| Ai được phép đưa model vào | ai sửa được biến env | chỉ bên có khoá trong `deploy/signers/`, thêm bên mới là một PR có review |

Điểm then chốt: đặc tả ML-001 nói rõ giới hạn của chính nó — *"Kyverno không đọc được bên
trong image, nên nó kiểm entry point đã biết là có verify"*. Với DA#51, thứ tương đương
không phải một entry point mà là **một cờ**: `VERIFY_MANIFEST=true`. Khi cờ đó bật, init
container từ chối khởi động nếu bất kỳ file nào lệch sha256 trong manifest đã ký.

---

## Đề xuất cụ thể

Thêm một nhánh thứ hai vào ML-001, không thay nhánh đang có:

**Nhận diện** — pod được coi là có bước tải model nếu **một trong hai**:
- (đang có) mount `emptyDir` tên `model-cache`, **hoặc** có env `*_MODEL_ID`; **hoặc**
- (mới) initContainer có env `S3_MODEL_URI` khớp `^s3://`

**Điều kiện kiểm cho nhánh mới:**

| # | Kiểm | Vi phạm khi |
|---|---|---|
| 1 | initContainer có `S3_MODEL_URI` thì phải có env `VERIFY_MANIFEST` | thiếu hẳn biến |
| 2 | `VERIFY_MANIFEST` phải là `"true"` | bằng `"false"` — nghĩa là nạp trọng số không ai kiểm |
| 3 | `S3_MODEL_URI` phải trỏ vào một prefix có đủ ba đoạn `models/<tên>/<version>` | prefix nông hơn: không xác định được version nào |
| 4 | Pod có mount `emptyDir` nhận trọng số thì `emptyDir` đó phải có `sizeLimit` | thiếu — đã do ML-005 phủ, nêu ở đây cho đủ |

Thông báo lỗi đề nghị, theo đúng yêu cầu "phải nói cách sửa" của catalog mục 1.4:

```
[ML-001] initContainer "<tên>" trong "<tên deployment>" tải trọng số từ <uri> nhưng
VERIFY_MANIFEST không bật, nên trọng số được nạp mà không đối chiếu manifest đã ký.
Cách sửa: đặt VERIFY_MANIFEST="true". Nếu checkpoint chưa có manifest thì phải xuất bản
lại theo hợp đồng model trước khi đưa lên production.
```

**Một điểm DA#51 sẽ trượt ngay, và đó là đúng.** `deploy/state.yaml` hiện khai
`verify: false` cho cả hai track, kèm lý do: *"checkpoint dua len tay truoc khi co hop dong
model; chua co manifest"*. Nhánh mới sẽ bắt đúng chỗ đó. DA#51 muốn nó bị bắt — ngoại lệ đó
cần một ngày hết hạn, và một rule chặn là cách tốt hơn một dòng ghi chú trong YAML.

---

## Việc nhỏ hơn, nếu nhánh trên là quá nhiều

Nếu thêm nhánh là quá sức tuần này, thì xin **một thay đổi một dòng**: cho ML-001 nổ thành
**vi phạm** khi pod có một `emptyDir` ≥ 10 GiB mà không khớp *bất kỳ* mẫu nhận diện nào.
Nói cách khác: *"pod này rõ ràng đang nạp một artefact lớn, và tôi không biết nó từ đâu"*.
Như vậy một luồng chưa được mô hình hoá sẽ thành **đỏ** chứ không thành **xanh**, và chính
là chiều an toàn khi sai.

---

## Hai điều DA#51 muốn nói kèm

**ML-007 đạt, và nên ghi nhận là một hình dạng khác chứ không phải không áp dụng.** DA#51
không đóng trọng số vào image, nên rule "image nhúng model phải có LABEL truy nguyên" không
chạm tới. Truy nguyên nằm ở manifest đã ký — mạnh hơn một LABEL, vì LABEL ai cũng viết được.

**ML-006 bắt được một rủi ro thật.** Volume `weights` của DA#51 là `emptyDir` nhận 5–15 GiB
mà không có trần `ephemeral-storage`. Đã sửa ngày 12/10 sau khi chạy policy của các bạn. Câu
trong đặc tả — *"một model lớn bất thường có thể ép node evict pod của người khác"* — là
mô tả đúng chỗ chart của DA#51 đang sai. Cảm ơn.

---

## Liên hệ

Kết quả đo đầy đủ, cách DA#51 chạy policy của các bạn từ GitHub Actions, và danh sách ngoại
lệ có thời hạn: [`docs/gate-compliance.md`](gate-compliance.md) trong repo công khai
[`Potato1476/vllm-bench`](https://github.com/Potato1476/vllm-bench). Chạy lại bằng
`make gate-check`.
