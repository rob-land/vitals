"""Tests for the Yucheng smart-ring plugin.

The wire layouts here were recovered from the YCBT SDK; these golden
tests pin the pure frame codec and record decoders so a regression in
byte offsets is caught without hardware.
"""

import asyncio
import struct
from datetime import datetime

from vitals.devices.yucheng_ring import (
    CAT_SETTING,
    KEY_SETTING_HEART_MONITOR,
    KEY_SETTING_SPO2_MONITOR,
    KEY_SETTING_TEMP_MONITOR,
    SERVICE_UUID,
    YuchengRing,
    build_frame,
    crc16,
    decode_battery,
    decode_power_battery,
    decode_history_heart,
    decode_history_sleep,
    decode_history_sport,
    decode_now_step,
    encode_time,
    parse_frame,
    parse_support_function,
    yc_time_to_unix,
)

YC_EPOCH = 946684800


# ── CRC16 ─────────────────────────────────────────────────────────
def test_crc16_check_value():
    # The SDK's byte-swapping CRC is exactly CRC-16/CCITT-FALSE, whose
    # canonical check value over b"123456789" is 0x29B1. This one assert
    # pins the whole algorithm.
    assert crc16(b"123456789") == 0x29B1


def test_crc16_empty():
    assert crc16(b"") == 0xFFFF


# ── Frame codec ───────────────────────────────────────────────────
def test_build_frame_layout():
    frame = build_frame(2, 1, b"\x7f\x46")
    # cat, key, len(LE), payload, crc(LE)
    assert frame[0] == 2 and frame[1] == 1
    assert frame[2] | (frame[3] << 8) == len(frame) == 8
    assert frame[4:6] == b"\x7f\x46"
    expected_crc = crc16(frame[:6])
    assert frame[6] | (frame[7] << 8) == expected_crc


def test_build_empty_payload_frame_is_six_bytes():
    frame = build_frame(5, 2)
    assert len(frame) == 6
    assert frame[2] | (frame[3] << 8) == 6


def test_parse_frame_round_trip():
    frame = build_frame(2, 12, b"\x01\x02\x03")
    assert parse_frame(frame) == (2, 12, b"\x01\x02\x03")


def test_parse_frame_incomplete_returns_none():
    frame = build_frame(2, 0, b"\x00" * 4)
    assert parse_frame(frame[:-3]) is None  # declared length exceeds buffer


# ── Timestamps ────────────────────────────────────────────────────
def test_yc_time_to_unix_converts_epoch_and_tz():
    # raw + 2000-epoch - tz offset
    assert yc_time_to_unix(1000, 0) == 1000 + YC_EPOCH
    assert yc_time_to_unix(1000, 3600) == 1000 + YC_EPOCH - 3600


def test_encode_time_layout():
    ts = datetime(2026, 7, 6, 14, 30, 45).timestamp()
    payload = encode_time(ts)
    assert len(payload) == 8
    assert payload[0] | (payload[1] << 8) == 2026     # year LE
    assert payload[2] == 7 and payload[3] == 6        # month, day
    assert payload[4:7] == bytes([14, 30, 45])        # h, m, s
    assert payload[7] == 0                             # 2026-07-06 is Monday


# ── Capability bitmap ─────────────────────────────────────────────
def test_parse_support_function_full():
    # byte0 = all high bits: steps(7) sleep(6) hr(3) ; byte1 spo2(3)
    payload = bytes([0b11011001, 0b00001000, 0x00])
    feats = parse_support_function(payload)
    assert "steps" in feats and "sleep" in feats and "heart_rate" in feats
    assert "spo2" in feats


def test_parse_support_function_minimal_device():
    # A plain pedometer: only the step bit, short bitmap.
    feats = parse_support_function(bytes([0b10000000]))
    assert feats == {"steps"}


def test_parse_support_function_empty_is_safe():
    assert parse_support_function(b"") == set()


# ── Snapshot decoders ─────────────────────────────────────────────
def test_decode_battery():
    payload = bytes([0, 0, 0, 5, 1, 87, 0, 0])
    assert decode_battery(payload) == 87


def test_decode_battery_out_of_range():
    assert decode_battery(bytes([0, 0, 0, 0, 0, 200])) is None
    assert decode_battery(b"\x00\x00") is None


def test_decode_power_battery():
    # PowerStatistics response: battery percentage at byte 29.
    payload = bytes(29) + bytes([87]) + bytes([0, 0, 0, 0])
    assert decode_power_battery(payload) == 87
    assert decode_power_battery(bytes(10)) is None       # too short
    assert decode_power_battery(bytes(29) + bytes([200])) is None  # out of range


def test_decode_now_step():
    # steps u24 = 12345, cal u16 = 320, dist u16 = 8000
    payload = struct.pack("<I", 12345)[:3] + struct.pack("<H", 320) + struct.pack("<H", 8000)
    got = decode_now_step(payload)
    assert got == {"steps": 12345, "calories": 320, "distance_m": 8000}


# ── History decoders ──────────────────────────────────────────────
def test_decode_history_sport():
    block = struct.pack("<IIHHH", 1000, 1900, 500, 300, 25)
    got = decode_history_sport(block + block, tz_offset=0)
    assert len(got) == 2
    first = got[0]
    assert first["start"] == 1000 + YC_EPOCH
    assert first["end"] == 1900 + YC_EPOCH
    assert first["steps"] == 500
    assert first["distance_m"] == 300
    assert first["calories"] == 25


def test_decode_history_sport_ignores_short_tail():
    block = struct.pack("<IIHHH", 1000, 1900, 500, 300, 25)
    assert len(decode_history_sport(block + b"\x00\x03", tz_offset=0)) == 1


def test_decode_history_heart_drops_zero_bpm():
    good = struct.pack("<I", 1000) + bytes([1, 72])
    zero = struct.pack("<I", 1060) + bytes([1, 0])
    got = decode_history_heart(good + zero, tz_offset=0)
    assert len(got) == 1
    assert got[0]["bpm"] == 72
    assert got[0]["timestamp"] == 1000 + YC_EPOCH


def test_decode_history_sleep_collects_deep_spans():
    header = (bytes([0, 0]) + struct.pack("<H", 36)
              + struct.pack("<I", 1000) + struct.pack("<I", 4600) + bytes(8))
    # sub-record length is in SECONDS (u24 le): 1800 s = 30 min.
    deep = bytes([0xF1]) + struct.pack("<I", 1000) + struct.pack("<I", 1800)[:3]
    light = bytes([0xF2]) + struct.pack("<I", 2800) + struct.pack("<I", 1800)[:3]
    sessions = decode_history_sleep(header + deep + light, tz_offset=0)
    assert len(sessions) == 1
    s = sessions[0]
    assert s["start"] == 1000 + YC_EPOCH
    assert s["end"] == 4600 + YC_EPOCH
    assert s["deep_spans"] == ((1000 + YC_EPOCH, 1000 + YC_EPOCH + 1800),)


# ── matches() ─────────────────────────────────────────────────────
def test_matches_on_service_uuid():
    assert YuchengRing.matches("R02", [SERVICE_UUID]) is True
    assert YuchengRing.matches("R02", [SERVICE_UUID.upper()]) is True


def test_does_not_match_without_service_uuid():
    assert YuchengRing.matches("Some Ring", []) is False
    assert YuchengRing.matches(None, ["0000180d-0000-1000-8000-00805f9b34fb"]) is False


def test_capability_flags_advertise_family_support():
    assert YuchengRing.SUPPORTS_TIME_SYNC is True
    assert YuchengRing.SUPPORTS_ACTIVITY_READ is True
    assert YuchengRing.SUPPORTS_SLEEP_READ is True
    assert YuchengRing.SUPPORTS_MONITORING_CONFIG is True
    assert YuchengRing.INTERACTION == "session"


# ── configure_monitoring ──────────────────────────────────────────
def _run_configure(features, enabled, interval):
    """Run configure_monitoring with a captured writer and given
    capability features; return the parsed frames it wrote."""
    ring = YuchengRing("AA:BB:CC:DD:EE:FF", "R02")
    ring._features = set(features)
    written = []

    async def fake_write(frame):
        written.append(frame)

    ring._write = fake_write
    asyncio.run(ring.configure_monitoring(enabled, interval))
    return [parse_frame(f) for f in written]


def test_configure_monitoring_enables_supported_sensors():
    frames = _run_configure({"heart_rate", "spo2", "temperature"}, True, 10)
    keys = {key for cat, key, _ in frames if cat == CAT_SETTING}
    assert keys == {KEY_SETTING_HEART_MONITOR, KEY_SETTING_SPO2_MONITOR,
                    KEY_SETTING_TEMP_MONITOR}
    # every toggle payload is [enabled, interval]
    for _cat, _key, payload in frames:
        assert payload == bytes([1, 10])


def test_configure_monitoring_skips_absent_sensors():
    # A ring with only a heart-rate sensor: only the HR toggle is sent.
    frames = _run_configure({"heart_rate"}, True, 30)
    assert len(frames) == 1
    cat, key, payload = frames[0]
    assert (cat, key, payload) == (CAT_SETTING, KEY_SETTING_HEART_MONITOR,
                                   bytes([1, 30]))


def test_configure_monitoring_disable_sends_zero_flag():
    frames = _run_configure({"heart_rate", "temperature"}, False, 15)
    assert all(payload[0] == 0 for _c, _k, payload in frames)


def test_configure_monitoring_clamps_interval_to_one_byte():
    frames = _run_configure({"heart_rate"}, True, 9999)
    assert frames[0][2] == bytes([1, 255])
    frames = _run_configure({"heart_rate"}, True, 0)
    assert frames[0][2] == bytes([1, 1])


def test_configure_monitoring_no_sensors_writes_nothing():
    assert _run_configure(set(), True, 10) == []
