"""Vitals preferences — dashboard settings (step goal, trends window)."""

from __future__ import annotations

from gi.repository import Adw, Gio, Gtk


class VitalsPreferences(Adw.PreferencesDialog):
    def __init__(self, settings: Gio.Settings):
        super().__init__()
        self._settings = settings

        page = Adw.PreferencesPage()
        group = Adw.PreferencesGroup(title="Dashboard")

        self._goal = Adw.SpinRow(
            title="Daily step goal",
            subtitle="Target for the Today ring (0 hides the goal)",
            adjustment=Gtk.Adjustment(lower=0, upper=100000,
                                      step_increment=500, page_increment=1000))
        self._goal.set_value(settings.get_int("daily-step-goal"))
        self._goal.connect("notify::value", self._on_goal)

        self._days = Adw.SpinRow(
            title="Trends window",
            subtitle="Days shown on the Trends chart",
            adjustment=Gtk.Adjustment(lower=7, upper=90,
                                      step_increment=1, page_increment=7))
        self._days.set_value(settings.get_int("trends-days"))
        self._days.connect("notify::value", self._on_days)

        group.add(self._goal)
        group.add(self._days)
        page.add(group)
        self.add(page)

    def _on_goal(self, row, _param):
        self._settings.set_int("daily-step-goal", int(row.get_value()))

    def _on_days(self, row, _param):
        self._settings.set_int("trends-days", int(row.get_value()))
