"""Tests for the Bangle.js app store + install (pure parts).

The catalogue filtering, the `.info` generation, and the install bundle
building are pinned here; the network fetch and the REPL upload are
exercised on-device.
"""

import base64
import json

from vitals.devices import bangle_store as bs
from vitals.devices.bangle import BangleDevice


# ── catalogue filtering ────────────────────────────────────────────

def test_supports_bangle2():
    assert bs._supports_bangle2({"supports": ["BANGLEJS2"]}) is True
    assert bs._supports_bangle2({"supports": ["BANGLEJS"]}) is False
    assert bs._supports_bangle2({}) is False


def test_is_installable_skips_custom_and_storageless():
    assert bs.is_installable({"storage": [{"name": "a"}]}) is True
    assert bs.is_installable(
        {"storage": [{"name": "a"}], "custom": "c.html"}) is False
    assert bs.is_installable({}) is False


# ── .info generation ───────────────────────────────────────────────

def test_build_app_info_clock():
    info = bs.build_app_info(
        {"id": "myface", "name": "My Face", "shortName": "MF",
         "version": "0.1", "type": "clock", "tags": "clock"},
        ["myface.app.js", "myface.img"])
    assert info["id"] == "myface"
    assert info["name"] == "MF"
    assert info["type"] == "clock"
    assert info["src"] == "myface.app.js"
    assert info["icon"] == "myface.img"
    assert info["files"] == "myface.info,myface.app.js,myface.img"


def test_build_app_info_plain_app_omits_type():
    info = bs.build_app_info({"id": "x", "name": "X", "version": "1"},
                             ["x.app.js"])
    assert "type" not in info
    assert info["files"] == "x.info,x.app.js"


# ── bundle building ────────────────────────────────────────────────

def test_build_bundle_encodes_files_and_appends_info():
    rec = {"id": "t", "name": "T", "version": "1", "type": "app",
           "storage": [
               {"name": "t.app.js", "content": "E.showMessage('hi')"},
               {"name": "t.img", "content": "require('hs')", "evaluate": True}]}
    bundle = json.loads(bs._build_bundle(rec).decode())
    by_name = {f["name"]: f for f in bundle["files"]}
    assert base64.b64decode(by_name["t.app.js"]["b64"]).decode() == \
        "E.showMessage('hi')"
    assert by_name["t.img"]["evaluate"] is True
    assert by_name["t.img"]["expr"] == "require('hs')"
    # The generated .info is written last so the launcher registers it.
    assert bundle["files"][-1]["name"] == "t.info"
    info = json.loads(base64.b64decode(by_name["t.info"]["b64"]))
    assert info["id"] == "t"


# ── install upload over a mocked REPL ──────────────────────────────

def test_install_app_writes_each_file(monkeypatch):
    import asyncio

    dev = BangleDevice("AA:BB:CC:DD:EE:FF")
    dev._client = object()  # pretend we're connected
    commands: list[str] = []

    async def fake_run(code, timeout=5.0):
        commands.append(code)
        return "\r\n>"  # clean prompt, no error

    monkeypatch.setattr(dev, "_run", fake_run)

    bundle = json.dumps({
        "id": "t", "name": "T", "files": [
            {"name": "t.app.js", "b64": base64.b64encode(b"x=1").decode(),
             "evaluate": False},
            {"name": "t.info", "b64": base64.b64encode(b"{}").decode(),
             "evaluate": False}]}).encode()
    asyncio.run(dev.install_app(bundle))

    joined = "\n".join(commands)
    # Each file is committed with a Storage.write(..., atob(__a)), and the
    # install reloads at the end.
    assert 'require("Storage").write("t.app.js",atob(__a))' in joined
    assert 'require("Storage").write("t.info",atob(__a))' in joined
    assert commands[-1] == "load()"


def test_install_app_evaluate_file_uses_eval(monkeypatch):
    import asyncio

    dev = BangleDevice("AA:BB")
    dev._client = object()
    commands: list[str] = []

    async def fake_run(code, timeout=5.0):
        commands.append(code)
        return "\r\n>"

    monkeypatch.setattr(dev, "_run", fake_run)
    bundle = json.dumps({"id": "t", "files": [
        {"name": "t.img", "evaluate": True, "expr": "require('hs')"}]}).encode()
    asyncio.run(dev.install_app(bundle))
    assert any('write("t.img",eval(__a))' in c for c in commands)
