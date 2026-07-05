"""PineTime device plugin (InfiniTime firmware).

Unlike Bangle.js (which uses Espruino's REPL-over-NUS), InfiniTime
exposes standard Bluetooth SIG services and a handful of custom ones:

  - 0x180F  Battery Service       — battery percentage (uint8 0..100)
  - 0x180A  Device Information    — firmware revision, serial
  - 0x180D  Heart Rate Service    — current HR + measurement notifications
  - 0x1805  Current Time Service  — bidirectional clock sync
  - 00030000-78fc-48fe-8e23-433b3a1942d0
            InfiniTime motion     — step counter + accel notifications

Time sync writes the standard CTS Current Time characteristic
(0x2A2B). Alarm push is *not* in InfiniTime's standard surface —
the watch's alarm app stores its config in flash that companion apps
can't touch over BLE without filesystem-over-BLE custom protocol —
so this plugin sets SUPPORTS_ALARM_PUSH=False and the application
surfaces a polite toast when the user tries.

Reference: https://github.com/InfiniTimeOrg/InfiniTime/tree/main/doc/ble
"""

from __future__ import annotations

import logging
import struct
import time
from datetime import datetime

from vitals.devices.base import ActivityReading, Device, register_device

log = logging.getLogger(__name__)

# Bluetooth SIG canonical UUIDs we use directly.
BATTERY_SERVICE_UUID    = "0000180f-0000-1000-8000-00805f9b34fb"
BATTERY_LEVEL_CHAR_UUID = "00002a19-0000-1000-8000-00805f9b34fb"
CURRENT_TIME_CHAR_UUID  = "00002a2b-0000-1000-8000-00805f9b34fb"

# InfiniTime's custom motion service — unique enough to use as a
# discovery hint when a watch advertises with a non-standard name.
INFINITIME_MOTION_SERVICE   = "00030000-78fc-48fe-8e23-433b3a1942d0"
INFINITIME_STEP_COUNT_CHAR  = "00030001-78fc-48fe-8e23-433b3a1942d0"


@register_device
class PineTimeDevice(Device):
    id = "pinetime"
    display_name = "PineTime"
    description = "PINE64 PineTime running InfiniTime firmware"

    SUPPORTS_TIME_SYNC     = True
    SUPPORTS_ALARM_PUSH    = False
    SUPPORTS_ACTIVITY_READ = True

    @classmethod
    def matches(cls, advertised_name: str | None,
                service_uuids: list[str]) -> bool:
        if advertised_name:
            n = advertised_name.lower()
            if n.startswith("infinitime") or n.startswith("pinetime"):
                return True
        return INFINITIME_MOTION_SERVICE in [u.lower() for u in service_uuids]

    def __init__(self, address: str, name: str = ""):
        super().__init__(address, name)
        self._client = None

    # ── Lifecycle ──────────────────────────────────────────────────

    async def connect(self) -> None:
        from bleak import BleakClient
        self._client = BleakClient(self.address)
        await self._client.connect()

    async def disconnect(self) -> None:
        if self._client is None:
            return
        try:
            await self._client.disconnect()
        finally:
            self._client = None

    @property
    def is_connected(self) -> bool:
        return self._client is not None and self._client.is_connected

    # ── Feature methods ────────────────────────────────────────────

    async def get_battery(self) -> int | None:
        if self._client is None:
            return None
        try:
            data = await self._client.read_gatt_char(BATTERY_LEVEL_CHAR_UUID)
        except Exception:
            return None
        return self._parse_battery_level(data)

    async def get_activity(self) -> ActivityReading | None:
        """Read the InfiniTime step counter.

        InfiniTime publishes the day's step count as a uint32 LE at
        the Motion service's step-count characteristic. Heart rate is
        notification-only (the watch only pushes a value when the
        HR sensor is actively measuring) so we don't try to read it
        on a one-shot sync — steps are what users actually want
        accumulated in the dashboard."""
        if self._client is None:
            return None
        try:
            data = await self._client.read_gatt_char(INFINITIME_STEP_COUNT_CHAR)
        except Exception:
            log.exception("PineTime: get_activity step-count read failed")
            return None
        steps = self._parse_step_count(data)
        log.info("PineTime: steps = %s", steps)
        if steps is None:
            return None
        return ActivityReading(steps=steps, timestamp=time.time())

    async def sync_time(self, unix_timestamp: float) -> None:
        """Write the Bluetooth SIG Current Time characteristic.

        Wire format (Bluetooth SIG XML 0x2A2B, 10 bytes total):
            uint16  year       (LE)
            uint8   month      (1..12)
            uint8   day        (1..31)
            uint8   hours      (0..23)
            uint8   minutes    (0..59)
            uint8   seconds    (0..59)
            uint8   day-of-week (1=Mon..7=Sun, per ISO-8601)
            uint8   fractions256 (256ths of a second; 0 is fine)
            uint8   adjust-reason flags (0 = manual)
        """
        if self._client is None:
            raise RuntimeError("not connected")
        payload = self._encode_current_time(unix_timestamp)
        await self._client.write_gatt_char(
            CURRENT_TIME_CHAR_UUID, payload, response=True)

    @staticmethod
    def _encode_current_time(unix_timestamp: float) -> bytes:
        """Pack `unix_timestamp` into the 10-byte CTS Current Time
        characteristic value. Uses local-time fields per the spec."""
        dt = datetime.fromtimestamp(unix_timestamp)
        # ISO weekday: Monday=1 ... Sunday=7
        return struct.pack(
            "<HBBBBBBBB",
            dt.year, dt.month, dt.day,
            dt.hour, dt.minute, dt.second,
            dt.isoweekday(),
            0,  # fractions256
            0,  # adjust-reason: manual update
        )

    @staticmethod
    def _parse_battery_level(data: bytes | bytearray | None) -> int | None:
        """The Battery Level characteristic is a single uint8 in the
        range 0..100. Anything else means we read garbage — return None.
        """
        if not data:
            return None
        level = int(data[0])
        if 0 <= level <= 100:
            return level
        return None

    @staticmethod
    def _parse_step_count(data: bytes | bytearray | None) -> int | None:
        """InfiniTime's step-count characteristic is a uint32 LE."""
        if not data or len(data) < 4:
            return None
        return struct.unpack("<I", bytes(data[:4]))[0]
