"""Tests for the pure record-building in the food log form."""

import json

from vitals.sources.food import (
    MEALS, NUTRIENTS, build_record, summarize_meals)

WHEN = "2026-06-10T09:00:00+00:00"
UID = "11111111-1111-1111-1111-111111111111"


def _row(meal, label, kcal, when_ms):
    return {
        "meta_json": json.dumps({"meal": meal}),
        "value_json": json.dumps(
            {"label": label,
             "nutrients": {"energy-kcal": {"value": kcal, "unit": "kcal"}}}),
        "effective_start": when_ms,
    }


def test_summarize_meals_groups_totals_in_daily_order():
    rows = [
        _row("dinner", "Pasta", 600, 5000),
        _row("breakfast", "Banana", 105, 2000),
        _row("breakfast", "Oatmeal", 300, 1000),
    ]
    meals = summarize_meals(rows)
    assert [m["meal"] for m in meals] == ["breakfast", "dinner"]  # lunch skipped
    breakfast = meals[0]
    assert breakfast["label"] == "Breakfast"
    assert breakfast["kcal"] == 405 and breakfast["item_count"] == 2
    # foods sorted by time within the meal
    assert [f["label"] for f in breakfast["foods"]] == ["Oatmeal", "Banana"]


def test_summarize_meals_tolerates_missing_kcal_and_label():
    rows = [{"meta_json": json.dumps({"meal": "snack"}),
             "value_json": json.dumps({"nutrients": {}}),
             "effective_start": 100}]
    (snack,) = summarize_meals(rows)
    assert snack["foods"][0]["label"] == "Food"
    assert snack["kcal"] == 0 and snack["item_count"] == 1


def test_build_record_shape():
    rec = build_record("Greek yogurt", "breakfast",
                       {"energy-kcal": 120, "sugars": 6}, WHEN, UID)
    assert rec["type"] == "nutrient_intake"
    assert rec["uuid"] == UID and rec["effective_start"] == WHEN
    assert rec["source"] == {"modality": "self_reported",
                             "device_name": "Manual entry"}
    assert rec["meta"] == {"meal": "breakfast"}
    assert rec["value"]["label"] == "Greek yogurt"
    # Structured types carry no envelope-level unit.
    assert "unit" not in rec


def test_nutrients_keyed_to_open_food_facts_with_units():
    rec = build_record("", "lunch",
                       {"energy-kcal": 250, "carbohydrates": 30, "sugars": 8,
                        "fat": 9, "proteins": 12}, WHEN, UID)
    nutrients = rec["value"]["nutrients"]
    assert nutrients["energy-kcal"] == {"value": 250, "unit": "kcal"}
    assert nutrients["carbohydrates"] == {"value": 30, "unit": "g"}
    assert nutrients["sugars"] == {"value": 8, "unit": "g"}
    assert nutrients["fat"] == {"value": 9, "unit": "g"}
    assert nutrients["proteins"] == {"value": 12, "unit": "g"}


def test_label_omitted_when_blank():
    rec = build_record("", "snack", {"energy-kcal": 90}, WHEN, UID)
    assert "label" not in rec["value"]


def test_only_entered_nutrients_are_included():
    rec = build_record("Apple", "snack", {"energy-kcal": 95}, WHEN, UID)
    assert list(rec["value"]["nutrients"]) == ["energy-kcal"]


def test_constants_use_canonical_keys():
    keys = {n["key"] for n in NUTRIENTS}
    assert {"energy-kcal", "carbohydrates", "sugars", "fat", "proteins"} <= keys
    assert [m[1] for m in MEALS] == ["breakfast", "lunch", "dinner", "snack"]


def test_lookup_food_records_amount_and_barcode():
    rec = build_record("Nutella (Ferrero)", "snack", {"energy-kcal": 81},
                       WHEN, UID, amount_g=15, barcode="3017620422003")
    assert rec["value"]["amount"] == {"value": 15, "unit": "g"}
    assert rec["meta"]["off_barcode"] == "3017620422003"


def test_manual_entry_omits_amount_and_barcode():
    rec = build_record("Apple", "snack", {"energy-kcal": 95}, WHEN, UID)
    assert "amount" not in rec["value"]
    assert "off_barcode" not in rec["meta"]


# ── the dietary_energy companion (new in vitals) ──────────────────

def test_meal_records_include_energy_companion():
    from vitals.sources.food import build_meal_records
    records = build_meal_records(
        "Toast", "breakfast", {"energy-kcal": 210, "fat": 8.5},
        WHEN, UID)
    assert [r["type"] for r in records] == ["nutrient_intake", "dietary_energy"]
    energy = records[1]
    assert energy["uuid"] == f"{UID}:energy"  # keyed to the meal's uuid
    assert energy["value"] == 210 and energy["unit"] == "kcal"
    assert energy["effective_start"] == WHEN


def test_meal_without_calories_has_no_companion():
    from vitals.sources.food import build_meal_records
    records = build_meal_records("Celery", "snack", {"fat": 0.1}, WHEN, UID)
    assert [r["type"] for r in records] == ["nutrient_intake"]
