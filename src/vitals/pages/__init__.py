"""Dashboard pages and their shared base.

``PulsePage`` is a vertical box wrapping a Stack that swaps between the
page's real content and a shared "Pulse isn't running" status screen, so
every page degrades the same way when the daemon is unreachable.
"""

from __future__ import annotations

from gi.repository import Adw, GLib, Gtk


class PulsePage(Gtk.Box):
    def __init__(self):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self._stack = Gtk.Stack(
            vexpand=True, hexpand=True,
            transition_type=Gtk.StackTransitionType.CROSSFADE)
        self.append(self._stack)
        self._stack.add_named(
            Adw.StatusPage(
                icon_name="network-offline-symbolic",
                title="Pulse isn’t running",
                description="Start the Pulse daemon to view your health data."),
            "unavailable")

    def _set_content(self, widget: Gtk.Widget) -> None:
        self._stack.add_named(widget, "content")

    def _show_content(self) -> None:
        self._stack.set_visible_child_name("content")

    def _show_unavailable(self) -> None:
        self._stack.set_visible_child_name("unavailable")

    def _toast(self, message: str) -> None:
        self.activate_action("win.toast", GLib.Variant("s", message))
