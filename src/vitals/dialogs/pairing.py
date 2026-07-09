"""The add-a-device pairing wizard.

A guided flow modelled on OMRON Connect's onboarding: pick a category →
pick a model (skipped when a category has one plugin) → follow the
device's own put-it-in-pairing-mode steps → scan (filtered to that
device family) → pick the discovered device → register it. Each plugin
supplies its own ``CATEGORY`` / ``ICON_NAME`` / ``PAIRING_STEPS`` (see
``devices.base``), so the wizard needs no per-device code.

A "Search for any device" escape hatch keeps the old scan-everything
behaviour for anything the categories miss.

Constructed imperatively (no .blp) to keep the surface small.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from gi.repository import Adw, GLib, Gtk

from vitals.ble import BleManager
from vitals.devices.base import (
    CATEGORY_INFO, matching_device, plugins_by_category)

log = logging.getLogger(__name__)


class PairingDialog(Adw.Dialog):
    __gtype_name__ = "VitalsPairingDialog"

    # Bangle.js in low-power broadcast advertises sparsely (~every 2 s);
    # 5 s wasn't enough to catch every wake cycle. 8 s gives 3-4 chances
    # without dragging out the dialog when nothing is around.
    SCAN_TIMEOUT_S = 8.0

    def __init__(self, ble: BleManager, broker,
                 on_paired: Callable[[str, str, str, bool], None]):
        super().__init__()
        self._ble = ble
        self._broker = broker
        self._on_paired = on_paired
        self._scan_generation = 0
        self._selected_plugin: type | None = None
        self._back_target = "category"

        self.set_title("Add a Device")
        self.set_content_width(400)
        self.set_content_height(520)

        toolbar = Adw.ToolbarView()
        self._header = Adw.HeaderBar()
        self._back = Gtk.Button.new_from_icon_name("go-previous-symbolic")
        self._back.set_tooltip_text("Back")
        self._back.connect("clicked", lambda *_: self._go_back())
        self._header.pack_start(self._back)
        self._rescan_button = Gtk.Button.new_from_icon_name(
            "view-refresh-symbolic")
        self._rescan_button.set_tooltip_text("Scan again")
        self._rescan_button.connect("clicked", lambda *_: self._start_scan())
        self._header.pack_end(self._rescan_button)
        toolbar.add_top_bar(self._header)

        self._stack = Gtk.Stack(
            transition_type=Gtk.StackTransitionType.CROSSFADE)
        toolbar.set_content(self._stack)

        self._build_category_page()
        self._build_model_page()
        self._build_guidance_page()
        self._build_scan_pages()

        self.set_child(toolbar)
        self._stack.connect("notify::visible-child-name", self._on_page_changed)
        self._show_page("category")

    # ── page helpers ──────────────────────────────────────────────
    def _show_page(self, name: str) -> None:
        self._stack.set_visible_child_name(name)

    def _on_page_changed(self, *_args) -> None:
        page = self._stack.get_visible_child_name()
        self._back.set_visible(page not in ("category", "connecting"))
        self._rescan_button.set_visible(page in ("results", "empty"))

    def _go_back(self) -> None:
        page = self._stack.get_visible_child_name()
        if page in ("scanning", "results", "empty"):
            self._show_page("guidance" if self._selected_plugin else "category")
        elif page == "guidance":
            self._show_page(self._back_target)
        else:  # model
            self._show_page("category")

    @staticmethod
    def _scroll(child) -> Gtk.ScrolledWindow:
        scroller = Gtk.ScrolledWindow(vexpand=True)
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroller.set_child(child)
        return scroller

    @staticmethod
    def _page_box(heading: str) -> Gtk.Box:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10,
                      margin_top=16, margin_bottom=16,
                      margin_start=14, margin_end=14)
        title = Gtk.Label(label=heading, xalign=0)
        title.add_css_class("title-4")
        box.append(title)
        return box

    # ── step 1: category ──────────────────────────────────────────
    def _build_category_page(self) -> None:
        box = self._page_box("What are you adding?")
        listbox = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        listbox.add_css_class("boxed-list")
        for category, plugins in plugins_by_category().items():
            label, icon, desc = CATEGORY_INFO.get(
                category, (category.title(), "bluetooth-symbolic", ""))
            row = Adw.ActionRow(activatable=True, title=label, subtitle=desc)
            row.add_prefix(Gtk.Image.new_from_icon_name(icon))
            row.add_suffix(Gtk.Image.new_from_icon_name("go-next-symbolic"))
            row._vitals_plugins = plugins
            listbox.append(row)
        any_row = Adw.ActionRow(
            activatable=True, title="Search for any device",
            subtitle="Scan and match automatically")
        any_row.add_prefix(Gtk.Image.new_from_icon_name("bluetooth-symbolic"))
        any_row._vitals_plugins = None
        listbox.append(any_row)
        listbox.connect("row-activated", self._on_category_activated)
        box.append(self._scroll(listbox))
        self._stack.add_named(box, "category")

    def _on_category_activated(self, _listbox, row) -> None:
        plugins = getattr(row, "_vitals_plugins", None)
        if plugins is None:                       # "any device" escape hatch
            self._selected_plugin = None
            self._start_scan()
        elif len(plugins) == 1:
            self._selected_plugin = plugins[0]
            self._back_target = "category"
            self._show_guidance()
        else:
            self._populate_model_page(plugins)
            self._show_page("model")

    # ── step 2: model ─────────────────────────────────────────────
    def _build_model_page(self) -> None:
        box = self._page_box("Which one?")
        self._model_list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        self._model_list.add_css_class("boxed-list")
        self._model_list.connect("row-activated", self._on_model_activated)
        box.append(self._scroll(self._model_list))
        self._stack.add_named(box, "model")

    def _populate_model_page(self, plugins) -> None:
        child = self._model_list.get_first_child()
        while child:
            self._model_list.remove(child)
            child = self._model_list.get_first_child()
        for cls in plugins:
            row = Adw.ActionRow(activatable=True, title=cls.display_name,
                                subtitle=cls.description)
            row.add_suffix(Gtk.Image.new_from_icon_name("go-next-symbolic"))
            row._vitals_cls = cls
            self._model_list.append(row)

    def _on_model_activated(self, _listbox, row) -> None:
        self._selected_plugin = getattr(row, "_vitals_cls", None)
        self._back_target = "model"
        self._show_guidance()

    # ── step 3: guidance ──────────────────────────────────────────
    def _build_guidance_page(self) -> None:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16,
                      margin_top=24, margin_bottom=24,
                      margin_start=20, margin_end=20, valign=Gtk.Align.CENTER)
        self._guide_icon = Gtk.Image()
        self._guide_icon.set_pixel_size(56)
        self._guide_icon.add_css_class("dim-label")
        box.append(self._guide_icon)
        self._guide_title = Gtk.Label()
        self._guide_title.add_css_class("title-2")
        self._guide_title.set_wrap(True)
        self._guide_title.set_justify(Gtk.Justification.CENTER)
        box.append(self._guide_title)
        self._guide_steps = Gtk.Box(orientation=Gtk.Orientation.VERTICAL,
                                    spacing=10)
        box.append(self._guide_steps)
        search = Gtk.Button(label="Search", halign=Gtk.Align.CENTER)
        search.add_css_class("pill")
        search.add_css_class("suggested-action")
        search.connect("clicked", lambda *_: self._start_scan())
        box.append(search)
        self._stack.add_named(box, "guidance")

    def _show_guidance(self) -> None:
        cls = self._selected_plugin
        self._guide_icon.set_from_icon_name(
            getattr(cls, "ICON_NAME", "bluetooth-symbolic"))
        self._guide_title.set_label(f"Set up your {cls.display_name}")
        child = self._guide_steps.get_first_child()
        while child:
            self._guide_steps.remove(child)
            child = self._guide_steps.get_first_child()
        for i, step in enumerate(cls.PAIRING_STEPS or ["Keep the device "
                                 "nearby and awake, then search."], 1):
            self._guide_steps.append(self._step_row(i, step))
        self._show_page("guidance")

    @staticmethod
    def _step_row(number: int, text: str) -> Gtk.Box:
        row = Gtk.Box(spacing=12)
        badge = Gtk.Label(label=str(number), valign=Gtk.Align.START)
        badge.add_css_class("numeric")
        badge.add_css_class("title-4")
        badge.set_size_request(20, -1)
        row.append(badge)
        label = Gtk.Label(label=text, xalign=0, wrap=True, hexpand=True)
        row.append(label)
        return row

    # ── scan / results / connect ──────────────────────────────────
    def _build_scan_pages(self) -> None:
        scanning = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12,
                           valign=Gtk.Align.CENTER, halign=Gtk.Align.CENTER,
                           vexpand=True)
        scanning.append(Adw.Spinner())
        scan_label = Gtk.Label(label="Scanning for nearby devices…")
        scan_label.add_css_class("dim-label")
        scanning.append(scan_label)
        self._stack.add_named(scanning, "scanning")

        results = Gtk.ScrolledWindow(vexpand=True)
        results.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self._listbox = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        self._listbox.add_css_class("boxed-list")
        self._listbox.set_margin_top(12)
        self._listbox.set_margin_bottom(12)
        self._listbox.set_margin_start(12)
        self._listbox.set_margin_end(12)
        self._listbox.connect("row-activated", self._on_row_activated)
        results.set_child(self._listbox)
        self._stack.add_named(results, "results")

        empty = Adw.StatusPage(
            icon_name="bluetooth-symbolic", title="No Devices Found",
            description=("Make sure your device is awake and in pairing "
                         "mode, then try again."))
        retry = Gtk.Button(label="Scan Again", halign=Gtk.Align.CENTER)
        retry.add_css_class("pill")
        retry.add_css_class("suggested-action")
        retry.connect("clicked", lambda *_: self._start_scan())
        empty.set_child(retry)
        self._stack.add_named(empty, "empty")

        connecting = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12,
                             valign=Gtk.Align.CENTER, halign=Gtk.Align.CENTER,
                             vexpand=True)
        connecting.append(Adw.Spinner())
        self._connecting_label = Gtk.Label(label="Connecting…")
        self._connecting_label.add_css_class("dim-label")
        connecting.append(self._connecting_label)
        self._stack.add_named(connecting, "connecting")

    def _start_scan(self) -> None:
        self._scan_generation += 1
        gen = self._scan_generation
        self._show_page("scanning")
        future = self._broker.collect(self.SCAN_TIMEOUT_S)
        future.add_done_callback(lambda f: self._scan_done(f, gen))

    def _scan_done(self, future, gen: int) -> None:
        if gen != self._scan_generation:
            return
        try:
            results = future.result()
        except Exception as e:
            log.exception("Pairing: scan failed")
            GLib.idle_add(self._show_error, f"Scan failed: {e}")
            return
        keep: list[tuple[str, str, type]] = []
        for addr, name, uuids in results:
            if self._selected_plugin is not None:
                if self._selected_plugin.matches(name, uuids):
                    keep.append((addr, name, self._selected_plugin))
            else:
                cls = matching_device(name, uuids)
                if cls is not None:
                    keep.append((addr, name, cls))
        log.info("Pairing: scan complete — %d raw, %d matched%s", len(results),
                 len(keep), f" ({self._selected_plugin.id})"
                 if self._selected_plugin else "")
        GLib.idle_add(self._render_results, keep)

    def _render_results(self, devices: list[tuple[str, str, type]]) -> None:
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
            row._vitals_address = addr
            row._vitals_name = name
            row._vitals_cls = cls
            self._listbox.append(row)
        self._show_page("results")

    def _on_row_activated(self, _listbox, row: Gtk.ListBoxRow) -> None:
        addr = getattr(row, "_vitals_address", None)
        name = getattr(row, "_vitals_name", "")
        cls = getattr(row, "_vitals_cls", None)
        if addr is None or cls is None:
            return

        self._connecting_label.set_text(
            f"Connecting to {name or cls.display_name}…")
        self._show_page("connecting")

        async def do_connect():
            instance = cls(address=addr, name=name)
            await instance.connect()
            try:
                recovery = await instance.is_in_recovery()
            except Exception:
                recovery = None
            await instance.disconnect()
            return cls.id, bool(recovery)

        future = self._ble.submit(do_connect())

        def done(f):
            try:
                device_type, recovery = f.result()
            except Exception as e:
                GLib.idle_add(self._show_error, f"Could not connect: {e}")
                return
            GLib.idle_add(self._finish, addr, name, device_type, recovery)

        future.add_done_callback(done)

    def _finish(self, addr: str, name: str, device_type: str,
                recovery: bool = False) -> None:
        self._on_paired(addr, name, device_type, recovery)
        self.close()

    def _show_error(self, message: str) -> None:
        parent = self.get_root()
        if parent is not None and hasattr(parent, "show_toast"):
            parent.show_toast(message)
        self._show_page("results"
                        if self._listbox.get_first_child() else "empty")
