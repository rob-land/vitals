"""Pebble timeline items — notification and pin blobs (pure encoders).

The firmware's notification UI and timeline both read ``TimelineItem``
blobs from BlobDB (databases 4 and 1). Layout per Gadgetbridge's
``PebbleProtocol.encodeNotification`` / ``encodeTimelinePin`` (the
long-standing known-good implementation): a 46-byte fixed header —
UUIDs big-endian, every other field little-endian — followed by
attribute TLVs, then actions.

    itemId(16) parentId(16) timestamp:u32 duration:u16(minutes)
    type:u8 flags:u16 layout:u8 payload_len:u16 attr_count:u8
    action_count:u8, then attributes + actions

Attribute TLV: id:u8 + length:u16 + data. Action: id:u8 + type:u8 +
attr_count:u8 + attribute TLVs.
"""

from __future__ import annotations

import struct
import uuid

# Parent for all notifications (a firmware-known UUID).
UUID_NOTIFICATIONS = uuid.UUID("b2cae818-10f8-46df-ad2b-98ad2254a3c1")
# Our itemId MSB namespace ("VITALS\0\0"): itemId = MSB + notification
# id, so a later delete-by-id can dismiss the same banner on the watch.
ITEM_UUID_MSB = 0x564954414C530000

TYPE_NOTIFICATION = 0x01
TYPE_PIN = 0x02

LAYOUT_GENERIC_PIN = 0x01
LAYOUT_CALENDAR_PIN = 0x02
LAYOUT_NOTIFICATION = 0x04

_FLAGS = 0x0001

ATTR_TITLE = 1
ATTR_SUBTITLE = 2
ATTR_BODY = 3
ATTR_TINY_ICON = 4
ATTR_LOCATION = 11

# System resource icon ids (PebbleIconID); 0x80000000 marks a system
# resource when written into the icon attribute.
ICON_NOTIFICATION_GENERIC = 1
ICON_TIMELINE_CALENDAR = 21
_SYSTEM_ICON_FLAG = 0x80000000

ACTION_ID_DISMISS = 0x02
ACTION_TYPE_GENERIC = 0x02

_MAX_TEXT = 512


def attribute(attr_id: int, data: bytes) -> bytes:
    return struct.pack("<BH", attr_id, len(data)) + data


def text_attribute(attr_id: int, text: str) -> bytes:
    return attribute(attr_id, text.encode("utf-8")[:_MAX_TEXT])


def icon_attribute(icon_id: int) -> bytes:
    return attribute(ATTR_TINY_ICON,
                     struct.pack("<I", _SYSTEM_ICON_FLAG | icon_id))


def action(action_id: int, action_type: int, attributes: list[bytes]) -> bytes:
    return (bytes([action_id, action_type, len(attributes)])
            + b"".join(attributes))


def item_uuid(item_id: int) -> bytes:
    """Deterministic 16-byte itemId for one notification id."""
    return struct.pack(">QQ", ITEM_UUID_MSB,
                       item_id & 0xFFFFFFFFFFFFFFFF)


def encode_timeline_item(item_id: bytes, parent_id: bytes, timestamp: int,
                         duration_min: int, item_type: int, layout: int,
                         attributes: list[bytes],
                         actions: list[bytes] = ()) -> bytes:
    payload = b"".join(attributes) + b"".join(actions)
    return (item_id + parent_id
            + struct.pack("<IHBHBHBB",
                          int(timestamp) & 0xFFFFFFFF,
                          duration_min & 0xFFFF,
                          item_type, _FLAGS, layout,
                          len(payload), len(attributes), len(actions))
            + payload)


def encode_notification(note) -> tuple[bytes, bytes]:
    """(insert_key, value) for one ``WatchNotification``.

    The insert key is a fresh random UUID; the itemId inside the blob
    is derived from the notification id so updates of the same banner
    replace rather than stack, and a delete-by-itemId can dismiss it.
    """
    attributes = [text_attribute(ATTR_TITLE, note.title)]
    if note.app_name:
        attributes.append(text_attribute(ATTR_SUBTITLE, note.app_name))
    if note.body:
        attributes.append(text_attribute(ATTR_BODY, note.body))
    attributes.append(icon_attribute(ICON_NOTIFICATION_GENERIC))
    actions = [action(ACTION_ID_DISMISS, ACTION_TYPE_GENERIC,
                      [text_attribute(ATTR_TITLE, "Dismiss")])]
    value = encode_timeline_item(
        item_uuid(note.id), UUID_NOTIFICATIONS.bytes,
        int(note.timestamp), 0, TYPE_NOTIFICATION, LAYOUT_NOTIFICATION,
        attributes, actions)
    return uuid.uuid4().bytes, value


def encode_pin(pin_uuid: bytes, start_utc: int, duration_min: int,
               title: str, body: str = "", location: str = "",
               icon_id: int = ICON_TIMELINE_CALENDAR) -> tuple[bytes, bytes]:
    """(key, value) for a timeline pin. Pins are keyed by their own
    itemId (parentId zero), so re-inserting the same uuid updates the
    pin and a delete with it removes the event from the timeline."""
    attributes = [icon_attribute(icon_id),
                  text_attribute(ATTR_TITLE, title)]
    if body:
        attributes.append(text_attribute(ATTR_BODY, body))
    if location:
        attributes.append(text_attribute(ATTR_LOCATION, location))
    layout = (LAYOUT_CALENDAR_PIN if icon_id == ICON_TIMELINE_CALENDAR
              else LAYOUT_GENERIC_PIN)
    value = encode_timeline_item(
        pin_uuid, bytes(16), start_utc, duration_min,
        TYPE_PIN, layout, attributes)
    return pin_uuid, value
