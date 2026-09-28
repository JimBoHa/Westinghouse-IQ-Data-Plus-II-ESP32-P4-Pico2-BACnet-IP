#!/usr/bin/env python3
"""LAN management client. Tokens stay in private files, never command arguments."""
import argparse
import hashlib
import ipaddress
import json
from pathlib import Path
import socket
import sys
import time
import urllib.error
import urllib.request

PROTECTED_IP = "192.168.75.151"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", required=True, help="Gateway IPv4 or its iqdata-XXXXXX.local name")
    parser.add_argument("--expected-mac", help="Required for writes: Ethernet MAC reported by serial status")
    parser.add_argument("--token-file", type=Path, help="Private file containing the gateway's 64-hex update token")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("status")
    sub.add_parser("points")
    sub.add_parser("configure").add_argument("json_file", type=Path)
    sub.add_parser("update").add_argument("app_bin", type=Path)
    sub.add_parser("pico-update").add_argument("uf2", type=Path)
    args = parser.parse_args()
    if any(c in args.host for c in "/:@?#"):
        parser.error("--host must be a hostname or IPv4, without URL syntax")
    resolved = {x[4][0] for x in socket.getaddrinfo(args.host, 80, family=socket.AF_INET, type=socket.SOCK_STREAM)}
    if len(resolved) != 1:
        parser.error("Gateway name must resolve to exactly one IPv4 address")
    address = resolved.pop()
    if address == PROTECTED_IP:
        parser.error("The working production gateway 192.168.75.151 is protected")
    if not ipaddress.ip_address(address).is_private:
        parser.error("Management is restricted to a trusted local network")
    # Pin resolved address for the complete operation; never use configured proxies.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    def request(path, data=None, headers=None):
        if data is not None:
            print(f"Sending {len(data)} bytes to {address}{path}", flush=True)
        req = urllib.request.Request(f"http://{address}{path}", data=data, headers=headers or {})
        with opener.open(req, timeout=150 if path.endswith("/firmware") else 10) as response:
            return json.load(response)
    before = request("/api/status")
    if before.get("project") != "iqdata_p4_gateway":
        parser.error("Target is not an IQData P4 gateway")
    if args.expected_mac and before.get("ethernet_mac", "").lower() != args.expected_mac.lower():
        parser.error("Target Ethernet MAC does not match --expected-mac")
    if args.command == "status":
        print(json.dumps(before, indent=2)); return
    if args.command == "points":
        print(json.dumps(request("/api/points"), indent=2)); return
    if not args.expected_mac or not args.token_file:
        parser.error("Writes require --expected-mac and --token-file")
    if args.token_file.stat().st_mode & 0o077:
        parser.error("Token file must be private: chmod 600 <token-file>")
    token = args.token_file.read_text().strip()
    if len(token) != 64 or any(c not in "0123456789abcdef" for c in token):
        parser.error("Token file must contain 64 lowercase hex characters")
    headers = {"Authorization": "Bearer " + token}
    if args.command == "configure":
        data = args.json_file.read_bytes()
        config = json.loads(data)
        if config.get("device_instance") == 75151 or config.get("ip") == PROTECTED_IP:
            parser.error("Production IP and Device instance are protected")
        headers["Content-Type"] = "application/json"
        print(json.dumps(request("/api/config", data, headers), indent=2))
        print("Configuration saved. Gateway will reboot; use its .local name after an IP change.")
        return
    if args.command == "pico-update":
        data = args.uf2.read_bytes()
        if len(data) < 1024 or len(data) > 1048576 or len(data) % 512 or data[:8] != bytes.fromhex("5546320a57515d9e"):
            parser.error("Expected a complete plain Pico 2 UF2 image, up to 1 MiB")
        headers.update({"Content-Type": "application/octet-stream", "X-SHA256": hashlib.sha256(data).hexdigest()})
        result = request("/api/pico/firmware", data, headers)
        if not result.get("pico_boot_verified"):
            raise RuntimeError("Pico image was not confirmed running")
        print(json.dumps(result, indent=2))
        return
    data = args.app_bin.read_bytes()
    if len(data) < 512 or data[0] != 0xe9 or data[12:14] != b"\x12\x00":
        parser.error("Expected an ESP32-P4 application binary, not a merged flash image")
    if len(data) > before["ota"]["slot_bytes"]:
        parser.error("Application exceeds target OTA slot size")
    headers.update({"Content-Type": "application/octet-stream", "X-SHA256": hashlib.sha256(data).hexdigest()})
    print(json.dumps(request("/api/firmware", data, headers), indent=2))
    expected_hash = data[176:208].hex()  # esp_app_desc_t.app_elf_sha256, after image + segment headers
    deadline = time.monotonic() + 75
    time.sleep(3)
    while time.monotonic() < deadline:
        try:
            after = request("/api/status")
            if after.get("ethernet_mac") != before["ethernet_mac"]:
                raise RuntimeError("Different device responded after reboot")
            if after.get("elf_sha256") == expected_hash and after["ota"]["running_slot"] != before["ota"]["running_slot"] and after["ota"]["image_state"] == 2:
                print(json.dumps({"boot_verified": True, "address": address, "version": after["version"], "slot": after["ota"]["running_slot"], "elf_sha256": after["elf_sha256"]}, indent=2))
                return
        except (urllib.error.URLError, TimeoutError, ConnectionError):
            pass
        time.sleep(2)
    raise RuntimeError("Upload accepted but new image did not become valid in 75 seconds; inspect status/rollback")


if __name__ == "__main__":
    try:
        main()
    except urllib.error.HTTPError as error:
        sys.exit(f"Gateway rejected request ({error.code}): {error.read(1024).decode(errors='replace')}")
    except (OSError, ValueError, KeyError, RuntimeError) as error:
        sys.exit(str(error))
