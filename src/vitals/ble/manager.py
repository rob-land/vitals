"""BLE manager — owns the asyncio event loop on a background thread.

GTK runs on the main thread; bleak is asyncio-based. The pattern, used
elsewhere in the cohort (jamjar's client / scrobbler), is to spawn a
single daemon thread that runs the asyncio loop forever, and hand
coroutines to it via `submit()`. Results come back as concurrent
futures; callers marshal back to GTK with GLib.idle_add.

There's exactly one BleManager per Application — created in
VitalsApplication.do_activate, stopped on quit.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from collections.abc import Coroutine
from concurrent.futures import Future

log = logging.getLogger(__name__)


class BleManager:
    """Background asyncio loop for BLE work."""

    def __init__(self):
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._started = threading.Event()

    def start(self) -> None:
        if self._thread is not None:
            return
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=self._run, name="vitals-ble-loop", daemon=True)
        self._thread.start()
        self._started.wait(timeout=2.0)

    def _run(self) -> None:
        # CRITICAL: `set_event_loop` must happen on this thread before
        # `run_coroutine_threadsafe` from the GTK thread will reach
        # the loop reliably (cf. jamjar's CLAUDE.md gotcha #3).
        asyncio.set_event_loop(self._loop)
        self._started.set()
        try:
            self._loop.run_forever()
        finally:
            self._loop.close()

    def submit(self, coro: Coroutine) -> Future:
        """Schedule a coroutine on the BLE loop.

        Returns a `concurrent.futures.Future` that resolves on the
        background thread. Callers should attach a callback that
        marshals back to GTK via GLib.idle_add — never call
        `future.result()` from the GTK main thread (it would block
        the UI)."""
        if self._loop is None:
            raise RuntimeError("BleManager not started")
        return asyncio.run_coroutine_threadsafe(coro, self._loop)

    def stop(self) -> None:
        if self._loop is not None and self._loop.is_running():
            self._loop.call_soon_threadsafe(self._loop.stop)
        if self._thread is not None:
            self._thread.join(timeout=2.0)
        self._thread = None
        self._loop = None


# ── Pure scan helpers ─────────────────────────────────────────────

async def scan_devices(timeout: float = 5.0) -> list[tuple[str, str, list[str]]]:
    """Return [(address, name, service_uuids), ...] from a fresh scan.

    bleak's `BleakScanner.discover` returns BLEDevice objects; we
    flatten them to a tuple shape the device plugins can consume via
    `Device.matches()`.
    """
    from bleak import BleakScanner
    log.info("scan_devices: starting BleakScanner.discover (timeout=%.1fs)",
             timeout)
    try:
        devices = await BleakScanner.discover(
            timeout=timeout, return_adv=True)
    except Exception:
        log.exception("scan_devices: BleakScanner.discover raised — "
                      "likely a BlueZ / adapter / permission issue. "
                      "Try `bluetoothctl power off; bluetoothctl power on` "
                      "on the host, or reset the Flatpak's bluez "
                      "permissions.")
        raise
    out: list[tuple[str, str, list[str]]] = []
    # `discover(return_adv=True)` returns dict[address] -> (BLEDevice, AdvertisementData)
    for addr, (device, adv) in devices.items():
        name = (adv.local_name if adv else None) or device.name or ""
        uuids = list(adv.service_uuids) if adv and adv.service_uuids else []
        out.append((addr, name, [u.lower() for u in uuids]))
        log.debug("scan_devices: %s name=%r uuids=%s",
                  addr, name, uuids)
    log.info("scan_devices: found %d devices total", len(out))
    return out
