"""Tests for the Bangle.js Nordic Secure DFU protocol (pure parts).

Package parsing, the Control-Point command encoders + response parsers,
and the CRC32 the bootloader checksums with. The BLE transfer is
on-device.
"""

import io
import json
import struct
import zipfile
import zlib

import pytest

from vitals.devices import bangle_dfu as dfu


def _package(dat=b"INITDATA", bin_=b"FIRMWARE-IMAGE") -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("manifest.json", json.dumps({"manifest": {"application": {
            "dat_file": "app.dat", "bin_file": "app.bin"}}}))
        z.writestr("app.dat", dat)
        z.writestr("app.bin", bin_)
    return buf.getvalue()


def test_parse_dfu_package():
    init, fw = dfu.parse_dfu_package(_package(b"DAT", b"BIN"))
    assert init == b"DAT"
    assert fw == b"BIN"


def test_parse_dfu_package_rejects_non_zip():
    with pytest.raises(dfu.DfuError):
        dfu.parse_dfu_package(b"not a zip")


def test_parse_dfu_package_rejects_missing_application():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("manifest.json", json.dumps({"manifest": {}}))
    with pytest.raises(dfu.DfuError):
        dfu.parse_dfu_package(buf.getvalue())


def test_control_point_encoders():
    assert dfu.cp_create(dfu.OBJ_DATA, 0x1234) == \
        struct.pack("<BBI", 0x01, 0x02, 0x1234)
    assert dfu.cp_set_prn(0) == struct.pack("<BH", 0x02, 0)
    assert dfu.cp_select(dfu.OBJ_COMMAND) == bytes([0x06, 0x01])
    assert dfu.cp_calc_checksum() == bytes([0x03])
    assert dfu.cp_execute() == bytes([0x04])


def test_parse_response_success_returns_payload():
    resp = bytes([dfu.OP_RESPONSE, dfu.OP_SELECT, dfu.RESULT_SUCCESS]) + b"\x01"
    assert dfu.parse_response(resp, dfu.OP_SELECT) == b"\x01"


def test_parse_response_rejects_failure():
    resp = bytes([dfu.OP_RESPONSE, dfu.OP_EXECUTE, 0x0A])  # an error result
    with pytest.raises(dfu.DfuError):
        dfu.parse_response(resp, dfu.OP_EXECUTE)


def test_parse_response_rejects_wrong_op():
    resp = bytes([dfu.OP_RESPONSE, dfu.OP_CREATE, dfu.RESULT_SUCCESS])
    with pytest.raises(dfu.DfuError):
        dfu.parse_response(resp, dfu.OP_SELECT)


def test_parse_select_and_checksum():
    sel = struct.pack("<III", 4096, 0, 0)
    assert dfu.parse_select(sel) == (4096, 0, 0)
    cks = struct.pack("<II", 256, 0xDEADBEEF)
    assert dfu.parse_checksum(cks) == (256, 0xDEADBEEF)


def test_crc32_matches_zlib():
    data = bytes(range(256))
    assert dfu.crc32(data) == zlib.crc32(data) & 0xFFFFFFFF
    # Rolling: seeding continues the checksum.
    assert dfu.crc32(b"BC", dfu.crc32(b"A")) == zlib.crc32(b"ABC") & 0xFFFFFFFF
