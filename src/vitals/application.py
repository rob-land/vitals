"""Vitals — Adw.Application subclass owning the in-process health core."""

from __future__ import annotations

import logging

from gi.repository import Adw, Gio, GLib, Gtk

from vitals.ble import BleManager, BluetoothMonitor
from vitals.ble.bluetooth_state import BluetoothState
from vitals.ble.scan_broker import ScanBroker
from vitals.const import APP_ID, APP_NAME, VERSION
from vitals.core import migrate, resources
from vitals.core.catalog import Catalog
from vitals.core.events import RecordBus
from vitals.core.store import Store
from vitals.devices.manager import DeviceManager
from vitals.ingest import Recorder
from vitals.window import VitalsWindow

log = logging.getLogger(__name__)


class VitalsApplication(Adw.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID,
                         flags=Gio.ApplicationFlags.HANDLES_COMMAND_LINE)
        self.settings = Gio.Settings.new(APP_ID)
        self.store: Store | None = None
        self.catalog: Catalog | None = None
        self.record_bus = RecordBus()
        self.recorder: Recorder | None = None
        self.ble: BleManager | None = None
        self.bluetooth: BluetoothMonitor | None = None
        self.scan_broker: ScanBroker | None = None
        self.device_manager: DeviceManager | None = None
        self._adoption: dict | None = None
        # True when launched via --background (autostart): the app holds
        # without a window so background syncs run; a later activate
        # builds the window.
        self._background_launch = False
        self._held = False

        # Registered so --help lists it and Gio accepts it; the actual
        # level is read from sys.argv by configure_logging().
        self.add_main_option(
            "debug", ord("d"), GLib.OptionFlags.NONE, GLib.OptionArg.NONE,
            "Enable debug logging", None)
        self.add_main_option(
            "background", ord("b"), GLib.OptionFlags.NONE, GLib.OptionArg.NONE,
            "Run without showing a window (autostart entry uses this)", None)

        self._make_action("about", self._show_about)
        self._make_action("quit", lambda *_: self.quit())
        self._make_action("refresh", self._refresh)
        self._make_action("preferences", self._show_preferences)

        self.set_accels_for_action("app.quit", ["<Control>q"])
        self.set_accels_for_action("app.refresh", ["<Control>r"])
        self.set_accels_for_action("app.preferences", ["<Control>comma"])
        self.set_accels_for_action("win.show-help-overlay", ["<Control>question"])

    def _make_action(self, name: str, cb) -> Gio.SimpleAction:
        action = Gio.SimpleAction.new(name, None)
        action.connect("activate", cb)
        self.add_action(action)
        return action

    # ── lifecycle ─────────────────────────────────────────────────
    def do_startup(self):
        Adw.Application.do_startup(self)
        # First run on a machine with a pulse database: adopt it before
        # the store creates a fresh one (no-op in every other case).
        try:
            self._adoption = migrate.adopt()
        except Exception:
            log.exception("pulse database adoption failed; starting fresh")
            self._adoption = None
        resources.db_path().parent.mkdir(parents=True, exist_ok=True)
        self.store = Store(str(resources.db_path()))
        self.store.migrate()
        self.catalog = Catalog.load()
        self.recorder = Recorder(self.store, self.catalog, self.record_bus)
        self.ble = BleManager()
        self.ble.start()
        # Track adapter power and, on hosts that idle it off, turn it back
        # on so timed syncs don't fail before they start.
        self.bluetooth = BluetoothMonitor()
        self.bluetooth.start()
        self.bluetooth.power_on()
        # Re-power the adapter the moment the host idles it off, so it's
        # up and settled before a sync rather than racing a cold power-on.
        self.bluetooth.connect("state-changed", self._on_bluetooth_state)
        self.scan_broker = ScanBroker(self.ble)
        self.device_manager = DeviceManager(
            self.store, self.recorder, self.settings, self.ble,
            bluetooth=self.bluetooth)
        self.device_manager.attach_scan_broker(self.scan_broker)
        self.device_manager.reschedule_background_sync()
        self.settings.connect(
            "changed::background-sync-interval",
            lambda *_: self.device_manager.reschedule_background_sync())

    def _on_bluetooth_state(self, _monitor, state) -> None:
        # Hosts that idle the controller off would otherwise leave every
        # sync failing; power it straight back on. Edge-triggered on the
        # off transition, so a denied power-on can't spin.
        if state == BluetoothState.POWERED_OFF and self.bluetooth is not None:
            log.info("Bluetooth adapter powered off; powering back on")
            self.bluetooth.power_on()

    def do_shutdown(self):
        if self.bluetooth is not None:
            self.bluetooth.stop()
            self.bluetooth = None
        if self.ble is not None:
            self.ble.stop()
            self.ble = None
        if self.store is not None:
            self.store.close()
        Adw.Application.do_shutdown(self)

    def do_command_line(self, command_line) -> int:
        opts = command_line.get_options_dict().end().unpack()
        if opts.get("background"):
            # Autostart launch: hold without a window; the background
            # timer (armed in do_startup) does the rest.
            self._background_launch = True
            self.hold_for_background()
        else:
            self.activate()
            self._maybe_open_pbw(command_line)
        return 0

    def _maybe_open_pbw(self, command_line) -> None:
        """Launched with a .pbw (Pebble app bundle, opened via the file
        association): offer to install it on the registered Pebble."""
        target = next((arg for arg in command_line.get_arguments()[1:]
                       if arg.lower().endswith(".pbw")), None)
        if target is None:
            return
        win = self.props.active_window
        entry = next((e for e in self.device_manager.list()
                      if e.kind == "pebble"), None)
        if win is None or entry is None:
            log.info("ignoring %s: no Pebble registered", target)
            return
        gfile = command_line.create_file_for_arg(target)
        from vitals.dialogs.pbw_install_dialog import PbwInstallDialog
        PbwInstallDialog(self.ble, entry, gfile).present(win)

    def hold_for_background(self) -> None:
        """Keep the app (and its BLE loop) alive with no window open."""
        if not self._held:
            self.hold()
            self._held = True

    def do_activate(self):
        if self._background_launch and self.props.active_window is None:
            self._background_launch = False
            return
        win = self.props.active_window
        if win is None:
            win = VitalsWindow(application=self)
            if self._adoption and self._adoption.get("adopted"):
                win.show_toast(
                    f"Imported {self._adoption['records']:,} records from "
                    "Pulse — the retiring apps no longer feed this data")
                self._adoption = None
        win.set_visible(True)
        win.present()
        # Returning to foreground after close-to-background: release
        # the hold we took for the hidden window.
        if self._held:
            self.release()
            self._held = False

    # ── actions ───────────────────────────────────────────────────
    def _refresh(self, *_):
        win = self.props.active_window
        if win is not None:
            win.refresh()

    def _show_preferences(self, *_):
        from vitals.preferences import VitalsPreferences
        win = self.props.active_window
        if win is not None:
            VitalsPreferences(self.settings).present(win)

    def _show_about(self, *_):
        about = Adw.AboutDialog(
            application_name=APP_NAME,
            application_icon=APP_ID,
            version=VERSION,
            license_type=Gtk.License.GPL_3_0,
            developer_name="Rob Daniel",
            comments="Health tracking for watches, sensors and manual logs.",
            website="https://codeberg.org/robland/vitals",
            issue_url="https://codeberg.org/robland/vitals/issues",
        )
        about.add_acknowledgement_section(
            "Generated by", ("Claude (Anthropic)\nhttps://claude.com",))
        about.present(self.props.active_window)
