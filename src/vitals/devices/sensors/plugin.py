"""The generic standard-GATT health-sensor plugin (from gauge).

One opportunistic plugin covers every standards-compliant sensor — BP
cuffs, scales, glucose meters, oximeters, thermometers, HR straps —
because the work is all in the service table (gatt.py) and the pure
decoders. These devices wake up to take a measurement, advertise
briefly, and go back to sleep, so there is no session to run: the
ScanBroker routes each advertisement here while the device is
registered.

Two read paths, straight from gauge's bridge:

  * advertisement-only devices (Xiaomi scales) decode from
    ``service_data`` without connecting;
  * standard-service devices get a short connect + subscribe, and each
    notification decodes to a reading.

Record uuids are deterministic over (address, type, minute, value), so
the burst of identical advertisements a scale emits during one weigh-in
upserts to a single record.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from datetime import datetime, timezone

from bleak import BleakClient

from vitals.devices.base import Device, register_device
from vitals.devices.sensors import gatt

log = logging.getLogger(__name__)

# How long to keep the link open waiting for a measurement indication.
_READ_WINDOW_S = 8


def build_sensor_record(reading: dict, address: str, name: str,
                        now: float | None = None) -> dict:
    """Pure: one decoded reading → an envelope with a dedup-friendly uuid."""
    ts = time.time() if now is None else now
    value_key = hashlib.sha256(
        json.dumps(reading["value"], sort_keys=True).encode()).hexdigest()[:8]
    record = {
        "uuid": f"vitals:{address}:{reading['type']}:{int(ts // 60)}:{value_key}",
        "type": reading["type"],
        "effective_start": datetime.fromtimestamp(
            ts, tz=timezone.utc).astimezone().isoformat(),
        "value": reading["value"],
        "source": {"modality": "sensed", "device_id": address,
                   "device_name": name or "BLE sensor"},
    }
    if reading.get("unit"):
        record["unit"] = reading["unit"]
    if reading.get("meta"):
        record["meta"] = reading["meta"]
    return record


@register_device
class GattSensorDevice(Device):
    id = "gatt-sensor"
    display_name = "Health Sensor"
    description = ("Standard Bluetooth health sensor — blood pressure, "
                   "scale, glucose, SpO2, thermometer, heart-rate strap")

    INTERACTION = "opportunistic"

    @classmethod
    def matches(cls, advertised_name, service_uuids) -> bool:
        advertised = {u.lower() for u in service_uuids}
        known = set(gatt.SERVICES) | set(gatt.ADVERTISEMENT_DECODERS)
        return bool(advertised & known)

    @classmethod
    def match_advertisement(cls, device, advertisement) -> bool:
        if any(u.lower() in gatt.ADVERTISEMENT_DECODERS
               for u in (advertisement.service_data or {})):
            return True
        return bool(gatt.matches(advertisement.service_uuids or []))

    # Opportunistic sensors have no session lifecycle.
    async def connect(self) -> None:
        return None

    async def disconnect(self) -> None:
        return None

    async def get_battery(self) -> int | None:
        return None

    async def handle_advertisement(self, device, advertisement) -> list[dict]:
        envelopes: list[dict] = []
        for svc_uuid, raw in (advertisement.service_data or {}).items():
            decode = gatt.ADVERTISEMENT_DECODERS.get(svc_uuid.lower())
            if decode:
                reading = decode(bytes(raw))
                if reading:
                    envelopes.append(self._envelope(reading))
        if envelopes:
            return envelopes

        matched = gatt.matches(advertisement.service_uuids or [])
        if matched:
            readings = await self._read_connected(device, matched)
            envelopes.extend(self._envelope(r) for r in readings)
        return envelopes

    def _envelope(self, reading: dict) -> dict:
        return build_sensor_record(reading, self.address, self.name)

    async def _read_connected(self, device, matched: list[str]) -> list[dict]:
        """Short connect + subscribe window; collect decoded readings."""
        import asyncio

        readings: list[dict] = []

        def on_packet(characteristic):
            def handler(_c, data):
                try:
                    reading = characteristic.decode(bytes(data))
                except Exception:
                    log.exception("decode failed for %s", characteristic.uuid)
                    return
                if reading:
                    readings.append(reading)
            return handler

        try:
            async with BleakClient(device) as client:
                for svc in matched:
                    for ch in gatt.SERVICES[svc]:
                        await client.start_notify(ch.uuid, on_packet(ch))
                await asyncio.sleep(_READ_WINDOW_S)
        except Exception:
            log.exception("read from %s failed", self.address)
        return readings
