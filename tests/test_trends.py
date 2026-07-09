"""Tests for the honest-trend / recap / freshness math."""

import pytest

from vitals.trends import (
    format_trend, recap_rows, staleness, trend_between, week_stat,
    weekly_trend)


# ── trend_between ─────────────────────────────────────────────────
def test_trend_up_down_and_flat():
    assert trend_between(110.0, 100.0)["direction"] == "up"
    assert trend_between(90.0, 100.0)["direction"] == "down"
    assert trend_between(101.0, 100.0)["direction"] == "flat"


def test_trend_ratio_is_relative_to_previous():
    assert trend_between(120.0, 100.0)["ratio"] == pytest.approx(0.2)
    assert trend_between(80.0, 100.0)["ratio"] == pytest.approx(-0.2)


def test_trend_refuses_sparse_or_missing_windows():
    assert trend_between(None, 100.0) is None
    assert trend_between(110.0, None) is None
    assert trend_between(110.0, 100.0, current_n=3) is None      # < MIN_COVERAGE
    assert trend_between(110.0, 100.0, previous_n=2) is None
    assert trend_between(110.0, 0.0) is None                     # zero baseline


# ── weekly_trend ──────────────────────────────────────────────────
def test_weekly_trend_compares_complete_weeks():
    daily = [1000.0] * 7 + [1200.0] * 7
    trend = weekly_trend(daily)
    assert trend["direction"] == "up"
    assert trend["ratio"] == pytest.approx(0.2)


def test_weekly_trend_ignores_gap_days_not_zeroes_them():
    # 4 recorded days averaging 1000 vs 4 averaging 1000 → level, even
    # though the raw sums differ. Missing days are absence of data.
    daily = ([1000.0, None, 1000.0, None, 1000.0, 1000.0, None]
             + [None, 1000.0, 1000.0, None, 1000.0, None, 1000.0])
    assert weekly_trend(daily)["direction"] == "flat"


def test_weekly_trend_is_none_when_a_week_is_too_sparse():
    daily = [1000.0] * 7 + [1200.0, 1200.0, 1200.0] + [None] * 4
    assert weekly_trend(daily) is None


def test_weekly_trend_wants_exactly_fourteen_days():
    with pytest.raises(ValueError):
        weekly_trend([1.0] * 7)


# ── format_trend ──────────────────────────────────────────────────
def test_format_trend_renders_all_directions():
    assert format_trend(trend_between(112.0, 100.0)) == "▲ 12% vs prior week"
    assert format_trend(trend_between(88.0, 100.0)) == "▼ 12% vs prior week"
    assert format_trend(trend_between(100.0, 100.0)) == "≈ level vs prior week"
    assert format_trend(None) == ""


# ── week_stat / recap_rows ────────────────────────────────────────
def test_week_stat_modes():
    week = [None, 100.0, 200.0, None, 300.0, None, None]
    assert week_stat(week, "per-day") == (200.0, 3)
    assert week_stat(week, "average") == (200.0, 3)
    assert week_stat(week, "change") == (200.0, 3)   # 300 - 100
    assert week_stat([None, 70.0] + [None] * 5, "change") == (None, 1)
    assert week_stat([None] * 7, "per-day") == (None, 0)


def test_recap_rows_reports_only_recorded_metrics():
    week = {"step_count": [8000.0] * 7}
    rows = recap_rows(week, {"step_count": [10000.0] * 7})
    assert len(rows) == 1
    assert rows[0]["title"] == "Steps"
    assert rows[0]["value_text"] == "8,000 steps / day"
    assert rows[0]["trend_text"] == "▼ 20% vs prior week"


def test_recap_rows_trend_stays_honest_without_prior_coverage():
    rows = recap_rows({"water_intake": [1500.0] * 7},
                      {"water_intake": [2000.0] * 2 + [None] * 5})
    assert rows[0]["value_text"] == "1,500 mL / day"
    assert rows[0]["trend_text"] == ""


def test_recap_rows_weight_is_a_change_not_a_trend():
    week = {"body_weight": [82.4, None, None, 82.0, None, None, 81.9]}
    rows = recap_rows(week, {})
    assert rows[0]["title"] == "Weight"
    assert rows[0]["value_text"] == "-0.5 kg over the week"
    assert rows[0]["trend_text"] == ""


# ── staleness ─────────────────────────────────────────────────────
def test_fresh_or_absent_data_keeps_full_opacity():
    assert staleness(None) == (1.0, "")
    assert staleness(0.5) == (1.0, "")
    assert staleness(5.9) == (1.0, "")


def test_stale_data_fades_and_reports_age():
    opacity, note = staleness(27.0)
    assert 0.55 < opacity < 1.0
    assert note == "Last reading 27 h ago"


def test_staleness_clamps_at_the_floor_and_switches_to_days():
    opacity, note = staleness(96.0)
    assert opacity == pytest.approx(0.55)
    assert note == "Last reading 4 d ago"
