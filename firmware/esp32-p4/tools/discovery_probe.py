#!/usr/bin/env python3
"""Verify discovery wire forms against one identity-checked gateway, without a LAN scan."""
import argparse
import json
import os
from pathlib import Path
import socket
import time
from gateway_client import Gateway
from soak_monitor import parse_i_am


def probe(address, instance, local_port, timeout):
    bound = instance.to_bytes(3, "big")
    limits = b"\x0b" + bound + b"\x1b" + bound
    other = (instance + 1 if instance < 4194302 else instance - 1).to_bytes(3, "big")
    cases = [
        ("local-unicast", 10, b"\x01\x00", limits, True),
        ("global-npdu-unicast", 10, b"\x01\x20\xff\xff\x00\xff", limits, True),
        ("local-bvlc-broadcast", 11, b"\x01\x00", limits, True),
        ("global-bvlc-broadcast", 11, b"\x01\x20\xff\xff\x00\xff", limits, True),
        ("unrestricted-who-is", 10, b"\x01\x00", b"", True),
        ("excluded-range", 10, b"\x01\x00", b"\x0b" + other + b"\x1b" + other, False),
        ("trailing-field", 10, b"\x01\x00", limits + b"\x00", False),
    ]
    results = []
    for name, function, npdu, data, expected in cases:
        payload = npdu + b"\x10\x08" + data
        packet = bytes([0x81, function]) + (len(payload) + 4).to_bytes(2, "big") + payload
        row = {"case": name, "expected_reply": expected, "received": False}
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as client:
            client.bind(("", local_port))
            client.connect(address)  # Always UDP unicast to the verified target IP, including BVLC broadcast forms.
            row["local_port"] = client.getsockname()[1]
            client.send(packet)
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                client.settimeout(max(0.01, deadline - time.monotonic()))
                try:
                    response = client.recv(2048)
                except TimeoutError:
                    break
                try:
                    reply = parse_i_am(response)
                except ValueError:
                    continue
                if reply["device_instance"] == instance:
                    row["received"] = True
                    row["reply"] = reply
                    row["unicast_reply"] = response[1] == 0x0a
                    break
        row["passed"] = row["received"] == expected and (not expected or row["unicast_reply"])
        results.append(row)
        print(json.dumps(row), flush=True)
    return results


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", required=True)
    parser.add_argument("--expected-mac", required=True)
    parser.add_argument("--pin-file", type=Path, required=True)
    parser.add_argument("--local-port", type=int, default=0, help="0 allocates a requester port; use 47808 to also check the standard port")
    parser.add_argument("--timeout", type=float, default=2)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if not 0 <= args.local_port <= 65535 or not 0 < args.timeout <= 10:
        parser.error("Invalid local port or timeout")
    gateway = Gateway(args.host, args.pin_file, expected_mac=args.expected_mac)
    before = gateway.status()
    instance = before["config"]["device_instance"]
    results = probe((gateway.address, before["config"]["bacnet_port"]), instance, args.local_port, args.timeout)
    after = gateway.status()
    report = {"target": gateway.address, "instance": instance, "version": before["version"],
              "method": "UDP unicast delivery of BACnet unicast/broadcast wire forms; no LAN broadcast scan",
              "cases": results, "discovery": after["bacnet"].get("discovery"),
              "passed": all(row["passed"] for row in results) and before["boot_id"] == after["boot_id"]}
    with open(args.output, "w", opener=lambda path, flags: os.open(path, flags, 0o600)) as output:
        json.dump(report, output, indent=2)
        output.write("\n")
    if not report["passed"]:
        raise SystemExit("Discovery verification failed; inspect the per-case report")


if __name__ == "__main__":
    main()
