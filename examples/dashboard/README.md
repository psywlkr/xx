# BLE Dashboard

A modern, dark-theme web dashboard for scanning, connecting to, and
interacting with BLE (Bluetooth Low Energy) devices — built on top of
the **bleak** library.

> This dashboard works with **any** BLE device and includes first-class
> support for Ninebot / Segway e-scooters via the UART protocol
> (`6e400001-...`).

---

## Features

| Area | What it does |
|---|---|
| **Scan** | Discover nearby BLE devices with RSSI, name, service UUIDs |
| **Device list** | Sorted by signal strength; one-click connect |
| **Connection status** | Live colour-coded indicator (Disconnected / Scanning / Connecting / Connected / Error) |
| **Live telemetry cards** | Battery %, Speed km/h, RSSI dBm, Temperature °C, Lock status, MTU |
| **GATT explorer** | Full service/characteristic tree with Read / Write / Notify buttons |
| **Actions panel** | Lock, Unlock, Get Status/Battery/Speed/Serial, custom Read/Write/Subscribe |
| **Event log** | Real-time BLE events (scan, connect, disconnect, notify, errors) via SSE |

---

## Architecture

```
examples/dashboard/
├── __init__.py
├── main.py               ← Entry point (argparse, asyncio.run)
├── ble_manager.py        ← Core BLE logic (wraps bleak)
├── scooter_protocol.py   ← Ninebot protocol UUIDs + parsers
├── server.py             ← aiohttp HTTP + SSE server + embedded HTML
└── requirements.txt
```

### Key design points

* **`BLEManager`** is the single source of truth for BLE state.  
  It wraps `BleakScanner` / `BleakClient` and exposes an *async event
  callback* system so any number of subscribers (SSE clients) can receive
  live updates.
* **Server-Sent Events** push BLE events to the browser in real time — no
  polling, no WebSocket library needed.
* The **HTML dashboard** is embedded in `server.py` as a string constant so
  the entire dashboard is a single Python package with zero static-file
  dependencies.
* All dashboard actions (scan, connect, commands) call the REST API; the
  results come back asynchronously via the SSE stream.

---

## Requirements

* Python ≥ 3.10
* `bleak` (already available in this repository)
* `aiohttp` ≥ 3.9

Install the extra dependency:

```bash
pip install aiohttp
```

---

## Running

From the repository root:

```bash
# Simplest – opens browser automatically
python -m examples.dashboard

# Custom host/port
python -m examples.dashboard --host 0.0.0.0 --port 9000

# No browser auto-open
python -m examples.dashboard --no-browser

# Verbose BLE + HTTP logging
python -m examples.dashboard --verbose
```

Then open **http://127.0.0.1:8765** in your browser.

---

## API Reference

| Method | Path | Body | Description |
|---|---|---|---|
| GET | `/` | – | Dashboard HTML |
| GET | `/api/status` | – | Full status JSON |
| GET | `/api/devices` | – | Discovered devices |
| GET | `/api/services` | – | GATT services of connected device |
| GET | `/events` | – | SSE stream |
| POST | `/api/scan` | `{"duration": 10}` | Start BLE scan |
| POST | `/api/connect` | `{"address": "XX:XX:XX:XX:XX:XX"}` | Connect |
| POST | `/api/disconnect` | `{}` | Disconnect |
| POST | `/api/read` | `{"uuid": "..."}` | Read characteristic |
| POST | `/api/write` | `{"uuid": "...", "data": [...]}` | Write characteristic |
| POST | `/api/notify/start` | `{"uuid": "..."}` | Subscribe to notifications |
| POST | `/api/notify/stop` | `{"uuid": "..."}` | Unsubscribe |
| POST | `/api/command` | `{"name": "lock"\|"unlock"\|...}` | Send scooter command |

---

## Scooter Commands

When connected to a Ninebot / Segway device (detected via service UUID
`6e400001-b5a3-f393-e0a9-e50e24dcca9e`), the following named commands are
available via `POST /api/command`:

| Name | Description |
|---|---|
| `lock` | Lock the scooter |
| `unlock` | Unlock the scooter |
| `get_status` | Request general status packet |
| `get_battery` | Request battery info |
| `get_speed` | Request current speed |
| `get_serial` | Request serial number |

Responses arrive as `notify` events in the SSE stream.

---

## Manual Test Checklist

- [ ] Open browser at `http://127.0.0.1:8765`
- [ ] SSE indicator turns green (top-right)
- [ ] Click **Scan** → device list populates with real BLE devices
- [ ] Click a device → connect dialog triggers
- [ ] Status indicator turns green (Connected)
- [ ] GATT Services panel expands with real services/characteristics
- [ ] MTU card shows negotiated value
- [ ] Click **R** (Read) on a readable characteristic → value appears in log
- [ ] Click **N** (Notify) on a notifying characteristic → live values in log
- [ ] Click **🔒 Lock** / **🔓 Unlock** (Ninebot only) → command sent, response in log
- [ ] Click **✕ Disconnect** → status returns to Disconnected
- [ ] Close browser tab and reconnect → SSE re-establishes automatically
- [ ] Event log scrolls and **Clear** button works
