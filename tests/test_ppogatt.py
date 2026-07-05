"""Tests for the PPoGATT GATT-server module.

The server itself talks to a live bluetoothd, so registration is
exercised on-device. These pin the Gadgetbridge-compatible UUIDs and
the D-Bus object-path layout — a wrong service path or characteristic
UUID would leave the watch unable to find the data pipe.
"""

import asyncio
import struct

from vitals.devices.pebble import ppogatt


class _FakeServer:
    """Stands in for PpogattServer: captures notify() bytes."""

    def __init__(self):
        self.sent = []
        self.on_write = None

    def notify(self, data):
        self.sent.append(bytes(data))


def test_server_uuids_match_gadgetbridge():
    assert ppogatt.PPOGATT_SERVER_SERVICE == \
        "10000000-328e-0fbb-c642-1aa6699bdada"
    assert ppogatt.PPOGATT_DATA_CHAR == \
        "10000001-328e-0fbb-c642-1aa6699bdada"
    assert ppogatt.PPOGATT_META_CHAR == \
        "10000002-328e-0fbb-c642-1aa6699bdada"
    assert ppogatt.PPOGATT_SECOND_SERVICE == \
        "badbadba-dbad-badb-adba-badbadbadbad"


def test_object_paths_are_nested_under_app_root():
    # The data + meta characteristics must live under the service, which
    # lives under the application root, or RegisterApplication rejects them.
    assert ppogatt.SERVICE_PATH.startswith(ppogatt.APP_ROOT + "/")
    assert ppogatt.DATA_PATH.startswith(ppogatt.SERVICE_PATH + "/")
    assert ppogatt.META_PATH.startswith(ppogatt.SERVICE_PATH + "/")
    assert ppogatt.SECOND_PATH.startswith(ppogatt.APP_ROOT + "/")
    # Distinct paths for every exported object.
    paths = [ppogatt.SERVICE_PATH, ppogatt.DATA_PATH,
             ppogatt.META_PATH, ppogatt.SECOND_PATH]
    assert len(set(paths)) == len(paths)


def test_meta_value_is_valid_pebblemetav0():
    # The watch's PPoGATT client reads this first and rejects the server
    # unless it is >= 18 bytes: {min_ver, max_ver, 16-byte app UUID}.
    v = ppogatt.PPOGATT_META_VALUE
    assert len(v) == 18
    assert v[0] == ppogatt.PPOGATT_MIN_VERSION == 0x00
    assert v[1] == ppogatt.PPOGATT_MAX_VERSION == 0x01
    # All-zero app UUID == Pebble's system session (NOT the all-0xFF
    # "invalid" UUID, which the watch rejects).
    assert v[2:] == bytes(16)
    assert v[2:] != b"\xff" * 16


def test_server_can_be_constructed_without_dbus():
    # Constructing the server must not import dbus_fast (it's lazy), so
    # the rest of the app and these tests stay dependency-free.
    server = ppogatt.PpogattServer(bus=None, on_write=lambda data: None)
    assert server.on_write is not None
    # notify() before register() is a safe no-op (no data char yet).
    server.notify(b"\x00")


# ── PPoGATT link layer ────────────────────────────────────────────

def test_ppogatt_header_round_trip():
    for command in range(4):
        for sequence in (0, 1, 15, 31):
            byte = ppogatt.ppogatt_header(command, sequence)
            assert ppogatt.parse_ppogatt_header(byte) == (command, sequence)


def test_link_answers_reset_request_with_reset_complete():
    server = _FakeServer()
    link = ppogatt.PpogattLink(server)
    # Watch's Reset Request: header 0x02 (cmd 2, seq 0) + version + serial.
    link._handle_inbound(bytes([0x02, 0x01]) + b"C114131100ME")
    assert link.is_open
    # Reset Complete = cmd 3, seq 0, then the two window bytes (25/25).
    assert server.sent[0] == bytes([0x03, 25, 25])


def test_link_acks_data_and_surfaces_message():
    server = _FakeServer()
    seen = []
    link = ppogatt.PpogattLink(server, on_message=lambda e, p: seen.append((e, p)))
    # DATA seq 0 carrying a 1-byte payload on endpoint 0x0011.
    pp = struct.pack(">HH", 1, 0x0011) + b"\x00"
    link._handle_inbound(bytes([ppogatt.ppogatt_header(0, 0)]) + pp)
    # First thing out is the ACK echoing sequence 0.
    assert server.sent[0] == bytes([ppogatt.ppogatt_header(1, 0)])
    assert seen == [(0x0011, b"\x00")]


def test_link_reassembles_message_split_across_packets():
    server = _FakeServer()
    seen = []
    link = ppogatt.PpogattLink(server, on_message=lambda e, p: seen.append((e, p)))
    full = struct.pack(">HH", 4, 0x000b) + b"\x01\x02\x03\x04"
    # Split the framed message across two DATA packets.
    link._handle_inbound(bytes([ppogatt.ppogatt_header(0, 0)]) + full[:5])
    assert seen == []  # incomplete — nothing surfaced yet
    link._handle_inbound(bytes([ppogatt.ppogatt_header(0, 1)]) + full[5:])
    assert seen == [(0x000b, b"\x01\x02\x03\x04")]


def test_link_send_message_frames_envelope():
    server = _FakeServer()
    link = ppogatt.PpogattLink(server)
    link.send_message(0x000b, b"\xaa\xbb")
    # DATA header (seq 0) + uint16 length + uint16 endpoint + payload.
    assert server.sent[0] == (bytes([ppogatt.ppogatt_header(0, 0)])
                              + struct.pack(">HH", 2, 0x000b) + b"\xaa\xbb")
    # Sequence advances for the next send.
    link.send_message(0x000b, b"")
    assert ppogatt.parse_ppogatt_header(server.sent[1][0]) == (0, 1)


# ── Windowed send (firmware transfer) ──────────────────────────────

def _reassemble_data(packets):
    """Concatenate the payloads of a run of DATA packets, asserting the
    sequence increments from zero."""
    body = bytearray()
    for i, pkt in enumerate(packets):
        cmd, seq = ppogatt.parse_ppogatt_header(pkt[0])
        assert (cmd, seq) == (ppogatt.PPOGATT_CMD_DATA, i % ppogatt.PPOGATT_SEQ_MOD)
        body += pkt[1:]
    return bytes(body)


def test_windowed_send_chunks_to_max_payload():
    server = _FakeServer()
    link = ppogatt.PpogattLink(server)
    link.set_max_payload(20)
    payload = bytes(range(50))
    # framed = 4 (len+endpoint) + 50 = 54 bytes -> three 20/20/14 packets,
    # all within the window so no ACKs are needed.
    asyncio.run(link.send_message_windowed(0x00F0, payload))
    assert len(server.sent) == 3
    framed = struct.pack(">HH", len(payload), 0x00F0) + payload
    assert _reassemble_data(server.sent) == framed


def test_windowed_send_blocks_on_window_until_acked():
    server = _FakeServer()
    link = ppogatt.PpogattLink(server)
    link.set_max_payload(20)
    big = bytes(255)  # framed 259 -> ceil(259/20) = 13 packets > window

    async def run():
        task = asyncio.create_task(link.send_message_windowed(0x00F0, big))
        for _ in range(5):
            await asyncio.sleep(0)
        # Gated: exactly one window's worth went out, then it blocked.
        assert not task.done()
        assert len(server.sent) == ppogatt.PPOGATT_TX_INFLIGHT
        # ACK each packet in turn; the window frees and the rest go out.
        for seq in range(13):
            link._handle_inbound(
                bytes([ppogatt.ppogatt_header(ppogatt.PPOGATT_CMD_ACK, seq)]))
            await asyncio.sleep(0)
            await asyncio.sleep(0)
        await asyncio.wait_for(task, 1.0)
        assert len(server.sent) == 13
        assert _reassemble_data(server.sent) == \
            struct.pack(">HH", len(big), 0x00F0) + big

    asyncio.run(run())


def test_reset_request_resets_window_base():
    server = _FakeServer()
    link = ppogatt.PpogattLink(server)
    # Pretend we sent some packets, then the watch resets the session.
    link._tx_seq = 7
    link._send_base = 3
    link._handle_inbound(bytes([0x02, 0x01]) + b"C114131100ME")
    assert link._tx_seq == 0
    assert link._send_base == 0
