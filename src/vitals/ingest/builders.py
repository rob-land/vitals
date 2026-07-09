"""Build health-record envelopes from watch readings.

Ported from tock's PulseSink; the record shapes and uuid scheme are the
IPC-era wire format, kept verbatim because the adopted database already
holds records with these uuids. In particular the ``tock:`` uuid prefix
is load-bearing: a watch re-draining an already-synced day must produce
the same uuids so the store upserts instead of duplicating history.

Two modelling decisions worth knowing:

  * **Heart rate** is a clean point-in-time observation: one
    ``heart_rate`` record per reading, with the sensor confidence
    carried in ``meta``.
  * **Steps** come in two shapes, distinguished by the reading's
    ``interval_seconds``:
      - *Cumulative snapshot* (Bangle's monotonic daily counter,
        ``interval_seconds`` unset) — emit ONE ``step_count`` record per
        local day with a deterministic per-day uuid, so a later sync the
        same day UPSERTS the latest day total.
      - *Interval delta* (a Pebble's per-minute records,
        ``interval_seconds`` set) — emit one ``step_count`` record per
        interval, uuid keyed on the interval start, which aggregation
        SUMS into the day total. The deterministic uuid keeps a
        re-drained minute idempotent.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

# The reading/session/workout objects are the device layer's
# ActivityReading / SleepSession / WorkoutSession dataclasses; builders
# only touch their documented attributes, so they stay duck-typed here.


@dataclass(frozen=True)
class HealthSample:
    """One activity reading tagged with the device it came from."""

    device_address: str
    device_name: str
    reading: Any


@dataclass(frozen=True)
class SleepSample:
    """One sleep session tagged with the device it came from."""

    device_address: str
    device_name: str
    session: Any


@dataclass(frozen=True)
class WorkoutSample:
    """One workout session tagged with the device it came from."""

    device_address: str
    device_name: str
    workout: Any


@dataclass(frozen=True)
class HydrationSample:
    """One logged drink tagged with the bottle it came from."""

    device_address: str
    device_name: str
    reading: Any


def build_records(sample: HealthSample) -> list[dict]:
    r = sample.reading
    # Watch readings always carry a real wall-clock timestamp.
    local = datetime.fromtimestamp(r.timestamp or 0.0, tz=timezone.utc).astimezone()
    device = sample.device_address or sample.device_name or "unknown"
    source = {
        "modality": "sensed",
        "device_id": sample.device_address or None,
        "device_name": sample.device_name or None,
    }
    records: list[dict] = []

    if r.heart_rate_bpm is not None:
        record = {
            "uuid": f"tock:{device}:heart_rate:{int(local.timestamp())}",
            "type": "heart_rate",
            "effective_start": local.isoformat(),
            "value": r.heart_rate_bpm,
            "unit": "/min",
            "source": source,
        }
        if r.heart_rate_confidence is not None:
            record["meta"] = {"confidence": r.heart_rate_confidence}
        records.append(record)

    # Active energy and distance are always per-interval deltas
    # (Pebble minute records); like steps, aggregation sums them by day.
    if r.interval_seconds is not None:
        ev_end = (local + timedelta(seconds=r.interval_seconds)).isoformat()
        ts = int(local.timestamp())
        if r.active_kcal is not None:
            records.append({
                "uuid": f"tock:{device}:active_energy:{ts}",
                "type": "active_energy",
                "effective_start": local.isoformat(),
                "effective_end": ev_end,
                "value": round(r.active_kcal, 3),
                "unit": "kcal",
                "source": source,
            })
        if r.distance_m is not None:
            records.append({
                "uuid": f"tock:{device}:distance:{ts}",
                "type": "distance",
                "effective_start": local.isoformat(),
                "effective_end": ev_end,
                "value": round(r.distance_m, 2),
                "unit": "m",
                "source": source,
            })

    if r.steps is not None and r.interval_seconds is not None:
        # Interval delta: one record per interval, summed by aggregation.
        end = local + timedelta(seconds=r.interval_seconds)
        records.append({
            "uuid": f"tock:{device}:step_count:{int(local.timestamp())}",
            "type": "step_count",
            "effective_start": local.isoformat(),
            "effective_end": end.isoformat(),
            "value": r.steps,
            "unit": "{steps}",
            "source": source,
        })
    elif r.steps is not None:
        # Cumulative day total: one record per day, upserted.
        day = local.date()
        day_start = local.replace(hour=0, minute=0, second=0, microsecond=0)
        records.append({
            "uuid": f"tock:{device}:step_count:{day.isoformat()}",
            "type": "step_count",
            "effective_start": day_start.isoformat(),
            "effective_end": local.isoformat(),
            "value": r.steps,
            "unit": "{steps}",
            "source": source,
        })
    return records


def build_sleep_record(sample: SleepSample) -> dict:
    """One ``sleep_episode`` (structured) per session. The envelope
    bounds the whole episode; the body tiles it into chronological
    light/deep stage spans — a Pebble distinguishes only restful (deep)
    from the rest (light) — plus the derived total. uuid keys on the
    episode start so a re-drained session upserts."""
    s = sample.session
    start = datetime.fromtimestamp(s.start or 0.0, tz=timezone.utc).astimezone()
    end = datetime.fromtimestamp(s.end or 0.0, tz=timezone.utc).astimezone()
    device = sample.device_address or sample.device_name or "unknown"
    record = {
        "uuid": f"tock:{device}:sleep_episode:{int(s.start)}",
        "type": "sleep_episode",
        "effective_start": start.isoformat(),
        "effective_end": end.isoformat(),
        "value": {
            "stages": _sleep_stages(s),
            "total_sleep_minutes": round(s.duration_seconds / 60),
        },
        "unit": None,
        "source": {
            "modality": "sensed",
            "device_id": sample.device_address or None,
            "device_name": sample.device_name or None,
        },
    }
    if s.is_nap:
        record["meta"] = {"nap": True}
    return record


def _sleep_stages(s) -> list[dict]:
    """Tile [start, end] into contiguous, non-overlapping light/deep
    stage spans (the schema's requirement), deep where the watch
    reported a restful period and light everywhere else."""
    def iso(ts: float) -> str:
        return datetime.fromtimestamp(
            ts, tz=timezone.utc).astimezone().isoformat()

    # Clamp deep spans into the window and merge in order.
    deep = sorted((max(s.start, a), min(s.end, b))
                  for a, b in s.deep_spans if b > a)
    stages: list[dict] = []
    cursor = s.start
    for a, b in deep:
        a = max(a, cursor)
        if a > cursor:
            stages.append({"stage": "light", "start": iso(cursor),
                           "end": iso(a)})
        if b > a:
            stages.append({"stage": "deep", "start": iso(a),
                           "end": iso(b)})
            cursor = b
    if cursor < s.end:
        stages.append({"stage": "light", "start": iso(cursor),
                       "end": iso(s.end)})
    return stages


def build_hydration_record(sample: HydrationSample) -> dict:
    """One ``water_intake`` (scalar mL) per logged drink.

    A bottle keeps its drink log until Vitals acknowledges receipt, so a
    sync can legitimately re-see drinks it already stored. The uuid keys
    on the drink's timestamp — the bottle logs at most one drink per
    second — so a re-drained drink UPSERTS instead of double-counting.

    Water temperature and TDS (water quality) ride along in ``meta`` when
    the bottle has those sensors; plainer models leave them None and the
    keys are simply absent.
    """
    r = sample.reading
    local = datetime.fromtimestamp(r.timestamp or 0.0, tz=timezone.utc).astimezone()
    device = sample.device_address or sample.device_name or "unknown"
    record = {
        "uuid": f"tock:{device}:water_intake:{int(r.timestamp)}",
        "type": "water_intake",
        "effective_start": local.isoformat(),
        "value": round(r.amount_ml, 1),
        "unit": "mL",
        "source": {
            "modality": "sensed",
            "device_id": sample.device_address or None,
            "device_name": sample.device_name or None,
        },
    }
    meta: dict = {}
    if r.temperature_c is not None:
        meta["water_temperature_c"] = round(r.temperature_c, 1)
    if r.tds_ppm is not None:
        meta["tds_ppm"] = r.tds_ppm
    if meta:
        record["meta"] = meta
    return record


def build_workout_record(sample: WorkoutSample) -> dict:
    """One ``workout`` (structured) per detected session. The envelope
    bounds the workout; the body carries the activity name, duration and
    whatever totals the watch recorded. uuid keys on the start so a
    re-drain upserts."""
    w = sample.workout
    start = datetime.fromtimestamp(w.start or 0.0, tz=timezone.utc).astimezone()
    end = datetime.fromtimestamp(w.end or 0.0, tz=timezone.utc).astimezone()
    device = sample.device_address or sample.device_name or "unknown"
    value: dict = {
        "activity_name": w.kind,
        "duration_seconds": round(w.duration_seconds),
    }
    if w.distance_m is not None:
        value["distance_meters"] = round(w.distance_m, 2)
    if w.active_kcal is not None:
        value["active_energy_kcal"] = round(w.active_kcal, 3)
    if w.steps is not None:
        value["steps"] = w.steps
    return {
        "uuid": f"tock:{device}:workout:{int(w.start)}",
        "type": "workout",
        "effective_start": start.isoformat(),
        "effective_end": end.isoformat(),
        "value": value,
        "unit": None,
        "source": {
            "modality": "sensed",
            "device_id": sample.device_address or None,
            "device_name": sample.device_name or None,
        },
        "meta": {"activity_name": w.kind},
    }
