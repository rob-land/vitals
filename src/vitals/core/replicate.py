"""Replicate the local change-feed to a Vault sync server.

    python3 -m vitals.core.replicate http://server:8765 [--once] [--interval N]

Reads the store's change feed directly (Vitals owns the database) and
POSTs each batch to ``<server>/changes``, persisting the seq cursor
between runs so each record is sent once.
"""

from __future__ import annotations

import argparse
import json
import logging
import time
import urllib.request

from vitals.core import records, resources
from vitals.core.store import Store

log = logging.getLogger(__name__)


def get_changes(store: Store, since: int, limit: int = 500) -> tuple[list[dict], int]:
    rows, next_seq = store.get_changes(since, limit)
    return [records.row_to_envelope(row) for row in rows], next_seq


def replicate(store: Store, post, since: int, batch: int = 500) -> int:
    """Push changes since ``since``, calling ``post(records)`` per batch.
    Returns the new cursor (highest seq reached). ``post`` is injectable so
    the loop is testable without a real server."""
    total = 0
    while True:
        envelopes, next_seq = get_changes(store, since, batch)
        if not envelopes:
            break
        post(envelopes)
        total += len(envelopes)
        since = next_seq
        if len(envelopes) < batch:
            break
    if total:
        log.info("replicated %d record(s), cursor now %d", total, since)
    return since


def _http_post(server_url: str):
    endpoint = server_url.rstrip("/") + "/changes"

    def post(envelopes):
        data = json.dumps({"records": envelopes}).encode()
        req = urllib.request.Request(
            endpoint, data=data, method="POST",
            headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=15).read()

    return post


def cursor_path() -> str:
    base = resources.user_data_dir()
    base.mkdir(parents=True, exist_ok=True)
    return str(base / "replicate-cursor")


def read_cursor() -> int:
    try:
        with open(cursor_path()) as fh:
            return int(fh.read().strip() or "0")
    except (OSError, ValueError):
        return 0


def write_cursor(seq: int) -> None:
    with open(cursor_path(), "w") as fh:
        fh.write(str(seq))


def main() -> None:
    parser = argparse.ArgumentParser(prog="vitals.core.replicate")
    parser.add_argument("server_url", help="Vault base URL, e.g. http://host:8765")
    parser.add_argument("--once", action="store_true", help="sync once and exit")
    parser.add_argument("--interval", type=int, default=300,
                        help="seconds between syncs in loop mode (default 300)")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")

    store = Store(str(resources.db_path()))
    store.migrate()
    post = _http_post(args.server_url)
    try:
        while True:
            try:
                write_cursor(replicate(store, post, read_cursor()))
            except Exception:
                log.exception("replication pass failed")
            if args.once:
                break
            time.sleep(args.interval)
    finally:
        store.close()


if __name__ == "__main__":
    main()
