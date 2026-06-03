"""Allow ``python3 -m vitals`` to run a built tree (loads the GResource the
launcher would otherwise register)."""

import os
import sys

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gio

from vitals import const

Gio.Resource.load(
    os.path.join(const.PKGDATADIR, const.APP_ID + ".gresource"))._register()

from vitals.main import main

sys.exit(main())
