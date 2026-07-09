"""A small cache of foods the user has logged, so a food entered once can
be re-added without re-typing its nutrients.

Stored as JSON next to the health database (not in it — it's a
convenience cache, not health data). Most-recently-used first, deduped
by name, capped so it stays a *recents* list rather than growing without
bound. Only named foods are cached; an unlabelled entry has nothing to
show or match on.
"""

from __future__ import annotations

import json
import logging

from vitals.core import resources

log = logging.getLogger(__name__)

_MAX_ENTRIES = 60
_FILENAME = "food-cache.json"


def _path():
    return resources.user_data_dir() / _FILENAME


def load() -> list[dict]:
    """Recently-logged foods, most recent first. Each entry has ``label``
    and ``nutrients`` (Open Food Facts nutriment key → amount) and,
    optionally, ``amount_g`` / ``barcode``."""
    try:
        data = json.loads(_path().read_text())
    except FileNotFoundError:
        return []
    except Exception:
        log.exception("food cache: could not read %s", _path())
        return []
    return data if isinstance(data, list) else []


def remember(label: str, nutrients: dict, amount_g: float | None = None,
             barcode: str | None = None) -> None:
    """Record a logged food, moving it to the front and dropping any
    older entry with the same (case-insensitive) name."""
    label = (label or "").strip()
    if not label or not nutrients:
        return
    entry: dict = {"label": label, "nutrients": dict(nutrients)}
    if amount_g:
        entry["amount_g"] = amount_g
    if barcode:
        entry["barcode"] = barcode
    items = [e for e in load()
             if e.get("label", "").strip().lower() != label.lower()]
    items.insert(0, entry)
    del items[_MAX_ENTRIES:]
    _save(items)


def _save(items: list[dict]) -> None:
    try:
        path = _path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(items))
    except Exception:
        log.exception("food cache: could not write %s", _path())
