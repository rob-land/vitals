"""DeviceManager — the multi-device registry and sync orchestrator.

Replaces tock's single ``paired-device-*`` GSettings triple with rows in
the health database's ``devices`` table, and generalises its global
``_syncing`` flag into per-device busy state. One BleManager loop serves
every device: each sync is its own coroutine; families whose transport
can only exist once per process (``EXCLUSIVE_TRANSPORT``) serialize on a
named lock, and a small semaphore keeps simultaneous BlueZ *connect
attempts* bounded (connects are what upset it — open links coexist
fine).

Threading: registry reads/writes happen on the GTK main thread (the
SQLite connection is thread-affine); sync coroutines run on the BLE
loop and marshal their results back with ``GLib.idle_add``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field

from gi.repository import GLib, GObject

from vitals.devices.base import Device, available_devices
from vitals.ingest import (
    HealthSample, SleepSample, WorkoutSample, build_records,
    build_sleep_record, build_workout_record)

log = logging.getLogger(__name__)

ROLE_WATCH = "watch"
ROLE_SENSOR = "sensor"

# Concurrent BLE connect attempts (not open connections).
_MAX_CONCURRENT_CONNECTS = 2


@dataclass
class DeviceEntry:
    """One registered device: a `devices` table row plus runtime state."""

    address: str
    name: str
    kind: str
    role: str
    enabled: bool
    settings: dict = field(default_factory=dict)
    last_sync_ms: int | None = None
    last_battery: int | None = None
    state: str = "idle"   # idle | syncing | flashing | error

    @property
    def plugin(self) -> type[Device] | None:
        return available_devices().get(self.kind)

    @property
    def busy(self) -> bool:
        return self.state in ("syncing", "flashing")


class DeviceManager(GObject.Object):
    __gsignals__ = {
        # The set of registered devices changed (add/forget/rename).
        "device-list-changed": (GObject.SignalFlags.RUN_FIRST, None, ()),
        # (address, state) — runtime state transitions for one device.
        "device-state-changed": (GObject.SignalFlags.RUN_FIRST, None, (str, str)),
        # (address, message) — a sync finished; message is toast-ready.
        "device-synced": (GObject.SignalFlags.RUN_FIRST, None, (str, str)),
    }

    def __init__(self, store, recorder, settings, ble):
        super().__init__()
        self._store = store
        self._recorder = recorder
        self._settings = settings
        self._ble = ble
        self._entries: dict[str, DeviceEntry] = {}
        # Named asyncio locks for EXCLUSIVE_TRANSPORT families and the
        # connect semaphore — created lazily *on the BLE loop*.
        self._transport_locks: dict[str, asyncio.Lock] = {}
        self._connect_sem: asyncio.Semaphore | None = None
        self._bg_sync_id: int | None = None
        self._load()

    # ── registry (main thread) ────────────────────────────────────
    def _load(self) -> None:
        rows = self._store.connection.execute(
            "SELECT * FROM devices ORDER BY created_at").fetchall()
        self._entries = {
            r["address"]: DeviceEntry(
                address=r["address"], name=r["name"], kind=r["kind"],
                role=r["role"], enabled=bool(r["enabled"]),
                settings=json.loads(r["settings_json"] or "{}"),
                last_sync_ms=r["last_sync_ms"], last_battery=r["last_battery"])
            for r in rows}

    def list(self) -> list[DeviceEntry]:
        return list(self._entries.values())

    def get(self, address: str) -> DeviceEntry | None:
        return self._entries.get(address)

    def add(self, address: str, name: str, kind: str) -> DeviceEntry:
        plugin = available_devices()[kind]
        role = (ROLE_SENSOR if plugin.INTERACTION == "opportunistic"
                else ROLE_WATCH)
        with self._store.connection as con:
            con.execute(
                "INSERT OR REPLACE INTO devices"
                "(address, name, kind, role, enabled, settings_json, created_at) "
                "VALUES (?,?,?,?,1,?,?)",
                (address, name, kind, role, "{}", round(time.time() * 1000)))
        self._load()
        self.emit("device-list-changed")
        return self._entries[address]

    def forget(self, address: str) -> None:
        with self._store.connection as con:
            con.execute("DELETE FROM devices WHERE address=?", (address,))
        self._entries.pop(address, None)
        self.emit("device-list-changed")

    def set_enabled(self, address: str, enabled: bool) -> None:
        with self._store.connection as con:
            con.execute("UPDATE devices SET enabled=? WHERE address=?",
                        (1 if enabled else 0, address))
        entry = self._entries.get(address)
        if entry:
            entry.enabled = enabled
        self.emit("device-list-changed")

    def update_settings(self, address: str, updates: dict) -> None:
        entry = self._entries[address]
        entry.settings.update(updates)
        with self._store.connection as con:
            con.execute("UPDATE devices SET settings_json=? WHERE address=?",
                        (json.dumps(entry.settings), address))

    def _set_state(self, address: str, state: str) -> None:
        entry = self._entries.get(address)
        if entry is None:
            return
        entry.state = state
        self.emit("device-state-changed", address, state)

    # ── sync (BLE loop) ───────────────────────────────────────────
    def sync_device(self, address: str) -> bool:
        """Kick off one sync. Returns False if it couldn't start."""
        entry = self._entries.get(address)
        if entry is None or entry.busy or entry.role != ROLE_WATCH:
            return False
        plugin = entry.plugin
        if plugin is None:
            self.emit("device-synced", address,
                      f"No plugin for device type '{entry.kind}'")
            return False

        device = plugin(address=entry.address, name=entry.name)
        sync_time = self._settings.get_boolean("sync-time-on-connect")
        alarms = entry.settings.get("alarms", [])
        previously_pushed = set(entry.settings.get("pushed_alarm_ids", []))
        push_alarms = bool(alarms) or bool(previously_pushed)

        self._set_state(address, "syncing")
        future = self._ble.submit(self._run_sync(
            device, plugin, sync_time, push_alarms, alarms, previously_pushed))
        future.add_done_callback(
            lambda f: GLib.idle_add(self._finish_sync, entry, f))
        return True

    async def _guarded(self, coro_factory, transport: str | None):
        if transport is None:
            return await coro_factory()
        lock = self._transport_locks.get(transport)
        if lock is None:
            lock = self._transport_locks[transport] = asyncio.Lock()
        async with lock:
            return await coro_factory()

    async def _run_sync(self, device, plugin, sync_time, push_alarms,
                        alarms, previously_pushed) -> dict:
        return await self._guarded(
            lambda: self._sync_pipeline(device, plugin, sync_time,
                                        push_alarms, alarms,
                                        previously_pushed),
            plugin.EXCLUSIVE_TRANSPORT)

    async def _sync_pipeline(self, device, plugin, sync_time, push_alarms,
                             alarms, previously_pushed) -> dict:
        """One full watch sync: connect → time → alarms → battery →
        health reads → sync() → disconnect → ingest."""
        result: dict = {"battery": None, "warnings": [], "pushed_ids": None}
        if self._connect_sem is None:
            self._connect_sem = asyncio.Semaphore(_MAX_CONCURRENT_CONNECTS)
        try:
            # Inside the try so a connect that fails after partially
            # setting up (e.g. a GATT server registered, then the link
            # drops) still tears down — an orphaned transport would
            # compete for the watch on the next sync.
            async with self._connect_sem:
                await device.connect()
            if sync_time and plugin.SUPPORTS_TIME_SYNC:
                await device.sync_time(time.time())
            elif sync_time:
                result["warnings"].append("time sync not supported")
            if push_alarms and plugin.SUPPORTS_ALARM_PUSH:
                result["pushed_ids"] = await device.push_alarms(
                    alarms, previously_pushed_ids=previously_pushed)
            elif push_alarms:
                result["warnings"].append("alarm push not supported")
            result["battery"] = await device.get_battery()

            activity = activity_series = hr_samples = None
            sleep_series = workout_series = None
            if plugin.SUPPORTS_ACTIVITY_READ:
                # Streaming sources (Pebble) return per-minute deltas;
                # cumulative sources (Bangle/PineTime) return None here
                # and the single snapshot is used instead.
                activity_series = await device.get_activity_series()
                activity = await device.get_activity()
                hr_samples = await device.get_heart_rate_samples()
            if plugin.SUPPORTS_SLEEP_READ:
                sleep_series = await device.get_sleep_series()
            if plugin.SUPPORTS_WORKOUT_READ:
                workout_series = await device.get_workout_series()
            await device.sync()
        finally:
            await device.disconnect()

        # Ingest after disconnecting so a slow write never holds the
        # BLE link open. The Recorder marshals to the main thread.
        readings = activity_series if activity_series is not None else (
            [activity] if activity else [])
        envelopes: list[dict] = []
        for reading in list(readings) + list(hr_samples or []):
            envelopes.extend(build_records(HealthSample(
                device_address=device.address, device_name=device.name,
                reading=reading)))
        for session in sleep_series or []:
            envelopes.append(build_sleep_record(SleepSample(
                device_address=device.address, device_name=device.name,
                session=session)))
        for workout in workout_series or []:
            envelopes.append(build_workout_record(WorkoutSample(
                device_address=device.address, device_name=device.name,
                workout=workout)))
        if envelopes:
            self._recorder.ingest_from_thread(envelopes)
        result["records"] = len(envelopes)
        return result

    def _finish_sync(self, entry: DeviceEntry, future) -> bool:
        try:
            result = future.result()
        except Exception as exc:
            log.exception("sync failed: %s", entry.address)
            self._set_state(entry.address, "error")
            self.emit("device-synced", entry.address, f"Sync failed: {exc}")
            return GLib.SOURCE_REMOVE

        battery = result.get("battery")
        updates: dict = {}
        if result.get("pushed_ids") is not None:
            updates["pushed_alarm_ids"] = sorted(result["pushed_ids"])
        if updates:
            self.update_settings(entry.address, updates)
        entry.last_sync_ms = round(time.time() * 1000)
        entry.last_battery = battery
        with self._store.connection as con:
            con.execute(
                "UPDATE devices SET last_sync_ms=?, last_battery=? "
                "WHERE address=?",
                (entry.last_sync_ms, battery, entry.address))

        message = "Sync complete"
        if battery is not None:
            message += f" · battery {battery}%"
        if result.get("warnings"):
            message += " · " + "; ".join(result["warnings"])
        self._set_state(entry.address, "idle")
        self.emit("device-synced", entry.address, message)
        return GLib.SOURCE_REMOVE

    def sync_all_enabled(self) -> int:
        """Sync every enabled watch (used by the background timer)."""
        started = 0
        for entry in self._entries.values():
            if entry.enabled and entry.role == ROLE_WATCH and not entry.busy:
                if self.sync_device(entry.address):
                    started += 1
        return started

    # ── background timer (main thread) ────────────────────────────
    def reschedule_background_sync(self) -> None:
        """(Re)arm the periodic sync timer from the configured interval.
        Idempotent; 0 minutes disables it."""
        if self._bg_sync_id is not None:
            GLib.source_remove(self._bg_sync_id)
            self._bg_sync_id = None
        minutes = self._settings.get_int("background-sync-interval")
        if minutes > 0:
            self._bg_sync_id = GLib.timeout_add_seconds(
                minutes * 60, self._on_tick)
            log.info("background sync: every %d min", minutes)

    def _on_tick(self) -> bool:
        started = self.sync_all_enabled()
        if started:
            log.info("background sync: started %d device(s)", started)
        return GLib.SOURCE_CONTINUE
