"""Devices page — placeholder until the device framework lands.

The multi-device registry, pairing and per-device detail pages arrive
with the device-framework phase; until then tock keeps handling the
watches.
"""

from __future__ import annotations

from gi.repository import Adw

from vitals.pages import Page


class Devices(Page):
    def __init__(self):
        super().__init__()
        self.append(Adw.StatusPage(
            icon_name="bluetooth-symbolic",
            title="No devices yet",
            description="Watch and sensor pairing moves here soon; "
                        "keep using Tock for your watch meanwhile.",
            vexpand=True))

    def refresh(self) -> None:
        pass
