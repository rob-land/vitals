"""OMRON blood-pressure monitors — BP5465 (= HEM-7382T1) and siblings.

OMRON's Connect / Intelli-IT monitors do **not** expose the standard
Bluetooth Blood Pressure service. They use a proprietary "memory-map"
protocol: over a bonded link you send framed EEPROM-read commands to a TX
characteristic and the device streams raw EEPROM bytes back as
notifications on an RX characteristic, which you slice into fixed 16-byte
reading records. Reverse-engineered from OMRON Connect and cross-checked
against the omblepy project; see ``docs/omron-bp.md``.

Newer HEM-738xT1 units (incl. the BP5465) use service ``0xFE4A`` with a
plain OS-level bond and no unlock key. Opportunistic sensor: after a
measurement the monitor advertises as ``BLESmart_…``; we connect, dump
the per-user record regions, and record each reading as ``blood_pressure``
carrying pulse and the irregular-heartbeat / body-movement flags.

Frame format (both directions): ``[len][type][payload…][bcc]`` where
``len`` is the whole-frame length and ``bcc`` makes the XOR of the entire
frame zero.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from functools import reduce
from operator import xor

from bleak import BleakClient

from vitals.devices.base import Device, register_device

log = logging.getLogger(__name__)

SERVICE_NEW = "0000fe4a-0000-1000-8000-00805f9b34fb"       # HEM-738xT1
SERVICE_LEGACY = "ecbe3980-c9a2-11e1-b1bd-0002a5d5c51b"    # older Complete/EVOLV
TX_CHARS = [
    "db5b55e0-aee7-11e1-965e-0002a5d5c51b",
    "e0b8a060-aee7-11e1-92f4-0002a5d5c51b",
    "0ae12b00-aee8-11e1-a192-0002a5d5c51b",
    "10e1ba60-aee8-11e1-89e5-0002a5d5c51b",
]
RX_CHARS = [
    "49123040-aee8-11e1-a74d-0002a5d5c51b",
    "4d0bf320-aee8-11e1-a0d9-0002a5d5c51b",
    "5128ce60-aee8-11e1-b84b-0002a5d5c51b",
    "560f1420-aee8-11e1-8184-0002a5d5c51b",
]

_NAME_PREFIX = "BLESmart"
_RECORD_SIZE = 16
_MAX_RECORDS = 100
# Per-user record-region base addresses, hardware-confirmed on a BP5465
# (HEM-7382T1). Each region is a ring of up to 100 16-byte records; an empty
# slot returns a short ack (no data), which ends the walk.
_REGION_BASES = [0x0810, 0x0E50]


# ── pure protocol ─────────────────────────────────────────────────
def build_frame(data: bytes) -> bytes:
    """Wrap a command body (type byte + payload) as ``[len][data…][bcc]``."""
    body = bytes([len(data) + 2]) + bytes(data)
    return body + bytes([reduce(xor, body, 0)])


def frame_ok(frame: bytes) -> bool:
    """A frame is valid when its length byte matches and the whole-frame
    XOR is zero."""
    return (len(frame) >= 3 and frame[0] == len(frame)
            and reduce(xor, frame, 0) == 0)


START_FRAME = build_frame(bytes([0x00, 0x00, 0x00, 0x00, 0x10, 0x00]))
END_FRAME = build_frame(bytes([0x0F, 0x00, 0x00, 0x00, 0x00, 0x00]))


def read_frame(addr: int, size: int) -> bytes:
    """An EEPROM-read command for `size` bytes at big-endian `addr`."""
    return build_frame(bytes([0x01, 0x00, (addr >> 8) & 0xFF, addr & 0xFF,
                              size, 0x00]))


def read_response_data(frame: bytes) -> bytes | None:
    """The EEPROM bytes carried by an ``0x81`` read-response frame
    ``[len][81][00][addr_hi][addr_lo][size][data…][bcc]``. Returns None for
    a short ack (``[08]…`` with no data, which the monitor sends when the
    address is out of range)."""
    if not frame_ok(frame) or len(frame) < 8 or frame[1] != 0x81:
        return None
    size = frame[5]
    if frame[0] < size + 7:      # a short ack (no data) for an out-of-range slot
        return None
    return frame[6:6 + size]


def frames_from_buffer(buf: bytearray) -> list[bytes]:
    """Pull complete length-prefixed frames out of a rolling RX buffer,
    resyncing past any junk. Consumes what it returns."""
    out: list[bytes] = []
    while buf:
        length = buf[0]
        if length < 3 or length > 64:
            del buf[0]           # not a plausible header — resync
            continue
        if len(buf) < length:
            break                # wait for the rest of the frame
        out.append(bytes(buf[:length]))
        del buf[:length]
    return out


def parse_record(rec: bytes) -> dict | None:
    """One 16-byte BP5465 reading record → a reading dict, or None if it
    isn't a plausible measurement. Byte layout hardware-confirmed against a
    known 115/72/73 reading::

        0-1  u16 LE: hour b0-4, day b5-9, month b10-13, b14=IHB, b15=movement
        2-3  u16 LE: second b0-5, minute b6-11
        6    sequence number
        12   systolic - 25
        13   diastolic
        14   pulse
        15   year - 2000
    """
    if len(rec) < _RECORD_SIZE:
        return None
    systolic = rec[12] + 25
    diastolic = rec[13]
    pulse = rec[14]
    if not (40 <= systolic <= 300 and 20 <= diastolic <= 200):
        return None
    w01 = rec[0] | (rec[1] << 8)
    w23 = rec[2] | (rec[3] << 8)
    month = (w01 >> 10) & 0x0F
    day = (w01 >> 5) & 0x1F
    hour = w01 & 0x1F
    minute = (w23 >> 6) & 0x3F
    second = min(w23 & 0x3F, 59)
    if not (1 <= month <= 12 and 1 <= day <= 31 and hour < 24 and minute < 60):
        return None
    return {
        "systolic": systolic, "diastolic": diastolic, "pulse": pulse,
        "year": 2000 + (rec[15] & 0x3F),
        "month": month, "day": day, "hour": hour,
        "minute": minute, "second": second,
        "irregular_heartbeat": bool(w01 & 0x4000),
        "body_movement": bool(w01 & 0x8000),
        "no_time": False,
        "sequence": rec[6],
    }


def parse_records(blob: bytes) -> list[dict]:
    """Slice a raw EEPROM region into valid reading records."""
    out = []
    for i in range(0, len(blob) - _RECORD_SIZE + 1, _RECORD_SIZE):
        reading = parse_record(blob[i:i + _RECORD_SIZE])
        if reading:
            out.append(reading)
    return out


def build_bp_record(reading: dict, address: str, name: str) -> dict:
    """A parsed reading → a ``blood_pressure`` envelope, deduped on the
    device timestamp so re-dumps upsert to one record."""
    when = datetime(reading["year"], reading["month"], reading["day"],
                    reading["hour"], reading["minute"],
                    reading["second"]).astimezone()
    record = {
        "uuid": f"vitals:{address}:blood_pressure:{when.strftime('%Y%m%d%H%M')}",
        "type": "blood_pressure",
        "effective_start": when.isoformat(),
        "value": {"systolic": reading["systolic"],
                  "diastolic": reading["diastolic"]},
        "source": {"modality": "sensed", "device_id": address,
                   "device_name": name or "OMRON monitor"},
    }
    meta = {}
    if reading.get("pulse"):
        meta["pulse_rate"] = reading["pulse"]
    if reading.get("irregular_heartbeat"):
        meta["irregular_heartbeat"] = True
    if reading.get("body_movement"):
        meta["body_movement"] = True
    if meta:
        record["meta"] = meta
    return record


@register_device
class OmronBpDevice(Device):
    id = "omron-bp"
    display_name = "OMRON Blood Pressure"
    description = ("OMRON blood-pressure monitor — BP5465 / HEM-738xT1 and "
                  "siblings")

    INTERACTION = "opportunistic"
    CATEGORY = "blood_pressure"
    ICON_NAME = "bluetooth-symbolic"
    PAIRING_STEPS = [
        "In the OMRON Connect phone app, delete this monitor — it pairs "
        "with only one device at a time.",
        "With the monitor off, press and hold its Bluetooth/Connect button "
        "for about 3–5 seconds until a 'P' blinks on the display.",
        "Keep it near the phone and search.",
    ]
    SENSOR_QUALITY = {"blood_pressure": "high"}

    @classmethod
    def matches(cls, advertised_name, service_uuids) -> bool:
        if advertised_name and advertised_name.startswith(_NAME_PREFIX):
            return True
        advertised = {u.lower() for u in service_uuids}
        return SERVICE_NEW in advertised or SERVICE_LEGACY in advertised

    @classmethod
    def match_specificity(cls, advertised_name, service_uuids) -> int:
        return cls.MATCH_VENDOR_SERVICE

    @classmethod
    def match_advertisement(cls, device, advertisement) -> bool:
        name = advertisement.local_name or getattr(device, "name", None)
        return cls.matches(name, advertisement.service_uuids or [])

    async def connect(self) -> None:
        return None

    async def disconnect(self) -> None:
        return None

    async def get_battery(self) -> int | None:
        return None

    async def handle_advertisement(self, device, advertisement) -> list[dict]:
        readings = await self._sync(device)
        return [build_bp_record(r, self.address, self.name)
                for r in readings if not r["no_time"]]

    async def _sync(self, device) -> list[dict]:
        """Bond, run the memory-map session, and return parsed readings."""
        readings: list[dict] = []
        buf = bytearray()

        def on_rx(_char, data):
            buf.extend(data)

        try:
            async with BleakClient(device) as client:
                for rx in RX_CHARS:
                    try:
                        await client.start_notify(rx, on_rx)
                    except Exception:
                        pass
                if not await self._command(client, START_FRAME, buf):
                    return readings
                for base in _REGION_BASES:
                    readings.extend(
                        await self._read_records(client, buf, base))
                await self._command(client, END_FRAME, buf)
        except Exception:
            log.exception("OMRON BP read from %s failed", self.address)
        return readings

    async def _command(self, client, frame: bytes, buf: bytearray,
                       timeout: float = 4.0) -> bytes | None:
        """Write a command frame and await the next valid response frame."""
        buf.clear()
        await client.write_gatt_char(TX_CHARS[0], frame, response=True)
        waited = 0.0
        while waited < timeout:
            for candidate in frames_from_buffer(buf):
                if frame_ok(candidate):
                    return candidate
            await asyncio.sleep(0.05)
            waited += 0.05
        return None

    async def _read_records(self, client, buf: bytearray,
                            base: int) -> list[dict]:
        """Walk a per-user region record-by-record until an empty slot (the
        monitor answers those with a short ack carrying no data)."""
        out = []
        for i in range(_MAX_RECORDS):
            resp = await self._command(
                client, read_frame(base + i * _RECORD_SIZE, _RECORD_SIZE), buf)
            data = read_response_data(resp) if resp else None
            if not data:
                break
            reading = parse_record(data)
            if reading:
                out.append(reading)
        return out
