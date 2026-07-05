"""Tests for one-time pulse database adoption.

Builds a faithful schema-v1 pulse database (0001 only, grants table and
all), adopts it, and checks the copy migrates forward with history,
sequence numbers and the replication cursor intact.
"""

import shutil
from pathlib import Path

import pytest

from vitals.core import migrate, records, resources
from vitals.core.catalog import Catalog
from vitals.core.store import Store

_SCHEMA = Path(__file__).resolve().parent.parent / "data" / "schema"


@pytest.fixture()
def pulse_dir(tmp_path, monkeypatch):
    """A fake ~/.local/share/pulse holding a v1 database + cursor."""
    # Schema dir with only the v1 migration, like the pulse daemon shipped.
    v1_schema = tmp_path / "pulse-schema"
    (v1_schema / "sql").mkdir(parents=True)
    shutil.copy(_SCHEMA / "record-types.yaml", v1_schema)
    shutil.copy(_SCHEMA / "sql" / "0001_initial.sql", v1_schema / "sql")

    pulse = tmp_path / "pulse-data"
    pulse.mkdir()
    monkeypatch.setenv("VITALS_DATA_DIR", str(v1_schema))
    store = Store(str(pulse / "health.db"))
    assert store.migrate() == 1
    cat = Catalog.load()
    batch = [records.validate_and_canonicalize({
        "uuid": f"tock:dev:step_count:2026-06-0{d}",
        "type": "step_count",
        "effective_start": f"2026-06-0{d}T00:00:00+00:00",
        "value": 1000 * d, "unit": "{steps}",
        "source": {"modality": "sensed", "device_id": "dev",
                   "device_name": "Watch"},
    }, cat.get("step_count")) for d in (1, 2, 3)]
    store.insert_records(batch, "land.rob.tock")
    store.close()
    (pulse / "replicate-cursor").write_text("2")
    monkeypatch.delenv("VITALS_DATA_DIR")
    return pulse


@pytest.fixture()
def target(tmp_path, monkeypatch):
    dest = tmp_path / "vitals-data"
    monkeypatch.setenv("VITALS_USER_DATA_DIR", str(dest))
    return dest


def test_adopt_copies_migrates_and_preserves_seq(pulse_dir, target, monkeypatch):
    monkeypatch.setenv("VITALS_ADOPT_DIR", str(pulse_dir))
    assert migrate.needs_adoption()

    result = migrate.adopt()
    assert result["adopted"] is True and result["records"] == 3

    store = Store(str(resources.db_path()))
    assert store.migrate() == 2  # 0002 applied on the copy
    rows, _ = store.read_records(["step_count"])
    assert len(rows) == 3
    assert [r["seq"] for r in rows] == [1, 2, 3]  # change feed intact
    assert [r["app_id"] for r in rows] == ["land.rob.tock"] * 3
    tables = {r[0] for r in store.connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    assert "devices" in tables and "grants" not in tables
    store.close()

    # Replication cursor came along, so nothing re-pushes to Vault.
    assert (target / "replicate-cursor").read_text() == "2"


def test_adopt_never_overwrites_an_existing_db(pulse_dir, target, monkeypatch):
    monkeypatch.setenv("VITALS_ADOPT_DIR", str(pulse_dir))
    assert migrate.adopt()["adopted"] is True
    assert migrate.needs_adoption() is False
    assert migrate.adopt()["adopted"] is False  # second run is a no-op


def test_nothing_to_adopt_is_a_clean_noop(target, monkeypatch, tmp_path):
    monkeypatch.setenv("VITALS_ADOPT_DIR", str(tmp_path / "empty"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))  # hide any real pulse DB
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    assert migrate.needs_adoption() is False
    assert migrate.adopt()["adopted"] is False
