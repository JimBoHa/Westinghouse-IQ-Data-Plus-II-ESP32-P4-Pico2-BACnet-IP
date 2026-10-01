#!/usr/bin/env python3
"""Read-only hardware/browser acceptance for retained meter diagnostics.

Does not enable polling, restart, flash, or initiate meter transactions. Keep
the output private: it contains raw protocol evidence already retained in RAM.
"""
import argparse
import json
import os
from pathlib import Path
import sys
from playwright.sync_api import sync_playwright, expect
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
from gateway_client import Gateway
from test_dashboard import browser_pin


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("host", "expected-mac", "token-file", "pin-file", "output"):
        parser.add_argument("--" + name, required=True)
    parser.add_argument("--require-traces", action="store_true")
    parser.add_argument("--browser-executable", default="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
    args = parser.parse_args()
    os.umask(0o077)
    gateway = Gateway(args.host, args.pin_file, args.token_file, args.expected_mac)
    before = gateway.status()
    code, _, _ = gateway.raw("/api/diagnostics", b"")
    assert code == 401, "Unauthenticated diagnostics were not rejected"
    snapshot = gateway.request("/api/diagnostics", b"", authenticated=True)
    meter = snapshot["meter_transactions"]
    assert meter["schema"] == 1 and meter["capacity"] == 8
    assert snapshot["boot_id"] == before["boot_id"]
    assert snapshot["source_revision"] == before["source_revision"]
    assert len(meter["entries"]) == min(meter["total"], 8)
    if args.require_traces:
        assert meter["entries"], "No retained transactions to inspect"
    for entry in meter["entries"]:
        trace = entry["event_trace"]
        assert len(trace["events"]) <= 64 and trace["total"] - len(trace["events"]) == trace["omitted"]
        assert isinstance(entry["buffer_accepted"], bool)
        if entry["terminal_received"]:
            assert entry["pico_reported"]["stop_code"] is not None
        if not entry["buffer_accepted"]:
            assert entry["error"]
    errors, paths = [], []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=args.browser_executable, headless=True,
            args=[f"--ignore-certificate-errors-spki-list={browser_pin(gateway)}"])
        context = browser.new_context(viewport={"width":1280,"height":1080}, accept_downloads=True)
        page = context.new_page()
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.on("request", lambda request: paths.append(request.url.split("/", 3)[-1]))
        page.goto(f"https://{args.host}/", wait_until="networkidle")
        expect(page.locator("#connection")).to_have_text("Connected", timeout=20000)
        expect(page.locator("#refresh-diagnostics")).to_be_disabled()
        page.locator("#key-file").set_input_files(args.token_file)
        expect(page.locator("#auth-state")).to_contain_text("Unlocked", timeout=20000)
        page.locator("[data-view=maintenance]").click()
        page.locator("#refresh-diagnostics").click()
        expect(page.locator("#diagnostic-state")).to_contain_text("reads this boot", timeout=20000)
        if args.require_traces:
            expect(page.locator("#meter-traces>details").first).to_be_visible()
            expect(page.locator("#meter-traces")).to_contain_text("Requests clocked:")
        page.set_viewport_size({"width":390,"height":844})
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth"), "Mobile horizontal overflow"
        page.screenshot(path=str(Path(args.output).with_suffix(".mobile.png")), full_page=True)
        page.set_viewport_size({"width":1280,"height":1080})
        page.screenshot(path=str(Path(args.output).with_suffix(".desktop.png")), full_page=True)
        with page.expect_download(timeout=20000) as downloaded:
            page.locator("#download").click()
        exported = json.loads(Path(downloaded.value.path()).read_text())
        assert exported["meter_transactions"]["schema"] == 1 and exported["boot_id"] == before["boot_id"]
        assert page.evaluate("localStorage.length === 0 && sessionStorage.length === 0 && document.cookie === ''")
        page.locator("#forget").click()
        expect(page.locator("#refresh-diagnostics")).to_be_disabled()
        assert not errors, errors
        assert not any(path in {"api/config", "api/reboot", "api/firmware", "api/pico/firmware"} for path in paths)
        context.close();browser.close()
    after = gateway.status()
    assert after["config"] == before["config"] and after["boot_id"] == before["boot_id"]
    report = {"passed":True, "configuration_unchanged":True, "boot_unchanged":True,
              "unauthenticated_rejected":True, "browser_refresh_download_and_mobile_layout":True,
              "snapshot":exported}
    Path(args.output).write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps({key:value for key,value in report.items() if key != "snapshot"}))


if __name__ == "__main__":
    main()
