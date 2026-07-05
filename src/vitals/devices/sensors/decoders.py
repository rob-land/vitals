"""Decoders for standard Bluetooth GATT health characteristics.

Pure functions: bytes in → a normalised reading dict out
(``{"type", "value", "unit"?, "meta"?}`` matching the catalog). The
bridge wraps these with a UUID, timestamp and source. Keeping the bit-twiddling
here, free of any BLE machinery, is what makes it unit-testable.

Layouts follow the Bluetooth SIG characteristic specs; numeric medical fields
use IEEE-11073 SFLOAT (16-bit) / FLOAT (32-bit).
"""

from __future__ import annotations

_LB_PER_KG = 0.45359237
_KPA_TO_MMHG = 7.50061683
_MGDL_PER_MMOLL = 18.0156   # glucose


def _u16(data: bytes, offset: int) -> int:
    return int.from_bytes(data[offset:offset + 2], "little")


def _sfloat(raw: int) -> float | None:
    """IEEE-11073 16-bit SFLOAT. Returns None for the special NaN/reserved
    values that mean 'no measurement'."""
    mantissa = raw & 0x0FFF
    exponent = (raw >> 12) & 0x000F
    if mantissa in (0x07FF, 0x0800, 0x0801, 0x07FE, 0x0802):
        return None  # NaN / NRes / reserved / ±INF
    if mantissa >= 0x0800:
        mantissa -= 0x1000   # sign-extend 12-bit
    if exponent >= 0x0008:
        exponent -= 0x0010   # sign-extend 4-bit
    return mantissa * (10.0 ** exponent)


def _ieee_float(raw: int) -> float | None:
    """IEEE-11073 32-bit FLOAT."""
    mantissa = raw & 0x00FFFFFF
    exponent = (raw >> 24) & 0x000000FF
    if mantissa >= 0x00800000:
        mantissa -= 0x01000000   # sign-extend 24-bit
    if exponent >= 0x80:
        exponent -= 0x100        # sign-extend 8-bit
    return mantissa * (10.0 ** exponent)


# ── standard characteristics ──────────────────────────────────────
def heart_rate(data: bytes) -> dict:
    """Heart Rate Measurement (0x2A37) → heart_rate (+ R-R intervals in meta)."""
    flags = data[0]
    if flags & 0x01:                       # 16-bit HR value
        hr, idx = _u16(data, 1), 3
    else:
        hr, idx = data[1], 2
    if flags & 0x08:                       # energy expended present
        idx += 2
    rr = []
    if flags & 0x10:                       # R-R intervals present
        while idx + 2 <= len(data):
            rr.append(_u16(data, idx))
            idx += 2
    meta = {}
    if rr:                                 # 1/1024 s units → ms (for HRV)
        meta["rr_intervals_ms"] = [round(x * 1000 / 1024) for x in rr]
    return {"type": "heart_rate", "value": hr, "unit": "/min", "meta": meta}


def weight(data: bytes) -> dict:
    """Weight Measurement (0x2A9D) → body_weight (kg)."""
    flags = data[0]
    raw = _u16(data, 1)
    if flags & 0x01:                       # Imperial: 0.01 lb resolution
        kg = raw * 0.01 * _LB_PER_KG
    else:                                  # SI: 0.005 kg resolution
        kg = raw * 0.005
    return {"type": "body_weight", "value": round(kg, 3), "unit": "kg"}


def blood_pressure(data: bytes) -> dict:
    """Blood Pressure Measurement (0x2A35) → blood_pressure {systolic, diastolic}."""
    flags = data[0]
    systolic = _sfloat(_u16(data, 1))
    diastolic = _sfloat(_u16(data, 3))     # data[5:7] is MAP — not used
    if flags & 0x01:                       # values in kPa → mmHg
        systolic *= _KPA_TO_MMHG
        diastolic *= _KPA_TO_MMHG
    return {"type": "blood_pressure",
            "value": {"systolic": round(systolic), "diastolic": round(diastolic)}}


def pulse_oximeter(data: bytes) -> dict:
    """PLX Spot-check Measurement (0x2A5E) → oxygen_saturation (+ pulse rate)."""
    spo2 = _sfloat(_u16(data, 1))
    pulse = _sfloat(_u16(data, 3))
    meta = {}
    if pulse is not None:
        meta["pulse_rate"] = round(pulse)
    return {"type": "oxygen_saturation", "value": round(spo2), "unit": "%", "meta": meta}


def temperature(data: bytes) -> dict:
    """Temperature Measurement (0x2A1C) → body_temperature (Cel)."""
    flags = data[0]
    temp = _ieee_float(int.from_bytes(data[1:5], "little"))
    if flags & 0x01:                       # Fahrenheit → Celsius
        temp = (temp - 32) * 5 / 9
    return {"type": "body_temperature", "value": round(temp, 1), "unit": "Cel"}


def glucose(data: bytes) -> dict | None:
    """Glucose Measurement (0x2A18) → blood_glucose (mmol/L)."""
    flags = data[0]
    idx = 1 + 2 + 7                         # flags + sequence + base time
    if flags & 0x01:                       # time offset present
        idx += 2
    if not flags & 0x02:                   # no concentration in this record
        return None
    conc = _sfloat(_u16(data, idx))
    if conc is None:
        return None
    if flags & 0x04:                       # concentration in mol/L
        mmol = conc * 1000
    else:                                  # kg/L → mg/dL → mmol/L
        mmol = (conc * 100000) / _MGDL_PER_MMOLL
    return {"type": "blood_glucose", "value": round(mmol, 1), "unit": "mmol/L"}


# ── proprietary (the common non-standard case) ────────────────────
def xiaomi_scale(data: bytes) -> dict | None:
    """Xiaomi Mi Body Composition Scale advertisement service data → body_weight.

    The Mi scale *advertises* the standard 0x181B UUID but encodes weight in a
    custom layout (see openScale). Weight is a little-endian uint16 at bytes
    11-12, in units of 0.005 kg; control byte 1 carries the unit + stabilised
    + removed flags. Only a stabilised, on-scale reading is returned.
    """
    if len(data) < 13:
        return None
    ctrl = data[1]
    stabilised = bool(ctrl & 0x20)
    removed = bool(ctrl & 0x80)
    if not stabilised or removed:
        return None
    raw = _u16(data, 11)
    if ctrl & 0x01:                        # pounds (0.01 lb units)
        kg = (raw / 100.0) * _LB_PER_KG
    else:                                  # kilograms (0.005 kg units)
        kg = raw / 200.0
    return {"type": "body_weight", "value": round(kg, 2), "unit": "kg"}
