"""Math and quality tests for memory-only live diagnostic estimates."""
from datetime import datetime, timedelta, timezone
import math
import unittest
from live_diagnostics import DiagnosticAccumulator, FIELD_SPECS

BASE = datetime(2026, 9, 22, tzinfo=timezone.utc)


def document(seconds=0, **changes):
    values = {"IA": 100, "IB": 110, "IC": 90, "VAB": 480, "VBC": 490, "VCA": 500,
              "P_W": 100000, "PF": -0.95, "FREQUENCY_Hz": 60, "ENERGY_kWh": 1000}
    values.update(changes)
    return {"available": True, "protocol_valid": True, "stale": False,
            "observed_utc": (BASE+timedelta(seconds=seconds)).isoformat(),
            "readings": {k: {"value": v, "valid": v is not None, "stale": False} for k, v in values.items()}}


def val(output, key):
    return output[key]["value"]


class LiveDiagnosticTests(unittest.TestCase):
    def test_specs_and_warmup(self):
        self.assertEqual(len(FIELD_SPECS), 48)
        self.assertEqual(len({s["key"] for s in FIELD_SPECS}), 48)
        self.assertEqual(len({(s["type"], s["instance"]) for s in FIELD_SPECS}), 48)
        output = DiagnosticAccumulator().update(document())
        self.assertEqual(set(output), {s["key"] for s in FIELD_SPECS})
        for key in ("ROLLING_15MIN_DEMAND_kW", "ROLLING_15MIN_LOAD_FACTOR_pct",
                    "ROLLING_HOUR_ENERGY_kWh", "ROLLING_DAY_ENERGY_kWh", "OBSERVED_ENERGY_kWh"):
            self.assertIsNone(val(output, key))
            self.assertFalse(output[key]["valid"])
        self.assertFalse(val(output, "ROLLING_DEMAND_READY"))
        self.assertTrue(output["ROLLING_DEMAND_READY"]["valid"])
        self.assertIsNone(val(output, "VOLTAGE_EXCURSION_ACTIVE"))
        self.assertIsNone(val(output, "NOMINAL_VOLTAGE_V"))

    def test_imbalance_highest_phase_and_extrema(self):
        acc = DiagnosticAccumulator()
        result = acc.update(document())
        self.assertAlmostEqual(val(result, "VLL_IMBALANCE_pct"), 1000/490)
        self.assertAlmostEqual(val(result, "CURRENT_IMBALANCE_pct"), 10)
        self.assertEqual(val(result, "HIGHEST_CURRENT_PHASE"), 2)
        self.assertEqual(val(result, "HIGHEST_CURRENT_A"), 110)
        result = acc.update(document(1, IA=80, IC=130, VCA=510, PF=-0.8))
        for key, expected in (("SESSION_VLL_MIN_V", 480), ("SESSION_VLL_MAX_V", 510),
                              ("SESSION_CURRENT_MIN_A", 80), ("SESSION_CURRENT_MAX_A", 130),
                              ("SESSION_PF_MIN", 0.8), ("SESSION_PF_MAX", 0.95), ("HIGHEST_CURRENT_PHASE", 3)):
            self.assertEqual(val(result, key), expected)

    def test_time_weighted_not_arithmetic_average(self):
        acc = DiagnosticAccumulator()
        acc.update(document(0, P_W=1000))
        acc.update(document(1, P_W=1000))
        result = acc.update(document(5, P_W=9000))
        self.assertEqual(val(result, "SESSION_AVERAGE_POWER_kW"), 4.2)
        self.assertEqual(val(result, "SESSION_PEAK_POWER_kW"), 9)
        self.assertEqual(val(result, "LOAD_CHANGE_kW"), 8)
        self.assertAlmostEqual(val(result, "SESSION_POWER_COVERAGE_h"), 5/3600)

    def test_full_rolling_window_boundary_interpolation(self):
        acc = DiagnosticAccumulator()
        for seconds in range(0, 903, 3):
            result = acc.update(document(seconds, P_W=seconds*1000))
        self.assertAlmostEqual(val(result, "ROLLING_15MIN_DEMAND_kW"), 450)
        self.assertAlmostEqual(val(result, "ROLLING_15MIN_LOAD_FACTOR_pct"), 50)
        result = acc.update(document(902, P_W=902000))
        self.assertAlmostEqual(val(result, "ROLLING_15MIN_DEMAND_kW"), 452)
        self.assertAlmostEqual(val(result, "ROLLING_15MIN_LOAD_FACTOR_pct"), 100*452/902)
        self.assertTrue(val(result, "ROLLING_DEMAND_READY"))

    def test_zero_and_negative_power_no_invented_load_factor(self):
        for watts in (0, -100000):
            acc = DiagnosticAccumulator()
            for seconds in range(0, 901, 5):
                result = acc.update(document(seconds, P_W=watts))
            self.assertEqual(val(result, "ROLLING_15MIN_DEMAND_kW"), watts/1000)
            self.assertEqual(val(result, "SESSION_AVERAGE_POWER_kW"), watts/1000)
            self.assertIsNone(val(result, "ROLLING_15MIN_LOAD_FACTOR_pct"))
        result = DiagnosticAccumulator().update(document(IA=0, IB=0, IC=0))
        self.assertEqual(val(result, "HIGHEST_CURRENT_A"), 0)
        self.assertIsNone(val(result, "HIGHEST_CURRENT_PHASE"))
        self.assertIsNone(val(result, "CURRENT_IMBALANCE_pct"))

    def test_gap_breaks_windows_and_load_change(self):
        acc = DiagnosticAccumulator()
        for seconds in range(0, 901, 5):
            result = acc.update(document(seconds))
        self.assertTrue(val(result, "ROLLING_DEMAND_READY"))
        result = acc.update(document(910, P_W=200000, ENERGY_kWh=2000))
        for key in ("LOAD_CHANGE_kW", "ROLLING_15MIN_DEMAND_kW"):
            self.assertIsNone(val(result, key))
        self.assertEqual(val(result, "SAMPLE_GAP_COUNT"), 1)
        self.assertEqual(val(result, "EXCLUDED_GAP_TIME_s"), 10)
        self.assertEqual(val(result, "OBSERVED_ENERGY_kWh"), 0)
        self.assertEqual(val(result, "CONTINUOUS_ENERGY_COVERAGE_h"), 0)
        for seconds in range(915, 1811, 5):
            result = acc.update(document(seconds, P_W=200000, ENERGY_kWh=2000))
        self.assertEqual(val(result, "ROLLING_15MIN_DEMAND_kW"), 200)
        self.assertEqual(val(result, "ROLLING_15MIN_LOAD_FACTOR_pct"), 100)

    def test_pf_and_excursion_observed_durations(self):
        acc = DiagnosticAccumulator(nominal_vll=480)
        result = acc.update(document(0, PF=-0.8, FREQUENCY_Hz=61, VAB=550))
        for key in ("LOW_PF", "FREQUENCY_EXCURSION", "VOLTAGE_EXCURSION"):
            self.assertTrue(val(result, key+"_ACTIVE"))
        result = acc.update(document(3, PF=-0.7, FREQUENCY_Hz=61, VAB=550))
        for key in ("LOW_PF", "FREQUENCY_EXCURSION", "VOLTAGE_EXCURSION"):
            self.assertEqual(val(result, key+"_CONTINUOUS_s"), 3)
            self.assertEqual(val(result, key+"_TOTAL_s"), 3)
        result = acc.update(document(10, PF=-0.7, FREQUENCY_Hz=61, VAB=550))
        self.assertEqual(val(result, "LOW_PF_CONTINUOUS_s"), 0)
        self.assertEqual(val(result, "LOW_PF_TOTAL_s"), 3)
        result = acc.update(document(11))
        self.assertFalse(val(result, "LOW_PF_ACTIVE"))
        self.assertEqual(val(result, "LOW_PF_TOTAL_s"), 3)

    def test_counter_full_hour_and_day(self):
        acc = DiagnosticAccumulator(max_gap_seconds=3600)
        for seconds in range(0, 86401, 3600):
            result = acc.update(document(seconds, ENERGY_kWh=1000+seconds/60))
            if seconds < 86400:
                self.assertFalse(result["ROLLING_DAY_ENERGY_kWh"]["valid"])
        for key, expected in (("ROLLING_HOUR_ENERGY_kWh", 60), ("ROLLING_DAY_ENERGY_kWh", 1440),
                              ("OBSERVED_ENERGY_kWh", 1440), ("CONTINUOUS_ENERGY_COVERAGE_h", 24),
                              ("SESSION_ENERGY_COVERAGE_h", 24)):
            self.assertEqual(val(result, key), expected)
        self.assertTrue(val(result, "ROLLING_DAY_ENERGY_READY"))
        result = acc.update(document(86405, ENERGY_kWh=2440))
        self.assertAlmostEqual(val(result, "ROLLING_DAY_ENERGY_kWh"), 1440-5/60)

    def test_counter_reset_or_wrap_rejects_increment(self):
        acc = DiagnosticAccumulator()
        acc.update(document(0, ENERGY_kWh=16777214))
        result = acc.update(document(1, ENERGY_kWh=16777215))
        self.assertEqual(val(result, "OBSERVED_ENERGY_kWh"), 1)
        result = acc.update(document(2, ENERGY_kWh=0))
        self.assertEqual(val(result, "ENERGY_COUNTER_RESET_COUNT"), 1)
        self.assertEqual(val(result, "OBSERVED_ENERGY_kWh"), 1)
        self.assertEqual(val(result, "CONTINUOUS_ENERGY_COVERAGE_h"), 0)
        self.assertIsNone(val(result, "ROLLING_HOUR_ENERGY_kWh"))
        result = acc.update(document(3, ENERGY_kWh=1))
        self.assertEqual(val(result, "OBSERVED_ENERGY_kWh"), 2)
        self.assertAlmostEqual(val(result, "SESSION_ENERGY_COVERAGE_h"), 2/3600)

    def test_duplicates_stale_and_out_of_order(self):
        acc = DiagnosticAccumulator()
        acc.update(document(0))
        original = acc.update(document(1, ENERGY_kWh=1001))
        for _ in range(3):
            self.assertEqual(acc.update(document(1, ENERGY_kWh=2000)), original)
        stale = document(2)
        stale["stale"] = True
        self.assertTrue(all(item["stale"] for item in acc.update(stale).values()))
        result = acc.update(document(3, ENERGY_kWh=1003))
        self.assertEqual(val(result, "OBSERVED_ENERGY_kWh"), 1)
        self.assertIsNone(val(result, "LOAD_CHANGE_kW"))
        self.assertEqual(val(result, "ACCEPTED_SAMPLES"), 3)
        for _ in range(2):
            rejected = acc.update(document(2))
            self.assertEqual(val(rejected, "OUT_OF_ORDER_COUNT"), 1)
            self.assertTrue(rejected["OUT_OF_ORDER_COUNT"]["stale"])
        result = acc.update(document(4, ENERGY_kWh=1004))
        self.assertEqual(val(result, "OBSERVED_ENERGY_kWh"), 1)
        self.assertEqual(val(result, "OUT_OF_ORDER_COUNT"), 1)

    def test_partial_invalid_readings_not_contaminate_totals(self):
        acc = DiagnosticAccumulator()
        acc.update(document())
        sample = document(1, P_W=math.nan, IA=-1, PF=1.1, ENERGY_kWh=None)
        sample["readings"]["FREQUENCY_Hz"]["stale"] = True
        result = acc.update(sample)
        for key in ("LOAD_CHANGE_kW", "CURRENT_IMBALANCE_pct", "HIGHEST_CURRENT_A",
                    "FREQUENCY_EXCURSION_ACTIVE", "LOW_PF_ACTIVE", "OBSERVED_ENERGY_kWh"):
            self.assertFalse(result[key]["valid"])
        self.assertTrue(result["VLL_IMBALANCE_pct"]["valid"])
        result = acc.update(document(2, ENERGY_kWh=1002))
        self.assertIsNone(val(result, "OBSERVED_ENERGY_kWh"))
        self.assertIsNone(val(result, "SESSION_AVERAGE_POWER_kW"))

    def test_bounded_history_never_false_full_window(self):
        acc = DiagnosticAccumulator(max_samples=10)
        for seconds in range(0, 1001):
            result = acc.update(document(seconds, ENERGY_kWh=seconds))
        self.assertEqual(len(acc.power), 10)
        self.assertEqual(len(acc.energy), 10)
        self.assertFalse(val(result, "ROLLING_DEMAND_READY"))
        self.assertFalse(val(result, "ROLLING_HOUR_ENERGY_READY"))
        self.assertEqual(val(result, "ROLLING_POWER_COVERAGE_s"), 10)

    def test_invalid_configuration_and_timestamps(self):
        for kwargs in ({"nominal_vll": 0}, {"nominal_hz": math.nan}, {"max_gap_seconds": -1},
                       {"low_pf_threshold": 2}, {"max_samples": 2}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                DiagnosticAccumulator(**kwargs)
        acc = DiagnosticAccumulator()
        malformed = document()
        for stamp in ("2026-09-22T00:00:00", "not a timestamp"):
            malformed["observed_utc"] = stamp
            self.assertEqual(acc.update(malformed), {})
        self.assertEqual(acc.accepted_samples, 0)

    def test_source_age_checked_even_if_caller_stale_flag_false(self):
        acc = DiagnosticAccumulator()
        old = document()
        old["age_seconds"] = 6
        self.assertEqual(acc.update(old), {})
        self.assertEqual(acc.accepted_samples, 0)
        fresh = document(1)
        fresh["age_seconds"] = 0.1
        fresh["readings"]["P_W"]["age_seconds"] = 8
        result = acc.update(fresh)
        self.assertFalse(result["SESSION_PEAK_POWER_kW"]["valid"])
        self.assertTrue(result["VLL_IMBALANCE_pct"]["valid"])


if __name__ == "__main__":
    unittest.main()
