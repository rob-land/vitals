"""Yucheng smart-ring / band plugin (the "SmartHealth" YCBT family).

Cheap knockoff smart rings built on the Yucheng YCBT SDK — the same
protocol a whole family of no-name rings and bands speak. One BLE
service carries a length-prefixed, CRC16-framed command protocol:

  - Service  be940000-7333-be46-b7ae-689e71722bd5
  - Write    be940001-…   (host → ring commands)
  - Notify   be940003-…   (ring → host, indications)

Every frame is ``[category, key, len_lo, len_hi, payload…, crc_lo, crc_hi]``
where ``len`` counts the whole frame and the trailing CRC16 covers
everything before it. Categories/keys are the YCBT command catalog:
``Setting/Time`` sets the clock, ``Get/SupportFunction`` returns the
device's capability bitmap, ``Get/DeviceInfo`` carries the battery, and
``Health/History*`` streams stored step / heart-rate / sleep records in
CRC-checked blocks that the host reassembles and acknowledges.

Because this SDK backs *many* different rings and bands, the specific
device in hand may lack sensors others have. The plugin reads the
capability bitmap on connect and gates each read on it: a ring without a
heart-rate sensor simply yields no heart-rate records rather than being
asked for data it can't produce.

Timestamps on the wire are seconds since 2000-01-01 in the device's
local time; :func:`yc_time_to_unix` converts them back to UTC.

The frame codec and record decoders here are pure and unit-tested
against the byte layouts recovered from the SDK; the live block-sync
handshake still needs on-device verification (see CLAUDE.md).
"""

from __future__ import annotations

import asyncio
import logging
import struct
import time
from datetime import datetime

from vitals.devices.base import (
    ActivityReading, Device, SleepSession, register_device)

log = logging.getLogger(__name__)

# ── Transport ─────────────────────────────────────────────────────────
SERVICE_UUID = "be940000-7333-be46-b7ae-689e71722bd5"
# be940001 is bidirectional (write + indicate): commands are written to
# it and the ring's responses come back as indications on it too. (The
# SDK also declares be940003 as a notify channel, but on real hardware
# the responses arrive on be940001; be940003 stays silent.)
WRITE_CHAR_UUID = "be940001-7333-be46-b7ae-689e71722bd5"
RESPONSE_CHAR_UUID = "be940001-7333-be46-b7ae-689e71722bd5"
NOTIFY_CHAR_UUID = "be940003-7333-be46-b7ae-689e71722bd5"

# ── Protocol constants ────────────────────────────────────────────────
# Seconds between the Unix epoch and the YCBT epoch (2000-01-01T00:00Z).
YC_EPOCH = 946684800

# Command categories (the frame's first byte).
CAT_SETTING = 1
CAT_GET = 2
CAT_HEALTH = 5

# Keys within a category (the frame's second byte).
KEY_SETTING_TIME = 0
# Automatic-monitoring toggles under CAT_SETTING; payload is
# ``[enabled(0/1), interval_minutes]``. Each covers one sensor; the ring
# logs periodic readings to the same history the sync drains.
KEY_SETTING_HEART_MONITOR = 12
KEY_SETTING_TEMP_MONITOR = 32
KEY_SETTING_SPO2_MONITOR = 38
# Feature name -> the monitor toggle key it enables. Only sensors the
# device's capability bitmap reports are configured.
_MONITOR_KEYS = {
    "heart_rate": KEY_SETTING_HEART_MONITOR,
    "temperature": KEY_SETTING_TEMP_MONITOR,
    "spo2": KEY_SETTING_SPO2_MONITOR,
}
# Interval must fit one byte; the vendor app offers 10-minute steps.
DEFAULT_MONITOR_INTERVAL_MIN = 10
KEY_GET_DEVICE_INFO = 0
KEY_GET_SUPPORT_FUNCTION = 1
KEY_GET_NOW_STEP = 12
KEY_GET_POWER_STATISTICS = 37
KEY_HEALTH_SPORT = 2
KEY_HEALTH_SLEEP = 4
KEY_HEALTH_HEART = 6
KEY_HEALTH_BLOCK = 128  # trailer / ack channel for streamed history

# Sleep sub-record types (0xF1..0xF4) the ring reports per stage span.
SLEEP_DEEP = 0xF1
SLEEP_LIGHT = 0xF2
SLEEP_REM = 0xF3
SLEEP_AWAKE = 0xF4


# ── CRC16 (Yucheng variant) ───────────────────────────────────────────
def crc16(data: bytes) -> int:
    """The YCBT frame checksum — a byte-swapping CRC16 seeded 0xFFFF.

    Ported bit-for-bit from the SDK's ``ByteUtil.crc16_compute`` (Java
    16-bit shorts, hence the 0xFFFF masks) so host-built frames validate
    on the ring and vice-versa.
    """
    crc = 0xFFFF
    for byte in data:
        a = ((crc << 8) & 0xFF00) | ((crc >> 8) & 0xFF)
        b = (a ^ byte) & 0xFFFF
        c = (b ^ ((b & 0xFF) >> 4)) & 0xFFFF
        d = (c ^ ((c << 12) & 0xFFFF)) & 0xFFFF
        crc = (d ^ ((d & 0xFF) << 5)) & 0xFFFF
    return crc & 0xFFFF


# ── Frame codec ───────────────────────────────────────────────────────
def build_frame(category: int, key: int, payload: bytes = b"") -> bytes:
    """Wrap a command payload in the length-prefixed, CRC16 frame."""
    total = len(payload) + 6
    body = bytes([category & 0xFF, key & 0xFF,
                  total & 0xFF, (total >> 8) & 0xFF]) + payload
    crc = crc16(body)
    return body + bytes([crc & 0xFF, (crc >> 8) & 0xFF])


def parse_frame(frame: bytes) -> tuple[int, int, bytes] | None:
    """Split one complete frame into ``(category, key, payload)``.

    Returns None when the buffer isn't a whole frame yet (the declared
    length exceeds what we have) — the caller keeps accumulating.
    """
    if len(frame) < 6:
        return None
    total = frame[2] | (frame[3] << 8)
    if total < 6 or total > len(frame):
        return None
    return frame[0], frame[1], bytes(frame[4:total - 2])


def yc_time_to_unix(raw: int, tz_offset_seconds: int) -> float:
    """Convert a device timestamp (seconds since 2000-01-01, local) to
    Unix seconds (UTC). ``tz_offset_seconds`` is the host's UTC offset."""
    return float(raw + YC_EPOCH - tz_offset_seconds)


def _tz_offset_seconds(now: float | None = None) -> int:
    """The host's current UTC offset in seconds (east-positive)."""
    ts = time.time() if now is None else now
    offset = datetime.fromtimestamp(ts).astimezone().utcoffset()
    return round(offset.total_seconds()) if offset else 0


# ── Encoders ──────────────────────────────────────────────────────────
def encode_time(unix_timestamp: float) -> bytes:
    """The 8-byte ``Setting/Time`` payload: local Y/M/D h:m:s + weekday.

    Weekday is 0=Monday…6=Sunday, matching the SDK's ``makeBleTime``.
    """
    dt = datetime.fromtimestamp(unix_timestamp)
    weekday = dt.weekday()  # Python: Monday=0 … Sunday=6 (already correct)
    return bytes([
        dt.year & 0xFF, (dt.year >> 8) & 0xFF,
        dt.month, dt.day, dt.hour, dt.minute, dt.second, weekday,
    ])


# ── Capability bitmap ─────────────────────────────────────────────────
def parse_support_function(payload: bytes) -> set[str]:
    """Decode the ``Get/SupportFunction`` bitmap into feature names.

    Only the features Vitals can actually act on are surfaced; the SDK
    exposes dozens more (notifications, dials, ECG…) we don't use. Bytes
    beyond the payload are treated as absent so short bitmaps from
    simpler devices are safe.
    """
    def bit(index: int, shift: int) -> bool:
        return index < len(payload) and bool((payload[index] >> shift) & 1)

    feats: set[str] = set()
    if bit(0, 7):
        feats.add("steps")
    if bit(0, 6):
        feats.add("sleep")
    if bit(0, 4):
        feats.add("firmware_update")
    if bit(0, 3):
        feats.add("heart_rate")
    if bit(0, 0):
        feats.add("blood_pressure")
    if bit(1, 3):
        feats.add("spo2")
    if bit(1, 2):
        feats.add("respiratory_rate")
    if bit(1, 1):
        feats.add("hrv")
    if bit(8, 0):
        feats.add("temperature")
    return feats


# ── Snapshot / history decoders ───────────────────────────────────────
def decode_battery(payload: bytes) -> int | None:
    """Battery percentage from a ``Get/DeviceInfo`` response (byte 5)."""
    if len(payload) < 6:
        return None
    level = payload[5]
    return level if 0 <= level <= 100 else None


def decode_power_battery(payload: bytes) -> int | None:
    """Battery percentage from a ``Get/PowerStatistics`` response (byte
    29). A fallback for rings whose DeviceInfo doesn't carry battery."""
    if len(payload) < 30:
        return None
    level = payload[29]
    return level if 0 <= level <= 100 else None


def decode_now_step(payload: bytes) -> dict | None:
    """Today's running totals from ``Get/NowStep``: steps (u24), calories
    (u16) and distance (u16), all little-endian."""
    if len(payload) < 7:
        return None
    steps = payload[0] | (payload[1] << 8) | (payload[2] << 16)
    calories = payload[3] | (payload[4] << 8)
    distance = payload[5] | (payload[6] << 8)
    return {"steps": steps, "calories": calories, "distance_m": distance}


def decode_history_sport(data: bytes, tz_offset: int) -> list[dict]:
    """Decode stored activity blocks (14 bytes each) into interval deltas.

    Each block: start/end (u32 le), steps (u16), distance m (u16),
    calories (u16). Blocks are per-window deltas that sum to a day total.
    """
    out: list[dict] = []
    i = 0
    while i + 14 <= len(data):
        start = struct.unpack_from("<I", data, i)[0]
        end = struct.unpack_from("<I", data, i + 4)[0]
        steps, distance, calories = struct.unpack_from("<HHH", data, i + 8)
        out.append({
            "start": yc_time_to_unix(start, tz_offset),
            "end": yc_time_to_unix(end, tz_offset),
            "steps": steps, "distance_m": distance, "calories": calories,
        })
        i += 14
    return out


def decode_history_heart(data: bytes, tz_offset: int) -> list[dict]:
    """Decode stored heart-rate points (6 bytes each): time (u32 le),
    mode (u8), bpm (u8). Zero-bpm samples (no reading) are dropped."""
    out: list[dict] = []
    i = 0
    while i + 6 <= len(data):
        ts = struct.unpack_from("<I", data, i)[0]
        bpm = data[i + 5]
        if 0 < bpm < 250:
            out.append({"timestamp": yc_time_to_unix(ts, tz_offset),
                        "bpm": bpm})
        i += 6
    return out


def decode_history_sleep(data: bytes, tz_offset: int) -> list[dict]:
    """Decode stored sleep sessions into ``{start, end, deep_spans}``.

    Each session is a 20-byte header — flags, block length (u16), start
    and end (u32 le) — followed by 8-byte stage sub-records (type u8,
    start u32 le, length-seconds u24 le). Deep-stage spans are collected
    so the ingest layer can tile the episode into light/deep.
    """
    sessions: list[dict] = []
    i = 0
    n = len(data)
    while i + 20 <= n:
        block_len = data[i + 2] | (data[i + 3] << 8)
        start = struct.unpack_from("<I", data, i + 4)[0]
        end = struct.unpack_from("<I", data, i + 8)[0]
        # The stage sub-records occupy the rest of this block.
        sub = i + 20
        sub_end = i + block_len if block_len >= 20 else n
        sub_end = min(sub_end, n)
        deep: list[tuple[float, float]] = []
        while sub + 8 <= sub_end:
            stage = data[sub]
            sub_start = struct.unpack_from("<I", data, sub + 1)[0]
            length_s = data[sub + 5] | (data[sub + 6] << 8) | (data[sub + 7] << 16)
            if stage == SLEEP_DEEP:
                s = yc_time_to_unix(sub_start, tz_offset)
                deep.append((s, s + length_s))
            sub += 8
        sessions.append({
            "start": yc_time_to_unix(start, tz_offset),
            "end": yc_time_to_unix(end, tz_offset),
            "deep_spans": tuple(deep),
        })
        i = sub_end if sub_end > i else n
    return sessions


@register_device
class YuchengRing(Device):
    id = "yucheng_ring"
    display_name = "Smart Ring"
    description = ("Yucheng-protocol smart ring / band (the SmartHealth "
                   "family) — steps, heart rate and sleep")
    CATEGORY = "ring"
    PAIRING_STEPS = [
        "Wear the ring or put it on its charger to wake it.",
        "Keep it close to the phone and search.",
    ]

    # The family can do all of these; the specific device's capability
    # bitmap gates each read at runtime, so a ring missing a sensor just
    # returns nothing rather than erroring.
    SUPPORTS_TIME_SYNC = True
    SUPPORTS_ACTIVITY_READ = True
    SUPPORTS_SLEEP_READ = True
    SUPPORTS_MONITORING_CONFIG = True

    # Per-frame response wait, and the window we allow a streamed history
    # block to arrive within.
    _RESPONSE_TIMEOUT = 4.0
    _HISTORY_TIMEOUT = 12.0
    # These cheap rings are slow/flaky to accept a connection (tens of
    # seconds is normal, and BlueZ often reports a transient "operation
    # in progress" when several devices sync at once), so allow generous
    # time and several spaced-out retries rather than bleak's short
    # default. The backoff has to outlast the in-progress window.
    _CONNECT_TIMEOUT = 30.0
    _CONNECT_ATTEMPTS = 5
    _CONNECT_BACKOFF = 3.0

    @classmethod
    def matches(cls, advertised_name: str | None,
                service_uuids: list[str]) -> bool:
        # The custom be94 service UUID is the reliable signal — it's
        # distinctive to this SDK, unlike the generic ring names these
        # devices advertise.
        return SERVICE_UUID in [u.lower() for u in service_uuids]

    @classmethod
    def match_specificity(cls, advertised_name, service_uuids) -> int:
        # be94 uniquely identifies a Yucheng device, so this must beat
        # the generic transports these rings also advertise (Nordic UART,
        # standard Heart Rate) which bangle / the sensor plugin grab.
        return cls.MATCH_VENDOR_SERVICE

    def __init__(self, address: str, name: str = ""):
        super().__init__(address, name)
        self._client = None
        # One reassembly buffer per characteristic — command responses
        # arrive on be940001 and streamed history blocks on be940003, and
        # interleaving them in a single buffer would corrupt frames.
        self._rx: dict[str, bytearray] = {}
        self._frames: asyncio.Queue[tuple[int, int, bytes]] | None = None
        self._features: set[str] = set()

    # ── Lifecycle ─────────────────────────────────────────────────────
    async def connect(self) -> None:
        from bleak import BleakClient
        self._frames = asyncio.Queue()
        self._rx = {}
        self._client = BleakClient(self.address, timeout=self._CONNECT_TIMEOUT)
        last_exc: Exception | None = None
        for attempt in range(self._CONNECT_ATTEMPTS):
            try:
                await self._client.connect()
                last_exc = None
                break
            except Exception as exc:  # noqa: BLE001 — retry any connect error
                last_exc = exc
                log.info("Ring %s: connect attempt %d failed (%s)",
                         self.address, attempt + 1, exc)
                await asyncio.sleep(self._CONNECT_BACKOFF)
        if last_exc is not None:
            raise last_exc
        # Subscribe both channels: command responses (and history
        # outlines) arrive on be940001, while the streamed history data
        # blocks arrive on be940003. Confirmed on-device — listening to
        # only one loses half the protocol.
        await self._client.start_notify(RESPONSE_CHAR_UUID, self._on_notify)
        try:
            await self._client.start_notify(NOTIFY_CHAR_UUID, self._on_notify)
        except Exception:
            pass  # some variants expose only the one indicate channel
        # Learn what this particular device can do before reading it.
        self._features = await self._read_support_function()
        log.info("Ring %s features: %s", self.address,
                 sorted(self._features) or "unknown")

    async def disconnect(self) -> None:
        if self._client is None:
            return
        for uuid in (RESPONSE_CHAR_UUID, NOTIFY_CHAR_UUID):
            try:
                await self._client.stop_notify(uuid)
            except Exception:
                pass
        try:
            await self._client.disconnect()
        finally:
            self._client = None

    @property
    def is_connected(self) -> bool:
        return self._client is not None and self._client.is_connected

    # ── Transport helpers ─────────────────────────────────────────────
    def _on_notify(self, char, data: bytearray) -> None:
        """Accumulate indications per characteristic and emit each
        complete frame onto the shared queue."""
        key = str(getattr(char, "uuid", char))
        buf = self._rx.setdefault(key, bytearray())
        buf.extend(data)
        while len(buf) >= 4:
            total = buf[2] | (buf[3] << 8)
            if total < 6 or len(buf) < total:
                break
            parsed = parse_frame(bytes(buf[:total]))
            del buf[:total]
            if parsed is not None and self._frames is not None:
                self._frames.put_nowait(parsed)

    def _drain_frames(self) -> None:
        if self._frames is None:
            return
        while not self._frames.empty():
            try:
                self._frames.get_nowait()
            except asyncio.QueueEmpty:
                break

    async def _write(self, frame: bytes) -> None:
        if self._client is None:
            raise RuntimeError("not connected")
        # Must be write-WITH-response: the ring stays silent to
        # write-without-response commands (confirmed on-device; the SDK
        # uses the characteristic's default write type, i.e. with-response).
        await self._client.write_gatt_char(
            WRITE_CHAR_UUID, frame, response=True)

    async def _request(self, category: int, key: int, payload: bytes = b"",
                       timeout: float | None = None) -> bytes | None:
        """Send one command and return the payload of the first response
        that echoes its category+key, or None on timeout."""
        if self._frames is None:
            raise RuntimeError("not connected")
        self._drain_frames()
        await self._write(build_frame(category, key, payload))
        deadline = (timeout or self._RESPONSE_TIMEOUT)
        try:
            async with asyncio.timeout(deadline):
                while True:
                    cat, k, resp = await self._frames.get()
                    if cat == category and k == key:
                        return resp
        except asyncio.TimeoutError:
            log.warning("Ring %s: no response to %d/%d", self.address,
                        category, key)
            return None

    async def _read_support_function(self) -> set[str]:
        payload = await self._request(
            CAT_GET, KEY_GET_SUPPORT_FUNCTION, b"\x7f\x46")
        if payload is None:
            return set()
        return parse_support_function(payload)

    async def _read_history(self, type_key: int) -> bytes:
        """Request a streamed history type and reassemble its blocks.

        The ring answers with an outline (record count + total byte
        length), a run of data blocks, then a trailer on the
        ``Health/HistoryBlock`` channel carrying the CRC. We concatenate
        the data, verify against the trailer, acknowledge, and hand the
        bytes to the per-type decoder. On timeout we return whatever
        arrived — the fixed-size record decoders ignore a short tail.
        """
        if self._frames is None:
            raise RuntimeError("not connected")
        self._drain_frames()
        await self._write(build_frame(CAT_HEALTH, type_key))

        buffer = bytearray()
        total_bytes: int | None = None
        got_outline = False
        try:
            async with asyncio.timeout(self._HISTORY_TIMEOUT):
                while True:
                    cat, key, payload = await self._frames.get()
                    if cat != CAT_HEALTH:
                        continue
                    if (not got_outline and key == type_key
                            and len(payload) >= 10):
                        total_bytes = struct.unpack_from("<I", payload, 6)[0]
                        got_outline = True
                        continue
                    if (key == KEY_HEALTH_BLOCK and len(payload) == 6
                            and (payload[2] | (payload[3] << 8)) == len(buffer)):
                        # Trailer: acknowledge so the ring advances.
                        await self._write(
                            build_frame(CAT_HEALTH, KEY_HEALTH_BLOCK, b"\x00"))
                        break
                    buffer.extend(payload)
                    if total_bytes is not None and len(buffer) >= total_bytes:
                        await self._write(
                            build_frame(CAT_HEALTH, KEY_HEALTH_BLOCK, b"\x00"))
                        break
        except asyncio.TimeoutError:
            log.warning("Ring %s: history %d timed out (%d bytes)",
                        self.address, type_key, len(buffer))
        if total_bytes is not None:
            return bytes(buffer[:total_bytes])
        return bytes(buffer)

    # ── Feature methods ───────────────────────────────────────────────
    async def get_battery(self) -> int | None:
        payload = await self._request(
            CAT_GET, KEY_GET_DEVICE_INFO, b"\x7f\x43")
        level = decode_battery(payload) if payload is not None else None
        if level is not None:
            return level
        # Some rings return a stub DeviceInfo without a battery byte;
        # fall back to the power-statistics command.
        payload = await self._request(CAT_GET, KEY_GET_POWER_STATISTICS)
        return decode_power_battery(payload) if payload is not None else None

    async def sync_time(self, unix_timestamp: float) -> None:
        await self._write(
            build_frame(CAT_SETTING, KEY_SETTING_TIME,
                        encode_time(unix_timestamp)))

    async def configure_monitoring(self, enabled: bool,
                                   interval_minutes: int) -> None:
        """Enable/disable the ring's automatic periodic monitoring.

        Sends one toggle per sensor the ring actually has (from the
        capability bitmap read at connect) — heart rate, temperature and
        SpO2 — so Vitals owns the measurement cadence and the vendor app
        is never needed. The interval is shared across sensors, in
        minutes, and clamped to a single byte.
        """
        interval = max(1, min(255, int(interval_minutes)))
        flag = 1 if enabled else 0
        payload = bytes([flag, interval])
        for feature, key in _MONITOR_KEYS.items():
            if feature in self._features:
                await self._write(build_frame(CAT_SETTING, key, payload))
        log.info("Ring %s: monitoring %s @ %d min (%s)", self.address,
                 "on" if enabled else "off", interval,
                 sorted(f for f in _MONITOR_KEYS if f in self._features))

    async def get_activity(self) -> ActivityReading | None:
        # Steps are recorded from the history-sport deltas
        # (get_activity_series); returning None here keeps the daily
        # total from being double-counted by a cumulative snapshot.
        return None

    async def get_activity_series(self) -> list[ActivityReading] | None:
        if "steps" not in self._features:
            return None
        tz = _tz_offset_seconds()
        blocks = decode_history_sport(
            await self._read_history(KEY_HEALTH_SPORT), tz)
        readings: list[ActivityReading] = []
        for b in blocks:
            interval = max(1, round(b["end"] - b["start"]))
            readings.append(ActivityReading(
                steps=b["steps"] or None,
                distance_m=float(b["distance_m"]) or None,
                active_kcal=float(b["calories"]) or None,
                timestamp=b["start"],
                interval_seconds=interval))
        return readings

    async def get_heart_rate_samples(self) -> list[ActivityReading]:
        if "heart_rate" not in self._features:
            return []
        tz = _tz_offset_seconds()
        points = decode_history_heart(
            await self._read_history(KEY_HEALTH_HEART), tz)
        return [ActivityReading(heart_rate_bpm=p["bpm"],
                                timestamp=p["timestamp"])
                for p in points]

    async def get_sleep_series(self) -> list[SleepSession] | None:
        if "sleep" not in self._features:
            return None
        tz = _tz_offset_seconds()
        sessions = decode_history_sleep(
            await self._read_history(KEY_HEALTH_SLEEP), tz)
        return [SleepSession(start=s["start"], end=s["end"],
                             deep_spans=s["deep_spans"])
                for s in sessions if s["end"] > s["start"]]
