"""Timeline page — everything entered, newest first, grouped by day.

Shows discrete events (meals, water, readings, workouts, sleep); the
high-frequency sensed streams (per-minute steps/heart-rate/energy) stay
on the Dashboard as aggregates, with each day's step total surfaced in
that day's header instead.
"""

from __future__ import annotations

import json
import logging
from datetime import date, datetime, timedelta

from gi.repository import Adw, Gtk

from vitals.core.catalog import Catalog
from vitals.core.store import Store
from vitals.format import format_measurement, format_value, humanize_key
from vitals.pages import Page, local_day_start, local_tz_name, to_ms

log = logging.getLogger(__name__)

_WINDOW_DAYS = 30

# Event-y types listed on the timeline, i.e. everything except the
# per-minute sensed streams. dietary_energy is also absent: the food
# form writes it alongside every nutrient_intake purely so the calorie
# aggregates work, and listing both would duplicate each meal.
_EVENT_TYPES = [
    "workout", "sleep_episode",
    "nutrient_intake", "water_intake", "caffeine_intake",
    "alcohol_intake",
    "body_weight", "body_fat_percentage", "body_mass_index",
    "blood_pressure", "blood_glucose", "oxygen_saturation",
    "body_temperature", "resting_heart_rate", "heart_rate_variability",
    "respiratory_rate", "mindful_minutes", "mood",
]


class Timeline(Page):
    def __init__(self, store: Store, catalog: Catalog):
        super().__init__()
        self._store = store
        self._catalog = catalog

        self._scroller = Gtk.ScrolledWindow(
            hscrollbar_policy=Gtk.PolicyType.NEVER, vexpand=True)
        self.append(self._scroller)

        self._empty = Adw.StatusPage(
            icon_name="document-open-recent-symbolic",
            title="Nothing logged yet",
            description="Entries from your devices and manual logs will "
                        "appear here.")

    def refresh(self) -> None:
        start = to_ms(local_day_start(_WINDOW_DAYS - 1))
        rows, _ = self._store.read_records(_EVENT_TYPES, start_ms=start,
                                           limit=10000)
        if not rows:
            self._scroller.set_child(self._empty)
            return

        steps_by_day = {
            b["start"][:10]: b["value"]
            for b in self._store.aggregate("step_count", "sum", "day",
                                           start_ms=start, tz=local_tz_name())}

        clamp = Adw.Clamp(maximum_size=560, margin_top=18, margin_bottom=18,
                          margin_start=12, margin_end=12)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        clamp.set_child(box)

        current_day: date | None = None
        group: Adw.PreferencesGroup | None = None
        for row in reversed(rows):  # newest first
            when = datetime.fromtimestamp(
                row["effective_start"] / 1000).astimezone()
            if when.date() != current_day:
                current_day = when.date()
                group = Adw.PreferencesGroup(title=_day_title(current_day))
                steps = steps_by_day.get(current_day.isoformat())
                if steps:
                    group.set_description(f"{format_value(steps)} steps")
                box.append(group)
            group.add(self._make_row(row, when))

        self._scroller.set_child(clamp)

    # ── row rendering ─────────────────────────────────────────────
    def _make_row(self, row, when: datetime) -> Adw.ActionRow:
        title, value, subtitle = self._describe(row)
        action = Adw.ActionRow(title=title, subtitle=subtitle or "")
        clock = Gtk.Label(label=when.strftime("%H:%M"))
        clock.add_css_class("dim-label")
        clock.add_css_class("numeric")
        action.add_prefix(clock)
        if value:
            suffix = Gtk.Label(label=value)
            suffix.add_css_class("title-4")
            action.add_suffix(suffix)
        return action

    def _describe(self, row) -> tuple[str, str, str]:
        """(title, value text, subtitle) for one stored record."""
        kind = row["type"]
        td = self._catalog.get(kind)
        title = td.title if td else humanize_key(kind)
        device = row["display_name"] or ""
        body = json.loads(row["value_json"]) if row["value_json"] else None

        if kind == "nutrient_intake":
            label = (body or {}).get("label") or "Meal"
            meal = ((json.loads(row["meta_json"]) if row["meta_json"] else {})
                    .get("meal") or "").capitalize()
            kcal = ((body or {}).get("nutrients", {})
                    .get("energy-kcal", {}).get("value"))
            value = f"{format_value(kcal, 'kcal')} kcal" if kcal else ""
            return label, value, " · ".join(p for p in (meal, device) if p)

        if kind == "water_intake":
            return ("Water",
                    format_measurement(row["value_num"], row["unit"]), device)

        if kind == "sleep_episode":
            total = (body or {}).get("total_sleep_minutes") or 0
            hours, mins = divmod(int(total), 60)
            return title, f"{hours} h {mins:02d} m", device

        if kind == "workout":
            name = ((body or {}).get("activity_name") or "Workout").capitalize()
            secs = (body or {}).get("duration_seconds") or 0
            value = f"{round(secs / 60)} min"
            extras = []
            if (body or {}).get("distance_meters"):
                extras.append(f"{body['distance_meters'] / 1000:.1f} km")
            if (body or {}).get("active_energy_kcal"):
                extras.append(f"{format_value(body['active_energy_kcal'], 'kcal')} kcal")
            extras.append(device)
            return name, value, " · ".join(p for p in extras if p)

        if kind == "blood_pressure":
            sys_v = (body or {}).get("systolic")
            dia_v = (body or {}).get("diastolic")
            return title, f"{format_value(sys_v)}/{format_value(dia_v)} mmHg", device

        if body is not None:  # any other structured body
            return title, "", device

        return title, format_measurement(row["value_num"], row["unit"]), device


def _day_title(day: date) -> str:
    today = datetime.now().astimezone().date()
    if day == today:
        return "Today"
    if day == today - timedelta(days=1):
        return "Yesterday"
    return day.strftime("%A %-d %B")
