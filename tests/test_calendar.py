"""Tests for the calendar → timeline-pin plumbing (the pure parts:
pin identity and reconciliation; EDS access needs a session bus)."""

from vitals.calendar import CalendarEvent, pin_uuid, reconcile


def _event(uid="ev-1", start=1_700_000_000, **kw):
    base = dict(uid=uid, title="Dentist", start_utc=start, duration_min=30)
    base.update(kw)
    return CalendarEvent(**base)


def test_pin_uuid_is_stable_per_occurrence():
    assert pin_uuid(_event()) == pin_uuid(_event(title="Renamed"))
    assert pin_uuid(_event()) != pin_uuid(_event(start=1_700_003_600))
    assert pin_uuid(_event()) != pin_uuid(_event(uid="ev-2"))
    assert len(pin_uuid(_event())) == 16


def test_reconcile_splits_current_and_stale():
    a, b = _event("a"), _event("b")
    previously = {pin_uuid(a).hex(), "00" * 16}   # a stays, other vanished
    events, current, stale = reconcile([a, b], previously)
    assert events == [a, b]
    assert current == {pin_uuid(a).hex(), pin_uuid(b).hex()}
    assert stale == {"00" * 16}


def test_reconcile_empty_calendar_marks_all_stale():
    previously = {"aa" * 16, "bb" * 16}
    events, current, stale = reconcile([], previously)
    assert events == [] and current == set()
    assert stale == previously
