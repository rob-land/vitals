"""Make ``import vitals`` work when pytest runs from the repo root, and
pin GI versions before tests import gi-using modules (the app pins these
once in its launcher, which tests bypass)."""

import pathlib
import sys

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

_SRC = str(pathlib.Path(__file__).resolve().parent.parent / "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)
