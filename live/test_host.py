"""Offline host parsing and saved-state tests. No serial ports or sockets opened."""
import json
from pathlib import Path
import tempfile
import unittest

import host


class HostTests(unittest.TestCase):
    def test_decoded_telemetry_preserves_unverified_display_and_freshness(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)
            data = {"source": "meter_write", "protocol_valid": True, "display_verified": False,
                    "evidence_refs": ["physical-trial.jsonl"], "readings": [{"value": 488, "unit": "V"}],
                    "observed_utc": host.utc(), "stale_after_seconds": 30, "stale": False}
            path = state / "decoded_readings.json"
            path.write_text(json.dumps(data))
            code, result = host.telemetry(state)
            self.assertEqual(code, 200)
            self.assertFalse(result["display_verified"])
            for changes in ({"stale": True}, {"observed_utc": "2020-01-01T00:00:00+00:00"},
                            {"protocol_valid": False}, {"source": "host_write"}, {"evidence_refs": []}):
                path.write_text(json.dumps({**data, **changes}))
                self.assertEqual(host.telemetry(state)[0], 503)
            path.write_text(json.dumps({**data, "observed_utc": "2020-01-01T00:00:00+00:00"}))
            _, old = host.telemetry(state)
            self.assertTrue(old["readings"][0]["stale"])
            self.assertEqual(old["quality"], "stale_or_failed")

    def test_order_probe_address_restriction(self):
        for kind in host.ORDER_PROBES:
            with self.subTest(kind=kind):
                self.assertEqual(host.make_command("transact", [kind, "0", "rising", "1000"]),
                                 (f"transact {kind} 0 rising 1000", 6.0))
                with self.assertRaises(ValueError):
                    host.make_command("transact", [kind, "1", "rising", "1000"])

    def test_bounds_and_allowlist(self):
        self.assertEqual(host.make_command("transact", ["currents", "4095", "falling", "5000"]),
                         ("transact currents 4095 falling 5000", 10.0))
        self.assertEqual(host.make_command("observe", ["10000"]), ("observe 10000", 15.0))
        for action, args in [("observe", ["0"]), ("observe", ["10001"]), ("trial_int", ["1001"]),
                             ("transact", ["currents", "4096", "rising", "5"]),
                             ("transact", ["currents", "-1", "rising", "5"]),
                             ("transact", ["currents", "1", "rising", "5001"]),
                             ("transact", ["unknown", "1", "rising", "5"]),
                             ("arm", []), ("info", ["extra"]), ("observe", ["10\nabort"])]:
            with self.subTest(action=action, args=args), self.assertRaises(ValueError):
                host.make_command(action, args)

    def test_result_boundary_ignores_started_and_host_payload(self):
        boundary = host.Boundary("transact")
        self.assertFalse(boundary.accept({"type": "started"}))
        self.assertFalse(boundary.accept({"type": "event", "source": "host_write", "payload": 123,
                                          "result": {"readings": [490]}}))
        self.assertFalse(boundary.accept({"type": "status", "result": True}))
        self.assertIsNone(boundary.terminal)
        actual = {"type": "result", "raw_word_count": 0}
        self.assertTrue(boundary.accept(actual))
        self.assertEqual(boundary.terminal, actual)
        with self.assertRaises(ValueError):
            boundary.accept({"type": "result"})

    def test_result_without_started_and_error(self):
        self.assertTrue(host.Boundary("observe").accept({"type": "result"}))
        error = host.Boundary("trial_int")
        self.assertTrue(error.accept({"type": "error", "error": "disabled"}))
        self.assertTrue(error.device_error)
        self.assertTrue(host.Boundary("info").accept({"type": "info"}))

    def test_malformed_json(self):
        for text in ('{"type":', '[]', '{"value":NaN}'):
            with self.subTest(text=text), self.assertRaises(ValueError):
                host.json_object(text)

    def test_telemetry_unavailable_gate(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)
            self.assertEqual(host.endpoint(state, "/telemetry")[0], 503)
            # A transmitted payload in latest_result never becomes telemetry.
            (state / "latest_result.json").write_text(json.dumps({"result": {"source": "host_write", "value": 490}}))
            self.assertEqual(host.endpoint(state, "/telemetry")[0], 503)
            valid = {"source": "meter_write", "display_verified": True,
                     "evidence_refs": ["trial.jsonl#rx-42", "display_observation.json"],
                     "readings": [{"quantity": "example", "value": 1, "unit": "test-only"}]}
            for changed in ({"source": "host_write"}, {"display_verified": False},
                            {"display_verified": 1}, {"evidence_refs": []}, {"readings": []}):
                (state / "validated_readings.json").write_text(json.dumps({**valid, **changed}))
                self.assertEqual(host.endpoint(state, "/telemetry")[0], 503)
            (state / "validated_readings.json").write_text(json.dumps(valid))
            code, payload = host.endpoint(state, "/telemetry")
            self.assertEqual(code, 200)
            self.assertTrue(payload["available"])
            self.assertEqual(payload["readings"], valid["readings"])
            self.assertTrue(host.endpoint(state, "/status")[1]["read_only"])
            self.assertEqual(host.endpoint(state, "/transact")[0], 404)

    def test_journal_preserves_received_bytes(self):
        with tempfile.TemporaryDirectory() as directory:
            journal = host.Journal(Path(directory) / "trial.json")
            raw = b'{"type":"result"}\r\n'
            journal.append("rx", "command", raw)
            journal.close()
            record = json.loads(journal.path.read_text())
            self.assertEqual(record["text"], raw.decode())
            self.assertEqual(host.base64.b64decode(record["raw_base64"]), raw)
            self.assertTrue(record["utc"])
            with self.assertRaises(FileExistsError):
                host.Journal(Path(directory) / "trial.json")


if __name__ == "__main__":
    unittest.main()
