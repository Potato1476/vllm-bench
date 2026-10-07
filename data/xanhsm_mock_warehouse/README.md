# Xanh SM Mock Data Warehouse

Kho dữ liệu giao dịch synthetic cho Data Analyst Copilot. Không có bản ghi nào đại diện cho khách hàng, tài xế, phương tiện, trạm sạc, chính sách hoặc kết quả kinh doanh thật của Xanh SM.

## Cách dùng

Mở trực tiếp SQLite:

```bash
sqlite3 data/xanhsm_mock_warehouse/xanhsm_mock_warehouse.sqlite
```

Hoặc nạp các file trong `csv/` vào Athena, Glue, Spark, PostgreSQL hay warehouse khác. Schema nằm tại `sql/schema.sql`; các view metric mẫu nằm tại `sql/views.sql`.

`sql/example_queries.sql` chứa các truy vấn mẫu cho booking, doanh thu, conversion, cancellation, tài xế, utilization, đội xe, sạc và retention. `quality_report.json` chứa kết quả kiểm tra integrity và orphan keys ở dạng máy đọc được.

### Hỏi bằng tiếng Việt qua chat

Sau khi build và triển khai guardrail image mới, dùng model grounded `qwen2.5-7b` qua luồng chat hiện tại. Ví dụ: **“Tổng số booking ở Hà Nội ngày 2026-01-01 là bao nhiêu?”** Hoặc gọi trực tiếp service:

```bash
kubectl -n llm-serving port-forward svc/guardrail 8080:8080
curl http://localhost:8080/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"qwen2.5-7b","messages":[{"role":"user","content":"Tổng số booking ở Hà Nội ngày 2026-01-01 là bao nhiêu?"}]}'
```

Phản hồi chứa số liệu, mã nguồn `[WAREHOUSE-QUERY-001]` và trường `warehouse.sql` để kiểm tra câu SQL đã chạy. Các chỉ số tổng hợp phổ biến, số tài xế online và thời gian chờ sạc dùng SQL đã định nghĩa trong dịch vụ; với câu hỏi khác, LLM đề xuất SQL. SQLite chỉ nhận một câu `SELECT` ở chế độ chỉ đọc, giới hạn thời gian và tối đa 20 dòng kết quả. Nếu SQL không hợp lệ, API trả lỗi `warehouse_query` thay vì suy đoán số liệu. Image guardrail tạo database từ generator có trong repo khi build; file SQLite lớn không được lưu trong Git.

## Mô hình dữ liệu

Dimensions: ngày, địa lý, dịch vụ, tài xế, khách hàng, phương tiện, trạm sạc và chiến dịch.

Facts: booking, trip, payment/refund, promotion, driver offer, trạng thái online, phiên sạc và snapshot đội xe.

`agg_daily_city_service.csv` là bảng tổng hợp kiểm tra nhanh, không thay thế fact tables.

## Phạm vi và đặc điểm synthetic

- Giai đoạn 240 ngày từ 2026-01-01.
- Ba thành phố demo: Hà Nội, TP.HCM và Đà Nẵng.
- Năm nhóm dịch vụ demo: TAXI, BIKE, EXPRESS, LUXURY và ENTERPRISE.
- Có seasonality theo giờ cao điểm, cuối tuần và các anomaly có chủ đích.
- TP.HCM có spike hủy chuyến giờ cao điểm trong một cửa sổ ngắn.
- Đà Nẵng có spike thời gian chờ sạc trong một cửa sổ ngắn.
- ID là mã giả lập; không có tên, số điện thoại, biển số hay tọa độ cá nhân.

Snapshot đội xe có grain một snapshot mỗi xe mỗi ngày để giữ kích thước demo thực dụng. Production có thể dùng snapshot 15 phút hoặc event sourcing.

## Tái tạo

```bash
python3 data/xanhsm_mock_warehouse/scripts/generate_warehouse.py
```

Generator dùng seed cố định nên có thể tái tạo cùng dataset.
