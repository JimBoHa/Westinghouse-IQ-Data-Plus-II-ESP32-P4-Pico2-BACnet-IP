#!/usr/bin/env python3
"""Bounded USB command capture and read-only HTTP views of saved Pi results."""
import argparse
import base64
import datetime as dt
import json
import os
import shutil
from pathlib import Path
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

ACTIVE = {"observe", "trial_int", "transact"}
ORDER_PROBES = {"fast_status_msb24", "fast_status_msb8", "fast_status_byte_swap"}
MAX_LINE = 65536


def utc():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def reject_constant(value):
    raise ValueError(f"Invalid JSON constant: {value}")


def json_object(text):
    value = json.loads(text, parse_constant=reject_constant)
    if not isinstance(value, dict):
        raise ValueError("Expected a JSON object")
    return value


def atomic_json(path, value):
    temporary = path.with_name(path.name + f".{os.getpid()}.tmp")
    with temporary.open("w") as stream:
        json.dump(value, stream, indent=2, allow_nan=False)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)


def make_command(action, parameters):
    """Validate the entire allowlisted command before opening a serial port."""
    def decimal(value, lower, upper, name):
        if not value.isascii() or not value.isdecimal() or not lower <= int(value) <= upper:
            raise ValueError(f"{name} must be a decimal integer from {lower} through {upper}")
        return int(value)

    if action in {"info", "status", "abort"} and not parameters:
        return action, 5.0
    if action in {"observe", "trial_int"} and len(parameters) == 1:
        duration = decimal(parameters[0], 1, 10000 if action == "observe" else 1000, "milliseconds")
        return f"{action} {duration}", duration / 1000 + 5
    if action == "transact" and len(parameters) == 4:
        kind, address, edge, milliseconds = parameters
        if kind not in {"line_voltages", "line_voltages_repeat", "currents", "currents_repeat", "fast_status", "fast_status_repeat", "legacy_status", "short_buffer", "all_standard_repeat", "power1_repeat", "power2_repeat", "energy_repeat", "flags_repeat", "settings_repeat", "trip_repeat"} | ORDER_PROBES or edge not in {"rising", "falling"}:
            raise ValueError("transact requires a supported request kind and rising|falling")
        address = decimal(address, 0, 4095, "address")
        if kind in ORDER_PROBES and address != 0:
            raise ValueError("Experimental Fast Status ordering probes require address 0")
        duration = decimal(milliseconds, 1, 5000, "milliseconds")
        return f"transact {kind} {address} {edge} {duration}", duration / 1000 + 5
    raise ValueError("Invalid arguments for the selected command")


class Boundary:
    """Only top-level terminal response types complete a command."""
    def __init__(self, action):
        self.action = action
        self.started = False
        self.terminal = None
        self.device_error = False

    def accept(self, message):
        kind = message.get("type")
        if not isinstance(kind, str):
            raise ValueError("Serial JSON object lacks a string type")
        if kind == "started":
            self.started = True
        terminal_types = {"result", "error"}
        if self.action not in ACTIVE:
            terminal_types.add(self.action)
        if kind in terminal_types:
            if self.terminal is not None:
                raise ValueError("More than one terminal response for one command")
            self.terminal = message
            self.device_error = kind == "error"
            return True
        return False


class Journal:
    def __init__(self, output):
        self.output = Path(output).resolve()
        self.output.parent.mkdir(parents=True, exist_ok=True)
        self.path = self.output.with_name(self.output.name + ".jsonl")
        # Every trial owns fresh files; existing trials are never overwritten.
        with self.output.open("x"):
            pass
        self.stream = self.path.open("x", buffering=1)
        self.received_lines = 0

    def append(self, direction, phase, raw, **extra):
        event = {"utc": utc(), "direction": direction, "phase": phase,
                 "raw_base64": base64.b64encode(raw).decode("ascii"),
                 "text": raw.decode("utf-8", errors="replace"), **extra}
        self.stream.write(json.dumps(event, allow_nan=False) + "\n")
        self.stream.flush()
        if direction == "rx":
            self.received_lines += 1
        return event

    def checkpoint(self):
        self.stream.flush()
        os.fsync(self.stream.fileno())

    def close(self):
        self.checkpoint()
        self.stream.close()


class Receiver:
    def __init__(self, serial_port, journal):
        self.serial = serial_port
        self.journal = journal
        self.pending = bytearray()

    def line(self, deadline, phase):
        while time.monotonic() < deadline:
            if b"\n" in self.pending:
                data, _, tail = self.pending.partition(b"\n")
                self.pending = bytearray(tail)
                raw = bytes(data) + b"\n"
                self.journal.append("rx", phase, raw)
                try:
                    message = json_object(raw.decode("utf-8"))
                except (UnicodeError, ValueError) as error:
                    raise ValueError(f"Malformed serial JSON at received line {self.journal.received_lines}: {error}") from error
                return message
            self.serial.timeout = max(0, min(0.1, deadline - time.monotonic()))
            chunk = self.serial.read(min(max(self.serial.in_waiting, 1), 4096))
            self.pending.extend(chunk)
            if len(self.pending.split(b"\n", 1)[0]) > MAX_LINE:
                raise ValueError("Serial response exceeds 64 KiB line limit")
        return None

    def partial(self, phase):
        if self.pending:
            self.journal.append("rx", phase, bytes(self.pending), partial=True)
            self.pending.clear()

    def send(self, line, phase):
        raw = (line + "\n").encode("ascii")
        self.journal.append("tx", phase, raw)
        self.journal.checkpoint()
        if self.serial.write(raw) != len(raw):
            raise OSError("Partial serial command write")


def execute(args):
    command, timeout = make_command(args.action, args.parameters)
    state = (args.state or args.output.resolve().parent).resolve()
    state.mkdir(parents=True, exist_ok=True)
    journal = Journal(args.output)
    report = {"schema_version": 1, "started_utc": utc(), "finished_utc": None,
              "command": command, "action": args.action, "serial_port": args.port,
              "deadline_seconds": timeout, "transport_complete": False, "status": "running",
              "transcript_jsonl": str(journal.path), "received_lines": 0,
              "result": None, "errors": [], "telemetry_validated": False,
              "interpretation": "USB responses and transmitted payloads are raw trial evidence, not validated meter readings."}
    atomic_json(journal.output, report)
    receiver = serial_port = None
    interrupted = False
    timer = time.monotonic()
    try:
        import serial
        serial_port = serial.Serial(port=None, baudrate=115200, timeout=0.1,
                                    write_timeout=1, exclusive=True)
        serial_port.dtr = True
        serial_port.rts = False
        serial_port.port = args.port
        serial_port.open()
        receiver = Receiver(serial_port, journal)
        # Preserve stale/startup records but never count them as this command's result.
        prelude_deadline = time.monotonic() + 0.2
        while receiver.line(prelude_deadline, "prelude") is not None:
            pass
        receiver.partial("prelude")
        boundary = Boundary(args.action)
        receiver.send(command, "command")
        deadline = time.monotonic() + timeout
        while boundary.terminal is None:
            message = receiver.line(deadline, "command")
            if message is None:
                raise TimeoutError("No terminal response before bounded command deadline")
            if message.get("type") == "info":
                atomic_json(state / "info.json", {"saved_utc": utc(), "trial": str(journal.output), "info": message})
            boundary.accept(message)
        report["result"] = boundary.terminal
        report["started_ack_received"] = boundary.started
        # Drain already queued complete lines so a second result cannot be hidden.
        drain_deadline = min(deadline, time.monotonic() + 0.1)
        while True:
            extra = receiver.line(drain_deadline, "command_tail")
            if extra is None:
                break
            boundary.accept(extra)
        if receiver.pending:
            raise ValueError("Partial response remained after terminal record")
        report["transport_complete"] = True
        report["status"] = "device_error" if boundary.device_error else "completed"
        if boundary.device_error:
            report["errors"].append("Device returned an error record; see result")
    except (Exception, KeyboardInterrupt) as error:
        interrupted = isinstance(error, KeyboardInterrupt)
        report["status"] = "failed"
        report["errors"].append(f"{type(error).__name__}: {error}")
        if receiver is not None and args.action in ACTIVE:
            # One safe cleanup command only. Never retry an active stimulus.
            try:
                receiver.partial("failed_command_partial")
                receiver.send("abort", "cleanup")
                cleanup = Boundary("abort")
                deadline = time.monotonic() + 2
                while cleanup.terminal is None:
                    message = receiver.line(deadline, "cleanup")
                    if message is None:
                        raise TimeoutError("No response to cleanup abort")
                    cleanup.accept(message)
                report["cleanup_response"] = cleanup.terminal
            except (Exception, KeyboardInterrupt) as cleanup_error:
                interrupted = interrupted or isinstance(cleanup_error, KeyboardInterrupt)
                report["errors"].append(f"Cleanup {type(cleanup_error).__name__}: {cleanup_error}")
    finally:
        if receiver is not None:
            receiver.partial("final_partial")
        if serial_port is not None:
            try:
                serial_port.close()
            except Exception as error:
                report["status"] = "failed"
                report["errors"].append(f"Serial close: {error}")
        report["finished_utc"] = utc()
        report["wall_seconds"] = time.monotonic() - timer
        report["received_lines"] = journal.received_lines
        journal.close()
        atomic_json(journal.output, report)
        atomic_json(state / "latest_result.json", report)
    if interrupted:
        # Cleanup and evidence are durable before the recorder sees the signal.
        # A continuous --recover recorder must not treat SIGINT as a retryable read.
        raise KeyboardInterrupt
    print(json.dumps({"status": report["status"], "transport_complete": report["transport_complete"],
                      "output": str(journal.output), "journal": str(journal.path), "errors": report["errors"]}))
    return 0 if report["status"] == "completed" else 1


def saved_json(path):
    try:
        if path.stat().st_size > 4 * 1024 * 1024:
            raise ValueError("Saved JSON exceeds 4 MiB")
        return json_object(path.read_text()), None
    except (OSError, ValueError, UnicodeError) as error:
        return None, f"{type(error).__name__}: {error}"


def telemetry(state):
    # Protocol-decoded physical replies are useful before a person compares the
    # display. Publish that distinction explicitly rather than inventing a match.
    decoded_path = state / "decoded_readings.json"
    if decoded_path.exists():
        document, error = saved_json(decoded_path)
        if error:
            return 503, {"available": False, "reason": error, "readings": None}
        references = document.get("evidence_refs")
        readings = document.get("readings")
        if (document.get("source") != "meter_write" or document.get("protocol_valid") is not True
                or not isinstance(document.get("display_verified"), bool)
                or not isinstance(references, list) or not references
                or not all(isinstance(reference, str) and reference.strip() for reference in references)
                or not isinstance(readings, (dict, list)) or not readings):
            return 503, {"available": False, "reason": "Incomplete protocol-decoded meter evidence", "readings": None}
        try:
            observed = dt.datetime.fromisoformat(document["observed_utc"])
            if observed.tzinfo is None:
                raise ValueError("Observation time requires a timezone")
            age = (dt.datetime.now(dt.timezone.utc) - observed).total_seconds()
            maximum_age = float(document["stale_after_seconds"])
            if not 0 < maximum_age <= 3600 or age < -5:
                raise ValueError("Invalid telemetry freshness metadata")
        except (KeyError, TypeError, ValueError) as error:
            return 503, {"available": False, "reason": str(error), "readings": None}
        stale = document.get("stale") is not False or age > maximum_age
        def fresh_item(item):
            if not isinstance(item, dict):
                return item
            try:
                stamp = dt.datetime.fromisoformat(item.get("observed_utc", document["observed_utc"]))
                if stamp.tzinfo is None:
                    raise ValueError("Missing timezone")
                item_age = (dt.datetime.now(dt.timezone.utc) - stamp).total_seconds()
            except (TypeError, ValueError):
                return {**item, "stale": True, "age_seconds": None}
            return {**item, "stale": item.get("stale", False) is not False or item_age > maximum_age or item_age < -5,
                    "age_seconds": max(0, item_age)}
        refreshed = ({name: fresh_item(item) for name, item in readings.items()}
                     if isinstance(readings, dict) else [fresh_item(item) for item in readings])
        return (503 if stale else 200), {**document, "available": not stale, "stale": stale,
                                        "quality": "stale_or_failed" if stale else document.get("quality", "decoded_unverified"),
                                        "readings": refreshed, "status": fresh_item(document.get("status")),
                                        "age_seconds": max(0, age), "served_utc": utc()}
    document, error = saved_json(state / "validated_readings.json")
    if error:
        return 503, {"available": False, "reason": "No readable validated_readings.json", "readings": None}
    references = document.get("evidence_refs")
    readings = document.get("readings")
    if (document.get("source") != "meter_write" or document.get("display_verified") is not True
            or not isinstance(references, list) or not references
            or not all(isinstance(reference, str) and reference.strip() for reference in references)
            or not isinstance(readings, (dict, list)) or not readings):
        return 503, {"available": False,
                     "reason": "Validated telemetry requires source=meter_write, display_verified=true, nonempty evidence_refs and readings",
                     "readings": None}
    return 200, {**document, "available": True}


def endpoint(state, path):
    route = urlsplit(path).path
    if route == "/telemetry":
        return telemetry(state)
    if route == "/status":
        info, info_error = saved_json(state / "info.json")
        latest, latest_error = saved_json(state / "latest_result.json")
        telemetry_code, _ = telemetry(state)
        return 200, {"served_utc": utc(), "read_only": True, "info": info, "latest_result": latest,
                     "saved_state_errors": {"info": info_error, "latest_result": latest_error},
                     "telemetry_available": telemetry_code == 200}
    return 404, {"error": "not_found"}


def handler_for(state, database_snapshot=None):
    class Handler(BaseHTTPRequestHandler):
        def database(self, body=True):
            if database_snapshot is None:
                return self.respond(404, {"error": "database_download_not_configured"}, body)
            try:
                stream = Path(database_snapshot).open("rb")
            except OSError:
                return self.respond(503, {"error": "database_snapshot_not_ready"}, body)
            with stream:
                size = os.fstat(stream.fileno()).st_size
                self.send_response(200)
                self.send_header("Content-Type", "application/vnd.sqlite3")
                self.send_header("Content-Disposition", 'attachment; filename="iqdata-latest.sqlite"')
                self.send_header("Content-Length", str(size))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                if body:
                    shutil.copyfileobj(stream, self.wfile, 1024 * 1024)

        def respond(self, code, payload, body=True):
            raw = (json.dumps(payload, allow_nan=False) + "\n").encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Cache-Control", "no-store")
            if code == 405:
                self.send_header("Allow", "GET, HEAD")
            self.end_headers()
            if body:
                self.wfile.write(raw)

        def do_GET(self):
            if urlsplit(self.path).path == "/database":
                return self.database()
            self.respond(*endpoint(state, self.path))

        def do_HEAD(self):
            if urlsplit(self.path).path == "/database":
                return self.database(body=False)
            self.respond(*endpoint(state, self.path), body=False)

        def reject_write(self):
            self.respond(405, {"error": "read_only_endpoint"})

        do_POST = do_PUT = do_PATCH = do_DELETE = do_OPTIONS = reject_write
    return Handler


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="mode", required=True)
    command = subparsers.add_parser("command", help="Run one bounded allowlisted USB command")
    command.add_argument("action", choices=["info", "status", "observe", "trial_int", "transact", "abort"])
    command.add_argument("parameters", nargs="*")
    command.add_argument("--port", required=True, help="USB serial device, preferably /dev/serial/by-id/...")
    command.add_argument("--output", required=True, type=Path, help="New per-trial JSON summary path")
    command.add_argument("--state", type=Path, help="Saved HTTP state directory; defaults to output parent")
    serve = subparsers.add_parser("serve", help="Expose saved state over read-only HTTP; never opens USB")
    serve.add_argument("--bind", default="0.0.0.0")
    serve.add_argument("--port", type=int, default=8080)
    serve.add_argument("--state", type=Path, required=True)
    serve.add_argument("--database-snapshot", type=Path, help="Optional consistent SQLite snapshot exposed at /database")
    args = parser.parse_args()
    if args.mode == "command":
        try:
            make_command(args.action, args.parameters)
            if "://" in args.port:
                raise ValueError("USB serial device paths only; URL transports are unsupported")
            return execute(args)
        except (OSError, ValueError) as error:
            parser.exit(2, f"Host command failed: {error}\n")
    if not 1 <= args.port <= 65535:
        parser.error("HTTP port must be between 1 and 65535")
    state = args.state.resolve()
    with ThreadingHTTPServer((args.bind, args.port), handler_for(state, args.database_snapshot)) as server:
        print(json.dumps({"mode": "read_only_http", "bind": args.bind, "port": args.port, "state": str(state)}), flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
