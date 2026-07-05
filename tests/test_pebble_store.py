"""Tests for the Pebble store catalogue mapping (pure parts)."""

from vitals.devices.pebble import pebble_store as ps


def test_collection_paths():
    # The API's app collection is "apps" (not "watchapps" — that 400s);
    # watchfaces is "watchfaces".
    assert ps._KIND_PATH["watchface"] == "watchfaces"
    assert ps._KIND_PATH["watchapp"] == "apps"


def test_to_store_app_picks_emery_platform():
    rec = {
        "id": "abc", "title": "Cool Face", "author": "me", "type": "watchface",
        "hardware_platforms": [
            {"name": "basalt", "images": {"screenshot": "basalt.png"},
             "description": "basalt desc"},
            {"name": "emery", "images": {"screenshot": "emery.png",
                                         "icon": "emery-icon.png"},
             "description": "emery desc"},
        ],
        "latest_release": {"pbw_file": "https://x/abc.pbw", "version": "1.2"},
    }
    app = ps._to_store_app(rec, "watchface")
    assert app.name == "Cool Face"
    assert app.author == "me"
    assert app.kind == "watchface"
    assert app.screenshot_url == "emery.png"      # emery, not basalt
    assert app.icon_url == "emery-icon.png"
    assert app.description == "emery desc"
    assert app.download_url == "https://x/abc.pbw"
    assert app.version == "1.2"


def test_to_store_app_tolerates_missing_fields():
    app = ps._to_store_app({"id": "x"}, "watchapp")
    assert app.name == "Untitled"
    assert app.screenshot_url is None
    assert app.download_url == ""
