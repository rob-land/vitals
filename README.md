# Vitals

**The health dashboard for Pulse.**

Vitals is a native GTK4 / libadwaita app that shows the health and fitness
data in your local [Pulse](../pulse) hub — and it is where you control
which apps may read or write each kind of data. It's a *view* on the Pulse
store, talking to the `land.rob.pulse` daemon over D-Bus. No account, no
cloud; your data stays on your device.

It's adaptive: the same binary works on a desktop and on a Phosh / Plasma
Mobile phone.

## Status

**v0.1.** Three views over Pulse:

- **Today** — a steps-toward-goal activity ring and your latest vital signs.
- **Trends** — a daily chart of any scalar metric over time.
- **Permissions** — grant or revoke each app's access, per data type. This
  is the consent UI: authorising a source like Tock is a dialog, not a
  `gdbus` call.

## Build & run

```sh
meson setup _build --prefix=/usr/local
meson install -C _build

vitals        # launches the dashboard (needs the pulse daemon running)
```

Vitals must be granted **read** access in its own Permissions page before
Today/Trends show data (Pulse filters reads by grant). The Permissions page
itself always works — it manages grants through the Pulse `Admin` interface.

## Layout

```
src/vitals/
  application.py    Adw.Application
  window.py         adaptive shell (ViewStack + ViewSwitcher + breakpoint)
  pulse_client.py   GDBus client for land.rob.pulse (Health + Admin)
  format.py         pure value-formatting + chart scaling
  pages/            today.py · trends.py · permissions.py
  widgets/charts.py ActivityRing + BarChart (Cairo)
data/ui/            *.blp Blueprint templates → GResource
```

## License

GPL-3.0-or-later. See [`COPYING`](COPYING).
