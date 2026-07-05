"""Tests for the Pebble firmware-update protocol (vitals.devices.pebble.fw_update).

The wire bytes here are pinned to ground truth: every encoder is checked
against the exact bytes captured from the official Core Devices app
onboarding a real "obelix" watch, and the legacy CRC is checked both
against libpebble2's reference algorithm (transcribed below) and the
real v4.12.0 manifest CRCs (firmware 0xb01cf2db, resources 0x169defb9),
which it was validated against when this was written. A wrong byte or a
wrong CRC means a rejected — or, worse, a corrupt — flash, so these are
deliberately exhaustive.
"""

import array
import asyncio
import io
import json
import random
import struct
import zipfile

import pytest

from vitals.devices.pebble import fw_update as fw


# ── Legacy CRC ─────────────────────────────────────────────────────

def _reference_crc(data: bytes) -> int:
    """libpebble2 util/stm32_crc.py, transcribed verbatim — the
    authority the watch's PutBytesCrcType_Legacy matches."""
    def process_word(d, crc=0xFFFFFFFF):
        if len(d) < 4:
            da = array.array("B", d)
            for _ in range(4 - len(d)):
                da.insert(0, 0)
            da.reverse()
            d = da.tobytes()
        x = array.array("I", d)[0]      # little-endian on the test host
        crc ^= x
        for _ in range(32):
            crc = (crc << 1) ^ fw._CRC_POLY if crc & 0x80000000 else crc << 1
        return crc & 0xFFFFFFFF

    word_count = len(data) // 4 + (1 if len(data) % 4 else 0)
    crc = 0xFFFFFFFF
    for i in range(word_count):
        crc = process_word(data[i * 4:(i + 1) * 4], crc)
    return crc


def test_legacy_crc_matches_libpebble2_reference():
    rng = random.Random(1234)
    # Cover every length-mod-4 class, including the padded partial tail.
    for length in [0, 1, 2, 3, 4, 5, 7, 8, 31, 256, 1000, 2003]:
        data = bytes(rng.randrange(256) for _ in range(length))
        assert fw.legacy_crc32(data) == _reference_crc(data), length


def test_legacy_crc_known_vectors():
    # Pinned values (cross-checked against the reference above). The empty
    # input returns the unmodified init value.
    assert fw.legacy_crc32(b"") == 0xFFFFFFFF
    assert fw.legacy_crc32(b"1234") == 0xC2091428
    assert fw.legacy_crc32(b"123456789") == 0xAFF19057
    assert fw.legacy_crc32(bytes(range(256))) == 0xB7EC66F4


# ── Encoders pinned to the official-app capture ────────────────────

def test_putbytes_init_matches_capture():
    # Firmware object: command, big-endian size, type, bank — no
    # filename (the app omits it; the message is exactly 7 bytes).
    assert fw.encode_putbytes_init(1943932, fw.OBJ_FIRMWARE) == \
        bytes.fromhex("01001da97c0100")
    assert fw.encode_putbytes_init(730210, fw.OBJ_SYSRESOURCES) == \
        bytes.fromhex("01000b24620300")


def test_putbytes_commit_install_match_capture():
    assert fw.encode_putbytes_commit(0x7405510A, 0x10D9F855) == \
        bytes.fromhex("037405510a10d9f855")
    assert fw.encode_putbytes_install(0x7405510A) == \
        bytes.fromhex("057405510a")


def test_putbytes_put_layout():
    put = fw.encode_putbytes_put(0x7405510A, b"\xaa\xbb\xcc")
    assert put == bytes.fromhex("02" "7405510a" "00000003") + b"\xaa\xbb\xcc"


def test_putbytes_response_round_trip():
    assert fw.parse_putbytes_response(bytes.fromhex("017405510a")) == \
        (fw.PB_ACK, 0x7405510A)
    assert fw.parse_putbytes_response(bytes.fromhex("027405510a")) == \
        (fw.PB_NACK, 0x7405510A)
    with pytest.raises(fw.FirmwareError):
        fw.parse_putbytes_response(b"\x01\x02")


def test_firmware_start_matches_capture():
    # The capture's smooth FirmwareStart: 00 01 + already(u32 LE)=0
    # + total(u32 LE)=0x0028cdde.
    assert fw.encode_firmware_start(0x0028CDDE) == \
        bytes.fromhex("000100000000decd2800")


def test_system_message_helpers():
    assert fw.encode_firmware_complete() == bytes([0x00, 0x02])
    assert fw.encode_firmware_fail() == bytes([0x00, 0x03])
    # Start response carries a status byte after the deprecated/type bytes.
    assert fw.parse_system_message(bytes.fromhex("000a01")) == \
        (fw.SYS_FIRMWARE_START_RESPONSE, b"\x01")
    with pytest.raises(fw.FirmwareError):
        fw.parse_system_message(b"\x00")


# ── .pbz parsing ───────────────────────────────────────────────────

def _make_pbz(firmware: bytes, resources: bytes, *,
              fw_crc=None, res_crc=None, fw_size=None, res_size=None) -> bytes:
    manifest = {
        "firmware": {
            "name": "tintin_fw.bin",
            "size": len(firmware) if fw_size is None else fw_size,
            "crc": fw.legacy_crc32(firmware) if fw_crc is None else fw_crc,
            "versionTag": "v4.12.0",
            "hwrev": "obelix_pvt",
        },
        "resources": {
            "name": "system_resources.pbpack",
            "size": len(resources) if res_size is None else res_size,
            "crc": fw.legacy_crc32(resources) if res_crc is None else res_crc,
        },
        "type": "firmware",
    }
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("manifest.json", json.dumps(manifest))
        z.writestr("tintin_fw.bin", firmware)
        z.writestr("system_resources.pbpack", resources)
    return buf.getvalue()


def test_parse_pbz_round_trip():
    firmware, resources = b"FIRMWARE-BYTES!!" * 4, b"RESOURCE-PACK" * 3
    bundle = fw.parse_pbz(_make_pbz(firmware, resources))
    assert bundle.firmware == firmware
    assert bundle.resources == resources
    assert bundle.version == "v4.12.0"
    assert bundle.hardware == "obelix_pvt"
    assert bundle.total_size == len(firmware) + len(resources)
    # The manifest CRC equals the legacy CRC of the bytes.
    assert bundle.firmware_crc == fw.legacy_crc32(firmware)
    assert bundle.resources_crc == fw.legacy_crc32(resources)


def test_parse_pbz_rejects_crc_mismatch():
    with pytest.raises(fw.FirmwareError, match="CRC"):
        fw.parse_pbz(_make_pbz(b"abcd", b"efgh", fw_crc=0xDEADBEEF))


def test_parse_pbz_rejects_size_mismatch():
    with pytest.raises(fw.FirmwareError, match="size"):
        fw.parse_pbz(_make_pbz(b"abcd", b"efgh", res_size=999))


def test_parse_pbz_rejects_non_zip():
    with pytest.raises(fw.FirmwareError):
        fw.parse_pbz(b"not a zip file")


# ── Orchestration over a fake transport ────────────────────────────

class _FakeWatch:
    """Records what the updater sends and replies the way the watch
    would: a start response, and an ACK (carrying the cookie) for every
    PutBytes message. Replies are posted via call_soon so they land
    after the updater starts awaiting."""

    def __init__(self, *, init_result=fw.PB_ACK, start_status=fw.FW_STATUS_RUNNING):
        self.sent: list[tuple[int, bytes]] = []
        self.updater: fw.FirmwareUpdater | None = None
        self.cookie = 0x11223344
        self._init_result = init_result
        self._start_status = start_status

    async def send(self, endpoint: int, payload: bytes) -> None:
        self.sent.append((endpoint, bytes(payload)))
        asyncio.get_running_loop().call_soon(self._reply, endpoint, bytes(payload))

    def _reply(self, endpoint: int, payload: bytes) -> None:
        if endpoint == fw.EP_SYSTEM:
            if payload[1] == fw.SYS_FIRMWARE_START:
                self.updater.handle_message(fw.EP_SYSTEM, bytes(
                    [0x00, fw.SYS_FIRMWARE_START_RESPONSE, self._start_status]))
            return
        cmd = payload[0]
        if cmd == fw.PB_INIT:
            self.updater.handle_message(
                fw.EP_PUTBYTES, struct.pack(">BI", self._init_result, self.cookie))
        else:
            cookie = struct.unpack_from(">I", payload, 1)[0]
            self.updater.handle_message(
                fw.EP_PUTBYTES, struct.pack(">BI", fw.PB_ACK, cookie))


def _flash(watch, firmware, resources, **kw):
    # Don't wait out the post-complete settle delay in tests.
    fw.COMPLETE_SETTLE = 0
    bundle = fw.parse_pbz(_make_pbz(firmware, resources))
    progress = []
    updater = fw.FirmwareUpdater(
        watch.send, on_progress=lambda *a: progress.append(a), **kw)
    watch.updater = updater
    asyncio.run(updater.flash(bundle))
    return updater, progress


def test_flash_sends_full_sequence_in_order():
    watch = _FakeWatch()
    firmware, resources = b"F" * 10, b"R" * 6
    _flash(watch, firmware, resources, chunk_size=4)

    kinds = []
    for endpoint, payload in watch.sent:
        if endpoint == fw.EP_SYSTEM:
            kinds.append(("sys", payload[1]))
        else:
            kinds.append(("pb", payload[0]))

    # Start, then the firmware object (init, 3 puts of 4/4/2, commit,
    # install), then the resources object (init, 2 puts, commit, install),
    # then complete.
    assert kinds == [
        ("sys", fw.SYS_FIRMWARE_START),
        ("pb", fw.PB_INIT),
        ("pb", fw.PB_PUT), ("pb", fw.PB_PUT), ("pb", fw.PB_PUT),
        ("pb", fw.PB_COMMIT), ("pb", fw.PB_INSTALL),
        ("pb", fw.PB_INIT),
        ("pb", fw.PB_PUT), ("pb", fw.PB_PUT),
        ("pb", fw.PB_COMMIT), ("pb", fw.PB_INSTALL),
        ("sys", fw.SYS_FIRMWARE_COMPLETE),
    ]


def test_flash_commit_carries_object_crc():
    watch = _FakeWatch()
    firmware, resources = b"firmware-payload", b"resource-payload"
    _flash(watch, firmware, resources, chunk_size=64)
    commits = [p for e, p in watch.sent
               if e == fw.EP_PUTBYTES and p[0] == fw.PB_COMMIT]
    fw_commit, res_commit = commits
    assert struct.unpack_from(">I", fw_commit, 5)[0] == fw.legacy_crc32(firmware)
    assert struct.unpack_from(">I", res_commit, 5)[0] == fw.legacy_crc32(resources)


def test_flash_reports_progress_to_completion():
    watch = _FakeWatch()
    firmware, resources = b"F" * 10, b"R" * 7
    _, progress = _flash(watch, firmware, resources, chunk_size=4)
    fw_prog = [sent for stage, sent, total in progress if stage == "firmware"]
    res_prog = [sent for stage, sent, total in progress if stage == "resources"]
    assert fw_prog[-1] == len(firmware)
    assert res_prog[-1] == len(resources)
    # Monotonic, ending at the object size.
    assert fw_prog == sorted(fw_prog)


def test_flash_aborts_and_raises_on_nack():
    watch = _FakeWatch(init_result=fw.PB_NACK)
    with pytest.raises(fw.FirmwareError):
        _flash(watch, b"F" * 8, b"R" * 4, chunk_size=4)
    # A NACK must trigger a best-effort FirmwareFail to the watch.
    assert (fw.EP_SYSTEM, fw.encode_firmware_fail()) in watch.sent


def test_flash_raises_when_watch_declines_start():
    watch = _FakeWatch(start_status=fw.FW_STATUS_FAILED)
    with pytest.raises(fw.FirmwareError, match="declined"):
        _flash(watch, b"F" * 8, b"R" * 4, chunk_size=4)
