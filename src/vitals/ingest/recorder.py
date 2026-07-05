"""The single write path into the health store.

Every producer — a watch sync, a passive sensor reading, a manual entry
form — hands envelope dicts to the one ``Recorder``, which validates
them against the catalog, inserts them (idempotent uuid upsert) and
announces the change on the ``RecordBus``.

Threading: the SQLite connection lives on the GTK main thread. Code on
the BLE worker loop must use ``ingest_from_thread``, which marshals the
batch over with ``GLib.idle_add``; ``ingest`` itself is main-thread-only.
"""

from __future__ import annotations

import logging

from gi.repository import GLib

from vitals.core.catalog import Catalog
from vitals.core.errors import InvalidRecord
from vitals.core.events import RecordBus
from vitals.core.records import validate_and_canonicalize
from vitals.core.store import Store

log = logging.getLogger(__name__)

APP_ID = "land.rob.vitals"


class Recorder:
    def __init__(self, store: Store, catalog: Catalog, bus: RecordBus,
                 app_id: str = APP_ID):
        self._store = store
        self._catalog = catalog
        self._bus = bus
        self._app_id = app_id

    def ingest(self, envelopes: list[dict]) -> dict:
        """Validate and store a batch. Returns the store summary plus a
        ``rejected`` list of ``(uuid, reason)`` for envelopes that failed
        validation — rejects never abort the rest of the batch."""
        normalized = []
        rejected: list[tuple[str | None, str]] = []
        for env in envelopes:
            td = self._catalog.get(env.get("type", ""))
            if td is None:
                rejected.append((env.get("uuid"), f"unknown type {env.get('type')!r}"))
                continue
            try:
                normalized.append(validate_and_canonicalize(env, td))
            except InvalidRecord as exc:
                rejected.append((env.get("uuid"), str(exc)))
        summary = self._store.insert_records(normalized, self._app_id)
        summary["rejected"] = rejected
        for uuid, reason in rejected:
            log.warning("rejected record %s: %s", uuid, reason)
        if summary["stored"]:
            self._bus.emit_changed(summary["types"])
        return summary

    def ingest_from_thread(self, envelopes: list[dict]) -> None:
        """Queue a batch from a worker thread onto the main thread."""
        GLib.idle_add(self._ingest_idle, list(envelopes),
                      priority=GLib.PRIORITY_DEFAULT)

    def _ingest_idle(self, envelopes: list[dict]) -> bool:
        try:
            self.ingest(envelopes)
        except Exception:
            log.exception("ingest failed for a batch of %d", len(envelopes))
        return GLib.SOURCE_REMOVE

    def delete(self, uuid: str) -> bool:
        """Tombstone one record and announce the change."""
        row = self._store.connection.execute(
            "SELECT type FROM samples WHERE uuid=?", (uuid,)).fetchone()
        if not self._store.delete_record(uuid):
            return False
        if row:
            self._bus.emit_changed([row["type"]])
        return True
