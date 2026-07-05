"""Tests for the Pebble weather provider + record serialization.

The WeatherDBEntry byte layout (packed, little-endian) is pinned here
against PebbleOS `weather_db.h`; the Open-Meteo parsing and WMO mapping
are exercised with sample responses.
"""

import struct

from vitals.devices import weather as w


# ── WMO mapping ────────────────────────────────────────────────────

def test_wmo_mapping():
    assert w.wmo_to_type_and_phrase(0) == (w.WT_SUN, "Clear")
    assert w.wmo_to_type_and_phrase(2) == (w.WT_PARTLY_CLOUDY, "Partly Cloudy")
    assert w.wmo_to_type_and_phrase(65) == (w.WT_HEAVY_RAIN, "Heavy Rain")
    assert w.wmo_to_type_and_phrase(71) == (w.WT_LIGHT_SNOW, "Light Snow")
    assert w.wmo_to_type_and_phrase(95)[0] == w.WT_HEAVY_RAIN
    # Unknown code → generic, never crashes.
    assert w.wmo_to_type_and_phrase(123) == (w.WT_GENERIC, "—")


def test_temp_rounds_clamps_and_marks_unknown():
    assert w._temp(11.4) == 11
    assert w._temp(12.6) == 13
    assert w._temp(None) == w.UNKNOWN_TEMP
    assert w._temp("x") == w.UNKNOWN_TEMP
    assert w._temp(40000) == 32767


def test_location_key_is_stable_16_bytes():
    a = w.location_key(51.5074, -0.1278)
    b = w.location_key(51.5074, -0.1278)
    c = w.location_key(48.8566, 2.3522)
    assert len(a) == 16
    assert a == b            # deterministic
    assert a != c            # per-location


# ── serialization (byte-exact) ─────────────────────────────────────

def _forecast(**kw):
    base = dict(location_name="London", short_phrase="Light Rain",
                current_type=w.WT_LIGHT_RAIN, current_temp=12,
                today_high=15, today_low=8,
                tomorrow_type=w.WT_PARTLY_CLOUDY, tomorrow_high=17,
                tomorrow_low=9, update_time_utc=1_700_000_000)
    base.update(kw)
    return w.Forecast(**base)


def test_serialize_entry_layout():
    blob = w.serialize_entry(_forecast(), is_current_location=True)
    (version, cur_temp, cur_type, th, tl, tt, toh, tol, upd, is_cur,
     dsize) = struct.unpack_from("<BhBhhBhhiBH", blob, 0)
    assert version == w.WEATHER_DB_VERSION == 3
    assert cur_temp == 12 and cur_type == w.WT_LIGHT_RAIN
    assert th == 15 and tl == 8
    assert tt == w.WT_PARTLY_CLOUDY and toh == 17 and tol == 9
    assert upd == 1_700_000_000
    assert is_cur == 1

    # The packed header is exactly 20 bytes; the strings follow.
    strings = blob[20:]
    assert dsize == len(strings)
    loc_len = struct.unpack_from("<H", strings, 0)[0]
    assert strings[2:2 + loc_len] == b"London"
    poff = 2 + loc_len
    phr_len = struct.unpack_from("<H", strings, poff)[0]
    assert strings[poff + 2:poff + 2 + phr_len] == b"Light Rain"


def test_serialize_marks_not_current_location():
    blob = w.serialize_entry(_forecast(), is_current_location=False)
    assert blob[17] == 0  # is_current_location byte


def test_serialize_truncates_overlong_strings():
    blob = w.serialize_entry(_forecast(
        location_name="X" * 200, short_phrase="Y" * 200))
    strings = blob[20:]
    loc_len = struct.unpack_from("<H", strings, 0)[0]
    assert loc_len <= 63
    poff = 2 + loc_len
    phr_len = struct.unpack_from("<H", strings, poff)[0]
    assert phr_len <= 31


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
        "current": {"temperature_2m": 11.4, "weather_code": 61},
        "daily": {
            "weather_code": [61, 3],
            "temperature_2m_max": [14.2, 12.0],
            "temperature_2m_min": [7.1, 5.2],
        },
    }
    f = w.parse_forecast(data, "London", now_utc=1_700_000_000)
    assert f.location_name == "London"
    assert f.current_type == w.WT_LIGHT_RAIN
    assert f.short_phrase == "Light Rain"
    assert f.current_temp == 11
    assert f.today_high == 14 and f.today_low == 7
    assert f.tomorrow_type == w.WT_CLOUDY_DAY
    assert f.tomorrow_high == 12 and f.tomorrow_low == 5
    assert f.update_time_utc == 1_700_000_000


def test_parse_forecast_missing_data_uses_unknowns():
    f = w.parse_forecast({}, "Nowhere", now_utc=42)
    assert f.current_temp == w.UNKNOWN_TEMP
    assert f.today_high == w.UNKNOWN_TEMP
    assert f.tomorrow_type == w.WT_UNKNOWN
