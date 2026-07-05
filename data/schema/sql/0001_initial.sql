-- Pulse health store — initial schema (migration 0001)
--
-- Engine: SQLite (WAL mode). One file at
--   $XDG_DATA_HOME/pulse/health.db   (typically ~/.local/share/pulse/).
-- The pulse daemon is the ONLY writer; client apps reach the data over
-- D-Bus, never by opening this file. See docs/design/03-storage.md.
--
-- Design notes that the schema encodes:
--   * Every sample carries a client-assigned UUID and is de-duplicated on
--     it, so an app re-sending a record after a crash is idempotent.
--   * `seq` is a monotonic, gap-free-per-write change counter that powers
--     the GetChanges() replication feed. Edits and tombstones bump it.
--   * Values are stored in the type's canonical UCUM unit (see
--     record-types.yaml); conversion happens at the daemon boundary.
--   * Scalars go in value_num; structured/component bodies go in
--     value_json. Exactly one is non-null for a live row.

PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;
PRAGMA user_version = 1;

-- ---------------------------------------------------------------------
-- Key/value metadata: schema version, the change-feed high-water mark,
-- the store's stable replica id, etc.
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- Seed rows (the daemon writes replica_id on first run).
INSERT OR IGNORE INTO meta(key, value) VALUES
    ('schema_version', '1'),
    ('seq_counter',    '0');

-- ---------------------------------------------------------------------
-- Sources: the (app, device) pairs that have written data. Denormalised
-- out of samples so the per-row footprint stays small and so a viewer can
-- present "data from <device>" without scanning every sample.
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS sources (
    id           INTEGER PRIMARY KEY,
    app_id       TEXT NOT NULL,            -- e.g. 'land.rob.tock'
    device_id    TEXT,                     -- stable per-device id, or NULL
    display_name TEXT,                     -- 'PineTime', 'Mi Scale', ...
    first_seen   INTEGER NOT NULL,         -- epoch ms UTC
    last_seen    INTEGER NOT NULL,
    UNIQUE(app_id, device_id)
);

-- ---------------------------------------------------------------------
-- Samples: the canonical record store.
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS samples (
    uuid            TEXT PRIMARY KEY,       -- client-assigned (UUIDv4/v7); dedup key
    type            TEXT NOT NULL,          -- taxonomy key from record-types.yaml
    schema_version  INTEGER NOT NULL DEFAULT 1,

    effective_start INTEGER NOT NULL,       -- epoch ms UTC
    effective_end   INTEGER,                -- NULL => point-in-time observation

    value_num       REAL,                   -- scalar value, in canonical unit
    value_json      TEXT,                   -- structured/component body (JSON)
    unit            TEXT,                    -- UCUM code; canonical for the type

    source_id       INTEGER NOT NULL REFERENCES sources(id),
    modality        TEXT NOT NULL,          -- 'sensed' | 'self_reported' | 'derived'
    meta_json       TEXT,                   -- type-specific extra fields (JSON)

    created_at      INTEGER NOT NULL,       -- epoch ms UTC, first insert
    modified_at     INTEGER NOT NULL,       -- epoch ms UTC, last edit
    seq             INTEGER NOT NULL DEFAULT 0,  -- change-feed sequence; set by trigger after insert

    deleted         INTEGER NOT NULL DEFAULT 0,  -- tombstone (1) for replication

    -- exactly one value channel is populated for a live (non-deleted) row
    CHECK (deleted = 1 OR (value_num IS NOT NULL) <> (value_json IS NOT NULL)),
    CHECK (modality IN ('sensed', 'self_reported', 'derived')),
    CHECK (effective_end IS NULL OR effective_end >= effective_start)
);

-- Read path: "all <type> between t0 and t1" and aggregation buckets.
CREATE INDEX IF NOT EXISTS idx_samples_type_time
    ON samples(type, effective_start);

-- Replication path: "everything changed since seq N".
CREATE INDEX IF NOT EXISTS idx_samples_seq
    ON samples(seq);

-- Source-filtered reads ("data from this device").
CREATE INDEX IF NOT EXISTS idx_samples_source
    ON samples(source_id, type, effective_start);

-- ---------------------------------------------------------------------
-- Permission grants: which app may read/write which type. '*' matches any
-- type EXCEPT categories flagged sensitive (reproductive), which always
-- require an explicit per-type grant. Enforced by the daemon, recorded
-- here so grants survive restarts and are auditable.
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS grants (
    id         INTEGER PRIMARY KEY,
    app_id     TEXT NOT NULL,
    type       TEXT NOT NULL,               -- a type key, or '*'
    access     TEXT NOT NULL,               -- 'read' | 'write'
    granted_at INTEGER NOT NULL,            -- epoch ms UTC
    expires_at INTEGER,                     -- NULL = until revoked
    CHECK (access IN ('read', 'write')),
    UNIQUE(app_id, type, access)
);

-- ---------------------------------------------------------------------
-- Blobs: large opaque payloads referenced by a sample's body — GPS tracks
-- (GPX/TCX/FIT), ECG waveforms, imported clinical documents. Kept out of
-- the hot samples table so range scans stay fast.
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS blobs (
    id         TEXT PRIMARY KEY,            -- referenced by meta_json/value_json
    media_type TEXT NOT NULL,               -- 'application/gpx+xml', 'application/vnd.ant.fit', ...
    bytes      BLOB NOT NULL,
    created_at INTEGER NOT NULL
);

-- ---------------------------------------------------------------------
-- Change-feed sequence. A single monotonic counter in `meta` is bumped on
-- every insert/update/delete and stamped onto the row's `seq`. Triggers
-- keep it authoritative even for writes that bypass the daemon's helper
-- (e.g. a migration). next = current + 1; readers page with
-- GetChanges(since_seq) ORDER BY seq.
-- ---------------------------------------------------------------------
CREATE TRIGGER IF NOT EXISTS samples_seq_insert
AFTER INSERT ON samples
BEGIN
    UPDATE meta SET value = CAST(CAST(value AS INTEGER) + 1 AS TEXT)
        WHERE key = 'seq_counter';
    UPDATE samples SET seq = (SELECT CAST(value AS INTEGER) FROM meta WHERE key = 'seq_counter')
        WHERE uuid = NEW.uuid;
END;

CREATE TRIGGER IF NOT EXISTS samples_seq_update
AFTER UPDATE OF value_num, value_json, unit, modality, meta_json, deleted, effective_start, effective_end ON samples
FOR EACH ROW WHEN NEW.seq = OLD.seq          -- guard against the trigger's own write recursing
BEGIN
    UPDATE meta SET value = CAST(CAST(value AS INTEGER) + 1 AS TEXT)
        WHERE key = 'seq_counter';
    UPDATE samples SET seq = (SELECT CAST(value AS INTEGER) FROM meta WHERE key = 'seq_counter'),
                       modified_at = NEW.modified_at
        WHERE uuid = NEW.uuid;
END;
