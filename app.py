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


class AlertManager:
    def __init__(self, settings: Settings, database: Database) -> None:
        self.settings = settings
        self.database = database
        self._last_sent: dict[str, datetime] = {}
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

    def notify_error(self, ts_utc: datetime, error_message: str) -> None:
        self._emit(
            ts_utc,
            "critical",
            "inverter_error",
            "Inverter communication problem",
            error_message,
            None,
        )

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
        self.client = InverterClient(settings)
        self.alert_manager = AlertManager(settings, database)
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
                self.alert_manager.evaluate_sample(timestamp, sample)
                self.database.prune()
                with self.lock:
                    self.latest = {"ts_utc": isoformat(timestamp), **sample}
                    self.last_success_at = isoformat(timestamp)
                    self.last_error = None
            except Exception as exc:
                self.alert_manager.notify_error(timestamp, str(exc))
                with self.lock:
                    self.last_error = str(exc)
            self._stop_event.wait(self.settings.poll_seconds)

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
                "ntfy_enabled": bool(self.settings.ntfy_topic_url),
            },
        }


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

    def _send_json(self, payload: dict[str, Any]) -> None:
        raw = json.dumps(payload).encode("utf-8")
        self.send_response(HTTPStatus.OK)
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
