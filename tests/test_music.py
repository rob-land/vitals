"""Tests for watch music control: Pebble endpoint-0x20 encoders and
command decode, and the InfiniTime event map."""

import struct

from vitals.devices.pebble import music as pm


def test_music_info_layout():
    payload = pm.encode_music_info("Artist", "Album", "Track",
                                   duration_s=185, track_count=12,
                                   track_nr=3)
    assert payload[0] == pm.CMD_SET_MUSIC_INFO
    off = 1
    parts = []
    for _ in range(3):
        length = payload[off]
        parts.append(payload[off + 1:off + 1 + length])
        off += 1 + length
    assert parts == [b"Artist", b"Album", b"Track"]
    dur_ms, count, nr = struct.unpack_from("<IHH", payload, off)
    assert dur_ms == 185_000 and count == 12 and nr == 3
    assert off + 8 == len(payload)


def test_music_info_empty_strings_are_zero_length():
    payload = pm.encode_music_info("", "", "Song")
    assert payload[1] == 0 and payload[2] == 0     # artist, album empty
    assert payload[3:8] == b"\x04Song"


def test_play_state_layout():
    payload = pm.encode_play_state(True, position_s=42)
    cmd, state, pos_ms, rate, shuffle, repeat = struct.unpack(
        "<BBiiBB", payload)
    assert cmd == pm.CMD_SET_PLAY_STATE
    assert state == pm.STATE_PLAYING and pos_ms == 42_000 and rate == 100
    assert shuffle == 0 and repeat == 0
    paused = pm.encode_play_state(False)
    assert paused[1] == pm.STATE_PAUSED


def test_watch_command_decode():
    assert pm.decode_watch_command(bytes([0x01])) == "playpause"
    assert pm.decode_watch_command(bytes([0x04])) == "next"
    assert pm.decode_watch_command(bytes([0x08])) == "refresh"
    assert pm.decode_watch_command(bytes([0x7F])) is None
    assert pm.decode_watch_command(b"") is None


def test_infinitime_event_map():
    from vitals.devices.pinetime import _MUSIC_EVENTS
    assert _MUSIC_EVENTS[0xE0] == "refresh"
    assert _MUSIC_EVENTS[0x00] == "play"
    assert _MUSIC_EVENTS[0x03] == "next"
    assert _MUSIC_EVENTS[0x06] == "volumedown"
