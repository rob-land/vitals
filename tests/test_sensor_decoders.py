"""Tests for the standard GATT characteristic decoders.

Each payload is hand-built to the Bluetooth SIG characteristic layout, with
the expected value computed by hand, so a regression in the bit-twiddling
is caught immediately.
"""

from vitals.devices.sensors import decoders


def test_sfloat_and_float_helpers():
    assert decoders._sfloat(0x0076) == 118          # mantissa 118, exp 0
    assert abs(decoders._sfloat(0xC037) - 0.0055) < 1e-9   # mantissa 55, exp -4
    assert decoders._sfloat(0x07FF) is None          # NaN
    assert decoders._ieee_float(0xFF000172) == 37.0  # mantissa 370, exp -1


def test_heart_rate_uint8():
    r = decoders.heart_rate(bytes([0x00, 72]))
    assert r == {"type": "heart_rate", "value": 72, "unit": "/min", "meta": {}}


def test_heart_rate_with_rr_intervals():
    # flags 0x10 = R-R present; HR 60; one R-R of 1024 (=1.000 s).
    r = decoders.heart_rate(bytes([0x10, 60, 0x00, 0x04]))
    assert r["value"] == 60
    assert r["meta"]["rr_intervals_ms"] == [1000]


def test_weight_si():
    # 14000 * 0.005 kg = 70.0 kg
    r = decoders.weight(bytes([0x00, 0xB0, 0x36]))
    assert r == {"type": "body_weight", "value": 70.0, "unit": "kg"}


def test_blood_pressure():
    # systolic 118, diastolic 76, MAP 90 (all SFLOAT, mmHg)
    r = decoders.blood_pressure(bytes([0x00, 0x76, 0x00, 0x4C, 0x00, 0x5A, 0x00]))
    assert r["type"] == "blood_pressure"
    assert r["value"] == {"systolic": 118, "diastolic": 76}


def test_pulse_oximeter():
    r = decoders.pulse_oximeter(bytes([0x00, 0x62, 0x00, 0x48, 0x00]))
    assert r["value"] == 98 and r["unit"] == "%"
    assert r["meta"]["pulse_rate"] == 72


def test_temperature_celsius():
    r = decoders.temperature(bytes([0x00, 0x72, 0x01, 0x00, 0xFF]))
    assert r == {"type": "body_temperature", "value": 37.0, "unit": "Cel"}


def test_glucose_mol_per_litre():
    # flags 0x06 (conc present, mol/L); seq 1; base time; conc 0.0055 mol/L.
    data = bytes([0x06, 0x01, 0x00, 0xEA, 0x07, 0x06, 0x03, 0x09, 0x00, 0x00,
                  0x37, 0xC0, 0x00])
    r = decoders.glucose(data)
    assert r == {"type": "blood_glucose", "value": 5.5, "unit": "mmol/L"}


def test_xiaomi_scale_stabilised():
    data = bytes([0x02, 0x20, 0xEA, 0x07, 0x06, 0x03, 0x09, 0x00, 0x00,
                  0x00, 0x00, 0xB0, 0x36])
    assert decoders.xiaomi_scale(data) == {"type": "body_weight", "value": 70.0, "unit": "kg"}


def test_xiaomi_scale_unstable_is_skipped():
    data = bytes([0x02, 0x00, 0xEA, 0x07, 0x06, 0x03, 0x09, 0x00, 0x00,
                  0x00, 0x00, 0xB0, 0x36])
    assert decoders.xiaomi_scale(data) is None
