"""Tests for the pure Open Food Facts parsing + scaling."""

from vitals.sources.food_lookup import (
    Food, is_barcode, parse_product, scale_nutrients)

PRODUCT = {
    "code": "3017620422003",
    "product_name": "Nutella",
    "brands": "Ferrero, Nutella",
    "serving_quantity": 15,
    "nutriments": {
        "energy-kcal_100g": 539,
        "carbohydrates_100g": 57.5,
        "sugars_100g": 56.3,
        "fat_100g": 30.9,
        "proteins_100g": 6.3,
    },
}


def test_is_barcode():
    assert is_barcode("3017620422003") is True
    assert is_barcode("0123456789") is True
    assert is_barcode("123") is False         # too short
    assert is_barcode("greek yogurt") is False
    assert is_barcode("  5449000000996 ") is True


def test_parse_product_maps_nutriments_to_our_keys():
    food = parse_product(PRODUCT)
    assert food.name == "Nutella"
    assert food.brand == "Ferrero"            # first of a comma list
    assert food.barcode == "3017620422003"
    assert food.serving_g == 15.0
    assert food.per_100g == {
        "energy-kcal": 539.0, "carbohydrates": 57.5, "sugars": 56.3,
        "fat": 30.9, "proteins": 6.3}


def test_parse_product_energy_from_kilojoules():
    food = parse_product({"product_name": "X", "nutriments": {
        "energy-kj_100g": 2000, "sugars_100g": 10}})
    # 2000 kJ / 4.184 ≈ 478 kcal
    assert round(food.per_100g["energy-kcal"]) == 478
    assert food.per_100g["sugars"] == 10.0


def test_parse_product_without_nutriments():
    food = parse_product({"product_name": "Mystery"})
    assert food.per_100g == {} and food.serving_g is None


def test_food_label():
    assert Food("Apple", "Brand", "1").label == "Apple (Brand)"
    assert Food("Apple", "", "1").label == "Apple"


def test_scale_nutrients_rounds_kcal_whole_and_grams_one_dp():
    scaled = scale_nutrients(parse_product(PRODUCT).per_100g, 15)
    assert scaled["energy-kcal"] == 81          # round(539 * 0.15)
    assert scaled["sugars"] == 8.4              # round(56.3 * 0.15, 1)
    assert scaled["fat"] == 4.6


def test_scale_nutrients_100g_is_identity():
    per_100g = {"energy-kcal": 250.0, "sugars": 12.4}
    scaled = scale_nutrients(per_100g, 100)
    assert scaled == {"energy-kcal": 250, "sugars": 12.4}
