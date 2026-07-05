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

        page.add(group)
        self.add(page)

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
