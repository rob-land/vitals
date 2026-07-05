"""UCUM unit conversion to canonical storage units.

Each scalar type has one canonical UCUM unit (``canonical_unit`` in the
catalog). Senders may submit any *convertible* unit; the daemon converts to
canonical on insert and may convert back to a display unit on read. The set
of conversions is deliberately small and explicit — only the pairs the
catalog's ``display_units`` actually need — so an unexpected unit is
rejected rather than silently mis-stored.

Note on glucose: ``mg/dL`` <-> ``mmol/L`` is substance-specific (it depends
on molar mass). In this catalog ``mg/dL`` is used only by the glucose
types, so registering the glucose factor (18.0156) globally is safe. If a
non-glucose ``mg/dL`` type is ever added, gate the conversion on the type.
"""

from __future__ import annotations

from typing import Callable

GLUCOSE_MG_DL_PER_MMOL_L = 18.0156

# (from_unit, to_unit) -> callable converting a value from -> to.
_CONVERSIONS: dict[tuple[str, str], Callable[[float], float]] = {
    # mass
    ("g", "kg"): lambda v: v / 1000.0,
    ("kg", "g"): lambda v: v * 1000.0,
    ("[lb_av]", "kg"): lambda v: v * 0.45359237,
    ("kg", "[lb_av]"): lambda v: v / 0.45359237,
    ("[stone_av]", "kg"): lambda v: v * 6.35029318,
    ("kg", "[stone_av]"): lambda v: v / 6.35029318,
    # length
    ("km", "m"): lambda v: v * 1000.0,
    ("m", "km"): lambda v: v / 1000.0,
    ("mi", "m"): lambda v: v * 1609.344,
    ("m", "mi"): lambda v: v / 1609.344,
    ("[in_i]", "cm"): lambda v: v * 2.54,
    ("cm", "[in_i]"): lambda v: v / 2.54,
    # temperature (affine)
    ("[degF]", "Cel"): lambda v: (v - 32.0) * 5.0 / 9.0,
    ("Cel", "[degF]"): lambda v: v * 9.0 / 5.0 + 32.0,
    ("K", "Cel"): lambda v: v - 273.15,
    ("Cel", "K"): lambda v: v + 273.15,
    # energy
    ("kJ", "kcal"): lambda v: v / 4.184,
    ("kcal", "kJ"): lambda v: v * 4.184,
    # volume
    ("[foz_us]", "mL"): lambda v: v * 29.5735295625,
    ("mL", "[foz_us]"): lambda v: v / 29.5735295625,
    ("L", "mL"): lambda v: v * 1000.0,
    ("mL", "L"): lambda v: v / 1000.0,
    # duration
    ("s", "min"): lambda v: v / 60.0,
    ("min", "s"): lambda v: v * 60.0,
    ("h", "min"): lambda v: v * 60.0,
    ("min", "h"): lambda v: v / 60.0,
    # glucose mass <-> molar (see module note)
    ("mg/dL", "mmol/L"): lambda v: v / GLUCOSE_MG_DL_PER_MMOL_L,
    ("mmol/L", "mg/dL"): lambda v: v * GLUCOSE_MG_DL_PER_MMOL_L,
}


class UnitError(ValueError):
    """No known conversion between two units."""


def convert(value: float, from_unit: str | None, to_unit: str | None) -> float:
    """Convert ``value`` from ``from_unit`` to ``to_unit``.

    Units that are equal (or both falsy) pass through unchanged. Otherwise a
    registered converter is required; missing pairs raise ``UnitError``.
    """
    if from_unit == to_unit or not from_unit or not to_unit:
        return value
    try:
        return _CONVERSIONS[(from_unit, to_unit)](value)
    except KeyError:
        raise UnitError(f"no conversion from {from_unit!r} to {to_unit!r}")


def can_convert(from_unit: str | None, to_unit: str | None) -> bool:
    if from_unit == to_unit or not from_unit or not to_unit:
        return True
    return (from_unit, to_unit) in _CONVERSIONS
