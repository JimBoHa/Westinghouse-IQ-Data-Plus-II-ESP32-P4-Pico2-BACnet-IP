#!/usr/bin/env python3
"""Pinned HTTPS management; secrets stay in private files."""
import argparse
import json
from pathlib import Path
import sys
import time
from gateway_client import Gateway, image_signature


def observe(client):
    queued = client.request("/api/pico/observe", b"", authenticated=True)
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        snapshot = client.request("/api/diagnostics", b"", authenticated=True)
        probe = snapshot.get("passive_observation", {})
        if snapshot.get("boot_id") != queued["boot_id"] or probe.get("sequence") != queued["sequence"]:
            raise RuntimeError("Gateway restarted or observation replaced; result cannot be attributed to this request")
        if probe.get("state") in ("complete", "failed"):
            return probe
        time.sleep(0.5)
    raise RuntimeError("Passive observation did not finish before the deadline")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", required=True)
    parser.add_argument("--expected-mac")
    parser.add_argument("--token-file", type=Path)
    parser.add_argument("--pin-file", type=Path, required=True)
    sub = parser.add_subparsers(dest="command", required=True)
    for command in ("pair", "status", "points", "auth-check", "diagnostics", "reboot", "observe"):
        sub.add_parser(command)
    sub.add_parser("configure").add_argument("json_file", type=Path)
    sub.add_parser("update").add_argument("image", type=Path)
    sub.add_parser("pico-update").add_argument("image", type=Path)
    args = parser.parse_args()
    client = Gateway(args.host, args.pin_file, args.token_file, args.expected_mac)
    if args.command == "pair":
        print(json.dumps(client.pair(), indent=2));return
    before = client.status()
    if args.command == "status":
        print(json.dumps(before, indent=2));return
    if args.command == "points":
        print(json.dumps(client.request("/api/points"), indent=2));return
    if args.command == "diagnostics":
        print(json.dumps(client.request("/api/diagnostics", b"", authenticated=True), indent=2));return
    if args.command == "observe":
        result = observe(client)
        print(json.dumps(result, indent=2))
        if result["state"] != "complete":
            raise RuntimeError("Passive observation failed: " + result.get("error", "unknown error"))
        return
    if args.command == "reboot":
        print(json.dumps(client.request("/api/reboot", b"", authenticated=True), indent=2));return
    if args.command == "auth-check":
        print(json.dumps(client.request("/api/auth/check", b"", authenticated=True), indent=2));return
    if args.command == "configure":
        data = args.json_file.read_bytes();config = json.loads(data)
        if config.get("device_instance") == 75151 or config.get("ip") == "192.168.75.151":
            parser.error("Production IP and Device instance are protected")
        print(json.dumps(client.request("/api/config", data, authenticated=True), indent=2));return
    target = "pico2" if args.command == "pico-update" else "esp32p4"
    data, headers = image_signature(args.image, target)
    if target == "esp32p4" and len(data) > before["ota"]["slot_bytes"]:
        parser.error("Application exceeds inactive OTA slot")
    path = "/api/pico/firmware" if target == "pico2" else "/api/firmware"
    result = client.request(path, data, authenticated=True, headers=headers, timeout=150)
    if target == "pico2":
        if not result.get("pico_boot_verified"):
            raise RuntimeError("Expected Pico firmware did not return")
        print(json.dumps(result, indent=2));return
    expected_hash = data[176:208].hex()
    deadline = time.monotonic() + 90
    time.sleep(3)
    while time.monotonic() < deadline:
        try:
            after = client.status()
            if after.get("elf_sha256") == expected_hash and after["ota"]["running_slot"] != before["ota"]["running_slot"] and after["ota"]["image_state"] == 2:
                print(json.dumps({"boot_verified": True, "version": after["version"], "slot": after["ota"]["running_slot"], "elf_sha256": expected_hash}, indent=2));return
        except (OSError, RuntimeError):
            pass
        time.sleep(2)
    raise RuntimeError("Upload accepted but exact new image did not become valid; inspect rollback")


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, KeyError, RuntimeError) as error:
        sys.exit(str(error))
