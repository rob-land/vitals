"""Map standard Bluetooth GATT health services to decoders.

A device that advertises one of these services is read by connecting and
subscribing to the listed characteristic, then decoding each notification.
This single table is what lets one bridge support every standards-compliant
device (Polar/Wahoo straps, A&D scales, standard BP cuffs, Contour glucose
meters, Nonin oximeters, …) with no per-model code.

Proprietary devices (e.g. Xiaomi scales) are handled separately from
advertisement data — see plugin.py.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from vitals.devices.sensors import decoders


def uuid16(n: int) -> str:
    """Expand a 16-bit GATT UUID to its full 128-bit string."""
    return f"0000{n:04x}-0000-1000-8000-00805f9b34fb"


@dataclass(frozen=True)
class Characteristic:
    uuid: str
    decode: Callable[[bytes], dict | None]
    # "notify" streams (HR); "indicate" delivers one measurement then idles
    # (scale, BP, thermometer, glucose). bleak's start_notify handles both.
    mode: str = "notify"


# Service UUID -> the characteristics to subscribe to.
SERVICES: dict[str, list[Characteristic]] = {
    uuid16(0x180D): [Characteristic(uuid16(0x2A37), decoders.heart_rate, "notify")],
    uuid16(0x181D): [Characteristic(uuid16(0x2A9D), decoders.weight, "indicate")],
    uuid16(0x1810): [Characteristic(uuid16(0x2A35), decoders.blood_pressure, "indicate")],
    uuid16(0x1808): [Characteristic(uuid16(0x2A18), decoders.glucose, "notify")],
    uuid16(0x1822): [Characteristic(uuid16(0x2A5E), decoders.pulse_oximeter, "indicate")],
    uuid16(0x1809): [Characteristic(uuid16(0x2A1C), decoders.temperature, "indicate")],
}

# Devices that advertise a standard UUID but encode data in advertisements
# rather than the standard characteristic. Keyed by the service-data UUID.
ADVERTISEMENT_DECODERS: dict[str, Callable[[bytes], dict | None]] = {
    uuid16(0x181B): decoders.xiaomi_scale,   # Mi Body Composition Scale
}


def matches(service_uuids: list[str]) -> list[str]:
    """Return the standard health services present in an advertisement."""
    advertised = {u.lower() for u in service_uuids}
    return [svc for svc in SERVICES if svc in advertised]
