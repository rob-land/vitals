"""Tests for the iHealth BG5S glucose-meter protocol.

The wire format, auth math, and record layout are reverse-engineered from
the iHealth Gluco-Smart app; see ``docs/ihealth-bg5.md``. These tests pin
the byte-level behaviour against hand-encoded vectors and check the XXTEA
port against an independent canonical implementation. The live handshake
still needs on-device confirmation (see the doc's status note).
"""

import struct

from vitals.devices import ihealth_bg5 as bg


# ── XXTEA: cross-check the port against a canonical implementation ──
def _canonical_xxtea(data: bytes, key: bytes) -> bytes:
    """Textbook XXTEA encrypt (Wheeler & Needham), big-endian, n=4."""
    def m(x):
        return x & 0xFFFFFFFF
    v = list(struct.unpack(">4I", data))
    k = list(struct.unpack(">4I", key))
    n = 4
    rounds = 6 + 52 // n
    total = 0
    z = v[n - 1]
    for _ in range(rounds):
        total = m(total + 0x9E3779B9)
        e = (total >> 2) & 3
        for p in range(n - 1):
            y = v[p + 1]
            mx = m(m((z >> 5 ^ m(y << 2)) + (y >> 3 ^ m(z << 4)))
                   ^ m((total ^ y) + (k[(p & 3) ^ e] ^ z)))
            v[p] = m(v[p] + mx)
            z = v[p]
        y = v[0]
        mx = m(m((z >> 5 ^ m(y << 2)) + (y >> 3 ^ m(z << 4)))
               ^ m((total ^ y) + (k[((n - 1) & 3) ^ e] ^ z)))
        v[n - 1] = m(v[n - 1] + mx)
        z = v[n - 1]
    return struct.pack(">4I", *v)


def test_xxtea_matches_canonical():
    for i in range(8):
        data = bytes((j * 7 + i) & 0xFF for j in range(16))
        key = bytes((j * 13 + 3 * i) & 0xFF for j in range(16))
        assert bg.xxtea_encrypt(data, key) == _canonical_xxtea(data, key)


def test_word_reverse_and_nibble_swap_are_involutions():
    b = bytes(range(16))
    assert bg._word_reverse(bg._word_reverse(b)) == b
    assert bg._nibble_swap(bg._nibble_swap(b)) == b
    # word_reverse flips each 4-byte group.
    assert bg._word_reverse(bytes(range(16)))[:4] == bytes([3, 2, 1, 0])


# ── auth handshake shape / determinism ────────────────────────────
def test_identify_init_shape():
    nonce = bytes(range(16))
    init = bg.identify_init(nonce)
    assert init[0] == 0xA2 and init[1] == 0xFA
    assert len(init) == 18
    assert init[2:] == bg._word_reverse(nonce)


def test_identify_response_deterministic_and_shaped():
    challenge = bytes((i * 3 + 1) & 0xFF for i in range(48))
    resp = bg.identify_response(challenge)
    assert resp[:2] == bytes([0xA2, 0xFC])
    assert len(resp) == 18
    # Pure function of the challenge + baked key: stable across calls.
    assert bg.identify_response(challenge) == resp


def test_bg5s_uses_bg5l_key():
    assert bg._BG5S_AUTH_MODEL == "BG5L"
    assert bg._MODEL_KEYS["BG5L"].hex() == "48e05e3231bbc447a066d8e9a2927b4e"


# ── ASCII UUID helpers ────────────────────────────────────────────
def test_ascii_uuid_roundtrip():
    u = bg._ascii_uuid("com.jiuan.BGV42")
    assert bg._uuid_text(u) == "com.jiuan.BGV42"
    # A standard 16-bit UUID isn't printable ASCII → not vendor text.
    assert bg._uuid_text("00002a37-0000-1000-8000-00805f9b34fb") == ""


def test_matches_by_name_and_service():
    assert bg.IHealthBg5Device.matches("BG5S", [])
    assert bg.IHealthBg5Device.matches(None, [bg._ascii_uuid("com.jiuan.BGV42")])
    assert bg.IHealthBg5Device.matches(None, [bg._ascii_uuid("com.jiuan.BGU42")])
    assert not bg.IHealthBg5Device.matches("Bangle.js", [])


# ── outbound framing ──────────────────────────────────────────────
def _seq():
    n = 1
    while True:
        yield n
        n = (n + 2) & 0xFF


def test_build_frames_single_command():
    # GetStatusInfo: [A2,26,00,00,00] → one frame, frag 0x00.
    frames = bg.build_frames(bytes([0xA2, 0x26, 0, 0, 0]), _seq())
    assert len(frames) == 1
    f = frames[0]
    assert f[0] == 0xB0            # host head
    assert f[1] == 5 + 2           # payload len + 2
    assert f[2] == 0x00            # single, unfragmented
    assert f[3] == 1               # first seq
    assert f[4:9] == bytes([0xA2, 0x26, 0, 0, 0])
    assert f[-1] == sum(f[2:-1]) & 0xFF


def test_build_frames_fragments_identify_init():
    # The 18-byte auth INIT is the only body that fragments (14 + 3).
    frames = bg.build_frames(bg.identify_init(bytes(range(16))), _seq())
    assert len(frames) == 2
    assert frames[0][2] == 0x11    # total=2 (hi nibble 1), idx=total-1=1
    assert frames[1][2] == 0x10    # idx=0 → last
    assert all(f[0] == 0xB0 for f in frames)
    assert all(f[4] == 0xA2 for f in frames)   # proto echoed in each fragment


# ── inbound framing / reassembly ──────────────────────────────────
def _dev_frame(cmd, payload, seq=1, frag=0x00):
    inner = bytes([frag, seq, 0xA2, cmd]) + bytes(payload)
    frame = bytes([0xA0, len(inner)]) + inner
    return frame + bytes([sum(inner) & 0xFF])


def test_frame_ok_and_single_reassembly():
    frame = _dev_frame(0x26, [0x64, 0x19])
    assert bg.frame_ok(frame)
    out = bg.Reassembler().feed(frame)
    assert out == [(0x26, bytes([0x64, 0x19]))]


def test_frame_ok_rejects_bad_checksum():
    frame = bytearray(_dev_frame(0x26, [1, 2, 3]))
    frame[-1] ^= 0xFF
    assert not bg.frame_ok(bytes(frame))


def test_reassembly_two_fragments():
    # A reply command 0x4B split across two fragments (data = A + B).
    chunk0, chunk1 = bytes([1, 2, 3]), bytes([4, 5])
    frag0 = _dev_fragment(0x11, 1, chunk0, first_cmd=0x4B)
    frag1 = _dev_fragment(0x10, 3, chunk1)
    r = bg.Reassembler()
    assert r.feed(frag0) == []
    assert r.feed(frag1) == [(0x4B, chunk0 + chunk1)]


def _dev_fragment(frag, seq, data, first_cmd=None):
    if first_cmd is not None:
        inner = bytes([frag, seq, 0xA2, first_cmd]) + bytes(data)
    else:
        inner = bytes([frag, seq, 0xA2]) + bytes(data)
    frame = bytes([0xA0, len(inner)]) + inner
    return frame + bytes([sum(inner) & 0xFF])


def test_build_ack_matches_sdk_formula():
    frame = _dev_fragment(0x11, 4, bytes([9, 9]), first_cmd=0x4B)
    ack = bg.build_ack(frame)
    # l(b2=(frag&0x0f)+0xA0, b3=(prev+2)); prev=seq-1=3 → b3=5; b2=0xA1.
    assert ack == bytes([0xB0, 0x03, 0xA1, 0x05, 0xA2, (0xA1 + 5 + 0xA2) & 0xFF])


# ── status parsing ────────────────────────────────────────────────
def test_parse_status():
    payload = bytes([
        87,                      # battery 87%
        26, 7, 13, 9, 30, 0,     # 2026-07-13 09:30:00
        0x20,                    # tz = +0x20/4 = +8.0 h
        0x00, 0x05,              # used strips = 5
        0x00, 0x03,              # offline count = 3
        3, 3,                    # code versions
        1,                       # unit
    ])
    s = bg.parse_status(payload)
    assert s["battery"] == 87
    assert (s["year"], s["month"], s["day"]) == (2026, 7, 13)
    assert s["timezone_hours"] == 8.0
    assert s["offline_count"] == 3
    assert s["unit"] == 1


def test_parse_status_short_returns_none():
    assert bg.parse_status(bytes([1, 2, 3])) is None


# ── offline record parsing ────────────────────────────────────────
def _encode_record(year, month, day, hour, minute, second,
                   value_mgdl, tz_quarters=0, time_reliable=True):
    """Hand-encode one 7-byte offline record per the documented bit layout
    — an independent check that parse_offline_records inverts it."""
    tz_raw = abs(tz_quarters) & 0x7F
    if tz_quarters < 0:
        tz_raw |= 0x80
    b0 = ((year - 2000) & 0x7F) | (0 if time_reliable else 0x80)
    b1 = (month & 0x0F) | (((hour >> 2) << 4) & 0xF0)
    b2 = (day & 0x1F) | (tz_raw & 0xE0)
    b3 = minute & 0xFF
    b4 = (second & 0x3F) | ((hour & 0x03) << 6)
    b5 = (((tz_raw & 0x1F) << 3) & 0xF8) | ((value_mgdl >> 8) & 0x03)
    b6 = value_mgdl & 0xFF
    return bytes([b0, b1, b2, b3, b4, b5, b6])


def test_parse_offline_one_record():
    rec = _encode_record(2026, 7, 13, 14, 35, 12, value_mgdl=101,
                         tz_quarters=32)   # +8h
    payload = bytes([1, 0]) + rec          # count=1, packet_index=0
    got = bg.parse_offline_records(payload)
    assert len(got) == 1
    r = got[0]
    assert (r["year"], r["month"], r["day"]) == (2026, 7, 13)
    assert (r["hour"], r["minute"], r["second"]) == (14, 35, 12)
    assert r["value_mgdl"] == 101
    assert r["timezone_hours"] == 8.0
    assert r["time_reliable"] is True


def test_parse_offline_high_value_and_flags():
    # value 260 mg/dL needs the 2 high bits; time flagged unreliable.
    rec = _encode_record(2025, 12, 1, 0, 0, 0, value_mgdl=260,
                         tz_quarters=-20, time_reliable=False)
    got = bg.parse_offline_records(bytes([1, 0]) + rec)
    r = got[0]
    assert r["value_mgdl"] == 260
    assert r["timezone_hours"] == -5.0
    assert r["time_reliable"] is False


def test_parse_offline_multiple_records():
    recs = (_encode_record(2026, 1, 2, 3, 4, 5, 90)
            + _encode_record(2026, 1, 2, 8, 9, 10, 140))
    got = bg.parse_offline_records(bytes([2, 0]) + recs)
    assert [r["value_mgdl"] for r in got] == [90, 140]


# ── record envelope ───────────────────────────────────────────────
def test_build_glucose_record_units_and_dedup():
    reading = {"year": 2026, "month": 7, "day": 13, "hour": 14,
               "minute": 35, "second": 12, "timezone_hours": 8.0,
               "value_mgdl": 101, "time_reliable": True}
    rec = bg.build_glucose_record(reading, "AA:BB", "BG5S")
    assert rec["type"] == "blood_glucose"
    assert rec["value"] == 101 and rec["unit"] == "mg/dL"
    assert rec["source"]["modality"] == "sensed"
    # UTC-normalised dedup key: 14:35:12 +08:00 → 06:35:12 UTC.
    assert rec["uuid"] == "vitals:AA:BB:blood_glucose:20260713063512"
    assert "meta" not in rec


def test_build_glucose_record_flags_unverified_time():
    reading = {"year": 2026, "month": 7, "day": 13, "hour": 1,
               "minute": 0, "second": 0, "timezone_hours": 0.0,
               "value_mgdl": 88, "time_reliable": False}
    rec = bg.build_glucose_record(reading, "AA:BB", "BG5S")
    assert rec["meta"] == {"time_unverified": True}
