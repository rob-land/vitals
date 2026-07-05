-- 0002: the single-app era.
--
-- Vitals absorbed the per-app consent model (grants are meaningless with
-- one writer) and gained a device registry: every paired watch and every
-- known sensor lives here, replacing tock's paired-device-* GSettings keys.

CREATE TABLE IF NOT EXISTS devices (
    address       TEXT PRIMARY KEY,   -- BLE MAC (or stable identifier)
    name          TEXT NOT NULL,      -- advertised / user-visible name
    kind          TEXT NOT NULL,      -- device plugin id (pebble, bangle, ...)
    role          TEXT NOT NULL DEFAULT 'watch',  -- watch | sensor
    enabled       INTEGER NOT NULL DEFAULT 1,
    settings_json TEXT,               -- per-device settings (alarms, toggles)
    last_sync_ms  INTEGER,
    last_battery  INTEGER,
    created_at    INTEGER NOT NULL
);

DROP TABLE IF EXISTS grants;
