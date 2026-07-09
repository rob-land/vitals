"""Pure dashboard-insight math: honest trends, the weekly recap rows
and data-freshness fading.

An *honest* trend never compares a partial period against a complete
one — today's half-day of steps would always "lose" against a full
yesterday. Every comparison here is between two equal, completed
windows, and a trend is only claimed when both windows have enough
recorded days to mean something; otherwise there is no trend at all,
which beats a misleading one. No GTK imports on purpose: this module
is unit-tested next to ``format``.
"""

from __future__ import annotations

from vitals.format import format_value

# Recorded days each week needs before a week-on-week trend is claimed.
MIN_COVERAGE = 4
# Relative change below which a trend reads as "level".
FLAT_THRESHOLD = 0.03

# Freshness fade: fully opaque up to FRESH_HOURS, then fading linearly
# to STALE_FLOOR at STALE_FULL_HOURS. The floor keeps stale cards
# readable — the fade is a nudge, not a redaction.
FRESH_HOURS = 6.0
STALE_FULL_HOURS = 48.0
STALE_FLOOR = 0.55

# The weekly recap: per-metric semantics over one week of day buckets.
#   per-day  → mean of the recorded days' daily sums (additive metrics)
#   average  → mean of the recorded days' daily averages
#   change   → last recorded value minus first (weight over the week)
RECAP_METRICS = [
    {"key": "step_count",     "title": "Steps",           "mode": "per-day",
     "unit": "{steps}", "label": "steps"},
    {"key": "dietary_energy", "title": "Calories eaten",  "mode": "per-day",
     "unit": "kcal", "label": "kcal"},
    {"key": "active_energy",  "title": "Calories burned", "mode": "per-day",
     "unit": "kcal", "label": "kcal"},
    {"key": "water_intake",   "title": "Water",           "mode": "per-day",
     "unit": "mL", "label": "mL"},
    {"key": "heart_rate",     "title": "Heart rate",      "mode": "average",
     "unit": "/min", "label": "bpm"},
    {"key": "body_weight",    "title": "Weight",          "mode": "change",
     "unit": "kg", "label": "kg"},
]


def _mean(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def trend_between(current: float | None, previous: float | None,
                  current_n: int = MIN_COVERAGE,
                  previous_n: int = MIN_COVERAGE) -> dict | None:
    """Week-on-week trend between two completed-window stats, or None
    when either side is missing or built from too few recorded days
    (``*_n`` counts) to be honest about."""
    if current is None or previous is None:
        return None
    if current_n < MIN_COVERAGE or previous_n < MIN_COVERAGE:
        return None
    denom = abs(previous)
    if denom == 0:
        return None
    ratio = (current - previous) / denom
    if abs(ratio) < FLAT_THRESHOLD:
        direction = "flat"
    else:
        direction = "up" if ratio > 0 else "down"
    return {"current": current, "previous": previous,
            "ratio": ratio, "direction": direction}


def weekly_trend(daily: list[float | None]) -> dict | None:
    """Trend of the last 7 *complete* days against the 7 before.

    ``daily`` is 14 day-values oldest → newest, ending with yesterday
    (today is excluded — it isn't over yet); gaps are None. Returns the
    ``trend_between`` dict, or None when either week has fewer than
    ``MIN_COVERAGE`` recorded days."""
    if len(daily) != 14:
        raise ValueError("weekly_trend wants exactly 14 complete days")
    prev = [v for v in daily[:7] if v is not None]
    cur = [v for v in daily[7:] if v is not None]
    return trend_between(_mean(cur), _mean(prev), len(cur), len(prev))


def format_trend(trend: dict | None) -> str:
    """Render a trend for a caption: '▲ 12% vs prior week'. Empty when
    there is no honest trend to report."""
    if trend is None:
        return ""
    if trend["direction"] == "flat":
        return "≈ level vs prior week"
    arrow = "▲" if trend["direction"] == "up" else "▼"
    return f"{arrow} {abs(trend['ratio']):.0%} vs prior week"


# ── weekly recap ──────────────────────────────────────────────────
def week_stat(daily: list[float | None], mode: str) -> tuple[float | None, int]:
    """(value, recorded-day count) for one week of day buckets under a
    recap mode. ``change`` needs at least two readings."""
    present = [v for v in daily if v is not None]
    if mode == "change":
        return (present[-1] - present[0] if len(present) >= 2 else None,
                len(present))
    return _mean(present), len(present)


def recap_rows(week: dict[str, list[float | None]],
               prior: dict[str, list[float | None]]) -> list[dict]:
    """Display rows for the weekly recap card.

    ``week`` / ``prior`` map a type key to its 7 day buckets (None for
    gaps) for the last completed week and the one before. Metrics with
    nothing recorded last week are dropped; the trend is honest — shown
    only when both weeks carry ``MIN_COVERAGE`` recorded days.
    Each row: ``{title, value_text, trend_text}``."""
    rows = []
    for spec in RECAP_METRICS:
        value, n = week_stat(week.get(spec["key"], []), spec["mode"])
        if value is None:
            continue
        if spec["mode"] == "change":
            value_text = (f"{value:+,.1f} {spec['label']} over the week")
            trend_text = ""
        else:
            prev, prev_n = week_stat(prior.get(spec["key"], []), spec["mode"])
            value_text = format_value(value, spec["unit"]) + f" {spec['label']}"
            if spec["mode"] == "per-day":
                value_text += " / day"
            trend_text = format_trend(trend_between(value, prev, n, prev_n))
        rows.append({"title": spec["title"], "value_text": value_text,
                     "trend_text": trend_text})
    return rows


# ── freshness ─────────────────────────────────────────────────────
def staleness(age_hours: float | None) -> tuple[float, str]:
    """(opacity, note) for a device-fed card whose newest sample is
    ``age_hours`` old. Fresh data — or none at all, which the empty
    state already explains — stays fully opaque with no note."""
    if age_hours is None or age_hours < FRESH_HOURS:
        return 1.0, ""
    span = STALE_FULL_HOURS - FRESH_HOURS
    frac = min((age_hours - FRESH_HOURS) / span, 1.0)
    opacity = 1.0 - frac * (1.0 - STALE_FLOOR)
    if age_hours < 48:
        age = f"{round(age_hours)} h"
    else:
        age = f"{age_hours / 24:.0f} d"
    return opacity, f"Last reading {age} ago"
