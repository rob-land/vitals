"""The SQLite store layer.

Vitals is the only writer of this database. This class owns the
connection, runs migrations, and provides insert / read / aggregate /
change-feed operations. It speaks ``NormalizedRecord`` in and dict rows
out.
"""

from __future__ import annotations

import base64
import logging
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from vitals.core import resources
from vitals.core.records import NormalizedRecord

log = logging.getLogger(__name__)

# Columns that, if changed, mean a stored record is genuinely different and
# should be re-written (bumping the change-feed seq). Mirrors the SQL trigger.
_MUTABLE = ("value_num", "value_json", "unit", "modality", "meta_json",
            "effective_start", "effective_end", "deleted")

# A read joins each sample to its source so the envelope can carry provenance.
_SELECT = """
    SELECT s.uuid, s.type, s.schema_version, s.effective_start, s.effective_end,
           s.value_num, s.value_json, s.unit, s.modality, s.meta_json,
           s.created_at, s.modified_at, s.seq, s.deleted,
           src.app_id AS app_id, src.device_id AS device_id, src.display_name AS display_name
    FROM samples s JOIN sources src ON s.source_id = src.id
"""

_MAX_LIMIT = 10_000


def _now_ms() -> int:
    return round(time.time() * 1000)


class Store:
    def __init__(self, db_path: str):
        self.db_path = db_path
        self._con = sqlite3.connect(db_path)
        self._con.row_factory = sqlite3.Row
        self._con.execute("PRAGMA journal_mode = WAL")
        self._con.execute("PRAGMA foreign_keys = ON")

    def close(self) -> None:
        self._con.close()

    @property
    def connection(self) -> sqlite3.Connection:
        """Shared connection, so the consent layer reuses it (single writer)."""
        return self._con

    # -- migrations ----------------------------------------------------
    def migrate(self) -> int:
        cur_version = self._con.execute("PRAGMA user_version").fetchone()[0]
        applied = cur_version
        for path in resources.migration_files():
            index = int(path.name.split("_", 1)[0])
            if index <= cur_version:
                continue
            log.info("applying migration %s", path.name)
            with self._con:
                self._con.executescript(path.read_text())
                self._con.execute(f"PRAGMA user_version = {index}")
            applied = index
        if applied != cur_version:
            log.info("store migrated to schema version %d", applied)
        return applied

    def schema_version(self) -> int:
        return self._con.execute("PRAGMA user_version").fetchone()[0]

    def latest_seq(self) -> int:
        row = self._con.execute("SELECT value FROM meta WHERE key='seq_counter'").fetchone()
        return int(row[0]) if row else 0

    # -- sources -------------------------------------------------------
    def _source_id(self, app_id: str, device_id: str, device_name: str | None, now: int) -> int:
        # device_id is normalised to '' (never NULL) so UNIQUE dedup works,
        # because SQLite treats NULLs as distinct in unique indexes.
        row = self._con.execute(
            "SELECT id FROM sources WHERE app_id=? AND device_id=?",
            (app_id, device_id),
        ).fetchone()
        if row:
            self._con.execute(
                "UPDATE sources SET last_seen=?, display_name=COALESCE(?, display_name) WHERE id=?",
                (now, device_name, row["id"]),
            )
            return row["id"]
        cur = self._con.execute(
            "INSERT INTO sources(app_id, device_id, display_name, first_seen, last_seen) "
            "VALUES (?,?,?,?,?)",
            (app_id, device_id, device_name, now, now),
        )
        return cur.lastrowid

    # -- writes --------------------------------------------------------
    def insert_records(self, records: list[NormalizedRecord], app_id: str) -> dict:
        """Upsert a batch (idempotent on uuid). Returns a summary dict."""
        now = _now_ms()
        stored = 0
        duplicates = 0
        types: set[str] = set()
        with self._con:  # one transaction for the whole batch
            for rec in records:
                source_id = self._source_id(app_id, rec.device_id or "", rec.device_name, now)
                existing = self._con.execute(
                    "SELECT value_num, value_json, unit, modality, meta_json, "
                    "effective_start, effective_end, deleted FROM samples WHERE uuid=?",
                    (rec.uuid,),
                ).fetchone()
                new_vals = (
                    rec.value_num, rec.value_json, rec.unit, rec.modality, rec.meta_json,
                    rec.effective_start, rec.effective_end, 0,
                )
                if existing is None:
                    self._con.execute(
                        "INSERT INTO samples(uuid, type, schema_version, effective_start, "
                        "effective_end, value_num, value_json, unit, source_id, modality, "
                        "meta_json, created_at, modified_at) "
                        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (rec.uuid, rec.type, rec.schema_version, rec.effective_start,
                         rec.effective_end, rec.value_num, rec.value_json, rec.unit,
                         source_id, rec.modality, rec.meta_json, now, now),
                    )
                    stored += 1
                    types.add(rec.type)
                elif tuple(existing[c] for c in _MUTABLE) != new_vals:
                    self._con.execute(
                        "UPDATE samples SET value_num=?, value_json=?, unit=?, modality=?, "
                        "meta_json=?, effective_start=?, effective_end=?, deleted=0, "
                        "source_id=?, modified_at=? WHERE uuid=?",
                        (rec.value_num, rec.value_json, rec.unit, rec.modality, rec.meta_json,
                         rec.effective_start, rec.effective_end, source_id, now, rec.uuid),
                    )
                    stored += 1
                    types.add(rec.type)
                else:
                    duplicates += 1
        return {
            "stored": stored,
            "duplicates": duplicates,
            "types": sorted(types),
            "high_seq": self.latest_seq(),
        }

    def delete_record(self, uuid: str) -> bool:
        """Tombstone a record (kept in the change feed for replication)."""
        now = _now_ms()
        with self._con:
            cur = self._con.execute(
                "UPDATE samples SET deleted=1, modified_at=? WHERE uuid=? AND deleted=0",
                (now, uuid),
            )
        return cur.rowcount > 0

    # -- reads ---------------------------------------------------------
    def read_records(self, types, start_ms=None, end_ms=None, sources=None,
                      limit=1000, cursor=None) -> tuple[list[sqlite3.Row], str | None]:
        if not types:
            return [], None
        limit = max(1, min(int(limit), _MAX_LIMIT))
        where = ["s.deleted = 0", f"s.type IN ({','.join('?' * len(types))})"]
        params: list = list(types)
        if start_ms is not None:
            where.append("s.effective_start >= ?")
            params.append(start_ms)
        if end_ms is not None:
            where.append("s.effective_start < ?")
            params.append(end_ms)
        if sources:
            where.append(f"src.app_id IN ({','.join('?' * len(sources))})")
            params.extend(sources)
        if cursor:
            c_start, c_uuid = _decode_cursor(cursor)
            where.append("(s.effective_start > ? OR (s.effective_start = ? AND s.uuid > ?))")
            params.extend([c_start, c_start, c_uuid])

        sql = (_SELECT + " WHERE " + " AND ".join(where)
               + " ORDER BY s.effective_start ASC, s.uuid ASC LIMIT ?")
        params.append(limit + 1)
        rows = self._con.execute(sql, params).fetchall()

        next_cursor = None
        if len(rows) > limit:
            rows = rows[:limit]
            last = rows[-1]
            next_cursor = _encode_cursor(last["effective_start"], last["uuid"])
        return rows, next_cursor

    def get_changes(self, since_seq: int, limit: int) -> tuple[list[sqlite3.Row], int]:
        limit = max(1, min(int(limit), _MAX_LIMIT))
        sql = _SELECT + " WHERE s.seq > ? ORDER BY s.seq ASC LIMIT ?"
        rows = self._con.execute(sql, (since_seq, limit)).fetchall()
        next_seq = rows[-1]["seq"] if rows else since_seq
        return rows, next_seq

    # -- aggregation ---------------------------------------------------
    def aggregate(self, type_key: str, op: str, bucket: str,
                  start_ms=None, end_ms=None, tz="UTC", modality=None,
                  source_trust: dict[str, int] | None = None,
                  discrepancy_threshold: float | None = None,
                  value_range: tuple[float, float] | None = None) -> list[dict]:
        """Bucket a scalar type over time. Buckets honour the given tz.

        By default every sample in a bucket is aggregated together,
        regardless of which device produced it. That double-counts
        additive metrics (steps reported by two devices) and blends point
        metrics across quality tiers.

        Pass ``source_trust`` — a ``{device_id: rank}`` map (higher =
        more trusted; ``""`` is the manual-entry / no-device source) — to
        resolve each bucket to a *single* source: the highest-ranked
        source present, breaking ties by sample count then id. The op is
        then applied to only that source's values, and each result
        carries the chosen ``source``. This is the correct cross-source
        behaviour; without the map the legacy behaviour is unchanged.
        """
        # Joining to `sources` (for the device id) forces table aliases;
        # `col` qualifies column names only when we join.
        resolving = source_trust is not None
        if resolving:
            select = ("s.effective_start AS effective_start, "
                      "s.value_num AS value_num, src.device_id AS device_id")
            table = "samples s JOIN sources src ON s.source_id = src.id"
            col = lambda c: "s." + c  # noqa: E731
        else:
            select = "effective_start, value_num"
            table = "samples"
            col = lambda c: c  # noqa: E731
        where = [f"{col('type')} = ?", f"{col('deleted')} = 0",
                 f"{col('value_num')} IS NOT NULL"]
        params: list = [type_key]
        if start_ms is not None:
            where.append(f"{col('effective_start')} >= ?"); params.append(start_ms)
        if end_ms is not None:
            where.append(f"{col('effective_start')} < ?"); params.append(end_ms)
        if modality:
            where.append(f"{col('modality')} = ?"); params.append(modality)
        if value_range is not None:
            # Drop physiologically impossible samples (sensor glitches like a
            # 0-bpm heart rate) before they corrupt the min/avg/max.
            where.append(f"{col('value_num')} >= ?"); params.append(value_range[0])
            where.append(f"{col('value_num')} <= ?"); params.append(value_range[1])
        rows = self._con.execute(
            f"SELECT {select} FROM {table} WHERE {' AND '.join(where)}",
            params,
        ).fetchall()

        try:
            zone = ZoneInfo(tz)
        except Exception:
            zone = timezone.utc

        if not resolving:
            groups: dict[str, list[float]] = {}
            for row in rows:
                key = _bucket_key(row["effective_start"], bucket, zone)
                groups.setdefault(key, []).append(row["value_num"])
            return [{"start": key, "value": _apply_op(op, groups[key]),
                     "n": len(groups[key])} for key in sorted(groups)]

        # Source-resolved: bucket -> device_id -> values.
        by_bucket: dict[str, dict[str, list[float]]] = {}
        for row in rows:
            key = _bucket_key(row["effective_start"], bucket, zone)
            dev = row["device_id"] or ""
            by_bucket.setdefault(key, {}).setdefault(dev, []).append(
                row["value_num"])

        out = []
        for key in sorted(by_bucket):
            per_source = by_bucket[key]
            chosen = _pick_source(per_source, source_trust)
            vals = per_source[chosen]
            entry = {"start": key, "value": _apply_op(op, vals),
                     "n": len(vals), "source": chosen}
            # Flag the sources we dropped that materially disagree with the
            # chosen one, so the UI can surface it instead of hiding them.
            if discrepancy_threshold is not None and len(per_source) > 1:
                others = {d: _apply_op(op, v) for d, v in per_source.items()
                          if d != chosen}
                disagree = {d: round(val, 2) for d, val in others.items()
                            if _relative_diff(entry["value"], val)
                            > discrepancy_threshold}
                if disagree:
                    entry["discrepancy"] = disagree
            out.append(entry)
        return out

    def types_for_device(self, device_id: str) -> list[str]:
        """The distinct record types a source (by device_id; "" is the
        manual-entry / no-device source) has produced."""
        rows = self._con.execute(
            "SELECT DISTINCT s.type FROM samples s "
            "JOIN sources src ON s.source_id = src.id "
            "WHERE src.device_id = ? AND s.deleted = 0 ORDER BY s.type",
            (device_id or "",),
        ).fetchall()
        return [r["type"] for r in rows]


# -- helpers -----------------------------------------------------------
def _pick_source(per_source: dict[str, list[float]],
                 trust: dict[str, int]) -> str:
    """The device_id to represent a bucket: highest trust, then the one
    with the most samples, then a stable id order."""
    return max(per_source,
               key=lambda dev: (trust.get(dev, 0), len(per_source[dev]), dev))


def _relative_diff(a: float, b: float) -> float:
    """Relative difference between two values, robust near zero."""
    denom = max(abs(a), abs(b), 1.0)
    return abs(a - b) / denom


def _apply_op(op: str, vals: list[float]) -> float:
    if op == "sum":
        return sum(vals)
    if op == "avg":
        return sum(vals) / len(vals)
    if op == "min":
        return min(vals)
    if op == "max":
        return max(vals)
    if op == "count":
        return len(vals)
    raise ValueError(f"unknown aggregate op {op!r}")


def _bucket_key(ms: int, bucket: str, zone) -> str:
    dt = datetime.fromtimestamp(ms / 1000, tz=timezone.utc).astimezone(zone)
    if bucket == "hour":
        start = dt.replace(minute=0, second=0, microsecond=0)
    elif bucket == "day":
        start = dt.replace(hour=0, minute=0, second=0, microsecond=0)
    elif bucket == "week":
        midnight = dt.replace(hour=0, minute=0, second=0, microsecond=0)
        start = midnight - timedelta(days=midnight.weekday())  # Monday
    elif bucket == "month":
        start = dt.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    else:
        raise ValueError(f"unknown bucket {bucket!r}")
    return start.isoformat()


def _encode_cursor(effective_start: int, uuid: str) -> str:
    return base64.urlsafe_b64encode(f"{effective_start}\n{uuid}".encode()).decode()


def _decode_cursor(cursor: str) -> tuple[int, str]:
    start, uuid = base64.urlsafe_b64decode(cursor.encode()).decode().split("\n", 1)
    return int(start), uuid
