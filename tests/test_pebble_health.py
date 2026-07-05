"""Tests for Pebble health decoding (vitals.devices.pebble.pebble_health).

The DataLogging message layouts and the versioned minute-record format
are pinned here; the byte layouts come from PebbleOS activity structs and
libpebble2's data-logging service. A wrong offset would silently mis-read
steps or heart rate, so the record decoder is exercised across firmware
versions and a truncated tail.
"""

import asyncio
import struct

from vitals.devices.pebble import pebble_health as h


# ── helpers to build wire bytes ───────────────────────────────────

def _minute_sample(steps: int, hr: int = 0, weight: int = 0,
                   resting: int = 0, active: int = 0, distance: int = 0,
                   zone: int = 0, size: int = 16) -> bytes:
    # steps, orientation, vmc(u16), light, flags, resting(u16),
    # active(u16), distance(u16), hr_bpm, hr_weight(u16), hr_zone
    full = (bytes([steps, 0]) + struct.pack("<H", 0) + bytes([0, 0])
            + struct.pack("<HHH", resting, active, distance)
            + bytes([hr]) + struct.pack("<H", weight) + bytes([zone]))
    return full[:size].ljust(size, b"\0")


def _minute_record(time_utc: int, samples: list[bytes], version: int = 13,
                   local_offset_15: int = 0) -> bytes:
    size = len(samples[0])
    header = struct.pack("<HIbBB", version, time_utc, local_offset_15,
                         size, len(samples))
    return header + b"".join(samples)


def _open_session(sid: int, tag: int, item_size: int = 16,
                  uuid: bytes = h.UUID_SYSTEM) -> bytes:
    return (bytes([h.DLS_OPEN_SESSION, sid]) + uuid
            + struct.pack("<II", 1000, tag) + bytes([0])
            + struct.pack("<H", item_size))


# ── DataLogging message parsing ────────────────────────────────────

def test_parse_open_session():
    s = h.parse_open_session(_open_session(3, h.TAG_MINUTE_DATA, 204))
    assert s.session_id == 3
    assert s.app_uuid == h.UUID_SYSTEM
    assert s.tag == h.TAG_MINUTE_DATA
    assert s.item_size == 204
    assert s.is_health is True


def test_open_session_non_system_uuid_is_not_health():
    s = h.parse_open_session(
        _open_session(1, h.TAG_MINUTE_DATA, uuid=b"\x01" * 16))
    assert s.is_health is False


def test_parse_send_data_keeps_payload_ignores_crc():
    payload = (bytes([h.DLS_SEND_DATA, 7])
               + struct.pack("<II", 0xFFFF, 0xDEADBEEF) + b"\x01\x02\x03")
    d = h.parse_send_data(payload)
    assert d.session_id == 7
    assert d.data == b"\x01\x02\x03"


def test_encoders():
    assert h.encode_report() == bytes([0x84])
    assert h.encode_ack(5) == bytes([0x85, 5])
    assert h.encode_empty_session(5) == bytes([0x88, 5])


# ── Minute-record decoding ─────────────────────────────────────────

def test_decode_minute_record_steps_and_hr():
    rec = _minute_record(1_700_000_000,
                         [_minute_sample(50, hr=72, weight=300),
                          _minute_sample(30, hr=70, weight=250)])
    samples = h.decode_minute_records(rec)
    assert [s.steps for s in samples] == [50, 30]
    assert [s.heart_rate_bpm for s in samples] == [72, 70]
    assert [s.heart_rate_weight for s in samples] == [300, 250]
    # Consecutive minutes.
    assert samples[1].time_utc - samples[0].time_utc == 60


def test_decode_minute_record_local_offset_shifts_time():
    rec = _minute_record(1_700_000_000, [_minute_sample(10)],
                         local_offset_15=4)  # +1 hour
    s = h.decode_minute_records(rec)[0]
    assert s.local_time_utc - s.time_utc == 4 * 15 * 60


def test_decode_minute_record_old_version_has_no_hr():
    # A v6 record (pre-heart-rate) with 12-byte samples.
    rec = _minute_record(1_700_000_000, [_minute_sample(40, size=12)],
                         version=6)
    s = h.decode_minute_records(rec)[0]
    assert s.steps == 40
    assert s.heart_rate_bpm is None


def test_decode_minute_record_calories_distance_zone():
    rec = _minute_record(1_700_000_000,
                         [_minute_sample(20, hr=80, resting=1200,
                                         active=450, distance=3300, zone=2)])
    s = h.decode_minute_records(rec)[0]
    # Raw wire units: gram-calories and centimetres.
    assert s.resting_calories == 1200
    assert s.active_calories == 450
    assert s.distance_cm == 3300
    assert s.heart_rate_zone == 2


def test_decode_minute_v6_has_calories_distance_but_no_zone():
    # v6 (calories/distance) but pre-zone: a 13-byte sample carries
    # calories+distance, no HR zone (which lives at offset 15).
    rec = _minute_record(1_700_000_000,
                         [_minute_sample(5, active=90, distance=120, size=13)],
                         version=6)
    s = h.decode_minute_records(rec)[0]
    assert s.active_calories == 90 and s.distance_cm == 120
    assert s.heart_rate_zone is None


def test_decode_minute_record_zero_hr_is_none():
    rec = _minute_record(1_700_000_000, [_minute_sample(5, hr=0)])
    assert h.decode_minute_records(rec)[0].heart_rate_bpm is None


def test_decode_stops_on_truncated_tail():
    rec = _minute_record(1_700_000_000, [_minute_sample(50)])
    # Append a partial record header — must be ignored, not crash.
    samples = h.decode_minute_records(rec + b"\x0d\x00\x01")
    assert len(samples) == 1


def test_decode_empty_blob():
    assert h.decode_minute_records(b"") == []


# ── Summary ────────────────────────────────────────────────────────

def test_summarize_sums_today_and_takes_latest_hr():
    now = 1_700_000_200
    rec = _minute_record(1_700_000_000,
                         [_minute_sample(50, hr=70),
                          _minute_sample(30, hr=75)])
    samples = h.decode_minute_records(rec)
    summary = h.summarize_minutes(samples, now=now)
    assert summary.steps_today == 80
    assert summary.latest_heart_rate == 75  # the later minute
    assert summary.minutes == 2


def test_summarize_excludes_other_days():
    now = 1_700_000_200
    old = _minute_record(1_600_000_000, [_minute_sample(200)])  # months ago
    today = _minute_record(1_700_000_000, [_minute_sample(40)])
    samples = (h.decode_minute_records(old) + h.decode_minute_records(today))
    summary = h.summarize_minutes(samples, now=now)
    assert summary.steps_today == 40


# ── Activity-session decoding (tag 84) ─────────────────────────────

def _session_record(activity: int, start_utc: int, elapsed_sec: int,
                    utc_to_local: int = 0, size: int = 26) -> bytes:
    # 18-byte header + the per-type union padded out to `size` (26 for the
    # ACTIVITY_SESSION_LOGGING_VERSION 3 record).
    header = struct.pack("<HHHiII", 3, size, activity, utc_to_local,
                         start_utc, elapsed_sec)
    return header.ljust(size, b"\0")


def test_decode_activity_record_sleep():
    rec = _session_record(h.SESSION_SLEEP, 1_700_000_000, 8 * 3600,
                          utc_to_local=-5 * 3600)
    [s] = h.decode_activity_records(rec)
    assert s.type == h.SESSION_SLEEP
    assert s.is_sleep and not s.is_deep and not s.is_nap
    assert s.start_utc == 1_700_000_000
    assert s.elapsed_sec == 8 * 3600
    assert s.end_utc == 1_700_000_000 + 8 * 3600
    assert s.local_start_utc == 1_700_000_000 - 5 * 3600


def test_decode_activity_records_multiple_and_types():
    blob = (_session_record(h.SESSION_SLEEP, 1_700_000_000, 28800)
            + _session_record(h.SESSION_RESTFUL_SLEEP, 1_700_003_600, 5400)
            + _session_record(h.SESSION_SLEEP + 4, 1_700_040_000, 1800))  # walk
    sessions = h.decode_activity_records(blob)
    assert [s.type for s in sessions] == [1, 2, 5]
    assert sessions[1].is_deep is True
    assert sessions[2].is_sleep is False  # a walk


def test_decode_activity_nap_flags():
    [deep_nap] = h.decode_activity_records(
        _session_record(h.SESSION_RESTFUL_NAP, 1_700_000_000, 1200))
    assert deep_nap.is_sleep and deep_nap.is_nap and deep_nap.is_deep


def test_decode_activity_stops_on_truncated_tail():
    good = _session_record(h.SESSION_SLEEP, 1_700_000_000, 28800)
    sessions = h.decode_activity_records(good + b"\x03\x00\x1a")
    assert len(sessions) == 1


def test_decode_activity_stops_on_implausible_size():
    bad = struct.pack("<HHHiII", 3, 5, h.SESSION_SLEEP, 0, 1_700_000_000, 1)
    assert h.decode_activity_records(bad) == []


def test_decode_activity_empty_blob():
    assert h.decode_activity_records(b"") == []


def test_decode_activity_record_workout_carries_step_data():
    # A walk: 26-byte record with the stepping union populated.
    rec = (struct.pack("<HHHiII", 3, 26, h.SESSION_WALK, 0,
                       1_700_000_000, 1800)
           + struct.pack("<HHHH", 2200, 95, 30, 1600))  # steps,act,rest,dist_m
    [w] = h.decode_activity_records(rec)
    assert w.is_workout and not w.is_sleep
    assert w.steps == 2200
    assert w.active_kcalories == 95
    assert w.distance_meters == 1600


def test_decode_workout_sessions_filters_sleep_and_minute():
    walk = (struct.pack("<HHHiII", 3, 26, h.SESSION_WALK, 0,
                        1_700_000_000, 1800)
            + struct.pack("<HHHH", 2000, 80, 25, 1500))
    sleep = _session_record(h.SESSION_SLEEP, 1_700_050_000, 28800)
    minute_blob = _minute_record(1_700_000_000, [_minute_sample(50)])
    sessions = {
        1: (h.parse_open_session(
                _open_session(1, h.TAG_ACTIVITY_SESSION)), walk + sleep),
        2: (h.parse_open_session(
                _open_session(2, h.TAG_MINUTE_DATA)), minute_blob),
    }
    out = h.decode_workout_sessions(sessions)
    assert [w.type for w in out] == [h.SESSION_WALK]


def test_decode_sleep_sessions_filters_and_ignores_minute_tag():
    sleep_blob = (_session_record(h.SESSION_SLEEP, 1_700_000_000, 28800)
                  + _session_record(5, 1_700_040_000, 1800))  # a walk
    minute_blob = _minute_record(1_700_000_000, [_minute_sample(50)])
    sessions = {
        1: (h.parse_open_session(
                _open_session(1, h.TAG_ACTIVITY_SESSION)), sleep_blob),
        2: (h.parse_open_session(
                _open_session(2, h.TAG_MINUTE_DATA)), minute_blob),
    }
    # Both activity sessions decode (start-time sorted); the minute-data
    # session is ignored by the activity decoder.
    assert [s.type for s in h.decode_activity_sessions(sessions)] == [1, 5]
    # The sleep filter drops the walk.
    assert [s.type for s in h.decode_sleep_sessions(sessions)] == [1]


# ── Collector over a fake transport ────────────────────────────────

class _FakeWatch:
    """Replies to Report with one health session, and to EmptySession
    with a minute-record blob — the way a real watch streams."""

    def __init__(self):
        self.sent: list[tuple[int, bytes]] = []
        self.collector: h.HealthCollector | None = None
        self.blob = _minute_record(1_700_000_000,
                                   [_minute_sample(50, hr=66)])

    def send(self, endpoint: int, payload: bytes) -> None:
        self.sent.append((endpoint, bytes(payload)))
        loop = asyncio.get_running_loop()
        cmd = payload[0]
        if cmd == h.DLS_REPORT:
            loop.call_soon(lambda: self.collector.handle_message(
                h.DLS_ENDPOINT, _open_session(3, h.TAG_MINUTE_DATA)))
        elif cmd == h.DLS_EMPTY_SESSION:
            sid = payload[1]
            msg = (bytes([h.DLS_SEND_DATA, sid])
                   + struct.pack("<II", 0, 0) + self.blob)
            loop.call_soon(lambda: self.collector.handle_message(
                h.DLS_ENDPOINT, msg))


def test_collector_drains_health_session():
    async def run():
        watch = _FakeWatch()
        collector = h.HealthCollector(watch.send)
        watch.collector = collector
        sessions = await collector.collect(idle_timeout=0.05,
                                           overall_timeout=2.0)
        return watch, sessions

    watch, sessions = asyncio.run(run())
    # The session was captured with its streamed blob.
    assert 3 in sessions
    session, blob = sessions[3]
    assert session.tag == h.TAG_MINUTE_DATA
    assert blob == watch.blob
    # We ACK'd the session and asked it to empty.
    assert (h.DLS_ENDPOINT, h.encode_ack(3)) in watch.sent
    assert (h.DLS_ENDPOINT, h.encode_empty_session(3)) in watch.sent
    # End to end: the blob summarizes to the sample's data.
    summary = h.summarize_sessions(sessions)
    assert summary.latest_heart_rate == 66


def test_collector_acks_but_skips_nonhealth_session():
    async def run():
        watch = _FakeWatch()
        # Override: report a non-system (app) session instead.
        def send(endpoint, payload):
            watch.sent.append((endpoint, bytes(payload)))
            if payload[0] == h.DLS_REPORT:
                asyncio.get_running_loop().call_soon(
                    lambda: watch.collector.handle_message(
                        h.DLS_ENDPOINT,
                        _open_session(9, 200, uuid=b"\x02" * 16)))
        watch.send = send
        collector = h.HealthCollector(watch.send)
        watch.collector = collector
        return watch, await collector.collect(idle_timeout=0.05,
                                               overall_timeout=2.0)

    watch, sessions = asyncio.run(run())
    # Non-health sessions are still tracked + ACK'd, but never emptied.
    assert (h.DLS_ENDPOINT, h.encode_ack(9)) in watch.sent
    assert (h.DLS_ENDPOINT, h.encode_empty_session(9)) not in watch.sent
