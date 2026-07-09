"""Cairo chart widgets — the cohort's shared health-charting primitives.

``ActivityRing`` draws a circular progress arc (steps toward a goal);
``BarChart`` draws a daily bar series with an optional goal line. Both are
plain ``Gtk.DrawingArea``s driven by ``set_draw_func`` and fed data via
setters, so the data prep stays testable in ``format`` and the widgets only
draw.
"""

from __future__ import annotations

import math

from gi.repository import Gtk

from vitals.format import format_value, nice_max

# A pleasant fixed accent; the track/labels derive from the theme fg colour.
# ACCENT is the activity colour (steps, energy burned); ACCENT2 the intake
# colour (food). The pair is CVD-validated on light and dark surfaces
# (#3685E3 / #D97706, ΔE ≥ 111 under protan/deutan simulation).
ACCENT = (0.21, 0.52, 0.89)
ACCENT2 = (0.851, 0.467, 0.024)
_ACCENT = ACCENT
# Semantic green for the "normal range" band (separate from the accent).
_NORMAL = (0.09, 0.56, 0.34)
# Colours for event markers overlaid on a metric chart (Oura-style tags).
EVENT_COLORS = {
    "workout": (0.21, 0.52, 0.89),
    "caffeine_intake": (0.55, 0.36, 0.17),
    "alcohol_intake": (0.55, 0.34, 0.72),
}


class ActivityRing(Gtk.DrawingArea):
    __gtype_name__ = "VitalsActivityRing"

    def __init__(self):
        super().__init__()
        self._fraction = 0.0
        self.set_content_width(180)
        self.set_content_height(180)
        self.set_draw_func(self._draw)

    def set_fraction(self, fraction: float) -> None:
        self._fraction = max(0.0, float(fraction))
        self.queue_draw()

    def _draw(self, _area, cr, width, height, *_):
        cx, cy = width / 2, height / 2
        radius = min(width, height) / 2 - 12
        thickness = 16
        fg = self.get_color()

        # Track.
        cr.set_line_width(thickness)
        cr.set_line_cap(1)  # cairo.LINE_CAP_ROUND
        cr.set_source_rgba(fg.red, fg.green, fg.blue, 0.12)
        cr.arc(cx, cy, radius, 0, 2 * math.pi)
        cr.stroke()

        # Progress arc, clockwise from the top (12 o'clock).
        if self._fraction > 0:
            start = -math.pi / 2
            end = start + 2 * math.pi * min(self._fraction, 1.0)
            cr.set_source_rgb(*_ACCENT)
            cr.arc(cx, cy, radius, start, end)
            cr.stroke()


class BarChart(Gtk.DrawingArea):
    __gtype_name__ = "VitalsBarChart"

    def __init__(self):
        super().__init__()
        self._values: list[float | None] = []
        self._goal: float | None = None
        self.set_content_height(180)
        self.set_hexpand(True)
        self.set_draw_func(self._draw)

    def set_data(self, values, goal: float | None = None) -> None:
        self._values = list(values)
        self._goal = goal
        self.queue_draw()

    def _draw(self, _area, cr, width, height, *_):
        fg = self.get_color()
        pad_top, pad_bottom = 8, 6
        plot_h = height - pad_top - pad_bottom

        present = [v for v in self._values if v is not None]
        if not present:
            cr.set_source_rgba(fg.red, fg.green, fg.blue, 0.45)
            cr.select_font_face("Sans")
            cr.set_font_size(13)
            cr.move_to(8, height / 2)
            cr.show_text("No data yet")
            return

        top = nice_max(present, floor=self._goal or 0.0)
        n = len(self._values)
        slot = width / n
        bar_w = slot * 0.6
        gap = (slot - bar_w) / 2

        # Baseline.
        cr.set_source_rgba(fg.red, fg.green, fg.blue, 0.15)
        cr.set_line_width(1)
        cr.move_to(0, pad_top + plot_h)
        cr.line_to(width, pad_top + plot_h)
        cr.stroke()

        # Bars.
        cr.set_source_rgb(*_ACCENT)
        for i, value in enumerate(self._values):
            if value is None:
                continue
            h = plot_h * (value / top)
            x = i * slot + gap
            y = pad_top + plot_h - h
            cr.rectangle(x, y, bar_w, h)
            cr.fill()

        # Goal line.
        if self._goal:
            gy = pad_top + plot_h - plot_h * min(self._goal / top, 1.0)
            cr.set_source_rgba(fg.red, fg.green, fg.blue, 0.5)
            cr.set_line_width(1)
            cr.set_dash([4, 3])
            cr.move_to(0, gy)
            cr.line_to(width, gy)
            cr.stroke()
            cr.set_dash([])
            cr.select_font_face("Sans")
            cr.set_font_size(10)
            cr.move_to(width - 28, max(gy - 3, pad_top + 9))
            cr.show_text("goal")

        # Axis maximum (top-left).
        cr.set_source_rgba(fg.red, fg.green, fg.blue, 0.5)
        cr.select_font_face("Sans")
        cr.set_font_size(11)
        cr.move_to(2, pad_top + 9)
        cr.show_text(format_value(top))


class LegendDot(Gtk.DrawingArea):
    """A small filled circle carrying a series colour next to its label
    (identity is never colour-alone: the label always accompanies it)."""

    __gtype_name__ = "VitalsLegendDot"

    def __init__(self, rgb: tuple[float, float, float]):
        super().__init__()
        self._rgb = rgb
        self.set_content_width(10)
        self.set_content_height(10)
        self.set_valign(Gtk.Align.CENTER)
        self.set_draw_func(self._draw)

    def _draw(self, _area, cr, width, height, *_):
        cr.set_source_rgb(*self._rgb)
        cr.arc(width / 2, height / 2, min(width, height) / 2, 0, 2 * math.pi)
        cr.fill()


class GroupedBarChart(Gtk.DrawingArea):
    """Paired daily bars for two same-unit series (calories in vs out).

    One shared axis — both series must be in the same unit; the widget
    draws marks only, the page provides the legend labels."""

    __gtype_name__ = "VitalsGroupedBarChart"

    def __init__(self):
        super().__init__()
        self._a: list[float | None] = []
        self._b: list[float | None] = []
        self.set_content_height(180)
        self.set_hexpand(True)
        self.set_draw_func(self._draw)

    def set_data(self, series_a, series_b) -> None:
        """Two equal-length series; index i is one day's pair."""
        self._a = list(series_a)
        self._b = list(series_b)
        self.queue_draw()

    def _draw(self, _area, cr, width, height, *_):
        fg = self.get_color()
        pad_top, pad_bottom = 8, 6
        plot_h = height - pad_top - pad_bottom

        present = [v for v in self._a + self._b if v]
        if not present:
            cr.set_source_rgba(fg.red, fg.green, fg.blue, 0.45)
            cr.select_font_face("Sans")
            cr.set_font_size(13)
            cr.move_to(8, height / 2)
            cr.show_text("No data yet")
            return

        top = nice_max(present)
        n = max(len(self._a), len(self._b))
        slot = width / n
        pair_w = slot * 0.66
        bar_w = (pair_w - 2) / 2  # 2px gap inside the pair
        gap = (slot - pair_w) / 2

        # Baseline.
        cr.set_source_rgba(fg.red, fg.green, fg.blue, 0.15)
        cr.set_line_width(1)
        cr.move_to(0, pad_top + plot_h)
        cr.line_to(width, pad_top + plot_h)
        cr.stroke()

        for series, colour, offset in ((self._a, ACCENT2, 0),
                                       (self._b, ACCENT, bar_w + 2)):
            cr.set_source_rgb(*colour)
            for i, value in enumerate(series):
                if not value:
                    continue
                h = plot_h * min(value / top, 1.0)
                x = i * slot + gap + offset
                cr.rectangle(x, pad_top + plot_h - h, bar_w, h)
                cr.fill()

        # Axis maximum (top-left).
        cr.set_source_rgba(fg.red, fg.green, fg.blue, 0.5)
        cr.select_font_face("Sans")
        cr.set_font_size(11)
        cr.move_to(2, pad_top + 9)
        cr.show_text(format_value(top))


class LineChart(Gtk.DrawingArea):
    """A line/area chart for metrics that vary around a value (weight, heart
    rate, glucose) rather than accumulate. Auto-scales to the data range."""

    __gtype_name__ = "VitalsLineChart"

    def __init__(self):
        super().__init__()
        self._values: list[float | None] = []
        self.set_content_height(180)
        self.set_hexpand(True)
        self.set_draw_func(self._draw)

    def set_data(self, values) -> None:
        self._values = list(values)
        self.queue_draw()

    def _draw(self, _area, cr, width, height, *_):
        fg = self.get_color()
        pad_top, pad_bottom, pad_left = 10, 18, 4
        plot_h = height - pad_top - pad_bottom

        present = [v for v in self._values if v is not None]
        if len(present) < 1:
            cr.set_source_rgba(fg.red, fg.green, fg.blue, 0.45)
            cr.select_font_face("Sans")
            cr.set_font_size(13)
            cr.move_to(8, height / 2)
            cr.show_text("No data yet")
            return

        lo, hi = min(present), max(present)
        span = (hi - lo) or max(abs(hi) * 0.1, 1.0)
        lo -= span * 0.1
        hi += span * 0.1
        rng = hi - lo

        n = len(self._values)
        step = (width - pad_left) / max(n - 1, 1)

        def xy(i, value):
            x = pad_left + i * step
            y = pad_top + plot_h * (1 - (value - lo) / rng)
            return x, y

        # Area fill under the line.
        points = [(i, v) for i, v in enumerate(self._values) if v is not None]
        cr.set_source_rgba(*_ACCENT, 0.12)
        cr.move_to(*xy(*points[0]))
        for i, v in points:
            cr.line_to(*xy(i, v))
        last_x = xy(points[-1][0], points[-1][1])[0]
        cr.line_to(last_x, pad_top + plot_h)
        cr.line_to(xy(*points[0])[0], pad_top + plot_h)
        cr.close_path()
        cr.fill()

        # The line itself.
        cr.set_source_rgb(*_ACCENT)
        cr.set_line_width(2)
        cr.set_line_join(1)  # round
        cr.move_to(*xy(*points[0]))
        for i, v in points:
            cr.line_to(*xy(i, v))
        cr.stroke()

        # End-point dot + latest value.
        ex, ey = xy(*points[-1])
        cr.arc(ex, ey, 3, 0, 2 * 3.14159)
        cr.fill()

        # Min / max labels.
        cr.set_source_rgba(fg.red, fg.green, fg.blue, 0.5)
        cr.select_font_face("Sans")
        cr.set_font_size(11)
        cr.move_to(2, pad_top + 9)
        cr.show_text(format_value(max(present)))
        cr.move_to(2, pad_top + plot_h)
        cr.show_text(format_value(min(present)))


class HistoryChart(Gtk.DrawingArea):
    """Interactive bucketed chart for the metric-detail history view.

    Renders a series of buckets as either **sum bars** (additive metrics)
    or **min–max range bars** (point-in-time metrics), and supports
    *scrubbing*: hovering (mouse) or press-dragging (touch) highlights the
    nearest bucket and calls ``on_select(index)`` so the page can show that
    bucket's value in a live readout. Gaps (``None`` buckets) render as
    empty slots and are never interpolated. Each bucket is a dict:

      * bars   → ``{"value": float}``
      * range  → ``{"low": float, "high": float, "avg": float | None}``
    """

    __gtype_name__ = "VitalsHistoryChart"

    def __init__(self):
        super().__init__()
        self._buckets: list[dict | None] = []
        self._mode = "bars"
        self._ticks: list[tuple[int, str]] = []
        self._goal: float | None = None
        self._normal: tuple[float, float] | None = None
        self._events: list[dict] = []
        self._selected: int | None = None
        self._on_select = None
        self._drag_x = 0.0
        self.set_content_height(210)
        self.set_hexpand(True)
        self.set_draw_func(self._draw)

        motion = Gtk.EventControllerMotion()
        motion.connect("motion", lambda _c, x, _y: self._select_at(x))
        motion.connect("leave", lambda _c: self._clear_selection())
        self.add_controller(motion)

        drag = Gtk.GestureDrag()
        drag.connect("drag-begin", self._on_drag_begin)
        drag.connect("drag-update", self._on_drag_update)
        self.add_controller(drag)

    def set_data(self, buckets, mode="bars", ticks=None, goal=None,
                 on_select=None, normal=None, events=None) -> None:
        self._buckets = list(buckets)
        self._mode = mode
        self._ticks = list(ticks or [])
        self._goal = goal
        self._normal = normal
        self._events = list(events or [])
        self._on_select = on_select
        self._selected = None
        self.queue_draw()

    # ── scrubbing ─────────────────────────────────────────────────
    def _on_drag_begin(self, _g, x, _y):
        self._drag_x = x
        self._select_at(x)

    def _on_drag_update(self, _g, dx, _dy):
        self._select_at(self._drag_x + dx)

    def _select_at(self, x: float) -> None:
        width = self.get_width()
        if width <= 0 or not self._buckets:
            return
        slot = width / len(self._buckets)
        i = max(0, min(len(self._buckets) - 1, int(x // slot)))
        if self._buckets[i] is None:
            i = self._nearest_present(i)
        if i is not None and i != self._selected:
            self._selected = i
            self.queue_draw()
            if self._on_select:
                self._on_select(i)

    def _nearest_present(self, i: int) -> int | None:
        for step in range(len(self._buckets)):
            for j in (i - step, i + step):
                if 0 <= j < len(self._buckets) and self._buckets[j] is not None:
                    return j
        return None

    def _clear_selection(self) -> None:
        if self._selected is not None:
            self._selected = None
            self.queue_draw()
        if self._on_select:
            self._on_select(None)

    # ── drawing ───────────────────────────────────────────────────
    def _draw(self, _area, cr, width, height, *_):
        fg = self.get_color()
        pad_top, pad_bottom = 12, 20
        plot_h = height - pad_top - pad_bottom
        baseline = pad_top + plot_h

        present = [b for b in self._buckets if b is not None]
        if not present:
            cr.set_source_rgba(fg.red, fg.green, fg.blue, 0.45)
            cr.select_font_face("Sans")
            cr.set_font_size(13)
            cr.move_to(8, height / 2)
            cr.show_text("No data in this period")
            return

        n = len(self._buckets)
        slot = width / n

        cr.set_source_rgba(fg.red, fg.green, fg.blue, 0.15)
        cr.set_line_width(1)
        cr.move_to(0, baseline)
        cr.line_to(width, baseline)
        cr.stroke()

        if self._mode == "range":
            self._draw_range(cr, present, slot, plot_h, baseline, fg, width)
        else:
            self._draw_bars(cr, present, slot, plot_h, baseline, fg, width)

        # Event markers (Oura-style tags) along the time axis.
        for ev in self._events:
            ex = min(max(ev["frac"] * width, 2), width - 2)
            colour = EVENT_COLORS.get(ev["kind"], (fg.red, fg.green, fg.blue))
            cr.set_source_rgba(*colour, 0.16)
            cr.set_line_width(1)
            cr.move_to(ex, pad_top)
            cr.line_to(ex, baseline)
            cr.stroke()
            cr.set_source_rgba(*colour, 0.95)
            cr.arc(ex, baseline, 2.5, 0, 2 * math.pi)
            cr.fill()

        # Scrub caret.
        if self._selected is not None:
            sx = self._selected * slot + slot / 2
            cr.set_source_rgba(*_ACCENT, 0.55)
            cr.set_line_width(1)
            cr.set_dash([2, 2])
            cr.move_to(sx, pad_top)
            cr.line_to(sx, baseline)
            cr.stroke()
            cr.set_dash([])

        # X-axis ticks.
        cr.set_source_rgba(fg.red, fg.green, fg.blue, 0.55)
        cr.select_font_face("Sans")
        cr.set_font_size(10)
        for idx, label in self._ticks:
            tx = idx * slot + slot / 2
            ext = cr.text_extents(label)
            cr.move_to(min(max(tx - ext.width / 2, 1), width - ext.width - 1),
                       height - 6)
            cr.show_text(label)

    def _draw_bars(self, cr, present, slot, plot_h, baseline, fg, width):
        top = nice_max([b["value"] for b in present], floor=self._goal or 0.0)
        bar_w = slot * 0.62
        for i, b in enumerate(self._buckets):
            if b is None:
                continue
            h = plot_h * (b["value"] / top) if top else 0
            x = i * slot + (slot - bar_w) / 2
            cr.set_source_rgba(*_ACCENT, 1.0 if i == self._selected else 0.82)
            cr.rectangle(x, baseline - h, bar_w, h)
            cr.fill()
        if self._goal:
            gy = baseline - plot_h * min(self._goal / top, 1.0)
            cr.set_source_rgba(fg.red, fg.green, fg.blue, 0.5)
            cr.set_line_width(1)
            cr.set_dash([4, 3])
            cr.move_to(0, gy)
            cr.line_to(width, gy)
            cr.stroke()
            cr.set_dash([])
        cr.set_source_rgba(fg.red, fg.green, fg.blue, 0.5)
        cr.select_font_face("Sans")
        cr.set_font_size(11)
        cr.move_to(2, 11)
        cr.show_text(format_value(top))

    def _draw_range(self, cr, present, slot, plot_h, baseline, fg, width):
        lows = [b["low"] for b in present]
        highs = [b["high"] for b in present]
        lo, hi = min(lows), max(highs)
        if self._normal:  # keep the reference band in view
            lo, hi = min(lo, self._normal[0]), max(hi, self._normal[1])
        span = (hi - lo) or max(abs(hi) * 0.1, 1.0)
        lo -= span * 0.1
        hi += span * 0.1
        rng = hi - lo
        bar_w = max(slot * 0.2, 3.0)

        def y_of(v):
            return baseline - plot_h * ((v - lo) / rng)

        # Normal-range band behind the data.
        if self._normal:
            y_hi, y_lo = y_of(self._normal[1]), y_of(self._normal[0])
            cr.set_source_rgba(*_NORMAL, 0.10)
            cr.rectangle(0, y_hi, width, y_lo - y_hi)
            cr.fill()
            cr.set_source_rgba(*_NORMAL, 0.35)
            cr.set_line_width(1)
            cr.set_dash([3, 3])
            for yy in (y_hi, y_lo):
                cr.move_to(0, yy)
                cr.line_to(width, yy)
                cr.stroke()
            cr.set_dash([])

        cr.set_line_cap(1)  # round
        for i, b in enumerate(self._buckets):
            if b is None:
                continue
            x = i * slot + slot / 2
            sel = i == self._selected
            cr.set_source_rgba(*_ACCENT, 1.0 if sel else 0.62)
            cr.set_line_width(bar_w + (1.5 if sel else 0))
            cr.move_to(x, y_of(b["high"]))
            cr.line_to(x, y_of(b["low"]))
            cr.stroke()
            if b.get("avg") is not None:
                cr.set_source_rgba(fg.red, fg.green, fg.blue, 0.85)
                cr.arc(x, y_of(b["avg"]), 1.6, 0, 2 * math.pi)
                cr.fill()
        cr.set_source_rgba(fg.red, fg.green, fg.blue, 0.5)
        cr.select_font_face("Sans")
        cr.set_font_size(11)
        cr.move_to(2, 11)
        cr.show_text(format_value(hi))
        cr.move_to(2, baseline)
        cr.show_text(format_value(lo))
