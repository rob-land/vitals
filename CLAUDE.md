# Vitals — CLAUDE.md

## What this project is

Vitals is the unified GTK4 / libadwaita health tracker — the merge of
the former tock (watch sync), pulse (health-data hub), larder
(food/water logging), jot (manual vitals) and gauge (BLE sensor
bridge) apps into one. It owns the health database in-process (no
D-Bus hub, no consent layer), syncs watches (Pebble, Bangle.js,
PineTime) and standard-GATT health sensors through a pluggable device
layer, and provides manual entry for food, water and measurements.
Three views: Dashboard (charts), Timeline (day-grouped events),
Devices (registry + per-device tools).

App ID: `land.rob.vitals`. License: GPL-3.0-or-later.

## Code quality

Well-structured, readable, idiomatic Python (PEP 8) and GNOME / libadwaita
conventions; the cohort-shared [`STYLE_GUIDE.md`](STYLE_GUIDE.md) layers on
top. When existing code doesn't meet that bar, refactor rather than
perpetuate the pattern.

## Architecture (the short version)

- `core/` — the former pulse daemon as an in-process library: type
  catalog (`data/schema/record-types.yaml`), validation, SQLite store
  (WAL, uuid-upsert, seq change feed), UCUM units, Vault replication,
  CSV export, one-time pulse-DB adoption (`core/migrate.py`).
- `ingest/` — every write goes through the one `Recorder`
  (validate → insert → `RecordBus::records-changed`); `builders.py`
  keeps the legacy `tock:` uuid scheme (load-bearing: adopted records
  must upsert on re-sync, never duplicate).
- `devices/` — plugin layer. `Device` ABC with capability flags;
  `INTERACTION` separates session watches from opportunistic sensors;
  `EXCLUSIVE_TRANSPORT` serializes one-per-process transports (Pebble
  PPoGATT). Registry rows live in the `devices` table of health.db,
  orchestrated by `DeviceManager`. See
  [`docs/adding-a-device-plugin.md`](docs/adding-a-device-plugin.md).
- `ble/` — one asyncio loop on a worker thread (`BleManager`), one
  shared `BleakScanner` (`ScanBroker`). SQLite is main-thread-only;
  BLE code marshals via `GLib.idle_add` (the Recorder does this).
- `sources/` — manual-entry dialogs (food + OFF/USDA lookup, water,
  measurements). Food writes `nutrient_intake` **plus** a companion
  `dietary_energy` scalar so calorie aggregates work; the Timeline
  hides the companion.
- `pages/`, `widgets/` — the three views and the Cairo chart set
  (ring/bar/line + grouped bars; ACCENT blue = activity, ACCENT2
  orange #D97706 = intake, pair is CVD-validated).

## Tech stack

- **Python 3.10+**, GTK4 + libadwaita (PyGObject), Blueprint `.blp` → `.ui`
  bundled via GResource. Meson + Ninja; Flatpak (GNOME 50).
- **pip deps**: PyYAML (catalog), bleak (BLE); dbus_fast rides along for
  the Pebble transport. Bundled via `build-aux/flatpak/python3-deps.json`.
- **BlueZ**: bleak for Bangle/PineTime/sensors; dbus_fast GATT *server*
  for Pebble (needs bluetoothd `Experimental = true` on the host).

## Before making changes

- Every new `src/vitals/**/*.py` MUST be listed in
  `src/vitals/meson.build` (and `.blp` in `data/ui/meson.build`) or it
  silently never ships — `tests/test_packaging.py` enforces this.
- UI lives in `data/ui/*.blp` for templates; ported tock dialogs are
  imperative and may stay so until touched.
- `gi.require_version` is declared once in `src/vitals/vitals.in`;
  sub-modules just `from gi.repository import …`.
- Run `python3 -m pytest tests/` (no hardware needed) and
  `python3 ~/projects/style-check.py` before committing.

## Hardware verification

Protocol logic is unit-tested against captures, but BlueZ GATT-server
registration, Pebble sync/flash, Bangle DFU and live sensor readings
need the FLX1s phone + real devices. Verify on-device before retiring
the corresponding legacy app (tock / gauge / pulse / larder / jot).
