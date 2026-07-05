"""Watch app/watchface store abstractions.

A device family (Pebble, Bangle.js) exposes a store of installable apps
and watchfaces. `AppStore` is the catalogue side — list and download,
purely over the network, no watch connection. Installing the downloaded
bundle is the device's job (`Device.install_app`), since that rides the
watch's own transport.

The UI is family-agnostic: it asks the paired device's plugin for its
`app_store()`, lists `StoreApp`s, downloads the chosen one, and hands the
bytes to `device.install_app`.
"""

from __future__ import annotations

import abc
from dataclasses import dataclass, field


@dataclass(frozen=True)
class StoreApp:
    """One listing in a watch app store."""
    id: str
    name: str
    kind: str            # "watchface" | "watchapp"
    author: str = ""
    description: str = ""
    version: str = ""
    icon_url: str | None = None
    screenshot_url: str | None = None
    # Where the installable bundle is fetched from (a .pbw URL, etc.).
    download_url: str = ""
    # The original store record, for family-specific download logic.
    raw: dict = field(default_factory=dict)


class AppStore(abc.ABC):
    """A browsable catalogue of installable apps/watchfaces."""

    display_name: str = "Store"
    # Whether this store distinguishes watchapps from watchfaces.
    has_watchapps: bool = True

    @abc.abstractmethod
    async def list_apps(self, kind: str = "watchface", query: str = "",
                        limit: int = 30) -> list[StoreApp]:
        """List apps of `kind` ("watchface"/"watchapp"), optionally
        filtered by a search `query`. Network-bound; runs off the BLE
        loop."""

    @abc.abstractmethod
    async def download(self, app: StoreApp) -> bytes:
        """Fetch the installable bundle for `app` (the bytes that
        `Device.install_app` expects)."""
