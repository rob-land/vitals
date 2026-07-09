"""Pebble system Weather app records (pure encoders).

The FW4 Weather app reads ``WeatherDBEntry`` blobs from BlobDB
database 5 — but it only *displays* locations that are enumerated in a
``"weatherApp"`` entry in the app-settings database (9). tock (and the
first Vitals port) inserted weather records under its own derived
UUIDs and never wrote that settings entry, so the watch dutifully
stored forecasts it would never show — the reason weather "never
worked". The fix, per Gadgetbridge's known-good implementation: store
the record under the fixed primary-location UUID and enrol that UUID
in the ``weatherApp`` settings blob.

WeatherDBEntry v3 (little-endian, 20-byte fixed part):

  version:u8(=3), current_temp:s16, current_condition:u8,
  today_high:s16, today_low:s16, tomorrow_condition:u8,
  tomorrow_high:s16, tomorrow_low:s16, last_update:u32 (UTC time_t),
  is_auto_location:u8(=0), strings_length:u16, then two PascalString16s
  (no attribute ids, no null): location name, current-condition phrase.

Temperatures are integers in the user's display unit — the watch
renders a bare "%i°".
"""

from __future__ import annotations

import struct
import uuid

from vitals.devices.weather import Forecast, to_display

WEATHER_DB_VERSION = 3
# WEATHER_SERVICE_LOCATION_FORECAST_UNKNOWN_TEMP — INT16_MAX.
UNKNOWN_TEMP = 0x7FFF
# Buffer caps from weather_service.h (bytes incl. the null the watch adds).
_MAX_LOCATION = 63
_MAX_PHRASE = 31

# The Weather app's fixed primary-location key (Gadgetbridge's
# UUID_LOCATION) and its settings entry in the app-settings database.
UUID_PRIMARY_LOCATION = uuid.UUID("2c7e6a86-51e5-4ddd-b606-db43d1e4ad28")
WEATHER_APP_SETTINGS_KEY = b"weatherApp"

# Neutral condition kind -> Pebble condition byte (WeatherType).
_CONDITION = {
    "partly": 0,
    "cloudy": 1,
    "snow": 2,
    "drizzle": 3,
    "rain": 3,
    "heavy_rain": 4,
    "thunderstorm": 4,
    "heavy_snow": 5,
    "fog": 6,
    "unknown": 6,
    "clear": 7,
    "sleet": 8,
}


def condition_byte(kind: str) -> int:
    return _CONDITION.get(kind, 6)


def _temp(celsius: float | None, unit: str) -> int:
    value = to_display(celsius, unit)
    if value is None:
        return UNKNOWN_TEMP
    return max(-32768, min(32767, value))


def serialize_entry(forecast: Forecast) -> bytes:
    """One WeatherDBEntry v3 blob for the primary location."""
    unit = forecast.display_unit
    today = forecast.day(0)
    tomorrow = forecast.day(1)
    location = forecast.location_name.encode("utf-8")[:_MAX_LOCATION]
    phrase = forecast.phrase.encode("utf-8")[:_MAX_PHRASE]
    strings = (struct.pack("<H", len(location)) + location
               + struct.pack("<H", len(phrase)) + phrase)
    return struct.pack(
        "<BhBhhBhhIBH",
        WEATHER_DB_VERSION,
        _temp(forecast.temp_c, unit),
        condition_byte(forecast.kind),
        _temp(today.high_c if today else None, unit),
        _temp(today.low_c if today else None, unit),
        condition_byte(tomorrow.kind if tomorrow else "unknown"),
        _temp(tomorrow.high_c if tomorrow else None, unit),
        _temp(tomorrow.low_c if tomorrow else None, unit),
        int(forecast.update_time_utc) & 0xFFFFFFFF,
        0,  # manually-configured location (1 would mean auto/GPS)
        len(strings),
    ) + strings


def encode_app_settings(
        location_uuids=(UUID_PRIMARY_LOCATION,)) -> bytes:
    """The ``weatherApp`` settings blob: u8 count + 16-byte UUIDs, in
    display order. Without this enrolment the app shows no locations."""
    out = bytes([len(location_uuids)])
    for u in location_uuids:
        out += u.bytes
    return out
