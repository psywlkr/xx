"""
BLE Dashboard – entry point.

Usage:
    python -m examples.dashboard          # from repo root
    python examples/dashboard/main.py     # direct invocation

The dashboard opens a browser and starts a web server on http://127.0.0.1:8765.
BLE scanning, connecting, and data operations are triggered from the browser UI.

Requirements:
    pip install aiohttp
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import webbrowser

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
    parser.add_argument("--host", default="127.0.0.1", help="Server host (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8765, help="Server port (default: 8765)")
    parser.add_argument("--no-browser", action="store_true", help="Do not open browser automatically")
    parser.add_argument("--verbose", "-v", action="store_true", help="Enable verbose logging")
    args = parser.parse_args()

    _setup_logging(args.verbose)
    asyncio.run(_run(args.host, args.port, args.no_browser))


if __name__ == "__main__":
    main()
