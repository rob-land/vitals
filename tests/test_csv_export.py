"""Tests for the whole-store CSV export."""

import csv

from vitals.core import records
from vitals.core.catalog import Catalog
from vitals.core.csv_export import export_to_path
from vitals.core.store import Store


def test_export_all_records(tmp_path):
    cat = Catalog.load()
    db = tmp_path / "health.db"
    store = Store(str(db))
    store.migrate()

    def norm(e):
        return records.validate_and_canonicalize(e, cat.get(e["type"]))

    store.insert_records([
        norm({"uuid": "w1", "type": "body_weight",
              "effective_start": "2026-07-01T08:00:00+00:00",
              "value": 80.0, "unit": "kg",
              "source": {"modality": "self_reported",
                         "device_name": "Manual entry"}}),
        norm({"uuid": "bp1", "type": "blood_pressure",
              "effective_start": "2026-07-01T08:05:00+00:00",
              "value": {"systolic": 120, "diastolic": 80},
              "source": {"modality": "sensed", "device_id": "AA:BB",
                         "device_name": "Cuff"}}),
    ], "land.rob.vitals")
    store.delete_record("bp1")  # tombstones stay out of the export
    store.close()

    out = tmp_path / "export.csv"
    assert export_to_path(str(db), str(out)) == 1

    rows = list(csv.DictReader(open(out)))
    (row,) = rows
    assert row["uuid"] == "w1" and row["type"] == "body_weight"
    assert float(row["value"]) == 80.0 and row["unit"] == "kg"
    assert row["device_name"] == "Manual entry"
    assert row["app_id"] == "land.rob.vitals"
