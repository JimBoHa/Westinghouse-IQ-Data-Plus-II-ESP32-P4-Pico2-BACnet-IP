#!/usr/bin/env python3
"""Make a consistent, standalone SQLite copy while acquisition keeps running."""
import argparse
from contextlib import closing
import datetime as dt
import json
import os
from pathlib import Path
import sqlite3
import uuid


def snapshot(source, destination):
    source, destination = Path(source).resolve(), Path(destination).resolve()
    if source == destination:
        raise ValueError("Snapshot must not overwrite the live database")
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(destination.name + ".tmp-" + uuid.uuid4().hex)
    try:
        with closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True, timeout=30)) as src:
            # Hold a fixed WAL read snapshot while new polls keep committing.
            # Otherwise a large incremental backup can restart after each write.
            src.execute("BEGIN")
            src.execute("SELECT COUNT(*) FROM sqlite_master").fetchone()
            dst = sqlite3.connect(temporary)
            try:
                src.backup(dst, pages=1024, sleep=0.01)
                dst.execute("PRAGMA journal_mode=DELETE")
                if dst.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
                    raise RuntimeError("Snapshot integrity check failed")
            finally:
                dst.close()
                src.execute("ROLLBACK")
        with temporary.open("rb") as stream:
            os.fsync(stream.fileno())
        os.replace(temporary, destination)
        descriptor = os.open(destination.parent, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        return {"snapshot": str(destination), "bytes": destination.stat().st_size,
                "finished_utc": dt.datetime.now(dt.timezone.utc).isoformat(), "quick_check": "ok"}
    finally:
        if temporary.exists():
            temporary.unlink()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("destination", type=Path)
    args = parser.parse_args()
    print(json.dumps(snapshot(args.source, args.destination)))
