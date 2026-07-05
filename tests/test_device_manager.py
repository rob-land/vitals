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
