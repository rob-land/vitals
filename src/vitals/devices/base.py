"""Device plugin base + registry.

A `Device` plugin knows how to talk to a specific family of watches —
how to recognise one in a BLE scan result, how to connect to it, and
how to read state and push commands. Plugins register themselves at
import time via the `@register_device` decorator and are discovered
through `available_devices()`.

Discovery happens in two passes:

  1. Built-in plugins (Bangle, PineTime) are imported eagerly the
     first time `available_devices()` is called.
  2. Third-party plugins shipped as separate Python packages are
     discovered via `importlib.metadata.entry_points` under the
     group `vitals.devices`. Each entry point's value is the import
     path of a module whose import has the side effect of calling
     `@register_device` on one or more Device subclasses.

A third-party Pebble plugin's pyproject.toml would declare:

    [project.entry-points."vitals.devices"]
    pebble = "vitals_pebble.device"

`vitals_pebble.device` is then imported at app startup; the
`@register_device` calls inside it land in this module's
`_REGISTRY` and the plugin shows up in pair / sync flows.
"""

from __future__ import annotations

import abc
import logging
from dataclasses import dataclass
from typing import ClassVar

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class ActivityReading:
    """A reading of the watch's wellness sensors.

    Any field may be None — different firmwares expose different
    things, and `Bangle.getHealthStatus()` for example may return
    `bpm: 0` when the heart-rate sensor isn't on. The application
    UI shows whichever fields actually came back.

    `interval_seconds` distinguishes two step semantics:

      * **None** — `steps` is the watch's *cumulative day total* at
        `timestamp` (a snapshot; the Bangle's monotonic daily counter).
      * **set** — `steps` is a *delta*: the count over the window
        `[timestamp, timestamp + interval_seconds)`. Sources that stream
        per-interval health records (e.g. a Pebble's minute records) use
        this; deltas sum to a daily total instead of being max'd.
    """
    steps: int | None = None
    heart_rate_bpm: int | None = None
    heart_rate_confidence: int | None = None  # 0-100
    timestamp: float = 0.0  # unix-seconds when we read it
    interval_seconds: int | None = None  # set => `steps` is an interval delta
    active_kcal: float | None = None   # active energy over the interval
    distance_m: float | None = None    # distance over the interval, metres


@dataclass(frozen=True)
class SleepSession:
    """One sleep (or nap) session detected by the watch.

    `start` / `end` are unix-seconds (UTC) bounding the asleep window.
    `deep_spans` are the restful (deep) sub-periods within it as
    `(start, end)` unix-second pairs — always subsets of the window, so
    the rest of the window is light sleep. `is_nap` marks a daytime nap
    rather than the main nightly sleep.
    """
    start: float
    end: float
    deep_spans: tuple[tuple[float, float], ...] = ()
    is_nap: bool = False

    @property
    def duration_seconds(self) -> float:
        return max(0.0, self.end - self.start)

    @property
    def deep_seconds(self) -> float:
        return sum(max(0.0, e - s) for s, e in self.deep_spans)


@dataclass(frozen=True)
class WorkoutSession:
    """One workout the watch detected (walk / run / generic).

    `start` / `end` are unix-seconds (UTC). `kind` is a short name
    (``walk``/``run``/``workout``). The metric fields are totals for the
    session and may be None when the watch didn't record them.
    """
    start: float
    end: float
    kind: str
    steps: int | None = None
    active_kcal: float | None = None
    distance_m: float | None = None

    @property
    def duration_seconds(self) -> float:
        return max(0.0, self.end - self.start)


@dataclass(frozen=True)
class HydrationReading:
    """One drink logged by a smart water bottle.

    `amount_ml` is the volume consumed in that sip/drink; `timestamp`
    is unix-seconds (UTC) when the bottle recorded it. `temperature_c`
    (water temperature) and `tds_ppm` (total dissolved solids — a
    water-quality reading) are optional: only the fuller bottle models
    have those sensors, so plainer bottles leave them None and the
    application never surfaces them for that device.
    """
    amount_ml: float
    timestamp: float
    temperature_c: float | None = None
    tds_ppm: int | None = None


@dataclass(frozen=True)
class WatchNotification:
    """One desktop notification to relay to a watch.

    ``id`` is stable across updates of the same banner (a messaging app
    updating its unread count), so a watch can replace rather than
    stack. Text is already plain (markup stripped)."""
    id: int
    app_name: str
    title: str
    body: str
    timestamp: float  # unix-seconds when it was posted


class Device(abc.ABC):
    """Abstract base for watch plugins.

    Subclasses set the class-level identity attributes (`id`,
    `display_name`, `description`), implement the `matches()`
    classmethod that gates discovery, and implement the connection
    lifecycle (`connect`, `disconnect`) plus per-feature methods
    (`get_battery`, `sync_time`, `push_alarms`, ...).

    Capability flags advertise which optional features the plugin
    actually supports. The default Device implementations of those
    methods raise NotImplementedError; the application checks the
    class-level flag before scheduling the call.

    The instance config (BLE address, watch name) is constructed by
    the application from GSettings and passed to `__init__`. The
    plugin's job is just: "given this address, talk to that watch."

    All async methods run on the BleManager's background event loop
    and should not touch GTK directly — the application marshals
    results back via GLib.idle_add.
    """

    # Short stable identifier persisted in GSettings (lower-snake-case).
    id: ClassVar[str]
    # User-visible label shown in Preferences and the pairing dialog.
    display_name: ClassVar[str]
    # One-line description.
    description: ClassVar[str]

    # ── Capability flags ──────────────────────────────────────────
    # Subclasses override to advertise support. The application
    # checks these before invoking optional feature methods so unsupported
    # combos surface as a polite toast rather than a stack trace.
    SUPPORTS_TIME_SYNC:       ClassVar[bool] = False
    SUPPORTS_ALARM_PUSH:      ClassVar[bool] = False
    SUPPORTS_NOTIFICATIONS:   ClassVar[bool] = False
    SUPPORTS_ACTIVITY_READ:   ClassVar[bool] = False
    SUPPORTS_SLEEP_READ:      ClassVar[bool] = False
    SUPPORTS_WORKOUT_READ:    ClassVar[bool] = False
    SUPPORTS_HYDRATION_READ:  ClassVar[bool] = False
    SUPPORTS_FIRMWARE_UPDATE: ClassVar[bool] = False
    SUPPORTS_APP_INSTALL:     ClassVar[bool] = False
    SUPPORTS_WEATHER_PUSH:    ClassVar[bool] = False
    # The device can measure health metrics (HR, SpO2, temperature, …)
    # on its own on a periodic schedule, and Vitals can turn that on/off
    # and set the interval — so the user configures the device from
    # Vitals rather than the vendor app. See configure_monitoring.
    SUPPORTS_MONITORING_CONFIG: ClassVar[bool] = False

    # Per-metric sensor quality, keyed by record-type. When several
    # devices report the same metric, Vitals resolves each time bucket to
    # the highest-quality source rather than summing/averaging across
    # them (a dedicated cuff or scale should outweigh a watch's estimate,
    # and two step counters must never be added together). Tiers are
    # "high" | "medium" | "low"; any metric not listed defaults to
    # "medium". See DeviceManager.source_trust and Store.aggregate.
    SENSOR_QUALITY: ClassVar[dict[str, str]] = {}

    # Firmware-update style. False (default): the watch is flashed over
    # its normal connection (Pebble's PRF onboarding). True: the watch
    # must first be put into a separate bootloader/DFU mode by the user
    # (Bangle.js), so the app uses the DFU-mode dialog instead.
    FIRMWARE_REQUIRES_DFU_MODE: ClassVar[bool] = False

    # How the app talks to this device family:
    #   "session"       — connect on demand / on a timer, run the sync
    #                     pipeline, disconnect (watches).
    #   "opportunistic" — never proactively connected; the ScanBroker
    #                     routes advertisements here whenever the device
    #                     wakes up to take a measurement (BP cuffs,
    #                     scales, thermometers).
    INTERACTION: ClassVar[str] = "session"

    # Non-None when this family's transport can only exist once per
    # process (the Pebble PPoGATT host-side GATT server). DeviceManager
    # holds a per-name lock so two such devices serialize.
    EXCLUSIVE_TRANSPORT: ClassVar[str | None] = None

    # ── onboarding (the pairing wizard) ───────────────────────────
    # Which category this device shows under in the "add a device"
    # wizard (see CATEGORY_INFO). The wizard groups plugins by this and
    # scans filtered to the chosen one.
    CATEGORY: ClassVar[str] = "sensor"
    # A symbolic icon shown on the guidance step.
    ICON_NAME: ClassVar[str] = "bluetooth-symbolic"
    # Ordered, human-facing steps for putting THIS device into a
    # pairable state — the guidance page renders them as a numbered list.
    PAIRING_STEPS: ClassVar[list[str]] = []

    address: str   # BLE MAC address of the paired watch
    name:    str   # Advertised BLE name at pairing time

    def __init__(self, address: str, name: str = ""):
        self.address = address
        self.name = name or self.display_name

    @classmethod
    @abc.abstractmethod
    def matches(cls, advertised_name: str | None,
                service_uuids: list[str]) -> bool:
        """Whether this plugin recognises a discovered device."""

    # Priority when several plugins all claim the same advertisement.
    # Higher wins. Only consulted when matches() is True. Cheap devices
    # advertise a jumble of shared transports (Nordic UART, standard
    # Heart Rate) alongside their real vendor service, so more than one
    # plugin can match — the strongest signal should win. Rough scale:
    #   1   generic fallback (the standard-GATT sensor catch-all)
    #   5   a shared/generic transport with nothing more specific
    #   10  a normal specific match (a family's own name or service)
    #   30  a unique vendor service that uniquely identifies the family
    MATCH_GENERIC_FALLBACK: ClassVar[int] = 1
    MATCH_SHARED_TRANSPORT: ClassVar[int] = 5
    MATCH_SPECIFIC: ClassVar[int] = 10
    MATCH_VENDOR_SERVICE: ClassVar[int] = 30

    @classmethod
    def match_specificity(cls, advertised_name: str | None,
                          service_uuids: list[str]) -> int:
        """How strongly this plugin claims the advertisement (see the
        MATCH_* scale). Default is a normal specific match; plugins that
        can match on a generic transport or are the catch-all override
        this so the most specific plugin wins in matching_device."""
        return cls.MATCH_SPECIFIC

    @abc.abstractmethod
    async def connect(self) -> None:
        """Open a BLE connection. Raise on failure."""

    @abc.abstractmethod
    async def disconnect(self) -> None:
        """Close the BLE connection. Idempotent."""

    @abc.abstractmethod
    async def get_battery(self) -> int | None:
        """Return battery percentage (0-100), or None if unknown."""

    # ── Optional feature methods ──────────────────────────────────

    async def sync_time(self, unix_timestamp: float) -> None:
        """Push the host clock to the watch.

        Default raises NotImplementedError. Subclasses that flip
        SUPPORTS_TIME_SYNC=True must override.
        """
        raise NotImplementedError(
            f"{self.display_name} does not support time sync")

    async def configure_monitoring(self, enabled: bool,
                                   interval_minutes: int) -> None:
        """Turn the device's automatic health monitoring on or off and
        set how often it samples.

        When ``enabled`` the device measures its sensors (whichever it
        has — heart rate, SpO2, temperature, …) every ``interval_minutes``
        and logs the readings to its own history, which a later sync
        drains. This is what lets Vitals own the device's configuration
        instead of relying on the vendor app.

        The application applies this on every connect (it's idempotent),
        so the device's cadence tracks the Vitals setting. Default raises
        NotImplementedError; subclasses that flip
        SUPPORTS_MONITORING_CONFIG=True must override.
        """
        raise NotImplementedError(
            f"{self.display_name} does not support monitoring configuration")

    async def push_alarms(self, alarms,
                          previously_pushed_ids=frozenset()) -> set[str]:
        """Reconcile the watch's alarm set with `alarms`.

        The plugin should:
          - keep alarms set on the watch directly (not in Vitals),
          - drop alarms whose id is in `previously_pushed_ids` (i.e.
            previously synced from Vitals and now removed),
          - drop alarms whose id matches any current `alarms` entry
            (we're about to re-add them with fresh fields),
          - add the current `alarms`.

        Returns the set of alarm ids now managed by Vitals — the
        application stores this in the device registry and passes it
        back on the next push.

        Default raises NotImplementedError. Subclasses that flip
        SUPPORTS_ALARM_PUSH=True must override.
        """
        raise NotImplementedError(
            f"{self.display_name} does not support alarm push")

    async def flash_firmware(self, firmware: bytes,
                             on_progress=None) -> None:
        """Flash a firmware bundle (`.pbz` bytes) onto the watch.

        Used to onboard a watch that ships in a recovery/PRF state with
        no normal firmware. `on_progress(stage, sent, total)` is called
        as bytes transfer. The watch reboots when done.

        Default raises NotImplementedError. Subclasses that flip
        SUPPORTS_FIRMWARE_UPDATE=True must override.
        """
        raise NotImplementedError(
            f"{self.display_name} does not support firmware updates")

    async def fetch_default_firmware(self, **opts) -> bytes:
        """Download the firmware bundle this watch should be onboarded
        with, returning `.pbz` bytes for `flash_firmware`.

        Default raises NotImplementedError. Subclasses that flip
        SUPPORTS_FIRMWARE_UPDATE=True override this to source firmware
        for their family (accepting family-specific options).
        """
        raise NotImplementedError(
            f"{self.display_name} cannot source firmware automatically")

    async def is_in_recovery(self) -> bool | None:
        """Whether the watch is currently running recovery firmware and
        needs a normal firmware installed (e.g. a factory-fresh Pebble in
        PRF, showing the setup QR code).

        Returns None when unknown / not applicable. Must be called while
        connected. Subclasses that support firmware updates override this
        so the app can offer to onboard a recovery watch automatically.
        """
        return None

    @classmethod
    def app_store(cls):
        """The app/watchface store for this watch family, or None.

        Returns an `AppStore` (see `vitals.devices.store`) the UI uses to
        list + download apps. Browsing needs no watch connection, so this
        is a classmethod. Subclasses that flip SUPPORTS_APP_INSTALL=True
        override it."""
        return None

    async def install_app(self, bundle: bytes, on_progress=None) -> None:
        """Install an app/watchface bundle (the bytes `app_store()` hands
        back — a `.pbw`, etc.) onto the connected watch.

        `on_progress(stage, sent, total)` reports transfer progress.
        Default raises NotImplementedError; subclasses that flip
        SUPPORTS_APP_INSTALL=True override it."""
        raise NotImplementedError(
            f"{self.display_name} does not support installing apps")

    async def push_notification(self, note: WatchNotification) -> None:
        """Show one forwarded desktop notification on the watch.

        Called over a persistent link (see ``ConnectionKeeper``) as
        banners arrive on the phone, not during the sync pipeline.
        Default raises NotImplementedError; subclasses that flip
        SUPPORTS_NOTIFICATIONS=True must override."""
        raise NotImplementedError(
            f"{self.display_name} does not support notifications")

    async def push_weather(self, forecast) -> None:
        """Store a ``vitals.devices.weather.Forecast`` on the watch, in
        whatever wire format this family uses. Default raises
        NotImplementedError; subclasses that flip SUPPORTS_WEATHER_PUSH=True
        override it."""
        raise NotImplementedError(
            f"{self.display_name} does not support weather")

    async def sync(self) -> None:
        """Pull whatever the watch has accumulated since the last
        sync. Default no-op; subclasses override for real syncs."""
        return None

    async def get_activity(self) -> ActivityReading | None:
        """Read current step/HR/sensor values from the watch.

        Default returns None. Subclasses that flip
        SUPPORTS_ACTIVITY_READ=True must override.
        """
        return None

    async def get_activity_series(self) -> list[ActivityReading] | None:
        """Read activity as a series of *interval deltas* accumulated
        since the last read (each with `interval_seconds` set), for
        sources that stream per-interval health records.

        Returns None for sources that report a single cumulative snapshot
        (the app then uses `get_activity`); returns a list (possibly
        empty) for streaming sources. The app stores and exports the
        whole series — deltas sum to a day total — rather than the one
        snapshot.
        """
        return None

    async def get_sleep_series(self) -> list[SleepSession] | None:
        """Read sleep/nap sessions accumulated since the last read.

        Returns a list (possibly empty) of `SleepSession`s for watches
        whose firmware tracks sleep; None for those that don't. Default
        None. Subclasses that flip SUPPORTS_SLEEP_READ=True must override.
        """
        return None

    async def get_workout_series(self) -> list[WorkoutSession] | None:
        """Read detected workout sessions accumulated since the last read.

        Returns a list (possibly empty) of `WorkoutSession`s for watches
        that detect workouts; None for those that don't. Default None.
        Subclasses that flip SUPPORTS_WORKOUT_READ=True must override.
        """
        return None

    async def get_hydration_series(self) -> list[HydrationReading] | None:
        """Read the drink log a smart water bottle has accumulated since
        the last sync.

        Returns a list (possibly empty) of `HydrationReading`s for water
        bottles; None for devices that don't track hydration. Default
        None. Subclasses that flip SUPPORTS_HYDRATION_READ=True must
        override.
        """
        return None

    async def get_heart_rate_samples(self) -> list[ActivityReading]:
        """Read fine-grained per-sample heart-rate readings accumulated
        since the last read, for sources that log them (e.g. a Pebble's
        protobuf HR log). Each is a point reading (`interval_seconds`
        unset). Default []; gated by SUPPORTS_ACTIVITY_READ at the call
        site, so overriding is optional even for activity sources.
        """
        return []

    # ── Opportunistic (sensor) hooks ──────────────────────────────

    @classmethod
    def match_advertisement(cls, device, advertisement) -> bool:
        """Whether a live BLE advertisement is this plugin's device
        waking up to deliver a measurement. Only consulted for plugins
        with ``INTERACTION = "opportunistic"``. Default False."""
        return False

    async def handle_advertisement(self, device, advertisement) -> list[dict]:
        """React to one advertisement from this (registered) sensor —
        decode it directly, or briefly connect and read — and return
        ready record envelopes for the Recorder. Runs on the BLE loop.
        Default: nothing."""
        return []


_REGISTRY: dict[str, type[Device]] = {}
_BUILTINS_LOADED = False
_EXTERNAL_LOADED = False
# Group name third-party plugins use in their pyproject.toml.
DEVICES_ENTRY_POINT_GROUP = "vitals.devices"


def register_device(cls: type[Device]) -> type[Device]:
    """Class decorator that registers a Device subclass."""
    if not getattr(cls, "id", None):
        raise ValueError(
            f"{cls.__name__} missing class-level `id` attribute")
    if cls.id in _REGISTRY:
        raise ValueError(
            f"device id {cls.id!r} already registered "
            f"(by {_REGISTRY[cls.id].__name__})")
    _REGISTRY[cls.id] = cls
    return cls


def _load_builtins() -> None:
    global _BUILTINS_LOADED
    if _BUILTINS_LOADED:
        return
    # Import side effects register built-in plugins.
    from vitals.devices import (
        and_oximeter,  # noqa: F401
        bangle,  # noqa: F401
        omron_bp,  # noqa: F401
        pinetime,  # noqa: F401
        waterh_bottle,  # noqa: F401
        yucheng_ring,  # noqa: F401
    )
    from vitals.devices.pebble import pebble  # noqa: F401
    from vitals.devices.sensors import plugin  # noqa: F401
    _BUILTINS_LOADED = True


def _load_external() -> None:
    """Import any third-party packages exposing a vitals.devices
    entry point. Each import is independently wrapped so a broken
    plugin can't take down the whole pair / sync flow — failures
    are logged and the rest of discovery continues."""
    global _EXTERNAL_LOADED
    if _EXTERNAL_LOADED:
        return
    _EXTERNAL_LOADED = True

    try:
        from importlib.metadata import entry_points
    except ImportError:
        return

    try:
        eps = entry_points(group=DEVICES_ENTRY_POINT_GROUP)
    except Exception:
        log.exception("Vitals: entry_points() lookup raised; skipping "
                      "external device discovery")
        return

    for ep in eps:
        try:
            ep.load()  # the load() side effect calls @register_device
            log.info("Vitals: loaded external device plugin %r from %r",
                     ep.name, ep.value)
        except Exception:
            log.exception("Vitals: failed to load external device "
                          "plugin %r (%r)", ep.name, ep.value)


def reset_external_loader_for_tests() -> None:
    """Test helper: reset the external-loader idempotency flag so a
    fresh entry-points fixture can be observed. Not part of the
    public API."""
    global _EXTERNAL_LOADED
    _EXTERNAL_LOADED = False


def available_devices() -> dict[str, type[Device]]:
    """Currently-registered device plugins, keyed by id."""
    _load_builtins()
    _load_external()
    return dict(_REGISTRY)


def matching_device(advertised_name: str | None,
                    service_uuids: list[str]) -> type[Device] | None:
    """Return the plugin that most specifically claims this BLE
    advertisement, or None if none match.

    A device can advertise several shared transports at once (a Yucheng
    ring exposes its vendor service *and* Nordic UART *and* standard
    Heart Rate), so multiple plugins may match. We pick the highest
    ``match_specificity`` — the strongest identity signal — breaking ties
    by registration order.
    """
    _load_builtins()
    _load_external()
    best: type[Device] | None = None
    best_score = 0
    for cls in _REGISTRY.values():
        try:
            if cls.matches(advertised_name, service_uuids):
                score = cls.match_specificity(advertised_name, service_uuids)
                if score > best_score:
                    best, best_score = cls, score
        except Exception:
            continue
    return best


# Wizard category metadata: id -> (label, symbolic icon, short description).
CATEGORY_INFO: dict[str, tuple[str, str, str]] = {
    "watch": ("Smartwatch", "phone-symbolic",
              "Pebble, Bangle.js, PineTime"),
    "blood_pressure": ("Blood-pressure monitor", "bluetooth-symbolic",
                       "OMRON and standard BP cuffs"),
    "scale": ("Scale", "bluetooth-symbolic", "Weight & body composition"),
    "oximeter": ("Pulse oximeter", "bluetooth-symbolic", "SpO2 & pulse"),
    "ring": ("Smart ring", "bluetooth-symbolic", "Yucheng / TK-series"),
    "bottle": ("Smart water bottle", "bluetooth-symbolic",
               "Hydration tracking"),
    "sensor": ("Other health sensor", "bluetooth-symbolic",
               "Standard Bluetooth health devices"),
}


def plugins_by_category() -> dict[str, list[type[Device]]]:
    """Registered plugins grouped by CATEGORY, in CATEGORY_INFO order."""
    _load_builtins()
    _load_external()
    groups: dict[str, list[type[Device]]] = {}
    for cls in _REGISTRY.values():
        groups.setdefault(cls.CATEGORY, []).append(cls)
    ordered = {k: groups[k] for k in CATEGORY_INFO if k in groups}
    for key, value in groups.items():
        ordered.setdefault(key, value)
    return ordered
