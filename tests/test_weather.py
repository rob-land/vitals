"""Tests for the watch-agnostic weather core and every per-watch
serialization: Pebble WeatherDBEntry + weatherApp enrolment (layout
pinned against Gadgetbridge's known-good encoder), InfiniTime
SimpleWeatherService structs, and Bangle GB JSON."""

import struct

from vitals.devices import weather as w
from vitals.devices.pebble import pebble_weather as pw


def _forecast(**kw):
    base = dict(
        location_name="London", kind="rain", phrase="Light Rain",
        temp_c=11.6, humidity=71, wind_kmh=14.5, wind_dir_deg=230,
        days=(w.DayForecast("rain", 14.2, 7.1),
              w.DayForecast("cloudy", 12.0, 5.2),
              w.DayForecast("clear", 15.0, 6.0)),
        update_time_utc=1_700_000_000, display_unit="celsius")
    base.update(kw)
    return w.Forecast(**base)


# ── neutral mapping / units ────────────────────────────────────────

def test_wmo_mapping_to_neutral_kinds():
    assert w.wmo_to_kind(0) == ("clear", "Clear")
    assert w.wmo_to_kind(2) == ("partly", "Partly Cloudy")
    assert w.wmo_to_kind(65) == ("heavy_rain", "Heavy Rain")
    assert w.wmo_to_kind(71) == ("snow", "Light Snow")
    assert w.wmo_to_kind(95)[0] == "thunderstorm"
    assert w.wmo_to_kind(123) == ("unknown", "—")   # never crashes
    assert w.wmo_to_kind(None) == ("unknown", "—")


def test_to_display_rounds_and_converts():
    assert w.to_display(11.4, "celsius") == 11
    assert w.to_display(12.6, "celsius") == 13
    assert w.to_display(0.0, "fahrenheit") == 32
    assert w.to_display(20.0, "fahrenheit") == 68
    assert w.to_display(None, "celsius") is None
    assert w.to_display("x", "celsius") is None


# ── response parsing ───────────────────────────────────────────────

def test_parse_geocode_builds_label():
    results = w.parse_geocode({"results": [
        {"name": "Paris", "latitude": 48.85, "longitude": 2.35,
         "admin1": "Île-de-France", "country": "France"},
        {"name": "NoCoords"},  # dropped — missing lat/lon
    ]})
    assert len(results) == 1
    assert results[0].name == "Paris, Île-de-France, France"
    assert results[0].latitude == 48.85 and results[0].longitude == 2.35


def test_parse_forecast_maps_current_and_days():
    data = {
        "current": {"temperature_2m": 11.4, "weather_code": 61,
                    "relative_humidity_2m": 71.2, "wind_speed_10m": 14.5,
                    "wind_direction_10m": 230.4},
        "daily": {
            "weather_code": [61, 3, 0],
            "temperature_2m_max": [14.2, 12.0, 15.0],
            "temperature_2m_min": [7.1, 5.2, 6.0],
        },
    }
    f = w.parse_forecast(data, "London", now_utc=1_700_000_000,
                         display_unit="fahrenheit")
    assert f.kind == "rain" and f.phrase == "Light Rain"
    assert f.temp_c == 11.4 and f.humidity == 71
    assert f.wind_kmh == 14.5 and f.wind_dir_deg == 230
    assert len(f.days) == 3
    assert f.day(0).high_c == 14.2 and f.day(1).kind == "cloudy"
    assert f.day(9) is None
    assert f.display_unit == "fahrenheit"


def test_parse_forecast_missing_data_stays_none():
    f = w.parse_forecast({}, "Nowhere", now_utc=42)
    assert f.kind == "unknown" and f.temp_c is None
    assert f.days == () and f.day(0) is None


# ── Pebble WeatherDBEntry + enrolment ──────────────────────────────

def test_pebble_entry_layout():
    blob = pw.serialize_entry(_forecast())
    (version, cur_temp, cur_cond, th, tl, tc, toh, tol, upd, auto,
     dsize) = struct.unpack_from("<BhBhhBhhIBH", blob, 0)
    assert version == 3
    assert cur_temp == 12 and cur_cond == 3          # rain
    assert th == 14 and tl == 7
    assert tc == 1 and toh == 12 and tol == 5        # cloudy tomorrow
    assert upd == 1_700_000_000
    assert auto == 0                                  # manual location
    strings = blob[20:]
    assert dsize == len(strings)
    loc_len = struct.unpack_from("<H", strings, 0)[0]
    assert strings[2:2 + loc_len] == b"London"
    poff = 2 + loc_len
    phr_len = struct.unpack_from("<H", strings, poff)[0]
    assert strings[poff + 2:poff + 2 + phr_len] == b"Light Rain"


def test_pebble_entry_respects_display_unit():
    blob = pw.serialize_entry(_forecast(display_unit="fahrenheit"))
    cur_temp = struct.unpack_from("<h", blob, 1)[0]
    assert cur_temp == 53                             # 11.6 °C → 53 °F


def test_pebble_entry_unknowns_and_truncation():
    blob = pw.serialize_entry(_forecast(
        temp_c=None, days=(), location_name="X" * 200, phrase="Y" * 200))
    cur_temp, = struct.unpack_from("<h", blob, 1)
    today_high, = struct.unpack_from("<h", blob, 4)
    assert cur_temp == pw.UNKNOWN_TEMP == today_high
    strings = blob[20:]
    loc_len = struct.unpack_from("<H", strings, 0)[0]
    assert loc_len <= 63
    phr_len = struct.unpack_from("<H", strings, 2 + loc_len)[0]
    assert phr_len <= 31


def test_pebble_app_settings_enrols_primary_location():
    blob = pw.encode_app_settings()
    assert blob[0] == 1                               # one location
    assert blob[1:17] == pw.UUID_PRIMARY_LOCATION.bytes
    assert len(blob) == 17


# ── InfiniTime SimpleWeatherService ────────────────────────────────

def test_infinitime_current_weather_v0_layout():
    from vitals.devices import pinetime as pt
    blob = pt.encode_current_weather(_forecast(), tz_offset_s=3600)
    assert len(blob) == 49
    assert blob[0] == 0 and blob[1] == 0              # CurrentWeather v0
    ts, = struct.unpack_from("<Q", blob, 2)
    assert ts == 1_700_000_000 + 3600                 # local time
    cur, lo, hi = struct.unpack_from("<hhh", blob, 10)
    assert cur == 1160 and lo == 710 and hi == 1420   # centi-°C
    assert blob[16:22] == b"London"
    assert blob[48] == 5                              # rain → CloudSunRain


def test_infinitime_forecast_layout():
    from vitals.devices import pinetime as pt
    blob = pt.encode_forecast(_forecast(), tz_offset_s=0)
    assert blob[0] == 1 and blob[1] == 0              # Forecast v0
    assert blob[10] == 3                              # three days
    lo, hi = struct.unpack_from("<hh", blob, 11)
    assert lo == 710 and hi == 1420
    assert blob[15] == 5                              # day-0 icon: rain
    assert len(blob) == 11 + 3 * 5
    assert pt.encode_forecast(_forecast(days=()), 0) is None


def test_infinitime_alert_encoding():
    from vitals.devices import pinetime as pt
    blob = pt.encode_alert("Signal: Alice", "hello there")
    assert blob[:3] == bytes([0xFA, 0x01, 0xFF])
    assert blob[3:] == b"Signal: Alice\x00hello there"
    long = pt.encode_alert("T" * 60, "B" * 90)
    assert len(long) <= 3 + 100


# ── Bangle GB JSON ─────────────────────────────────────────────────

def test_bangle_gb_weather_units():
    from vitals.devices import bangle as bg
    msg = bg.gb_weather(_forecast())
    assert msg["t"] == "weather" and msg["v"] == 1
    assert msg["temp"] == 285                         # 11.6 °C in Kelvin
    assert msg["hi"] == 287 and msg["lo"] == 280
    assert msg["hum"] == 71 and msg["wind"] == 14.5
    assert msg["code"] == 500 and msg["txt"] == "Light Rain"
    sparse = bg.gb_weather(_forecast(temp_c=None, humidity=None, days=()))
    assert "temp" not in sparse and "hum" not in sparse


def test_bangle_gb_message_framing():
    from vitals.devices import bangle as bg
    raw = bg.gb_message({"t": "notify", "id": 1})
    assert raw.startswith(b"\x10GB(") and raw.endswith(b")\n")
    assert raw.decode("ascii")                        # 8-bit safe


def test_bangle_gb_notify_caps_fields():
    from vitals.devices import bangle as bg
    from vitals.devices.base import WatchNotification
    note = WatchNotification(9, "A" * 60, "T" * 100, "B" * 500, 0.0)
    msg = bg.gb_notify(note)
    assert msg["id"] == 9
    assert len(msg["src"]) == 40
    assert len(msg["title"]) == 80 and len(msg["body"]) == 400
