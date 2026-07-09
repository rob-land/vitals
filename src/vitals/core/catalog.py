"""The metric type catalog, loaded from ``record-types.yaml``.

The YAML is the single source of truth (see CLAUDE.md). This module loads
it once at startup into ``TypeDef`` objects the rest of the daemon queries.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

import yaml

from vitals.core import resources

log = logging.getLogger(__name__)

SENSITIVE_CATEGORY = "reproductive"

# Value shapes (see docs/design/02-data-model.md).
SCALAR = "scalar"
COMPONENTS = "components"
STRUCTURED = "structured"


@dataclass(frozen=True)
class TypeDef:
    key: str
    category: str
    title: str
    value_shape: str
    canonical_unit: str | None
    display_units: tuple[str, ...]
    interval: bool
    modalities: tuple[str, ...]
    meta_fields: tuple[str, ...]
    component_units: dict[str, str]  # for COMPONENTS types: name -> UCUM unit
    fhir: dict
    omh: dict | None
    gatt: dict | None
    notes: str | None = field(default=None)
    # Physiologically plausible display range (canonical units), or None.
    plausible: tuple[float, float] | None = field(default=None)
    # Aggregates by sum (additive) vs by average with a min–max range (point).
    additive: bool = field(default=False)
    # Reference "normal" band (canonical units) to shade behind charts, or None.
    normal_range: tuple[float, float] | None = field(default=None)

    @property
    def sensitive(self) -> bool:
        return self.category == SENSITIVE_CATEGORY

    def as_dict(self) -> dict:
        """Serialisable form for the ListTypes() reply."""
        return {
            "key": self.key,
            "category": self.category,
            "title": self.title,
            "value": self.value_shape,
            "canonical_unit": self.canonical_unit,
            "display_units": list(self.display_units),
            "interval": self.interval,
            "modalities": list(self.modalities),
            "components": self.component_units or None,
            "fhir": self.fhir,
            "sensitive": self.sensitive,
        }


class Catalog:
    def __init__(self, types: dict[str, TypeDef], version: int):
        self._types = types
        self.version = version

    @classmethod
    def load(cls) -> "Catalog":
        with open(resources.catalog_path(), encoding="utf-8") as fh:
            raw = yaml.safe_load(fh)
        types: dict[str, TypeDef] = {}
        for entry in raw.get("types", []):
            td = _parse_type(entry)
            types[td.key] = td
        log.info("catalog loaded: %d types (catalog version %s)", len(types), raw.get("version"))
        return cls(types, int(raw.get("version", 1)))

    def get(self, key: str) -> TypeDef | None:
        return self._types.get(key)

    def has(self, key: str) -> bool:
        return key in self._types

    def all(self) -> list[TypeDef]:
        return list(self._types.values())

    def sensitive_keys(self) -> set[str]:
        return {k for k, t in self._types.items() if t.sensitive}

    def as_json_obj(self) -> dict:
        return {
            "catalog_version": self.version,
            "types": [t.as_dict() for t in self._types.values()],
        }


# Physiologically plausible ranges in canonical units. A sample outside the
# range is almost certainly a sensor glitch (a 0-bpm heart rate, a 0-kg
# weight), so charts and aggregates drop it at display time — the raw record
# is never touched. Only point-in-time metrics where 0 / out-of-range is
# impossible appear here; additive metrics (steps, water) legitimately reach
# 0 and must not be filtered.
_PLAUSIBLE: dict[str, tuple[float, float]] = {
    "heart_rate": (20, 250),
    "resting_heart_rate": (20, 200),
    "heart_rate_variability": (1, 400),
    "respiratory_rate": (4, 60),
    "oxygen_saturation": (50, 100),
    "body_temperature": (25, 45),
    "basal_body_temperature": (25, 45),
    "body_weight": (2, 500),
    "body_mass_index": (5, 100),
    "body_fat_percentage": (1, 75),
    "lean_body_mass": (2, 300),
    "waist_circumference": (20, 250),
    "body_height": (30, 260),
    "blood_glucose": (0.5, 45),          # mmol/L (canonical)
    "continuous_glucose": (0.5, 45),
    "vo2_max": (10, 90),
}

# Reference "normal" band (canonical units) drawn behind the chart so a
# reading reads as normal / out-of-range at a glance. Only for metrics with
# a broadly-applicable healthy range that doesn't depend on context like
# activity or height (so heart_rate and body_weight are deliberately absent).
_NORMAL_RANGE: dict[str, tuple[float, float]] = {
    "resting_heart_rate": (50, 90),
    "oxygen_saturation": (95, 100),
    "respiratory_rate": (12, 20),
    "body_temperature": (36.1, 37.5),
    "body_mass_index": (18.5, 25.0),
    "blood_glucose": (4.0, 7.8),           # mmol/L, general reference
}

# Scalar metrics that aggregate by SUM despite not being interval
# measurements: discrete intake / dose events that accumulate over a day.
_ADDITIVE_KEYS: set[str] = {
    "water_intake", "dietary_energy", "caffeine_intake", "alcohol_intake",
    "insulin_dose",
}


def _parse_type(entry: dict) -> TypeDef:
    component_units: dict[str, str] = {}
    for name, spec in (entry.get("components") or {}).items():
        component_units[name] = spec.get("canonical_unit")
    return TypeDef(
        key=entry["key"],
        category=entry["category"],
        title=entry.get("title", entry["key"]),
        value_shape=entry["value"],
        canonical_unit=entry.get("canonical_unit"),
        display_units=tuple(entry.get("display_units") or []),
        interval=bool(entry.get("interval", False)),
        modalities=tuple(entry.get("modalities") or []),
        meta_fields=tuple(entry.get("meta_fields") or []),
        component_units=component_units,
        fhir=entry.get("fhir") or {},
        omh=entry.get("omh"),
        gatt=entry.get("gatt"),
        notes=entry.get("notes"),
        plausible=_PLAUSIBLE.get(entry["key"]),
        additive=bool(entry.get("interval", False))
        or entry["key"] in _ADDITIVE_KEYS,
        normal_range=_NORMAL_RANGE.get(entry["key"]),
    )
