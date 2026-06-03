# Vitals — CLAUDE.md

## What this project is

Vitals is the GTK4 / libadwaita health dashboard for Pulse, the local-first
health-data hub. It is a *view* client: it reads activity, vitals and trends
from the `land.rob.pulse` daemon over D-Bus and renders them, and it owns
the **consent UI** for granting apps access to each data type (via Pulse's
`land.rob.pulse.Admin` interface). It is the companion to the headless
`pulse` daemon and to `tock` (which feeds Pulse as a source).

App ID: `land.rob.vitals`. License: GPL-3.0-or-later.

## Code quality

Well-structured, readable, idiomatic Python (PEP 8) and GNOME / libadwaita
conventions; the cohort-shared [`STYLE_GUIDE.md`](STYLE_GUIDE.md) layers on
top. This is a list-of-views (ViewStack) app, not list+detail, so it uses
an `Adw.ViewStack` + `Adw.ViewSwitcher` shell with a breakpoint, per the
style guide's adaptive-shell section.

## Tech stack

- **Python 3.10+**, GTK4 + libadwaita (PyGObject), Blueprint `.blp` → `.ui`
  bundled via GResource. Meson + Ninja; Flatpak (GNOME 50).
- **No pip dependencies** — `gi`/GTK/Adw come from the runtime; `build-all.sh`
  skips the python3-deps step and the manifest omits it.
- **D-Bus**: GDBus (`Gio.DBusConnection`), synchronous calls on the main
  loop — local Pulse round-trips are sub-millisecond, so a dashboard refresh
  calls them directly (no worker thread). All wrapped in `pulse_client.py`.

## Source layout

```
src/vitals/
  main.py / __main__.py    entry point (launcher loads the GResource first)
  application.py           VitalsApplication (Adw.Application)
  window.py                VitalsWindow — the adaptive shell, hosts 3 pages
  pulse_client.py          PulseClient: Health (read) + Admin (consent)
  format.py                pure formatting + nice_max axis scaling (tested)
  logging_setup.py         rotating-file logging (VITALS_DEBUG / --debug)
  pages/                   PulsePage base + today / trends / permissions
  widgets/charts.py        ActivityRing + BarChart (Cairo DrawingAreas)
data/ui/                   window.blp, help-overlay.blp, gresource.xml
data/                      .desktop / .metainfo / .gschema / icons
```

## Key conventions

- **Pages** subclass `PulsePage`: a Stack that swaps real content with a
  shared "Pulse isn't running" status page. Each page has a `refresh()` the
  window calls when that page becomes visible (lazy — only the visible page
  pulls data).
- **Consent**: Vitals manages grants through Pulse's `Admin` interface
  (owner-only). Vitals itself needs a read grant to display data; the
  Permissions page is how the user gives it (and authorises sources like
  Tock). A future Pulse portal will let apps *request* access and surface it
  here as a prompt.
- **Drawing**: charts are plain `Gtk.DrawingArea`s; the data prep
  (`nice_max`, formatting) lives in `format.py` and is unit-tested, the
  widgets only draw.
- **gettext** only in `.blp` (`_()` / `C_()`); Python uses plain strings,
  matching the cohort.

## Things to watch out for

- Window template ids in `window.blp` must match the `Gtk.Template.Child`
  names in `window.py` (`toast_overlay`, `view_stack`, `title_stack`,
  `view_switcher_bar`, `today_bin`, `trends_bin`, `permissions_bin`).
- The ViewStack's visible child is the `Adw.Bin`, not the page widget inside
  it — `window.refresh()` dispatches by page name, not by the visible child.
- ViewStackPage `icon-name`s are stock symbolic icons; a missing one
  degrades to blank, it doesn't crash.
