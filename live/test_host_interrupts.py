"""Exercise real host -> recorder interrupt propagation, with mocked USB only."""
import contextlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

import host
import poll_meter


class HostInterruptTests(unittest.TestCase):
    def test_interrupt_cleans_up_records_attempt_and_stops_without_retry(self):
        for responses in ([None, KeyboardInterrupt(), {"type": "abort", "released": True}],
                          [None, TimeoutError("read timeout"), KeyboardInterrupt()]):
            with self.subTest(responses=responses), tempfile.TemporaryDirectory() as directory:
                receiver = Mock(pending=bytearray())
                receiver.line.side_effect = responses
                serial = Mock()
                store = Mock()
                argv = ["poll_meter.py", "--port", "/dev/OFFLINE_TEST", "--state", directory,
                        "--continuous", "--recover", "--database", directory + "/test.sqlite",
                        "--kinds", "all_standard", "--duration-ms", "500"]
                with patch.dict(sys.modules, {"serial": serial}), \
                        patch.object(host, "Receiver", return_value=receiver), \
                        patch("store_readings.ReadingStore", return_value=store), \
                        patch("sys.argv", argv), \
                        patch.object(poll_meter, "poll_once", wraps=poll_meter.poll_once) as poll, \
                        patch.object(poll_meter.time, "sleep") as sleep, \
                        contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(poll_meter.main(), 130)
                self.assertEqual(poll.call_count, 1)
                sleep.assert_not_called()
                serial.Serial.return_value.close.assert_called_once()
                receiver.send.assert_any_call("abort", "cleanup")
                store.record_attempt.assert_called_once()
                self.assertTrue(store.record_attempt.call_args.args[0]["interrupted"])
                store.close.assert_called_once()
                report = json.loads((Path(directory) / "latest_result.json").read_text())
                self.assertEqual(report["status"], "failed")
                self.assertTrue(any("KeyboardInterrupt" in error for error in report["errors"]))


if __name__ == "__main__":
    unittest.main()
