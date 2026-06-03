"""Today page — steps-toward-goal ring plus the latest vital signs."""

from __future__ import annotations

import logging
from datetime import datetime

from gi.repository import Adw, Gtk

from vitals.format import format_measurement, humanize_key
from vitals.pages import PulsePage
from vitals.pulse_client import PulseUnavailable
from vitals.widgets import ActivityRing

log = logging.getLogger(__name__)

# Point-in-time metrics surfaced as "latest reading" cards, in order.
_LATEST_TYPES = ["heart_rate", "resting_heart_rate", "oxygen_saturation",
                 "body_weight", "blood_glucose", "body_temperature"]


class TodayPage(PulsePage):
    def __init__(self, client, settings):
        super().__init__()
        self._client = client
        self._settings = settings
        self._latest_rows: list[Gtk.Widget] = []

        scroller = Gtk.ScrolledWindow(
            hscrollbar_policy=Gtk.PolicyType.NEVER, vexpand=True)
        clamp = Adw.Clamp(maximum_size=480, margin_top=18, margin_bottom=18,
                          margin_start=12, margin_end=12)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        clamp.set_child(box)
        scroller.set_child(clamp)
        self._set_content(scroller)

        # Steps ring with centred labels.
        self._ring = ActivityRing()
        overlay = Gtk.Overlay(halign=Gtk.Align.CENTER)
        overlay.set_child(self._ring)
        centre = Gtk.Box(orientation=Gtk.Orientation.VERTICAL,
                         valign=Gtk.Align.CENTER, halign=Gtk.Align.CENTER)
        self._steps_label = Gtk.Label()
        self._steps_label.add_css_class("title-1")
        sub = Gtk.Label(label="steps today")
        sub.add_css_class("dim-label")
        sub.add_css_class("caption")
        self._goal_label = Gtk.Label()
        self._goal_label.add_css_class("dim-label")
        self._goal_label.add_css_class("caption")
        centre.append(self._steps_label)
        centre.append(sub)
        centre.append(self._goal_label)
        overlay.add_overlay(centre)
        box.append(overlay)

        self._latest_group = Adw.PreferencesGroup(title="Latest")
        box.append(self._latest_group)

    def refresh(self) -> None:
        try:
            catalog = {t["key"]: t for t in self._client.list_types().get("types", [])}
            self._update_steps()
            self._update_latest(catalog)
            self._show_content()
        except PulseUnavailable:
            self._show_unavailable()

    def _update_steps(self) -> None:
        goal = self._settings.get_int("daily-step-goal")
        steps = 0
        record = self._client.latest("step_count", within_days=2)
        if record is not None:
            end = record.get("effective_end") or record.get("effective_start")
            try:
                when = datetime.fromisoformat(end).astimezone().date()
            except (TypeError, ValueError):
                when = None
            if when == datetime.now().astimezone().date():
                steps = int(record.get("value") or 0)
        self._steps_label.set_label(f"{steps:,}")
        self._goal_label.set_label(f"of {goal:,}" if goal else "")
        self._ring.set_fraction(steps / goal if goal else 0.0)

    def _update_latest(self, catalog: dict) -> None:
        for row in self._latest_rows:
            self._latest_group.remove(row)
        self._latest_rows = []

        for key in _LATEST_TYPES:
            record = self._client.latest(key)
            if not record:
                continue
            title = (catalog.get(key) or {}).get("title") or humanize_key(key)
            row = Adw.ActionRow(title=title)
            value = Gtk.Label(
                label=format_measurement(record.get("value"), record.get("unit")))
            value.add_css_class("title-4")
            row.add_suffix(value)
            device = (record.get("source") or {}).get("device_name")
            if device:
                row.set_subtitle(device)
            self._latest_group.add(row)
            self._latest_rows.append(row)

        if not self._latest_rows:
            placeholder = Adw.ActionRow(
                title="No readings yet",
                subtitle="Grant Vitals read access in Permissions, then sync a device")
            self._latest_group.add(placeholder)
            self._latest_rows.append(placeholder)
