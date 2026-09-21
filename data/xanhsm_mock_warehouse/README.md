# Xanh SM Mock Data Warehouse

Kho dữ liệu giao dịch synthetic cho Data Analyst Copilot. Không có bản ghi nào đại diện cho khách hàng, tài xế, phương tiện, trạm sạc, chính sách hoặc kết quả kinh doanh thật của Xanh SM.

## Cách dùng

Mở trực tiếp SQLite:

```bash
sqlite3 data/xanhsm_mock_warehouse/xanhsm_mock_warehouse.sqlite
```

Hoặc nạp các file trong `csv/` vào Athena, Glue, Spark, PostgreSQL hay warehouse khác. Schema nằm tại `sql/schema.sql`; các view metric mẫu nằm tại `sql/views.sql`.

`sql/example_queries.sql` chứa các truy vấn mẫu cho booking, doanh thu, conversion, cancellation, tài xế, utilization, đội xe, sạc và retention. `quality_report.json` chứa kết quả kiểm tra integrity và orphan keys ở dạng máy đọc được.

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
