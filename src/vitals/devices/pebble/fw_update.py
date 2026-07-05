"""Pebble firmware update — flash a normal firmware onto a watch in PRF.

A factory-fresh Core Devices Pebble boots into **PRF** (Pebble Recovery
Firmware) showing the QR screen; it has no normal firmware, so the time
and health endpoints report "unhandled". Onboarding means flashing the
normal firmware (and its resource pack) over the air, after which the
watch reboots into normal operation. This module is the host side of
that flash; it rides the PPoGATT link the rest of the Pebble path uses.

Everything here is transport-independent and (apart from the async
`FirmwareUpdater`) pure, so the wire format is unit-tested in isolation.
The byte layouts and the legacy CRC were validated against a btsnoop
capture of the official Core Devices app onboarding a real "obelix"
watch, and against the published v4.12.0 `.pbz` (the CRC reproduces the
manifest's `crc` field exactly). See docs/pebble-firmware-update.md.

Two endpoints carry the flash, both over the existing link:

  - **SystemMessage** (endpoint 0x12, little-endian): bookends the
    transfer — FirmwareStart (with the total byte count) and, at the
    end, FirmwareComplete which reboots the watch. Every message begins
    with a deprecated 0x00 byte, then the message type.
  - **PutBytes** (endpoint 0xBEEF, big-endian): the object transfer.
    Per object: Init (declares size + type) → the watch returns a
    *cookie* → Put (the bytes, chunked) → Commit (the legacy CRC over
    the whole object) → Install. The firmware object is sent first,
    then the system-resources object.

The watch ACKs each PutBytes message on 0xBEEF (`result, cookie`), so
each step is a request/response round-trip; the FirmwareStart is
answered with a FirmwareStartResponse status on 0x12.
"""

from __future__ import annotations

import asyncio
import json
import logging
import struct
import zipfile
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from io import BytesIO

log = logging.getLogger(__name__)


# ── Legacy (STM32 "defective") CRC ─────────────────────────────────
# PutBytesCommit carries this CRC over the whole object; the watch
# recomputes it (PebbleOS `PutBytesCrcType_Legacy`, a software emulation
# of the STM32 hardware CRC's quirks) and rejects a mismatch. It is the
# value the .pbz manifest stores as each object's `crc` — this
# implementation reproduces both the firmware and resources manifest
# CRCs exactly. Parameters: poly 0x04C11DB7, init 0xFFFFFFFF, no input
# or output reflection, no final XOR; data consumed as little-endian
# 32-bit words; a trailing partial word is left-padded with zeros and
# byte-reversed (the STM32 driver's quirk). Mirrors libpebble2
# `util/stm32_crc.py`.

_CRC_POLY = 0x04C11DB7


def _crc_word(word: bytes, crc: int) -> int:
    if len(word) < 4:
        padded = bytearray(word)
        for _ in range(4 - len(word)):
            padded.insert(0, 0)
        padded.reverse()
        word = bytes(padded)
    crc ^= int.from_bytes(word[:4], "little")
    for _ in range(32):
        if crc & 0x80000000:
            crc = ((crc << 1) ^ _CRC_POLY) & 0xFFFFFFFF
        else:
            crc = (crc << 1) & 0xFFFFFFFF
    return crc


def legacy_crc32(data: bytes, crc: int = 0xFFFFFFFF) -> int:
    """The Pebble legacy CRC over `data` (see module note)."""
    for i in range(0, len(data), 4):
        crc = _crc_word(data[i:i + 4], crc)
    return crc


# ── .pbz bundle ────────────────────────────────────────────────────

class FirmwareError(Exception):
    """A malformed bundle or a flash that the watch rejected."""


_MANIFEST_NAME  = "manifest.json"
_FIRMWARE_NAME  = "tintin_fw.bin"
_RESOURCES_NAME = "system_resources.pbpack"


@dataclass(frozen=True)
class FirmwareBundle:
    """A parsed `.pbz`: the firmware binary, the resource pack, and the
    manifest metadata. `firmware_crc` / `resources_crc` are the manifest
    CRCs, which equal `legacy_crc32` of the respective bytes."""

    firmware: bytes
    resources: bytes
    manifest: dict

    @property
    def firmware_crc(self) -> int:
        return int(self.manifest["firmware"]["crc"])

    @property
    def resources_crc(self) -> int:
        return int(self.manifest["resources"]["crc"])

    @property
    def version(self) -> str:
        return str(self.manifest.get("firmware", {}).get("versionTag", "?"))

    @property
    def hardware(self) -> str:
        """The hardware revision the firmware targets, e.g. obelix_pvt."""
        return str(self.manifest.get("firmware", {}).get("hwrev", "?"))

    @property
    def total_size(self) -> int:
        return len(self.firmware) + len(self.resources)


def parse_pbz(data: bytes) -> FirmwareBundle:
    """Parse `.pbz` bytes into a `FirmwareBundle`, verifying that the
    firmware and resource sizes and legacy CRCs match the manifest. A
    mismatch means a corrupt download — raise rather than flash it."""
    try:
        archive = zipfile.ZipFile(BytesIO(data))
        manifest = json.loads(archive.read(_MANIFEST_NAME))
        firmware = archive.read(_FIRMWARE_NAME)
        resources = archive.read(_RESOURCES_NAME)
    except (zipfile.BadZipFile, KeyError, ValueError) as exc:
        raise FirmwareError(f"not a valid .pbz bundle: {exc}") from exc

    bundle = FirmwareBundle(firmware=firmware, resources=resources,
                            manifest=manifest)
    _verify_object("firmware", firmware, manifest.get("firmware", {}),
                   bundle.firmware_crc)
    _verify_object("resources", resources, manifest.get("resources", {}),
                   bundle.resources_crc)
    log.info("Pebble fw: parsed %s for %s (firmware %d B, resources %d B)",
             bundle.version, bundle.hardware,
             len(firmware), len(resources))
    return bundle


def _verify_object(label: str, payload: bytes, meta: dict,
                   manifest_crc: int) -> None:
    size = meta.get("size")
    if size is not None and size != len(payload):
        raise FirmwareError(
            f"{label} size {len(payload)} != manifest {size}")
    actual = legacy_crc32(payload)
    if actual != (manifest_crc & 0xFFFFFFFF):
        raise FirmwareError(
            f"{label} CRC 0x{actual:08x} != manifest 0x{manifest_crc:08x}")


# ── Wire protocol ──────────────────────────────────────────────────

EP_SYSTEM   = 0x12     # SystemMessage endpoint (little-endian payloads)
EP_PUTBYTES = 0xBEEF   # PutBytes endpoint (big-endian payloads)

# SystemMessage types (src/fw/kernel/system_message.h). Every message is
# prefixed with a deprecated 0x00 byte.
SYS_FIRMWARE_START          = 0x01
SYS_FIRMWARE_COMPLETE       = 0x02
SYS_FIRMWARE_FAIL           = 0x03
SYS_FIRMWARE_START_RESPONSE = 0x0A

# FirmwareUpdateStatus returned in a start response (include/.../firmware_update.h).
FW_STATUS_STOPPED   = 0
FW_STATUS_RUNNING   = 1
FW_STATUS_CANCELLED = 2
FW_STATUS_FAILED    = 3

# PutBytes commands (low byte of the message).
PB_INIT    = 0x01
PB_PUT     = 0x02
PB_COMMIT  = 0x03
PB_ABORT   = 0x04
PB_INSTALL = 0x05

# PutBytes object types (include/.../put_bytes.h).
OBJ_FIRMWARE     = 0x01
OBJ_RECOVERY     = 0x02
OBJ_SYSRESOURCES = 0x03
OBJ_APP_RESOURCES = 0x04  # an app's resource pack
OBJ_APP_BINARY    = 0x05  # an app's executable
OBJ_WORKER        = 0x07  # an app's background worker
# Set on the object-type byte of an app-install Init to signal the
# alternate "cookie" form (app id in place of bank + filename).
PB_HAS_COOKIE = 0x80

# PutBytesResponse result.
PB_ACK  = 0x01
PB_NACK = 0x02


def encode_firmware_start(total_bytes: int, already: int = 0) -> bytes:
    """SystemMessage FirmwareStart with the smooth-progress total (the
    watch shows a progress bar). `0x00` deprecated byte + type + two
    little-endian uint32s (already-transferred, total)."""
    return struct.pack("<BBII", 0x00, SYS_FIRMWARE_START, already,
                       total_bytes)


def encode_firmware_complete() -> bytes:
    """SystemMessage FirmwareComplete — the watch reboots into the new
    firmware a few seconds later."""
    return bytes([0x00, SYS_FIRMWARE_COMPLETE])


def encode_firmware_fail() -> bytes:
    """SystemMessage FirmwareFail — abort the update on the watch."""
    return bytes([0x00, SYS_FIRMWARE_FAIL])


def parse_system_message(payload: bytes) -> tuple[int, bytes]:
    """Return (type, body) of a SystemMessage; body is whatever follows
    the deprecated byte + type (e.g. the status byte of a start
    response)."""
    if len(payload) < 2:
        raise FirmwareError("short system message")
    return payload[1], payload[2:]


def encode_putbytes_init(object_size: int, object_type: int,
                         bank: int = 0, cookie: int | None = None) -> bytes:
    """PutBytes Init. Big-endian size, then the object type.

    Two forms: firmware/system objects carry a bank byte and no filename
    (seven bytes total, matching the official app). App-install objects
    use the "cookie" form (`cookie` set) — the object type's high bit is
    set and the app id follows as a big-endian uint32 in place of the
    bank + filename."""
    if cookie is not None:
        return struct.pack(">BIBI", PB_INIT, object_size,
                           object_type | PB_HAS_COOKIE, cookie & 0xFFFFFFFF)
    return struct.pack(">BIBB", PB_INIT, object_size, object_type, bank)


def encode_putbytes_put(cookie: int, chunk: bytes) -> bytes:
    """PutBytes Put: cookie + payload length (both big-endian) + bytes."""
    return struct.pack(">BII", PB_PUT, cookie, len(chunk)) + chunk


def encode_putbytes_commit(cookie: int, object_crc: int) -> bytes:
    """PutBytes Commit: cookie + the legacy CRC of the whole object."""
    return struct.pack(">BII", PB_COMMIT, cookie, object_crc & 0xFFFFFFFF)


def encode_putbytes_install(cookie: int) -> bytes:
    """PutBytes Install: mark the transferred object as ready."""
    return struct.pack(">BI", PB_INSTALL, cookie)


def encode_putbytes_abort(cookie: int) -> bytes:
    """PutBytes Abort: discard a partially-transferred object."""
    return struct.pack(">BI", PB_ABORT, cookie)


def parse_putbytes_response(payload: bytes) -> tuple[int, int]:
    """Return (result, cookie) of a PutBytes response. The Init
    response's cookie is reused by Put/Commit/Install."""
    if len(payload) < 5:
        raise FirmwareError("short PutBytes response")
    result, cookie = struct.unpack_from(">BI", payload, 0)
    return result, cookie


# ── Orchestration ──────────────────────────────────────────────────

# `send(endpoint, payload)` ships one Pebble Protocol message (the
# transport chunks + paces it); inbound replies are delivered to the
# updater's `handle_message`.
SendFn = Callable[[int, bytes], Awaitable[None]]
# `on_progress(stage, sent, total)` — stage is "firmware" or "resources".
ProgressFn = Callable[[str, int, int], None]

# The watch should answer each round-trip well within this; a stall
# almost always means it dropped the link.
RESPONSE_TIMEOUT = 30.0
# Bytes per PutBytes Put. The transport splits this across link packets;
# the caller sizes it so one Put fits the link's send window.
DEFAULT_CHUNK = 1024
# Seconds to let the FirmwareComplete notification reach the watch before
# returning (the caller disconnects right after, and this is the message
# that reboots the watch into the new firmware).
COMPLETE_SETTLE = 1.0


class FirmwareUpdater:
    """Drives a PRF→normal firmware flash over a Pebble link.

    `send` ships a Pebble Protocol message; the owning transport must
    route inbound SystemMessage (0x12) and PutBytes (0xBEEF) replies to
    `handle_message`. `flash(bundle)` runs the whole sequence and calls
    `on_progress` as bytes go out. All protocol bytes come from the pure
    encoders above, so this class is only the state machine.
    """

    def __init__(self, send: SendFn, *, on_progress: ProgressFn | None = None,
                 chunk_size: int = DEFAULT_CHUNK):
        self._send = send
        self._on_progress = on_progress
        self._chunk_size = max(1, chunk_size)
        self._pb_response: asyncio.Future | None = None
        self._sys_response: asyncio.Future | None = None

    # ── inbound (called by the transport) ──────────────────────────

    def handle_message(self, endpoint: int, payload: bytes) -> None:
        """Route a watch reply to whoever is awaiting it."""
        if endpoint == EP_PUTBYTES:
            self._resolve(self._pb_response, payload)
        elif endpoint == EP_SYSTEM:
            self._resolve(self._sys_response, payload)

    @staticmethod
    def _resolve(future: asyncio.Future | None, payload: bytes) -> None:
        if future is not None and not future.done():
            future.set_result(payload)

    # ── the flash ──────────────────────────────────────────────────

    async def flash(self, bundle: FirmwareBundle) -> None:
        """Flash `bundle` onto the watch: FirmwareStart, the firmware
        and resource objects, then FirmwareComplete. On any failure the
        watch is told to abort (best effort) before the error
        propagates; the watch stays in PRF, safe to retry."""
        try:
            await self._start(bundle.total_size)
            await self._put_object("firmware", bundle.firmware,
                                   OBJ_FIRMWARE, bundle.firmware_crc)
            await self._put_object("resources", bundle.resources,
                                   OBJ_SYSRESOURCES, bundle.resources_crc)
            await self._complete()
        except Exception:
            await self._abort_quietly()
            raise

    async def _start(self, total: int) -> None:
        log.info("Pebble fw: FirmwareStart (%d bytes total)", total)
        self._sys_response = asyncio.get_running_loop().create_future()
        await self._send(EP_SYSTEM, encode_firmware_start(total))
        payload = await self._await(self._sys_response, "FirmwareStart")
        msg_type, body = parse_system_message(payload)
        if msg_type != SYS_FIRMWARE_START_RESPONSE:
            raise FirmwareError(
                f"unexpected system message 0x{msg_type:02x} after start")
        status = body[0] if body else FW_STATUS_RUNNING
        if status in (FW_STATUS_CANCELLED, FW_STATUS_FAILED):
            raise FirmwareError(f"watch declined firmware update (status {status})")
        log.info("Pebble fw: watch ready (status %d)", status)

    async def _put_object(self, stage: str, data: bytes, object_type: int,
                          object_crc: int) -> None:
        cookie = await self._init_object(len(data), object_type)
        log.info("Pebble fw: sending %s object (%d bytes, cookie 0x%08x)",
                 stage, len(data), cookie)
        sent = 0
        total = len(data)
        self._report(stage, 0, total)
        for offset in range(0, total, self._chunk_size):
            chunk = data[offset:offset + self._chunk_size]
            await self._request(encode_putbytes_put(cookie, chunk),
                                 f"{stage} put @{offset}")
            sent += len(chunk)
            self._report(stage, sent, total)
        await self._request(encode_putbytes_commit(cookie, object_crc),
                            f"{stage} commit")
        await self._request(encode_putbytes_install(cookie),
                            f"{stage} install")
        log.info("Pebble fw: %s object committed + installed", stage)

    async def _init_object(self, size: int, object_type: int) -> int:
        # The Init response (already checked for ACK by _request) carries
        # the cookie that Put/Commit/Install reuse for this object.
        payload = await self._request(
            encode_putbytes_init(size, object_type), "init")
        _result, cookie = parse_putbytes_response(payload)
        return cookie

    async def _request(self, message: bytes, what: str) -> bytes:
        """Send one PutBytes message and await the watch's ACK on
        0xBEEF, raising on a NACK."""
        self._pb_response = asyncio.get_running_loop().create_future()
        await self._send(EP_PUTBYTES, message)
        payload = await self._await(self._pb_response, what)
        result, _cookie = parse_putbytes_response(payload)
        if result != PB_ACK:
            raise FirmwareError(f"watch NACK'd {what} (result {result})")
        return payload

    async def _complete(self) -> None:
        log.info("Pebble fw: FirmwareComplete — watch will reboot")
        await self._send(EP_SYSTEM, encode_firmware_complete())
        # Give the notification time to reach the watch before the caller
        # tears the link down — this message is what triggers the reboot.
        await asyncio.sleep(COMPLETE_SETTLE)

    async def _abort_quietly(self) -> None:
        try:
            await self._send(EP_SYSTEM, encode_firmware_fail())
        except Exception:
            log.debug("Pebble fw: abort send failed", exc_info=True)

    @staticmethod
    async def _await(future: asyncio.Future, what: str) -> bytes:
        try:
            return await asyncio.wait_for(future, RESPONSE_TIMEOUT)
        except asyncio.TimeoutError as exc:
            raise FirmwareError(f"timed out waiting for {what} reply") from exc

    def _report(self, stage: str, sent: int, total: int) -> None:
        if self._on_progress is not None:
            try:
                self._on_progress(stage, sent, total)
            except Exception:
                log.debug("Pebble fw: progress callback raised", exc_info=True)
