"""Tests for the A&D UC-450BLE scale's framed protocol.

The test vectors are taken verbatim from the 2026-07-17 btsnoop capture
of the vendor app driving a real UC-450BLE-CV (see docs/and-uc450.md),
so they pin the codec byte-for-byte: frame envelope, command builders,
the login identity echo, and the flag-gated measurement parser.
"""

from datetime import datetime, timezone

from vitals.devices import and_uc450 as u


# Captured frames (phone tz was UTC-5; timestamps are true Unix UTC).
LOGIN_REQUEST = bytes.fromhex("100a000701de5252de010064")
LOGIN_RESPONSE = bytes.fromhex("100b00080101de5252de010102")
INIT_REQUEST = bytes.fromhex("10030009 18".replace(" ", ""))
INIT_RESPONSE = bytes.fromhex("1008000a186a5aaabafb")
BINDING = bytes.fromhex("10040003 0001".replace(" ", ""))
SET_TIME = bytes.fromhex("100810 02036a5aaac2fb".replace(" ", ""))
SETTING_RESP = bytes.fromhex("100510 00100201".replace(" ", ""))
SYNC_REQUEST = bytes.fromhex("10044801 0001".replace(" ", ""))
MEASUREMENT = bytes.fromhex("101148 020000000140082364 6a5aaac9 0208 00"
                            .replace(" ", ""))


# ── frame envelope ────────────────────────────────────────────────
def test_build_and_parse_frame_roundtrip():
    frame = u.build_frame(u.CMD_SYNC_REQUEST, bytes([0x00, 0x01]))
    assert frame == SYNC_REQUEST
    assert frame[1] == 4                          # len covers cmd + payload
    cmd, payload = u.parse_frame(frame)
    assert cmd == u.CMD_SYNC_REQUEST
    assert payload == bytes([0x00, 0x01])


def test_command_codes_are_canonical():
    # The wire uses the enum's canonical (fromByte) codes in both
    # directions — the app's getByte()<<1 values never appear on air.
    assert u.CMD_LOGIN_REQUEST == 0x0007
    assert u.CMD_INIT_REQUEST == 0x0009
    assert u.CMD_SET_TIME == 0x1002
    assert u.CMD_SYNC_REQUEST == 0x4801
    assert u.CMD_MEASUREMENT_DATA == 0x4802


def test_parse_captured_scale_frames():
    assert u.parse_frame(LOGIN_REQUEST) == (
        u.CMD_LOGIN_REQUEST, bytes.fromhex("01de5252de010064"))
    assert u.parse_frame(INIT_REQUEST) == (u.CMD_INIT_REQUEST, b"\x18")
    assert u.parse_frame(SETTING_RESP) == (
        u.CMD_SETTING_RESP, bytes.fromhex("100201"))


def test_ack_frame_is_recognised():
    ack = u.build_ack()
    assert ack == bytes([0x00, 0x01, 0x01])       # confirmed on hardware
    assert u.is_ack(ack)
    assert u.parse_frame(ack) is None             # an ACK carries no command
    assert not u.is_ack(SET_TIME)


# ── command builders (against the captured phone frames) ──────────
def test_login_response_echoes_identity_code():
    login = u.parse_login_request(u.parse_frame(LOGIN_REQUEST)[1])
    assert login["code"] == bytes.fromhex("de5252de")
    assert login["battery_percent"] == 100
    assert u.build_login_response(login["code"], bound=True) == LOGIN_RESPONSE
    # First-time onboarding differs only in the bound byte.
    first = u.build_login_response(login["code"], bound=False)
    assert first == bytes.fromhex("100b00080101de5252de010002")


def test_init_response_echoes_property_and_clock():
    assert u.build_init_response(0x18, 0x6A5AAABA,
                                 tz_offset_hours=-5) == INIT_RESPONSE


def test_set_time_payload():
    assert u.build_set_time(0x6A5AAAC2, tz_offset_hours=-5) == SET_TIME


def test_binding_and_sync_request():
    assert u.build_binding() == BINDING
    assert u.build_sync_request() == SYNC_REQUEST


# ── measurement parsing ───────────────────────────────────────────
def _measurement(flags: int, weight_raw: int, tail: bytes = b"",
                 remaining: int = 0) -> bytes:
    """A syncMeasurementDataResponse payload: fixed 8-byte head + body."""
    return (remaining.to_bytes(2, "big") + (1).to_bytes(2, "big")
            + flags.to_bytes(2, "big") + weight_raw.to_bytes(2, "big") + tail)


def test_parse_captured_measurement():
    r = u.parse_measurement(u.parse_frame(MEASUREMENT)[1])
    assert r["remaining"] == 0
    assert r["sequence"] == 1
    assert r["unit"] == "kg"
    assert r["weight_kg"] == 90.6
    assert r["utc"] == 0x6A5AAAC9                 # 2026-07-17 22:20:57 UTC
    assert r["impedance_ohms"] == 520
    # The trailing 0x00 byte past the flagged fields is ignored.


def test_parse_rejects_implausible_weight():
    assert u.parse_measurement(_measurement(0x0000, 0)) is None       # 0 kg
    assert u.parse_measurement(b"\x00\x00") is None                   # short


def test_parse_full_body_composition():
    flags = (u._WITH_USER_NUMBER | u._WITH_UTC | u._WITH_TIMEZONE
             | u._WITH_BMI | u._WITH_BODY_FAT | u._WITH_FAT_FREE_MASS
             | u._WITH_IMPEDANCE)
    epoch = 1_700_000_000
    tail = (bytes([3])                             # user_number
            + epoch.to_bytes(4, "big")            # utc
            + bytes([2])                          # timezone +2h
            + (228).to_bytes(2, "big")           # bmi 22.8
            + (185).to_bytes(2, "big")           # body fat 18.5 %
            + (5500).to_bytes(2, "big")          # fat-free mass 55.0 kg
            + (480).to_bytes(2, "big"))          # impedance 480 ohm
    r = u.parse_measurement(_measurement(flags, 7250, tail, remaining=1))
    assert r["remaining"] == 1
    assert r["user_number"] == 3
    assert r["utc"] == epoch
    assert r["bmi"] == 22.8
    assert r["body_fat_percentage"] == 18.5
    assert r["fat_free_mass_kg"] == 55.0
    assert r["impedance_ohms"] == 480


def test_build_records_weight_and_derived():
    flags = (u._WITH_UTC | u._WITH_BMI | u._WITH_BODY_FAT
             | u._WITH_FAT_FREE_MASS | u._WITH_IMPEDANCE)
    epoch = 1_700_000_000
    tail = (epoch.to_bytes(4, "big")
            + (228).to_bytes(2, "big")
            + (185).to_bytes(2, "big")
            + (5500).to_bytes(2, "big")
            + (480).to_bytes(2, "big"))
    reading = u.parse_measurement(_measurement(flags, 7250, tail))
    records = u.build_records(reading, "AA:BB:CC:DD:EE:FF", "UC-450BLE")
    types = {r["type"]: r for r in records}
    assert set(types) == {"body_weight", "body_mass_index",
                          "body_fat_percentage", "lean_body_mass"}
    assert types["body_weight"]["value"] == 72.5
    assert types["body_weight"]["unit"] == "kg"
    # Impedance/BIA extras with no record type ride along on weight meta.
    assert types["body_weight"]["meta"]["impedance_ohms"] == 480
    assert types["lean_body_mass"]["value"] == 55.0
    # Timestamp comes from the scale's utc, and drives a stable dedup uuid.
    stamp = datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%Y%m%d%H%M%S")
    assert all(r["uuid"].endswith(stamp) for r in records)


def test_records_dedup_uuid_is_stable():
    r1 = u.parse_measurement(_measurement(u._WITH_UTC, 7250,
                                          (1_700_000_000).to_bytes(4, "big")))
    a = u.build_records(r1, "AA:BB:CC:DD:EE:FF", "x")[0]
    b = u.build_records(r1, "AA:BB:CC:DD:EE:FF", "x")[0]
    assert a["uuid"] == b["uuid"]


# ── discovery ─────────────────────────────────────────────────────
def test_matches_on_name_and_service():
    assert u.AndUc450Device.matches("UC-450BLE_1234", [])
    assert u.AndUc450Device.matches(None, [u.SERVICE])
    assert not u.AndUc450Device.matches("BLESmart_0000", [])
