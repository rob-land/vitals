"""The health-data core: catalog, validation, SQLite store, replication.

This package is the former pulse daemon's storage layer running
in-process. Vitals is the single writer of the database; there is no
IPC, no consent layer, and no daemon.
"""
