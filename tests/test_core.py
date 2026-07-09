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


def _multi_source_steps(cat):
    # One day: the ring reports 1000+500 steps, the Pebble reports 1200.
    return [
        _norm(cat, env("step_count", 1000, "2026-06-01T09:00:00+00:00",
                       unit="{steps}", device="ring", uuid="r1")),
        _norm(cat, env("step_count", 500, "2026-06-01T10:00:00+00:00",
                       unit="{steps}", device="ring", uuid="r2")),
        _norm(cat, env("step_count", 1200, "2026-06-01T09:00:00+00:00",
                       unit="{steps}", device="pebble", uuid="p1")),
    ]


def test_aggregate_double_counts_across_sources_by_default(cat, store):
    store.insert_records(_multi_source_steps(cat), APP)
    (day,) = store.aggregate("step_count", "sum", "day", tz="UTC")
    # 1000 + 500 + 1200 — both devices' steps summed together.
    assert day["value"] == pytest.approx(2700)
    assert "source" not in day


def test_aggregate_resolves_additive_to_one_source(cat, store):
    store.insert_records(_multi_source_steps(cat), APP)
    (day,) = store.aggregate("step_count", "sum", "day", tz="UTC",
                             source_trust={"ring": 30, "pebble": 10})
    assert day["value"] == pytest.approx(1500)   # only the ring's steps
    assert day["source"] == "ring"


def test_aggregate_resolution_respects_trust_order(cat, store):
    store.insert_records(_multi_source_steps(cat), APP)
    (day,) = store.aggregate("step_count", "sum", "day", tz="UTC",
                             source_trust={"ring": 5, "pebble": 20})
    assert day["value"] == pytest.approx(1200)   # Pebble now wins
    assert day["source"] == "pebble"


def test_aggregate_resolution_does_not_blend_point_metric(cat, store):
    store.insert_records([
        _norm(cat, env("heart_rate", 60, "2026-06-01T09:00:00+00:00",
                       unit="/min", device="ring", uuid="h1")),
        _norm(cat, env("heart_rate", 100, "2026-06-01T09:05:00+00:00",
                       unit="/min", device="pebble", uuid="h2")),
    ], APP)
    (hour,) = store.aggregate("heart_rate", "avg", "hour", tz="UTC",
                              source_trust={"ring": 30, "pebble": 10})
    assert hour["value"] == pytest.approx(60)    # the ring's, not 80
    assert hour["source"] == "ring"


def test_aggregate_resolution_tiebreaks_by_sample_count(cat, store):
    store.insert_records([
        _norm(cat, env("step_count", 100, "2026-06-01T09:00:00+00:00",
                       unit="{steps}", device="a", uuid="a1")),
        _norm(cat, env("step_count", 100, "2026-06-01T09:30:00+00:00",
                       unit="{steps}", device="a", uuid="a2")),
        _norm(cat, env("step_count", 999, "2026-06-01T09:00:00+00:00",
                       unit="{steps}", device="b", uuid="b1")),
    ], APP)
    (day,) = store.aggregate("step_count", "sum", "day", tz="UTC",
                             source_trust={})   # equal (default) trust
    assert day["source"] == "a"                 # a has more samples
    assert day["value"] == pytest.approx(200)


def test_catalog_plausible_and_additive(cat):
    assert cat.get("heart_rate").plausible == (20, 250)
    assert cat.get("heart_rate").additive is False        # point-in-time
    assert cat.get("step_count").additive is True         # interval:true
    assert cat.get("water_intake").additive is True       # intake override
    assert cat.get("water_intake").plausible is None      # additive: no range
    assert cat.get("body_weight").plausible == (2, 500)
    # Normal-range bands: present for context-free vitals, absent otherwise.
    assert cat.get("oxygen_saturation").normal_range == (95, 100)
    assert cat.get("heart_rate").normal_range is None     # activity-dependent


def test_aggregate_value_range_drops_glitches(cat, store):
    store.insert_records([
        _norm(cat, env("heart_rate", 0, "2026-06-01T09:00:00+00:00",
                       unit="/min", uuid="z")),           # sensor glitch
        _norm(cat, env("heart_rate", 62, "2026-06-01T09:05:00+00:00",
                       unit="/min", uuid="a")),
        _norm(cat, env("heart_rate", 300, "2026-06-01T09:10:00+00:00",
                       unit="/min", uuid="b")),            # sensor glitch
        _norm(cat, env("heart_rate", 80, "2026-06-01T09:15:00+00:00",
                       unit="/min", uuid="c")),
    ], APP)
    (lo,) = store.aggregate("heart_rate", "min", "day", tz="UTC",
                            value_range=(20, 250))
    (hi,) = store.aggregate("heart_rate", "max", "day", tz="UTC",
                            value_range=(20, 250))
    assert lo["value"] == 62 and hi["value"] == 80         # glitches excluded
    # Without the range the 0 corrupts the minimum.
    assert store.aggregate("heart_rate", "min", "day", tz="UTC")[0]["value"] == 0


def test_types_for_device(cat, store):
    store.insert_records([
        _norm(cat, env("heart_rate", 60, "2026-06-01T09:00:00+00:00",
                       unit="/min", device="ring", uuid="a")),
        _norm(cat, env("step_count", 100, "2026-06-01T09:00:00+00:00",
                       unit="{steps}", device="ring", uuid="b")),
        _norm(cat, env("water_intake", 200, "2026-06-01T09:00:00+00:00",
                       unit="mL", device="", uuid="c")),
    ], APP)
    assert store.types_for_device("ring") == ["heart_rate", "step_count"]
    assert store.types_for_device("") == ["water_intake"]   # manual entry
    assert store.types_for_device("nope") == []


def test_latest_time_is_the_newest_sample(cat, store):
    store.insert_records(_batch(cat), APP)     # newest starts 02 Jun 09:00
    newest = store.latest_time(["step_count"])
    assert newest == 1780390800000             # 2026-06-02T09:00:00Z in ms
    assert store.latest_time(["step_count", "heart_rate"]) == newest
    assert store.latest_time(["heart_rate"]) is None
    assert store.latest_time([]) is None


def test_aggregate_flags_source_discrepancy(cat, store):
    # Ring reads 60, Pebble reads 100 in the same hour — a big disagreement.
    store.insert_records([
        _norm(cat, env("heart_rate", 60, "2026-06-01T09:00:00+00:00",
                       unit="/min", device="ring", uuid="d1")),
        _norm(cat, env("heart_rate", 100, "2026-06-01T09:05:00+00:00",
                       unit="/min", device="pebble", uuid="d2")),
    ], APP)
    (hour,) = store.aggregate("heart_rate", "avg", "hour", tz="UTC",
                              source_trust={"ring": 30, "pebble": 10},
                              discrepancy_threshold=0.15)
    assert hour["source"] == "ring" and hour["value"] == pytest.approx(60)
    assert hour["discrepancy"] == {"pebble": 100.0}   # dropped source disagrees


def test_aggregate_no_discrepancy_when_sources_agree(cat, store):
    store.insert_records([
        _norm(cat, env("heart_rate", 60, "2026-06-01T09:00:00+00:00",
                       unit="/min", device="ring", uuid="d1")),
        _norm(cat, env("heart_rate", 62, "2026-06-01T09:05:00+00:00",
                       unit="/min", device="pebble", uuid="d2")),
    ], APP)
    (hour,) = store.aggregate("heart_rate", "avg", "hour", tz="UTC",
                              source_trust={"ring": 30, "pebble": 10},
                              discrepancy_threshold=0.15)
    assert "discrepancy" not in hour   # 60 vs 62 is within the threshold


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
