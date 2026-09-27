"""Settings expiration must reach BACnet even without a new measurement."""
import asyncio
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import unittest

from bacnet_server import auxiliary_readings, EXTRA_SPECS
from bacnet_extras import build_extra_objects, refresh_extra_objects
from live_diagnostics import DiagnosticAccumulator
from test_live_diagnostics import document


class SettingsQualityTests(unittest.TestCase):
    def test_independent_expiration_faults_dependents_and_recovers(self):
        async def check():
            with tempfile.TemporaryDirectory() as directory:
                state = Path(directory)
                now = datetime.now(timezone.utc)
                config = {key: {"value": value, "valid": True, "stale": False,
                    "observed_utc": now.isoformat()} for key, value in
                    [("CONFIG_NOMINAL_LL_V", 460), ("CONFIG_FREQUENCY_Hz", 60)]}
                def save():
                    (state / "meter_diagnostics.json").write_text(json.dumps({"readings": config}))
                save()
                acc = DiagnosticAccumulator()
                sample = document()
                output = auxiliary_readings(state, sample, acc)
                specs = [s for s in EXTRA_SPECS if s["key"] in {
                    "NOMINAL_VOLTAGE_V", "NOMINAL_FREQUENCY_Hz", "VOLTAGE_EXCURSION_ACTIVE",
                    "FREQUENCY_EXCURSION_ACTIVE", "VOLTAGE_EXCURSION_TOTAL_s"}]
                objects = build_extra_objects(specs)
                # Quality evaluation uses the synthetic measurement time.
                stamp = datetime.fromisoformat(sample["observed_utc"]).timestamp()
                self.assertTrue(all(refresh_extra_objects(objects, specs, output, now=stamp).values()))
                config["CONFIG_NOMINAL_LL_V"]["observed_utc"] = (now-timedelta(seconds=3601)).isoformat()
                save()
                # Same measurement: cached output must not bypass invalidation.
                output = auxiliary_readings(state, sample, acc)
                quality = refresh_extra_objects(objects, specs, output, now=stamp)
                self.assertFalse(quality["NOMINAL_VOLTAGE_V"])
                self.assertFalse(quality["VOLTAGE_EXCURSION_ACTIVE"])
                self.assertFalse(quality["VOLTAGE_EXCURSION_TOTAL_s"])
                self.assertTrue(quality["NOMINAL_FREQUENCY_Hz"])
                self.assertEqual(acc.accepted_samples, 1)
                config.clear()
                save()
                output = auxiliary_readings(state, document(1), acc)
                self.assertFalse(output["FREQUENCY_EXCURSION_ACTIVE"]["valid"])
                self.assertTrue(output["VLL_IMBALANCE_pct"]["valid"])
                config.update({key: {"value": value, "valid": True, "stale": False,
                    "observed_utc": now.isoformat()} for key, value in
                    [("CONFIG_NOMINAL_LL_V", 480), ("CONFIG_FREQUENCY_Hz", 60)]})
                save()
                output = auxiliary_readings(state, document(2), acc)
                self.assertTrue(output["NOMINAL_VOLTAGE_V"]["valid"])
                self.assertEqual(output["NOMINAL_VOLTAGE_V"]["value"], 480)
                self.assertTrue(output["FREQUENCY_EXCURSION_ACTIVE"]["valid"])
                self.assertEqual(output["VOLTAGE_EXCURSION_CONTINUOUS_s"]["value"], 0)
                await asyncio.sleep(0)
        asyncio.run(check())


if __name__ == "__main__":
    unittest.main()
