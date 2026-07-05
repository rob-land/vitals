"""The main views and their shared helpers.

Pages are plain boxes over the in-process store; each implements
``refresh()`` and the window calls it when the page becomes visible or
when the RecordBus reports new records.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from gi.repository import GLib, Gtk


class Page(Gtk.Box):
    def __init__(self):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)

    def refresh(self) -> None:  # pragma: no cover - overridden
        raise NotImplementedError

    def _toast(self, message: str) -> None:
        self.activate_action("win.toast", GLib.Variant("s", message))


def local_tz_name() -> str:
    """IANA name of the local zone for the store's bucketed aggregates."""
    return GLib.TimeZone.new_local().get_identifier()


def local_day_start(days_back: int = 0) -> datetime:
    midnight = datetime.now().astimezone().replace(
        hour=0, minute=0, second=0, microsecond=0)
    return midnight - timedelta(days=days_back)


def to_ms(dt: datetime) -> int:
    return round(dt.timestamp() * 1000)
