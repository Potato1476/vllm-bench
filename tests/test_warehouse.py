"""Warehouse queries remain read-only even when model-generated SQL is hostile."""

from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from services.llm_pipeline import warehouse

ROOT = Path(__file__).resolve().parents[1]


class WarehouseTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "test.sqlite"
        with sqlite3.connect(self.path) as conn:
            conn.execute("CREATE TABLE agg_daily_city_service "
                         "(calendar_date TEXT, city_id TEXT, valid_bookings INTEGER)")
            conn.executemany("INSERT INTO agg_daily_city_service VALUES (?, ?, ?)", [
                ("2026-01-01", "HAN", 42), ("2026-01-02", "HAN", 51),
            ])
            conn.execute("CREATE TABLE fact_driver_online_sessions "
                         "(driver_id TEXT, city_id TEXT, state TEXT, start_at TEXT, end_at TEXT)")
            conn.executemany("INSERT INTO fact_driver_online_sessions VALUES (?,?,?,?,?)", [
                ("d1", "HAN", "AVAILABLE", "2025-12-31 18:00:00", "2025-12-31 19:00:00"),
                ("d2", "HAN", "OFFLINE", "2025-12-31 18:00:00", "2025-12-31 19:00:00"),
            ])
            conn.execute("CREATE TABLE dim_charging_station (station_id TEXT, city_id TEXT)")
            conn.execute("INSERT INTO dim_charging_station VALUES ('s1','DAD')")
            conn.execute("CREATE TABLE fact_charging_sessions "
                         "(station_id TEXT, session_status TEXT, station_arrival_at TEXT, "
                         "charge_start_at TEXT)")
            conn.execute("INSERT INTO fact_charging_sessions VALUES "
                         "('s1','COMPLETED','2026-01-01 00:00:00','2026-01-01 00:15:00')")

    def test_select_uses_database_rows(self) -> None:
        result = warehouse.execute(
            "SELECT SUM(valid_bookings) AS total_bookings "
            "FROM agg_daily_city_service WHERE city_id='HAN'", self.path,
        )
        self.assertEqual(result.rows, ((93,),))
        self.assertIn("total_bookings=93", warehouse.format_answer(result))

    def test_common_booking_question_uses_sum_and_filters(self) -> None:
        sql = warehouse.plan_common(
            "Tổng số booking ở Hà Nội ngày 2026-01-01 là bao nhiêu?"
        )
        self.assertIsNotNone(sql)
        assert sql is not None
        self.assertIn("SUM(valid_bookings)", sql)
        self.assertIn("calendar_date = '2026-01-01'", sql)
        self.assertEqual(warehouse.execute(sql, self.path).rows[0][-1], 42)

    def test_fact_metric_templates_use_rows_and_local_day(self) -> None:
        online = warehouse.plan_common(
            "Có bao nhiêu tài xế online ở Hà Nội ngày 2026-01-01?"
        )
        queue = warehouse.plan_common(
            "Thời gian chờ sạc trung bình ở Đà Nẵng là bao nhiêu phút?"
        )
        assert online is not None and queue is not None
        self.assertEqual(warehouse.execute(online, self.path).rows, ((1,),))
        self.assertEqual(warehouse.execute(queue, self.path).rows, ((1, 15.0),))

    def test_mutations_and_database_introspection_are_denied(self) -> None:
        for sql in (
            "DELETE FROM agg_daily_city_service",
            "WITH x AS (DELETE FROM agg_daily_city_service RETURNING *) SELECT * FROM x",
            "SELECT name FROM sqlite_master",
            "SELECT load_extension('bad')",
            "SELECT 1; DROP TABLE agg_daily_city_service",
            "SELECT 1",
        ):
            with self.subTest(sql=sql), self.assertRaises(warehouse.WarehouseQueryError):
                warehouse.execute(sql, self.path)
        self.assertEqual(
            warehouse.execute("SELECT COUNT(*) FROM agg_daily_city_service", self.path).rows,
            ((2,),),
        )

    def test_result_is_bounded(self) -> None:
        result = warehouse.execute(
            "SELECT a.valid_bookings FROM agg_daily_city_service a "
            "CROSS JOIN agg_daily_city_service b "
            "CROSS JOIN agg_daily_city_service c "
            "CROSS JOIN agg_daily_city_service d "
            "CROSS JOIN agg_daily_city_service e", self.path,
        )
        self.assertEqual(len(result.rows), warehouse.MAX_ROWS)
        self.assertTrue(result.truncated)


if __name__ == "__main__":
    unittest.main()


class RouterTest(unittest.TestCase):
    """What goes to SQL and what goes to the documents.

    Untested when the router landed, and that is how a definition question came to be
    answered with a total. The failure is silent by construction: both paths return a
    confident, well-formed answer, so only an assertion about WHICH path ran can catch it.
    """

    # Every one of these is a real question from the corpus categories or from
    # bench/agents_sim. They are full of metric words, which is exactly why _INTENT alone
    # routes them wrong.
    EXPLAIN = (
        "GBV được tính như thế nào?",
        "GBV được tính như thế nào và loại trừ những khoản nào?",
        "Tỷ lệ huỷ chuyến tính trên mẫu số nào?",
        "Một chuyến xe được tính là hoàn thành khi đáp ứng điều kiện nào?",
        "Bảng đặt chuyến có grain là gì, và cần lưu ý gì khi join với bảng tài xế?",
        "GBV tuần này giảm so với tuần trước, cần kiểm tra những gì trước?",
        "Trường trip_status nhận những giá trị nào?",
    )
    FIGURES = (
        "Ngày 2026-01-01 có bao nhiêu booking?",
        "Top 5 thành phố theo GBV tháng 3",
        "Tổng doanh thu tuần trước là bao nhiêu?",
        "So sánh số chuyến giữa Hà Nội và Đà Nẵng",
    )

    def test_explanations_go_to_the_documents(self) -> None:
        for q in self.EXPLAIN:
            with self.subTest(q=q):
                self.assertFalse(warehouse.looks_analytical(q),
                                 "cau hoi giai thich bi dinh tuyen sang SQL")

    def test_figure_questions_go_to_the_warehouse(self) -> None:
        """The other half: narrowing the router must not switch the feature off."""
        for q in self.FIGURES:
            with self.subTest(q=q):
                self.assertTrue(warehouse.looks_analytical(q),
                                "cau hoi so lieu khong toi duoc warehouse")

    def test_no_gold_evaluation_question_is_routed_to_sql(self) -> None:
        """The whole 144, not a handful I picked.

        A hand-written sample is chosen from questions I already had in mind, so it
        reflects the bug I was thinking about rather than the ones I was not. The gold set
        is the corpus's own ground truth for what the copilot answers, and measuring
        against all of it is what showed the first fix was still letting 42% through --
        a sample of seven had looked clean.
        """
        path = ROOT / "data" / "xanhsm_retrieval_mock" / "eval" / "retrieval_eval.jsonl"
        gold = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()
                if line.strip()]
        questions = [r.get("query") or r.get("question") or "" for r in gold]
        self.assertGreater(len(questions), 100, "khong doc duoc bo cau hoi vang")
        routed = [q for q in questions if warehouse.looks_analytical(q)]
        self.assertEqual(routed, [], f"{len(routed)}/{len(questions)} cau vang bi day sang SQL")

    def test_the_router_is_what_protects_plan_common(self) -> None:
        """plan_common assumes it was routed to; it does not re-check, and should not --
        a second copy of the rule in two places is a pair that drifts.

        That makes looks_analytical the only thing standing between a definition question
        and a metric template, with NO model call in between to fail first. This records
        where the responsibility sits: plan_common answers "GBV được tính như thế nào?"
        with a SUM over the daily aggregate when asked directly.
        """
        self.assertIsNotNone(warehouse.plan_common("GBV được tính như thế nào?"))
        self.assertFalse(warehouse.looks_analytical("GBV được tính như thế nào?"))
