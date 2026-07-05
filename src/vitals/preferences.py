"""Vitals preferences — daily goals for the dashboard rings."""

from __future__ import annotations

from gi.repository import Adw, Gio, Gtk


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
        page.add(services)
        self.add(page)

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
