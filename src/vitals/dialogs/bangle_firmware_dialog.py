"""Bangle.js firmware-update dialog (Nordic DFU).

Bangle.js firmware can't be flashed over the normal connection: the
watch must first be put into its bootloader by a physical long-press
(Espruino disables remote DFU entry), after which it advertises as
``DfuTarg`` and Vitals streams the image to it. So this is a guided flow —
explain how to enter DFU mode, then download + scan + flash — rather than
the one-tap Pebble onboarding. See docs/bangle-firmware.md.
"""

from __future__ import annotations

import logging

from gi.repository import Adw, GLib, Gtk

from vitals.ble import BleManager
from vitals.devices.base import available_devices

log = logging.getLogger(__name__)

_INSTRUCTIONS = (
    "Bangle.js installs firmware in a special update mode that you start "
    "on the watch:\n\n"
    "1. Press and hold the button (about 10 seconds) until the screen "
    "goes blank.\n"
    "2. Release while the bar is moving across the screen.\n"
    "3. The watch shows “DfuTarg” — it's now ready.\n\n"
    "Then tap Download &amp; Flash below. A failed flash is safe — the watch "
    "stays in update mode so you can try again.")


class BangleFirmwareDialog(Adw.Dialog):
    __gtype_name__ = "VitalsBangleFirmwareDialog"

    def __init__(self, ble: BleManager, entry):
        super().__init__()
        self._ble = ble
        self._entry = entry
        self._closed = False
        self.connect("closed", lambda *_: setattr(self, "_closed", True))

        device_type = entry.kind
        self._plugin = available_devices().get(device_type)
        self._addr = entry.address
        self._name = entry.name

        self.set_title("Update Watch Firmware")
        self.set_content_width(380)
        self.set_content_height(480)

        toolbar = Adw.ToolbarView()
        toolbar.add_top_bar(Adw.HeaderBar())
        self._stack = Gtk.Stack()
        self._stack.set_transition_type(Gtk.StackTransitionType.CROSSFADE)
        toolbar.set_content(self._stack)
        self.set_child(toolbar)

        self._build_intro_page()
        self._build_working_page()
        self._build_result_page()
        self._stack.set_visible_child_name("intro")

    # ── Pages ─────────────────────────────────────────────────────

    def _build_intro_page(self) -> None:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        box.set_margin_top(18)
        box.set_margin_bottom(18)
        box.set_margin_start(18)
        box.set_margin_end(18)

        status = Adw.StatusPage(
            icon_name="software-update-available-symbolic",
            title="Update Watch Firmware", description=_INSTRUCTIONS)
        status.set_vexpand(True)
        box.append(status)

        group = Adw.PreferencesGroup()
        self._version_row = Adw.EntryRow()
        self._version_row.set_title("Version (optional)")
        default = getattr(self._plugin, "FIRMWARE_DEFAULT_VERSION", "")
        self._version_row.set_text(default)
        group.add(self._version_row)
        box.append(group)

        flash = Gtk.Button(label="Download & Flash")
        flash.add_css_class("suggested-action")
        flash.add_css_class("pill")
        flash.set_halign(Gtk.Align.CENTER)
        flash.connect("clicked", lambda *_: self._start())
        box.append(flash)

        self._stack.add_named(box, "intro")

    def _build_working_page(self) -> None:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18,
                      valign=Gtk.Align.CENTER)
        box.set_margin_start(24)
        box.set_margin_end(24)
        box.append(Adw.Spinner(height_request=48))
        self._status_label = Gtk.Label(label="Starting…")
        self._status_label.add_css_class("title-4")
        self._status_label.set_wrap(True)
        self._status_label.set_justify(Gtk.Justification.CENTER)
        self._status_label.set_max_width_chars(26)
        box.append(self._status_label)
        self._progress = Gtk.ProgressBar()
        self._progress.set_hexpand(True)
        box.append(self._progress)
        hint = Gtk.Label(label="Keep the watch in update mode and nearby.")
        hint.add_css_class("dim-label")
        hint.set_wrap(True)
        hint.set_justify(Gtk.Justification.CENTER)
        box.append(hint)
        self._stack.add_named(box, "working")

    def _build_result_page(self) -> None:
        self._result_page = Adw.StatusPage(vexpand=True)
        button = Gtk.Button(label="Close", halign=Gtk.Align.CENTER)
        button.add_css_class("pill")
        button.connect("clicked", lambda *_: self.close())
        self._result_page.set_child(button)
        self._stack.add_named(self._result_page, "result")

    # ── Flow ──────────────────────────────────────────────────────

    def _start(self) -> None:
        if not self._addr or self._plugin is None:
            self._show_result(False, "No watch paired",
                              "Pair a watch before updating firmware.")
            return
        version = self._version_row.get_text().strip()
        device = self._plugin(address=self._addr, name=self._name)

        self.set_can_close(False)
        self._set_status("Downloading firmware…", None)
        self._stack.set_visible_child_name("working")

        async def do_flash() -> None:
            firmware = (await device.fetch_default_firmware(version)
                        if version
                        else await device.fetch_default_firmware())
            GLib.idle_add(self._set_status,
                          "Looking for the watch in update mode…", None)
            await device.flash_firmware(firmware, on_progress=self._on_progress)

        future = self._ble.submit(do_flash())
        future.add_done_callback(self._flash_done)

    def _on_progress(self, stage: str, sent: int, total: int) -> None:
        fraction = (sent / total) if total else 0.0
        GLib.idle_add(self._set_status, "Installing firmware…", fraction)

    def _flash_done(self, future) -> None:
        try:
            future.result()
        except Exception as exc:  # noqa: BLE001
            log.exception("Bangle firmware: flash failed")
            GLib.idle_add(
                self._show_result, False, "Update Failed",
                f"{exc}\n\nMake sure the watch shows “DfuTarg”, then try "
                "again. The watch is safe — it stays in update mode.")
            return
        GLib.idle_add(
            self._show_result, True, "Firmware Updated",
            "Your watch will restart on the new firmware.")

    # ── Helpers ───────────────────────────────────────────────────

    def _set_status(self, text: str, fraction: float | None) -> bool:
        if self._closed:
            return False
        self._status_label.set_text(text)
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
