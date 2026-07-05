"""Device plugins and the multi-device manager."""

from vitals.devices.base import (
    ActivityReading, Device, SleepSession, WorkoutSession,
    available_devices, matching_device, register_device)
from vitals.devices.manager import DeviceEntry, DeviceManager

__all__ = [
    "ActivityReading", "Device", "SleepSession", "WorkoutSession",
    "available_devices", "matching_device", "register_device",
    "DeviceEntry", "DeviceManager",
]
