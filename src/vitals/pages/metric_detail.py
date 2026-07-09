"""The metric-detail history view.

Pushed when a Dashboard metric is tapped: a full-height page that lets you
navigate one metric through time at five granularities — Day, Week, Month,
6 Months, Year — with a period pager and a scrub-to-read chart. The heavy
lifting is already in ``Store.aggregate`` (hour/day/week/month buckets,
source-resolved); this page just picks the window + bucket for the chosen
granularity and renders it.

Additive metrics (``interval: true`` in the catalog — steps, energy, water)
show summed bars; point-in-time metrics (``interval: false`` — heart rate,
weight, SpO2) show a min–max range per bucket, the way Apple Health does.

The window/label/bucket math lives in module-level pure functions so it can
be unit-tested without a display.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

from gi.repository import Adw, Gtk

from vitals.core.catalog import Catalog
from vitals.core.store import Store
from vitals.pages import local_tz_name, to_ms
from vitals.widgets.charts import EVENT_COLORS, HistoryChart, LegendDot

GRAINS = ["D", "W", "M", "6M", "Y"]
_GRAIN_TITLES = {"D": "Day", "W": "Week", "M": "Month", "6M": "6 Months",
                 "Y": "Year"}
_UNIT_LABEL = {"/min": "bpm", "{steps}": "steps", "%": "%", "kcal": "kcal",
               "mL": "mL", "kg": "kg", "mmHg": "mmHg", "Cel": "°C",
               "mg/dL": "mg/dL", "km": "km", "m": "m"}

# Logged events overlaid on the chart to correlate with the metric (Oura's
# tags idea). Only at day/week/month zoom — a year would be a smear.
_ANNOTATION_TYPES = ["workout", "caffeine_intake", "alcohol_intake"]
_EVENT_LABELS = {"workout": "Workout", "caffeine_intake": "Caffeine",
                 "alcohol_intake": "Alcohol"}
_EVENT_GRAINS = {"D", "W", "M"}


# ── pure window / bucket helpers ──────────────────────────────────
def _month_add(dt: datetime, months: int) -> datetime:
    """Shift to the first of a month `months` away from `dt`'s month."""
    total = dt.month - 1 + months
    return dt.replace(year=dt.year + total // 12, month=total % 12 + 1, day=1,
                      hour=0, minute=0, second=0, microsecond=0)


def period_window(grain: str, offset: int, now: datetime):
    """(start, end, bucket) for a granularity and a whole-period `offset`
    into the past (0 = the current period). `start` inclusive, `end`
    exclusive, both local datetimes; `bucket` is a Store bucket name."""
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    if grain == "D":
        start = midnight - timedelta(days=offset)
        return start, start + timedelta(days=1), "hour"
    if grain == "W":
        monday = midnight - timedelta(days=midnight.weekday())
        start = monday - timedelta(weeks=offset)
        return start, start + timedelta(days=7), "day"
    if grain == "M":
        start = _month_add(midnight.replace(day=1), -offset)
        return start, _month_add(start, 1), "day"
    if grain == "6M":
        end = _month_add(midnight.replace(day=1), 1 - offset * 6)
        return _month_add(end, -6), end, "week"
    if grain == "Y":
        jan = midnight.replace(month=1, day=1)
        start = jan.replace(year=jan.year - offset)
        return start, start.replace(year=start.year + 1), "month"
    raise ValueError(f"unknown grain {grain!r}")


def period_label(grain: str, start: datetime, end: datetime,
                 now: datetime) -> str:
    last = end - timedelta(days=1)
    if grain == "D":
        d = start.date()
        if d == now.date():
            return "Today"
        if d == (now - timedelta(days=1)).date():
            return "Yesterday"
        return start.strftime("%a %-d %b %Y")
    if grain == "W":
        if start.month == last.month:
            return f"{start.day}–{last.day} {start.strftime('%b %Y')}"
        return f"{start.strftime('%-d %b')} – {last.strftime('%-d %b %Y')}"
    if grain == "M":
        return start.strftime("%B %Y")
    if grain == "6M":
        return f"{start.strftime('%b')} – {last.strftime('%b %Y')}"
    return start.strftime("%Y")


def event_fraction(ev_ms: int, start_ms: int, end_ms: int) -> float:
    """Position 0..1 of an event's timestamp within the window."""
    span = end_ms - start_ms
    return (ev_ms - start_ms) / span if span else 0.0


def offset_for_date(grain: str, target: date, now: datetime) -> int:
    """The period offset whose window contains `target`, for a granularity.
    Clamped to 0 so a future date maps to the current period."""
    if grain == "D":
        return max(0, (now.date() - target).days)
    if grain == "W":
        this_mon = now.date() - timedelta(days=now.weekday())
        tgt_mon = target - timedelta(days=target.weekday())
        return max(0, (this_mon - tgt_mon).days // 7)
    if grain == "M":
        return max(0, (now.year - target.year) * 12 + now.month - target.month)
    if grain == "6M":
        months = (now.year - target.year) * 12 + now.month - target.month
        return max(0, months // 6)
    return max(0, now.year - target.year)  # Y


def bucket_starts(start: datetime, end: datetime, bucket: str) -> list:
    """The ordered bucket-start datetimes spanning [start, end)."""
    out, cur = [], start
    while cur < end:
        out.append(cur)
        if bucket == "hour":
            cur = cur + timedelta(hours=1)
        elif bucket == "day":
            cur = cur + timedelta(days=1)
        elif bucket == "week":
            cur = cur + timedelta(weeks=1)
        else:  # month
            cur = _month_add(cur, 1)
    return out


def norm_key(value, bucket: str) -> str:
    """Normalise an ISO string or datetime to a bucket-matching key, so the
    Store's aggregate rows line up with generated bucket starts."""
    iso = value.isoformat() if isinstance(value, datetime) else value
    return {"hour": iso[:13], "day": iso[:10], "week": iso[:10],
            "month": iso[:7]}[bucket]


def _num(v: float) -> str:
    v = round(v, 1)
    return f"{int(v):,}" if v == int(v) else f"{v:,.1f}"


class MetricDetailPage(Adw.NavigationPage):
    __gtype_name__ = "VitalsMetricDetailPage"

    def __init__(self, store: Store, catalog: Catalog, type_key: str,
                 device_manager=None):
        self._td = catalog.get(type_key)
        super().__init__(title=self._td.title if self._td else type_key)
        self._store = store
        self._type_key = type_key
        self._manager = device_manager
        self._additive = bool(self._td and self._td.additive)
        self._range = self._td.plausible if self._td else None
        self._normal = self._td.normal_range if self._td else None
        self._unit = _UNIT_LABEL.get(self._unit_key(), self._unit_key())
        self._grain = "M"
        self._offset = 0
        self._starts: list[datetime] = []
        self._buckets: list[dict | None] = []

        toolbar = Adw.ToolbarView()
        header = Adw.HeaderBar()
        cal_button = Gtk.MenuButton(icon_name="x-office-calendar-symbolic",
                                    tooltip_text="Jump to a date")
        self._cal_popover = Gtk.Popover()
        self._calendar = Gtk.Calendar()
        self._calendar.connect("day-selected", self._on_calendar_pick)
        self._cal_popover.set_child(self._calendar)
        cal_button.set_popover(self._cal_popover)
        header.pack_end(cal_button)
        toolbar.add_top_bar(header)
        self.set_child(toolbar)

        clamp = Adw.Clamp(maximum_size=620, margin_top=8, margin_bottom=20,
                          margin_start=12, margin_end=12)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        clamp.set_child(box)
        toolbar.set_content(clamp)

        # Granularity segmented control (linked toggle buttons).
        seg = Gtk.Box(halign=Gtk.Align.CENTER, css_classes=["linked"])
        self._grain_buttons: dict[str, Gtk.ToggleButton] = {}
        anchor = None
        for grain in GRAINS:
            btn = Gtk.ToggleButton(label=grain)
            btn.set_tooltip_text(_GRAIN_TITLES[grain])
            if anchor is None:
                anchor = btn
            else:
                btn.set_group(anchor)
            btn.connect("toggled", self._on_grain, grain)
            seg.append(btn)
            self._grain_buttons[grain] = btn
        box.append(seg)

        # Period pager: ◀  label  ▶   Today
        pager = Gtk.Box(spacing=6)
        self._prev = Gtk.Button(icon_name="go-previous-symbolic",
                                css_classes=["flat"])
        self._prev.connect("clicked", lambda _b: self._step(+1))
        self._period = Gtk.Label(hexpand=True)
        self._period.add_css_class("title-4")
        self._next = Gtk.Button(icon_name="go-next-symbolic",
                                css_classes=["flat"])
        self._next.connect("clicked", lambda _b: self._step(-1))
        self._today = Gtk.Button(label="Today", css_classes=["flat"])
        self._today.connect("clicked", lambda _b: self._go_today())
        for w in (self._prev, self._period, self._next, self._today):
            pager.append(w)
        box.append(pager)

        # Readout (summary by default; the scrubbed sample while dragging).
        self._readout = Gtk.Label(xalign=0)
        self._readout.add_css_class("title-2")
        self._readout_sub = Gtk.Label(xalign=0)
        self._readout_sub.add_css_class("dim-label")
        self._readout_sub.add_css_class("caption")
        readbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1,
                          margin_top=4)
        readbox.append(self._readout)
        readbox.append(self._readout_sub)
        box.append(readbox)

        card = Gtk.Box(css_classes=["card"])
        self._chart = HistoryChart()
        self._chart.set_margin_top(8)
        self._chart.set_margin_bottom(6)
        self._chart.set_margin_start(8)
        self._chart.set_margin_end(8)
        card.append(self._chart)
        box.append(card)

        # Legend for event markers, shown only when events are overlaid.
        self._legend = Gtk.Box(spacing=8, halign=Gtk.Align.CENTER, margin_top=2)
        self._legend.set_visible(False)
        box.append(self._legend)

        self._grain_buttons["M"].set_active(True)
        self._reload()

    # ── state ─────────────────────────────────────────────────────
    def _unit_key(self) -> str:
        td = self._td
        if td and td.display_units:
            return td.display_units[0]
        return (td.canonical_unit if td else "") or ""

    def _on_grain(self, btn: Gtk.ToggleButton, grain: str) -> None:
        if btn.get_active():
            self._grain = grain
            self._offset = 0
            self._reload()

    def _step(self, direction: int) -> None:
        self._offset = max(0, self._offset + direction)
        self._reload()

    def _go_today(self) -> None:
        self._offset = 0
        self._reload()

    def _on_calendar_pick(self, calendar) -> None:
        # Only respond to real user picks (the popover is open), not the
        # signal GtkCalendar emits while being set up.
        if not self._cal_popover.get_visible():
            return
        gd = calendar.get_date()
        target = date(gd.get_year(), gd.get_month(), gd.get_day_of_month())
        now = datetime.now().astimezone()
        if target <= now.date():
            self._offset = offset_for_date(self._grain, target, now)
            self._reload()
        self._cal_popover.popdown()

    # ── data → view ───────────────────────────────────────────────
    def _reload(self) -> None:
        now = datetime.now().astimezone()
        start, end, bucket = period_window(self._grain, self._offset, now)
        self._bucket = bucket
        self._starts = bucket_starts(start, end, bucket)
        keys = [norm_key(s, bucket) for s in self._starts]
        tz = local_tz_name()
        trust = (self._manager.source_trust(self._type_key)
                 if self._manager else None)
        s_ms, e_ms = to_ms(start), to_ms(end)

        def agg(op):
            rows = self._store.aggregate(
                self._type_key, op, bucket, start_ms=s_ms, end_ms=e_ms,
                tz=tz, source_trust=trust, value_range=self._range)
            return {norm_key(r["start"], bucket): r for r in rows}

        # Each bucket keeps the aggregate's provenance — the resolved
        # source and sample count — so the scrub readout can show where
        # a value actually came from.
        def provenance(row):
            return {"n": row["n"], "source": row.get("source")}

        if self._additive:
            vals = agg("sum")
            self._buckets = [
                {"value": vals[k]["value"], **provenance(vals[k])}
                if k in vals else None for k in keys]
        else:
            avg, lo, hi = agg("avg"), agg("min"), agg("max")
            self._buckets = [
                {"avg": avg[k]["value"], "low": lo[k]["value"],
                 "high": hi[k]["value"], **provenance(avg[k])}
                if k in avg else None for k in keys]

        goal = self._goal() if self._additive else None
        events = self._fetch_events(s_ms, e_ms)
        self._chart.set_data(self._buckets, mode="bars" if self._additive
                             else "range", ticks=self._ticks(),
                             goal=goal, on_select=self._on_scrub,
                             normal=self._normal, events=events)
        self._update_event_legend(events)
        self._period.set_label(period_label(self._grain, start, end, now))
        self._next.set_sensitive(self._offset > 0)
        self._today.set_visible(self._offset > 0)
        self._show_summary()

    def _goal(self) -> float | None:
        return None  # per-metric goals can hang off settings later

    def _ticks(self) -> list:
        n = len(self._starts)
        if n == 0:
            return []
        idxs = sorted({0, n // 2, n - 1})
        return [(i, self._tick_label(self._starts[i])) for i in idxs]

    def _fetch_events(self, s_ms: int, e_ms: int) -> list[dict]:
        """Context events (workouts, caffeine, alcohol) within the window,
        as chart markers — omitted at the wide 6M/Y zooms."""
        if self._grain not in _EVENT_GRAINS:
            return []
        rows, _ = self._store.read_records(_ANNOTATION_TYPES, start_ms=s_ms,
                                           end_ms=e_ms, limit=2000)
        return [{"frac": event_fraction(r["effective_start"], s_ms, e_ms),
                 "kind": r["type"]} for r in rows]

    def _update_event_legend(self, events: list[dict]) -> None:
        child = self._legend.get_first_child()
        while child is not None:
            nxt = child.get_next_sibling()
            self._legend.remove(child)
            child = nxt
        kinds = []
        for event in events:
            if event["kind"] not in kinds:
                kinds.append(event["kind"])
        self._legend.set_visible(bool(kinds))
        for kind in kinds:
            self._legend.append(LegendDot(EVENT_COLORS.get(kind, (0.5, 0.5, 0.5))))
            label = Gtk.Label(label=_EVENT_LABELS.get(kind, kind))
            label.add_css_class("caption")
            label.add_css_class("dim-label")
            self._legend.append(label)

    def _tick_label(self, dt: datetime) -> str:
        if self._bucket == "hour":
            return dt.strftime("%-H:00")
        if self._bucket == "month":
            return dt.strftime("%b")
        return dt.strftime("%-d %b")

    # ── readout ───────────────────────────────────────────────────
    def _show_summary(self) -> None:
        present = [b for b in self._buckets if b is not None]
        if not present:
            self._readout.set_label("No data")
            self._readout_sub.set_label("Nothing recorded in this period")
            return
        if self._additive:
            total = sum(b["value"] for b in present)
            days = max((self._starts[-1] - self._starts[0]).days + 1, 1) \
                if self._bucket != "hour" else 1
            if self._bucket == "hour":
                self._readout.set_label(f"{_num(total)} {self._unit}")
                self._readout_sub.set_label("total today")
            else:
                self._readout.set_label(f"{_num(total / days)} {self._unit}")
                self._readout_sub.set_label(
                    f"per day on average · {_num(total)} total")
        else:
            avgs = [b["avg"] for b in present if b["avg"] is not None]
            avg = sum(avgs) / len(avgs) if avgs else 0
            lo = min(b["low"] for b in present)
            hi = max(b["high"] for b in present)
            self._readout.set_label(f"{_num(avg)} {self._unit}")
            self._readout_sub.set_label(f"average · range {_num(lo)}–{_num(hi)}")

    def _on_scrub(self, index) -> None:
        if index is None:
            self._show_summary()
            return
        bucket = self._buckets[index]
        parts = [self._bucket_date_label(self._starts[index])]
        if self._additive:
            self._readout.set_label(f"{_num(bucket['value'])} {self._unit}")
        else:
            self._readout.set_label(
                f"{_num(bucket['low'])}–{_num(bucket['high'])} {self._unit}")
            if bucket.get("avg") is not None:
                parts.append(f"avg {_num(bucket['avg'])}")
        parts.append(self._provenance(bucket))
        self._readout_sub.set_label(" · ".join(p for p in parts if p))

    def _provenance(self, bucket: dict) -> str:
        """Where the scrubbed bucket's value came from: its sample count
        and the source the aggregate resolved to."""
        n = bucket.get("n")
        text = f"{n} sample{'s' if n != 1 else ''}" if n else ""
        name = self._source_name(bucket.get("source"))
        if name:
            text = f"{text} · {name}" if text else name
        return text

    def _source_name(self, device_id: str | None) -> str | None:
        if device_id is None:
            return None
        if device_id == "":
            return "Manual entry"
        if self._manager is not None:
            for entry in self._manager.list():
                if entry.address == device_id:
                    return entry.name
        return device_id

    def _bucket_date_label(self, dt: datetime) -> str:
        now = datetime.now().astimezone()
        if self._bucket == "hour":
            return dt.strftime("%-H:00")
        if self._bucket == "day":
            d = dt.date()
            if d == now.date():
                return "Today"
            if d == (now - timedelta(days=1)).date():
                return "Yesterday"
            return dt.strftime("%a %-d %b")
        if self._bucket == "week":
            return "week of " + dt.strftime("%-d %b")
        return dt.strftime("%B %Y")
