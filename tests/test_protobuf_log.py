"""Tests for the Pebble protobuf measurements-log decoder (tag 85).

The protobuf wire layout (proto2) and the DataLogging record framing
(2-byte msg_size header + Payload, padded to item_size) are pinned here by
encoding synthetic Payloads and decoding them back. The HR logger records
two columns — BPM + HeartRateQuality — so a row is one heart-rate sample.
"""

from vitals.devices.pebble import protobuf_log as p


# ── minimal proto2 encoders (mirror the wire format) ───────────────

def _varint(n: int) -> bytes:
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


def _tag(field: int, wt: int) -> bytes:
    return _varint((field << 3) | wt)


def _vf(field: int, val: int) -> bytes:        # varint field
    return _tag(field, 0) + _varint(val)


def _ld(field: int, data: bytes) -> bytes:     # length-delimited field
    return _tag(field, 2) + _varint(len(data)) + data


def _measurement(offset_sec: int, data: list[int]) -> bytes:
    packed = b"".join(_varint(v) for v in data)
    return _vf(1, offset_sec) + _ld(2, packed)


def _measurement_set(time_utc: int, types: list[int], rows, packed=False):
    body = _vf(3, time_utc)
    if packed:
        body += _ld(7, b"".join(_varint(t) for t in types))
    else:
        for t in types:
            body += _vf(7, t)
    for offset, data in rows:
        body += _ld(8, _measurement(offset, data))
    return body


def _payload(*measurement_sets: bytes) -> bytes:
    return b"".join(_ld(12, ms) for ms in measurement_sets)


def _record(payload: bytes, item_size: int) -> bytes:
    return (len(payload).to_bytes(2, "little") + payload).ljust(item_size, b"\0")


BPM_Q = [p.MTYPE_BPM, p.MTYPE_HR_QUALITY]
ITEM = 128


# ── tests ──────────────────────────────────────────────────────────

def test_decode_single_record_two_samples():
    ms = _measurement_set(1_700_000_000, BPM_Q,
                          [(0, [72, 6]), (5, [75, 7])])
    blob = _record(_payload(ms), ITEM)
    samples = p.decode_protobuf_log_records(blob, ITEM)
    assert [(s.time_utc, s.bpm, s.quality) for s in samples] == [
        (1_700_000_000, 72, 6),
        (1_700_000_005, 75, 7),
    ]


def test_packed_types_header():
    ms = _measurement_set(1_700_000_000, BPM_Q, [(0, [60, 5])], packed=True)
    [s] = p.decode_protobuf_log_records(_record(_payload(ms), ITEM), ITEM)
    assert s.bpm == 60 and s.quality == 5


def test_quality_name_and_trustworthiness():
    ms = _measurement_set(1_700_000_000, BPM_Q,
                          [(0, [80, 1]), (1, [82, 6])])  # off_wrist, good
    a, b = p.decode_protobuf_log_records(_record(_payload(ms), ITEM), ITEM)
    assert a.quality_name == "off_wrist" and a.is_trustworthy is False
    assert b.quality_name == "good" and b.is_trustworthy is True


def test_bpm_only_set_has_no_quality():
    ms = _measurement_set(1_700_000_000, [p.MTYPE_BPM], [(0, [66])])
    [s] = p.decode_protobuf_log_records(_record(_payload(ms), ITEM), ITEM)
    assert s.bpm == 66 and s.quality is None and s.is_trustworthy is True


def test_non_hr_set_is_ignored():
    # A set with no BPM column yields nothing.
    ms = _measurement_set(1_700_000_000, [p.MTYPE_STEPS], [(0, [10])])
    assert p.decode_protobuf_log_records(_record(_payload(ms), ITEM), ITEM) == []


def test_multiple_records_concatenated():
    r1 = _record(_payload(_measurement_set(1_700_000_000, BPM_Q,
                                           [(0, [70, 6])])), ITEM)
    r2 = _record(_payload(_measurement_set(1_700_000_100, BPM_Q,
                                           [(0, [73, 7])])), ITEM)
    samples = p.decode_protobuf_log_records(r1 + r2, ITEM)
    assert [s.bpm for s in samples] == [70, 73]


def test_two_sets_in_one_payload():
    payload = _payload(
        _measurement_set(1_700_000_000, BPM_Q, [(0, [70, 6])]),
        _measurement_set(1_700_000_060, BPM_Q, [(0, [90, 7])]),
    )
    samples = p.decode_protobuf_log_records(_record(payload, ITEM), ITEM)
    assert [(s.time_utc, s.bpm) for s in samples] == [
        (1_700_000_000, 70), (1_700_000_060, 90)]


def test_corrupt_record_is_skipped_not_fatal():
    good = _record(_payload(_measurement_set(1_700_000_000, BPM_Q,
                                             [(0, [70, 6])])), ITEM)
    # A record whose declared msg_size overruns the body.
    corrupt = (50).to_bytes(2, "little") + b"\x01\x02"
    corrupt = corrupt.ljust(ITEM, b"\0")
    samples = p.decode_protobuf_log_records(good + corrupt, ITEM)
    assert [s.bpm for s in samples] == [70]


def test_empty_and_tiny():
    assert p.decode_protobuf_log_records(b"", ITEM) == []
    assert p.decode_protobuf_log_records(b"\x00" * ITEM, ITEM) == []
    assert p.decode_protobuf_log_records(b"abc", 2) == []  # item_size < 3
