"""Diagnostic decoding checks against protocol/manual tables and captured words.

Only the explicit physical-vector tests are meter evidence supplied by the
acquisition owner. Other inputs are software checks, not simulated acquisition.
"""

import json
import unittest

from decode_diagnostics import FIELD_SPECS, decode_diagnostics


def settings_words(switches, revision=1, version=8):
    header = 2 | (revision << 8) | (version << 16)
    payloads = [header]
    for start in (0, 3):
        payloads.append(sum(switches[start + n] << (8 * n) for n in range(3)))
    return [value << 1 for value in payloads]


def trip_words(flags=1, payloads=None):
    # Sixteen data slots, including a reserved fourth current slot.
    if payloads is None:
        payloads = [0x400000 | n for n in range(1, 17)]
    return [17 << 1, flags << 1] + [value << 1 for value in payloads]


class DiagnosticsTests(unittest.TestCase):
    def test_exact_counts_header_and_control_framing(self):
        examples = {"flags": [2, 0], "settings": settings_words([0] * 6),
                    "trip": trip_words()}
        for kind, good in examples.items():
            for bad in ([], good[:-1], good + [0], [0] + good[1:]):
                with self.subTest(kind=kind, bad=bad):
                    result = decode_diagnostics(kind, bad)
                    self.assertFalse(result["protocol_valid"])
                    self.assertFalse(result["complete"])
                    self.assertEqual(result["readings"], {})
            for index in range(len(good)):
                bad = good[:]
                bad[index] |= 1
                result = decode_diagnostics(kind, bad)
                self.assertFalse(result["protocol_valid"])
                self.assertEqual(result["readings"], {})

    def test_bad_word_types_ranges_and_aliases(self):
        for invalid in (-1, 0x2000000, True, 1.2, None, "not a number"):
            result = decode_diagnostics("flags", [2, invalid])
            self.assertFalse(result["complete"])
            self.assertTrue(result["errors"])
            self.assertEqual(result["input_words"], [2, invalid])
        self.assertFalse(decode_diagnostics("unknown", [])['protocol_valid'])
        result = decode_diagnostics("flags_repeat", ["0x2", "0x0"])
        self.assertTrue(result["valid"])
        self.assertEqual(result["kind"], "flags")
        self.assertTrue(decode_diagnostics("settings_repeat", settings_words([0] * 6))["valid"])
        self.assertTrue(decode_diagnostics("trip_repeat", trip_words())["valid"])

    def test_flag_cause_bits_are_independent(self):
        expected = ["EXTERNAL", "OVERVOLTAGE", "UNDERVOLTAGE", "PHASE_UNBALANCE",
                    "PHASE_LOSS", "PHASE_REVERSAL", "RAM_ROM_FAILURE"]
        for bit, active in enumerate(expected):
            result = decode_diagnostics("flags", [2, (1 << (16 + bit)) << 1])
            self.assertTrue(result["valid"])
            for name in expected:
                self.assertEqual(result["readings"]["FLAGS_" + name]["value"], name == active)
            self.assertFalse(result["readings"]["FLAGS_TRIP"]["value"])
            self.assertFalse(result["readings"]["FLAGS_ALARM"]["value"])
        result = decode_diagnostics("flags", [2, 0x101 << 1])
        self.assertTrue(result["readings"]["FLAGS_TRIP"]["value"])
        self.assertTrue(result["readings"]["FLAGS_ALARM"]["value"])

    def test_invalid_flag_bytes_do_not_become_true(self):
        for payload, invalid in ((2, "FLAGS_TRIP"), (0x200, "FLAGS_ALARM")):
            result = decode_diagnostics("flags", [2, payload << 1])
            self.assertTrue(result["protocol_valid"])
            self.assertFalse(result["valid"])
            self.assertIsNone(result["readings"][invalid]["value"])
            self.assertTrue(result["readings"]["FLAGS_EXTERNAL"]["valid"])

    def test_reserved_bits_preserved_without_false_causes(self):
        result = decode_diagnostics("flags", [0xA51201 << 1, 0x800000 << 1])
        self.assertTrue(result["valid"])
        self.assertEqual(result["header"]["raw_payload"], 0xA51201)
        self.assertEqual(result["flags"]["reserved_cause_bits"], 0x80)
        self.assertEqual(len(result["warnings"]), 2)
        self.assertTrue(all(item["value"] is False for item in result["readings"].values()))

    def test_switch_byte_order_and_all_48_polarities(self):
        for bank in range(6):
            for bit in range(8):
                switches = [0] * 6
                switches[bank] = 1 << bit
                result = decode_diagnostics("settings", settings_words(switches, 0xA5, 0x5A))
                readings = result["readings"]
                self.assertEqual(readings["FIRMWARE_REVISION"]["value"], 0xA5)
                self.assertEqual(readings["FIRMWARE_VERSION"]["value"], 0x5A)
                for b in range(6):
                    self.assertEqual(readings[f"SW{b + 1}_RAW"]["value"], switches[b])
                    for n in range(8):
                        self.assertEqual(readings[f"SW{b + 1}_{n + 1}_ON"]["value"],
                                         not (b == bank and n == bit))

    def test_plus_ii_ct_table_including_3200_not_legacy_table(self):
        expected = [100, 150, 200, 250, 300, 400, 500, 600, 800, 1000,
                    1200, 1500, 1600, 2000, 2500, 3000, 3200, 4000, 5000]
        for code in range(32):
            result = decode_diagnostics("settings", settings_words([code, 0, 0, 0, 0, 0]))
            self.assertTrue(result["protocol_valid"])
            reading = result["readings"]["CONFIG_CT_PRIMARY_A"]
            self.assertEqual(reading["value"], expected[code] if code < 19 else None)
            self.assertEqual(reading["valid"], code < 19)
            self.assertTrue(result["readings"]["SW1_RAW"]["valid"])
            self.assertTrue(result["readings"]["CONFIG_PT_RATIO"]["valid"])

    def test_pt_table_international_override_and_nominal_voltages(self):
        expected = [1, 2, 4, 5, 20, 30, 35, 40, 55, 60, 70, 100, 120, 30, 60, 100]
        for code, ratio in enumerate(expected):
            readings = decode_diagnostics("settings", settings_words([0, 0, code, 6, 0, 0]))["readings"]
            self.assertEqual(readings["CONFIG_PT_RATIO"]["value"], ratio)
            self.assertEqual(readings["CONFIG_PT_INTERNATIONAL"]["value"], code >= 13)
            self.assertEqual(readings["CONFIG_NOMINAL_LL_V"]["value"], 110 if code >= 13 else 460)
            self.assertEqual(readings["CONFIG_NOMINAL_LN_V"]["value"], 64 if code >= 13 else 266)
        for code, pair in enumerate(((120, 69), (208, 120), (220, 127), (240, 138),
                                      (380, 219), (416, 240), (460, 266), (575, 332))):
            readings = decode_diagnostics("settings", settings_words([0, 0, 0, code, 0, 0]))["readings"]
            self.assertEqual((readings["CONFIG_NOMINAL_LL_V"]["value"],
                              readings["CONFIG_NOMINAL_LN_V"]["value"]), pair)

    def test_threshold_delay_pulse_and_demand_tables(self):
        for code in range(8):
            readings = decode_diagnostics("settings", settings_words(
                [0, 0, 0, 0, code | (code << 5), code | (code << 4)]))["readings"]
            self.assertEqual(readings["CONFIG_OVERVOLTAGE_PERCENT"]["value"], 105 + code * 5)
            self.assertEqual(readings["CONFIG_UNDERVOLTAGE_PERCENT"]["value"], 95 - code * 5)
            self.assertEqual(readings["CONFIG_UNBALANCE_PERCENT"]["value"], 5 + code * 5)
            self.assertEqual(readings["CONFIG_PROTECTION_DELAY_S"]["value"], 1 + code)
        for code, minutes in enumerate((5, 10, 15, 30)):
            readings = decode_diagnostics("settings", settings_words([0, 0, code << 4, 0, 0, 0]))["readings"]
            self.assertEqual(readings["CONFIG_DEMAND_MINUTES"]["value"], minutes)
            self.assertFalse(readings["CONFIG_DEMAND_MINUTES"]["internal_window_enabled"])
        pulse_wh = [100, 500, 1000, 5000, 10000, 50000, 100000, 500000,
                    1000000, 5000000, 10000000, 50000000, 100000000,
                    500000000, 500000000, 500000000]
        for code, energy in enumerate(pulse_wh):
            readings = decode_diagnostics("settings", settings_words([0, 0, 0, code << 4, 0, 0]))["readings"]
            self.assertEqual(readings["CONFIG_PULSE_Wh"]["value"], energy)

    def test_protection_responses_and_reverse_polarity_pf_setting(self):
        for offset, condition in enumerate(("OVERVOLTAGE", "UNDERVOLTAGE",
                                            "PHASE_LOSS_REVERSAL", "PHASE_UNBALANCE")):
            for code in range(4):
                readings = decode_diagnostics("settings", settings_words(
                    [0, code << (offset * 2), 0, 0, 0, 0]))["readings"]
                self.assertEqual(readings[f"CONFIG_{condition}_TRIP_ENABLED"]["value"], bool(code & 1))
                self.assertEqual(readings[f"CONFIG_{condition}_ALARM_ENABLED"]["value"], bool(code & 2))
        for sw5 in (0, 8):
            readings = decode_diagnostics("settings", settings_words([0, 0, 0, 0, sw5, 0]))["readings"]
            self.assertEqual(readings["CONFIG_ALTERNATE_PF"]["value"], bool(sw5 & 8))
            self.assertEqual(readings["SW5_4_ON"]["value"], not bool(sw5 & 8))

    def test_trip_order_scaling_and_reserved_word(self):
        words = trip_words()
        words[5] = 0xFFFFFF << 1  # reserved, not an invalid IMPACC measurement
        result = decode_diagnostics("trip", words)
        self.assertTrue(result["valid"])
        expected = {"IA": 1, "IB": 2, "IC": 3, "VAB": 5, "VBC": 6, "VCA": 7,
                    "VAN": 8, "VBN": 9, "VCN": 10, "P_W": 11, "DEMAND_W": 12,
                    "ENERGY_Wh": 13, "FREQUENCY_Hz": 14, "Q_var": 15, "PF": 16}
        for key, value in expected.items():
            reading = result["readings"]["TRIP_" + key]
            self.assertEqual(reading["value"], value)
            self.assertIsNone(reading["event_time"])
            self.assertEqual(reading["context"], "trip_buffer_not_live")
        self.assertEqual(result["reserved"][1]["index"], 5)
        self.assertEqual(result["reserved"][1]["raw_word"], 0xFFFFFF << 1)
        self.assertNotIn("TRIP_IX", result["readings"])
        self.assertTrue(result["readings"]["TRIP_DATA_VALID"]["value"])

    def test_no_trip_or_alarm_only_never_qualifies_event(self):
        for flags in (0, 0x100, 0x300100, 2):
            result = decode_diagnostics("trip", trip_words(flags))
            self.assertTrue(result["protocol_valid"])
            self.assertTrue(result["complete"])
            self.assertFalse(result["valid"])
            self.assertFalse(result["event_evidence"])
            self.assertIsNone(result["event_time"])
            for key in ("TRIP_IA", "TRIP_P_W", "TRIP_PF"):
                self.assertIsNone(result["readings"][key]["value"])
                self.assertIsNone(result["readings"][key]["value_exact"])
                self.assertFalse(result["readings"][key]["valid"])
                self.assertTrue(result["readings"][key]["scale_valid"])
            if flags == 2:
                self.assertIsNone(result["readings"]["TRIP_DATA_VALID"]["value"])
            else:
                self.assertFalse(result["readings"]["TRIP_DATA_VALID"]["value"])

    def test_trip_invalid_scalar_preserves_valid_siblings(self):
        words = trip_words()
        words[2] &= ~(0x400000 << 1)
        result = decode_diagnostics("trip", words)
        self.assertTrue(result["protocol_valid"])
        self.assertFalse(result["valid"])
        self.assertTrue(result["event_evidence"])
        self.assertFalse(result["readings"]["TRIP_DATA_VALID"]["value"])
        self.assertFalse(result["readings"]["TRIP_IA"]["valid"])
        self.assertIsNone(result["readings"]["TRIP_IA"]["value"])
        self.assertEqual(result["readings"]["TRIP_IB"]["value"], 2)
        self.assertTrue(result["readings"]["TRIP_IB"]["valid"])

    def test_physical_flags_and_settings(self):
        # Parent captured each request independently with24 MHz analyzer;
        # 0x400027 repeat handshakes were removed by acquisition provenance.
        flags = decode_diagnostics("flags_repeat", [0x2, 0x0])
        self.assertTrue(flags["valid"])
        self.assertTrue(all(reading["value"] is False for reading in flags["readings"].values()))
        settings = decode_diagnostics("settings_repeat", [0x100204, 0x1840022, 0xF6E65C])
        self.assertTrue(settings["valid"])
        expected = {"FIRMWARE_REVISION": 1, "FIRMWARE_VERSION": 8,
                    "SW1_RAW": 0x11, "SW2_RAW": 0, "SW3_RAW": 0xC2,
                    "SW4_RAW": 0x2E, "SW5_RAW": 0x73, "SW6_RAW": 0x7B,
                    "CONFIG_CT_PRIMARY_A": 4000, "CONFIG_CT_SECONDARY_A": 5,
                    "CONFIG_PT_RATIO": 4, "CONFIG_FREQUENCY_Hz": 60,
                    "CONFIG_WIRING_WIRES": 4, "CONFIG_DEMAND_MINUTES": 5,
                    "CONFIG_DEMAND_SYNC_ENABLED": False, "CONFIG_PROTECTION_ENABLED": False,
                    "CONFIG_TEST_MODE": False, "CONFIG_PULSE_Wh": 1000,
                    "CONFIG_NOMINAL_LL_V": 460, "CONFIG_NOMINAL_LN_V": 266,
                    "CONFIG_OVERVOLTAGE_PERCENT": 120, "CONFIG_UNDERVOLTAGE_PERCENT": 80,
                    "CONFIG_UNBALANCE_PERCENT": 20, "CONFIG_PROTECTION_DELAY_S": 8,
                    "CONFIG_DELAY_ENABLED": True, "CONFIG_ALTERNATE_PF": False,
                    "CONFIG_AUTO_RESET_ENABLED": False, "CONFIG_ENERGY_RESET_ENABLED": False}
        for key, value in expected.items():
            self.assertEqual(settings["readings"][key]["value"], value, key)

    def test_physical_trip_is_separate_from_current_flags(self):
        words = [0x22, 0x600202, 0x800044, 0x800050, 0x800048, 0x800000,
                 0x800020, 0x8000D0, 0x8000EE, 0x800088, 0x800076, 0x80008A,
                 0x1C7FFFE, 0x1C60000, 0xC60000, 0xFC2EE0, 0x1C7FFF8, 0x1FC003A]
        result = decode_diagnostics("trip_repeat", words)
        self.assertTrue(result["valid"])
        expected = {"TRIP_TRIP": True, "TRIP_ALARM": True,
                    "TRIP_PHASE_LOSS": True, "TRIP_PHASE_REVERSAL": True,
                    "TRIP_EXTERNAL": False, "TRIP_DATA_VALID": True,
                    "TRIP_IA": 34, "TRIP_IB": 40, "TRIP_IC": 36,
                    "TRIP_VAB": 16, "TRIP_VBC": 104, "TRIP_VCA": 119,
                    "TRIP_VAN": 68, "TRIP_VBN": 59, "TRIP_VCN": 69,
                    "TRIP_P_W": -1000, "TRIP_DEMAND_W": 0, "TRIP_ENERGY_Wh": 0,
                    "TRIP_FREQUENCY_Hz": 60, "TRIP_Q_var": -4000, "TRIP_PF": .29}
        for key, value in expected.items():
            self.assertEqual(result["readings"][key]["value"], value, key)
        self.assertIsNone(result["event_time"])
        self.assertFalse(result["retention_verified"])
        self.assertEqual(result["measurement_context"], "trip_buffer_not_live")
        self.assertNotIn("observed_utc", result)

    def test_specs_cover_every_key_with_unique_bacnet_identifiers(self):
        self.assertEqual(len({s["key"] for s in FIELD_SPECS}), len(FIELD_SPECS))
        self.assertEqual(len({s["name"] for s in FIELD_SPECS}), len(FIELD_SPECS))
        self.assertEqual(len({(s["type"], s["instance"]) for s in FIELD_SPECS}), len(FIELD_SPECS))
        for group, words in (("flags", [2, 0]), ("settings", settings_words([0] * 6)),
                              ("trip", trip_words())):
            result = decode_diagnostics(group, words)
            self.assertEqual(set(result["readings"]), {s["key"] for s in FIELD_SPECS if s["group"] == group})
            json.dumps(result, allow_nan=False)
        for spec in FIELD_SPECS:
            self.assertTrue(spec["name"].startswith("IQData-"))
            self.assertIn(spec["type"], ("analog", "binary"))
            self.assertTrue(spec["units"])
            if spec["type"] == "analog":
                self.assertGreaterEqual(spec["instance"], 100)
            else:
                self.assertGreaterEqual(spec["instance"], 10)


if __name__ == "__main__":
    unittest.main()
