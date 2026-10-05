# Cắm đề án của bạn vào nền tảng MOC

Dành cho các đội **DA#19, #20, #32, #39, #41, #44, #45**.

Nền tảng này tương thích OpenAI. Nếu đề án của bạn đã gọi OpenAI SDK, phần tích hợp là
**đổi hai dòng**. Trang này tồn tại vì có bốn chỗ khác biệt, và cả bốn đều **hỏng trong im
lặng** — không lỗi, không cảnh báo, chỉ là kết quả sai mà bạn phát hiện ra muộn.

---

## 1. Lấy key và kiểm tra trước khi viết dòng code nào

Xin key của đề án mình (mỗi đề án một key riêng, không dùng chung).

```bash
pip install openai

export MOC_BASE_URL=https://.../v1
export MOC_API_KEY=sk-...
export MOC_AGENT_ID=da32          # id đề án của bạn

python3 clients/python/selftest.py
```

Sáu kiểm tra, khoảng 30 giây. Nó trả lời luôn: key sống chưa, được dùng model nào, guardrail
có chặn thật không, và câu trả lời trông ra sao. **Chạy cái này trước** — nó rẻ hơn nhiều so
với việc viết xong rồi mới phát hiện key cấp sai model.

---

## 2. Cách dùng ngắn nhất

```python
from openai import OpenAI

client = OpenAI(
    base_url="https://.../v1",
    api_key="sk-...",
    default_headers={"X-Agent-Id": "da32"},   # BẮT BUỘC, xem mục 3
)

r = client.chat.completions.create(
    model="qwen2.5-7b",
    messages=[{"role": "user", "content": "Chuyến hoàn thành được định nghĩa thế nào?"}],
)
print(r.choices[0].message.content)
```

Chỉ vậy. Không SDK riêng, không giao thức riêng.

Nếu muốn xử lý từ chối và trích dẫn gọn hơn, dùng `moc_copilot.py` trong thư mục này —
một file, không phụ thuộc gì ngoài `openai`:

```python
from moc_copilot import MocCopilot, Refused

moc = MocCopilot(base_url=..., api_key=..., agent_id="da32")

r = moc.ask("Chuyến hoàn thành được định nghĩa thế nào?")
if isinstance(r, Refused):
    log.warning("guardrail tu choi tai %s: %s", r.stage, r.message)
else:
    print(r.body)          # kèm dòng nhắc nguồn dữ liệu — dùng cái này để HIỂN THỊ
    print(r.cited)         # mã tài liệu đã trích
```

---

## 3. Bốn chỗ khác OpenAI, và tại sao chúng nguy hiểm

### `X-Agent-Id` là bắt buộc, trường `user` không có tác dụng

```python
default_headers={"X-Agent-Id": "da32"}      # ✅
...create(..., user="da32")                  # ❌ không làm gì cả
```

LiteLLM v1.90.2 **không** lấy nhãn từ trường `user` trong body — đo trên chính hệ thống
này, mọi chuỗi đều trả về `end_user="None"`. Thiếu header thì request vẫn chạy bình thường,
chỉ là **dashboard chi phí và độ trễ của đề án bạn sẽ trống**, và không có lỗi nào báo.

### Guardrail từ chối trả về HTTP 400 — đừng retry

OpenAI SDK ném `BadRequestError`, mà phần lớn code hiểu là "mình gửi sai định dạng" rồi
retry. **Không phải.** Đó là quyết định về **nội dung**, và retry chỉ tốn GPU để bị từ chối
lần nữa.

```python
try:
    r = client.chat.completions.create(...)
except BadRequestError as e:
    stage = e.body["error"]["code"]     # injection | grounding | pii_egress | ...
```

| stage | Nghĩa là gì | Retry có ích không |
|---|---|---|
| `injection` | đầu vào bị coi là tấn công | ❌ gửi lại y hệt thì y hệt |
| `pii_ingress` | câu hỏi chứa thông tin cá nhân | ❌ |
| `policy` | tài liệu cần thiết ngoài quyền của key | ❌ |
| `grounding` | model trả lời mà không có nguồn hợp lệ | ⚠️ nền tảng đã tự thử lại một lần |
| `pii_egress` | câu trả lời chứa thông tin cá nhân | ⚠️ |

`Refused.input_was_rejected` trong `moc_copilot.py` phân biệt sẵn hai nhóm này.

### Câu trả lời có một dòng nhắc nguồn dữ liệu ở đầu

Kho tài liệu hiện là **dữ liệu mô phỏng**. Mọi câu trả lời được chèn sẵn một dòng nói rõ
điều đó, ở tầng guardrail chứ không phải trong prompt, nên nó luôn có mặt.

Nếu code của bạn cắt chuỗi theo vị trí, nó sẽ vấp. `moc_copilot.Answer` tách sẵn:
`.body` có dòng nhắc (**dùng để hiển thị cho người**), `.answer` không có (dùng để phân
tích). Đừng tự ý bỏ dòng nhắc khi hiển thị — đó là thứ ngăn một con số mô phỏng đi vào báo
cáo thật.

### Streaming không chạy từng chữ

`stream=True` chạy được, nhưng guardrail **đệm toàn bộ câu trả lời** rồi mới phát, vì một
PII hay một trích dẫn bịa đã gửi đi thì không thu hồi được. Nên sẽ im vài giây rồi hiện cả
đoạn.

Trong giao diện, dùng **spinner** chứ đừng dùng con trỏ gõ chữ — con trỏ gõ chữ đứng yên
đọc như treo máy.

---

## 4. Thứ nền tảng không phục vụ

| | |
|---|---|
| Function calling / tools | từ chối rõ ràng. Guardrail đầu ra kiểm trích dẫn và PII trên **văn bản**, mà tool call không mang văn bản nào |
| `/v1/embeddings` | chưa đi qua guardrail |
| `/v1/completions` | chưa đi qua guardrail |
| Câu hỏi ngoài kho tài liệu | sẽ nhận câu thoái thác, không phải câu trả lời bịa |

Nếu đề án của bạn **cần** function calling hoặc embeddings, hãy nói sớm — nó đổi kiến trúc
chứ không phải bật một cờ.

---

## 5. Chọn model — và chọn đúng profile

Đây là chỗ quyết định đề án của bạn chạy được hay bị chặn trên mọi request.

| Model | Truy hồi tài liệu | Bắt buộc trích dẫn | Dùng cho |
|---|---|---|---|
| `qwen2.5-7b` | ✅ | ✅ | hỏi đáp trên kho tài liệu MOC |
| `qwen2.5-1.5b` | ✅ | ✅ | như trên, câu hỏi đóng |
| `qwen2.5-7b-plain` | ❌ | ❌ | **mọi việc khác** |
| `qwen2.5-1.5b-plain` | ❌ | ❌ | như trên, việc đơn giản |

**Nếu đề án của bạn không phải hỏi đáp trên kho tài liệu MOC, hãy dùng `-plain`.**

Bản thường ép mọi câu trả lời phải trích dẫn một tài liệu trong kho MOC. Gửi một bài toán
phân loại, tóm tắt hay trích xuất vào đó thì model không có tài liệu nào để dẫn, và
guardrail chặn ở tầng grounding — **gần như mọi request**. Đó không phải lỗi của bạn, chỉ
là chọn sai profile.

Bản `-plain` **giữ nguyên** chặn prompt injection và che PII ở cả hai chiều. Nó chỉ bỏ
truy hồi và yêu cầu trích dẫn. Nói cách khác, bạn vẫn có đủ phần guardrail mà đề án của
bạn cần, chỉ bỏ phần vốn dành riêng cho copilot MOC.

```python
moc = MocCopilot(..., agent_id="da32", model="qwen2.5-7b-plain")
```

Key của đề án bạn được cấp cả bốn tên. Nếu gọi một tên không nằm trong danh sách, LiteLLM
trả về `This key can only access models=[...]` — đó là cách nhanh nhất để biết key cấp sai.

### Chọn giữa 7B và 1.5B

| | |
|---|---|
| `7b` | suy luận nhiều bước, câu hỏi mở, nội dung gửi tới người đọc |
| `1.5b` | phân loại, trích xuất, câu hỏi đóng có khuôn trả lời |

Đừng gửi cùng một workload cho cả hai rồi so sánh — chúng ở hai tầng khác nhau, không phải
hai phiên bản của cùng một thứ. Bản 1.5B cần cưỡng chế trích dẫn lúc decode mới giữ được
grounding, nên nó hợp việc tự động hơn là việc mà người đọc trực tiếp câu trả lời.

---

## 6. Khi gặp vấn đề

Mỗi phản hồi có header `X-Trace-Id`. Gửi kèm id đó khi báo lỗi — nó tra ngược được toàn bộ
đường đi của request, nhanh hơn mọi mô tả bằng lời.

```python
raw = client.chat.completions.with_raw_response.create(...)
trace = raw.headers.get("X-Trace-Id")
```
