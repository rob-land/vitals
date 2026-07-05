"""Bangle.js app/watchface store — the Espruino "App Loader" catalogue.

Lists apps from banglejs.com/apps and packages a chosen app's storage
files into a bundle the Bangle plugin installs over the Espruino REPL
(the same Nordic-UART transport it already uses). This mirrors what the
web App Loader does; the wire protocol is plain `require("Storage")`
writes, nothing browser-specific.

A bundle is JSON: the app's identity plus its storage files (each file's
bytes base64-encoded, or, for the loader's `evaluate` files, the raw JS
expression to run on the watch). The plugin also writes a generated
`<id>.info` file so the launcher lists the app.

Apps whose install needs the loader's in-browser customiser
(`custom.html`) can't be installed headlessly and are skipped.
"""

from __future__ import annotations

import asyncio
import json
import logging
import urllib.request

from vitals.devices.store import AppStore, StoreApp

log = logging.getLogger(__name__)

CATALOG_URL = "https://banglejs.com/apps/apps.json"
APP_FILE_BASE = "https://raw.githubusercontent.com/espruino/BangleApps/master/apps"
DEVICE = "BANGLEJS2"
_TIMEOUT = 30.0

# Store "kind" → the Bangle app `type`s that belong under it.
_KIND_TYPES = {
    "watchface": {"clock"},
    "watchapp": {"app", ""},   # absent type defaults to "app"
}


class BangleStore(AppStore):
    display_name = "Bangle.js App Loader"

    async def list_apps(self, kind: str = "watchface", query: str = "",
                        limit: int = 30) -> list[StoreApp]:
        catalog = await asyncio.to_thread(_get_json, CATALOG_URL)
        wanted = _KIND_TYPES.get(kind, {"app", ""})
        q = query.lower()
        out: list[StoreApp] = []
        for rec in catalog:
            if not _supports_bangle2(rec):
                continue
            if (rec.get("type") or "app") not in wanted:
                continue
            if not is_installable(rec):
                continue
            if q and q not in rec.get("name", "").lower() \
                    and q not in rec.get("description", "").lower():
                continue
            out.append(_to_store_app(rec, kind))
            if len(out) >= limit:
                break
        return out

    async def download(self, app: StoreApp) -> bytes:
        """Fetch the app's storage files and package an install bundle.

        Each file is pulled from the BangleApps repo; `evaluate` files
        keep their JS expression verbatim, others are base64-encoded."""
        return await asyncio.to_thread(_build_bundle, app.raw)


# ── catalogue helpers (pure) ───────────────────────────────────────

def _supports_bangle2(rec: dict) -> bool:
    supports = rec.get("supports")
    # Older entries with no `supports` were Bangle.js 1 only.
    return isinstance(supports, list) and DEVICE in supports


def is_installable(rec: dict) -> bool:
    """Apps needing the loader's in-browser customiser can't be installed
    headlessly — skip them."""
    if rec.get("custom"):
        return False
    return bool(rec.get("storage"))


def _to_store_app(rec: dict, kind: str) -> StoreApp:
    app_id = rec.get("id", "")
    icon = rec.get("icon")
    return StoreApp(
        id=app_id,
        name=rec.get("name") or app_id,
        kind=kind,
        author=rec.get("author", ""),
        description=rec.get("description", ""),
        version=str(rec.get("version", "")),
        icon_url=f"{APP_FILE_BASE}/{app_id}/{icon}" if icon else None,
        screenshot_url=None,
        download_url="",   # Bangle assembles a bundle rather than one URL
        raw=rec,
    )


def build_app_info(rec: dict, file_names: list[str]) -> dict:
    """The `<id>.info` launcher-registration file the loader generates.
    `src` is the app's entry `.app.js`, `icon` its `.img`, and `files`
    lists everything written (info first) so uninstall can clean up."""
    app_id = rec.get("id", "")
    info: dict = {
        "id": app_id,
        "name": rec.get("shortName") or rec.get("name") or app_id,
        "version": str(rec.get("version", "")),
        "files": ",".join([f"{app_id}.info"] + file_names),
    }
    app_type = rec.get("type")
    if app_type and app_type != "app":
        info["type"] = app_type
    src = next((n for n in file_names if n.endswith(".app.js")), None)
    if src:
        info["src"] = src
    icon = next((n for n in file_names if n.endswith(".img")), None)
    if icon:
        info["icon"] = icon
    if rec.get("tags"):
        info["tags"] = rec["tags"]
    return info


# ── bundle building (network) ──────────────────────────────────────

def _build_bundle(rec: dict) -> bytes:
    app_id = rec.get("id", "")
    files: list[dict] = []
    names: list[str] = []
    for entry in rec.get("storage", []):
        name = entry.get("name")
        if not name:
            continue
        names.append(name)
        if "content" in entry:
            raw = entry["content"].encode("utf-8")
        else:
            raw = _download(f"{APP_FILE_BASE}/{app_id}/{entry['url']}")
        if entry.get("evaluate"):
            # The loader stores the *value* of this JS expression.
            files.append({"name": name, "evaluate": True,
                          "expr": raw.decode("utf-8", "replace")})
        else:
            files.append({"name": name, "b64": _b64(raw), "evaluate": False})

    info = build_app_info(rec, names)
    files.append({"name": f"{app_id}.info",
                  "b64": _b64(json.dumps(info).encode("utf-8")),
                  "evaluate": False})
    bundle = {"id": app_id, "name": rec.get("name", app_id),
              "type": rec.get("type", "app"), "files": files}
    return json.dumps(bundle).encode("utf-8")


def _b64(data: bytes) -> str:
    import base64
    return base64.b64encode(data).decode("ascii")


def _get_json(url: str):
    log.info("Bangle store: GET %s", url)
    req = urllib.request.Request(url, headers={"User-Agent": "vitals"})
    with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
        return json.load(resp)


def _download(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "vitals"})
    with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
        return resp.read()
