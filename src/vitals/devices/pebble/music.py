"""Pebble music endpoint (0x20) — pure encoders and command decode.

The watch's Music app talks a tiny protocol: the phone pushes track
info (0x10, three Pascal strings + LE duration/track counters) and
play state (0x11); the watch sends one-byte playback commands. Layout
per Gadgetbridge's ``encodeSetMusicInfo`` / ``encodeSetMusicState``.
"""

from __future__ import annotations

import struct

ENDPOINT_MUSIC = 0x0020

CMD_SET_MUSIC_INFO = 0x10
CMD_SET_PLAY_STATE = 0x11

STATE_PAUSED = 0x00
STATE_PLAYING = 0x01
STATE_UNKNOWN = 0x04

# Watch → phone command bytes.
WATCH_COMMANDS = {
    0x01: "playpause",
    0x02: "pause",
    0x03: "play",
    0x04: "next",
    0x05: "previous",
    0x06: "volumeup",
    0x07: "volumedown",
    0x08: "refresh",   # GETNOWPLAYING — answer with a fresh push
}


def _pascal(text: str) -> bytes:
    data = (text or "").encode("utf-8")[:255]
    return bytes([len(data)]) + data


def encode_music_info(artist: str, album: str, track: str,
                      duration_s: int = 0, track_count: int = 0,
                      track_nr: int = 0) -> bytes:
    """The SETMUSICINFO payload (endpoint framing is the transport's)."""
    return (bytes([CMD_SET_MUSIC_INFO])
            + _pascal(artist) + _pascal(album) + _pascal(track)
            + struct.pack("<IHH", max(0, duration_s) * 1000,
                          track_count & 0xFFFF, track_nr & 0xFFFF))


def encode_play_state(playing: bool, position_s: int = 0) -> bytes:
    """The SETPLAYSTATE payload: state, position ms, play rate (%),
    shuffle, repeat."""
    state = STATE_PLAYING if playing else STATE_PAUSED
    rate = 100 if playing else 0
    return struct.pack("<BBiiBB", CMD_SET_PLAY_STATE, state,
                       max(0, position_s) * 1000, rate, 0, 0)


def decode_watch_command(payload: bytes) -> str | None:
    """A watch-side button press → a neutral command string."""
    if not payload:
        return None
    return WATCH_COMMANDS.get(payload[0])
