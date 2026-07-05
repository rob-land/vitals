# Vitals

**One local-first health tracker.**

Vitals is a native GTK4 / libadwaita app that syncs smartwatches
(Pebble, Bangle.js, PineTime) and standard Bluetooth health sensors
(blood-pressure cuffs, scales, glucose meters, pulse oximeters,
thermometers), and logs food, water and measurements by hand — all
into one on-device SQLite database. No account, no cloud; optionally
replicate the change feed to your own [Vault](../vault) server.

It is the unification of five earlier apps — tock (watch sync), pulse
(health-data hub daemon), larder (food/water), jot (manual vitals) and
gauge (sensor bridge) — into a single codebase with a pluggable device
layer. On first run it adopts the existing Pulse database, history and
all.

It's adaptive: the same binary works on a desktop and on a Phosh /
Plasma Mobile phone.

## Views

- **Dashboard** — steps ring and week, calories eaten vs burned, water,
  weight trend, heart rate, last sleep.
- **Timeline** — everything you logged, newest first, grouped by day
  (with each day's step total in the header).
- **Devices** — pair watches and sensors; per-device tools appear by
  capability: sync, firmware update, app/watchface store, alarms,
  weather push.

Manual entry lives behind the header's **+**: food (with Open Food
Facts and USDA FoodData Central lookup), water quick-add, and
measurements (weight, blood pressure, glucose, and more).

## Build & run

```sh
meson setup _build --prefix=/usr/local
meson install -C _build

vitals               # the app
vitals --background  # headless (autostart uses this)
```

Tests need no hardware:

```sh
python3 -m pytest tests/
```

The Pebble transport hosts a BLE GATT server, which requires
`Experimental = true` in `/etc/bluetooth/main.conf` on the host.

## Layout

```
src/vitals/
  core/       catalog, store, units, replication, adoption (ex-pulse)
  ingest/     Recorder + envelope builders (the single write path)
  ble/        BLE worker loop, adapter monitor, shared scanner
  devices/    Device ABC + DeviceManager; pebble/, bangle, pinetime,
              sensors/ plugins
  sources/    manual-entry dialogs (food / water / measurements)
  pages/      dashboard, timeline, devices, device detail
  widgets/    Cairo charts
data/schema/  record-types.yaml + SQL migrations
docs/         adding-a-device-plugin.md and friends
```

## License

GPL-3.0-or-later. See [`COPYING`](COPYING).
