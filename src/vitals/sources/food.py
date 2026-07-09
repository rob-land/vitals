"""The food dialog: enter a food and its nutrients (ported from larder).

Writes a ``nutrient_intake`` record — calories, sugar, carbs, fat,
protein — with a meal tag, plus a companion ``dietary_energy`` scalar
whenever calories were entered, so the dashboard's calories-eaten
aggregate works (structured bodies can't be aggregated). Nutrient names
are keyed to Open Food Facts ``nutriments`` so a barcode lookup
populates the same fields directly.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta, timezone

from gi.repository import Adw, GLib, Gtk

from vitals.sources import food_cache

log = logging.getLogger(__name__)

# Nutrients we log. `key` is the Open Food Facts nutriment name; values are
# entered in (and stored as) the UCUM `unit`, matching the catalog.
NUTRIENTS: list[dict] = [
    {"key": "energy-kcal",   "title": "Calories",      "unit": "kcal", "step": 10, "digits": 0, "max": 5000},
    {"key": "carbohydrates", "title": "Carbohydrates", "unit": "g",    "step": 1,  "digits": 1, "max": 1000},
    {"key": "sugars",        "title": "Sugars",        "unit": "g",    "step": 1,  "digits": 1, "max": 1000},
    {"key": "fat",           "title": "Fat",           "unit": "g",    "step": 1,  "digits": 1, "max": 1000},
    {"key": "proteins",      "title": "Protein",       "unit": "g",    "step": 1,  "digits": 1, "max": 1000},
]
_UNITS = {n["key"]: n["unit"] for n in NUTRIENTS}

# Meal tag: (label, meta value), in daily order.
MEALS: list[tuple[str, str]] = [
    ("Breakfast", "breakfast"), ("Lunch", "lunch"),
    ("Dinner", "dinner"), ("Snack", "snack"),
]
_MEAL_KEYS = [m[1] for m in MEALS]
_MEAL_LABELS = {m[1]: m[0] for m in MEALS}


def summarize_meals(rows) -> list[dict]:
    """Group ``nutrient_intake`` store rows into per-meal totals, in daily
    meal order (breakfast → snack, then anything else).

    Each meal is ``{meal, label, kcal, item_count, foods}`` where every
    food is ``{label, kcal, when_ms}``. Pure over the rows' documented
    columns (``value_json``, ``meta_json``, ``effective_start``) so it's
    testable without a store.
    """
    import json

    by_meal: dict[str, list[dict]] = {}
    for row in rows:
        meta = json.loads(row["meta_json"] or "{}")
        meal = meta.get("meal") or "other"
        value = json.loads(row["value_json"] or "{}")
        kcal = value.get("nutrients", {}).get("energy-kcal", {}).get("value")
        by_meal.setdefault(meal, []).append({
            "label": value.get("label") or "Food",
            "kcal": float(kcal) if kcal else 0.0,
            "when_ms": row["effective_start"],
        })

    order = _MEAL_KEYS + [m for m in by_meal if m not in _MEAL_KEYS]
    out = []
    for meal in order:
        foods = by_meal.get(meal)
        if not foods:
            continue
        foods.sort(key=lambda f: f["when_ms"])
        out.append({
            "meal": meal,
            "label": _MEAL_LABELS.get(meal, meal.replace("_", " ").title()),
            "kcal": round(sum(f["kcal"] for f in foods)),
            "item_count": len(foods),
            "foods": foods,
        })
    return out


def build_record(label: str, meal: str, nutrients: dict[str, float],
                 when_iso: str, uuid_str: str,
                 amount_g: float | None = None,
                 barcode: str | None = None) -> dict:
    """Pure: assemble a ``nutrient_intake`` envelope.

    ``nutrients`` maps an Open Food Facts nutriment key to a numeric amount
    (only the ones the user actually entered). ``amount_g`` and ``barcode``
    are set when the food came from an Open Food Facts lookup. Structured
    types carry no envelope-level unit."""
    record = {
        "uuid": uuid_str,
        "type": "nutrient_intake",
        "effective_start": when_iso,
        "source": {"modality": "self_reported", "device_name": "Manual entry"},
        "value": {
            "nutrients": {k: {"value": v, "unit": _UNITS.get(k, "g")}
                          for k, v in nutrients.items()},
        },
        "meta": {"meal": meal},
    }
    if label:
        record["value"]["label"] = label
    if amount_g:
        record["value"]["amount"] = {"value": amount_g, "unit": "g"}
    if barcode:
        record["meta"]["off_barcode"] = barcode
    return record


def build_energy_record(kcal: float, when_iso: str, meal_uuid: str) -> dict:
    """Pure: the companion ``dietary_energy`` scalar for a meal, keyed to
    the meal's uuid so the pair stays consistent under upserts. Hidden on
    the Timeline (the meal row carries the kcal); it exists so
    ``Store.aggregate`` can sum calories eaten per day."""
    return {
        "uuid": f"{meal_uuid}:energy",
        "type": "dietary_energy",
        "effective_start": when_iso,
        "value": round(kcal),
        "unit": "kcal",
        "source": {"modality": "self_reported", "device_name": "Manual entry"},
    }


def build_meal_records(label: str, meal: str, nutrients: dict[str, float],
                       when_iso: str, uuid_str: str,
                       amount_g: float | None = None,
                       barcode: str | None = None) -> list[dict]:
    """The full batch one logged food produces."""
    records = [build_record(label, meal, nutrients, when_iso, uuid_str,
                            amount_g=amount_g, barcode=barcode)]
    kcal = nutrients.get("energy-kcal")
    if kcal:
        records.append(build_energy_record(kcal, when_iso, uuid_str))
    return records


def copy_meal_records(rows, target, uuid_factory=None) -> list[dict]:
    """Pure: re-log one day's foods onto the ``target`` date.

    ``rows`` are that day's ``nutrient_intake`` store rows. Each food
    keeps its label, meal tag, nutrients, portion, barcode and
    wall-clock time — only the date moves — and gets a fresh uuid, so
    a copy is a new logging, not an upsert of the original. Companion
    ``dietary_energy`` scalars come along via ``build_meal_records``.
    """
    import json

    make_uuid = uuid_factory or (lambda: str(uuid.uuid4()))
    records: list[dict] = []
    for row in rows:
        value = json.loads(row["value_json"] or "{}")
        meta = json.loads(row["meta_json"] or "{}")
        nutrients = {k: v["value"]
                     for k, v in value.get("nutrients", {}).items()}
        src_local = datetime.fromtimestamp(
            row["effective_start"] / 1000).astimezone()
        when = datetime.combine(target, src_local.time()).astimezone()
        records.extend(build_meal_records(
            value.get("label", ""), meta.get("meal") or "other", nutrients,
            when.isoformat(), make_uuid(),
            amount_g=value.get("amount", {}).get("value"),
            barcode=meta.get("off_barcode")))
    return records


class FoodDialog(Adw.Dialog):
    __gtype_name__ = "VitalsFoodDialog"

    def __init__(self, recorder, settings):
        super().__init__()
        self._recorder = recorder
        self._settings = settings
        self._spins: dict[str, Adw.SpinRow] = {}
        # Set when the food came from a lookup; cleared when the user
        # hand-edits a nutrient (so the portion/product no longer claims
        # to match). `_filling` guards programmatic fills.
        self._amount: float | None = None
        self._barcode: str | None = None
        self._filling = False

        self.set_title("Log Food")
        self.set_content_width(420)

        toolbar = Adw.ToolbarView()
        toolbar.add_top_bar(Adw.HeaderBar())
        self.set_child(toolbar)

        clamp = Adw.Clamp(maximum_size=480, margin_top=12, margin_bottom=18,
                          margin_start=12, margin_end=12)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        clamp.set_child(box)
        scroller = Gtk.ScrolledWindow(
            hscrollbar_policy=Gtk.PolicyType.NEVER, propagate_natural_height=True)
        scroller.set_child(clamp)
        toolbar.set_content(scroller)

        lookup = Gtk.Button(halign=Gtk.Align.CENTER)
        lookup.set_child(Adw.ButtonContent(
            icon_name="edit-find-symbolic", label="Look up a food"))
        lookup.add_css_class("pill")
        lookup.connect("clicked", self._on_lookup)
        box.append(lookup)

        # Recent foods — tap one to fill the form without re-typing.
        self._recent_group = Adw.PreferencesGroup(title="Recent")
        box.append(self._recent_group)
        self._populate_recents()

        group = Adw.PreferencesGroup()

        self._food_row = Adw.EntryRow(title="Food (optional)")
        group.add(self._food_row)

        self._meal_combo = Adw.ComboRow(
            title="Meal", model=Gtk.StringList.new([m[0] for m in MEALS]))
        last = settings.get_string("last-meal")
        if last in _MEAL_KEYS:
            self._meal_combo.set_selected(_MEAL_KEYS.index(last))
        self._meal_combo.connect("notify::selected", self._on_meal_changed)
        group.add(self._meal_combo)

        # When it was eaten — defaults to "now" until the user adjusts it.
        self._time_touched = False
        now = datetime.now()
        self._when_row = Adw.ExpanderRow(title="Time", subtitle="Now")
        self._day_combo = Adw.ComboRow(
            title="Day", model=Gtk.StringList.new(["Today", "Yesterday"]))
        self._hour_spin = Adw.SpinRow(
            title="Hour", adjustment=Gtk.Adjustment(
                lower=0, upper=23, step_increment=1, value=now.hour))
        self._minute_spin = Adw.SpinRow(
            title="Minute", adjustment=Gtk.Adjustment(
                lower=0, upper=59, step_increment=1, value=now.minute))
        self._when_row.add_row(self._day_combo)
        self._when_row.add_row(self._hour_spin)
        self._when_row.add_row(self._minute_spin)
        for widget, signal in ((self._day_combo, "notify::selected"),
                               (self._hour_spin, "notify::value"),
                               (self._minute_spin, "notify::value")):
            widget.connect(signal, self._on_time_changed)
        group.add(self._when_row)

        for nutrient in NUTRIENTS:
            row = Adw.SpinRow(
                title=f"{nutrient['title']} ({nutrient['unit']})",
                digits=nutrient["digits"],
                adjustment=Gtk.Adjustment(
                    lower=0, upper=nutrient["max"],
                    step_increment=nutrient["step"],
                    page_increment=nutrient["step"] * 10, value=0))
            row.connect("notify::value", self._on_nutrient_edited)
            self._spins[nutrient["key"]] = row
            group.add(row)
        box.append(group)

        log_button = Gtk.Button(label="Log food", halign=Gtk.Align.CENTER)
        log_button.add_css_class("suggested-action")
        log_button.add_css_class("pill")
        log_button.connect("clicked", self._on_log)
        box.append(log_button)

    # ── recent foods ──────────────────────────────────────────────
    def _populate_recents(self) -> None:
        items = food_cache.load()[:8]
        self._recent_group.set_visible(bool(items))
        for entry in items:
            row = Adw.ActionRow(title=entry["label"], activatable=True)
            kcal = entry.get("nutrients", {}).get("energy-kcal")
            if kcal:
                row.set_subtitle(f"{int(kcal)} kcal")
            row.add_suffix(Gtk.Image.new_from_icon_name("list-add-symbolic"))
            row.connect("activated", self._on_recent_activated, entry)
            self._recent_group.add(row)

    def _on_recent_activated(self, _row, entry) -> None:
        self._apply_food(entry["label"], entry.get("nutrients", {}),
                         entry.get("amount_g"), entry.get("barcode"))
        self._toast(f"Filled from {entry['label']}")

    # ── consumed-at time ──────────────────────────────────────────
    def _on_time_changed(self, *_):
        self._time_touched = True
        when = self._chosen_when()
        self._when_row.set_subtitle(when.strftime("%a %d %b, %H:%M"))

    def _chosen_when(self) -> datetime:
        """The consumed-at time. Left untouched it tracks the real clock
        so a quick log lands at 'now'; once adjusted it's the picked
        day + time."""
        if not self._time_touched:
            return datetime.now(timezone.utc).astimezone()
        days_ago = self._day_combo.get_selected()
        return (datetime.now().astimezone().replace(
            hour=int(self._hour_spin.get_value()),
            minute=int(self._minute_spin.get_value()),
            second=0, microsecond=0) - timedelta(days=days_ago))

    # ── state ─────────────────────────────────────────────────────
    def _current_meal(self) -> str:
        return _MEAL_KEYS[self._meal_combo.get_selected()]

    def _on_meal_changed(self, *_):
        self._settings.set_string("last-meal", self._current_meal())

    def _on_nutrient_edited(self, *_):
        # A hand-edit means the values no longer match a looked-up portion.
        if not self._filling:
            self._amount = None
            self._barcode = None

    # ── food lookup (Open Food Facts / USDA) ──────────────────────
    def _on_lookup(self, _button):
        from vitals.sources.food_lookup import FoodLookupDialog
        FoodLookupDialog(on_use=self._apply_food,
                         settings=self._settings).present(self)

    def _apply_food(self, label, nutrients, amount_g, barcode):
        """Fill the form from a looked-up food (scaled to the amount)."""
        self._filling = True
        try:
            self._food_row.set_text(label)
            for nutrient in NUTRIENTS:
                self._spins[nutrient["key"]].set_value(
                    nutrients.get(nutrient["key"], 0))
        finally:
            self._filling = False
        self._amount = amount_g
        self._barcode = barcode

    def _entered_nutrients(self) -> dict[str, float]:
        out: dict[str, float] = {}
        for nutrient in NUTRIENTS:
            value = self._spins[nutrient["key"]].get_value()
            if value > 0:
                out[nutrient["key"]] = (round(value) if nutrient["digits"] == 0
                                        else round(value, nutrient["digits"]))
        return out

    # ── logging ───────────────────────────────────────────────────
    def _on_log(self, _button):
        nutrients = self._entered_nutrients()
        if not nutrients:
            self._toast("Enter at least the calories")
            return
        label = self._food_row.get_text().strip()
        when = self._chosen_when()
        records = build_meal_records(
            label, self._current_meal(), nutrients, when.isoformat(),
            str(uuid.uuid4()), amount_g=self._amount, barcode=self._barcode)

        summary = self._recorder.ingest(records)
        if summary["rejected"]:
            self._toast(f"Couldn’t log: {summary['rejected'][0][1]}")
            return
        # Cache the named food so it can be re-added without re-typing.
        food_cache.remember(label, nutrients, self._amount, self._barcode)
        self._toast(f"Logged {label or 'food'}")
        self.close()

    def _toast(self, message: str):
        self.activate_action("win.toast", GLib.Variant("s", message))
