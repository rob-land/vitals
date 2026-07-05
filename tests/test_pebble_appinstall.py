"""Tests for the Pebble app-install protocol (vitals.devices.pebble.pebble_appinstall).

The encoders are pinned to the libpebble2 byte layouts (BlobDB,
AppRunState, AppFetch — all little-endian; the app-form PutBytes Init is
big-endian), and the installer's full sequence is exercised over a fake
watch.
"""

import asyncio
import struct

import pytest

from vitals.devices.pebble import pebble_appinstall as ai
from vitals.devices.pebble.pbw import PebbleApp

UUID = bytes(range(16))


def _app(binary=b"BINARY-DATA", resources=b"RES", worker=None) -> PebbleApp:
    return PebbleApp(
        platform="emery", binary=binary, resources=resources, worker=worker,
        uuid=UUID, flags=0x30, icon=9, app_version=(1, 0),
        sdk_version=(5, 95), name="Tester")


# ── encoders ───────────────────────────────────────────────────────

def test_app_metadata_layout():
    meta = ai.encode_app_metadata(_app())
    assert len(meta) == 126
    assert meta[:16] == UUID
    assert struct.unpack_from("<I", meta, 16)[0] == 0x30   # flags
    assert struct.unpack_from("<I", meta, 20)[0] == 9      # icon
    assert tuple(meta[24:30]) == (1, 0, 5, 95, 0, 0)       # versions + face
    assert meta[30:].rstrip(b"\0") == b"Tester"


def test_blobdb_insert_layout():
    ins = ai.encode_blobdb_insert(0x1234, ai.BLOB_DB_APP, UUID, b"VALUE")
    assert ins[0] == ai.BLOB_INSERT
    assert struct.unpack_from("<H", ins, 1)[0] == 0x1234   # token LE
    assert ins[3] == ai.BLOB_DB_APP
    assert ins[4] == len(UUID)
    assert ins[5:5 + 16] == UUID
    assert struct.unpack_from("<H", ins, 21)[0] == 5       # value_size LE
    assert ins[23:] == b"VALUE"


def test_blob_response_parse():
    assert ai.parse_blob_response(struct.pack("<HB", 7, ai.BLOB_SUCCESS)) == \
        (7, ai.BLOB_SUCCESS)


def test_app_run_state_and_fetch_round_trip():
    start = ai.encode_app_run_state_start(UUID)
    assert start == bytes([ai.APP_RUN_STATE_START]) + UUID
    req = bytes([0x01]) + UUID + struct.pack("<i", 77)
    assert ai.parse_app_fetch_request(req) == (UUID, 77)
    assert ai.encode_app_fetch_response(ai.APP_FETCH_START) == b"\x01\x01"


# ── installer over a fake watch ────────────────────────────────────

class _FakeWatch:
    def __init__(self):
        self.sent: list[tuple[int, bytes]] = []
        self.installer: ai.AppInstaller | None = None
        self.cookie = 0xABCD

    async def send(self, endpoint: int, payload: bytes) -> None:
        self.sent.append((endpoint, bytes(payload)))
        asyncio.get_running_loop().call_soon(self._reply, endpoint, bytes(payload))

    def _reply(self, endpoint: int, payload: bytes) -> None:
        if endpoint == ai.EP_BLOBDB:
            token = struct.unpack_from("<H", payload, 1)[0]
            self.installer.handle_message(
                ai.EP_BLOBDB, struct.pack("<HB", token, ai.BLOB_SUCCESS))
        elif endpoint == ai.EP_APP_RUN_STATE:
            uuid = payload[1:17]
            self.installer.handle_message(
                ai.EP_APP_FETCH,
                bytes([0x01]) + uuid + struct.pack("<i", 55))
        elif endpoint == ai.EP_PUTBYTES:
            self.installer.handle_message(
                ai.EP_PUTBYTES, struct.pack(">BI", 0x01, self.cookie))
        # AppFetchResponse (us → watch) needs no reply.


def _install(app, **kw):
    watch = _FakeWatch()
    installer = ai.AppInstaller(watch.send, **kw)
    watch.installer = installer
    asyncio.run(installer.install(app))
    return watch


def test_install_runs_full_sequence():
    watch = _install(_app(resources=b"RES", worker=b"WORK"), chunk_size=4)
    eps = [ep for ep, _ in watch.sent]
    # BlobDB insert, AppRunState start, AppFetchResponse(start), then three
    # PutBytes objects (binary, resources, worker).
    assert eps[0] == ai.EP_BLOBDB
    assert eps[1] == ai.EP_APP_RUN_STATE
    assert eps[2] == ai.EP_APP_FETCH          # our start response
    putbytes = [p for ep, p in watch.sent if ep == ai.EP_PUTBYTES]
    inits = [p for p in putbytes if p[0] == 0x01]
    assert len(inits) == 3                     # binary, resources, worker
    # App-form init: object type has the cookie bit set.
    assert inits[0][5] & 0x80


def test_install_skips_absent_resources_and_worker():
    watch = _install(_app(resources=None, worker=None), chunk_size=4)
    inits = [p for ep, p in watch.sent
             if ep == ai.EP_PUTBYTES and p[0] == 0x01]
    assert len(inits) == 1                     # binary only


def test_install_raises_on_blobdb_failure():
    class _Reject(_FakeWatch):
        def _reply(self, endpoint, payload):
            if endpoint == ai.EP_BLOBDB:
                token = struct.unpack_from("<H", payload, 1)[0]
                self.installer.handle_message(
                    ai.EP_BLOBDB, struct.pack("<HB", token, 0x02))  # failure
    watch = _Reject()
    installer = ai.AppInstaller(watch.send)
    watch.installer = installer
    with pytest.raises(ai.AppInstallError):
        asyncio.run(installer.install(_app()))


def test_install_raises_on_uuid_mismatch():
    class _WrongUuid(_FakeWatch):
        def _reply(self, endpoint, payload):
            if endpoint == ai.EP_BLOBDB:
                token = struct.unpack_from("<H", payload, 1)[0]
                self.installer.handle_message(
                    ai.EP_BLOBDB, struct.pack("<HB", token, ai.BLOB_SUCCESS))
            elif endpoint == ai.EP_APP_RUN_STATE:
                self.installer.handle_message(
                    ai.EP_APP_FETCH,
                    bytes([0x01]) + bytes(16) + struct.pack("<i", 1))
    watch = _WrongUuid()
    installer = ai.AppInstaller(watch.send)
    watch.installer = installer
    with pytest.raises(ai.AppInstallError):
        asyncio.run(installer.install(_app()))
