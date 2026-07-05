"""Pebble weather — fetch a forecast and serialize it for the watch.

The Pebble's built-in Weather app reads from a watch-side BlobDB (database
id 0x05, ``BlobDBIdWeather``) that the phone fills in. We fetch a forecast
from **Open-Meteo** (free, no API key), map it onto the firmware's
``WeatherDBEntry`` (version 3) and hand the bytes to the Pebble plugin,
which inserts them over the existing BlobDB transport.

The watch renders the temperature as a bare ``"%i°"`` — no unit
conversion — so we request the forecast from Open-Meteo already in the
user's chosen unit and send the integer as-is.

Wire format (``include/pbl/services/blob_db/weather_db.h``, packed,
little-endian):

  key   = 16-byte Uuid (one per location)
  value = version:u8(=3), current_temp:i16, current_type:u8,
          today_high:i16, today_low:i16, tomorrow_type:u8,
          tomorrow_high:i16, tomorrow_low:i16, last_update_utc:i32 (time_t),
          is_current_location:u8, then a SerializedArray of two
          PascalString16s — location name and a short phrase:
              data_size:u16, then {str_len:u16 + bytes} × 2

The parsing/serialization here is pure and unit-tested; the HTTP calls run
off the main thread (the caller handles that). The time field is a 4-byte
``time_t`` — the firmware static-asserts ``sizeof(time_t) == 4`` (and
builds with ``-D_USE_LONG_TIME_T``), so this is exact, not assumed.
"""

from __future__ import annotations

import json
import logging
import struct
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass

log = logging.getLogger(__name__)

WEATHER_DB_VERSION = 3
# WEATHER_SERVICE_LOCATION_FORECAST_UNKNOWN_TEMP — INT16_MAX.
UNKNOWN_TEMP = 0x7FFF
# Buffer caps from weather_service.h (bytes incl. the null the watch adds).
_MAX_LOCATION = 63
_MAX_PHRASE = 31

# WeatherType enum (weather_type_tuples.def).
WT_PARTLY_CLOUDY = 0
WT_CLOUDY_DAY    = 1
WT_LIGHT_SNOW    = 2
WT_LIGHT_RAIN    = 3
WT_HEAVY_RAIN    = 4
WT_HEAVY_SNOW    = 5
WT_GENERIC       = 6
WT_SUN           = 7
WT_RAIN_AND_SNOW = 8
WT_UNKNOWN       = 255

# WMO weather-interpretation code (Open-Meteo) -> (WeatherType, short phrase).
_WMO: dict[int, tuple[int, str]] = {
    0:  (WT_SUN, "Clear"),
    1:  (WT_SUN, "Mainly Clear"),
    2:  (WT_PARTLY_CLOUDY, "Partly Cloudy"),
    3:  (WT_CLOUDY_DAY, "Cloudy"),
    45: (WT_GENERIC, "Fog"),
    48: (WT_GENERIC, "Rime Fog"),
    51: (WT_LIGHT_RAIN, "Light Drizzle"),
    53: (WT_LIGHT_RAIN, "Drizzle"),
    55: (WT_LIGHT_RAIN, "Heavy Drizzle"),
    56: (WT_RAIN_AND_SNOW, "Freezing Drizzle"),
    57: (WT_RAIN_AND_SNOW, "Freezing Drizzle"),
    61: (WT_LIGHT_RAIN, "Light Rain"),
    63: (WT_LIGHT_RAIN, "Rain"),
    65: (WT_HEAVY_RAIN, "Heavy Rain"),
    66: (WT_RAIN_AND_SNOW, "Freezing Rain"),
    67: (WT_RAIN_AND_SNOW, "Freezing Rain"),
    71: (WT_LIGHT_SNOW, "Light Snow"),
    73: (WT_LIGHT_SNOW, "Snow"),
    75: (WT_HEAVY_SNOW, "Heavy Snow"),
    77: (WT_LIGHT_SNOW, "Snow Grains"),
    80: (WT_LIGHT_RAIN, "Light Showers"),
    81: (WT_LIGHT_RAIN, "Showers"),
    82: (WT_HEAVY_RAIN, "Heavy Showers"),
    85: (WT_LIGHT_SNOW, "Snow Showers"),
    86: (WT_HEAVY_SNOW, "Snow Showers"),
    95: (WT_HEAVY_RAIN, "Thunderstorm"),
    96: (WT_HEAVY_RAIN, "Thunderstorm"),
    99: (WT_HEAVY_RAIN, "Thunderstorm"),
}

# Stable namespace so one configured location maps to one BlobDB key (a new
# city overwrites the same watch entry rather than piling up).
# Kept verbatim from tock: these seed the deterministic BlobDB keys on
# the watch, and changing the namespace would orphan every weather entry
# a tock-era sync already stored there.
_KEY_NAMESPACE = uuid.uuid5(uuid.NAMESPACE_DNS, "weather.tock.rob.land")

_GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"
_FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
_TIMEOUT = 15.0


@dataclass(frozen=True)
class GeoResult:
    name: str          # display label, e.g. "Paris, Île-de-France, France"
    latitude: float
    longitude: float


@dataclass(frozen=True)
class Forecast:
    location_name: str
    short_phrase: str
    current_type: int
    current_temp: int
    today_high: int
    today_low: int
    tomorrow_type: int
    tomorrow_high: int
    tomorrow_low: int
    update_time_utc: int


# ── pure mapping / serialization ───────────────────────────────────

def wmo_to_type_and_phrase(code: int) -> tuple[int, str]:
    """Map an Open-Meteo WMO code to a Pebble WeatherType + short phrase."""
    return _WMO.get(code, (WT_GENERIC, "—"))


def _temp(value) -> int:
    """Round a temperature to the watch's int16, or the unknown sentinel."""
    if not isinstance(value, (int, float)):
        return UNKNOWN_TEMP
    return max(-32768, min(32767, round(value)))


def location_key(latitude: float, longitude: float) -> bytes:
    """A stable 16-byte BlobDB key for a location."""
    name = f"{latitude:.4f},{longitude:.4f}"
    return uuid.uuid5(_KEY_NAMESPACE, name).bytes


def serialize_entry(forecast: Forecast, is_current_location: bool = True) -> bytes:
    """Serialize a forecast into a WeatherDBEntry blob (see module docstring)."""
    location = forecast.location_name.encode("utf-8")[:_MAX_LOCATION]
    phrase = forecast.short_phrase.encode("utf-8")[:_MAX_PHRASE]
    strings = (struct.pack("<H", len(location)) + location
               + struct.pack("<H", len(phrase)) + phrase)
    return struct.pack(
        "<BhBhhBhhiBH",
        WEATHER_DB_VERSION,
        _temp(forecast.current_temp),
        forecast.current_type & 0xFF,
        _temp(forecast.today_high),
        _temp(forecast.today_low),
        forecast.tomorrow_type & 0xFF,
        _temp(forecast.tomorrow_high),
        _temp(forecast.tomorrow_low),
        int(forecast.update_time_utc) & 0xFFFFFFFF,
        1 if is_current_location else 0,
        len(strings),
    ) + strings


# ── response parsing (pure) ────────────────────────────────────────

def parse_geocode(data: dict) -> list[GeoResult]:
    out: list[GeoResult] = []
    for r in data.get("results") or []:
        lat, lon = r.get("latitude"), r.get("longitude")
        if not isinstance(lat, (int, float)) or not isinstance(lon, (int, float)):
            continue
        parts = [r.get("name"), r.get("admin1"), r.get("country")]
        label = ", ".join(p for p in parts if p)
        out.append(GeoResult(label or str(r.get("name", "")),
                             float(lat), float(lon)))
    return out


def parse_forecast(data: dict, location_name: str, now_utc: int) -> Forecast:
    """Build a Forecast from an Open-Meteo forecast response."""
    current = data.get("current") or {}
    daily = data.get("daily") or {}
    codes = daily.get("weather_code") or []
    highs = daily.get("temperature_2m_max") or []
    lows = daily.get("temperature_2m_min") or []

    def day(idx: int):
        code = codes[idx] if idx < len(codes) else None
        wtype = wmo_to_type_and_phrase(code)[0] if code is not None else WT_UNKNOWN
        high = highs[idx] if idx < len(highs) else None
        low = lows[idx] if idx < len(lows) else None
        return wtype, high, low

    cur_code = current.get("weather_code")
    cur_type, phrase = (wmo_to_type_and_phrase(cur_code)
                        if cur_code is not None else (WT_UNKNOWN, "—"))
    today_type, today_high, today_low = day(0)
    tomorrow_type, tomorrow_high, tomorrow_low = day(1)
    # The current condition drives the headline icon/phrase; fall back to
    # today's daily code if there's no current code.
    if cur_code is None:
        cur_type, phrase = today_type, wmo_to_type_and_phrase(
            codes[0])[1] if codes else "—"

    return Forecast(
        location_name=location_name,
        short_phrase=phrase,
        current_type=cur_type,
        current_temp=_temp(current.get("temperature_2m")),
        today_high=_temp(today_high),
        today_low=_temp(today_low),
        tomorrow_type=tomorrow_type,
        tomorrow_high=_temp(tomorrow_high),
        tomorrow_low=_temp(tomorrow_low),
        update_time_utc=now_utc,
    )


# ── network (run off the main thread) ──────────────────────────────

def geocode(query: str, limit: int = 8) -> list[GeoResult]:
    params = urllib.parse.urlencode(
        {"name": query, "count": limit, "format": "json"})
    return parse_geocode(_get_json(f"{_GEOCODE_URL}?{params}"))


def fetch_forecast(latitude: float, longitude: float, location_name: str,
                   unit: str, now_utc: int) -> Forecast:
    """Fetch a 2-day forecast. `unit` is "fahrenheit" or "celsius"."""
    params = urllib.parse.urlencode({
        "latitude": latitude,
        "longitude": longitude,
        "current": "temperature_2m,weather_code",
        "daily": "weather_code,temperature_2m_max,temperature_2m_min",
        "temperature_unit": "fahrenheit" if unit == "fahrenheit" else "celsius",
        "forecast_days": 2,
        "timezone": "auto",
    })
    data = _get_json(f"{_FORECAST_URL}?{params}")
    return parse_forecast(data, location_name, now_utc)


def _get_json(url: str) -> dict:
    log.info("Open-Meteo: GET %s", url.split("?")[0])
    with urllib.request.urlopen(url, timeout=_TIMEOUT) as resp:
        return json.load(resp)
