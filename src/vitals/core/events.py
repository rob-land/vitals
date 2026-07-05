"""In-process change notifications.

The pulse daemon broadcast a ``RecordsChanged`` D-Bus signal; with the
store in-process a plain GObject signal does the same job. The
application owns one ``RecordBus`` and every page that renders health
data connects to it.
"""

from __future__ import annotations

from gi.repository import GObject


class RecordBus(GObject.Object):
    """Emitted after records land in (or vanish from) the store."""

    __gsignals__ = {
        # arg: tuple of affected type keys, e.g. ("heart_rate", "step_count")
        "records-changed": (GObject.SignalFlags.RUN_FIRST, None, (object,)),
    }

    def emit_changed(self, types) -> None:
        affected = tuple(sorted(set(types)))
        if affected:
            self.emit("records-changed", affected)
