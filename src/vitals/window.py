"""Vitals main window — the adaptive shell hosting the three views."""

from __future__ import annotations

import logging

from gi.repository import Adw, Gio, GLib, Gtk

from vitals.pages.permissions import PermissionsPage
from vitals.pages.today import TodayPage
from vitals.pages.trends import TrendsPage
from vitals.pulse_client import PulseClient

log = logging.getLogger(__name__)


@Gtk.Template(resource_path="/land/rob/vitals/ui/window.ui")
class VitalsWindow(Adw.ApplicationWindow):
    __gtype_name__ = "VitalsWindow"

    toast_overlay:      Adw.ToastOverlay = Gtk.Template.Child()
    view_stack:         Adw.ViewStack    = Gtk.Template.Child()
    title_stack:        Gtk.Stack        = Gtk.Template.Child()
    view_switcher_bar:  Adw.ViewSwitcherBar = Gtk.Template.Child()
    today_bin:          Adw.Bin          = Gtk.Template.Child()
    trends_bin:         Adw.Bin          = Gtk.Template.Child()
    permissions_bin:    Adw.Bin          = Gtk.Template.Child()

    def __init__(self, *, settings: Gio.Settings, client: PulseClient, **kwargs):
        super().__init__(**kwargs)
        self._settings = settings
        self._client = client

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

        self._today = TodayPage(self._client, self._settings)
        self._trends = TrendsPage(self._client, self._settings)
        self._permissions = PermissionsPage(self._client)
        self.today_bin.set_child(self._today)
        self.trends_bin.set_child(self._trends)
        self.permissions_bin.set_child(self._permissions)

        self.set_default_size(
            settings.get_int("window-width"), settings.get_int("window-height"))
        if settings.get_boolean("window-maximized"):
            self.maximize()
        self.connect("close-request", self._on_close_request)

        # Refresh the visible page when the user switches views, so we
        # don't pull every page's data up front.
        self.view_stack.connect("notify::visible-child", self._on_view_changed)
        # Reflect preference changes (step goal, trends window) immediately.
        for key in ("daily-step-goal", "trends-days"):
            settings.connect(f"changed::{key}", lambda *_: self.refresh())
        self.refresh()

    # ── Public API ────────────────────────────────────────────────
    def show_toast(self, message: str) -> None:
        toast = Adw.Toast.new(message)
        toast.set_timeout(4)
        self.toast_overlay.add_toast(toast)

    def refresh(self) -> None:
        """Reload the currently visible page from Pulse."""
        pages = {"today": self._today, "trends": self._trends,
                 "permissions": self._permissions}
        page = pages.get(self.view_stack.get_visible_child_name())
        if page is not None:
            page.refresh()

    # ── Internals ─────────────────────────────────────────────────
    def _on_view_changed(self, *_):
        self.refresh()

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
