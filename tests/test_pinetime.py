"""Tests for the PineTime / InfiniTime plugin."""

from vitals.devices.pinetime import (
    INFINITIME_MOTION_SERVICE,
    INFINITIME_STEP_COUNT_CHAR,
    PineTimeDevice,
)


# ── UUID constants ────────────────────────────────────────────────
# Pin to InfiniTime's documented Motion service UUIDs. The wire
# format is exercised by _parse_step_count, but only this guards the
# characteristic we actually read — a wrong UUID reads nothing and
# fails silently. See src/components/ble/MotionService.cpp upstream
# (base 0003yyxx-78fc-48fe-8e23-433b3a1942d0).

def test_motion_service_uuid_matches_infinitime():
    assert INFINITIME_MOTION_SERVICE == "00030000-78fc-48fe-8e23-433b3a1942d0"


def test_step_count_char_uuid_matches_infinitime():
    assert INFINITIME_STEP_COUNT_CHAR == "00030001-78fc-48fe-8e23-433b3a1942d0"


# ── matches() ─────────────────────────────────────────────────────

def test_matches_infinitime_advertised_name():
    assert PineTimeDevice.matches("InfiniTime", []) is True
    assert PineTimeDevice.matches("infinitime-1.10", []) is True


def test_matches_pinetime_advertised_name():
    """Some older / alternative firmware advertises as 'Pinetime-JF'."""
    assert PineTimeDevice.matches("Pinetime-JF", []) is True
    assert PineTimeDevice.matches("PineTime", []) is True


def test_matches_infinitime_motion_service_uuid():
    """Motion service UUID is unique to InfiniTime — claim by UUID."""
    assert PineTimeDevice.matches(None, [INFINITIME_MOTION_SERVICE]) is True
    assert PineTimeDevice.matches("Random",
                                  [INFINITIME_MOTION_SERVICE.upper()]) is True


def test_matches_unknown_device():
    assert PineTimeDevice.matches("Bangle.js a1b2", []) is False
    assert PineTimeDevice.matches(None, []) is False
    # Battery service alone is too generic to claim.
    assert PineTimeDevice.matches(None,
                                  ["0000180f-0000-1000-8000-00805f9b34fb"]) is False


# ── _parse_battery_level ──────────────────────────────────────────

def test_parse_battery_level_typical():
    assert PineTimeDevice._parse_battery_level(b"\x55") == 0x55  # 85
    assert PineTimeDevice._parse_battery_level(bytearray([0])) == 0
    assert PineTimeDevice._parse_battery_level(bytearray([100])) == 100


def test_parse_battery_level_out_of_range_rejected():
    """Anything > 100 in the byte means the char wasn't a battery level."""
    assert PineTimeDevice._parse_battery_level(bytearray([101])) is None
    assert PineTimeDevice._parse_battery_level(bytearray([255])) is None


def test_parse_battery_level_empty():
    assert PineTimeDevice._parse_battery_level(b"") is None
    assert PineTimeDevice._parse_battery_level(None) is None


# ── _parse_step_count ─────────────────────────────────────────────

def test_parse_step_count_typical():
    # 4321 steps, little-endian uint32 → 0xE1 0x10 0x00 0x00
    assert PineTimeDevice._parse_step_count(b"\xe1\x10\x00\x00") == 4321
    assert PineTimeDevice._parse_step_count(bytearray([0, 0, 0, 0])) == 0


def test_parse_step_count_short_or_empty():
    assert PineTimeDevice._parse_step_count(b"") is None
    assert PineTimeDevice._parse_step_count(b"\x01\x02") is None
    assert PineTimeDevice._parse_step_count(None) is None
