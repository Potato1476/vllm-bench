"""Warehouse queries remain read-only even when model-generated SQL is hostile."""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from services.llm_pipeline import warehouse


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
