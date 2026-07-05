"""Device detail — one device's status and capability-driven actions.

An ``Adw.NavigationPage`` pushed over the main view. The action rows
come from the plugin's capability flags, so a new device family gets
the right controls without touching this file.
"""

from __future__ import annotations

import logging
from datetime import datetime

from gi.repository import Adw, GLib, Gtk

log = logging.getLogger(__name__)


class DeviceDetailPage(Adw.NavigationPage):
    __gtype_name__ = "VitalsDeviceDetailPage"

    def __init__(self, manager, address: str):
        entry = manager.get(address)
        super().__init__(title=entry.name if entry else "Device")
        self._manager = manager
        self._address = address

        toolbar = Adw.ToolbarView()
        toolbar.add_top_bar(Adw.HeaderBar())
        self.set_child(toolbar)

        self._scroller = Gtk.ScrolledWindow(
            hscrollbar_policy=Gtk.PolicyType.NEVER, vexpand=True)
        toolbar.set_content(self._scroller)

        self._handlers = [
            manager.connect("device-state-changed", self._on_changed),
            manager.connect("device-synced", self._on_changed),
            manager.connect("device-list-changed", self._on_changed),
        ]
        self.connect("hidden", self._on_hidden)
        self._rebuild()

    # ── lifecycle ─────────────────────────────────────────────────
    def _on_hidden(self, *_):
        for handler in self._handlers:
            self._manager.disconnect(handler)
        self._handlers = []

    def _on_changed(self, *_args) -> None:
        if self._manager.get(self._address) is None:
            return  # forgotten; the page is being popped
        self._rebuild()

    # ── content ───────────────────────────────────────────────────
    def _rebuild(self) -> None:
        entry = self._manager.get(self._address)
        if entry is None:
            return
        plugin = entry.plugin

        clamp = Adw.Clamp(maximum_size=560, margin_top=18, margin_bottom=18,
                          margin_start=12, margin_end=12)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        clamp.set_child(box)

        status = Adw.PreferencesGroup(
            title=plugin.display_name if plugin else entry.kind,
            description=entry.address)
        status.add(_info_row("Battery",
                             f"{entry.last_battery}%"
                             if entry.last_battery is not None else "—"))
        status.add(_info_row("Last sync", _sync_text(entry)))
        if entry.state != "idle":
            status.add(_info_row("Status", entry.state.capitalize()))
        box.append(status)

        actions = Adw.PreferencesGroup()
        if entry.role == "watch":
            sync_row = Adw.ButtonRow(title="Sync Now")
            sync_row.set_start_icon_name("emblem-synchronizing-symbolic")
            sync_row.set_sensitive(not entry.busy)
            sync_row.connect("activated", self._on_sync)
            actions.add(sync_row)
        if plugin is not None and plugin.SUPPORTS_FIRMWARE_UPDATE:
            actions.add(_stub_row(self, "Firmware Update",
                                  "software-update-available-symbolic"))
        if plugin is not None and plugin.SUPPORTS_APP_INSTALL:
            actions.add(_stub_row(self, "App Store",
                                  "folder-download-symbolic"))
        if plugin is not None and plugin.SUPPORTS_ALARM_PUSH:
            actions.add(_stub_row(self, "Alarms", "alarm-symbolic"))
        box.append(actions)

        settings = Adw.PreferencesGroup()
        enabled = Adw.SwitchRow(
            title="Enabled",
            subtitle="Include this device in background syncs")
        enabled.set_active(entry.enabled)
        enabled.connect("notify::active",
                        lambda row, _p: self._manager.set_enabled(
                            self._address, row.get_active()))
        settings.add(enabled)
        box.append(settings)

        danger = Adw.PreferencesGroup()
        forget = Adw.ButtonRow(title="Forget This Device")
        forget.add_css_class("destructive-action")
        forget.connect("activated", self._on_forget)
        danger.add(forget)
        box.append(danger)

        self._scroller.set_child(clamp)

    # ── actions ───────────────────────────────────────────────────
    def _on_sync(self, *_):
        if not self._manager.sync_device(self._address):
            self._toast("Couldn’t start the sync")

    def _on_forget(self, *_):
        entry = self._manager.get(self._address)
        dialog = Adw.AlertDialog(
            heading="Forget this device?",
            body=f"{entry.name} will stop syncing. Its recorded health "
                 "data stays.")
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("forget", "Forget")
        dialog.set_response_appearance(
            "forget", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.connect("response", self._on_forget_response)
        dialog.present(self)

    def _on_forget_response(self, _dialog, response: str) -> None:
        if response != "forget":
            return
        root = self.get_root()
        self._manager.forget(self._address)
        if root is not None and hasattr(root, "pop_device_detail"):
            root.pop_device_detail()

    def _toast(self, message: str) -> None:
        self.activate_action("win.toast", GLib.Variant("s", message))


def _info_row(title: str, value: str) -> Adw.ActionRow:
    row = Adw.ActionRow(title=title)
    label = Gtk.Label(label=value)
    label.add_css_class("dim-label")
    row.add_suffix(label)
    return row


def _stub_row(page: DeviceDetailPage, title: str, icon: str) -> Adw.ButtonRow:
    # Placeholder rows for capabilities whose dialogs arrive with the
    # Pebble/Bangle port; showing them keeps the layout honest.
    row = Adw.ButtonRow(title=title)
    row.set_start_icon_name(icon)
    row.connect("activated",
                lambda *_: page._toast(f"{title} arrives with the "
                                       "Pebble/Bangle port"))
    return row


def _sync_text(entry) -> str:
    if not entry.last_sync_ms:
        return "Never"
    when = datetime.fromtimestamp(entry.last_sync_ms / 1000).astimezone()
    return when.strftime("%d %b %H:%M")
