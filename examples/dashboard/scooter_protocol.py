"""
Scooter / Ninebot BLE Protocol Definitions.

Provides:
- Known service and characteristic UUIDs for Ninebot/Segway e-scooters
- Standard BLE service/characteristic UUIDs (Battery, Device Information)
- Data-parsing helpers for standard BLE characteristics
- Ninebot command builders
- Human-readable UUID name lookup
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

# ---------------------------------------------------------------------------
# Ninebot / Segway UART service UUIDs
# ---------------------------------------------------------------------------

NINEBOT_SERVICE_UUID = "6e400001-b5a3-f393-e0a9-e50e24dcca9e"
NINEBOT_RX_UUID = "6e400002-b5a3-f393-e0a9-e50e24dcca9e"   # Write (commands)
NINEBOT_TX_UUID = "6e400003-b5a3-f393-e0a9-e50e24dcca9e"   # Notify (responses)

# ---------------------------------------------------------------------------
# Standard BLE service UUIDs (Bluetooth SIG assigned)
# ---------------------------------------------------------------------------

BATTERY_SERVICE_UUID = "0000180f-0000-1000-8000-00805f9b34fb"
BATTERY_LEVEL_UUID = "00002a19-0000-1000-8000-00805f9b34fb"

DEVICE_INFO_SERVICE_UUID = "0000180a-0000-1000-8000-00805f9b34fb"
FIRMWARE_REVISION_UUID = "00002a26-0000-1000-8000-00805f9b34fb"
HARDWARE_REVISION_UUID = "00002a27-0000-1000-8000-00805f9b34fb"
MANUFACTURER_NAME_UUID = "00002a29-0000-1000-8000-00805f9b34fb"
MODEL_NUMBER_UUID = "00002a24-0000-1000-8000-00805f9b34fb"
SERIAL_NUMBER_UUID = "00002a25-0000-1000-8000-00805f9b34fb"
SOFTWARE_REVISION_UUID = "00002a28-0000-1000-8000-00805f9b34fb"

GENERIC_ACCESS_SERVICE_UUID = "00001800-0000-1000-8000-00805f9b34fb"
DEVICE_NAME_UUID = "00002a00-0000-1000-8000-00805f9b34fb"
APPEARANCE_UUID = "00002a01-0000-1000-8000-00805f9b34fb"

GENERIC_ATTRIBUTE_SERVICE_UUID = "00001801-0000-1000-8000-00805f9b34fb"

# ---------------------------------------------------------------------------
# Ninebot protocol – pre-built command bytes
# ---------------------------------------------------------------------------

# Lock / unlock
CMD_LOCK = bytes([0x55, 0xAA, 0x06, 0x08, 0x12, 0x01, 0x01, 0x01, 0x00, 0x00])
CMD_UNLOCK = bytes([0x55, 0xAA, 0x06, 0x08, 0x12, 0x01, 0x01, 0x00, 0x00, 0x00])

# Info / telemetry requests
CMD_GET_SERIAL = bytes([0x55, 0xAA, 0x04, 0x20, 0x61, 0x01, 0x0E])
CMD_GET_FIRMWARE = bytes([0x55, 0xAA, 0x04, 0x20, 0x61, 0x01, 0x1A])
CMD_GET_BATTERY_INFO = bytes([0x55, 0xAA, 0x04, 0x20, 0x61, 0x01, 0x22])
CMD_GET_SPEED = bytes([0x55, 0xAA, 0x04, 0x20, 0x61, 0x01, 0x25])
CMD_GET_STATUS = bytes([0x55, 0xAA, 0x04, 0x20, 0x64, 0x01, 0x00])

# ---------------------------------------------------------------------------
# Telemetry dataclass
# ---------------------------------------------------------------------------

@dataclass
class ScooterTelemetry:
    """Decoded scooter telemetry values."""

    battery_percent: Optional[int] = None
    speed_kmh: Optional[float] = None
    is_locked: Optional[bool] = None
    temperature_celsius: Optional[float] = None
    total_mileage_km: Optional[float] = None
    remaining_range_km: Optional[float] = None
    firmware_version: Optional[str] = None
    hardware_version: Optional[str] = None
    serial_number: Optional[str] = None
    manufacturer: Optional[str] = None
    model: Optional[str] = None
    error_code: Optional[int] = None
    ride_mode: Optional[int] = None  # 0=eco, 1=drive, 2=sport

    def to_dict(self) -> dict:
        return {k: v for k, v in self.__dict__.items()}


# ---------------------------------------------------------------------------
# Parsers – standard BLE characteristics
# ---------------------------------------------------------------------------

def parse_battery_level(data: bytes) -> Optional[int]:
    """Parse BLE Battery Level characteristic (0x2A19).  Returns 0-100."""
    if len(data) >= 1:
        return min(100, max(0, int(data[0])))
    return None


def parse_string_characteristic(data: bytes) -> Optional[str]:
    """Decode a UTF-8 string characteristic (firmware rev, serial, etc.)."""
    try:
        return data.decode("utf-8").strip("\x00").strip()
    except UnicodeDecodeError:
        return data.hex()


# ---------------------------------------------------------------------------
# Parsers – Ninebot UART protocol
# ---------------------------------------------------------------------------

def parse_ninebot_response(data: bytes) -> Optional[dict]:
    """
    Parse a Ninebot BLE UART response packet.

    Packet format:
    [0x55, 0xAA, length, addr, mode, cmd, payload…, checksum_lo, checksum_hi]
    """
    if len(data) < 6:
        return None
    if data[0] != 0x55 or data[1] != 0xAA:
        return None

    pkt_len = data[2]
    addr = data[3]
    mode = data[4]
    cmd = data[5]
    payload_end = 6 + pkt_len - 2
    payload = data[6:payload_end] if len(data) >= payload_end else b""

    return {
        "length": pkt_len,
        "addr": addr,
        "mode": mode,
        "cmd": cmd,
        "payload": list(payload),
        "payload_hex": payload.hex(),
        "raw_hex": data.hex(),
    }


def parse_ninebot_speed(payload: bytes) -> Optional[float]:
    """Decode speed from Ninebot telemetry payload (little-endian int16, unit: 0.1 km/h)."""
    if len(payload) >= 2:
        raw = int.from_bytes(payload[:2], "little", signed=True)
        return round(raw * 0.1, 1)
    return None


def parse_ninebot_battery(payload: bytes) -> Optional[int]:
    """Decode battery percent from Ninebot telemetry payload."""
    if len(payload) >= 1:
        return min(100, max(0, int(payload[0])))
    return None


def parse_ninebot_temperature(payload: bytes) -> Optional[float]:
    """Decode temperature (°C) from Ninebot telemetry payload (int16 / 10)."""
    if len(payload) >= 2:
        raw = int.from_bytes(payload[:2], "little", signed=True)
        return round(raw / 10.0, 1)
    return None


# ---------------------------------------------------------------------------
# Command builder
# ---------------------------------------------------------------------------

def build_ninebot_command(cmd: int, addr: int = 0x20, payload: bytes = b"") -> bytes:
    """
    Build a Ninebot BLE UART command packet.

    Packet: [0x55, 0xAA, len, addr, 0x20, cmd, payload…, csum_lo, csum_hi]
    Checksum is computed over bytes from *length* onwards.
    """
    length = len(payload) + 2
    header = bytes([0x55, 0xAA, length, addr, 0x20, cmd])
    packet = header + payload
    # Checksum: the sum of all bytes from the length field onwards, then
    # take (256 - sum % 256) % 256 so the total including the checksum byte
    # is divisible by 256 (i.e. the lower byte of the sum is 0x00).
    checksum = (256 - sum(packet[2:]) % 256) % 256
    return packet + bytes([checksum, 0x00])


# ---------------------------------------------------------------------------
# UUID name lookup tables
# ---------------------------------------------------------------------------

SERVICE_NAMES: dict[str, str] = {
    NINEBOT_SERVICE_UUID: "Ninebot UART Service",
    BATTERY_SERVICE_UUID: "Battery Service",
    DEVICE_INFO_SERVICE_UUID: "Device Information",
    GENERIC_ACCESS_SERVICE_UUID: "Generic Access",
    GENERIC_ATTRIBUTE_SERVICE_UUID: "Generic Attribute",
    "00001802-0000-1000-8000-00805f9b34fb": "Immediate Alert",
    "00001803-0000-1000-8000-00805f9b34fb": "Link Loss",
    "00001804-0000-1000-8000-00805f9b34fb": "Tx Power",
    "00001805-0000-1000-8000-00805f9b34fb": "Current Time",
    "00001811-0000-1000-8000-00805f9b34fb": "Alert Notification",
    "0000fe59-0000-1000-8000-00805f9b34fb": "Nordic DFU",
    "00001530-1212-efde-1523-785feabcd123": "Nordic Legacy DFU",
}

CHARACTERISTIC_NAMES: dict[str, str] = {
    NINEBOT_RX_UUID: "Ninebot RX – Write",
    NINEBOT_TX_UUID: "Ninebot TX – Notify",
    BATTERY_LEVEL_UUID: "Battery Level",
    FIRMWARE_REVISION_UUID: "Firmware Revision",
    HARDWARE_REVISION_UUID: "Hardware Revision",
    MANUFACTURER_NAME_UUID: "Manufacturer Name",
    MODEL_NUMBER_UUID: "Model Number",
    SERIAL_NUMBER_UUID: "Serial Number",
    SOFTWARE_REVISION_UUID: "Software Revision",
    DEVICE_NAME_UUID: "Device Name",
    APPEARANCE_UUID: "Appearance",
    "00002a04-0000-1000-8000-00805f9b34fb": "Preferred Connection Params",
    "00002a05-0000-1000-8000-00805f9b34fb": "Service Changed",
    "00002a2a-0000-1000-8000-00805f9b34fb": "IEEE 11073-20601 Regulatory",
    "00002a50-0000-1000-8000-00805f9b34fb": "PnP ID",
}


def identify_service(uuid: str) -> str:
    """Return a human-readable name for a service UUID, or the UUID itself."""
    return SERVICE_NAMES.get(uuid.lower(), uuid)


def identify_characteristic(uuid: str) -> str:
    """Return a human-readable name for a characteristic UUID."""
    return CHARACTERISTIC_NAMES.get(uuid.lower(), uuid)


def is_ninebot_device(service_uuids: list[str]) -> bool:
    """Return True if the advertised service UUIDs indicate a Ninebot device."""
    return any(u.lower() == NINEBOT_SERVICE_UUID for u in service_uuids)
