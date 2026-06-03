"""Pure value-formatting and chart-scaling helpers.

No GTK imports here on purpose: this is the unit-tested core that the pages
and chart widgets build on.
"""

from __future__ import annotations

import math

# UCUM canonical unit -> short display label.
_UNIT_LABELS = {
    "/min": "bpm",
    "{steps}": "steps",
    "{floors}": "floors",
    "Cel": "°C",
    "mm[Hg]": "mmHg",
    "mmol/L": "mmol/L",
    "kcal": "kcal",
    "kg": "kg",
    "cm": "cm",
    "m": "m",
    "%": "%",
    "ms": "ms",
}

# Types shown without decimals.
_INTEGER_UNITS = {"/min", "{steps}", "{floors}", "mm[Hg]", "%", "kcal", "ms"}


def unit_label(unit: str | None) -> str:
    if not unit:
        return ""
    return _UNIT_LABELS.get(unit, unit)


def format_value(value, unit: str | None = None) -> str:
    """Render a numeric value for display, choosing sensible precision."""
    if value is None:
        return "—"  # em dash
    try:
        value = float(value)
    except (TypeError, ValueError):
        return str(value)
    if unit in _INTEGER_UNITS or value == int(value):
        return f"{int(round(value)):,}"
    return f"{value:,.1f}"


def format_measurement(value, unit: str | None = None) -> str:
    """Value plus its unit label, e.g. ``72 bpm`` or ``68.4 kg``."""
    label = unit_label(unit)
    text = format_value(value, unit)
    return f"{text} {label}".strip()


def humanize_key(key: str) -> str:
    """Fallback title for a type key when the catalog has none."""
    return key.replace("_", " ").strip().capitalize()


def nice_max(values, floor: float = 0.0) -> float:
    """A 'nice' axis maximum at least as large as the data (and ``floor``).

    Rounds up to 1, 2, 2.5 or 5 times a power of ten so chart gridlines
    land on readable numbers.
    """
    peak = max([v for v in values if v is not None] + [floor], default=0.0)
    if peak <= 0:
        return 1.0
    magnitude = 10 ** math.floor(math.log10(peak))
    for mult in (1, 2, 2.5, 5, 10):
        if mult * magnitude >= peak:
            return mult * magnitude
    return 10 * magnitude
