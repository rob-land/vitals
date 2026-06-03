"""Trends page — a daily chart of a chosen scalar metric over time."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from gi.repository import Adw, Gtk

from vitals.format import humanize_key, unit_label
from vitals.pages import PulsePage
from vitals.pulse_client import PulseUnavailable
from vitals.widgets import BarChart

log = logging.getLogger(__name__)

# Additive metrics are summed per day; everything else is averaged.
_ADDITIVE = {"step_count", "active_energy", "distance", "floors_climbed",
             "dietary_energy", "water_intake", "mindful_minutes"}


class TrendsPage(PulsePage):
    def __init__(self, client, settings):
        super().__init__()
        self._client = client
        self._settings = settings
        self._keys: list[str] = []          # parallel to the dropdown model
        self._catalog: dict = {}
        self._loading = False               # guard against reentrant refresh

        clamp = Adw.Clamp(maximum_size=560, margin_top=18, margin_bottom=18,
                          margin_start=12, margin_end=12)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        clamp.set_child(box)
        self._set_content(clamp)

        self._dropdown = Gtk.DropDown(model=Gtk.StringList())
        self._dropdown.set_hexpand(True)
        self._dropdown.connect("notify::selected", self._on_metric_changed)
        box.append(self._dropdown)

        self._caption = Gtk.Label(halign=Gtk.Align.START)
        self._caption.add_css_class("dim-label")
        self._caption.add_css_class("caption")
        box.append(self._caption)

        self._chart = BarChart()
        frame = Gtk.Frame()
        frame.add_css_class("card")
        frame.set_child(self._chart)
        box.append(frame)

    def refresh(self) -> None:
        try:
            self._catalog = {t["key"]: t for t in
                             self._client.list_types().get("types", [])}
            if not self._keys:
                self._populate_metrics()
            self._update_chart()
            self._show_content()
        except PulseUnavailable:
            self._show_unavailable()

    def _populate_metrics(self) -> None:
        scalars = [t for t in self._catalog.values() if t.get("value") == "scalar"]
        scalars.sort(key=lambda t: t.get("title") or t["key"])
        model = Gtk.StringList()
        self._keys = []
        for t in scalars:
            model.append(t.get("title") or humanize_key(t["key"]))
            self._keys.append(t["key"])
        self._loading = True
        self._dropdown.set_model(model)
        saved = self._settings.get_string("trends-metric")
        if saved in self._keys:
            self._dropdown.set_selected(self._keys.index(saved))
        self._loading = False

    def _on_metric_changed(self, *_):
        if self._loading or not self._keys:
            return
        idx = self._dropdown.get_selected()
        if 0 <= idx < len(self._keys):
            self._settings.set_string("trends-metric", self._keys[idx])
        try:
            self._update_chart()
            self._show_content()
        except PulseUnavailable:
            self._show_unavailable()

    def _update_chart(self) -> None:
        idx = self._dropdown.get_selected()
        if not self._keys or not (0 <= idx < len(self._keys)):
            return
        key = self._keys[idx]
        days = self._settings.get_int("trends-days")
        op = "sum" if key in _ADDITIVE else "avg"
        start = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        buckets = self._client.aggregate({
            "type": key, "op": op, "bucket": "day", "start": start, "tz": "UTC",
        }).get("buckets", [])

        values = [b["value"] for b in buckets]
        goal = (self._settings.get_int("daily-step-goal")
                if key == "step_count" else None)
        self._chart.set_data(values, goal or None)

        unit = (self._catalog.get(key) or {}).get("canonical_unit")
        verb = "total" if op == "sum" else "average"
        self._caption.set_label(
            f"Daily {verb} · last {days} days · {unit_label(unit) or 'value'}")
