"""Decoder checks: manufacturer examples, field limits, and rejected frames.

These are software test vectors, not captured meter measurements.
"""
from decimal import Decimal
import unittest

from decode_meter import decode_impacc, decode_request


class DecodeMeterTests(unittest.TestCase):
    def test_manufacturer_scaling_examples(self):
        # Part A2011 printed14: two representations of41300, then-79.46.
        for payload, expected in [(0x611022, "41300"), (0x62019D, "41300"),
                                  (0xFEE0F6, "-79.46")]:
            with self.subTest(payload=hex(payload)):
                value = decode_impacc(payload)
                self.assertTrue(value["valid"])
                self.assertEqual(Decimal(value["value_exact"]), Decimal(expected))

    def test_exponent_limits_and_mantissa_signedness(self):
        cases = [
            (0x500001, "0.0000152587890625", -16),
            (0x4F0001, "32768", 15),
            (0x700001, "0.0000000000000001", -16),
            (0x6F0001, "1000000000000000", 15),
            (0x40FFFF, "65535", 0), (0xC0FFFF, "-1", 0),
            (0xC08000, "-32768", 0), (0xC07FFF, "32767", 0),
            (0x5F0002, "1", -1),
        ]
        for payload, expected, exponent in cases:
            with self.subTest(payload=hex(payload)):
                reading = decode_impacc(payload)
                self.assertEqual(Decimal(reading["value_exact"]), Decimal(expected))
                self.assertEqual(reading["exponent"], exponent)

    def test_physical_status_reply(self):
        # Parent-reported physical reply; independent analyzer verification separate.
        result = decode_request("fast_status_repeat", [0xB01482])
        self.assertTrue(result["valid"])
        status = result["status"]
        self.assertEqual((status["division"], status["product_id"],
                          status["communication_version"], status["status_byte"]),
                         (1, 2, 9, 0x58))
        self.assertEqual(status["state"], "normal_active")
        self.assertEqual(status["product_specific_bits"], 0x18)
        self.assertFalse(status["remote_open_or_off"])

    def test_buffer_order_and_current_reserved(self):
        words = [0x400001 << 1, 0x400002 << 1, 0x400003 << 1]
        volts = decode_request("line_voltages_repeat", words)
        self.assertTrue(volts["valid"])
        self.assertEqual([volts["readings"][name]["value"] for name in
                          ("VAB", "VBC", "VCA")], [1, 2, 3])
        for reserved in (0, 0xFFFFFF << 1):
            currents = decode_request("currents_repeat", words + [reserved])
            self.assertTrue(currents["valid"])
            self.assertEqual(set(currents["readings"]), {"IA", "IB", "IC"})
            self.assertEqual(currents["reserved"][0]["raw_word"], reserved)
            self.assertEqual(currents["readings"]["IA"]["unit"], "A")

    def test_invalid_analog_retains_flags_but_no_value(self):
        words = [0x000001 << 1, 0x400002 << 1, 0x400003 << 1]
        result = decode_request("voltage", words)
        self.assertTrue(result["complete"])
        self.assertFalse(result["valid"])
        self.assertIsNone(result["readings"]["VAB"]["value"])
        self.assertIsNone(result["readings"]["VAB"]["value_exact"])
        self.assertEqual(result["readings"]["VAB"]["mantissa"], 1)
        self.assertEqual(result["readings"]["VBC"]["value"], 2)

    def test_incomplete_overlong_control_and_bad_words_rejected(self):
        for words in ([], [0xB01482, 0xB01482], [0x400027], [-1],
                      [0x2000000], [True], [1.5], ["not_hex"]):
            with self.subTest(words=words):
                result = decode_request("status", words)
                self.assertFalse(result["complete"])
                self.assertFalse(result["valid"])
                self.assertTrue(result["errors"])
                self.assertIsNone(result["status"])
                self.assertEqual(result["readings"], {})
                self.assertEqual(result["input_words"], words)
        self.assertFalse(decode_request("currents", [0, 0, 0])["complete"])
        self.assertFalse(decode_request("voltage", [0, 0, 0, 0])["complete"])
        self.assertFalse(decode_request("currents", [0, 0, 0, 1])["complete"])

    def test_identity_and_numeric_string_inputs(self):
        self.assertTrue(decode_request("status", ["0xB01482"])["valid"])
        result = decode_request("status", [0])
        self.assertTrue(result["complete"])
        self.assertFalse(result["valid"])
        self.assertFalse(result["status"]["matches_iq_data_plus_ii"])
        self.assertFalse(decode_request("unknown", [0])["valid"])
        for payload in (-1, 0x1000000, True):
            with self.assertRaises(ValueError):
                decode_impacc(payload)

    def test_power_buffers_preserve_order_units_and_sign(self):
        power1 = decode_request("power1_repeat", [0xC0FF9C << 1, 0x4000C8 << 1,
                                                 0x62019D << 1])
        self.assertTrue(power1["valid"])
        self.assertEqual([(name, r["value"], r["unit"]) for name, r in
                          power1["readings"].items()],
                         [("P_W", -100, "W"), ("DEMAND_W", 200, "W"),
                          ("ENERGY_Wh", 41300, "Wh")])
        power2 = decode_request("power2_repeat", [0x7E1770 << 1, 0xC0FFCE << 1,
                                                 0xFEFF9D << 1])
        self.assertEqual([(name, r["value"], r["unit"]) for name, r in
                          power2["readings"].items()],
                         [("FREQUENCY_Hz", 60, "Hz"), ("Q_var", -50, "var"),
                          ("PF", -0.99, "1")])
        self.assertTrue(power2["valid"])
        self.assertEqual(power2["readings"]["PF"]["phase_relation"], "lagging")
        self.assertEqual(power2["readings"]["PF"]["calculation_mode"], "unknown_meter_setting")
        for kind in ("power1", "power2"):
            self.assertFalse(decode_request(kind, [0, 0])["complete"])
            self.assertFalse(decode_request(kind, [0, 0, 0, 0])["complete"])

    def test_energy_is_unsigned_integer_without_scale_valid_bit(self):
        for payload in (0, 1, 0x800000, 0xFFFFFF):
            with self.subTest(payload=payload):
                result = decode_request("energy_repeat", [payload << 1])
                self.assertTrue(result["valid"])
                reading = result["readings"]["ENERGY_kWh"]
                self.assertEqual(reading["value"], payload)
                self.assertEqual(reading["value_exact"], str(payload))
                self.assertEqual(reading["encoding"], "uint24")
                self.assertNotIn("scale_byte", reading)
        self.assertFalse(decode_request("energy", [1])["complete"])

    def test_all_standard_full_map_order_and_word_indices(self):
        # Capability bits0,3,5..A. Header itself is not an analog value.
        payloads = [0x7E9] + [0x400000 | n for n in range(1, 17)] + [123456]
        result = decode_request("all_standard_repeat", [p << 1 for p in payloads])
        self.assertTrue(result["valid"])
        self.assertEqual(result["expected_words"], 18)
        self.assertEqual(result["buffer_map"]["included_buffers"],
                         ["currents", "line_voltages", "neutral_voltages",
                          "power1", "power2", "energy"])
        for name, value, index in [("IA", 1, 1), ("IC", 3, 3), ("VAB", 5, 5),
                                   ("VAN", 8, 8), ("P_W", 11, 11),
                                   ("FREQUENCY_Hz", 14, 14), ("PF", 16, 16),
                                   ("ENERGY_kWh", 123456, 17)]:
            self.assertEqual(result["readings"][name]["value"], value)
            self.assertEqual(result["readings"][name]["word_index"], index)
        self.assertEqual(result["reserved"][0]["index"], 4)
        self.assertNotIn("IX", result["readings"])

    def test_all_standard_sparse_map_and_header_only(self):
        # C0 capability does not insert status into the response. Expanded buffer
        # count is metadata, not an instruction to consume more DATA messages.
        header = (63 << 16) | (1 << 9) | 1
        result = decode_request("all_standard", [header << 1] + [0x400001 << 1] * 3)
        self.assertTrue(result["valid"])
        self.assertEqual(result["expected_words"], 4)
        self.assertEqual(result["buffer_map"]["expanded_buffer_count"], 63)
        self.assertEqual(set(result["readings"]), {"FREQUENCY_Hz", "Q_var", "PF"})
        empty = decode_request("all_standard", [0])
        self.assertTrue(empty["complete"])
        self.assertEqual(empty["readings"], {})

    def test_all_standard_rejects_unknown_map_and_wrong_counts(self):
        for payloads in ([], [0x800], [0x400000], [0x800000], [0x20, 0, 0, 0],
                         [0, 0]):
            result = decode_request("all_standard", [p << 1 for p in payloads])
            self.assertFalse(result["complete"])
            self.assertFalse(result["protocol_valid"])
            self.assertEqual(result["readings"], {})

    def test_all_standard_invalid_optional_field_keeps_valid_siblings(self):
        # Three LN values are advertised but unavailable; power1 still decodes.
        header = (1 << 7) | (1 << 8)
        result = decode_request("all_standard", [header << 1] + [0] * 3 +
                                [0x400001 << 1] * 3)
        self.assertTrue(result["complete"])
        self.assertTrue(result["protocol_valid"])
        self.assertFalse(result["valid"])
        self.assertFalse(result["all_readings_valid"])
        self.assertIsNone(result["readings"]["VAN"]["value"])
        self.assertTrue(result["readings"]["P_W"]["valid"])
        self.assertEqual(result["readings"]["P_W"]["value"], 1)

    def test_physical_all_standard_reply(self):
        # Parent-reported first physical aggregate response. The preceding
        # 0x400027 handshake was removed by transaction provenance checking.
        words = [0xFD6, 0x80026C, 0x800246, 0x800246, 0x800000, 0x8003D0,
                 0x8003D8, 0x8003D8, 0x800238, 0x800238, 0x800238, 0x1C601D6,
                 0x1C605A0, 0xCC3E96, 0xFC2EEA, 0x1C7FF54, 0x1FDFF44, 0xF47E96]
        result = decode_request("all_standard", words)
        self.assertTrue(result["protocol_valid"])
        self.assertTrue(result["all_readings_valid"])
        self.assertTrue(result["valid"])
        self.assertEqual(result["buffer_map"]["raw_payload"], 0x7EB)
        expected = {"IA": 310, "IB": 291, "IC": 291, "VAB": 488,
                    "VBC": 492, "VCA": 492, "VAN": 284, "VBN": 284, "VCN": 284,
                    "P_W": 235000, "DEMAND_W": 720000, "ENERGY_Wh": 8011000000,
                    "FREQUENCY_Hz": 60.05, "Q_var": -86000, "PF": -0.94,
                    "ENERGY_kWh": 8011595}
        self.assertEqual({k: v["value"] for k, v in result["readings"].items()}, expected)
        self.assertEqual(result["readings"]["PF"]["phase_relation"], "lagging")
        self.assertEqual(result["readings"]["PF"]["method"], "meter_reported")
        self.assertIn("+P/-Q inductive lagging", result["readings"]["Q_var"]["sign_convention"])
        self.assertEqual(result["readings"]["ENERGY_Wh"]["exponent"], 6)
        self.assertEqual(result["readings"]["ENERGY_kWh"]["encoding"], "uint24")


if __name__ == "__main__":
    unittest.main()
