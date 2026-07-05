"""Bangle.js firmware update over Nordic Secure DFU.

Bangle.js 2 runs Espruino on a near-stock Nordic nRF52 bootloader, so
its firmware updates over the standard **Nordic Secure DFU** protocol.
Two useful facts: the bootloader's signature check is *disabled* (any
key passes, so the published `.zip` flashes as-is, no signing), and a
failed app flash is recoverable — the bootloader stays put and the watch
falls back to DFU mode.

The catch: Espruino **disables buttonless DFU**, so the watch can only
enter its `DfuTarg` bootloader by a physical long-press. A companion app
can do the flashing but can't trigger DFU mode remotely — the user must
put the watch into DFU mode first. See docs/bangle-firmware.md.

This module is the protocol: parse the DFU `.zip` (the `.dat` init
packet + `.bin` image), the Control-Point command state machine, and a
bleak driver that streams the image to a `DfuTarg`. The package parse and
command encoders are pure and unit-tested; the transfer is on-device.
"""

from __future__ import annotations

import json
import logging
import struct
import zipfile
import zlib
from io import BytesIO

log = logging.getLogger(__name__)

# Secure DFU service + characteristics (Nordic SDK >= 12).
DFU_SERVICE       = "0000fe59-0000-1000-8000-00805f9b34fb"
DFU_CONTROL_POINT = "8ec90001-f315-4f60-9fb8-838830daea50"  # write + notify
DFU_PACKET        = "8ec90002-f315-4f60-9fb8-838830daea50"  # write-no-response

# Control-Point opcodes.
OP_CREATE        = 0x01
OP_SET_PRN       = 0x02
OP_CALC_CHECKSUM = 0x03
OP_EXECUTE       = 0x04
OP_SELECT        = 0x06
OP_RESPONSE      = 0x60
RESULT_SUCCESS   = 0x01

# Object types.
OBJ_COMMAND = 0x01   # the .dat init packet
OBJ_DATA    = 0x02   # the .bin firmware image

# The DfuTarg bootloader advertises under this name.
DFU_TARGET_NAME = "DfuTarg"


class DfuError(Exception):
    """A DFU package problem or a rejected transfer step."""


# ── package parsing (pure) ─────────────────────────────────────────

def parse_dfu_package(data: bytes) -> tuple[bytes, bytes]:
    """Return (init_packet, firmware) from a Nordic DFU `.zip` (the
    application's `.dat` and `.bin`). Raises `DfuError` if it isn't a
    well-formed single-application package."""
    try:
        archive = zipfile.ZipFile(BytesIO(data))
        manifest = json.loads(archive.read("manifest.json"))
    except (zipfile.BadZipFile, KeyError, ValueError) as exc:
        raise DfuError(f"not a valid DFU package: {exc}") from exc

    app = (manifest.get("manifest") or {}).get("application")
    if not app:
        raise DfuError("DFU package has no application image")
    try:
        init_packet = archive.read(app["dat_file"])
        firmware = archive.read(app["bin_file"])
    except KeyError as exc:
        raise DfuError(f"DFU package missing {exc}") from exc
    return init_packet, firmware


# ── Control-Point encoders / parsers (pure) ────────────────────────

def cp_create(object_type: int, size: int) -> bytes:
    return struct.pack("<BBI", OP_CREATE, object_type, size)


def cp_set_prn(value: int) -> bytes:
    return struct.pack("<BH", OP_SET_PRN, value)


def cp_select(object_type: int) -> bytes:
    return bytes([OP_SELECT, object_type])


def cp_calc_checksum() -> bytes:
    return bytes([OP_CALC_CHECKSUM])


def cp_execute() -> bytes:
    return bytes([OP_EXECUTE])


def parse_response(data: bytes, expected_op: int) -> bytes:
    """Validate a Control-Point notification (`0x60, request_op, result`)
    and return the trailing payload. Raises on a non-success result."""
    if len(data) < 3 or data[0] != OP_RESPONSE:
        raise DfuError(f"malformed DFU response: {data.hex()}")
    if data[1] != expected_op:
        raise DfuError(
            f"DFU response for op 0x{data[1]:02x}, expected 0x{expected_op:02x}")
    if data[2] != RESULT_SUCCESS:
        raise DfuError(f"DFU op 0x{expected_op:02x} failed (result {data[2]})")
    return data[3:]


def parse_select(payload: bytes) -> tuple[int, int, int]:
    """(max_size, offset, crc) from a Select response payload."""
    return struct.unpack_from("<III", payload, 0)


def parse_checksum(payload: bytes) -> tuple[int, int]:
    """(offset, crc) from a Calculate-Checksum response payload."""
    return struct.unpack_from("<II", payload, 0)


def crc32(data: bytes, seed: int = 0) -> int:
    """The standard CRC32 the Secure DFU bootloader checksums with."""
    return zlib.crc32(data, seed) & 0xFFFFFFFF


# ── transfer driver (bleak; on-device) ─────────────────────────────

# Packet-char write size; the negotiated MTU usually allows more, but a
# conservative chunk avoids overrunning the controller buffer.
PACKET_CHUNK = 20


async def run_dfu(client, init_packet: bytes, firmware: bytes,
                  on_progress=None) -> None:
    """Run Secure DFU over a connected bleak `client` (the DfuTarg). Sends
    the init packet, then the firmware in bootloader-sized objects, with
    a CRC check after each. `on_progress(stage, sent, total)` reports the
    firmware transfer."""
    import asyncio

    responses: asyncio.Queue = asyncio.Queue()

    def on_notify(_char, data: bytearray) -> None:
        responses.put_nowait(bytes(data))

    await client.start_notify(DFU_CONTROL_POINT, on_notify)
    try:
        async def command(payload: bytes, expected_op: int) -> bytes:
            await client.write_gatt_char(DFU_CONTROL_POINT, payload,
                                         response=True)
            data = await asyncio.wait_for(responses.get(), 30.0)
            return parse_response(data, expected_op)

        async def stream(data: bytes) -> None:
            for i in range(0, len(data), PACKET_CHUNK):
                await client.write_gatt_char(
                    DFU_PACKET, data[i:i + PACKET_CHUNK], response=False)

        await command(cp_set_prn(0), OP_SET_PRN)

        # Command object — the init packet.
        parse_select(await command(cp_select(OBJ_COMMAND), OP_SELECT))
        await command(cp_create(OBJ_COMMAND, len(init_packet)), OP_CREATE)
        await stream(init_packet)
        _offset, crc = parse_checksum(
            await command(cp_calc_checksum(), OP_CALC_CHECKSUM))
        if crc != crc32(init_packet):
            raise DfuError("init-packet checksum mismatch")
        await command(cp_execute(), OP_EXECUTE)

        # Data object — the firmware, one bootloader page-object at a time.
        max_size, _o, _c = parse_select(
            await command(cp_select(OBJ_DATA), OP_SELECT))
        max_size = max_size or 4096
        sent = 0
        running = 0
        total = len(firmware)
        if on_progress:
            on_progress("firmware", 0, total)
        for offset in range(0, total, max_size):
            chunk = firmware[offset:offset + max_size]
            await command(cp_create(OBJ_DATA, len(chunk)), OP_CREATE)
            await stream(chunk)
            running = crc32(chunk, running)
            _off, crc = parse_checksum(
                await command(cp_calc_checksum(), OP_CALC_CHECKSUM))
            if crc != running:
                raise DfuError(f"firmware checksum mismatch at {offset}")
            await command(cp_execute(), OP_EXECUTE)
            sent += len(chunk)
            if on_progress:
                on_progress("firmware", sent, total)
        log.info("Bangle DFU: firmware transfer complete (%d bytes)", total)
    finally:
        try:
            await client.stop_notify(DFU_CONTROL_POINT)
        except Exception:
            pass
