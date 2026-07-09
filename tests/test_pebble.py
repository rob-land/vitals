"""Tests for the Pebble / PebbleOS plugin.

These cover discovery, the standard-GATT value parsers (battery), and
the transport-independent wire encoders. The PPoGATT transport itself
isn't implemented yet (it needs a host-side GATT server — see
src/tock/devices/pebble.py), so its framing is pinned against the
Pebble Protocol as documented by libpebble2 / Gadgetbridge rather than
exercised on-device.
"""

import struct

from vitals.devices.pebble.pebble import (
    BATTERY_LEVEL_CHAR_UUID,
    ENDPOINT_TIME,
    PAIRING_CONNECTIVITY_CHAR,
    PAIRING_SERVICE_UUID,
    PAIRING_TRIGGER_CHAR,
    PPOGATT_READ_CHAR,
    PPOGATT_SERVICE_UUID,
    PPOGATT_WRITE_CHAR,
    PebbleDevice,
)


# ── UUID constants ────────────────────────────────────────────────
# Pinned to Gadgetbridge's documented Pebble LE UUIDs.

def test_pairing_service_uuid():
    assert PAIRING_SERVICE_UUID == "0000fed9-0000-1000-8000-00805f9b34fb"


def test_ppogatt_uuids():
    assert PPOGATT_SERVICE_UUID == "30000003-328e-0fbb-c642-1aa6699bdada"
    assert PPOGATT_READ_CHAR    == "30000004-328e-0fbb-c642-1aa6699bdada"
    assert PPOGATT_WRITE_CHAR   == "30000006-328e-0fbb-c642-1aa6699bdada"


def test_pairing_service_char_uuids():
    # Confirmed live on a Core Devices "obelix" watch (fw v4.9.142).
    assert PAIRING_CONNECTIVITY_CHAR == "00000001-328e-0fbb-c642-1aa6699bdada"
    assert PAIRING_TRIGGER_CHAR      == "00000002-328e-0fbb-c642-1aa6699bdada"


def test_battery_level_char_is_sig_standard():
    assert BATTERY_LEVEL_CHAR_UUID == "00002a19-0000-1000-8000-00805f9b34fb"


# ── matches() ─────────────────────────────────────────────────────

def test_matches_classic_pebble_name():
    assert PebbleDevice.matches("Pebble Time 2", []) is True
    assert PebbleDevice.matches("Pebble 2 SE LE a1b2", []) is True


def test_matches_core_devices_names():
    assert PebbleDevice.matches("Core 2 Duo", []) is True
    assert PebbleDevice.matches("Core Time 2", []) is True


def test_matches_pairing_service_uuid():
    assert PebbleDevice.matches(None, [PAIRING_SERVICE_UUID]) is True
    assert PebbleDevice.matches("Whatever",
                                [PAIRING_SERVICE_UUID.upper()]) is True


def test_matches_unknown_device():
    assert PebbleDevice.matches("Bangle.js a1b2", []) is False
    assert PebbleDevice.matches(None, []) is False
    # "Core" without a watch-model hint shouldn't be claimed.
    assert PebbleDevice.matches("CoreAudio Speaker", []) is False


# ── PPoGATT header byte ───────────────────────────────────────────

def test_ppogatt_header_bit_layout():
    # command in low 3 bits, sequence in high 5 bits.
    assert PebbleDevice._ppogatt_header(0, 5) == (5 << 3)        # 0x28
    assert PebbleDevice._ppogatt_header(1, 7) == (7 << 3) | 1    # 0x39


def test_ppogatt_header_sequence_wraps_mod_32():
    assert PebbleDevice._ppogatt_header(0, 32) == 0
    assert PebbleDevice._ppogatt_header(0, 33) == (1 << 3)


def test_ppogatt_header_round_trip():
    for command in range(4):
        for sequence in (0, 1, 15, 31):
            byte = PebbleDevice._ppogatt_header(command, sequence)
            assert PebbleDevice._parse_ppogatt_header(byte) == (
                command, sequence)


# ── Pebble Protocol envelope ──────────────────────────────────────

def test_frame_pebble_packet_header():
    framed = PebbleDevice._frame_pebble_packet(ENDPOINT_TIME, b"\xaa\xbb")
    # uint16 length (payload only) + uint16 endpoint, big-endian.
    assert framed[:4] == struct.pack(">HH", 2, 0x000B)
    assert framed[4:] == b"\xaa\xbb"


def test_frame_pebble_packet_length_excludes_header():
    framed = PebbleDevice._frame_pebble_packet(0x0001, b"")
    assert framed == b"\x00\x00\x00\x01"


# ── Time SetUTC payload ───────────────────────────────────────────

def test_encode_set_utc_layout():
    # unix 0x01020304, offset -300 min (-5h), tz "UTC"
    payload = PebbleDevice._encode_set_utc(0x01020304, -300, "UTC")
    assert payload[0] == 0x03                       # kind = SetUTC
    assert payload[1:5] == b"\x01\x02\x03\x04"      # uint32 BE unix time
    assert payload[5:7] == struct.pack(">h", -300)  # int16 BE offset
    assert payload[7] == 3                           # tz pascal length
    assert payload[8:] == b"UTC"


def test_encode_set_utc_negative_offset_is_signed():
    # -720 minutes must be a signed int16, not wrap to a huge uint.
    payload = PebbleDevice._encode_set_utc(0, -720, "X")
    assert struct.unpack(">h", payload[5:7])[0] == -720


def test_build_set_time_packet_is_ppogatt_data():
    pkt = PebbleDevice._build_set_time_packet(
        0x01020304, 0, "UTC", sequence=5)
    # First byte is the PPoGATT DATA header for sequence 5.
    assert PebbleDevice._parse_ppogatt_header(pkt[0]) == (0, 5)
    # Followed by the framed Time packet.
    inner = PebbleDevice._encode_set_utc(0x01020304, 0, "UTC")
    assert pkt[1:] == PebbleDevice._frame_pebble_packet(ENDPOINT_TIME, inner)


# ── Battery Level parsing (standard 0x2A19) ───────────────────────

def test_parse_battery_level_valid():
    # Confirmed read from the watch: 0x3d == 61 %.
    assert PebbleDevice._parse_battery_level(b"\x3d") == 61
    assert PebbleDevice._parse_battery_level(b"\x00") == 0
    assert PebbleDevice._parse_battery_level(b"\x64") == 100


def test_parse_battery_level_out_of_range():
    # > 100 % is a bad read, not a real level.
    assert PebbleDevice._parse_battery_level(b"\xff") is None


def test_parse_battery_level_empty_or_none():
    assert PebbleDevice._parse_battery_level(b"") is None
    assert PebbleDevice._parse_battery_level(None) is None


# ── Capability flags ──────────────────────────────────────────────
# Time sync rides PPoGATT (PebbleGateway). Alarms / notifications /
# activity aren't wired yet.

def test_capabilities():
    assert PebbleDevice.SUPPORTS_TIME_SYNC is True
    assert PebbleDevice.SUPPORTS_ALARM_PUSH is False
    assert PebbleDevice.SUPPORTS_NOTIFICATIONS is True
    assert PebbleDevice.SUPPORTS_ACTIVITY_READ is True
    assert PebbleDevice.SUPPORTS_FIRMWARE_UPDATE is True


# ── WatchVersion recovery detection ───────────────────────────────
# Pinned to the obelix capture: the running FirmwareMetadata's recovery
# byte (payload offset 45) has bit 0 set on PRF firmware and clear on
# normal firmware. WatchVersion responses start with command 0x01.

def _watch_version_response(version_tag: str, git: str,
                            recovery_byte: int) -> bytes:
    meta = (struct.pack(">I", 0x69B2F4BF)
            + version_tag.encode().ljust(32, b"\0")
            + git.encode().ljust(8, b"\0")
            + bytes([recovery_byte, 0x12, 0x01]))
    return b"\x01" + meta + meta  # running + recovery blocks


def test_parse_is_recovery_prf_watch():
    resp = _watch_version_response("v4.9.142", "e531f0e", 0x05)
    assert PebbleDevice._parse_is_recovery(resp) is True


def test_parse_is_recovery_normal_watch():
    resp = _watch_version_response("v4.11.0", "923d860", 0x0C)
    assert PebbleDevice._parse_is_recovery(resp) is False


def test_parse_is_recovery_rejects_garbage():
    assert PebbleDevice._parse_is_recovery(b"\x01\x02") is None  # too short
    assert PebbleDevice._parse_is_recovery(b"\x00" * 60) is None  # not a response


# ── Activity series (per-minute interval deltas) ──────────────────

def _seed_health_sessions(dev, sessions):
    """Pre-seed the device's cached health drain with raw DataLogging
    sessions (so get_activity_series / get_sleep_series need no live
    link). `sessions` is {session_id: (OpenSession, blob)}."""
    dev._health_sessions = sessions


def _minute_session_blob(start_utc, samples):
    """Build a tag-81 minute-data session: (OpenSession, blob). Each
    sample is (steps, hr)."""
    from vitals.devices.pebble import pebble_health as h

    def sample(steps, hr):
        return (bytes([steps, 0]) + struct.pack("<H", 0) + bytes([0, 0])
                + struct.pack("<HHH", 0, 0, 0)
                + bytes([hr]) + struct.pack("<H", 0) + bytes([0]))

    body = [sample(s, hr) for s, hr in samples]
    header = struct.pack("<HIbBB", 13, start_utc, 0, 16, len(body))
    blob = header + b"".join(body)
    open_msg = (bytes([h.DLS_OPEN_SESSION, 1]) + h.UUID_SYSTEM
                + struct.pack("<II", 1000, h.TAG_MINUTE_DATA) + bytes([0])
                + struct.pack("<H", 16))
    return h.parse_open_session(open_msg), blob


def test_get_activity_series_from_minute_samples():
    import asyncio

    dev = PebbleDevice("AA:BB:CC:DD:EE:FF")
    # steps 50/0/0 over three consecutive minutes, HR 72/none/66.
    session = _minute_session_blob(1_700_000_000,
                                   [(50, 72), (0, 0), (0, 66)])
    _seed_health_sessions(dev, {1: session})
    series = asyncio.run(dev.get_activity_series())

    # The idle minute (no steps, no HR) is dropped; the others become
    # interval deltas with the minute's timestamp.
    assert len(series) == 2
    assert all(r.interval_seconds == 60 for r in series)
    assert series[0].steps == 50
    assert series[0].heart_rate_bpm == 72
    assert series[0].timestamp == 1_700_000_000.0
    assert series[1].steps == 0 and series[1].heart_rate_bpm == 66


def _protobuf_hr_session(time_utc, rows):
    """Build a tag-85 (OpenSession, blob) carrying one MeasurementSet with
    BPM + HRQuality columns. `rows` is [(offset_sec, bpm, quality)]."""
    from vitals.devices.pebble import pebble_health as h
    from vitals.devices.pebble import protobuf_log as pl

    def v(n):
        out = bytearray()
        while True:
            b = n & 0x7F
            n >>= 7
            out.append(b | 0x80 if n else b)
            if not n:
                return bytes(out)

    def ld(field, data):
        return v((field << 3) | 2) + v(len(data)) + data

    def vf(field, val):
        return v((field << 3) | 0) + v(val)

    body = vf(3, time_utc) + vf(7, pl.MTYPE_BPM) + vf(7, pl.MTYPE_HR_QUALITY)
    for off, bpm, q in rows:
        meas = vf(1, off) + ld(2, v(bpm) + v(q))
        body += ld(8, meas)
    payload = ld(12, body)
    item_size = 128
    rec = (len(payload).to_bytes(2, "little") + payload).ljust(item_size, b"\0")
    open_msg = (bytes([h.DLS_OPEN_SESSION, 4]) + h.UUID_SYSTEM
                + struct.pack("<II", 1000, h.TAG_PROTOBUF_LOG)
                + bytes([0]) + struct.pack("<H", item_size))
    return h.parse_open_session(open_msg), rec


def test_get_heart_rate_samples_filters_quality_and_range():
    import asyncio

    dev = PebbleDevice("AA:BB:CC:DD:EE:FF")
    # good 72bpm; off-wrist (dropped); good but 0bpm out of range (dropped).
    session = _protobuf_hr_session(1_700_000_000, [
        (0, 72, 6),    # good → kept
        (5, 80, 1),    # off_wrist → dropped
        (10, 0, 6),    # good quality but bpm out of range → dropped
        (15, 75, 7),   # excellent → kept
    ])
    _seed_health_sessions(dev, {4: session})
    readings = asyncio.run(dev.get_heart_rate_samples())

    assert [(r.heart_rate_bpm, r.timestamp) for r in readings] == [
        (72, 1_700_000_000.0),
        (75, 1_700_000_015.0),
    ]
    assert all(r.interval_seconds is None for r in readings)


def test_get_workout_series_decodes_walk():
    import asyncio

    from vitals.devices.pebble import pebble_health as h

    walk = (struct.pack("<HHHiII", 3, 26, h.SESSION_WALK, 0,
                        1_700_000_000, 1800)
            + struct.pack("<HHHH", 2200, 95, 30, 1600))
    open_msg = (bytes([h.DLS_OPEN_SESSION, 3]) + h.UUID_SYSTEM
                + struct.pack("<II", 1000, h.TAG_ACTIVITY_SESSION)
                + bytes([0]) + struct.pack("<H", 26))
    dev = PebbleDevice("AA:BB:CC:DD:EE:FF")
    _seed_health_sessions(dev, {3: (h.parse_open_session(open_msg), walk)})
    workouts = asyncio.run(dev.get_workout_series())

    assert len(workouts) == 1
    w = workouts[0]
    assert w.kind == "walk"
    assert w.start == 1_700_000_000.0
    assert w.end == 1_700_000_000.0 + 1800
    assert w.steps == 2200
    assert w.active_kcal == 95
    assert w.distance_m == 1600.0


def test_get_sleep_series_pairs_deep_within_overall():
    import asyncio

    from vitals.devices.pebble import pebble_health as h

    def session_blob(records):
        # records: list of (type, start, elapsed)
        blob = b"".join(
            struct.pack("<HHHiII", 3, 26, t, 0, start, el).ljust(26, b"\0")
            for (t, start, el) in records)
        open_msg = (bytes([h.DLS_OPEN_SESSION, 2]) + h.UUID_SYSTEM
                    + struct.pack("<II", 1000, h.TAG_ACTIVITY_SESSION)
                    + bytes([0]) + struct.pack("<H", 26))
        return h.parse_open_session(open_msg), blob

    dev = PebbleDevice("AA:BB:CC:DD:EE:FF")
    start = 1_700_000_000
    _seed_health_sessions(dev, {2: session_blob([
        (h.SESSION_SLEEP, start, 8 * 3600),                  # overall night
        (h.SESSION_RESTFUL_SLEEP, start + 3600, 2 * 3600),   # deep, nested
        (h.SESSION_NAP, start + 50_000, 1800),               # a daytime nap
    ])})
    sleep = asyncio.run(dev.get_sleep_series())

    # Two overall sessions (the restful period is folded into the night).
    assert len(sleep) == 2
    night, nap = sleep
    assert night.start == float(start)
    assert night.end == float(start + 8 * 3600)
    assert night.deep_seconds == 2 * 3600
    assert night.is_nap is False
    assert nap.is_nap is True and nap.deep_seconds == 0
