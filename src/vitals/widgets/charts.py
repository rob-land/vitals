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
_ACCENT = (0.21, 0.52, 0.89)


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
