"""ConnectionKeeper — a persistent link to one watch.

Sync sessions connect, drain and disconnect; features like notification
forwarding instead need the watch *reachable when something happens on
the phone*. A keeper owns one plugin instance on the BLE loop, keeps it
connected with exponential-backoff reconnects, and serializes every
operation over the link through one lock — a health drain and a
notification push must never interleave frames on the same transport.

The DeviceManager starts/stops keepers to match per-device settings and
routes both notification pushes and (when a keeper exists) whole sync
pipelines through ``run()``.
"""

from __future__ import annotations

import asyncio
import logging

log = logging.getLogger(__name__)

_RETRY_START_S = 10
_RETRY_CAP_S = 300


class KeeperNotConnected(Exception):
    """The persistent link is down (reconnect is being retried)."""


class ConnectionKeeper:
    def __init__(self, device, on_state=None):
        self.device = device
        # Called with (address, connected) from the BLE loop on changes.
        self._on_state = on_state
        self.connected = False
        self._stopping = False
        # Loop-affine primitives, created in start() on the BLE loop.
        self._lock: asyncio.Lock | None = None
        self._wake: asyncio.Event | None = None
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        if self._task is not None:
            return
        self._lock = asyncio.Lock()
        self._wake = asyncio.Event()
        self._task = asyncio.create_task(self._maintain())

    async def stop(self) -> None:
        self._stopping = True
        if self._wake is not None:
            self._wake.set()
        if self._task is not None:
            await self._task
            self._task = None

    async def run(self, op):
        """Run ``op(device)`` over the live link.

        Raises ``KeeperNotConnected`` while the link is down; any other
        failure marks the link broken (waking the reconnect loop) and
        propagates."""
        if not self.connected:
            raise KeeperNotConnected(
                f"{self.device.name} is not reachable right now")
        async with self._lock:
            try:
                return await op(self.device)
            except Exception:
                self._set_connected(False)
                self._wake.set()
                raise

    # ── internals (BLE loop) ──────────────────────────────────────
    async def _maintain(self) -> None:
        backoff = _RETRY_START_S
        while not self._stopping:
            if not self.connected:
                try:
                    async with self._lock:
                        await self.device.connect()
                    self._set_connected(True)
                    backoff = _RETRY_START_S
                except Exception as exc:
                    log.debug("keeper %s: connect failed (%s); retry in %ds",
                              self.device.address, exc, backoff)
                    await self._sleep_or_wake(backoff)
                    backoff = min(backoff * 2, _RETRY_CAP_S)
                    continue
            await self._wake.wait()
            self._wake.clear()
        try:
            async with self._lock:
                await self.device.disconnect()
        except Exception:
            log.debug("keeper %s: disconnect on stop failed",
                      self.device.address)
        self._set_connected(False)

    async def _sleep_or_wake(self, seconds: float) -> None:
        try:
            await asyncio.wait_for(self._wake.wait(), seconds)
        except asyncio.TimeoutError:
            pass
        self._wake.clear()

    def _set_connected(self, value: bool) -> None:
        if value == self.connected:
            return
        self.connected = value
        if self._on_state is not None:
            self._on_state(self.device.address, value)
