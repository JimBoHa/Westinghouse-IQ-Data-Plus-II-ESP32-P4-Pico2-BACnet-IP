#!/usr/bin/env python3
"""Read-only, pinned HTTPS and directed BACnet health soak. No meter values saved."""
import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import socket
import sys
import time
from gateway_client import Gateway


def schedule(duration, interval):
    if not all(type(x) in (int, float) and math.isfinite(x) and x > 0 for x in (duration, interval)):
        raise ValueError("Duration and interval must be finite and positive")
    if math.ceil(duration/interval)+1 > 1000000:
        raise ValueError("At most one million samples per run")
    count = math.ceil(duration/interval)
    return [min(i*interval, duration) for i in range(count+1)]


def parse_i_am(packet):
    """Strict independent decoder for direct local BACnet/IPv4 I-Am responses."""
    if len(packet) < 16 or packet[:2] not in (b"\x81\x0a", b"\x81\x0b") or int.from_bytes(packet[2:4], "big") != len(packet):
        raise ValueError("Malformed BVLC frame")
    # This monitor intentionally queries only its directly attached local peer.
    if packet[4:8] != b"\x01\x00\x10\x00":
        raise ValueError("Expected a direct local I-Am")
    offset = 8; values = []
    for tag, lengths in ((12, (4,)), (2, (1, 2, 3, 4)), (9, (1,)), (2, (1, 2))):
        if offset >= len(packet):
            raise ValueError("Truncated I-Am")
        header = packet[offset]; length = header & 7; offset += 1
        if header >> 4 != tag or header & 8 or length not in lengths or offset+length > len(packet):
            raise ValueError("Invalid I-Am application tag")
        values.append(int.from_bytes(packet[offset:offset+length], "big")); offset += length
    if offset != len(packet) or values[0] >> 22 != 8 or values[0] & 0x3fffff >= 4194303 or not 50 <= values[1] <= 1476 or values[2] > 3:
        raise ValueError("Invalid I-Am values")
    return {"device_instance": values[0] & 0x3fffff, "max_apdu": values[1], "segmentation": values[2], "vendor_id": values[3]}


def probe_bacnet(address, instance, port, timeout):
    if address == "192.168.75.151" or instance == 75151 or not 0 <= instance < 4194303:
        raise ValueError("Protected or invalid BACnet target")
    bound = instance.to_bytes(3, "big")
    body = b"\x01\x00\x10\x08\x0b"+bound+b"\x1b"+bound
    packet = b"\x81\x0a"+(len(body)+4).to_bytes(2,"big")+body
    deadline = time.monotonic()+timeout
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as client:
        client.connect((address,port))  # Kernel filters replies from other peers.
        client.send(packet)
        while True:
            left = deadline-time.monotonic()
            if left <= 0:
                raise TimeoutError("Directed I-Am timed out")
            client.settimeout(left); data = client.recv(2048)
            try:
                reply = parse_i_am(data)
            except ValueError:
                continue  # Unrelated notifications cannot satisfy discovery.
            if reply["device_instance"] != instance:
                raise ValueError("Peer answered with an unexpected Device instance")
            return reply


def measured(probe):
    start = time.monotonic()
    try:
        return {"ok":True,"value":probe(),"elapsed_ms":(time.monotonic()-start)*1000}
    except Exception as error:
        return {"ok":False,"error":f"{type(error).__name__}: {error}","elapsed_ms":(time.monotonic()-start)*1000}


def collect(probes):
    with ThreadPoolExecutor(max_workers=len(probes)) as pool:
        pending = {name:pool.submit(measured, probe) for name,probe in probes.items()}
        return {name:future.result() for name,future in pending.items()}


def sample_alerts(header, baseline, previous, row):
    alerts = []
    for name in ("status", "bacnet"):
        probe = row.get("probes",{}).get(name,{})
        if probe.get("ok") is not True:
            alerts.append(f"{name}-probe-failed")
    status = row.get("probes",{}).get("status",{})
    if status.get("ok") is not True:
        return alerts
    s = status["value"]; target = header["target"]
    try:
        checks = {
            "identity-changed":s["ethernet_mac"] != target["mac"] or s["config"]["device_instance"] != target["device_instance"] or s["project"] != "iqdata_p4_gateway",
            "network-unhealthy":not s["ethernet"]["link_up"] or not s["ethernet"]["ready"] or s["ethernet"]["protected_address_blocked"] or s["ethernet"]["ip"] != target["ip"],
            "startup-unhealthy":not s["ota"]["startup_health"]["accepted"] or s["ota"]["image_state"] != 2 or not s["boot_id"] or s.get("restarting",False),
            "internal-heap-low":s["internal_free_heap"] < header["minimum_heap"],
            "pico-unhealthy":not s["pico"]["connected"] or not s["pico"]["qualified"] or s["pico"]["maintenance"] or s["pico"]["heartbeat_age_seconds"] >= 7,
            "bacnet-unhealthy":not s["bacnet"]["initialized"] or s["bacnet"]["heartbeat_age_seconds"] >= 2 or s["bacnet"]["analog_inputs"] != 106 or s["bacnet"]["binary_inputs"] != 92,
            "duplicate-device-instance":s["bacnet"]["instance_conflicts"] != 0,
            "restart-notice-unhealthy":not s["bacnet"]["restart_notification"]["timestamp_frozen"] or s["bacnet"]["restart_notification"]["sent"] != 1 or s["bacnet"]["restart_notification"]["failures"] != 0,
            "polling-mode-changed":s["config"]["poll_enabled"] != header["poll_enabled"],
        }
        if baseline:
            for name in ("boot_id","version","source_revision","elf_sha256","reset_reason","config"):
                checks[f"{name}-changed"] = s[name] != baseline[name]
            checks["ota-slot-changed"] = s["ota"]["running_slot"] != baseline["ota"]["running_slot"]
            for name in ("version","connections","failures","overflows"):
                checks[f"pico-{name}-changed"] = s["pico"][name] != baseline["pico"][name]
            checks["cov-timeouts-increased"] = s["bacnet"]["cov_timeouts"] != baseline["bacnet"]["cov_timeouts"]
            checks["clock-lost"] = baseline["clock"]["synchronized"] and not s["clock"]["synchronized"]
            checks["uptime-drift"] = abs(s["uptime_seconds"]-baseline["uptime_seconds"]-row["elapsed_seconds"]) > header["timeout"]*2+2
            checks["heap-loss-exceeded"] = baseline["internal_free_heap"]-s["internal_free_heap"] > header["maximum_heap_loss"]
        else:
            checks["no-initial-status-baseline"] = True
        if previous:
            checks["uptime-decreased"] = s["uptime_seconds"] < previous["uptime_seconds"]
        bacnet = row["probes"].get("bacnet",{})
        if bacnet.get("ok") is True:
            checks["bacnet-identity-mismatch"] = bacnet["value"]["device_instance"] != target["device_instance"]
        alerts.extend(name for name, failed in checks.items() if failed)
        for flag in (s["ethernet"]["link_up"],s["ethernet"]["ready"],s["ethernet"]["protected_address_blocked"],
                     s["ota"]["startup_health"]["accepted"],s["pico"]["connected"],s["pico"]["qualified"],
                     s["pico"]["maintenance"],s["bacnet"]["initialized"],s["config"]["poll_enabled"],s["clock"]["synchronized"]):
            if type(flag) is not bool:
                alerts.append("invalid-health-boolean")
        for number in (s["uptime_seconds"], s["internal_free_heap"], s["pico"]["heartbeat_age_seconds"], s["bacnet"]["heartbeat_age_seconds"]):
            if type(number) not in (int,float) or not math.isfinite(number) or number < 0:
                alerts.append("invalid-health-number")
    except (KeyError, TypeError, ValueError) as error:
        alerts.append(f"invalid-status:{error}")
    return alerts


def review_records(records, minimum_duration=0):
    """Recompute health; never trust a saved 'passed' flag or missing tail."""
    if not records or records[0].get("type") != "header" or records[0].get("schema") != 1:
        raise ValueError("Missing schema-1 header")
    header = records[0]; planned = schedule(header["duration"],header["interval"])
    if type(minimum_duration) not in (int,float) or not math.isfinite(minimum_duration) or minimum_duration < 0:
        raise ValueError("Required minimum duration must be finite and nonnegative")
    if (type(header["timeout"]) not in (int,float) or not math.isfinite(header["timeout"]) or not 0 < header["timeout"] <= 60 or
        type(header["minimum_heap"]) is not int or header["minimum_heap"] < 32768 or
        type(header["maximum_heap_loss"]) is not int or header["maximum_heap_loss"] < 0 or
        type(header["poll_enabled"]) is not bool):
        raise ValueError("Invalid saved health limits")
    samples = []; final = None
    for row in records[1:]:
        if final is not None:
            raise ValueError("Records after final summary")
        if row.get("type") == "sample":
            samples.append(row)
        elif row.get("type") == "summary":
            final = row
        else:
            raise ValueError("Unknown record type")
    issues = []; findings = []; previous = None; last_elapsed = -1; heaps = []
    baseline = samples[0].get("probes",{}).get("status",{}).get("value") if samples else None
    if header["duration"] < minimum_duration:
        issues.append("planned-duration-below-required-minimum")
    if len(samples) != len(planned):
        issues.append("sample-count-incomplete")
    for seq,row in enumerate(samples):
        alerts = sample_alerts(header,baseline,previous,row)
        if row.get("sequence") != seq:
            alerts.append("sequence-gap")
        elapsed = row.get("elapsed_seconds")
        valid_time = type(elapsed) in (int,float) and math.isfinite(elapsed) and elapsed >= 0
        if not valid_time or elapsed < last_elapsed:
            alerts.append("invalid-monotonic-time")
        if seq >= len(planned) or row.get("scheduled_seconds") != planned[seq]:
            alerts.append("schedule-mismatch")
        elif valid_time and (elapsed+0.001 < planned[seq] or elapsed-planned[seq] > max(2,header["interval"]*.5)):
            alerts.append("sample-outside-schedule")
        if row.get("ok") is not True or row.get("alerts"):
            alerts.append("recorded-sample-failed")
        if valid_time:
            last_elapsed = elapsed
        status = row.get("probes",{}).get("status",{})
        if status.get("ok") is True:
            previous = status["value"]
            heap = previous.get("internal_free_heap")
            if type(heap) in (int,float) and math.isfinite(heap):
                heaps.append(heap)
        if alerts:
            findings.append({"sequence":seq,"alerts":sorted(set(alerts))})
    complete = bool(final and final.get("completed") is True and final.get("sample_count") == len(planned) and len(samples) == len(planned) and last_elapsed >= header["duration"])
    if not complete:
        issues.append("run-not-complete")
    if final and final.get("passed") is not True:
        issues.append("recorded-run-failed")
    return {"passed":complete and not issues and not findings,"completed":complete,"sample_count":len(samples),
            "planned_duration_seconds":header["duration"],"observed_seconds":max(0,last_elapsed),
            "minimum_internal_heap":min(heaps) if heaps else None,"issues":issues,"findings":findings}


def load_records(path):
    rows = []
    def invalid(value):
        raise ValueError(f"Non-finite JSON: {value}")
    with Path(path).open() as source:
        while line := source.readline(2*1024*1024+1):
            if len(line) > 2*1024*1024 or len(rows) > 1000002:
                raise ValueError("Oversized soak log")
            rows.append(json.loads(line,parse_constant=invalid))
    return rows


def run(args):
    offsets = schedule(args.duration,args.interval)
    if not 0 < args.timeout <= 60 or args.minimum_heap < 32768 or args.maximum_heap_loss < 0 or not 0 <= args.device < 4194303 or args.device == 75151 or not 1 <= args.bacnet_port <= 65535:
        raise ValueError("Invalid health limits or target")
    gateway = Gateway(args.host,args.pin_file,expected_mac=args.expected_mac,timeout=args.timeout)
    header = {"type":"header","schema":1,"started_utc":datetime.now(timezone.utc).isoformat(),
        "duration":args.duration,"interval":args.interval,"timeout":args.timeout,
        "minimum_heap":args.minimum_heap,"maximum_heap_loss":args.maximum_heap_loss,"poll_enabled":args.poll_enabled,
        "target":{"ip":gateway.address,"mac":args.expected_mac.lower(),"device_instance":args.device,"bacnet_port":args.bacnet_port}}
    args.output.parent.mkdir(parents=True,exist_ok=True)
    fd = os.open(args.output,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600)
    rows = [header]; baseline = previous = None; completed = False; interrupted = False
    start = time.monotonic()
    with os.fdopen(fd,"w",buffering=1) as output:
        def record(row):
            output.write(json.dumps(row,allow_nan=False,separators=(",",":"))+"\n")
        record(header)
        try:
            for seq,offset in enumerate(offsets):
                time.sleep(max(0,start+offset-time.monotonic()))
                elapsed = time.monotonic()-start
                probes = collect({"status":gateway.status,"bacnet":lambda:probe_bacnet(gateway.address,args.device,args.bacnet_port,args.timeout)})
                if seq == 0 and probes["status"]["ok"]:
                    baseline = probes["status"]["value"]
                row = {"type":"sample","sequence":seq,"scheduled_seconds":offset,"elapsed_seconds":elapsed,
                       "probes":probes,"finished_seconds":time.monotonic()-start}
                alerts = sample_alerts(header,baseline,previous,row)
                if elapsed-offset > max(2,args.interval*.5):
                    alerts.append("sample-outside-schedule")
                row.update(ok=not alerts,alerts=alerts);rows.append(row);record(row)
                if probes["status"]["ok"]:
                    previous = probes["status"]["value"]
                if alerts:
                    print(json.dumps({"sequence":seq,"alerts":alerts}),flush=True)
            completed = True
        except KeyboardInterrupt:
            interrupted = True
        finally:
            tail = {"type":"summary","completed":completed,"passed":True,"sample_count":len(rows)-1,"interrupted":interrupted}
            result = review_records(rows+[tail]);tail["passed"] = result["passed"]
            record(tail);os.fsync(output.fileno())
    print(json.dumps(result,indent=2))
    return 0 if result["passed"] else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("host","expected-mac"):
        parser.add_argument("--"+name,required=True)
    parser.add_argument("--pin-file",type=Path,required=True)
    parser.add_argument("--device",type=int,required=True)
    parser.add_argument("--bacnet-port",type=int,default=47808)
    parser.add_argument("--duration",type=float,default=86400)
    parser.add_argument("--interval",type=float,default=10)
    parser.add_argument("--timeout",type=float,default=4)
    parser.add_argument("--minimum-heap",type=int,default=65536)
    parser.add_argument("--maximum-heap-loss",type=int,default=65536)
    parser.add_argument("--poll-enabled",action="store_true",help="Expect polling already enabled; never changes it")
    parser.add_argument("--output",type=Path,required=True)
    return run(parser.parse_args())


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (OSError,ValueError,KeyError) as error:
        sys.exit(str(error))
