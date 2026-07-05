"""Tests for the pure USDA FoodData Central parsing + source wiring."""

from vitals.sources.food_lookup import Food, OpenFoodFactsSource, available_sources
from vitals.sources.usda import UsdaSource, parse_usda_food

# A Branded result (packaged): brand + UPC + per-100g nutrients.
BRANDED = {
    "fdcId": 123,
    "description": "THIN CRUST PIZZA",
    "dataType": "Branded",
    "brandName": "DIGIORNO",
    "gtinUpc": "071921005010",
    "servingSize": 140,
    "servingSizeUnit": "g",
    "foodNutrients": [
        {"nutrientName": "Energy", "nutrientNumber": "208",
         "unitName": "KCAL", "value": 250},
        {"nutrientNumber": "203", "unitName": "G", "value": 11},
        {"nutrientNumber": "204", "unitName": "G", "value": 10},
        {"nutrientNumber": "205", "unitName": "G", "value": 30},
        {"nutrientNumber": "269", "unitName": "G", "value": 4},
    ],
}

# A Survey (FNDDS) result: a restaurant / prepared food, no brand or UPC.
RESTAURANT = {
    "description": "Chicken nuggets, from fast food / restaurant",
    "dataType": "Survey (FNDDS)",
    "foodNutrients": [
        {"nutrientNumber": "208", "unitName": "KCAL", "value": 296},
        {"nutrientNumber": "203", "unitName": "G", "value": 15.9},
        {"nutrientNumber": "204", "unitName": "G", "value": 19.0},
        {"nutrientNumber": "205", "unitName": "G", "value": 15.2},
    ],
}


def test_parse_branded_maps_nutrients_brand_and_upc():
    food = parse_usda_food(BRANDED)
    assert food.name == "THIN CRUST PIZZA"
    assert food.brand == "Digiorno"          # ALL-CAPS brand title-cased
    assert food.barcode == "071921005010"
    assert food.serving_g == 140.0
    assert food.source == "USDA"
    assert food.per_100g == {
        "energy-kcal": 250.0, "proteins": 11.0, "fat": 10.0,
        "carbohydrates": 30.0, "sugars": 4.0}


def test_parse_restaurant_food_without_brand_or_barcode():
    food = parse_usda_food(RESTAURANT)
    assert "restaurant" in food.name
    assert food.brand == "USDA Survey"        # provenance from the data type
    assert food.barcode == ""
    assert food.serving_g is None             # no gram serving given
    assert food.per_100g["energy-kcal"] == 296.0
    assert "sugars" not in food.per_100g      # not reported, not invented


def test_energy_in_kilojoules_is_ignored():
    food = parse_usda_food({"description": "X", "foodNutrients": [
        {"nutrientNumber": "208", "unitName": "kJ", "value": 1046},
        {"nutrientNumber": "203", "unitName": "G", "value": 5},
    ]})
    assert "energy-kcal" not in food.per_100g
    assert food.per_100g["proteins"] == 5.0


def test_alternate_nutrient_numbers_fill_gaps():
    # Atwater kcal (957) for energy, FNDDS sugars (2000) when primaries absent.
    food = parse_usda_food({"description": "Y", "foodNutrients": [
        {"nutrientNumber": "957", "unitName": "KCAL", "value": 180},
        {"nutrientNumber": "2000", "unitName": "G", "value": 7},
    ]})
    assert food.per_100g["energy-kcal"] == 180.0
    assert food.per_100g["sugars"] == 7.0


def test_primary_energy_wins_over_alternate():
    food = parse_usda_food({"description": "Z", "foodNutrients": [
        {"nutrientNumber": "957", "unitName": "KCAL", "value": 999},
        {"nutrientNumber": "208", "unitName": "KCAL", "value": 200},
    ]})
    assert food.per_100g["energy-kcal"] == 200.0


class _FakeSettings:
    def __init__(self, key=""):
        self._key = key

    def get_string(self, _name):
        return self._key


def test_available_sources_includes_off_and_usda():
    sources = available_sources(_FakeSettings())
    assert [s.id for s in sources] == ["off", "usda"]
    assert isinstance(sources[0], OpenFoodFactsSource)
    assert isinstance(sources[1], UsdaSource)


def test_usda_source_reads_key_from_settings():
    src = UsdaSource(_FakeSettings("MYKEY"))
    assert src._api_key == "MYKEY"


def test_food_source_field_defaults_empty():
    assert Food("Apple", "", "1").source == ""
