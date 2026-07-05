"""Pairing dialog — scan for nearby watches, let the user pick one.

Constructed imperatively (no .blp) to keep the v0.1.0 surface small.
A future revision can extract the layout to a Blueprint template if
the dialog grows.

The dialog runs the scan asynchronously on the BleManager and
surfaces results in a ListBox. Picking a row dispatches a connect
attempt; success persists the result to GSettings and closes the
dialog. Failures show a toast in the parent window and return the
user to the list.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from gi.repository import Adw, GLib, Gtk

from vitals.ble import BleManager, scan_devices
from vitals.devices.base import matching_device

log = logging.getLogger(__name__)


class PairingDialog(Adw.Dialog):
    __gtype_name__ = "VitalsPairingDialog"

    # Bangle.js in low-power broadcast advertises sparsely (~every 2 s);
    # 5 s wasn't enough to catch every wake cycle. 8 s gives 3-4 chances
    # without dragging out the dialog when nothing is around.
    SCAN_TIMEOUT_S = 8.0

    def __init__(self, ble: BleManager,
                 on_paired: Callable[[str, str, str, bool], None]):
        super().__init__()
        self._ble = ble
        self._on_paired = on_paired
        self._scan_generation = 0

        self.set_title("Pair a Device")
        self.set_content_width(400)
        self.set_content_height(480)

        toolbar = Adw.ToolbarView()
        header = Adw.HeaderBar()
        self._rescan_button = Gtk.Button.new_from_icon_name("view-refresh-symbolic")
        self._rescan_button.set_tooltip_text("Scan again")
        self._rescan_button.connect("clicked", lambda *_: self._start_scan())
        header.pack_end(self._rescan_button)
        toolbar.add_top_bar(header)

        self._stack = Gtk.Stack()
        self._stack.set_transition_type(Gtk.StackTransitionType.CROSSFADE)
        toolbar.set_content(self._stack)

        # ── Scanning page ──
        scan_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        scan_box.set_valign(Gtk.Align.CENTER)
        scan_box.set_halign(Gtk.Align.CENTER)
        scan_box.set_vexpand(True)
        scan_box.append(Adw.Spinner())
        scan_label = Gtk.Label(label="Scanning for nearby devices…")
        scan_label.add_css_class("dim-label")
        scan_box.append(scan_label)
        self._stack.add_named(scan_box, "scanning")

        # ── Results page ──
        results = Gtk.ScrolledWindow()
        results.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        results.set_vexpand(True)
        self._listbox = Gtk.ListBox()
        self._listbox.set_selection_mode(Gtk.SelectionMode.NONE)
        self._listbox.add_css_class("boxed-list")
        self._listbox.set_margin_top(12)
        self._listbox.set_margin_bottom(12)
        self._listbox.set_margin_start(12)
        self._listbox.set_margin_end(12)
        self._listbox.connect("row-activated", self._on_row_activated)
        results.set_child(self._listbox)
        self._stack.add_named(results, "results")

        # ── Empty page ──
        empty = Adw.StatusPage(
            icon_name="bluetooth-symbolic",
            title="No Devices Found",
            description=("Make sure your device is awake and in range, "
                         "then try again."))
        retry = Gtk.Button(label="Scan Again", halign=Gtk.Align.CENTER)
        retry.add_css_class("pill")
        retry.add_css_class("suggested-action")
        retry.connect("clicked", lambda *_: self._start_scan())
        empty.set_child(retry)
        self._stack.add_named(empty, "empty")

        # ── Connecting page ──
        connecting = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        connecting.set_valign(Gtk.Align.CENTER)
        connecting.set_halign(Gtk.Align.CENTER)
        connecting.append(Adw.Spinner())
        self._connecting_label = Gtk.Label(label="Connecting…")
        self._connecting_label.add_css_class("dim-label")
        connecting.append(self._connecting_label)
        self._stack.add_named(connecting, "connecting")

        self.set_child(toolbar)

        self._stack.connect("notify::visible-child-name",
                            self._on_page_changed)
        self._show_page("scanning")
        self._start_scan()

    # ── Page bookkeeping ──────────────────────────────────────────

    def _show_page(self, name: str) -> None:
        self._stack.set_visible_child_name(name)

    def _on_page_changed(self, *_args) -> None:
        # Rescan only makes sense once the in-flight scan has resolved
        # (i.e. on the results / empty pages); during scan + connect
        # the action is meaningless and the spinner already implies
        # "wait".
        page = self._stack.get_visible_child_name()
        self._rescan_button.set_sensitive(page in ("results", "empty"))

    # ── Scan ──────────────────────────────────────────────────────

    def _start_scan(self) -> None:
        # Bump the generation so any in-flight scan callback from a
        # previous click is ignored when it resolves.
        self._scan_generation += 1
        gen = self._scan_generation
        self._show_page("scanning")
        future = self._ble.submit(scan_devices(timeout=self.SCAN_TIMEOUT_S))
        future.add_done_callback(lambda f: self._scan_done(f, gen))

    def _scan_done(self, future, gen: int) -> None:
        if gen != self._scan_generation:
            log.debug("Pairing: stale scan callback (gen=%d, current=%d) "
                      "— ignoring", gen, self._scan_generation)
            return
        try:
            results = future.result()
        except Exception as e:
            log.exception("Pairing: scan failed")
            GLib.idle_add(self._show_error, f"Scan failed: {e}")
            return
        # Filter to devices that some plugin claims.
        keep: list[tuple[str, str, type]] = []
        for addr, name, uuids in results:
            cls = matching_device(name, uuids)
            if cls is not None:
                keep.append((addr, name, cls))
        log.info("Pairing: scan complete — %d raw devices, %d matched a "
                 "plugin", len(results), len(keep))
        for addr, name, cls in keep:
            log.info("Pairing:   match: %s %r -> %s", addr, name, cls.id)
        if not keep and results:
            # Helpful hint: if the user expected to see a watch, log the
            # full device list at INFO so they can confirm the watch is
            # advertising at all.
            for addr, name, uuids in results:
                log.info("Pairing:   unmatched: %s name=%r uuids=%s",
                         addr, name, uuids)
        GLib.idle_add(self._render_results, keep)

    def _render_results(self, devices: list[tuple[str, str, type]]) -> None:
        # Clear any previous list.
        child = self._listbox.get_first_child()
        while child:
            self._listbox.remove(child)
            child = self._listbox.get_first_child()

        if not devices:
            self._show_page("empty")
            return

        for addr, name, cls in devices:
            row = Adw.ActionRow(activatable=True,
                                title=name or cls.display_name,
                                subtitle=f"{cls.display_name} · {addr}")
            row.add_suffix(Gtk.Image.new_from_icon_name("go-next-symbolic"))
            # Stash the device class + address on the row so the
            # row-activated handler can dispatch.
            row._vitals_address = addr
            row._vitals_name = name
            row._vitals_cls = cls
            self._listbox.append(row)

        self._show_page("results")

    # ── Row activation -> connect ─────────────────────────────────

    def _on_row_activated(self, _listbox, row: Gtk.ListBoxRow) -> None:
        addr = getattr(row, "_vitals_address", None)
        name = getattr(row, "_vitals_name", "")
        cls  = getattr(row, "_vitals_cls",  None)
        if addr is None or cls is None:
            return

        self._connecting_label.set_text(f"Connecting to {name or cls.display_name}…")
        self._show_page("connecting")

        async def do_connect():
            instance = cls(address=addr, name=name)
            await instance.connect()
            try:
                # Detect a watch that needs onboarding (e.g. a Pebble in
                # PRF showing the setup QR) so we can offer to flash it.
                recovery = await instance.is_in_recovery()
            except Exception:
                recovery = None
            await instance.disconnect()  # we just verify pairing for v0.1.0
            return cls.id, bool(recovery)

        future = self._ble.submit(do_connect())

        def done(f):
            try:
                device_type, recovery = f.result()
            except Exception as e:
                GLib.idle_add(self._show_error,
                              f"Could not connect: {e}")
                return
            GLib.idle_add(self._finish, addr, name, device_type, recovery)

        future.add_done_callback(done)

    def _finish(self, addr: str, name: str, device_type: str,
                recovery: bool = False) -> None:
        self._on_paired(addr, name, device_type, recovery)
        self.close()

    def _show_error(self, message: str) -> None:
        # Bounce back to the results list (or empty page) and surface
        # the error via the parent's toast overlay.
        parent = self.get_root()
        if parent is not None and hasattr(parent, "show_toast"):
            parent.show_toast(message)
        if self._listbox.get_first_child() is None:
            self._show_page("empty")
        else:
            self._show_page("results")
