"""iHealth Gluco+ BG5S blood-glucose meter.

The BG5S is an iHealth (Jiuan) glucose meter that speaks the vendor
"Jiuan" BLE protocol — *not* the standard Bluetooth Glucose service. It
advertises as ``BG5S`` over a custom GATT service whose 128-bit UUID is
ASCII text: ``com.jiuan.BGV42`` (and the BGU42/BGV20/BGV40 siblings). The
write and notify characteristics are the same string with the ``com.``
prefix swapped for ``sed.`` (host→meter) and ``rec.`` (meter→host).

Everything below is reverse-engineered from the iHealth Gluco-Smart
Android app (``com.ihealth.communication`` SDK, v4.10.4) and its native
``libiHealth.so``; see ``docs/ihealth-bg5.md``. Opportunistic sensor: when
the meter is powered on (or finishes a strip measurement) it advertises,
the ScanBroker routes the advertisement here, and we connect, authenticate,
drain the meter's stored readings, and record each as ``blood_glucose``.

Wire framing (both directions) wraps a command body ``[0xA2, cmd, …]`` as::

    [head][len][frag][seq][ 0xA2 cmd payload… ][checksum]

``head`` is ``0xB0`` host→meter and ``0xA0`` meter→host; ``len`` is the byte
count from ``frag`` onward minus one; ``checksum`` is the 8-bit sum of every
byte from ``frag`` through the last payload byte. Bodies longer than 15
bytes (only the auth handshake) fragment into 14-byte chunks; the meter
fragments its longer replies the same way and each fragment is ACKed.

Authentication is an XXTEA challenge-response keyed by a per-model 16-byte
secret baked into ``libiHealth.so`` (the BG5S uses the ``BG5L`` key). We
send a random nonce, the meter returns a 48-byte challenge, and we reply
with an XXTEA-derived token the meter verifies. No OS-level BLE bond.
"""

from __future__ import annotations

import asyncio
import logging
import os
import struct
from datetime import datetime, timedelta, timezone
from typing import ClassVar

from bleak import BleakClient
from bleak.uuids import normalize_uuid_str

from vitals.devices.base import Device, register_device

log = logging.getLogger(__name__)

# ── Jiuan ASCII UUIDs ─────────────────────────────────────────────
# The vendor encodes service/characteristic names as the raw bytes of a
# 128-bit UUID: "com.jiuan.BGV42" etc. We match/derive them as text rather
# than hard-coding one model's hex, so every BG glucose variant works.
def _ascii_uuid(text: str) -> str:
    """The 128-bit UUID whose 16 bytes are the ASCII of ``text`` (padded
    with NUL), lower-case ``xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx``."""
    raw = text.encode("ascii")[:16].ljust(16, b"\0")
    h = raw.hex()
    return f"{h[0:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:32]}"


def _uuid_text(uuid: str) -> str:
    """Inverse of `_ascii_uuid`: decode a 128-bit UUID back to its ASCII
    text (trailing NULs stripped), or "" if it isn't printable ASCII."""
    try:
        raw = bytes.fromhex(uuid.replace("-", ""))
    except ValueError:
        return ""
    raw = raw.rstrip(b"\0")
    if raw and all(32 <= b < 127 for b in raw):
        return raw.decode("ascii")
    return ""


# Glucose models advertise a "com.jiuan.BG…" service. We accept the whole
# family; the characteristics are discovered by their sed./rec. prefixes.
_BG_MODELS = ("BGV42", "BGU42", "BGV20", "BGV40")
_SERVICE_UUIDS = {_ascii_uuid(f"com.jiuan.{m}").lower() for m in _BG_MODELS}
_NAME_PREFIXES = ("BG5S", "BG5")

# ── protocol constants ────────────────────────────────────────────
_HEAD_TX = 0xB0        # host → meter frame head
_HEAD_RX = 0xA0        # meter → host frame head
_PROTO = 0xA2          # the SDK's per-family "command flag" byte

# Response command bytes (Bg5sInsSet.Command).
_R_VERIFY_FEEDBACK = 0xFB   # 48-byte auth challenge
_R_VERIFY_SUCCESS = 0xFD
_R_VERIFY_FAILED = 0xFE
_R_STATUS = 0x26            # GetStatusInfo_Success
_R_MEASURE_RESULT = 0x36    # live strip result
_R_OFFLINE = 0x4B           # SyncOfflineData_Data
_R_SET_TIME = 0x49

_MAX_RECORDS_PER_PACKET = 19
_STATUS_LEN = 15
_RECORD_SIZE = 7

# Per-model 16-byte auth keys extracted from libiHealth.so getKey(). The
# BG5S connects on its com.jiuan.BGV* service (the SDK's "o1 = false" path)
# and authenticates as model "BG5L".
_MODEL_KEYS = {
    "BG5": bytes.fromhex("1108781187f7f1d5f10e35f87a3bcb98"),
    "BG5L": bytes.fromhex("48e05e3231bbc447a066d8e9a2927b4e"),
    "BG1304": bytes.fromhex("cdd03d65291f60b43f5aeae0c051578d"),
    "BG1305": bytes.fromhex("2944688c0d2ec8326f5fa3c687d5b44e"),
}
_BG5S_AUTH_MODEL = "BG5L"
# The fixed second XXTEA key, ASCII "Ch/HQ4LzItYT42s=" (IdentifyIns.g).
_G_KEY = bytes([67, 104, 47, 72, 81, 52, 76, 122,
                73, 116, 89, 84, 52, 50, 115, 61])


# ── XXTEA + auth math ─────────────────────────────────────────────
_XXTEA_DELTA = 0x9E3779B9
_M32 = 0xFFFFFFFF


def xxtea_encrypt(data: bytes, key: bytes) -> bytes:
    """XXTEA-encrypt one 16-byte block with a 16-byte key, big-endian
    words — a byte-exact port of the app's ``XXTEA.d`` (19 rounds)."""
    v = list(struct.unpack(">4I", data))
    k = list(struct.unpack(">4I", key))
    n = 4
    rounds = 6 + 52 // n
    total = 0
    z = v[n - 1]
    for _ in range(rounds):
        total = (total + _XXTEA_DELTA) & _M32
        e = (total >> 2) & 3
        for p in range(n):
            y = v[(p + 1) % n]
            mx = ((((z >> 5) ^ ((y << 2) & _M32))
                   + ((y >> 3) ^ ((z << 4) & _M32))) & _M32) \
                ^ (((total ^ y) + (k[(p & 3) ^ e] ^ z)) & _M32)
            z = v[p] = (v[p] + mx) & _M32
    return struct.pack(">4I", *v)


def _word_reverse(b: bytes) -> bytes:
    """Reverse the byte order within each 4-byte word (IdentifyIns.y)."""
    out = bytearray(16)
    for i in range(4):
        out[i] = b[3 - i]
        out[i + 4] = b[7 - i]
        out[i + 8] = b[11 - i]
        out[i + 12] = b[15 - i]
    return bytes(out)


def _nibble_swap(b: bytes) -> bytes:
    """Swap the high and low nibble of every byte (IdentifyIns.C)."""
    return bytes(((x & 0xF0) >> 4) | ((x & 0x0F) << 4) for x in b)


def _derived_key(model: str) -> bytes:
    """``J(model)`` — XXTEA(nibbleswap(model key), nibbleswap(g))."""
    return xxtea_encrypt(_nibble_swap(_MODEL_KEYS[model]),
                         _nibble_swap(_G_KEY))


def identify_init(nonce: bytes) -> bytes:
    """The auth INIT body: ``[0xA2, 0xFA, <16 nonce bytes>]``. The meter
    ignores the nonce value in its response, but the app sends one."""
    return bytes([_PROTO, 0xFA]) + _word_reverse(nonce)


def identify_response(challenge: bytes, model: str = _BG5S_AUTH_MODEL) -> bytes:
    """Compute the auth reply body ``[0xA2, 0xFC, <16 bytes>]`` from the
    meter's 48-byte challenge (three 16-byte blocks) — port of
    IdentifyIns.c."""
    if len(challenge) < 48:
        raise ValueError("challenge must be 48 bytes")
    e_blk, b_blk, d_blk = challenge[0:16], challenge[16:32], challenge[32:48]
    seed = xxtea_encrypt(_word_reverse(e_blk), _derived_key(model))
    # b_blk feeds a value the app keeps but never sends; only d_blk is echoed.
    token = _word_reverse(xxtea_encrypt(_word_reverse(d_blk), seed))
    return bytes([_PROTO, 0xFC]) + token


# ── framing ───────────────────────────────────────────────────────
def _checksum(body: bytes) -> int:
    return sum(body) & 0xFF


def build_frames(payload: bytes, seq_gen) -> list[bytes]:
    """Wrap a command body ``[0xA2, cmd, …]`` into one or more host→meter
    frames. Bodies ≤15 bytes are a single ``frag=0x00`` frame; longer ones
    fragment into 14-byte chunks (``frag`` high nibble = frag count-1, low
    nibble counts down to 0), mirroring the SDK's ``o()`` / ``u()``.

    ``seq_gen`` yields the per-frame sequence byte (host counter)."""
    if len(payload) <= 15:
        seq = next(seq_gen) & 0xFF
        body = bytes([0x00, seq]) + payload
        frame = bytes([_HEAD_TX, len(payload) + 2]) + body
        return [frame + bytes([_checksum(body)])]

    # Fragmented: byte 0 (0xA2) is echoed in every fragment; the remaining
    # bytes are chunked 14 at a time.
    proto, rest = payload[0], payload[1:]
    chunks = [rest[i:i + 14] for i in range(0, len(rest), 14)]
    total = len(chunks)
    frames = []
    for idx, chunk in enumerate(chunks):
        seq = next(seq_gen) & 0xFF
        frag = (((total - 1) << 4) | (total - 1 - idx)) & 0xFF
        body = bytes([frag, seq, proto]) + chunk
        frame = bytes([_HEAD_TX, len(chunk) + 3]) + body
        frames.append(frame + bytes([_checksum(body)]))
    return frames


def frame_ok(frame: bytes) -> bool:
    """Validate a meter→host ``0xA0`` frame: head, declared length, and the
    trailing 8-bit sum checksum."""
    if len(frame) < 6 or frame[0] != _HEAD_RX:
        return False
    if frame[1] != len(frame) - 3:
        return False
    return _checksum(frame[2:-1]) == frame[-1]


def build_ack(frame: bytes) -> bytes:
    """The 6-byte host ACK a non-final meter fragment expects
    (``BleCommProtocol.l``)."""
    frag, seq = frame[2], frame[3]
    prev = 0xFF if seq == 0 else (seq - 1) & 0xFF
    b2 = ((frag & 0x0F) + 0xA0) & 0xFF
    b3 = (prev + 2) & 0xFF
    return bytes([_HEAD_TX, 0x03, b2, b3, _PROTO, (b2 + b3 + _PROTO) & 0xFF])


class Reassembler:
    """Turns validated meter→host frames into ``(command, payload)`` pairs,
    reassembling fragmented replies in arrival order (the meter waits for a
    per-fragment ACK, so fragments never overtake one another)."""

    def __init__(self):
        self._cmd: int | None = None
        self._buf = bytearray()
        self._last_seq: int | None = None

    def feed(self, frame: bytes) -> list[tuple[int, bytes]]:
        frag = frame[2]
        seq = frame[3]
        if frag in (0x00, 0xF0):            # single, unfragmented reply
            if seq == self._last_seq:       # meter re-sent; ignore the dup
                return []
            self._last_seq = seq
            return [(frame[5], bytes(frame[6:-1]))]
        # Fragmented (frag < 0xA0): high nibble+1 = count, low nibble = index.
        total = (frag >> 4) + 1
        idx = frag & 0x0F
        if idx == total - 1:                # first fragment carries the command
            self._cmd = frame[5]
            self._buf = bytearray(frame[6:-1])
        else:
            self._buf += frame[5:-1]
        if idx == 0 and self._cmd is not None:   # last fragment
            out = [(self._cmd, bytes(self._buf))]
            self._cmd = None
            self._buf = bytearray()
            return out
        return []


# ── payload parsing ───────────────────────────────────────────────
def parse_status(payload: bytes) -> dict | None:
    """Decode a GetStatusInfo (0x26) reply. Returns battery %, the meter
    clock, stored-reading count, and unit, or None if too short."""
    if len(payload) < _STATUS_LEN:
        return None
    battery = payload[0]
    if not 1 <= battery <= 100:
        battery = None
    tz_byte = payload[7]
    tz_hours = (tz_byte & 0x7F) / 4.0
    if tz_byte & 0x80:
        tz_hours = -tz_hours
    return {
        "battery": battery,
        "year": 2000 + payload[1], "month": payload[2], "day": payload[3],
        "hour": payload[4], "minute": payload[5], "second": payload[6],
        "timezone_hours": tz_hours,
        "used_strips": (payload[8] << 8) | payload[9],
        "offline_count": (payload[10] << 8) | payload[11],
        "code_version_blood": payload[12],
        "code_version_ctl": payload[13],
        "unit": payload[14] if payload[14] in (0, 1, 2) else None,
    }


def parse_offline_records(payload: bytes) -> list[dict]:
    """Decode one offline-data (0x4B) packet into reading dicts.

    Packet layout: ``[record_count][packet_index][records…]`` where each
    record is 7 bytes of bit-packed date/time, timezone and glucose value
    (mg/dL). Byte-exact port of ``Bg5sInsSet.c``::

        b0: bit7 = time-unset flag, bit0-6 = year-2000
        b1: bit0-3 = month, bit4-7 = hour high (<<2)
        b2: bit0-4 = day, bit5-7 = timezone high
        b3: minute
        b4: bit0-5 = second, bit6-7 = hour low
        b5: bit3-7 = timezone low, bit0-1 = value high
        b6: value low  (value = (b5 & 3) << 8 | b6, mg/dL)
    """
    if len(payload) < 2:
        return []
    count = payload[0]
    out: list[dict] = []
    for r in range(count):
        base = 2 + r * _RECORD_SIZE
        rec = payload[base:base + _RECORD_SIZE]
        if len(rec) < _RECORD_SIZE:
            break
        b0, b1, b2, b3, b4, b5, b6 = rec
        year = 2000 + (b0 & 0x7F)
        time_reliable = (b0 & 0x80) == 0
        month = b1 & 0x0F
        hour = ((b1 & 0xF0) >> 2) + ((b4 & 0xC0) >> 6)
        day = b2 & 0x1F
        minute = b3
        second = b4 & 0x3F
        tz_raw = (b2 & 0xE0) + ((b5 & 0xF8) >> 3)
        tz_hours = (tz_raw & 0x7F) / 4.0
        if tz_raw & 0x80:
            tz_hours = -tz_hours
        value_mgdl = ((b5 & 0x03) << 8) | b6
        if not (1 <= month <= 12 and 1 <= day <= 31
                and hour < 24 and minute < 60 and second < 60):
            continue
        out.append({
            "year": year, "month": month, "day": day,
            "hour": hour, "minute": minute, "second": second,
            "timezone_hours": tz_hours, "value_mgdl": value_mgdl,
            "time_reliable": time_reliable,
        })
    return out


def build_glucose_record(reading: dict, address: str, name: str) -> dict:
    """A parsed reading → a ``blood_glucose`` envelope in mg/dL (core
    converts to canonical mmol/L), deduped on the meter timestamp."""
    tz = timezone(timedelta(hours=reading["timezone_hours"]))
    when = datetime(reading["year"], reading["month"], reading["day"],
                    reading["hour"], reading["minute"], reading["second"],
                    tzinfo=tz)
    stamp = when.astimezone(timezone.utc).strftime("%Y%m%d%H%M%S")
    record = {
        "uuid": f"vitals:{address}:blood_glucose:{stamp}",
        "type": "blood_glucose",
        "effective_start": when.isoformat(),
        "value": reading["value_mgdl"],
        "unit": "mg/dL",
        "source": {"modality": "sensed", "device_id": address,
                   "device_name": name or "iHealth BG5S"},
    }
    if not reading.get("time_reliable", True):
        record["meta"] = {"time_unverified": True}
    return record


@register_device
class IHealthBg5Device(Device):
    id = "ihealth-bg5"
    display_name = "iHealth Gluco+ (BG5S)"
    description = "iHealth BG5S blood-glucose meter — stored readings"

    INTERACTION = "opportunistic"
    CATEGORY = "glucose"
    ICON_NAME = "bluetooth-symbolic"
    PAIRING_STEPS = [
        "Insert a test strip or press the meter's button to power it on — "
        "its screen shows the Bluetooth icon.",
        "Keep it near the phone and search.",
    ]
    # A dedicated meter: its glucose readings outrank any estimate.
    SENSOR_QUALITY = {"blood_glucose": "high"}

    # Throttle reconnects while the meter stays awake (fresh instance per
    # advertisement, so the cooldown lives on the class).
    _last_read: ClassVar[dict[str, float]] = {}
    _COOLDOWN_S = 30.0

    @classmethod
    def matches(cls, advertised_name, service_uuids) -> bool:
        if advertised_name and any(advertised_name.startswith(p)
                                   for p in _NAME_PREFIXES):
            return True
        advertised = {normalize_uuid_str(u) for u in service_uuids}
        if advertised & _SERVICE_UUIDS:
            return True
        # Accept any com.jiuan.BG* service (future BG variants).
        return any(_uuid_text(u).startswith("com.jiuan.BG") for u in advertised)

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
        return [build_glucose_record(r, self.address, self.name)
                for r in readings]

    # ── BLE session ───────────────────────────────────────────────
    async def _sync(self, device) -> list[dict]:
        """Connect, authenticate, and drain the meter's stored readings."""
        session = _Session(self.address)
        try:
            async with BleakClient(device) as client:
                if not await session.open(client):
                    return []
                if not await session.authenticate():
                    log.warning("BG5S %s: authentication failed", self.address)
                    return []
                return await session.read_offline()
        except Exception:
            log.exception("BG5S %s: sync failed", self.address)
            return []


class _Session:
    """Drives one BG5S connection: characteristic discovery, the framed
    request/response exchange (with fragment ACKs), and the command flow."""

    def __init__(self, address: str):
        self.address = address
        self._client: BleakClient | None = None
        self._write_uuid: str | None = None
        self._notify_uuid: str | None = None
        self._reasm = Reassembler()
        self._incoming: asyncio.Queue = asyncio.Queue()
        self._seq = self._seq_counter()

    @staticmethod
    def _seq_counter():
        n = 1
        while True:
            yield n
            n = (n + 2) & 0xFF

    async def open(self, client: BleakClient) -> bool:
        """Find the sed./rec. characteristics and start notifications."""
        self._client = client
        for service in client.services:
            for ch in service.characteristics:
                text = _uuid_text(ch.uuid)
                # "rec" is the characteristic the meter receives on (host
                # writes), "sed" is the one it sends on (host notifies) —
                # confirmed against the live GATT table on a real BG5S.
                if text.startswith("rec.jiuan."):
                    self._write_uuid = ch.uuid
                elif text.startswith("sed.jiuan."):
                    self._notify_uuid = ch.uuid
        if not (self._write_uuid and self._notify_uuid):
            log.warning("BG5S %s: sed./rec. characteristics not found",
                        self.address)
            return False
        await client.start_notify(self._notify_uuid, self._on_notify)
        return True

    def _on_notify(self, _char, data: bytearray) -> None:
        frame = bytes(data)
        if len(frame) == 6 and frame[0] == _HEAD_RX:
            return                           # a meter ACK of our fragment
        if not frame_ok(frame):
            return
        # The meter retransmits any frame we don't ACK; only the final-type
        # 0xF0 frame is exempt (confirmed on a real BG5S).
        if frame[2] != 0xF0 and self._client and self._write_uuid:
            ack = build_ack(frame)
            asyncio.get_running_loop().create_task(
                self._client.write_gatt_char(self._write_uuid, ack,
                                             response=False))
        for cmd, payload in self._reasm.feed(frame):
            self._incoming.put_nowait((cmd, payload))

    async def _send(self, payload: bytes) -> None:
        for frame in build_frames(payload, self._seq):
            await self._client.write_gatt_char(self._write_uuid, frame,
                                               response=False)
            await asyncio.sleep(0.03)        # let the meter ACK a fragment

    async def _await(self, wanted: set[int], timeout: float = 5.0):
        """Wait for the next response whose command is in ``wanted``."""
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

    async def authenticate(self) -> bool:
        nonce = os.urandom(16)
        await self._send(identify_init(nonce))
        got = await self._await({_R_VERIFY_FEEDBACK, _R_VERIFY_FAILED})
        if not got or got[0] != _R_VERIFY_FEEDBACK:
            return False
        await self._send(identify_response(got[1]))
        got = await self._await({_R_VERIFY_SUCCESS, _R_VERIFY_FAILED})
        return bool(got and got[0] == _R_VERIFY_SUCCESS)

    async def read_offline(self) -> list[dict]:
        """Ask the meter for its status, then page through stored readings."""
        await self._send(bytes([_PROTO, _R_STATUS, 0x00, 0x00, 0x00]))
        got = await self._await({_R_STATUS})
        status = parse_status(got[1]) if got else None
        if not status or status["offline_count"] < 1:
            return []
        total = status["offline_count"]
        packets = total // _MAX_RECORDS_PER_PACKET + 1
        readings: list[dict] = []
        for index in range(packets):
            await self._send(bytes([_PROTO, _R_OFFLINE, index, 0x00, 0x00]))
            got = await self._await({_R_OFFLINE}, timeout=8.0)
            if not got:
                break
            readings.extend(parse_offline_records(got[1]))
            if len(readings) >= total:
                break
        return readings
