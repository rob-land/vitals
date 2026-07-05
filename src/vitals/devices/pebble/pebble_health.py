"""Pebble health/activity — read steps, heart rate, and sleep.

A Pebble doesn't expose current step/HR totals as a readable value; it
accumulates per-minute health records and ships them to the phone over
the **DataLogging** service (endpoint 0x1A7A). The phone drains the open
sessions, and the health sessions (created under the all-zero "system"
UUID with well-known tags) carry the minute records and activity
sessions we decode here.

  Tags (DlsSystemTag*, all under UUID_SYSTEM = 16 zero bytes):
    81  minute data  — AlgMinuteDLSRecord blobs (steps, HR, light, …)
    84  activity sessions — walks/runs/sleep with start + duration

The minute record is versioned and grows over firmware revisions, so we
read the per-record header (version / sample_size / num_samples) and
decode only the fields a given version actually carries. Layouts are
from PebbleOS `activity_algorithm.h` and libpebble2's data-logging
service; everything here is little-endian (the DataLogging payloads
override the Pebble Protocol's big-endian default).

`HealthCollector` drives the drain over a transport; the decoders and
the summary are pure and unit-tested.
"""

from __future__ import annotations

import asyncio
import logging
import struct
import time
from collections.abc import Callable
from dataclasses import dataclass

log = logging.getLogger(__name__)

# DataLogging service endpoint.
DLS_ENDPOINT = 0x1A7A

# Command byte. The top bit marks direction (set = phone→watch); the low
# 7 bits are the command (DLS_ENDPOINT_CMD_MASK = 0x7f).
DLS_CMD_MASK       = 0x7F
DLS_OPEN_SESSION   = 0x01  # watch→phone
DLS_SEND_DATA      = 0x02  # watch→phone
DLS_CLOSE_SESSION  = 0x03  # watch→phone
DLS_TIMEOUT        = 0x07  # watch→phone
DLS_REPORT         = 0x84  # phone→watch: list your open sessions
DLS_ACK            = 0x85  # phone→watch
DLS_NACK           = 0x86  # phone→watch
DLS_EMPTY_SESSION  = 0x88  # phone→watch: send me this session's data

# Health data-logging tags (under the all-zero system UUID).
UUID_SYSTEM        = bytes(16)
TAG_MINUTE_DATA    = 81
TAG_ACTIVITY_SESSION = 84
TAG_PROTOBUF_LOG   = 85  # protobuf measurements log (per-sample HR + quality)
_HEALTH_TAGS = (TAG_MINUTE_DATA, TAG_ACTIVITY_SESSION, TAG_PROTOBUF_LOG)

# Minute-record sample field offsets (AlgMinuteDLSSample, little-endian)
# and the record versions that introduced each. We read steps always and
# the rest when the record is new enough to carry it. Full layout:
#   steps u8 @0, orientation u8 @1, vmc u16 @2, light u8 @4, flags u8 @5,
#   resting_calories u16 @6, active_calories u16 @8, distance_cm u16 @10,
#   heart_rate_bpm u8 @12, heart_rate_total_weight_x100 u16 @13,
#   heart_rate_zone u8 @15.
# Calories are gram-calories (cal); divide by 1000 for kcal. Distance is
# in centimetres.
_MIN_VERSION_CAL_DIST  = 6   # resting/active calories + distance added
_MIN_VERSION_HR        = 7   # heart_rate_bpm added
_MIN_VERSION_HR_WEIGHT = 12  # heart_rate_total_weight_x100 added
_MIN_VERSION_HR_ZONE   = 13  # heart_rate_zone added
_RESTING_CAL_OFFSET = 6
_ACTIVE_CAL_OFFSET  = 8
_DISTANCE_OFFSET    = 10
_HR_BPM_OFFSET      = 12
_HR_WEIGHT_OFFSET   = 13
_HR_ZONE_OFFSET     = 15
CALORIES_PER_KCAL   = 1000

# Activity session types (ActivitySessionType).
SESSION_SLEEP        = 1
SESSION_RESTFUL_SLEEP = 2
SESSION_NAP          = 3
SESSION_RESTFUL_NAP  = 4
SESSION_WALK         = 5
SESSION_RUN          = 6
SESSION_OPEN         = 7   # generic / catch-all workout
_SLEEP_TYPES = frozenset(
    {SESSION_SLEEP, SESSION_RESTFUL_SLEEP, SESSION_NAP, SESSION_RESTFUL_NAP})
_WORKOUT_TYPES = frozenset({SESSION_WALK, SESSION_RUN, SESSION_OPEN})
# Human-readable name for the workout session types.
WORKOUT_NAMES = {SESSION_WALK: "walk", SESSION_RUN: "run",
                 SESSION_OPEN: "workout"}


# ── DataLogging message encoders / parsers ─────────────────────────

def encode_report() -> bytes:
    """Ask the watch to report its open sessions (empty session list)."""
    return bytes([DLS_REPORT])


def encode_ack(session_id: int) -> bytes:
    return bytes([DLS_ACK, session_id & 0xFF])


def encode_nack(session_id: int) -> bytes:
    return bytes([DLS_NACK, session_id & 0xFF])


def encode_empty_session(session_id: int) -> bytes:
    """Ask the watch to send (then empty) a session's accumulated data."""
    return bytes([DLS_EMPTY_SESSION, session_id & 0xFF])


@dataclass(frozen=True)
class OpenSession:
    session_id: int
    app_uuid: bytes
    timestamp: int
    tag: int
    item_type: int
    item_size: int

    @property
    def is_health(self) -> bool:
        return self.app_uuid == UUID_SYSTEM and self.tag in _HEALTH_TAGS


def parse_open_session(payload: bytes) -> OpenSession:
    """Parse an Open-Session message body (command byte included)."""
    if len(payload) < 29:
        raise ValueError("short open-session message")
    session_id = payload[1]
    app_uuid = bytes(payload[2:18])
    timestamp, tag = struct.unpack_from("<II", payload, 18)
    item_type = payload[26]
    item_size = struct.unpack_from("<H", payload, 27)[0]
    return OpenSession(session_id, app_uuid, timestamp, tag,
                       item_type, item_size)


@dataclass(frozen=True)
class SendData:
    session_id: int
    items_left: int
    crc: int
    data: bytes


def parse_send_data(payload: bytes) -> SendData:
    """Parse a Send-Data message body (command byte included). The
    watch's CRC and items-left fields are unreliable (firmware fills them
    with placeholders) so we ignore them and keep the bytes."""
    if len(payload) < 10:
        raise ValueError("short send-data message")
    session_id = payload[1]
    items_left, crc = struct.unpack_from("<II", payload, 2)
    return SendData(session_id, items_left, crc, bytes(payload[10:]))


# ── Minute-record decoding ─────────────────────────────────────────

@dataclass(frozen=True)
class MinuteSample:
    """One minute of health data. Calories are gram-calories (cal) and
    distance is centimetres — the watch's raw wire units; convert at the
    point of use (`/1000` for kcal, `/100` for metres)."""
    time_utc: int
    local_time_utc: int   # time_utc shifted to the watch's local time
    steps: int
    heart_rate_bpm: int | None
    heart_rate_weight: int | None
    resting_calories: int | None = None  # gram-calories (cal)
    active_calories: int | None = None   # gram-calories (cal)
    distance_cm: int | None = None
    heart_rate_zone: int | None = None


def decode_minute_records(blob: bytes) -> list[MinuteSample]:
    """Decode a tag-81 session blob (a run of AlgMinuteDLSRecords) into
    per-minute samples. Each record is self-describing via its header
    (version, sample_size, num_samples), so mixed/odd records are handled
    and a truncated tail simply stops the scan."""
    samples: list[MinuteSample] = []
    offset = 0
    n = len(blob)
    while offset + 9 <= n:
        version, time_utc = struct.unpack_from("<HI", blob, offset)
        local_offset_15 = struct.unpack_from("<b", blob, offset + 6)[0]
        sample_size = blob[offset + 7]
        num_samples = blob[offset + 8]
        body = offset + 9
        record_end = body + sample_size * num_samples
        if sample_size == 0 or record_end > n:
            break
        local_shift = local_offset_15 * 15 * 60
        for i in range(num_samples):
            base = body + i * sample_size
            sample_time = time_utc + i * 60
            samples.append(MinuteSample(
                time_utc=sample_time,
                local_time_utc=sample_time + local_shift,
                steps=blob[base],
                heart_rate_bpm=_minute_hr(version, sample_size, blob, base),
                heart_rate_weight=_minute_hr_weight(
                    version, sample_size, blob, base),
                resting_calories=_minute_u16(
                    version, _MIN_VERSION_CAL_DIST, _RESTING_CAL_OFFSET,
                    sample_size, blob, base),
                active_calories=_minute_u16(
                    version, _MIN_VERSION_CAL_DIST, _ACTIVE_CAL_OFFSET,
                    sample_size, blob, base),
                distance_cm=_minute_u16(
                    version, _MIN_VERSION_CAL_DIST, _DISTANCE_OFFSET,
                    sample_size, blob, base),
                heart_rate_zone=_minute_hr_zone(
                    version, sample_size, blob, base),
            ))
        offset = record_end
    return samples


def _minute_hr(version: int, sample_size: int, blob: bytes,
               base: int) -> int | None:
    if version < _MIN_VERSION_HR or sample_size <= _HR_BPM_OFFSET:
        return None
    bpm = blob[base + _HR_BPM_OFFSET]
    return bpm if bpm > 0 else None


def _minute_hr_weight(version: int, sample_size: int, blob: bytes,
                      base: int) -> int | None:
    if version < _MIN_VERSION_HR_WEIGHT or sample_size < _HR_WEIGHT_OFFSET + 2:
        return None
    return struct.unpack_from("<H", blob, base + _HR_WEIGHT_OFFSET)[0]


def _minute_hr_zone(version: int, sample_size: int, blob: bytes,
                    base: int) -> int | None:
    if version < _MIN_VERSION_HR_ZONE or sample_size <= _HR_ZONE_OFFSET:
        return None
    return blob[base + _HR_ZONE_OFFSET]


def _minute_u16(version: int, min_version: int, offset: int,
                sample_size: int, blob: bytes, base: int) -> int | None:
    """Read a little-endian u16 field present from `min_version` on,
    guarding the record version and sample width."""
    if version < min_version or sample_size < offset + 2:
        return None
    return struct.unpack_from("<H", blob, base + offset)[0]


@dataclass(frozen=True)
class ActivitySummary:
    steps_today: int
    latest_heart_rate: int | None
    latest_hr_weight: int | None
    minutes: int  # how many minute samples contributed


def summarize_minutes(samples: list[MinuteSample],
                      now: float | None = None) -> ActivitySummary:
    """Aggregate minute samples into today's step total and the most
    recent heart-rate reading. "Today" is the watch's local day."""
    now = time.time() if now is None else now
    today = time.gmtime(now).tm_yday, time.gmtime(now).tm_year
    steps_today = 0
    counted = 0
    latest: MinuteSample | None = None
    for s in samples:
        lt = time.gmtime(s.local_time_utc)
        if (lt.tm_yday, lt.tm_year) == today:
            steps_today += s.steps
            counted += 1
        if s.heart_rate_bpm is not None and (
                latest is None or s.time_utc > latest.time_utc):
            latest = s
    return ActivitySummary(
        steps_today=steps_today,
        latest_heart_rate=latest.heart_rate_bpm if latest else None,
        latest_hr_weight=latest.heart_rate_weight if latest else None,
        minutes=counted,
    )


# ── Activity-session decoding (tag 84) ─────────────────────────────

# ActivitySessionDataLoggingRecord (ACTIVITY_SESSION_LOGGING_VERSION 3):
# a PACKED 18-byte little-endian header — version, size, activity type,
# utc_to_local, start_utc, elapsed_sec — followed by a per-type union we
# don't need. Sleep sessions carry no extra fields (the sleep union member
# is empty), and even walks/runs put everything we care about in the
# header. We step records by their own `size` field, so firmware that
# grows the union still parses.
_SESSION_HEADER = struct.Struct("<HHHiII")
_SESSION_HEADER_LEN = _SESSION_HEADER.size  # 18
_SESSION_SIZE_MAX = 128  # sanity bound on a single record
# The per-type union: ActivitySessionDataStepping = 4×u16 (steps,
# active_kcalories, resting_kcalories, distance_meters); already in kcal
# and metres (unlike the minute record). Sleep's union member is empty.
_SESSION_STEP_DATA = struct.Struct("<HHHH")
_SESSION_STEP_DATA_LEN = _SESSION_STEP_DATA.size  # 8


@dataclass(frozen=True)
class ActivitySession:
    """One activity session — sleep, nap, walk, run, … — from tag 84.

    The stepping fields (`steps`, `active_kcalories`, `resting_kcalories`,
    `distance_meters`) are only meaningful for workout types; sleep
    sessions carry zeros there."""
    type: int
    start_utc: int
    elapsed_sec: int
    utc_to_local: int
    steps: int | None = None
    active_kcalories: int | None = None
    resting_kcalories: int | None = None
    distance_meters: int | None = None

    @property
    def end_utc(self) -> int:
        return self.start_utc + self.elapsed_sec

    @property
    def local_start_utc(self) -> int:
        """start_utc shifted to the watch's local time."""
        return self.start_utc + self.utc_to_local

    @property
    def is_sleep(self) -> bool:
        return self.type in _SLEEP_TYPES

    @property
    def is_deep(self) -> bool:
        """A restful (deep) period — always nested inside an overall
        Sleep/Nap session covering the same span."""
        return self.type in (SESSION_RESTFUL_SLEEP, SESSION_RESTFUL_NAP)

    @property
    def is_nap(self) -> bool:
        return self.type in (SESSION_NAP, SESSION_RESTFUL_NAP)

    @property
    def is_workout(self) -> bool:
        return self.type in _WORKOUT_TYPES


def decode_activity_records(blob: bytes) -> list[ActivitySession]:
    """Decode a tag-84 blob (a run of ActivitySessionDataLoggingRecords)
    into activity sessions. Each record is self-sizing via its `size`
    field, so a truncated or implausible tail simply stops the scan."""
    sessions: list[ActivitySession] = []
    offset = 0
    n = len(blob)
    while offset + _SESSION_HEADER_LEN <= n:
        (_version, size, activity, utc_to_local, start_utc,
         elapsed_sec) = _SESSION_HEADER.unpack_from(blob, offset)
        if not _SESSION_HEADER_LEN <= size <= _SESSION_SIZE_MAX:
            break
        if offset + size > n:
            break
        steps = active = resting = distance = None
        if size >= _SESSION_HEADER_LEN + _SESSION_STEP_DATA_LEN:
            steps, active, resting, distance = _SESSION_STEP_DATA.unpack_from(
                blob, offset + _SESSION_HEADER_LEN)
        sessions.append(ActivitySession(
            type=activity,
            start_utc=start_utc,
            elapsed_sec=elapsed_sec,
            utc_to_local=utc_to_local,
            steps=steps,
            active_kcalories=active,
            resting_kcalories=resting,
            distance_meters=distance,
        ))
        offset += size
    return sessions


# ── DataLogging drain ──────────────────────────────────────────────

# Stop draining after this long with no inbound DataLogging message, or
# after the overall cap, whichever comes first. Each inbound message
# resets the idle timer, so this only fires once the watch is genuinely
# done streaming.
IDLE_TIMEOUT    = 3.0
OVERALL_TIMEOUT = 30.0


class HealthCollector:
    """Drains the watch's open DataLogging sessions over a transport.

    `send(endpoint, payload)` ships a (small, single-packet) DataLogging
    message; the owning transport routes inbound 0x1A7A messages to
    `handle_message`. `collect()` requests the session list, ACKs and
    empties each health session, accumulates the streamed bytes, and
    returns `{session_id: (OpenSession, bytes)}` once the watch goes
    quiet.
    """

    def __init__(self, send: Callable[[int, bytes], None]):
        self._send = send
        self._sessions: dict[int, OpenSession] = {}
        self._data: dict[int, bytearray] = {}
        self._activity: asyncio.Event | None = None

    def handle_message(self, endpoint: int, payload: bytes) -> None:
        if endpoint != DLS_ENDPOINT or not payload:
            return
        if self._activity is not None:
            self._activity.set()
        command = payload[0] & DLS_CMD_MASK
        try:
            if command == DLS_OPEN_SESSION:
                self._on_open(payload)
            elif command == DLS_SEND_DATA:
                self._on_data(payload)
            elif command == DLS_CLOSE_SESSION and len(payload) >= 2:
                self._send(DLS_ENDPOINT, encode_ack(payload[1]))
        except Exception:
            log.debug("Pebble health: bad DataLogging message", exc_info=True)

    async def collect(self, idle_timeout: float = IDLE_TIMEOUT,
                      overall_timeout: float = OVERALL_TIMEOUT
                      ) -> dict[int, tuple[OpenSession, bytes]]:
        self._activity = asyncio.Event()
        self._send(DLS_ENDPOINT, encode_report())
        loop = asyncio.get_running_loop()
        deadline = loop.time() + overall_timeout
        while loop.time() < deadline:
            self._activity.clear()
            try:
                await asyncio.wait_for(self._activity.wait(), idle_timeout)
            except asyncio.TimeoutError:
                break  # the watch has gone quiet — we're done
        return {sid: (self._sessions[sid], bytes(self._data.get(sid, b"")))
                for sid in self._sessions}

    # ── internals ──────────────────────────────────────────────────

    def _on_open(self, payload: bytes) -> None:
        session = parse_open_session(payload)
        self._sessions[session.session_id] = session
        self._data.setdefault(session.session_id, bytearray())
        self._send(DLS_ENDPOINT, encode_ack(session.session_id))
        # Only pull the data for health sessions; ACK-only for the rest.
        if session.is_health:
            self._send(DLS_ENDPOINT, encode_empty_session(session.session_id))

    def _on_data(self, payload: bytes) -> None:
        data = parse_send_data(payload)
        if data.session_id in self._sessions:
            self._data.setdefault(data.session_id, bytearray()).extend(data.data)
        self._send(DLS_ENDPOINT, encode_ack(data.session_id))


def decode_minute_sessions(
        sessions: dict[int, tuple[OpenSession, bytes]]) -> list[MinuteSample]:
    """Decode every minute-data (tag 81) session into a flat, time-sorted
    list of per-minute samples."""
    samples: list[MinuteSample] = []
    for session, blob in sessions.values():
        if session.app_uuid == UUID_SYSTEM and session.tag == TAG_MINUTE_DATA:
            samples.extend(decode_minute_records(blob))
    samples.sort(key=lambda s: s.time_utc)
    return samples


def summarize_sessions(
        sessions: dict[int, tuple[OpenSession, bytes]]) -> ActivitySummary:
    """Decode every minute-data session and summarize them together."""
    return summarize_minutes(decode_minute_sessions(sessions))


def decode_activity_sessions(
        sessions: dict[int, tuple[OpenSession, bytes]]
        ) -> list[ActivitySession]:
    """Decode every activity-session (tag 84) blob into a flat,
    start-time-sorted list of activity sessions."""
    out: list[ActivitySession] = []
    for session, blob in sessions.values():
        if (session.app_uuid == UUID_SYSTEM
                and session.tag == TAG_ACTIVITY_SESSION):
            out.extend(decode_activity_records(blob))
    out.sort(key=lambda s: s.start_utc)
    return out


def decode_sleep_sessions(
        sessions: dict[int, tuple[OpenSession, bytes]]
        ) -> list[ActivitySession]:
    """Just the sleep and nap sessions, start-time-sorted."""
    return [s for s in decode_activity_sessions(sessions) if s.is_sleep]


def decode_workout_sessions(
        sessions: dict[int, tuple[OpenSession, bytes]]
        ) -> list[ActivitySession]:
    """Just the workout sessions (walk/run/open), start-time-sorted."""
    return [s for s in decode_activity_sessions(sessions) if s.is_workout]


def decode_hr_sample_sessions(
        sessions: dict[int, tuple[OpenSession, bytes]]) -> list:
    """Decode every protobuf-log (tag 85) session into a flat,
    time-sorted list of `HrSample`s (per-sample heart rate + quality)."""
    from vitals.devices.pebble.protobuf_log import decode_protobuf_log_records
    samples: list = []
    for session, blob in sessions.values():
        if (session.app_uuid == UUID_SYSTEM
                and session.tag == TAG_PROTOBUF_LOG):
            samples.extend(
                decode_protobuf_log_records(blob, session.item_size))
    samples.sort(key=lambda s: s.time_utc)
    return samples
