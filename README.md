<<<<<<< HEAD
# EG4 6500 Power Monitor

This project runs directly on a Raspberry Pi, polls your EG4 inverter over the existing RS232 connection, stores samples in SQLite, and serves a lightweight dashboard you can open from a browser on the same Wi-Fi.

## What it does

- Polls `QPIGS` from the inverter on `/dev/ttyUSB0`
- Stores readings every few seconds in `data/power_monitor.sqlite3`
- Serves a live dashboard with Dashboard / History / Alerts pages
- Records built-in alerts for low battery, high load, solar shortfall support, and inverter communication issues
- Works with a single Python process and one dependency: `pyserial`

## Quick start on the Pi

Copy this folder onto the Raspberry Pi as `~/power-monitor`, then run:

```bash
cd ~/power-monitor
chmod +x run.sh
./run.sh
```

Then open:

```text
http://RASPBERRY_PI_IP:8080
```

Example:

```text
http://192.168.1.50:8080
```

## Manual start

```bash
cd ~/power-monitor
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
python3 app.py --host 0.0.0.0 --port 8080
```

## Useful options

Use mock mode if you want to test the dashboard without the inverter attached:

```bash
python3 app.py --mock --host 0.0.0.0 --port 8080
```

You can also override settings with environment variables:

```bash
POWER_MONITOR_SERIAL_PORT=/dev/ttyUSB0
POWER_MONITOR_BAUD_RATE=2400
POWER_MONITOR_POLL_SECONDS=5
POWER_MONITOR_PORT=8080
POWER_MONITOR_LOW_BATTERY_PERCENT=25
POWER_MONITOR_HIGH_LOAD_WATTS=4000
POWER_MONITOR_ALERT_COOLDOWN_MINUTES=20
POWER_MONITOR_NTFY_TOPIC_URL=
POWER_MONITOR_FORECAST_LATITUDE=
POWER_MONITOR_FORECAST_LONGITUDE=
POWER_MONITOR_FORECAST_CHECK_HOURS=6
POWER_MONITOR_FORECAST_CLOUD_THRESHOLD_PERCENT=70
POWER_MONITOR_FORECAST_ADVISORY_HOUR=17
POWER_MONITOR_FORECAST_RESERVE_BATTERY_PERCENT=70
```

If you set `POWER_MONITOR_NTFY_TOPIC_URL` to an `ntfy` topic URL, the built-in alerts will also try to send push notifications there.
You can also edit alert thresholds, weather advisory settings, and send a test notification directly from the `Alerts` page in the web UI.

## Forecast advisory alert (cloudy tomorrow)

If you set `POWER_MONITOR_FORECAST_LATITUDE` and `POWER_MONITOR_FORECAST_LONGITUDE`, the monitor will check Open-Meteo forecast data and create an alert in the evening when tomorrow is forecast cloudy and battery reserve is below your target.

Example:

```bash
export POWER_MONITOR_NTFY_TOPIC_URL="https://ntfy.sh/your-unique-topic"
export POWER_MONITOR_FORECAST_LATITUDE="35.3733"
export POWER_MONITOR_FORECAST_LONGITUDE="-119.0187"
export POWER_MONITOR_FORECAST_CLOUD_THRESHOLD_PERCENT="70"
export POWER_MONITOR_FORECAST_ADVISORY_HOUR="17"
export POWER_MONITOR_FORECAST_RESERVE_BATTERY_PERCENT="70"
```

Then restart the service so new settings apply.

## Run at boot with systemd

Create `/etc/systemd/system/power-monitor.service` with:

```ini
[Unit]
Description=EG4 Power Monitor
After=network.target

[Service]
Type=simple
User=pi
WorkingDirectory=/home/pi/power-monitor
ExecStart=/home/pi/power-monitor/.venv/bin/python3 /home/pi/power-monitor/app.py --host 0.0.0.0 --port 8080
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

Then enable it:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now power-monitor.service
```

## Notes about the inverter fields

The parser is built around the common `QPIGS` field order used by EG4/Axpert-style inverters:

- Grid voltage / frequency
- Output voltage / frequency
- Apparent and active power
- Load percentage
- Battery voltage and charging current
- Battery capacity
- Inverter temperature
- PV current / voltage

If your inverter returns a slightly different field order, the dashboard will still run, but some labels may need adjustment in `app.py`.

## Remote access later

For secure off-site access, the simplest next step is usually Tailscale on the Raspberry Pi. That avoids exposing the dashboard directly to the internet.
=======
# EG4-Power-Monitor
>>>>>>> 84fb31e00292c2448ac22276a4a1340f9ddeeebc
