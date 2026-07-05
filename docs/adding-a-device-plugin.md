# Adding a device plugin

Vitals talks to each watch family through a small **device plugin**. A
plugin's whole job is: *given a Bluetooth address, talk to that watch* —
recognise it in a scan, connect, and implement whichever features it
supports (battery, time sync, alarms, activity). This guide walks
through writing one.

If you haven't yet, skim [`devices.md`](devices.md) for the bigger
picture (the two BLE backends, the threading model, the support matrix).
The shipped plugins are the best worked examples:

- `src/vitals/devices/pinetime.py` — the simplest case: a standard LE
  peripheral with Bluetooth SIG characteristics.
- `src/vitals/devices/bangle.py` — a text REPL (Espruino over Nordic UART).
- `src/vitals/devices/pebble.py` + `ppogatt.py` — the advanced case: a
  custom transport on a different BLE backend.

## The contract

A plugin subclasses `Device` (`src/vitals/devices/base.py`). You must
provide an identity, a `matches()` discovery check, the connect
lifecycle, and `get_battery()`. Everything else is optional and gated by
a capability flag.

```python
from vitals.devices.base import Device, register_device


@register_device
class AcmeWatch(Device):
    id = "acme"                       # stable, lower-snake-case; stored in GSettings
    display_name = "Acme Watch"       # shown in the UI
    description = "Acme Series 1 smartwatch"

    SUPPORTS_TIME_SYNC = True         # advertise only what you implement

    @classmethod
    def matches(cls, advertised_name, service_uuids):
        # Claim a device from a scan result. Be specific — a too-broad
        # match steals devices from other plugins.
        if advertised_name and advertised_name.lower().startswith("acme"):
            return True
        return ACME_SERVICE_UUID in [u.lower() for u in service_uuids]

    def __init__(self, address, name=""):
        super().__init__(address, name)
        self._client = None

    async def connect(self):
        from bleak import BleakClient        # lazy import — see "Conventions"
        self._client = BleakClient(self.address)
        await self._client.connect()

    async def disconnect(self):
        if self._client is None:
            return
        try:
            await self._client.disconnect()
        finally:
            self._client = None

    async def get_battery(self):
        if self._client is None:
            return None
        try:
            data = await self._client.read_gatt_char(BATTERY_LEVEL_UUID)
        except Exception:
            return None
        level = int(data[0])
        return level if 0 <= level <= 100 else None

    async def sync_time(self, unix_timestamp):
        # Only reached because SUPPORTS_TIME_SYNC is True.
        await self._client.write_gatt_char(
            CURRENT_TIME_UUID, _encode_current_time(unix_timestamp))
```

That's a complete, working plugin.

## What the methods are for

**Identity** — `id` is the stable key persisted in settings (don't ever
change it once shipped); `display_name` and `description` are
user-facing.

**`matches(advertised_name, service_uuids)`** (classmethod) — return True
if this plugin recognises a scanned device. Match on the advertised name
prefix and/or a service UUID that's distinctive to the firmware. Keep it
tight: the first plugin whose `matches()` returns True wins.

**`connect()` / `disconnect()`** — open and close the link. `connect()`
raises on failure; `disconnect()` is idempotent (safe to call when not
connected).

**`get_battery()`** (required) — return 0–100, or `None` if unknown.
Returning `None` on a failed read is preferred over raising, so a
flaky battery read doesn't sink the whole sync.

**Optional feature methods** — each is a no-op/raises by default; you
override it *and* flip its capability flag:

| Method | Flag | Notes |
|---|---|---|
| `sync_time(unix_timestamp)` | `SUPPORTS_TIME_SYNC` | push the host clock to the watch |
| `push_alarms(alarms, previously_pushed_ids)` | `SUPPORTS_ALARM_PUSH` | reconcile the watch's alarms; return the set of alarm ids now managed by Vitals |
| `get_activity()` | `SUPPORTS_ACTIVITY_READ` | return an `ActivityReading` (steps / heart rate), or `None` |
| `sync()` | — | pull anything accumulated since last sync; default no-op |

`ActivityReading` is a small frozen dataclass — set whichever of `steps`,
`heart_rate_bpm`, `heart_rate_confidence`, `timestamp` you can read; the
UI shows whatever came back.

**Capability flags are a promise.** The app checks the flag before
calling the method, so a watch that can't do something surfaces a polite
toast instead of a crash. Don't flip a flag you haven't implemented.

## How the app drives a plugin

On a sync the app constructs your plugin from the saved address, then
roughly:

```
await connect()
  if sync_time enabled and SUPPORTS_TIME_SYNC:  await sync_time(now)
  if alarms     enabled and SUPPORTS_ALARM_PUSH: await push_alarms(...)
  battery = await get_battery()
  if SUPPORTS_ACTIVITY_READ:                     await get_activity()
  await sync()
await disconnect()
```

So a plugin is used as a short connect → do-work → disconnect cycle.

## Registering the plugin

**Built-in** (in this repo): drop the file in `src/vitals/devices/`, add it
to the eager import in `base.py`'s `_load_builtins()`, and list it in
`src/vitals/meson.build`.

**Third-party** (your own pip package, no fork needed): expose it through
the `vitals.devices` entry-point group. Vitals discovers and imports it at
startup; the import runs your `@register_device` and the plugin shows up
in the pair / sync flows.

```toml
# pyproject.toml of your package
[project.entry-points."vitals.devices"]
acme = "vitals_acme.device"      # value = an importable module that registers the plugin
```

A broken third-party plugin is caught and logged during discovery, so it
can't take down the rest.

## BLE backend & threading

Most watches are ordinary LE peripherals — use **bleak** (`BleakClient`),
as in the example. All plugin async methods run on a single background
asyncio loop (`BleManager`); **never touch GTK from a plugin** — return
plain data and let the app marshal it to the UI.

If your device needs something bleak can't do — forcing an LE connection
on a dual-mode device, or hosting a GATT *server* — you can talk to BlueZ
directly over `dbus_fast` (already a dependency). That's how the Pebble
plugin works; see [`pebble-ppogatt.md`](pebble-ppogatt.md). It also means
the host's bluetoothd needs `Experimental = true`. Reach for this only
when you must.

## Conventions & gotchas

- **Lazy-import bleak** inside `connect()` (`from bleak import BleakClient`)
  rather than at module top, so importing the plugin registry stays cheap
  and import-safe.
- **`get_battery()` returns `None` on failure**, it doesn't raise — keep
  one flaky read from failing the whole sync. Feature methods that the
  user explicitly asked for (e.g. `sync_time`) may raise; the app shows
  the error.
- **`disconnect()` is idempotent.**
- **Validate values** — clamp battery to 0–100, treat garbage as
  unknown.
- **Keep the wire format pure and tested.** Encoders/parsers (time
  payloads, packet framing) should be plain functions or static methods
  with unit tests — they're the part you can test without hardware. See
  [`TESTING.md`](../TESTING.md); if your tests import `gi`, pin the
  versions in `tests/conftest.py` (a `PyGIWarning` otherwise).

## Checklist

- [ ] Subclass `Device`, set `id` / `display_name` / `description`.
- [ ] Implement `matches()` — specific enough not to grab other watches.
- [ ] Implement `connect()` / `disconnect()` / `get_battery()`.
- [ ] Implement each feature you support, and flip its capability flag.
- [ ] Register it (built-in import + meson, or an entry point).
- [ ] Unit-test the pure wire-format helpers.

## Opportunistic sensors (BP cuffs, scales, meters)

Watches are *session* devices: the app connects on demand, runs the
sync pipeline, disconnects. Sensors are the opposite — they sleep until
a measurement is taken, advertise for a few seconds, and vanish. The
framework models this with three additions on `Device`:

- `INTERACTION = "opportunistic"` — the DeviceManager never schedules
  syncs; instead the ScanBroker routes live advertisements to the
  plugin while at least one of its devices is registered and enabled.
- `match_advertisement(cls, device, advertisement)` — classmethod gate:
  is this advertisement my device waking up with data?
- `async handle_advertisement(self, device, advertisement)` — decode
  the advertisement directly (Xiaomi-style service data) or briefly
  connect/subscribe (standard GATT indications), and return ready
  record envelopes; the manager hands them to the Recorder.

`devices/sensors/plugin.py` is the reference implementation: one
generic plugin covering every standards-compliant sensor via the
service table in `devices/sensors/gatt.py`.

### A&D Medical readiness

The A&D family (see tock's `docs/and-medical-ble.md` teardown) maps
onto these hooks directly:

- SIG-profile models (UA-651BLE and friends) already work through the
  generic `gatt-sensor` plugin — 0x1810 blood pressure and 0x181D
  weight are in the service table.
- The custom-protocol models (0x7809 / 0x7892 services, the UC-450's
  framed transport) become their own plugin: `matches()` /
  `match_advertisement()` key on the custom service UUIDs, and
  `handle_advertisement()` owns the pairing handshake, RACP drain and
  frame parsing before returning envelopes. `EXCLUSIVE_TRANSPORT` is
  not needed — each read is one short-lived BleakClient connection.
