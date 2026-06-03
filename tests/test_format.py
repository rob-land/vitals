"""Tests for the pure formatting / chart-scaling helpers."""

from vitals.format import (format_measurement, format_value, humanize_key,
                           nice_max, unit_label)


def test_format_value_integer_units():
    assert format_value(72, "/min") == "72"
    assert format_value(8543, "{steps}") == "8,543"
    assert format_value(98, "%") == "98"


def test_format_value_decimal_units():
    assert format_value(68.42, "kg") == "68.4"
    assert format_value(5.4, "mmol/L") == "5.4"
    # A whole number stays integer even with a decimal unit.
    assert format_value(70.0, "kg") == "70"


def test_format_value_none():
    assert format_value(None) == "—"


def test_unit_label():
    assert unit_label("/min") == "bpm"
    assert unit_label("{steps}") == "steps"
    assert unit_label("Cel") == "°C"
    assert unit_label(None) == ""
    assert unit_label("weird") == "weird"


def test_format_measurement():
    assert format_measurement(72, "/min") == "72 bpm"
    assert format_measurement(68.4, "kg") == "68.4 kg"


def test_humanize_key():
    assert humanize_key("heart_rate") == "Heart rate"
    assert humanize_key("step_count") == "Step count"


def test_nice_max_rounds_up():
    assert nice_max([8543]) == 10000
    assert nice_max([1200, 800]) == 2000
    assert nice_max([42]) == 50
    assert nice_max([0.7]) == 1.0


def test_nice_max_respects_floor():
    # Goal (floor) higher than data still raises the axis top.
    assert nice_max([1200], floor=10000) == 10000


def test_nice_max_empty_and_none():
    assert nice_max([]) == 1.0
    assert nice_max([None, None]) == 1.0
