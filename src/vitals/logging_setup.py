"""Logging configuration (cohort convention).

Mirrors each run to a rotating file under the app data dir so Phosh users
can read logs without journalctl. Default level INFO; ``VITALS_DEBUG=1`` or
``--debug`` bumps to DEBUG.
"""

from __future__ import annotations

import logging
import logging.handlers
import os
import sys

from gi.repository import GLib

_LOG_FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"
_DATE_FORMAT = "%H:%M:%S"
_LOG_FILE_BYTES = 512 * 1024
_LOG_BACKUPS = 2


def log_path() -> str:
    path = os.path.join(GLib.get_user_data_dir(), "vitals")
    os.makedirs(path, exist_ok=True)
    return os.path.join(path, "vitals.log")


def is_debug() -> bool:
    if "--debug" in sys.argv:
        return True
    return os.environ.get("VITALS_DEBUG", "").strip().lower() in ("1", "true", "yes", "on")


def configure_logging() -> None:
    """Configure the root logger. Idempotent."""
    level = logging.DEBUG if is_debug() else logging.INFO
    fmt = logging.Formatter(_LOG_FORMAT, datefmt=_DATE_FORMAT)

    root = logging.getLogger()
    root.setLevel(level)
    for handler in list(root.handlers):
        root.removeHandler(handler)

    stream = logging.StreamHandler()
    stream.setFormatter(fmt)
    root.addHandler(stream)

    try:
        file_handler = logging.handlers.RotatingFileHandler(
            log_path(), maxBytes=_LOG_FILE_BYTES, backupCount=_LOG_BACKUPS,
            encoding="utf-8")
        file_handler.setFormatter(fmt)
        root.addHandler(file_handler)
    except Exception:
        logging.getLogger(__name__).exception(
            "file logging setup failed; continuing with stream only")
