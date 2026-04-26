#!/usr/bin/env python3
import argparse
import json
import math
import os
import random
import re
import sqlite3
import sys
import threading
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

try:
    import serial  # type: ignore
except ImportError:
    serial = None


BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
DB_PATH = BASE_DIR / "data" / "power_monitor.sqlite3"


def optional_float_env(name: str) -> float | None:
    raw = os.getenv(name, "").strip()
    if not raw:
        return None
    return float(raw)


def optional_int_env(name: str) -> int | None:
    raw = os.getenv(name, "").strip()
    if not raw:
        return None
    return int(raw)


@dataclass
class Settings:
    serial_port: str = os.getenv("POWER_MONITOR_SERIAL_PORT", "/dev/ttyUSB0")
    baud_rate: int = int(os.getenv("POWER_MONITOR_BAUD_RATE", "2400"))
    poll_seconds: int = int(os.getenv("POWER_MONITOR_POLL_SECONDS", "5"))
    host: str = os.getenv("POWER_MONITOR_HOST", "0.0.0.0")
    port: int = int(os.getenv("POWER_MONITOR_PORT", "8080"))
    mock_mode: bool = os.getenv("POWER_MONITOR_MOCK", "0").lower() in {"1", "true", "yes"}
    serial_timeout: float = float(os.getenv("POWER_MONITOR_SERIAL_TIMEOUT", "2.5"))
    low_battery_percent: int = int(os.getenv("POWER_MONITOR_LOW_BATTERY_PERCENT", "25"))
    high_load_watts: int = int(os.getenv("POWER_MONITOR_HIGH_LOAD_WATTS", "4000"))
    alert_cooldown_minutes: int = int(os.getenv("POWER_MONITOR_ALERT_COOLDOWN_MINUTES", "20"))
    ntfy_topic_url: str = os.getenv("POWER_MONITOR_NTFY_TOPIC_URL", "").strip()
    forecast_latitude: float | None = optional_float_env("POWER_MONITOR_FORECAST_LATITUDE")
    forecast_longitude: float | None = optional_float_env("POWER_MONITOR_FORECAST_LONGITUDE")
    forecast_check_hours: int = int(os.getenv("POWER_MONITOR_FORECAST_CHECK_HOURS", "6"))
    forecast_cloud_threshold_percent: int = int(os.getenv("POWER_MONITOR_FORECAST_CLOUD_THRESHOLD_PERCENT", "70"))
    forecast_evening_advisory_hour: int = int(os.getenv("POWER_MONITOR_FORECAST_ADVISORY_HOUR", "17"))
    forecast_reserve_battery_percent: int = int(os.getenv("POWER_MONITOR_FORECAST_RESERVE_BATTERY_PERCENT", "70"))
    # Battery state-of-charge estimator settings
    battery_count: int = int(os.getenv("POWER_MONITOR_BATTERY_COUNT", "0"))
    capacity_per_battery_kwh: float = float(os.getenv("POWER_MONITOR_CAPACITY_PER_BATTERY_KWH", "0"))
    usable_capacity_percent: int = int(os.getenv("POWER_MONITOR_USABLE_CAPACITY_PERCENT", "100"))
    battery_estimate_enabled: bool = os.getenv("POWER_MONITOR_BATTERY_ESTIMATE_ENABLED", "0").lower() in {"1", "true", "yes"}
    low_estimated_battery_percent: int | None = optional_int_env("POWER_MONITOR_LOW_ESTIMATED_BATTERY_PERCENT")


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def isoformat(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


class InverterProtocolError(RuntimeError):
    pass


class InverterClient:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def poll(self) -> dict[str, Any]:
        if self.settings.mock_mode:
            return self._mock_sample()
        if serial is None:
            raise RuntimeError("pyserial is not installed. Run: pip install -r requirements.txt")

        with serial.Serial(
            self.settings.serial_port,
            baudrate=self.settings.baud_rate,
            bytesize=8,
            parity="N",
            stopbits=1,
            timeout=self.settings.serial_timeout,
        ) as connection:
            connection.reset_input_buffer()
            connection.write(b"QPIGS\r")
            connection.flush()
            raw_bytes = connection.read_until(b"\r")

        if not raw_bytes:
            raise InverterProtocolError("No response from inverter")

        raw_response = raw_bytes.decode("ascii", errors="ignore").strip()
        if "NAK" in raw_response:
            raise InverterProtocolError(f"Inverter rejected QPIGS command: {raw_response}")
        return parse_qpigs(raw_response)

    def _mock_sample(self) -> dict[str, Any]:
        base_load = 1100 + 250 * (1 + math.sin(time.time() / 90))
        pv_power = max(0, 1800 * (1 + math.sin(time.time() / 240 - 1.2)) / 2)
        battery_voltage = 51.2 + random.uniform(-0.6, 0.6)
        battery_capacity = min(100, max(20, int((battery_voltage - 47.5) * 22)))
        sample = {
            "grid_voltage_v": 120.1,
            "grid_frequency_hz": 60.0,
            "output_voltage_v": 120.0,
            "output_frequency_hz": 60.0,
            "output_apparent_power_va": int(base_load * 1.03),
            "output_active_power_w": int(base_load),
            "load_percent": min(100, round(base_load / 65, 1)),
            "bus_voltage_v": 385,
            "battery_voltage_v": round(battery_voltage, 2),
            "battery_charging_current_a": round(max(0, pv_power / max(battery_voltage, 1)) * 0.8, 1),
            "battery_capacity_percent": battery_capacity,
            "inverter_temp_c": round(30 + random.uniform(-1, 5), 1),
            "pv_input_current_a": round(pv_power / max(160, 1), 1),
            "pv_input_voltage_v": round(160 + random.uniform(-8, 8), 1),
            "battery_voltage_scc_v": round(battery_voltage + random.uniform(0.0, 0.5), 2),
            "battery_discharge_current_a": round(max(0, base_load - pv_power) / max(battery_voltage, 1), 1),
            "device_status_bits": "00010110",
            "pv_input_power_w": int(pv_power),
            "raw_response": "MOCK",
        }
        return sample


def parse_qpigs(raw_response: str) -> dict[str, Any]:
    payload = raw_response.strip()
    if payload.startswith("("):
        payload = payload[1:]
    payload = payload.split("\r", 1)[0]
    tokens = re.findall(r"[A-Za-z0-9.\-]+", payload)

    if len(tokens) < 16:
        raise InverterProtocolError(f"Unexpected QPIGS response: {raw_response}")

    def to_float(index: int) -> float | None:
        try:
            return float(tokens[index])
        except (IndexError, ValueError):
            return None

    def to_int(index: int) -> int | None:
        value = to_float(index)
        return None if value is None else int(round(value))

    sample = {
        "grid_voltage_v": to_float(0),
        "grid_frequency_hz": to_float(1),
        "output_voltage_v": to_float(2),
        "output_frequency_hz": to_float(3),
        "output_apparent_power_va": to_int(4),
        "output_active_power_w": to_int(5),
        "load_percent": to_float(6),
        "bus_voltage_v": to_int(7),
        "battery_voltage_v": to_float(8),
        "battery_charging_current_a": to_float(9),
        "battery_capacity_percent": to_int(10),
        "inverter_temp_c": to_float(11),
        "pv_input_current_a": to_float(12),
        "pv_input_voltage_v": to_float(13),
        "battery_voltage_scc_v": to_float(14),
        "battery_discharge_current_a": to_float(15),
        "device_status_bits": tokens[16] if len(tokens) > 16 else None,
        "pv_input_power_w": None,
        "raw_response": raw_response,
    }

    if sample["pv_input_current_a"] is not None and sample["pv_input_voltage_v"] is not None:
        sample["pv_input_power_w"] = int(
            round(sample["pv_input_current_a"] * sample["pv_input_voltage_v"])
        )

    return sample


class BatterySOCEstimator:
    """Estimates battery state-of-charge (SOC) percentage based on energy flow.

    The estimator tracks net energy (load watts minus input/solar watts) across
    successive poll readings and adjusts a running battery percentage estimate
    accordingly.  It resets to 100 % whenever the inverter itself reports a
    fully-charged battery, providing a natural calibration point.

    Calculation overview (per poll interval):
        netWatts      = loadWatts - inputWatts
        elapsedHours  = elapsedSeconds / 3600
        netKwh        = netWatts * elapsedHours / 1000
        estimatedPct -= (netKwh / usableCapacityKwh) * 100

    Positive netWatts → load exceeds input → battery is draining.
    Negative netWatts → input exceeds load  → battery is charging.
    """

    def __init__(self) -> None:
        self._estimated_percent: float | None = None
        self._last_ts: datetime | None = None

    def update(
        self,
        ts_utc: datetime,
        inverter_battery_percent: int | None,
        load_watts: float | None,
        input_watts: float | None,
        battery_count: int,
        capacity_per_battery_kwh: float,
        usable_capacity_percent: int,
    ) -> float | None:
        """Update and return the estimated battery percentage.

        Returns None when the estimate is not yet seeded (first reading or
        no inverter reading available).  The returned value is clamped to
        [0, 100].

        Args:
            ts_utc: UTC timestamp for the current reading.
            inverter_battery_percent: Inverter-reported battery percentage.
            load_watts: Current output / load power in watts.
            input_watts: Current solar / charging input power in watts.
            battery_count: Number of batteries in the bank (must be > 0).
            capacity_per_battery_kwh: Nameplate capacity of each battery (kWh).
            usable_capacity_percent: Fraction of total capacity considered
                usable, expressed as a percentage (1–100).
        """
        # When the inverter reports a full charge, reset the running estimate
        # and record this moment as the new time baseline.
        if inverter_battery_percent == 100:
            self._estimated_percent = 100.0
            self._last_ts = ts_utc
            return self._estimated_percent

        # On the very first reading there is no elapsed interval to compute.
        # Seed the estimate from the inverter reading (if available) so the
        # display is immediately useful, then wait for the next reading.
        if self._last_ts is None:
            self._last_ts = ts_utc
            if inverter_battery_percent is not None:
                self._estimated_percent = float(inverter_battery_percent)
            return self._estimated_percent

        # If we still have no estimate (inverter reading was unavailable on
        # the first call), try to seed from the current inverter reading.
        if self._estimated_percent is None:
            if inverter_battery_percent is not None:
                self._estimated_percent = float(inverter_battery_percent)
            self._last_ts = ts_utc
            return self._estimated_percent

        # Validate required settings before calculating.
        if (
            battery_count <= 0
            or capacity_per_battery_kwh <= 0
            or not (0 < usable_capacity_percent <= 100)
        ):
            self._last_ts = ts_utc
            return self._estimated_percent

        # Both power readings must be present for a meaningful calculation.
        if load_watts is None or input_watts is None:
            self._last_ts = ts_utc
            return self._estimated_percent

        # Total and usable capacity in kWh.
        total_capacity_kwh = battery_count * capacity_per_battery_kwh
        usable_capacity_kwh = total_capacity_kwh * (usable_capacity_percent / 100.0)
        if usable_capacity_kwh <= 0:
            self._last_ts = ts_utc
            return self._estimated_percent

        # Net watts: positive when draining battery, negative when charging.
        net_watts = load_watts - input_watts

        # Elapsed time in hours since the last reading.
        elapsed_seconds = (ts_utc - self._last_ts).total_seconds()
        elapsed_hours = elapsed_seconds / 3600.0

        # Net energy transferred during this interval (kWh).
        net_kwh = net_watts * elapsed_hours / 1000.0

        # Adjust the running estimate and clamp to the valid [0, 100] range.
        self._estimated_percent -= (net_kwh / usable_capacity_kwh) * 100.0
        self._estimated_percent = max(0.0, min(100.0, self._estimated_percent))
        self._last_ts = ts_utc
        return self._estimated_percent

    def reset(self) -> None:
        """Clear the current estimate and timestamp, forcing re-initialisation."""
        self._estimated_percent = None
        self._last_ts = None


class Database:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS samples (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts_utc TEXT NOT NULL,
                    grid_voltage_v REAL,
                    grid_frequency_hz REAL,
                    output_voltage_v REAL,
                    output_frequency_hz REAL,
                    output_apparent_power_va INTEGER,
                    output_active_power_w INTEGER,
                    load_percent REAL,
                    bus_voltage_v INTEGER,
                    battery_voltage_v REAL,
                    battery_charging_current_a REAL,
                    battery_capacity_percent INTEGER,
                    inverter_temp_c REAL,
                    pv_input_current_a REAL,
                    pv_input_voltage_v REAL,
                    battery_voltage_scc_v REAL,
                    battery_discharge_current_a REAL,
                    device_status_bits TEXT,
                    pv_input_power_w INTEGER,
                    raw_response TEXT
                )
                """
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_samples_ts_utc ON samples(ts_utc)"
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS alerts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts_utc TEXT NOT NULL,
                    level TEXT NOT NULL,
                    code TEXT NOT NULL,
                    title TEXT NOT NULL,
                    message TEXT NOT NULL,
                    sample_json TEXT,
                    delivered INTEGER NOT NULL DEFAULT 0
                )
                """
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_alerts_ts_utc ON alerts(ts_utc DESC)"
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS app_config (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                )
                """
            )

    def insert_sample(self, ts_utc: datetime, sample: dict[str, Any]) -> None:
        record = {"ts_utc": isoformat(ts_utc), **sample}
        columns = ", ".join(record.keys())
        placeholders = ", ".join("?" for _ in record)
        with self._lock, self._connect() as connection:
            connection.execute(
                f"INSERT INTO samples ({columns}) VALUES ({placeholders})",
                tuple(record.values()),
            )

    def fetch_history(self, hours: int) -> list[dict[str, Any]]:
        cutoff = isoformat(utc_now() - timedelta(hours=hours))
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                """
                SELECT ts_utc, output_active_power_w, pv_input_power_w,
                       battery_voltage_v, battery_capacity_percent, load_percent
                FROM samples
                WHERE ts_utc >= ?
                ORDER BY ts_utc ASC
                """,
                (cutoff,),
            ).fetchall()
        return [dict(row) for row in rows]

    def prune(self, days_to_keep: int = 30) -> int:
        cutoff = isoformat(utc_now() - timedelta(days=days_to_keep))
        with self._lock, self._connect() as connection:
            cursor = connection.execute("DELETE FROM samples WHERE ts_utc < ?", (cutoff,))
            return cursor.rowcount

    def insert_alert(
        self,
        ts_utc: datetime,
        level: str,
        code: str,
        title: str,
        message: str,
        sample: dict[str, Any] | None,
        delivered: bool,
    ) -> None:
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                INSERT INTO alerts (ts_utc, level, code, title, message, sample_json, delivered)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    isoformat(ts_utc),
                    level,
                    code,
                    title,
                    message,
                    json.dumps(sample) if sample is not None else None,
                    1 if delivered else 0,
                ),
            )

    def fetch_alerts(self, limit: int = 50) -> list[dict[str, Any]]:
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                """
                SELECT ts_utc, level, code, title, message, sample_json, delivered
                FROM alerts
                ORDER BY ts_utc DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        alerts = []
        for row in rows:
            item = dict(row)
            item["sample"] = json.loads(item.pop("sample_json")) if item["sample_json"] else None
            item["delivered"] = bool(item["delivered"])
            alerts.append(item)
        return alerts

    def fetch_config(self, key: str) -> str | None:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT value FROM app_config WHERE key = ?",
                (key,),
            ).fetchone()
        return None if row is None else str(row["value"])

    def upsert_config(self, key: str, value: str) -> None:
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                INSERT INTO app_config (key, value)
                VALUES (?, ?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value
                """,
                (key, value),
            )


class AlertManager:
    def __init__(self, settings: Settings, database: Database) -> None:
        self.settings = settings
        self.database = database
        self._last_sent: dict[str, datetime] = {}
        self._next_forecast_check_at: datetime | None = None
        self._forecast_alerted_for_date: str | None = None
        self._lock = threading.Lock()

    def evaluate_sample(self, ts_utc: datetime, sample: dict[str, Any]) -> None:
        battery_capacity = sample.get("battery_capacity_percent")
        load_watts = sample.get("output_active_power_w")
        solar_watts = sample.get("pv_input_power_w")
        battery_voltage = sample.get("battery_voltage_v")
        battery_discharge_current = sample.get("battery_discharge_current_a")

        if battery_capacity is not None and battery_capacity <= self.settings.low_battery_percent:
            self._emit(
                ts_utc,
                "warning",
                "low_battery",
                "Battery is low",
                f"Battery capacity is at {battery_capacity}% (threshold {self.settings.low_battery_percent}%).",
                sample,
            )

        if load_watts is not None and load_watts >= self.settings.high_load_watts:
            self._emit(
                ts_utc,
                "warning",
                "high_load",
                "Load is running high",
                f"Load is {load_watts} W (threshold {self.settings.high_load_watts} W).",
                sample,
            )

        if (
            load_watts is not None
            and solar_watts is not None
            and battery_voltage is not None
            and battery_discharge_current is not None
            and load_watts > solar_watts
        ):
            estimated_battery_watts = battery_voltage * battery_discharge_current
            if estimated_battery_watts >= 500:
                self._emit(
                    ts_utc,
                    "info",
                    "battery_supporting_load",
                    "Battery is covering a solar shortfall",
                    (
                        f"Solar is {solar_watts} W while load is {load_watts} W. "
                        f"Estimated battery output is {int(round(estimated_battery_watts))} W."
                    ),
                    sample,
                )

        # Alert when the estimated battery percentage falls below the configured threshold.
        estimated_pct = sample.get("estimated_battery_percent")
        if (
            self.settings.battery_estimate_enabled
            and self.settings.low_estimated_battery_percent is not None
            and estimated_pct is not None
            and estimated_pct <= self.settings.low_estimated_battery_percent
        ):
            self._emit(
                ts_utc,
                "warning",
                "low_estimated_battery",
                "Estimated battery is low",
                (
                    f"Estimated battery capacity is at {estimated_pct:.1f}% "
                    f"(threshold {self.settings.low_estimated_battery_percent}%)."
                ),
                sample,
            )

    def notify_error(self, ts_utc: datetime, error_message: str) -> None:
        self._emit(
            ts_utc,
            "critical",
            "inverter_error",
            "Inverter communication problem",
            error_message,
            None,
        )

    def send_test_notification(self, ts_utc: datetime) -> bool:
        title = "Power monitor test notification"
        message = "Test notification from EG4 Power Monitor. If you received this, push delivery is working."
        delivered = self._deliver_notification(title, message)
        self.database.insert_alert(
            ts_utc,
            "info",
            "test_notification",
            title,
            message,
            None,
            delivered,
        )
        return delivered

    def evaluate_forecast_advisory(self, ts_utc: datetime, sample: dict[str, Any]) -> None:
        if self.settings.forecast_latitude is None or self.settings.forecast_longitude is None:
            return
        if self._next_forecast_check_at is not None and ts_utc < self._next_forecast_check_at:
            return

        self._next_forecast_check_at = ts_utc + timedelta(hours=max(1, self.settings.forecast_check_hours))

        local_now = ts_utc.astimezone()
        if local_now.hour < self.settings.forecast_evening_advisory_hour:
            return

        forecast = self._fetch_tomorrow_daylight_cloud(local_now)
        if forecast is None:
            return
        tomorrow_key, avg_cloud_percent = forecast
        if avg_cloud_percent < self.settings.forecast_cloud_threshold_percent:
            return
        if self._forecast_alerted_for_date == tomorrow_key:
            return

        battery_capacity = sample.get("battery_capacity_percent")
        if battery_capacity is not None and battery_capacity >= self.settings.forecast_reserve_battery_percent:
            return

        battery_text = (
            f"Battery is at {battery_capacity}%."
            if battery_capacity is not None
            else "Battery percentage is unavailable."
        )
        self._emit(
            ts_utc,
            "info",
            "forecast_reserve_battery",
            "Cloudy forecast tomorrow: reserve battery tonight",
            (
                f"Tomorrow daylight cloud cover is forecast around {int(round(avg_cloud_percent))}%. "
                f"{battery_text} Consider reducing evening loads to preserve reserve."
            ),
            sample,
        )
        self._forecast_alerted_for_date = tomorrow_key

    def _fetch_tomorrow_daylight_cloud(self, local_now: datetime) -> tuple[str, float] | None:
        latitude = self.settings.forecast_latitude
        longitude = self.settings.forecast_longitude
        if latitude is None or longitude is None:
            return None

        url = (
            "https://api.open-meteo.com/v1/forecast"
            f"?latitude={latitude}"
            f"&longitude={longitude}"
            "&hourly=cloud_cover,is_day"
            "&timezone=auto"
            "&forecast_days=3"
        )
        request = urllib.request.Request(url, method="GET")
        try:
            with urllib.request.urlopen(request, timeout=8) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError):
            return None

        hourly = payload.get("hourly", {})
        times = hourly.get("time") or []
        clouds = hourly.get("cloud_cover") or []
        is_day = hourly.get("is_day") or []
        if not times or len(times) != len(clouds) or len(times) != len(is_day):
            return None

        tomorrow = (local_now.date() + timedelta(days=1)).isoformat()
        daylight_clouds = []
        for index, ts in enumerate(times):
            if not isinstance(ts, str):
                continue
            if not ts.startswith(tomorrow):
                continue
            if int(is_day[index]) != 1:
                continue
            daylight_clouds.append(float(clouds[index]))

        if not daylight_clouds:
            return None
        return tomorrow, sum(daylight_clouds) / len(daylight_clouds)

    def _emit(
        self,
        ts_utc: datetime,
        level: str,
        code: str,
        title: str,
        message: str,
        sample: dict[str, Any] | None,
    ) -> None:
        with self._lock:
            last_sent = self._last_sent.get(code)
            if last_sent is not None and ts_utc - last_sent < timedelta(minutes=self.settings.alert_cooldown_minutes):
                return
            delivered = self._deliver_notification(title, message)
            self.database.insert_alert(ts_utc, level, code, title, message, sample, delivered)
            self._last_sent[code] = ts_utc

    def _deliver_notification(self, title: str, message: str) -> bool:
        if not self.settings.ntfy_topic_url:
            return False
        request = urllib.request.Request(
            self.settings.ntfy_topic_url,
            data=message.encode("utf-8"),
            method="POST",
            headers={"Title": title},
        )
        try:
            with urllib.request.urlopen(request, timeout=5):
                return True
        except (urllib.error.URLError, TimeoutError):
            return False


class PowerMonitorState:
    def __init__(self, settings: Settings, database: Database) -> None:
        self.settings = settings
        self.database = database
        self._load_persisted_alert_settings()
        self.client = InverterClient(settings)
        self.alert_manager = AlertManager(settings, database)
        self.soc_estimator = BatterySOCEstimator()
        self.lock = threading.Lock()
        self.latest: dict[str, Any] | None = None
        self.last_error: str | None = None
        self.last_success_at: str | None = None
        self.started_at = isoformat(utc_now())
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run_loop, name="poller", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=2)

    def _run_loop(self) -> None:
        while not self._stop_event.is_set():
            timestamp = utc_now()
            try:
                sample = self.client.poll()
                self.database.insert_sample(timestamp, sample)

                # Compute estimated battery percentage (not persisted to DB).
                estimated_pct: float | None = None
                if self.settings.battery_estimate_enabled:
                    estimated_pct = self.soc_estimator.update(
                        timestamp,
                        sample.get("battery_capacity_percent"),
                        sample.get("output_active_power_w"),
                        sample.get("pv_input_power_w"),
                        self.settings.battery_count,
                        self.settings.capacity_per_battery_kwh,
                        self.settings.usable_capacity_percent,
                    )

                # Build an enriched sample that includes the estimated SOC so
                # that alerts and the live payload see a consistent value.
                enriched_sample = {
                    **sample,
                    "estimated_battery_percent": (
                        round(estimated_pct, 1) if estimated_pct is not None else None
                    ),
                }

                self.alert_manager.evaluate_sample(timestamp, enriched_sample)
                self.alert_manager.evaluate_forecast_advisory(timestamp, enriched_sample)
                self.database.prune()
                with self.lock:
                    self.latest = {"ts_utc": isoformat(timestamp), **enriched_sample}
                    self.last_success_at = isoformat(timestamp)
                    self.last_error = None
            except Exception as exc:
                self.alert_manager.notify_error(timestamp, str(exc))
                with self.lock:
                    self.last_error = str(exc)
            self._stop_event.wait(self.settings.poll_seconds)

    def _load_persisted_alert_settings(self) -> None:
        low_battery = self.database.fetch_config("low_battery_percent")
        high_load = self.database.fetch_config("high_load_watts")
        cooldown = self.database.fetch_config("alert_cooldown_minutes")
        ntfy_topic = self.database.fetch_config("ntfy_topic_url")
        forecast_latitude = self.database.fetch_config("forecast_latitude")
        forecast_longitude = self.database.fetch_config("forecast_longitude")
        forecast_check_hours = self.database.fetch_config("forecast_check_hours")
        forecast_cloud_threshold_percent = self.database.fetch_config("forecast_cloud_threshold_percent")
        forecast_evening_advisory_hour = self.database.fetch_config("forecast_evening_advisory_hour")
        forecast_reserve_battery_percent = self.database.fetch_config("forecast_reserve_battery_percent")
        battery_count = self.database.fetch_config("battery_count")
        capacity_per_battery_kwh = self.database.fetch_config("capacity_per_battery_kwh")
        usable_capacity_percent = self.database.fetch_config("usable_capacity_percent")
        battery_estimate_enabled = self.database.fetch_config("battery_estimate_enabled")
        low_estimated_battery_percent = self.database.fetch_config("low_estimated_battery_percent")

        if low_battery is not None:
            try:
                self.settings.low_battery_percent = int(low_battery)
            except ValueError:
                pass
        if high_load is not None:
            try:
                self.settings.high_load_watts = int(high_load)
            except ValueError:
                pass
        if cooldown is not None:
            try:
                self.settings.alert_cooldown_minutes = int(cooldown)
            except ValueError:
                pass
        if ntfy_topic is not None:
            self.settings.ntfy_topic_url = ntfy_topic
        if forecast_latitude is not None:
            try:
                self.settings.forecast_latitude = float(forecast_latitude)
            except ValueError:
                pass
        if forecast_longitude is not None:
            try:
                self.settings.forecast_longitude = float(forecast_longitude)
            except ValueError:
                pass
        if forecast_check_hours is not None:
            try:
                self.settings.forecast_check_hours = int(forecast_check_hours)
            except ValueError:
                pass
        if forecast_cloud_threshold_percent is not None:
            try:
                self.settings.forecast_cloud_threshold_percent = int(forecast_cloud_threshold_percent)
            except ValueError:
                pass
        if forecast_evening_advisory_hour is not None:
            try:
                self.settings.forecast_evening_advisory_hour = int(forecast_evening_advisory_hour)
            except ValueError:
                pass
        if forecast_reserve_battery_percent is not None:
            try:
                self.settings.forecast_reserve_battery_percent = int(forecast_reserve_battery_percent)
            except ValueError:
                pass
        if battery_count is not None:
            try:
                self.settings.battery_count = int(battery_count)
            except ValueError:
                pass
        if capacity_per_battery_kwh is not None:
            try:
                self.settings.capacity_per_battery_kwh = float(capacity_per_battery_kwh)
            except ValueError:
                pass
        if usable_capacity_percent is not None:
            try:
                self.settings.usable_capacity_percent = int(usable_capacity_percent)
            except ValueError:
                pass
        if battery_estimate_enabled is not None:
            self.settings.battery_estimate_enabled = battery_estimate_enabled == "1"
        if low_estimated_battery_percent is not None:
            if low_estimated_battery_percent == "":
                self.settings.low_estimated_battery_percent = None
            else:
                try:
                    self.settings.low_estimated_battery_percent = int(low_estimated_battery_percent)
                except ValueError:
                    pass

    def get_live_payload(self) -> dict[str, Any]:
        with self.lock:
            return {
                "settings": {
                    "serial_port": self.settings.serial_port,
                    "poll_seconds": self.settings.poll_seconds,
                    "mock_mode": self.settings.mock_mode,
                },
                "started_at": self.started_at,
                "last_success_at": self.last_success_at,
                "last_error": self.last_error,
                "sample": self.latest,
            }

    def get_alerts_payload(self) -> dict[str, Any]:
        return {
            "alerts": self.database.fetch_alerts(),
            "settings": {
                "low_battery_percent": self.settings.low_battery_percent,
                "high_load_watts": self.settings.high_load_watts,
                "alert_cooldown_minutes": self.settings.alert_cooldown_minutes,
                "ntfy_topic_url": self.settings.ntfy_topic_url,
                "ntfy_enabled": bool(self.settings.ntfy_topic_url),
                "forecast_latitude": self.settings.forecast_latitude,
                "forecast_longitude": self.settings.forecast_longitude,
                "forecast_check_hours": self.settings.forecast_check_hours,
                "forecast_cloud_threshold_percent": self.settings.forecast_cloud_threshold_percent,
                "forecast_evening_advisory_hour": self.settings.forecast_evening_advisory_hour,
                "forecast_reserve_battery_percent": self.settings.forecast_reserve_battery_percent,
                "battery_count": self.settings.battery_count,
                "capacity_per_battery_kwh": self.settings.capacity_per_battery_kwh,
                "usable_capacity_percent": self.settings.usable_capacity_percent,
                "battery_estimate_enabled": self.settings.battery_estimate_enabled,
                "low_estimated_battery_percent": self.settings.low_estimated_battery_percent,
            },
        }

    def update_alert_settings(self, updates: dict[str, Any]) -> dict[str, Any]:
        with self.lock:
            if "low_battery_percent" in updates:
                value = int(updates["low_battery_percent"])
                if not 1 <= value <= 100:
                    raise ValueError("low_battery_percent must be between 1 and 100")
                self.settings.low_battery_percent = value
                self.database.upsert_config("low_battery_percent", str(value))

            if "high_load_watts" in updates:
                value = int(updates["high_load_watts"])
                if value < 100:
                    raise ValueError("high_load_watts must be at least 100")
                self.settings.high_load_watts = value
                self.database.upsert_config("high_load_watts", str(value))

            if "alert_cooldown_minutes" in updates:
                value = int(updates["alert_cooldown_minutes"])
                if not 1 <= value <= 1440:
                    raise ValueError("alert_cooldown_minutes must be between 1 and 1440")
                self.settings.alert_cooldown_minutes = value
                self.database.upsert_config("alert_cooldown_minutes", str(value))

            if "ntfy_topic_url" in updates:
                value = str(updates["ntfy_topic_url"]).strip()
                self.settings.ntfy_topic_url = value
                self.database.upsert_config("ntfy_topic_url", value)

            if "forecast_latitude" in updates:
                raw = updates["forecast_latitude"]
                if raw in ("", None):
                    self.settings.forecast_latitude = None
                    self.database.upsert_config("forecast_latitude", "")
                else:
                    value = float(raw)
                    if not -90 <= value <= 90:
                        raise ValueError("forecast_latitude must be between -90 and 90")
                    self.settings.forecast_latitude = value
                    self.database.upsert_config("forecast_latitude", str(value))

            if "forecast_longitude" in updates:
                raw = updates["forecast_longitude"]
                if raw in ("", None):
                    self.settings.forecast_longitude = None
                    self.database.upsert_config("forecast_longitude", "")
                else:
                    value = float(raw)
                    if not -180 <= value <= 180:
                        raise ValueError("forecast_longitude must be between -180 and 180")
                    self.settings.forecast_longitude = value
                    self.database.upsert_config("forecast_longitude", str(value))

            if "forecast_check_hours" in updates:
                value = int(updates["forecast_check_hours"])
                if not 1 <= value <= 24:
                    raise ValueError("forecast_check_hours must be between 1 and 24")
                self.settings.forecast_check_hours = value
                self.database.upsert_config("forecast_check_hours", str(value))

            if "forecast_cloud_threshold_percent" in updates:
                value = int(updates["forecast_cloud_threshold_percent"])
                if not 1 <= value <= 100:
                    raise ValueError("forecast_cloud_threshold_percent must be between 1 and 100")
                self.settings.forecast_cloud_threshold_percent = value
                self.database.upsert_config("forecast_cloud_threshold_percent", str(value))

            if "forecast_evening_advisory_hour" in updates:
                value = int(updates["forecast_evening_advisory_hour"])
                if not 0 <= value <= 23:
                    raise ValueError("forecast_evening_advisory_hour must be between 0 and 23")
                self.settings.forecast_evening_advisory_hour = value
                self.database.upsert_config("forecast_evening_advisory_hour", str(value))

            if "forecast_reserve_battery_percent" in updates:
                value = int(updates["forecast_reserve_battery_percent"])
                if not 1 <= value <= 100:
                    raise ValueError("forecast_reserve_battery_percent must be between 1 and 100")
                self.settings.forecast_reserve_battery_percent = value
                self.database.upsert_config("forecast_reserve_battery_percent", str(value))

            if "battery_count" in updates:
                value = int(updates["battery_count"])
                if value < 0:
                    raise ValueError("battery_count must be 0 or greater")
                self.settings.battery_count = value
                self.database.upsert_config("battery_count", str(value))

            if "capacity_per_battery_kwh" in updates:
                value = float(updates["capacity_per_battery_kwh"])
                if value < 0:
                    raise ValueError("capacity_per_battery_kwh must be 0 or greater")
                self.settings.capacity_per_battery_kwh = value
                self.database.upsert_config("capacity_per_battery_kwh", str(value))

            if "usable_capacity_percent" in updates:
                value = int(updates["usable_capacity_percent"])
                if not 1 <= value <= 100:
                    raise ValueError("usable_capacity_percent must be between 1 and 100")
                self.settings.usable_capacity_percent = value
                self.database.upsert_config("usable_capacity_percent", str(value))

            if "battery_estimate_enabled" in updates:
                enabled = bool(updates["battery_estimate_enabled"])
                self.settings.battery_estimate_enabled = enabled
                self.database.upsert_config("battery_estimate_enabled", "1" if enabled else "0")

            if "low_estimated_battery_percent" in updates:
                raw = updates["low_estimated_battery_percent"]
                if raw in ("", None):
                    self.settings.low_estimated_battery_percent = None
                    self.database.upsert_config("low_estimated_battery_percent", "")
                else:
                    value = int(raw)
                    if not 1 <= value <= 99:
                        raise ValueError("low_estimated_battery_percent must be between 1 and 99")
                    self.settings.low_estimated_battery_percent = value
                    self.database.upsert_config("low_estimated_battery_percent", str(value))

        return self.get_alerts_payload()["settings"]

    def send_test_notification(self) -> dict[str, Any]:
        delivered = self.alert_manager.send_test_notification(utc_now())
        return {"delivered": delivered}


class RequestHandler(BaseHTTPRequestHandler):
    server_version = "PowerMonitor/1.0"

    @property
    def app_state(self) -> PowerMonitorState:
        return self.server.app_state  # type: ignore[attr-defined]

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/api/live":
            self._send_json(self.app_state.get_live_payload())
            return
        if parsed.path == "/api/history":
            query = parse_qs(parsed.query)
            hours = max(1, min(168, int(query.get("hours", ["24"])[0])))
            self._send_json({"points": self.app_state.database.fetch_history(hours)})
            return
        if parsed.path == "/api/status":
            self._send_json(
                {
                    "ok": self.app_state.get_live_payload()["sample"] is not None,
                    "settings": asdict(self.app_state.settings),
                }
            )
            return
        if parsed.path == "/api/alerts":
            self._send_json(self.app_state.get_alerts_payload())
            return
        self._serve_static(parsed.path)

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/api/alerts/settings":
            try:
                payload = self._read_json_body()
            except ValueError as exc:
                self._send_json({"ok": False, "error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
                return
            try:
                settings = self.app_state.update_alert_settings(payload)
            except ValueError as exc:
                self._send_json({"ok": False, "error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
                return
            self._send_json({"ok": True, "settings": settings})
            return
        if parsed.path == "/api/alerts/test":
            result = self.app_state.send_test_notification()
            self._send_json({"ok": True, **result})
            return
        self.send_error(HTTPStatus.NOT_FOUND)

    def log_message(self, format: str, *args: Any) -> None:
        return

    def _serve_static(self, request_path: str) -> None:
        path = STATIC_DIR / ("index.html" if request_path in {"/", ""} else request_path.lstrip("/"))
        try:
            resolved = path.resolve()
            resolved.relative_to(STATIC_DIR.resolve())
        except Exception:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        if not resolved.exists() or not resolved.is_file():
            self.send_error(HTTPStatus.NOT_FOUND)
            return

        mime = "text/plain; charset=utf-8"
        if resolved.suffix == ".html":
            mime = "text/html; charset=utf-8"
        elif resolved.suffix == ".css":
            mime = "text/css; charset=utf-8"
        elif resolved.suffix == ".js":
            mime = "application/javascript; charset=utf-8"

        content = resolved.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def _read_json_body(self) -> dict[str, Any]:
        try:
            content_length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            content_length = 0
        raw = self.rfile.read(content_length) if content_length > 0 else b"{}"
        try:
            parsed = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError("Invalid JSON payload") from exc
        if not isinstance(parsed, dict):
            raise ValueError("JSON payload must be an object")
        return parsed

    def _send_json(self, payload: dict[str, Any], status: HTTPStatus = HTTPStatus.OK) -> None:
        raw = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="EG4 inverter power monitor")
    parser.add_argument("--host", default=None, help="Host to bind the web server to")
    parser.add_argument("--port", type=int, default=None, help="Port to bind the web server to")
    parser.add_argument("--mock", action="store_true", help="Use generated data instead of the serial port")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    settings = Settings()
    if args.host:
        settings.host = args.host
    if args.port:
        settings.port = args.port
    if args.mock:
        settings.mock_mode = True

    database = Database(DB_PATH)
    app_state = PowerMonitorState(settings, database)
    app_state.start()

    server = ThreadingHTTPServer((settings.host, settings.port), RequestHandler)
    server.app_state = app_state  # type: ignore[attr-defined]

    print(
        f"Power monitor running on http://{settings.host}:{settings.port} "
        f"(mock_mode={settings.mock_mode}, serial_port={settings.serial_port})"
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down...")
    finally:
        server.server_close()
        app_state.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
