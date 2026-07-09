"""Tests for the device plugin registry + matching dispatch."""

import pytest

from vitals.devices import (
    Device,
    available_devices,
    matching_device,
    register_device,
)
from vitals.devices.base import _REGISTRY


def test_pinetime_registered():
    """The built-in PineTime plugin is discoverable via the registry.
    (Bangle/Pebble assertions return with their port.)"""
    assert "pinetime" in available_devices()


def test_register_requires_id():
    class Broken(Device):
        display_name = "broken"
        description = ""
        @classmethod
        def matches(cls, name, uuids): return False
        async def connect(self): pass
        async def disconnect(self): pass
        async def get_battery(self): return None

    with pytest.raises(ValueError, match="missing class-level"):
        register_device(Broken)


def test_register_rejects_duplicate_id():
    class FirstDup(Device):
        id = "dup_dev_test"
        display_name = "First"
        description = ""
        @classmethod
        def matches(cls, name, uuids): return False
        async def connect(self): pass
        async def disconnect(self): pass
        async def get_battery(self): return None

    class SecondDup(Device):
        id = "dup_dev_test"
        display_name = "Second"
        description = ""
        @classmethod
        def matches(cls, name, uuids): return False
        async def connect(self): pass
        async def disconnect(self): pass
        async def get_battery(self): return None

    register_device(FirstDup)
    try:
        with pytest.raises(ValueError, match="already registered"):
            register_device(SecondDup)
    finally:
        _REGISTRY.pop("dup_dev_test", None)


def test_matching_device_uses_first_match():
    """InfiniTime's advertised name is recognised."""
    cls = matching_device("InfiniTime", [])
    assert cls is not None
    assert cls.id == "pinetime"


def test_matching_device_no_match():
    """An unknown device returns None."""
    cls = matching_device("Random Device", ["00001800-0000-1000-8000-00805f9b34fb"])
    assert cls is None


# ── match specificity: most-specific plugin wins ─────────────────
# A real Yucheng ring (TK5) advertises its vendor service *and* Nordic
# UART *and* standard Heart Rate at once, so bangle (NUS), the sensor
# catch-all (HR) and the ring plugin (be94) all match. The vendor
# service is the strongest identity, so the ring must win.
_TK5_UUIDS = [
    "00001800-0000-1000-8000-00805f9b34fb",
    "0000180d-0000-1000-8000-00805f9b34fb",          # standard Heart Rate
    "0000ae00-0000-1000-8000-00805f9b34fb",          # Jieli
    "6e400001-b5a3-f393-e0a9-e50e24dcca9e",          # Nordic UART
    "be940000-7333-be46-b7ae-689e71722bd5",          # Yucheng vendor svc
]


def test_multi_service_ring_matches_vendor_plugin():
    assert matching_device("TK5 DEA8", _TK5_UUIDS).id == "yucheng_ring"


def test_bare_nordic_uart_still_matches_bangle():
    # A pure-NUS device (no vendor service) still goes to bangle.
    cls = matching_device("SomeGadget", ["6e400001-b5a3-f393-e0a9-e50e24dcca9e"])
    assert cls.id == "bangle"


def test_plain_hr_device_goes_to_sensor_plugin():
    # Standard Heart Rate only, no proprietary service → the catch-all.
    cls = matching_device("Polar H10", ["0000180d-0000-1000-8000-00805f9b34fb"])
    assert cls.id == "gatt-sensor"


def test_specificity_ordering():
    from vitals.devices.base import Device
    assert (Device.MATCH_VENDOR_SERVICE > Device.MATCH_SPECIFIC
            > Device.MATCH_SHARED_TRANSPORT > Device.MATCH_GENERIC_FALLBACK)


# ── External plugin discovery via entry_points ───────────────────

class _FakeEntryPoint:
    """Stand-in for importlib.metadata.EntryPoint that registers a
    Device when load() is called."""

    def __init__(self, name: str, value: str, on_load):
        self.name = name
        self.value = value
        self._on_load = on_load

    def load(self):
        self._on_load()


def _make_external_device_class(plugin_id: str):
    class ExternalDevice(Device):
        id = plugin_id
        display_name = "External"
        description = "External plugin under test"

        @classmethod
        def matches(cls, name, uuids):
            return name == "external-watch"

        async def connect(self): pass
        async def disconnect(self): pass
        async def get_battery(self): return None

    return ExternalDevice


def test_external_plugin_loaded_via_entry_point(monkeypatch):
    """An entry point under group `vitals.devices` is loaded on the
    next available_devices() call."""
    from vitals.devices import base as base_mod

    def _on_load():
        register_device(_make_external_device_class("ext_test_a"))

    fake_ep = _FakeEntryPoint("ext", "tock_ext.device", _on_load)

    monkeypatch.setattr(
        "importlib.metadata.entry_points",
        lambda group=None: [fake_ep] if group == "vitals.devices" else [],
    )
    base_mod.reset_external_loader_for_tests()
    try:
        assert "ext_test_a" in available_devices()
    finally:
        _REGISTRY.pop("ext_test_a", None)
        base_mod.reset_external_loader_for_tests()


def test_external_plugin_load_failure_does_not_break_discovery(monkeypatch, caplog):
    """A broken entry point gets logged and skipped — the rest of
    discovery continues."""
    from vitals.devices import base as base_mod

    def _bad_load():
        raise RuntimeError("simulated import failure")

    def _good_load():
        register_device(_make_external_device_class("ext_test_b"))

    fake_eps = [
        _FakeEntryPoint("bad",  "tock_bad.device",  _bad_load),
        _FakeEntryPoint("good", "tock_good.device", _good_load),
    ]
    monkeypatch.setattr(
        "importlib.metadata.entry_points",
        lambda group=None: fake_eps if group == "vitals.devices" else [],
    )
    base_mod.reset_external_loader_for_tests()
    try:
        with caplog.at_level("ERROR"):
            devs = available_devices()
        assert "ext_test_b" in devs
        # The bad plugin was logged.
        assert any("simulated import failure" in r.message
                   or "tock_bad" in r.message
                   for r in caplog.records)
    finally:
        _REGISTRY.pop("ext_test_b", None)
        base_mod.reset_external_loader_for_tests()


def test_external_loader_is_idempotent(monkeypatch):
    """Calling available_devices() twice doesn't re-import every
    external plugin (would re-register and raise duplicate-id)."""
    from vitals.devices import base as base_mod

    load_count = [0]

    def _on_load():
        load_count[0] += 1
        # Register only on first call so the test checks call count
        # without colliding with the duplicate-id guard.
        if load_count[0] == 1:
            register_device(_make_external_device_class("ext_test_c"))

    fake_ep = _FakeEntryPoint("ep", "tock_ep.device", _on_load)
    monkeypatch.setattr(
        "importlib.metadata.entry_points",
        lambda group=None: [fake_ep] if group == "vitals.devices" else [],
    )
    base_mod.reset_external_loader_for_tests()
    try:
        available_devices()
        available_devices()
        assert load_count[0] == 1
    finally:
        _REGISTRY.pop("ext_test_c", None)
        base_mod.reset_external_loader_for_tests()
