"""Tests for the WaterH smart water-bottle plugin.

Request hex strings and the 13-byte drink-log layout were recovered from
the WaterH app; these pin them plus the per-model feature gating.
"""

import struct
from datetime import datetime

from vitals.devices.waterh_bottle import (
    MODEL_BOOST,
    MODEL_VITA,
    OP_PUT,
    WaterHBottle,
    build_request,
    decode_water_log_packet,
    drink_timestamp,
    model_for_name,
    req_ack_received_size,
    req_bottle_data,
    req_clear_offline,
    req_registration,
    req_sync_data,
    req_water_logs,
)


# ── Request framing (verbatim from the app) ───────────────────────
def test_request_frame_is_op_len_payload():
    frame = build_request(OP_PUT, bytes([0x02, 0x1C, 0x01]))
    assert frame == bytes.fromhex("50540003021c01")


def test_known_request_hex():
    assert req_bottle_data().hex() == "47540001ff"
    assert req_water_logs().hex() == "4754000106"
    assert req_registration().hex() == "50540003021c01"
    assert req_clear_offline().hex() == "50540003021c05"


def test_ack_received_size_encodes_byte_length():
    # 3 records * 13 bytes = 39 = 0x0027, appended big-endian.
    assert req_ack_received_size(3).hex() == "5250000403060027"


def test_sync_data_layout():
    ts = datetime(2026, 7, 6, 14, 30, 45).timestamp()
    frame = req_sync_data(ts)
    assert frame[:2] == OP_PUT
    assert struct.unpack(">H", frame[2:4])[0] == 20  # payload length
    payload = frame[4:]
    assert len(payload) == 20
    assert payload[0:2] == bytes([0x03, 0x05])           # goal marker
    assert payload[2:4] == b"\x00\x00"                   # default goal
    assert payload[4:6] == bytes([0x07, 0x03])           # date marker
    assert payload[6:12] == bytes([26, 7, 6, 14, 30, 45])  # yy m d h m s
    assert payload[12:14] == bytes([0x07, 0x26])         # reminder marker


# ── Drink-log decoding ────────────────────────────────────────────
def _drink_record(year=26, month=7, day=6, hour=14, minute=30, second=0,
                  amount=250, tds=45, temp=20, flag=0):
    return bytes([year, month, day, hour, minute, second,
                  (amount >> 8) & 0xFF, amount & 0xFF,
                  (tds >> 8) & 0xFF, tds & 0xFF,
                  temp & 0xFF, 0x00, flag])


def test_decode_water_log_packet_fields():
    rec = _drink_record(amount=250, tds=45, temp=20)
    got = decode_water_log_packet(rec, 0)
    assert len(got) == 1
    d = got[0]
    assert d["year"] == 2026 and d["month"] == 7 and d["day"] == 6
    assert d["hour"] == 14 and d["minute"] == 30 and d["second"] == 0
    assert d["amount_ml"] == 250
    assert d["tds"] == 45
    assert d["temp_c"] == 20
    assert d["is_drink"] is True


def test_decode_water_log_packet_flag_marks_non_drink():
    rec = _drink_record(flag=1)
    assert decode_water_log_packet(rec, 0)[0]["is_drink"] is False


def test_decode_water_log_packet_multiple_from_offset():
    blob = b"\xff\xff" + _drink_record(amount=100) + _drink_record(amount=200)
    got = decode_water_log_packet(blob, 2)
    assert [d["amount_ml"] for d in got] == [100, 200]


def test_decode_negative_temperature():
    rec = _drink_record(temp=-5 & 0xFF)
    assert decode_water_log_packet(rec, 0)[0]["temp_c"] == -5


def test_drink_timestamp_is_local_wall_clock():
    rec = decode_water_log_packet(_drink_record(), 0)[0]
    assert drink_timestamp(rec) == datetime(2026, 7, 6, 14, 30, 0).timestamp()


# ── Model detection + feature gating ──────────────────────────────
def test_model_for_name():
    assert model_for_name("WaterH-Bottle-B003") == MODEL_BOOST
    assert model_for_name("WaterH-Bottle-1") == MODEL_VITA
    assert model_for_name("WaterH-xyz") == MODEL_VITA
    assert model_for_name(None) == MODEL_VITA


def test_vita_reading_keeps_water_quality():
    bottle = WaterHBottle("AA:BB:CC:DD:EE:FF", "WaterH-Bottle-1")
    assert bottle.has_water_quality is True
    rec = decode_water_log_packet(_drink_record(temp=21, tds=50), 0)[0]
    reading = bottle._reading_from_record(rec)
    assert reading.amount_ml == 250.0
    assert reading.temperature_c == 21.0
    assert reading.tds_ppm == 50


def test_boost_reading_omits_water_quality():
    bottle = WaterHBottle("AA:BB:CC:DD:EE:FF", "WaterH-Bottle-B003")
    assert bottle.has_water_quality is False
    rec = decode_water_log_packet(_drink_record(temp=21, tds=50), 0)[0]
    reading = bottle._reading_from_record(rec)
    assert reading.amount_ml == 250.0
    assert reading.temperature_c is None
    assert reading.tds_ppm is None


def test_empty_drink_dropped():
    bottle = WaterHBottle("AA:BB:CC:DD:EE:FF", "WaterH-Bottle-1")
    rec = decode_water_log_packet(_drink_record(amount=0), 0)[0]
    assert bottle._reading_from_record(rec) is None


# ── Notification routing ──────────────────────────────────────────
def test_route_water_log_start_packet_collects_drinks():
    bottle = WaterHBottle("AA:BB:CC:DD:EE:FF", "WaterH-Bottle-1")
    record = _drink_record(amount=330)
    # PT start packet: op, total-bytes(BE), this-bytes, 0x06, records...
    packet = OP_PUT + struct.pack(">H", 13) + bytes([13, 0x06]) + record
    bottle._route(packet)
    assert len(bottle._drinks) == 1
    assert bottle._drinks[0]["amount_ml"] == 330
    assert bottle._log_total == 1
    assert bottle._log_done.is_set()


def test_route_report_updates_battery():
    bottle = WaterHBottle("AA:BB:CC:DD:EE:FF", "WaterH-Bottle-1")
    # RT report: op(2), _, _, _, field=2 (power), value
    report = b"RT" + bytes([0, 0, 0, 0x02, 66])
    bottle._route(report)
    assert bottle._battery == 66


def test_route_registration_confirmed():
    bottle = WaterHBottle("AA:BB:CC:DD:EE:FF", "WaterH-Bottle-1")
    # RT report: field 0x1c, value 3 = user confirmed on the bottle
    bottle._route(b"RT" + bytes([0, 0, 0, 0x1C, 3]))
    assert bottle._reg_confirmed.is_set()


def test_route_new_data_sets_model_and_battery():
    bottle = WaterHBottle("AA:BB:CC:DD:EE:FF", "WaterH")
    # RP response, value[2]=0, value[3]=0x27 => Boost, battery at [6]
    resp = b"RP" + bytes([0, 0x27, 0, 0, 55, 0, 0, 0])
    bottle._route(resp)
    assert bottle._model == MODEL_BOOST
    assert bottle._battery == 55
    assert bottle._new_data.is_set()


# ── matches() ─────────────────────────────────────────────────────
def test_matches_on_name_prefix():
    assert WaterHBottle.matches("WaterH-Bottle-1", []) is True
    assert WaterHBottle.matches("WaterH-Bottle-B003", []) is True


def test_does_not_match_other_devices():
    assert WaterHBottle.matches("Mi Smart Band", []) is False
    assert WaterHBottle.matches(None, ["0000ffe0-0000-1000-8000-00805f9b34fb"]) is False


def test_hydration_capability_flag():
    assert WaterHBottle.SUPPORTS_HYDRATION_READ is True
    assert WaterHBottle.INTERACTION == "session"
