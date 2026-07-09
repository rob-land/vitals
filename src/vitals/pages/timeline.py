"""Timeline page — a day-addressed ledger of everything logged.

Shows one day at a time (default today), navigable by a date pager and a
calendar jump so any past day is reachable. Lists that day's discrete
events (meals, water, readings, workouts, sleep), newest first, with the
day's step total in the header; the high-frequency sensed streams
(per-minute steps/heart-rate/energy) stay on the Dashboard as aggregates.
"""

from __future__ import annotations

import json
import logging
from datetime import date, datetime, timedelta

from gi.repository import Adw, Gtk

from vitals.core.catalog import Catalog
from vitals.core.store import Store
from vitals.format import format_measurement, format_value, humanize_key
from vitals.pages import Page, local_tz_name, to_ms

log = logging.getLogger(__name__)

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
        self._day = datetime.now().astimezone().date()

        # Date pager: ◀  [date → calendar]  ▶   Today
        bar = Gtk.Box(spacing=6, margin_top=8, margin_bottom=4,
                      margin_start=12, margin_end=12)
        self._prev = Gtk.Button(icon_name="go-previous-symbolic",
                                css_classes=["flat"], tooltip_text="Previous day")
        self._prev.connect("clicked", lambda _b: self._shift(-1))
        self._date_btn = Gtk.MenuButton(hexpand=True, css_classes=["flat"])
        self._date_label = Gtk.Label(css_classes=["title-4"])
        self._date_btn.set_child(self._date_label)
        self._cal_pop = Gtk.Popover()
        self._calendar = Gtk.Calendar()
        self._calendar.connect("day-selected", self._on_calendar_pick)
        self._cal_pop.set_child(self._calendar)
        self._date_btn.set_popover(self._cal_pop)
        self._next = Gtk.Button(icon_name="go-next-symbolic",
                                css_classes=["flat"], tooltip_text="Next day")
        self._next.connect("clicked", lambda _b: self._shift(1))
        self._today_btn = Gtk.Button(label="Today", css_classes=["flat"])
        self._today_btn.connect("clicked", lambda _b: self._go_today())
        for w in (self._prev, self._date_btn, self._next, self._today_btn):
            bar.append(w)
        self.append(bar)

        self._scroller = Gtk.ScrolledWindow(
            hscrollbar_policy=Gtk.PolicyType.NEVER, vexpand=True)
        self.append(self._scroller)

    # ── day navigation ────────────────────────────────────────────
    def _shift(self, days: int) -> None:
        self._day = self._day + timedelta(days=days)
        self.refresh()

    def _go_today(self) -> None:
        self._day = datetime.now().astimezone().date()
        self.refresh()

    def _on_calendar_pick(self, calendar) -> None:
        if not self._cal_pop.get_visible():
            return
        gd = calendar.get_date()
        picked = date(gd.get_year(), gd.get_month(), gd.get_day_of_month())
        if picked <= datetime.now().astimezone().date():
            self._day = picked
            self.refresh()
        self._cal_pop.popdown()

    def refresh(self) -> None:
        today = datetime.now().astimezone().date()
        self._date_label.set_label(_day_title(self._day))
        self._next.set_sensitive(self._day < today)
        self._today_btn.set_visible(self._day != today)

        start_dt = datetime(self._day.year, self._day.month,
                            self._day.day).astimezone()
        start, end = to_ms(start_dt), to_ms(start_dt + timedelta(days=1))
        rows, _ = self._store.read_records(_EVENT_TYPES, start_ms=start,
                                           end_ms=end, limit=10000)
        if not rows:
            self._scroller.set_child(Adw.StatusPage(
                icon_name="document-open-recent-symbolic",
                title="Nothing logged",
                description=("Log food, water or a measurement to see it here."
                             if self._day == today
                             else "No entries on this day.")))
            return

        steps = {b["start"][:10]: b["value"]
                 for b in self._store.aggregate("step_count", "sum", "day",
                                                start_ms=start, end_ms=end,
                                                tz=local_tz_name())}
        clamp = Adw.Clamp(maximum_size=560, margin_top=4, margin_bottom=18,
                          margin_start=12, margin_end=12)
        group = Adw.PreferencesGroup()
        day_steps = steps.get(self._day.isoformat())
        if day_steps:
            group.set_description(f"{format_value(day_steps)} steps")
        for row in reversed(rows):  # newest first within the day
            when = datetime.fromtimestamp(
                row["effective_start"] / 1000).astimezone()
            group.add(self._make_row(row, when))
        clamp.set_child(group)
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
        # Tapping a scalar reading opens its history view.
        td = self._catalog.get(row["type"])
        if td and td.value_shape == "scalar":
            action.set_activatable(True)
            action.add_suffix(Gtk.Image.new_from_icon_name("go-next-symbolic"))
            action.connect("activated",
                           lambda _r, k=row["type"]: self._open_metric(k))
        return action

    def _open_metric(self, type_key: str) -> None:
        root = self.get_root()
        if root is not None:
            root.push_metric_detail(type_key)

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
