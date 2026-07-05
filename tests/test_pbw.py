"""Tests for .pbw parsing (vitals.devices.pebble.pbw)."""

import io
import struct
import zipfile

import pytest

from vitals.devices.pebble import pbw


def _app_binary(uuid: bytes, *, flags=0x10, icon=7, app=(1, 2),
                sdk=(5, 95), name="Cooltest") -> bytes:
    b = bytearray(130)
    b[0:6] = b"PBLAPP"
    b[10], b[11] = sdk
    b[12], b[13] = app
    struct.pack_into("<I", b, 88, icon)
    struct.pack_into("<I", b, 96, flags)
    b[104:120] = uuid
    nm = name.encode()
    b[24:24 + len(nm)] = nm
    return bytes(b) + b"\xaa" * 64  # + dummy code


def _pbw(platforms: dict) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for plat, files in platforms.items():
            prefix = f"{plat}/" if plat else ""
            for fname, content in files.items():
                z.writestr(prefix + fname, content)
    return buf.getvalue()


UUID = bytes(range(16))


def test_parse_app_header_fields():
    hdr = pbw.parse_app_header(_app_binary(UUID))
    assert hdr["uuid"] == UUID
    assert hdr["flags"] == 0x10
    assert hdr["icon"] == 7
    assert hdr["app_version"] == (1, 2)
    assert hdr["sdk_version"] == (5, 95)
    assert hdr["name"] == "Cooltest"


def test_parse_app_header_rejects_non_pebble():
    with pytest.raises(pbw.PbwError):
        pbw.parse_app_header(b"\x00" * 130)


def test_parse_pbw_selects_emery():
    data = _pbw({
        "emery": {"pebble-app.bin": _app_binary(UUID, name="emery"),
                  "app_resources.pbpack": b"RES"},
        "aplite": {"pebble-app.bin": _app_binary(UUID, name="aplite")},
    })
    app = pbw.parse_pbw(data, platform="emery")
    assert app.platform == "emery"
    assert app.name == "emery"
    assert app.resources == b"RES"
    assert app.worker is None


def test_parse_pbw_falls_back_when_platform_absent():
    data = _pbw({"basalt": {"pebble-app.bin": _app_binary(UUID, name="b")}})
    app = pbw.parse_pbw(data, platform="emery")
    assert app.platform == "basalt"


def test_parse_pbw_legacy_root_layout():
    data = _pbw({"": {"pebble-app.bin": _app_binary(UUID),
                      "app_resources.pbpack": b"R"}})
    app = pbw.parse_pbw(data, platform="emery")
    assert app.platform == "default"
    assert app.resources == b"R"


def test_parse_pbw_includes_worker_when_present():
    data = _pbw({"emery": {"pebble-app.bin": _app_binary(UUID),
                           "pebble-worker.bin": b"WORK"}})
    app = pbw.parse_pbw(data, platform="emery")
    assert app.worker == b"WORK"


def test_parse_pbw_rejects_bundle_without_binary():
    data = _pbw({"emery": {"app_resources.pbpack": b"R"}})
    with pytest.raises(pbw.PbwError):
        pbw.parse_pbw(data)


def test_parse_pbw_rejects_non_zip():
    with pytest.raises(pbw.PbwError):
        pbw.parse_pbw(b"not a zip")
