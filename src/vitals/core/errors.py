"""Errors raised by the health-data core."""

from __future__ import annotations


class CoreError(Exception):
    """Base class for health-data core errors."""


class InvalidRecord(CoreError):
    """An envelope failed validation against its type definition."""


class UnknownType(CoreError):
    """A record or query referenced a type missing from the catalog."""
