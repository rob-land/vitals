"""ScanBroker — the process's single BleakScanner, shared by everyone.

BlueZ tolerates exactly one discovery session per client, so passive
sensor listening and the pairing dialog's scan must share a scanner
rather than each starting their own. The broker runs on the BLE loop:

  * **listening** — while enabled, every advertisement is handed to the
    sensor handler (DeviceManager routes it to registered sensors);
  * **collecting** — a pairing scan registers a collector for a few
    seconds and gets the deduplicated (address, name, uuids) list back,
    piggybacking on the running scanner or spinning one up just for the
    collection window.

All public methods are main-thread-safe; the state lives on the loop.
"""

from __future__ import annotations

import asyncio
import logging

from bleak import BleakScanner

log = logging.getLogger(__name__)


class ScanBroker:
    def __init__(self, ble):
        self._ble = ble
        self._scanner: BleakScanner | None = None
        self._listening = False
        self._sensor_handler = None      # async (device, advertisement)
        self._collectors: list[dict] = []
        self._handler_tasks: set[asyncio.Task] = set()

    # ── main-thread API ───────────────────────────────────────────
    def set_sensor_handler(self, handler) -> None:
        """`handler(device, advertisement)` coroutine, called on the BLE
        loop for every advertisement while listening is on."""
        self._sensor_handler = handler

    def start_listening(self) -> None:
        self._ble.submit(self._set_listening(True))

    def stop_listening(self) -> None:
        self._ble.submit(self._set_listening(False))

    def collect(self, timeout: float):
        """Scan for `timeout` seconds; returns a concurrent Future of
        [(address, name, service_uuids)] like tock's scan_devices()."""
        return self._ble.submit(self._collect(timeout))

    # ── loop side ─────────────────────────────────────────────────
    async def _set_listening(self, on: bool) -> None:
        self._listening = on
        await self._reconcile()

    async def _reconcile(self) -> None:
        want = self._listening or bool(self._collectors)
        if want and self._scanner is None:
            self._scanner = BleakScanner(detection_callback=self._on_advert)
            await self._scanner.start()
            log.info("scan broker: scanner started")
        elif not want and self._scanner is not None:
            scanner, self._scanner = self._scanner, None
            await scanner.stop()
            log.info("scan broker: scanner stopped")

    async def _collect(self, timeout: float) -> list[tuple[str, str, list[str]]]:
        collector = {"seen": {}}
        self._collectors.append(collector)
        try:
            await self._reconcile()
            await asyncio.sleep(timeout)
        finally:
            self._collectors.remove(collector)
            await self._reconcile()
        return list(collector["seen"].values())

    def _on_advert(self, device, advertisement) -> None:
        for collector in self._collectors:
            collector["seen"][device.address] = (
                device.address,
                device.name or advertisement.local_name or "",
                list(advertisement.service_uuids or []))
        if self._listening and self._sensor_handler is not None:
            task = asyncio.create_task(
                self._sensor_handler(device, advertisement))
            # Keep a reference so the task isn't garbage-collected mid-run.
            self._handler_tasks.add(task)
            task.add_done_callback(self._handler_tasks.discard)
