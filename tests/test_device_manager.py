"""Tests for the DeviceManager: registry CRUD, the sync pipeline with a
fake plugin, and exclusive-transport serialization."""

import asyncio

import pytest

from vitals.core.store import Store
from vitals.devices import base
from vitals.devices.base import ActivityReading, Device, SleepSession
from vitals.devices.manager import DeviceManager


class FakeRecorder:
    def __init__(self):
        self.batches: list[list[dict]] = []

    def ingest_from_thread(self, envelopes):
        self.batches.append(list(envelopes))


class FakeWatch(Device):
    id = "fakewatch"
    display_name = "Fake Watch"
    description = "test double"
    SUPPORTS_TIME_SYNC = True
    SUPPORTS_ACTIVITY_READ = True
    SUPPORTS_SLEEP_READ = True

    calls: list[str] = []

    @classmethod
    def matches(cls, name, uuids):
        return bool(name) and name.startswith("Fake")

    async def connect(self):
        self.calls.append("connect")

    async def disconnect(self):
        self.calls.append("disconnect")

    async def get_battery(self):
        self.calls.append("battery")
        return 88

    async def sync_time(self, ts):
        self.calls.append("sync_time")

    async def get_activity(self):
        return ActivityReading(steps=4321, heart_rate_bpm=70,
                               timestamp=1_700_000_000.0)

    async def get_sleep_series(self):
        return [SleepSession(start=1_700_000_000.0, end=1_700_028_800.0)]


@pytest.fixture()
def rig(tmp_path):
    if "fakewatch" not in base._REGISTRY:
        base.register_device(FakeWatch)
    store = Store(str(tmp_path / "health.db"))
    store.migrate()
    recorder = FakeRecorder()
    manager = DeviceManager(store, recorder, settings=None, ble=None)
    yield manager, store, recorder
    store.close()
    base._REGISTRY.pop("fakewatch", None)


ADDR = "AA:BB:CC:DD:EE:FF"


def test_registry_crud(rig):
    manager, store, _ = rig
    entry = manager.add(ADDR, "Fake One", "fakewatch")
    assert entry.role == "watch" and entry.enabled

    manager.update_settings(ADDR, {"alarms": [{"id": "a1"}]})
    reloaded = DeviceManager(store, None, None, None)
    assert reloaded.get(ADDR).settings == {"alarms": [{"id": "a1"}]}

    manager.set_enabled(ADDR, False)
    assert not manager.get(ADDR).enabled

    manager.forget(ADDR)
    assert manager.list() == []
    assert DeviceManager(store, None, None, None).list() == []


def test_sync_pipeline_order_and_ingest(rig):
    manager, _store, recorder = rig
    FakeWatch.calls = []
    device = FakeWatch(address=ADDR, name="Fake One")
    result = asyncio.run(manager._sync_pipeline(
        device, FakeWatch, sync_time=True, push_alarms=False,
        alarms=[], previously_pushed=set()))

    assert FakeWatch.calls == ["connect", "sync_time", "battery", "disconnect"]
    assert result["battery"] == 88
    assert result["warnings"] == []
    # One batch: cumulative steps + HR point + one sleep episode.
    (batch,) = recorder.batches
    assert {r["type"] for r in batch} == {"step_count", "heart_rate",
                                          "sleep_episode"}
    assert result["records"] == 3


def test_sync_reuses_a_persistent_link(rig):
    manager, _store, recorder = rig
    FakeWatch.calls = []
    device = FakeWatch(address=ADDR, name="Fake One")

    class LiveKeeper:
        connected = True

        async def run(self, op):
            return await op(device)

    manager._keepers[ADDR] = LiveKeeper()
    result = asyncio.run(manager._run_sync(
        device, FakeWatch, sync_time=True, push_alarms=False,
        alarms=[], previously_pushed=set()))

    # The link stays owned by the keeper: no connect/disconnect.
    assert FakeWatch.calls == ["sync_time", "battery"]
    assert result["battery"] == 88 and result["records"] == 3


def test_hydration_config_reflects_settings(rig):
    manager, _store, _ = rig
    entry = manager.add(ADDR, "Bottle", "fakewatch")
    # No Gio.Settings in the rig → no app-wide goal; per-device defaults
    # give the stock reminder window.
    assert manager._hydration_config(entry) == {
        "goal_ml": 0, "reminder": (8, 20, 60)}

    manager.update_settings(ADDR, {"hydration_reminder_enabled": False})
    assert manager._hydration_config(entry)["reminder"] is None

    manager.update_settings(ADDR, {"hydration_reminder_enabled": True,
                                   "hydration_reminder_start": 9,
                                   "hydration_reminder_end": 22,
                                   "hydration_reminder_interval": 45})
    assert manager._hydration_config(entry)["reminder"] == (9, 22, 45)


def test_sync_steps_apply_hydration_config(rig):
    manager, _store, _ = rig

    class FakeBottle(Device):
        id = "fakebottle"
        display_name = "Fake Bottle"
        description = "test double"
        SUPPORTS_HYDRATION_READ = True
        SUPPORTS_HYDRATION_CONFIG = True

        applied = None

        @classmethod
        def matches(cls, name, uuids):
            return False

        async def connect(self):
            pass

        async def disconnect(self):
            pass

        async def get_battery(self):
            return 50

        async def configure_hydration(self, goal_ml, reminder):
            FakeBottle.applied = (goal_ml, reminder)

        async def get_hydration_series(self):
            return []

    device = FakeBottle(address=ADDR, name="Bottle")
    result = asyncio.run(manager._sync_steps(
        device, FakeBottle, sync_time=False, push_alarms=False,
        alarms=[], previously_pushed=set(),
        hydration={"goal_ml": 1500, "reminder": (9, 21, 30)}))

    assert FakeBottle.applied == (1500, (9, 21, 30))
    assert result["warnings"] == []


def test_forward_notifications_setting_round_trips(rig):
    manager, store, _ = rig
    manager.add(ADDR, "Fake One", "fakewatch")
    manager.set_forward_notifications(ADDR, True)   # ble=None → no keeper
    assert manager.get(ADDR).settings["forward_notifications"] is True
    assert not manager.has_links
    reloaded = DeviceManager(store, None, None, None)
    assert reloaded.get(ADDR).settings["forward_notifications"] is True


def test_source_trust_ranks_by_quality_and_preference(rig):
    manager, _store, _ = rig
    manager.add("AA:BB", "Fake One", "fakewatch")                 # medium
    manager.add("07:32:00:00:00:01", "HR Strap", "gatt-sensor")   # high HR

    trust = manager.source_trust("heart_rate")
    assert trust["07:32:00:00:00:01"] == 30   # dedicated sensor: high
    assert trust["AA:BB"] == 20                # watch: default medium
    assert trust[""] == 20                     # manual-entry baseline
    # A metric the sensor doesn't rate falls back to medium.
    assert manager.source_trust("step_count")["07:32:00:00:00:01"] == 20
    # A user-pinned device wins outright.
    manager.update_settings("AA:BB", {"preferred_metrics": ["heart_rate"]})
    assert manager.source_trust("heart_rate")["AA:BB"] == 120


def test_contested_metrics(rig):
    from vitals.core import records as rec_mod
    from vitals.core.catalog import Catalog
    manager, store, _ = rig
    cat = Catalog.load()

    def rec(type_key, dev, unit):
        return rec_mod.validate_and_canonicalize({
            "uuid": f"{type_key}-{dev}", "type": type_key,
            "effective_start": "2026-06-01T09:00:00+00:00", "value": 60,
            "unit": unit,
            "source": {"modality": "sensed", "device_id": dev,
                       "device_name": dev},
        }, cat.get(type_key))

    manager.add("ring", "Ring", "fakewatch")
    manager.add("peb", "Peb", "fakewatch")
    store.insert_records([
        rec("heart_rate", "ring", "/min"),
        rec("step_count", "ring", "{steps}"),
        rec("heart_rate", "peb", "/min"),
    ], "test.app")
    # heart_rate is reported by both; step_count only by the ring.
    assert manager.contested_metrics("ring") == ["heart_rate"]
    assert manager.contested_metrics("peb") == ["heart_rate"]


def test_sync_device_powers_on_adapter(tmp_path):
    """sync_device asks the BluetoothMonitor to power the adapter on
    before a sync — hosts that idle it off would otherwise fail."""
    import concurrent.futures

    if "fakewatch" not in base._REGISTRY:
        base.register_device(FakeWatch)
    store = Store(str(tmp_path / "health.db"))
    store.migrate()

    class FakeSettings:
        def get_boolean(self, _key):
            return False

    class FakeBle:
        def submit(self, coro):
            coro.close()  # don't actually run the pipeline
            fut = concurrent.futures.Future()
            fut.set_result({"battery": None, "warnings": [],
                            "pushed_ids": None, "records": 0})
            return fut

    powered = []

    class FakeBluetooth:
        def power_on(self):
            powered.append(True)
            return True

    manager = DeviceManager(store, FakeRecorder(), FakeSettings(),
                            FakeBle(), bluetooth=FakeBluetooth())
    manager.add(ADDR, "Fake One", "fakewatch")
    assert manager.sync_device(ADDR) is True
    assert powered == [True]
    store.close()
    base._REGISTRY.pop("fakewatch", None)


def test_pipeline_disconnects_after_connect_failure(rig):
    manager, _store, recorder = rig

    class Exploding(FakeWatch):
        async def get_battery(self):
            raise RuntimeError("boom")

    device = Exploding(address=ADDR, name="Fake One")
    FakeWatch.calls = []
    with pytest.raises(RuntimeError):
        asyncio.run(manager._sync_pipeline(
            device, Exploding, sync_time=False, push_alarms=False,
            alarms=[], previously_pushed=set()))
    # The finally still tears the link down, and nothing was ingested.
    assert FakeWatch.calls[-1] == "disconnect"
    assert recorder.batches == []


def test_exclusive_transport_serializes(rig):
    manager, _, _ = rig
    running = {"now": 0, "peak": 0}

    async def hold():
        running["now"] += 1
        running["peak"] = max(running["peak"], running["now"])
        await asyncio.sleep(0.01)
        running["now"] -= 1

    async def main():
        await asyncio.gather(
            manager._guarded(hold, "ppogatt-server"),
            manager._guarded(hold, "ppogatt-server"),
            manager._guarded(hold, "ppogatt-server"))

    asyncio.run(main())
    assert running["peak"] == 1  # never concurrent under one transport

    running["peak"] = running["now"] = 0

    async def mixed():
        await asyncio.gather(
            manager._guarded(hold, "ppogatt-server"),
            manager._guarded(hold, None))

    # Locks belong to the loop they were created on.
    manager._transport_locks.clear()
    asyncio.run(mixed())
    assert running["peak"] == 2  # unrelated transports run together
