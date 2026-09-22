"""Recorder control-flow checks; no serial, network, or real delays."""
import tempfile
import unittest
from unittest.mock import patch, Mock, call

import poll_meter
from derived_power import add_derived


class RecordingTests(unittest.TestCase):
    def test_power_triangle_keeps_origin_and_zero_unknown_pf(self):
        sample = {"readings": {"P_W": {"value": 3000, "valid": True},
                               "Q_var": {"value": -4000, "valid": True}}}
        values = add_derived(sample)["readings"]
        self.assertEqual(values["S_PQ_estimate_VA"]["value"], 5000)
        self.assertEqual(values["PF_PQ_magnitude"]["value"], 0.6)
        self.assertEqual(values["S_PQ_estimate_VA"]["source"], "derived")
        sample["readings"]["P_W"]["value"] = sample["readings"]["Q_var"]["value"] = 0
        self.assertIsNone(add_derived(sample)["readings"]["PF_PQ_magnitude"]["value"])

    def run_recorder(self, attempts):
        attempts = [{"kind": "all_standard", "started_utc": "2026-09-22T03:00:00+00:00",
                     "finished_utc": "2026-09-22T03:00:01+00:00", **item} for item in attempts]
        with tempfile.TemporaryDirectory() as directory:
            args = ["poll_meter.py", "--state", directory, "--port", "/dev/example-test-only",
                    "--continuous", "--recover", "--database", directory + "/test.sqlite",
                    "--kinds", "all_standard", "--interval", "1.05"]
            store = Mock()
            with patch("sys.argv", args), patch("store_readings.ReadingStore", return_value=store), \
                    patch.object(poll_meter, "poll_once", side_effect=attempts) as poll, \
                    patch.object(poll_meter.time, "monotonic", return_value=0), \
                    patch.object(poll_meter.time, "sleep") as sleep, patch("builtins.print"):
                code = poll_meter.main()
            self.assertEqual(code, 130)
            self.assertEqual(store.record_attempt.call_count, len(attempts))
            self.assertEqual(poll.call_count, len(attempts))
            store.close.assert_called_once()
            return sleep.call_args_list

    def test_operator_interrupt_never_retries(self):
        self.assertEqual(self.run_recorder([{"ok": False, "interrupted": True}]), [])

    def test_failed_reads_back_off_and_are_recorded(self):
        waits = self.run_recorder([{"ok": False}, {"ok": False}, {"ok": False, "interrupted": True}])
        self.assertEqual(waits, [call(2), call(4)])

    def test_diagnostic_slots_never_enter_measurement_database(self):
        with tempfile.TemporaryDirectory() as directory:
            argv = ["poll_meter.py", "--state", directory, "--port", "/dev/example-test-only",
                    "--database", directory + "/test.sqlite", "--kinds", "all_standard",
                    "--duration-ms", "500", "--interval", "1.05", "--cycles", "2", "--diagnostics"]
            store = Mock()
            def poll(args, kind, buffers):
                # Settings failure must not drop the successful primary sample.
                ok = kind != "settings"
                if ok:
                    buffers[kind] = {"last_good": {"decoded": {"readings": {
                        "FLAGS_TRIP": {"value": False}}}}}
                return {"kind": kind, "ok": ok, "started_utc": "2026-09-22T03:00:00+00:00",
                        "finished_utc": "2026-09-22T03:00:01+00:00"}
            with patch("sys.argv", argv), patch("store_readings.ReadingStore", return_value=store), \
                    patch.object(poll_meter, "poll_once", side_effect=poll) as execute, \
                    patch.object(poll_meter.time, "monotonic", return_value=0), \
                    patch.object(poll_meter.time, "sleep") as sleep, patch("builtins.print"):
                self.assertEqual(poll_meter.main(), 0)
            self.assertEqual([item.args[1] for item in execute.call_args_list],
                             ["all_standard", "flags", "all_standard", "settings"])
            self.assertEqual([item.args[0]["kind"] for item in store.record_attempt.call_args_list],
                             ["all_standard", "all_standard"])
            self.assertTrue(all(item.args[0] >= 1.0 for item in sleep.call_args_list))

    def test_diagnostic_payloads_and_trip_refresh(self):
        self.assertEqual(poll_meter.payload_for("flags", 0), 0x8000c3)
        self.assertEqual(poll_meter.payload_for("settings", 0), 0x9000c3)
        self.assertEqual(poll_meter.payload_for("trip", 0), 0xa000c3)
        schedule = poll_meter.DiagnosticSchedule()
        def flags(value):
            return {"flags": {"last_good": {"decoded": {"readings": {"FLAGS_TRIP": {"value": value}}}}}}
        schedule.completed("flags", {"ok": True}, flags(False), 10)
        schedule.due["trip"] = 100
        schedule.completed("flags", {"ok": True}, flags(True), 20)
        self.assertEqual(schedule.due["trip"], 0)


if __name__ == "__main__":
    unittest.main()
