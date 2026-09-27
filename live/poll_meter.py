#!/usr/bin/env python3
"""Sequential, bounded IQ Data Plus II reads with preserved USB evidence.

Default: one cycle. --continuous explicitly opts into recurring acquisition.
This module does not open a serial port when imported or run with --self-test.
"""
import argparse
import base64
import datetime as dt
import fcntl
import json
import math
import sys
import time
import uuid
from pathlib import Path
from types import SimpleNamespace

import host

DIAGNOSTIC_KINDS = ("flags", "settings", "trip")
KINDS = ("fast_status", "line_voltages", "currents", "power1", "power2", "energy", "all_standard") + DIAGNOSTIC_KINDS
HANDSHAKE = 0x400027
MAX_JOURNAL_BYTES = 16 * 1024 * 1024


def integer(value, name, low=0, high=0xffffffff):
    if type(value) is not int or not low <= value <= high:
        raise ValueError(f"Invalid {name}: {value!r}")
    return value


def payload_for(kind, address):
    if kind in DIAGNOSTIC_KINDS:
        return 3 | (12 << 4) | (address << 8) | ({"flags": 8, "settings": 9, "trip": 10}[kind] << 20)
    return 3 | (address << 8) | ({"fast_status": 0, "line_voltages": 6,
                                  "currents": 5, "power1": 8, "power2": 9,
                                  "energy": 10, "all_standard": 3}[kind] << 20)


def journal_messages(path):
    if path.stat().st_size > MAX_JOURNAL_BYTES:
        raise ValueError("USB journal exceeds 16 MiB")
    messages = []
    for number, line in enumerate(path.read_text().splitlines(), 1):
        record = host.json_object(line)
        if record.get("direction") != "rx" or record.get("phase") not in {"command", "command_tail"}:
            continue
        if record.get("partial"):
            raise ValueError("Partial USB record in command journal")
        raw = base64.b64decode(record["raw_base64"], validate=True)
        text = raw.decode("utf-8")
        if text != record.get("text"):
            raise ValueError("Journal text differs from preserved USB bytes")
        messages.append((host.json_object(text), f"{path}#L{number}"))
    return messages


def extract_meter_words(report, messages, kind, address, edge, duration_ms):
    """Reject failed or ambiguous trials; never decode presented request images."""
    request_kind = kind + "_repeat"
    expected_command = f"transact {request_kind} {address} {edge} {duration_ms}"
    if (report.get("command") != expected_command or report.get("status") != "completed"
            or report.get("transport_complete") is not True
            or report.get("started_ack_received") is not True or report.get("errors")):
        raise ValueError("USB transaction did not complete cleanly for requested command")
    result = report.get("result", {})
    if (result.get("type") != "result" or result.get("stop_code") != 0
            or result.get("malformed_writes") != 0 or result.get("released") is not True):
        raise ValueError("Firmware stopped with a fault, malformed frame, or unreleased outputs")
    starts = [m for m, _ in messages if m.get("type") == "started"]
    terminals = [m for m, _ in messages if m.get("type") in {"result", "error"}]
    if len(starts) != 1 or terminals != [result]:
        raise ValueError("Journal lacks a unique matching start/result boundary")
    start = starts[0]
    if (start.get("command") != "transact" or start.get("request_kind") != request_kind
            or start.get("payload") != payload_for(kind, address)
            or start.get("duration_ms") != duration_ms or start.get("shift_edge") != edge):
        raise ValueError("Firmware start record does not match requested read")
    expected_image = 5 | (payload_for(kind, address) << 3)
    requests = completed_requests = completions = completed_completions = writes = event_count = 0
    handshake = False
    handshake_completed = False
    completion_clock_us = None
    active_image = None
    words, references = [], []
    last_us = -1
    started = ended = False
    for message, reference in messages:
        message_type = message.get("type")
        if message_type == "started":
            started = True
            continue
        if message_type == "result":
            ended = True
            continue
        if message_type != "event":
            if message_type != "timing":
                raise ValueError(f"Unexpected command record type: {message_type!r}")
            continue
        if not started or ended:
            raise ValueError("Event outside command start/result boundary")
        event_count += 1
        at = integer(message.get("us"), "event time", 0, duration_ms * 1000 + 100000)
        if at < last_us:
            raise ValueError("Nonmonotonic firmware event timestamps")
        last_us = at
        event = message.get("event")
        origin = message.get("origin_code")
        clocks = integer(message.get("clocks"), "clock count")
        word = integer(message.get("word"), "raw word")
        if event == "request_presented":
            requests += 1
            if (requests > 2 or active_image is not None or origin != 1
                    or clocks != 27 or word != expected_image):
                raise ValueError("Unexpected request image or request count")
            if requests == 2 and (not handshake_completed or words
                                 or at - completion_clock_us < 20000):
                raise ValueError("Repeat lacks matching control reply, completion clock, and backoff")
            active_image = (1, word, 27)
        elif event == "completion_presented":
            if (origin != 2 or clocks != 1 or word != 0 or active_image is not None
                    or completions >= writes):
                raise ValueError("Unexpected completion image")
            completions += 1
            active_image = (2, 0, 1)
        elif event == "read_poll":
            if active_image != (origin, word, clocks):
                raise ValueError("Completed image has no matching presentation")
            active_image = None
            if origin == 1:
                completed_requests += 1
            elif origin == 2:
                completed_completions += 1
                if handshake and not handshake_completed:
                    handshake_completed = True
                    completion_clock_us = at
        elif event == "clock_only_fragment":
            if origin != 2 or active_image != (2, 0, 1):
                raise ValueError("Request or unowned read fragment")
            active_image = None
        elif event == "empty_write":
            if origin != 3 or clocks != 0 or word != 0:
                raise ValueError("Malformed empty write")
        elif event == "meter_write_candidate":
            if (origin != 3 or clocks != 25 or word > 0x1ffffff
                    or message.get("data_released_during_write") is not True
                    or completed_requests != requests or not requests):
                raise ValueError("Meter write lacks complete request/ownership provenance")
            writes += 1
            if word & 1:
                if word != HANDSHAKE or handshake or words or requests != 1:
                    raise ValueError(f"Unexpected control reply: 0x{word:07x}")
                handshake = True
            else:
                if handshake and requests != 2:
                    raise ValueError("DATA appeared before expected repeat request")
                words.append(word)
                references.append(reference)
        else:
            raise ValueError(f"Unexpected/fault event: {event!r}")
    if (event_count != result.get("events") or requests != result.get("requests")
            or completed_requests != requests or completions != result.get("completions")
            or writes != result.get("valid_write_lengths") or completions != writes
            or completed_completions != completions or active_image is not None or not words):
        raise ValueError("Incomplete response or inconsistent firmware event totals")
    return words, references


def finite_tree(value):
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("Decoded value is not finite")
    if isinstance(value, dict):
        for item in value.values():
            finite_tree(item)
    elif isinstance(value, list):
        for item in value:
            finite_tree(item)


def stamp_seconds(stamp):
    parsed = dt.datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("Timestamp lacks timezone")
    return parsed.timestamp()


def make_state(buffers, kinds, interval, last_poll, now=None):
    now = now or host.utc()
    current_seconds = stamp_seconds(now)
    freshness = max(30.0, interval * 3)
    readings, status, references, good_times, errors = {}, None, [], [], {}
    stale = False
    protocol_valid = True
    for kind in kinds:
        buffer = buffers.get(kind, {})
        good = buffer.get("last_good")
        error = buffer.get("last_error")
        if error:
            errors[kind] = error
        if not good:
            stale = True
            protocol_valid = False
            continue
        age = current_seconds - stamp_seconds(good["observed_utc"])
        buffer_stale = bool(error) or age < 0 or age > freshness
        stale |= buffer_stale
        good_times.append(good["observed_utc"])
        references.extend(good["evidence_refs"])
        for name, reading in good["decoded"]["readings"].items():
            readings[name] = {**reading, "source": reading.get("source", "meter_write"), "display_verified": False,
                              "observed_utc": good["observed_utc"], "stale": buffer_stale,
                              "evidence_refs": good["evidence_refs"]}
        if good["decoded"].get("status") is not None:
            status = {**good["decoded"]["status"], "observed_utc": good["observed_utc"],
                      "stale": buffer_stale, "evidence_refs": good["evidence_refs"]}
    state = {"schema_version": 1, "source": "meter_write", "protocol_valid": protocol_valid,
             "display_verified": False, "telemetry_validated": False,
             "capture_verified": False, "updated_utc": now,
             "observed_utc": min(good_times, key=stamp_seconds) if good_times else None,
             "stale_after_seconds": freshness, "stale": stale,
             "quality": "stale_or_failed" if stale else "decoded_unverified",
             "readings": readings, "status": status,
             "evidence_refs": list(dict.fromkeys(references)), "buffers": buffers,
             "requested_kinds": list(kinds), "last_poll": last_poll, "last_poll_errors": errors,
             "interpretation": "Decoded meter-write words; no per-poll external capture or display verification."}
    finite_tree(state)
    return state


def poll_once(args, kind, buffers, execute=None, decoder=None):
    execute = execute or host.execute
    if decoder is None:
        if kind in DIAGNOSTIC_KINDS:
            from decode_diagnostics import decode_diagnostics
            decoder = decode_diagnostics
        else:
            from decode_meter import decode_request
            decoder = decode_request
    token = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%S%fZ") + "_" + uuid.uuid4().hex[:12]
    output = args.raw_dir / f"poll_{kind}_{token}.json"
    started = host.utc()
    attempt = {"kind": kind, "started_utc": started, "finished_utc": None,
               "ok": False, "errors": [], "evidence_refs": []}
    try:
        invocation = SimpleNamespace(action="transact", parameters=[kind + "_repeat", str(args.address),
                                    args.edge, str(args.duration_ms)], port=args.port,
                                    output=output, state=args.state)
        code = execute(invocation)
        report, error = host.saved_json(output)
        if error:
            raise ValueError(f"Cannot read preserved trial summary: {error}")
        journal = output.with_name(output.name + ".jsonl")
        attempt["evidence_refs"] = [str(output), str(journal)]
        if code != 0:
            raise ValueError(f"USB host failed: {report.get('errors', [])}")
        result = report.get("result")
        if not isinstance(result, dict):
            raise ValueError("USB host completed without a result object")
        attempt["transport"] = {key: result.get(key) for key in (
            "stop_code", "elapsed_us", "malformed_writes", "requests", "completions", "valid_write_lengths")}
        words, data_refs = extract_meter_words(report, journal_messages(journal), kind,
                                              args.address, args.edge, args.duration_ms)
        decoded = decoder(kind, words)
        if kind not in DIAGNOSTIC_KINDS:
            from derived_power import add_derived
            decoded = add_derived(decoded)
        finite_tree(decoded)
        accepted = decoded.get("protocol_valid") if kind == "all_standard" or kind in DIAGNOSTIC_KINDS else decoded.get("valid")
        if accepted is not True or decoded.get("complete") is not True:
            raise ValueError(f"Invalid/incomplete decoded response: {decoded.get('errors', [])}")
        if not isinstance(decoded.get("readings"), dict):
            raise ValueError("Decoder returned no readings mapping")
        observed = report["finished_utc"]
        stamp_seconds(observed)
        buffers[kind] = {"last_good": {"observed_utc": observed, "decoded": decoded,
                         "acquisition_started_utc": report["started_utc"],
                         "acquisition_finished_utc": observed,
                         "evidence_refs": [str(output), str(journal)] + data_refs},
                         "last_error": None, "last_attempt_utc": observed}
        attempt["ok"] = True
    except (Exception, KeyboardInterrupt) as error:
        error_text = f"{type(error).__name__}: {error}"
        attempt["errors"].append(error_text)
        attempt["evidence_refs"] = [str(p) for p in (output, output.with_name(output.name + ".jsonl")) if p.exists()]
        previous = buffers.setdefault(kind, {})
        previous["last_error"] = error_text
        previous["last_attempt_utc"] = host.utc()
        if isinstance(error, KeyboardInterrupt):
            attempt["interrupted"] = True
    attempt["finished_utc"] = host.utc()
    attempt["duration_seconds"] = max(0, stamp_seconds(attempt["finished_utc"]) - stamp_seconds(started))
    if kind in DIAGNOSTIC_KINDS:
        host.atomic_json(args.state / "meter_diagnostics.json", make_diagnostic_state(buffers, attempt))
    else:
        host.atomic_json(args.state / "decoded_readings.json",
                         make_state(buffers, args.kinds, args.interval, attempt))
    return attempt


def make_diagnostic_state(buffers, attempt):
    groups, readings = {}, {}
    for kind in DIAGNOSTIC_KINDS:
        buffer = buffers.get(kind, {})
        good = buffer.get("last_good")
        groups[kind] = buffer
        if not good:
            continue
        for key, value in good["decoded"]["readings"].items():
            readings[key] = {**value, "observed_utc": good["observed_utc"],
                             "source": "meter_write", "group": kind,
                             "stale": bool(buffer.get("last_error"))}
    return {"schema_version": 1, "source": "meter_write", "updated_utc": host.utc(),
            "readings": readings, "buffers": groups, "last_poll": attempt,
            "interpretation": "Read-only diagnostic buffers; trip data has no meter event timestamp."}


class DiagnosticSchedule:
    """At most one diagnostic slot between normal measurement slots."""
    periods = {"flags": 10.0, "settings": 300.0, "trip": 60.0}

    def __init__(self):
        self.due = {kind: 0.0 for kind in DIAGNOSTIC_KINDS}
        self.last_flags = None
        self.failures = {kind: 0 for kind in DIAGNOSTIC_KINDS}

    def next(self, now):
        due = [kind for kind in DIAGNOSTIC_KINDS if self.due[kind] <= now]
        return min(due, key=lambda k: self.due[k]) if due else None

    def completed(self, kind, attempt, buffers, now):
        self.failures[kind] = 0 if attempt["ok"] else self.failures[kind] + 1
        self.due[kind] = now + max(self.periods[kind], min(300, 2 ** min(self.failures[kind], 9)))
        if kind == "flags" and attempt["ok"]:
            values = buffers[kind]["last_good"]["decoded"]["readings"]
            flags = tuple((key, item.get("value")) for key, item in sorted(values.items()))
            if self.last_flags is not None and flags != self.last_flags:
                self.due["trip"] = 0.0
            self.last_flags = flags


def update_health(health, attempt):
    health.setdefault("started_utc", attempt["started_utc"])
    health["updated_utc"] = attempt["finished_utc"]
    key = "diagnostics" if attempt["kind"] in DIAGNOSTIC_KINDS else "measurements"
    group = health.setdefault(key, {"attempts": 0, "failures": 0, "malformed_writes": 0})
    group["attempts"] += 1
    group["failures"] += int(not attempt["ok"])
    group["consecutive_failures"] = 0 if attempt["ok"] else group.get("consecutive_failures", 0) + 1
    group["last_duration_seconds"] = attempt.get("duration_seconds", 0)
    group["last_attempt_utc"] = attempt["finished_utc"]
    group["last_ok"] = attempt["ok"]
    if attempt["ok"]:
        group["last_success_utc"] = attempt["finished_utc"]
    malformed = attempt.get("transport", {}).get("malformed_writes")
    if isinstance(malformed, int) and malformed >= 0:
        group["malformed_writes"] += malformed
    group["last_stop_code"] = attempt.get("transport", {}).get("stop_code")
    group["failure_percent"] = 100 * group["failures"] / group["attempts"]
    return health


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", help="Local USB serial path; prefer /dev/serial/by-id/...")
    parser.add_argument("--state", type=Path, help="Directory exposed by read-only host HTTP server")
    parser.add_argument("--raw-dir", type=Path, help="Fresh per-request journals; defaults to STATE/polls")
    parser.add_argument("--kinds", choices=KINDS, nargs="+", default=["line_voltages", "currents"])
    parser.add_argument("--address", type=int, default=0)
    parser.add_argument("--edge", choices=["rising", "falling"], default="rising")
    parser.add_argument("--duration-ms", type=int, default=1000)
    parser.add_argument("--interval", type=float, default=1.05, help="Minimum seconds between poll starts; at least 1")
    parser.add_argument("--database", type=Path, help="Durable SQLite history, committed after every attempt")
    parser.add_argument("--diagnostics", action="store_true", help="Interleave read-only diagnostic slots; no diagnostic database rows")
    parser.add_argument("--recover", action="store_true", help="Continuous recording: retain failures and back off up to 60 seconds")
    runs = parser.add_mutually_exclusive_group()
    runs.add_argument("--cycles", type=int, default=1, help="Finite cycles (default1)")
    runs.add_argument("--continuous", action="store_true", help="Explicitly run until interrupted or a failed poll")
    parser.add_argument("--self-test", action="store_true", help="Offline tests; never opens USB")
    args = parser.parse_args()
    if args.self_test:
        return self_test()
    if not args.port or not args.state or "://" in args.port:
        parser.error("--port must be a local serial path and --state is required")
    if args.recover and (not args.continuous or not args.database):
        parser.error("--recover requires --continuous and durable --database recording")
    if not math.isfinite(args.interval) or not 1 <= args.interval <= 1200:
        parser.error("--interval must be finite and between 1 and 1200 seconds")
    if not 0 <= args.address <= 4095 or not 50 <= args.duration_ms <= 5000 or args.cycles < 1:
        parser.error("address0..4095, duration50..5000ms, and cycles>=1 required")
    args.kinds = list(dict.fromkeys(args.kinds))
    if args.database and any(kind in DIAGNOSTIC_KINDS for kind in args.kinds):
        parser.error("Diagnostic reads are current state only; omit --database or use --diagnostics with all_standard")
    if args.diagnostics and args.kinds != ["all_standard"]:
        parser.error("--diagnostics requires --kinds all_standard")
    for kind in args.kinds:
        host.make_command("transact", [kind + "_repeat", str(args.address), args.edge, str(args.duration_ms)])
    # Resolve imports/configuration before opening a serial port.
    from decode_meter import decode_request
    args.state = args.state.resolve()
    args.raw_dir = (args.raw_dir or args.state / "polls").resolve()
    args.state.mkdir(parents=True, exist_ok=True)
    args.raw_dir.mkdir(parents=True, exist_ok=True)
    with (args.state / ".poll_meter.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            parser.exit(2, "Another poller owns this state directory\n")
        saved = args.state / "decoded_readings.json"
        buffers = {}
        if saved.exists():
            document, error = host.saved_json(saved)
            if error or document.get("source") != "meter_write" or not isinstance(document.get("buffers"), dict):
                parser.exit(2, "Existing decoded state is unreadable or incompatible; preserved unchanged\n")
            buffers = document["buffers"]
        store = None
        if args.database:
            from store_readings import ReadingStore
            store = ReadingStore(args.database.resolve())
        diag_buffers = {}
        diagnostic_path = args.state / "meter_diagnostics.json"
        if diagnostic_path.exists():
            previous, error = host.saved_json(diagnostic_path)
            if not error and isinstance(previous.get("buffers"), dict):
                diag_buffers = previous["buffers"]
        health, _ = host.saved_json(args.state / "poll_health.json")
        health = health if isinstance(health, dict) else {}
        schedule = DiagnosticSchedule() if args.diagnostics else None
        cycle = 0
        consecutive_failures = 0
        next_poll = time.monotonic()
        try:
            while args.continuous or cycle < args.cycles:
                for position, kind in enumerate(args.kinds):
                    delay = next_poll - time.monotonic()
                    if delay > 0:
                        time.sleep(delay)
                    poll_began = time.monotonic()
                    next_poll = poll_began + args.interval
                    attempt = poll_once(args, kind, diag_buffers if kind in DIAGNOSTIC_KINDS else buffers)
                    if store:
                        store.record_attempt(attempt, buffers.get(kind))
                    host.atomic_json(args.state / "poll_health.json", update_health(health, attempt))
                    print(json.dumps(attempt, allow_nan=False), flush=True)
                    if attempt.get("interrupted"):
                        return 130
                    # Finite experiments stop on failure. Explicit production
                    # recording preserves failures and backs off between reads.
                    if not attempt["ok"]:
                        if not args.recover:
                            return 1
                        consecutive_failures += 1
                        next_poll = time.monotonic() + max(args.interval, min(60, 2 ** min(consecutive_failures, 6)))
                    else:
                        consecutive_failures = 0
                    # Extra reads use separate slots and files. They neither
                    # enter the measurements database nor speed up requests.
                    diagnostic = schedule.next(time.monotonic()) if schedule and attempt["ok"] else None
                    if diagnostic:
                        delay = next_poll - time.monotonic()
                        if delay > 0:
                            time.sleep(delay)
                        diag_began = time.monotonic()
                        next_poll = diag_began + args.interval
                        extra = poll_once(args, diagnostic, diag_buffers)
                        schedule.completed(diagnostic, extra, diag_buffers, time.monotonic())
                        host.atomic_json(args.state / "poll_health.json", update_health(health, extra))
                        print(json.dumps(extra, allow_nan=False), flush=True)
                        if extra.get("interrupted"):
                            return 130
                cycle += 1
        except KeyboardInterrupt:
            return 130
        finally:
            if store:
                store.close()
    return 0


def self_test():
    import copy
    import unittest

    class Checks(unittest.TestCase):
        def fixture(self):
            def event(name, us, clocks, word, origin):
                return {"type": "event", "event": name, "us": us, "clocks": clocks,
                        "word": word, "origin_code": origin,
                        "data_released_during_write": True if origin == 3 else None}
            events = [event("request_presented", 20000, 27, 29, 1),
                      event("read_poll", 20110, 27, 29, 1),
                      event("meter_write_candidate", 20300, 25, HANDSHAKE, 3),
                      event("completion_presented", 20330, 1, 0, 2),
                      event("read_poll", 20340, 1, 0, 2),
                      event("request_presented", 40340, 27, 29, 1),
                      event("read_poll", 40450, 27, 29, 1),
                      event("meter_write_candidate", 40600, 25, 0xB01482, 3),
                      event("completion_presented", 40630, 1, 0, 2),
                      event("read_poll", 40640, 1, 0, 2)]
            result = {"type": "result", "stop_code": 0, "malformed_writes": 0,
                      "released": True, "events": len(events), "requests": 2,
                      "completions": 2, "valid_write_lengths": 2}
            start = {"type": "started", "command": "transact", "request_kind": "fast_status_repeat",
                     "payload": 3, "duration_ms": 1000, "shift_edge": "rising"}
            messages = [(m, f"synthetic.jsonl#L{i}") for i, m in enumerate([start] + events + [result])]
            report = {"command": "transact fast_status_repeat 0 rising 1000", "status": "completed",
                      "transport_complete": True, "started_ack_received": True, "errors": [], "result": result}
            return report, messages

        def parse(self, report, messages):
            return extract_meter_words(report, messages, "fast_status", 0, "rising", 1000)

        def test_data_only_and_handshake(self):
            report, messages = self.fixture()
            self.assertEqual(self.parse(report, messages)[0], [0xB01482])

        def test_bad_stop_control_and_provenance(self):
            for index, field, value in [(3, "word", 0x200027), (8, "origin_code", 1),
                                        (8, "clocks", 24), (6, "us", 40339)]:
                report, messages = copy.deepcopy(self.fixture())
                messages[index][0][field] = value
                with self.assertRaises(ValueError):
                    self.parse(report, messages)
            report, messages = self.fixture()
            report["result"]["stop_code"] = 1
            with self.assertRaises(ValueError):
                self.parse(report, messages)

        def test_failed_poll_retains_last_good_as_stale(self):
            now = "2026-01-01T00:00:00+00:00"
            buffers = {"line_voltages": {"last_good": {"observed_utc": now,
                       "evidence_refs": ["synthetic.jsonl#L1"],
                       "decoded": {"readings": {"VAB": {"value": 490.0, "unit": "V"}}, "status": None}},
                       "last_error": "Synthetic failed poll"}}
            state = make_state(buffers, ["line_voltages"], 2, {}, now)
            self.assertEqual(state["readings"]["VAB"]["value"], 490)
            self.assertTrue(state["stale"])
            self.assertTrue(state["readings"]["VAB"]["stale"])
            self.assertFalse(state["display_verified"])
            self.assertIn("line_voltages", state["last_poll_errors"])

        def test_nonfinite_rejected(self):
            with self.assertRaises(ValueError):
                finite_tree({"readings": [{"value": float("nan")} ]})

        def test_missing_final_completion_rejected(self):
            report, messages = self.fixture()
            messages.pop(-2)
            report["result"]["events"] -= 1
            with self.assertRaises(ValueError):
                self.parse(report, messages)

    outcome = unittest.TextTestRunner().run(unittest.defaultTestLoader.loadTestsFromTestCase(Checks))
    return 0 if outcome.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())
