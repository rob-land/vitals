"""MPRIS bridge — the phone side of watch music control.

Tracks every ``org.mpris.MediaPlayer2.*`` player on the session bus,
follows the active one's ``PropertiesChanged``, and exposes the two
things watches need: a ``NowPlaying`` snapshot (emitted as
``now-playing-changed``) and simple playback commands. Runs on the
main thread; the DeviceManager marshals watch-side commands here and
pushes snapshots back over the persistent links.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from gi.repository import Gio, GLib, GObject

log = logging.getLogger(__name__)

_PREFIX = "org.mpris.MediaPlayer2."
_PLAYER_IFACE = "org.mpris.MediaPlayer2.Player"
_PATH = "/org/mpris/MediaPlayer2"
_DEBOUNCE_MS = 300


@dataclass(frozen=True)
class NowPlaying:
    artist: str = ""
    album: str = ""
    track: str = ""
    duration_s: int = 0
    position_s: int = 0
    playing: bool = False


class MprisBridge(GObject.Object):
    __gsignals__ = {
        # A fresh NowPlaying snapshot after track/state changes.
        "now-playing-changed": (GObject.SignalFlags.RUN_FIRST, None,
                                (object,)),
    }

    def __init__(self):
        super().__init__()
        self._bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        self._emit_pending = 0
        self._bus.signal_subscribe(
            "org.freedesktop.DBus", "org.freedesktop.DBus",
            "NameOwnerChanged", "/org/freedesktop/DBus", None,
            Gio.DBusSignalFlags.NONE, self._on_name_owner_changed)
        self._bus.signal_subscribe(
            None, "org.freedesktop.DBus.Properties", "PropertiesChanged",
            _PATH, None, Gio.DBusSignalFlags.NONE,
            self._on_properties_changed)

    # ── players ───────────────────────────────────────────────────
    def players(self) -> list[str]:
        try:
            names = self._bus.call_sync(
                "org.freedesktop.DBus", "/org/freedesktop/DBus",
                "org.freedesktop.DBus", "ListNames", None,
                GLib.VariantType.new("(as)"), Gio.DBusCallFlags.NONE,
                1000, None).unpack()[0]
        except GLib.Error:
            return []
        return [n for n in names if n.startswith(_PREFIX)]

    def _get_property(self, name: str, prop: str):
        try:
            return self._bus.call_sync(
                name, _PATH, "org.freedesktop.DBus.Properties", "Get",
                GLib.Variant("(ss)", (_PLAYER_IFACE, prop)),
                GLib.VariantType.new("(v)"), Gio.DBusCallFlags.NONE,
                1000, None).unpack()[0]
        except GLib.Error:
            return None

    def active_player(self) -> str | None:
        """The player a watch should control: playing > paused > any."""
        best, best_rank = None, -1
        for name in self.players():
            status = self._get_property(name, "PlaybackStatus")
            rank = {"Playing": 2, "Paused": 1}.get(status, 0)
            if rank > best_rank:
                best, best_rank = name, rank
        return best

    # ── snapshot ──────────────────────────────────────────────────
    def now_playing(self) -> NowPlaying:
        name = self.active_player()
        if name is None:
            return NowPlaying()
        meta = self._get_property(name, "Metadata") or {}
        status = self._get_property(name, "PlaybackStatus")
        position = self._get_property(name, "Position") or 0
        artists = meta.get("xesam:artist") or []
        return NowPlaying(
            artist=artists[0] if artists else "",
            album=meta.get("xesam:album") or "",
            track=meta.get("xesam:title") or "",
            duration_s=int((meta.get("mpris:length") or 0) / 1_000_000),
            position_s=int(position / 1_000_000),
            playing=status == "Playing",
        )

    # ── control ───────────────────────────────────────────────────
    def command(self, cmd: str) -> None:
        """One watch command: play/pause/playpause/next/previous/
        volumeup/volumedown."""
        name = self.active_player()
        if name is None:
            log.info("mpris: no player for %r", cmd)
            return
        method = {"playpause": "PlayPause", "play": "Play",
                  "pause": "Pause", "next": "Next",
                  "previous": "Previous"}.get(cmd)
        try:
            if method is not None:
                self._bus.call_sync(name, _PATH, _PLAYER_IFACE, method,
                                    None, None, Gio.DBusCallFlags.NONE,
                                    1000, None)
            elif cmd in ("volumeup", "volumedown"):
                volume = self._get_property(name, "Volume")
                if volume is None:
                    return
                step = 0.1 if cmd == "volumeup" else -0.1
                self._bus.call_sync(
                    name, _PATH, "org.freedesktop.DBus.Properties", "Set",
                    GLib.Variant("(ssv)", (_PLAYER_IFACE, "Volume",
                                           GLib.Variant(
                                               "d", max(0.0, min(1.0,
                                                        volume + step))))),
                    None, Gio.DBusCallFlags.NONE, 1000, None)
            else:
                log.warning("mpris: unknown command %r", cmd)
        except GLib.Error:
            log.warning("mpris: %r failed on %s", cmd, name, exc_info=True)

    # ── change tracking ───────────────────────────────────────────
    def _on_name_owner_changed(self, _c, _s, _p, _i, _m, params) -> None:
        name = params.unpack()[0]
        if name.startswith(_PREFIX):
            self._schedule_emit()

    def _on_properties_changed(self, _c, _sender, _path, _iface, _member,
                               params) -> None:
        iface = params.unpack()[0]
        if iface == _PLAYER_IFACE:
            self._schedule_emit()

    def _schedule_emit(self) -> None:
        # Debounce: track changes fire several PropertiesChanged.
        if self._emit_pending:
            GLib.source_remove(self._emit_pending)
        self._emit_pending = GLib.timeout_add(_DEBOUNCE_MS, self._emit_now)

    def _emit_now(self) -> bool:
        self._emit_pending = 0
        self.emit("now-playing-changed", self.now_playing())
        return GLib.SOURCE_REMOVE
