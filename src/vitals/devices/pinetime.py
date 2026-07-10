"""PineTime device plugin (InfiniTime firmware).

Unlike Bangle.js (which uses Espruino's REPL-over-NUS), InfiniTime
exposes standard Bluetooth SIG services and a handful of custom ones:

  - 0x180F  Battery Service       — battery percentage (uint8 0..100)
  - 0x180A  Device Information    — firmware revision, serial
  - 0x180D  Heart Rate Service    — current HR + measurement notifications
  - 0x1805  Current Time Service  — bidirectional clock sync
  - 0x1811  Alert Notification    — forwarded phone notifications
  - 00030000-78fc-48fe-8e23-433b3a1942d0
            InfiniTime motion     — step counter + accel notifications
  - 00050000-78fc-48fe-8e23-433b3a1942d0
            SimpleWeatherService  — current conditions + 5-day forecast

Time sync writes the standard CTS Current Time characteristic
(0x2A2B). Alarm push is *not* in InfiniTime's standard surface —
the watch's alarm app stores its config in flash that companion apps
can't touch over BLE without filesystem-over-BLE custom protocol —
so this plugin sets SUPPORTS_ALARM_PUSH=False and the application
surfaces a polite toast when the user tries.

Reference: https://github.com/InfiniTimeOrg/InfiniTime/tree/main/doc/ble
"""

from __future__ import annotations

import asyncio
import logging
import struct
import time
from datetime import datetime

from vitals.devices.base import ActivityReading, Device, register_device

log = logging.getLogger(__name__)

# InfiniTime releases ship a legacy-DFU zip per tag; see pinetime_dfu.
_INFINITIME_FW_URL = ("https://github.com/InfiniTimeOrg/InfiniTime/releases/"
                      "download/{version}/pinetime-mcuboot-app-dfu-"
                      "{version}.zip")
_INFINITIME_FW_DEFAULT_VERSION = "1.16.1"

# Bluetooth SIG canonical UUIDs we use directly.
BATTERY_SERVICE_UUID    = "0000180f-0000-1000-8000-00805f9b34fb"
BATTERY_LEVEL_CHAR_UUID = "00002a19-0000-1000-8000-00805f9b34fb"
CURRENT_TIME_CHAR_UUID  = "00002a2b-0000-1000-8000-00805f9b34fb"

# InfiniTime's custom motion service — unique enough to use as a
# discovery hint when a watch advertises with a non-standard name.
INFINITIME_MOTION_SERVICE   = "00030000-78fc-48fe-8e23-433b3a1942d0"
INFINITIME_STEP_COUNT_CHAR  = "00030001-78fc-48fe-8e23-433b3a1942d0"

# Alert Notification Service (0x1811) New Alert — how notifications
# reach the watch. InfiniTime reads byte 0 as the category, skips two
# bytes, and takes the rest as "title\\0body" (≤100 chars).
NEW_ALERT_CHAR_UUID = "00002a46-0000-1000-8000-00805f9b34fb"
# Gadgetbridge's CustomHuami category byte — its 3-byte header
# (category, count, custom icon) is exactly what InfiniTime skips.
_ALERT_HEADER = bytes([0xFA, 0x01, 0xFF])
_ALERT_MAX_CHARS = 100

# Music service: per-field write characteristics + an event char the
# watch notifies playback buttons on. Integers are big-endian seconds.
MUSIC_EVENT_CHAR_UUID    = "00000001-78fc-48fe-8e23-433b3a1942d0"
MUSIC_STATUS_CHAR_UUID   = "00000002-78fc-48fe-8e23-433b3a1942d0"
MUSIC_ARTIST_CHAR_UUID   = "00000003-78fc-48fe-8e23-433b3a1942d0"
MUSIC_TRACK_CHAR_UUID    = "00000004-78fc-48fe-8e23-433b3a1942d0"
MUSIC_ALBUM_CHAR_UUID    = "00000005-78fc-48fe-8e23-433b3a1942d0"
MUSIC_POSITION_CHAR_UUID = "00000006-78fc-48fe-8e23-433b3a1942d0"
MUSIC_LENGTH_CHAR_UUID   = "00000007-78fc-48fe-8e23-433b3a1942d0"
# Event byte → neutral command (MusicService.h).
_MUSIC_EVENTS = {0xE0: "refresh", 0x00: "play", 0x01: "pause",
                 0x03: "next", 0x04: "previous",
                 0x05: "volumeup", 0x06: "volumedown"}

# SimpleWeatherService — write-only, message type + version prefix.
WEATHER_DATA_CHAR_UUID = "00050001-78fc-48fe-8e23-433b3a1942d0"
_WEATHER_MAX_DAYS = 5
# Neutral condition kind -> InfiniTime icon enum.
_WEATHER_ICON = {
    "clear": 0,        # Sun
    "partly": 1,       # CloudsSun
    "cloudy": 2,       # Clouds
    "heavy_rain": 4,   # CloudShowerHeavy
    "drizzle": 5,      # CloudSunRain
    "rain": 5,
    "thunderstorm": 6,
    "snow": 7,
    "heavy_snow": 7,
    "sleet": 7,
    "fog": 8,          # Smog
    "unknown": 255,
}


def encode_alert(title: str, body: str) -> bytes:
    """One ANS New Alert payload: 3-byte header + "title\\0body"."""
    message = title
    if body:
        message += "\0" + body
    return _ALERT_HEADER + message[:_ALERT_MAX_CHARS].encode("utf-8")


def _centi(celsius: float | None) -> int:
    if not isinstance(celsius, (int, float)):
        return -32768
    return max(-32768, min(32767, round(celsius * 100)))


def encode_current_weather(forecast, tz_offset_s: int) -> bytes:
    """SimpleWeatherService CurrentWeather, version 0 (49 bytes) — the
    format every InfiniTime ≥1.14 accepts. Timestamps are local."""
    today = forecast.day(0)
    location = forecast.location_name.encode("utf-8")[:32]
    return (bytes([0x00, 0x00])
            + struct.pack("<Q", int(forecast.update_time_utc) + tz_offset_s)
            + struct.pack("<hhh",
                          _centi(forecast.temp_c),
                          _centi(today.low_c if today else None),
                          _centi(today.high_c if today else None))
            + location.ljust(32, b"\0")
            + bytes([_WEATHER_ICON.get(forecast.kind, 255)]))


def encode_forecast(forecast, tz_offset_s: int) -> bytes | None:
    """SimpleWeatherService Forecast (type 1, version 0): up to five
    days of min/max/icon. None when there are no day entries."""
    days = [d for d in forecast.days[:_WEATHER_MAX_DAYS] if d is not None]
    if not days:
        return None
    out = (bytes([0x01, 0x00])
           + struct.pack("<Q", int(forecast.update_time_utc) + tz_offset_s)
           + bytes([len(days)]))
    for day in days:
        out += struct.pack("<hhB", _centi(day.low_c), _centi(day.high_c),
                           _WEATHER_ICON.get(day.kind, 255))
    return out


@register_device
class PineTimeDevice(Device):
    id = "pinetime"
    display_name = "PineTime"
    description = "PINE64 PineTime running InfiniTime firmware"
    CATEGORY = "watch"
    ICON_NAME = "phone-symbolic"
    PAIRING_STEPS = [
        "Wake your PineTime and keep it near the phone.",
        "InfiniTime advertises automatically — no button press needed.",
    ]

    SUPPORTS_TIME_SYNC       = True
    SUPPORTS_ALARM_PUSH      = False
    SUPPORTS_ACTIVITY_READ   = True
    SUPPORTS_NOTIFICATIONS   = True
    SUPPORTS_WEATHER_PUSH    = True
    SUPPORTS_MUSIC_CONTROL   = True
    SUPPORTS_FIRMWARE_UPDATE = True

    # InfiniTime exposes legacy DFU from the running firmware, so an
    # update flashes over the normal connection — no bootloader mode.
    FIRMWARE_DEFAULT_VERSION = _INFINITIME_FW_DEFAULT_VERSION
    FIRMWARE_INTRO = (
        "Downloads an InfiniTime release and installs it over the "
        "watch's regular connection. Keep the watch nearby and "
        "charged; a failed transfer is safe — the watch keeps its "
        "current firmware.")
    FIRMWARE_SUCCESS_NOTE = (
        "To keep the new firmware, validate it on the watch: "
        "Settings → Firmware → Validate. Otherwise the watch reverts "
        "on its next restart.")

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
        self._music_events_on = False
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

    async def push_now_playing(self, track) -> None:
        """Write the music app's fields and subscribe (once per
        connection) to its event characteristic for watch buttons."""
        if self._client is None:
            raise RuntimeError("not connected")
        if not getattr(self, "_music_events_on", False):
            try:
                await self._client.start_notify(
                    MUSIC_EVENT_CHAR_UUID, self._on_music_event)
                self._music_events_on = True
            except Exception:
                log.debug("PineTime: music event subscribe failed",
                          exc_info=True)
        writes = (
            (MUSIC_ARTIST_CHAR_UUID, track.artist.encode("utf-8")),
            (MUSIC_TRACK_CHAR_UUID, track.track.encode("utf-8")),
            (MUSIC_ALBUM_CHAR_UUID, track.album.encode("utf-8")),
            (MUSIC_LENGTH_CHAR_UUID,
             struct.pack(">I", max(0, track.duration_s))),
            (MUSIC_POSITION_CHAR_UUID,
             struct.pack(">I", max(0, track.position_s))),
            (MUSIC_STATUS_CHAR_UUID, bytes([1 if track.playing else 0])),
        )
        for char, value in writes:
            await self._client.write_gatt_char(char, value, response=True)

    def _on_music_event(self, _char, data: bytearray) -> None:
        handler = getattr(self, "_music_handler", None)
        command = _MUSIC_EVENTS.get(data[0]) if data else None
        if handler is not None and command is not None:
            handler(command)

    async def push_notification(self, note) -> None:
        """Forward one banner via the Alert Notification Service."""
        if self._client is None:
            raise RuntimeError("not connected")
        title = (f"{note.app_name}: {note.title}" if note.app_name
                 else note.title)
        await self._client.write_gatt_char(
            NEW_ALERT_CHAR_UUID, encode_alert(title, note.body),
            response=True)

    async def push_weather(self, forecast) -> None:
        """Write current conditions + the 5-day forecast to InfiniTime's
        SimpleWeatherService (fw ≥ 1.14). Skipped for older firmware —
        the characteristic simply isn't there and the write raises."""
        if self._client is None:
            raise RuntimeError("not connected")
        offset = datetime.now().astimezone().utcoffset()
        tz_offset_s = int(offset.total_seconds()) if offset else 0
        await self._client.write_gatt_char(
            WEATHER_DATA_CHAR_UUID,
            encode_current_weather(forecast, tz_offset_s), response=True)
        days = encode_forecast(forecast, tz_offset_s)
        if days is not None:
            await self._client.write_gatt_char(
                WEATHER_DATA_CHAR_UUID, days, response=True)

    # ── Firmware update (Nordic legacy DFU) ────────────────────────

    async def fetch_default_firmware(
            self, version: str = _INFINITIME_FW_DEFAULT_VERSION) -> bytes:
        """Download an InfiniTime DFU `.zip` from the GitHub release
        for `version` (tags are plain, e.g. "1.16.1")."""
        url = _INFINITIME_FW_URL.format(version=version.lstrip("v"))
        return await asyncio.to_thread(_download_infinitime_firmware, url)

    async def flash_firmware(self, firmware: bytes, on_progress=None) -> None:
        """Flash an InfiniTime DFU `.zip` over the live connection.

        The watch reboots into the new image when the transfer
        finishes; the user must then validate it on the watch
        (Settings → Firmware) or MCUBoot rolls back on the next
        restart. See pinetime_dfu."""
        from vitals.devices.pinetime_dfu import parse_dfu_package, run_legacy_dfu
        if self._client is None:
            raise RuntimeError("not connected")
        init_packet, image = parse_dfu_package(firmware)
        await run_legacy_dfu(self._client, init_packet, image, on_progress)

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


def _download_infinitime_firmware(url: str, timeout: float = 120.0) -> bytes:
    """Download an InfiniTime firmware DFU `.zip`. Blocking — call via
    a worker thread."""
    import urllib.request
    log.info("PineTime: downloading firmware %s", url)
    req = urllib.request.Request(url, headers={"User-Agent": "vitals"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()
