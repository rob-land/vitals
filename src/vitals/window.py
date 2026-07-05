"""Vitals main window — the adaptive shell hosting the three views."""

from __future__ import annotations

import logging

from gi.repository import Adw, Gio, GLib, Gtk

from vitals.pages.dashboard import Dashboard
from vitals.pages.devices import Devices
from vitals.pages.timeline import Timeline

log = logging.getLogger(__name__)

_REFRESH_DEBOUNCE_MS = 300


@Gtk.Template(resource_path="/land/rob/vitals/ui/window.ui")
class VitalsWindow(Adw.ApplicationWindow):
    __gtype_name__ = "VitalsWindow"

    toast_overlay:      Adw.ToastOverlay = Gtk.Template.Child()
    navigation_view:    Adw.NavigationView = Gtk.Template.Child()
    view_stack:         Adw.ViewStack    = Gtk.Template.Child()
    title_stack:        Gtk.Stack        = Gtk.Template.Child()
    view_switcher_bar:  Adw.ViewSwitcherBar = Gtk.Template.Child()
    dashboard_bin:      Adw.Bin          = Gtk.Template.Child()
    timeline_bin:       Adw.Bin          = Gtk.Template.Child()
    devices_bin:        Adw.Bin          = Gtk.Template.Child()

    def __init__(self, *, application, **kwargs):
        super().__init__(application=application, **kwargs)
        app = application
        self._settings = app.settings
        self._refresh_pending = 0

        # Suite-standard window action: any child can fire a toast via
        # widget.activate_action("win.toast", GLib.Variant("s", msg)).
        toast_action = Gio.SimpleAction.new("toast", GLib.VariantType.new("s"))
        toast_action.connect(
            "activate",
            lambda _a, p: self.toast_overlay.add_toast(Adw.Toast.new(p.get_string())))
        self.add_action(toast_action)

        help_action = Gio.SimpleAction.new("show-help-overlay", None)
        help_action.connect("activate", self._show_help_overlay)
        self.add_action(help_action)

        for name, opener in (("add-food", self._add_food),
                             ("add-water", self._add_water),
                             ("add-measurement", self._add_measurement)):
            action = Gio.SimpleAction.new(name, None)
            action.connect("activate", opener)
            self.add_action(action)

        self._pages = {
            "dashboard": Dashboard(app.store, app.settings),
            "timeline": Timeline(app.store, app.catalog),
            "devices": Devices(app.device_manager, app.ble, app.scan_broker),
        }
        self.dashboard_bin.set_child(self._pages["dashboard"])
        self.timeline_bin.set_child(self._pages["timeline"])
        self.devices_bin.set_child(self._pages["devices"])

        self.set_default_size(self._settings.get_int("window-width"),
                              self._settings.get_int("window-height"))
        if self._settings.get_boolean("window-maximized"):
            self.maximize()
        self.connect("close-request", self._on_close_request)

        # Refresh the visible page on tab switch (pages showing stale
        # data catch up lazily) and when new records land in the store.
        self.view_stack.connect("notify::visible-child", self._on_view_changed)
        app.record_bus.connect("records-changed", self._on_records_changed)
        app.device_manager.connect(
            "device-synced", lambda _m, _addr, msg: self.show_toast(msg))
        self.connect("close-request", self._maybe_close_to_background)
        self.refresh()

    # ── Public API ────────────────────────────────────────────────
    def show_toast(self, message: str) -> None:
        toast = Adw.Toast.new(message)
        toast.set_timeout(4)
        self.toast_overlay.add_toast(toast)

    def refresh(self) -> None:
        """Reload the currently visible page from the store."""
        page = self._pages.get(self.view_stack.get_visible_child_name())
        if page is not None:
            page.refresh()

    def push_device_detail(self, address: str) -> None:
        from vitals.pages.device_detail import DeviceDetailPage
        app = self.get_application()
        self.navigation_view.push(DeviceDetailPage(app.device_manager, address))

    def pop_device_detail(self) -> None:
        self.navigation_view.pop_to_tag("main")

    # ── Internals ─────────────────────────────────────────────────
    def _on_records_changed(self, _bus, _types) -> None:
        # Debounce: a sync drains hundreds of batches back to back.
        if self._refresh_pending:
            GLib.source_remove(self._refresh_pending)
        self._refresh_pending = GLib.timeout_add(
            _REFRESH_DEBOUNCE_MS, self._debounced_refresh)

    def _debounced_refresh(self) -> bool:
        self._refresh_pending = 0
        self.refresh()
        return GLib.SOURCE_REMOVE

    def _on_view_changed(self, *_):
        self.refresh()

    def _add_food(self, *_):
        from vitals.sources.food import FoodDialog
        app = self.get_application()
        FoodDialog(app.recorder, app.settings).present(self)

    def _add_water(self, *_):
        from vitals.sources.water import WaterDialog
        app = self.get_application()
        WaterDialog(app.recorder, app.settings).present(self)

    def _add_measurement(self, *_):
        from vitals.sources.measurements import MeasurementDialog
        app = self.get_application()
        MeasurementDialog(app.recorder, app.settings).present(self)

    def _show_help_overlay(self, *_):
        builder = Gtk.Builder.new_from_resource("/land/rob/vitals/ui/help-overlay.ui")
        overlay = builder.get_object("help_overlay")
        overlay.set_transient_for(self)
        overlay.present()

    def _on_close_request(self, *_):
        if not self.is_maximized():
            self._settings.set_int("window-width", self.get_width())
            self._settings.set_int("window-height", self.get_height())
        self._settings.set_boolean("window-maximized", self.is_maximized())
        return False

    def _maybe_close_to_background(self, *_):
        if not self._settings.get_boolean("run-in-background"):
            return False
        # Hide instead of quitting so background syncs keep running.
        self.get_application().hold_for_background()
        self.set_visible(False)
        return True
