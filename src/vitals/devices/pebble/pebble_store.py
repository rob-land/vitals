"""Pebble app/watchface store — the Rebble / Core Devices catalogue.

Lists and downloads apps from the open Pebble app store API (the same
one the official Core Devices app uses). Browsing and `.pbw` download
need no auth. The watch's platform is **emery** (obelix / Pebble Time 2),
so listings are filtered to that hardware. Network I/O is blocking and
runs off the BLE loop via `asyncio.to_thread`.
"""

from __future__ import annotations

import asyncio
import json
import logging
import urllib.parse
import urllib.request

from vitals.devices.store import AppStore, StoreApp

log = logging.getLogger(__name__)

# Core Devices / rePebble store (apps.repebble.com). The community Rebble
# store (appstore-api.rebble.io) serves the same /api/v1 shape.
STORE_BASE = "https://appstore-api.repebble.com"
PLATFORM = "emery"
_KIND_PATH = {"watchface": "watchfaces", "watchapp": "apps"}
_TIMEOUT = 30.0


class PebbleStore(AppStore):
    display_name = "Rebble app store"

    async def list_apps(self, kind: str = "watchface", query: str = "",
                        limit: int = 30) -> list[StoreApp]:
        type_path = _KIND_PATH.get(kind, "watchfaces")
        params = {"hardware": PLATFORM, "limit": str(limit)}
        if query:
            # The collection endpoint has no search; the dedicated search
            # endpoint does. Fall back to filtering the collection client
            # side when searching to keep one code path simple.
            params["query"] = query
        url = (f"{STORE_BASE}/api/v1/apps/collection/all/{type_path}"
               f"?{urllib.parse.urlencode(params)}")
        data = await asyncio.to_thread(_get_json, url)
        apps = [_to_store_app(rec, kind) for rec in (data.get("data") or [])]
        if query:
            q = query.lower()
            apps = [a for a in apps
                    if q in a.name.lower() or q in a.author.lower()]
        return apps

    async def download(self, app: StoreApp) -> bytes:
        if not app.download_url:
            raise RuntimeError(f"{app.name} has no downloadable bundle")
        return await asyncio.to_thread(_download, app.download_url)


def _to_store_app(rec: dict, kind: str) -> StoreApp:
    platforms = rec.get("hardware_platforms") or []
    emery = next((p for p in platforms if p.get("name") == PLATFORM),
                 platforms[0] if platforms else {})
    images = emery.get("images") or {}
    release = rec.get("latest_release") or {}
    return StoreApp(
        id=str(rec.get("id", "")),
        name=rec.get("title") or rec.get("name") or "Untitled",
        kind=kind,
        author=rec.get("author", ""),
        description=emery.get("description") or rec.get("description", ""),
        version=str(release.get("version", "")),
        icon_url=images.get("icon") or None,
        screenshot_url=images.get("screenshot") or None,
        download_url=release.get("pbw_file", ""),
        raw=rec,
    )


def _get_json(url: str) -> dict:
    log.info("Pebble store: GET %s", url)
    req = urllib.request.Request(
        url, headers={"User-Agent": "vitals",
                      "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
        return json.load(resp)


def _download(url: str) -> bytes:
    log.info("Pebble store: downloading %s", url)
    req = urllib.request.Request(url, headers={"User-Agent": "vitals"})
    with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
        return resp.read()
