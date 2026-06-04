"""Permissions page — the consent UI.

Lists which apps may read or write which health data, and lets the user
grant or revoke access. This replaces the interim ``land.rob.pulse.Admin``
command-line dance: authorising a source like Tock is now a dialog rather
than a gdbus call. (A future Pulse portal will let apps *request* access and
have it appear here as a prompt; for now grants are added explicitly.)
"""

from __future__ import annotations

import logging

from gi.repository import Adw, Gtk

from vitals.pages import PulsePage
from vitals.pulse_client import PulseUnavailable

log = logging.getLogger(__name__)

# Presets offered in the grant dialog: label -> list of type keys.
_DATA_PRESETS = [
    ("Everything (all data types)", ["*"]),
    ("Activity (steps, heart rate, energy, distance)",
     ["step_count", "heart_rate", "active_energy", "distance"]),
    ("Heart rate", ["heart_rate"]),
    ("Steps", ["step_count"]),
]
_ACCESS = ["read", "write"]


class PermissionsPage(PulsePage):
    def __init__(self, client):
        super().__init__()
        self._client = client
        self._app_groups: list[Gtk.Widget] = []

        scroller = Gtk.ScrolledWindow(
            hscrollbar_policy=Gtk.PolicyType.NEVER, vexpand=True)
        self._page = Adw.PreferencesPage()
        scroller.set_child(self._page)
        self._set_content(scroller)

        intro = Adw.PreferencesGroup(
            description="Control which apps may read or write each kind of "
                        "health data. All data stays in the local Pulse store.")
        grant_button = Gtk.Button(label="Grant access…", halign=Gtk.Align.START)
        grant_button.add_css_class("suggested-action")
        grant_button.connect("clicked", lambda *_: self._open_grant_dialog())
        intro.add(grant_button)
        self._page.add(intro)

        # Pending access requests appear above the granted-apps list.
        self._requests_group = Adw.PreferencesGroup(
            title="Requests", visible=False,
            description="Apps asking for access. Approve to grant what they "
                        "requested.")
        self._page.add(self._requests_group)
        self._request_rows: list[Gtk.Widget] = []

        # Live-update when a source asks for (or is granted) access.
        self._client.subscribe_requests(self.refresh)

    def refresh(self) -> None:
        try:
            requests = self._client.list_requests()
            grants = self._client.list_grants()
            self._rebuild_requests(requests)
            self._rebuild(grants)
            self._show_content()
        except PulseUnavailable:
            self._show_unavailable()

    # ── pending requests ──────────────────────────────────────────
    def _rebuild_requests(self, requests: list[dict]) -> None:
        for row in self._request_rows:
            self._requests_group.remove(row)
        self._request_rows = []
        self._requests_group.set_visible(bool(requests))

        for req in requests:
            parts = []
            if req.get("write"):
                parts.append("write " + ", ".join(req["write"]))
            if req.get("read"):
                parts.append("read " + ", ".join(req["read"]))
            row = Adw.ActionRow(title=req["app_id"],
                                subtitle="; ".join(parts) or "no data types")
            deny = Gtk.Button(label="Deny", valign=Gtk.Align.CENTER)
            deny.add_css_class("flat")
            deny.connect("clicked", self._on_deny, req["app_id"])
            approve = Gtk.Button(label="Approve", valign=Gtk.Align.CENTER)
            approve.add_css_class("suggested-action")
            approve.connect("clicked", self._on_approve, req["app_id"])
            row.add_suffix(deny)
            row.add_suffix(approve)
            self._requests_group.add(row)
            self._request_rows.append(row)

    def _on_approve(self, _button, app_id: str) -> None:
        try:
            self._client.approve_request(app_id)
        except PulseUnavailable:
            self._show_unavailable()
            return
        self._toast(f"Granted access to {app_id}")
        self.refresh()

    def _on_deny(self, _button, app_id: str) -> None:
        try:
            self._client.deny_request(app_id)
        except PulseUnavailable:
            self._show_unavailable()
            return
        self._toast(f"Dismissed request from {app_id}")
        self.refresh()

    # ── grants list ───────────────────────────────────────────────
    def _rebuild(self, grants: list[dict]) -> None:
        for group in self._app_groups:
            self._page.remove(group)
        self._app_groups = []

        by_app: dict[str, dict[str, list[str]]] = {}
        for grant in grants:
            entry = by_app.setdefault(grant["app_id"], {"read": [], "write": []})
            entry[grant["access"]].append(grant["type"])

        if not by_app:
            empty = Adw.PreferencesGroup()
            empty.add(Adw.ActionRow(
                title="No apps have access yet",
                subtitle="Use “Grant access…” to authorise a source like Tock"))
            self._page.add(empty)
            self._app_groups.append(empty)
            return

        for app_id in sorted(by_app):
            group = Adw.PreferencesGroup(title=app_id)
            for access in _ACCESS:
                types = sorted(by_app[app_id][access])
                if not types:
                    continue
                row = Adw.ActionRow(
                    title=access.capitalize(),
                    subtitle=", ".join(types))
                revoke = Gtk.Button(
                    icon_name="user-trash-symbolic", valign=Gtk.Align.CENTER,
                    tooltip_text=f"Revoke {access} access")
                revoke.add_css_class("flat")
                revoke.connect("clicked", self._on_revoke, app_id, types, access)
                row.add_suffix(revoke)
                group.add(row)
            self._page.add(group)
            self._app_groups.append(group)

    def _on_revoke(self, _button, app_id: str, types: list[str], access: str) -> None:
        try:
            self._client.revoke(app_id, types, access)
        except PulseUnavailable:
            self._show_unavailable()
            return
        self._toast(f"Revoked {access} access from {app_id}")
        self.refresh()

    # ── grant dialog ──────────────────────────────────────────────
    def _open_grant_dialog(self) -> None:
        dialog = Adw.Dialog(title="Grant access", content_width=480)
        toolbar = Adw.ToolbarView()
        header = Adw.HeaderBar(show_start_title_buttons=False,
                               show_end_title_buttons=False)
        cancel = Gtk.Button(label="Cancel")
        cancel.connect("clicked", lambda *_: dialog.close())
        grant = Gtk.Button(label="Grant")
        grant.add_css_class("suggested-action")
        header.pack_start(cancel)
        header.pack_end(grant)
        toolbar.add_top_bar(header)

        clamp = Adw.Clamp(margin_top=12, margin_bottom=12,
                          margin_start=12, margin_end=12)
        group = Adw.PreferencesGroup()
        app_row = Adw.EntryRow(title="App ID")
        app_row.set_text("land.rob.tock")
        access_row = Adw.ComboRow(title="Access",
                                  model=Gtk.StringList.new(["Read", "Write"]))
        access_row.set_selected(1)  # write — the common case for a source
        data_row = Adw.ComboRow(
            title="Data",
            model=Gtk.StringList.new([label for label, _ in _DATA_PRESETS]))
        group.add(app_row)
        group.add(access_row)
        group.add(data_row)
        clamp.set_child(group)
        toolbar.set_content(clamp)
        dialog.set_child(toolbar)

        grant.connect("clicked", self._on_grant_confirmed,
                      dialog, app_row, access_row, data_row)
        dialog.present(self)

    def _on_grant_confirmed(self, _button, dialog, app_row, access_row, data_row):
        app_id = app_row.get_text().strip()
        if not app_id:
            self._toast("Enter an app ID to grant access")
            return
        access = _ACCESS[access_row.get_selected()]
        types = _DATA_PRESETS[data_row.get_selected()][1]
        try:
            self._client.grant(app_id, types, access)
        except PulseUnavailable:
            dialog.close()
            self._show_unavailable()
            return
        dialog.close()
        self._toast(f"Granted {access} access to {app_id}")
        self.refresh()
