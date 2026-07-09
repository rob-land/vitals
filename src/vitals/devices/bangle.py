"""Bangle.js 2 device plugin.

Bangle.js exposes a Nordic UART Service (NUS) on which it speaks
Espruino REPL — UTF-8 JavaScript code goes in on TX, output comes
back on RX as notifications. The watch evaluates whatever you send.

  TX (write): 6E400002-...     (commands → watch)
  RX (notify): 6E400003-...    (output ← watch)

Reference: https://www.espruino.com/Reference#Bluetooth
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time

from vitals.alarms import Alarm
from vitals.devices.base import ActivityReading, Device, register_device

log = logging.getLogger(__name__)


# Nordic UART Service used by Bangle.js (and Espruino devices in general).
NUS_UUID = "6e400001-b5a3-f393-e0a9-e50e24dcca9e"
TX_UUID  = "6e400002-b5a3-f393-e0a9-e50e24dcca9e"
RX_UUID  = "6e400003-b5a3-f393-e0a9-e50e24dcca9e"

# Espruino's per-line input buffer on Bangle.js 2 is small (~128 bytes
# in default firmware). A single-line `writeJSON(...)` command much
# bigger than that gets truncated on the way in, the watch's JSON
# parse fails, and the REPL is left in a confused state that breaks
# the very next command (e.g. get_battery returning None). Chunked
# writes keep each REPL line well under that limit.
_BANGLE_LINE_BUDGET = 80

# Base64 / JS-expression characters per upload line. Each app-file write
# accumulates the content with `__a += "<chunk>"` lines, which must stay
# under the REPL line budget (`__a+=` + quotes + the chunk).
_BANGLE_UPLOAD_CHUNK = 50

# Espruino firmware downloads. There's no "latest" API, so a recent
# default is used and can be overridden. The flashable artifact is the
# Nordic DFU .zip.
_BANGLE_FW_URL = "https://www.espruino.com/binaries/espruino_{version}_banglejs2.zip"
_BANGLE_FW_DEFAULT_VERSION = "2v27"


# Gadgetbridge-protocol messages (`GB({...})`) are written raw to the
# UART with 0x10 ("echo off for this line") — the watch's GB() handler
# consumes them; there's no REPL response to wait for. Payloads chunk
# to the default 20-byte MTU. ensure_ascii keeps them 8-bit safe.
_GB_CHUNK = 20
# Field caps from Gadgetbridge's Bangle support.
_GB_TITLE_MAX = 80
_GB_BODY_MAX = 400
_GB_SRC_MAX = 40

# Neutral condition kind -> OpenWeatherMap condition id (what Bangle
# weather apps key their icons on).
_OWM_CODE = {
    "clear": 800, "partly": 802, "cloudy": 804, "fog": 741,
    "drizzle": 300, "rain": 500, "heavy_rain": 502, "sleet": 611,
    "snow": 600, "heavy_snow": 602, "thunderstorm": 200, "unknown": 800,
}


def gb_message(obj: dict) -> bytes:
    js = json.dumps(obj, separators=(",", ":"), ensure_ascii=True)
    return ("\x10GB(" + js + ")\n").encode("ascii")


def gb_notify(note) -> dict:
    return {"t": "notify", "id": int(note.id),
            "src": note.app_name[:_GB_SRC_MAX],
            "title": note.title[:_GB_TITLE_MAX],
            "body": note.body[:_GB_BODY_MAX]}


def gb_weather(forecast) -> dict:
    """The v1 GB weather shape: temperature in Kelvin (raw), wind km/h."""
    today = forecast.day(0)
    msg: dict = {"t": "weather", "v": 1,
                 "code": _OWM_CODE.get(forecast.kind, 800),
                 "txt": forecast.phrase,
                 "loc": forecast.location_name}

    def kelvin(c):
        return round(c + 273.15) if isinstance(c, (int, float)) else None

    for key, value in (("temp", kelvin(forecast.temp_c)),
                       ("hi", kelvin(today.high_c if today else None)),
                       ("lo", kelvin(today.low_c if today else None)),
                       ("hum", forecast.humidity),
                       ("wind", forecast.wind_kmh),
                       ("wdir", forecast.wind_dir_deg)):
        if value is not None:
            msg[key] = value
    return msg


def _looks_like_espruino_error(response: str) -> bool:
    """Heuristic: did Espruino throw on the line we just sent?

    Espruino prints uncaught errors with markers like
    `Uncaught SyntaxError`, `Uncaught Error`, or `at line 1 col 12`.
    The REPL still emits a prompt afterwards so `_run` happily
    returns — but the side effect we wanted didn't happen.
    """
    if not response:
        return False
    return ("Uncaught " in response
            or "ERROR" in response
            or "ReferenceError" in response
            or "SyntaxError" in response)


@register_device
class BangleDevice(Device):
    id = "bangle"
    display_name = "Bangle.js"
    description = "Espruino-based smartwatch (Bangle.js 1, Bangle.js 2)"
    CATEGORY = "watch"
    ICON_NAME = "phone-symbolic"
    PAIRING_STEPS = [
        "On the Bangle, make sure Bluetooth is on and it isn't already "
        "connected to another phone.",
        "Keep the watch awake (tap the screen) and nearby, then search.",
    ]

    SUPPORTS_TIME_SYNC     = True
    SUPPORTS_ALARM_PUSH    = True
    SUPPORTS_ACTIVITY_READ = True
    SUPPORTS_APP_INSTALL   = True
    SUPPORTS_NOTIFICATIONS = True
    SUPPORTS_WEATHER_PUSH  = True
    SUPPORTS_MUSIC_CONTROL = True
    # Firmware updates run over Nordic DFU, which the user enters with a
    # physical long-press (Espruino disables remote DFU entry).
    SUPPORTS_FIRMWARE_UPDATE   = True
    FIRMWARE_REQUIRES_DFU_MODE = True
    # A recent default; the dialog lets the user override it.
    FIRMWARE_DEFAULT_VERSION   = _BANGLE_FW_DEFAULT_VERSION

    # Bangle's REPL prints `>` followed by a space when it's ready for
    # the next line — used by `_run` to detect end-of-response.
    _PROMPT = b"\r\n>"

    @classmethod
    def matches(cls, advertised_name: str | None,
                service_uuids: list[str]) -> bool:
        if advertised_name and advertised_name.lower().startswith("bangle"):
            return True
        return NUS_UUID in [u.lower() for u in service_uuids]

    @classmethod
    def match_specificity(cls, advertised_name, service_uuids) -> int:
        # A "Bangle" name is a strong signal; matching on Nordic UART
        # alone is not — plenty of unrelated devices (including Yucheng
        # rings) expose NUS, so that fallback must yield to a vendor match.
        if advertised_name and advertised_name.lower().startswith("bangle"):
            return cls.MATCH_SPECIFIC
        return cls.MATCH_SHARED_TRANSPORT

    def __init__(self, address: str, name: str = ""):
        super().__init__(address, name)
        self._client = None
        self._buffer = bytearray()
        self._response_event = asyncio.Event()

    # ── Lifecycle ──────────────────────────────────────────────────

    async def connect(self) -> None:
        from bleak import BleakClient
        self._client = BleakClient(self.address)
        await self._client.connect()
        await self._client.start_notify(RX_UUID, self._on_notify)
        # Send a Ctrl+C to interrupt anything currently running and
        # land us at a clean prompt.
        await self._client.write_gatt_char(TX_UUID, b"\x03")
        await asyncio.sleep(0.1)
        self._buffer.clear()

    async def disconnect(self) -> None:
        if self._client is None:
            return
        try:
            await self._client.stop_notify(RX_UUID)
        except Exception:
            pass
        try:
            await self._client.disconnect()
        finally:
            self._client = None

    @property
    def is_connected(self) -> bool:
        return self._client is not None and self._client.is_connected

    # ── Espruino REPL helper ──────────────────────────────────────

    async def _run(self, code: str, timeout: float = 5.0) -> str:
        """Send `code` and return collected RX output up to the next
        prompt (`\\r\\n>`) or until the timeout elapses.

        Timeouts are surfaced as a WARNING — silent timeouts were
        masking failed pushes whose RX never made it back."""
        if self._client is None:
            raise RuntimeError("not connected")
        if not code.endswith("\n"):
            code += "\n"
        if len(code) > _BANGLE_LINE_BUDGET + 30:
            # Catch this in dev rather than waiting for BlueZ to reject
            # it as a GATT protocol error. The +30 lets the JS-string-
            # literal escape overhead through.
            log.warning("Bangle: REPL line is %d bytes, over the "
                        "%d-byte budget — likely to be rejected: %r",
                        len(code), _BANGLE_LINE_BUDGET + 30,
                        code.rstrip("\n")[:60])

        self._buffer.clear()
        self._response_event.clear()

        await self._client.write_gatt_char(TX_UUID, code.encode("utf-8"))

        timed_out = False
        try:
            async with asyncio.timeout(timeout):
                while self._PROMPT not in self._buffer:
                    self._response_event.clear()
                    await self._response_event.wait()
        except asyncio.TimeoutError:
            timed_out = True

        response = self._buffer.decode("utf-8", errors="replace")
        if timed_out:
            log.warning("Bangle: REPL timeout (%.1fs) on %r — partial "
                        "response %r",
                        timeout, code.rstrip("\n")[:60], response[:120])
        return response

    def _on_notify(self, _char, data: bytearray) -> None:
        self._buffer.extend(data)
        self._response_event.set()

    async def _send_gb(self, obj: dict) -> None:
        """Write one Gadgetbridge-protocol message raw (no REPL echo,
        nothing to wait for), chunked to the default MTU."""
        if self._client is None:
            raise RuntimeError("not connected")
        payload = gb_message(obj)
        for i in range(0, len(payload), _GB_CHUNK):
            await self._client.write_gatt_char(
                TX_UUID, payload[i:i + _GB_CHUNK])

    async def push_notification(self, note) -> None:
        """Forward one banner via the Bangle's GB() protocol (rendered
        by the stock 'Android Integration' messages app)."""
        await self._send_gb(gb_notify(note))

    async def push_weather(self, forecast) -> None:
        """Send current conditions in the GB v1 weather shape; the
        stock weather widget/app stores and shows it."""
        await self._send_gb(gb_weather(forecast))

    async def push_now_playing(self, track) -> None:
        """GB musicinfo + musicstate for the stock music-control app.
        Watch-side buttons come back as JSON lines the plugin doesn't
        listen for yet, so control is one-way for now."""
        await self._send_gb({"t": "musicinfo", "artist": track.artist,
                             "album": track.album, "track": track.track,
                             "dur": track.duration_s})
        await self._send_gb({"t": "musicstate",
                             "state": "play" if track.playing else "pause",
                             "position": track.position_s})

    # ── Feature methods ────────────────────────────────────────────

    async def get_activity(self) -> ActivityReading | None:
        """Read steps + heart-rate from `Bangle.getHealthStatus()`.

        Wraps in try/catch on the watch side so missing-method
        firmwares (the same ones that lack `Bangle.getBattery`)
        return "null" instead of an Espruino traceback. The
        outgoing command is 75 bytes, well under the MTU budget.

        Returns None if the watch lacks the method or returns
        nothing parseable.
        """
        try:
            response = await self._run(
                'try{print(JSON.stringify(Bangle.getHealthStatus()))}'
                'catch(e){print("null")}')
        except Exception:
            log.exception("Bangle: get_activity REPL call raised")
            return None
        log.info("Bangle: get_activity raw response: %r", response[:300])
        return self._parse_activity(response)

    async def get_battery(self) -> int | None:
        # `E.getBattery()` is the cross-board Espruino method; on
        # Bangle.js 2 it returns the same percentage (0–100) as
        # `Bangle.getBattery()`. v0.3.x called Bangle.getBattery
        # directly, which threw "Function getBattery not found!" on
        # at least one user's firmware — the error message contains
        # `at REPL (:1:14)` which the lenient parser picked up as
        # battery = 1%. Switching to E.getBattery() avoids the
        # firmware variation and keeps the response numeric.
        try:
            response = await self._run("print(E.getBattery())")
        except Exception:
            log.exception("Bangle: get_battery REPL call raised")
            return None
        log.info("Bangle: get_battery raw response: %r", response[:200])
        result = self._parse_battery(response)
        if result is None:
            log.warning("Bangle: get_battery couldn't parse response %r",
                        response[:200])
        else:
            log.info("Bangle: battery = %d%%", result)
        return result

    async def sync_time(self, unix_timestamp: float) -> None:
        """Push the host clock to the watch via Espruino's setTime()."""
        cmd = f"setTime({unix_timestamp:.3f})"
        response = await self._run(cmd)
        log.info("Bangle: sync_time @ %.0f -> %r",
                 unix_timestamp, response[:120])

    async def push_alarms(
        self,
        alarms: list[Alarm],
        previously_pushed_ids: set[str] | frozenset = frozenset(),
    ) -> set[str]:
        """Reconcile sched.json on the watch with `alarms`.

        Bangle.js stores alarms (and other scheduled events) in a
        single JSON file under its internal Storage. The alarm app
        re-reads this file each time it's opened, and the sched
        boot script re-runs whenever the watch wakes from a
        scheduled event.

        Up through v0.3.7 we replaced sched.json wholesale, which
        wiped any alarms the user had set on the watch directly.
        v0.4.0 reads the existing file, drops entries whose id is
        in `previously_pushed_ids` or in the current alarm set
        (so deletions and edits round-trip cleanly), and keeps
        everything else.

        The full JSON can't go over the REPL in one line — Bangle's
        input buffer truncates anything past ~128 bytes and the
        post-chunk write also has to stay under the negotiated MTU
        (~138 bytes on Bangle.js 2). Both the read of existing
        sched.json and the write back are kept short.

        Returns the set of alarm ids that are now Vitals-managed on
        the watch — the application caller stores these in
        `pushed-alarm-ids` GSetting for the next push.
        """
        # ── Step 1: read what's already on the watch ──
        read_resp = await self._run(
            'print(require("Storage").read("sched.json")||"[]")')
        existing = self._parse_sched_response(read_resp)
        current_ids = {a.id for a in alarms}
        kept, dropped = self._reconcile_existing(
            existing, current_ids, set(previously_pushed_ids))
        log.info("Bangle: reconcile — watch had %d entries, keeping %d "
                 "(non-Vitals), dropping %d (stale Vitals), adding %d "
                 "Vitals entries",
                 len(existing), len(kept), dropped, len(alarms))

        # ── Step 2: render merged list ──
        merged = kept + json.loads(self._render_sched_json(alarms))
        body = json.dumps(merged, separators=(",", ":"))
        return await self._write_sched_json(body, current_ids)

    @staticmethod
    def _reconcile_existing(existing: list[dict],
                            current_ids: set[str],
                            previously_pushed_ids: set[str]
                            ) -> tuple[list[dict], int]:
        """Decide which existing watch entries to keep vs drop.

        Drop entries whose id is in `current_ids` (about to be re-added
        with fresh fields) or `previously_pushed_ids` (Vitals pushed
        them last time but they're no longer in the alarm list, so
        they should be cleared). Keep everything else — this is what
        preserves user-set-on-the-watch alarms across syncs.

        Returns (kept_entries, dropped_count).
        """
        drop_ids = current_ids | previously_pushed_ids
        kept: list[dict] = []
        dropped = 0
        for entry in existing:
            if not isinstance(entry, dict):
                continue
            if entry.get("id") in drop_ids:
                dropped += 1
                continue
            kept.append(entry)
        return kept, dropped

    async def _write_sched_json(self, body: str,
                                 current_ids: set[str]) -> set[str]:
        chunks = (len(body) + _BANGLE_LINE_BUDGET - 1) // _BANGLE_LINE_BUDGET
        log.info("Bangle: writing %d-byte merged sched.json (%d chunks)",
                 len(body), chunks)

        await self._run('var __a = ""')
        for i in range(0, len(body), _BANGLE_LINE_BUDGET):
            chunk = body[i:i + _BANGLE_LINE_BUDGET]
            # json.dumps quotes + escapes to a JS-safe string literal.
            chunk_response = await self._run(f"__a += {json.dumps(chunk)}")
            if _looks_like_espruino_error(chunk_response):
                log.warning("Bangle: chunk %d/%d returned what looks "
                            "like an Espruino error: %r",
                            i // _BANGLE_LINE_BUDGET + 1, chunks,
                            chunk_response[:200])

        # The post-chunk commands must also stay under the line/MTU
        # budget. v0.3.6 packed Storage.write + __a = undefined +
        # a multi-arg print onto one ~150-byte line; on a desktop
        # BlueZ stack with MTU 138 that hit
        # `BleakGATTProtocolError: ... Application-specific Error 0x82`
        # because the L2CAP write was rejected before Espruino ever
        # saw it. Three short calls instead.
        write_resp = await self._run(
            'require("Storage").write("sched.json", __a)')
        if _looks_like_espruino_error(write_resp):
            log.warning("Bangle: Storage.write returned what looks like "
                        "an Espruino error: %r", write_resp[:300])
        else:
            log.info("Bangle: Storage.write response: %r", write_resp[:200])
        await self._run('__a = undefined')
        verify_resp = await self._run(
            'print(require("Storage").read("sched.json").length)')
        log.info("Bangle: sched.json byte-count readback: %r",
                 verify_resp[:200])
        return set(current_ids)

    @staticmethod
    def _render_sched_json(alarms: list[Alarm]) -> str:
        """Format `alarms` into Bangle's Scheduler-expected JSON.

        Format reference: github.com/espruino/BangleApps `apps/sched`
        and `apps/alarm`. The fields the Scheduler module actually
        reads are:

          - id    : stable id string (required for dedupe)
          - appid : "alarm" — tells the alarm app to claim this entry.
                    Without it the Scheduler treats the entry as an
                    untyped event and the alarm app's UI ignores it.
          - on    : enabled boolean
          - t     : milliseconds-since-midnight (integer). v0.3.2 and
                    earlier mistakenly put `"alarm"` in here and the
                    time-of-day in a `hr` decimal field, which made
                    every alarm land at 00:00 on the watch.
          - last  : ms-since-midnight of the last firing today, or 0.
                    Setting to 0 ensures the alarm fires today even if
                    the watch's RTC moved across the alarm time during
                    sync.
          - rp    : repeat? true for any non-zero `days` mask
          - dow   : day-of-week mask, bit 0 = Sunday, bit 6 = Saturday
                    (rotated from Vitals's bit 0 = Monday convention)
          - msg   : alarm label
          - del   : delete after firing — true for one-shot alarms
                    (days == 0), false for repeating
          - vibrate / hidden : presentation defaults
        """
        out = []
        for a in alarms:
            t_ms = (a.hour * 3600 + a.minute * 60) * 1000
            # Convert mask: our bit i (Mon..Sun) -> Bangle bit (i+1) % 7 (Sun..Sat)
            bangle_dow = 0
            for i in range(7):
                if a.days & (1 << i):
                    bangle_dow |= 1 << ((i + 1) % 7)
            repeating = a.days != 0
            out.append({
                "id":      a.id,
                "appid":   "alarm",
                "on":      bool(a.enabled),
                "t":       t_ms,
                "last":    0,
                "rp":      repeating,
                "dow":     bangle_dow,
                "msg":     a.label or "Alarm",
                "del":     not repeating,
                "vibrate": ".",
                "hidden":  False,
            })
        return json.dumps(out, separators=(",", ":"))

    # ── App install (Espruino App Loader) ──────────────────────────

    @classmethod
    def app_store(cls):
        from vitals.devices.bangle_store import BangleStore
        return BangleStore()

    async def install_app(self, bundle: bytes, on_progress=None) -> None:
        """Install an app bundle (from `BangleStore.download`) by writing
        each storage file to the watch over the Espruino REPL, then
        reloading so the launcher picks it up.

        Each file's content is accumulated in chunks small enough for the
        REPL line buffer, then committed with a single `Storage.write`
        (`atob` for normal files, `eval` for the loader's evaluate
        files). `on_progress(stage, done, total)` ticks per file."""
        if self._client is None:
            raise RuntimeError("not connected")
        data = json.loads(bundle.decode("utf-8"))
        files = data.get("files", [])
        total = len(files)
        for index, entry in enumerate(files):
            name = entry["name"]
            if on_progress is not None:
                on_progress("install", index, total)
            if entry.get("evaluate"):
                await self._upload_storage(name, entry["expr"], wrap="eval")
            else:
                await self._upload_storage(name, entry["b64"], wrap="atob")
        if on_progress is not None:
            on_progress("install", total, total)
        # Reload so the launcher registers the new app.
        await self._run("load()")
        log.info("Bangle: installed %s (%d files)", data.get("id"), total)

    async def _upload_storage(self, name: str, text: str, wrap: str) -> None:
        """Accumulate `text` over the REPL, then commit it to Storage.

        `wrap` is "atob" (text is base64 of the file bytes) or "eval"
        (text is a JS expression whose *value* is stored). The "eval" is
        Espruino's on-watch interpreter evaluating a store app's
        `evaluate` storage entry — exactly what the official App Loader
        does for files like the heatshrink-compressed `.img` icon. It is
        not a host-side eval and adds no attack surface beyond installing
        the chosen community app, whose code runs on the watch anyway."""
        await self._run('var __a=""')
        for i in range(0, len(text), _BANGLE_UPLOAD_CHUNK):
            chunk = text[i:i + _BANGLE_UPLOAD_CHUNK]
            resp = await self._run(f"__a+={json.dumps(chunk)}")
            if _looks_like_espruino_error(resp):
                raise RuntimeError(
                    f"Bangle: error while uploading {name!r}")
        resp = await self._run(
            f'require("Storage").write({json.dumps(name)},{wrap}(__a))')
        await self._run("__a=undefined")
        if _looks_like_espruino_error(resp):
            raise RuntimeError(f"Bangle: Storage.write failed for {name!r}")

    # ── Firmware update (Nordic Secure DFU) ────────────────────────

    async def fetch_default_firmware(
            self, version: str = _BANGLE_FW_DEFAULT_VERSION) -> bytes:
        """Download an Espruino firmware DFU `.zip` for Bangle.js 2."""
        url = _BANGLE_FW_URL.format(version=version)
        return await asyncio.to_thread(_download_bangle_firmware, url)

    async def flash_firmware(self, firmware: bytes, on_progress=None,
                             dfu_address: str | None = None) -> None:
        """Flash an Espruino DFU `.zip` over Nordic Secure DFU.

        The watch must already be in DFU mode: Espruino disables remote
        DFU entry, so the user long-presses the button until the watch
        advertises as `DfuTarg`. We then connect to that bootloader and
        stream the image. A failed flash is recoverable — the bootloader
        stays and the watch falls back to DFU mode. See
        docs/bangle-firmware.md."""
        from bleak import BleakClient, BleakScanner

        from vitals.devices.bangle_dfu import (
            DFU_TARGET_NAME, parse_dfu_package, run_dfu)

        init_packet, image = parse_dfu_package(firmware)
        address = dfu_address
        if address is None:
            log.info("Bangle: scanning for the %s bootloader", DFU_TARGET_NAME)
            device = await BleakScanner.find_device_by_name(
                DFU_TARGET_NAME, timeout=30.0)
            if device is None:
                raise RuntimeError(
                    "No DFU target found — long-press the watch into DFU "
                    "mode (it advertises as DfuTarg) and try again")
            address = device.address
        client = BleakClient(address)
        await client.connect()
        try:
            await run_dfu(client, init_packet, image, on_progress)
        finally:
            await client.disconnect()

    @staticmethod
    def _extract_print_output(response: str) -> str | None:
        """Pull the print() output from an Espruino REPL response.

        Bangle's REPL echoes the command, then the print-output line(s),
        then `=<return-value>`, then the prompt:

            print(Bangle.getBattery())\\r\\n85\\r\\n=undefined\\r\\n>
            \\----- echo ------------/    \\__ output __/

        The text between the first `\\r\\n` and the `\\r\\n=` is the
        print output. Returns None if the response doesn't have that
        shape (e.g. timed out, or the watch returned an error).
        """
        match = re.search(r"\r\n(.*?)\r\n=", response, re.DOTALL)
        if match is None:
            return None
        return match.group(1)

    @classmethod
    def _parse_battery(cls, response: str) -> int | None:
        """Parse `Bangle.getBattery()`'s print() output to an int 0–100.

        v0.3.7 used `re.finditer(r"\\b\\d+\\b", response)` — the first
        integer in [0, 100] anywhere in the response. That picked up
        random digits from anywhere in the response (e.g. characters
        in echo buffers, terminal control codes) and could yield
        bogus low values like 1. v0.4.0 anchors on the print() output
        line specifically and accepts both int and float forms.
        """
        out = cls._extract_print_output(response)
        if out is None:
            return None
        try:
            n = float(out.strip())
        except (ValueError, TypeError):
            return None
        rounded = round(n)
        if 0 <= rounded <= 100:
            return rounded
        return None

    @classmethod
    def _parse_activity(cls, response: str) -> ActivityReading | None:
        """Parse `Bangle.getHealthStatus()`'s JSON output.

        Expected shape (Bangle.js 2 firmware):
            {bpm, bpmConfidence, steps, movement, ...}

        Returns None on missing-method firmware (where the watch
        prints "null") or unparseable response.
        """
        out = cls._extract_print_output(response)
        if out is None:
            return None
        out = out.strip()
        if not out or out == "null" or out == "undefined":
            return None
        try:
            data = json.loads(out)
        except json.JSONDecodeError:
            log.warning("Bangle: couldn't parse getHealthStatus response: %r",
                        out[:200])
            return None
        if not isinstance(data, dict):
            return None
        steps = data.get("steps")
        bpm = data.get("bpm")
        bpmc = data.get("bpmConfidence")
        # bpm == 0 with low/zero confidence means "no reading", not zero
        # heart rate. Treat that as None so the UI doesn't display "0 bpm".
        if isinstance(bpm, (int, float)) and bpm == 0 and (
                not isinstance(bpmc, (int, float)) or bpmc <= 50):
            bpm = None
            bpmc = None
        return ActivityReading(
            steps=int(steps) if isinstance(steps, (int, float)) else None,
            heart_rate_bpm=int(bpm) if isinstance(bpm, (int, float)) else None,
            heart_rate_confidence=(
                int(bpmc) if isinstance(bpmc, (int, float)) else None),
            timestamp=time.time(),
        )

    @classmethod
    def _parse_sched_response(cls, response: str) -> list[dict]:
        """Parse a `print(Storage.read("sched.json")||"[]")` response
        to a list of alarm dicts. Returns [] for any unparseable or
        missing response."""
        out = cls._extract_print_output(response)
        if out is None:
            return []
        out = out.strip()
        if not out or out == "undefined" or out == "null":
            return []
        try:
            parsed = json.loads(out)
        except json.JSONDecodeError:
            log.warning("Bangle: couldn't parse existing sched.json: %r",
                        out[:300])
            return []
        if not isinstance(parsed, list):
            return []
        return [e for e in parsed if isinstance(e, dict)]


def _download_bangle_firmware(url: str, timeout: float = 120.0) -> bytes:
    """Download an Espruino firmware DFU `.zip`. Blocking — call via a
    worker thread."""
    import urllib.request
    log.info("Bangle: downloading firmware %s", url)
    req = urllib.request.Request(url, headers={"User-Agent": "vitals"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()
