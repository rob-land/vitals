"""Tests for the opportunistic GATT-sensor plugin and its record shape."""

import asyncio
import struct
from types import SimpleNamespace

from vitals.devices.sensors import gatt
from vitals.devices.sensors.plugin import (GattSensorDevice,
                                           build_sensor_record)

ADDR = "CC:DD:EE:FF:00:11"


def _xiaomi_payload(raw_weight: int, ctrl: int = 0x20) -> bytes:
    # 13 bytes: ctrl at [1], weight LE uint16 at [11:13].
    data = bytearray(13)
    data[1] = ctrl
    data[11:13] = struct.pack("<H", raw_weight)
    return bytes(data)


def _advert(service_data=None, service_uuids=None):
    return SimpleNamespace(service_data=service_data or {},
                           service_uuids=service_uuids or [])


def test_matches_on_standard_service_uuids():
    assert GattSensorDevice.matches("A&D UA-651", [gatt.uuid16(0x1810)])
    assert GattSensorDevice.matches(None, [gatt.uuid16(0x181B)])
    assert not GattSensorDevice.matches("Pebble", ["0000feed-0000-1000-8000-00805f9b34fb"])


def test_match_advertisement_paths():
    assert GattSensorDevice.match_advertisement(
        None, _advert(service_uuids=[gatt.uuid16(0x181D)]))
    assert GattSensorDevice.match_advertisement(
        None, _advert(service_data={gatt.uuid16(0x181B): b"\x00" * 13}))
    assert not GattSensorDevice.match_advertisement(None, _advert())


def test_advertisement_only_reading_needs_no_connection():
    sensor = GattSensorDevice(address=ADDR, name="Mi Scale")
    adv = _advert(service_data={
        gatt.uuid16(0x181B): _xiaomi_payload(16000)})  # 16000*0.005 = 80 kg
    envelopes = asyncio.run(sensor.handle_advertisement(None, adv))
    (env,) = envelopes
    assert env["type"] == "body_weight"
    assert env["value"] == 80.0 and env["unit"] == "kg"
    assert env["source"]["modality"] == "sensed"
    assert env["source"]["device_id"] == ADDR


def test_unstabilised_advertisement_produces_nothing():
    sensor = GattSensorDevice(address=ADDR, name="Mi Scale")
    adv = _advert(service_data={
        gatt.uuid16(0x181B): _xiaomi_payload(16000, ctrl=0x00)})
    assert asyncio.run(sensor.handle_advertisement(None, adv)) == []


def test_record_uuid_dedupes_the_advert_burst():
    now = 1_700_000_000.0
    reading = {"type": "body_weight", "value": 80.0, "unit": "kg"}
    a = build_sensor_record(reading, ADDR, "Mi Scale", now=now)
    b = build_sensor_record(reading, ADDR, "Mi Scale", now=now + 10)
    assert a["uuid"] == b["uuid"]  # same minute, same value → one record
    c = build_sensor_record({**reading, "value": 80.5}, ADDR, "Mi Scale",
                            now=now + 10)
    assert c["uuid"] != a["uuid"]  # different value stays distinct
    d = build_sensor_record(reading, ADDR, "Mi Scale", now=now + 90)
    assert d["uuid"] != a["uuid"]  # next minute is a new event
