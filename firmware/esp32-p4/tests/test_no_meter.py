#!/usr/bin/env python3
"""Physical no-meter timeout/load test. Restores commissioning in a finally block."""
import argparse
import ipaddress
import json
from pathlib import Path
import subprocess
import sys
import time
import urllib.error
import urllib.request


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", required=True, type=ipaddress.IPv4Address)
    parser.add_argument("--expected-mac", required=True)
    parser.add_argument("--token-file", required=True, type=Path)
    parser.add_argument("--client-address", required=True)
    parser.add_argument("--pico-uf2", type=Path, help="Also test an Ethernet Pico update during meter-fault backoff")
    parser.add_argument("--meter-disconnected", action="store_true", required=True,
                        help="Confirm that meter signal wiring is physically absent")
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    if str(args.target) == "192.168.75.151" or not args.target.is_private:
        parser.error("Only an identified private development gateway is allowed")
    if args.token_file.stat().st_mode & 0o077:
        parser.error("Token file must have mode 0600")
    token = args.token_file.read_text().strip()
    assert len(token) == 64 and all(c in "0123456789abcdef" for c in token)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def request(path, value=None):
        data = json.dumps(value).encode() if value is not None else None
        headers = {"Authorization": "Bearer " + token, "Content-Type": "application/json"} if data else {}
        req = urllib.request.Request(f"http://{args.target}{path}", data=data, headers=headers)
        with opener.open(req, timeout=5) as response:
            return json.load(response)

    def status():
        s = request("/api/status")
        assert s["project"] == "iqdata_p4_gateway"
        assert s["ethernet_mac"].lower() == args.expected_mac.lower()
        assert s["config"]["device_instance"] != 75151
        return s

    def wait_config(config):
        until = time.monotonic() + 65
        while time.monotonic() < until:
            try:
                s = status()
                if s["config"] == config and not s["restarting"] and s["ota"]["image_state"] == 2:
                    return s
            except (OSError, urllib.error.URLError):
                pass
            time.sleep(1)
        raise RuntimeError("Expected commissioned configuration did not return")

    before = status()
    original = before["config"]
    assert not original["poll_enabled"], "Start with commissioning polling disabled"
    assert before["pico"]["qualified"], "Pico identity must already be qualified"
    report = {"passed": False, "samples": []}
    child = None
    args.output.parent.mkdir(parents=True, exist_ok=True)
    try:
        request("/api/config", original | {"poll_enabled": True})
        start = wait_config(original | {"poll_enabled": True})
        test = Path(__file__).with_name("test_bacnet.py")
        with args.output.with_suffix(".bacnet.log").open("w") as log:
            child = subprocess.Popen([sys.executable, str(test), "--address", args.client_address,
                "--instance", "75298", "--target", str(args.target), "--device", str(original["device_instance"]),
                "--broadcast", "--no-meter", "--load-seconds", "60", "--output",
                str(args.output.with_suffix(".bacnet.json"))], stdout=log, stderr=subprocess.STDOUT)
            until = time.monotonic() + 150
            previous = start["uptime_seconds"]
            while child.poll() is None and time.monotonic() < until:
                s = status()
                assert s["uptime_seconds"] >= previous, "Unexpected reboot during load"
                previous = s["uptime_seconds"]
                assert s["bacnet"]["heartbeat_age_seconds"] < 2
                assert s["internal_free_heap"] > 32768
                report["samples"].append({key: s[key] for key in
                    ("uptime_seconds", "free_heap", "internal_free_heap", "pico", "bacnet")})
                points = request("/api/points")
                report["samples"][-1]["poll"] = {p["key"]: p for p in points
                    if p["key"] in ("POLL_ATTEMPTS", "POLL_LAST_STOP", "POLL_FAILURES")}
                if args.pico_uf2 and not report.get("pico_update_during_faults") and s["pico"]["qualified"] and s["pico"]["failures"] >= 3:
                    manager = Path(__file__).resolve().parents[1] / "tools/manage.py"
                    subprocess.run([sys.executable, str(manager), "--host", str(args.target),
                        "--expected-mac", args.expected_mac, "--token-file", str(args.token_file),
                        "pico-update", str(args.pico_uf2)], check=True, timeout=155)
                    report["pico_update_during_faults"] = True
                time.sleep(2)
            assert child.poll() == 0, "BACnet load test failed or exceeded deadline"
        assert any(s["pico"]["failures"] for s in report["samples"]), "No physical timeout was observed"
        assert max(s["poll"]["POLL_ATTEMPTS"]["value"] for s in report["samples"]) >= 3, "No repeated bounded meter attempts"
        assert any(s["pico"]["qualified"] for s in report["samples"]), "Pico did not retain/recover identity"
        if args.pico_uf2:
            assert report.get("pico_update_during_faults"), "Firmware update during backoff was not exercised"
        report["passed"] = True
    finally:
        if child and child.poll() is None:
            child.terminate()
            child.wait(timeout=5)
        try:
            request("/api/config", original)
            restored = wait_config(original)
            report["polling_restored_off"] = not restored["config"]["poll_enabled"]
        finally:
            args.output.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps({"passed": report["passed"], "polling_restored_off": report["polling_restored_off"],
                      "status_samples": len(report["samples"])}, indent=2))


if __name__ == "__main__":
    main()
