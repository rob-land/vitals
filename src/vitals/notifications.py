"""Desktop-notification capture for watch forwarding.

``NotificationMonitor`` opens a *monitor* connection to the session bus
(``org.freedesktop.DBus.Monitoring.BecomeMonitor``) and watches
``org.freedesktop.Notifications.Notify`` method calls — the same
stream phosh/GNOME Shell renders as banners. Each call is parsed,
filtered and de-duplicated into a ``WatchNotification``, then emitted
as a GObject signal on the main thread; the DeviceManager fans it out
to every watch kept connected for forwarding.

A monitor connection is read-only and private, so it never interferes
with the app's normal bus connection. Parsing and de-duplication are
pure and unit-tested; only the thin D-Bus plumbing needs a session bus.
"""

from __future__ import annotations

import html
import logging
import re
import time

from gi.repository import Gio, GLib, GObject

from vitals.devices.base import WatchNotification

log = logging.getLogger(__name__)

_MATCH_RULE = ("type='method_call',"
               "interface='org.freedesktop.Notifications',member='Notify'")

# Body text may carry Pango-ish markup (<b>, <a href=…>); watches want
# plain text.
_TAG_RE = re.compile(r"<[^>]+>")

# Repeated identical banners inside this window are one event (apps
# love to re-Notify on state changes).
_DEDUP_WINDOW_S = 3.0


def strip_markup(text: str) -> str:
    return html.unescape(_TAG_RE.sub("", text or "")).strip()


def parse_notify(args, counter, own_app_names=frozenset()) -> WatchNotification | None:
    """One ``Notify`` argument tuple → a ``WatchNotification``, or None
    for banners a watch shouldn't relay: our own toasts, transient
    hints, and empty summaries. ``counter`` supplies ids for calls that
    don't replace an earlier banner."""
    try:
        app_name, replaces_id, _icon, summary, body, _actions, hints, _to = args
    except (TypeError, ValueError):
        return None
    if not summary or app_name in own_app_names:
        return None
    hints = hints or {}
    if hints.get("transient"):
        return None
    return WatchNotification(
        id=int(replaces_id) or counter,
        app_name=str(app_name or ""),
        title=strip_markup(str(summary)),
        body=strip_markup(str(body or "")),
        timestamp=time.time(),
    )


class NotificationDeduper:
    """Drops identical (app, title, body) repeats within a short window."""

    def __init__(self, window_s: float = _DEDUP_WINDOW_S):
        self._window = window_s
        self._seen: dict[tuple, float] = {}

    def is_duplicate(self, note: WatchNotification, now: float) -> bool:
        self._seen = {k: t for k, t in self._seen.items()
                      if now - t < self._window}
        key = (note.app_name, note.title, note.body)
        if key in self._seen:
            return True
        self._seen[key] = now
        return False


class NotificationMonitor(GObject.Object):
    __gsignals__ = {
        # One forwardable WatchNotification, delivered on the main thread.
        "notification": (GObject.SignalFlags.RUN_FIRST, None, (object,)),
    }

    def __init__(self, own_app_names=("Vitals",)):
        super().__init__()
        self._own = frozenset(own_app_names)
        self._conn: Gio.DBusConnection | None = None
        self._dedup = NotificationDeduper()
        self._counter = 0

    @property
    def running(self) -> bool:
        return self._conn is not None

    def start(self) -> bool:
        if self._conn is not None:
            return True
        try:
            address = Gio.dbus_address_get_for_bus_sync(
                Gio.BusType.SESSION, None)
            conn = Gio.DBusConnection.new_for_address_sync(
                address,
                Gio.DBusConnectionFlags.AUTHENTICATION_CLIENT
                | Gio.DBusConnectionFlags.MESSAGE_BUS_CONNECTION,
                None, None)
            conn.call_sync(
                "org.freedesktop.DBus", "/org/freedesktop/DBus",
                "org.freedesktop.DBus.Monitoring", "BecomeMonitor",
                GLib.Variant("(asu)", ([_MATCH_RULE], 0)),
                None, Gio.DBusCallFlags.NONE, -1, None)
            conn.add_filter(self._on_message)
        except GLib.Error:
            log.exception("notification monitor: could not start")
            return False
        self._conn = conn
        log.info("notification monitor: started")
        return True

    def stop(self) -> None:
        if self._conn is None:
            return
        try:
            self._conn.close_sync(None)
        except GLib.Error:
            pass
        self._conn = None
        log.info("notification monitor: stopped")

    # Runs on GDBus's worker thread — unpack and hop to the main thread.
    def _on_message(self, _conn, message, incoming):
        if (incoming
                and message.get_message_type() == Gio.DBusMessageType.METHOD_CALL
                and message.get_interface() == "org.freedesktop.Notifications"
                and message.get_member() == "Notify"):
            body = message.get_body()
            if body is not None:
                GLib.idle_add(self._deliver, body.unpack())
        return message

    def _deliver(self, args) -> bool:
        self._counter = (self._counter + 1) % 0x7FFFFFFF
        note = parse_notify(args, self._counter, self._own)
        if note is not None and not self._dedup.is_duplicate(note, time.time()):
            self.emit("notification", note)
        return GLib.SOURCE_REMOVE
