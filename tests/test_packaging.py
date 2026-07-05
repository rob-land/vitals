"""Packaging guardrails.

The unit tests import from the source tree, so they pass even if a module
is missing from `src/vitals/meson.build` — which means it never gets
installed into the Flatpak (that exact gap shipped broken tock releases).
These tests fail the build if any source file isn't listed for
installation, and likewise for Blueprint templates and schema data.
"""

import pathlib

_REPO = pathlib.Path(__file__).resolve().parent.parent
_PKG = _REPO / "src" / "vitals"


def test_every_source_file_is_in_the_meson_manifest():
    manifest = (_PKG / "meson.build").read_text()
    missing = []
    for path in _PKG.rglob("*.py"):
        rel = path.relative_to(_PKG).as_posix()
        if "__pycache__" in rel or rel == "const.py":  # const.py is generated
            continue
        if f"'{rel}'" not in manifest:
            missing.append(rel)
    assert not missing, (
        "source files not listed in src/vitals/meson.build (won't be "
        f"installed into the Flatpak): {sorted(missing)}")


def test_every_blueprint_is_in_the_ui_meson_manifest():
    ui = _REPO / "data" / "ui"
    manifest = (ui / "meson.build").read_text()
    missing = [p.name for p in ui.glob("*.blp") if f"'{p.name}'" not in manifest]
    assert not missing, (
        f"Blueprint templates not listed in data/ui/meson.build: {sorted(missing)}")


def test_every_schema_file_is_in_the_data_manifest():
    manifest = (_REPO / "data" / "meson.build").read_text()
    schema = _REPO / "data" / "schema"
    missing = []
    for path in schema.rglob("*"):
        if path.is_file() and f"'{path.relative_to(_REPO / 'data').as_posix()}'" not in manifest:
            missing.append(path.name)
    assert not missing, (
        f"schema files not installed by data/meson.build: {sorted(missing)}")
