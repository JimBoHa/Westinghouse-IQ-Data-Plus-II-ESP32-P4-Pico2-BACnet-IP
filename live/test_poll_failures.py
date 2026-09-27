"""Failure-path integration checks with preserved reports and no hardware."""
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

import poll_meter


class PollFailureTests(unittest.TestCase):
    def attempt(self, result, code):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)
            args = SimpleNamespace(raw_dir=state, state=state, address=0, edge="rising",
                                   duration_ms=500, port="/dev/OFFLINE_TEST",
                                   kinds=["all_standard"], interval=1.05)
            def execute(invocation):
                invocation.output.write_text(json.dumps({"result": result,
                    "errors": ["TimeoutError: No terminal response before bounded command deadline"]}))
                invocation.output.with_suffix(".json.jsonl").write_text("")
                return code
            decoder = Mock()
            attempt = poll_meter.poll_once(args, "all_standard", {}, execute=execute, decoder=decoder)
            decoder.assert_not_called()
            self.assertFalse(attempt["ok"])
            self.assertEqual(len(attempt["evidence_refs"]), 2)
            saved = json.loads((state / "decoded_readings.json").read_text())
            self.assertTrue(saved["stale"])
            return attempt

    def test_timeout_keeps_original_error_when_result_missing(self):
        attempt = self.attempt(None, 1)
        self.assertIn("TimeoutError: No terminal response", attempt["errors"][0])
        self.assertNotIn("AttributeError", attempt["errors"][0])

    def test_success_without_result_is_rejected_before_decoding(self):
        for result in (None, [], "invalid"):
            with self.subTest(result=result):
                self.assertIn("without a result object", self.attempt(result, 0)["errors"][0])


if __name__ == "__main__":
    unittest.main()
