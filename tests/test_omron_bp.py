"""Tests for the OMRON BP memory-map protocol (BP5465 / HEM-7382T1).

The record vector below is a real EEPROM record captured from the author's
BP5465 — a known 115/72/73 reading taken 2026-07-08 14:34:51.
"""

from vitals.devices import omron_bp as o

REC = bytes([0x0E, 0x1D, 0xB3, 0x18, 0x00, 0x00, 0x02, 0x00,
             0x00, 0x02, 0x2E, 0x00, 0x5A, 0x48, 0x49, 0x1A])


def _data_frame(addr, data):
    """Build a device-shaped read response: [len][81][00][ah][al][size]
    [data][00][bcc]."""
    body = bytes([0x81, 0x00, (addr >> 8) & 0xFF, addr & 0xFF, len(data)])
    return o.build_frame(body + data + bytes([0x00]))


def test_frames_match_spec_and_verify():
    assert o.START_FRAME == bytes([0x08, 0, 0, 0, 0, 0x10, 0, 0x18])
    assert o.END_FRAME == bytes([0x08, 0x0F, 0, 0, 0, 0, 0, 0x07])
    assert o.frame_ok(o.START_FRAME) and o.frame_ok(o.END_FRAME)
    rf = o.read_frame(0x0810, 0x10)
    assert o.frame_ok(rf)
    assert rf[:6] == bytes([0x08, 0x01, 0x00, 0x08, 0x10, 0x10])


def test_frame_ok_rejects_bad_checksum():
    bad = bytearray(o.START_FRAME)
    bad[-1] ^= 0x01
    assert not o.frame_ok(bytes(bad))


def test_read_response_data():
    data = bytes(range(16))
    assert o.read_response_data(_data_frame(0x0810, data)) == data
    assert o.read_response_data(o.START_FRAME) is None          # not an 0x81 reply
    # A real short ack the monitor sends for an empty / out-of-range slot.
    assert o.read_response_data(bytes.fromhex("088100080010e372")) is None


def test_parse_record_decodes_real_reading():
    r = o.parse_record(REC)
    assert (r["systolic"], r["diastolic"], r["pulse"]) == (115, 72, 73)
    assert (r["year"], r["month"], r["day"]) == (2026, 7, 8)
    assert (r["hour"], r["minute"], r["second"]) == (14, 34, 51)
    assert r["irregular_heartbeat"] is False and r["body_movement"] is False
    assert r["sequence"] == 2


def test_parse_record_rejects_implausible():
    assert o.parse_record(bytes([0xFF] * 16)) is None           # diastolic 255
    bad = bytearray(REC)
    bad[0] = bad[1] = 0x00                                       # month/day 0
    assert o.parse_record(bytes(bad)) is None


def test_parse_records_slices_and_filters():
    blob = REC + bytes([0xFF] * 16) + REC     # valid, empty, valid
    assert len(o.parse_records(blob)) == 2


def test_build_bp_record():
    rec = o.build_bp_record(o.parse_record(REC), "AA:BB", "OMRON")
    assert rec["type"] == "blood_pressure"
    assert rec["value"] == {"systolic": 115, "diastolic": 72}
    assert rec["meta"]["pulse_rate"] == 73
    assert rec["source"]["device_id"] == "AA:BB"
    assert "202607081434" in rec["uuid"]      # deduped on the reading's time


def test_matches_by_name_or_service():
    m = o.OmronBpDevice.matches
    assert m("BLESmart_0000012E37BEDA0", [])
    assert m(None, [o.SERVICE_NEW])
    assert m(None, [o.SERVICE_LEGACY.upper()])
    assert not m("Pebble DD96", ["0000180d-0000-1000-8000-00805f9b34fb"])


def test_frames_from_buffer_reassembles():
    buf = bytearray(o.START_FRAME[:3])        # partial frame
    assert o.frames_from_buffer(buf) == []
    buf.extend(o.START_FRAME[3:])
    assert o.frames_from_buffer(buf) == [o.START_FRAME]
