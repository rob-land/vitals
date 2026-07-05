"""Tests for the time-sync wire format on each device plugin."""

from datetime import datetime

from vitals.devices.bangle import BangleDevice
from vitals.devices.pinetime import PineTimeDevice


# ── PineTime CTS payload ──────────────────────────────────────────

def test_pinetime_cts_payload_length():
    """The Bluetooth SIG Current Time characteristic is 10 bytes."""
    payload = PineTimeDevice._encode_current_time(0)
    assert len(payload) == 10


def test_pinetime_cts_payload_year_le():
    """First two bytes are uint16 year, little-endian."""
    # 2026-01-01 00:00:00 local
    ts = datetime(2026, 1, 1).timestamp()
    payload = PineTimeDevice._encode_current_time(ts)
    assert payload[0] == 0xEA  # 2026 & 0xFF
    assert payload[1] == 0x07  # 2026 >> 8


def test_pinetime_cts_payload_month_day_time():
    """Bytes 2..7 are month, day, hour, minute, second, day-of-week."""
    # Tue 2026-05-12 14:30:45 local
    ts = datetime(2026, 5, 12, 14, 30, 45).timestamp()
    p = PineTimeDevice._encode_current_time(ts)
    assert p[2] == 5     # month
    assert p[3] == 12    # day
    assert p[4] == 14    # hour
    assert p[5] == 30    # minute
    assert p[6] == 45    # second
    assert p[7] == 2     # ISO weekday: Tue


def test_pinetime_cts_payload_fractions_and_reason_zero():
    """Fractions256 + adjust-reason are both 0 by default."""
    p = PineTimeDevice._encode_current_time(0)
    assert p[8] == 0
    assert p[9] == 0


# ── Bangle.js sched.json shape ────────────────────────────────────

def test_bangle_sched_json_empty_list():
    assert BangleDevice._render_sched_json([]) == "[]"


def test_bangle_sched_json_one_alarm():
    """7:30 every day -> appid=alarm, t=27000000 ms, dow=0x7F, on/rp=true."""
    from vitals.alarms import DAYS_EVERY_DAY, Alarm

    a = Alarm(hour=7, minute=30, label="Wake",
              days=DAYS_EVERY_DAY, enabled=True)
    body = BangleDevice._render_sched_json([a])
    import json
    parsed = json.loads(body)
    assert len(parsed) == 1
    item = parsed[0]
    # Type discriminator: "alarm" goes in appid, NOT t. (The opposite
    # mistake in v0.3.2 made every alarm land at 00:00 on the watch
    # because t was being parsed as numeric and falling through to 0.)
    assert item["appid"] == "alarm"
    assert item["on"] is True
    assert item["rp"] is True
    # 7:30 = 7*3600 + 30*60 = 27000 sec = 27_000_000 ms
    assert item["t"] == 27_000_000
    assert isinstance(item["t"], int)
    assert item["last"] == 0
    assert item["msg"] == "Wake"
    # Bangle's dow uses bit 0 = Sunday, bit 6 = Saturday. Every-day = 0x7F.
    assert item["dow"] == 0x7F
    # Repeating alarm should not be auto-deleted on fire.
    assert item["del"] is False


def test_bangle_sched_json_one_shot():
    """days=0 (one-shot) sets rp=false and del=true."""
    from vitals.alarms import DAYS_NEVER, Alarm
    a = Alarm(hour=8, minute=0, days=DAYS_NEVER)
    body = BangleDevice._render_sched_json([a])
    import json
    parsed = json.loads(body)
    assert parsed[0]["rp"] is False
    assert parsed[0]["dow"] == 0
    assert parsed[0]["del"] is True
    assert parsed[0]["t"] == 8 * 3600 * 1000


def test_bangle_dow_bit_mapping_monday():
    """Bit 0 in our mask = Monday; in Bangle's mask Monday = bit 1."""
    from vitals.alarms import Alarm
    a = Alarm(hour=8, minute=0, days=0b0000001)  # Monday only
    body = BangleDevice._render_sched_json([a])
    import json
    assert json.loads(body)[0]["dow"] == 0b0000010


def test_bangle_sched_json_midnight_is_zero_ms():
    """Edge case: 00:00 maps to t=0 (this is the value v0.3.2 was
    accidentally producing for every alarm)."""
    from vitals.alarms import Alarm
    a = Alarm(hour=0, minute=0)
    body = BangleDevice._render_sched_json([a])
    import json
    assert json.loads(body)[0]["t"] == 0


def test_bangle_sched_json_label_falls_back_to_alarm():
    """Empty label is replaced with 'Alarm' so the watch shows
    something rather than a blank notification."""
    from vitals.alarms import Alarm
    a = Alarm(hour=7, minute=30, label="")
    body = BangleDevice._render_sched_json([a])
    import json
    assert json.loads(body)[0]["msg"] == "Alarm"


# ── REPL response parsers ─────────────────────────────────────────

def test_extract_print_output_typical_response():
    response = 'print(Bangle.getBattery())\r\n85\r\n=undefined\r\n>'
    assert BangleDevice._extract_print_output(response) == "85"


def test_extract_print_output_missing_returns_none():
    assert BangleDevice._extract_print_output("") is None
    assert BangleDevice._extract_print_output("just gibberish") is None


def test_parse_battery_from_print_output():
    response = 'print(Bangle.getBattery())\r\n85\r\n=undefined\r\n>'
    assert BangleDevice._parse_battery(response) == 85


def test_parse_battery_handles_float_response():
    """Some Espruino Bangle.getBattery() implementations return a
    float (0..1 normalized) rather than an int 0..100. Round and
    accept rather than fail — anything in the valid output range
    should produce a usable number."""
    # 0.85 normalized → 1 (rounded). Edge case but parser-tolerant.
    response = 'print(Bangle.getBattery())\r\n0.85\r\n=undefined\r\n>'
    result = BangleDevice._parse_battery(response)
    assert result is not None
    assert 0 <= result <= 100


def test_parse_battery_strips_whitespace_around_value():
    """Defensive: the print output line may have stray whitespace
    around the integer (some firmwares add a trailing space)."""
    response = 'print(Bangle.getBattery())\r\n  85  \r\n=undefined\r\n>'
    assert BangleDevice._parse_battery(response) == 85


def test_parse_battery_one_percent():
    """1% must round-trip; v0.3.7's parser was too eager and would
    return 1 even when the actual battery wasn't 1."""
    response = 'print(Bangle.getBattery())\r\n1\r\n=undefined\r\n>'
    assert BangleDevice._parse_battery(response) == 1


def test_parse_battery_returns_none_on_garbage():
    assert BangleDevice._parse_battery("") is None
    assert BangleDevice._parse_battery("no digits here") is None


def test_parse_sched_response_typical():
    """The watch's sched.json comes back through print() as a JSON
    string between the echo and the prompt's =undefined."""
    response = (
        'print(require("Storage").read("sched.json")||"[]")\r\n'
        '[{"id":"abc","appid":"alarm","t":27000000}]\r\n'
        '=undefined\r\n>'
    )
    parsed = BangleDevice._parse_sched_response(response)
    assert parsed == [{"id": "abc", "appid": "alarm", "t": 27000000}]


def test_parse_sched_response_empty_file_returns_empty_list():
    """The `||"[]"` fallback produces an empty array as a string."""
    response = (
        'print(require("Storage").read("sched.json")||"[]")\r\n'
        '[]\r\n=undefined\r\n>'
    )
    assert BangleDevice._parse_sched_response(response) == []


def test_parse_sched_response_undefined_returns_empty_list():
    """Defensive: if for some reason the watch echoes 'undefined'
    instead of the empty array, we still return []."""
    response = (
        'print(require("Storage").read("sched.json"))\r\n'
        'undefined\r\n=undefined\r\n>'
    )
    assert BangleDevice._parse_sched_response(response) == []


def test_parse_sched_response_unparseable_json_logs_and_returns_empty():
    response = (
        'print(require("Storage").read("sched.json")||"[]")\r\n'
        'corrupt[bytes\r\n=undefined\r\n>'
    )
    assert BangleDevice._parse_sched_response(response) == []


# ── reconcile (alarm merge) ───────────────────────────────────────

def test_reconcile_keeps_watch_only_alarms():
    """An alarm set directly on the watch (id Vitals has never seen)
    must survive a Vitals sync."""
    existing = [
        {"id": "watch-set-1", "appid": "alarm", "t": 28800000, "on": True},
        {"id": "tock-managed-x", "appid": "alarm", "t": 27000000, "on": True},
    ]
    kept, dropped = BangleDevice._reconcile_existing(
        existing,
        current_ids={"tock-managed-x"},
        previously_pushed_ids=set(),
    )
    assert {e["id"] for e in kept} == {"watch-set-1"}
    assert dropped == 1


def test_reconcile_drops_alarms_in_previously_pushed():
    """An alarm Vitals pushed last time but no longer has in its list
    (user deleted it from Vitals) must be dropped from the watch."""
    existing = [
        {"id": "stale-tock", "appid": "alarm", "t": 27000000, "on": True},
        {"id": "watch-set-1", "appid": "alarm", "t": 28800000, "on": True},
    ]
    kept, dropped = BangleDevice._reconcile_existing(
        existing,
        current_ids=set(),  # Vitals has no alarms now
        previously_pushed_ids={"stale-tock"},
    )
    assert {e["id"] for e in kept} == {"watch-set-1"}
    assert dropped == 1


def test_reconcile_handles_empty_watch():
    """First-ever sync: nothing on the watch yet."""
    kept, dropped = BangleDevice._reconcile_existing(
        existing=[],
        current_ids={"abc"},
        previously_pushed_ids=set(),
    )
    assert kept == []
    assert dropped == 0


def test_reconcile_skips_non_dict_entries():
    """Defensive: corrupt sched.json may contain non-dict junk; we
    just drop those rather than crash."""
    existing = [
        {"id": "ok", "appid": "alarm", "t": 0},
        "junk string",
        None,
        42,
    ]
    kept, dropped = BangleDevice._reconcile_existing(
        existing,
        current_ids=set(),
        previously_pushed_ids=set(),
    )
    assert kept == [{"id": "ok", "appid": "alarm", "t": 0}]


def test_parse_activity_typical_response():
    """Bangle.getHealthStatus() returns a JSON object with bpm, steps,
    bpmConfidence and other fields. We pull steps + HR + confidence."""
    response = (
        'try{print(JSON.stringify(Bangle.getHealthStatus()))}'
        'catch(e){print("null")}\r\n'
        '{"bpm":72,"bpmConfidence":85,"steps":4321,"movement":1234}\r\n'
        '=undefined\r\n>'
    )
    reading = BangleDevice._parse_activity(response)
    assert reading is not None
    assert reading.steps == 4321
    assert reading.heart_rate_bpm == 72
    assert reading.heart_rate_confidence == 85


def test_parse_activity_zero_bpm_with_low_confidence_means_no_reading():
    """Bangle reports bpm=0 with confidence=0 when the HR sensor isn't
    on. Treat that as 'unknown' rather than '0 bpm' so the UI doesn't
    show a misleading 0."""
    response = (
        'try{print(JSON.stringify(Bangle.getHealthStatus()))}'
        'catch(e){print("null")}\r\n'
        '{"bpm":0,"bpmConfidence":0,"steps":1234}\r\n'
        '=undefined\r\n>'
    )
    reading = BangleDevice._parse_activity(response)
    assert reading is not None
    assert reading.steps == 1234
    assert reading.heart_rate_bpm is None
    assert reading.heart_rate_confidence is None


def test_parse_activity_zero_bpm_with_high_confidence_kept():
    """A genuine zero reading with high confidence is kept (rare but
    valid for paused HR sensors)."""
    response = (
        'try{print(JSON.stringify(Bangle.getHealthStatus()))}'
        'catch(e){print("null")}\r\n'
        '{"bpm":0,"bpmConfidence":100,"steps":50}\r\n'
        '=undefined\r\n>'
    )
    reading = BangleDevice._parse_activity(response)
    assert reading is not None
    assert reading.heart_rate_bpm == 0


def test_parse_activity_missing_method_returns_none():
    """Firmware without `Bangle.getHealthStatus` triggers the
    try/catch on the watch and prints 'null'. Parser returns None."""
    response = (
        'try{print(JSON.stringify(Bangle.getHealthStatus()))}'
        'catch(e){print("null")}\r\n'
        'null\r\n=undefined\r\n>'
    )
    assert BangleDevice._parse_activity(response) is None


def test_parse_activity_unparseable_json_returns_none():
    response = (
        'try{print(JSON.stringify(Bangle.getHealthStatus()))}'
        'catch(e){print("null")}\r\n'
        '{corrupt[bytes\r\n=undefined\r\n>'
    )
    assert BangleDevice._parse_activity(response) is None


def test_parse_activity_partial_fields():
    """Different firmware versions expose different fields; missing
    ones become None on the dataclass."""
    response = (
        'try{print(JSON.stringify(Bangle.getHealthStatus()))}'
        'catch(e){print("null")}\r\n'
        '{"steps":99}\r\n=undefined\r\n>'
    )
    reading = BangleDevice._parse_activity(response)
    assert reading is not None
    assert reading.steps == 99
    assert reading.heart_rate_bpm is None


def test_reconcile_overlap_between_current_and_previous():
    """current_ids and previously_pushed_ids commonly overlap when
    the user's alarm list is unchanged across syncs. Make sure each
    matching entry is only dropped once."""
    existing = [
        {"id": "a", "appid": "alarm"},
        {"id": "b", "appid": "alarm"},
    ]
    kept, dropped = BangleDevice._reconcile_existing(
        existing,
        current_ids={"a", "b"},
        previously_pushed_ids={"a", "b"},
    )
    assert kept == []
    assert dropped == 2
