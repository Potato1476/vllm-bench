#!/usr/bin/env python3
"""Generate a deterministic, relational mock warehouse for a Xanh SM DA copilot.

The output is synthetic and contains no real customer, driver, vehicle, or business data.
CSV files are the portable source of truth. A SQLite database is built from the same files.
"""

from __future__ import annotations

import csv
import json
import math
import random
import sqlite3
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CSV_DIR = ROOT / "csv"
SQL_DIR = ROOT / "sql"
DB_PATH = ROOT / "xanhsm_mock_warehouse.sqlite"
SEED = 20260921
random.seed(SEED)

START_DATE = date(2026, 1, 1)
DAYS = 240
BOOKINGS = 300_000
CUSTOMERS = 60_000
DRIVERS = 5_000
VEHICLES = 3_500
STATIONS = 36
CAMPAIGNS = 24
ONLINE_SESSIONS = 360_000
CHARGING_SESSIONS = 60_000

CITIES = {
    "HAN": {"name": "Hà Nội", "weight": 0.39, "districts": ["Cầu Giấy", "Đống Đa", "Hai Bà Trưng", "Hoàn Kiếm", "Long Biên", "Nam Từ Liêm", "Tây Hồ", "Thanh Xuân"]},
    "HCM": {"name": "TP.HCM", "weight": 0.43, "districts": ["Quận 1", "Quận 3", "Quận 7", "Bình Thạnh", "Gò Vấp", "Phú Nhuận", "Tân Bình", "Thủ Đức"]},
    "DAD": {"name": "Đà Nẵng", "weight": 0.18, "districts": ["Hải Châu", "Thanh Khê", "Sơn Trà", "Ngũ Hành Sơn", "Liên Chiểu", "Cẩm Lệ"]},
}

SERVICES = {
    "TAXI": {"name": "Xanh SM Taxi - Demo", "vehicle_type": "CAR", "weight": 0.45, "base_fare": 28_000, "per_km": 12_000},
    "BIKE": {"name": "Xanh SM Bike - Demo", "vehicle_type": "BIKE", "weight": 0.28, "base_fare": 12_000, "per_km": 5_000},
    "EXPRESS": {"name": "Green Express - Demo", "vehicle_type": "BIKE", "weight": 0.12, "base_fare": 16_000, "per_km": 6_000},
    "LUXURY": {"name": "Luxury - Demo", "vehicle_type": "CAR", "weight": 0.07, "base_fare": 45_000, "per_km": 17_000},
    "ENTERPRISE": {"name": "Green SM Enterprise - Demo", "vehicle_type": "CAR", "weight": 0.08, "base_fare": 32_000, "per_km": 11_000},
}

ACQUISITION_CHANNELS = ["organic", "paid_search", "social", "referral", "partner", "enterprise"]
CONTRACT_TYPES = ["employee", "platform_partner", "rental_partner"]
VEHICLE_MODELS = {"CAR": ["EV-CAR-A", "EV-CAR-B", "EV-CAR-C"], "BIKE": ["EV-BIKE-A", "EV-BIKE-B"]}


def iso(dt: datetime | None) -> str:
    return dt.strftime("%Y-%m-%d %H:%M:%S") if dt else ""


def weighted_choice(weight_map: dict[str, float]) -> str:
    return random.choices(list(weight_map), weights=list(weight_map.values()), k=1)[0]


def money(value: float) -> int:
    return max(0, int(round(value / 1000.0)) * 1000)


def open_csv(name: str, headers: list[str]):
    handle = (CSV_DIR / f"{name}.csv").open("w", encoding="utf-8", newline="")
    writer = csv.writer(handle)
    writer.writerow(headers)
    return handle, writer


def build_dimensions():
    date_rows = []
    day_names = ["Thứ hai", "Thứ ba", "Thứ tư", "Thứ năm", "Thứ sáu", "Thứ bảy", "Chủ nhật"]
    for offset in range(DAYS):
        current = START_DATE + timedelta(days=offset)
        date_rows.append([current.strftime("%Y%m%d"), current.isoformat(), current.year, (current.month - 1) // 3 + 1, current.month, current.isocalendar().week, current.weekday() + 1, day_names[current.weekday()], int(current.weekday() >= 5)])

    location_rows, zones_by_city = [], defaultdict(list)
    for city_id, city in CITIES.items():
        for district_index, district_name in enumerate(city["districts"], 1):
            district_id = f"{city_id}-D{district_index:02d}"
            for zone_index in range(1, 4):
                zone_id = f"{district_id}-Z{zone_index:02d}"
                zone_name = f"{district_name} - vùng {zone_index}"
                location_rows.append([zone_id, city_id, city["name"], district_id, district_name, zone_name, "geo-2026.1"])
                zones_by_city[city_id].append(zone_id)

    service_rows = [[sid, cfg["name"], sid, cfg["vehicle_type"], "2026-01-01", "", 1] for sid, cfg in SERVICES.items()]

    driver_rows, drivers_by_city_type, driver_meta = [], defaultdict(list), {}
    city_weights = {key: cfg["weight"] for key, cfg in CITIES.items()}
    for number in range(1, DRIVERS + 1):
        driver_id = f"DRV{number:06d}"
        city_id = weighted_choice(city_weights)
        vehicle_type = random.choices(["CAR", "BIKE"], [0.67, 0.33])[0]
        if random.random() < 0.22:
            activation = START_DATE + timedelta(days=random.randrange(DAYS - 30))
        else:
            activation = START_DATE - timedelta(days=random.randrange(30, 700))
        contract = random.choices(CONTRACT_TYPES, [0.48, 0.32, 0.20])[0]
        status = "ACTIVE" if random.random() > 0.035 else "SUSPENDED"
        row = [driver_id, activation.isoformat(), contract, vehicle_type, city_id, 0, status]
        driver_rows.append(row)
        drivers_by_city_type[(city_id, vehicle_type)].append(driver_id)
        driver_meta[driver_id] = {"city": city_id, "type": vehicle_type, "activation": activation, "status": status}

    vehicle_rows, vehicles_by_city_type, vehicle_meta = [], defaultdict(list), {}
    for number in range(1, VEHICLES + 1):
        vehicle_id = f"VEH{number:06d}"
        city_id = weighted_choice(city_weights)
        vehicle_type = random.choices(["CAR", "BIKE"], [0.70, 0.30])[0]
        model = random.choice(VEHICLE_MODELS[vehicle_type])
        activation = START_DATE - timedelta(days=random.randrange(1, 900))
        valid_until = date(2027, random.randint(1, 12), random.randint(1, 28))
        row = [vehicle_id, vehicle_type, model, city_id, activation.isoformat(), valid_until.isoformat()]
        vehicle_rows.append(row)
        vehicles_by_city_type[(city_id, vehicle_type)].append(vehicle_id)
        vehicle_meta[vehicle_id] = {"city": city_id, "type": vehicle_type, "model": model}

    station_rows, stations_by_city = [], defaultdict(list)
    for number in range(1, STATIONS + 1):
        city_id = list(CITIES)[(number - 1) % len(CITIES)]
        station_id = f"CHG{number:04d}"
        zone_id = random.choice(zones_by_city[city_id])
        connectors = random.choice([8, 12, 16, 20, 24])
        station_rows.append([station_id, city_id, zone_id, f"Trạm sạc demo {number:02d}", connectors, "2025-01-01", 1])
        stations_by_city[city_id].append(station_id)

    campaign_rows = []
    campaign_ids = []
    for number in range(1, CAMPAIGNS + 1):
        campaign_id = f"CMP{number:04d}"
        start = START_DATE + timedelta(days=random.randrange(DAYS - 35))
        end = min(START_DATE + timedelta(days=DAYS - 1), start + timedelta(days=random.randrange(14, 50)))
        funding = random.choice(["company", "partner", "shared"])
        service_id = random.choice(list(SERVICES))
        campaign_rows.append([campaign_id, f"Chiến dịch demo {number:02d}", service_id, funding, start.isoformat(), end.isoformat(), "synthetic"])
        campaign_ids.append(campaign_id)

    customer_signup = []
    customer_first_trip = [None] * CUSTOMERS
    for _ in range(CUSTOMERS):
        customer_signup.append(START_DATE - timedelta(days=random.randrange(1, 800)))

    return {
        "dates": date_rows,
        "locations": location_rows,
        "zones_by_city": zones_by_city,
        "services": service_rows,
        "drivers": driver_rows,
        "drivers_by_city_type": drivers_by_city_type,
        "driver_meta": driver_meta,
        "vehicles": vehicle_rows,
        "vehicles_by_city_type": vehicles_by_city_type,
        "vehicle_meta": vehicle_meta,
        "stations": station_rows,
        "stations_by_city": stations_by_city,
        "campaigns": campaign_rows,
        "campaign_ids": campaign_ids,
        "customer_signup": customer_signup,
        "customer_first_trip": customer_first_trip,
    }


def write_dimension_csvs(dim):
    specs = {
        "dim_date": (["date_key", "calendar_date", "year", "quarter", "month", "week_of_year", "day_of_week", "day_name_vi", "is_weekend"], dim["dates"]),
        "dim_location": (["zone_id", "city_id", "city_name", "district_id", "district_name", "zone_name", "geo_version"], dim["locations"]),
        "dim_service": (["service_id", "service_name", "service_group", "vehicle_type", "effective_from", "effective_to", "active_flag"], dim["services"]),
        "dim_driver": (["driver_id", "activation_date", "contract_type", "vehicle_type", "home_city_id", "is_test_account", "status"], dim["drivers"]),
        "dim_vehicle": (["vehicle_id", "vehicle_type", "model_code", "city_id", "activation_date", "document_valid_until"], dim["vehicles"]),
        "dim_charging_station": (["station_id", "city_id", "zone_id", "station_name", "connector_count", "effective_from", "active_flag"], dim["stations"]),
        "dim_campaign": (["campaign_id", "campaign_name", "service_id", "funding_type", "start_date", "end_date", "data_scope"], dim["campaigns"]),
    }
    for name, (headers, rows) in specs.items():
        handle, writer = open_csv(name, headers)
        writer.writerows(rows)
        handle.close()


def choose_hour() -> int:
    hours = list(range(24))
    weights = [2, 1, 1, 1, 2, 5, 10, 14, 15, 10, 7, 7, 8, 7, 7, 8, 11, 15, 16, 13, 9, 7, 5, 3]
    return random.choices(hours, weights, k=1)[0]


def choose_active(pool: list[str], meta: dict, booking_day: date) -> str:
    for _ in range(20):
        item = random.choice(pool)
        if meta[item]["activation"] <= booking_day and meta[item]["status"] == "ACTIVE":
            return item
    return random.choice(pool)


def generate_booking_facts(dim):
    booking_headers = ["booking_id", "booking_created_at", "date_key", "customer_id", "service_id", "pickup_zone_id", "dropoff_zone_id", "city_id", "booking_status", "cancel_actor", "cancel_reason", "matched_at", "driver_id", "is_test", "is_duplicate"]
    trip_headers = ["trip_id", "booking_id", "trip_started_at", "completed_at", "trip_status", "distance_km", "duration_minutes", "driver_id", "vehicle_id", "final_fare", "surcharge", "fraud_confirmed"]
    payment_headers = ["payment_id", "trip_id", "transaction_type", "payment_status", "gross_amount", "customer_discount", "company_funded_discount", "partner_funded_discount", "refund_amount", "tax_excluded_adjustment", "paid_at"]
    promo_headers = ["booking_id", "campaign_id", "voucher_code_hash", "discount_amount", "funded_by_company", "funded_by_partner", "applied_at"]
    offer_headers = ["offer_id", "booking_id", "driver_id", "service_id", "sent_at", "responded_at", "response", "recalled_within_10s", "delivery_error"]
    handles_writers = [open_csv(name, headers) for name, headers in [
        ("fact_bookings", booking_headers), ("fact_trips", trip_headers), ("fact_payments", payment_headers), ("fact_promotions", promo_headers), ("fact_driver_offers", offer_headers)
    ]]
    (booking_h, booking_w), (trip_h, trip_w), (payment_h, payment_w), (promo_h, promo_w), (offer_h, offer_w) = handles_writers

    city_weights = {key: cfg["weight"] for key, cfg in CITIES.items()}
    service_weights = {key: cfg["weight"] for key, cfg in SERVICES.items()}
    daily = defaultdict(lambda: {"bookings": 0, "completed": 0, "cancelled": 0, "gbv": 0, "net_revenue": 0, "discount": 0, "drivers": set()})
    trip_seq = payment_seq = offer_seq = 0
    for number in range(1, BOOKINGS + 1):
        booking_id = f"BKG{number:09d}"
        day_offset = random.randrange(DAYS)
        booking_day = START_DATE + timedelta(days=day_offset)
        hour = choose_hour()
        minute, second = random.randrange(60), random.randrange(60)
        created = datetime.combine(booking_day, datetime.min.time()) + timedelta(hours=hour, minutes=minute, seconds=second)
        city_id = weighted_choice(city_weights)
        service_id = weighted_choice(service_weights)
        service = SERVICES[service_id]
        vehicle_type = service["vehicle_type"]
        pickup = random.choice(dim["zones_by_city"][city_id])
        dropoff = random.choice(dim["zones_by_city"][city_id])
        customer_index = random.randrange(CUSTOMERS)
        customer_id = f"CUS{customer_index + 1:07d}"
        is_test = int(random.random() < 0.002)
        is_duplicate = int(random.random() < 0.004)
        peak = hour in (7, 8, 9, 17, 18, 19)
        cancel_prob = 0.105 + (0.035 if peak else 0) + (0.025 if service_id == "EXPRESS" else 0)
        if city_id == "HCM" and 170 <= day_offset <= 177 and peak:
            cancel_prob += 0.12

        pre_cancel = random.random() < cancel_prob * 0.58
        driver_id = matched_at = None
        cancel_actor = cancel_reason = ""
        trip_status = None
        if is_test or is_duplicate:
            booking_status = "INVALID"
        elif pre_cancel:
            booking_status = "CANCELLED"
            cancel_actor = random.choices(["CUSTOMER", "SYSTEM"], [0.62, 0.38])[0]
            cancel_reason = "no_driver_available" if cancel_actor == "SYSTEM" else random.choice(["customer_changed_mind", "wait_too_long", "payment_failure"])
        else:
            pool = dim["drivers_by_city_type"][(city_id, vehicle_type)]
            driver_id = choose_active(pool, dim["driver_meta"], booking_day)
            match_seconds = int(random.lognormvariate(4.2 if not peak else 4.7, 0.45))
            matched_at = created + timedelta(seconds=min(match_seconds, 1200))
            post_cancel = random.random() < cancel_prob * 0.42
            if post_cancel:
                booking_status = "CANCELLED"
                cancel_actor = random.choices(["CUSTOMER", "DRIVER", "SYSTEM"], [0.50, 0.42, 0.08])[0]
                cancel_reason = {"CUSTOMER": "wait_too_long", "DRIVER": "driver_requested_cancel", "SYSTEM": "vehicle_issue"}[cancel_actor]
                trip_status = "CANCELLED"
            else:
                booking_status = "COMPLETED"
                trip_status = "COMPLETED"

        booking_w.writerow([booking_id, iso(created), booking_day.strftime("%Y%m%d"), customer_id, service_id, pickup, dropoff, city_id, booking_status, cancel_actor, cancel_reason, iso(matched_at), driver_id or "", is_test, is_duplicate])

        eligible_offers = random.randint(1, 4 if driver_id else 5)
        offer_pool = dim["drivers_by_city_type"][(city_id, vehicle_type)]
        offered = set()
        for offer_no in range(eligible_offers):
            offer_seq += 1
            candidate = driver_id if driver_id and offer_no == eligible_offers - 1 else random.choice(offer_pool)
            while candidate in offered and len(offered) < len(offer_pool):
                candidate = random.choice(offer_pool)
            offered.add(candidate)
            sent = created + timedelta(seconds=offer_no * random.randint(8, 35))
            if candidate == driver_id and driver_id:
                response, responded = "ACCEPTED", sent + timedelta(seconds=random.randint(2, 18))
            else:
                response = random.choices(["REJECTED", "TIMEOUT"], [0.58, 0.42])[0]
                responded = sent + timedelta(seconds=random.randint(8, 35))
            recalled = int(random.random() < 0.015)
            delivery_error = int(random.random() < 0.008)
            offer_w.writerow([f"OFR{offer_seq:010d}", booking_id, candidate, service_id, iso(sent), iso(responded), response, recalled, delivery_error])

        daily_key = (booking_day.isoformat(), city_id, service_id)
        daily[daily_key]["bookings"] += int(not is_test and not is_duplicate)
        if booking_status == "CANCELLED":
            daily[daily_key]["cancelled"] += 1

        if trip_status is not None:
            trip_seq += 1
            trip_id = f"TRP{trip_seq:09d}"
            vehicle_id = random.choice(dim["vehicles_by_city_type"][(city_id, vehicle_type)])
            distance = round(max(0.4, random.lognormvariate(1.65 if vehicle_type == "CAR" else 1.30, 0.55)), 2)
            pickup_minutes = random.randint(3, 18) + (5 if peak else 0)
            started = matched_at + timedelta(minutes=pickup_minutes) if trip_status == "COMPLETED" else None
            duration = max(3, int(distance / (23 if vehicle_type == "CAR" else 19) * 60 + random.randint(1, 8)))
            completed = started + timedelta(minutes=duration) if started else None
            fare = money(service["base_fare"] + distance * service["per_km"] * random.uniform(0.92, 1.15)) if trip_status == "COMPLETED" else 0
            surcharge = money(random.choice([0, 0, 0, 5_000, 10_000, 15_000])) if trip_status == "COMPLETED" else 0
            fraud = int(trip_status == "COMPLETED" and random.random() < 0.003)
            trip_w.writerow([trip_id, booking_id, iso(started), iso(completed), trip_status, distance if trip_status == "COMPLETED" else 0, duration if trip_status == "COMPLETED" else 0, driver_id or "", vehicle_id, fare, surcharge, fraud])

            if trip_status == "COMPLETED":
                if dim["customer_first_trip"][customer_index] is None or completed < dim["customer_first_trip"][customer_index]:
                    dim["customer_first_trip"][customer_index] = completed
                discount = 0
                company_discount = partner_discount = 0
                if random.random() < 0.27:
                    campaign_id = random.choice(dim["campaign_ids"])
                    discount = min(money(fare * random.choice([0.10, 0.15, 0.20, 0.25])), random.choice([20_000, 30_000, 50_000]))
                    funding = random.choice(["company", "partner", "shared"])
                    company_discount = discount if funding == "company" else discount // 2 if funding == "shared" else 0
                    partner_discount = discount - company_discount
                    promo_w.writerow([booking_id, campaign_id, f"VCH{campaign_id[-2:]}-{number % 997:03d}", discount, company_discount, partner_discount, iso(created)])
                payment_seq += 1
                refund = money(fare * random.uniform(0.20, 1.0)) if random.random() < 0.035 else 0
                adjustment = random.choice([0, 0, 0, 1000, -1000])
                payment_w.writerow([f"PAY{payment_seq:010d}", trip_id, "CHARGE", "SUCCESS", fare + surcharge, discount, company_discount, partner_discount, 0, adjustment, iso(completed + timedelta(minutes=random.randint(0, 5)))])
                if refund:
                    payment_seq += 1
                    refund_at = completed + timedelta(days=random.randint(0, 10), hours=random.randint(0, 12))
                    payment_w.writerow([f"PAY{payment_seq:010d}", trip_id, "REFUND", "SUCCESS", 0, 0, 0, 0, refund, 0, iso(refund_at)])
                net_revenue = fare + surcharge - company_discount - refund - adjustment
                daily[daily_key]["completed"] += int(not fraud)
                daily[daily_key]["gbv"] += fare
                daily[daily_key]["net_revenue"] += net_revenue
                daily[daily_key]["discount"] += discount
                daily[daily_key]["drivers"].add(driver_id)

        if number % 50_000 == 0:
            print(f"bookings {number:,}/{BOOKINGS:,}", flush=True)

    for handle, _ in handles_writers:
        handle.close()

    agg_h, agg_w = open_csv("agg_daily_city_service", ["calendar_date", "city_id", "city_name", "service_id", "valid_bookings", "completed_trips", "cancelled_bookings", "gbv_vnd", "net_revenue_vnd", "discount_vnd", "active_drivers", "completion_rate", "cancellation_rate"])
    for (calendar_date, city_id, service_id), values in sorted(daily.items()):
        bookings = values["bookings"]
        agg_w.writerow([calendar_date, city_id, CITIES[city_id]["name"], service_id, bookings, values["completed"], values["cancelled"], values["gbv"], values["net_revenue"], values["discount"], len(values["drivers"]), round(values["completed"] / bookings, 6) if bookings else "", round(values["cancelled"] / bookings, 6) if bookings else ""])
    agg_h.close()


def write_customers(dim):
    handle, writer = open_csv("dim_customer", ["customer_id", "signup_date", "first_completed_trip_at", "acquisition_channel", "consent_analytics", "deleted_at"])
    for index in range(CUSTOMERS):
        first = dim["customer_first_trip"][index]
        consent = int(random.random() < 0.96)
        deleted = START_DATE + timedelta(days=random.randrange(DAYS)) if random.random() < 0.006 else None
        writer.writerow([f"CUS{index + 1:07d}", dim["customer_signup"][index].isoformat(), iso(first), random.choice(ACQUISITION_CHANNELS), consent, deleted.isoformat() if deleted else ""])
    handle.close()


def generate_online_sessions(dim):
    handle, writer = open_csv("fact_driver_online_sessions", ["session_id", "driver_id", "state", "start_at", "end_at", "city_id", "zone_id", "duration_minutes"])
    states = ["AVAILABLE", "ASSIGNED", "PICKUP", "ON_TRIP", "CHARGING", "BREAK", "OFFLINE"]
    state_weights = [0.30, 0.08, 0.10, 0.30, 0.07, 0.10, 0.05]
    drivers = list(dim["driver_meta"])
    for number in range(1, ONLINE_SESSIONS + 1):
        driver_id = random.choice(drivers)
        meta = dim["driver_meta"][driver_id]
        earliest = max(0, (meta["activation"] - START_DATE).days)
        day_offset = random.randrange(earliest, DAYS) if earliest < DAYS else DAYS - 1
        current = START_DATE + timedelta(days=day_offset)
        start = datetime.combine(current, datetime.min.time()) + timedelta(hours=choose_hour(), minutes=random.randrange(60))
        state = random.choices(states, state_weights, k=1)[0]
        duration = random.randint(10, 180 if state in ("AVAILABLE", "ON_TRIP") else 90)
        end = min(start + timedelta(minutes=duration), datetime.combine(current + timedelta(days=1), datetime.min.time()))
        writer.writerow([f"SES{number:010d}", driver_id, state, iso(start), iso(end), meta["city"], random.choice(dim["zones_by_city"][meta["city"]]), int((end - start).total_seconds() / 60)])
        if number % 100_000 == 0:
            print(f"online sessions {number:,}/{ONLINE_SESSIONS:,}", flush=True)
    handle.close()


def generate_charging_sessions(dim):
    handle, writer = open_csv("fact_charging_sessions", ["charging_session_id", "vehicle_id", "driver_id", "station_id", "station_arrival_at", "charge_start_at", "charge_end_at", "energy_kwh", "start_soc", "end_soc", "session_status"])
    vehicles = list(dim["vehicle_meta"])
    for number in range(1, CHARGING_SESSIONS + 1):
        vehicle_id = random.choice(vehicles)
        vmeta = dim["vehicle_meta"][vehicle_id]
        city_id = vmeta["city"]
        current = START_DATE + timedelta(days=random.randrange(DAYS))
        arrival = datetime.combine(current, datetime.min.time()) + timedelta(hours=random.randrange(24), minutes=random.randrange(60))
        day_offset = (current - START_DATE).days
        queue_minutes = random.randint(0, 25)
        if city_id == "DAD" and 120 <= day_offset <= 135:
            queue_minutes += random.randint(20, 55)
        start = arrival + timedelta(minutes=queue_minutes)
        open_session = random.random() < 0.008
        charge_minutes = random.randint(25, 95)
        end = None if open_session else start + timedelta(minutes=charge_minutes)
        start_soc = random.randint(8, 45)
        end_soc = "" if open_session else min(100, start_soc + random.randint(35, 78))
        energy = "" if open_session else round((int(end_soc) - start_soc) * (0.65 if vmeta["type"] == "CAR" else 0.08), 2)
        driver_id = random.choice(dim["drivers_by_city_type"][(city_id, vmeta["type"])])
        writer.writerow([f"CHS{number:09d}", vehicle_id, driver_id, random.choice(dim["stations_by_city"][city_id]), iso(arrival), iso(start), iso(end), energy, start_soc, end_soc, "OPEN" if open_session else "COMPLETED"])
    handle.close()


def generate_vehicle_status(dim):
    handle, writer = open_csv("fact_vehicle_status", ["snapshot_id", "snapshot_at", "date_key", "vehicle_id", "city_id", "state", "battery_soc", "document_valid", "service_id"])
    vehicle_items = list(dim["vehicle_meta"].items())
    number = 0
    for day_offset in range(DAYS):
        current = START_DATE + timedelta(days=day_offset)
        snapshot_at = datetime.combine(current, datetime.min.time()) + timedelta(hours=6 + day_offset % 4 * 4)
        for vehicle_id, meta in vehicle_items:
            number += 1
            state = random.choices(["AVAILABLE", "IN_SERVICE", "CHARGING", "MAINTENANCE", "RESERVED", "INACTIVE"], [0.34, 0.43, 0.10, 0.055, 0.045, 0.03], k=1)[0]
            soc = random.randint(8, 100) if state != "CHARGING" else random.randint(5, 75)
            compatible = [sid for sid, cfg in SERVICES.items() if cfg["vehicle_type"] == meta["type"]]
            writer.writerow([f"VSS{number:010d}", iso(snapshot_at), current.strftime("%Y%m%d"), vehicle_id, meta["city"], state, soc, 1, random.choice(compatible)])
        if (day_offset + 1) % 60 == 0:
            print(f"vehicle snapshots day {day_offset + 1}/{DAYS}", flush=True)
    handle.close()


TABLE_SCHEMAS = {
    "dim_date": "date_key TEXT PRIMARY KEY, calendar_date TEXT, year INTEGER, quarter INTEGER, month INTEGER, week_of_year INTEGER, day_of_week INTEGER, day_name_vi TEXT, is_weekend INTEGER",
    "dim_location": "zone_id TEXT PRIMARY KEY, city_id TEXT, city_name TEXT, district_id TEXT, district_name TEXT, zone_name TEXT, geo_version TEXT",
    "dim_service": "service_id TEXT PRIMARY KEY, service_name TEXT, service_group TEXT, vehicle_type TEXT, effective_from TEXT, effective_to TEXT, active_flag INTEGER",
    "dim_driver": "driver_id TEXT PRIMARY KEY, activation_date TEXT, contract_type TEXT, vehicle_type TEXT, home_city_id TEXT, is_test_account INTEGER, status TEXT",
    "dim_customer": "customer_id TEXT PRIMARY KEY, signup_date TEXT, first_completed_trip_at TEXT, acquisition_channel TEXT, consent_analytics INTEGER, deleted_at TEXT",
    "dim_vehicle": "vehicle_id TEXT PRIMARY KEY, vehicle_type TEXT, model_code TEXT, city_id TEXT, activation_date TEXT, document_valid_until TEXT",
    "dim_charging_station": "station_id TEXT PRIMARY KEY, city_id TEXT, zone_id TEXT, station_name TEXT, connector_count INTEGER, effective_from TEXT, active_flag INTEGER",
    "dim_campaign": "campaign_id TEXT PRIMARY KEY, campaign_name TEXT, service_id TEXT, funding_type TEXT, start_date TEXT, end_date TEXT, data_scope TEXT",
    "fact_bookings": "booking_id TEXT PRIMARY KEY, booking_created_at TEXT, date_key TEXT, customer_id TEXT, service_id TEXT, pickup_zone_id TEXT, dropoff_zone_id TEXT, city_id TEXT, booking_status TEXT, cancel_actor TEXT, cancel_reason TEXT, matched_at TEXT, driver_id TEXT, is_test INTEGER, is_duplicate INTEGER",
    "fact_trips": "trip_id TEXT PRIMARY KEY, booking_id TEXT, trip_started_at TEXT, completed_at TEXT, trip_status TEXT, distance_km REAL, duration_minutes INTEGER, driver_id TEXT, vehicle_id TEXT, final_fare INTEGER, surcharge INTEGER, fraud_confirmed INTEGER",
    "fact_payments": "payment_id TEXT PRIMARY KEY, trip_id TEXT, transaction_type TEXT, payment_status TEXT, gross_amount INTEGER, customer_discount INTEGER, company_funded_discount INTEGER, partner_funded_discount INTEGER, refund_amount INTEGER, tax_excluded_adjustment INTEGER, paid_at TEXT",
    "fact_promotions": "booking_id TEXT, campaign_id TEXT, voucher_code_hash TEXT, discount_amount INTEGER, funded_by_company INTEGER, funded_by_partner INTEGER, applied_at TEXT",
    "fact_driver_offers": "offer_id TEXT PRIMARY KEY, booking_id TEXT, driver_id TEXT, service_id TEXT, sent_at TEXT, responded_at TEXT, response TEXT, recalled_within_10s INTEGER, delivery_error INTEGER",
    "fact_driver_online_sessions": "session_id TEXT PRIMARY KEY, driver_id TEXT, state TEXT, start_at TEXT, end_at TEXT, city_id TEXT, zone_id TEXT, duration_minutes INTEGER",
    "fact_charging_sessions": "charging_session_id TEXT PRIMARY KEY, vehicle_id TEXT, driver_id TEXT, station_id TEXT, station_arrival_at TEXT, charge_start_at TEXT, charge_end_at TEXT, energy_kwh REAL, start_soc INTEGER, end_soc INTEGER, session_status TEXT",
    "fact_vehicle_status": "snapshot_id TEXT PRIMARY KEY, snapshot_at TEXT, date_key TEXT, vehicle_id TEXT, city_id TEXT, state TEXT, battery_soc INTEGER, document_valid INTEGER, service_id TEXT",
    "agg_daily_city_service": "calendar_date TEXT, city_id TEXT, city_name TEXT, service_id TEXT, valid_bookings INTEGER, completed_trips INTEGER, cancelled_bookings INTEGER, gbv_vnd INTEGER, net_revenue_vnd INTEGER, discount_vnd INTEGER, active_drivers INTEGER, completion_rate REAL, cancellation_rate REAL",
}


def build_sqlite():
    if DB_PATH.exists():
        DB_PATH.unlink()
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=OFF")
    conn.execute("PRAGMA synchronous=OFF")
    conn.execute("PRAGMA temp_store=MEMORY")
    for table, schema in TABLE_SCHEMAS.items():
        conn.execute(f"CREATE TABLE {table} ({schema})")
        path = CSV_DIR / f"{table}.csv"
        with path.open(encoding="utf-8", newline="") as handle:
            reader = csv.reader(handle)
            headers = next(reader)
            placeholders = ",".join("?" for _ in headers)
            batch = []
            for row in reader:
                batch.append([None if value == "" else value for value in row])
                if len(batch) == 10_000:
                    conn.executemany(f"INSERT INTO {table} VALUES ({placeholders})", batch)
                    batch.clear()
            if batch:
                conn.executemany(f"INSERT INTO {table} VALUES ({placeholders})", batch)
        print(f"sqlite loaded {table}", flush=True)
    indexes = [
        "CREATE INDEX idx_booking_date_city_service ON fact_bookings(date_key, city_id, service_id)",
        "CREATE INDEX idx_booking_customer ON fact_bookings(customer_id)",
        "CREATE INDEX idx_trip_booking ON fact_trips(booking_id)",
        "CREATE INDEX idx_trip_driver ON fact_trips(driver_id)",
        "CREATE INDEX idx_payment_trip ON fact_payments(trip_id)",
        "CREATE INDEX idx_offer_booking ON fact_driver_offers(booking_id)",
        "CREATE INDEX idx_online_driver_time ON fact_driver_online_sessions(driver_id, start_at)",
        "CREATE INDEX idx_charge_station_time ON fact_charging_sessions(station_id, station_arrival_at)",
        "CREATE INDEX idx_vehicle_status_date ON fact_vehicle_status(date_key, city_id, state)",
    ]
    for statement in indexes:
        conn.execute(statement)
    conn.executescript((SQL_DIR / "views.sql").read_text(encoding="utf-8"))
    conn.execute("ANALYZE")
    conn.commit()
    conn.close()


def write_sql_files():
    schema_lines = ["-- Synthetic mock warehouse schema. All identifiers and values are fictional."]
    for table, schema in TABLE_SCHEMAS.items():
        schema_lines.append(f"CREATE TABLE {table} ({schema});")
    (SQL_DIR / "schema.sql").write_text("\n\n".join(schema_lines) + "\n", encoding="utf-8")
    views = """-- Metrics aligned with the retrieval metric catalog.
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
"""
    (SQL_DIR / "views.sql").write_text(views, encoding="utf-8")


def write_manifest():
    counts = {}
    for path in sorted(CSV_DIR.glob("*.csv")):
        with path.open(encoding="utf-8") as handle:
            counts[path.stem] = sum(1 for _ in handle) - 1
    manifest = {
        "name": "xanhsm-mock-warehouse",
        "version": "1.0.0",
        "generated_at": "2026-09-21",
        "seed": SEED,
        "period": {"start": START_DATE.isoformat(), "days": DAYS, "end": (START_DATE + timedelta(days=DAYS - 1)).isoformat()},
        "data_scope": "synthetic-demo-only",
        "warning": "No record represents a real Xanh SM customer, driver, vehicle, station, policy, or business result.",
        "row_counts": counts,
        "total_rows": sum(counts.values()),
        "primary_database": DB_PATH.name,
    }
    (ROOT / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(manifest, ensure_ascii=False, indent=2), flush=True)


def write_quality_report():
    conn = sqlite3.connect(DB_PATH)
    checks = {
        "sqlite_integrity": conn.execute("PRAGMA integrity_check").fetchone()[0],
        "orphan_booking_customer": conn.execute("SELECT COUNT(*) FROM fact_bookings b LEFT JOIN dim_customer c ON c.customer_id=b.customer_id WHERE c.customer_id IS NULL").fetchone()[0],
        "orphan_booking_service": conn.execute("SELECT COUNT(*) FROM fact_bookings b LEFT JOIN dim_service s ON s.service_id=b.service_id WHERE s.service_id IS NULL").fetchone()[0],
        "orphan_trip_booking": conn.execute("SELECT COUNT(*) FROM fact_trips t LEFT JOIN fact_bookings b ON b.booking_id=t.booking_id WHERE b.booking_id IS NULL").fetchone()[0],
        "orphan_trip_driver": conn.execute("SELECT COUNT(*) FROM fact_trips t LEFT JOIN dim_driver d ON d.driver_id=t.driver_id WHERE t.driver_id IS NOT NULL AND d.driver_id IS NULL").fetchone()[0],
        "orphan_trip_vehicle": conn.execute("SELECT COUNT(*) FROM fact_trips t LEFT JOIN dim_vehicle v ON v.vehicle_id=t.vehicle_id WHERE v.vehicle_id IS NULL").fetchone()[0],
        "orphan_payment_trip": conn.execute("SELECT COUNT(*) FROM fact_payments p LEFT JOIN fact_trips t ON t.trip_id=p.trip_id WHERE t.trip_id IS NULL").fetchone()[0],
        "orphan_offer_booking": conn.execute("SELECT COUNT(*) FROM fact_driver_offers o LEFT JOIN fact_bookings b ON b.booking_id=o.booking_id WHERE b.booking_id IS NULL").fetchone()[0],
        "valid_bookings": conn.execute("SELECT COUNT(*) FROM fact_bookings WHERE is_test=0 AND is_duplicate=0").fetchone()[0],
        "completed_trips": conn.execute("SELECT COUNT(*) FROM vw_completed_trips").fetchone()[0],
    }
    conn.close()
    report = {
        "generated_at": "2026-09-21",
        "status": "pass" if checks["sqlite_integrity"] == "ok" and not any(value for key, value in checks.items() if key.startswith("orphan_")) else "fail",
        "checks": checks,
    }
    (ROOT / "quality_report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main():
    CSV_DIR.mkdir(parents=True, exist_ok=True)
    SQL_DIR.mkdir(parents=True, exist_ok=True)
    dim = build_dimensions()
    write_dimension_csvs(dim)
    generate_booking_facts(dim)
    write_customers(dim)
    generate_online_sessions(dim)
    generate_charging_sessions(dim)
    generate_vehicle_status(dim)
    write_sql_files()
    build_sqlite()
    write_manifest()
    write_quality_report()


if __name__ == "__main__":
    main()
