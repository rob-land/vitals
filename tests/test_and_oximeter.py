"""Tests for the A&D UP-200BLE oximeter protocol (Contec CMS50D)."""

from vitals.devices import and_oximeter as ox


def make_frame(spo2, pr, tail=b"\x7f\x00"):
    """An 8-byte real-time SpO2+PR frame with a correct checksum."""
    body = bytes([0xEB, 0x01, 0x02 if (pr & 0x80) else 0x00,
                  pr & 0x7F, spo2]) + tail
    return body + bytes([sum(body) & 0x7F])


def test_command_frames_are_short_and_exact():
    # Byte-exact from a btsnoop of the vendor app driving the device — the
    # padded form the device silently ignored is gone.
    assert ox.CONN_NOTIFY == bytes.fromhex("9a1a")
    assert ox.START_SPO2_PR == bytes.fromhex("9b011c")
    assert ox.STOP == bytes.fromhex("9b7f1a")


def test_decode_real_captured_frames():
    # Real 8-byte notifications captured off the device.
    assert ox.decode_spo2_pr(bytes.fromhex("eb0105555f7f0024")) == (95, 85)
    assert ox.decode_spo2_pr(bytes.fromhex("eb01077f7f7f0070")) is None  # no finger


def test_decode_valid_frame():
    frame = make_frame(98, 72)
    assert len(frame) == 8
    assert ox.decode_spo2_pr(frame) == (98, 72)


def test_decode_pulse_high_bit_carried_from_flags():
    # A pulse over 127 needs its top bit carried in the flags byte.
    assert ox.decode_spo2_pr(make_frame(95, 200)) == (95, 200)


def test_decode_rejects_invalid_readings():
    assert ox.decode_spo2_pr(make_frame(127, 60)) is None   # SpO2 0x7F = no finger
    assert ox.decode_spo2_pr(make_frame(0, 60)) is None
    assert ox.decode_spo2_pr(make_frame(98, 0)) is None      # PR invalid
    assert ox.decode_spo2_pr(make_frame(98, 255)) is None


def test_decode_rejects_bad_checksum():
    bad = bytearray(make_frame(98, 72))
    bad[-1] ^= 0x01
    assert ox.decode_spo2_pr(bytes(bad)) is None


def test_decode_ignores_non_spo2_frames():
    assert ox.decode_spo2_pr(make_frame(98, 72)[:6]) is None     # wrong length
    wave = bytes([0xEB, 0x00, 0, 0])
    wave += bytes([sum(wave) & 0x7F])
    assert ox.decode_spo2_pr(wave) is None                       # waveform type


def test_reassemble_multiple_frames():
    buf = bytearray(make_frame(98, 72) + make_frame(97, 70))
    frames = ox.reassemble(buf)
    assert len(frames) == 2 and len(buf) == 0
    assert [ox.decode_spo2_pr(f) for f in frames] == [(98, 72), (97, 70)]


def test_reassemble_waits_for_partial_frame():
    whole = make_frame(96, 66)          # 8 bytes
    buf = bytearray(whole[:5])          # only part of a frame
    assert ox.reassemble(buf) == []
    assert len(buf) == 5                # partial kept for the next chunk
    buf.extend(whole[5:])
    (frame,) = ox.reassemble(buf)
    assert ox.decode_spo2_pr(frame) == (96, 66)


def test_reassemble_skips_noise_before_header():
    buf = bytearray(b"\x00\x11\x22" + make_frame(99, 61))
    (frame,) = ox.reassemble(buf)
    assert ox.decode_spo2_pr(frame) == (99, 61)


def test_matches_by_name_or_service():
    m = ox.AndOximeterDevice.matches
    assert m("UP-200BLE_005765", [])
    assert m(None, [ox.SERVICE])
    assert m(None, [ox.SERVICE.upper()])
    assert not m("Pebble DD96", ["0000180d-0000-1000-8000-00805f9b34fb"])


def test_specificity_is_vendor_level():
    dev = ox.AndOximeterDevice
    assert dev.match_specificity("UP-200BLE_1", []) == dev.MATCH_VENDOR_SERVICE


def test_build_record_is_dedup_friendly():
    r1 = ox.build_reading_record("oxygen_saturation", 98, "%", "AA:BB",
                                 "Ox", now=1_800_000_000.0)
    assert (r1["type"], r1["value"], r1["unit"]) == ("oxygen_saturation", 98, "%")
    assert r1["source"] == {"modality": "sensed", "device_id": "AA:BB",
                            "device_name": "Ox"}
    # Same minute + value → same uuid (so a burst upserts to one record).
    r2 = ox.build_reading_record("oxygen_saturation", 98, "%", "AA:BB",
                                 "Ox", now=1_800_000_030.0)
    assert r1["uuid"] == r2["uuid"]
