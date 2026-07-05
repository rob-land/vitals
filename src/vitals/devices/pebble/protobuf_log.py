"""Decoder for the Pebble protobuf measurements log (DataLogging tag 85).

Newer PebbleOS logs heart-rate samples through a second channel alongside
the minute records: a protobuf "measurements log". Each tag-85 DataLogging
record is a fixed-size blob = a 2-byte little-endian length header
(`PLogMessageHdr.msg_size`) followed by an encoded `Payload` protobuf
(package `pebble.pipeline`, `src/idl/nanopb`). A `Payload` carries
`MeasurementSet`s; each MeasurementSet is CSV-like — a `types` header
naming the columns and rows of packed `data` under `measurements`.

We hand-decode the protobuf wire format (proto2; no nanopb dependency),
reading only the handful of fields we need. The HR logger records two
columns — `BPM` and `HeartRateQuality` — so each row is one heart-rate
sample with a signal-quality grade we use to drop off-wrist / no-signal
junk.

Everything here is pure and unit-tested against synthetic encodings; the
framing is from PebbleOS `protobuf_log.c` / `protobuf_log_private.h`.

NOTE: the `RR` (HRV), `Temperature`, `VMC`, … types exist in the proto
enum but are NOT logged by the current firmware's HR path, so only BPM +
quality are available here. This decoder has not yet been validated
against an on-device capture.
"""

from __future__ import annotations

from dataclasses import dataclass

# MeasurementSet.Type enum (measurements.proto). Only BPM + HRQuality are
# actually logged today; the rest are listed for when/if firmware adds them.
MTYPE_TIME_MS       = 1
MTYPE_VMC           = 2
MTYPE_STEPS         = 3
MTYPE_DISTANCE_CM   = 4
MTYPE_RESTING_GCAL  = 5
MTYPE_ACTIVE_GCAL   = 6
MTYPE_BPM           = 7
MTYPE_RR            = 8
MTYPE_ORIENTATION   = 9
MTYPE_LIGHT         = 10
MTYPE_TEMPERATURE   = 11
MTYPE_HR_QUALITY    = 12

# MeasurementSet.HeartRateQuality enum.
HR_QUALITY_NAMES = {
    0: "no_accel", 1: "off_wrist", 2: "no_signal", 3: "worst",
    4: "poor", 5: "acceptable", 6: "good", 7: "excellent",
}
HR_QUALITY_ACCEPTABLE = 5  # samples at or above this are trustworthy

# Payload / MeasurementSet / Measurement field numbers we read.
_PAYLOAD_MEASUREMENT_SETS = 12
_MS_TIME_UTC = 3
_MS_TYPES    = 7
_MS_MEASUREMENTS = 8
_MEAS_OFFSET_SEC = 1
_MEAS_DATA       = 2


@dataclass(frozen=True)
class HrSample:
    """One heart-rate sample from the protobuf log."""
    time_utc: int
    bpm: int
    quality: int | None  # HeartRateQuality enum 0-7, or None if not logged

    @property
    def quality_name(self) -> str:
        return HR_QUALITY_NAMES.get(self.quality, "unknown")

    @property
    def is_trustworthy(self) -> bool:
        return self.quality is None or self.quality >= HR_QUALITY_ACCEPTABLE


# ── protobuf wire-format primitives ────────────────────────────────

def _read_varint(buf: bytes, pos: int) -> tuple[int, int]:
    result = shift = 0
    n = len(buf)
    while True:
        if pos >= n:
            raise ValueError("truncated varint")
        b = buf[pos]
        pos += 1
        result |= (b & 0x7F) << shift
        if not b & 0x80:
            return result, pos
        shift += 7
        if shift > 63:
            raise ValueError("varint too long")


def _iter_fields(buf: bytes):
    """Yield (field_number, wire_type, value) for each field. `value` is an
    int for varints, raw bytes for length-delimited / fixed fields."""
    pos, n = 0, len(buf)
    while pos < n:
        tag, pos = _read_varint(buf, pos)
        field, wt = tag >> 3, tag & 7
        if wt == 0:  # varint
            val, pos = _read_varint(buf, pos)
            yield field, wt, val
        elif wt == 2:  # length-delimited
            length, pos = _read_varint(buf, pos)
            if pos + length > n:
                raise ValueError("truncated length-delimited field")
            yield field, wt, buf[pos:pos + length]
            pos += length
        elif wt == 5:  # 32-bit
            if pos + 4 > n:
                raise ValueError("truncated 32-bit field")
            yield field, wt, buf[pos:pos + 4]
            pos += 4
        elif wt == 1:  # 64-bit
            if pos + 8 > n:
                raise ValueError("truncated 64-bit field")
            yield field, wt, buf[pos:pos + 8]
            pos += 8
        else:
            raise ValueError(f"unsupported wire type {wt}")


def _packed_varints(buf: bytes) -> list[int]:
    out: list[int] = []
    pos = 0
    while pos < len(buf):
        val, pos = _read_varint(buf, pos)
        out.append(val)
    return out


# ── message parsing ────────────────────────────────────────────────

def _parse_measurement(buf: bytes) -> tuple[int, list[int]]:
    offset_sec = 0
    data: list[int] = []
    for field, wt, val in _iter_fields(buf):
        if field == _MEAS_OFFSET_SEC and wt == 0:
            offset_sec = val
        elif field == _MEAS_DATA and wt == 2:
            data = _packed_varints(val)        # packed (the firmware default)
        elif field == _MEAS_DATA and wt == 0:
            data.append(val)                    # unpacked fallback
    return offset_sec, data


def _parse_measurement_set(buf: bytes) -> tuple[int, list[int], list[bytes]]:
    time_utc = 0
    types: list[int] = []
    measurements: list[bytes] = []
    for field, wt, val in _iter_fields(buf):
        if field == _MS_TIME_UTC and wt == 0:
            time_utc = val
        elif field == _MS_TYPES and wt == 0:
            types.append(val)                   # unpacked repeated enum
        elif field == _MS_TYPES and wt == 2:
            types.extend(_packed_varints(val))  # packed repeated enum
        elif field == _MS_MEASUREMENTS and wt == 2:
            measurements.append(val)
    return time_utc, types, measurements


def _hr_samples_from_set(buf: bytes) -> list[HrSample]:
    time_utc, types, measurements = _parse_measurement_set(buf)
    if MTYPE_BPM not in types:
        return []
    bpm_i = types.index(MTYPE_BPM)
    q_i = types.index(MTYPE_HR_QUALITY) if MTYPE_HR_QUALITY in types else None
    samples: list[HrSample] = []
    for raw in measurements:
        offset_sec, data = _parse_measurement(raw)
        if bpm_i >= len(data):
            continue
        quality = (data[q_i] if q_i is not None and q_i < len(data) else None)
        samples.append(HrSample(time_utc + offset_sec, data[bpm_i], quality))
    return samples


def _payload_measurement_sets(buf: bytes) -> list[bytes]:
    """The encoded MeasurementSet blobs in a Payload (field 12). Nested
    `payloads` (field 10) aren't produced by the watch, so we don't
    recurse."""
    return [val for field, wt, val in _iter_fields(buf)
            if field == _PAYLOAD_MEASUREMENT_SETS and wt == 2]


def decode_protobuf_log_records(blob: bytes, item_size: int) -> list[HrSample]:
    """Decode a tag-85 session blob into heart-rate samples.

    The blob is a run of fixed `item_size`-byte records (item_size from the
    session's OpenSession), each a 2-byte little-endian `msg_size` header
    followed by an encoded `Payload` protobuf and zero padding. A malformed
    record is skipped rather than aborting the whole scan."""
    samples: list[HrSample] = []
    if item_size < 3:
        return samples
    n = len(blob)
    off = 0
    while off + 2 <= n:
        msg_size = int.from_bytes(blob[off:off + 2], "little")
        body = off + 2
        if 0 < msg_size <= item_size - 2 and body + msg_size <= n:
            payload = blob[body:body + msg_size]
            try:
                for ms in _payload_measurement_sets(payload):
                    samples.extend(_hr_samples_from_set(ms))
            except ValueError:
                pass  # skip a corrupt record, keep going
        off += item_size
    return samples
