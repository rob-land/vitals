"""Record validation and canonicalisation.

Turns an inbound JSON envelope (ISO-8601 times, value in any convertible
unit) into the internal storage form (epoch-ms UTC times, scalar value in
the type's canonical unit, structured bodies as JSON), and back again for
reads. Invalid envelopes raise ``InvalidRecord``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from vitals.core import catalog, units
from vitals.core.errors import InvalidRecord

_UTC = timezone.utc
_EPOCH = datetime(1970, 1, 1, tzinfo=_UTC)
_VALID_MODALITIES = {"sensed", "self_reported", "derived"}


@dataclass
class NormalizedRecord:
    uuid: str
    type: str
    schema_version: int
    effective_start: int          # epoch ms UTC
    effective_end: int | None
    value_num: float | None
    value_json: str | None
    unit: str | None
    modality: str
    meta_json: str | None
    device_id: str | None
    device_name: str | None


def iso_to_ms(value: str) -> int:
    try:
        dt = datetime.fromisoformat(value)
    except (ValueError, TypeError) as exc:
        raise InvalidRecord(f"invalid timestamp {value!r}: {exc}")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_UTC)
    return round(dt.timestamp() * 1000)


def ms_to_iso(ms: int) -> str:
    return (_EPOCH + timedelta(milliseconds=ms)).isoformat()


def _require(record: dict, key: str):
    if key not in record or record[key] is None:
        raise InvalidRecord(f"missing required field {key!r}")
    return record[key]


def _is_number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def validate_and_canonicalize(record: dict, td: catalog.TypeDef) -> NormalizedRecord:
    """Validate one envelope against its type and convert it to storage form."""
    uuid = _require(record, "uuid")
    if not isinstance(uuid, str) or not uuid:
        raise InvalidRecord("uuid must be a non-empty string")

    start = iso_to_ms(_require(record, "effective_start"))
    end_raw = record.get("effective_end")
    end = iso_to_ms(end_raw) if end_raw else None
    if end is not None and end < start:
        raise InvalidRecord("effective_end precedes effective_start")

    source = record.get("source") or {}
    modality = source.get("modality")
    if modality not in _VALID_MODALITIES:
        raise InvalidRecord(f"source.modality must be one of {sorted(_VALID_MODALITIES)}")

    value = record.get("value")
    value_num: float | None = None
    value_json: str | None = None
    unit: str | None = None

    if td.value_shape == catalog.SCALAR:
        if not _is_number(value):
            raise InvalidRecord(f"{td.key} requires a numeric value")
        submitted_unit = record.get("unit") or td.canonical_unit
        try:
            value_num = float(units.convert(float(value), submitted_unit, td.canonical_unit))
        except units.UnitError as exc:
            raise InvalidRecord(str(exc))
        unit = td.canonical_unit
    elif td.value_shape == catalog.COMPONENTS:
        if not isinstance(value, dict):
            raise InvalidRecord(f"{td.key} requires an object value")
        missing = [c for c in td.component_units if c not in value]
        if missing:
            raise InvalidRecord(f"{td.key} missing components: {missing}")
        for name in td.component_units:
            if not _is_number(value[name]):
                raise InvalidRecord(f"{td.key}.{name} must be numeric")
        value_json = json.dumps(value, separators=(",", ":"))
    elif td.value_shape == catalog.STRUCTURED:
        if not isinstance(value, dict):
            raise InvalidRecord(f"{td.key} requires an object value")
        value_json = json.dumps(value, separators=(",", ":"))
    else:  # pragma: no cover - guarded by the catalog
        raise InvalidRecord(f"unknown value shape {td.value_shape!r}")

    meta = record.get("meta")
    meta_json = json.dumps(meta, separators=(",", ":")) if meta else None

    return NormalizedRecord(
        uuid=uuid,
        type=td.key,
        schema_version=int(record.get("schema_version", 1)),
        effective_start=start,
        effective_end=end,
        value_num=value_num,
        value_json=value_json,
        unit=unit,
        modality=modality,
        meta_json=meta_json,
        device_id=source.get("device_id"),
        device_name=source.get("device_name"),
    )


def row_to_envelope(row: dict, display_unit: str | None = None) -> dict:
    """Build an output envelope from a stored row (a sqlite Row mapping).

    ``display_unit`` optionally converts a scalar value back from canonical.
    """
    env: dict = {
        "uuid": row["uuid"],
        "type": row["type"],
        "schema_version": row["schema_version"],
        "effective_start": ms_to_iso(row["effective_start"]),
        "effective_end": ms_to_iso(row["effective_end"]) if row["effective_end"] is not None else None,
        "source": {
            "app_id": row["app_id"],
            "device_id": row["device_id"] or None,
            "device_name": row["display_name"],
            "modality": row["modality"],
        },
        "created_at": ms_to_iso(row["created_at"]),
        "modified_at": ms_to_iso(row["modified_at"]),
    }
    if row["deleted"]:
        env["deleted"] = True

    if row["value_json"] is not None:
        env["value"] = json.loads(row["value_json"])
        env["unit"] = None
    else:
        value = row["value_num"]
        unit = row["unit"]
        if display_unit and display_unit != unit:
            value = units.convert(value, unit, display_unit)
            unit = display_unit
        env["value"] = value
        env["unit"] = unit

    if row["meta_json"]:
        env["meta"] = json.loads(row["meta_json"])
    return env
