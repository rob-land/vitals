"""Bluetooth adapter state — queried from BlueZ over the system bus.

Vitals relies on a powered BLE adapter; when Bluetooth is turned off in
GNOME Settings (or the host has no adapter at all) every scan and
connect attempt fails with a generic "Operation failed" error from
bleak. Catching that after the fact still leaves the UI mute on open,
so we proactively check `org.bluez.Adapter1.Powered` over D-Bus and
expose a `state-changed` signal the window can bind a banner to.

We use Gio.DBusConnection rather than dbus_fast (a bleak sub-dep)
because GLib's bus integrates with the GTK main loop natively — no
asyncio bridging needed for a few property lookups and one signal
subscription.
"""

from __future__ import annotations

import enum
import logging

from gi.repository import Gio, GLib, GObject

log = logging.getLogger(__name__)

_BLUEZ        = "org.bluez"
_ADAPTER_IFACE = "org.bluez.Adapter1"
_OBJ_MANAGER  = "org.freedesktop.DBus.ObjectManager"
_PROPS_IFACE  = "org.freedesktop.DBus.Properties"


class BluetoothState(enum.Enum):
    UNAVAILABLE  = "unavailable"
    POWERED_OFF  = "powered-off"
    POWERED_ON   = "powered-on"


class BluetoothMonitor(GObject.GObject):
    """Tracks the system's BLE adapter state and emits on change.

    Owned by the Application. Constructed and `start()`ed on the GTK
    main thread; `stop()`ped at shutdown. The `state-changed` signal
    fires on the main loop whenever the cached state flips."""

    __gsignals__ = {
        "state-changed": (GObject.SignalFlags.RUN_FIRST, None,
                          (object,)),
    }

    def __init__(self):
        super().__init__()
        self._state: BluetoothState = BluetoothState.UNAVAILABLE
        self._bus: Gio.DBusConnection | None = None
        # D-Bus signal subscription tokens, kept so we can unsubscribe.
        self._sub_ids: list[int] = []

    @property
    def state(self) -> BluetoothState:
        return self._state

    def start(self) -> None:
        if self._bus is not None:
            return
        try:
            self._bus = Gio.bus_get_sync(Gio.BusType.SYSTEM, None)
        except GLib.Error:
            log.exception("BluetoothMonitor: failed to get system bus")
            return
        self._sub_ids.append(self._bus.signal_subscribe(
            _BLUEZ, _PROPS_IFACE, "PropertiesChanged", None,
            _ADAPTER_IFACE, Gio.DBusSignalFlags.NONE,
            self._on_properties_changed))
        self._sub_ids.append(self._bus.signal_subscribe(
            _BLUEZ, _OBJ_MANAGER, "InterfacesAdded", None, None,
            Gio.DBusSignalFlags.NONE, self._on_interfaces_changed))
        self._sub_ids.append(self._bus.signal_subscribe(
            _BLUEZ, _OBJ_MANAGER, "InterfacesRemoved", None, None,
            Gio.DBusSignalFlags.NONE, self._on_interfaces_changed))
        self._refresh()

    def stop(self) -> None:
        if self._bus is None:
            return
        for sid in self._sub_ids:
            self._bus.signal_unsubscribe(sid)
        self._sub_ids.clear()
        self._bus = None

    def power_on(self) -> bool:
        """Ensure the BLE adapter is powered (set ``Adapter1.Powered``).

        Some hosts — notably Halium phones — power the controller down
        when nothing is scanning or connected, so an otherwise-idle
        Vitals would see every timed sync fail with "no powered adapter".
        Vitals powers it back up before syncing rather than depending on
        the user to. Returns True if the adapter is (now) powered.
        Requires ``start()`` first; safe to call from the main thread.
        """
        if self._bus is None:
            return False
        path = self._adapter_path()
        if path is None:
            return False
        try:
            self._bus.call_sync(
                _BLUEZ, path, _PROPS_IFACE, "Set",
                GLib.Variant("(ssv)", (_ADAPTER_IFACE, "Powered",
                                       GLib.Variant("b", True))),
                None, Gio.DBusCallFlags.NONE, 3000, None)
            return True
        except GLib.Error:
            log.exception("BluetoothMonitor: could not power on the adapter")
            return False

    # ── Internal ──────────────────────────────────────────────────

    def _adapter_path(self) -> str | None:
        """The object path of the first BLE adapter, or None."""
        if self._bus is None:
            return None
        try:
            reply = self._bus.call_sync(
                _BLUEZ, "/", _OBJ_MANAGER, "GetManagedObjects", None,
                GLib.VariantType("(a{oa{sa{sv}}})"),
                Gio.DBusCallFlags.NONE, 2000, None)
        except GLib.Error:
            return None
        for path, ifaces in reply.unpack()[0].items():
            if _ADAPTER_IFACE in ifaces:
                return path
        return None

    def _refresh(self) -> None:
        new_state = self._query_state()
        if new_state != self._state:
            log.info("BluetoothMonitor: %s -> %s",
                     self._state.value, new_state.value)
            self._state = new_state
            self.emit("state-changed", new_state)

    def _query_state(self) -> BluetoothState:
        if self._bus is None:
            return BluetoothState.UNAVAILABLE
        try:
            reply = self._bus.call_sync(
                _BLUEZ, "/", _OBJ_MANAGER, "GetManagedObjects",
                None, GLib.VariantType("(a{oa{sa{sv}}})"),
                Gio.DBusCallFlags.NONE, 2000, None)
        except GLib.Error as e:
            # ServiceUnknown means bluez isn't running (or isn't
            # installed); treat both as "no Bluetooth".
            log.debug("BluetoothMonitor: GetManagedObjects failed: %s", e)
            return BluetoothState.UNAVAILABLE
        objects = reply.unpack()[0]
        found_adapter = False
        for _path, ifaces in objects.items():
            adapter_props = ifaces.get(_ADAPTER_IFACE)
            if adapter_props is None:
                continue
            found_adapter = True
            if adapter_props.get("Powered"):
                return BluetoothState.POWERED_ON
        return (BluetoothState.POWERED_OFF if found_adapter
                else BluetoothState.UNAVAILABLE)

    def _on_properties_changed(self, _conn, _sender, _path, _iface,
                               _signal, _params) -> None:
        # We filtered to Adapter1 in the subscription, so any signal
        # here is a candidate state change — just re-query.
        self._refresh()

    def _on_interfaces_changed(self, _conn, _sender, _path, _iface,
                               _signal, _params) -> None:
        self._refresh()
