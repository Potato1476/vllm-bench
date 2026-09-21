#!/usr/bin/env python3
"""Generate a deterministic Vietnamese mock retrieval corpus for a Xanh SM DA copilot.

All internal policies and operational figures are synthetic. Public-source records are
short paraphrased briefs with provenance URLs, not copies of the source pages.
"""

from __future__ import annotations

import hashlib
import csv
import json
import math
import random
from datetime import date, timedelta
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CORPUS_DIR = ROOT / "corpus"
EVAL_DIR = ROOT / "eval"
SOURCES_DIR = ROOT / "sources"
SEED = 20260921
random.seed(SEED)


def stable_id(prefix: str, value: str) -> str:
    return f"{prefix}_{hashlib.sha1(value.encode('utf-8')).hexdigest()[:12]}"


def write_jsonl(path: Path, rows: list[dict]) -> None:
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")


def doc(
    doc_id: str,
    title: str,
    category: str,
    content: str,
    *,
    source_type: str = "synthetic",
    status: str = "active",
    version: str = "1.0",
    effective_date: str = "2026-01-01",
    source_url: str | None = None,
    tags: list[str] | None = None,
    access_level: str = "internal-demo",
) -> dict:
    return {
        "document_id": doc_id,
        "title": title,
        "category": category,
        "content": content.strip(),
        "language": "vi",
        "source_type": source_type,
        "status": status,
        "version": version,
        "effective_date": effective_date,
        "source_url": source_url,
        "retrieved_at": "2026-09-21" if source_type == "public-summary" else None,
        "access_level": access_level,
        "organization_context": "Xanh SM - mock/demo",
        "is_official_internal_data": False,
        "tags": tags or [],
    }


CORE_DOCS = [
    doc("METRIC-TRIP-001", "Định nghĩa chuyến hoàn thành", "metric-catalog", """
Trong bộ dữ liệu demo, một booking được tính là chuyến hoàn thành khi trip_status = COMPLETED, có completed_at hợp lệ, quãng đường thực hiện lớn hơn 0,2 km và không bị đánh dấu test hoặc fraud_confirmed. Chuyến đã nhận tài xế nhưng khách không lên xe không phải chuyến hoàn thành. Chuyến bị hoàn tiền sau đó vẫn giữ trạng thái vận hành COMPLETED nhưng doanh thu thuần được điều chỉnh ở fact_payments. Công thức completed_trips = count distinct trip_id thỏa toàn bộ điều kiện trên.
""", tags=["trip", "completed", "kpi"]),
    doc("METRIC-REV-001", "Gross Booking Value và doanh thu thuần", "metric-catalog", """
GBV là tổng final_fare của các chuyến hoàn thành trước khuyến mại, hoàn tiền và điều chỉnh. Net Revenue trong demo = final_fare + surcharge - customer_discount - refund_amount - tax_excluded_adjustment. Khoản tài xế được hưởng không được trừ khi tính GBV nhưng được hạch toán riêng khi phân tích contribution margin. Không dùng trường quoted_fare để báo cáo doanh thu thực tế.
""", tags=["revenue", "gbv", "finance"]),
    doc("METRIC-CANCEL-001", "Cancellation Rate và phân loại hủy", "metric-catalog", """
Booking Cancellation Rate = số booking có trạng thái CANCELLED chia cho số booking hợp lệ đã tạo. Post-match Cancellation Rate chỉ dùng các booking đã có driver_id. cancel_actor nhận CUSTOMER, DRIVER hoặc SYSTEM. Nhóm lý do chuẩn gồm customer_changed_mind, wait_too_long, driver_requested_cancel, vehicle_issue, no_driver_available và payment_failure. Booking test, duplicate hoặc invalid bị loại khỏi mẫu số.
""", tags=["cancellation", "booking", "kpi"]),
    doc("METRIC-DRIVER-ACTIVE-001", "Định nghĩa tài xế hoạt động", "metric-catalog", """
Active Driver Daily là tài xế có ít nhất 30 phút online hợp lệ hoặc hoàn thành ít nhất một chuyến trong ngày địa phương. Active Driver Monthly là tài xế có ít nhất một ngày active trong tháng. Tài khoản đào tạo, test, suspended toàn ngày và tài xế chưa kích hoạt bị loại. Thành phố của tài xế được gán theo phần lớn online_minutes trong ngày.
""", tags=["driver", "active", "kpi"]),
    doc("METRIC-UTIL-001", "Online Hours, Busy Hours và Utilization", "metric-catalog", """
Online Hours là tổng thời gian tài xế ở trạng thái AVAILABLE, ASSIGNED, PICKUP hoặc ON_TRIP. Busy Hours gồm ASSIGNED, PICKUP và ON_TRIP; không gồm BREAK, OFFLINE hoặc CHARGING. Driver Utilization Rate = busy_minutes / online_minutes. Fleet Utilization Rate là số xe có chuyến hoàn thành chia số xe khả dụng trong cửa sổ báo cáo. Hai loại utilization không được dùng thay thế nhau.
""", tags=["driver", "online", "utilization"]),
    doc("METRIC-ACCEPT-001", "Acceptance Rate của tài xế", "metric-catalog", """
Acceptance Rate = accepted_offers / eligible_offers. Eligible offer là đề nghị chuyến gửi thành công, tài xế đang AVAILABLE và không bị hệ thống thu hồi trong 10 giây đầu. Offer trùng, lỗi ứng dụng hoặc ngoài vùng phục vụ bị loại. Chỉ so sánh giữa các dịch vụ và khung giờ khi áp cùng phiên bản quy tắc.
""", tags=["driver", "acceptance", "offer"]),
    doc("METRIC-CUSTOMER-001", "Phân nhóm vòng đời khách hàng", "metric-catalog", """
New Customer là khách có chuyến hoàn thành đầu tiên trong kỳ. Returning Customer đã có chuyến hoàn thành trước kỳ và có thêm chuyến trong kỳ. Reactivated Customer có chuyến trong kỳ sau ít nhất 60 ngày không hoàn thành chuyến. Retained-30 là khách quay lại có ít nhất một chuyến trong 30 ngày sau chuyến đầu. Không dùng booking bị hủy để xác định vòng đời.
""", tags=["customer", "retention", "segmentation"]),
    doc("METRIC-PROMO-001", "Hạch toán voucher và khuyến mại", "metric-catalog", """
customer_discount là phần giảm trực tiếp cho khách. funded_by_company và funded_by_partner phải được tách riêng. Net Revenue chỉ trừ phần công ty tài trợ; phần đối tác tài trợ được ghi receivable. Promo Cost = company_funded_discount + campaign_fee. Một booking có nhiều voucher chỉ được tính một lần theo booking_id khi tổng hợp chuyến.
""", tags=["voucher", "promotion", "revenue"]),
    doc("METRIC-GEO-001", "Quy tắc gán địa lý cho KPI", "metric-catalog", """
KPI nhu cầu, booking và chuyến được gán theo pickup_zone_id. KPI điểm đến dùng dropoff_zone_id và phải ghi rõ trong tên báo cáo. KPI tài xế dùng operating_city_id suy ra từ phần lớn online_minutes. Nếu điểm GPS nằm ngoài polygon, dùng geocoding fallback; bản ghi không xác định được đưa vào UNKNOWN, không tự gán cho thành phố gần nhất.
""", tags=["location", "city", "zone"]),
    doc("METRIC-CONVERSION-001", "Booking Conversion và funnel", "metric-catalog", """
Funnel chuẩn gồm booking_created, driver_matched, driver_arrived, trip_started và trip_completed. Booking-to-completed Conversion = số booking hợp lệ có chuyến hoàn thành / số booking hợp lệ được tạo. Mỗi booking_id chỉ xuất hiện một lần trong từng bước. Khi phân tích rơi rụng, ưu tiên bước xa nhất mà booking đã đạt được.
""", tags=["booking", "conversion", "funnel"]),
    doc("METRIC-WAIT-001", "Thời gian ghép tài xế và đón khách", "metric-catalog", """
Match Time = matched_at - booking_created_at. Pickup ETA Actual = trip_started_at - matched_at. Customer Wait Time = trip_started_at - booking_created_at. Báo cáo mặc định dùng median và p90 bên cạnh trung bình để tránh ngoại lệ. Các khoảng âm, lớn hơn 180 phút hoặc thiếu timestamp bị gắn data_quality_flag.
""", tags=["wait-time", "eta", "operations"]),
    doc("METRIC-SUPPLY-001", "Chỉ số cân bằng cung cầu", "metric-catalog", """
Demand là booking hợp lệ được tạo trong mỗi zone và time bucket 15 phút. Available Supply là số tài xế AVAILABLE ít nhất 5 phút trong bucket. Demand Supply Ratio = valid_bookings / available_drivers; nếu supply bằng 0 thì gắn cờ no_supply thay vì chia. High-demand-low-supply được xác định khi tỷ lệ vượt 2,0 và có ít nhất 20 booking.
""", tags=["supply", "demand", "zone"]),
    doc("METRIC-FLEET-001", "Trạng thái và mức sử dụng đội xe", "metric-catalog", """
Trạng thái xe tại một thời điểm gồm AVAILABLE, IN_SERVICE, CHARGING, MAINTENANCE, RESERVED hoặc INACTIVE. Available Fleet loại xe bảo dưỡng, không giấy tờ hợp lệ và pin dưới ngưỡng điều phối 15%. Fleet Utilization = số xe có ít nhất một chuyến hoàn thành / số xe khả dụng trong ngày. Không cộng các snapshot trạng thái để suy ra số xe duy nhất.
""", tags=["fleet", "vehicle", "utilization"]),
    doc("METRIC-CHARGE-001", "Định nghĩa phiên sạc và thời gian chờ", "metric-catalog", """
Charging Session bắt đầu khi kết nối trụ được xác nhận và kết thúc khi ngắt kết nối. Queue Time = charge_start_at - station_arrival_at. Charging Time = charge_end_at - charge_start_at. Theo quy tắc demo, trạng thái CHARGING không được tính vào Driver Online Hours; thời gian di chuyển có điều phối tới trạm được lưu riêng. Phiên thiếu end time là open session và không dùng trong trung bình hoàn tất.
""", tags=["charging", "ev", "queue"]),
    doc("METRIC-RETENTION-001", "Retention và churn tài xế", "metric-catalog", """
Driver 30-day Retention = tài xế mới có ít nhất một ngày active trong cửa sổ ngày 24 đến 30 sau activation_date chia tổng tài xế đủ thời gian quan sát. Driver Churn Proxy là tài xế từng active nhưng không active 28 ngày liên tiếp. Đây là chỉ báo vận hành, không phải trạng thái chấm dứt hợp đồng.
""", tags=["driver", "retention", "churn"]),
    doc("POLICY-ANALYSIS-001", "Quy trình điều tra biến động KPI", "analysis-playbook", """
Khi KPI biến động, analyst phải: xác nhận phiên bản định nghĩa; kiểm tra completeness và freshness; phân rã theo thành phố, dịch vụ, zone và giờ; so sánh cùng thứ trong tuần; kiểm tra booking, supply, cancellation và payment; sau đó mới nêu giả thuyết. Báo cáo phải tách quan sát, bằng chứng và suy luận. Không khẳng định quan hệ nhân quả chỉ từ tương quan.
""", tags=["reasoning", "root-cause", "playbook"]),
    doc("POLICY-PRIVACY-001", "Chính sách dữ liệu demo cho Data Analyst Copilot", "data-governance", """
Copilot chỉ được trả dữ liệu tổng hợp. Không hiển thị số điện thoại, họ tên, tọa độ chính xác, biển số hoặc lịch sử chuyến của một cá nhân. Nhóm dưới 10 khách hàng hoặc tài xế phải được làm mờ hay gộp. ID trong dataset đều giả lập. Khi người dùng yêu cầu PII, hệ thống từ chối và gợi ý báo cáo tổng hợp phù hợp.
""", tags=["privacy", "pii", "guardrail"]),
]


SCHEMA_DOCS = [
    ("SCHEMA-BOOKING-001", "fact_bookings", "Một dòng trên booking_id. Trường chính: booking_created_at, customer_id, service_id, pickup_zone_id, dropoff_zone_id, booking_status, cancel_actor, cancel_reason, matched_at, driver_id, is_test, is_duplicate. Dùng cho demand, funnel, matching và cancellation."),
    ("SCHEMA-TRIP-001", "fact_trips", "Một dòng trên trip_id, liên kết booking_id. Trường chính: trip_started_at, completed_at, trip_status, distance_km, duration_minutes, driver_id, vehicle_id, final_fare, surcharge, fraud_confirmed. Dùng cho completed trips, GBV, thời gian và quãng đường."),
    ("SCHEMA-PAYMENT-001", "fact_payments", "Một dòng trên payment transaction. Trường: trip_id, payment_status, gross_amount, customer_discount, company_funded_discount, partner_funded_discount, refund_amount, tax_excluded_adjustment, paid_at. Một trip có thể có nhiều giao dịch; cần tổng hợp theo trip trước khi join."),
    ("SCHEMA-ONLINE-001", "fact_driver_online_sessions", "Một dòng trên đoạn trạng thái tài xế. Trường: driver_id, state, start_at, end_at, city_id, zone_id. State gồm AVAILABLE, ASSIGNED, PICKUP, ON_TRIP, CHARGING, BREAK và OFFLINE. Cắt session qua ranh giới ngày trước khi tổng hợp."),
    ("SCHEMA-OFFER-001", "fact_driver_offers", "Một dòng trên offer_id. Trường: booking_id, driver_id, sent_at, responded_at, response, recalled_within_10s, delivery_error, service_id. Dùng tính eligible offers và acceptance rate."),
    ("SCHEMA-CHARGE-001", "fact_charging_sessions", "Một dòng trên phiên sạc. Trường: vehicle_id, driver_id, station_id, station_arrival_at, charge_start_at, charge_end_at, energy_kwh, start_soc, end_soc, session_status. Dùng tính queue time, charging time và mức sử dụng trạm."),
    ("SCHEMA-FLEET-001", "fact_vehicle_status", "Snapshot trạng thái xe mỗi 15 phút. Trường: snapshot_at, vehicle_id, city_id, state, battery_soc, document_valid, service_id. Khi đếm xe phải count distinct vehicle_id trong snapshot đã chọn."),
    ("SCHEMA-DRIVER-001", "dim_driver", "Một dòng trên driver_id giả lập. Trường: activation_date, contract_type, vehicle_type, home_city_id, suspended_from, suspended_to, is_test_account. Không chứa tên hoặc số điện thoại."),
    ("SCHEMA-CUSTOMER-001", "dim_customer", "Một dòng trên customer_id đã băm. Trường: signup_date, first_completed_trip_at, acquisition_channel, consent_analytics, deleted_at. Không chứa PII trực tiếp."),
    ("SCHEMA-SERVICE-001", "dim_service", "Danh mục dịch vụ demo: TAXI, LUXURY, BIKE, EXPRESS và ENTERPRISE. Trường: service_id, service_group, vehicle_type, effective_from, effective_to, active_flag."),
    ("SCHEMA-LOCATION-001", "dim_location", "Danh mục city_id, district_id và zone_id. Polygon được version hóa bằng geo_version. Báo cáo phải lưu geo_version để tái lập kết quả khi ranh giới zone thay đổi."),
    ("SCHEMA-PROMO-001", "fact_promotions", "Một dòng trên booking-voucher. Trường: booking_id, campaign_id, voucher_code_hash, discount_amount, funded_by_company, funded_by_partner, applied_at. Cần tránh nhân bản trip khi join nhiều voucher."),
]

for schema_id, table_name, body in SCHEMA_DOCS:
    CORE_DOCS.append(doc(schema_id, f"Data dictionary: {table_name}", "data-dictionary", body, tags=["schema", table_name]))


PUBLIC_SOURCES = [
    {
        "source_id": "PUB-XSM-EXPRESS",
        "title": "Green Express - trang đặt dịch vụ",
        "url": "https://booking.xanhsm.com/",
        "summary": "Trang công khai xác nhận Green Express là dịch vụ giao hàng và luồng đăng nhập có bước đồng ý điều khoản, xử lý dữ liệu cá nhân. Dataset chỉ dùng thông tin phân loại dịch vụ; không thu thập dữ liệu người dùng.",
        "tags": ["express", "public", "service"],
    },
    {
        "source_id": "PUB-XSM-ENTERPRISE",
        "title": "Green SM Enterprise - đăng ký tư vấn",
        "url": "https://business.xanhsm.com/lead",
        "summary": "Trang công khai mô tả giải pháp giao thông xanh cho doanh nghiệp, nhấn mạnh tối ưu chi phí vận hành, đáp ứng nhu cầu di chuyển, tài xế sẵn sàng và hệ thống quản lý.",
        "tags": ["enterprise", "public", "service"],
    },
    {
        "source_id": "PUB-XSM-DRIVER",
        "title": "Trang tuyển dụng tài xế Green SM",
        "url": "https://tuyentaixe.xanhsm.com/gioithieu",
        "summary": "Trang công khai cho thấy hệ sinh thái có nhóm tài xế taxi ô tô điện và xe máy điện. Các con số thu nhập hoặc ưu đãi trên trang mang tính thời điểm và không được dùng làm chính sách nội bộ trong corpus mock.",
        "tags": ["driver", "taxi", "bike", "public"],
    },
    {
        "source_id": "PUB-XSM-PLATFORM",
        "title": "Thông tin nền tảng dành cho tài xế",
        "url": "https://tuyentaixe.xanhsm.com/kolkoc",
        "summary": "Nguồn công khai mô tả mô hình nền tảng cho tài xế xe máy điện và hoạt động vận doanh. Corpus chỉ dùng để tạo taxonomy đối tác/nội bộ; không coi tỷ lệ chia sẻ hoặc ưu đãi công khai là quy tắc cố định.",
        "tags": ["driver", "platform", "public"],
    },
    {
        "source_id": "PUB-XSM-PROMO",
        "title": "Ví dụ chương trình ưu đãi công khai đã hết hạn",
        "url": "https://www.xanhsm.com/news/den-golden-gate-khai-tiec-no-ne-goi-xanh-sm-don-phu-phe-uu-dai",
        "summary": "Bài công khai năm 2024 minh họa chương trình ưu đãi có phạm vi dịch vụ, mức giảm, trần giảm, số lượt và thời hạn. Nguồn chỉ dùng để thiết kế schema promotion; mọi ưu đãi nêu tại đây được đánh dấu historical/expired.",
        "tags": ["promotion", "historical", "public"],
    },
    {
        "source_id": "PUB-XSM-DRIVER-POLICY",
        "title": "Ví dụ Q&A chính sách thu nhập tài xế công khai",
        "url": "https://cdn.xanhsm.com/2026/04/0cbee2eb-qa-chinh-sach-thu-nhap-tai-xe-bike-20.04.2026.pdf",
        "summary": "Tài liệu công khai dạng hỏi đáp minh họa rằng chính sách tài xế được version hóa theo thời gian, dịch vụ và điều kiện vận doanh. Dataset không sao chép mức chia sẻ; nguồn được dùng để tạo bài test ưu tiên phiên bản tài liệu hiện hành.",
        "tags": ["driver", "policy", "versioning", "public"],
    },
]


def service_catalog_doc() -> dict:
    return doc("CATALOG-SERVICE-001", "Danh mục dịch vụ demo", "service-catalog", """
Corpus demo sử dụng năm nhóm dịch vụ: TAXI cho chuyến ô tô phổ thông; LUXURY cho ô tô phân khúc cao hơn; BIKE cho chuyến xe máy; EXPRESS cho giao nhận; ENTERPRISE cho nhu cầu doanh nghiệp. Đây là taxonomy phân tích giả lập, không phải danh mục thương mại hiện hành. Mỗi báo cáo phải lọc theo effective_from/effective_to trong dim_service.
""", tags=["service", "taxonomy"])


def deprecated_docs() -> list[dict]:
    return [
        doc("OLD-METRIC-ACTIVE-2025", "[HẾT HIỆU LỰC] Active Driver theo đăng nhập", "deprecated-definition", "Phiên bản cũ từng tính tài xế active chỉ cần mở ứng dụng một lần trong ngày. Định nghĩa này hết hiệu lực từ 2026-01-01 và không được dùng cho báo cáo hiện hành.", status="deprecated", version="0.8", effective_date="2025-01-01", tags=["hard-negative", "driver"]),
        doc("OLD-METRIC-CANCEL-2025", "[HẾT HIỆU LỰC] Cancellation Rate chỉ sau ghép", "deprecated-definition", "Phiên bản cũ gọi Cancellation Rate là số hủy sau ghép chia số booking đã ghép. Hiện metric này được đổi tên thành Post-match Cancellation Rate; không dùng thay Booking Cancellation Rate.", status="deprecated", version="0.9", effective_date="2025-06-01", tags=["hard-negative", "cancellation"]),
        doc("DRAFT-UTIL-002", "[BẢN NHÁP] Utilization gồm thời gian sạc", "draft-definition", "Bản nháp đề xuất cộng CHARGING vào online_minutes nhưng chưa được phê duyệt. Hệ thống retrieval phải ưu tiên METRIC-UTIL-001 và METRIC-CHARGE-001 có trạng thái active.", status="draft", version="draft-2", effective_date="2026-07-01", tags=["hard-negative", "utilization"]),
        doc("OLD-GEO-001", "[HẾT HIỆU LỰC] Gán thành phố theo điểm trả", "deprecated-definition", "Quy tắc thử nghiệm cũ gán mọi KPI chuyến theo dropoff city. Quy tắc hiện hành gán booking và chuyến theo pickup_zone_id, trừ báo cáo điểm đến ghi rõ mục đích.", status="deprecated", version="0.7", effective_date="2025-03-01", tags=["hard-negative", "geo"]),
    ]


def generate_daily_reports() -> list[dict]:
    warehouse_agg = ROOT.parent / "xanhsm_mock_warehouse" / "csv" / "agg_daily_city_service.csv"
    if warehouse_agg.exists():
        grouped: dict[tuple[str, str, str], list[dict]] = {}
        with warehouse_agg.open(encoding="utf-8", newline="") as handle:
            for record in csv.DictReader(handle):
                key = (record["calendar_date"], record["city_id"], record["city_name"])
                grouped.setdefault(key, []).append(record)
        aligned = []
        for (calendar_date, city_id, city_name), records in sorted(grouped.items()):
            total_bookings = sum(int(r["valid_bookings"]) for r in records)
            total_completed = sum(int(r["completed_trips"]) for r in records)
            total_cancelled = sum(int(r["cancelled_bookings"]) for r in records)
            total_gbv = sum(int(r["gbv_vnd"]) for r in records)
            total_net = sum(int(r["net_revenue_vnd"]) for r in records)
            service_lines = [
                f"{r['service_id']}: bookings={r['valid_bookings']}; completed={r['completed_trips']}; "
                f"cancelled={r['cancelled_bookings']}; gbv_vnd={r['gbv_vnd']}; "
                f"net_revenue_vnd={r['net_revenue_vnd']}; active_drivers={r['active_drivers']}"
                for r in records
            ]
            content = (
                f"Báo cáo vận hành giả lập ngày {calendar_date} tại {city_name}. "
                f"Nguồn: agg_daily_city_service của mock warehouse, seed={SEED}. "
                f"Tổng valid_bookings={total_bookings}; completed_trips={total_completed}; "
                f"cancelled_bookings={total_cancelled}; gbv_vnd={total_gbv}; net_revenue_vnd={total_net}. "
                "Chi tiết theo dịch vụ: " + " | ".join(service_lines) + ". "
                "Mọi con số là synthetic và không đại diện cho kết quả kinh doanh thật."
            )
            aligned.append(doc(
                f"OPS-{calendar_date}-{city_id}",
                f"Báo cáo ngày {calendar_date} - {city_name}",
                "daily-operations",
                content,
                effective_date=calendar_date,
                tags=["daily", city_id, "synthetic-kpi", "warehouse-aligned"],
            ))
        return aligned

    rows = []
    cities = {
        "HAN": {"name": "Hà Nội", "base": 12500, "fare": 92000},
        "HCM": {"name": "TP.HCM", "base": 14800, "fare": 88000},
    }
    services = {
        "TAXI": (0.48, 0.79),
        "BIKE": (0.34, 0.83),
        "EXPRESS": (0.12, 0.76),
        "LUXURY": (0.06, 0.86),
    }
    start = date(2026, 1, 1)
    for day_index in range(240):
        current = start + timedelta(days=day_index)
        weekday_factor = 1.12 if current.weekday() in (4, 5, 6) else 1.0
        seasonal = 1 + 0.08 * math.sin(day_index / 17)
        for city_code, city in cities.items():
            total = int(city["base"] * weekday_factor * seasonal * random.uniform(0.94, 1.06))
            service_lines = []
            city_completed = 0
            city_gbv = 0
            city_cancelled = 0
            for service, (share, conversion) in services.items():
                bookings = max(20, int(total * share * random.uniform(0.96, 1.04)))
                completed = int(bookings * conversion * random.uniform(0.97, 1.03))
                cancelled = int(bookings * random.uniform(0.075, 0.14))
                avg_fare_factor = {"TAXI": 1.0, "BIKE": 0.34, "EXPRESS": 0.44, "LUXURY": 1.55}[service]
                avg_fare = int(city["fare"] * avg_fare_factor * random.uniform(0.94, 1.08) / 1000) * 1000
                gbv = completed * avg_fare
                city_completed += completed
                city_cancelled += cancelled
                city_gbv += gbv
                service_lines.append(f"{service}: bookings={bookings}; completed={completed}; cancelled={cancelled}; avg_fare_vnd={avg_fare}; gbv_vnd={gbv}")
            active_drivers = int(city_completed / random.uniform(7.2, 9.6))
            supply_ratio = round(total / max(active_drivers, 1), 2)
            report_id = f"OPS-{current.isoformat()}-{city_code}"
            content = (
                f"Báo cáo vận hành giả lập ngày {current.isoformat()} tại {city['name']}. "
                f"Mọi con số là synthetic. Tổng booking={total}; completed={city_completed}; "
                f"cancelled={city_cancelled}; GBV={city_gbv} VND; active_drivers={active_drivers}; "
                f"bookings_per_active_driver={supply_ratio}. Chi tiết theo dịch vụ: " + " | ".join(service_lines) + ". "
                "Số liệu tuân theo metric catalog phiên bản 1.0; không dùng như dữ liệu kinh doanh thật."
            )
            rows.append(doc(report_id, f"Báo cáo ngày {current.isoformat()} - {city['name']}", "daily-operations", content, effective_date=current.isoformat(), tags=["daily", city_code, "synthetic-kpi"]))
    return rows


def generate_playbooks() -> list[dict]:
    playbooks = [
        ("PLAYBOOK-REV-001", "Phân tích biến động doanh thu", "Xác nhận Net Revenue hay GBV; so sánh cùng thứ; phân rã completed trips và average fare; sau đó kiểm tra service mix, discount, refund và surcharge. Join fact_trips với payment đã aggregate theo trip_id."),
        ("PLAYBOOK-CANCEL-001", "Phân tích tăng hủy chuyến", "Phân rã theo pre-match/post-match, cancel_actor, cancel_reason, zone và time bucket. Kiểm tra match time, pickup wait và supply. Không gộp no_driver_available với tài xế chủ động hủy."),
        ("PLAYBOOK-SUPPLY-001", "Phân tích thiếu cung", "Dùng bucket 15 phút theo pickup zone. So sánh valid bookings với available drivers; kiểm tra no_supply, p90 match time và booking conversion. Chỉ gắn nhãn hotspot khi đạt ngưỡng volume tối thiểu."),
        ("PLAYBOOK-DRIVER-001", "Phân tích hiệu suất tài xế", "Tách active drivers, online hours, busy hours, trips per driver và acceptance rate. So sánh trong cùng thành phố, dịch vụ và ca. Không xếp hạng cá nhân; chỉ báo cáo cohort đủ lớn."),
        ("PLAYBOOK-FLEET-001", "Phân tích đội xe điện", "Chọn snapshot cùng thời điểm; đếm distinct vehicle_id theo state; kết hợp trip, charging và maintenance. Phân biệt driver utilization với fleet utilization."),
        ("PLAYBOOK-RETENTION-001", "Phân tích giữ chân khách hàng", "Dùng cohort theo first_completed_trip_at; đo quay lại trong cửa sổ cố định; loại booking hủy. Phân rã acquisition channel và service đầu tiên nhưng tuân thủ ngưỡng riêng tư."),
        ("PLAYBOOK-DQ-001", "Kiểm tra chất lượng dữ liệu trước phân tích", "Kiểm tra freshness, row count, duplicate primary key, null timestamp, quan hệ booking-trip-payment, timezone và data_quality_flag. Nếu dữ liệu chưa hoàn tất, trả trạng thái chưa đủ chứng cứ thay vì suy đoán."),
        ("PLAYBOOK-CITATION-001", "Quy tắc trích dẫn của copilot", "Câu trả lời định nghĩa phải trích metric catalog active. Câu trả lời schema phải trích data dictionary. Số liệu ngày phải trích đúng báo cáo có effective_date. Không trích tài liệu deprecated/draft làm nguồn chính."),
    ]
    return [doc(pid, title, "analysis-playbook", body, tags=["playbook"]) for pid, title, body in playbooks]


def generate_handbook_articles() -> list[dict]:
    """Focused knowledge articles intended to be retrieved as answer evidence."""
    articles = [
        ("KB-BOOKING-LIFECYCLE", "Vòng đời booking và thứ tự trạng thái", "booking", "Booking hợp lệ đi qua CREATED, SEARCHING, MATCHED, DRIVER_ARRIVED, ON_TRIP và COMPLETED. CANCELLED có thể xảy ra trước hoặc sau MATCHED. Khi event đến sai thứ tự, bảng fact dùng trạng thái có event_time mới nhất nhưng lưu cờ out_of_order_event để kiểm tra. Không suy ra COMPLETED chỉ từ việc có payment."),
        ("KB-STATUS-PRECEDENCE", "Quy tắc ưu tiên trạng thái chuyến", "trip", "Nếu cùng trip_id có nhiều event, COMPLETED chỉ hợp lệ khi có trip_started_at và completed_at. FRAUD_CONFIRMED không xóa trạng thái vận hành nhưng loại chuyến khỏi KPI chuẩn. REFUNDED là trạng thái tài chính, không ghi đè trip_status. Báo cáo luôn nêu rõ bộ lọc fraud và test."),
        ("KB-TIMEZONE", "Timezone và ranh giới ngày báo cáo", "data-quality", "Tất cả timestamp kho dữ liệu lưu UTC; ngày kinh doanh được quy đổi sang Asia/Ho_Chi_Minh trước khi group. Session online, chuyến hoặc phiên sạc đi qua 00:00 phải được cắt theo ngày địa phương. Không dùng DATE(timestamp_utc) trực tiếp cho dashboard Việt Nam."),
        ("KB-LATE-DATA", "Dữ liệu đến muộn và thời điểm chốt số", "data-quality", "Báo cáo D-1 được xem là sơ bộ lúc 08:00 và chốt lại lúc 12:00 ngày kế tiếp. Payment, refund và trip event đến muộn có thể làm thay đổi Net Revenue. Mọi câu trả lời số liệu phải kèm data_as_of; nếu freshness vượt SLA thì cảnh báo chưa đủ dữ liệu."),
        ("KB-DEDUP", "Loại booking trùng và tài khoản test", "data-quality", "Booking có is_duplicate=true hoặc is_test=true bị loại khỏi funnel. Nếu cùng id xuất hiện nhiều bản ghi do CDC, giữ record có source_updated_at mới nhất. Tài khoản test được nhận diện ở cả booking và dim_driver; chỉ lọc một phía có thể làm sai mẫu số."),
        ("KB-JOIN-BOOKING-TRIP", "Join booking với trip an toàn", "sql-guide", "Quan hệ chuẩn là fact_bookings.booking_id = fact_trips.booking_id. Một booking hợp lệ có tối đa một trip vận hành chính trong bản mock. Aggregate payment và promotion về một dòng trên trip hoặc booking trước khi join để tránh nhân doanh thu và số chuyến."),
        ("KB-JOIN-PAYMENT", "Join payment và xử lý nhiều giao dịch", "sql-guide", "Một trip có thể có charge, retry, refund và adjustment. Chỉ lấy giao dịch thành công rồi aggregate gross, discount và refund theo trip_id. Không count dòng payment như số chuyến. Net Revenue dùng quy tắc METRIC-REV-001 sau bước aggregate."),
        ("KB-REFUND", "Phân tích hoàn tiền", "finance", "Refund có thể phát sinh sau ngày hoàn thành chuyến. Báo cáo theo trip date phản ánh hiệu quả vận hành; báo cáo theo accounting date phản ánh dòng điều chỉnh tài chính. Hai cách nhìn phải được ghi nhãn, không trộn trong cùng chuỗi thời gian."),
        ("KB-PROMO-EFFECT", "Đánh giá hiệu quả khuyến mại", "promotion", "So sánh cohort đủ tương đồng và tách company-funded khỏi partner-funded. Theo dõi incremental completed trips, Net Revenue sau chi phí, repeat rate và cannibalization. Không kết luận hiệu quả chỉ từ số lượt dùng voucher. Chương trình hết hạn phải được lọc theo effective dates."),
        ("KB-GEO-VERSION", "Version hóa zone và bản đồ", "location", "Mỗi fact giữ zone_id tại thời điểm event và geo_version. Khi ranh giới thay đổi, báo cáo lịch sử mặc định giữ phiên bản cũ để tái lập. Muốn restate theo bản đồ mới phải chạy phép ánh xạ riêng và ghi rõ restated=true."),
        ("KB-UNKNOWN-ZONE", "Xử lý điểm đón ngoài vùng hoặc không xác định", "location", "GPS không khớp polygon sau geocoding fallback được gán UNKNOWN. UNKNOWN phải xuất hiện thành một nhóm trong kiểm tra chất lượng dữ liệu; không âm thầm bỏ khỏi mẫu số và không tự gán vào zone gần nhất."),
        ("KB-DRIVER-COHORT", "Cohort tài xế mới và retention", "driver", "Cohort theo activation_date địa phương. Chỉ đưa tài xế đủ 30 ngày quan sát vào mẫu số retention-30. So sánh cohort theo contract_type, vehicle_type và city nhưng không hiển thị nhóm dưới 10 người."),
        ("KB-DRIVER-UTIL", "Phân tích utilization tài xế", "driver", "Utilization cao không luôn tốt: có thể phản ánh thiếu cung và thời gian chờ khách tăng. Đọc cùng trips per online hour, p90 pickup time, cancellation và demand-supply ratio. Loại CHARGING, BREAK và OFFLINE khỏi online minutes theo định nghĩa hiện hành."),
        ("KB-DRIVER-ACCEPT", "Diễn giải Acceptance Rate", "driver", "Acceptance Rate thấp có thể liên quan loại dịch vụ, quãng đường đón, zone, giờ hoặc lỗi gửi offer. Trước khi đánh giá cohort, loại offer không eligible và đặt ngưỡng volume tối thiểu 30 offer. Không dùng metric để xếp hạng cá nhân trong copilot."),
        ("KB-FLEET-SNAPSHOT", "Đếm đội xe từ snapshot", "fleet", "Chọn snapshot gần nhất nhưng không muộn hơn as_of_time. Count distinct vehicle_id theo trạng thái. Không cộng số xe của nhiều snapshot vì một xe sẽ bị đếm lặp. Khi snapshot trễ quá 30 phút, đánh dấu fleet data stale."),
        ("KB-FLEET-AVAIL", "Xe khả dụng và ngưỡng pin điều phối", "fleet", "Xe chỉ được tính AVAILABLE nếu state phù hợp, giấy tờ còn hiệu lực, không có maintenance block và battery_soc từ 15% trở lên. Ngưỡng 15% là giả lập phục vụ demo. Báo cáo phải trích phiên bản quy tắc nếu thay đổi ngưỡng."),
        ("KB-CHARGE-QUEUE", "Phân tích hàng chờ trạm sạc", "charging", "Queue Time đo từ station_arrival_at đến charge_start_at. Báo median, p90 và tỷ lệ phiên trên 20 phút theo station và giờ. Loại open session khỏi thời gian hoàn tất nhưng báo riêng số phiên mở để tránh che lỗi dữ liệu."),
        ("KB-CHARGE-IMPACT", "Đánh giá tác động vận hành của sạc", "charging", "Ghép availability và charging theo city-zone-time bucket. Kiểm tra queue time, số xe CHARGING, available fleet, demand-supply ratio và completion rate. Đây là phân tích quan sát; muốn khẳng định tác động cần thiết kế đối chứng hoặc thí nghiệm."),
        ("KB-OUTLIER", "Xử lý ngoại lệ thời gian và quãng đường", "data-quality", "Trip có duration âm, distance âm, tốc độ suy ra phi thực tế hoặc wait time trên 180 phút được gắn data_quality_flag. KPI chuẩn loại bản ghi lỗi nghiêm trọng; báo cáo chất lượng dữ liệu vẫn giữ để theo dõi tỷ lệ lỗi."),
        ("KB-SERVICE-SCD", "Danh mục dịch vụ thay đổi theo thời gian", "service", "dim_service là slowly changing dimension với effective_from và effective_to. Join fact theo event date nằm trong khoảng hiệu lực. Không dùng active_flag hiện tại để phân loại toàn bộ lịch sử vì có thể làm đổi service mix quá khứ."),
        ("KB-CURRENT-DOC", "Ưu tiên phiên bản tài liệu hiện hành", "retrieval-policy", "Retriever ưu tiên status=active, effective_date không sau ngày hỏi và version mới nhất. Draft/deprecated chỉ được trả khi người dùng hỏi lịch sử. Nếu hai nguồn active mâu thuẫn, copilot phải báo xung đột thay vì tự chọn im lặng."),
        ("KB-CITATION", "Citation cho câu trả lời phân tích", "retrieval-policy", "Định nghĩa KPI trích metric catalog; hướng dẫn trường dữ liệu trích data dictionary; số liệu snapshot trích đúng báo cáo ngày. Citation cần source_id và version. Public brief không được dùng thay thế chính sách nội bộ synthetic."),
        ("KB-FRESHNESS", "Freshness theo nhóm dữ liệu", "data-quality", "Booking và trip kỳ vọng trễ không quá 30 phút; online session và vehicle status không quá 15 phút; payment/refund có thể chốt D+1. Các SLA này là giả lập. Copilot phải kiểm tra data_as_of trước câu trả lời có số liệu."),
        ("KB-COMPLETION-DROP", "Checklist khi completion rate giảm", "analysis-guide", "Xác nhận booking volume và definition version; kiểm tra driver matching, no-driver rate, cancel actor/reason, pickup p90, available supply và data freshness. Phân rã theo city, service, zone và 15-minute bucket. Chỉ nêu nguyên nhân khi có bằng chứng nhất quán."),
        ("KB-DEMAND-HEATMAP", "Tạo heatmap nhu cầu", "analysis-guide", "Dùng pickup_zone_id và booking_created_at đã đổi timezone. Đếm valid bookings theo bucket 15 hoặc 30 phút. Giữ UNKNOWN để kiểm tra coverage. Với zone ít hơn 20 booking, gộp cửa sổ dài hơn thay vì diễn giải dao động ngẫu nhiên."),
        ("KB-SUPPLY-SHORTAGE", "Nhận diện thiếu cung", "analysis-guide", "Một hotspot demo cần demand-supply ratio trên 2, ít nhất 20 booking và p90 match time tăng. Kiểm tra đồng thời available drivers và fleet availability. Nếu chỉ booking tăng nhưng service level giữ ổn, chưa kết luận thiếu cung."),
        ("KB-REV-DECOMPOSE", "Phân rã biến động doanh thu", "analysis-guide", "Thay đổi GBV có thể tách gần đúng thành hiệu ứng completed trips, average fare và service mix. Net Revenue còn chịu discount, partner funding, refund và adjustment. Luôn so cùng thứ và ghi rõ cửa sổ dữ liệu."),
        ("KB-CUSTOMER-RETENTION", "Phân tích khách hàng quay lại", "customer", "Xây cohort từ first_completed_trip_at, không từ signup hoặc booking đầu tiên. Retention cần cửa sổ quan sát đầy đủ. Báo cáo theo acquisition channel và first service, tuân thủ ngưỡng nhóm tối thiểu và không xuất customer_id."),
        ("KB-ENTERPRISE", "Phân tích dịch vụ doanh nghiệp trong mock", "enterprise", "ENTERPRISE được xem là service group riêng. Báo cáo có thể phân tích completed trips, GBV, account-level monthly usage và SLA pickup theo account giả lập. Không kết hợp thông tin liên hệ doanh nghiệp hay dữ liệu thật từ trang đăng ký công khai."),
        ("KB-ANSWER-NO-EVIDENCE", "Xử lý câu hỏi không đủ chứng cứ", "retrieval-policy", "Nếu corpus không chứa định nghĩa hoặc snapshot phù hợp, copilot trả lời chưa đủ chứng cứ, nêu dữ liệu còn thiếu và không tự tạo số. Có thể đề xuất bảng hoặc tài liệu cần bổ sung. Đây là hành vi đúng, không phải lỗi retrieval."),
    ]
    return [
        doc(article_id, title, "knowledge-base", body, tags=[topic, "retrieval-evidence"])
        for article_id, title, topic, body in articles
    ]


QUESTIONS = [
    ("Q01", "Một chuyến xe được tính là hoàn thành khi đáp ứng những điều kiện nào?", "retrieval", ["METRIC-TRIP-001"], "Chỉ tính trip COMPLETED, có completed_at, distance > 0,2 km và không phải test/fraud."),
    ("Q02", "Gross Booking Value và doanh thu thuần khác nhau như thế nào?", "retrieval", ["METRIC-REV-001"], "GBV dùng final fare trước giảm/hoàn; Net Revenue điều chỉnh khuyến mại do công ty tài trợ, hoàn tiền và các khoản quy định."),
    ("Q03", "Cancellation Rate được tính trên số booking tạo mới hay booking đã nhận tài xế?", "retrieval", ["METRIC-CANCEL-001"], "Booking Cancellation Rate dùng booking hợp lệ đã tạo; Post-match Cancellation Rate chỉ dùng booking đã ghép tài xế."),
    ("Q04", "Trường hợp khách hủy và tài xế hủy được phân loại như thế nào?", "retrieval", ["METRIC-CANCEL-001"], "Dùng cancel_actor CUSTOMER/DRIVER/SYSTEM và nhóm cancel_reason chuẩn."),
    ("Q05", "Một tài xế được xem là tài xế hoạt động trong ngày khi nào?", "retrieval", ["METRIC-DRIVER-ACTIVE-001"], "Có ít nhất 30 phút online hợp lệ hoặc ít nhất một chuyến hoàn thành."),
    ("Q06", "Online Hours, Busy Hours và Utilization Rate được định nghĩa như thế nào?", "retrieval", ["METRIC-UTIL-001"], "Online gồm AVAILABLE/ASSIGNED/PICKUP/ON_TRIP; Busy gồm ba trạng thái sau; utilization = busy/online."),
    ("Q07", "Acceptance Rate của tài xế được tính theo công thức nào?", "retrieval", ["METRIC-ACCEPT-001", "SCHEMA-OFFER-001"], "accepted_offers chia eligible_offers sau khi loại offer lỗi, trùng hoặc bị thu hồi sớm."),
    ("Q08", "Thế nào là khách hàng mới, quay lại và tái kích hoạt?", "retrieval", ["METRIC-CUSTOMER-001"], "Phân nhóm dựa trên lịch sử chuyến hoàn thành và khoảng không hoạt động 60 ngày."),
    ("Q09", "Một chuyến có voucher được ghi nhận doanh thu và chi phí khuyến mại thế nào?", "retrieval", ["METRIC-PROMO-001", "SCHEMA-PROMO-001"], "Tách phần công ty và đối tác tài trợ; tránh nhân bản trip khi join nhiều voucher."),
    ("Q10", "KPI được gán cho thành phố theo điểm đón, điểm trả hay khu vực tài xế?", "retrieval", ["METRIC-GEO-001"], "Booking/chuyến theo pickup; báo cáo điểm đến theo dropoff; tài xế theo phần lớn online minutes."),
    ("Q11", "Tổng booking, chuyến hoàn thành và doanh thu theo thành phố lấy từ bảng nào?", "hybrid", ["SCHEMA-BOOKING-001", "SCHEMA-TRIP-001", "SCHEMA-PAYMENT-001", "METRIC-REV-001"], "Booking từ fact_bookings, completed/GBV từ fact_trips, Net Revenue từ payment aggregate."),
    ("Q12", "Muốn so doanh thu tuần này với tuần trước thì dùng metric và kiểm tra gì?", "hybrid", ["METRIC-REV-001", "PLAYBOOK-REV-001"], "Chốt metric, cùng cửa sổ thời gian và phân rã trips, fare, service mix, discount/refund."),
    ("Q13", "Phân tích đóng góp doanh thu theo Taxi, Bike và Express dùng dimension nào?", "hybrid", ["SCHEMA-SERVICE-001", "CATALOG-SERVICE-001", "METRIC-REV-001"], "Join service_id với dim_service theo hiệu lực rồi tổng hợp metric doanh thu đã chốt."),
    ("Q14", "Làm sao tìm khung giờ có nhu cầu đặt xe cao nhất?", "hybrid", ["SCHEMA-BOOKING-001", "METRIC-SUPPLY-001"], "Đếm booking hợp lệ theo booking_created_at, pickup zone/city và time bucket."),
    ("Q15", "Làm sao xác định khu vực có booking tăng mạnh nhất trong bảy ngày?", "hybrid", ["SCHEMA-BOOKING-001", "SCHEMA-LOCATION-001", "METRIC-GEO-001"], "Tổng hợp booking theo pickup_zone và so với cửa sổ đối chứng cùng thứ."),
    ("Q16", "Giá trị trung bình mỗi chuyến theo dịch vụ được tính ra sao?", "hybrid", ["METRIC-REV-001", "SCHEMA-TRIP-001", "SCHEMA-SERVICE-001"], "GBV chia completed trips trong cùng service; không dùng quoted fare."),
    ("Q17", "Tỷ lệ booking chuyển thành chuyến hoàn thành tính như thế nào?", "hybrid", ["METRIC-CONVERSION-001", "SCHEMA-BOOKING-001", "SCHEMA-TRIP-001"], "Distinct booking hoàn thành chia booking hợp lệ đã tạo."),
    ("Q18", "Số chuyến hoàn thành giảm dù booking tăng thì nên điều tra gì?", "reasoning", ["POLICY-ANALYSIS-001", "METRIC-CONVERSION-001", "PLAYBOOK-CANCEL-001", "PLAYBOOK-SUPPLY-001"], "Kiểm tra conversion, matching, cancellation, supply và chất lượng dữ liệu trước khi kết luận."),
    ("Q19", "Thời gian từ đặt xe đến ghép tài xế được định nghĩa thế nào?", "hybrid", ["METRIC-WAIT-001", "SCHEMA-BOOKING-001"], "matched_at trừ booking_created_at; báo median/p90 cùng trung bình."),
    ("Q20", "Thời gian tài xế đến điểm đón trung bình lấy từ timestamp nào?", "hybrid", ["METRIC-WAIT-001", "SCHEMA-BOOKING-001"], "trip_started_at trừ matched_at theo định nghĩa pickup actual."),
    ("Q21", "Khu vực không tìm được tài xế xác định bằng trường nào?", "hybrid", ["METRIC-CANCEL-001", "SCHEMA-BOOKING-001", "SCHEMA-LOCATION-001"], "Dùng cancel_reason=no_driver_available và pickup_zone_id."),
    ("Q22", "Tỷ lệ hủy theo lý do và bên hủy cần nhóm thế nào?", "hybrid", ["METRIC-CANCEL-001", "PLAYBOOK-CANCEL-001"], "Nhóm theo actor/reason và tách pre-match khỏi post-match."),
    ("Q23", "Khung giờ mất cân bằng cung cầu được đo bằng chỉ số nào?", "hybrid", ["METRIC-SUPPLY-001", "SCHEMA-ONLINE-001", "SCHEMA-BOOKING-001"], "Demand Supply Ratio theo zone và bucket 15 phút."),
    ("Q24", "Làm sao tìm khu vực nhu cầu cao nhưng tỷ lệ hoàn thành thấp?", "reasoning", ["METRIC-SUPPLY-001", "METRIC-CONVERSION-001", "POLICY-ANALYSIS-001"], "Lọc volume đủ lớn rồi kết hợp demand, supply, conversion và cancellation."),
    ("Q25", "Nên điều tra nguyên nhân thời gian đón khách tăng theo trình tự nào?", "reasoning", ["METRIC-WAIT-001", "PLAYBOOK-SUPPLY-001", "POLICY-ANALYSIS-001"], "Kiểm tra freshness, phân rã zone/giờ, supply, match time và ngoại lệ."),
    ("Q26", "Số tài xế active theo thành phố và loại phương tiện lấy từ đâu?", "hybrid", ["METRIC-DRIVER-ACTIVE-001", "SCHEMA-ONLINE-001", "SCHEMA-DRIVER-001"], "Xác định active từ online session/chuyến rồi join driver dimension."),
    ("Q27", "Số chuyến và doanh thu trung bình trên mỗi tài xế tính ra sao?", "hybrid", ["METRIC-DRIVER-ACTIVE-001", "METRIC-REV-001", "SCHEMA-TRIP-001"], "Completed trips hoặc Net Revenue chia active drivers cùng lát cắt."),
    ("Q28", "Utilization Rate của tài xế theo giờ tính thế nào?", "hybrid", ["METRIC-UTIL-001", "SCHEMA-ONLINE-001"], "Cắt session theo giờ rồi busy minutes chia online minutes."),
    ("Q29", "Nhóm tài xế có Acceptance Rate thấp xác định thế nào?", "hybrid", ["METRIC-ACCEPT-001", "SCHEMA-OFFER-001", "PLAYBOOK-DRIVER-001"], "Tính trên eligible offers và so theo cohort đủ lớn trong cùng bối cảnh."),
    ("Q30", "Tỷ lệ tài xế mới còn hoạt động sau 30 ngày tính thế nào?", "hybrid", ["METRIC-RETENTION-001", "SCHEMA-DRIVER-001", "SCHEMA-ONLINE-001"], "Dùng cohort activation và active trong ngày 24-30 khi đủ thời gian quan sát."),
    ("Q31", "Phân tích tài xế ngừng hoạt động cần lưu ý gì?", "reasoning", ["METRIC-RETENTION-001", "PLAYBOOK-DRIVER-001", "POLICY-ANALYSIS-001"], "Churn proxy không đồng nghĩa nghỉ việc; chỉ nêu tương quan sau kiểm tra cohort và dữ liệu."),
    ("Q32", "Thời gian sạc có được tính vào Online Hours không?", "retrieval", ["METRIC-CHARGE-001", "METRIC-UTIL-001"], "Không; CHARGING bị loại khỏi Online Hours theo quy tắc demo hiện hành."),
    ("Q33", "Đếm số xe khả dụng, đang chạy, đang sạc và bảo dưỡng thế nào?", "hybrid", ["METRIC-FLEET-001", "SCHEMA-FLEET-001"], "Chọn snapshot và count distinct vehicle_id theo state."),
    ("Q34", "Tỷ lệ sử dụng đội xe theo dòng xe và thành phố tính thế nào?", "hybrid", ["METRIC-FLEET-001", "SCHEMA-FLEET-001", "SCHEMA-TRIP-001"], "Số xe có completed trip chia số xe khả dụng, phân theo city và vehicle type."),
    ("Q35", "Trạm sạc có thời gian chờ cao nhất được xác định thế nào?", "hybrid", ["METRIC-CHARGE-001", "SCHEMA-CHARGE-001"], "Tính charge_start_at - station_arrival_at theo station; báo median và p90."),
    ("Q36", "Đánh giá thiếu xe hoặc thời gian sạc ảnh hưởng completion rate thế nào?", "reasoning", ["METRIC-FLEET-001", "METRIC-CHARGE-001", "METRIC-CONVERSION-001", "POLICY-ANALYSIS-001"], "Kết hợp fleet availability, charging queue và conversion theo zone/time; không khẳng định nhân quả chỉ từ tương quan."),
]


PARAPHRASE_PREFIXES = [
    "Cho tôi biết: ",
    "Theo định nghĩa hiện hành, ",
    "Trong bộ dữ liệu demo, ",
]


def make_eval_rows(doc_to_chunk: dict[str, list[str]]) -> list[dict]:
    rows = []
    for qid, question, query_type, relevant_docs, expected in QUESTIONS:
        variants = [question]
        lower = question[0].lower() + question[1:]
        variants.extend(prefix + lower for prefix in PARAPHRASE_PREFIXES)
        for index, variant in enumerate(variants):
            rows.append({
                "query_id": f"{qid}-V{index + 1}",
                "base_question_id": qid,
                "query": variant,
                "query_type": query_type,
                "language": "vi",
                "relevant_document_ids": relevant_docs,
                "relevant_chunk_ids": [cid for did in relevant_docs for cid in doc_to_chunk.get(did, [])],
                "expected_answer": expected,
                "must_cite": relevant_docs,
                "dataset_scope": "synthetic-demo",
            })
    return rows


def main() -> None:
    documents = list(CORE_DOCS)
    documents.append(service_catalog_doc())
    documents.extend(generate_playbooks())
    documents.extend(generate_handbook_articles())
    documents.extend(deprecated_docs())
    documents.extend(
        doc(
            source["source_id"],
            source["title"],
            "public-source-brief",
            source["summary"],
            source_type="public-summary",
            source_url=source["url"],
            tags=source["tags"],
            access_level="public",
        )
        for source in PUBLIC_SOURCES
    )
    documents.extend(generate_daily_reports())

    chunks = []
    doc_to_chunk: dict[str, list[str]] = {}
    for item in documents:
        chunk_id = stable_id("CHK", item["document_id"] + ":0")
        chunk = {
            "chunk_id": chunk_id,
            "document_id": item["document_id"],
            "title": item["title"],
            "text": item["content"],
            "category": item["category"],
            "language": item["language"],
            "source_type": item["source_type"],
            "status": item["status"],
            "version": item["version"],
            "effective_date": item["effective_date"],
            "source_url": item["source_url"],
            "access_level": item["access_level"],
            "is_official_internal_data": item["is_official_internal_data"],
            "tags": item["tags"],
            "content_sha256": hashlib.sha256(item["content"].encode("utf-8")).hexdigest(),
        }
        chunks.append(chunk)
        doc_to_chunk.setdefault(item["document_id"], []).append(chunk_id)

    eval_rows = make_eval_rows(doc_to_chunk)
    manifest = {
        "name": "xanhsm-da-retrieval-mock-vi",
        "version": "1.0.0",
        "primary_purpose": "retrieval-corpus",
        "primary_file": "corpus/retrieval_corpus.jsonl",
        "generated_at": "2026-09-21",
        "seed": SEED,
        "language": "vi",
        "license_note": "Synthetic content is for demo/testing. Public briefs are paraphrases with source URLs; review source terms before redistribution.",
        "warning": "No synthetic policy or figure in this dataset represents actual Xanh SM internal data.",
        "counts": {
            "documents": len(documents),
            "chunks": len(chunks),
            "retrieval_records": len(chunks),
            "knowledge_base_articles": sum(1 for d in documents if d["category"] == "knowledge-base"),
            "evaluation_queries": len(eval_rows),
            "public_source_briefs": len(PUBLIC_SOURCES),
            "synthetic_daily_reports": sum(1 for d in documents if d["category"] == "daily-operations"),
            "hard_negative_documents": sum(1 for d in documents if d["status"] != "active"),
        },
    }

    write_jsonl(CORPUS_DIR / "documents.jsonl", documents)
    write_jsonl(CORPUS_DIR / "chunks.jsonl", chunks)
    retrieval_records = [
        {
            "id": chunk["chunk_id"],
            "text": f"{chunk['title']}\n\n{chunk['text']}",
            "metadata": {key: value for key, value in chunk.items() if key not in {"chunk_id", "title", "text"}},
        }
        for chunk in chunks
    ]
    write_jsonl(CORPUS_DIR / "retrieval_corpus.jsonl", retrieval_records)
    write_jsonl(EVAL_DIR / "retrieval_eval.jsonl", eval_rows)
    (SOURCES_DIR / "public_sources.json").write_text(json.dumps(PUBLIC_SOURCES, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (ROOT / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(manifest, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
