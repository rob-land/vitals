"""Tests for the metric-detail history window math (pure helpers)."""

from datetime import date, datetime

from vitals.pages.metric_detail import (
    bucket_starts, event_fraction, norm_key, offset_for_date, period_label,
    period_window)


def test_event_fraction_positions_within_window():
    assert event_fraction(150, 100, 200) == 0.5
    assert event_fraction(100, 100, 200) == 0.0
    assert event_fraction(200, 100, 200) == 1.0
    assert event_fraction(100, 100, 100) == 0.0     # zero span is safe

# A fixed "now": Friday 14 Nov 2025, 09:30 local.
NOW = datetime(2025, 11, 14, 9, 30)


def test_day_window_is_hourly():
    start, end, bucket = period_window("D", 0, NOW)
    assert bucket == "hour"
    assert start == datetime(2025, 11, 14)
    assert end == datetime(2025, 11, 15)
    assert len(bucket_starts(start, end, bucket)) == 24


def test_day_offset_walks_back():
    start, end, _ = period_window("D", 3, NOW)
    assert start == datetime(2025, 11, 11)
    assert end == datetime(2025, 11, 12)


def test_week_window_is_monday_to_sunday_daily():
    start, end, bucket = period_window("W", 0, NOW)
    assert bucket == "day"
    assert start == datetime(2025, 11, 10)          # Monday
    assert end == datetime(2025, 11, 17)
    assert len(bucket_starts(start, end, bucket)) == 7


def test_month_window_is_calendar_month_daily():
    start, end, bucket = period_window("M", 0, NOW)
    assert bucket == "day"
    assert start == datetime(2025, 11, 1)
    assert end == datetime(2025, 12, 1)
    assert len(bucket_starts(start, end, bucket)) == 30


def test_month_offset_crosses_year():
    start, end, _ = period_window("M", 11, NOW)      # 11 months back
    assert start == datetime(2024, 12, 1)
    assert end == datetime(2025, 1, 1)


def test_six_month_window_is_weekly():
    start, end, bucket = period_window("6M", 0, NOW)
    assert bucket == "week"
    assert start == datetime(2025, 6, 1)             # 6 calendar months
    assert end == datetime(2025, 12, 1)


def test_year_window_is_monthly():
    start, end, bucket = period_window("Y", 0, NOW)
    assert bucket == "month"
    assert start == datetime(2025, 1, 1)
    assert end == datetime(2026, 1, 1)
    assert len(bucket_starts(start, end, bucket)) == 12


def test_year_offset():
    start, end, _ = period_window("Y", 2, NOW)
    assert start == datetime(2023, 1, 1)
    assert end == datetime(2024, 1, 1)


def test_period_labels():
    assert period_label("D", *period_window("D", 0, NOW)[:2], NOW) == "Today"
    assert period_label("D", *period_window("D", 1, NOW)[:2], NOW) == "Yesterday"
    assert period_label("M", *period_window("M", 0, NOW)[:2], NOW) == "November 2025"
    assert period_label("Y", *period_window("Y", 0, NOW)[:2], NOW) == "2025"
    wk = period_label("W", *period_window("W", 0, NOW)[:2], NOW)
    assert wk == "10–16 Nov 2025"


def test_offset_for_date_jumps_to_the_right_period():
    assert offset_for_date("D", date(2025, 11, 11), NOW) == 3
    assert offset_for_date("W", date(2025, 11, 3), NOW) == 1     # prev week
    assert offset_for_date("W", date(2025, 11, 10), NOW) == 0    # this week
    assert offset_for_date("M", date(2025, 1, 15), NOW) == 10
    assert offset_for_date("M", date(2024, 11, 1), NOW) == 12
    assert offset_for_date("Y", date(2023, 6, 1), NOW) == 2
    assert offset_for_date("6M", date(2025, 10, 1), NOW) == 0    # in current 6M
    assert offset_for_date("6M", date(2025, 5, 1), NOW) == 1     # prior 6M
    assert offset_for_date("D", date(2026, 1, 1), NOW) == 0      # future clamps


def test_norm_key_aligns_buckets_to_aggregate_rows():
    # A day bucket start and the ISO string the store would return match.
    dt = datetime(2025, 11, 14)
    assert norm_key(dt, "day") == norm_key(dt.isoformat(), "day") == "2025-11-14"
    assert norm_key(datetime(2025, 11, 14, 8), "hour") == "2025-11-14T08"
    assert norm_key(datetime(2025, 11, 1), "month") == "2025-11"
