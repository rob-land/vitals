"""Tests for the in-process health-data core.

Exercises catalog loading, unit conversion, record validation /
canonicalisation and the SQLite store (insert / idempotency / read /
aggregate / change-feed / tombstone / devices table). Ported from the
pulse daemon's test_core.py, minus the consent layer, which no longer
exists.
"""

import pytest

from vitals.core import records, units
from vitals.core.catalog import Catalog
from vitals.core.errors import InvalidRecord
from vitals.core.store import Store

APP = "land.rob.vitals"


@pytest.fixture(scope="module")
def cat():
    return Catalog.load()


@pytest.fixture()
def store(tmp_path):
    s = Store(str(tmp_path / "health.db"))
    s.migrate()
    yield s
    s.close()


def env(type_key, value, start, unit=None, modality="sensed", end=None,
        meta=None, uuid=None, device="dev1"):
    e = {
        "uuid": uuid or f"{type_key}-{start}",
        "type": type_key,
        "effective_start": start,
        "value": value,
        "source": {"modality": modality, "device_id": device,
                   "device_name": "Test Device"},
    }
    if unit is not None:
        e["unit"] = unit
    if end is not None:
        e["effective_end"] = end
    if meta is not None:
        e["meta"] = meta
    return e


# ── catalog ───────────────────────────────────────────────────────

def test_catalog_types(cat):
    assert len(cat.all()) == 36
    hr = cat.get("heart_rate")
    assert hr.value_shape == "scalar" and hr.canonical_unit == "/min"
    bp = cat.get("blood_pressure")
    assert set(bp.component_units) == {"systolic", "diastolic"}


# ── units ─────────────────────────────────────────────────────────

def test_unit_conversions():
    assert units.convert(220.462, "[lb_av]", "kg") == pytest.approx(100.0, abs=1e-3)
    assert units.convert(98.6, "[degF]", "Cel") == pytest.approx(37.0, abs=1e-2)
    assert units.convert(90, "mg/dL", "mmol/L") == pytest.approx(90 / 18.0156)
    assert units.convert(1, "mi", "m") == pytest.approx(1609.344)
    assert units.convert(5, "kg", "kg") == 5
    with pytest.raises(units.UnitError):
        units.convert(1, "furlong", "m")


# ── record validation / canonicalisation ─────────────────────────

def test_scalar_canonicalised_to_kg(cat):
    nr = records.validate_and_canonicalize(
        env("body_weight", 220.462, "2026-06-02T08:00:00+00:00", unit="[lb_av]"),
        cat.get("body_weight"))
    assert nr.value_num == pytest.approx(100.0, abs=1e-3) and nr.unit == "kg"


def test_components_and_structured_stored_as_json(cat):
    bp = records.validate_and_canonicalize(
        env("blood_pressure", {"systolic": 118, "diastolic": 76},
            "2026-06-02T08:00:00+00:00"),
        cat.get("blood_pressure"))
    assert bp.value_json is not None and bp.value_num is None

    sleep = records.validate_and_canonicalize(
        env("sleep_episode", {"stages": [{"stage": "deep", "start": "x", "end": "y"}]},
            "2026-06-01T23:00:00+00:00", end="2026-06-02T07:00:00+00:00"),
        cat.get("sleep_episode"))
    assert sleep.value_json is not None
    assert sleep.effective_end > sleep.effective_start


@pytest.mark.parametrize("bad", [
    env("heart_rate", "fast", "2026-06-02T08:00:00+00:00"),
    env("heart_rate", 60, "not-a-date"),
    env("blood_pressure", {"systolic": 118}, "2026-06-02T08:00:00+00:00"),
])
def test_invalid_records_rejected(cat, bad):
    with pytest.raises(InvalidRecord):
        records.validate_and_canonicalize(bad, cat.get(bad["type"]))


# ── store ─────────────────────────────────────────────────────────

def _norm(cat, e):
    return records.validate_and_canonicalize(e, cat.get(e["type"]))


def _batch(cat):
    return [
        _norm(cat, env("step_count", 1200, "2026-06-01T09:00:00+00:00",
                       end="2026-06-01T10:00:00+00:00", uuid="s1")),
        _norm(cat, env("step_count", 800, "2026-06-01T10:00:00+00:00",
                       end="2026-06-01T11:00:00+00:00", uuid="s2")),
        _norm(cat, env("step_count", 1500, "2026-06-02T09:00:00+00:00",
                       end="2026-06-02T10:00:00+00:00", uuid="s3")),
    ]


def test_store_schema_is_current(store):
    # 0002 dropped grants and added the device registry.
    assert store.schema_version() == 2
    tables = {r[0] for r in store.connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    assert "devices" in tables and "grants" not in tables


def test_insert_read_roundtrip(cat, store):
    summary = store.insert_records(_batch(cat), APP)
    assert summary["stored"] == 3 and summary["duplicates"] == 0
    assert summary["high_seq"] == 3

    rows, cursor = store.read_records(["step_count"])
    assert len(rows) == 3 and cursor is None
    env0 = records.row_to_envelope(rows[0])
    assert env0["source"]["app_id"] == APP
    assert env0["value"] == 1200 and env0["unit"] == "{steps}"


def test_reinsert_is_idempotent(cat, store):
    store.insert_records(_batch(cat), APP)
    again = store.insert_records(_batch(cat), APP)
    assert again["stored"] == 0 and again["duplicates"] == 3
    assert again["high_seq"] == 3


def test_aggregate_sums_by_day(cat, store):
    store.insert_records(_batch(cat), APP)
    buckets = store.aggregate("step_count", "sum", "day", tz="UTC")
    by_day = {b["start"][:10]: b["value"] for b in buckets}
    assert by_day["2026-06-01"] == pytest.approx(2000)
    assert by_day["2026-06-02"] == pytest.approx(1500)


def test_edit_and_tombstone_flow_through_change_feed(cat, store):
    store.insert_records(_batch(cat), APP)
    edited = _norm(cat, env("step_count", 1300, "2026-06-01T09:00:00+00:00",
                            end="2026-06-01T10:00:00+00:00", uuid="s1"))
    s = store.insert_records([edited], APP)
    assert s["stored"] == 1 and s["high_seq"] == 4

    assert store.delete_record("s2") is True
    changes, next_seq = store.get_changes(3, 100)
    deleted = [c for c in changes if c["uuid"] == "s2"]
    assert len(deleted) == 1 and deleted[0]["deleted"] == 1
    assert next_seq >= 5
