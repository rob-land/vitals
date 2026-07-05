"""Alarm model + per-device storage in the device registry.

Alarms live in each device's ``settings_json`` (the ``alarms`` key)
so they follow the watch they were created for. The wire format is
intentionally simple so it can be extended without a schema bump:

    [
      {"id":"abcd1234", "hour":7, "minute":30, "label":"Wake up",
       "days":127, "enabled":true},
      ...
    ]

`days` is a 7-bit mask: bit 0 = Monday, bit 6 = Sunday. 127 means
every day. The Alarm dataclass exposes helpers for the common cases
(weekdays-only, weekends-only, every-day, one-shot).
"""

from __future__ import annotations

import json
import secrets
from dataclasses import asdict, dataclass, field

# Day-of-week mask helpers. Bit 0 = Monday … bit 6 = Sunday.
DAYS_NEVER     = 0
DAYS_EVERY_DAY = 0b1111111
DAYS_WEEKDAYS  = 0b0011111
DAYS_WEEKENDS  = 0b1100000

# Friendly day-of-week labels for the UI; index = bit position.
DAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def _new_alarm_id() -> str:
    """Short hex token unique enough for the lifetime of one alarm row."""
    return secrets.token_hex(4)


@dataclass(frozen=True)
class Alarm:
    id: str = field(default_factory=_new_alarm_id)
    hour: int = 7
    minute: int = 0
    label: str = ""
    days: int = DAYS_EVERY_DAY
    enabled: bool = True

    def __post_init__(self):
        if not 0 <= self.hour <= 23:
            raise ValueError(f"hour out of range: {self.hour}")
        if not 0 <= self.minute <= 59:
            raise ValueError(f"minute out of range: {self.minute}")
        if not 0 <= self.days <= 0b1111111:
            raise ValueError(f"days mask out of range: {self.days:b}")

    @classmethod
    def from_dict(cls, d: dict) -> Alarm:
        return cls(
            id=str(d.get("id") or _new_alarm_id()),
            hour=int(d.get("hour", 7)),
            minute=int(d.get("minute", 0)),
            label=str(d.get("label", "")),
            days=int(d.get("days", DAYS_EVERY_DAY)),
            enabled=bool(d.get("enabled", True)),
        )

    def to_dict(self) -> dict:
        return asdict(self)

    def time_str(self) -> str:
        return f"{self.hour:02d}:{self.minute:02d}"

    def days_str(self) -> str:
        if self.days == DAYS_EVERY_DAY:
            return "Every day"
        if self.days == DAYS_WEEKDAYS:
            return "Weekdays"
        if self.days == DAYS_WEEKENDS:
            return "Weekends"
        if self.days == DAYS_NEVER:
            return "Once"
        chosen = [DAY_NAMES[i] for i in range(7) if self.days & (1 << i)]
        return ", ".join(chosen)


def serialize(alarms: list[Alarm]) -> str:
    """JSON-encode a list of alarms."""
    return json.dumps([a.to_dict() for a in alarms], separators=(",", ":"))


def deserialize(blob: str) -> list[Alarm]:
    """Parse a JSON blob back into Alarm objects.

    Any malformed entry is skipped (the field defaults take over in
    Alarm.from_dict). An empty / unparseable string returns []."""
    if not blob:
        return []
    try:
        raw = json.loads(blob)
    except json.JSONDecodeError:
        return []
    if not isinstance(raw, list):
        return []
    out: list[Alarm] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        try:
            out.append(Alarm.from_dict(entry))
        except (ValueError, TypeError):
            continue
    return out


# ── Per-device alarm storage ──────────────────────────────────────

def load_for_entry(entry) -> list[Alarm]:
    """Alarms stored on one registry entry (malformed entries skipped)."""
    out: list[Alarm] = []
    for raw in entry.settings.get("alarms", []):
        if not isinstance(raw, dict):
            continue
        try:
            out.append(Alarm.from_dict(raw))
        except (ValueError, TypeError):
            continue
    return out


def save_for_entry(manager, address: str, alarms: list[Alarm]) -> None:
    manager.update_settings(address, {"alarms": [a.to_dict() for a in alarms]})
