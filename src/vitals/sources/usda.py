"""USDA FoodData Central lookup — a second nutrition source alongside
Open Food Facts.

FDC is the U.S. Department of Agriculture's food database. Where Open Food
Facts is barcoded packaged products, FDC also covers **restaurant and
fast-food** items and generic/prepared foods (its *Survey (FNDDS)* and
*Branded* data types) — so it's how Vitals logs a meal out, not just a box
from the cupboard.

The API needs a free key (https://fdc.nal.usda.gov/api-key-signup.html);
without one we fall back to the shared ``DEMO_KEY``, which works for trying
it out but is heavily rate-limited. Set your own key in Preferences.

Nutrient values in a search result are per 100 g (matching Open Food
Facts), so they reuse ``food_lookup.scale_nutrients`` unchanged. The
parsing is pure and unit-tested; the dialog runs the HTTP off the main
thread.
"""

from __future__ import annotations

import json
import logging
import urllib.parse
import urllib.request
from dataclasses import replace

from vitals.sources.food_lookup import Food, Source

log = logging.getLogger(__name__)

USDA_BASE = "https://api.nal.usda.gov/fdc/v1"
DEMO_KEY = "DEMO_KEY"
_TIMEOUT = 15.0

# FDC nutrient *numbers* (stable across data types) -> our nutrient key.
_USDA_NUTRIENT_NUMBERS = {
    "208": "energy-kcal",     # Energy (kcal)
    "205": "carbohydrates",   # Carbohydrate, by difference
    "269": "sugars",          # Sugars, total including NLEA
    "204": "fat",             # Total lipid (fat)
    "203": "proteins",        # Protein
}
# Alternates some data types use; only taken when the primary is absent.
_USDA_ALT_NUMBERS = {
    "957": "energy-kcal",     # Energy (Atwater General Factors), kcal
    "2047": "energy-kcal",    # Energy (Atwater Specific Factors), kcal
    "2000": "sugars",         # Sugars, total (FNDDS)
}


# ── pure parsing ───────────────────────────────────────────────────

def parse_usda_food(item: dict) -> Food:
    """Pull the nutrients we log out of one FDC search result."""
    per_100g: dict[str, float] = {}
    alts: dict[str, float] = {}
    for nutrient in item.get("foodNutrients") or []:
        number = str(nutrient.get("nutrientNumber")
                     or nutrient.get("number") or "")
        value = nutrient.get("value")
        if not isinstance(value, (int, float)):
            continue
        key = _USDA_NUTRIENT_NUMBERS.get(number)
        if key is not None:
            # Guard energy: only take a kcal-unit value, not kJ.
            if key == "energy-kcal" and not _is_kcal(nutrient):
                continue
            per_100g.setdefault(key, float(value))
            continue
        alt = _USDA_ALT_NUMBERS.get(number)
        if alt is not None and (alt != "energy-kcal" or _is_kcal(nutrient)):
            alts.setdefault(alt, float(value))
    for key, value in alts.items():        # fill gaps the primaries missed
        per_100g.setdefault(key, value)

    return Food(
        name=(item.get("description") or "").strip(),
        brand=_brand(item),
        barcode=str(item.get("gtinUpc") or "").strip(),
        per_100g=per_100g,
        serving_g=_serving_grams(item),
        source="USDA",
    )


def _is_kcal(nutrient: dict) -> bool:
    return (nutrient.get("unitName") or "").upper() in ("KCAL", "")


def _brand(item: dict) -> str:
    """A short provenance label: the brand for packaged items, else the
    FDC data type tidied up (so restaurant/survey foods read sensibly)."""
    brand = (item.get("brandName") or item.get("brandOwner") or "").strip()
    if brand:
        return brand.title() if brand.isupper() else brand
    data_type = (item.get("dataType") or "").strip()
    return {"Survey (FNDDS)": "USDA Survey",
            "SR Legacy": "USDA Reference",
            "Foundation": "USDA Foundation"}.get(data_type, data_type)


def _serving_grams(item: dict) -> float | None:
    unit = (item.get("servingSizeUnit") or "").lower()
    try:
        size = float(item.get("servingSize"))
    except (TypeError, ValueError):
        return None
    return size if size > 0 and unit in ("g", "gram", "grams") else None


# ── network (off the main thread) ──────────────────────────────────

def search(query: str, api_key: str, limit: int = 20) -> list[Food]:
    params = urllib.parse.urlencode({
        "query": query, "pageSize": limit, "api_key": api_key or DEMO_KEY})
    data = _get_json(f"{USDA_BASE}/foods/search?{params}")
    foods = [parse_usda_food(f) for f in (data.get("foods") or [])]
    return [f for f in foods if f.name and f.per_100g]


def lookup_barcode(code: str, api_key: str) -> Food | None:
    # FDC matches a UPC/GTIN when it's the query (Branded items).
    foods = search(code, api_key, limit=1)
    return foods[0] if foods else None


def _get_json(url: str) -> dict:
    # Log without the api_key query parameter.
    log.info("USDA FDC: GET %s", url.split("?")[0])
    with urllib.request.urlopen(url, timeout=_TIMEOUT) as resp:
        return json.load(resp)


# ── source ─────────────────────────────────────────────────────────

class UsdaSource(Source):
    id = "usda"
    name = "USDA"

    def __init__(self, settings):
        self._api_key = settings.get_string("usda-api-key") if settings else ""

    def search(self, query: str) -> list[Food]:
        return [replace(f, source="USDA")
                for f in search(query, self._api_key)]

    def lookup_barcode(self, code: str) -> Food | None:
        return lookup_barcode(code, self._api_key)
