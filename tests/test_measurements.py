"""Tests for the pure record-building in the logging form."""

from vitals.format import unit_label
from vitals.sources.measurements import METRICS, ORDER, build_record

WHEN = "2026-06-03T09:00:00+00:00"
UID = "11111111-1111-1111-1111-111111111111"


def test_scalar_record():
    rec = build_record("body_weight", {"value": 70.5}, WHEN, UID)
    assert rec["type"] == "body_weight"
    assert rec["value"] == 70.5 and rec["unit"] == "kg"
    assert rec["effective_start"] == WHEN and rec["uuid"] == UID
    assert rec["source"] == {"modality": "self_reported", "device_name": "Manual entry"}


def test_blood_pressure_is_components():
    rec = build_record("blood_pressure", {"systolic": 118, "diastolic": 76}, WHEN, UID)
    assert rec["type"] == "blood_pressure"
    assert rec["value"] == {"systolic": 118, "diastolic": 76}
    assert "unit" not in rec  # components carry no envelope-level unit


def test_glucose_unit():
    rec = build_record("blood_glucose", {"value": 5.4}, WHEN, UID)
    assert rec["unit"] == "mmol/L"


def test_unit_label():
    assert unit_label("/min") == "bpm"
    assert unit_label("Cel") == "°C"
    assert unit_label("mm[Hg]") == "mmHg"


def test_every_metric_builds():
    # Every catalogued metric produces a valid-looking record.
    for key in ORDER:
        if METRICS[key]["kind"] == "bp":
            values = {"systolic": 120, "diastolic": 80}
        else:
            values = {"value": METRICS[key]["default"]}
        rec = build_record(key, values, WHEN, UID)
        assert rec["type"] == key and "value" in rec and rec["source"]["modality"] == "self_reported"
