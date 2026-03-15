"""
BLE Dashboard – entry point.

Usage:
    python -m examples.dashboard          # from repo root
    python examples/dashboard/main.py     # direct invocation

The dashboard opens a browser and starts a web server on http://127.0.0.1:8765.
BLE scanning, connecting, and data operations are triggered from the browser UI.

Requirements:
    pip install aiohttp

Android (APK):
    Built with Buildozer from examples/dashboard/android/.
    The Kivy app starts the aiohttp server in a background thread and displays
    the dashboard UI in a full-screen Android WebView.
"""
from __future__ import annotations

import asyncio
import logging
import threading

# ── Android detection ─────────────────────────────────────────────────────
# The ``android`` package is only present when running inside a p4a APK.
try:
    import android  # noqa: F401
    _ANDROID = True
except ImportError:
    _ANDROID = False

# ── Dashboard package imports ─────────────────────────────────────────────
try:
    from .ble_manager import BLEManager
    from .server import DashboardServer
except ImportError:
    # Support running as a plain script: python examples/dashboard/main.py
    import sys
    import os
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))
    from examples.dashboard.ble_manager import BLEManager
    from examples.dashboard.server import DashboardServer

_SERVER_HOST = "127.0.0.1"
_SERVER_PORT = 8765
# Time (seconds) the Kivy WebView waits before loading the dashboard URL,
# giving aiohttp time to bind to the port on slower Android devices.
_WEBVIEW_STARTUP_DELAY_S = 1.5


def _setup_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    # Quiet down noisy third-party loggers unless in verbose mode
    if not verbose:
        for name in ("bleak", "aiohttp.access", "asyncio"):
            logging.getLogger(name).setLevel(logging.WARNING)


def _run_server_forever() -> None:
    """Run the aiohttp dashboard server in a dedicated background event loop.

    Runs indefinitely until the process exits.  Intended to be called from a
    daemon thread (Android) so it does not block the main Kivy event loop.
    """
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    async def _inner() -> None:
        ble = BLEManager()
        server = DashboardServer(ble, host=_SERVER_HOST, port=_SERVER_PORT)
        await server.start()
        try:
            while True:
                await asyncio.sleep(3600)
        except asyncio.CancelledError:
            pass
        finally:
            if ble.is_connected:
                await ble.disconnect()

    loop.run_until_complete(_inner())


# ── Android entry point ───────────────────────────────────────────────────
if _ANDROID:
    from kivy.app import App  # type: ignore[import]
    from kivy.clock import Clock  # type: ignore[import]
    from kivy.uix.widget import Widget  # type: ignore[import]
    from android.runnable import run_on_ui_thread  # type: ignore[import]
    from jnius import autoclass  # type: ignore[import]

    _WebView = autoclass("android.webkit.WebView")
    _WebSettings = autoclass("android.webkit.WebSettings")
    _WebViewClient = autoclass("android.webkit.WebViewClient")
    _LayoutParams = autoclass("android.view.ViewGroup$LayoutParams")
    _PythonActivity = autoclass("org.kivy.android.PythonActivity")

    class _DashboardApp(App):
        """Kivy application shell that hosts the BLE dashboard in a WebView."""

        title = "BLE Dashboard"

        def build(self) -> Widget:
            _setup_logging(False)
            t = threading.Thread(
                target=_run_server_forever, daemon=True, name="ble-server"
            )
            t.start()
            # Give the server time to start before loading the WebView
            Clock.schedule_once(self._attach_webview, _WEBVIEW_STARTUP_DELAY_S)
            return Widget()

        @run_on_ui_thread  # type: ignore[misc]
        def _attach_webview(self, _dt: float) -> None:
            """Create and attach a full-screen Android WebView on the UI thread."""
            activity = _PythonActivity.mActivity
            wv = _WebView(activity)
            s = wv.getSettings()
            s.setJavaScriptEnabled(True)
            s.setDomStorageEnabled(True)
            s.setCacheMode(_WebSettings.LOAD_NO_CACHE)
            s.setBuiltInZoomControls(False)
            s.setDisplayZoomControls(False)
            wv.setWebViewClient(_WebViewClient())
            lp = _LayoutParams(
                _LayoutParams.MATCH_PARENT,
                _LayoutParams.MATCH_PARENT,
            )
            activity.addContentView(wv, lp)
            wv.loadUrl(f"http://{_SERVER_HOST}:{_SERVER_PORT}")

    def main() -> None:
        _DashboardApp().run()

# ── Desktop / server entry point ──────────────────────────────────────────
else:
    import argparse
    import webbrowser

    async def _run(host: str, port: int, no_browser: bool) -> None:
        ble = BLEManager()
        server = DashboardServer(ble, host=host, port=port)
        await server.start()

        url = f"http://{host}:{port}"
        if not no_browser:
            # Give the server a moment to bind before opening the browser
            await asyncio.sleep(0.5)
            webbrowser.open(url)

        # Run until interrupted
        try:
            while True:
                await asyncio.sleep(3600)
        except (KeyboardInterrupt, asyncio.CancelledError):
            pass
        finally:
            # Best-effort cleanup
            if ble.is_connected:
                await ble.disconnect()

    def main() -> None:
        parser = argparse.ArgumentParser(description="BLE Dashboard")
        parser.add_argument("--host", default=_SERVER_HOST, help="Server host (default: 127.0.0.1)")
        parser.add_argument("--port", type=int, default=_SERVER_PORT, help="Server port (default: 8765)")
        parser.add_argument("--no-browser", action="store_true", help="Do not open browser automatically")
        parser.add_argument("--verbose", "-v", action="store_true", help="Enable verbose logging")
        args = parser.parse_args()

        _setup_logging(args.verbose)
        asyncio.run(_run(args.host, args.port, args.no_browser))


if __name__ == "__main__":
    main()
