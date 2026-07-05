"""CSV export of the whole health store (the csv_sink's successor).

One flat file, one row per record, canonical units; structured values
stay JSON in the ``value`` column. Runs against its own read-only
connection so callers may use it from a worker thread.
"""

from __future__ import annotations

import csv
import sqlite3

from vitals.core.records import ms_to_iso

_HEADER = ["uuid", "type", "effective_start", "effective_end", "value",
           "unit", "modality", "device_name", "app_id", "meta"]

_SELECT = """
    SELECT s.uuid, s.type, s.effective_start, s.effective_end,
           s.value_num, s.value_json, s.unit, s.modality, s.meta_json,
           src.app_id AS app_id, src.display_name AS display_name
    FROM samples s JOIN sources src ON s.source_id = src.id
    WHERE s.deleted = 0
    ORDER BY s.effective_start ASC, s.uuid ASC
"""


def row_to_csv(row) -> list:
    value = row["value_num"]
    if value is None:
        value = row["value_json"]
    return [
        row["uuid"], row["type"],
        ms_to_iso(row["effective_start"]),
        ms_to_iso(row["effective_end"]) if row["effective_end"] is not None else "",
        value, row["unit"] or "", row["modality"],
        row["display_name"] or "", row["app_id"], row["meta_json"] or "",
    ]


def export_to_path(db_path: str, out_path: str) -> int:
    """Write every live record to ``out_path``; returns the row count."""
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        with open(out_path, "w", newline="") as fh:
            writer = csv.writer(fh)
            writer.writerow(_HEADER)
            count = 0
            for row in con.execute(_SELECT):
                writer.writerow(row_to_csv(row))
                count += 1
    finally:
        con.close()
    return count
