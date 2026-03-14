"""
BLE Manager - Core BLE management built on top of bleak.

Handles scanning, connection, data reading/writing, notifications, and events.
Provides an async event system so the web server can push real-time updates to
connected browsers via Server-Sent Events.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Callable, Coroutine, Optional

from bleak import BleakClient, BleakScanner
from bleak.backends.device import BLEDevice
from bleak.backends.scanner import AdvertisementData
from bleak.exc import BleakError

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Enumerations
# ---------------------------------------------------------------------------

class ConnectionState(str, Enum):
    DISCONNECTED = "disconnected"
    SCANNING = "scanning"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    DISCONNECTING = "disconnecting"
    ERROR = "error"


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class DeviceInfo:
    """Discovered BLE device with advertisement data."""

    address: str
    name: str
    rssi: int
    advertisement: AdvertisementData
    last_seen: datetime = field(default_factory=datetime.now)

    def to_dict(self) -> dict:
        return {
            "address": self.address,
            "name": self.name or "Unknown",
            "rssi": self.rssi,
            "manufacturer_data": {
                str(k): list(v)
                for k, v in self.advertisement.manufacturer_data.items()
            },
            "service_uuids": list(self.advertisement.service_uuids),
            "service_data": {
                k: list(v) for k, v in self.advertisement.service_data.items()
            },
            "tx_power": self.advertisement.tx_power,
            "local_name": self.advertisement.local_name,
            "last_seen": self.last_seen.isoformat(),
        }


@dataclass
class CharacteristicInfo:
    """GATT characteristic with properties and descriptors."""

    uuid: str
    handle: int
    properties: list[str]
    descriptors: list[dict]
    service_uuid: str

    def to_dict(self) -> dict:
        return {
            "uuid": self.uuid,
            "handle": self.handle,
            "properties": self.properties,
            "descriptors": self.descriptors,
            "service_uuid": self.service_uuid,
        }


@dataclass
class ServiceInfo:
    """GATT service with its characteristics."""

    uuid: str
    handle: int
    characteristics: list[CharacteristicInfo]

    def to_dict(self) -> dict:
        return {
            "uuid": self.uuid,
            "handle": self.handle,
            "characteristics": [c.to_dict() for c in self.characteristics],
        }


@dataclass
class BLEEvent:
    """A timestamped BLE event with type, message and optional payload."""

    event_type: str  # scan | connect | disconnect | data | notify | error | command | state
    message: str
    data: Any = None
    timestamp: datetime = field(default_factory=datetime.now)

    def to_dict(self) -> dict:
        return {
            "type": self.event_type,
            "message": self.message,
            "data": self.data,
            "timestamp": self.timestamp.isoformat(),
        }


# Type alias for event callbacks
EventCallback = Callable[[BLEEvent], Coroutine[Any, Any, None]]


# ---------------------------------------------------------------------------
# BLE Manager
# ---------------------------------------------------------------------------

class BLEManager:
    """
    Central BLE management class built on top of bleak.

    Responsibilities:
    - BLE scanning (start/stop, device discovery)
    - Device connection / disconnection
    - GATT service and characteristic enumeration
    - Characteristic read / write / notify
    - Event system for real-time UI updates
    - Telemetry cache for most-recently-received notification values
    """

    def __init__(self) -> None:
        self._state: ConnectionState = ConnectionState.DISCONNECTED
        self._client: Optional[BleakClient] = None
        self._connected_address: Optional[str] = None
        self._discovered: dict[str, DeviceInfo] = {}
        self._services: list[ServiceInfo] = []
        self._event_callbacks: list[EventCallback] = []
        self._active_notifies: set[str] = set()
        self._telemetry: dict[str, Any] = {}
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # Public properties
    # ------------------------------------------------------------------

    @property
    def state(self) -> ConnectionState:
        return self._state

    @property
    def is_connected(self) -> bool:
        return (
            self._state == ConnectionState.CONNECTED
            and self._client is not None
            and self._client.is_connected
        )

    @property
    def connected_address(self) -> Optional[str]:
        return self._connected_address

    @property
    def discovered_devices(self) -> list[DeviceInfo]:
        return list(self._discovered.values())

    @property
    def services(self) -> list[ServiceInfo]:
        return self._services

    @property
    def telemetry(self) -> dict[str, Any]:
        return dict(self._telemetry)

    # ------------------------------------------------------------------
    # Event system
    # ------------------------------------------------------------------

    def add_event_callback(self, callback: EventCallback) -> Callable[[], None]:
        """Register an async event callback.  Returns an unregister callable."""
        self._event_callbacks.append(callback)

        def unregister() -> None:
            try:
                self._event_callbacks.remove(callback)
            except ValueError:
                pass

        return unregister

    async def _emit(self, event_type: str, message: str, data: Any = None) -> None:
        event = BLEEvent(event_type=event_type, message=message, data=data)
        logger.debug("BLE Event [%s]: %s", event_type, message)
        for cb in list(self._event_callbacks):
            try:
                await cb(event)
            except Exception as exc:
                logger.error("Error in BLE event callback: %s", exc)

    # ------------------------------------------------------------------
    # Scanning
    # ------------------------------------------------------------------

    async def start_scan(
        self,
        duration: float = 10.0,
        service_uuids: Optional[list[str]] = None,
    ) -> None:
        """Scan for BLE devices for *duration* seconds."""
        async with self._lock:
            if self._state == ConnectionState.SCANNING:
                await self._emit("error", "Already scanning")
                return
            if self._state == ConnectionState.CONNECTING:
                await self._emit("error", "Cannot scan while connecting")
                return

        self._discovered.clear()
        await self._set_state(ConnectionState.SCANNING)
        await self._emit("scan", f"Starting BLE scan for {duration:.0f} s …")

        loop = asyncio.get_running_loop()

        def _detection_cb(device: BLEDevice, adv: AdvertisementData) -> None:
            name = device.name or adv.local_name or "Unknown"
            is_new = device.address not in self._discovered
            info = DeviceInfo(
                address=device.address,
                name=name,
                rssi=adv.rssi,
                advertisement=adv,
            )
            self._discovered[device.address] = info
            coro = self._emit(
                "scan",
                f"{'Found' if is_new else 'Updated'}: {name} ({device.address}) "
                f"RSSI: {adv.rssi} dBm",
                {"device": info.to_dict(), "is_new": is_new},
            )
            asyncio.run_coroutine_threadsafe(coro, loop)

        try:
            scanner = BleakScanner(
                detection_callback=_detection_cb,
                service_uuids=service_uuids or [],
            )
            async with scanner:
                await asyncio.sleep(duration)

            await self._emit(
                "scan",
                f"Scan complete – found {len(self._discovered)} device(s)",
                {"count": len(self._discovered)},
            )
        except BleakError as exc:
            logger.error("Scan error: %s", exc)
            await self._emit("error", f"Scan error: {exc}")
        except asyncio.CancelledError:
            await self._emit("scan", "Scan cancelled")
        except Exception as exc:
            logger.error("Unexpected scan error: %s", exc)
            await self._emit("error", f"Unexpected scan error: {exc}")
        finally:
            if self._state == ConnectionState.SCANNING:
                await self._set_state(ConnectionState.DISCONNECTED)

    # ------------------------------------------------------------------
    # Connection management
    # ------------------------------------------------------------------

    async def connect(self, address: str, timeout: float = 20.0) -> bool:
        """Connect to a BLE device by address."""
        if self.is_connected:
            await self._emit("error", "Already connected – disconnect first")
            return False
        if self._state in (ConnectionState.CONNECTING, ConnectionState.DISCONNECTING):
            await self._emit("error", f"Cannot connect while {self._state.value}")
            return False

        device_info = self._discovered.get(address)
        display_name = device_info.name if device_info else address

        await self._set_state(ConnectionState.CONNECTING)
        await self._emit("connect", f"Connecting to {display_name} ({address}) …")

        try:
            self._client = BleakClient(
                address,
                timeout=timeout,
                disconnected_callback=self._on_unexpected_disconnect,
            )
            await self._client.connect()

            if not self._client.is_connected:
                raise BleakError("Client reports not connected after connect()")

            self._connected_address = address
            await self._enumerate_services()
            await self._set_state(ConnectionState.CONNECTED)
            await self._emit(
                "connect",
                f"Connected to {display_name} ({address})",
                {
                    "address": address,
                    "name": display_name,
                    "mtu": self._client.mtu_size,
                    "services_count": len(self._services),
                },
            )
            return True

        except BleakError as exc:
            logger.error("Connect error: %s", exc)
            await self._cleanup_after_error()
            await self._emit("error", f"Connection failed: {exc}")
            return False
        except Exception as exc:
            logger.error("Unexpected connect error: %s", exc)
            await self._cleanup_after_error()
            await self._emit("error", f"Connection error: {exc}")
            return False

    async def disconnect(self) -> None:
        """Disconnect from the currently connected device."""
        if not self._client:
            await self._emit("error", "Not connected")
            return

        await self._set_state(ConnectionState.DISCONNECTING)
        await self._emit("disconnect", "Disconnecting …")

        try:
            await self._client.disconnect()
        except Exception as exc:
            logger.error("Disconnect error: %s", exc)
            await self._emit("error", f"Disconnect error: {exc}")
        finally:
            await self._cleanup_after_disconnect()

    def _on_unexpected_disconnect(self, _client: BleakClient) -> None:
        """Called by bleak when the device disconnects unexpectedly."""
        logger.warning("Device disconnected unexpectedly")
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = asyncio.new_event_loop()
        asyncio.run_coroutine_threadsafe(
            self._handle_unexpected_disconnect(), loop
        )

    async def _handle_unexpected_disconnect(self) -> None:
        await self._cleanup_after_disconnect()
        await self._emit("disconnect", "Device disconnected unexpectedly")

    async def _cleanup_after_disconnect(self) -> None:
        self._client = None
        self._connected_address = None
        self._services = []
        self._active_notifies.clear()
        self._telemetry = {}
        await self._set_state(ConnectionState.DISCONNECTED)
        await self._emit("disconnect", "Disconnected")

    async def _cleanup_after_error(self) -> None:
        self._client = None
        self._connected_address = None
        self._services = []
        self._active_notifies.clear()
        await self._set_state(ConnectionState.DISCONNECTED)

    # ------------------------------------------------------------------
    # Service / characteristic enumeration
    # ------------------------------------------------------------------

    async def _enumerate_services(self) -> None:
        """Walk the GATT service tree and cache results."""
        if not self._client:
            return
        self._services = []
        try:
            for svc in self._client.services:
                chars: list[CharacteristicInfo] = []
                for char in svc.characteristics:
                    descriptors = [
                        {"uuid": str(d.uuid), "handle": d.handle}
                        for d in char.descriptors
                    ]
                    chars.append(
                        CharacteristicInfo(
                            uuid=str(char.uuid),
                            handle=char.handle,
                            properties=list(char.properties),
                            descriptors=descriptors,
                            service_uuid=str(svc.uuid),
                        )
                    )
                self._services.append(
                    ServiceInfo(
                        uuid=str(svc.uuid),
                        handle=svc.handle,
                        characteristics=chars,
                    )
                )
            await self._emit(
                "data",
                f"Enumerated {len(self._services)} service(s)",
                {"services": [s.to_dict() for s in self._services]},
            )
        except Exception as exc:
            logger.error("Service enumeration error: %s", exc)
            await self._emit("error", f"Failed to enumerate services: {exc}")

    # ------------------------------------------------------------------
    # Read / Write / Notify
    # ------------------------------------------------------------------

    async def read_characteristic(self, uuid: str) -> Optional[bytes]:
        """Read a GATT characteristic by UUID. Returns raw bytes or None."""
        if not self.is_connected:
            await self._emit("error", "Not connected")
            return None
        try:
            raw = await self._client.read_gatt_char(uuid)
            data = bytes(raw)
            await self._emit(
                "data",
                f"Read {uuid}: {data.hex()} ({len(data)} bytes)",
                {"uuid": uuid, "data": list(data), "hex": data.hex()},
            )
            return data
        except BleakError as exc:
            await self._emit("error", f"Read error on {uuid}: {exc}")
            return None
        except Exception as exc:
            await self._emit("error", f"Unexpected read error on {uuid}: {exc}")
            return None

    async def write_characteristic(
        self,
        uuid: str,
        data: bytes,
        with_response: bool = True,
    ) -> bool:
        """Write *data* to a GATT characteristic by UUID."""
        if not self.is_connected:
            await self._emit("error", "Not connected")
            return False
        try:
            await self._client.write_gatt_char(uuid, data, response=with_response)
            await self._emit(
                "command",
                f"Write {uuid}: {data.hex()} ({len(data)} bytes)",
                {"uuid": uuid, "data": list(data), "hex": data.hex()},
            )
            return True
        except BleakError as exc:
            await self._emit("error", f"Write error on {uuid}: {exc}")
            return False
        except Exception as exc:
            await self._emit("error", f"Unexpected write error on {uuid}: {exc}")
            return False

    async def start_notify(
        self,
        uuid: str,
        user_callback: Optional[Callable[[bytes], None]] = None,
    ) -> bool:
        """Subscribe to notifications on *uuid*."""
        if not self.is_connected:
            await self._emit("error", "Not connected")
            return False
        if uuid in self._active_notifies:
            await self._emit("error", f"Already subscribed to {uuid}")
            return False

        async def _handler(_char: Any, raw: bytearray) -> None:
            data = bytes(raw)
            hex_str = data.hex()
            self._telemetry[uuid] = {"raw": list(data), "hex": hex_str}
            await self._emit(
                "notify",
                f"Notify {uuid}: {hex_str} ({len(data)} bytes)",
                {"uuid": uuid, "data": list(data), "hex": hex_str},
            )
            if user_callback is not None:
                user_callback(data)

        try:
            await self._client.start_notify(uuid, _handler)
            self._active_notifies.add(uuid)
            await self._emit("data", f"Subscribed to notifications on {uuid}")
            return True
        except BleakError as exc:
            await self._emit("error", f"Notify subscribe error on {uuid}: {exc}")
            return False
        except Exception as exc:
            await self._emit("error", f"Unexpected notify error on {uuid}: {exc}")
            return False

    async def stop_notify(self, uuid: str) -> bool:
        """Unsubscribe from notifications on *uuid*."""
        if not self.is_connected:
            return False
        try:
            await self._client.stop_notify(uuid)
            self._active_notifies.discard(uuid)
            await self._emit("data", f"Unsubscribed from {uuid}")
            return True
        except BleakError as exc:
            await self._emit("error", f"Stop notify error on {uuid}: {exc}")
            return False

    # ------------------------------------------------------------------
    # State management
    # ------------------------------------------------------------------

    async def _set_state(self, new_state: ConnectionState) -> None:
        old = self._state
        self._state = new_state
        if old != new_state:
            await self._emit(
                "state",
                f"State: {old.value} → {new_state.value}",
                {
                    "state": new_state.value,
                    "previous": old.value,
                    "is_connected": self.is_connected,
                },
            )

    # ------------------------------------------------------------------
    # Status snapshot (for REST API)
    # ------------------------------------------------------------------

    def get_status(self) -> dict:
        """Return a JSON-serialisable status snapshot."""
        connected_info = self._discovered.get(self._connected_address or "")
        return {
            "state": self._state.value,
            "is_connected": self.is_connected,
            "connected_device": {
                "address": self._connected_address,
                "name": connected_info.name if connected_info else self._connected_address,
                "rssi": connected_info.rssi if connected_info else None,
                "mtu": self._client.mtu_size if self._client else None,
            } if self._connected_address else None,
            "discovered_count": len(self._discovered),
            "discovered_devices": [d.to_dict() for d in self._discovered.values()],
            "services_count": len(self._services),
            "services": [s.to_dict() for s in self._services],
            "active_notifies": list(self._active_notifies),
            "telemetry": self._telemetry,
        }
