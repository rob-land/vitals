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

    def __init__(self, manager, address: str, catalog=None):
        entry = manager.get(address)
        super().__init__(title=entry.name if entry else "Device")
        self._manager = manager
        self._address = address
        self._catalog = catalog  # for record-type titles in the trust UI

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
            sync_row = _action_row("Sync Now", "emblem-synchronizing-symbolic")
            sync_row.set_sensitive(not entry.busy)
            sync_row.connect("activated", self._on_sync)
            actions.add(sync_row)
        if plugin is not None and plugin.SUPPORTS_FIRMWARE_UPDATE:
            row = _action_row("Firmware Update",
                              "software-update-available-symbolic")
            row.connect("activated", self._on_firmware)
            actions.add(row)
        if plugin is not None and plugin.SUPPORTS_APP_INSTALL:
            row = _action_row("App and Watchface Store",
                              "folder-download-symbolic")
            row.connect("activated", self._on_store)
            actions.add(row)
        if plugin is not None and plugin.SUPPORTS_ALARM_PUSH:
            row = _action_row("Alarms", "alarm-symbolic")
            row.connect("activated", self._on_alarms)
            actions.add(row)
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

        if plugin is not None and plugin.SUPPORTS_NOTIFICATIONS:
            forward = Adw.SwitchRow(
                title="Forward notifications",
                subtitle="Keep the watch connected and mirror phone "
                         "notifications to it")
            forward.set_active(
                bool(entry.settings.get("forward_notifications")))
            forward.connect(
                "notify::active",
                lambda row, _p: self._manager.set_forward_notifications(
                    self._address, row.get_active()))
            settings.add(forward)

        if plugin is not None and plugin.SUPPORTS_MONITORING_CONFIG:
            self._add_monitoring_rows(settings, entry)
        box.append(settings)

        if plugin is not None and plugin.SUPPORTS_HYDRATION_CONFIG:
            self._add_hydration_group(box, entry)

        self._add_preference_group(box, entry)

        danger = Adw.PreferencesGroup()
        forget = Adw.ButtonRow(title="Forget This Device")
        forget.add_css_class("destructive-action")
        forget.connect("activated", self._on_forget)
        danger.add(forget)
        box.append(danger)

        self._scroller.set_child(clamp)

    # ── source preference (overlapping metrics) ───────────────────
    def _add_preference_group(self, box, entry) -> None:
        """When another source also reports some of this device's metrics,
        let the user pin which device wins for each. Persisted to the
        registry so cross-source resolution honours it."""
        contested = [m for m in self._manager.contested_metrics(self._address)
                     if self._is_scalar(m)]
        if not contested:
            return
        preferred = set(entry.settings.get("preferred_metrics", []))
        group = Adw.PreferencesGroup(
            title="Preferred source",
            description="Another device also records these — prefer this "
                        "one's readings when they overlap.")
        for metric in contested:
            row = Adw.SwitchRow(title=f"Prefer for {self._metric_title(metric)}")
            row.set_active(metric in preferred)
            row.connect(
                "notify::active",
                lambda r, _p, m=metric: self._on_preference_toggled(m, r))
            group.add(row)
        box.append(group)

    def _is_scalar(self, metric: str) -> bool:
        td = self._catalog.get(metric) if self._catalog else None
        return td is not None and td.value_shape == "scalar"

    def _metric_title(self, metric: str) -> str:
        td = self._catalog.get(metric) if self._catalog else None
        return (td.title if td else metric).lower()

    def _on_preference_toggled(self, metric: str, switch) -> None:
        entry = self._manager.get(self._address)
        if entry is None:
            return
        preferred = set(entry.settings.get("preferred_metrics", []))
        preferred.add(metric) if switch.get_active() else preferred.discard(metric)
        self._manager.update_settings(
            self._address, {"preferred_metrics": sorted(preferred)})
        self._toast("Applies to the dashboard")

    # ── hydration config (smart bottles) ──────────────────────────
    _REMINDER_INTERVALS = [30, 45, 60, 90, 120]

    def _add_hydration_group(self, box, entry) -> None:
        """Let the user set the bottle's drink-reminder window; pushed
        to the bottle on every sync, alongside the app-wide daily water
        goal. See Device.configure_hydration."""
        group = Adw.PreferencesGroup(
            title="Hydration",
            description="Applied on the next sync, along with your daily "
                        "water goal from Preferences.")

        remind = Adw.SwitchRow(
            title="Drink reminders",
            subtitle="Have the bottle nudge you to drink through the day")
        remind.set_active(
            bool(entry.settings.get("hydration_reminder_enabled", True)))
        group.add(remind)

        start = _hour_row("From (hour)",
                          int(entry.settings.get("hydration_reminder_start", 8)))
        group.add(start)

        end = _hour_row("Until (hour)",
                        int(entry.settings.get("hydration_reminder_end", 20)))
        group.add(end)

        interval = Adw.ComboRow(
            title="Reminder interval",
            model=Gtk.StringList.new(
                [f"{m} min" for m in self._REMINDER_INTERVALS]))
        current = int(entry.settings.get("hydration_reminder_interval", 60))
        interval.set_selected(self._REMINDER_INTERVALS.index(current)
                              if current in self._REMINDER_INTERVALS else 2)
        group.add(interval)

        for row in (start, end, interval):
            row.set_sensitive(remind.get_active())
        remind.connect(
            "notify::active",
            lambda row, _p: self._on_reminder_toggled(
                row, (start, end, interval)))
        start.connect(
            "notify::value",
            lambda row, _p: self._on_reminder_setting(
                "hydration_reminder_start", int(row.get_value())))
        end.connect(
            "notify::value",
            lambda row, _p: self._on_reminder_setting(
                "hydration_reminder_end", int(row.get_value())))
        interval.connect(
            "notify::selected",
            lambda row, _p: self._on_reminder_setting(
                "hydration_reminder_interval",
                self._REMINDER_INTERVALS[row.get_selected()]))
        box.append(group)

    def _on_reminder_toggled(self, switch, dependent_rows) -> None:
        active = switch.get_active()
        for row in dependent_rows:
            row.set_sensitive(active)
        self._on_reminder_setting("hydration_reminder_enabled", active)

    def _on_reminder_setting(self, key: str, value) -> None:
        self._manager.update_settings(self._address, {key: value})
        self._toast("Applies on the next sync")

    # ── monitoring config ─────────────────────────────────────────
    _MONITOR_INTERVALS = [10, 20, 30, 60]

    def _add_monitoring_rows(self, group, entry) -> None:
        """Let the user turn the device's own periodic monitoring on/off
        and set its interval — the device is configured from Vitals, no
        vendor app needed. Applied on the next sync."""
        monitor = Adw.SwitchRow(
            title="Automatic monitoring",
            subtitle="Have the device sample its sensors periodically "
                     "(heart rate, SpO₂, temperature)")
        monitor.set_active(bool(entry.settings.get("monitoring_enabled", True)))
        group.add(monitor)

        interval = Adw.ComboRow(
            title="Monitoring interval",
            model=Gtk.StringList.new(
                [f"{m} min" for m in self._MONITOR_INTERVALS]))
        current = int(entry.settings.get("monitoring_interval", 10))
        interval.set_selected(self._MONITOR_INTERVALS.index(current)
                              if current in self._MONITOR_INTERVALS else 0)
        interval.set_sensitive(monitor.get_active())
        group.add(interval)

        monitor.connect(
            "notify::active",
            lambda row, _p, ir=interval: self._on_monitoring_toggled(row, ir))
        interval.connect(
            "notify::selected",
            lambda row, _p: self._on_monitoring_interval(row.get_selected()))

    def _on_monitoring_toggled(self, switch, interval_row) -> None:
        active = switch.get_active()
        self._manager.update_settings(
            self._address, {"monitoring_enabled": active})
        interval_row.set_sensitive(active)
        self._toast("Monitoring updates on the next sync")

    def _on_monitoring_interval(self, index: int) -> None:
        minutes = self._MONITOR_INTERVALS[index]
        self._manager.update_settings(
            self._address, {"monitoring_interval": minutes})
        self._toast("Monitoring updates on the next sync")

    # ── actions ───────────────────────────────────────────────────
    def _on_sync(self, *_):
        if not self._manager.sync_device(self._address):
            self._toast("Couldn’t start the sync")

    def _ble(self):
        return self.get_root().get_application().ble

    def _on_firmware(self, *_):
        entry = self._manager.get(self._address)
        if entry.plugin.FIRMWARE_REQUIRES_DFU_MODE:
            from vitals.dialogs.bangle_firmware_dialog import (
                BangleFirmwareDialog)
            BangleFirmwareDialog(self._ble(), entry).present(self)
        else:
            from vitals.dialogs.firmware_dialog import FirmwareDialog
            FirmwareDialog(self._ble(), entry).present(self)

    def _on_store(self, *_):
        from vitals.dialogs.app_store_dialog import AppStoreDialog
        AppStoreDialog(self._ble(), self._manager.get(self._address)).present(self)

    def _on_alarms(self, *_):
        from vitals.dialogs.alarms_dialog import AlarmsDialog
        entry = self._manager.get(self._address)
        AlarmsDialog(self._manager, self._address, entry.name).present(self)

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


def _action_row(title: str, icon: str) -> Adw.ButtonRow:
    row = Adw.ButtonRow(title=title)
    row.set_start_icon_name(icon)
    return row


def _hour_row(title: str, value: int) -> Adw.SpinRow:
    row = Adw.SpinRow(
        title=title,
        adjustment=Gtk.Adjustment(lower=0, upper=23, step_increment=1,
                                  page_increment=6))
    row.set_value(value)
    return row


def _sync_text(entry) -> str:
    if not entry.last_sync_ms:
        return "Never"
    when = datetime.fromtimestamp(entry.last_sync_ms / 1000).astimezone()
    return when.strftime("%d %b %H:%M")
