"""GDBus client for the Pulse daemon (``land.rob.pulse``).

Wraps the Health (read/aggregate/grants) and Admin (grant/revoke/list)
interfaces. Calls are synchronous GDBus on the main loop — local D-Bus
round-trips are sub-millisecond, so a dashboard refresh can call these
directly without a worker thread. Every call raises ``PulseUnavailable``
when the daemon can't be reached, which the pages turn into a friendly
empty state.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone

from gi.repository import Gio, GLib

log = logging.getLogger(__name__)

BUS_NAME = "land.rob.pulse"
HEALTH_PATH, HEALTH_IFACE = "/land/rob/pulse/Health", "land.rob.pulse.Health"
ADMIN_PATH, ADMIN_IFACE = "/land/rob/pulse/Admin", "land.rob.pulse.Admin"
_TIMEOUT_MS = 4000


class PulseUnavailable(Exception):
    """The Pulse daemon could not be reached."""


class PulseClient:
    def __init__(self):
        self._bus: Gio.DBusConnection | None = None

    def _bus_get(self) -> Gio.DBusConnection:
        if self._bus is None:
            try:
                self._bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
            except GLib.Error as exc:
                raise PulseUnavailable(f"no session bus: {exc.message}") from exc
        return self._bus

    def _call(self, path, iface, method, params, reply_sig):
        try:
            return self._bus_get().call_sync(
                BUS_NAME, path, iface, method, params,
                GLib.VariantType(reply_sig) if reply_sig else None,
                Gio.DBusCallFlags.NONE, _TIMEOUT_MS, None)
        except GLib.Error as exc:
            raise PulseUnavailable(exc.message) from exc

    def _json_call(self, method, payload, path=HEALTH_PATH, iface=HEALTH_IFACE):
        params = GLib.Variant("(s)", (json.dumps(payload),)) if payload is not None else None
        return json.loads(self._call(path, iface, method, params, "(s)").unpack()[0])

    # ── Health (read) ─────────────────────────────────────────────
    def available(self) -> bool:
        try:
            self.list_types()
            return True
        except PulseUnavailable:
            return False

    def list_types(self) -> dict:
        return self._json_call("ListTypes", None)

    def read_records(self, query: dict) -> dict:
        return self._json_call("ReadRecords", query)

    def aggregate(self, query: dict) -> dict:
        return self._json_call("Aggregate", query)

    def get_grants(self) -> dict:
        return self._json_call("GetGrants", None)

    def latest(self, type_key: str, within_days: int = 30) -> dict | None:
        """Most recent record of a type within a recent window, or None.

        Pulse returns records oldest-first, so the last element of a recent
        window is the newest reading.
        """
        start = (datetime.now(timezone.utc) - timedelta(days=within_days)).isoformat()
        records = self.read_records({"types": [type_key], "start": start}).get("records", [])
        return records[-1] if records else None

    # ── Admin (consent management) ────────────────────────────────
    def list_grants(self) -> list[dict]:
        return self._json_call("ListGrants", None, path=ADMIN_PATH, iface=ADMIN_IFACE)

    def grant(self, app_id: str, types, access: str, duration: int = 0) -> None:
        self._call(ADMIN_PATH, ADMIN_IFACE, "Grant",
                   GLib.Variant("(sassu)", (app_id, list(types), access, duration)), None)

    def revoke(self, app_id: str, types, access: str) -> None:
        self._call(ADMIN_PATH, ADMIN_IFACE, "Revoke",
                   GLib.Variant("(sass)", (app_id, list(types), access)), None)

    # ── Access requests (the consent prompt) ──────────────────────
    def list_requests(self) -> list[dict]:
        return self._json_call("ListRequests", None, path=ADMIN_PATH, iface=ADMIN_IFACE)

    def approve_request(self, app_id: str) -> None:
        self._call(ADMIN_PATH, ADMIN_IFACE, "ApproveRequest",
                   GLib.Variant("(s)", (app_id,)), None)

    def deny_request(self, app_id: str) -> None:
        self._call(ADMIN_PATH, ADMIN_IFACE, "DenyRequest",
                   GLib.Variant("(s)", (app_id,)), None)

    def subscribe_requests(self, callback) -> int:
        """Call ``callback()`` (on the main loop) whenever the pending
        request set changes. Returns a subscription id (0 if no bus)."""
        try:
            bus = self._bus_get()
        except PulseUnavailable:
            return 0
        return bus.signal_subscribe(
            BUS_NAME, ADMIN_IFACE, "RequestsChanged", ADMIN_PATH, None,
            Gio.DBusSignalFlags.NONE, lambda *_: callback())
