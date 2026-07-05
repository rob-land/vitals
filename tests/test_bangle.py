"""Tests for the Bangle.js device plugin (matching + REPL response parsing)."""

from vitals.devices.bangle import NUS_UUID, BangleDevice


# ── matches() ─────────────────────────────────────────────────────

def test_matches_advertised_name_prefix():
    assert BangleDevice.matches("Bangle.js a1b2", []) is True
    assert BangleDevice.matches("BANGLE.JS xx", []) is True
    assert BangleDevice.matches("bangle.js v2", []) is True


def test_matches_nordic_uart_service():
    """Watches that advertise NUS but no friendly name are still claimed."""
    assert BangleDevice.matches(None, [NUS_UUID]) is True
    assert BangleDevice.matches("", [NUS_UUID.upper()]) is True


def test_matches_unknown_device():
    assert BangleDevice.matches("Random Watch", []) is False
    assert BangleDevice.matches(None, []) is False
    assert BangleDevice.matches("Random",
                                ["00001800-0000-1000-8000-00805f9b34fb"]) is False


# ── _parse_battery ────────────────────────────────────────────────

def test_parse_battery_canonical_repl_response():
    raw = "print(E.getBattery())\r\n78\r\n=undefined\r\n>"
    assert BangleDevice._parse_battery(raw) == 78


def test_parse_battery_zero():
    raw = "print(E.getBattery())\r\n0\r\n=undefined\r\n>"
    assert BangleDevice._parse_battery(raw) == 0


def test_parse_battery_out_of_range_returns_none():
    """Print output outside 0..100 is not a valid battery percentage,
    even if it's a valid number. v0.3.x's parser would scan past it
    looking for the next in-range number; v0.4.0 trusts the print
    output line and returns None on bogus values."""
    raw = "print(E.getBattery())\r\n999\r\n=undefined\r\n>"
    assert BangleDevice._parse_battery(raw) is None


def test_parse_battery_no_number():
    """Print output is an error message — no number to parse."""
    raw = "print(E.getBattery())\r\nUncaught Error: ...\r\n=undefined\r\n>"
    assert BangleDevice._parse_battery(raw) is None
    assert BangleDevice._parse_battery("") is None


def test_parse_battery_real_world_bangle_v0_4_x_response():
    """Regression: the OnePlus 6T's Bangle firmware was missing
    `Bangle.getBattery` — the response contained 'at REPL (:1:14)'
    whose column number 1 was being picked up by v0.3.x's lenient
    parser as battery=1%. v0.4.0+ uses E.getBattery() which works
    on this firmware; this test pins the parser's behaviour against
    the old error response to ensure we don't regress to picking up
    column numbers."""
    error_response = (
        'print(Bangle.getBattery())\r\n'
        'Uncaught Error: Function "getBattery" not found!\r\n'
        '    at REPL (:1:14)\r\n'
        'print(Bangle.getBattery())\r\n'
        '             ^\r\n>'
    )
    # Strict parser sees no `\r\n=` so returns None (correct).
    assert BangleDevice._parse_battery(error_response) is None


# ── address / name init ───────────────────────────────────────────

def test_constructor_keeps_address_and_name():
    d = BangleDevice(address="AA:BB:CC:DD:EE:FF", name="Bangle.js a1b2")
    assert d.address == "AA:BB:CC:DD:EE:FF"
    assert d.name == "Bangle.js a1b2"


def test_constructor_falls_back_to_display_name_when_unnamed():
    d = BangleDevice(address="AA:BB:CC:DD:EE:FF")
    assert d.name == BangleDevice.display_name
