"""Phone-calendar reader for watch timeline pins.

Reads upcoming events from Evolution Data Server (what GNOME Calendar
and GNOME Online Accounts sync into) and normalises them into plain
``CalendarEvent``s a watch plugin can pin to its timeline. EDS may be
absent (not installed, or a Flatpak built without the client libs) —
``read_events`` then raises ``CalendarUnavailable`` and the sync
pipeline reports it as a warning rather than failing.

The EDS calls are blocking D-Bus; callers run them off the main loop
(the sync pipeline wraps them in ``asyncio.to_thread``). Everything
below ``events_to_pins`` is pure and unit-tested.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta

log = logging.getLogger(__name__)

# How far ahead pins reach. The recent past stays so an in-progress
# meeting keeps its pin.
LOOKBACK_H = 24
LOOKAHEAD_D = 7

# Stable namespace: one calendar occurrence → one pin uuid, so a
# re-push updates the same pin and a vanished event can be deleted.
_PIN_NAMESPACE = uuid.uuid5(uuid.NAMESPACE_DNS, "calendar.vitals.rob.land")


class CalendarUnavailable(Exception):
    """Evolution Data Server (or its GIR bindings) is not available."""


@dataclass(frozen=True)
class CalendarEvent:
    uid: str            # EDS uid + recurrence discriminator
    title: str
    start_utc: int      # unix seconds; all-day events at local midnight
    duration_min: int
    location: str = ""
    description: str = ""


def pin_uuid(event: CalendarEvent) -> bytes:
    """Deterministic 16-byte pin id for one event occurrence."""
    return uuid.uuid5(_PIN_NAMESPACE,
                      f"{event.uid}:{event.start_utc}").bytes


def reconcile(events: list[CalendarEvent],
              previously_pushed: set[str]) -> tuple[list, set[str], set[str]]:
    """What to insert and what to delete on the watch.

    Returns ``(events, current_ids, stale_ids)`` where ids are pin
    uuids as hex strings. Everything current is (re-)inserted — pins
    are keyed by their uuid so an unchanged event is an idempotent
    overwrite; ids pushed before but no longer present are stale and
    should be deleted from the watch."""
    current = {pin_uuid(e).hex() for e in events}
    stale = set(previously_pushed) - current
    return events, current, stale


# ── EDS access (blocking; run off the main loop) ──────────────────

def read_events(now: datetime | None = None) -> list[CalendarEvent]:
    """Upcoming events from every enabled EDS calendar."""
    try:
        import gi
        gi.require_version("ECal", "2.0")
        gi.require_version("EDataServer", "1.2")
        gi.require_version("ICalGLib", "3.0")
        from gi.repository import ECal, EDataServer
    except (ImportError, ValueError) as exc:
        raise CalendarUnavailable(
            "calendar support needs evolution-data-server") from exc

    now = now or datetime.now().astimezone()
    start = round((now - timedelta(hours=LOOKBACK_H)).timestamp())
    end = round((now + timedelta(days=LOOKAHEAD_D)).timestamp())

    try:
        registry = EDataServer.SourceRegistry.new_sync(None)
        sources = registry.list_enabled(
            EDataServer.SOURCE_EXTENSION_CALENDAR)
    except Exception as exc:
        raise CalendarUnavailable(f"EDS not reachable: {exc}") from exc

    events: list[CalendarEvent] = []

    def on_instance(comp, instance_start, instance_end, *_extra) -> bool:
        # Recurrences arrive expanded: one call per occurrence in the
        # window, with the occurrence's own start/end.
        event = _event_from_instance(comp, instance_start, instance_end)
        if event is not None:
            events.append(event)
        return True

    for source in sources:
        try:
            client = ECal.Client.connect_sync(
                source, ECal.ClientSourceType.EVENTS, 5, None)
            client.generate_instances_sync(start, end, None, on_instance)
        except Exception:
            log.warning("calendar: could not read %s",
                        source.get_display_name(), exc_info=True)
    events.sort(key=lambda e: e.start_utc)
    return events


def _event_from_instance(comp, instance_start,
                         instance_end) -> CalendarEvent | None:
    """One expanded occurrence (ICalGLib component + occurrence times)
    → a CalendarEvent (None for busted entries)."""
    try:
        start_dt = _to_datetime(instance_start)
        duration = _to_datetime(instance_end) - start_dt
        duration_min = max(0, round(duration.total_seconds() / 60))
        return CalendarEvent(
            uid=comp.get_uid() or "",
            title=comp.get_summary() or "Busy",
            start_utc=round(start_dt.timestamp()),
            duration_min=duration_min,
            location=comp.get_location() or "",
            description=(comp.get_description() or "")[:200],
        )
    except Exception:
        log.warning("calendar: skipping unparsable event", exc_info=True)
        return None


def _to_datetime(ical) -> datetime:
    """ICalGLib.Time → an aware datetime. Date-only values (all-day
    events) land at local midnight, matching what a watch shows for an
    all-day pin; timed values convert through libical's own zone-aware
    epoch conversion."""
    from datetime import timezone as _tz
    if ical.is_date():
        return datetime(ical.get_year(), ical.get_month(),
                        ical.get_day()).astimezone()
    zone = ical.get_timezone()
    epoch = (ical.as_timet_with_zone(zone) if zone is not None
             else ical.as_timet())
    return datetime.fromtimestamp(epoch, tz=_tz.utc)
