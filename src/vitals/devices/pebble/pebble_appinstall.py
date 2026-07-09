"""Install a Pebble app/watchface over the PPoGATT link.

The modern (PebbleOS) install flow, per libpebble2's installer:

  1. **BlobDB** (endpoint 0xB1DB): insert the app's metadata into the
     App database, keyed by its UUID.
  2. **AppRunState** (0x34): tell the watch to launch the UUID. The watch
     replies — on **AppFetch** (0x1771) — with an `AppFetchRequest`
     asking us to send the app, carrying an `app_id` (the transfer id).
  3. **PutBytes** (0xBEEF): transfer the binary, then the resource pack,
     then the worker (if any), each tagged with `app_id` — the same
     Init→Put→Commit(legacy CRC)→Install transfer the firmware flasher
     uses, but with the app-install "cookie" form of Init.

BlobDB, AppRunState and AppFetch payloads are **little-endian**; PutBytes
is big-endian. Byte layouts are from libpebble2 (`protocol/blobdb.py`,
`protocol/apps.py`, `services/install.py`).
"""

from __future__ import annotations

import asyncio
import logging
import struct
from collections.abc import Awaitable, Callable

from vitals.devices.pebble.fw_update import (
    EP_PUTBYTES, OBJ_APP_BINARY, OBJ_APP_RESOURCES, OBJ_WORKER, PB_ACK,
    encode_putbytes_commit, encode_putbytes_init, encode_putbytes_install,
    encode_putbytes_put, legacy_crc32, parse_putbytes_response,
)
from vitals.devices.pebble.pbw import PebbleApp

log = logging.getLogger(__name__)

# Endpoints.
EP_BLOBDB        = 0xB1DB
EP_APP_RUN_STATE = 0x34
EP_APP_FETCH     = 0x1771

# BlobDB.
BLOB_INSERT   = 0x01
BLOB_DELETE   = 0x04
BLOB_CLEAR    = 0x05
BLOB_DB_PIN          = 0x01
BLOB_DB_APP          = 0x02
BLOB_DB_NOTIFICATION = 0x04
BLOB_DB_APPSETTINGS  = 0x09
BLOB_SUCCESS  = 0x01

# AppRunState / AppFetch.
APP_RUN_STATE_START   = 0x01
APP_FETCH_START       = 0x01   # AppFetchStatus.Start
APP_FETCH_INVALID_UUID = 0x03

RESPONSE_TIMEOUT = 30.0
DEFAULT_CHUNK = 1024

SendFn = Callable[[int, bytes], Awaitable[None]]
ProgressFn = Callable[[str, int, int], None]


class AppInstallError(Exception):
    """The watch rejected an app install step."""


# ── wire encoders / parsers (pure) ─────────────────────────────────

def encode_app_metadata(app: PebbleApp) -> bytes:
    """The 126-byte AppMetadata BlobDB value (little-endian): uuid,
    flags, icon, app + sdk versions, two app-face bytes (0), name(96)."""
    name = app.name.encode("utf-8")[:96]
    return (app.uuid
            + struct.pack("<II", app.flags & 0xFFFFFFFF, app.icon & 0xFFFFFFFF)
            + bytes([app.app_version[0], app.app_version[1],
                     app.sdk_version[0], app.sdk_version[1], 0, 0])
            + name.ljust(96, b"\0"))


def encode_blobdb_insert(token: int, database: int, key: bytes,
                         value: bytes) -> bytes:
    """A BlobDB Insert command (little-endian): command, token, db,
    key_size, key, value_size, value."""
    return (struct.pack("<BHB", BLOB_INSERT, token & 0xFFFF, database)
            + bytes([len(key)]) + key
            + struct.pack("<H", len(value)) + value)


def encode_blobdb_delete(token: int, database: int, key: bytes) -> bytes:
    """A BlobDB Delete command: command, token, db, key_size, key."""
    return (struct.pack("<BHB", BLOB_DELETE, token & 0xFFFF, database)
            + bytes([len(key)]) + key)


def encode_blobdb_clear(token: int, database: int) -> bytes:
    """A BlobDB Clear command: command, token, db."""
    return struct.pack("<BHB", BLOB_CLEAR, token & 0xFFFF, database)


def parse_blob_response(payload: bytes) -> tuple[int, int]:
    """(token, status) of a BlobDB response."""
    if len(payload) < 3:
        raise AppInstallError("short BlobDB response")
    token, status = struct.unpack_from("<HB", payload, 0)
    return token, status


def encode_app_run_state_start(uuid: bytes) -> bytes:
    """AppRunState 'start this UUID' (little-endian): command + uuid."""
    return bytes([APP_RUN_STATE_START]) + uuid


def parse_app_fetch_request(payload: bytes) -> tuple[bytes, int]:
    """(uuid, app_id) of an AppFetchRequest (command, uuid, int32 LE)."""
    if len(payload) < 21:
        raise AppInstallError("short AppFetch request")
    uuid = bytes(payload[1:17])
    app_id = struct.unpack_from("<i", payload, 17)[0]
    return uuid, app_id


def encode_app_fetch_response(status: int) -> bytes:
    """AppFetchResponse: command (0x01) + status."""
    return bytes([0x01, status])


# ── orchestration ──────────────────────────────────────────────────

class AppInstaller:
    """Drives an app install over a Pebble link.

    `send` ships one Pebble Protocol message (chunked + ACK-paced by the
    transport); the transport routes inbound replies to `handle_message`.
    `on_progress(stage, sent, total)` reports the PutBytes transfer.
    """

    def __init__(self, send: SendFn, *, on_progress: ProgressFn | None = None,
                 chunk_size: int = DEFAULT_CHUNK):
        self._send = send
        self._on_progress = on_progress
        self._chunk = max(1, chunk_size)
        self._futures: dict[int, asyncio.Future] = {}
        self._token = 0

    def handle_message(self, endpoint: int, payload: bytes) -> None:
        future = self._futures.get(endpoint)
        if future is not None and not future.done():
            future.set_result(payload)

    async def install(self, app: PebbleApp) -> None:
        await self._blobdb_insert(app)
        app_id = await self._begin_fetch(app)
        await self._put_object("binary", app.binary, OBJ_APP_BINARY, app_id)
        if app.resources:
            await self._put_object("resources", app.resources,
                                   OBJ_APP_RESOURCES, app_id)
        if app.worker:
            await self._put_object("worker", app.worker, OBJ_WORKER, app_id)
        log.info("Pebble: app %s installed", app.name)

    # ── steps ──────────────────────────────────────────────────────

    async def _blobdb_insert(self, app: PebbleApp) -> None:
        self._token = (self._token + 1) & 0xFFFF
        value = encode_app_metadata(app)
        payload = await self._exchange(
            EP_BLOBDB,
            encode_blobdb_insert(self._token, BLOB_DB_APP, app.uuid, value),
            EP_BLOBDB, "BlobDB insert")
        _token, status = parse_blob_response(payload)
        if status != BLOB_SUCCESS:
            raise AppInstallError(f"BlobDB insert failed (status {status})")
        log.info("Pebble: app metadata stored (%s)", app.name)

    async def _begin_fetch(self, app: PebbleApp) -> int:
        # AppRunState start → the watch asks us to fetch the app.
        payload = await self._exchange(
            EP_APP_RUN_STATE, encode_app_run_state_start(app.uuid),
            EP_APP_FETCH, "app fetch request")
        uuid, app_id = parse_app_fetch_request(payload)
        if uuid != app.uuid:
            await self._send(EP_APP_FETCH,
                             encode_app_fetch_response(APP_FETCH_INVALID_UUID))
            raise AppInstallError("watch requested a different app UUID")
        await self._send(EP_APP_FETCH,
                         encode_app_fetch_response(APP_FETCH_START))
        log.info("Pebble: watch ready to receive app (id %d)", app_id)
        return app_id

    async def _put_object(self, stage: str, data: bytes, object_type: int,
                          app_id: int) -> None:
        cookie = await self._init_object(len(data), object_type, app_id)
        total = len(data)
        sent = 0
        self._report(stage, 0, total)
        for offset in range(0, total, self._chunk):
            chunk = data[offset:offset + self._chunk]
            await self._putbytes(encode_putbytes_put(cookie, chunk),
                                 f"{stage} put")
            sent += len(chunk)
            self._report(stage, sent, total)
        await self._putbytes(encode_putbytes_commit(cookie, legacy_crc32(data)),
                             f"{stage} commit")
        await self._putbytes(encode_putbytes_install(cookie),
                             f"{stage} install")
        log.info("Pebble: app %s object committed", stage)

    async def _init_object(self, size: int, object_type: int,
                           app_id: int) -> int:
        payload = await self._putbytes(
            encode_putbytes_init(size, object_type, cookie=app_id), "init")
        _result, cookie = parse_putbytes_response(payload)
        return cookie

    async def _putbytes(self, message: bytes, what: str) -> bytes:
        payload = await self._exchange(EP_PUTBYTES, message, EP_PUTBYTES, what)
        result, _cookie = parse_putbytes_response(payload)
        if result != PB_ACK:
            raise AppInstallError(f"watch NACK'd {what} (result {result})")
        return payload

    async def _exchange(self, send_endpoint: int, payload: bytes,
                        reply_endpoint: int, what: str) -> bytes:
        loop = asyncio.get_running_loop()
        future = loop.create_future()
        self._futures[reply_endpoint] = future
        try:
            await self._send(send_endpoint, payload)
            return await asyncio.wait_for(future, RESPONSE_TIMEOUT)
        except asyncio.TimeoutError as exc:
            raise AppInstallError(f"timed out waiting for {what}") from exc
        finally:
            if self._futures.get(reply_endpoint) is future:
                del self._futures[reply_endpoint]

    def _report(self, stage: str, sent: int, total: int) -> None:
        if self._on_progress is not None:
            try:
                self._on_progress(stage, sent, total)
            except Exception:
                log.debug("Pebble: app progress callback raised", exc_info=True)
