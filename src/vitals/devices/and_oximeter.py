"""A&D UP-200BLE fingertip pulse oximeter.

The UP-200BLE is an A&D-rebadged Contec CMS50D-family oximeter: it speaks
the Contec real-time protocol over a custom GATT service (write a START
command, then SpO2 + pulse stream as framed notifications). Protocol
reverse-engineered from the A&D Heart Track app (Blutter decompilation of
its Dart ``ContecPulseoxCms50dDevice``) and confirmed on the wire — see
``docs/and-oximeter.md``.

Opportunistic sensor: while the oximeter is worn it advertises, so the
ScanBroker routes each advertisement here; we connect briefly, capture a
window of readings, and record the median SpO2 and pulse. As a dedicated
instrument its readings are rated ``high`` so they outrank a watch's or
ring's estimate of the same metric.

Frame format (both directions): ``[header][payload…][checksum]`` where the
header byte always has bit 7 set and ``checksum = sum(previous) & 0x7F``.
The real-time SpO2+PR notification is a 16-byte ``0xEB 0x01`` frame.

NOTE: parked / unproven on hardware — the real UP-200BLE connects but
withholds data (all-zero characteristics, no frames) despite the exact
app handshake, so a firmware-level precondition invisible to the
decompiler is still missing. The decode below is verified from the app
source and unit-tested; the on-wire trigger is not yet confirmed. See
``docs/and-oximeter.md``.
"""

from __future__ import annotations

import asyncio
import logging
import statistics
import time
from datetime import datetime, timezone
from typing import ClassVar

from bleak import BleakClient

from vitals.devices.base import Device, register_device

log = logging.getLogger(__name__)

SERVICE = "0000ff12-0000-1000-8000-00805f9b34fb"
WRITE_CHAR = "0000ff01-0000-1000-8000-00805f9b34fb"   # master → slave
NOTIFY_CHAR = "0000ff02-0000-1000-8000-00805f9b34fb"  # slave → master

def _frame(header: int, *payload: int) -> bytes:
    """A Contec command frame: header, payload, then a 7-bit sum checksum.
    The frames are SHORT — no zero padding. (A btsnoop of the vendor app
    showed the device silently ignores the padded form.)"""
    return bytes([header, *payload, (header + sum(payload)) & 0x7F])


# Control frames written to FF01 (write-without-response), byte-exact from a
# capture of A&D Heart Track driving this unit.
CONN_NOTIFY = _frame(0x9A)            # 9a 1a — announce the connection
START_SPO2_PR = _frame(0x9B, 0x01)    # 9b 01 1c — start SpO2 + pulse stream
STOP = _frame(0x9B, 0x7F)             # 9b 7f 1a

_NAME_PREFIX = "UP-200BLE"
_READ_WINDOW_S = 8       # how long to stream once connected
_COOLDOWN_S = 45         # min gap between reads of the same worn device
_SPO2_INVALID = 127      # 0x7F → no finger / invalid

# Frame header → total length. 0xEB's length depends on its type byte; a
# real-time SpO2+PR frame is 8 bytes (confirmed on the wire).
_FIXED_LEN = {0x9A: 2, 0x9B: 3, 0xF3: 3}
_EB_LEN = {0x00: 5, 0x01: 8, 0x7F: 3}


def frame_length(buf, i: int) -> int | None:
    """Total length of the frame starting at ``buf[i]``: an int, ``None``
    if more bytes are needed to decide (a 0xEB awaiting its type byte),
    or ``-1`` if the header is unknown."""
    header = buf[i]
    if header == 0xEB:
        if i + 1 >= len(buf):
            return None
        return _EB_LEN.get(buf[i + 1], -1)
    return _FIXED_LEN.get(header, -1)


def reassemble(buf: bytearray) -> list[bytes]:
    """Pull complete Contec frames out of a rolling notify buffer. A frame
    starts at any byte with bit 7 set; leading non-header bytes are
    dropped. Consumes what it returns (and the skipped bytes) from
    ``buf``, leaving any partial trailing frame for the next call."""
    frames: list[bytes] = []
    i = 0
    while i < len(buf):
        if not (buf[i] & 0x80):
            i += 1
            continue
        length = frame_length(buf, i)
        if length is None:
            break                       # 0xEB awaiting its type byte
        if length == -1:
            i += 1                       # unknown header, skip a byte
            continue
        if i + length > len(buf):
            break                        # wait for the rest of the frame
        frames.append(bytes(buf[i:i + length]))
        i += length
    del buf[:i]
    return frames


def valid_checksum(frame: bytes) -> bool:
    return len(frame) >= 2 and (sum(frame[:-1]) & 0x7F) == frame[-1]


def decode_spo2_pr(frame: bytes) -> tuple[int, int] | None:
    """A real-time SpO2+PR frame (``0xEB 0x01``, 16 bytes) → ``(spo2,
    pulse)``, or ``None`` if it's a different frame, fails its checksum,
    or reads invalid (no finger)."""
    if len(frame) != 8 or frame[0] != 0xEB or frame[1] != 0x01:
        return None
    if not valid_checksum(frame):
        return None
    pulse = ((frame[2] & 0x02) << 6) | frame[3]   # 7-bit low + carried top bit
    spo2 = frame[4]
    if spo2 in (0, _SPO2_INVALID) or pulse in (0, 255):
        return None
    return spo2, pulse


def build_reading_record(type_key: str, value: int, unit: str,
                         address: str, name: str,
                         now: float | None = None) -> dict:
    """Pure: one scalar reading → an envelope with a dedup-friendly uuid
    (one record per address/type/minute/value)."""
    ts = time.time() if now is None else now
    return {
        "uuid": f"vitals:{address}:{type_key}:{int(ts // 60)}:{int(value)}",
        "type": type_key,
        "effective_start": datetime.fromtimestamp(
            ts, tz=timezone.utc).astimezone().isoformat(),
        "value": value,
        "unit": unit,
        "source": {"modality": "sensed", "device_id": address,
                   "device_name": name or "Pulse oximeter"},
    }


@register_device
class AndOximeterDevice(Device):
    id = "and-oximeter"
    display_name = "A&D Pulse Oximeter"
    description = "A&D UP-200BLE fingertip pulse oximeter — SpO2 and pulse"
    CATEGORY = "oximeter"
    PAIRING_STEPS = [
        "Turn the oximeter on by clipping it onto your finger.",
        "Keep it near the phone and search.",
    ]

    INTERACTION = "opportunistic"

    # A purpose-built oximeter: its SpO2 and pulse outrank a watch or ring.
    SENSOR_QUALITY = {"oxygen_saturation": "high", "heart_rate": "high"}

    # Throttle reconnects while the device is worn (fresh instance per
    # advertisement, so the cooldown lives on the class).
    _last_read: ClassVar[dict[str, float]] = {}

    @classmethod
    def matches(cls, advertised_name, service_uuids) -> bool:
        if advertised_name and advertised_name.startswith(_NAME_PREFIX):
            return True
        return SERVICE in {u.lower() for u in service_uuids}

    @classmethod
    def match_specificity(cls, advertised_name, service_uuids) -> int:
        return cls.MATCH_VENDOR_SERVICE

    @classmethod
    def match_advertisement(cls, device, advertisement) -> bool:
        name = advertisement.local_name or getattr(device, "name", None)
        return cls.matches(name, advertisement.service_uuids or [])

    # Opportunistic sensors have no session lifecycle.
    async def connect(self) -> None:
        return None

    async def disconnect(self) -> None:
        return None

    async def get_battery(self) -> int | None:
        return None

    async def handle_advertisement(self, device, advertisement) -> list[dict]:
        now = time.time()
        if now - self._last_read.get(self.address, 0.0) < _COOLDOWN_S:
            return []
        self._last_read[self.address] = now            # throttle even on miss

        samples = await self._capture(device)
        if not samples:
            return []
        spo2 = round(statistics.median(s[0] for s in samples))
        pulse = round(statistics.median(s[1] for s in samples))
        ts = time.time()
        return [
            build_reading_record("oxygen_saturation", spo2, "%",
                                 self.address, self.name, ts),
            build_reading_record("heart_rate", pulse, "/min",
                                 self.address, self.name, ts),
        ]

    async def _capture(self, device) -> list[tuple[int, int]]:
        """Connect, start real-time streaming, and collect valid
        SpO2+pulse samples for one read window."""
        buf = bytearray()
        samples: list[tuple[int, int]] = []

        def on_notify(_ch, data):
            buf.extend(data)
            for frame in reassemble(buf):
                sample = decode_spo2_pr(frame)
                if sample:
                    samples.append(sample)

        try:
            async with BleakClient(device) as client:
                # Enabling notify writes the FF02 CCCD; then announce the
                # connection and request the SpO2+PR stream. No bond, no
                # clock-set needed (confirmed by the vendor-app capture).
                await client.start_notify(NOTIFY_CHAR, on_notify)
                await client.write_gatt_char(WRITE_CHAR, CONN_NOTIFY,
                                             response=False)
                await asyncio.sleep(0.2)
                await client.write_gatt_char(WRITE_CHAR, START_SPO2_PR,
                                             response=False)
                await asyncio.sleep(_READ_WINDOW_S)
                try:
                    await client.write_gatt_char(WRITE_CHAR, STOP,
                                                 response=False)
                except Exception:
                    pass
        except Exception:
            log.exception("oximeter read from %s failed", self.address)
        return samples
