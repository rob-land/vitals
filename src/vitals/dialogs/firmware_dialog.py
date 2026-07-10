"""Firmware-install dialog for watches flashed over their normal
connection.

Two families use this flow today, both driven entirely by the plugin's
firmware declarations (`FIRMWARE_INTRO`, `FIRMWARE_VARIANTS`,
`FIRMWARE_DEFAULT_VERSION`, `FIRMWARE_SUCCESS_NOTE`):

  - **Pebble** — PRF onboarding. A factory-fresh Core Devices Pebble
    boots into recovery firmware showing a setup QR code; this dialog
    downloads the matching normal firmware (with a pvt/dvt variant
    picker) and flashes it over PutBytes.
  - **PineTime** — a routine InfiniTime update over the in-firmware
    legacy DFU service, with a version row and a post-install note
    (the user must validate the new firmware on the watch).

Watches that need a separate bootloader mode (Bangle.js) use
BangleFirmwareDialog instead — see FIRMWARE_REQUIRES_DFU_MODE.

It follows the PairingDialog pattern: a Gtk.Stack of pages driven by
work submitted to the BleManager, with results marshalled back to GTK
via GLib.idle_add. The whole flow — download, connect, transfer — runs
on the BLE loop; this dialog only reflects its progress. A failed
flash is safe: the watch keeps its current firmware.
"""

from __future__ import annotations

import asyncio
import logging

from gi.repository import Adw, GLib, Gtk

from vitals.ble import BleManager
from vitals.devices.base import available_devices

log = logging.getLogger(__name__)

_GENERIC_INTRO = ("Vitals can download this watch's firmware and install "
                  "it over the air. Keep the watch awake and nearby.")

# Post-flash the watch reboots into normal firmware; wait, then retry
# connecting and setting the clock for a while (it may still be in
# on-device first-time setup, or swapping the new image in).
REBOOT_INITIAL_WAIT = 30.0
REBOOT_SYNC_WINDOW = 180.0
REBOOT_RETRY_INTERVAL = 15.0


class FirmwareDialog(Adw.Dialog):
    __gtype_name__ = "VitalsFirmwareDialog"

    def __init__(self, ble: BleManager, entry, on_done=None):
        super().__init__()
        self._ble = ble
        self._entry = entry
        self._plugin = available_devices().get(entry.kind)
        self._on_done = on_done
        self._variant_row = None
        self._version_row = None
        # Set once the dialog is dismissed, so background flash callbacks
        # stop touching destroyed widgets (the post-reboot sync can still
        # be running on the BLE loop after the user closes the dialog).
        self._closed = False
        self.connect("closed", lambda *_: setattr(self, "_closed", True))

        self.set_title("Install Watch Firmware")
        # Keep the preferred width within a phone screen; on narrow
        # displays libadwaita presents this as a bottom sheet anyway.
        self.set_content_width(360)
        self.set_content_height(440)

        toolbar = Adw.ToolbarView()
        toolbar.add_top_bar(Adw.HeaderBar())

        self._stack = Gtk.Stack()
        self._stack.set_transition_type(Gtk.StackTransitionType.CROSSFADE)
        toolbar.set_content(self._stack)

        self._build_intro_page()
        self._build_working_page()
        self._build_result_page()

        self.set_child(toolbar)
        self._stack.set_visible_child_name("intro")

    # ── Pages ─────────────────────────────────────────────────────

    def _build_intro_page(self) -> None:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        box.set_margin_top(18)
        box.set_margin_bottom(18)
        box.set_margin_start(18)
        box.set_margin_end(18)

        intro = (self._plugin.FIRMWARE_INTRO if self._plugin else "") \
            or _GENERIC_INTRO
        status = Adw.StatusPage(
            icon_name="software-update-available-symbolic",
            title="Install Watch Firmware", description=intro)
        status.set_vexpand(True)
        box.append(status)

        group = self._build_options_group()
        if group is not None:
            box.append(group)

        install = Gtk.Button(label="Download & Install")
        install.add_css_class("suggested-action")
        install.add_css_class("pill")
        install.set_halign(Gtk.Align.CENTER)
        install.connect("clicked", lambda *_: self._start())
        box.append(install)

        self._stack.add_named(box, "intro")

    def _build_options_group(self) -> Adw.PreferencesGroup | None:
        """Rows for whatever choices this family declares: a variant
        picker and/or an editable version. None when there are none."""
        if self._plugin is None:
            return None
        group = Adw.PreferencesGroup()
        if self._plugin.FIRMWARE_VARIANTS:
            self._variant_row = Adw.ComboRow()
            self._variant_row.set_title("Firmware Variant")
            self._variant_row.set_subtitle(
                "A wrong choice is rejected safely — the watch keeps "
                "its current firmware")
            self._variant_row.set_model(Gtk.StringList.new(
                [label for label, _ in self._plugin.FIRMWARE_VARIANTS]))
            group.add(self._variant_row)
        if self._plugin.FIRMWARE_DEFAULT_VERSION:
            self._version_row = Adw.EntryRow()
            self._version_row.set_title("Version")
            self._version_row.set_text(self._plugin.FIRMWARE_DEFAULT_VERSION)
            group.add(self._version_row)
        if self._variant_row is None and self._version_row is None:
            return None
        return group

    def _build_working_page(self) -> None:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        box.set_valign(Gtk.Align.CENTER)
        box.set_margin_start(24)
        box.set_margin_end(24)

        box.append(Adw.Spinner(height_request=48))
        self._status_label = Gtk.Label(label="Starting…")
        self._status_label.add_css_class("title-4")
        # Long status lines ("Connecting — confirm pairing on your
        # watch…") must wrap, not push the dialog past the screen edge.
        self._status_label.set_wrap(True)
        self._status_label.set_justify(Gtk.Justification.CENTER)
        self._status_label.set_max_width_chars(24)
        box.append(self._status_label)

        self._progress = Gtk.ProgressBar()
        self._progress.set_show_text(True)
        self._progress.set_hexpand(True)
        box.append(self._progress)

        hint = Gtk.Label(label="Don't turn off the watch or close Vitals.")
        hint.add_css_class("dim-label")
        hint.set_wrap(True)
        hint.set_justify(Gtk.Justification.CENTER)
        box.append(hint)

        self._stack.add_named(box, "working")

    def _build_result_page(self) -> None:
        self._result_page = Adw.StatusPage()
        self._result_page.set_vexpand(True)
        button = Gtk.Button(label="Close")
        button.add_css_class("pill")
        button.set_halign(Gtk.Align.CENTER)
        button.connect("clicked", lambda *_: self.close())
        self._result_page.set_child(button)
        self._stack.add_named(self._result_page, "result")

    # ── Flow ──────────────────────────────────────────────────────

    def _fetch_opts(self) -> dict:
        """fetch_default_firmware kwargs from the option rows."""
        opts: dict = {}
        if self._variant_row is not None:
            _label, value = self._plugin.FIRMWARE_VARIANTS[
                self._variant_row.get_selected()]
            opts["variant"] = value
        if self._version_row is not None:
            version = self._version_row.get_text().strip()
            if version:
                opts["version"] = version
        return opts

    def _start(self) -> None:
        addr = self._entry.address
        plugin = self._plugin
        if not addr or plugin is None:
            self._show_result(False, "No watch paired",
                              "Pair a watch before installing firmware.")
            return
        if not plugin.SUPPORTS_FIRMWARE_UPDATE:
            self._show_result(
                False, "Not supported",
                f"{plugin.display_name} doesn't support firmware installs.")
            return

        opts = self._fetch_opts()
        device = plugin(address=addr, name=self._entry.name)

        # No closing mid-flash — interrupting a transfer just means
        # re-doing it (the watch keeps its firmware), but discourage it.
        self.set_can_close(False)
        self._set_status("Downloading firmware…", None)
        self._stack.set_visible_child_name("working")

        async def do_flash() -> dict:
            firmware = await device.fetch_default_firmware(**opts)
            GLib.idle_add(self._set_status,
                          "Connecting — confirm pairing on your watch…", None)
            await device.connect()
            try:
                await device.flash_firmware(
                    firmware, on_progress=self._on_progress)
            finally:
                await device.disconnect()
            # The transfer is done and the watch is rebooting into the new
            # firmware; the rest is best-effort, so let the user leave.
            GLib.idle_add(self.set_can_close, True)
            synced = await self._wait_for_reboot_and_sync(device)
            return {"name": device.name, "synced": synced}

        future = self._ble.submit(do_flash())
        future.add_done_callback(self._flash_done)

    async def _wait_for_reboot_and_sync(self, device) -> bool:
        """After the flash the watch reboots into normal firmware (~a
        minute); wait for it to come back, then set its clock. Best
        effort — a fresh watch may still be in on-device setup and refuse
        until that's done, in which case the user syncs later."""
        import time

        GLib.idle_add(self._set_status,
                      "Waiting for the watch to restart…", None)
        await asyncio.sleep(REBOOT_INITIAL_WAIT)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + REBOOT_SYNC_WINDOW
        while loop.time() < deadline and not self._closed:
            try:
                await device.connect()
                try:
                    GLib.idle_add(self._set_status, "Setting the time…", None)
                    await device.sync_time(time.time())
                    return True
                finally:
                    await device.disconnect()
            except Exception:
                log.debug("Firmware: post-reboot sync retry", exc_info=True)
                await asyncio.sleep(REBOOT_RETRY_INTERVAL)
        return False

    def _on_progress(self, stage: str, sent: int, total: int) -> None:
        # Called on the BLE loop — marshal to GTK.
        fraction = (sent / total) if total else 0.0
        GLib.idle_add(
            self._set_status,
            f"Installing {stage}…", fraction)

    def _flash_done(self, future) -> None:
        try:
            result = future.result()
        except Exception as exc:  # noqa: BLE001 — surface any failure
            log.exception("Firmware: flash failed")
            GLib.idle_add(
                self._show_result, False, "Install Failed",
                f"{exc}\n\nThe watch is unharmed — it keeps its current "
                "firmware, so you can try again.")
            return
        name = result["name"] or "Your watch"
        if result["synced"]:
            description = f"{name} is up and running, and the time is set."
        else:
            description = (f"{name} will restart and finish setup. Once it's "
                           "ready, open Vitals and tap Sync Now to set the time.")
        note = self._plugin.FIRMWARE_SUCCESS_NOTE if self._plugin else ""
        if note:
            description += f"\n\n{note}"
        GLib.idle_add(self._show_result, True, "Firmware Installed", description)

    # ── Helpers ───────────────────────────────────────────────────

    def _set_status(self, text: str, fraction: float | None) -> None:
        if self._closed:
            return
        self._status_label.set_text(text)
        if fraction is None:
            self._progress.pulse()
            self._progress.set_text(None)
        else:
            self._progress.set_fraction(fraction)
            self._progress.set_text(f"{int(round(fraction * 100))}%")

    def _show_result(self, ok: bool, title: str, description: str) -> None:
        if self._closed:
            return
        self.set_can_close(True)
        self._result_page.set_icon_name(
            "object-select-symbolic" if ok else "dialog-warning-symbolic")
        self._result_page.set_title(title)
        self._result_page.set_description(description)
        self._stack.set_visible_child_name("result")
        if ok and self._on_done is not None:
            self._on_done()
