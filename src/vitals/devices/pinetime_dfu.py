"""PineTime firmware update over Nordic *legacy* DFU.

InfiniTime updates over the legacy Nordic DFU protocol (SDK ≤ 11) —
not the Secure DFU that Bangle.js uses — and, unlike the Bangle,
exposes the DFU service from the *running* firmware. So an update
happens over the watch's normal connection: no bootloader button
dance, no separate `DfuTarg` device. The release asset
(`pinetime-mcuboot-app-dfu-<version>.zip`) has the same
manifest.json + `.dat`/`.bin` layout as a Secure DFU package, so the
package parser is shared with `bangle_dfu`.

The protocol, application image only:

  1. **Start DFU** (`01 04`) on the Control Point, then the image
     sizes (softdevice, bootloader, application — three uint32 LE) on
     the Packet characteristic; the watch acknowledges with a response
     notification.
  2. **Initialize DFU parameters** (`02 00`), stream the init packet
     (`.dat`) over the Packet characteristic, then `02 01` to finish.
  3. Request a **packet-receipt notification** every N packets
     (`08 N`), send **Receive Firmware Image** (`03`), and stream the
     image in 20-byte writes — pausing at each receipt to confirm the
     watch's byte count matches ours (that's the protocol's only flow
     control *and* its transfer check).
  4. **Validate** (`04`), then **Activate & Reset** (`05`): the watch
     reboots and MCUBoot swaps the new image in.

After the reboot the new firmware runs *provisionally*: the user must
validate it on the watch (Settings → Firmware → Validate) or MCUBoot
rolls back on the next reset. The plugin surfaces that in the dialog
copy. Encoders/parsers are pure and unit-tested; `run_legacy_dfu` is
tested against a scripted fake client, and the real transfer needs
the watch.

Reference: InfiniTime doc/ble.md + doc/dfu.md, Nordic nRF5 SDK 11
`ble_dfu` service.
"""

from __future__ import annotations

import asyncio
import logging
import struct

from vitals.devices.bangle_dfu import DfuError, parse_dfu_package  # noqa: F401 — re-exported for callers

log = logging.getLogger(__name__)

# Legacy DFU service + characteristics (Nordic SDK ≤ 11; what
# InfiniTime's running firmware exposes).
DFU_SERVICE       = "00001530-1212-efde-1523-785feabcd123"
DFU_CONTROL_POINT = "00001531-1212-efde-1523-785feabcd123"  # write + notify
DFU_PACKET        = "00001532-1212-efde-1523-785feabcd123"  # write-no-response

# Control-Point opcodes.
OP_START_DFU        = 0x01
OP_INITIALIZE       = 0x02
OP_RECEIVE_IMAGE    = 0x03
OP_VALIDATE         = 0x04
OP_ACTIVATE_RESET   = 0x05
OP_PKT_RECEIPT_REQ  = 0x08
OP_RESPONSE         = 0x10
OP_PKT_RECEIPT      = 0x11
RESULT_SUCCESS      = 0x01

# Start-DFU image type: application only (no softdevice/bootloader).
MODE_APPLICATION = 0x04
# Initialize-DFU sub-commands.
INIT_RECEIVE  = 0x00
INIT_COMPLETE = 0x01

# Packet-characteristic write size (the legacy bootloader assumes
# unfragmented 20-byte writes) and how often we ask for a receipt.
PACKET_CHUNK = 20
RECEIPT_INTERVAL = 10


# ── Control-Point / Packet encoders (pure) ─────────────────────────

def cp_start_dfu() -> bytes:
    return bytes([OP_START_DFU, MODE_APPLICATION])


def start_sizes(app_size: int) -> bytes:
    """The Packet-characteristic payload that follows Start DFU:
    softdevice, bootloader and application image sizes (uint32 LE);
    we only ever flash an application."""
    return struct.pack("<III", 0, 0, app_size)


def cp_init_params(complete: bool) -> bytes:
    return bytes([OP_INITIALIZE, INIT_COMPLETE if complete else INIT_RECEIVE])


def cp_receive_image() -> bytes:
    return bytes([OP_RECEIVE_IMAGE])


def cp_validate() -> bytes:
    return bytes([OP_VALIDATE])


def cp_activate_and_reset() -> bytes:
    return bytes([OP_ACTIVATE_RESET])


def cp_receipt_interval(packets: int) -> bytes:
    return struct.pack("<BH", OP_PKT_RECEIPT_REQ, packets)


# ── notification parsers (pure) ────────────────────────────────────

def parse_response(data: bytes, expected_op: int) -> None:
    """Validate a Control-Point response (`0x10, request_op, result`).
    Raises `DfuError` on a malformed frame, a response to a different
    request, or a non-success result."""
    if len(data) < 3 or data[0] != OP_RESPONSE:
        raise DfuError(f"malformed DFU response: {data.hex()}")
    if data[1] != expected_op:
        raise DfuError(
            f"DFU response for op 0x{data[1]:02x}, expected 0x{expected_op:02x}")
    if data[2] != RESULT_SUCCESS:
        raise DfuError(f"DFU op 0x{expected_op:02x} failed (result {data[2]})")


def parse_receipt(data: bytes) -> int:
    """Bytes-received count from a packet-receipt notification
    (`0x11, uint32 LE`)."""
    if len(data) < 5 or data[0] != OP_PKT_RECEIPT:
        raise DfuError(f"malformed packet receipt: {data.hex()}")
    return struct.unpack_from("<I", data, 1)[0]


# ── transfer driver (bleak; fake-client tested) ────────────────────

async def run_legacy_dfu(client, init_packet: bytes, firmware: bytes,
                         on_progress=None) -> None:
    """Run legacy DFU over a connected bleak `client` (the watch's
    normal connection). Sends the init packet, streams the image with
    receipt-based flow control, validates, then triggers the reboot.
    `on_progress(stage, sent, total)` reports the firmware transfer."""
    notifications: asyncio.Queue = asyncio.Queue()

    def on_notify(_char, data: bytearray) -> None:
        notifications.put_nowait(bytes(data))

    async def next_notification(timeout: float = 30.0) -> bytes:
        return await asyncio.wait_for(notifications.get(), timeout)

    async def control(payload: bytes) -> None:
        await client.write_gatt_char(DFU_CONTROL_POINT, payload, response=True)

    async def stream(data: bytes) -> None:
        for i in range(0, len(data), PACKET_CHUNK):
            await client.write_gatt_char(
                DFU_PACKET, data[i:i + PACKET_CHUNK], response=False)

    await client.start_notify(DFU_CONTROL_POINT, on_notify)
    try:
        # Start DFU: opcode on the Control Point, sizes on Packet; the
        # response only arrives after the watch has seen both.
        await control(cp_start_dfu())
        await client.write_gatt_char(
            DFU_PACKET, start_sizes(len(firmware)), response=False)
        parse_response(await next_notification(), OP_START_DFU)

        # Init packet (the .dat): announce, stream, mark complete.
        await control(cp_init_params(complete=False))
        await stream(init_packet)
        await control(cp_init_params(complete=True))
        parse_response(await next_notification(), OP_INITIALIZE)

        # Firmware image, with a receipt every RECEIPT_INTERVAL packets
        # as flow control: wait for each and check the watch's count.
        await control(cp_receipt_interval(RECEIPT_INTERVAL))
        await control(cp_receive_image())
        total = len(firmware)
        sent = 0
        packets = 0
        if on_progress:
            on_progress("firmware", 0, total)
        for offset in range(0, total, PACKET_CHUNK):
            chunk = firmware[offset:offset + PACKET_CHUNK]
            await client.write_gatt_char(DFU_PACKET, chunk, response=False)
            sent += len(chunk)
            packets += 1
            if packets % RECEIPT_INTERVAL == 0:
                received = parse_receipt(await next_notification())
                if received != sent:
                    raise DfuError(
                        f"transfer out of sync: watch has {received} "
                        f"bytes, sent {sent}")
                if on_progress:
                    on_progress("firmware", sent, total)
        parse_response(await next_notification(), OP_RECEIVE_IMAGE)
        if on_progress:
            on_progress("firmware", total, total)

        await control(cp_validate())
        parse_response(await next_notification(), OP_VALIDATE)

        # Activate & reset reboots the watch mid-write, so the write
        # (and even the notify teardown) may error — that's success.
        try:
            await control(cp_activate_and_reset())
        except Exception:
            log.debug("PineTime DFU: activate write dropped (watch "
                      "rebooting)", exc_info=True)
        log.info("PineTime DFU: firmware transfer complete (%d bytes)", total)
    finally:
        try:
            await client.stop_notify(DFU_CONTROL_POINT)
        except Exception:
            pass
