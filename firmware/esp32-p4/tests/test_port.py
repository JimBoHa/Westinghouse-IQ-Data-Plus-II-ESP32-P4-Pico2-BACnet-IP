"""Native ASan/UBSan parity checks; synthetic vectors are not meter evidence."""
import json
import math
from pathlib import Path
import random
import subprocess
import sys
import unittest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "live"))
from decode_meter import decode_impacc, decode_request
from decode_diagnostics import decode_diagnostics
from derived_power import add_derived
from live_diagnostics import DiagnosticAccumulator, FIELD_SPECS
from test_decode_diagnostics import settings_words, trip_words
from test_live_diagnostics import document

ORACLE = Path(sys.argv.pop(1)) if len(sys.argv) > 1 and not sys.argv[1].startswith("-") else ROOT / "build/p4-native/iq_oracle"


class PortTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.proc = subprocess.Popen([str(ORACLE)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)

    @classmethod
    def tearDownClass(cls):
        cls.proc.stdin.close()
        if cls.proc.wait(timeout=10):
            raise AssertionError("Native oracle exited with sanitizer or application error")
        cls.proc.stdout.close()

    def call(self, op, **fields):
        self.proc.stdin.write(json.dumps(dict(op=op, **fields)) + "\n")
        self.proc.stdin.flush()
        response = self.proc.stdout.readline()
        self.assertTrue(response, "Native oracle terminated unexpectedly")
        return json.loads(response)

    def setUp(self):
        self.call("reset")

    def compare(self, actual, expected):
        for key, ref in expected.items():
            with self.subTest(key=key):
                self.assertIn(key, actual)
                valid = bool(ref["valid"] and not ref.get("stale", False))
                self.assertEqual(actual[key]["valid"], valid)
                if valid:
                    self.assertTrue(math.isclose(actual[key]["value"], ref["value"], rel_tol=1e-10, abs_tol=1e-8), (key, actual[key], ref))

    def decode(self, kind, words):
        ref = add_derived(decode_request(kind, words)) if kind == "all_standard" else decode_diagnostics(kind, words)
        got = self.call("decode", kind=kind, words=words)
        self.assertEqual(got["ok"], ref["protocol_valid"])
        self.compare(got["readings"], ref["readings"])

    def test_impacc_all_scales_and_boundary_mantissas(self):
        for scale in range(256):
            for mantissa in (0, 1, 0x7fff, 0x8000, 0xffff):
                payload = scale << 16 | mantissa
                expected = decode_impacc(payload)
                got = self.call("impacc", payload=payload)
                self.assertEqual(got["ok"], expected["valid"])
                if got["ok"]:
                    self.assertTrue(math.isclose(got["value"], expected["value"], rel_tol=1e-12, abs_tol=1e-15))

    def test_standard_maps_partial_fields_and_invalid_frames(self):
        for bitmap in range(64):
            payloads = [bitmap << 5 | 9]
            for bit, count in enumerate((4, 3, 3, 3, 3, 1)):
                if bitmap & (1 << bit):
                    payloads.extend(0x400000 | n for n in range(1, count + 1))
            self.decode("all_standard", [p << 1 for p in payloads])
        rng = random.Random(75201)
        for _ in range(100):
            self.decode("all_standard", [0x7e9 << 1] + [rng.randrange(1 << 24) << 1 for _ in range(17)])
        for words in ([], [0x1000], [0, 0], [0x800000], [0x2000000], [1]):
            self.decode("all_standard", words)

    def test_all_switch_polarities_and_configuration_tables(self):
        for bank in range(6):
            for byte in range(256):
                switches = [0] * 6
                switches[bank] = byte
                self.decode("settings", settings_words(switches, 0xa5, 0x5a))

    def test_flags_trip_qualification_and_individual_quality(self):
        for payload in (0, 1, 2, 0x100, 0x101, 0x200, 0xffffff, *[1 << n for n in range(16, 24)]):
            self.decode("flags", [2, payload << 1])
            self.decode("trip", trip_words(payload))
        for index in range(18):
            words = trip_words()
            words[index] |= 1
            self.decode("trip", words)
        for index in range(2, 18):
            words = trip_words()
            words[index] &= ~(0x400000 << 1)
            self.decode("trip", words)

    @staticmethod
    def standard(seconds=0):
        # Exact integer encoding and 5-second cadence permit Python/C parity.
        payloads = [0x7e9, 0x400064, 0x40006e, 0x40005a, 0,
                    0x4001e0, 0x4001ea, 0x4001f4, 0x40010a, 0x40010a, 0x40010a,
                    0x620000 | (1000 + seconds), 0x400000, 0x400000,
                    0x40003c, 0x400000, 0xfeffa1, 1000 + seconds]
        return [p << 1 for p in payloads]

    def test_rolling_math_warmup_gap_and_recovery_against_python(self):
        self.call("accept", kind="settings", words=settings_words([0, 0, 0, 6, 0, 0]), now=1000)
        acc = DiagnosticAccumulator(nominal_vll=460, nominal_hz=60)
        for seconds in list(range(0, 906, 5)) + [920, 925, 930]:
            words = self.standard(seconds)
            decoded = decode_request("all_standard", words)
            sample = document(seconds, **{key: value["value"] for key, value in decoded["readings"].items()})
            expected = acc.update(sample)
            actual = self.call("accept", kind="all_standard", words=words, now=1000 + seconds * 1000)
            self.assertTrue(actual["ok"])
            self.compare(actual["readings"], expected)

    def test_freshness_failures_and_settings_expiration(self):
        self.call("accept", kind="settings", words=settings_words([0, 0, 0, 6, 0, 0]), now=1000)
        self.call("accept", kind="all_standard", words=self.standard(), now=1000)
        valid = self.call("snapshot", now=6000)["readings"]
        self.assertTrue(valid["VAB"]["valid"])
        stale = self.call("snapshot", now=6001)["readings"]
        self.assertFalse(stale["VAB"]["valid"])
        self.assertTrue(stale["age_seconds"]["valid"])
        self.call("fail", kind="all_standard", stop=8, now=7000)
        recovered = self.call("accept", kind="all_standard", words=self.standard(10), now=11000)["readings"]
        self.assertTrue(recovered["VAB"]["valid"])
        self.assertFalse(recovered["ROLLING_15MIN_DEMAND_kW"]["valid"])
        expired = self.call("accept", kind="all_standard", words=self.standard(10), now=3601001)["readings"]
        self.assertFalse(expired["NOMINAL_VOLTAGE_V"]["valid"])
        self.assertFalse(expired["FREQUENCY_EXCURSION_ACTIVE"]["valid"])

    @staticmethod
    def stream_records():
        payload = 3 | 0xc0 | (8 << 20)
        records = [dict(type="started", command="transact", request_kind="flags_repeat", payload=payload, duration_ms=500, shift_edge="rising")]
        time = 0
        def event(name, origin, clocks, word, **fields):
            nonlocal time
            time += 100
            records.append(dict(type="event", event=name, us=time, origin_code=origin, clocks=clocks, word=word, **fields))
        for index, words in enumerate(([0x400027], [2, 0])):
            if index:
                time += 20000
            event("request_presented", 1, 27, 5 | payload << 3)
            event("read_poll", 1, 27, 5 | payload << 3)
            for word in words:
                event("meter_write_candidate", 3, 25, word, data_released_during_write=True)
                event("completion_presented", 2, 1, 0)
                event("read_poll", 2, 1, 0)
        records.append(dict(type="result", stop_code=0, malformed_writes=0, released=True, events=len(records)-1, requests=2, completions=3, valid_write_lengths=3))
        return records

    def stream(self, records, **fields):
        text = "".join(json.dumps(r) + "\n" for r in records)
        return self.call("stream", kind="flags", text=text, **fields)

    def test_usb_fragmentation_provenance_and_terminal_status(self):
        records = self.stream_records()
        for chunk in (1, 2, 3, 7, 31, 63, 64, 127, 512, 4096):
            got = self.stream(records, chunk=chunk)
            self.assertTrue(got["ok"], got)
            self.assertEqual(got["words"], [2, 0])
        for index, key, value in ((0, "payload", 3), (0, "duration_ms", 1000), (1, "word", 0), (3, "data_released_during_write", False), (4, "clocks", 27), (6, "us", 600), (-1, "events", 999), (-1, "released", False), (-1, "stop_code", 8)):
            bad = [dict(r) for r in records]
            bad[index][key] = value
            self.assertFalse(self.stream(bad)["ok"], (index, key))
        self.assertFalse(self.stream(records[:-1])["ok"])
        got = self.stream(records, transport_error="disconnect after terminal")
        self.assertFalse(got["ok"])
        self.assertEqual(got["stop"], 0)
        self.assertEqual(got["error"], "disconnect after terminal")

    def test_commissioning_rejects_protected_or_malformed_configuration(self):
        base = dict(device_instance=75201, name="IQData-Test", dhcp=True, poll_enabled=False)
        self.assertTrue(self.call("config", text=json.dumps(base))["ok"])
        for fields in (dict(device_instance=75151), dict(device_instance=4194303), dict(device_instance=True), dict(name=""), dict(unknown=1), dict(dhcp="true"), dict(meter_address=4096), dict(dhcp=False, ip="192.168.75.151", mask="255.255.255.0", gateway="192.168.75.1"), dict(dhcp=False, ip="192.168.75.0", mask="255.255.255.0", gateway="192.168.75.1")):
            self.assertFalse(self.call("config", text=json.dumps(base | fields))["ok"], fields)
        self.assertFalse(self.call("config", text='{"device_instance":75201,"device_instance":75151,"name":"Test"}')["ok"])


if __name__ == "__main__":
    unittest.main()
