#!/usr/bin/env python3
"""Negative management tests; invalid requests must not reboot or alter devices."""
import argparse
import hashlib
import ipaddress
import json
from pathlib import Path
import urllib.error
import urllib.request


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", required=True, type=ipaddress.IPv4Address)
    parser.add_argument("--expected-mac", required=True)
    parser.add_argument("--token-file", required=True, type=Path)
    parser.add_argument("--app-bin", required=True, type=Path)
    parser.add_argument("--pico-uf2", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    if str(args.target) == "192.168.75.151" or not args.target.is_private:
        parser.error("Use only the identified development gateway on the private LAN")
    if args.token_file.stat().st_mode & 0o077:
        parser.error("Token file must have mode 0600")
    token = args.token_file.read_text().strip()
    assert len(token) == 64 and all(c in "0123456789abcdef" for c in token)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def request(path, payload=None, headers=None):
        req = urllib.request.Request(f"http://{args.target}{path}", data=payload, headers=headers or {})
        try:
            with opener.open(req, timeout=20) as response:
                return response.status, response.read()
        except urllib.error.HTTPError as error:
            return error.code, error.read(1024)

    def status():
        code, body = request("/api/status")
        assert code == 200
        state = json.loads(body)
        assert state["project"] == "iqdata_p4_gateway"
        assert state["ethernet_mac"].lower() == args.expected_mac.lower()
        assert state["config"]["device_instance"] != 75151
        return state

    before = status()
    assert before["pico"]["qualified"], "Qualify the Pico before running these tests"
    checks = {}

    def reject(name, path, payload, expected=400, valid_token=True, digest=None):
        headers = {"Content-Type": "application/octet-stream",
                   "X-SHA256": digest or hashlib.sha256(payload).hexdigest()}
        if valid_token:
            headers["Authorization"] = "Bearer " + token
        code, body = request(path, payload, headers)
        assert code == expected, (name, code, body)
        checks[name] = {"http_status": code, "reason": body.decode(errors="replace")}

    pico = args.pico_uf2.read_bytes()
    app = args.app_bin.read_bytes()
    reject("missing_authentication", "/api/pico/firmware", pico[:1024], expected=401, valid_token=False)
    wrong_project = bytearray(app[:512])
    # ESP image+segment headers (32) + esp_app_desc_t project_name offset (48).
    wrong_project[80:112] = b"unrelated-project".ljust(32, b"\0")
    reject("wrong_p4_project", "/api/firmware", wrong_project)
    reject("pico_digest_mismatch", "/api/pico/firmware", pico, digest="0" * 64)
    wrong_family = bytearray(pico)
    offset = 512 if int.from_bytes(pico[8:12], "little") == 0xa000 else 0
    wrong_family[offset+28:offset+32] = b"\xff" * 4
    reject("wrong_pico_family", "/api/pico/firmware", wrong_family)
    reject("truncated_uf2", "/api/pico/firmware", pico[:-1])
    reject("invalid_configuration", "/api/config", b'{"device_instance":75151,"name":"Forbidden"}')
    after = status()
    assert after["uptime_seconds"] >= before["uptime_seconds"], "Gateway rebooted"
    for key in ("config", "version", "elf_sha256", "ota"):
        assert after[key] == before[key], key
    assert after["pico"]["qualified"]
    assert after["pico"]["connections"] == before["pico"]["connections"], "Pico was reset by a rejected upload"
    report = {"passed": True, "checks": checks, "configuration_and_boot_unchanged": True,
              "pico_connection_unchanged": True}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
