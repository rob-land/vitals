"""Open Food Facts lookup — find a food by barcode or name and pull its
nutrients, so you don't type every value by hand.

The product parsing and scaling are pure (Open Food Facts gives nutrients
per 100 g; we scale to the amount eaten). The search dialog runs the HTTP
calls off the main thread and hands the chosen food back to the form,
which fills its fields. Nutrient keys match `form.NUTRIENTS` (the Open
Food Facts `nutriments` names), so they drop straight into the record.
"""

from __future__ import annotations

import json
import logging
import threading
import urllib.parse
import urllib.request
from dataclasses import dataclass, field, replace

from gi.repository import Adw, GLib, Gtk

log = logging.getLogger(__name__)

OFF_BASE = "https://world.openfoodfacts.org"
# Open Food Facts asks API clients to identify themselves.
_USER_AGENT = "Vitals/1.0 (land.rob.vitals)"
_FIELDS = "code,product_name,product_name_en,brands,nutriments,serving_quantity"
_TIMEOUT = 15.0

# Our nutrient key -> the Open Food Facts per-100g nutriment field.
_OFF_PER_100G = {
    "energy-kcal":   "energy-kcal_100g",
    "carbohydrates": "carbohydrates_100g",
    "sugars":        "sugars_100g",
    "fat":           "fat_100g",
    "proteins":      "proteins_100g",
}


@dataclass(frozen=True)
class Food:
    name: str
    brand: str
    barcode: str
    per_100g: dict = field(default_factory=dict)   # our key -> value / 100g
    serving_g: float | None = None
    source: str = ""        # human label of where it came from (e.g. "USDA")

    @property
    def label(self) -> str:
        return f"{self.name} ({self.brand})" if self.brand else self.name


# ── pure parsing / scaling ─────────────────────────────────────────

def is_barcode(query: str) -> bool:
    q = query.strip()
    return q.isdigit() and 8 <= len(q) <= 14


def parse_product(product: dict) -> Food:
    """Pull the nutrients we log out of an Open Food Facts product."""
    nutriments = product.get("nutriments") or {}
    per_100g: dict[str, float] = {}
    for our_key, off_key in _OFF_PER_100G.items():
        value = nutriments.get(off_key)
        if our_key == "energy-kcal" and not isinstance(value, (int, float)):
            value = _kcal_from_kj(nutriments)
        if isinstance(value, (int, float)):
            per_100g[our_key] = float(value)
    name = (product.get("product_name")
            or product.get("product_name_en") or "").strip()
    brand = (product.get("brands") or "").split(",")[0].strip()
    return Food(name=name, brand=brand,
                barcode=str(product.get("code", "")),
                per_100g=per_100g, serving_g=_serving_grams(product))


def _kcal_from_kj(nutriments: dict) -> float | None:
    kj = nutriments.get("energy-kj_100g")
    if not isinstance(kj, (int, float)):
        kj = nutriments.get("energy_100g")  # usually kJ
    return kj / 4.184 if isinstance(kj, (int, float)) else None


def _serving_grams(product: dict) -> float | None:
    try:
        sq = float(product.get("serving_quantity"))
        return sq if sq > 0 else None
    except (TypeError, ValueError):
        return None


def scale_nutrients(per_100g: dict, grams: float) -> dict:
    """Nutrient amounts for `grams` of a food (its per-100g values scaled).
    Calories round to whole; grams to one decimal."""
    factor = grams / 100.0
    return {k: (round(v * factor) if k == "energy-kcal" else round(v * factor, 1))
            for k, v in per_100g.items()}


# ── network (off the main thread) ──────────────────────────────────

def lookup_barcode(code: str) -> Food | None:
    url = f"{OFF_BASE}/api/v2/product/{code}.json?fields={_FIELDS}"
    data = _get_json(url)
    if data.get("status") == 1 and data.get("product"):
        return parse_product(data["product"])
    return None


def search(query: str, limit: int = 20) -> list[Food]:
    params = urllib.parse.urlencode({
        "search_terms": query, "search_simple": 1, "json": 1,
        "page_size": limit, "fields": _FIELDS})
    data = _get_json(f"{OFF_BASE}/cgi/search.pl?{params}")
    foods = [parse_product(p) for p in (data.get("products") or [])]
    return [f for f in foods if f.name and f.per_100g]


def _get_json(url: str) -> dict:
    log.info("Open Food Facts: GET %s", url)
    req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
        return json.load(resp)


# ── pluggable nutrition sources ────────────────────────────────────

class Source:
    """A nutrition database Vitals can search. Implementations run their
    HTTP off the main thread (the dialog handles that) and return `Food`s
    with `per_100g` nutrients keyed to `form.NUTRIENTS`."""
    id: str = ""
    name: str = ""

    def available(self) -> bool:
        return True

    def search(self, query: str) -> list[Food]:
        raise NotImplementedError

    def lookup_barcode(self, code: str) -> Food | None:
        return None


class OpenFoodFactsSource(Source):
    id = "off"
    name = "Open Food Facts"

    def search(self, query: str) -> list[Food]:
        return [replace(f, source=self.name) for f in search(query)]

    def lookup_barcode(self, code: str) -> Food | None:
        food = lookup_barcode(code)
        return replace(food, source=self.name) if food else None


def available_sources(settings) -> list[Source]:
    """The nutrition sources to offer, in display order. Open Food Facts
    (packaged / barcoded foods) is always present; USDA FoodData Central
    adds restaurant, fast-food and generic/prepared foods."""
    sources: list[Source] = [OpenFoodFactsSource()]
    try:
        from vitals.sources.usda import UsdaSource
        sources.append(UsdaSource(settings))
    except Exception:  # a broken optional source mustn't break lookup
        log.exception("USDA source unavailable")
    return sources


# ── search dialog ──────────────────────────────────────────────────

class FoodLookupDialog(Adw.Dialog):
    __gtype_name__ = "VitalsFoodLookupDialog"

    def __init__(self, on_use, settings=None):
        super().__init__()
        self._on_use = on_use
        self._closed = False
        self.connect("closed", lambda *_: setattr(self, "_closed", True))
        self._gen = 0
        self._food: Food | None = None
        self._sources = available_sources(settings)

        self.set_title("Look up a food")
        self.set_content_width(400)
        self.set_content_height(540)

        toolbar = Adw.ToolbarView()
        toolbar.add_top_bar(Adw.HeaderBar())
        self._stack = Gtk.Stack()
        self._stack.set_transition_type(Gtk.StackTransitionType.CROSSFADE)
        toolbar.set_content(self._stack)
        self.set_child(toolbar)

        self._build_search_page()
        self._build_loading_page()
        self._build_detail_page()
        self._build_empty_page()
        self._stack.set_visible_child_name("search")

    # ── pages ─────────────────────────────────────────────────────

    def _build_search_page(self) -> None:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        box.set_margin_top(12)
        box.set_margin_bottom(12)
        box.set_margin_start(12)
        box.set_margin_end(12)

        search_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self._entry = Gtk.SearchEntry(hexpand=True)
        self._entry.set_placeholder_text("Barcode or food name")
        self._entry.connect("activate", lambda *_: self._do_search())
        search_row.append(self._entry)
        # Only offer a source picker when there's a choice to make.
        self._source_dropdown = None
        if len(self._sources) > 1:
            self._source_dropdown = Gtk.DropDown.new_from_strings(
                [s.name for s in self._sources])
            self._source_dropdown.set_tooltip_text("Nutrition source")
            self._source_dropdown.connect(
                "notify::selected", lambda *_: self._update_hint())
            search_row.append(self._source_dropdown)
        box.append(search_row)

        self._hint = Gtk.Label(xalign=0, wrap=True)
        self._hint.add_css_class("dim-label")
        self._hint.add_css_class("caption")
        box.append(self._hint)
        self._update_hint()

        scroller = Gtk.ScrolledWindow(vexpand=True)
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self._results = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        self._results.add_css_class("boxed-list")
        self._results.connect("row-activated", self._on_result_activated)
        scroller.set_child(self._results)
        box.append(scroller)
        self._stack.add_named(box, "search")

    def _build_loading_page(self) -> None:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, valign=Gtk.Align.CENTER,
                      vexpand=True)
        box.append(Adw.Spinner())
        self._stack.add_named(box, "loading")

    def _build_detail_page(self) -> None:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        box.set_margin_top(18)
        box.set_margin_bottom(18)
        box.set_margin_start(18)
        box.set_margin_end(18)
        self._detail_title = Gtk.Label(wrap=True, justify=Gtk.Justification.CENTER)
        self._detail_title.add_css_class("title-3")
        box.append(self._detail_title)

        group = Adw.PreferencesGroup()
        self._amount_row = Adw.SpinRow(
            title="Amount (g)", digits=0,
            adjustment=Gtk.Adjustment(lower=1, upper=5000, step_increment=10,
                                      page_increment=50, value=100))
        self._amount_row.connect("notify::value", lambda *_: self._refresh_preview())
        group.add(self._amount_row)
        self._preview_group = Adw.PreferencesGroup(title="Nutrients")
        box.append(group)
        box.append(self._preview_group)
        self._preview_rows: list[Gtk.Widget] = []

        use = Gtk.Button(label="Use this food", halign=Gtk.Align.CENTER)
        use.add_css_class("suggested-action")
        use.add_css_class("pill")
        use.connect("clicked", lambda *_: self._use())
        box.append(use)
        self._stack.add_named(box, "detail")

    def _build_empty_page(self) -> None:
        self._empty = Adw.StatusPage(
            icon_name="edit-find-symbolic", title="No matches",
            description="Try a different name or barcode.", vexpand=True)
        self._stack.add_named(self._empty, "empty")

    # ── search ────────────────────────────────────────────────────

    def _selected_source(self) -> Source:
        if self._source_dropdown is not None:
            return self._sources[self._source_dropdown.get_selected()]
        return self._sources[0]

    def _update_hint(self) -> None:
        source = self._selected_source()
        if source.id == "usda":
            self._hint.set_text("USDA also has restaurant, fast-food and "
                                "generic prepared foods.")
            self._hint.set_visible(True)
        else:
            self._hint.set_visible(False)

    def _do_search(self) -> None:
        query = self._entry.get_text().strip()
        if not query:
            return
        source = self._selected_source()
        self._gen += 1
        gen = self._gen
        self._stack.set_visible_child_name("loading")
        barcode = is_barcode(query)

        def work() -> None:
            try:
                if barcode:
                    food = source.lookup_barcode(query)
                    results = [food] if food else []
                else:
                    results = source.search(query)
                results = [f for f in results if f is not None]
            except Exception as exc:  # noqa: BLE001
                log.exception("Food lookup failed")
                GLib.idle_add(self._show_error, gen, source.name, str(exc))
                return
            GLib.idle_add(self._on_results, gen, results)

        threading.Thread(target=work, name="vitals-lookup", daemon=True).start()

    def _on_results(self, gen: int, foods: list[Food]) -> bool:
        if gen != self._gen or self._closed:
            return False
        if not foods:
            self._empty.set_title("No matches")
            self._empty.set_description("Try a different name or barcode.")
            self._stack.set_visible_child_name("empty")
            return False
        if len(foods) == 1:
            self._show_detail(foods[0])
            return False
        child = self._results.get_first_child()
        while child:
            self._results.remove(child)
            child = self._results.get_first_child()
        for food in foods:
            row = Adw.ActionRow(activatable=True, title=food.name,
                                subtitle=food.brand or "")
            row.add_suffix(Gtk.Image.new_from_icon_name("go-next-symbolic"))
            row._vitals_food = food
            self._results.append(row)
        self._stack.set_visible_child_name("search")
        return False

    def _show_error(self, gen: int, source_name: str, message: str) -> bool:
        if gen != self._gen or self._closed:
            return False
        self._empty.set_title(f"Couldn’t reach {source_name}")
        description = message
        if any(s in message for s in
               ("API_KEY", "OVER_RATE", "rate limit", "403", "429")):
            description = ("USDA’s shared demo key is rate-limited — add "
                           "your own free key in Preferences.")
        self._empty.set_description(description)
        self._stack.set_visible_child_name("empty")
        return False

    def _on_result_activated(self, _listbox, row) -> None:
        food = getattr(row, "_vitals_food", None)
        if food is not None:
            self._show_detail(food)

    # ── detail ────────────────────────────────────────────────────

    def _show_detail(self, food: Food) -> None:
        self._food = food
        self._detail_title.set_text(food.label)
        self._amount_row.set_value(food.serving_g or 100)
        self._refresh_preview()
        self._stack.set_visible_child_name("detail")

    def _refresh_preview(self) -> None:
        for row in self._preview_rows:
            self._preview_group.remove(row)
        self._preview_rows = []
        if self._food is None:
            return
        nutrients = scale_nutrients(self._food.per_100g,
                                    self._amount_row.get_value())
        from vitals.sources.food import NUTRIENTS
        for nutrient in NUTRIENTS:
            value = nutrients.get(nutrient["key"])
            if value is None:
                continue
            row = Adw.ActionRow(title=nutrient["title"])
            shown = int(value) if nutrient["digits"] == 0 else value
            label = Gtk.Label(label=f"{shown} {nutrient['unit']}")
            label.add_css_class("numeric")
            row.add_suffix(label)
            self._preview_group.add(row)
            self._preview_rows.append(row)

    def _use(self) -> None:
        if self._food is None:
            return
        grams = self._amount_row.get_value()
        nutrients = scale_nutrients(self._food.per_100g, grams)
        self._on_use(self._food.label, nutrients, grams, self._food.barcode)
        self.close()
