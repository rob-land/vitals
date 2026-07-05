"""Vitals preferences — goals, device sync, weather and services."""

from __future__ import annotations

import logging
import threading

from gi.repository import Adw, Gio, GLib, Gtk

log = logging.getLogger(__name__)


class VitalsPreferences(Adw.PreferencesDialog):
    def __init__(self, settings: Gio.Settings):
        super().__init__()
        self._settings = settings

        page = Adw.PreferencesPage()
        group = Adw.PreferencesGroup(title="Goals")

        group.add(self._spin_row(
            "Daily step goal", "Target for the steps ring (0 hides the goal)",
            "daily-step-goal", upper=100000, step=500, page=1000))
        group.add(self._spin_row(
            "Daily water goal", "Millilitres per day (0 hides the goal)",
            "water-goal-ml", upper=10000, step=100, page=250))

        sync = Adw.PreferencesGroup(title="Devices")
        sync.add(self._switch_row(
            "Sync time on connect",
            "Push the host clock to a watch on every sync",
            "sync-time-on-connect"))
        interval = self._spin_row(
            "Background sync interval",
            "Minutes between automatic watch syncs (0 disables)",
            "background-sync-interval", upper=1440, step=15, page=60)
        sync.add(interval)
        sync.add(self._switch_row(
            "Run in background",
            "Keep syncing when the window is closed",
            "run-in-background"))
        autostart = self._switch_row(
            "Start at login",
            "Launch in the background when you log in",
            "autostart-enabled")
        autostart.connect("notify::active", self._on_autostart)
        sync.add(autostart)

        services = Adw.PreferencesGroup(
            title="Services",
            description="USDA FoodData Central adds restaurant and generic "
                        "foods to the lookup. The shared demo key is heavily "
                        "rate-limited; a personal key is free.")
        key_row = Adw.PasswordEntryRow(title="USDA API key")
        key_row.set_text(settings.get_string("usda-api-key"))
        key_row.connect(
            "changed",
            lambda row: settings.set_string("usda-api-key", row.get_text().strip()))
        services.add(key_row)

        page.add(group)
        page.add(sync)
        page.add(self._weather_group())
        page.add(services)
        page.add(self._data_group())
        self.add(page)

    # ── Data group (Vault replication + export) ───────────────────
    def _data_group(self) -> Adw.PreferencesGroup:
        group = Adw.PreferencesGroup(
            title="Data",
            description="Your health data lives on this device; replication "
                        "pushes changes to your own Vault server.")

        url = Adw.EntryRow(title="Vault server URL")
        url.set_text(self._settings.get_string("vault-url"))
        url.connect("changed",
                    lambda row: self._settings.set_string(
                        "vault-url", row.get_text().strip()))
        group.add(url)

        replicate = Adw.ButtonRow(title="Replicate Now")
        replicate.set_start_icon_name("emblem-synchronizing-symbolic")
        replicate.connect("activated", self._on_replicate)
        group.add(replicate)

        export = Adw.ButtonRow(title="Export All Data as CSV…")
        export.set_start_icon_name("document-save-symbolic")
        export.connect("activated", self._on_export)
        group.add(export)
        return group

    def _on_replicate(self, *_):
        url = self._settings.get_string("vault-url")
        if not url:
            self.add_toast(Adw.Toast.new("Set the Vault server URL first"))
            return

        def work():
            # Worker thread: sqlite connections are thread-affine, so
            # replication opens its own (the store's stays on the main
            # thread).
            from vitals.core import replicate, resources
            from vitals.core.store import Store
            store = Store(str(resources.db_path()))
            try:
                before = replicate.read_cursor()
                cursor = replicate.replicate(
                    store, replicate._http_post(url), before)
                replicate.write_cursor(cursor)
                message = ("Replication up to date" if cursor == before
                           else f"Replicated to seq {cursor}")
            except Exception as exc:
                log.exception("replication failed")
                message = f"Replication failed: {exc}"
            finally:
                store.close()
            GLib.idle_add(lambda: self.add_toast(Adw.Toast.new(message)))

        threading.Thread(target=work, name="vitals-replicate",
                         daemon=True).start()

    def _on_export(self, *_):
        dialog = Gtk.FileDialog(initial_name="vitals-export.csv")
        dialog.save(self.get_root(), None, self._on_export_chosen)

    def _on_export_chosen(self, dialog, result) -> None:
        try:
            gfile = dialog.save_finish(result)
        except GLib.Error:
            return  # cancelled
        path = gfile.get_path()

        def work():
            from vitals.core import resources
            from vitals.core.csv_export import export_to_path
            try:
                count = export_to_path(str(resources.db_path()), path)
                message = f"Exported {count:,} records"
            except Exception as exc:
                log.exception("csv export failed")
                message = f"Export failed: {exc}"
            GLib.idle_add(lambda: self.add_toast(Adw.Toast.new(message)))

        threading.Thread(target=work, name="vitals-export",
                         daemon=True).start()

    # ── Weather group (Pebble forecast push) ──────────────────────
    def _weather_group(self) -> Adw.PreferencesGroup:
        group = Adw.PreferencesGroup(
            title="Weather",
            description="Send a forecast to the watch's Weather app, via "
                        "Open-Meteo (no account needed).")

        enable = Adw.SwitchRow(title="Weather sync",
                               subtitle="Update the forecast on every sync")
        self._settings.bind("weather-enabled", enable, "active",
                            Gio.SettingsBindFlags.DEFAULT)
        group.add(enable)

        self._weather_loc = Adw.EntryRow(title="Location")
        self._weather_loc.set_text(
            self._settings.get_string("weather-location-name"))
        self._weather_loc.set_show_apply_button(True)
        self._weather_loc.connect("apply", self._on_weather_location_apply)
        group.add(self._weather_loc)

        units = Adw.ComboRow(title="Temperature units",
                             subtitle="Match what your watch shows")
        units.set_model(Gtk.StringList.new(["Celsius (°C)", "Fahrenheit (°F)"]))
        units.set_selected(
            1 if self._settings.get_string("weather-units") == "fahrenheit"
            else 0)
        units.connect(
            "notify::selected",
            lambda r, _: self._settings.set_string(
                "weather-units",
                "fahrenheit" if r.get_selected() == 1 else "celsius"))
        group.add(units)
        return group

    def _on_weather_location_apply(self, row: Adw.EntryRow) -> None:
        query = row.get_text().strip()
        if not query:
            return

        def work():
            from vitals.devices import weather as weather_mod
            try:
                results = weather_mod.geocode(query)
            except Exception:
                log.exception("weather: geocode failed")
                results = []
            GLib.idle_add(self._weather_located,
                          results[0] if results else None)

        threading.Thread(target=work, name="vitals-geocode",
                         daemon=True).start()

    def _weather_located(self, result) -> bool:
        if result is None:
            self.add_toast(Adw.Toast.new("Couldn’t find that location"))
            return False
        self._settings.set_string("weather-location-name", result.name)
        self._settings.set_double("weather-latitude", result.latitude)
        self._settings.set_double("weather-longitude", result.longitude)
        self._weather_loc.set_text(result.name)
        self.add_toast(Adw.Toast.new(f"Weather location: {result.name}"))
        return False

    def _switch_row(self, title: str, subtitle: str, key: str) -> Adw.SwitchRow:
        row = Adw.SwitchRow(title=title, subtitle=subtitle)
        row.set_active(self._settings.get_boolean(key))
        row.connect("notify::active",
                    lambda r, _p: self._settings.set_boolean(key, r.get_active()))
        return row

    def _on_autostart(self, row, _param) -> None:
        from vitals.background_portal import request_background
        from vitals.const import APP_ID, APP_NAME
        request_background(autostart=row.get_active(),
                           app_id=APP_ID, app_name=APP_NAME)

    def _spin_row(self, title: str, subtitle: str, key: str,
                  upper: int, step: int, page: int) -> Adw.SpinRow:
        row = Adw.SpinRow(
            title=title, subtitle=subtitle,
            adjustment=Gtk.Adjustment(lower=0, upper=upper,
                                      step_increment=step, page_increment=page))
        row.set_value(self._settings.get_int(key))
        row.connect("notify::value",
                    lambda r, _p: self._settings.set_int(key, int(r.get_value())))
        return row
