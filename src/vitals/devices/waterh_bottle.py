"""WaterH smart water-bottle plugin.

A cheap Bluetooth water bottle that weighs each sip and stores a drink
log the phone drains later. Two models speak the same protocol over
different feature sets:

  - **WaterH-Bottle-1**   ("Vita")  — battery, water temperature, fill
    level and a TDS (water-quality) sensor, plus the drink log.
  - **WaterH-Bottle-B003** ("Boost") — battery and the drink log only.

The bottle exposes three GATT services: commands are written to
``FFE5/FFE9`` and every response/report is notified on ``FFE0/FFE4``.
The wire protocol is ASCII-tagged: a 2-byte op (``GT`` get, ``PT`` put,
``RP`` response, ``RT`` report) + a big-endian length + payload. Drink
history streams back as fixed 13-byte records.

Because the two models differ, the plugin advertises hydration + battery
for both but only attaches water-temperature / TDS metadata for the
model that actually has those sensors — a Boost's drinks carry no
phantom temperature.

Registration: a factory-fresh bottle must be associated once, which
needs the user to tap the bottle to confirm; :meth:`connect` drives that
handshake when the bottle asks for it and is otherwise a no-op, so
routine syncs just drain the log.

The frame codec and the drink-log decoder are pure and unit-tested; the
live connect/registration handshake needs on-device verification.
"""

from __future__ import annotations

import asyncio
import logging
import struct
import time
from datetime import datetime

from vitals.devices.base import Device, HydrationReading, register_device

log = logging.getLogger(__name__)

# ── Transport ─────────────────────────────────────────────────────────
WRITE_CHAR_UUID = "0000ffe9-0000-1000-8000-00805f9b34fb"
NOTIFY_CHAR_UUID = "0000ffe4-0000-1000-8000-00805f9b34fb"
NAME_PREFIX = "waterh"

# ── Op tags (first two bytes of every frame) ──────────────────────────
OP_PUT = b"PT"   # host → bottle command
OP_GET = b"GT"   # host → bottle request
OP_RSP = b"RP"   # bottle → host response
OP_RPT = b"RT"   # bottle → host asynchronous report

# One drink log is a fixed-size record.
LOG_RECORD_SIZE = 13

# Model identity: advertised-name suffix → (key, has water-quality sensors).
MODEL_VITA = "vita"
MODEL_BOOST = "boost"


# ── Frame codec ───────────────────────────────────────────────────────
def build_request(op: bytes, payload: bytes) -> bytes:
    """Frame a command: ``op(2) + length(2 BE) + payload``."""
    return op + struct.pack(">H", len(payload)) + payload


def _u16be(hi: int, lo: int) -> int:
    return (hi << 8) | lo


# Concrete commands (payloads verbatim from the app).
def req_bottle_data() -> bytes:
    return build_request(OP_GET, bytes([0xFF]))


def req_water_logs() -> bytes:
    return build_request(OP_GET, bytes([0x06]))


def req_registration() -> bytes:
    return build_request(OP_PUT, bytes([0x02, 0x1C, 0x01]))


def req_clear_offline() -> bytes:
    return build_request(OP_PUT, bytes([0x02, 0x1C, 0x05]))


def req_ack_received_size(record_count: int) -> bytes:
    """Tell the bottle how many log bytes we took so it can advance."""
    return build_request(
        OP_RSP, bytes([0x03, 0x06]) + struct.pack(">H", record_count * LOG_RECORD_SIZE))


def req_sync_data(unix_timestamp: float, goal: bytes = b"\x00\x00",
                  reminder: bytes = b"\x00\x08\x00\x14\x00\x3c") -> bytes:
    """Push the clock (+ goal/reminder defaults); the bottle answers by
    becoming ready to hand over its drink log. Date fields are single
    bytes in local time, year modulo 100, month 1-based."""
    dt = datetime.fromtimestamp(unix_timestamp)
    date = bytes([dt.year % 100, dt.month, dt.day, dt.hour, dt.minute, dt.second])
    payload = (bytes([0x03, 0x05]) + goal
               + bytes([0x07, 0x03]) + date
               + bytes([0x07, 0x26]) + reminder)
    return build_request(OP_PUT, payload)


# ── Drink-log decoder ─────────────────────────────────────────────────
def decode_water_log_packet(value: bytes, start: int) -> list[dict]:
    """Split one notification into 13-byte drink records from ``start``.

    Layout per record: y(+2000) m d h min s, amount mL (u16 BE),
    tds (u16 BE), temp °C (int8), reserved, flag. ``flag == 0`` marks a
    real drink; anything else is a bookkeeping entry the caller skips.
    """
    records: list[dict] = []
    i = start
    while i + LOG_RECORD_SIZE <= len(value):
        rec = value[i:i + LOG_RECORD_SIZE]
        records.append({
            "year": 2000 + rec[0], "month": rec[1], "day": rec[2],
            "hour": rec[3], "minute": rec[4], "second": rec[5],
            "amount_ml": _u16be(rec[6], rec[7]),
            "tds": _u16be(rec[8], rec[9]),
            "temp_c": struct.unpack("b", rec[10:11])[0],
            "is_drink": rec[12] == 0,
        })
        i += LOG_RECORD_SIZE
    return records


def drink_timestamp(rec: dict) -> float:
    """Local wall-clock of a drink record → Unix seconds."""
    return datetime(rec["year"], rec["month"], rec["day"],
                    rec["hour"], rec["minute"], rec["second"]).timestamp()


async def _wait_any(events, timeout: float) -> None:
    """Wait until any of ``events`` is set or ``timeout`` elapses,
    cleaning up the still-pending waiters."""
    tasks = [asyncio.create_task(e.wait()) for e in events]
    try:
        async with asyncio.timeout(timeout):
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    except asyncio.TimeoutError:
        pass
    finally:
        for t in tasks:
            t.cancel()


def model_for_name(name: str | None) -> str:
    """Map an advertised bottle name to a model key. The Boost model is
    named ``…B003``; everything else on this protocol is Vita-class
    (temperature + TDS)."""
    n = (name or "").lower()
    if "b003" in n:
        return MODEL_BOOST
    return MODEL_VITA


@register_device
class WaterHBottle(Device):
    id = "waterh_bottle"
    display_name = "WaterH Bottle"
    description = ("WaterH smart water bottle — logs hydration; fuller "
                   "models also report water temperature and quality")
    CATEGORY = "bottle"
    PAIRING_STEPS = [
        "Press or twist the WaterH bottle's cap button to wake it.",
        "Keep it near the phone and search.",
    ]

    SUPPORTS_HYDRATION_READ = True

    _RESPONSE_TIMEOUT = 6.0
    _LOG_TIMEOUT = 15.0
    _REGISTRATION_TIMEOUT = 60.0  # user has to tap the bottle to confirm

    @classmethod
    def matches(cls, advertised_name: str | None,
                service_uuids: list[str]) -> bool:
        # The FFE0 module UUID is shared by countless cheap devices, so
        # match the distinctive advertised name instead.
        return bool(advertised_name
                    and advertised_name.lower().startswith(NAME_PREFIX))

    def __init__(self, address: str, name: str = ""):
        super().__init__(address, name)
        self._client = None
        self._model = model_for_name(name)
        self._battery: int | None = None
        # Drink-log accumulation state (reset per read).
        self._drinks: list[dict] = []
        self._log_total: int | None = None
        self._log_seen = 0
        # Events the sync steps await.
        self._new_data = asyncio.Event()
        self._ready_for_log = asyncio.Event()
        self._log_done = asyncio.Event()
        self._no_log_data = asyncio.Event()
        self._reg_needed = asyncio.Event()
        self._reg_confirmed = asyncio.Event()
        self._reg_done = asyncio.Event()

    @property
    def has_water_quality(self) -> bool:
        """Whether this model carries temperature + TDS sensors."""
        return self._model == MODEL_VITA

    # ── Lifecycle ─────────────────────────────────────────────────────
    async def connect(self) -> None:
        from bleak import BleakClient
        self._client = BleakClient(self.address)
        await self._client.connect()
        await self._client.start_notify(NOTIFY_CHAR_UUID, self._on_notify)
        await self._prepare()

    async def disconnect(self) -> None:
        if self._client is None:
            return
        try:
            await self._client.stop_notify(NOTIFY_CHAR_UUID)
        except Exception:
            pass
        try:
            await self._client.disconnect()
        finally:
            self._client = None

    @property
    def is_connected(self) -> bool:
        return self._client is not None and self._client.is_connected

    async def _prepare(self) -> None:
        """Ask for current bottle data; run the registration handshake if
        the bottle says it isn't associated yet. Populates battery/model
        for :meth:`get_battery`."""
        self._new_data.clear()
        self._reg_needed.clear()
        await self._write(req_bottle_data())
        # Whichever comes first: a data snapshot (already paired) or a
        # request to register.
        await _wait_any((self._new_data, self._reg_needed),
                        self._RESPONSE_TIMEOUT)
        if self._reg_needed.is_set() and not self._new_data.is_set():
            await self._register()
            await self._write(req_bottle_data())

    async def _register(self) -> None:
        """Drive the one-time association: ask to register, wait for the
        user to confirm on the bottle, then finish. Best-effort — a
        bottle that never confirms just leaves us unregistered."""
        log.info("WaterH %s: registering (confirm on the bottle)", self.address)
        self._reg_confirmed.clear()
        self._reg_done.clear()
        await self._write(req_registration())
        try:
            async with asyncio.timeout(self._REGISTRATION_TIMEOUT):
                await self._reg_confirmed.wait()
        except asyncio.TimeoutError:
            log.warning("WaterH %s: registration not confirmed in time",
                        self.address)
            return
        await self._write(req_clear_offline())
        try:
            async with asyncio.timeout(self._RESPONSE_TIMEOUT):
                await self._reg_done.wait()
        except asyncio.TimeoutError:
            pass

    # ── Transport ─────────────────────────────────────────────────────
    async def _write(self, frame: bytes) -> None:
        if self._client is None:
            raise RuntimeError("not connected")
        await self._client.write_gatt_char(WRITE_CHAR_UUID, frame, response=False)

    def _on_notify(self, _char, data: bytearray) -> None:
        try:
            self._route(bytes(data))
        except Exception:
            log.exception("WaterH %s: bad notification %r", self.address,
                          bytes(data))

    def _route(self, value: bytes) -> None:
        """Dispatch one notification, mirroring the app's decode paths."""
        if len(value) < 2:
            return
        op = value[:2]
        if op == OP_RSP:
            self._route_response(value)
        elif op == OP_RPT:
            self._route_report(value)
        elif op == OP_PUT and len(value) > 5 and value[5] == 0x06:
            # Water-log start packet: total = BE(len)/13, records at [6:].
            self._log_total = _u16be(value[2], value[3]) // LOG_RECORD_SIZE
            self._collect_logs(value, 6)
        elif len(value) > 1 and value[1] == 0x06:
            # Water-log continuation packet: records at [2:].
            self._collect_logs(value, 2)

    def _route_response(self, value: bytes) -> None:
        if len(value) < 6:
            return
        # New-Data snapshots carry model + battery.
        if value[2] == 0 and len(value) > 3 and value[3] == 0x31:
            self._model = MODEL_VITA
            self._battery = value[9] if len(value) > 9 else self._battery
            self._new_data.set()
            return
        if value[2] == 0 and len(value) > 3 and value[3] == 0x27:
            self._model = MODEL_BOOST
            self._battery = value[6] if len(value) > 6 else self._battery
            self._new_data.set()
            return
        if value[2] == 0 and len(value) > 3 and value[3] == 0x0F:
            self._ready_for_log.set()
            return
        marker = value[5]
        if marker == 0x06 and len(value) > 6 and value[6] == 0:
            self._no_log_data.set()
        elif marker == 0x1C and len(value) > 6:
            if value[6] == 2:      # registration prompt started
                self._reg_needed.set()
            elif value[6] == 6:    # registration finished (data cleared)
                self._reg_done.set()

    def _route_report(self, value: bytes) -> None:
        if len(value) < 6:
            return
        field = value[5]
        if field == 0x1C and len(value) > 6:
            if value[6] == 3:      # user confirmed on the bottle
                self._reg_confirmed.set()
            return
        if len(value) < 7:
            return
        if field == 2:             # battery %
            self._battery = value[6]

    def _collect_logs(self, value: bytes, start: int) -> None:
        for rec in decode_water_log_packet(value, start):
            self._log_seen += 1
            if rec["is_drink"]:
                self._drinks.append(rec)
        if self._log_total is not None and self._log_seen >= self._log_total:
            self._log_done.set()

    # ── Feature methods ───────────────────────────────────────────────
    async def get_battery(self) -> int | None:
        return self._battery

    async def get_hydration_series(self) -> list[HydrationReading] | None:
        """Drain the bottle's drink log since the last sync.

        Puts the bottle into log-transfer mode, collects every stored
        drink, acknowledges receipt (so the bottle drops what we took),
        and returns the drinks as readings. Deterministic per-drink uuids
        downstream keep an un-acknowledged re-read idempotent.
        """
        self._drinks = []
        self._log_total = None
        self._log_seen = 0
        self._log_done.clear()
        self._no_log_data.clear()
        self._ready_for_log.clear()

        await self._write(req_sync_data(time.time()))
        await _wait_any((self._ready_for_log,), self._RESPONSE_TIMEOUT)

        await self._write(req_water_logs())
        await _wait_any((self._log_done, self._no_log_data), self._LOG_TIMEOUT)
        if not (self._log_done.is_set() or self._no_log_data.is_set()):
            log.warning("WaterH %s: drink-log read timed out (%d of %s)",
                        self.address, self._log_seen, self._log_total)

        if self._log_seen:
            await self._write(req_ack_received_size(self._log_seen))

        readings = [r for r in map(self._reading_from_record, self._drinks)
                    if r is not None]
        log.info("WaterH %s: %d drinks", self.address, len(readings))
        return readings

    def _reading_from_record(self, rec: dict) -> HydrationReading | None:
        """Turn one decoded drink into a reading, dropping empty drinks
        and omitting temperature/TDS for models that lack those sensors."""
        if rec["amount_ml"] <= 0:
            return None
        temp = float(rec["temp_c"]) if self.has_water_quality else None
        tds = rec["tds"] if (self.has_water_quality and rec["tds"]) else None
        return HydrationReading(
            amount_ml=float(rec["amount_ml"]),
            timestamp=drink_timestamp(rec),
            temperature_c=temp,
            tds_ppm=tds)
