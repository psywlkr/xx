"""
BLE Dashboard – aiohttp web server.

Serves a dark-theme, real-time BLE/Scooter dashboard via:
- HTTP GET  /            → Dashboard HTML
- HTTP GET  /api/status  → JSON status snapshot
- HTTP GET  /api/devices → JSON list of discovered devices
- HTTP GET  /api/services → JSON list of GATT services
- HTTP GET  /events      → Server-Sent Events stream (real-time BLE events)
- HTTP POST /api/scan    → Start BLE scan  (body: {"duration": 10})
- HTTP POST /api/connect → Connect         (body: {"address": "XX:XX:XX:XX:XX:XX"})
- HTTP POST /api/disconnect → Disconnect
- HTTP POST /api/read    → Read characteristic (body: {"uuid": "..."})
- HTTP POST /api/write   → Write characteristic (body: {"uuid": "...", "data": [...]})
- HTTP POST /api/notify/start  → Subscribe  (body: {"uuid": "..."})
- HTTP POST /api/notify/stop   → Unsubscribe (body: {"uuid": "..."})
- HTTP POST /api/command → Send Ninebot command (body: {"name": "lock"|"unlock"|...})
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from aiohttp import web

from .ble_manager import BLEEvent, BLEManager
from .scooter_protocol import (
    CMD_GET_BATTERY_INFO,
    CMD_GET_SERIAL,
    CMD_GET_SPEED,
    CMD_GET_STATUS,
    CMD_LOCK,
    CMD_UNLOCK,
    NINEBOT_RX_UUID,
)

logger = logging.getLogger(__name__)

DASHBOARD_VERSION = "1.0"

# ---------------------------------------------------------------------------
# PWA assets
# ---------------------------------------------------------------------------

_MANIFEST_JSON = json.dumps({
    "name": "BLE Dashboard",
    "short_name": "BLE",
    "description": "BLE device scanner and controller powered by bleak",
    "start_url": "/",
    "display": "standalone",
    "orientation": "any",
    "background_color": "#0a0e1a",
    "theme_color": "#0a0e1a",
    "lang": "en",
    "categories": ["utilities", "tools"],
    "icons": [
        {
            "src": "/icon-192.png",
            "sizes": "192x192",
            "type": "image/png",
            "purpose": "any maskable",
        },
        {
            "src": "/icon-512.png",
            "sizes": "512x512",
            "type": "image/png",
            "purpose": "any maskable",
        },
    ],
    "shortcuts": [
        {
            "name": "Start Scan",
            "short_name": "Scan",
            "description": "Start a BLE device scan immediately",
            "url": "/?action=scan",
            "icons": [{"src": "/icon-192.png", "sizes": "192x192"}],
        }
    ],
})

_SERVICE_WORKER_JS = f"""\
/* BLE Dashboard Service Worker – v{DASHBOARD_VERSION} */
const CACHE = 'ble-dashboard-v{DASHBOARD_VERSION}';
const SHELL = ['/', '/manifest.json', '/icon-192.png', '/icon-512.png'];

self.addEventListener('install', e => {{
  e.waitUntil(caches.open(CACHE).then(c => c.addAll(SHELL)));
  self.skipWaiting();
}});

self.addEventListener('activate', e => {{
  e.waitUntil(
    caches.keys().then(keys =>
      Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k)))
    )
  );
  self.clients.claim();
}});

self.addEventListener('fetch', e => {{
  const url = new URL(e.request.url);
  // API calls and SSE stream: always go to network (real-time BLE data)
  if (url.pathname.startsWith('/api/') || url.pathname === '/events' ||
      url.pathname === '/sw.js') {{
    e.respondWith(
      fetch(e.request).catch(() =>
        new Response(JSON.stringify({{error: 'Server offline – start the Python server'}}),
          {{status: 503, headers: {{'Content-Type': 'application/json'}}}})
      )
    );
    return;
  }}
  // App shell: stale-while-revalidate
  e.respondWith(
    caches.open(CACHE).then(cache =>
      cache.match(e.request).then(cached => {{
        const fetched = fetch(e.request).then(resp => {{
          if (resp.ok) cache.put(e.request, resp.clone());
          return resp;
        }}).catch(() => null);
        return cached || fetched;
      }})
    )
  );
}});
"""


def _make_icon_png(size: int) -> bytes:
    """
    Generate a PNG app icon at runtime using only the Python standard library.

    Design: dark navy background (#0a0e1a) with a concentric two-tone circle
    in accent blue (#4f8ef7) and cyan (#00d4ff) – evokes a Bluetooth signal.
    """
    import math
    import struct
    import zlib

    bg     = (10,  14,  26)   # #0a0e1a
    ring   = (79,  142, 247)  # #4f8ef7
    inner  = (0,   212, 255)  # #00d4ff

    cx = cy   = size / 2.0
    outer_r   = size * 0.42
    inner_r   = size * 0.22

    rows: list[bytes] = []
    for y in range(size):
        row = bytearray([0])  # PNG filter byte: None
        for x in range(size):
            d = math.sqrt((x - cx) ** 2 + (y - cy) ** 2)
            if d <= inner_r:
                row.extend(inner)
            elif d <= outer_r:
                row.extend(ring)
            else:
                row.extend(bg)
        rows.append(bytes(row))

    raw = b"".join(rows)

    def _chunk(ctype: bytes, data: bytes) -> bytes:
        body = ctype + data
        return (
            struct.pack(">I", len(data))
            + body
            + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)
        )

    sig  = b"\x89PNG\r\n\x1a\n"
    ihdr = _chunk(b"IHDR", struct.pack(">II", size, size) + bytes([8, 2, 0, 0, 0]))
    idat = _chunk(b"IDAT", zlib.compress(raw, 6))
    iend = _chunk(b"IEND", b"")
    return sig + ihdr + idat + iend


# Pre-generate icons once at module load time to avoid per-request overhead
_ICON_192 = _make_icon_png(192)
_ICON_512 = _make_icon_png(512)

# ---------------------------------------------------------------------------
# Embedded dashboard HTML
# ---------------------------------------------------------------------------

_DASHBOARD_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8" />
<meta name="viewport" content="width=device-width, initial-scale=1.0, viewport-fit=cover" />
<meta name="theme-color" content="#0a0e1a" />
<meta name="mobile-web-app-capable" content="yes" />
<meta name="apple-mobile-web-app-capable" content="yes" />
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent" />
<meta name="apple-mobile-web-app-title" content="BLE Dashboard" />
<link rel="manifest" href="/manifest.json" />
<link rel="apple-touch-icon" href="/icon-192.png" />
<title>BLE Dashboard</title>
<style>
  *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }

  :root {
    --bg:        #0a0e1a;
    --surface:   #12172b;
    --surface2:  #1a2035;
    --border:    #1e2847;
    --accent:    #4f8ef7;
    --accent2:   #00d4ff;
    --text:      #e2e8f0;
    --muted:     #64748b;
    --success:   #10b981;
    --warning:   #f59e0b;
    --error:     #ef4444;
    --info:      #6366f1;
    --radius:    10px;
    --shadow:    0 4px 24px rgba(0,0,0,.4);
  }

  html, body {
    height: 100%;
    background: var(--bg);
    color: var(--text);
    font-family: 'Inter', 'Segoe UI', system-ui, sans-serif;
    font-size: 14px;
    line-height: 1.5;
  }

  /* PWA standalone: extra top padding to avoid status-bar overlap on iOS */
  @media (display-mode: standalone) {
    .header { padding-top: env(safe-area-inset-top, 0px); }
    .app    { grid-template-rows: calc(56px + env(safe-area-inset-top, 0px)) 1fr; }
  }

  /* ── Layout ─────────────────────────────── */
  .app {
    display: grid;
    grid-template-rows: 56px 1fr;
    height: 100vh;
  }

  .header {
    display: flex;
    align-items: center;
    padding: 0 24px;
    background: var(--surface);
    border-bottom: 1px solid var(--border);
    gap: 16px;
  }
  .header-logo { font-size: 20px; font-weight: 700; letter-spacing: -.5px; }
  .header-logo span { color: var(--accent2); }
  .header-spacer { flex: 1; }
  .header-version { color: var(--muted); font-size: 12px; }

  .main {
    display: grid;
    grid-template-columns: 300px 1fr 320px;
    overflow: hidden;
  }

  /* ── Sidebar ─────────────────────────────── */
  .sidebar {
    background: var(--surface);
    border-right: 1px solid var(--border);
    display: flex;
    flex-direction: column;
    overflow: hidden;
  }

  /* ── Center panel ────────────────────────── */
  .center {
    overflow-y: auto;
    padding: 20px;
    display: flex;
    flex-direction: column;
    gap: 20px;
  }

  /* ── Right panel ─────────────────────────── */
  .rightpanel {
    background: var(--surface);
    border-left: 1px solid var(--border);
    display: flex;
    flex-direction: column;
    overflow: hidden;
  }

  /* ── Panels / sections ───────────────────── */
  .panel {
    background: var(--surface2);
    border-radius: var(--radius);
    border: 1px solid var(--border);
    overflow: hidden;
    box-shadow: var(--shadow);
  }
  .panel-header {
    padding: 12px 16px;
    background: var(--surface);
    border-bottom: 1px solid var(--border);
    display: flex;
    align-items: center;
    gap: 8px;
    font-size: 12px;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: .6px;
    color: var(--muted);
  }
  .panel-header .dot {
    width: 8px; height: 8px;
    border-radius: 50%;
    background: var(--accent);
  }
  .panel-body { padding: 16px; }

  /* ── Sidebar sections ────────────────────── */
  .sidebar-section {
    border-bottom: 1px solid var(--border);
    display: flex;
    flex-direction: column;
  }
  .sidebar-section-header {
    padding: 10px 16px;
    font-size: 11px;
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: .8px;
    color: var(--muted);
    background: var(--surface);
  }
  .sidebar-section-body { padding: 12px; }

  .device-list { display: flex; flex-direction: column; gap: 6px; flex: 1; overflow-y: auto; }
  .device-item {
    display: flex;
    flex-direction: column;
    gap: 2px;
    padding: 10px 12px;
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 8px;
    cursor: pointer;
    transition: border-color .15s, background .15s;
  }
  .device-item:hover { border-color: var(--accent); background: #1e2847; }
  .device-item.selected { border-color: var(--accent2); background: #0d1b2e; }
  .device-name { font-weight: 600; font-size: 13px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .device-addr { font-family: monospace; font-size: 11px; color: var(--muted); }
  .device-rssi { font-size: 11px; color: var(--muted); }

  /* ── Connection status bar ───────────────── */
  .status-bar {
    display: flex;
    align-items: center;
    gap: 10px;
    padding: 10px 16px;
    border-radius: var(--radius);
    background: var(--surface2);
    border: 1px solid var(--border);
  }
  .status-indicator {
    width: 10px; height: 10px;
    border-radius: 50%;
    flex-shrink: 0;
    transition: background .3s;
  }
  .state-disconnected { background: var(--muted); }
  .state-scanning     { background: var(--warning); animation: pulse 1s infinite; }
  .state-connecting   { background: var(--accent); animation: pulse 1s infinite; }
  .state-connected    { background: var(--success); }
  .state-disconnecting{ background: var(--warning); animation: pulse 1s infinite; }
  .state-error        { background: var(--error); }

  @keyframes pulse {
    0%, 100% { opacity: 1; }
    50% { opacity: .4; }
  }

  /* ── Data cards ──────────────────────────── */
  .cards-grid {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(160px, 1fr));
    gap: 14px;
  }
  .data-card {
    background: var(--surface2);
    border: 1px solid var(--border);
    border-radius: var(--radius);
    padding: 16px;
    display: flex;
    flex-direction: column;
    gap: 6px;
    box-shadow: var(--shadow);
    transition: border-color .2s;
  }
  .data-card:hover { border-color: var(--accent); }
  .data-card-icon { font-size: 22px; }
  .data-card-label { font-size: 11px; font-weight: 600; text-transform: uppercase; letter-spacing: .6px; color: var(--muted); }
  .data-card-value { font-size: 26px; font-weight: 700; color: var(--text); line-height: 1; }
  .data-card-unit  { font-size: 13px; color: var(--muted); font-weight: 400; }

  /* ── Services accordion ──────────────────── */
  .service-item { margin-bottom: 8px; border: 1px solid var(--border); border-radius: 8px; overflow: hidden; }
  .service-header {
    padding: 10px 14px;
    background: var(--surface);
    cursor: pointer;
    display: flex;
    align-items: center;
    gap: 8px;
    font-size: 12px;
    font-weight: 600;
    user-select: none;
  }
  .service-header:hover { background: #1e2847; }
  .service-uuid { font-family: monospace; font-size: 11px; color: var(--muted); flex: 1; }
  .char-list { padding: 8px 14px; display: flex; flex-direction: column; gap: 6px; background: var(--surface2); }
  .char-item { display: flex; align-items: center; gap: 8px; font-size: 12px; }
  .char-uuid { font-family: monospace; flex: 1; color: var(--accent2); }
  .char-props { display: flex; gap: 4px; flex-wrap: wrap; }
  .prop-badge {
    font-size: 10px; font-weight: 600; padding: 1px 6px;
    border-radius: 4px; background: var(--surface); border: 1px solid var(--border);
    color: var(--muted); text-transform: lowercase;
  }
  .prop-badge.notify { border-color: var(--accent); color: var(--accent); }
  .prop-badge.write  { border-color: var(--success); color: var(--success); }
  .prop-badge.read   { border-color: var(--info); color: var(--info); }
  .char-actions { display: flex; gap: 4px; }

  /* ── Buttons ─────────────────────────────── */
  .btn {
    padding: 8px 16px;
    border-radius: 7px;
    border: 1px solid transparent;
    cursor: pointer;
    font-size: 13px;
    font-weight: 600;
    transition: opacity .15s, transform .1s;
    display: inline-flex;
    align-items: center;
    gap: 6px;
    white-space: nowrap;
  }
  .btn:active { transform: scale(.97); }
  .btn:disabled { opacity: .4; cursor: not-allowed; pointer-events: none; }
  .btn-primary { background: var(--accent); color: #fff; }
  .btn-primary:hover { background: #6a9fff; }
  .btn-success { background: var(--success); color: #fff; }
  .btn-success:hover { background: #0dca8f; }
  .btn-danger  { background: var(--error); color: #fff; }
  .btn-danger:hover  { background: #ff6060; }
  .btn-warning { background: var(--warning); color: #000; }
  .btn-warning:hover { background: #fbbf24; }
  .btn-ghost   { background: transparent; border-color: var(--border); color: var(--text); }
  .btn-ghost:hover { background: var(--surface2); }
  .btn-xs { padding: 3px 8px; font-size: 11px; border-radius: 5px; }
  .btn-sm { padding: 6px 12px; font-size: 12px; }

  /* ── Action row ──────────────────────────── */
  .actions-row { display: flex; flex-wrap: wrap; gap: 10px; }

  /* ── Event log ───────────────────────────── */
  .event-log {
    flex: 1;
    overflow-y: auto;
    padding: 10px;
    font-family: 'JetBrains Mono', 'Fira Code', monospace;
    font-size: 11.5px;
    display: flex;
    flex-direction: column;
    gap: 4px;
    background: #080b14;
  }
  .log-entry { display: flex; gap: 8px; align-items: flex-start; line-height: 1.4; }
  .log-time  { color: var(--muted); flex-shrink: 0; font-size: 10.5px; padding-top: 1px; }
  .log-type  { flex-shrink: 0; font-weight: 700; font-size: 10px; padding: 1px 6px; border-radius: 4px; margin-top: 1px; }
  .log-msg   { word-break: break-all; }
  .log-scan     .log-type { background: #1e3a5f; color: var(--accent); }
  .log-connect  .log-type { background: #0d3326; color: var(--success); }
  .log-disconnect .log-type { background: #3d2000; color: var(--warning); }
  .log-data     .log-type { background: #1a1060; color: var(--info); }
  .log-notify   .log-type { background: #2d1060; color: #a78bfa; }
  .log-error    .log-type { background: #3d0a0a; color: var(--error); }
  .log-command  .log-type { background: #102820; color: #34d399; }
  .log-state    .log-type { background: #1a1a1a; color: var(--muted); }

  /* ── Custom send box ─────────────────────── */
  .input-row { display: flex; gap: 8px; }
  .input-row input, .input-row select {
    flex: 1;
    background: var(--bg);
    border: 1px solid var(--border);
    border-radius: 7px;
    color: var(--text);
    padding: 8px 12px;
    font-size: 13px;
    font-family: 'JetBrains Mono', monospace;
    outline: none;
    transition: border-color .15s;
  }
  .input-row input:focus, .input-row select:focus { border-color: var(--accent); }

  /* ── Badges ──────────────────────────────── */
  .badge {
    display: inline-flex; align-items: center;
    padding: 2px 8px; border-radius: 20px; font-size: 11px; font-weight: 600;
  }
  .badge-success { background: rgba(16,185,129,.15); color: var(--success); border: 1px solid rgba(16,185,129,.3); }
  .badge-error   { background: rgba(239,68,68,.15);  color: var(--error);   border: 1px solid rgba(239,68,68,.3); }
  .badge-warning { background: rgba(245,158,11,.15); color: var(--warning); border: 1px solid rgba(245,158,11,.3); }
  .badge-info    { background: rgba(99,102,241,.15); color: var(--info);    border: 1px solid rgba(99,102,241,.3); }
  .badge-muted   { background: rgba(100,116,139,.1); color: var(--muted);   border: 1px solid rgba(100,116,139,.2); }

  /* ── Empty / loading states ──────────────── */
  .empty-state {
    display: flex; flex-direction: column; align-items: center; justify-content: center;
    padding: 32px 16px; gap: 8px; color: var(--muted); text-align: center;
  }
  .empty-icon { font-size: 32px; opacity: .5; }
  .empty-msg  { font-size: 13px; }

  /* ── Scrollbar ───────────────────────────── */
  ::-webkit-scrollbar { width: 6px; height: 6px; }
  ::-webkit-scrollbar-track { background: transparent; }
  ::-webkit-scrollbar-thumb { background: var(--border); border-radius: 3px; }
  ::-webkit-scrollbar-thumb:hover { background: var(--muted); }

  /* ── Responsive ──────────────────────────── */
  @media (max-width: 900px) {
    .main { grid-template-columns: 1fr; }
    .sidebar, .rightpanel { display: none; }
  }

  .divider { height: 1px; background: var(--border); margin: 4px 0; }
  .text-muted { color: var(--muted); }
  .text-sm { font-size: 12px; }
  .mt-2 { margin-top: 8px; }
  .w-full { width: 100%; }
  .gap-2 { gap: 8px; }
  .flex { display: flex; }
  .items-center { align-items: center; }
  .justify-between { justify-content: space-between; }
  .font-mono { font-family: monospace; }
</style>
</head>
<body>
<div class="app">
  <!-- ── Header ─────────────────────────────── -->
  <header class="header">
    <span class="header-logo">BLE <span>Dashboard</span></span>
    <div class="status-bar" id="globalStatus" style="padding:6px 14px; font-size:13px;">
      <span class="status-indicator state-disconnected" id="statusDot"></span>
      <span id="statusText">Disconnected</span>
      <span id="connectedDeviceName" class="text-muted text-sm"></span>
    </div>
    <div class="header-spacer"></div>
    <button id="btnInstall" class="btn btn-primary btn-sm"
      style="display:none;" onclick="doInstall()" title="Install as app">
      📲 Install App
    </button>
    <span class="header-version text-muted text-sm">bleak dashboard v1.0</span>
    <span id="sseStatus" class="badge badge-muted">● SSE</span>
  </header>

  <!-- ── Main ───────────────────────────────── -->
  <div class="main">
    <!-- ── Sidebar ──────────────────────────── -->
    <aside class="sidebar">
      <!-- Scan controls -->
      <div class="sidebar-section">
        <div class="sidebar-section-header">Scan &amp; Connect</div>
        <div class="sidebar-section-body" style="display:flex;flex-direction:column;gap:8px;">
          <div class="input-row">
            <input type="number" id="scanDuration" value="10" min="2" max="60" style="width:70px;flex:none;" title="Scan duration (s)" />
            <button class="btn btn-primary w-full" id="btnScan" onclick="doScan()">
              <span>⚡</span> Scan
            </button>
          </div>
          <button class="btn btn-danger w-full" id="btnDisconnect" onclick="doDisconnect()" disabled>
            <span>✕</span> Disconnect
          </button>
          <button class="btn btn-ghost w-full btn-sm" onclick="refreshStatus()">
            ↻ Refresh Status
          </button>
        </div>
      </div>

      <!-- Device list -->
      <div class="sidebar-section" style="flex:1;min-height:0;overflow:hidden;display:flex;flex-direction:column;">
        <div class="sidebar-section-header flex items-center justify-between">
          <span>Devices</span>
          <span id="deviceCount" class="badge badge-muted">0</span>
        </div>
        <div class="device-list" id="deviceList" style="padding:8px;">
          <div class="empty-state">
            <div class="empty-icon">📡</div>
            <div class="empty-msg">Start a scan to discover BLE devices</div>
          </div>
        </div>
      </div>
    </aside>

    <!-- ── Center ────────────────────────────── -->
    <main class="center">
      <!-- Telemetry cards -->
      <div class="panel">
        <div class="panel-header"><span class="dot" style="background:var(--accent2)"></span>Live Telemetry</div>
        <div class="panel-body">
          <div class="cards-grid" id="telemetryCards">
            <!-- Battery -->
            <div class="data-card" id="card-battery">
              <div class="data-card-icon">🔋</div>
              <div class="data-card-label">Battery</div>
              <div><span class="data-card-value" id="val-battery">–</span><span class="data-card-unit"> %</span></div>
            </div>
            <!-- Speed -->
            <div class="data-card" id="card-speed">
              <div class="data-card-icon">⚡</div>
              <div class="data-card-label">Speed</div>
              <div><span class="data-card-value" id="val-speed">–</span><span class="data-card-unit"> km/h</span></div>
            </div>
            <!-- RSSI -->
            <div class="data-card" id="card-rssi">
              <div class="data-card-icon">📶</div>
              <div class="data-card-label">RSSI</div>
              <div><span class="data-card-value" id="val-rssi">–</span><span class="data-card-unit"> dBm</span></div>
            </div>
            <!-- Temperature -->
            <div class="data-card" id="card-temp">
              <div class="data-card-icon">🌡️</div>
              <div class="data-card-label">Temperature</div>
              <div><span class="data-card-value" id="val-temp">–</span><span class="data-card-unit"> °C</span></div>
            </div>
            <!-- Lock status -->
            <div class="data-card" id="card-lock">
              <div class="data-card-icon">🔒</div>
              <div class="data-card-label">Lock Status</div>
              <div><span class="data-card-value" id="val-lock" style="font-size:18px;">–</span></div>
            </div>
            <!-- MTU -->
            <div class="data-card" id="card-mtu">
              <div class="data-card-icon">📐</div>
              <div class="data-card-label">MTU</div>
              <div><span class="data-card-value" id="val-mtu">–</span><span class="data-card-unit"> bytes</span></div>
            </div>
          </div>
        </div>
      </div>

      <!-- Device info -->
      <div class="panel" id="deviceInfoPanel" style="display:none;">
        <div class="panel-header"><span class="dot" style="background:var(--success)"></span>Connected Device</div>
        <div class="panel-body">
          <div style="display:grid;grid-template-columns:1fr 1fr;gap:12px;" id="deviceInfoGrid"></div>
        </div>
      </div>

      <!-- Actions -->
      <div class="panel">
        <div class="panel-header"><span class="dot" style="background:var(--warning)"></span>Actions</div>
        <div class="panel-body" style="display:flex;flex-direction:column;gap:14px;">
          <div class="actions-row">
            <button class="btn btn-warning" onclick="sendCommand('lock')" id="btnLock" disabled>🔒 Lock</button>
            <button class="btn btn-success" onclick="sendCommand('unlock')" id="btnUnlock" disabled>🔓 Unlock</button>
            <button class="btn btn-ghost" onclick="sendCommand('get_status')" id="btnStatus" disabled>📊 Get Status</button>
            <button class="btn btn-ghost" onclick="sendCommand('get_battery')" id="btnBattery" disabled>🔋 Get Battery</button>
            <button class="btn btn-ghost" onclick="sendCommand('get_speed')" id="btnSpeed" disabled>⚡ Get Speed</button>
            <button class="btn btn-ghost" onclick="sendCommand('get_serial')" id="btnSerial" disabled>🏷️ Get Serial</button>
          </div>
          <!-- Custom write -->
          <div>
            <div class="text-muted text-sm" style="margin-bottom:6px;font-weight:600;">Custom Write</div>
            <div class="input-row">
              <input type="text" id="writeUuid" placeholder="Characteristic UUID" />
              <input type="text" id="writeHex" placeholder="Hex data (e.g. 0102ff)" style="flex:.7;" />
              <button class="btn btn-primary btn-sm" onclick="doCustomWrite()" id="btnWrite" disabled>Write</button>
            </div>
          </div>
          <!-- Custom read -->
          <div>
            <div class="text-muted text-sm" style="margin-bottom:6px;font-weight:600;">Custom Read</div>
            <div class="input-row">
              <input type="text" id="readUuid" placeholder="Characteristic UUID" />
              <button class="btn btn-ghost btn-sm" onclick="doCustomRead()" id="btnRead" disabled>Read</button>
            </div>
          </div>
          <!-- Subscribe / Unsubscribe -->
          <div>
            <div class="text-muted text-sm" style="margin-bottom:6px;font-weight:600;">Notifications</div>
            <div class="input-row">
              <input type="text" id="notifyUuid" placeholder="Characteristic UUID" />
              <button class="btn btn-primary btn-sm" onclick="doNotifyStart()" id="btnNotifyStart" disabled>Subscribe</button>
              <button class="btn btn-ghost btn-sm" onclick="doNotifyStop()" id="btnNotifyStop" disabled>Unsubscribe</button>
            </div>
          </div>
        </div>
      </div>

      <!-- Services -->
      <div class="panel" id="servicesPanel">
        <div class="panel-header flex items-center justify-between" style="display:flex;justify-content:space-between;">
          <span style="display:flex;align-items:center;gap:8px;">
            <span class="dot" style="background:var(--info)"></span>GATT Services
          </span>
          <span id="servicesCount" class="badge badge-muted">–</span>
        </div>
        <div class="panel-body" id="servicesList">
          <div class="empty-state">
            <div class="empty-icon">🔍</div>
            <div class="empty-msg">Connect to a device to explore its GATT services</div>
          </div>
        </div>
      </div>
    </main>

    <!-- ── Right panel (event log) ────────────── -->
    <aside class="rightpanel">
      <div class="panel-header flex items-center justify-between" style="display:flex;justify-content:space-between;padding:12px 16px;">
        <span style="display:flex;align-items:center;gap:8px;">
          <span class="dot" style="background:var(--accent)"></span>Event Log
        </span>
        <button class="btn btn-ghost btn-xs" onclick="clearLog()">Clear</button>
      </div>
      <div class="event-log" id="eventLog">
        <div class="log-entry log-state">
          <span class="log-time">--:--:--</span>
          <span class="log-type">INFO</span>
          <span class="log-msg">Dashboard loaded. Click Scan to discover BLE devices.</span>
        </div>
      </div>
    </aside>
  </div>
</div>

<script>
// ── State ─────────────────────────────────────────────────────────────────
let state = {
  status: 'disconnected',
  isConnected: false,
  connectedDevice: null,
  devices: {},
  selectedAddress: null,
};

// ── SSE connection ─────────────────────────────────────────────────────────
let evtSource = null;

function connectSSE() {
  if (evtSource) evtSource.close();
  evtSource = new EventSource('/events');
  evtSource.onopen = () => {
    document.getElementById('sseStatus').textContent = '● SSE';
    document.getElementById('sseStatus').className = 'badge badge-success';
    addLog('state', 'SSE stream connected');
  };
  evtSource.onerror = () => {
    document.getElementById('sseStatus').textContent = '● SSE';
    document.getElementById('sseStatus').className = 'badge badge-error';
    addLog('error', 'SSE stream disconnected – retrying…');
    setTimeout(connectSSE, 3000);
  };
  evtSource.onmessage = (e) => {
    try {
      const evt = JSON.parse(e.data);
      handleEvent(evt);
    } catch (err) {
      console.error('SSE parse error', err, e.data);
    }
  };
}

function handleEvent(evt) {
  addLog(evt.type, evt.message, evt.data);
  switch (evt.type) {
    case 'state':
      if (evt.data) updateConnectionState(evt.data.state, evt.data.is_connected);
      break;
    case 'scan':
      if (evt.data && evt.data.device) {
        state.devices[evt.data.device.address] = evt.data.device;
        renderDeviceList();
      }
      break;
    case 'connect':
      if (evt.data) {
        state.connectedDevice = evt.data;
        updateConnectionState('connected', true);
        updateDeviceInfo(evt.data);
        updateMetricCard('mtu', evt.data.mtu, 'bytes');
      }
      break;
    case 'disconnect':
      updateConnectionState('disconnected', false);
      clearTelemetry();
      break;
    case 'data':
      if (evt.data && evt.data.services) renderServices(evt.data.services);
      break;
    case 'notify':
      if (evt.data) updateTelemetryFromNotify(evt.data);
      break;
  }
}

// ── Telemetry helpers ──────────────────────────────────────────────────────
function updateTelemetryFromNotify(data) {
  // For standard Battery Level characteristic
  if (data.uuid && data.uuid.toLowerCase().includes('2a19') && data.data.length >= 1) {
    updateMetricCard('battery', data.data[0], '%');
  }
}

function updateMetricCard(id, value, unit) {
  const el = document.getElementById('val-' + id);
  if (el && value != null) el.textContent = value;
}

function clearTelemetry() {
  ['battery','speed','rssi','temp','lock','mtu'].forEach(id => {
    const el = document.getElementById('val-' + id);
    if (el) el.textContent = '–';
  });
  document.getElementById('deviceInfoPanel').style.display = 'none';
  renderServices([]);
}

// ── Connection state UI ────────────────────────────────────────────────────
const stateColors = {
  disconnected: '#64748b', scanning: '#f59e0b',
  connecting: '#4f8ef7', connected: '#10b981',
  disconnecting: '#f59e0b', error: '#ef4444'
};

function updateConnectionState(s, isConnected) {
  state.status = s;
  state.isConnected = isConnected;
  const dot = document.getElementById('statusDot');
  const text = document.getElementById('statusText');
  dot.className = 'status-indicator state-' + s;
  text.textContent = s.charAt(0).toUpperCase() + s.slice(1);

  if (!isConnected) {
    state.connectedDevice = null;
    document.getElementById('connectedDeviceName').textContent = '';
  }

  // Enable/disable buttons based on state
  const connected = isConnected;
  const scanning = s === 'scanning';
  const busy = ['scanning','connecting','disconnecting'].includes(s);

  document.getElementById('btnScan').disabled = scanning;
  document.getElementById('btnDisconnect').disabled = !connected;
  ['btnLock','btnUnlock','btnStatus','btnBattery','btnSpeed','btnSerial',
   'btnWrite','btnRead','btnNotifyStart','btnNotifyStop'].forEach(id => {
    const el = document.getElementById(id);
    if (el) el.disabled = !connected;
  });
}

function updateDeviceInfo(info) {
  const panel = document.getElementById('deviceInfoPanel');
  const grid = document.getElementById('deviceInfoGrid');
  panel.style.display = '';
  document.getElementById('connectedDeviceName').textContent =
    info.name ? ` — ${info.name}` : '';

  const fields = [
    ['Address', info.address || '–'],
    ['Name', info.name || '–'],
    ['MTU', info.mtu ? info.mtu + ' bytes' : '–'],
    ['Services', info.services_count != null ? info.services_count : '–'],
  ];
  grid.innerHTML = fields.map(([label, value]) => `
    <div>
      <div class="text-muted text-sm">${label}</div>
      <div style="font-weight:600;font-family:monospace;font-size:13px;">${value}</div>
    </div>
  `).join('');

  // Update RSSI if device info available
  const dev = state.devices[info.address];
  if (dev) updateMetricCard('rssi', dev.rssi, 'dBm');
}

// ── Device list ────────────────────────────────────────────────────────────
function renderDeviceList() {
  const list = document.getElementById('deviceList');
  const devs = Object.values(state.devices);
  document.getElementById('deviceCount').textContent = devs.length;

  if (devs.length === 0) {
    list.innerHTML = `<div class="empty-state">
      <div class="empty-icon">📡</div>
      <div class="empty-msg">Start a scan to discover BLE devices</div>
    </div>`;
    return;
  }

  // Sort by RSSI (strongest first)
  devs.sort((a, b) => (b.rssi || -999) - (a.rssi || -999));

  list.innerHTML = devs.map(d => {
    const isSelected = d.address === state.selectedAddress;
    const rssiBar = rssiToBar(d.rssi);
    const isNinebot = (d.service_uuids || []).some(u => u.includes('6e400001'));
    return `<div class="device-item${isSelected ? ' selected' : ''}"
        onclick="selectDevice('${d.address}')">
      <div class="device-name">${escHtml(d.name || 'Unknown')}${isNinebot ? ' 🛴' : ''}</div>
      <div class="device-addr">${d.address}</div>
      <div class="device-rssi">${rssiBar} ${d.rssi} dBm</div>
    </div>`;
  }).join('');
}

function rssiToBar(rssi) {
  if (!rssi) return '▁▁▁▁';
  if (rssi > -60) return '▇▇▇▇';
  if (rssi > -70) return '▇▇▇▁';
  if (rssi > -80) return '▇▇▁▁';
  return '▇▁▁▁';
}

function selectDevice(address) {
  state.selectedAddress = address;
  renderDeviceList();
  if (!state.isConnected) {
    doConnect(address);
  }
}

// ── Services tree ──────────────────────────────────────────────────────────
function renderServices(services) {
  const el = document.getElementById('servicesList');
  document.getElementById('servicesCount').textContent = services.length || '–';

  if (!services || services.length === 0) {
    el.innerHTML = `<div class="empty-state">
      <div class="empty-icon">🔍</div>
      <div class="empty-msg">Connect to a device to explore its GATT services</div>
    </div>`;
    return;
  }

  el.innerHTML = services.map((svc, si) => {
    const chars = (svc.characteristics || []).map(c => {
      const props = (c.properties || []).map(p =>
        `<span class="prop-badge ${p}">${p}</span>`
      ).join('');
      const canRead = c.properties.includes('read');
      const canWrite = c.properties.includes('write') || c.properties.includes('write-without-response');
      const canNotify = c.properties.includes('notify') || c.properties.includes('indicate');
      return `<div class="char-item">
        <span class="char-uuid" title="${c.uuid}">${shortUuid(c.uuid)}</span>
        <span class="char-props">${props}</span>
        <span class="char-actions">
          ${canRead ? `<button class="btn btn-ghost btn-xs" onclick="doRead('${c.uuid}')">R</button>` : ''}
          ${canWrite ? `<button class="btn btn-ghost btn-xs" onclick="prefillWrite('${c.uuid}')">W</button>` : ''}
          ${canNotify ? `<button class="btn btn-ghost btn-xs" onclick="doNotify('${c.uuid}')">N</button>` : ''}
        </span>
      </div>`;
    }).join('');

    return `<div class="service-item">
      <div class="service-header" onclick="toggleService(this)">
        <span>▶</span>
        <span>${shortUuid(svc.uuid)}</span>
        <span class="service-uuid">${svc.uuid}</span>
        <span class="badge badge-muted">${(svc.characteristics||[]).length} chars</span>
      </div>
      <div class="char-list" style="display:none;">${chars || '<div class="text-muted text-sm">No characteristics</div>'}</div>
    </div>`;
  }).join('');
}

function toggleService(header) {
  const body = header.nextElementSibling;
  const arrow = header.querySelector('span');
  const open = body.style.display !== 'none';
  body.style.display = open ? 'none' : '';
  arrow.textContent = open ? '▶' : '▼';
}

// ── API helpers ────────────────────────────────────────────────────────────
async function api(method, path, body) {
  const opts = { method, headers: {'Content-Type':'application/json'} };
  if (body) opts.body = JSON.stringify(body);
  const r = await fetch(path, opts);
  if (!r.ok) {
    const err = await r.text();
    addLog('error', `API ${path} failed: ${err}`);
    throw new Error(err);
  }
  return r.json();
}

async function doScan() {
  state.devices = {};
  renderDeviceList();
  const dur = parseInt(document.getElementById('scanDuration').value) || 10;
  try { await api('POST', '/api/scan', {duration: dur}); }
  catch(e) { addLog('error', 'Scan request failed: ' + e.message); }
}

async function doConnect(address) {
  try { await api('POST', '/api/connect', {address}); }
  catch(e) { addLog('error', 'Connect failed: ' + e.message); }
}

async function doDisconnect() {
  try { await api('POST', '/api/disconnect', {}); }
  catch(e) { addLog('error', 'Disconnect failed: ' + e.message); }
}

async function sendCommand(name) {
  try { await api('POST', '/api/command', {name}); }
  catch(e) { addLog('error', `Command ${name} failed: ` + e.message); }
}

async function doCustomWrite() {
  const uuid = document.getElementById('writeUuid').value.trim();
  const hex = document.getElementById('writeHex').value.trim().replace(/\s+/g, '');
  if (!uuid || !hex) { addLog('error', 'UUID and hex data required'); return; }
  const data = [];
  for (let i = 0; i < hex.length; i += 2)
    data.push(parseInt(hex.substring(i, i+2), 16));
  try { await api('POST', '/api/write', {uuid, data}); }
  catch(e) { addLog('error', 'Write failed: ' + e.message); }
}

async function doCustomRead() {
  const uuid = document.getElementById('readUuid').value.trim();
  if (!uuid) { addLog('error', 'UUID required'); return; }
  try { await api('POST', '/api/read', {uuid}); }
  catch(e) { addLog('error', 'Read failed: ' + e.message); }
}

function doRead(uuid) {
  document.getElementById('readUuid').value = uuid;
  doCustomRead();
}

function prefillWrite(uuid) {
  document.getElementById('writeUuid').value = uuid;
  document.getElementById('writeHex').focus();
}

function doNotify(uuid) {
  document.getElementById('notifyUuid').value = uuid;
  doNotifyStart();
}

async function doNotifyStart() {
  const uuid = document.getElementById('notifyUuid').value.trim();
  if (!uuid) { addLog('error', 'UUID required'); return; }
  try { await api('POST', '/api/notify/start', {uuid}); }
  catch(e) { addLog('error', 'Subscribe failed: ' + e.message); }
}

async function doNotifyStop() {
  const uuid = document.getElementById('notifyUuid').value.trim();
  if (!uuid) { addLog('error', 'UUID required'); return; }
  try { await api('POST', '/api/notify/stop', {uuid}); }
  catch(e) { addLog('error', 'Unsubscribe failed: ' + e.message); }
}

async function refreshStatus() {
  try {
    const s = await api('GET', '/api/status');
    state.isConnected = s.is_connected;
    state.status = s.state;
    updateConnectionState(s.state, s.is_connected);

    if (s.discovered_devices) {
      s.discovered_devices.forEach(d => { state.devices[d.address] = d; });
      renderDeviceList();
    }
    if (s.services && s.services.length) renderServices(s.services);
    if (s.connected_device) {
      updateDeviceInfo(s.connected_device);
      if (s.connected_device.rssi != null)
        updateMetricCard('rssi', s.connected_device.rssi, 'dBm');
      if (s.connected_device.mtu != null)
        updateMetricCard('mtu', s.connected_device.mtu, 'bytes');
    }
    addLog('state', 'Status refreshed – ' + s.state);
  } catch(e) { addLog('error', 'Refresh failed: ' + e.message); }
}

// ── Event log ──────────────────────────────────────────────────────────────
const MAX_LOG_ENTRIES = 500;

function addLog(type, msg, _data) {
  const log = document.getElementById('eventLog');
  const now = new Date();
  const time = `${pad(now.getHours())}:${pad(now.getMinutes())}:${pad(now.getSeconds())}`;
  const entry = document.createElement('div');
  entry.className = `log-entry log-${type}`;
  entry.innerHTML = `<span class="log-time">${time}</span>` +
    `<span class="log-type">${type.toUpperCase()}</span>` +
    `<span class="log-msg">${escHtml(msg)}</span>`;
  log.appendChild(entry);
  // Keep log capped
  while (log.children.length > MAX_LOG_ENTRIES) log.removeChild(log.firstChild);
  log.scrollTop = log.scrollHeight;
}

function clearLog() {
  document.getElementById('eventLog').innerHTML = '';
}

function pad(n) { return String(n).padStart(2, '0'); }

function escHtml(s) {
  return String(s)
    .replace(/&/g,'&amp;').replace(/</g,'&lt;')
    .replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}

function shortUuid(uuid) {
  if (!uuid) return '?';
  // 16-bit Bluetooth SIG UUIDs: 0000XXXX-0000-1000-...
  const m = uuid.match(/^0000([0-9a-f]{4})-0000-1000-8000-00805f9b34fb$/i);
  if (m) return `0x${m[1].toUpperCase()}`;
  // Ninebot / custom – show first 8 chars
  return uuid.substring(0, 8) + '…';
}

// ── PWA – Service Worker registration ─────────────────────────────────────
if ('serviceWorker' in navigator) {
  window.addEventListener('load', async () => {
    try {
      const reg = await navigator.serviceWorker.register('/sw.js', {scope: '/'});
      reg.addEventListener('updatefound', () => {
        addLog('state', 'PWA update available – reload to apply');
      });
      addLog('state', 'PWA service worker ready');
    } catch (err) {
      // SW may fail in non-secure contexts (non-localhost) – that's OK
      console.warn('Service worker registration failed:', err);
    }
  });
}

// ── PWA – Install prompt ───────────────────────────────────────────────────
let _installPrompt = null;

window.addEventListener('beforeinstallprompt', (e) => {
  e.preventDefault();
  _installPrompt = e;
  const btn = document.getElementById('btnInstall');
  if (btn) {
    btn.style.display = '';
    addLog('state', 'App is installable – click "📲 Install App" to add to your home screen');
  }
});

window.addEventListener('appinstalled', () => {
  _installPrompt = null;
  const btn = document.getElementById('btnInstall');
  if (btn) btn.style.display = 'none';
  addLog('state', '✅ App installed successfully!');
});

async function doInstall() {
  if (!_installPrompt) return;
  _installPrompt.prompt();
  const { outcome } = await _installPrompt.userChoice;
  addLog('state', `Install ${outcome === 'accepted' ? 'accepted ✅' : 'dismissed'}`);
  _installPrompt = null;
  document.getElementById('btnInstall').style.display = 'none';
}

// ── Handle URL shortcuts (e.g. ?action=scan) ─────────────────────────────
(function () {
  const params = new URLSearchParams(window.location.search);
  if (params.get('action') === 'scan') {
    // Delay lets the SSE connection and initial status refresh complete first
    // so scan events are displayed correctly from the moment they arrive.
    window.addEventListener('load', () => setTimeout(doScan, 1200));
  }
})();

// ── Init ───────────────────────────────────────────────────────────────────
connectSSE();
refreshStatus();
</script>
</body>
</html>"""


# ---------------------------------------------------------------------------
# Server Application
# ---------------------------------------------------------------------------

class DashboardServer:
    """
    aiohttp-based web server that bridges the BLE Manager with a browser UI.

    The browser receives real-time events via Server-Sent Events and sends
    commands via REST API endpoints.
    """

    def __init__(self, ble: BLEManager, host: str = "127.0.0.1", port: int = 8765) -> None:
        self.ble = ble
        self.host = host
        self.port = port
        self._sse_queues: list[asyncio.Queue] = []
        self._app = self._build_app()

    # ------------------------------------------------------------------
    # SSE broadcast
    # ------------------------------------------------------------------

    def _broadcast_event(self, event: BLEEvent) -> None:
        """Push a BLE event to all connected SSE clients."""
        payload = json.dumps(event.to_dict())
        dead: list[asyncio.Queue] = []
        for q in self._sse_queues:
            try:
                q.put_nowait(payload)
            except asyncio.QueueFull:
                dead.append(q)
        for q in dead:
            self._sse_queues.remove(q)

    async def _ble_event_callback(self, event: BLEEvent) -> None:
        self._broadcast_event(event)

    # ------------------------------------------------------------------
    # App & route setup
    # ------------------------------------------------------------------

    def _build_app(self) -> web.Application:
        app = web.Application()
        # PWA assets
        app.router.add_get("/manifest.json", self._handle_manifest)
        app.router.add_get("/sw.js", self._handle_sw)
        app.router.add_get("/icon-192.png", self._handle_icon_192)
        app.router.add_get("/icon-512.png", self._handle_icon_512)
        # Dashboard + SSE
        app.router.add_get("/", self._handle_index)
        app.router.add_get("/events", self._handle_sse)
        # REST API
        app.router.add_get("/api/status", self._handle_status)
        app.router.add_get("/api/devices", self._handle_devices)
        app.router.add_get("/api/services", self._handle_services)
        app.router.add_post("/api/scan", self._handle_scan)
        app.router.add_post("/api/connect", self._handle_connect)
        app.router.add_post("/api/disconnect", self._handle_disconnect)
        app.router.add_post("/api/read", self._handle_read)
        app.router.add_post("/api/write", self._handle_write)
        app.router.add_post("/api/notify/start", self._handle_notify_start)
        app.router.add_post("/api/notify/stop", self._handle_notify_stop)
        app.router.add_post("/api/command", self._handle_command)
        return app

    # ------------------------------------------------------------------
    # HTTP handlers
    # ------------------------------------------------------------------

    async def _handle_manifest(self, _req: web.Request) -> web.Response:
        return web.Response(
            text=_MANIFEST_JSON,
            content_type="application/manifest+json",
            charset="utf-8",
        )

    async def _handle_sw(self, _req: web.Request) -> web.Response:
        return web.Response(
            text=_SERVICE_WORKER_JS,
            content_type="application/javascript",
            charset="utf-8",
            headers={"Service-Worker-Allowed": "/"},
        )

    async def _handle_icon_192(self, _req: web.Request) -> web.Response:
        return web.Response(
            body=_ICON_192,
            content_type="image/png",
            headers={"Cache-Control": "public, max-age=86400"},
        )

    async def _handle_icon_512(self, _req: web.Request) -> web.Response:
        return web.Response(
            body=_ICON_512,
            content_type="image/png",
            headers={"Cache-Control": "public, max-age=86400"},
        )

    async def _handle_index(self, _req: web.Request) -> web.Response:
        html = _DASHBOARD_HTML.replace(
            "bleak dashboard v1.0",
            f"bleak dashboard v{DASHBOARD_VERSION}",
        )
        return web.Response(
            text=html,
            content_type="text/html",
            charset="utf-8",
        )

    async def _handle_sse(self, req: web.Request) -> web.StreamResponse:
        resp = web.StreamResponse(
            status=200,
            headers={
                "Content-Type": "text/event-stream",
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
                "Access-Control-Allow-Origin": "*",
            },
        )
        await resp.prepare(req)

        queue: asyncio.Queue = asyncio.Queue(maxsize=200)
        self._sse_queues.append(queue)
        try:
            # Send current status as first event
            status = self.ble.get_status()
            init_event = BLEEvent(
                event_type="state",
                message=f"Dashboard connected – state: {status['state']}",
                data={"state": status["state"], "is_connected": status["is_connected"]},
            )
            await resp.write(
                f"data: {json.dumps(init_event.to_dict())}\n\n".encode()
            )

            while True:
                try:
                    payload = await asyncio.wait_for(queue.get(), timeout=30)
                    await resp.write(f"data: {payload}\n\n".encode())
                except asyncio.TimeoutError:
                    # Heartbeat to keep connection alive
                    await resp.write(b": heartbeat\n\n")
        except (ConnectionResetError, asyncio.CancelledError):
            pass
        finally:
            if queue in self._sse_queues:
                self._sse_queues.remove(queue)
        return resp

    async def _handle_status(self, _req: web.Request) -> web.Response:
        return web.json_response(self.ble.get_status())

    async def _handle_devices(self, _req: web.Request) -> web.Response:
        return web.json_response(
            [d.to_dict() for d in self.ble.discovered_devices]
        )

    async def _handle_services(self, _req: web.Request) -> web.Response:
        return web.json_response(
            [s.to_dict() for s in self.ble.services]
        )

    async def _handle_scan(self, req: web.Request) -> web.Response:
        body = await self._parse_body(req)
        duration = float(body.get("duration", 10))
        service_uuids = body.get("service_uuids") or []
        # Run scan in background so the request returns immediately
        asyncio.ensure_future(
            self.ble.start_scan(duration=duration, service_uuids=service_uuids)
        )
        return web.json_response({"status": "scan started", "duration": duration})

    async def _handle_connect(self, req: web.Request) -> web.Response:
        body = await self._parse_body(req)
        address = body.get("address", "")
        if not address:
            return web.Response(status=400, text="Missing 'address'")
        timeout = float(body.get("timeout", 20))
        asyncio.ensure_future(self.ble.connect(address, timeout=timeout))
        return web.json_response({"status": "connecting", "address": address})

    async def _handle_disconnect(self, _req: web.Request) -> web.Response:
        asyncio.ensure_future(self.ble.disconnect())
        return web.json_response({"status": "disconnecting"})

    async def _handle_read(self, req: web.Request) -> web.Response:
        body = await self._parse_body(req)
        uuid = body.get("uuid", "")
        if not uuid:
            return web.Response(status=400, text="Missing 'uuid'")
        asyncio.ensure_future(self.ble.read_characteristic(uuid))
        return web.json_response({"status": "reading", "uuid": uuid})

    async def _handle_write(self, req: web.Request) -> web.Response:
        body = await self._parse_body(req)
        uuid = body.get("uuid", "")
        data_list = body.get("data", [])
        if not uuid or not isinstance(data_list, list):
            return web.Response(status=400, text="Missing 'uuid' or 'data'")
        data = bytes(data_list)
        with_response = bool(body.get("with_response", True))
        asyncio.ensure_future(
            self.ble.write_characteristic(uuid, data, with_response=with_response)
        )
        return web.json_response({"status": "writing", "uuid": uuid, "bytes": len(data)})

    async def _handle_notify_start(self, req: web.Request) -> web.Response:
        body = await self._parse_body(req)
        uuid = body.get("uuid", "")
        if not uuid:
            return web.Response(status=400, text="Missing 'uuid'")
        asyncio.ensure_future(self.ble.start_notify(uuid))
        return web.json_response({"status": "subscribing", "uuid": uuid})

    async def _handle_notify_stop(self, req: web.Request) -> web.Response:
        body = await self._parse_body(req)
        uuid = body.get("uuid", "")
        if not uuid:
            return web.Response(status=400, text="Missing 'uuid'")
        asyncio.ensure_future(self.ble.stop_notify(uuid))
        return web.json_response({"status": "unsubscribing", "uuid": uuid})

    async def _handle_command(self, req: web.Request) -> web.Response:
        body = await self._parse_body(req)
        name = body.get("name", "")
        commands: dict[str, bytes] = {
            "lock":        CMD_LOCK,
            "unlock":      CMD_UNLOCK,
            "get_status":  CMD_GET_STATUS,
            "get_battery": CMD_GET_BATTERY_INFO,
            "get_speed":   CMD_GET_SPEED,
            "get_serial":  CMD_GET_SERIAL,
        }
        if name not in commands:
            return web.Response(
                status=400,
                text=f"Unknown command '{name}'. Valid: {list(commands.keys())}",
            )
        asyncio.ensure_future(
            self.ble.write_characteristic(
                NINEBOT_RX_UUID, commands[name], with_response=True
            )
        )
        return web.json_response({"status": "command sent", "name": name})

    # ------------------------------------------------------------------
    # Helper
    # ------------------------------------------------------------------

    @staticmethod
    async def _parse_body(req: web.Request) -> dict[str, Any]:
        try:
            return await req.json()
        except Exception:
            return {}

    # ------------------------------------------------------------------
    # Server lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Register BLE event callback and start the HTTP server."""
        self.ble.add_event_callback(self._ble_event_callback)
        runner = web.AppRunner(self._app, access_log=None)
        await runner.setup()
        site = web.TCPSite(runner, self.host, self.port)
        await site.start()
        logger.info("Dashboard running at http://%s:%s", self.host, self.port)
        print(f"\n  BLE Dashboard →  http://{self.host}:{self.port}\n")
