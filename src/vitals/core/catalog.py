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
    )
