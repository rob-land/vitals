"""Install a Pebble app from a downloaded .pbw file.

Opened when a `.pbw` is launched into Vitals (the file association — e.g.
downloading one from apps.repebble.com). It reads and validates the
bundle, then offers to install it on a paired Pebble, reusing
`PebbleDevice.install_app`. If no Pebble is paired it explains what's
needed rather than failing.
"""

from __future__ import annotations

import logging
import threading

from gi.repository import Adw, GLib, Gtk

from vitals.ble import BleManager
from vitals.devices.base import available_devices
from vitals.devices.pebble.pbw import PbwError, parse_pbw

log = logging.getLogger(__name__)

# .pbw is the Pebble bundle format; only the Pebble plugin installs it.
_PEBBLE_ID = "pebble"


class PbwInstallDialog(Adw.Dialog):
    __gtype_name__ = "VitalsPbwInstallDialog"

    def __init__(self, ble: BleManager, entry, gfile):
        super().__init__()
        self._ble = ble
        self._entry = entry
        self._gfile = gfile
        self._closed = False
        self.connect("closed", lambda *_: setattr(self, "_closed", True))
        self._pbw_bytes: bytes | None = None

        self.set_title("Install Pebble App")
        self.set_content_width(380)
        self.set_content_height(420)

        toolbar = Adw.ToolbarView()
        toolbar.add_top_bar(Adw.HeaderBar())
        self._stack = Gtk.Stack()
        self._stack.set_transition_type(Gtk.StackTransitionType.CROSSFADE)
        toolbar.set_content(self._stack)
        self.set_child(toolbar)

        self._build_loading_page()
        self._build_confirm_page()
        self._build_busy_page()
        self._build_result_page()
        self._stack.set_visible_child_name("loading")
        self._load()

    # ── Pages ─────────────────────────────────────────────────────

    def _build_loading_page(self) -> None:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12,
                      valign=Gtk.Align.CENTER, vexpand=True)
        box.append(Adw.Spinner())
        self._stack.add_named(box, "loading")

    def _build_confirm_page(self) -> None:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        box.set_margin_top(18)
        box.set_margin_bottom(18)
        box.set_margin_start(18)
        box.set_margin_end(18)
        self._confirm_status = Adw.StatusPage(
            icon_name="folder-download-symbolic")
        self._confirm_status.set_vexpand(True)
        box.append(self._confirm_status)
        self._install_button = Gtk.Button(label="Install")
        self._install_button.add_css_class("suggested-action")
        self._install_button.add_css_class("pill")
        self._install_button.set_halign(Gtk.Align.CENTER)
        self._install_button.connect("clicked", lambda *_: self._start())
        box.append(self._install_button)
        self._stack.add_named(box, "confirm")

    def _build_busy_page(self) -> None:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18,
                      valign=Gtk.Align.CENTER)
        box.set_margin_start(24)
        box.set_margin_end(24)
        box.append(Adw.Spinner(height_request=48))
        self._busy_label = Gtk.Label(label="Installing…")
        self._busy_label.add_css_class("title-4")
        self._busy_label.set_wrap(True)
        self._busy_label.set_justify(Gtk.Justification.CENTER)
        box.append(self._busy_label)
        self._progress = Gtk.ProgressBar()
        self._progress.set_hexpand(True)
        box.append(self._progress)
        self._stack.add_named(box, "busy")

    def _build_result_page(self) -> None:
        self._result_page = Adw.StatusPage(vexpand=True)
        button = Gtk.Button(label="Close", halign=Gtk.Align.CENTER)
        button.add_css_class("pill")
        button.connect("clicked", lambda *_: self.close())
        self._result_page.set_child(button)
        self._stack.add_named(self._result_page, "result")

    # ── Load + validate ───────────────────────────────────────────

    def _load(self) -> None:
        def work() -> None:
            try:
                ok, data, _etag = self._gfile.load_contents(None)
                app = parse_pbw(bytes(data), platform="emery")
            except (GLib.Error, PbwError) as exc:
                GLib.idle_add(self._show_result, False, "Can't open file",
                              f"This doesn't look like a Pebble app.\n\n{exc}")
                return
            except Exception as exc:  # noqa: BLE001
                GLib.idle_add(self._show_result, False, "Can't open file",
                              str(exc))
                return
            GLib.idle_add(self._on_loaded, bytes(data), app.name)

        threading.Thread(target=work, name="tock-pbw-load",
                         daemon=True).start()

    def _on_loaded(self, data: bytes, app_name: str) -> bool:
        if self._closed:
            return False
        self._pbw_bytes = data
        addr = self._entry.address
        device_type = self._entry.kind
        plugin = available_devices().get(device_type)

        if not addr or device_type != _PEBBLE_ID or plugin is None \
                or not plugin.SUPPORTS_APP_INSTALL:
            self._confirm_status.set_title(app_name)
            self._confirm_status.set_description(
                "This is a Pebble app. Pair a Pebble watch to install it.")
            self._install_button.set_visible(False)
        else:
            watch = self._entry.name or "watch"
            self._confirm_status.set_title(app_name)
            self._confirm_status.set_description(f"Install on {watch}?")
            self._install_button.set_visible(True)
        self._stack.set_visible_child_name("confirm")
        return False

    # ── Install ───────────────────────────────────────────────────

    def _start(self) -> None:
        if self._pbw_bytes is None:
            return
        addr = self._entry.address
        plugin = available_devices().get(
            self._entry.kind)
        if not addr or plugin is None:
            return
        device = plugin(address=addr,
                        name=self._entry.name)
        bundle = self._pbw_bytes

        self.set_can_close(False)
        self._set_busy("Connecting — confirm on the watch if asked…", None)
        self._stack.set_visible_child_name("busy")

        async def do_install() -> None:
            await device.connect()
            try:
                await device.install_app(bundle, on_progress=self._on_progress)
            finally:
                await device.disconnect()

        future = self._ble.submit(do_install())
        future.add_done_callback(self._install_done)

    def _on_progress(self, stage: str, sent: int, total: int) -> None:
        fraction = (sent / total) if total else 0.0
        GLib.idle_add(self._set_busy, f"Installing {stage}…", fraction)

    def _install_done(self, future) -> None:
        try:
            future.result()
        except Exception as exc:  # noqa: BLE001
            log.exception("PBW install failed")
            GLib.idle_add(self._show_result, False, "Install Failed", str(exc))
            return
        GLib.idle_add(self._show_result, True, "Installed",
                      "The app is now on your watch.")

    # ── Helpers ───────────────────────────────────────────────────

    def _set_busy(self, text: str, fraction: float | None) -> bool:
        if self._closed:
            return False
        self._busy_label.set_text(text)
        if fraction is None:
            self._progress.pulse()
        else:
            self._progress.set_fraction(fraction)
        return False

    def _show_result(self, ok: bool, title: str, description: str) -> bool:
        if self._closed:
            return False
        self.set_can_close(True)
        self._result_page.set_icon_name(
            "object-select-symbolic" if ok else "dialog-warning-symbolic")
        self._result_page.set_title(title)
        self._result_page.set_description(description)
        self._stack.set_visible_child_name("result")
        return False
