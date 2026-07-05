"""The water dialog: quick-add a drink (ported from larder).

Amounts are entered in millilitres or US fluid ounces (the user's pick,
remembered), but always stored in millilitres — the canonical unit — so
the choice is purely display.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone

from gi.repository import Adw, GLib, Gtk

log = logging.getLogger(__name__)

ML_PER_FLOZ = 29.5735

# Unit code -> (combo label, short suffix, quick-add presets, spin config).
UNITS: dict[str, dict] = {
    "ml":   {"label": "Millilitres", "suffix": "mL",
             "presets": [200, 330, 500], "upper": 3000, "step": 50, "digits": 0},
    "floz": {"label": "Fluid ounces", "suffix": "fl oz",
             "presets": [8, 12, 16], "upper": 100, "step": 1, "digits": 0},
}
_ORDER = ["ml", "floz"]


def to_ml(amount: float, unit: str) -> float:
    """Convert an entered amount to millilitres (the stored unit)."""
    return amount * ML_PER_FLOZ if unit == "floz" else float(amount)


def build_water_record(ml: float, when_iso: str, uuid_str: str) -> dict:
    """Pure: a ``water_intake`` envelope, value in millilitres."""
    return {
        "uuid": uuid_str,
        "type": "water_intake",
        "effective_start": when_iso,
        "value": round(ml, 1),
        "unit": "mL",
        "source": {"modality": "self_reported", "device_name": "Manual entry"},
    }


class WaterDialog(Adw.Dialog):
    __gtype_name__ = "VitalsWaterDialog"

    def __init__(self, recorder, settings):
        super().__init__()
        self._recorder = recorder
        self._settings = settings
        self._unit = (settings.get_string("water-unit")
                      if settings.get_string("water-unit") in UNITS else "ml")

        self.set_title("Log Water")
        self.set_content_width(420)

        toolbar = Adw.ToolbarView()
        toolbar.add_top_bar(Adw.HeaderBar())
        self.set_child(toolbar)

        clamp = Adw.Clamp(maximum_size=480, margin_top=12, margin_bottom=18,
                          margin_start=12, margin_end=12)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        clamp.set_child(box)
        toolbar.set_content(clamp)

        # Quick-add: one tap logs a common amount and closes.
        self._quick_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL,
                                  spacing=8, halign=Gtk.Align.CENTER)
        box.append(self._quick_box)

        group = Adw.PreferencesGroup()
        self._spin = Adw.SpinRow(
            title="Amount", digits=0,
            adjustment=Gtk.Adjustment(lower=0, upper=3000, step_increment=50,
                                      page_increment=200, value=0))
        group.add(self._spin)
        self._unit_combo = Adw.ComboRow(
            title="Unit",
            model=Gtk.StringList.new([UNITS[u]["label"] for u in _ORDER]))
        self._unit_combo.set_selected(_ORDER.index(self._unit))
        self._unit_combo.connect("notify::selected", self._on_unit_changed)
        group.add(self._unit_combo)
        box.append(group)

        log_button = Gtk.Button(label="Log water", halign=Gtk.Align.CENTER)
        log_button.add_css_class("suggested-action")
        log_button.add_css_class("pill")
        log_button.connect("clicked", lambda *_: self._log(self._spin.get_value()))
        box.append(log_button)

        self._apply_unit()

    # ── unit ──────────────────────────────────────────────────────
    def _on_unit_changed(self, *_):
        self._unit = _ORDER[self._unit_combo.get_selected()]
        self._settings.set_string("water-unit", self._unit)
        self._apply_unit()

    def _apply_unit(self):
        cfg = UNITS[self._unit]
        self._spin.set_title(f"Amount ({cfg['suffix']})")
        adj = self._spin.get_adjustment()
        adj.set_upper(cfg["upper"])
        adj.set_step_increment(cfg["step"])
        adj.set_value(0)
        # Rebuild quick-add buttons for the current unit.
        child = self._quick_box.get_first_child()
        while child:
            self._quick_box.remove(child)
            child = self._quick_box.get_first_child()
        for amount in cfg["presets"]:
            button = Gtk.Button(label=f"{amount} {cfg['suffix']}")
            button.add_css_class("pill")
            button.connect("clicked", lambda _b, a=amount: self._log(a))
            self._quick_box.append(button)

    # ── logging ───────────────────────────────────────────────────
    def _log(self, amount: float):
        if amount <= 0:
            self._toast("Enter an amount")
            return
        ml = to_ml(amount, self._unit)
        when = datetime.now(timezone.utc).astimezone()
        record = build_water_record(ml, when.isoformat(), str(uuid.uuid4()))

        summary = self._recorder.ingest([record])
        if summary["rejected"]:
            self._toast(f"Couldn’t log: {summary['rejected'][0][1]}")
            return
        suffix = UNITS[self._unit]["suffix"]
        self._toast(f"Logged {int(amount)} {suffix} of water")
        self.close()

    def _toast(self, message: str):
        self.activate_action("win.toast", GLib.Variant("s", message))
