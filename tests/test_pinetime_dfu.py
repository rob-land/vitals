"""Tests for the PineTime legacy Nordic DFU protocol.

The encoders and notification parsers are pure; `run_legacy_dfu` is
exercised end-to-end against a scripted fake bleak client that answers
like an InfiniTime watch (responses after Start/Init/Receive/Validate,
packet receipts every RECEIPT_INTERVAL packets). The real transfer is
on-device.
"""

import asyncio
import struct

import pytest

from vitals.devices import pinetime_dfu as dfu


# ── encoders / parsers ─────────────────────────────────────────────

def test_control_point_encoders():
    assert dfu.cp_start_dfu() == bytes([0x01, 0x04])
    assert dfu.cp_init_params(complete=False) == bytes([0x02, 0x00])
    assert dfu.cp_init_params(complete=True) == bytes([0x02, 0x01])
    assert dfu.cp_receive_image() == bytes([0x03])
    assert dfu.cp_validate() == bytes([0x04])
    assert dfu.cp_activate_and_reset() == bytes([0x05])
    assert dfu.cp_receipt_interval(10) == struct.pack("<BH", 0x08, 10)


def test_start_sizes_is_application_only():
    # softdevice and bootloader sizes stay zero.
    assert dfu.start_sizes(0x4000) == struct.pack("<III", 0, 0, 0x4000)


def test_parse_response_accepts_success():
    dfu.parse_response(bytes([0x10, dfu.OP_START_DFU, 0x01]),
                       dfu.OP_START_DFU)


def test_parse_response_rejects_failure_and_wrong_op():
    with pytest.raises(dfu.DfuError):
        dfu.parse_response(bytes([0x10, dfu.OP_VALIDATE, 0x05]),  # CRC error
                           dfu.OP_VALIDATE)
    with pytest.raises(dfu.DfuError):
        dfu.parse_response(bytes([0x10, dfu.OP_INITIALIZE, 0x01]),
                           dfu.OP_START_DFU)
    with pytest.raises(dfu.DfuError):
        dfu.parse_response(b"\x99", dfu.OP_START_DFU)


def test_parse_receipt():
    assert dfu.parse_receipt(struct.pack("<BI", 0x11, 1200)) == 1200
    with pytest.raises(dfu.DfuError):
        dfu.parse_receipt(bytes([0x10, 0, 0, 0, 0]))
    with pytest.raises(dfu.DfuError):
        dfu.parse_receipt(bytes([0x11, 0]))


def test_package_parser_is_shared_with_bangle():
    from vitals.devices import bangle_dfu
    assert dfu.parse_dfu_package is bangle_dfu.parse_dfu_package
    assert dfu.DfuError is bangle_dfu.DfuError


# ── transfer driver against a scripted fake watch ──────────────────

class FakeInfiniTime:
    """A bleak-client stand-in that speaks the watch's side of legacy
    DFU: acknowledges each phase and emits a packet receipt every
    RECEIPT_INTERVAL image packets."""

    def __init__(self, drop_activate=False, lie_in_receipts=False):
        self.notify_cb = None
        self.control_writes: list[bytes] = []
        self.packet_writes: list[bytes] = []
        self._image_bytes = 0
        self._image_packets = 0
        self._phase = "idle"
        self.drop_activate = drop_activate
        self.lie_in_receipts = lie_in_receipts

    async def start_notify(self, _char, callback):
        self.notify_cb = callback

    async def stop_notify(self, _char):
        self.notify_cb = None

    def _notify(self, data: bytes) -> None:
        self.notify_cb(None, bytearray(data))

    async def write_gatt_char(self, char, data, response=False):
        if char == dfu.DFU_CONTROL_POINT:
            await self._on_control(bytes(data))
        else:
            await self._on_packet(bytes(data))

    async def _on_control(self, data: bytes) -> None:
        self.control_writes.append(data)
        op = data[0]
        if op == dfu.OP_START_DFU:
            self._phase = "start"
        elif op == dfu.OP_INITIALIZE and data[1] == dfu.INIT_RECEIVE:
            self._phase = "init"
        elif op == dfu.OP_INITIALIZE and data[1] == dfu.INIT_COMPLETE:
            self._notify(bytes([0x10, dfu.OP_INITIALIZE, 0x01]))
        elif op == dfu.OP_RECEIVE_IMAGE:
            self._phase = "image"
        elif op == dfu.OP_VALIDATE:
            self._notify(bytes([0x10, dfu.OP_VALIDATE, 0x01]))
        elif op == dfu.OP_ACTIVATE_RESET:
            if self.drop_activate:
                raise OSError("connection dropped (watch rebooting)")

    async def _on_packet(self, data: bytes) -> None:
        self.packet_writes.append(data)
        if self._phase == "start":
            self.expected_size = struct.unpack("<III", data)[2]
            self._phase = "sizes-taken"
            self._notify(bytes([0x10, dfu.OP_START_DFU, 0x01]))
        elif self._phase == "image":
            self._image_bytes += len(data)
            self._image_packets += 1
            if self._image_packets % dfu.RECEIPT_INTERVAL == 0:
                count = (self._image_bytes - 1 if self.lie_in_receipts
                         else self._image_bytes)
                self._notify(struct.pack("<BI", 0x11, count))
            if self._image_bytes >= self.expected_size:
                self._notify(bytes([0x10, dfu.OP_RECEIVE_IMAGE, 0x01]))


def test_run_legacy_dfu_full_transfer():
    watch = FakeInfiniTime(drop_activate=True)
    init_packet = b"\x00" * 14
    firmware = bytes(range(256)) * 17  # 4352 bytes; not chunk-aligned +2
    progress: list[tuple[str, int, int]] = []

    asyncio.run(dfu.run_legacy_dfu(
        watch, init_packet, firmware,
        on_progress=lambda *a: progress.append(a)))

    # The init packet went over the Packet characteristic after the
    # 12-byte sizes prelude, and the image bytes all arrived.
    assert watch.packet_writes[0] == dfu.start_sizes(len(firmware))
    assert watch.packet_writes[1] == init_packet
    image = b"".join(watch.packet_writes[2:])
    assert image == firmware
    # The phases ran in order and the reboot write was survived.
    ops = [w[0] for w in watch.control_writes]
    assert ops == [dfu.OP_START_DFU, dfu.OP_INITIALIZE, dfu.OP_INITIALIZE,
                   dfu.OP_PKT_RECEIPT_REQ, dfu.OP_RECEIVE_IMAGE,
                   dfu.OP_VALIDATE, dfu.OP_ACTIVATE_RESET]
    # Progress is monotonic and finishes at the full size.
    sizes = [sent for _stage, sent, _total in progress]
    assert sizes == sorted(sizes) and sizes[-1] == len(firmware)


def test_run_legacy_dfu_rejects_out_of_sync_receipts():
    watch = FakeInfiniTime(lie_in_receipts=True)
    with pytest.raises(dfu.DfuError, match="out of sync"):
        asyncio.run(dfu.run_legacy_dfu(
            watch, b"\x00" * 14, bytes(400)))
