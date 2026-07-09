"""Pebble device plugin (repebble / PebbleOS firmware).

The revived Pebble watches from Core Devices (Core 2 Duo, Core Time 2)
and the classic Pebbles all run the now-open-source PebbleOS and speak
the long-established *Pebble Protocol*. A live scan of a Core Devices
"obelix" watch (firmware v4.9.142) shows it exposes a few standard
Bluetooth SIG services directly — Battery (0x180F) and Device
Information (0x180A) read straight over GATT, no bonding required — but
its real features (time, health, apps, notifications) do not surface as
GATT characteristics. Those ride a framed, sequenced transport called
**PPoGATT** (Pebble Protocol over GATT).

  ┌─────────────────────────────────────────────────────────────┐
  │ PPoGATT data packet (one GATT write)                          │
  │  ┌────────────┬──────────────────────────────────────────┐   │
  │  │ 1-byte hdr │ Pebble Protocol packet                    │   │
  │  │ seq<<3|cmd │  ┌────────┬──────────┬─────────────────┐  │   │
  │  │            │  │ len:u16│ endpt:u16│ payload         │  │   │
  │  │            │  └────────┴──────────┴─────────────────┘  │   │
  │  └────────────┴──────────────────────────────────────────┘   │
  └─────────────────────────────────────────────────────────────┘

  - PPoGATT header byte: low 3 bits = command, high 5 bits = sequence
    (mod 32). Commands: 0 DATA, 1 ACK, 2 RESET, 3 RESET-ACK. The peer
    ACKs each DATA packet by echoing its sequence with command 1.
  - Pebble Protocol envelope: uint16 payload-length + uint16 endpoint,
    both big-endian, then the payload.
  - Time lives on endpoint 0x000b; SetUTC (kind 0x03) carries a uint32
    unix time, an int16 UTC offset in minutes, and a Pascal-string
    timezone name. See `_encode_set_utc` / `_build_set_time_packet`.

The watch hosts no PPoGATT service of its own: the *host* runs the GATT
server and the watch connects into it as a client. That whole transport
— force-LE connect, authenticated pairing, the GATT server + link, and
the connectivity-subscription that makes the watch open PPoGATT — lives
in `vitals.devices.ppogatt.PebbleGateway` (it needs `Experimental = true`
in /etc/bluetooth/main.conf). This plugin is a thin layer over it:
`connect()` brings the gateway up, `get_battery()` / `get_device_info()`
read standard characteristics, and `sync_time()` sends a SetUTC over
PPoGATT.

Status — discovery, battery/device-info reads, and time sync all work.
Note `sync_time` only takes visible effect once the watch is past
first-time setup; an un-onboarded watch reports the time endpoint as
unhandled (see docs/pebble-ppogatt.md). Alarms, notifications, and
activity/health are not wired yet.

References (the repebble developer docs cover the on-watch app SDK, not
this companion protocol):
  - Pebble Protocol / Time endpoint:  github.com/pebble/libpebble2
  - PPoGATT framing + GATT UUIDs:     codeberg.org/Freeyourgadget/Gadgetbridge
  - BLE reference implementation:     github.com/leso-kn/pebble-le
"""

from __future__ import annotations

import asyncio
import logging
import struct
import time

from vitals.devices.base import (
    ActivityReading, Device, SleepSession, WorkoutSession, register_device)
from vitals.devices.pebble.ppogatt import PebbleGateway

log = logging.getLogger(__name__)

# Pebble LE Pairing Service — advertised by the watch; recognised in a
# scan (see matches()). Confirmed live on a Core Devices "obelix" watch
# (fw v4.9.142): the watch hosts GAP/GATT, Device Information, this
# pairing service, and a standard Battery Service — and nothing else.
PAIRING_SERVICE_UUID = "0000fed9-0000-1000-8000-00805f9b34fb"

# Characteristics inside the pairing service (Pebble vendor UUID base).
# Connectivity is read/notify; writing the trigger characteristic asks
# the watch to start LE bonding — the Gadgetbridge-style pairing path,
# used instead of the Core Devices companion app.
PAIRING_CONNECTIVITY_CHAR = "00000001-328e-0fbb-c642-1aa6699bdada"
PAIRING_TRIGGER_CHAR      = "00000002-328e-0fbb-c642-1aa6699bdada"

# Standard Bluetooth SIG characteristics the watch exposes directly —
# readable over plain GATT, no PPoGATT and no bonding.
BATTERY_LEVEL_CHAR_UUID     = "00002a19-0000-1000-8000-00805f9b34fb"
# Device Information (0x180A) strings.
MODEL_NUMBER_CHAR_UUID      = "00002a24-0000-1000-8000-00805f9b34fb"
FIRMWARE_REVISION_CHAR_UUID = "00002a26-0000-1000-8000-00805f9b34fb"
MANUFACTURER_NAME_CHAR_UUID = "00002a29-0000-1000-8000-00805f9b34fb"

# PPoGATT data service + characteristics. NOTE: the watch does *not*
# host these — a live scan found no PPoGATT service on the device, which
# confirms the data channel runs the other way (the host hosts the GATT
# server the watch connects into). Kept for the transport work — see the
# module docstring — and to be reconciled with the host-server role when
# it lands.
PPOGATT_SERVICE_UUID = "30000003-328e-0fbb-c642-1aa6699bdada"
PPOGATT_READ_CHAR    = "30000004-328e-0fbb-c642-1aa6699bdada"  # notify
PPOGATT_WRITE_CHAR   = "30000006-328e-0fbb-c642-1aa6699bdada"  # write

# PPoGATT packet commands (low 3 bits of the header byte).
PPOGATT_DATA      = 0
PPOGATT_ACK       = 1
PPOGATT_RESET     = 2
PPOGATT_RESET_ACK = 3
PPOGATT_SEQ_MOD   = 32  # sequence is 5 bits

# Pebble Protocol endpoints + message kinds we encode.
ENDPOINT_TIME    = 0x000B
TIME_SET_UTC     = 0x03
ENDPOINT_VERSION = 0x0010  # WatchVersion (request 0x00 → response 0x01)
BLOB_DB_WEATHER  = 0x05    # BlobDBIdWeather — the watch's weather database

# Where the open PebbleOS firmware is published. Onboarding a PRF watch
# downloads the matching normal-firmware bundle from the latest release.
PEBBLE_FW_REPO  = "coredevices/PebbleOS"
PEBBLE_FW_API   = f"https://api.github.com/repos/{PEBBLE_FW_REPO}"
# Per-watch firmware variant. obelix ships as production (pvt) or
# development (dvt) builds; a consumer watch is pvt. A wrong variant is
# rejected by the bootloader's platform check — safe, the watch stays in
# PRF — so pvt is a safe default with dvt as the fallback.
PEBBLE_FW_DEFAULT_MODEL   = "obelix"
PEBBLE_FW_DEFAULT_VARIANT = "pvt"
PEBBLE_FW_DEFAULT_SLOT    = 0
# App/SDK platform for obelix (Pebble Time 2) — which build to pull from
# a multi-platform .pbw.
PEBBLE_APP_PLATFORM = "emery"


@register_device
class PebbleDevice(Device):
    id = "pebble"
    display_name = "Pebble"
    description = "Pebble / Core Devices watch running PebbleOS firmware"
    CATEGORY = "watch"
    ICON_NAME = "phone-symbolic"
    PAIRING_STEPS = [
        "On the Pebble, open Settings → Bluetooth.",
        "Keep it nearby and search. A factory-fresh Pebble showing a "
        "setup screen is fine — you'll be offered to set it up.",
    ]

    # Everything rides the PPoGATT transport (PebbleGateway). Alarms are
    # watch-local on Pebble, so alarm push stays off by design.
    SUPPORTS_TIME_SYNC       = True
    SUPPORTS_ALARM_PUSH      = False
    SUPPORTS_NOTIFICATIONS   = True
    SUPPORTS_ACTIVITY_READ   = True
    SUPPORTS_SLEEP_READ      = True
    SUPPORTS_WORKOUT_READ    = True
    SUPPORTS_FIRMWARE_UPDATE = True
    SUPPORTS_APP_INSTALL     = True
    SUPPORTS_WEATHER_PUSH    = True
    SUPPORTS_CALENDAR_PUSH   = True
    SUPPORTS_MUSIC_CONTROL   = True

    # The host-side PPoGATT GATT server can only exist once per process;
    # DeviceManager serializes anything sharing this transport name.
    EXCLUSIVE_TRANSPORT = "ppogatt-server"

    @classmethod
    def matches(cls, advertised_name: str | None,
                service_uuids: list[str]) -> bool:
        if advertised_name:
            n = advertised_name.lower()
            # Classic "Pebble …" plus the new Core Devices model names
            # ("Core 2 Duo", "Core Time 2"). Names are best-effort until
            # confirmed against a real advertisement.
            if n.startswith("pebble"):
                return True
            if n.startswith("core") and ("time" in n or "duo" in n):
                return True
        return PAIRING_SERVICE_UUID in [u.lower() for u in service_uuids]

    def __init__(self, address: str, name: str = ""):
        super().__init__(address, name)
        self._client = None
        # Raw drained DataLogging health sessions for this connection. The
        # drain is destructive (the watch empties each session as we read),
        # so steps, heart rate and sleep all decode from this one cached
        # drain rather than reading the watch three times.
        self._health_sessions: dict | None = None

    # ── Lifecycle ──────────────────────────────────────────────────

    async def connect(self) -> None:
        # PebbleGateway brings up the whole connection: force-LE connect
        # (bleak can't — the dual-mode watch makes bluetoothd try classic
        # Bluetooth and time out), authenticated pairing, the PPoGATT
        # server + link, and the connectivity subscription that opens
        # PPoGATT. See vitals.devices.ppogatt.
        log.info("Pebble: bringing up PPoGATT gateway to %s", self.address)
        self._client = PebbleGateway(self.address)
        self._health_sessions = None
        await self._client.connect()
        # Base handler for unsolicited messages (music buttons). The
        # firmware/app/health flows swap their own handlers in and
        # restore this one when done.
        self._client.set_message_handler(self._on_unsolicited)
        await self._log_device_info()

    def _on_unsolicited(self, endpoint: int, payload: bytes) -> None:
        from vitals.devices.pebble.music import (
            ENDPOINT_MUSIC, decode_watch_command)
        if endpoint == ENDPOINT_MUSIC:
            handler = getattr(self, "_music_handler", None)
            command = decode_watch_command(payload)
            if handler is not None and command is not None:
                handler(command)

    async def disconnect(self) -> None:
        if self._client is None:
            return
        try:
            await self._client.disconnect()
        finally:
            self._client = None

    @property
    def is_connected(self) -> bool:
        return self._client is not None and self._client.is_connected

    # ── Feature methods ────────────────────────────────────────────

    async def sync_time(self, unix_timestamp: float) -> None:
        """Set the watch clock with a Pebble Protocol SetUTC message over
        PPoGATT (endpoint 0x000B).

        Note: the watch only applies this once it is past first-time
        setup — an un-onboarded watch reports the time endpoint as
        unhandled (see docs/pebble-ppogatt.md)."""
        if self._client is None:
            raise RuntimeError("not connected")
        lt = time.localtime(unix_timestamp)
        offset_min = (lt.tm_gmtoff or 0) // 60
        tz_name = lt.tm_zone or "UTC"
        payload = self._encode_set_utc(unix_timestamp, offset_min, tz_name)
        self._client.send_message(ENDPOINT_TIME, payload)
        log.info("Pebble: sent SetUTC (offset %d min, tz %s)",
                 offset_min, tz_name)

    async def flash_firmware(self, firmware: bytes,
                             on_progress=None) -> None:
        """Flash a `.pbz` firmware bundle onto the watch over PPoGATT.

        This onboards a watch that ships in PRF (recovery firmware,
        showing the QR screen) by installing the normal firmware + its
        resource pack; the watch reboots into normal operation, where
        time and health work. A failed flash leaves the watch in PRF to
        retry — PRF is the recovery net, so this can't brick it.

        `on_progress(stage, sent, total)` reports transfer progress per
        object ("firmware" then "resources")."""
        if self._client is None:
            raise RuntimeError("not connected")
        # Imported lazily so the protocol/zip machinery only loads when a
        # flash is actually requested.
        from vitals.devices.pebble.fw_update import FirmwareUpdater, parse_pbz
        bundle = parse_pbz(firmware)
        log.info("Pebble: flashing firmware %s for %s",
                 bundle.version, bundle.hardware)
        # Size each PutBytes chunk so the whole chunk fits the link's
        # send window in one round-trip (see PpogattLink.PPOGATT_TX_INFLIGHT).
        chunk_size = 6 * self._client.max_payload
        updater = FirmwareUpdater(self._client.send_message_async,
                                  on_progress=on_progress,
                                  chunk_size=chunk_size)
        self._client.set_message_handler(updater.handle_message)
        try:
            await updater.flash(bundle)
        finally:
            self._client.set_message_handler(self._on_unsolicited)

    @classmethod
    def app_store(cls):
        from vitals.devices.pebble.pebble_store import PebbleStore
        return PebbleStore()

    async def install_app(self, bundle: bytes, on_progress=None) -> None:
        """Install a `.pbw` app/watchface onto the watch over PPoGATT.

        Parses the bundle for the emery (obelix) build, stores its
        metadata in the watch's BlobDB, then transfers the binary,
        resources, and worker via PutBytes. `on_progress(stage, sent,
        total)` reports each object's transfer."""
        if self._client is None or not self._client.is_link_open:
            raise RuntimeError("PPoGATT link is not open")
        from vitals.devices.pebble.pbw import parse_pbw
        from vitals.devices.pebble.pebble_appinstall import AppInstaller
        app = parse_pbw(bundle, platform=PEBBLE_APP_PLATFORM)
        log.info("Pebble: installing %s (%s build, uuid %s)",
                 app.name, app.platform, app.uuid.hex())
        chunk_size = 6 * self._client.max_payload
        installer = AppInstaller(self._client.send_message_async,
                                 on_progress=on_progress, chunk_size=chunk_size)
        self._client.set_message_handler(installer.handle_message)
        try:
            await installer.install(app)
        finally:
            self._client.set_message_handler(self._on_unsolicited)

    async def _blobdb(self, payload: bytes, what: str,
                      timeout: float = 15.0) -> None:
        """One BlobDB command over the open link, checked for success."""
        if self._client is None or not self._client.is_link_open:
            raise RuntimeError("PPoGATT link is not open")
        from vitals.devices.pebble.pebble_appinstall import (
            BLOB_SUCCESS, EP_BLOBDB, parse_blob_response)
        resp = await self._client.request(EP_BLOBDB, payload, EP_BLOBDB,
                                          timeout=timeout)
        _token, status = parse_blob_response(resp)
        if status != BLOB_SUCCESS:
            raise RuntimeError(f"{what} failed (BlobDB status {status})")

    def _blob_token(self) -> int:
        self._blob_token_counter = (
            getattr(self, "_blob_token_counter", 0) + 1) & 0xFFFF
        return self._blob_token_counter

    async def push_weather(self, forecast) -> None:
        """Store the forecast in the watch's weather database *and*
        enrol the location in the Weather app's settings entry — the
        app only displays enrolled locations, which is why records
        stored under arbitrary keys never appeared."""
        from vitals.devices.pebble.pebble_appinstall import (
            BLOB_DB_APPSETTINGS, encode_blobdb_clear, encode_blobdb_insert)
        from vitals.devices.pebble.pebble_weather import (
            UUID_PRIMARY_LOCATION, WEATHER_APP_SETTINGS_KEY,
            encode_app_settings, serialize_entry)
        # Clear first: it also removes invisible tock-era entries stored
        # under the old derived keys.
        await self._blobdb(
            encode_blobdb_clear(self._blob_token(), BLOB_DB_WEATHER),
            "weather clear")
        value = serialize_entry(forecast)
        await self._blobdb(
            encode_blobdb_insert(self._blob_token(), BLOB_DB_WEATHER,
                                 UUID_PRIMARY_LOCATION.bytes, value),
            "weather insert")
        await self._blobdb(
            encode_blobdb_insert(self._blob_token(), BLOB_DB_APPSETTINGS,
                                 WEATHER_APP_SETTINGS_KEY,
                                 encode_app_settings()),
            "weather app enrolment")
        log.info("Pebble: weather stored + enrolled (%d bytes)", len(value))

    async def push_notification(self, note) -> None:
        """Show one forwarded notification via the notification BlobDB."""
        from vitals.devices.pebble.pebble_appinstall import (
            BLOB_DB_NOTIFICATION, encode_blobdb_insert)
        from vitals.devices.pebble.timeline import encode_notification
        key, value = encode_notification(note)
        await self._blobdb(
            encode_blobdb_insert(self._blob_token(), BLOB_DB_NOTIFICATION,
                                 key, value),
            "notification insert", timeout=10.0)

    async def push_now_playing(self, track) -> None:
        """Update the Music app: track info then play state."""
        if self._client is None or not self._client.is_link_open:
            raise RuntimeError("PPoGATT link is not open")
        from vitals.devices.pebble.music import (
            ENDPOINT_MUSIC, encode_music_info, encode_play_state)
        self._client.send_message(ENDPOINT_MUSIC, encode_music_info(
            track.artist, track.album, track.track, track.duration_s))
        self._client.send_message(ENDPOINT_MUSIC, encode_play_state(
            track.playing, track.position_s))

    async def push_calendar(self, events, stale_pin_ids) -> None:
        """Reconcile the timeline's calendar pins (BlobDB db 1): delete
        pins for vanished events, then upsert one pin per event (pins
        are keyed by their uuid, so re-inserts update in place)."""
        from vitals.calendar import pin_uuid
        from vitals.devices.pebble.pebble_appinstall import (
            BLOB_DB_PIN, encode_blobdb_delete, encode_blobdb_insert)
        from vitals.devices.pebble.timeline import encode_pin
        for stale in stale_pin_ids:
            try:
                await self._blobdb(
                    encode_blobdb_delete(self._blob_token(), BLOB_DB_PIN,
                                         bytes.fromhex(stale)),
                    "pin delete", timeout=10.0)
            except RuntimeError as exc:
                # Already gone on the watch (KEY_DOES_NOT_EXIST) — fine.
                log.debug("Pebble: pin delete skipped: %s", exc)
        for event in events:
            key, value = encode_pin(
                pin_uuid(event), event.start_utc, event.duration_min,
                event.title, body=event.description,
                location=event.location)
            await self._blobdb(
                encode_blobdb_insert(self._blob_token(), BLOB_DB_PIN,
                                     key, value),
                "pin insert", timeout=10.0)
        log.info("Pebble: calendar pins reconciled (%d current, %d removed)",
                 len(events), len(stale_pin_ids))

    async def fetch_default_firmware(
            self, variant: str = PEBBLE_FW_DEFAULT_VARIANT,
            slot: int = PEBBLE_FW_DEFAULT_SLOT,
            version: str | None = None) -> bytes:
        """Download the latest matching normal-firmware `.pbz` for this
        watch (the bytes `flash_firmware` expects). The blocking HTTP
        fetch runs in a worker thread so it never stalls the BLE loop.

        `version` pins a release tag (e.g. "v4.12.0"); the default takes
        the latest. The watch model is read from Device Information when
        connected, else defaults to obelix."""
        model = PEBBLE_FW_DEFAULT_MODEL
        if self._client is not None:
            info = await self.get_device_info()
            model = info.get("model") or model
        return await asyncio.to_thread(
            download_pebble_firmware, model, variant, slot, version)

    async def is_in_recovery(self) -> bool | None:
        """Whether the watch is running recovery firmware (PRF / the
        setup QR screen) and needs onboarding.

        Queries WatchVersion (endpoint 0x0010) over PPoGATT and reads the
        running firmware's recovery flag. Returns None if the link isn't
        open or the query fails."""
        if self._client is None or not self._client.is_link_open:
            return None
        try:
            resp = await self._client.request(
                ENDPOINT_VERSION, b"\x00", ENDPOINT_VERSION, timeout=10.0)
        except Exception:
            log.debug("Pebble: WatchVersion query failed", exc_info=True)
            return None
        return self._parse_is_recovery(resp)

    @staticmethod
    def _parse_is_recovery(resp: bytes) -> bool | None:
        """Read the running firmware's recovery flag from a WatchVersion
        response.

        Layout (big-endian, per libpebble2): command (0x01) then the
        running FirmwareMetadata — uint32 timestamp, 32-byte version tag,
        8-byte git hash, then the recovery-flag byte at offset 45.
        Validated against a captured obelix watch: its PRF firmware sets
        bit 0 of that byte and the normal firmware clears it (Core
        Devices packs other flags into the high bits, so test bit 0 only
        rather than the whole byte)."""
        if len(resp) < 46 or resp[0] != 0x01:
            return None
        return bool(resp[45] & 0x01)

    async def get_battery(self) -> int | None:
        """Read the standard Battery Level characteristic (0x2A19).

        Unlike most Pebble features, battery is a plain Bluetooth SIG
        characteristic on the watch — readable without PPoGATT or
        bonding."""
        if self._client is None:
            return None
        try:
            data = await self._client.read_gatt_char(BATTERY_LEVEL_CHAR_UUID)
        except Exception:
            log.exception("Pebble: battery read failed")
            return None
        return self._parse_battery_level(data)

    async def get_activity_series(self) -> list[ActivityReading] | None:
        """Read the watch's health data as per-minute interval deltas.

        Drains the health DataLogging sessions and returns one
        `ActivityReading` per non-empty minute (steps, heart rate, active
        energy and distance for that minute when logged), each
        `interval_seconds=60`. The app sums these into a day total and
        ships each to Pulse as an interval record, so the cumulative total
        stays correct even though the watch empties its buffer on every
        drain. Returns [] when the watch had no new health data."""
        from vitals.devices.pebble.pebble_health import (
            CALORIES_PER_KCAL, decode_minute_sessions)
        samples = decode_minute_sessions(await self._drain_health_sessions())
        readings: list[ActivityReading] = []
        for s in samples:
            if (s.steps == 0 and s.heart_rate_bpm is None
                    and not s.active_calories and not s.distance_cm):
                continue  # an idle minute with nothing worth logging
            readings.append(ActivityReading(
                steps=s.steps,
                heart_rate_bpm=s.heart_rate_bpm,
                heart_rate_confidence=None,
                timestamp=float(s.time_utc),
                interval_seconds=60,
                active_kcal=(None if s.active_calories is None
                             else s.active_calories / CALORIES_PER_KCAL),
                distance_m=(None if s.distance_cm is None
                            else s.distance_cm / 100),
            ))
        log.info("Pebble: %d health minute(s) drained", len(readings))
        return readings

    async def get_activity(self) -> ActivityReading | None:
        """A single snapshot — today's drained steps + latest heart rate.

        Note this only reflects the minutes drained *this* read (the
        drain is destructive); the accurate running total lives in the
        sinks/store the series feeds. Prefer `get_activity_series`."""
        from vitals.devices.pebble.pebble_health import summarize_sessions
        summary = summarize_sessions(await self._drain_health_sessions())
        if summary.minutes == 0 and summary.latest_heart_rate is None:
            return None
        return ActivityReading(
            steps=summary.steps_today if summary.minutes else None,
            heart_rate_bpm=summary.latest_heart_rate,
            heart_rate_confidence=None,
            timestamp=time.time(),
        )

    async def get_sleep_series(self) -> list[SleepSession] | None:
        """Read the watch's sleep/nap sessions since the last drain.

        The firmware's own Pebble Health algorithm logs each finalised
        sleep session (and the restful/deep periods nested inside it) to
        DataLogging; we pair each overall session with the deep periods it
        contains so the deep time is reported without double-counting the
        total. Shares the one destructive drain with steps/HR. Returns []
        when the watch had no new sleep data."""
        from vitals.devices.pebble.pebble_health import decode_sleep_sessions
        raw = decode_sleep_sessions(await self._drain_health_sessions())
        overall = [s for s in raw if not s.is_deep]
        deep = [s for s in raw if s.is_deep]
        sessions: list[SleepSession] = []
        for s in overall:
            deep_spans = tuple(
                (float(d.start_utc), float(d.end_utc)) for d in deep
                if d.start_utc >= s.start_utc and d.end_utc <= s.end_utc)
            sessions.append(SleepSession(
                start=float(s.start_utc),
                end=float(s.end_utc),
                deep_spans=deep_spans,
                is_nap=s.is_nap,
            ))
        log.info("Pebble: %d sleep session(s) drained", len(sessions))
        return sessions

    async def get_workout_series(self) -> list[WorkoutSession] | None:
        """Read the watch's detected workouts (walk/run/generic) since the
        last drain. Shares the one destructive health drain with steps,
        HR and sleep. Returns [] when there were no new workouts."""
        from vitals.devices.pebble.pebble_health import (
            WORKOUT_NAMES, decode_workout_sessions)
        raw = decode_workout_sessions(await self._drain_health_sessions())
        workouts = [
            WorkoutSession(
                start=float(w.start_utc),
                end=float(w.end_utc),
                kind=WORKOUT_NAMES.get(w.type, "workout"),
                steps=w.steps,
                active_kcal=w.active_kcalories,
                distance_m=(None if w.distance_meters is None
                            else float(w.distance_meters)),
            )
            for w in raw
        ]
        log.info("Pebble: %d workout(s) drained", len(workouts))
        return workouts

    async def get_heart_rate_samples(self) -> list[ActivityReading]:
        """Read the watch's per-sample heart rate from the protobuf log
        (tag 85) — finer-grained than the per-minute median, with a signal
        quality grade we use to drop off-wrist / no-signal junk. Shares the
        one destructive health drain. Returns point HR readings
        (`interval_seconds` unset); [] when there were none.

        This is a best-effort extra on top of the per-minute HR, so any
        decode failure degrades to [] rather than failing the whole sync."""
        sessions = await self._drain_health_sessions()
        try:
            from vitals.devices.pebble.pebble_health import decode_hr_sample_sessions
            samples = decode_hr_sample_sessions(sessions)
        except Exception:
            log.exception("Pebble: heart-rate sample decode unavailable")
            return []
        readings = [
            ActivityReading(
                heart_rate_bpm=s.bpm,
                timestamp=float(s.time_utc),
            )
            for s in samples
            if s.is_trustworthy and 30 <= s.bpm <= 240
        ]
        log.info("Pebble: %d heart-rate sample(s) drained", len(readings))
        return readings

    async def _drain_health_sessions(self) -> dict:
        """Drain the health DataLogging sessions once, caching the raw
        `{session_id: (OpenSession, bytes)}` for this connection. The
        drain is destructive (the watch empties each session as we read),
        so steps, heart rate and sleep all decode from this one cache."""
        if self._health_sessions is not None:
            return self._health_sessions
        if self._client is None or not self._client.is_link_open:
            self._health_sessions = {}
            return self._health_sessions
        from vitals.devices.pebble.pebble_health import HealthCollector
        collector = HealthCollector(self._client.send_message)
        self._client.set_message_handler(collector.handle_message)
        try:
            self._health_sessions = await collector.collect()
        except Exception:
            log.exception("Pebble: health collection failed")
            self._health_sessions = {}
        finally:
            self._client.set_message_handler(self._on_unsolicited)
        return self._health_sessions

    async def get_device_info(self) -> dict[str, str]:
        """Read the Device Information strings (model codename, firmware
        revision, manufacturer) over standard GATT.

        Returns {} when not connected; individually unreadable or empty
        fields are simply omitted rather than failing the whole read."""
        if self._client is None:
            return {}
        fields = {
            "model":        MODEL_NUMBER_CHAR_UUID,
            "firmware":     FIRMWARE_REVISION_CHAR_UUID,
            "manufacturer": MANUFACTURER_NAME_CHAR_UUID,
        }
        info: dict[str, str] = {}
        for key, uuid in fields.items():
            try:
                data = await self._client.read_gatt_char(uuid)
            except Exception:
                continue
            text = bytes(data).decode("utf-8", "replace").strip()
            if text:
                info[key] = text
        return info

    async def _log_device_info(self) -> None:
        """Best-effort diagnostic log of model + firmware after connect.
        Never raises — device-info reads are optional."""
        try:
            info = await self.get_device_info()
        except Exception:
            return
        if info:
            log.info("Pebble: connected to %s (model=%s firmware=%s)",
                     info.get("manufacturer", "?"),
                     info.get("model", "?"),
                     info.get("firmware", "?"))

    @staticmethod
    def _parse_battery_level(data: bytes | bytearray | None) -> int | None:
        """The Battery Level characteristic is a single uint8 (0..100).
        Anything outside that range means a bad read — return None."""
        if not data:
            return None
        level = int(data[0])
        return level if 0 <= level <= 100 else None

    # ── Wire-format encoders (transport-independent) ───────────────
    # These are pure and fully unit-tested; they are the building
    # blocks the future PPoGATT transport will send. Kept here so the
    # protocol details live next to the device that uses them.

    @staticmethod
    def _ppogatt_header(command: int, sequence: int) -> int:
        """Pack a PPoGATT header byte: high 5 bits sequence (mod 32),
        low 3 bits command."""
        return ((sequence % PPOGATT_SEQ_MOD) << 3) | (command & 0x07)

    @staticmethod
    def _parse_ppogatt_header(byte: int) -> tuple[int, int]:
        """Inverse of `_ppogatt_header` → (command, sequence)."""
        return byte & 0x07, (byte >> 3) & 0x1F

    @staticmethod
    def _frame_pebble_packet(endpoint: int, payload: bytes) -> bytes:
        """Wrap `payload` in the Pebble Protocol envelope:
        uint16 length + uint16 endpoint (both big-endian) + payload.
        The length field counts the payload only, not the header."""
        return struct.pack(">HH", len(payload), endpoint) + payload

    @staticmethod
    def _encode_set_utc(unix_timestamp: float,
                        utc_offset_minutes: int,
                        tz_name: str) -> bytes:
        """Encode a Time-endpoint SetUTC message payload.

        Layout (big-endian, per libpebble2 protocol/system.py):
            uint8   kind          = 0x03 (SetUTC)
            uint32  unix_time     (seconds since epoch, UTC)
            int16   utc_offset    (local minus UTC, in minutes)
            uint8   tz_name length
            bytes   tz_name       (UTF-8, Pascal string)
        """
        tz_bytes = tz_name.encode("utf-8")[:255]
        return (struct.pack(">BIh", TIME_SET_UTC,
                            int(unix_timestamp) & 0xFFFFFFFF,
                            utc_offset_minutes)
                + bytes([len(tz_bytes)]) + tz_bytes)

    @classmethod
    def _build_set_time_packet(cls, unix_timestamp: float,
                               utc_offset_minutes: int, tz_name: str,
                               sequence: int) -> bytes:
        """The full bytes for one PPoGATT DATA write that sets the
        watch clock: header byte + framed Time/SetUTC packet.

        Only valid when the SetUTC payload fits a single GATT write;
        larger payloads (long timezone names past the negotiated MTU)
        will need chunking once the transport exists.
        """
        inner = cls._encode_set_utc(
            unix_timestamp, utc_offset_minutes, tz_name)
        framed = cls._frame_pebble_packet(ENDPOINT_TIME, inner)
        return bytes([cls._ppogatt_header(PPOGATT_DATA, sequence)]) + framed


# ── Firmware source ────────────────────────────────────────────────
# The published PebbleOS releases name assets
# `normal_<model>_<variant>_<tag>_slot<n>.pbz`; pick the matching one
# from the newest release that has it. Kept module-level (not a method)
# so it can run off the BLE loop in a worker thread.

def _firmware_asset_name(model: str, variant: str, version: str,
                         slot: int) -> str:
    return f"normal_{model}_{variant}_{version}_slot{slot}.pbz"


def _find_release_asset(release: dict, name: str) -> str | None:
    for asset in release.get("assets", []):
        if asset.get("name") == name:
            return asset.get("browser_download_url")
    return None


def download_pebble_firmware(model: str = PEBBLE_FW_DEFAULT_MODEL,
                             variant: str = PEBBLE_FW_DEFAULT_VARIANT,
                             slot: int = PEBBLE_FW_DEFAULT_SLOT,
                             version: str | None = None,
                             timeout: float = 120.0) -> bytes:
    """Return the `.pbz` bytes for `model`/`variant`/`slot`, from the
    PebbleOS release tagged `version` (or the newest release that
    publishes a matching asset). Blocking — call via a worker thread.
    Raises RuntimeError if no matching asset exists."""
    import json
    import urllib.request

    def _get_json(url: str):
        req = urllib.request.Request(
            url, headers={"User-Agent": "vitals",
                          "Accept": "application/vnd.github+json"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.load(resp)

    url: str | None = None
    if version:
        release = _get_json(f"{PEBBLE_FW_API}/releases/tags/{version}")
        url = _find_release_asset(
            release, _firmware_asset_name(model, variant, version, slot))
    else:
        for release in _get_json(f"{PEBBLE_FW_API}/releases?per_page=20"):
            tag = release.get("tag_name") or ""
            url = _find_release_asset(
                release, _firmware_asset_name(model, variant, tag, slot))
            if url:
                version = tag
                break

    if not url:
        raise RuntimeError(
            f"no firmware found for {model}/{variant}/slot{slot}"
            + (f" at {version}" if version else " in recent releases"))

    log.info("Pebble: downloading firmware %s", url)
    req = urllib.request.Request(url, headers={"User-Agent": "vitals"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()
