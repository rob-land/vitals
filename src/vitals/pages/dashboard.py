"""Dashboard page — the at-a-glance cards: steps, calories in vs out,
water, weight trend, heart rate and last night's sleep.

Every card reads the store through ``Store.aggregate`` (tz-aware daily /
hourly buckets); the window triggers ``refresh()`` when records change.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta

from gi.repository import Adw, Gio, Gtk

from vitals.core.store import Store
from vitals.format import format_value
from vitals.pages import Page, local_day_start, local_tz_name, to_ms
from vitals.widgets.charts import (
    ACCENT, ACCENT2, ActivityRing, BarChart, GroupedBarChart, LegendDot,
    LineChart)

log = logging.getLogger(__name__)

_DAYS = 7  # bar-chart window, today inclusive


class Dashboard(Page):
    def __init__(self, store: Store, settings: Gio.Settings):
        super().__init__()
        self._store = store
        self._settings = settings

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
        self._build_water_card()
        self._build_weight_card()
        self._build_heart_card()
        self._build_sleep_card()

        for key in ("daily-step-goal", "water-goal-ml"):
            settings.connect(f"changed::{key}", lambda *_: self.refresh())

    # ── card scaffolding ──────────────────────────────────────────
    def _card(self, title: str, legend: list[tuple] | None = None) -> Gtk.Box:
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
        inner.append(header)

        self._box.append(card)
        return inner

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

    # ── cards ─────────────────────────────────────────────────────
    def _build_steps_card(self) -> None:
        inner = self._card("Steps")
        self._steps_ring = ActivityRing()
        self._steps_value = Gtk.Label()
        self._steps_goal = Gtk.Label()
        inner.append(self._ring_with_labels(
            self._steps_ring, self._steps_value, "steps today", self._steps_goal))
        self._steps_chart = BarChart()
        inner.append(self._steps_chart)

    def _build_energy_card(self) -> None:
        inner = self._card("Calories", legend=[(ACCENT2, "Eaten"),
                                               (ACCENT, "Burned")])
        self._energy_today = Gtk.Label(xalign=0)
        self._energy_today.add_css_class("dim-label")
        self._energy_today.add_css_class("caption")
        inner.append(self._energy_today)
        self._energy_chart = GroupedBarChart()
        inner.append(self._energy_chart)

    def _build_water_card(self) -> None:
        inner = self._card("Water")
        self._water_ring = ActivityRing()
        self._water_value = Gtk.Label()
        self._water_goal = Gtk.Label()
        inner.append(self._ring_with_labels(
            self._water_ring, self._water_value, "mL today", self._water_goal))
        self._water_chart = BarChart()
        inner.append(self._water_chart)

    def _build_weight_card(self) -> None:
        inner = self._card("Weight — 30 days")
        self._weight_chart = LineChart()
        inner.append(self._weight_chart)

    def _build_heart_card(self) -> None:
        inner = self._card("Heart rate — today")
        self._heart_summary = Gtk.Label(xalign=0)
        self._heart_summary.add_css_class("dim-label")
        self._heart_summary.add_css_class("caption")
        inner.append(self._heart_summary)
        self._heart_chart = LineChart()
        inner.append(self._heart_chart)

    def _build_sleep_card(self) -> None:
        inner = self._card("Last sleep")
        self._sleep_label = Gtk.Label(xalign=0)
        self._sleep_label.add_css_class("title-3")
        self._sleep_detail = Gtk.Label(xalign=0)
        self._sleep_detail.add_css_class("dim-label")
        inner.append(self._sleep_label)
        inner.append(self._sleep_detail)

    # ── data ──────────────────────────────────────────────────────
    def refresh(self) -> None:
        tz = local_tz_name()
        days = [local_day_start(_DAYS - 1 - i) for i in range(_DAYS)]
        keys = [d.date().isoformat() for d in days]
        start = to_ms(days[0])

        def day_series(type_key: str, op: str = "sum") -> list[float | None]:
            buckets = self._store.aggregate(type_key, op, "day",
                                            start_ms=start, tz=tz)
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

        # Calories in vs out (both canonical kcal).
        eaten = day_series("dietary_energy")
        burned = day_series("active_energy")
        self._energy_chart.set_data(eaten, burned)
        self._energy_today.set_label(
            f"Today: {format_value(eaten[-1], 'kcal')} kcal eaten · "
            f"{format_value(burned[-1], 'kcal')} kcal burned")

        # Water.
        water = day_series("water_intake")
        water_goal = self._settings.get_int("water-goal-ml")
        today_water = int(water[-1] or 0)
        self._water_value.set_label(f"{today_water:,}")
        self._water_goal.set_label(f"of {water_goal:,} mL" if water_goal else "")
        self._water_ring.set_fraction(
            today_water / water_goal if water_goal else 0.0)
        self._water_chart.set_data(water, goal=water_goal or None)

        # Weight, 30 days (sparse — the line skips gaps).
        w_start = to_ms(local_day_start(29))
        buckets = self._store.aggregate("body_weight", "avg", "day",
                                        start_ms=w_start, tz=tz)
        by_day = {b["start"][:10]: b["value"] for b in buckets}
        w_keys = [(local_day_start(29 - i)).date().isoformat() for i in range(30)]
        self._weight_chart.set_data([by_day.get(k) for k in w_keys])

        # Heart rate today: hourly average line + min/avg/max summary.
        t_start = to_ms(local_day_start())
        hourly = self._store.aggregate("heart_rate", "avg", "hour",
                                       start_ms=t_start, tz=tz)
        by_hour = {b["start"][11:13]: b["value"] for b in hourly}
        self._heart_chart.set_data(
            [by_hour.get(f"{h:02d}") for h in range(24)])
        lo = self._store.aggregate("heart_rate", "min", "day", start_ms=t_start, tz=tz)
        avg = self._store.aggregate("heart_rate", "avg", "day", start_ms=t_start, tz=tz)
        hi = self._store.aggregate("heart_rate", "max", "day", start_ms=t_start, tz=tz)
        if avg:
            self._heart_summary.set_label(
                f"min {format_value(lo[-1]['value'], '/min')} · "
                f"avg {format_value(avg[-1]['value'], '/min')} · "
                f"max {format_value(hi[-1]['value'], '/min')} bpm")
        else:
            self._heart_summary.set_label("No readings today")

        self._update_sleep()

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
