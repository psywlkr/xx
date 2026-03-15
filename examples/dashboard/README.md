# BLE Dashboard

A modern, dark-theme web dashboard for scanning, connecting to, and
interacting with BLE (Bluetooth Low Energy) devices — built on top of
the **bleak** library.

> This dashboard works with **any** BLE device and includes first-class
> support for Ninebot / Segway e-scooters via the UART protocol
> (`6e400001-...`).

---

## PWA or Native App?

**Short answer: PWA is the right choice here, and it works exactly like a
native app.**

### Why PWA works perfectly

The dashboard uses **Python + bleak** on the backend, which accesses the
native OS Bluetooth stack (BlueZ on Linux, WinRT on Windows, CoreBluetooth
on macOS). The browser is just the UI layer. This means:

| Concern | Reality |
|---|---|
| BLE access | ✅ Full native OS Bluetooth via `bleak` – no browser limitations |
| iOS / Android | ✅ Works in any browser that can reach the Python server |
| Offline shell | ✅ Service Worker caches the app so it loads instantly |
| Home screen icon | ✅ Chrome / Edge / Safari prompt to install |
| Full-screen mode | ✅ `display: standalone` removes all browser chrome |
| Background BLE | ✅ Python server keeps running; UI reconnects via SSE |
| Push notifications | ⚠️ Requires HTTPS for Web Push – use OS notifications instead |

### Native app packaging (optional)

If you want a self-contained binary with no separate Python server step,
you can package the Python server with the dashboard as a desktop app:

```bash
# Electron-like packaging with Tauri (requires Rust) or PyInstaller
pip install pyinstaller
pyinstaller --onefile --add-data "examples/dashboard:examples/dashboard" \
            examples/dashboard/main.py
```

Or use the existing **Kivy Android APK** in `examples/kivy/` for a true
mobile native app.

### Why NOT pure Web Bluetooth PWA

A fully browser-based PWA using the [Web Bluetooth API](https://developer.mozilla.org/en-US/docs/Web/API/Web_Bluetooth_API)
would require:
- Chrome / Edge only (no Firefox, no iOS Safari)
- HTTPS (not just localhost)
- User gesture for every scan
- Limited service UUID filtering

The Python-backend approach has **no such restrictions**.

---

## Installing the PWA

### Desktop (Chrome / Edge)

1. Start the server: `python -m examples.dashboard`
2. Open `http://127.0.0.1:8765` in Chrome or Edge
3. Click the **📲 Install App** button in the top bar  
   *(or use the browser's address-bar install icon)*
4. The dashboard opens as a standalone window — no browser chrome

### Android (Chrome)

1. Run the server on the same machine or local network with `--host 0.0.0.0`
2. Open `http://<your-ip>:8765` in Chrome on Android
3. Chrome shows "Add to Home Screen" — tap it
4. The app appears on your home screen with the BLE icon

### iOS (Safari)

1. Open the URL in Safari
2. Tap **Share → Add to Home Screen**
3. The app opens full-screen using the meta tags already in the HTML

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
| **PWA** | Installable, offline shell, home screen icon, standalone mode |

---

## Architecture

```
examples/dashboard/
├── __init__.py
├── main.py               ← Entry point (argparse, asyncio.run)
├── ble_manager.py        ← Core BLE logic (wraps bleak)
├── scooter_protocol.py   ← Ninebot protocol UUIDs + parsers
├── server.py             ← aiohttp HTTP + SSE server + embedded HTML + PWA assets
└── requirements.txt
```

### Key design points

* **`BLEManager`** is the single source of truth for BLE state.  
  It wraps `BleakScanner` / `BleakClient` and exposes an *async event
  callback* system so any number of subscribers (SSE clients) can receive
  live updates.
* **Server-Sent Events** push BLE events to the browser in real time — no
  polling, no WebSocket library needed.
* **PWA assets** (`/manifest.json`, `/sw.js`, `/icon-192.png`, `/icon-512.png`)
  are generated and served by the Python server — no static files needed.
* **Service Worker** caches the app shell for instant offline load and uses
  network-first for all API / SSE calls (real-time BLE data is never cached).
* The **HTML dashboard** is embedded in `server.py` as a string constant so
  the entire dashboard is a single Python package with zero static-file
  dependencies.

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

# Expose on local network (for Android PWA install)
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
| GET | `/manifest.json` | – | Web App Manifest |
| GET | `/sw.js` | – | Service Worker |
| GET | `/icon-192.png` | – | 192×192 PNG icon |
| GET | `/icon-512.png` | – | 512×512 PNG icon |
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
- [ ] Event log shows "PWA service worker ready"
- [ ] Event log shows "App is installable – click 📲 Install App"
- [ ] Click **📲 Install App** → browser install dialog appears
- [ ] After install, app opens in its own standalone window (no browser chrome)
- [ ] Click **Scan** → device list populates with real BLE devices
- [ ] Click a device → connect dialog triggers
- [ ] Status indicator turns green (Connected)
- [ ] GATT Services panel expands with real services/characteristics
- [ ] MTU card shows negotiated value
- [ ] Click **R** (Read) on a readable characteristic → value appears in log
- [ ] Click **N** (Notify) on a notifying characteristic → live values in log
- [ ] Click **🔒 Lock** / **🔓 Unlock** (Ninebot only) → command sent, response in log
- [ ] Click **✕ Disconnect** → status returns to Disconnected
- [ ] Stop the Python server, reload the PWA → cached shell loads (offline mode)
- [ ] Restart Python server, reload → SSE reconnects, live data resumes
- [ ] Open `http://127.0.0.1:8765/?action=scan` → scan starts automatically
