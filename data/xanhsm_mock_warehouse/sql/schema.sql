-- Synthetic mock warehouse schema. All identifiers and values are fictional.

CREATE TABLE dim_date (date_key TEXT PRIMARY KEY, calendar_date TEXT, year INTEGER, quarter INTEGER, month INTEGER, week_of_year INTEGER, day_of_week INTEGER, day_name_vi TEXT, is_weekend INTEGER);

CREATE TABLE dim_location (zone_id TEXT PRIMARY KEY, city_id TEXT, city_name TEXT, district_id TEXT, district_name TEXT, zone_name TEXT, geo_version TEXT);

CREATE TABLE dim_service (service_id TEXT PRIMARY KEY, service_name TEXT, service_group TEXT, vehicle_type TEXT, effective_from TEXT, effective_to TEXT, active_flag INTEGER);

CREATE TABLE dim_driver (driver_id TEXT PRIMARY KEY, activation_date TEXT, contract_type TEXT, vehicle_type TEXT, home_city_id TEXT, is_test_account INTEGER, status TEXT);

CREATE TABLE dim_customer (customer_id TEXT PRIMARY KEY, signup_date TEXT, first_completed_trip_at TEXT, acquisition_channel TEXT, consent_analytics INTEGER, deleted_at TEXT);

CREATE TABLE dim_vehicle (vehicle_id TEXT PRIMARY KEY, vehicle_type TEXT, model_code TEXT, city_id TEXT, activation_date TEXT, document_valid_until TEXT);

CREATE TABLE dim_charging_station (station_id TEXT PRIMARY KEY, city_id TEXT, zone_id TEXT, station_name TEXT, connector_count INTEGER, effective_from TEXT, active_flag INTEGER);

CREATE TABLE dim_campaign (campaign_id TEXT PRIMARY KEY, campaign_name TEXT, service_id TEXT, funding_type TEXT, start_date TEXT, end_date TEXT, data_scope TEXT);

CREATE TABLE fact_bookings (booking_id TEXT PRIMARY KEY, booking_created_at TEXT, date_key TEXT, customer_id TEXT, service_id TEXT, pickup_zone_id TEXT, dropoff_zone_id TEXT, city_id TEXT, booking_status TEXT, cancel_actor TEXT, cancel_reason TEXT, matched_at TEXT, driver_id TEXT, is_test INTEGER, is_duplicate INTEGER);

CREATE TABLE fact_trips (trip_id TEXT PRIMARY KEY, booking_id TEXT, trip_started_at TEXT, completed_at TEXT, trip_status TEXT, distance_km REAL, duration_minutes INTEGER, driver_id TEXT, vehicle_id TEXT, final_fare INTEGER, surcharge INTEGER, fraud_confirmed INTEGER);

CREATE TABLE fact_payments (payment_id TEXT PRIMARY KEY, trip_id TEXT, transaction_type TEXT, payment_status TEXT, gross_amount INTEGER, customer_discount INTEGER, company_funded_discount INTEGER, partner_funded_discount INTEGER, refund_amount INTEGER, tax_excluded_adjustment INTEGER, paid_at TEXT);

CREATE TABLE fact_promotions (booking_id TEXT, campaign_id TEXT, voucher_code_hash TEXT, discount_amount INTEGER, funded_by_company INTEGER, funded_by_partner INTEGER, applied_at TEXT);

CREATE TABLE fact_driver_offers (offer_id TEXT PRIMARY KEY, booking_id TEXT, driver_id TEXT, service_id TEXT, sent_at TEXT, responded_at TEXT, response TEXT, recalled_within_10s INTEGER, delivery_error INTEGER);

CREATE TABLE fact_driver_online_sessions (session_id TEXT PRIMARY KEY, driver_id TEXT, state TEXT, start_at TEXT, end_at TEXT, city_id TEXT, zone_id TEXT, duration_minutes INTEGER);

CREATE TABLE fact_charging_sessions (charging_session_id TEXT PRIMARY KEY, vehicle_id TEXT, driver_id TEXT, station_id TEXT, station_arrival_at TEXT, charge_start_at TEXT, charge_end_at TEXT, energy_kwh REAL, start_soc INTEGER, end_soc INTEGER, session_status TEXT);

CREATE TABLE fact_vehicle_status (snapshot_id TEXT PRIMARY KEY, snapshot_at TEXT, date_key TEXT, vehicle_id TEXT, city_id TEXT, state TEXT, battery_soc INTEGER, document_valid INTEGER, service_id TEXT);

CREATE TABLE agg_daily_city_service (calendar_date TEXT, city_id TEXT, city_name TEXT, service_id TEXT, valid_bookings INTEGER, completed_trips INTEGER, cancelled_bookings INTEGER, gbv_vnd INTEGER, net_revenue_vnd INTEGER, discount_vnd INTEGER, active_drivers INTEGER, completion_rate REAL, cancellation_rate REAL);
