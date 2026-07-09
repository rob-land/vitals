"""Tests for the envelope builders (ported from tock's PulseSink tests).

The reading/session dataclasses live in the device layer, which arrives
in a later phase; the builders are duck-typed, so local stand-ins with
the same attributes keep these golden tests self-contained.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone

from vitals.ingest import (HealthSample, HydrationSample, SleepSample,
                           WorkoutSample, build_hydration_record,
                           build_records, build_sleep_record,
                           build_workout_record)

ADDR = "AA:BB:CC:DD:EE:FF"
TS = 1_700_000_000.0


@dataclass(frozen=True)
class Reading:  # mirrors devices.base.ActivityReading
    steps: int | None = None
    heart_rate_bpm: int | None = None
    heart_rate_confidence: int | None = None
    timestamp: float | None = None
    interval_seconds: int | None = None
    active_kcal: float | None = None
    distance_m: float | None = None


@dataclass(frozen=True)
class Sleep:  # mirrors devices.base.SleepSession
    start: float
    end: float
    deep_spans: tuple = field(default_factory=tuple)
    is_nap: bool = False

    @property
    def duration_seconds(self) -> float:
        return self.end - self.start


@dataclass(frozen=True)
class Workout:  # mirrors devices.base.WorkoutSession
    start: float
    end: float
    kind: str
    steps: int | None = None
    active_kcal: float | None = None
    distance_m: float | None = None

    @property
    def duration_seconds(self) -> float:
        return self.end - self.start


@dataclass(frozen=True)
class Hydration:  # mirrors devices.base.HydrationReading
    amount_ml: float
    timestamp: float
    temperature_c: float | None = None
    tds_ppm: int | None = None


def _sample(steps=4321, bpm=72, conf=90, ts=TS, name="PineTime"):
    return HealthSample(
        device_address=ADDR, device_name=name,
        reading=Reading(steps=steps, heart_rate_bpm=bpm,
                        heart_rate_confidence=conf, timestamp=ts))


def _by_type(records):
    return {r["type"]: r for r in records}


# ── build_records: point + cumulative shapes ─────────────────────

def test_heart_rate_is_point_observation():
    rec = _by_type(build_records(_sample()))["heart_rate"]
    assert rec["value"] == 72 and rec["unit"] == "/min"
    assert rec["meta"] == {"confidence": 90}
    assert rec["effective_start"] and "effective_end" not in rec
    assert rec["source"]["modality"] == "sensed"
    assert rec["source"]["device_id"] == ADDR
    # Legacy prefix: the adopted DB already holds tock:* uuids.
    assert rec["uuid"] == f"tock:{ADDR}:heart_rate:{int(TS)}"


def test_step_count_is_per_day_interval():
    rec = _by_type(build_records(_sample()))["step_count"]
    local = datetime.fromtimestamp(TS, tz=timezone.utc).astimezone()
    assert rec["value"] == 4321 and rec["unit"] == "{steps}"
    # uuid is keyed on the local day so same-day re-syncs upsert it.
    assert rec["uuid"] == f"tock:{ADDR}:step_count:{local.date().isoformat()}"
    # interval spans [local midnight, reading time].
    assert rec["effective_start"] < rec["effective_end"]
    start = datetime.fromisoformat(rec["effective_start"])
    assert (start.hour, start.minute, start.second) == (0, 0, 0)


def test_missing_fields_skipped():
    assert _by_type(build_records(_sample(bpm=None, conf=None))).keys() == {"step_count"}
    assert _by_type(build_records(_sample(steps=None))).keys() == {"heart_rate"}
    assert build_records(_sample(steps=None, bpm=None, conf=None)) == []


def test_no_confidence_omits_meta():
    rec = _by_type(build_records(_sample(conf=None)))["heart_rate"]
    assert "meta" not in rec


def test_step_uuid_stable_within_day_hr_uuid_per_reading():
    a = _by_type(build_records(_sample(ts=TS)))
    b = _by_type(build_records(_sample(ts=TS + 60)))
    assert a["step_count"]["uuid"] == b["step_count"]["uuid"]
    assert a["heart_rate"]["uuid"] != b["heart_rate"]["uuid"]


# ── interval deltas (Pebble per-minute records) ───────────────────

def _interval_sample(steps=12, ts=TS, interval=60, bpm=None,
                     active_kcal=None, distance_m=None):
    return HealthSample(
        device_address=ADDR, device_name="Pebble",
        reading=Reading(steps=steps, heart_rate_bpm=bpm, timestamp=ts,
                        interval_seconds=interval, active_kcal=active_kcal,
                        distance_m=distance_m))


def test_interval_active_energy_and_distance_records():
    recs = _by_type(build_records(
        _interval_sample(steps=30, active_kcal=0.45, distance_m=33.0)))
    assert recs["active_energy"]["value"] == 0.45
    assert recs["active_energy"]["unit"] == "kcal"
    assert recs["distance"]["value"] == 33.0
    assert recs["distance"]["unit"] == "m"
    assert recs["active_energy"]["uuid"] == f"tock:{ADDR}:active_energy:{int(TS)}"
    assert "effective_end" in recs["distance"]


def test_energy_distance_omitted_when_absent():
    types = {r["type"] for r in build_records(_interval_sample())}
    assert "active_energy" not in types and "distance" not in types


def test_interval_steps_are_a_delta_record():
    rec = _by_type(build_records(_interval_sample()))["step_count"]
    assert rec["value"] == 12
    # Interval record (start..end), uuid keyed per interval start —
    # aggregation SUMS the minutes instead of upserting a daily total.
    assert "effective_end" in rec
    assert rec["uuid"] == f"tock:{ADDR}:step_count:{int(TS)}"


def test_interval_vs_cumulative_have_different_uuids():
    interval = _by_type(build_records(_interval_sample()))
    cumulative = _by_type(build_records(_sample()))
    assert interval["step_count"]["uuid"] != cumulative["step_count"]["uuid"]


# ── sleep episodes (structured) ───────────────────────────────────

def _sleep_sample(start=TS, end=TS + 8 * 3600,
                  deep_spans=((TS + 3600, TS + 3 * 3600),), nap=False):
    return SleepSample(
        device_address=ADDR, device_name="Pebble",
        session=Sleep(start=start, end=end, deep_spans=deep_spans, is_nap=nap))


def test_sleep_record_is_a_structured_episode():
    rec = build_sleep_record(_sleep_sample())
    assert rec["type"] == "sleep_episode"
    assert rec["unit"] is None
    assert rec["value"]["total_sleep_minutes"] == 8 * 60
    assert "effective_start" in rec and "effective_end" in rec
    assert rec["uuid"] == f"tock:{ADDR}:sleep_episode:{int(TS)}"
    assert "meta" not in rec  # not a nap


def test_sleep_stages_tile_window_with_deep_in_place():
    rec = build_sleep_record(_sleep_sample())
    stages = rec["value"]["stages"]
    assert [st["stage"] for st in stages] == ["light", "deep", "light"]
    assert stages[0]["end"] == stages[1]["start"]
    assert stages[1]["end"] == stages[2]["start"]
    assert stages[0]["start"] == rec["effective_start"]
    assert stages[-1]["end"] == rec["effective_end"]


def test_sleep_without_deep_is_one_light_span():
    rec = build_sleep_record(_sleep_sample(deep_spans=(), nap=True))
    assert [st["stage"] for st in rec["value"]["stages"]] == ["light"]
    assert rec["meta"] == {"nap": True}


# ── workouts (structured) ─────────────────────────────────────────

def _workout_sample(kind="run", start=TS, end=TS + 1800, steps=3000,
                    active_kcal=210.0, distance_m=4200.0):
    return WorkoutSample(
        device_address=ADDR, device_name="Pebble",
        workout=Workout(start=start, end=end, kind=kind, steps=steps,
                        active_kcal=active_kcal, distance_m=distance_m))


def test_workout_record_is_structured():
    rec = build_workout_record(_workout_sample())
    assert rec["type"] == "workout"
    assert rec["unit"] is None
    v = rec["value"]
    assert v["activity_name"] == "run"
    assert v["duration_seconds"] == 1800
    assert v["distance_meters"] == 4200.0
    assert v["active_energy_kcal"] == 210.0
    assert v["steps"] == 3000
    assert rec["meta"]["activity_name"] == "run"
    assert rec["uuid"] == f"tock:{ADDR}:workout:{int(TS)}"


def test_workout_record_omits_absent_metrics():
    rec = build_workout_record(
        _workout_sample(steps=None, active_kcal=None, distance_m=None))
    v = rec["value"]
    assert "steps" not in v and "distance_meters" not in v
    assert "active_energy_kcal" not in v
    assert v["activity_name"] == "run"


# ── hydration ─────────────────────────────────────────────────────
def _hydration_sample(**kw):
    return HydrationSample(device_address=ADDR, device_name="WaterH Bottle",
                           reading=Hydration(**kw))


def test_hydration_record_scalar_ml():
    rec = build_hydration_record(
        _hydration_sample(amount_ml=250.0, timestamp=TS))
    assert rec["type"] == "water_intake"
    assert rec["value"] == 250.0
    assert rec["unit"] == "mL"
    assert rec["source"]["modality"] == "sensed"
    # deterministic per-drink uuid so a re-drained log upserts
    assert rec["uuid"] == f"tock:{ADDR}:water_intake:{int(TS)}"
    assert "meta" not in rec


def test_hydration_record_carries_water_quality_meta():
    rec = build_hydration_record(
        _hydration_sample(amount_ml=330.0, timestamp=TS,
                          temperature_c=21.0, tds_ppm=48))
    assert rec["meta"] == {"water_temperature_c": 21.0, "tds_ppm": 48}


def test_hydration_record_uuid_is_per_second():
    a = build_hydration_record(_hydration_sample(amount_ml=100.0, timestamp=TS))
    b = build_hydration_record(
        _hydration_sample(amount_ml=100.0, timestamp=TS + 60))
    assert a["uuid"] != b["uuid"]
