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
from vitals.devices.keeper import ConnectionKeeper
from vitals.ingest import (
    HealthSample, HydrationSample, SleepSample, WorkoutSample, build_records,
    build_hydration_record, build_sleep_record, build_workout_record)

log = logging.getLogger(__name__)

ROLE_WATCH = "watch"
ROLE_SENSOR = "sensor"

# Concurrent BLE connect attempts (not open connections).
_MAX_CONCURRENT_CONNECTS = 2

# Upper bound on calendar events pinned to a watch timeline per sync.
_MAX_CALENDAR_PINS = 50

# Sensor-quality tiers → numeric trust for cross-source resolution.
_QUALITY_SCORE = {"high": 30, "medium": 20, "low": 10}
_DEFAULT_QUALITY = _QUALITY_SCORE["medium"]
# A device the user pins for a metric wins outright over quality tiers.
_PREFERRED_BOOST = 100


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
        # The set of persistent links (notification forwarding) changed.
        "links-changed": (GObject.SignalFlags.RUN_FIRST, None, ()),
        # (address, connected) — a persistent link went up or down.
        "link-state-changed": (GObject.SignalFlags.RUN_FIRST, None,
                               (str, bool)),
        # (address, command) — a watch pressed a playback button.
        "music-command": (GObject.SignalFlags.RUN_FIRST, None, (str, str)),
    }

    def __init__(self, store, recorder, settings, ble, bluetooth=None):
        super().__init__()
        self._store = store
        self._recorder = recorder
        self._settings = settings
        self._ble = ble
        # Optional BluetoothMonitor: some hosts idle the controller off,
        # so we power it back on before a sync rather than failing.
        self._bluetooth = bluetooth
        self._entries: dict[str, DeviceEntry] = {}
        # Named asyncio locks for EXCLUSIVE_TRANSPORT families and the
        # connect semaphore — created lazily *on the BLE loop*.
        self._transport_locks: dict[str, asyncio.Lock] = {}
        self._connect_sem: asyncio.Semaphore | None = None
        self._bg_sync_id: int | None = None
        self._broker = None
        # Persistent links (ConnectionKeeper) for notification forwarding.
        self._keepers: dict[str, ConnectionKeeper] = {}
        # Sensor addresses currently mid-read (BLE loop only).
        self._sensor_busy: set[str] = set()
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

    def source_trust(self, metric: str) -> dict[str, int]:
        """Rank each source (keyed by device_id) for `metric` — how much
        to trust it when several devices report the same thing.

        Combines the plugin's declared sensor quality with any per-device
        user preference (``settings["preferred_metrics"]``). The empty
        string is the manual-entry / no-device source. The result feeds
        ``Store.aggregate(..., source_trust=…)`` so overlapping metrics
        resolve to one source instead of double-counting or blending.
        """
        trust: dict[str, int] = {"": _DEFAULT_QUALITY}
        for entry in self._entries.values():
            plugin = entry.plugin
            tier = plugin.SENSOR_QUALITY.get(metric) if plugin else None
            score = _QUALITY_SCORE.get(tier, _DEFAULT_QUALITY)
            if metric in entry.settings.get("preferred_metrics", []):
                score += _PREFERRED_BOOST
            trust[entry.address] = score
        return trust

    def contested_metrics(self, address: str) -> list[str]:
        """Record types this device produces that another source (another
        device, or manual entry) also produces — the metrics where a
        'prefer this device' choice actually changes the resolved value.
        """
        mine = set(self._store.types_for_device(address))
        if not mine:
            return []
        others: set[str] = set(self._store.types_for_device(""))
        for addr in self._entries:
            if addr != address:
                others |= set(self._store.types_for_device(addr))
        return sorted(mine & others)

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
        self.reconcile_links()
        return self._entries[address]

    def forget(self, address: str) -> None:
        with self._store.connection as con:
            con.execute("DELETE FROM devices WHERE address=?", (address,))
        self._entries.pop(address, None)
        self.emit("device-list-changed")
        self.reconcile_links()

    def set_enabled(self, address: str, enabled: bool) -> None:
        with self._store.connection as con:
            con.execute("UPDATE devices SET enabled=? WHERE address=?",
                        (1 if enabled else 0, address))
        entry = self._entries.get(address)
        if entry:
            entry.enabled = enabled
        self.emit("device-list-changed")
        self.reconcile_links()

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

    # ── persistent links (notification forwarding) ────────────────
    def _wants_link(self, entry: DeviceEntry) -> bool:
        plugin = entry.plugin
        return (entry.enabled and entry.role == ROLE_WATCH
                and plugin is not None and plugin.SUPPORTS_NOTIFICATIONS
                and bool(entry.settings.get("forward_notifications")))

    def reconcile_links(self) -> None:
        """Start/stop persistent watch links to match per-device
        settings. Idempotent; safe to call after any registry change."""
        if self._ble is None:
            return
        want = {a for a, e in self._entries.items() if self._wants_link(e)}
        have = set(self._keepers)
        if want == have:
            return
        for address in have - want:
            keeper = self._keepers.pop(address)
            self._ble.submit(keeper.stop())
        if want - have and self._bluetooth is not None:
            self._bluetooth.power_on()
        for address in want - have:
            entry = self._entries[address]
            device = entry.plugin(address=address, name=entry.name)
            if entry.plugin.SUPPORTS_MUSIC_CONTROL:
                device.set_music_command_handler(
                    lambda cmd, a=address: self._on_music_command(a, cmd))
            keeper = ConnectionKeeper(device, on_state=self._on_link_state)
            self._keepers[address] = keeper
            self._ble.submit(keeper.start())
        self.emit("links-changed")

    def set_forward_notifications(self, address: str, forward: bool) -> None:
        self.update_settings(address, {"forward_notifications": forward})
        self.reconcile_links()

    @property
    def has_links(self) -> bool:
        return bool(self._keepers)

    def link_connected(self, address: str) -> bool | None:
        """Connection state of a persistent link, or None if there is
        no link configured for this device."""
        keeper = self._keepers.get(address)
        return keeper.connected if keeper else None

    def _on_link_state(self, address: str, connected: bool) -> None:
        # Called from the BLE loop.
        GLib.idle_add(lambda: (self.emit("link-state-changed", address,
                                         connected), False)[1])

    def _on_music_command(self, address: str, command: str) -> None:
        # Called from the BLE loop by the plugin's transport.
        GLib.idle_add(lambda: (self.emit("music-command", address, command),
                               False)[1])

    def push_now_playing(self, track, address: str | None = None) -> int:
        """Send a NowPlaying snapshot to every connected music-capable
        link (or just `address`). Returns how many pushes started."""
        started = 0
        for addr, keeper in self._keepers.items():
            entry = self._entries.get(addr)
            if (not keeper.connected or entry is None
                    or entry.plugin is None
                    or not entry.plugin.SUPPORTS_MUSIC_CONTROL
                    or (address is not None and addr != address)):
                continue
            future = self._ble.submit(keeper.run(
                lambda device, t=track: device.push_now_playing(t)))
            future.add_done_callback(_log_notification_result)
            started += 1
        return started

    def forward_notification(self, note) -> int:
        """Push one desktop notification to every connected watch link.
        Returns how many pushes were started."""
        started = 0
        for keeper in self._keepers.values():
            if not keeper.connected:
                continue
            future = self._ble.submit(keeper.run(
                lambda device, n=note: device.push_notification(n)))
            future.add_done_callback(_log_notification_result)
            started += 1
        return started

    # ── sync (BLE loop) ───────────────────────────────────────────
    def sync_device(self, address: str) -> bool:
        """Kick off one sync. Returns False if it couldn't start."""
        entry = self._entries.get(address)
        if entry is None or entry.busy or entry.role != ROLE_WATCH:
            return False
        # Make sure the controller is powered — on hosts that idle it off
        # a timed sync would otherwise fail before it starts.
        if self._bluetooth is not None:
            self._bluetooth.power_on()
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
        # Devices that can self-monitor are configured from Vitals: on by
        # default once paired so the vendor app is never needed.
        monitoring = None
        if plugin.SUPPORTS_MONITORING_CONFIG:
            monitoring = {
                "enabled": bool(entry.settings.get("monitoring_enabled", True)),
                "interval": int(entry.settings.get("monitoring_interval", 10)),
            }

        self._set_state(address, "syncing")
        pushed_pins = frozenset(entry.settings.get("pushed_pin_ids", []))
        future = self._ble.submit(self._run_sync(
            device, plugin, sync_time, push_alarms, alarms, previously_pushed,
            monitoring, pushed_pins))
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
                        alarms, previously_pushed, monitoring=None,
                        pushed_pins=frozenset()) -> dict:
        keeper = self._keepers.get(device.address)
        if keeper is not None:
            # A persistent link already owns this watch (and with it the
            # exclusive transport) — sync over it rather than opening a
            # competing connection. keeper.run serializes against
            # notification pushes.
            return await keeper.run(
                lambda live: self._sync_steps(live, plugin, sync_time,
                                              push_alarms, alarms,
                                              previously_pushed, monitoring,
                                              pushed_pins))
        return await self._guarded(
            lambda: self._sync_pipeline(device, plugin, sync_time,
                                        push_alarms, alarms,
                                        previously_pushed, monitoring,
                                        pushed_pins),
            plugin.EXCLUSIVE_TRANSPORT)

    async def _sync_pipeline(self, device, plugin, sync_time, push_alarms,
                             alarms, previously_pushed, monitoring=None,
                             pushed_pins=frozenset()) -> dict:
        """One full watch sync over a fresh connection: connect → the
        sync steps → disconnect."""
        if self._connect_sem is None:
            self._connect_sem = asyncio.Semaphore(_MAX_CONCURRENT_CONNECTS)
        try:
            # Inside the try so a connect that fails after partially
            # setting up (e.g. a GATT server registered, then the link
            # drops) still tears down — an orphaned transport would
            # compete for the watch on the next sync.
            async with self._connect_sem:
                await device.connect()
            return await self._sync_steps(device, plugin, sync_time,
                                          push_alarms, alarms,
                                          previously_pushed, monitoring,
                                          pushed_pins)
        finally:
            await device.disconnect()

    async def _sync_steps(self, device, plugin, sync_time, push_alarms,
                          alarms, previously_pushed, monitoring=None,
                          pushed_pins=frozenset()) -> dict:
        """The sync body, over an already-open link: time → config →
        alarms → battery → health reads → weather → sync() → ingest."""
        result: dict = {"battery": None, "warnings": [], "pushed_ids": None}
        if sync_time and plugin.SUPPORTS_TIME_SYNC:
            await device.sync_time(time.time())
        elif sync_time:
            result["warnings"].append("time sync not supported")
        if monitoring is not None and plugin.SUPPORTS_MONITORING_CONFIG:
            # A config push failure shouldn't abort the data read.
            try:
                await device.configure_monitoring(
                    monitoring["enabled"], monitoring["interval"])
            except Exception:
                log.exception("monitoring config failed: %s",
                              device.address)
                result["warnings"].append("monitoring config failed")
        if push_alarms and plugin.SUPPORTS_ALARM_PUSH:
            result["pushed_ids"] = await device.push_alarms(
                alarms, previously_pushed_ids=previously_pushed)
        elif push_alarms:
            result["warnings"].append("alarm push not supported")
        result["battery"] = await device.get_battery()

        activity = activity_series = hr_samples = None
        sleep_series = workout_series = hydration_series = None
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
        if plugin.SUPPORTS_HYDRATION_READ:
            hydration_series = await device.get_hydration_series()
        if (plugin.SUPPORTS_WEATHER_PUSH and self._settings is not None
                and self._settings.get_boolean("weather-enabled")):
            await self._push_weather(device, result)
        if (plugin.SUPPORTS_CALENDAR_PUSH and self._settings is not None
                and self._settings.get_boolean("calendar-sync-enabled")):
            await self._push_calendar(device, result, pushed_pins)
        await device.sync()

        # Ingest scheduling is a cheap idle_add; the real write happens
        # on the main thread and never holds the BLE link.
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
        for drink in hydration_series or []:
            envelopes.append(build_hydration_record(HydrationSample(
                device_address=device.address, device_name=device.name,
                reading=drink)))
        if envelopes:
            self._recorder.ingest_from_thread(envelopes)
        result["records"] = len(envelopes)
        return result

    async def _push_weather(self, device, result: dict) -> None:
        """Fetch a forecast (off the BLE loop) and store it on the watch.
        A weather failure is a post-sync warning, never fatal to the
        sync. Runs on the BLE worker loop with the link open."""
        lat = self._settings.get_double("weather-latitude")
        lon = self._settings.get_double("weather-longitude")
        name = self._settings.get_string("weather-location-name")
        if not name or (lat == 0 and lon == 0):
            result["warnings"].append("weather: set a location in Preferences")
            return
        try:
            from vitals.devices import weather as weather_mod
            unit = self._settings.get_string("weather-units")
            forecast = await asyncio.to_thread(
                weather_mod.fetch_forecast, lat, lon, name, unit,
                int(time.time()))
            await device.push_weather(forecast)
            log.info("sync: weather pushed (%s)", name)
        except Exception:
            log.exception("sync: weather push failed")
            result["warnings"].append("weather update failed")

    async def _push_calendar(self, device, result: dict,
                             previously_pushed) -> None:
        """Read upcoming phone-calendar events (off the BLE loop) and
        reconcile the watch's pins. Failures warn, never abort."""
        from vitals import calendar as cal_mod
        try:
            events = await asyncio.to_thread(cal_mod.read_events)
        except cal_mod.CalendarUnavailable as exc:
            result["warnings"].append(str(exc))
            return
        except Exception:
            log.exception("sync: calendar read failed")
            result["warnings"].append("calendar read failed")
            return
        events, current, stale = cal_mod.reconcile(
            events[:_MAX_CALENDAR_PINS], set(previously_pushed))
        try:
            await device.push_calendar(events, sorted(stale))
            result["pushed_pin_ids"] = sorted(current)
            log.info("sync: calendar pinned (%d events)", len(events))
        except Exception:
            log.exception("sync: calendar push failed")
            result["warnings"].append("calendar push failed")

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
        if result.get("pushed_pin_ids") is not None:
            updates["pushed_pin_ids"] = result["pushed_pin_ids"]
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

    # ── opportunistic sensors (ScanBroker) ────────────────────────
    def attach_scan_broker(self, broker) -> None:
        """Route advertisements to registered sensors; listening runs
        whenever at least one enabled sensor is registered."""
        self._broker = broker
        broker.set_sensor_handler(self._on_sensor_advert)
        self.connect("device-list-changed",
                     lambda *_: self._reconcile_listening())
        self._reconcile_listening()

    def _reconcile_listening(self) -> None:
        if self._broker is None:
            return
        want = any(e.enabled and e.role == ROLE_SENSOR
                   for e in self._entries.values())
        if want:
            self._broker.start_listening()
        else:
            self._broker.stop_listening()

    async def _on_sensor_advert(self, device, advertisement) -> None:
        """BLE loop: one advertisement → maybe a reading from a
        registered sensor. `_entries` is only mutated on the main
        thread; the lookup here is read-only."""
        entry = self._entries.get(device.address)
        if (entry is None or not entry.enabled
                or entry.role != ROLE_SENSOR or entry.plugin is None):
            return
        plugin = entry.plugin
        if not plugin.match_advertisement(device, advertisement):
            return
        if device.address in self._sensor_busy:
            return
        self._sensor_busy.add(device.address)
        try:
            instance = plugin(address=entry.address, name=entry.name)
            envelopes = await instance.handle_advertisement(
                device, advertisement)
        finally:
            self._sensor_busy.discard(device.address)
        if envelopes:
            self._recorder.ingest_from_thread(envelopes)
            GLib.idle_add(self._touch_sensor, entry.address, len(envelopes))

    def _touch_sensor(self, address: str, count: int) -> bool:
        entry = self._entries.get(address)
        if entry is not None:
            entry.last_sync_ms = round(time.time() * 1000)
            with self._store.connection as con:
                con.execute("UPDATE devices SET last_sync_ms=? WHERE address=?",
                            (entry.last_sync_ms, address))
            self.emit("device-synced", address,
                      f"{entry.name}: reading received")
        return GLib.SOURCE_REMOVE

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


def _log_notification_result(future) -> None:
    try:
        future.result()
    except Exception as exc:
        log.warning("notification push failed: %s", exc)
