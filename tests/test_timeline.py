"""Tests for the Pebble TimelineItem encoders (notifications + pins).
Layout pinned against Gadgetbridge's PebbleProtocol."""

import struct

from vitals.devices.base import WatchNotification
from vitals.devices.pebble import timeline as tl


def _header(blob):
    item_id, parent_id = blob[:16], blob[16:32]
    (ts, dur, typ, flags, layout, payload_len,
     n_attrs, n_actions) = struct.unpack_from("<IHBHBHBB", blob, 32)
    return (item_id, parent_id, ts, dur, typ, flags, layout,
            payload_len, n_attrs, n_actions)


def _read_attrs(payload, count):
    attrs, off = [], 0
    for _ in range(count):
        attr_id, length = struct.unpack_from("<BH", payload, off)
        attrs.append((attr_id, payload[off + 3:off + 3 + length]))
        off += 3 + length
    return attrs, off


def test_notification_item_layout():
    note = WatchNotification(id=7, app_name="Signal", title="Alice",
                             body="hello", timestamp=1_700_000_000.0)
    key, blob = tl.encode_notification(note)
    assert len(key) == 16                          # random insert key

    (item_id, parent_id, ts, dur, typ, flags, layout,
     payload_len, n_attrs, n_actions) = _header(blob)
    assert item_id == struct.pack(">QQ", tl.ITEM_UUID_MSB, 7)
    assert parent_id == tl.UUID_NOTIFICATIONS.bytes
    assert ts == 1_700_000_000 and dur == 0
    assert typ == tl.TYPE_NOTIFICATION and layout == tl.LAYOUT_NOTIFICATION
    assert flags == 0x0001
    payload = blob[46:]
    assert payload_len == len(payload)

    attrs, off = _read_attrs(payload, n_attrs)
    assert attrs[0] == (tl.ATTR_TITLE, b"Alice")
    assert attrs[1] == (tl.ATTR_SUBTITLE, b"Signal")
    assert attrs[2] == (tl.ATTR_BODY, b"hello")
    icon_id, icon_value = attrs[3]
    assert icon_id == tl.ATTR_TINY_ICON
    assert struct.unpack("<I", icon_value)[0] == (
        0x80000000 | tl.ICON_NOTIFICATION_GENERIC)

    # One dismiss action: id, type, attr count, then its title.
    assert n_actions == 1
    action = payload[off:]
    assert action[0] == tl.ACTION_ID_DISMISS
    assert action[1] == tl.ACTION_TYPE_GENERIC
    assert action[2] == 1
    _, title = _read_attrs(action[3:], 1)[0][0], action[6:]
    assert title == b"Dismiss"


def test_notification_same_id_reuses_item_uuid():
    a = WatchNotification(5, "App", "T", "", 0.0)
    b = WatchNotification(5, "App", "T2", "", 0.0)
    _, blob_a = tl.encode_notification(a)
    _, blob_b = tl.encode_notification(b)
    assert blob_a[:16] == blob_b[:16]     # same banner → same itemId
    key_a, _ = tl.encode_notification(a)
    key_b, _ = tl.encode_notification(b)
    assert key_a != key_b                 # inserts keyed randomly


def test_notification_skips_empty_optional_attributes():
    note = WatchNotification(1, "", "Just a title", "", 0.0)
    _, blob = tl.encode_notification(note)
    n_attrs = _header(blob)[8]
    assert n_attrs == 2                   # title + icon only


def test_pin_layout():
    pin_uuid = bytes(range(16))
    key, blob = tl.encode_pin(pin_uuid, start_utc=1_700_000_000,
                              duration_min=45, title="Dentist",
                              body="Bring forms", location="12 High St")
    assert key == pin_uuid                # pins are keyed by itemId
    (item_id, parent_id, ts, dur, typ, _flags, layout,
     _plen, n_attrs, n_actions) = _header(blob)
    assert item_id == pin_uuid and parent_id == bytes(16)
    assert ts == 1_700_000_000 and dur == 45
    assert typ == tl.TYPE_PIN and layout == tl.LAYOUT_CALENDAR_PIN
    assert n_actions == 0

    attrs, _ = _read_attrs(blob[46:], n_attrs)
    by_id = dict(attrs)
    assert struct.unpack("<I", by_id[tl.ATTR_TINY_ICON])[0] == (
        0x80000000 | tl.ICON_TIMELINE_CALENDAR)
    assert by_id[tl.ATTR_TITLE] == b"Dentist"
    assert by_id[tl.ATTR_BODY] == b"Bring forms"
    assert by_id[tl.ATTR_LOCATION] == b"12 High St"


def test_text_attributes_are_capped():
    attr = tl.text_attribute(tl.ATTR_BODY, "x" * 2000)
    _id, length = struct.unpack_from("<BH", attr, 0)
    assert length == 512
