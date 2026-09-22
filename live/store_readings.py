#!/usr/bin/env python3
"""Durable SQLite storage for accepted meter replies and failed poll attempts.

Integration: with ReadingStore(path) as store:
    store.record_attempt(attempt, buffers.get(attempt["kind"]))

Every call commits one complete attempt or raises; callers must stop polling on
storage failure. Failed attempts never copy last_good readings into new rows.
Original UTC is retained, including backwards clock changes/backfill. attempt_id
provides monotonic database order; recorded_monotonic_ns is local process/boot
clock evidence, not a replacement measurement timestamp.

Collection while logging continues:
    python3 store_readings.py backup --database readings.sqlite --output overnight.sqlite
Copy the resulting backup file. Do not copy just the live .sqlite file while its
WAL is active. Read-only clients may connect with SQLite URI mode=ro, not immutable.
No derived measurements are calculated by this module. Supplied estimates are
stored separately in derived_readings, with their method, inputs and assumptions.
scalar_readings combines both origins with an explicit per-metric source. Full
decoded_json retains unavailable_quantities (including unavailable THD); missing
quantities are never replaced with zero or synthesized as scalar rows.
"""
import argparse
from contextlib import closing
import datetime as dt
import hashlib
import json
import math
import os
from pathlib import Path
import sqlite3
import time
import uuid

APPLICATION_ID = 0x49514450
SCHEMA_VERSION = 1
MAX_REPORT_BYTES = 4 * 1024 * 1024
EPOCH = dt.datetime(1970, 1, 1, tzinfo=dt.timezone.utc)


def _json(value):
    return json.dumps(value, allow_nan=False, sort_keys=True, separators=(",", ":"))


def _timestamp(value, name):
    if not isinstance(value, str):
        raise ValueError(f"{name} must be a timezone-qualified UTC timestamp")
    parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError(f"{name} lacks a timezone")
    parsed = parsed.astimezone(dt.timezone.utc)
    delta = parsed - EPOCH
    micros = (delta.days * 86400 + delta.seconds) * 1000000 + delta.microseconds
    return parsed.isoformat(timespec="microseconds"), micros


def _report_from_attempt(attempt):
    report = attempt.get("raw_report")
    refs = attempt.get("evidence_refs", [])
    if not isinstance(refs, list) or not all(isinstance(p, str) for p in refs):
        raise ValueError("evidence_refs must be a list of paths")
    paths = [p.split("#", 1)[0] for p in refs]
    summary = next((p for p in paths if p.endswith(".json")), None)
    journal = next((p for p in paths if p.endswith(".jsonl")), None)
    if report is None and summary and Path(summary).is_file():
        path = Path(summary)
        if path.stat().st_size > MAX_REPORT_BYTES:
            raise ValueError("Raw trial report exceeds 4 MiB")
        report = json.loads(path.read_text())
    if report is not None and not isinstance(report, dict):
        raise ValueError("raw_report must be a JSON object")
    return report or {}, summary, journal


class ReadingStore:
    """One synchronous writer connection. SQLite serializes other writers.

    WAL permits concurrent readers; synchronous=FULL commits the WAL before
    acknowledging success. sqlite3 exceptions propagate to the polling caller.
    record_attempt returns {attempt_id, inserted}; repeated imports are no-ops,
    while a reused identity with different data raises instead of rewriting.
    """
    def __init__(self, path):
        self.path = Path(path).expanduser().resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.path), timeout=5, isolation_level=None)
        try:
            app = self._conn.execute("PRAGMA application_id").fetchone()[0]
            version = self._conn.execute("PRAGMA user_version").fetchone()[0]
            tables = self._conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
            if app not in (0, APPLICATION_ID) or version not in (0, SCHEMA_VERSION) or (app == 0 and tables):
                raise ValueError("Refusing an unrelated or unsupported SQLite database")
            self._conn.execute("PRAGMA foreign_keys=ON")
            if self._conn.execute("PRAGMA journal_mode=WAL").fetchone()[0].lower() != "wal":
                raise RuntimeError("SQLite WAL mode unavailable")
            self._conn.execute("PRAGMA synchronous=FULL")
            self._conn.execute("PRAGMA busy_timeout=5000")
            self._conn.execute("BEGIN IMMEDIATE")
            for statement in SCHEMA:
                self._conn.execute(statement)
            self._conn.execute(f"PRAGMA application_id={APPLICATION_ID}")
            self._conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
            self._conn.execute("COMMIT")
        except BaseException:
            self._conn.close()
            raise

    def __enter__(self):
        return self

    def __exit__(self, *unused):
        self.close()

    def close(self):
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def record_attempt(self, attempt, buffer=None):
        if not isinstance(attempt, dict) or type(attempt.get("ok")) is not bool:
            raise ValueError("Attempt requires an explicit boolean ok")
        kind = attempt.get("kind")
        if not isinstance(kind, str) or not kind:
            raise ValueError("Attempt requires a request kind")
        started, _ = _timestamp(attempt.get("started_utc"), "started_utc")
        finished, _ = _timestamp(attempt.get("finished_utc"), "finished_utc")
        report, summary, journal = _report_from_attempt(attempt)
        source_started, _ = _timestamp(report.get("started_utc", started), "source_started_utc")
        command = report.get("command", attempt.get("command"))
        if command is not None and not isinstance(command, str):
            raise ValueError("Raw request command must be text")
        good = (buffer or {}).get("last_good") if attempt["ok"] else None
        decoded, observed, observed_us = None, None, None
        readings, derived_readings, words = [], [], []
        protocol_valid = decoded_valid = display_verified = False
        if attempt["ok"]:
            if not isinstance(good, dict) or not journal:
                raise ValueError("Successful attempt requires its own last_good buffer and journal")
            decoded = good.get("decoded")
            if not isinstance(decoded, dict) or decoded.get("complete") is not True:
                raise ValueError("Successful attempt requires a complete decoded buffer")
            if decoded.get("kind") != kind:
                raise ValueError("Decoded kind differs from attempt kind")
            observed, observed_us = _timestamp(good.get("observed_utc"), "observed_utc")
            if report.get("finished_utc") is not None:
                expected_observed, _ = _timestamp(report["finished_utc"], "report.finished_utc")
                if observed != expected_observed:
                    raise ValueError("Last-good buffer belongs to a different acquisition time")
            if journal not in [p.split("#", 1)[0] for p in good.get("evidence_refs", [])]:
                raise ValueError("Last-good buffer does not reference this attempt's journal")
            protocol_valid = True
            decoded_valid = decoded.get("valid") is True
            display_verified = good.get("display_verified") is True
            raw_words = decoded.get("raw_words")
            if not isinstance(raw_words, list) or not raw_words:
                raise ValueError("Complete buffer has no raw words")
            for index, word in enumerate(raw_words):
                if type(word) is not int or not 0 <= word <= 0x1ffffff or word & 1:
                    raise ValueError("Decoded raw words must be 25-bit DATA frames")
                words.append((index, word, word >> 1))
            values = decoded.get("readings")
            if not isinstance(values, dict):
                raise ValueError("Decoded readings must be a mapping")
            for name, reading in values.items():
                if not isinstance(name, str) or not isinstance(reading, dict):
                    raise ValueError("Invalid scalar reading")
                source = reading.get("source", "meter_write")
                if source not in ("meter_write", "derived"):
                    raise ValueError("Unknown scalar source")
                if reading.get("derived") and source != "derived":
                    raise ValueError("Derived scalar must declare source=derived")
                valid = reading.get("valid") is True
                value = reading.get("value")
                if value is not None and (type(value) not in (int, float) or not math.isfinite(value)):
                    raise ValueError("Scalar values must be finite numbers or null")
                if valid == (value is None):
                    raise ValueError("Invalid readings must be null; valid readings require a value")
                if source == "derived":
                    method, inputs = reading.get("method"), reading.get("inputs")
                    assumption = reading.get("assumption")
                    if not isinstance(method, str) or not method.strip():
                        raise ValueError("Derived scalar requires an explicit method")
                    if (not isinstance(inputs, list) or not inputs or
                            not all(isinstance(item, str) and item in values for item in inputs)):
                        raise ValueError("Derived scalar requires named inputs from this acquisition")
                    if not isinstance(assumption, str) or not assumption.strip():
                        raise ValueError("Derived scalar requires an explicit assumption")
                    derived_readings.append((name, value, reading.get("unit"), int(valid),
                                             method, _json(inputs), assumption, _json(reading)))
                else:
                    readings.append((name, value, reading.get("unit"), int(valid),
                                     reading.get("value_exact"), reading.get("raw_payload"), _json(reading)))
        identity = attempt.get("attempt_key") or hashlib.sha256(_json({
            "kind": kind, "source_started_utc": source_started, "command": command,
            "journal_basename": Path(journal).name if journal else None,
        }).encode()).hexdigest()
        if not isinstance(identity, str) or not identity:
            raise ValueError("attempt_key must be nonempty text")
        fingerprint = hashlib.sha256(_json({"kind": kind, "source_started_utc": source_started,
            "command": command, "ok": attempt["ok"], "observed_utc": observed,
            "decoded": decoded, "errors": attempt.get("errors", []),
            "device_result": report.get("result")}).encode()).hexdigest()
        # Serialize before BEGIN so malformed JSON cannot leave a partial transaction.
        attempt_json, report_json = _json(attempt), _json(report)
        decoded_json = _json(decoded) if decoded is not None else None
        recorded = dt.datetime.now(dt.timezone.utc).isoformat(timespec="microseconds")
        conn = self._conn
        conn.execute("BEGIN IMMEDIATE")
        try:
            existing = conn.execute("SELECT attempt_id,fingerprint FROM attempts WHERE attempt_key=?", (identity,)).fetchone()
            if existing:
                if existing[1] != fingerprint:
                    raise ValueError("Attempt identity already exists with different evidence/decoding")
                conn.execute("COMMIT")
                return {"attempt_id": existing[0], "inserted": False}
            previous = conn.execute("SELECT MAX(observed_utc_us) FROM attempts").fetchone()[0]
            out_of_order = observed_us is not None and previous is not None and observed_us < previous
            cursor = conn.execute("""INSERT INTO attempts
                (attempt_key,fingerprint,kind,ok,started_utc,finished_utc,source_started_utc,
                 observed_utc,observed_utc_us,source_time_out_of_order,recorded_utc,
                 recorded_monotonic_ns,source,protocol_valid,decoded_valid,display_verified,
                 request_command,summary_path,journal_path,attempt_json,raw_report_json,decoded_json)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (identity,fingerprint,kind,int(attempt["ok"]),started,finished,source_started,
                 observed,observed_us,int(out_of_order),recorded,time.monotonic_ns(),
                 "meter_write" if attempt["ok"] else None,int(protocol_valid),int(decoded_valid),int(display_verified),
                 command,summary,journal,attempt_json,report_json,decoded_json))
            row_id = cursor.lastrowid
            conn.executemany("INSERT INTO raw_words(attempt_id,word_index,raw_word,payload) VALUES (?,?,?,?)",
                             [(row_id, *row) for row in words])
            conn.executemany("""INSERT INTO readings(attempt_id,name,value,unit,valid,value_exact,raw_payload,details_json)
                              VALUES (?,?,?,?,?,?,?,?)""", [(row_id, *row) for row in readings])
            conn.executemany("""INSERT INTO derived_readings
                              (attempt_id,name,value,unit,valid,method,inputs_json,assumption,details_json)
                              VALUES (?,?,?,?,?,?,?,?,?)""", [(row_id, *row) for row in derived_readings])
            conn.execute("COMMIT")
            return {"attempt_id": row_id, "inserted": True}
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise

    def backup(self, destination, timeout=60):
        return backup_database(self.path, destination, timeout=timeout)


SCHEMA = [
    """CREATE TABLE IF NOT EXISTS attempts (
       attempt_id INTEGER PRIMARY KEY AUTOINCREMENT, attempt_key TEXT NOT NULL UNIQUE,
       fingerprint TEXT NOT NULL, kind TEXT NOT NULL, ok INTEGER NOT NULL CHECK(ok IN(0,1)),
       started_utc TEXT NOT NULL, finished_utc TEXT NOT NULL, source_started_utc TEXT NOT NULL,
       observed_utc TEXT, observed_utc_us INTEGER, source_time_out_of_order INTEGER NOT NULL,
       recorded_utc TEXT NOT NULL, recorded_monotonic_ns INTEGER NOT NULL,
       source TEXT, protocol_valid INTEGER NOT NULL, decoded_valid INTEGER NOT NULL,
       display_verified INTEGER NOT NULL, request_command TEXT, summary_path TEXT, journal_path TEXT,
       attempt_json TEXT NOT NULL, raw_report_json TEXT NOT NULL, decoded_json TEXT)""",
    """CREATE TABLE IF NOT EXISTS raw_words (
       attempt_id INTEGER NOT NULL REFERENCES attempts(attempt_id), word_index INTEGER NOT NULL,
       raw_word INTEGER NOT NULL, payload INTEGER NOT NULL, PRIMARY KEY(attempt_id,word_index))""",
    """CREATE TABLE IF NOT EXISTS readings (
       attempt_id INTEGER NOT NULL REFERENCES attempts(attempt_id), name TEXT NOT NULL,
       value REAL, unit TEXT, valid INTEGER NOT NULL CHECK(valid IN(0,1)), value_exact TEXT,
       raw_payload INTEGER, details_json TEXT NOT NULL, PRIMARY KEY(attempt_id,name))""",
    """CREATE TABLE IF NOT EXISTS derived_readings (
       attempt_id INTEGER NOT NULL REFERENCES attempts(attempt_id), name TEXT NOT NULL,
       value REAL, unit TEXT, valid INTEGER NOT NULL CHECK(valid IN(0,1)),
       method TEXT NOT NULL, inputs_json TEXT NOT NULL, assumption TEXT NOT NULL,
       details_json TEXT NOT NULL, PRIMARY KEY(attempt_id,name))""",
    "CREATE INDEX IF NOT EXISTS attempts_observed_time ON attempts(observed_utc_us)",
    "CREATE INDEX IF NOT EXISTS readings_name_attempt ON readings(name,attempt_id)",
    "CREATE INDEX IF NOT EXISTS derived_readings_name_attempt ON derived_readings(name,attempt_id)",
    """CREATE VIEW IF NOT EXISTS scalar_readings AS
       SELECT a.attempt_id,a.kind,a.observed_utc,a.recorded_utc,'meter_write' AS source,a.protocol_valid,
              a.decoded_valid,a.display_verified,a.source_time_out_of_order,
              r.name,r.value,r.unit,r.valid,r.value_exact,r.raw_payload,
              a.request_command,a.summary_path,a.journal_path,
              NULL AS method,NULL AS inputs_json,NULL AS assumption
       FROM readings r JOIN attempts a USING(attempt_id)
       UNION ALL
       SELECT a.attempt_id,a.kind,a.observed_utc,a.recorded_utc,'derived' AS source,a.protocol_valid,
              a.decoded_valid,a.display_verified,a.source_time_out_of_order,
              r.name,r.value,r.unit,r.valid,NULL AS value_exact,NULL AS raw_payload,
              a.request_command,a.summary_path,a.journal_path,
              r.method,r.inputs_json,r.assumption
       FROM derived_readings r JOIN attempts a USING(attempt_id)""",
]


def backup_database(source, destination, timeout=60):
    """Publish a consistent, standalone backup without stopping the WAL writer."""
    source, destination = Path(source).resolve(), Path(destination).resolve()
    if source == destination or destination.exists():
        raise FileExistsError("Backup destination must be new and different from source")
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("Backup timeout must be positive and finite")
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name("." + destination.name + "." + uuid.uuid4().hex + ".tmp")
    deadline = time.monotonic() + timeout
    def progress(status, remaining, total):
        if time.monotonic() > deadline:
            raise TimeoutError("SQLite online backup deadline exceeded")
    try:
        descriptor = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(descriptor)
        with closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True, timeout=5)) as original:
            original.execute("BEGIN")
            original.execute("SELECT COUNT(*) FROM sqlite_master").fetchone()
            if original.execute("PRAGMA application_id").fetchone()[0] != APPLICATION_ID:
                raise ValueError("Source is not an IQ Data reading database")
            with closing(sqlite3.connect(str(temporary), timeout=5)) as copy:
                original.backup(copy, pages=128, progress=progress, sleep=0.05)
                copy.execute("PRAGMA journal_mode=DELETE")
                if copy.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                    raise RuntimeError("Backup failed SQLite quick_check")
        with temporary.open("rb") as stream:
            os.fsync(stream.fileno())
        os.link(temporary, destination)  # Atomic publication; refuses overwrite.
        temporary.unlink()
        directory = os.open(destination.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
        return str(destination)
    finally:
        for path in (temporary, Path(str(temporary) + "-wal"), Path(str(temporary) + "-shm")):
            if path.exists():
                path.unlink()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["status", "backup"])
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.action == "backup":
        if not args.output:
            parser.error("backup requires --output")
        print(json.dumps({"backup": backup_database(args.database, args.output), "consistent": True}))
    else:
        with sqlite3.connect(args.database.resolve().as_uri() + "?mode=ro", uri=True) as conn:
            if conn.execute("PRAGMA application_id").fetchone()[0] != APPLICATION_ID:
                parser.error("Not an IQ Data reading database")
            attempts, failures, first, last = conn.execute(
                "SELECT COUNT(*),COALESCE(SUM(ok=0),0),MIN(observed_utc),MAX(observed_utc) FROM attempts").fetchone()
            readings = conn.execute("SELECT COUNT(*) FROM scalar_readings").fetchone()[0]
            derived = conn.execute("SELECT COUNT(*) FROM derived_readings").fetchone()[0]
            print(json.dumps({"attempts": attempts, "failed_attempts": failures, "scalar_readings": readings,
                              "derived_readings": derived,
                              "first_observed_utc": first, "last_observed_utc": last}))
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
