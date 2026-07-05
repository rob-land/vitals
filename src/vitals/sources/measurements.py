"""The measurement dialog: pick a metric, enter a value (ported from jot)."""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone

from gi.repository import Adw, GLib, Gtk

log = logging.getLogger(__name__)

# Loggable measurements. kind "scalar" -> one value; "bp" -> systolic +
# diastolic components. Values are entered in (and stored as) these canonical
# UCUM units, matching the catalog.
METRICS: dict[str, dict] = {
    "body_weight":         {"title": "Weight",            "kind": "scalar", "unit": "kg",     "range": (0, 500),   "step": 0.1, "digits": 1, "default": 70},
    "blood_pressure":      {"title": "Blood pressure",    "kind": "bp",     "unit": "mm[Hg]", "range": (0, 300),   "step": 1,   "digits": 0, "default": 120},
    "blood_glucose":       {"title": "Blood glucose",     "kind": "scalar", "unit": "mmol/L", "range": (0, 50),    "step": 0.1, "digits": 1, "default": 5},
    "heart_rate":          {"title": "Heart rate",        "kind": "scalar", "unit": "/min",   "range": (20, 250),  "step": 1,   "digits": 0, "default": 70},
    "oxygen_saturation":   {"title": "Oxygen saturation", "kind": "scalar", "unit": "%",      "range": (50, 100),  "step": 1,   "digits": 0, "default": 98},
    "body_temperature":    {"title": "Body temperature",  "kind": "scalar", "unit": "Cel",    "range": (25, 45),   "step": 0.1, "digits": 1, "default": 37},
    "body_fat_percentage": {"title": "Body fat",          "kind": "scalar", "unit": "%",      "range": (1, 70),    "step": 0.1, "digits": 1, "default": 20},
}
ORDER = list(METRICS.keys())


def build_record(key: str, values: dict, when_iso: str, uuid_str: str) -> dict:
    """Pure: assemble an envelope from form values. ``values`` is
    {"value": n} for scalars or {"systolic", "diastolic"} for blood pressure."""
    metric = METRICS[key]
    record = {
        "uuid": uuid_str,
        "type": key,
        "effective_start": when_iso,
        "source": {"modality": "self_reported", "device_name": "Manual entry"},
    }
    if metric["kind"] == "bp":
        record["value"] = {"systolic": values["systolic"],
                           "diastolic": values["diastolic"]}
    else:
        record["value"] = values["value"]
        record["unit"] = metric["unit"]
    return record


class MeasurementDialog(Adw.Dialog):
    __gtype_name__ = "VitalsMeasurementDialog"

    def __init__(self, recorder, settings):
        super().__init__()
        self._recorder = recorder
        self._settings = settings
        self._value_rows: list[Gtk.Widget] = []

        self.set_title("Log a Reading")
        self.set_content_width(420)

        toolbar = Adw.ToolbarView()
        toolbar.add_top_bar(Adw.HeaderBar())
        self.set_child(toolbar)

        clamp = Adw.Clamp(maximum_size=480, margin_top=12, margin_bottom=18,
                          margin_start=12, margin_end=12)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        clamp.set_child(box)
        toolbar.set_content(clamp)

        self._group = Adw.PreferencesGroup()
        self._combo = Adw.ComboRow(
            title="Measurement",
            model=Gtk.StringList.new([METRICS[k]["title"] for k in ORDER]))
        last = settings.get_string("last-metric")
        if last in ORDER:
            self._combo.set_selected(ORDER.index(last))
        self._combo.connect("notify::selected", self._on_metric_changed)
        self._group.add(self._combo)
        box.append(self._group)

        log_button = Gtk.Button(label="Log reading", halign=Gtk.Align.CENTER)
        log_button.add_css_class("suggested-action")
        log_button.add_css_class("pill")
        log_button.connect("clicked", self._on_log)
        box.append(log_button)

        self._rebuild_value_rows()

    # ── dynamic value rows ────────────────────────────────────────
    def _current_key(self) -> str:
        return ORDER[self._combo.get_selected()]

    def _on_metric_changed(self, *_):
        self._settings.set_string("last-metric", self._current_key())
        self._rebuild_value_rows()

    def _rebuild_value_rows(self):
        from vitals.format import unit_label
        for row in self._value_rows:
            self._group.remove(row)
        metric = METRICS[self._current_key()]
        if metric["kind"] == "bp":
            self._value_rows = [self._spin("Systolic", metric),
                                self._spin("Diastolic", metric, default=80)]
        else:
            self._value_rows = [self._spin(unit_label(metric["unit"]), metric)]
        for row in self._value_rows:
            self._group.add(row)

    def _spin(self, title, metric, default=None):
        lo, hi = metric["range"]
        return Adw.SpinRow(
            title=title,
            digits=metric["digits"],
            adjustment=Gtk.Adjustment(
                lower=lo, upper=hi, step_increment=metric["step"],
                page_increment=metric["step"] * 10,
                value=default if default is not None else metric["default"]))

    # ── logging ───────────────────────────────────────────────────
    def _on_log(self, _button):
        key = self._current_key()
        metric = METRICS[key]
        if metric["kind"] == "bp":
            values = {"systolic": self._value_rows[0].get_value(),
                      "diastolic": self._value_rows[1].get_value()}
        else:
            values = {"value": self._value_rows[0].get_value()}
        when = datetime.now(timezone.utc).astimezone()
        record = build_record(key, values, when.isoformat(), str(uuid.uuid4()))

        summary = self._recorder.ingest([record])
        if summary["rejected"]:
            self._toast(f"Couldn’t log: {summary['rejected'][0][1]}")
            return
        self._toast(f"Logged {metric['title'].lower()}")
        self.close()

    def _toast(self, message: str):
        self.activate_action("win.toast", GLib.Variant("s", message))
