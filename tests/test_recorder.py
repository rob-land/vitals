"""Tests for the Recorder — the single write path into the store."""

import pytest

from vitals.core.catalog import Catalog
from vitals.core.events import RecordBus
from vitals.core.store import Store
from vitals.ingest import Recorder


@pytest.fixture(scope="module")
def cat():
    return Catalog.load()


@pytest.fixture()
def rig(tmp_path, cat):
    store = Store(str(tmp_path / "health.db"))
    store.migrate()
    bus = RecordBus()
    changes: list[tuple] = []
    bus.connect("records-changed", lambda _bus, types: changes.append(types))
    yield Recorder(store, cat, bus), store, changes
    store.close()


def _weight(value=80.0, uuid="w1", start="2026-07-01T08:00:00+00:00"):
    return {
        "uuid": uuid, "type": "body_weight",
        "effective_start": start, "value": value, "unit": "kg",
        "source": {"modality": "self_reported", "device_name": "Manual entry"},
    }


def test_ingest_stores_and_signals(rig):
    recorder, store, changes = rig
    summary = recorder.ingest([_weight()])
    assert summary["stored"] == 1 and summary["rejected"] == []
    assert changes == [("body_weight",)]
    rows, _ = store.read_records(["body_weight"])
    assert len(rows) == 1


def test_duplicate_ingest_does_not_signal(rig):
    recorder, _store, changes = rig
    recorder.ingest([_weight()])
    recorder.ingest([_weight()])  # identical → duplicate, no change
    assert len(changes) == 1


def test_bad_records_rejected_without_aborting_batch(rig):
    recorder, store, changes = rig
    summary = recorder.ingest([
        _weight(),
        {"uuid": "x1", "type": "no_such_type",
         "effective_start": "2026-07-01T08:00:00+00:00", "value": 1,
         "source": {"modality": "sensed"}},
        {"uuid": "x2", "type": "heart_rate",
         "effective_start": "2026-07-01T08:00:00+00:00", "value": "fast",
         "source": {"modality": "sensed"}},
    ])
    assert summary["stored"] == 1
    assert {u for u, _ in summary["rejected"]} == {"x1", "x2"}
    rows, _ = store.read_records(["body_weight"])
    assert len(rows) == 1


def test_delete_tombstones_and_signals(rig):
    recorder, store, changes = rig
    recorder.ingest([_weight()])
    assert recorder.delete("w1") is True
    assert changes[-1] == ("body_weight",)
    rows, _ = store.read_records(["body_weight"])
    assert rows == []
    assert recorder.delete("w1") is False  # already gone
