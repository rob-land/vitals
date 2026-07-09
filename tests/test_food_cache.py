"""Tests for the recently-logged-food cache."""

import pytest

from vitals.sources import food_cache


@pytest.fixture
def cache_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("VITALS_USER_DATA_DIR", str(tmp_path))
    return tmp_path


def test_load_missing_is_empty(cache_dir):
    assert food_cache.load() == []


def test_remember_and_load(cache_dir):
    food_cache.remember("Oatmeal", {"energy-kcal": 300, "carbohydrates": 54})
    items = food_cache.load()
    assert len(items) == 1
    assert items[0]["label"] == "Oatmeal"
    assert items[0]["nutrients"]["energy-kcal"] == 300


def test_remember_dedups_and_moves_to_front(cache_dir):
    food_cache.remember("Apple", {"energy-kcal": 95})
    food_cache.remember("Banana", {"energy-kcal": 105})
    food_cache.remember("apple", {"energy-kcal": 100})  # re-log, different case
    items = food_cache.load()
    assert [i["label"] for i in items] == ["apple", "Banana"]  # moved up, deduped
    assert items[0]["nutrients"]["energy-kcal"] == 100        # value refreshed


def test_remember_skips_unnamed_or_empty(cache_dir):
    food_cache.remember("", {"energy-kcal": 100})
    food_cache.remember("X", {})
    assert food_cache.load() == []


def test_remember_caps_entries(cache_dir):
    for i in range(70):
        food_cache.remember(f"food{i}", {"energy-kcal": i})
    items = food_cache.load()
    assert len(items) == 60                # _MAX_ENTRIES
    assert items[0]["label"] == "food69"   # most recent first


def test_amount_and_barcode_are_kept(cache_dir):
    food_cache.remember("Nutella", {"energy-kcal": 80}, amount_g=15,
                        barcode="3017620422003")
    entry = food_cache.load()[0]
    assert entry["amount_g"] == 15
    assert entry["barcode"] == "3017620422003"
