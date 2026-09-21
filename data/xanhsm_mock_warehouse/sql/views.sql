-- Metrics aligned with the retrieval metric catalog.
CREATE VIEW vw_completed_trips AS
SELECT * FROM fact_trips
WHERE trip_status = 'COMPLETED'
  AND completed_at IS NOT NULL
  AND distance_km > 0.2
  AND fraud_confirmed = 0;

CREATE VIEW vw_daily_service_kpis AS
SELECT b.date_key, b.city_id, b.service_id,
       COUNT(DISTINCT CASE WHEN b.is_test = 0 AND b.is_duplicate = 0 THEN b.booking_id END) AS valid_bookings,
       COUNT(DISTINCT t.trip_id) AS completed_trips,
       SUM(t.final_fare) AS gbv_vnd,
       1.0 * COUNT(DISTINCT t.trip_id) /
         NULLIF(COUNT(DISTINCT CASE WHEN b.is_test = 0 AND b.is_duplicate = 0 THEN b.booking_id END), 0) AS completion_rate
FROM fact_bookings b
LEFT JOIN vw_completed_trips t ON t.booking_id = b.booking_id
GROUP BY b.date_key, b.city_id, b.service_id;

CREATE VIEW vw_driver_utilization AS
SELECT driver_id, substr(start_at, 1, 10) AS calendar_date,
       SUM(CASE WHEN state IN ('ASSIGNED','PICKUP','ON_TRIP') THEN duration_minutes ELSE 0 END) AS busy_minutes,
       SUM(CASE WHEN state IN ('AVAILABLE','ASSIGNED','PICKUP','ON_TRIP') THEN duration_minutes ELSE 0 END) AS online_minutes
FROM fact_driver_online_sessions
GROUP BY driver_id, substr(start_at, 1, 10);
