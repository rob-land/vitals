"""Tests for the pure water record-building + unit conversion."""

from vitals.sources.water import (
    ML_PER_FLOZ, UNITS, build_water_record, to_ml)

WHEN = "2026-06-11T09:00:00+00:00"
UID = "22222222-2222-2222-2222-222222222222"


def test_to_ml_millilitres_is_identity():
    assert to_ml(250, "ml") == 250.0


def test_to_ml_fluid_ounces_converts():
    assert to_ml(8, "floz") == 8 * ML_PER_FLOZ


def test_build_water_record_stores_millilitres():
    rec = build_water_record(500, WHEN, UID)
    assert rec["type"] == "water_intake"
    assert rec["value"] == 500.0
    assert rec["unit"] == "mL"
    assert rec["uuid"] == UID and rec["effective_start"] == WHEN
    assert rec["source"] == {"modality": "self_reported",
                             "device_name": "Manual entry"}


def test_floz_record_is_in_millilitres():
    rec = build_water_record(to_ml(12, "floz"), WHEN, UID)
    assert rec["unit"] == "mL"
    assert rec["value"] == round(12 * ML_PER_FLOZ, 1)


def test_units_have_presets_and_config():
    assert set(UNITS) == {"ml", "floz"}
    for cfg in UNITS.values():
        assert cfg["presets"] and cfg["suffix"] and cfg["upper"] > 0
