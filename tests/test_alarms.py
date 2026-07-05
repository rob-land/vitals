"""Tests for the Alarm dataclass + JSON serialisation."""

import json

import pytest

from vitals.alarms import (
    DAYS_EVERY_DAY,
    DAYS_NEVER,
    DAYS_WEEKDAYS,
    DAYS_WEEKENDS,
    Alarm,
    deserialize,
    load_for_entry,
    save_for_entry,
    serialize,
)


# ── Alarm dataclass ───────────────────────────────────────────────

def test_alarm_defaults():
    a = Alarm()
    assert a.hour == 7
    assert a.minute == 0
    assert a.days == DAYS_EVERY_DAY
    assert a.enabled is True
    assert a.id   # default factory generates a non-empty id


def test_alarm_validates_hour():
    with pytest.raises(ValueError, match="hour"):
        Alarm(hour=25)
    with pytest.raises(ValueError, match="hour"):
        Alarm(hour=-1)


def test_alarm_validates_minute():
    with pytest.raises(ValueError, match="minute"):
        Alarm(minute=60)


def test_alarm_validates_days_mask():
    with pytest.raises(ValueError, match="days"):
        Alarm(days=128)  # 8 bits set


# ── Display helpers ───────────────────────────────────────────────

def test_alarm_time_str_zero_pads():
    assert Alarm(hour=7, minute=5).time_str() == "07:05"
    assert Alarm(hour=23, minute=59).time_str() == "23:59"


def test_alarm_days_str_named_presets():
    assert Alarm(days=DAYS_EVERY_DAY).days_str() == "Every day"
    assert Alarm(days=DAYS_WEEKDAYS).days_str() == "Weekdays"
    assert Alarm(days=DAYS_WEEKENDS).days_str() == "Weekends"
    assert Alarm(days=DAYS_NEVER).days_str() == "Once"


def test_alarm_days_str_custom_mask():
    """Bit 0=Mon, bit 4=Fri → 'Mon, Fri'."""
    a = Alarm(days=0b0010001)
    assert a.days_str() == "Mon, Fri"


# ── Serialise / deserialise round-trip ────────────────────────────

def test_serialize_roundtrip():
    alarms = [
        Alarm(hour=7,  minute=30, label="Wake",   days=DAYS_WEEKDAYS),
        Alarm(hour=22, minute=0,  label="Sleep", days=DAYS_EVERY_DAY,
              enabled=False),
    ]
    blob = serialize(alarms)
    restored = deserialize(blob)
    assert len(restored) == 2
    assert restored[0].label == "Wake"
    assert restored[0].days == DAYS_WEEKDAYS
    assert restored[1].enabled is False


def test_deserialize_empty():
    assert deserialize("") == []
    assert deserialize("not json") == []


def test_deserialize_skips_malformed_entries():
    """A malformed entry doesn't break the rest of the list."""
    raw = json.dumps([
        {"hour": 7, "minute": 0, "label": "ok"},
        "garbage",
        {"hour": 99, "minute": 0, "label": "bad-hour"},  # validates -> skipped
        {"hour": 9, "minute": 30, "label": "ok2"},
    ])
    out = deserialize(raw)
    labels = [a.label for a in out]
    assert labels == ["ok", "ok2"]


def test_deserialize_non_list_returns_empty():
    assert deserialize('{"hour": 7}') == []
    assert deserialize("42") == []


# ── Per-device storage ───────────────────────────────────────────

class _FakeSettings:
    """In-memory stand-in for Gio.Settings — only get/set string."""

    def __init__(self, initial: dict | None = None):
        self._d = dict(initial or {})

    def get_string(self, key: str) -> str:
        return self._d.get(key, "")

    def set_string(self, key: str, value: str) -> None:
        self._d[key] = value


# ── per-device storage (device registry) ─────────────────────────

class _FakeEntry:
    def __init__(self, alarms=None):
        self.settings = {"alarms": alarms} if alarms is not None else {}


class _FakeManager:
    def __init__(self):
        self.saved = {}

    def update_settings(self, address, updates):
        self.saved.setdefault(address, {}).update(updates)


def test_load_for_entry_empty():
    assert load_for_entry(_FakeEntry()) == []


def test_load_for_entry_skips_malformed():
    entry = _FakeEntry([
        {"id": "a1", "hour": 7, "minute": 30},
        "not-a-dict",
        {"id": "a2", "hour": 99},          # out of range -> skipped
        {"id": "a3", "hour": 22, "minute": 5},
    ])
    ids = [a.id for a in load_for_entry(entry)]
    assert ids == ["a1", "a3"]


def test_save_for_entry_roundtrip():
    manager = _FakeManager()
    alarms = [Alarm(id="x1", hour=6, minute=45, days=DAYS_WEEKDAYS)]
    save_for_entry(manager, "AA:BB", alarms)
    stored = manager.saved["AA:BB"]["alarms"]
    assert stored == [alarms[0].to_dict()]
    assert [a.id for a in load_for_entry(_FakeEntry(stored))] == ["x1"]
