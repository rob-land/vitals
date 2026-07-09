"""Dashboard page — the at-a-glance cards: steps, calories in vs out,
water, weight trend, heart rate and last night's sleep.

Every card reads the store through ``Store.aggregate`` (tz-aware daily /
hourly buckets); the window triggers ``refresh()`` when records change.
"""

from __future__ import annotations

import json
import logging
from datetime import date, datetime, timedelta

from gi.repository import Adw, Gdk, Gio, Gtk

from vitals.core.store import Store
from vitals.format import format_value
from vitals.pages import Page, local_day_start, local_tz_name, to_ms
from vitals.trends import (
    RECAP_METRICS, format_trend, recap_rows, staleness, weekly_trend)
from vitals.widgets.charts import (
    ACCENT, ACCENT2, ActivityRing, BarChart, GroupedBarChart, LegendDot,
    LineChart)

log = logging.getLogger(__name__)

_DAYS = 7  # bar-chart window, today inclusive


class Dashboard(Page):
    def __init__(self, store: Store, settings: Gio.Settings,
                 device_manager=None, catalog=None, recorder=None):
        super().__init__()
        self._store = store
        self._settings = settings
        self._recorder = recorder
        # Used to rank sources so overlapping metrics (e.g. heart rate
        # from a watch and a ring) resolve to one device rather than
        # double-counting or blending. None → legacy cross-source view.
        self._manager = device_manager
        self._catalog = catalog

        scroller = Gtk.ScrolledWindow(
            hscrollbar_policy=Gtk.PolicyType.NEVER, vexpand=True)
        clamp = Adw.Clamp(maximum_size=560, margin_top=18, margin_bottom=18,
                          margin_start=12, margin_end=12)
        self._box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
        clamp.set_child(self._box)
        scroller.set_child(clamp)
        self.append(scroller)

        self._build_steps_card()
        self._build_energy_card()
        self._build_meals_card()
        self._build_water_card()
        self._build_weight_card()
        self._build_heart_card()
        self._build_sleep_card()
        self._build_recap_card()

        for key in ("daily-step-goal", "water-goal-ml"):
            settings.connect(f"changed::{key}", lambda *_: self.refresh())

    # ── card scaffolding ──────────────────────────────────────────
    def _card(self, title: str, legend: list[tuple] | None = None,
              metric: str | None = None) -> Gtk.Box:
        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        card.add_css_class("card")
        inner = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10,
                        margin_top=14, margin_bottom=14,
                        margin_start=14, margin_end=14)
        card.append(inner)

        header = Gtk.Box(spacing=12)
        label = Gtk.Label(label=title, xalign=0, hexpand=True)
        label.add_css_class("heading")
        header.append(label)
        for colour, text in legend or []:
            header.append(LegendDot(colour))
            entry = Gtk.Label(label=text)
            entry.add_css_class("caption")
            entry.add_css_class("dim-label")
            header.append(entry)
        # A tapped metric card opens its full history view.
        if metric is not None:
            chevron = Gtk.Image.new_from_icon_name("go-next-symbolic")
            chevron.add_css_class("dim-label")
            header.append(chevron)
            card.set_cursor(Gdk.Cursor.new_from_name("pointer", None))
            click = Gtk.GestureClick()
            click.connect("released", lambda *_: self._open_metric(metric))
            card.add_controller(click)
        inner.append(header)

        self._box.append(card)
        return inner

    def _open_metric(self, type_key: str) -> None:
        root = self.get_root()
        if root is not None:
            root.push_metric_detail(type_key)

    @staticmethod
    def _ring_with_labels(ring: ActivityRing, value: Gtk.Label,
                          caption: str, goal: Gtk.Label) -> Gtk.Overlay:
        overlay = Gtk.Overlay(halign=Gtk.Align.CENTER)
        overlay.set_child(ring)
        centre = Gtk.Box(orientation=Gtk.Orientation.VERTICAL,
                         valign=Gtk.Align.CENTER, halign=Gtk.Align.CENTER)
        value.add_css_class("title-1")
        sub = Gtk.Label(label=caption)
        sub.add_css_class("dim-label")
        sub.add_css_class("caption")
        goal.add_css_class("dim-label")
        goal.add_css_class("caption")
        centre.append(value)
        centre.append(sub)
        centre.append(goal)
        overlay.add_overlay(centre)
        return overlay

    @staticmethod
    def _caption_label(inner: Gtk.Box) -> Gtk.Label:
        """A dim caption line that hides itself when empty (trend and
        freshness notes)."""
        label = Gtk.Label(xalign=0, wrap=True, visible=False)
        label.add_css_class("caption")
        label.add_css_class("dim-label")
        inner.append(label)
        return label

    @staticmethod
    def _set_caption(label: Gtk.Label, text: str) -> None:
        label.set_label(text)
        label.set_visible(bool(text))

    # ── cards ─────────────────────────────────────────────────────
    def _build_steps_card(self) -> None:
        inner = self._card("Steps", metric="step_count")
        self._steps_ring = ActivityRing()
        self._steps_value = Gtk.Label()
        self._steps_goal = Gtk.Label()
        self._steps_body = self._ring_with_labels(
            self._steps_ring, self._steps_value, "steps today", self._steps_goal)
        inner.append(self._steps_body)
        self._steps_source = Gtk.Label(xalign=0, wrap=True)
        self._steps_source.add_css_class("caption")
        inner.append(self._steps_source)
        self._steps_chart = BarChart()
        inner.append(self._steps_chart)
        self._steps_trend = self._caption_label(inner)
        self._steps_fresh = self._caption_label(inner)

    def _build_energy_card(self) -> None:
        inner = self._card("Calories", legend=[(ACCENT2, "Eaten"),
                                               (ACCENT, "Burned")])
        self._energy_today = Gtk.Label(xalign=0)
        self._energy_today.add_css_class("dim-label")
        self._energy_today.add_css_class("caption")
        inner.append(self._energy_today)
        self._energy_chart = GroupedBarChart()
        inner.append(self._energy_chart)
        self._energy_trend = self._caption_label(inner)

    def _build_meals_card(self) -> None:
        # A boxed list (its own card); each meal is an expandable row that
        # reveals the foods logged for it.
        self._meals_group = Adw.PreferencesGroup(title="Meals — today")
        self._meal_rows: list[Gtk.Widget] = []
        # Copy a past day's meals onto today (same foods, same times).
        copy_button = Gtk.MenuButton(
            icon_name="edit-copy-symbolic", css_classes=["flat"],
            valign=Gtk.Align.CENTER,
            tooltip_text="Copy meals from another day")
        self._copy_popover = Gtk.Popover()
        self._copy_calendar = Gtk.Calendar()
        self._copy_calendar.connect("day-selected", self._on_copy_day_picked)
        self._copy_popover.set_child(self._copy_calendar)
        copy_button.set_popover(self._copy_popover)
        self._meals_group.set_header_suffix(copy_button)
        self._box.append(self._meals_group)

    def _build_water_card(self) -> None:
        inner = self._card("Water", metric="water_intake")
        self._water_ring = ActivityRing()
        self._water_value = Gtk.Label()
        self._water_goal = Gtk.Label()
        inner.append(self._ring_with_labels(
            self._water_ring, self._water_value, "mL today", self._water_goal))
        self._water_chart = BarChart()
        inner.append(self._water_chart)
        self._water_trend = self._caption_label(inner)

    def _build_weight_card(self) -> None:
        inner = self._card("Weight — 30 days", metric="body_weight")
        self._weight_chart = LineChart()
        inner.append(self._weight_chart)

    def _build_heart_card(self) -> None:
        inner = self._card("Heart rate — today", metric="heart_rate")
        self._heart_summary = Gtk.Label(xalign=0)
        self._heart_summary.add_css_class("dim-label")
        self._heart_summary.add_css_class("caption")
        inner.append(self._heart_summary)
        self._heart_source = Gtk.Label(xalign=0, wrap=True)
        self._heart_source.add_css_class("caption")
        inner.append(self._heart_source)
        self._heart_chart = LineChart()
        inner.append(self._heart_chart)
        self._heart_fresh = self._caption_label(inner)

    def _build_sleep_card(self) -> None:
        inner = self._card("Last sleep")
        self._sleep_label = Gtk.Label(xalign=0)
        self._sleep_label.add_css_class("title-3")
        self._sleep_detail = Gtk.Label(xalign=0)
        self._sleep_detail.add_css_class("dim-label")
        inner.append(self._sleep_label)
        inner.append(self._sleep_detail)

    def _build_recap_card(self) -> None:
        inner = self._card("Last week")
        self._recap_caption = Gtk.Label(xalign=0)
        self._recap_caption.add_css_class("caption")
        self._recap_caption.add_css_class("dim-label")
        inner.append(self._recap_caption)
        self._recap_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL,
                                  spacing=8)
        inner.append(self._recap_box)

    # ── data ──────────────────────────────────────────────────────
    def _trust(self, type_key: str) -> dict[str, int] | None:
        """Per-source trust map for a metric, or None when we have no
        device manager (then aggregation keeps its legacy behaviour)."""
        return self._manager.source_trust(type_key) if self._manager else None

    def _range(self, type_key: str) -> tuple[float, float] | None:
        """Plausible display range for a metric, so glitch samples (a
        0-bpm heart rate) don't corrupt the min/avg/max shown."""
        td = self._catalog.get(type_key) if self._catalog else None
        return td.plausible if td else None

    def _source_names(self) -> dict[str, str]:
        """Map each source device_id to a display name."""
        names = {"": "Manual entry"}
        if self._manager is not None:
            for entry in self._manager.list():
                names[entry.address] = entry.name
        return names

    def _set_source_note(self, label, bucket, fmt) -> None:
        """Name the source a resolved value came from, and warn when a
        dropped source materially disagreed. ``fmt`` renders a value."""
        label.remove_css_class("warning")
        label.remove_css_class("dim-label")
        if not bucket or bucket.get("source") is None:
            label.set_label("")
            return
        names = self._source_names()
        chosen = names.get(bucket["source"], "a device")
        disc = bucket.get("discrepancy")
        if disc:
            others = " · ".join(f"{names.get(d, d)} {fmt(v)}"
                                for d, v in disc.items())
            label.set_label(
                f"⚠ Sources disagree — showing {chosen} "
                f"{fmt(bucket['value'])}; also {others}")
            label.add_css_class("warning")
        elif len(names) > 2:  # more than manual + one device registered
            label.set_label(f"Source: {chosen}")
            label.add_css_class("dim-label")
        else:
            label.set_label("")

    def _day_values(self, type_key: str, op: str, start_day: int,
                    end_day: int) -> list[float | None]:
        """Daily buckets for [start_day, end_day) days back, oldest →
        newest, None for days with nothing recorded."""
        tz = local_tz_name()
        buckets = self._store.aggregate(
            type_key, op, "day", start_ms=to_ms(local_day_start(start_day)),
            end_ms=to_ms(local_day_start(end_day)), tz=tz,
            source_trust=self._trust(type_key),
            value_range=self._range(type_key))
        by_day = {b["start"][:10]: b["value"] for b in buckets}
        keys = [local_day_start(d).date().isoformat()
                for d in range(start_day, end_day, -1)]
        return [by_day.get(k) for k in keys]

    def _set_trend(self, label: Gtk.Label, type_key: str,
                   prefix: str = "") -> None:
        """An honest week-on-week caption: the last 7 *complete* days
        against the 7 before (today, still in progress, is excluded)."""
        text = format_trend(weekly_trend(self._day_values(type_key, "sum",
                                                          14, 0)))
        self._set_caption(label, f"{prefix}{text}" if text else "")

    def _apply_freshness(self, types: list[str], label: Gtk.Label,
                         *widgets) -> None:
        """Fade a device-fed card's data widgets when its newest sample
        has gone stale, and say how old it is."""
        newest = self._store.latest_time(types)
        age_hours = None
        if newest is not None:
            age_hours = max(
                (datetime.now().timestamp() * 1000 - newest) / 3_600_000, 0.0)
        opacity, note = staleness(age_hours)
        for widget in widgets:
            widget.set_opacity(opacity)
        self._set_caption(label, note)

    def refresh(self) -> None:
        tz = local_tz_name()
        days = [local_day_start(_DAYS - 1 - i) for i in range(_DAYS)]
        keys = [d.date().isoformat() for d in days]
        start = to_ms(days[0])

        def day_series(type_key: str, op: str = "sum") -> list[float | None]:
            buckets = self._store.aggregate(
                type_key, op, "day", start_ms=start, tz=tz,
                source_trust=self._trust(type_key))
            by_day = {b["start"][:10]: b["value"] for b in buckets}
            return [by_day.get(k) for k in keys]

        # Steps.
        steps = day_series("step_count")
        goal = self._settings.get_int("daily-step-goal")
        today_steps = int(steps[-1] or 0)
        self._steps_value.set_label(f"{today_steps:,}")
        self._steps_goal.set_label(f"of {goal:,}" if goal else "")
        self._steps_ring.set_fraction(today_steps / goal if goal else 0.0)
        self._steps_chart.set_data(steps, goal=goal or None)
        steps_today = self._store.aggregate(
            "step_count", "sum", "day", start_ms=to_ms(local_day_start()), tz=tz,
            source_trust=self._trust("step_count"), discrepancy_threshold=0.25)
        self._set_source_note(self._steps_source,
                              steps_today[-1] if steps_today else None,
                              lambda v: f"{int(v):,}")
        self._set_trend(self._steps_trend, "step_count")
        self._apply_freshness(["step_count"], self._steps_fresh,
                              self._steps_body, self._steps_chart)

        # Calories in vs out (both canonical kcal).
        eaten = day_series("dietary_energy")
        burned = day_series("active_energy")
        self._energy_chart.set_data(eaten, burned)
        self._energy_today.set_label(
            f"Today: {format_value(eaten[-1], 'kcal')} kcal eaten · "
            f"{format_value(burned[-1], 'kcal')} kcal burned")
        self._set_trend(self._energy_trend, "dietary_energy", prefix="Eaten ")

        self._update_meals()

        # Water.
        water = day_series("water_intake")
        water_goal = self._settings.get_int("water-goal-ml")
        today_water = int(water[-1] or 0)
        self._water_value.set_label(f"{today_water:,}")
        self._water_goal.set_label(f"of {water_goal:,} mL" if water_goal else "")
        self._water_ring.set_fraction(
            today_water / water_goal if water_goal else 0.0)
        self._water_chart.set_data(water, goal=water_goal or None)
        self._set_trend(self._water_trend, "water_intake")

        # Weight, 30 days (sparse — the line skips gaps).
        w_start = to_ms(local_day_start(29))
        buckets = self._store.aggregate(
            "body_weight", "avg", "day", start_ms=w_start, tz=tz,
            source_trust=self._trust("body_weight"),
            value_range=self._range("body_weight"))
        by_day = {b["start"][:10]: b["value"] for b in buckets}
        w_keys = [(local_day_start(29 - i)).date().isoformat() for i in range(30)]
        self._weight_chart.set_data([by_day.get(k) for k in w_keys])

        # Heart rate today: hourly average line + min/avg/max summary.
        t_start = to_ms(local_day_start())
        hr_trust = self._trust("heart_rate")
        hr_range = self._range("heart_rate")
        hourly = self._store.aggregate("heart_rate", "avg", "hour",
                                       start_ms=t_start, tz=tz,
                                       source_trust=hr_trust,
                                       value_range=hr_range)
        by_hour = {b["start"][11:13]: b["value"] for b in hourly}
        self._heart_chart.set_data(
            [by_hour.get(f"{h:02d}") for h in range(24)])
        lo = self._store.aggregate("heart_rate", "min", "day", start_ms=t_start,
                                   tz=tz, source_trust=hr_trust,
                                   value_range=hr_range)
        avg = self._store.aggregate("heart_rate", "avg", "day", start_ms=t_start,
                                    tz=tz, source_trust=hr_trust,
                                    discrepancy_threshold=0.12,
                                    value_range=hr_range)
        hi = self._store.aggregate("heart_rate", "max", "day", start_ms=t_start,
                                   tz=tz, source_trust=hr_trust,
                                   value_range=hr_range)
        if avg:
            self._heart_summary.set_label(
                f"min {format_value(lo[-1]['value'], '/min')} · "
                f"avg {format_value(avg[-1]['value'], '/min')} · "
                f"max {format_value(hi[-1]['value'], '/min')} bpm")
            self._set_source_note(
                self._heart_source, avg[-1],
                lambda v: f"{format_value(v, '/min')} bpm")
        else:
            self._heart_summary.set_label("No readings today")
            self._set_source_note(self._heart_source, None, str)
        self._apply_freshness(["heart_rate"], self._heart_fresh,
                              self._heart_chart)

        self._update_sleep()
        self._update_recap()

    def _update_meals(self) -> None:
        from vitals.sources.food import summarize_meals

        for row in self._meal_rows:
            self._meals_group.remove(row)
        self._meal_rows = []

        start = to_ms(local_day_start())
        rows, _ = self._store.read_records(["nutrient_intake"], start_ms=start)
        meals = summarize_meals(rows)
        if not meals:
            empty = Adw.ActionRow(title="No meals logged today")
            empty.set_subtitle("Tap + to log food")
            self._meals_group.add(empty)
            self._meal_rows.append(empty)
            return
        for meal in meals:
            items = meal["item_count"]
            expander = Adw.ExpanderRow(
                title=meal["label"],
                subtitle=f"{items} item{'s' if items != 1 else ''}")
            total = Gtk.Label(label=f"{meal['kcal']:,} kcal", valign=Gtk.Align.CENTER)
            total.add_css_class("dim-label")
            expander.add_suffix(total)
            for food in meal["foods"]:
                frow = Adw.ActionRow(title=food["label"])
                when = datetime.fromtimestamp(
                    food["when_ms"] / 1000).astimezone()
                frow.set_subtitle(when.strftime("%H:%M"))
                if food["kcal"]:
                    kcal = Gtk.Label(label=f"{int(food['kcal']):,} kcal",
                                     valign=Gtk.Align.CENTER)
                    kcal.add_css_class("dim-label")
                    frow.add_suffix(kcal)
                expander.add_row(frow)
            self._meals_group.add(expander)
            self._meal_rows.append(expander)

    def _on_copy_day_picked(self, calendar) -> None:
        # Only respond to real user picks (the popover is open), not the
        # signal GtkCalendar emits while being set up.
        if not self._copy_popover.get_visible():
            return
        gd = calendar.get_date()
        source = date(gd.get_year(), gd.get_month(), gd.get_day_of_month())
        self._copy_popover.popdown()
        self._copy_meals_from(source)

    def _copy_meals_from(self, source: date) -> None:
        """Re-log every food from ``source`` onto today, same times."""
        from vitals.sources.food import copy_meal_records

        today = datetime.now().astimezone().date()
        if source >= today or self._recorder is None:
            self._toast("Pick a past day to copy from")
            return
        day_start = datetime(source.year, source.month,
                             source.day).astimezone()
        rows, _ = self._store.read_records(
            ["nutrient_intake"], start_ms=to_ms(day_start),
            end_ms=to_ms(day_start + timedelta(days=1)))
        if not rows:
            self._toast(f"No meals logged on {source:%a %-d %b}")
            return
        summary = self._recorder.ingest(copy_meal_records(rows, today))
        if summary["rejected"]:
            self._toast(f"Couldn’t copy: {summary['rejected'][0][1]}")
            return
        count = len(rows)
        self._toast(f"Copied {count} food{'s' if count != 1 else ''} "
                    f"from {source:%a %-d %b}")

    def _update_recap(self) -> None:
        # The last *completed* Monday–Sunday week against the one
        # before — two equal, finished windows, so the deltas are honest.
        back = datetime.now().astimezone().weekday()
        week: dict[str, list[float | None]] = {}
        prior: dict[str, list[float | None]] = {}
        for spec in RECAP_METRICS:
            op = "sum" if spec["mode"] == "per-day" else "avg"
            week[spec["key"]] = self._day_values(
                spec["key"], op, back + 7, back)
            prior[spec["key"]] = self._day_values(
                spec["key"], op, back + 14, back + 7)

        self._recap_caption.set_label(
            f"{local_day_start(back + 7):%-d %b} – "
            f"{local_day_start(back + 1):%-d %b}")
        child = self._recap_box.get_first_child()
        while child is not None:
            nxt = child.get_next_sibling()
            self._recap_box.remove(child)
            child = nxt
        rows = recap_rows(week, prior)
        if not rows:
            empty = Gtk.Label(label="Nothing recorded last week", xalign=0)
            empty.add_css_class("dim-label")
            self._recap_box.append(empty)
            return
        for row in rows:
            line = Gtk.Box(spacing=12)
            title = Gtk.Label(label=row["title"], xalign=0, hexpand=True)
            line.append(title)
            values = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
            value = Gtk.Label(label=row["value_text"], xalign=1)
            values.append(value)
            if row["trend_text"]:
                trend = Gtk.Label(label=row["trend_text"], xalign=1)
                trend.add_css_class("caption")
                trend.add_css_class("dim-label")
                values.append(trend)
            line.append(values)
            self._recap_box.append(line)

    def _update_sleep(self) -> None:
        since = to_ms(datetime.now().astimezone() - timedelta(hours=36))
        rows, _ = self._store.read_records(["sleep_episode"], start_ms=since)
        if not rows:
            self._sleep_label.set_label("—")
            self._sleep_detail.set_label("No sleep recorded in the last 36 hours")
            return
        row = rows[-1]
        body = json.loads(row["value_json"] or "{}")
        total = body.get("total_sleep_minutes") or 0
        deep = sum(
            (datetime.fromisoformat(s["end"]) -
             datetime.fromisoformat(s["start"])).total_seconds() / 60
            for s in body.get("stages", []) if s.get("stage") == "deep")
        self._sleep_label.set_label(_fmt_minutes(total))
        detail = f"{_fmt_minutes(round(deep))} deep" if deep else ""
        ended = datetime.fromtimestamp(
            (row["effective_end"] or row["effective_start"]) / 1000).astimezone()
        woke = f"woke {ended.strftime('%H:%M')}"
        self._sleep_detail.set_label(" · ".join(p for p in (detail, woke) if p))


def _fmt_minutes(minutes: int) -> str:
    hours, mins = divmod(int(minutes), 60)
    return f"{hours} h {mins:02d} m" if hours else f"{mins} m"
