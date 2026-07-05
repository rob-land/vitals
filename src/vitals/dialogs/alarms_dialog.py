"""Alarms-for-this-device dialog.

v0.5.x kept the alarm list in app preferences, which (a) didn't make
sense — alarms belong to a watch, not the app — and (b) leaked one
watch's alarms onto another when the user re-paired. v0.6.0 moves
this into a standalone dialog reachable from the dashboard's
"Alarms" button when a watch is paired, backed by per-device
storage in `vitals.alarms.load_for_entry` / `save_for_entry`.

Constructed imperatively (no .blp) — same pattern as PairingDialog.
"""

from __future__ import annotations

from gi.repository import Adw, Gtk

from vitals.alarms import (
    DAYS_EVERY_DAY,
    DAYS_NEVER,
    DAYS_WEEKDAYS,
    DAYS_WEEKENDS,
    Alarm,
    load_for_entry,
    save_for_entry,
)

_DAY_PRESETS = [
    ("Every day", DAYS_EVERY_DAY),
    ("Weekdays",  DAYS_WEEKDAYS),
    ("Weekends",  DAYS_WEEKENDS),
    ("Once",      DAYS_NEVER),
]


class AlarmsDialog(Adw.Dialog):
    __gtype_name__ = "VitalsAlarmsDialog"

    def __init__(self, manager, address: str, device_name: str = ""):
        super().__init__()
        self._manager = manager
        self._address = address

        title = f"Alarms — {device_name}" if device_name else "Alarms"
        self.set_title(title)
        self.set_content_width(420)
        self.set_content_height(520)

        toolbar = Adw.ToolbarView()
        header = Adw.HeaderBar()
        toolbar.add_top_bar(header)

        add_btn = Gtk.Button.new_from_icon_name("list-add-symbolic")
        add_btn.set_tooltip_text("Add alarm")
        add_btn.connect("clicked", self._on_add_clicked)
        header.pack_end(add_btn)

        self._scrolled = Gtk.ScrolledWindow()
        self._scrolled.set_policy(
            Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self._scrolled.set_vexpand(True)

        self._list_box = Gtk.ListBox()
        self._list_box.set_selection_mode(Gtk.SelectionMode.NONE)
        self._list_box.add_css_class("boxed-list")
        self._list_box.set_margin_top(12)
        self._list_box.set_margin_bottom(12)
        self._list_box.set_margin_start(12)
        self._list_box.set_margin_end(12)

        self._scrolled.set_child(self._list_box)
        toolbar.set_content(self._scrolled)
        self.set_child(toolbar)

        self._refresh()

    # ── List rendering ────────────────────────────────────────────

    def _refresh(self) -> None:
        # Clear current rows.
        child = self._list_box.get_first_child()
        while child:
            self._list_box.remove(child)
            child = self._list_box.get_first_child()

        alarms = load_for_entry(self._manager.get(self._address))
        if not alarms:
            empty = Adw.ActionRow(
                title="No alarms",
                subtitle="Tap + above to add one. Pushed to the watch "
                         "on the next sync.")
            empty.set_sensitive(False)
            self._list_box.append(empty)
            return
        for alarm in alarms:
            self._list_box.append(self._row_for_alarm(alarm))

    def _row_for_alarm(self, alarm: Alarm) -> Adw.ActionRow:
        row = Adw.ActionRow(activatable=True)
        row.set_title(alarm.label or alarm.time_str())
        row.set_subtitle(f"{alarm.time_str()} · {alarm.days_str()}")

        switch = Gtk.Switch()
        switch.set_valign(Gtk.Align.CENTER)
        switch.set_active(alarm.enabled)
        switch.connect(
            "notify::active",
            lambda s, _, a=alarm: self._toggle_alarm(a, s.get_active()))
        row.add_suffix(switch)

        delete = Gtk.Button.new_from_icon_name("user-trash-symbolic")
        delete.add_css_class("flat")
        delete.set_tooltip_text("Remove alarm")
        delete.set_valign(Gtk.Align.CENTER)
        delete.connect("clicked", lambda _b, a=alarm: self._delete_alarm(a))
        row.add_suffix(delete)

        row.connect("activated", lambda _r, a=alarm: self._edit_alarm(a))
        return row

    # ── Storage helpers ───────────────────────────────────────────

    def _all(self) -> list[Alarm]:
        return load_for_entry(self._manager.get(self._address))

    def _save(self, alarms: list[Alarm]) -> None:
        save_for_entry(self._manager, self._address, alarms)
        self._refresh()

    def _toggle_alarm(self, target: Alarm, enabled: bool) -> None:
        updated = []
        for a in self._all():
            if a.id == target.id:
                updated.append(Alarm(
                    id=a.id, hour=a.hour, minute=a.minute,
                    label=a.label, days=a.days, enabled=enabled))
            else:
                updated.append(a)
        # Don't re-render rows on a toggle — would lose switch focus.
        save_for_entry(self._manager, self._address, updated)

    def _delete_alarm(self, target: Alarm) -> None:
        self._save([a for a in self._all() if a.id != target.id])

    # ── Add / edit ────────────────────────────────────────────────

    def _on_add_clicked(self, *_):
        self._edit_alarm(
            Alarm(hour=7, minute=0, label="", days=DAYS_EVERY_DAY))

    def _edit_alarm(self, alarm: Alarm) -> None:
        existing = next((a for a in self._all() if a.id == alarm.id), None)
        is_new = existing is None

        dialog = Adw.AlertDialog()
        dialog.set_heading("Edit Alarm" if not is_new else "New Alarm")

        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        body.set_margin_top(8)

        hour_spin = Gtk.SpinButton.new_with_range(0, 23, 1)
        hour_spin.set_value(alarm.hour)
        minute_spin = Gtk.SpinButton.new_with_range(0, 59, 1)
        minute_spin.set_value(alarm.minute)
        time_box = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL,
            spacing=6, halign=Gtk.Align.CENTER)
        time_box.append(hour_spin)
        time_box.append(Gtk.Label(label=":"))
        time_box.append(minute_spin)
        body.append(time_box)

        label_entry = Gtk.Entry()
        label_entry.set_placeholder_text("Label (optional)")
        label_entry.set_text(alarm.label)
        body.append(label_entry)

        days_combo = Gtk.DropDown.new_from_strings(
            [name for name, _ in _DAY_PRESETS])
        active_idx = 0
        for i, (_name, mask) in enumerate(_DAY_PRESETS):
            if mask == alarm.days:
                active_idx = i
                break
        days_combo.set_selected(active_idx)
        body.append(days_combo)

        dialog.set_extra_child(body)
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("save", "Save")
        dialog.set_response_appearance(
            "save", Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response("save")

        def on_response(_d, response):
            if response != "save":
                return
            mask = _DAY_PRESETS[days_combo.get_selected()][1]
            updated = Alarm(
                id=alarm.id,
                hour=int(hour_spin.get_value()),
                minute=int(minute_spin.get_value()),
                label=label_entry.get_text().strip(),
                days=mask,
                enabled=alarm.enabled if not is_new else True,
            )
            current = self._all()
            current = [a for a in current if a.id != updated.id]
            current.append(updated)
            current.sort(key=lambda a: (a.hour, a.minute))
            self._save(current)

        dialog.connect("response", on_response)
        dialog.present(self)
