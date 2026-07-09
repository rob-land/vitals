"""Watch-agnostic weather — fetch a forecast and map conditions.

Fetches a 5-day forecast from **Open-Meteo** (free, no API key),
always in °C, and normalises the WMO condition code to a neutral
``kind`` string. Each watch plugin serializes the resulting
``Forecast`` into its own wire format — Pebble WeatherDB entries
(``devices/pebble/pebble_weather.py``), InfiniTime's
SimpleWeatherService structs, Bangle.js GB JSON — converting units as
it goes; ``display_unit`` records the user's preference for watches
that render the number as-is.

Parsing and mapping are pure and unit-tested; the HTTP calls run off
the main thread (the caller handles that).
"""

from __future__ import annotations

import json
import logging
import urllib.parse
import urllib.request
from dataclasses import dataclass

log = logging.getLogger(__name__)

# Neutral condition kinds every watch backend maps from:
#   clear, partly, cloudy, fog, drizzle, rain, heavy_rain, sleet,
#   snow, heavy_snow, thunderstorm, unknown
# WMO weather-interpretation code (Open-Meteo) -> (kind, short phrase).
_WMO: dict[int, tuple[str, str]] = {
    0:  ("clear", "Clear"),
    1:  ("clear", "Mainly Clear"),
    2:  ("partly", "Partly Cloudy"),
    3:  ("cloudy", "Cloudy"),
    45: ("fog", "Fog"),
    48: ("fog", "Rime Fog"),
    51: ("drizzle", "Light Drizzle"),
    53: ("drizzle", "Drizzle"),
    55: ("drizzle", "Heavy Drizzle"),
    56: ("sleet", "Freezing Drizzle"),
    57: ("sleet", "Freezing Drizzle"),
    61: ("rain", "Light Rain"),
    63: ("rain", "Rain"),
    65: ("heavy_rain", "Heavy Rain"),
    66: ("sleet", "Freezing Rain"),
    67: ("sleet", "Freezing Rain"),
    71: ("snow", "Light Snow"),
    73: ("snow", "Snow"),
    75: ("heavy_snow", "Heavy Snow"),
    77: ("snow", "Snow Grains"),
    80: ("rain", "Light Showers"),
    81: ("rain", "Showers"),
    82: ("heavy_rain", "Heavy Showers"),
    85: ("snow", "Snow Showers"),
    86: ("heavy_snow", "Snow Showers"),
    95: ("thunderstorm", "Thunderstorm"),
    96: ("thunderstorm", "Thunderstorm"),
    99: ("thunderstorm", "Thunderstorm"),
}

_GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"
_FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
_TIMEOUT = 15.0
FORECAST_DAYS = 5


@dataclass(frozen=True)
class GeoResult:
    name: str          # display label, e.g. "Paris, Île-de-France, France"
    latitude: float
    longitude: float


@dataclass(frozen=True)
class DayForecast:
    kind: str
    high_c: float | None
    low_c: float | None


@dataclass(frozen=True)
class Forecast:
    """A normalised forecast, temperatures in °C. ``days[0]`` is today."""
    location_name: str
    kind: str
    phrase: str
    temp_c: float | None
    humidity: int | None
    wind_kmh: float | None
    wind_dir_deg: int | None
    days: tuple[DayForecast, ...]
    update_time_utc: int
    display_unit: str = "celsius"   # "celsius" | "fahrenheit"

    def day(self, index: int) -> DayForecast | None:
        return self.days[index] if index < len(self.days) else None


# ── pure mapping helpers ───────────────────────────────────────────

def wmo_to_kind(code) -> tuple[str, str]:
    """Map an Open-Meteo WMO code to (neutral kind, short phrase)."""
    if code is None:
        return "unknown", "—"
    return _WMO.get(int(code), ("unknown", "—"))


def to_display(celsius: float | None, unit: str) -> int | None:
    """°C → a rounded integer in the display unit, None passing through."""
    if not isinstance(celsius, (int, float)):
        return None
    value = celsius * 9 / 5 + 32 if unit == "fahrenheit" else celsius
    return round(value)


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


def _number(value) -> float | None:
    return float(value) if isinstance(value, (int, float)) else None


def parse_forecast(data: dict, location_name: str, now_utc: int,
                   display_unit: str = "celsius") -> Forecast:
    """Build a Forecast from an Open-Meteo forecast response (°C)."""
    current = data.get("current") or {}
    daily = data.get("daily") or {}
    codes = daily.get("weather_code") or []
    highs = daily.get("temperature_2m_max") or []
    lows = daily.get("temperature_2m_min") or []

    days = []
    for i in range(min(FORECAST_DAYS, len(codes))):
        kind, _ = wmo_to_kind(codes[i])
        days.append(DayForecast(
            kind=kind,
            high_c=_number(highs[i]) if i < len(highs) else None,
            low_c=_number(lows[i]) if i < len(lows) else None))

    cur_code = current.get("weather_code")
    if cur_code is not None:
        kind, phrase = wmo_to_kind(cur_code)
    elif codes:
        # No current condition — fall back to today's daily code.
        kind, phrase = wmo_to_kind(codes[0])
    else:
        kind, phrase = "unknown", "—"

    humidity = _number(current.get("relative_humidity_2m"))
    wind_dir = _number(current.get("wind_direction_10m"))
    return Forecast(
        location_name=location_name,
        kind=kind,
        phrase=phrase,
        temp_c=_number(current.get("temperature_2m")),
        humidity=round(humidity) if humidity is not None else None,
        wind_kmh=_number(current.get("wind_speed_10m")),
        wind_dir_deg=round(wind_dir) if wind_dir is not None else None,
        days=tuple(days),
        update_time_utc=now_utc,
        display_unit=display_unit,
    )


# ── network (run off the main thread) ──────────────────────────────

def geocode(query: str, limit: int = 8) -> list[GeoResult]:
    params = urllib.parse.urlencode(
        {"name": query, "count": limit, "format": "json"})
    return parse_geocode(_get_json(f"{_GEOCODE_URL}?{params}"))


def fetch_forecast(latitude: float, longitude: float, location_name: str,
                   unit: str, now_utc: int) -> Forecast:
    """Fetch a 5-day forecast (°C). `unit` only tags the user's display
    preference — see ``Forecast.display_unit``."""
    params = urllib.parse.urlencode({
        "latitude": latitude,
        "longitude": longitude,
        "current": "temperature_2m,relative_humidity_2m,weather_code,"
                   "wind_speed_10m,wind_direction_10m",
        "daily": "weather_code,temperature_2m_max,temperature_2m_min",
        "forecast_days": FORECAST_DAYS,
        "timezone": "auto",
    })
    data = _get_json(f"{_FORECAST_URL}?{params}")
    return parse_forecast(data, location_name, now_utc,
                          display_unit=("fahrenheit" if unit == "fahrenheit"
                                        else "celsius"))


def _get_json(url: str) -> dict:
    log.info("Open-Meteo: GET %s", url.split("?")[0])
    with urllib.request.urlopen(url, timeout=_TIMEOUT) as resp:
        return json.load(resp)
