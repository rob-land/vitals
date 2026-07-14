"""Tests for notification capture (parse/filter/dedup), the
ConnectionKeeper, and the manager's forwarding plumbing."""

import asyncio

import pytest

from vitals.devices.base import WatchNotification
from vitals.devices.keeper import ConnectionKeeper, KeeperNotConnected
from vitals.notifications import (
    NotificationDeduper, parse_notify, strip_markup)


def notify_args(app="Signal", replaces=0, summary="Alice", body="hi",
                hints=None):
    return (app, replaces, "", summary, body, [], hints or {}, -1)


# ── parse_notify ──────────────────────────────────────────────────
def test_parse_builds_a_watch_notification():
    note = parse_notify(notify_args(), counter=7)
    assert note.app_name == "Signal"
    assert note.title == "Alice" and note.body == "hi"
    assert note.id == 7                       # counter when replaces_id=0


def test_replaces_id_keeps_a_stable_id():
    note = parse_notify(notify_args(replaces=42), counter=7)
    assert note.id == 42


def test_parse_filters_own_transient_and_empty():
    assert parse_notify(notify_args(app="Vitals"), 1, {"Vitals"}) is None
    assert parse_notify(notify_args(summary=""), 1) is None
    assert parse_notify(notify_args(hints={"transient": True}), 1) is None
    assert parse_notify(("bad",), 1) is None  # malformed call


def test_markup_is_stripped_for_the_watch():
    args = notify_args(summary="<b>Alice</b>",
                       body='<a href="x">link</a> &amp; more')
    note = parse_notify(args, 1)
    assert note.title == "Alice"
    assert note.body == "link & more"
    assert strip_markup(None) == ""


def test_dedup_drops_repeats_within_window():
    dedup = NotificationDeduper(window_s=3.0)
    note = parse_notify(notify_args(), 1)
    assert not dedup.is_duplicate(note, now=100.0)
    assert dedup.is_duplicate(note, now=101.0)
    assert not dedup.is_duplicate(note, now=104.5)   # window expired
    other = parse_notify(notify_args(body="hello again"), 2)
    assert not dedup.is_duplicate(other, now=104.6)  # different body


# ── ConnectionKeeper ──────────────────────────────────────────────
class FlakyDevice:
    def __init__(self, fail_first_connects=0):
        self.address = "AA:BB"
        self.name = "Flaky"
        self.calls: list[str] = []
        self._fail = fail_first_connects

    async def connect(self):
        self.calls.append("connect")
        if self._fail > 0:
            self._fail -= 1
            raise OSError("out of range")

    async def disconnect(self):
        self.calls.append("disconnect")

    async def push_notification(self, note):
        self.calls.append(f"push:{note.title}")


async def _spin(keeper, predicate, timeout=1.0):
    async def wait():
        while not predicate():
            await asyncio.sleep(0.005)
    await asyncio.wait_for(wait(), timeout)


def test_keeper_connects_and_runs_ops():
    async def scenario():
        device = FlakyDevice()
        keeper = ConnectionKeeper(device)
        await keeper.start()
        await _spin(keeper, lambda: keeper.connected)
        note = WatchNotification(1, "Signal", "Alice", "hi", 0.0)
        await keeper.run(lambda d: d.push_notification(note))
        await keeper.stop()
        return device

    device = asyncio.run(scenario())
    assert device.calls == ["connect", "push:Alice", "disconnect"]


def test_keeper_marks_link_broken_and_reconnects():
    async def scenario():
        states = []
        device = FlakyDevice()
        keeper = ConnectionKeeper(
            device, on_state=lambda _a, up: states.append(up))
        await keeper.start()
        await _spin(keeper, lambda: keeper.connected)

        async def boom(_d):
            raise OSError("link dropped")
        with pytest.raises(OSError):
            await keeper.run(boom)
        assert not keeper.connected
        with pytest.raises(KeeperNotConnected):
            await keeper.run(lambda d: d.push_notification(None))
        # The maintain loop notices and reconnects.
        await _spin(keeper, lambda: keeper.connected)
        await keeper.stop()
        return states

    states = asyncio.run(scenario())
    assert states == [True, False, True, False]  # final False from stop()


def test_keeper_retries_failed_connects():
    async def scenario():
        device = FlakyDevice(fail_first_connects=1)
        keeper = ConnectionKeeper(device)
        # Shrink the retry delay so the test is fast.
        import vitals.devices.keeper as keeper_mod
        old = keeper_mod._RETRY_START_S
        keeper_mod._RETRY_START_S = 0.01
        try:
            await keeper.start()
            await _spin(keeper, lambda: keeper.connected)
        finally:
            keeper_mod._RETRY_START_S = old
            await keeper.stop()
        return device

    device = asyncio.run(scenario())
    assert device.calls.count("connect") == 2


# ── NotificationMonitor availability ──────────────────────────────
def test_available_is_false_and_cached_when_the_bus_denies(monkeypatch):
    from gi.repository import Gio, GLib
    from vitals.notifications import NotificationMonitor

    attempts = []

    def deny(*_args):
        attempts.append(1)
        raise GLib.Error("BecomeMonitor denied")

    monkeypatch.setattr(Gio, "dbus_address_get_for_bus_sync", deny)
    monitor = NotificationMonitor()
    assert monitor.available is False
    assert monitor.available is False
    assert len(attempts) == 1        # probed once, then cached
    assert not monitor.running


def test_available_probe_does_not_leave_the_monitor_running(monkeypatch):
    from gi.repository import Gio
    from vitals.notifications import NotificationMonitor

    class FakeConn:
        def call_sync(self, *_args, **_kwargs): pass
        def add_filter(self, _cb): pass
        def close_sync(self, _cancellable): pass

    monkeypatch.setattr(
        Gio, "dbus_address_get_for_bus_sync", lambda *_a: "addr")
    monkeypatch.setattr(
        Gio.DBusConnection, "new_for_address_sync",
        staticmethod(lambda *_a, **_k: FakeConn()), raising=False)
    monitor = NotificationMonitor()
    assert monitor.available is True
    assert not monitor.running       # the probe closed its connection
    monitor.start()                  # a real start still works afterwards
    assert monitor.running
