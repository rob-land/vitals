"""A&D Medical UC-450BLE body-composition scale.

The UC-450BLE does **not** speak the standard Bluetooth Weight Scale
(``0x181D``) or Body Composition (``0x181B``) services. It uses a framed
proprietary transport over a custom command service (``0xA602``) — the
protocol is a Lifesense OEM design (the scale's DIS manufacturer string
reads "Lifesense"). Originally transcribed from the A&D Heart Track
Android app (Blutter decompile of ``flutter_bluetooth_devices/.../scale/
uc450``) and then corrected against a btsnoop capture of the vendor app
driving real hardware (2026-07-17); see ``docs/and-uc450.md`` and
``docs/reverse-engineered-protocols.md``.

Opportunistic sensor: after a weigh-in the scale advertises as
``UC-450BLE``; the ScanBroker routes the advertisement here and we
connect, subscribe to the three scale→phone characteristics (which
prompts the scale to open with its login request), answer the handshake,
set the clock, and drain measurements, recording each as ``body_weight``
(plus any body-composition metrics the frame carries).

Wire framing (both directions), no CRC::

    [frag][len][cmd_hi cmd_lo][payload…]

``frag`` is a fragmentation nibble-pair (high = total packet count, low =
packet index); a single-packet frame is ``0x10`` and an **ACK** frame is
``0x00`` (packet count 0). ``len`` counts the command **and** payload
bytes (``2 + len(payload)``). ``cmd`` is a **big-endian** u16 command
code — the enum's *canonical* value from the decompile (the app's
``getByte == fromByte << 1`` is an internal artifact; the capture shows
canonical codes on the wire in both directions).

Auth is a plain **identity echo** — the scale's login request carries a
4-byte code that the phone echoes back; there is no crypto, nothing is
persisted between sessions, and the link is unencrypted (no BLE bond —
the scale actively refuses ``pair()``).
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import ClassVar

from vitals.devices.base import Device, register_device

log = logging.getLogger(__name__)

# ── GATT wiring (custom service 0xA602) ───────────────────────────
_BASE = "0000{:04x}-0000-1000-8000-00805f9b34fb"
SERVICE = _BASE.format(0xA602)
# Upstream = scale→phone measurement/notify pipe; host acks on 0xA622.
UPSTREAM_INDICATE = _BASE.format(0xA620)
UPSTREAM_NOTIFY = _BASE.format(0xA621)
UPSTREAM_ACK_WRITE = _BASE.format(0xA622)
# Downstream = phone→scale command pipe; scale acks on 0xA625.
DOWNSTREAM_WRITE = _BASE.format(0xA624)
DOWNSTREAM_ACK_NOTIFY = _BASE.format(0xA625)
DIS_SERIAL = _BASE.format(0x2A25)

_NAME_PREFIX = "UC-450"

# ── command codes (on-wire, big-endian u16) ───────────────────────
# Canonical enum codes, confirmed on the wire by the 2026-07-17 capture.
CMD_REGISTRATION = 0x0001        # phone → scale (unused by the vendor app)
CMD_REGISTRATION_RESP = 0x0002   # scale → phone
CMD_BINDING = 0x0003             # phone → scale
CMD_BINDING_RESP = 0x0004        # scale → phone
CMD_LOGIN_REQUEST = 0x0007       # scale → phone (hello + identity code)
CMD_LOGIN_RESPONSE = 0x0008      # phone → scale (echo)
CMD_INIT_REQUEST = 0x0009        # scale → phone (asks for a property)
CMD_INIT_RESPONSE = 0x000A       # phone → scale (property + clock)
CMD_SET_TIME = 0x1002            # phone → scale
CMD_SET_UNIT = 0x1004            # phone → scale
CMD_SETTING_RESP = 0x1000        # scale → phone (downstream setting ack)
CMD_SYNC_REQUEST = 0x4801        # phone → scale
CMD_MEASUREMENT_DATA = 0x4802    # scale → phone (one reading)

_FRAG_SINGLE = 0x10              # one packet, index 0
_FRAG_ACK = 0x00                # packet count 0 marks an ACK frame
_TIME_PROPERTY = 0x03           # set-time property id (capture constant)

# Presence bits in the measurement flags word (u16 @ payload[4]).
_UNIT_MASK = 0x0003
_WITH_USER_NUMBER = 1 << 2
_WITH_UTC = 1 << 3
_WITH_TIMEZONE = 1 << 4
_WITH_TIMESTAMP = 1 << 5
_WITH_BMI = 1 << 6
_WITH_BODY_FAT = 1 << 7
_WITH_BASAL_METABOLISM = 1 << 8
_WITH_MUSCLE_PCT = 1 << 9
_WITH_MUSCLE_MASS = 1 << 10
_WITH_FAT_FREE_MASS = 1 << 11
_WITH_SOFT_LEAN_MASS = 1 << 12
_WITH_BODY_WATER_MASS = 1 << 13
_WITH_IMPEDANCE = 1 << 14

_UNITS = {0: "kg", 1: "lb", 2: "st", 3: "catty"}


# ── pure protocol ─────────────────────────────────────────────────
def build_frame(command: int, payload: bytes = b"") -> bytes:
    """Wrap a command + payload as ``[0x10][2+len][cmd_be16][payload…]``
    (the length byte covers the command bytes too)."""
    return (bytes([_FRAG_SINGLE, 2 + len(payload),
                   (command >> 8) & 0xFF, command & 0xFF]) + bytes(payload))


def frame_ok(frame: bytes) -> bool:
    """A frame is structurally valid when its declared length (command +
    payload, from byte 2) is fully present."""
    return len(frame) >= 4 and frame[1] >= 2 and len(frame) >= 2 + frame[1]


def is_ack(frame: bytes) -> bool:
    """Whether a frame is an ACK (fragmentation packet-count nibble 0)."""
    return len(frame) >= 1 and (frame[0] >> 4) == 0


def parse_frame(frame: bytes) -> tuple[int, bytes] | None:
    """Split a validated data frame into ``(command, payload)``. Returns
    None for an ACK frame or a malformed one."""
    if is_ack(frame) or not frame_ok(frame):
        return None
    command = (frame[2] << 8) | frame[3]
    return command, bytes(frame[4:2 + frame[1]])


def build_ack() -> bytes:
    """The transport ACK for a received data frame: ``00 01 01``. Both
    sides acknowledge every data frame with exactly these three bytes
    (host on ``0xA622``, scale on ``0xA625``)."""
    return bytes([_FRAG_ACK, 0x01, 0x01])


def parse_login_request(payload: bytes) -> dict | None:
    """Decode the scale's login request (command ``0x0007``), its opening
    frame after the host subscribes: ``[01][code 4B][01][bound][battery]``.
    The 4-byte identity code must be echoed back; ``battery`` is percent.
    """
    if len(payload) < 5:
        return None
    out = {"code": bytes(payload[1:5])}
    if len(payload) >= 8:
        out["battery_percent"] = payload[7]
    return out


def build_login_response(code: bytes, bound: bool = True) -> bytes:
    """Answer the scale's login request (command ``0x0008``): echo the
    4-byte identity code — ``[01][01][code 4B][01][bound][02]``.

    The vendor app sends ``bound=0`` on first-time onboarding (after which
    the scale asks for init + binding) and ``bound=1`` thereafter. The
    scale never learns the phone's identity, so claiming ``bound=1`` works
    from any host once the scale has been set up.
    """
    code = bytes(code[:4]).ljust(4, b"\x00")
    return build_frame(CMD_LOGIN_RESPONSE,
                       bytes([0x01, 0x01]) + code
                       + bytes([0x01, 0x01 if bound else 0x00, 0x02]))


def _tz_hours(tz_offset_hours: int | None) -> int:
    if tz_offset_hours is None:
        local = datetime.now().astimezone()
        tz_offset_hours = round(
            (local.utcoffset() or timedelta()).total_seconds() / 3600)
    return tz_offset_hours & 0xFF


def _clock_payload(unix_timestamp: float, tz_offset_hours: int | None) -> bytes:
    return (int(unix_timestamp).to_bytes(4, "big")
            + bytes([_tz_hours(tz_offset_hours)]))


def build_init_response(echo: int, unix_timestamp: float,
                        tz_offset_hours: int | None = None) -> bytes:
    """The reply to the scale's init request (command ``0x000A``):
    ``[echo][utc u32 BE][tz i8]`` — echo the property byte the scale asked
    for (``0x18`` = clock) and supply Unix-epoch seconds + signed
    timezone-hours."""
    return build_frame(CMD_INIT_RESPONSE,
                       bytes([echo & 0xFF])
                       + _clock_payload(unix_timestamp, tz_offset_hours))


def build_set_time(unix_timestamp: float,
                   tz_offset_hours: int | None = None) -> bytes:
    """Sync the scale's clock (command ``0x1002``):
    ``[0x03][utc u32 BE][tz i8]`` — Unix epoch, not the BP path's 2010
    epoch. The scale acks with ``0x1000 [10 02][status]``."""
    return build_frame(CMD_SET_TIME,
                       bytes([_TIME_PROPERTY])
                       + _clock_payload(unix_timestamp, tz_offset_hours))


def build_binding(user_number: int = 0) -> bytes:
    """Bind to a user slot (command ``0x0003``): ``[userNumber][01]``."""
    return build_frame(CMD_BINDING, bytes([user_number & 0xFF, 0x01]))


def build_sync_request(user_number: int = 0) -> bytes:
    """Request measurement sync (command ``0x4801``): ``[userNumber][01]``.
    ``userNumber`` 0 = all users. The scale then streams a ``0x4802``
    frame per reading (including one taken live while connected)."""
    return build_frame(CMD_SYNC_REQUEST, bytes([user_number & 0xFF, 0x01]))


# Variable measurement fields, in wire order after the fixed 8-byte head:
# (name, presence bit, byte size, divisor  — divisor None = raw integer).
_MEASUREMENT_FIELDS = [
    ("user_number", _WITH_USER_NUMBER, 1, None),
    ("utc", _WITH_UTC, 4, None),
    ("timezone_hours", _WITH_TIMEZONE, 1, None),
    ("timestamp", _WITH_TIMESTAMP, 7, None),
    ("bmi", _WITH_BMI, 2, 10.0),
    ("body_fat_percentage", _WITH_BODY_FAT, 2, 10.0),
    ("basal_metabolism_kcal", _WITH_BASAL_METABOLISM, 2, None),
    ("muscle_percentage", _WITH_MUSCLE_PCT, 2, 10.0),
    ("muscle_mass_kg", _WITH_MUSCLE_MASS, 2, 100.0),
    ("fat_free_mass_kg", _WITH_FAT_FREE_MASS, 2, 100.0),
    ("soft_lean_mass_kg", _WITH_SOFT_LEAN_MASS, 2, 100.0),
    ("body_water_mass_kg", _WITH_BODY_WATER_MASS, 2, 100.0),
    ("impedance_ohms", _WITH_IMPEDANCE, 2, None),
]


def parse_measurement(payload: bytes) -> dict | None:
    """Decode a ``syncMeasurementDataResponse`` payload into a reading.

    Fixed head (big-endian): ``[remaining u16][sequence u16][flags u16]
    [weight u16]``; weight is ``raw / 100`` kg and always present. The
    flags word gates the variable body-composition fields that follow at
    offset 8 (see ``_MEASUREMENT_FIELDS``); trailing bytes past the
    flagged fields are ignored. Returns None if too short or the weight
    is implausible.

    Capture-confirmed example (90.60 kg, utc + 520 Ω impedance)::

        00 00  00 01  40 08  23 64  6a 5a aa c9  02 08  00
    """
    if len(payload) < 8:
        return None
    remaining = int.from_bytes(payload[0:2], "big")
    flags = int.from_bytes(payload[4:6], "big")
    weight_kg = int.from_bytes(payload[6:8], "big") / 100.0
    if not (2.0 <= weight_kg <= 400.0):     # reject garbage / empty slots
        return None

    reading: dict = {
        "remaining": remaining,
        "sequence": int.from_bytes(payload[2:4], "big"),
        "unit": _UNITS.get(flags & _UNIT_MASK, "kg"),
        "weight_kg": round(weight_kg, 2),
    }
    offset = 8
    for name, bit, size, divisor in _MEASUREMENT_FIELDS:
        if not flags & bit:
            continue
        if offset + size > len(payload):
            break
        raw = payload[offset:offset + size]
        offset += size
        if name == "timestamp":             # 7-byte local time; utc is used instead
            continue
        value = int.from_bytes(raw, "big")
        reading[name] = value if divisor is None else round(value / divisor, 2)
    return reading


def _reading_time(reading: dict) -> datetime:
    """The reading's wall-clock instant, from the scale's utc + timezone if
    present, else now."""
    utc = reading.get("utc")
    if utc:
        tz = timezone(timedelta(hours=_signed8(reading.get("timezone_hours", 0))))
        return datetime.fromtimestamp(utc, tz=timezone.utc).astimezone(tz)
    return datetime.now().astimezone()


def _signed8(value: int) -> int:
    return value - 256 if value >= 128 else value


def build_records(reading: dict, address: str, name: str) -> list[dict]:
    """A parsed reading → ``body_weight`` plus any body-composition
    envelopes the frame carried, all deduped on the scale timestamp so a
    re-drained reading upserts to the same records."""
    when = _reading_time(reading)
    stamp = when.astimezone(timezone.utc).strftime("%Y%m%d%H%M%S")
    device_name = name or "A&D UC-450BLE"

    def envelope(rtype: str, value, unit: str | None = None,
                 meta: dict | None = None) -> dict:
        rec = {
            "uuid": f"vitals:{address}:{rtype}:{stamp}",
            "type": rtype,
            "effective_start": when.isoformat(),
            "value": value,
            "source": {"modality": "sensed", "device_id": address,
                       "device_name": device_name},
        }
        if unit:
            rec["unit"] = unit
        if meta:
            rec["meta"] = meta
        return rec

    # Extra BIA metrics the record catalog has no dedicated type for ride
    # along on the weight record's meta rather than being dropped.
    weight_meta = {k: reading[k] for k in (
        "impedance_ohms", "basal_metabolism_kcal", "muscle_mass_kg",
        "muscle_percentage", "body_water_mass_kg", "soft_lean_mass_kg")
        if k in reading}

    records = [envelope("body_weight", reading["weight_kg"], "kg",
                        weight_meta or None)]
    if "bmi" in reading:
        records.append(envelope("body_mass_index", reading["bmi"], "kg/m2"))
    if "body_fat_percentage" in reading:
        records.append(envelope("body_fat_percentage",
                                reading["body_fat_percentage"], "%"))
    if "fat_free_mass_kg" in reading:      # fat-free mass ≈ lean body mass
        records.append(envelope("lean_body_mass",
                                reading["fat_free_mass_kg"], "kg"))
    return records


@register_device
class AndUc450Device(Device):
    id = "and-uc450"
    display_name = "A&D UC-450BLE"
    description = "A&D UC-450BLE body-composition scale — weight & BIA"

    INTERACTION = "opportunistic"
    CATEGORY = "scale"
    ICON_NAME = "bluetooth-symbolic"
    PAIRING_STEPS = [
        "Set the scale up once in the vendor phone app if it is factory-"
        "new (Vitals reads an already-initialised scale directly).",
        "Step on the scale (or press its button) so it powers on and shows "
        "the Bluetooth indicator.",
        "Keep it near the phone and search.",
    ]
    SENSOR_QUALITY = {
        "body_weight": "high",
        "body_mass_index": "high",
        "body_fat_percentage": "high",
        "lean_body_mass": "high",
    }

    # Fresh instance per advertisement, so the reconnect cooldown lives on
    # the class (the scale keeps advertising for a few seconds).
    _last_read: ClassVar[dict[str, float]] = {}
    _COOLDOWN_S = 20.0

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

    # Opportunistic sensors have no persistent session.
    async def connect(self) -> None:
        return None

    async def disconnect(self) -> None:
        return None

    async def get_battery(self) -> int | None:
        return None

    async def handle_advertisement(self, device, advertisement) -> list[dict]:
        import time
        now = time.monotonic()
        if now - self._last_read.get(self.address, 0.0) < self._COOLDOWN_S:
            return []
        self._last_read[self.address] = now       # throttle even on failure
        readings = await self._sync(device)
        out: list[dict] = []
        for reading in readings:
            out.extend(build_records(reading, self.address, self.name))
        return out

    async def _sync(self, device) -> list[dict]:
        """Connect, run the framed handshake, and drain stored readings."""
        from bleak import BleakClient
        session = _Session(self.address)
        try:
            async with BleakClient(device) as client:
                if not await session.open(client):
                    return []
                return await session.run()
        except Exception:
            log.exception("UC-450 %s: sync failed", self.address)
            return []


class _Session:
    """Drives one UC-450 connection.

    Subscribing to the three scale→phone characteristics is what starts
    the exchange: the scale sends its login request within milliseconds of
    the last CCC write (2026-07-15's "scale is passive" finding was an
    artifact of an incomplete subscription — the vendor capture shows the
    scale leading as soon as ``0xA625``, ``0xA621`` and ``0xA620`` are all
    enabled, in that order). We answer the login (and, on a fresh scale,
    the init/binding setup), push the clock, then drive the measurement
    sync. Every non-ACK frame the scale sends is acknowledged on the
    upstream ACK characteristic.
    """

    def __init__(self, address: str):
        self.address = address
        self._client = None
        self._incoming: asyncio.Queue = asyncio.Queue()
        self.battery_percent: int | None = None

    async def open(self, client) -> bool:
        self._client = client
        # Vendor-app subscription order; 0xA620 (indicate) last — its CCC
        # write is what the scale's opening login request follows.
        for uuid in (DOWNSTREAM_ACK_NOTIFY, UPSTREAM_NOTIFY, UPSTREAM_INDICATE):
            try:
                await client.start_notify(uuid, self._on_notify)
            except Exception:
                log.warning("UC-450 %s: subscribe %s failed",
                            self.address, uuid, exc_info=True)
                return False
        return True

    def _on_notify(self, _char, data: bytearray) -> None:
        frame = bytes(data)
        if is_ack(frame):
            return                          # the scale acking our command
        parsed = parse_frame(frame)
        if parsed is None:
            return
        # Acknowledge every data frame the scale sends (upstream pipe).
        if self._client is not None:
            asyncio.get_running_loop().create_task(self._ack())
        self._incoming.put_nowait(parsed)

    async def _ack(self) -> None:
        try:
            await self._client.write_gatt_char(
                UPSTREAM_ACK_WRITE, build_ack(), response=False)
        except Exception:
            pass

    async def _send(self, frame: bytes) -> None:
        await self._client.write_gatt_char(
            DOWNSTREAM_WRITE, frame, response=False)

    async def _await(self, wanted: set[int], timeout: float = 5.0):
        """Wait for the next scale frame whose command is in ``wanted``."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                return None
            try:
                cmd, payload = await asyncio.wait_for(
                    self._incoming.get(), remaining)
            except asyncio.TimeoutError:
                return None
            if cmd in wanted:
                return cmd, payload

    async def run(self) -> list[dict]:
        """Answer the scale-led handshake, then drain measurements.

        Sequence (vendor capture, 2026-07-17): scale sends login request →
        we echo its identity code (claiming ``bound``) → if the scale asks
        for setup (init request, fresh scale) we answer it and bind → we
        push set-time → sync request → the scale streams a measurement
        frame per reading.
        """
        import time
        got = await self._await({CMD_LOGIN_REQUEST}, timeout=10.0)
        if got is None:
            log.warning("UC-450 %s: no login request after subscribe",
                        self.address)
            return []
        login = parse_login_request(got[1])
        if login is None:
            return []
        self.battery_percent = login.get("battery_percent")
        await self._send(build_login_response(login["code"]))

        # A fresh scale follows up with an init request for the clock and
        # expects a binding; an already-set-up one goes quiet until sync.
        got = await self._await({CMD_INIT_REQUEST}, timeout=1.5)
        if got is not None:
            echo = got[1][0] if got[1] else 0x18
            await self._send(build_init_response(echo, time.time()))
            await self._send(build_binding())
            await self._await({CMD_BINDING_RESP}, timeout=3.0)

        await self._send(build_set_time(time.time()))
        await self._await({CMD_SETTING_RESP}, timeout=3.0)
        return await self._drain()

    async def _drain(self) -> list[dict]:
        """Ask for stored measurements and collect them until the scale
        reports none remaining. The first frame can lag well behind the
        request — a live weigh-in only completes once the reading
        stabilises — so it gets a generous timeout."""
        await self._send(build_sync_request())
        readings: list[dict] = []
        timeout = 30.0
        for _ in range(256):                # hard cap against a stuck stream
            got = await self._await({CMD_MEASUREMENT_DATA}, timeout=timeout)
            if got is None:
                break
            timeout = 6.0
            reading = parse_measurement(got[1])
            if reading is not None:
                readings.append(reading)
                if reading["remaining"] <= 0:
                    break
            elif got[1][:2] == b"\x00\x00":
                break                       # remaining 0 with no reading
        return readings
