-- 1. Booking, completed trips, and revenue by city and day.
SELECT a.calendar_date, a.city_name,
       SUM(a.valid_bookings) AS valid_bookings,
       SUM(a.completed_trips) AS completed_trips,
       SUM(a.net_revenue_vnd) AS net_revenue_vnd
FROM agg_daily_city_service a
GROUP BY a.calendar_date, a.city_name
ORDER BY a.calendar_date, a.city_name;

-- 2. Revenue contribution by service.
SELECT service_id, SUM(net_revenue_vnd) AS net_revenue_vnd,
       1.0 * SUM(net_revenue_vnd) / SUM(SUM(net_revenue_vnd)) OVER () AS revenue_share
FROM agg_daily_city_service
GROUP BY service_id
ORDER BY net_revenue_vnd DESC;

-- 3. Peak booking hours by city.
SELECT city_id, CAST(strftime('%H', booking_created_at) AS INTEGER) AS hour_of_day,
       COUNT(*) AS valid_bookings
FROM fact_bookings
WHERE is_test = 0 AND is_duplicate = 0
GROUP BY city_id, hour_of_day
ORDER BY city_id, valid_bookings DESC;

-- 4. Cancellation breakdown.
SELECT city_id, service_id, cancel_actor, cancel_reason, COUNT(*) AS cancellations
FROM fact_bookings
WHERE booking_status = 'CANCELLED'
GROUP BY city_id, service_id, cancel_actor, cancel_reason
ORDER BY cancellations DESC;

-- 5. Booking-to-completed conversion.
SELECT date_key, city_id, service_id,
       COUNT(DISTINCT CASE WHEN is_test = 0 AND is_duplicate = 0 THEN b.booking_id END) AS valid_bookings,
       COUNT(DISTINCT t.trip_id) AS completed_trips,
       1.0 * COUNT(DISTINCT t.trip_id) /
         NULLIF(COUNT(DISTINCT CASE WHEN is_test = 0 AND is_duplicate = 0 THEN b.booking_id END), 0) AS completion_rate
FROM fact_bookings b
LEFT JOIN vw_completed_trips t ON t.booking_id = b.booking_id
GROUP BY date_key, city_id, service_id;

-- 6. Match and pickup time by city.
SELECT b.city_id,
       AVG((julianday(b.matched_at) - julianday(b.booking_created_at)) * 1440.0) AS avg_match_minutes,
       AVG((julianday(t.trip_started_at) - julianday(b.matched_at)) * 1440.0) AS avg_pickup_minutes
FROM fact_bookings b
JOIN vw_completed_trips t ON t.booking_id = b.booking_id
GROUP BY b.city_id;

-- 7. Active drivers and completed trips per driver.
SELECT substr(t.completed_at, 1, 10) AS calendar_date, b.city_id,
       COUNT(DISTINCT t.driver_id) AS active_drivers,
       COUNT(*) AS completed_trips,
       1.0 * COUNT(*) / COUNT(DISTINCT t.driver_id) AS trips_per_active_driver
FROM vw_completed_trips t
JOIN fact_bookings b ON b.booking_id = t.booking_id
GROUP BY calendar_date, b.city_id;

-- 8. Driver utilization.
SELECT calendar_date,
       SUM(busy_minutes) AS busy_minutes,
       SUM(online_minutes) AS online_minutes,
       1.0 * SUM(busy_minutes) / NULLIF(SUM(online_minutes), 0) AS utilization_rate
FROM vw_driver_utilization
GROUP BY calendar_date
ORDER BY calendar_date;

-- 9. Acceptance rate from eligible offers.
SELECT service_id,
       SUM(CASE WHEN response = 'ACCEPTED' THEN 1 ELSE 0 END) AS accepted_offers,
       SUM(CASE WHEN recalled_within_10s = 0 AND delivery_error = 0 THEN 1 ELSE 0 END) AS eligible_offers,
       1.0 * SUM(CASE WHEN response = 'ACCEPTED' THEN 1 ELSE 0 END) /
         NULLIF(SUM(CASE WHEN recalled_within_10s = 0 AND delivery_error = 0 THEN 1 ELSE 0 END), 0) AS acceptance_rate
FROM fact_driver_offers
GROUP BY service_id;

-- 10. Fleet status from the latest snapshot in each city.
WITH latest AS (
  SELECT city_id, MAX(snapshot_at) AS snapshot_at
  FROM fact_vehicle_status GROUP BY city_id
)
SELECT s.city_id, s.state, COUNT(DISTINCT s.vehicle_id) AS vehicles
FROM fact_vehicle_status s
JOIN latest l ON l.city_id = s.city_id AND l.snapshot_at = s.snapshot_at
GROUP BY s.city_id, s.state;

-- 11. Charging queue by station.
SELECT station_id, COUNT(*) AS completed_sessions,
       AVG((julianday(charge_start_at) - julianday(station_arrival_at)) * 1440.0) AS avg_queue_minutes,
       MAX((julianday(charge_start_at) - julianday(station_arrival_at)) * 1440.0) AS max_queue_minutes
FROM fact_charging_sessions
WHERE session_status = 'COMPLETED'
GROUP BY station_id
ORDER BY avg_queue_minutes DESC;

-- 12. Customer 30-day repeat proxy.
WITH first_trip AS (
  SELECT b.customer_id, MIN(t.completed_at) AS first_completed_at
  FROM vw_completed_trips t JOIN fact_bookings b ON b.booking_id = t.booking_id
  GROUP BY b.customer_id
), repeated AS (
  SELECT f.customer_id,
         MAX(CASE WHEN t.completed_at > f.first_completed_at
                   AND t.completed_at <= datetime(f.first_completed_at, '+30 day') THEN 1 ELSE 0 END) AS repeated_30d
  FROM first_trip f
  JOIN fact_bookings b ON b.customer_id = f.customer_id
  JOIN vw_completed_trips t ON t.booking_id = b.booking_id
  GROUP BY f.customer_id
)
SELECT AVG(repeated_30d) AS repeat_rate_30d FROM repeated;
