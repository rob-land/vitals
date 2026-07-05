"""Cohort-shared autostart-entry helper.

The XDG Background portal would be the conventional channel to
register a per-user autostart .desktop, but Phosh (FuriOS,
postmarketOS, …) ships an xdg-desktop-portal with no Background
implementer — `RequestBackground` hangs forever waiting for a
Response signal that never gets emitted. Verified on FuriOS 14:
introspection on /org/freedesktop/portal/desktop shows no
`org.freedesktop.portal.Background` interface, and none of the
loaded backends (gtk, phosh, phosh-shell, phrosh, gnome-keyring)
declares the impl interface either.

So write the .desktop directly. The user already consented by
flipping the switch in Preferences, so the portal's permission
dialog is redundant; same end-state on GNOME desktop and Phosh.

Inside Flatpak we need write access to the host's autostart dir;
the manifest grants it via `--filesystem=xdg-config/autostart:create`.

Path used: ~/.config/autostart/<app_id>.desktop on the host. The
sandbox rewrites XDG_CONFIG_HOME to the per-app dir, so we can't
follow that env var — go via Path.home() / ".config" directly.

API: parameterized on `app_id` and `app_name`. The helper is a
verbatim copy across cohort apps; only the caller's arguments
differ.
"""

import logging
import os
from pathlib import Path

from gi.repository import GLib

log = logging.getLogger(__name__)


def autostart_commandline(app_id: str) -> list:
    """Default argv for the autostart entry: `<binary> --background`,
    wrapped in `flatpak run` when we're inside the sandbox.

    Binary name is the lowercased last dot-segment of app_id (cohort
    convention: land.rob.vitals → vitals)."""
    binary = app_id.rsplit('.', 1)[-1].lower()
    if os.path.exists("/.flatpak-info"):
        return ["flatpak", "run", f"--command={binary}", app_id, "--background"]
    return [binary, "--background"]


def _autostart_path(app_id: str) -> Path:
    if os.path.exists("/.flatpak-info"):
        # Sandbox rewrites XDG_CONFIG_HOME to ~/.var/app/<id>/config;
        # the host's ~/.config/autostart is bind-mounted at the
        # literal ~/.config/autostart path via the manifest's
        # --filesystem=xdg-config/autostart:create grant.
        base = str(Path.home() / ".config")
    else:
        base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "autostart" / f"{app_id}.desktop"


def _desktop_file_body(app_name: str, commandline: list) -> str:
    exec_line = " ".join(GLib.shell_quote(arg) for arg in commandline)
    return (
        "[Desktop Entry]\n"
        "Type=Application\n"
        f"Name={app_name}\n"
        f"Exec={exec_line}\n"
        "X-GNOME-Autostart-enabled=true\n"
        "X-GNOME-Autostart-Phase=Applications\n"
        "X-GNOME-Autostart-Delay=3\n"
        "NoDisplay=true\n"
    )


def request_background(*, autostart: bool, app_id: str, app_name: str,
                       commandline: list = None,
                       on_response=None, **_ignored) -> None:
    """Write or remove the per-user autostart .desktop for the app.
    Invokes `on_response(0)` on success or `(2)` on filesystem error,
    scheduled on the GLib main loop so UI handlers run in the right
    context.

    `**_ignored` swallows portal-era kwargs (e.g. parent_xdg_handle)
    that callers no longer need to pass."""
    if commandline is None:
        commandline = autostart_commandline(app_id)

    path = _autostart_path(app_id)
    try:
        if autostart:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(_desktop_file_body(app_name, commandline))
            log.info("autostart: wrote %s", path)
        else:
            try:
                path.unlink()
                log.info("autostart: removed %s", path)
            except FileNotFoundError:
                pass
        code = 0
    except OSError as e:
        log.warning("autostart: filesystem error: %s", e)
        code = 2

    if on_response:
        GLib.idle_add(on_response, code)
