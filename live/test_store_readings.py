"""Offline SQLite durability/provenance tests. No serial/network access."""
import copy
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from store_readings import ReadingStore, backup_database


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.store = ReadingStore(self.root / "readings.sqlite")

    def tearDown(self):
        self.store.close()
        self.directory.cleanup()

    def fixture(self, token="first", observed="2026-09-22T01:40:02.000000+00:00"):
        report = {"started_utc": observed, "finished_utc": observed,
                  "command": "transact currents_repeat 0 rising 1000",
                  "result": {"type": "result", "stop_code": 0, "valid_write_lengths": 5}}
        summary, journal = self.root / f"{token}.json", self.root / f"{token}.json.jsonl"
        summary.write_text(json.dumps(report))
        journal.write_text("Raw evidence remains external; this is a synthetic test fixture.\n")
        refs = [str(summary), str(journal)]
        attempt = {"kind": "currents", "ok": True, "started_utc": observed,
                   "finished_utc": observed, "errors": [], "evidence_refs": refs}
        decoded = {"kind": "currents", "valid": True, "complete": True,
                   "raw_words": [0x80037c, 0x80032e, 0x80037c, 0x800000],
                   "readings": {name: {"valid": True, "value": value, "unit": "A",
                                      "value_exact": str(value), "raw_payload": payload}
                                for name, value, payload in [("IA", 446, 0x4001be),
                                                            ("IB", 407, 0x400197),
                                                            ("IC", 446, 0x4001be)]},
                   "reserved": [{"raw_word": 0x800000}], "status": None}
        buffer = {"last_good": {"observed_utc": observed, "decoded": decoded,
                                "evidence_refs": refs}}
        return attempt, buffer

    def test_success_raw_reserved_and_flags(self):
        attempt, buffer = self.fixture()
        result = self.store.record_attempt(attempt, buffer)
        self.assertEqual(result, {"attempt_id": 1, "inserted": True})
        conn = self.store._conn
        self.assertEqual(conn.execute("PRAGMA journal_mode").fetchone()[0], "wal")
        self.assertEqual(conn.execute("PRAGMA synchronous").fetchone()[0], 2)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM raw_words").fetchone()[0], 4)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM readings").fetchone()[0], 3)
        row = conn.execute("SELECT source,protocol_valid,display_verified,decoded_json,request_command FROM attempts").fetchone()
        self.assertEqual(row[:3], ("meter_write", 1, 0))
        self.assertEqual(json.loads(row[3])["reserved"], [{"raw_word": 0x800000}])
        self.assertEqual(row[4], "transact currents_repeat 0 rising 1000")

    def test_failure_never_repeats_old_values(self):
        attempt, buffer = self.fixture()
        self.store.record_attempt(attempt, buffer)
        failed, _ = self.fixture("failed", "2026-09-22T01:40:04+00:00")
        failed.update(ok=False, errors=["Synthetic serial timeout"])
        self.store.record_attempt(failed, buffer)
        self.assertEqual(self.store._conn.execute("SELECT COUNT(*) FROM readings").fetchone()[0], 3)
        self.assertEqual(self.store._conn.execute("SELECT ok,source,observed_utc,decoded_json FROM attempts WHERE attempt_id=2").fetchone(), (0, None, None, None))
        self.assertIn("Synthetic serial timeout", self.store._conn.execute("SELECT attempt_json FROM attempts WHERE attempt_id=2").fetchone()[0])

    def test_idempotence_and_conflicting_identity(self):
        attempt, buffer = self.fixture()
        self.store.record_attempt(attempt, buffer)
        self.assertEqual(self.store.record_attempt(attempt, buffer), {"attempt_id": 1, "inserted": False})
        changed = copy.deepcopy(buffer)
        changed["last_good"]["decoded"]["readings"]["IA"]["value"] = 447
        with self.assertRaises(ValueError):
            self.store.record_attempt(attempt, changed)
        self.assertFalse(self.store._conn.in_transaction)
        self.assertEqual(self.store._conn.execute("SELECT COUNT(*) FROM attempts").fetchone()[0], 1)

    def test_invalid_optional_scalar_is_null(self):
        attempt, buffer = self.fixture()
        decoded = buffer["last_good"]["decoded"]
        decoded["valid"] = False
        decoded["readings"]["IA"].update(valid=False, value=None, value_exact=None)
        self.store.record_attempt(attempt, buffer)
        self.assertEqual(self.store._conn.execute("SELECT value,valid FROM readings WHERE name='IA'").fetchone(), (None, 0))
        self.assertEqual(self.store._conn.execute("SELECT protocol_valid,decoded_valid FROM attempts").fetchone(), (1, 0))

    def test_derived_origin_and_missing_thd_retained(self):
        attempt, buffer = self.fixture()
        decoded = buffer["last_good"]["decoded"]
        decoded["readings"]["test_estimate"] = {
            "source": "derived", "method": "IA + IB", "inputs": ["IA", "IB"],
            "assumption": "Synthetic sum for storage validation only",
            "value": 853, "unit": "A", "valid": True}
        decoded["readings"]["test_invalid_estimate"] = {
            "source": "derived", "method": "undefined", "inputs": ["IA"],
            "assumption": "Synthetic unavailable estimate", "value": None,
            "unit": "1", "valid": False}
        decoded["unavailable_quantities"] = {"THD": "No harmonic measurement available"}
        self.store.record_attempt(attempt, buffer)
        conn = self.store._conn
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM readings").fetchone()[0], 3)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM derived_readings").fetchone()[0], 2)
        self.assertEqual(conn.execute("SELECT source,value,method,inputs_json FROM scalar_readings WHERE name='test_estimate'").fetchone(),
                         ("derived", 853, "IA + IB", '["IA","IB"]'))
        self.assertEqual(conn.execute("SELECT source,value,valid FROM scalar_readings WHERE name='test_invalid_estimate'").fetchone(), ("derived", None, 0))
        self.assertEqual(conn.execute("SELECT source FROM scalar_readings WHERE name='IA'").fetchone(), ("meter_write",))
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM scalar_readings WHERE name='THD'").fetchone()[0], 0)
        stored = json.loads(conn.execute("SELECT decoded_json FROM attempts").fetchone()[0])
        self.assertEqual(stored["unavailable_quantities"], decoded["unavailable_quantities"])

    def test_derived_failure_rolls_back_meter_and_raw_rows(self):
        attempt, buffer = self.fixture()
        decoded = buffer["last_good"]["decoded"]
        derived = {"source": "derived", "value": 1, "valid": True, "unit": "1",
                   "method": "IA / IA", "inputs": ["IA"], "assumption": "IA nonzero"}
        decoded["readings"]["estimate"] = derived
        for key in ("method", "inputs", "assumption"):
            changed = copy.deepcopy(buffer)
            del changed["last_good"]["decoded"]["readings"]["estimate"][key]
            with self.subTest(key=key), self.assertRaises(ValueError):
                self.store.record_attempt(attempt, changed)
        self.store._conn.execute("""CREATE TRIGGER reject_derived BEFORE INSERT ON derived_readings
                                 BEGIN SELECT RAISE(ABORT,'synthetic derived write failure'); END""")
        with self.assertRaises(sqlite3.DatabaseError):
            self.store.record_attempt(attempt, buffer)
        for table in ("attempts", "raw_words", "readings", "derived_readings"):
            self.assertEqual(self.store._conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0], 0)

    def test_insert_failure_rolls_back_entire_attempt(self):
        attempt, buffer = self.fixture()
        self.store._conn.execute("""CREATE TRIGGER reject_reading BEFORE INSERT ON readings
                                 BEGIN SELECT RAISE(ABORT,'synthetic write failure'); END""")
        with self.assertRaises(sqlite3.DatabaseError):
            self.store.record_attempt(attempt, buffer)
        self.assertFalse(self.store._conn.in_transaction)
        for table in ("attempts", "raw_words", "readings"):
            self.assertEqual(self.store._conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0], 0)
        self.store._conn.execute("DROP TRIGGER reject_reading")
        self.assertTrue(self.store.record_attempt(attempt, buffer)["inserted"])

    def test_sqlite_full_rolls_back(self):
        attempt, buffer = self.fixture()
        page_count = self.store._conn.execute("PRAGMA page_count").fetchone()[0]
        self.store._conn.execute(f"PRAGMA max_page_count={page_count}")
        attempt["extra_test_data"] = "x" * (1024 * 1024)
        with self.assertRaises(sqlite3.DatabaseError) as failure:
            self.store.record_attempt(attempt, buffer)
        self.assertIn("full", str(failure.exception).lower())
        self.assertFalse(self.store._conn.in_transaction)
        self.assertEqual(self.store._conn.execute("SELECT COUNT(*) FROM attempts").fetchone()[0], 0)
        self.assertEqual(self.store._conn.execute("PRAGMA quick_check").fetchone()[0], "ok")

    def test_readers_and_standalone_backup(self):
        self.store.record_attempt(*self.fixture())
        reader = sqlite3.connect(self.store.path.as_uri() + "?mode=ro", uri=True)
        try:
            reader.execute("BEGIN")
            self.assertEqual(reader.execute("SELECT COUNT(*) FROM attempts").fetchone()[0], 1)
            self.store.record_attempt(*self.fixture("second", "2026-09-22T01:40:04+00:00"))
            self.assertEqual(reader.execute("SELECT COUNT(*) FROM attempts").fetchone()[0], 1)
            destination = self.root / "overnight.sqlite"
            self.store.backup(destination)
            with sqlite3.connect(destination) as backup:
                self.assertEqual(backup.execute("SELECT COUNT(*) FROM attempts").fetchone()[0], 2)
                self.assertEqual(backup.execute("PRAGMA quick_check").fetchone()[0], "ok")
                self.assertEqual(backup.execute("PRAGMA journal_mode").fetchone()[0], "delete")
            backup.close()
            self.assertFalse(Path(str(destination) + "-wal").exists())
            with self.assertRaises(FileExistsError):
                backup_database(self.store.path, destination)
        finally:
            reader.close()

    def test_utc_regression_is_flagged_not_rewritten(self):
        self.store.record_attempt(*self.fixture())
        self.store.record_attempt(*self.fixture("backfill", "2026-09-22T01:30:00Z"))
        row = self.store._conn.execute("SELECT attempt_id,observed_utc,source_time_out_of_order FROM attempts WHERE attempt_id=2").fetchone()
        self.assertEqual(row, (2, "2026-09-22T01:30:00.000000+00:00", 1))

    def test_bad_scalar_or_old_buffer_rejected(self):
        attempt, buffer = self.fixture()
        for value in (float("nan"), float("inf"), "446", True, None):
            changed = copy.deepcopy(buffer)
            changed["last_good"]["decoded"]["readings"]["IA"]["value"] = value
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.store.record_attempt(attempt, changed)
        buffer["last_good"]["observed_utc"] = "2026-09-21T00:00:00Z"
        with self.assertRaises(ValueError):
            self.store.record_attempt(attempt, buffer)
        self.assertEqual(self.store._conn.execute("SELECT COUNT(*) FROM attempts").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
