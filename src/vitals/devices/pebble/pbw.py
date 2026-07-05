"""Parse a Pebble app bundle (`.pbw`) for installation.

A `.pbw` is a ZIP with one folder per supported platform (aplite,
basalt, chalk, diorite, emery, …); modern single-platform bundles keep
the files at the root. Each platform folder holds the compiled app
binary (`pebble-app.bin`), its resource pack (`app_resources.pbpack`),
and optionally a background worker (`pebble-worker.bin`).

We pick the folder for the watch's platform (a Core Devices "obelix" /
Pebble Time 2 is the **emery** platform), read those three objects, and
parse the app binary's header (`PebbleProcessInfo`) for the metadata the
install needs — the app UUID, flags, icon, and versions — which is the
same source of truth the watch uses. The legacy CRC each PutBytes commit
carries is computed from the object bytes (see `fw_update.legacy_crc32`),
so the bundle's manifest CRCs aren't needed.
"""

from __future__ import annotations

import struct
import zipfile
from dataclasses import dataclass
from io import BytesIO

# Platform folders in preference order; obelix == emery. If the watch's
# platform isn't in the bundle we fall back to the first one present
# (the firmware runs older platforms' apps in a bezel/scaled mode).
PLATFORM_PREFERENCE = ["emery", "gabbro", "chalk", "diorite", "basalt",
                       "aplite", "flint"]

_APP_BIN = "pebble-app.bin"
_RESOURCES = "app_resources.pbpack"
_WORKER = "pebble-worker.bin"

# PebbleProcessInfo header (little-endian) — offsets into pebble-app.bin.
_MAGIC = b"PBLAPP"
_OFF_SDK_VERSION   = 10   # u8 major, u8 minor
_OFF_APP_VERSION   = 12   # u8 major, u8 minor  (process_version)
_OFF_ICON          = 88   # u32
_OFF_FLAGS         = 96   # u32
_OFF_UUID          = 104  # 16 bytes
_OFF_NAME          = 24   # char[32]
_HEADER_MIN_LEN    = 120


class PbwError(Exception):
    """A malformed or unsupported `.pbw`."""


@dataclass(frozen=True)
class PebbleApp:
    """A parsed `.pbw`, ready to install onto the watch."""
    platform: str
    binary: bytes
    resources: bytes | None
    worker: bytes | None
    uuid: bytes               # 16 raw bytes
    flags: int
    icon: int
    app_version: tuple[int, int]
    sdk_version: tuple[int, int]
    name: str


def parse_pbw(data: bytes, platform: str = "emery") -> PebbleApp:
    """Parse `.pbw` bytes, selecting `platform`'s build (falling back to
    whatever the bundle ships). Raises `PbwError` if it can't."""
    try:
        archive = zipfile.ZipFile(BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise PbwError(f"not a valid .pbw: {exc}") from exc

    names = set(archive.namelist())
    chosen = _select_platform(names, platform)
    prefix = f"{chosen}/" if chosen else ""

    binary = _read(archive, prefix + _APP_BIN)
    if binary is None:
        raise PbwError("no pebble-app.bin in the bundle")
    resources = _read(archive, prefix + _RESOURCES)
    worker = _read(archive, prefix + _WORKER)

    header = parse_app_header(binary)
    return PebbleApp(platform=chosen or "default", binary=binary,
                     resources=resources, worker=worker, **header)


def parse_app_header(binary: bytes) -> dict:
    """Pull the install metadata out of a compiled Pebble app binary's
    `PebbleProcessInfo` header (little-endian)."""
    if len(binary) < _HEADER_MIN_LEN or binary[:6] != _MAGIC:
        raise PbwError("app binary missing PBLAPP header")
    sdk = (binary[_OFF_SDK_VERSION], binary[_OFF_SDK_VERSION + 1])
    app = (binary[_OFF_APP_VERSION], binary[_OFF_APP_VERSION + 1])
    icon = struct.unpack_from("<I", binary, _OFF_ICON)[0]
    flags = struct.unpack_from("<I", binary, _OFF_FLAGS)[0]
    uuid = bytes(binary[_OFF_UUID:_OFF_UUID + 16])
    name = (binary[_OFF_NAME:_OFF_NAME + 32]
            .split(b"\0", 1)[0].decode("utf-8", "replace"))
    return {"uuid": uuid, "flags": flags, "icon": icon,
            "app_version": app, "sdk_version": sdk, "name": name}


def _select_platform(names: set[str], preferred: str) -> str:
    """Return the platform folder to install (\"\" for a root/legacy
    single-platform bundle)."""
    order = [preferred] + [p for p in PLATFORM_PREFERENCE if p != preferred]
    for plat in order:
        if f"{plat}/{_APP_BIN}" in names:
            return plat
    if _APP_BIN in names:        # legacy single-platform bundle
        return ""
    raise PbwError("bundle has no installable app binary")


def _read(archive: zipfile.ZipFile, name: str) -> bytes | None:
    try:
        return archive.read(name)
    except KeyError:
        return None
