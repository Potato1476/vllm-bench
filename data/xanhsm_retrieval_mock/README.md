# Xanh SM Data Analyst Retrieval Mock Corpus

Bộ dữ liệu tiếng Việt dùng làm **knowledge corpus để retrieve** cho một Data Analyst Copilot trong bối cảnh Xanh SM. Sản phẩm chính là `corpus/retrieval_corpus.jsonl`, không phải tập câu hỏi benchmark. Đây là dữ liệu demo; không có chính sách hay số liệu synthetic nào đại diện cho dữ liệu nội bộ thật của Xanh SM.

## Thành phần

- `corpus/retrieval_corpus.jsonl`: file chính để embed/index, định dạng `id`, `text`, `metadata`.
- `corpus/documents.jsonl`: tài liệu nguồn cùng metadata và trạng thái phiên bản.
- `corpus/chunks.jsonl`: biểu diễn phẳng của từng đơn vị retrieval.
- `sources/public_sources.json`: manifest các nguồn công khai chính thức đã được tóm lược.
- `scripts/generate_dataset.py`: generator deterministic để tái tạo corpus.
- `manifest.json`: phiên bản, seed và số lượng bản ghi.
- `eval/retrieval_eval.jsonl`: tập kiểm tra tùy chọn để kiểm tra nhanh sau khi index; không phải corpus và không được nạp vào vector database.

## Loại nội dung

1. Metric catalog: định nghĩa KPI, công thức và điều kiện loại trừ.
2. Data dictionary: bảng, grain, trường dữ liệu và cảnh báo khi join.
3. Analysis playbook: cách điều tra biến động KPI và kiểm tra chất lượng dữ liệu.
4. Daily operations: báo cáo synthetic theo ngày và thành phố để tạo volume và kiểm thử temporal retrieval.
5. Public-source brief: bản tóm lược ngắn có URL nguồn, không phải bản sao trang web.
6. Hard negatives: tài liệu hết hiệu lực hoặc bản nháp để kiểm thử ưu tiên `status=active`.

## Nạp vào vector database

Embed trường `text` trong `corpus/retrieval_corpus.jsonl`. Lưu object `metadata` để có thể filter:

```json
{
  "status": "active",
  "language": "vi",
  "access_level": ["public", "internal-demo"]
}
```

Mặc định không cho tài liệu `deprecated` hoặc `draft` vào top-k, trừ khi câu hỏi yêu cầu lịch sử định nghĩa. Với câu hỏi theo ngày, lọc hoặc rerank bằng `effective_date`.

Không index bất kỳ file nào trong thư mục `eval/`.

## Kiểm tra tùy chọn

Từ `eval/retrieval_eval.jsonl`, đo tối thiểu:

- Document Recall@1, @3, @5.
- Chunk Recall@K.
- MRR theo document đầu tiên đúng.
- Tỷ lệ top-k chứa tài liệu `deprecated/draft` khi câu hỏi hỏi định nghĩa hiện hành.
- Citation precision: citation có nằm trong `must_cite` hay không.

Các câu loại `hybrid` và `reasoning` mới chỉ đánh giá bước truy xuất định nghĩa/schema/playbook. Để trả lời bằng con số, cần bổ sung mock warehouse và lớp sinh SQL.

## Tái tạo

```bash
python3 data/xanhsm_retrieval_mock/scripts/generate_dataset.py
```

Generator dùng seed cố định nên ID và dữ liệu không thay đổi giữa các lần chạy cùng phiên bản.

Chạy smoke test lexical không cần cài thêm thư viện:

```bash
python3 data/xanhsm_retrieval_mock/scripts/evaluate_lexical.py
```

Kết quả này chỉ là baseline kiểm tra dataset. Benchmark chính thức nên dùng đúng embedding model, vector database, filter và reranker của pipeline mục tiêu.
