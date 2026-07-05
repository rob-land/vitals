"""Devices page — every registered watch and sensor, plus pairing."""

from __future__ import annotations

import logging
from datetime import datetime

from gi.repository import Adw, Gtk

from vitals.pages import Page

log = logging.getLogger(__name__)

_STATE_LABELS = {"syncing": "Syncing…", "flashing": "Updating…",
                 "error": "Error"}


class Devices(Page):
    def __init__(self, manager, ble):
        super().__init__()
        self._manager = manager
        self._ble = ble

        self._scroller = Gtk.ScrolledWindow(
            hscrollbar_policy=Gtk.PolicyType.NEVER, vexpand=True)
        self.append(self._scroller)

        manager.connect("device-list-changed", lambda *_: self.refresh())
        manager.connect("device-state-changed", lambda *_: self.refresh())
        manager.connect("device-synced", lambda *_a: self.refresh())

    def refresh(self) -> None:
        entries = self._manager.list()
        if not entries:
            empty = Adw.StatusPage(
                icon_name="bluetooth-symbolic",
                title="No Devices",
                description="Pair a watch or health sensor to sync its "
                            "readings automatically.")
            button = Gtk.Button(label="Pair a Device…",
                                halign=Gtk.Align.CENTER)
            button.add_css_class("pill")
            button.add_css_class("suggested-action")
            button.connect("clicked", self._on_pair)
            empty.set_child(button)
            self._scroller.set_child(empty)
            return

        clamp = Adw.Clamp(maximum_size=560, margin_top=18, margin_bottom=18,
                          margin_start=12, margin_end=12)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        clamp.set_child(box)

        group = Adw.PreferencesGroup(title="Devices")
        for entry in entries:
            group.add(self._device_row(entry))
        box.append(group)

        pair = Gtk.Button(halign=Gtk.Align.CENTER)
        pair.set_child(Adw.ButtonContent(icon_name="list-add-symbolic",
                                         label="Pair a Device…"))
        pair.add_css_class("pill")
        pair.connect("clicked", self._on_pair)
        box.append(pair)

        self._scroller.set_child(clamp)

    def _device_row(self, entry) -> Adw.ActionRow:
        plugin = entry.plugin
        family = plugin.display_name if plugin else entry.kind
        row = Adw.ActionRow(activatable=True, title=entry.name,
                            subtitle=" · ".join(
                                p for p in (family, _last_sync(entry)) if p))
        icon = ("preferences-system-time-symbolic"
                if entry.role == "watch" else "network-cellular-symbolic")
        row.add_prefix(Gtk.Image.new_from_icon_name(icon))
        if entry.state in _STATE_LABELS:
            status = Gtk.Label(label=_STATE_LABELS[entry.state])
            status.add_css_class("dim-label")
            row.add_suffix(status)
        elif entry.last_battery is not None:
            battery = Gtk.Label(label=f"{entry.last_battery}%")
            battery.add_css_class("dim-label")
            battery.add_css_class("numeric")
            row.add_suffix(battery)
        if not entry.enabled:
            off = Gtk.Label(label="Off")
            off.add_css_class("dim-label")
            row.add_suffix(off)
        row.add_suffix(Gtk.Image.new_from_icon_name("go-next-symbolic"))
        row.connect("activated",
                    lambda *_: self.get_root().push_device_detail(entry.address))
        return row

    def _on_pair(self, *_):
        from vitals.dialogs.pairing import PairingDialog
        PairingDialog(self._ble, self._on_paired).present(self.get_root())

    def _on_paired(self, address: str, name: str, kind: str,
                   recovery: bool) -> None:
        entry = self._manager.add(address, name, kind)
        self._toast(f"Paired {entry.name}")
        if recovery:
            # A factory-fresh watch on recovery firmware (Pebble PRF) —
            # the firmware dialog arrives with the Pebble port; until
            # then just tell the user what we saw.
            self._toast(f"{entry.name} is in recovery mode — firmware "
                        "install lands with the Pebble port")


def _last_sync(entry) -> str:
    if not entry.last_sync_ms:
        return "Never synced"
    when = datetime.fromtimestamp(entry.last_sync_ms / 1000).astimezone()
    today = datetime.now().astimezone().date()
    if when.date() == today:
        return f"Synced {when.strftime('%H:%M')}"
    return f"Synced {when.strftime('%d %b')}"
