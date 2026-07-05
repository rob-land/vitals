"""One-time adoption of the pulse daemon's database.

Vitals inherits the health data that tock/larder/jot/gauge wrote through
the pulse daemon. On first run — no vitals database yet, a pulse one
found — the pulse DB is copied wholesale (``sqlite3`` online backup, so
a live WAL database copies consistently) into the vitals data dir, then
normal migrations bring the copy up to the current schema. ``samples``
rows keep their ``seq`` values and triggers, so Vault replication
continues from the copied cursor without re-pushing history.

After adoption the copy diverges from the original: the retiring apps
keep writing to a database nobody reads. The caller should tell the
user so.
"""

from __future__ import annotations

import logging
import os
import shutil
import sqlite3
from pathlib import Path

from vitals.core import resources

log = logging.getLogger(__name__)


def _pulse_data_dirs() -> list[Path]:
    """Candidate pulse data dirs, host install first, then its Flatpak."""
    candidates: list[Path] = []
    env = os.environ.get("VITALS_ADOPT_DIR")  # tests / manual override
    if env:
        candidates.append(Path(env))
    xdg = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
    candidates.append(Path(xdg) / "pulse")
    candidates.append(Path.home() / ".local/share/pulse")
    candidates.append(Path.home() / ".var/app/land.rob.pulse/data/pulse")
    seen: set[Path] = set()
    return [p for p in candidates if not (p in seen or seen.add(p))]


def find_source() -> Path | None:
    """Return the pulse database to adopt, if one exists."""
    for base in _pulse_data_dirs():
        db = base / "health.db"
        if db.is_file():
            return db
    return None


def needs_adoption() -> bool:
    return not resources.db_path().exists() and find_source() is not None


def adopt(source: Path | None = None) -> dict:
    """Copy the pulse database (and replication cursor) into place.

    Returns ``{"adopted": bool, "records": int, "source": str | None}``.
    Only ever runs when no vitals database exists yet; the caller runs
    ``Store.migrate()`` afterwards as it would on any startup.
    """
    dest = resources.db_path()
    if dest.exists():
        return {"adopted": False, "records": 0, "source": None}
    src = source or find_source()
    if src is None:
        return {"adopted": False, "records": 0, "source": None}

    dest.parent.mkdir(parents=True, exist_ok=True)
    src_con = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
    try:
        dst_con = sqlite3.connect(dest)
        try:
            src_con.backup(dst_con)
            count = dst_con.execute(
                "SELECT COUNT(*) FROM samples WHERE deleted = 0").fetchone()[0]
        finally:
            dst_con.close()
    finally:
        src_con.close()

    cursor = src.parent / "replicate-cursor"
    if cursor.is_file():
        shutil.copy(cursor, Path(resources.user_data_dir()) / "replicate-cursor")

    log.info("adopted pulse database %s (%d records)", src, count)
    return {"adopted": True, "records": count, "source": str(src)}
