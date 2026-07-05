"""Watch app/watchface store browser.

Family-agnostic: it asks the paired device's plugin for its
`app_store()` (Pebble's Rebble catalogue, Bangle's App Loader), lists
watchfaces/apps, and on selection downloads the bundle and hands it to
`device.install_app`. All network + watch work runs on the BleManager;
results marshal back via GLib.idle_add, like the other dialogs.
"""

from __future__ import annotations

import logging
import threading

from gi.repository import Adw, Gdk, GdkPixbuf, GLib, Gtk

from vitals.ble import BleManager
from vitals.devices.base import available_devices

log = logging.getLogger(__name__)

_KINDS = [("Watch Faces", "watchface"), ("Apps", "watchapp")]
_THUMB_SIZE = 48


class AppStoreDialog(Adw.Dialog):
    __gtype_name__ = "VitalsAppStoreDialog"

    def __init__(self, ble: BleManager, entry):
        super().__init__()
        self._ble = ble
        self._entry = entry
        self._closed = False
        self.connect("closed", lambda *_: setattr(self, "_closed", True))

        self._addr = entry.address
        self._device_type = entry.kind
        self._plugin = available_devices().get(self._device_type)
        self._store = (self._plugin.app_store()
                       if self._plugin and self._plugin.SUPPORTS_APP_INSTALL
                       else None)
        self._kind = "watchface"
        self._gen = 0
        self._thumb_cache: dict[str, Gdk.Texture] = {}

        self.set_title("Watch Store")
        self.set_content_width(400)
        self.set_content_height(560)

        toolbar = Adw.ToolbarView()
        toolbar.add_top_bar(Adw.HeaderBar())
        self._stack = Gtk.Stack()
        self._stack.set_transition_type(Gtk.StackTransitionType.CROSSFADE)
        toolbar.set_content(self._stack)
        self.set_child(toolbar)

        self._build_browse_page()
        self._build_busy_page()
        self._build_result_page()

        if self._store is None:
            self._show_result(
                False, "Not available",
                "Pair a watch that supports installing apps first.")
        else:
            self._reload()

    # ── Pages ─────────────────────────────────────────────────────

    def _build_browse_page(self) -> None:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        box.set_margin_top(12)
        box.set_margin_bottom(12)
        box.set_margin_start(12)
        box.set_margin_end(12)

        switcher = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL,
                           halign=Gtk.Align.CENTER)
        switcher.add_css_class("linked")
        self._kind_buttons: list[Gtk.ToggleButton] = []
        first: Gtk.ToggleButton | None = None
        for label, kind in _KINDS:
            btn = Gtk.ToggleButton(label=label)
            if first is None:
                first = btn
                btn.set_active(True)
            else:
                btn.set_group(first)
            btn.connect("toggled", self._on_kind_toggled, kind)
            switcher.append(btn)
            self._kind_buttons.append(btn)
        box.append(switcher)

        self._search = Gtk.SearchEntry()
        self._search.set_placeholder_text("Search…")
        self._search.connect("activate", lambda *_: self._reload())
        box.append(self._search)

        scroller = Gtk.ScrolledWindow()
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroller.set_vexpand(True)
        self._listbox = Gtk.ListBox()
        self._listbox.set_selection_mode(Gtk.SelectionMode.NONE)
        self._listbox.add_css_class("boxed-list")
        self._listbox.connect("row-activated", self._on_row_activated)
        scroller.set_child(self._listbox)
        box.append(scroller)

        self._stack.add_named(box, "browse")

        spinner_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12,
                              valign=Gtk.Align.CENTER, vexpand=True)
        spinner_box.append(Adw.Spinner())
        self._stack.add_named(spinner_box, "loading")

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
        button = Gtk.Button(label="Done", halign=Gtk.Align.CENTER)
        button.add_css_class("pill")
        button.connect("clicked", lambda *_: self.close())
        self._result_page.set_child(button)
        self._stack.add_named(self._result_page, "result")

    # ── Browse ────────────────────────────────────────────────────

    def _on_kind_toggled(self, button: Gtk.ToggleButton, kind: str) -> None:
        if button.get_active():
            self._kind = kind
            self._reload()

    def _reload(self) -> None:
        if self._store is None:
            return
        self._gen += 1
        gen = self._gen
        self._stack.set_visible_child_name("loading")
        query = self._search.get_text() if hasattr(self, "_search") else ""
        future = self._ble.submit(self._store.list_apps(self._kind, query))
        future.add_done_callback(lambda f: self._list_done(f, gen))

    def _list_done(self, future, gen: int) -> None:
        if gen != self._gen:
            return
        try:
            apps = future.result()
        except Exception as exc:  # noqa: BLE001
            log.exception("Store: listing failed")
            GLib.idle_add(self._show_result, False, "Store unavailable",
                          f"Couldn't reach the store.\n\n{exc}")
            return
        GLib.idle_add(self._render_list, gen, apps)

    def _render_list(self, gen: int, apps: list) -> bool:
        if gen != self._gen or self._closed:
            return False
        child = self._listbox.get_first_child()
        while child:
            self._listbox.remove(child)
            child = self._listbox.get_first_child()
        for app in apps:
            row = Adw.ActionRow(activatable=True, title=app.name,
                                subtitle=(app.author or app.description or ""))
            thumb_url = app.screenshot_url or app.icon_url
            if thumb_url:
                image = Gtk.Image()
                image.set_size_request(_THUMB_SIZE, _THUMB_SIZE)
                image.set_valign(Gtk.Align.CENTER)
                row.add_prefix(image)
                self._load_thumbnail(gen, image, thumb_url)
            row.add_suffix(Gtk.Image.new_from_icon_name(
                "folder-download-symbolic"))
            row._tock_app = app
            self._listbox.append(row)
        self._stack.set_visible_child_name("browse")
        return False

    # ── Thumbnails ────────────────────────────────────────────────

    def _load_thumbnail(self, gen: int, image: Gtk.Image, url: str) -> None:
        cached = self._thumb_cache.get(url)
        if cached is not None:
            image.set_from_paintable(cached)
            return

        def work() -> None:
            try:
                pixbuf = _decode_thumbnail(_fetch_image_bytes(url), _THUMB_SIZE)
            except Exception:
                return
            if pixbuf is not None:
                GLib.idle_add(self._apply_thumbnail, gen, image, url, pixbuf)

        threading.Thread(target=work, name="tock-thumb", daemon=True).start()

    def _apply_thumbnail(self, gen: int, image: Gtk.Image, url: str,
                         pixbuf) -> bool:
        if gen != self._gen or self._closed:
            return False
        texture = _pixbuf_to_texture(pixbuf)
        self._thumb_cache[url] = texture
        image.set_from_paintable(texture)
        return False

    # ── Install ───────────────────────────────────────────────────

    def _on_row_activated(self, _listbox, row: Gtk.ListBoxRow) -> None:
        app = getattr(row, "_tock_app", None)
        if app is None or self._plugin is None:
            return
        device = self._plugin(
            address=self._addr,
            name=self._entry.name)

        self.set_can_close(False)
        self._set_busy(f"Downloading {app.name}…", None)
        self._stack.set_visible_child_name("busy")

        async def do_install() -> str:
            bundle = await self._store.download(app)
            GLib.idle_add(self._set_busy,
                          "Connecting — confirm on the watch if asked…", None)
            await device.connect()
            try:
                await device.install_app(bundle, on_progress=self._on_progress)
            finally:
                await device.disconnect()
            return app.name

        future = self._ble.submit(do_install())
        future.add_done_callback(self._install_done)

    def _on_progress(self, stage: str, done: int, total: int) -> None:
        fraction = (done / total) if total else 0.0
        GLib.idle_add(self._set_busy, "Installing…", fraction)

    def _install_done(self, future) -> None:
        try:
            name = future.result()
        except Exception as exc:  # noqa: BLE001
            log.exception("Store: install failed")
            GLib.idle_add(self._show_result, False, "Install Failed", str(exc))
            return
        GLib.idle_add(self._show_result, True, "Installed",
                      f"{name} is now on your watch.")

    # ── Helpers ───────────────────────────────────────────────────

    def _set_busy(self, text: str, fraction: float | None) -> None:
        if self._closed:
            return
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


def _fetch_image_bytes(url: str, timeout: float = 15.0) -> bytes:
    import urllib.request
    req = urllib.request.Request(url, headers={"User-Agent": "tock"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def _decode_thumbnail(data: bytes, size: int):
    """Decode image bytes and scale to a `size`-px thumbnail (aspect
    preserved). Runs in a worker thread; returns a GdkPixbuf or None."""
    loader = GdkPixbuf.PixbufLoader()
    loader.write(data)
    loader.close()
    pixbuf = loader.get_pixbuf()
    if pixbuf is None:
        return None
    width, height = pixbuf.get_width(), pixbuf.get_height()
    if width <= 0 or height <= 0:
        return None
    scale = size / max(width, height)
    return pixbuf.scale_simple(max(1, round(width * scale)),
                               max(1, round(height * scale)),
                               GdkPixbuf.InterpType.BILINEAR)


def _pixbuf_to_texture(pixbuf) -> Gdk.Texture:
    """Wrap a GdkPixbuf in a Gdk.MemoryTexture (the non-deprecated path)."""
    fmt = (Gdk.MemoryFormat.R8G8B8A8 if pixbuf.get_has_alpha()
           else Gdk.MemoryFormat.R8G8B8)
    return Gdk.MemoryTexture.new(
        pixbuf.get_width(), pixbuf.get_height(), fmt,
        GLib.Bytes.new(pixbuf.get_pixels()), pixbuf.get_rowstride())
