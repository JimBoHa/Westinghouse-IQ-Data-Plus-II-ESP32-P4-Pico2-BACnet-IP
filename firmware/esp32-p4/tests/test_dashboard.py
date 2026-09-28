#!/usr/bin/env python3
"""Physical browser acceptance test. Verifies the TLS pin before trusting its SPKI.

Requires playwright==1.55.0 and Chrome (or --browser-executable). No trace,
HAR, cookies, private key, or page storage is exported. Meter must stay absent.
"""
import argparse
import base64
import hashlib
import json
from pathlib import Path
import socket
import ssl
import subprocess
import sys
from playwright.sync_api import sync_playwright, expect
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
from gateway_client import Gateway, image_signature


def browser_pin(gateway):
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False; context.verify_mode = ssl.CERT_NONE
    with socket.create_connection((gateway.address, 443), timeout=10) as raw:
        with context.wrap_socket(raw, server_hostname=gateway.address) as tls:
            certificate = tls.getpeercert(binary_form=True)
    assert hashlib.sha256(certificate).hexdigest() == gateway.pin["certificate_sha256"]
    public = subprocess.check_output(["openssl", "x509", "-inform", "DER", "-pubkey", "-noout"], input=certificate)
    der = subprocess.check_output(["openssl", "pkey", "-pubin", "-outform", "DER"], input=public)
    return base64.b64encode(hashlib.sha256(der).digest()).decode()


def run(args):
    if not args.meter_disconnected:
        raise ValueError("Confirm disconnected meter for this hardware acceptance run")
    gateway = Gateway(args.host, args.pin_file, args.token_file, args.expected_mac)
    before = gateway.status()
    assert not before["config"]["poll_enabled"]
    spki = browser_pin(gateway)
    for image, target in ((args.app_bin, "esp32p4"), (args.pico_uf2, "pico2")):
        image_signature(image, target)
    report = {"target": gateway.address, "checks": {}}
    checks = report["checks"]
    args.output.parent.mkdir(parents=True, exist_ok=True)
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(executable_path=str(args.browser_executable),
                headless=True, args=[f"--ignore-certificate-errors-spki-list={spki}",
                    f"--host-resolver-rules=MAP {args.host} {gateway.address}"])
            context = browser.new_context(viewport={"width":1280,"height":1080}, accept_downloads=True)
            page = context.new_page();errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.on("dialog", lambda dialog: dialog.accept())
            response = page.goto(f"https://{args.host}/", wait_until="networkidle")
            assert response.status == 200 and "frame-ancestors 'none'" in response.headers["content-security-policy"]
            expect(page.locator("#connection")).to_have_text("Connected", timeout=20000)
            for asset in ("dashboard.js", "dashboard.css"):
                served = page.evaluate("async name => (await fetch('/'+name)).text()", asset)
                assert served == (Path(__file__).resolve().parents[1]/"main/web"/asset).read_text()
            expect(page.locator("#review-config")).to_be_disabled()
            page.screenshot(path=str(args.output.with_suffix(".desktop.png")), full_page=True)
            page.set_viewport_size({"width":390,"height":844})
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
            page.screenshot(path=str(args.output.with_suffix(".mobile.png")), full_page=True)
            page.set_viewport_size({"width":1280,"height":1080})
            checks["responsive_overview_and_locked_controls"] = True
            page.locator("[data-view=points]").click()
            expect(page.locator("#point-rows tr")).to_have_count(198)
            page.locator("#quality").select_option("invalid")
            assert page.locator("#point-rows tr").count() > 150
            assert all(value == "Unavailable" for value in page.locator("#point-rows td:nth-child(3)").all_text_contents())
            page.locator("#search").fill("Phase A-B Voltage")
            assert page.locator("#point-rows tr").count() < 198
            page.locator("#search").fill("")
            checks["198_points_filters_and_invalid_values_hidden"] = True
            page.locator("#key-file").set_input_files({"name":"wrong.key","mimeType":"text/plain","buffer":b"0"*64})
            expect(page.locator("#notice")).to_contain_text("401", timeout=20000)
            expect(page.locator("#auth-state")).to_contain_text("Locked")
            page.locator("#key-file").set_input_files(str(args.token_file))
            expect(page.locator("#auth-state")).to_contain_text("Unlocked", timeout=20000)
            assert page.evaluate("localStorage.length === 0 && sessionStorage.length === 0 && document.cookie === ''")
            assert page.locator("#key-file").input_value() == ""
            checks["browser_hmac_wrong_key_rejection_and_no_key_storage"] = True
            page.locator("[data-view=settings]").click()
            page.locator("[name=device_instance]").fill("75151")
            page.locator("#review-config").click()
            expect(page.locator("#notice")).to_contain_text("protected")
            assert not page.locator("#config-review").is_visible()
            page.locator("[name=device_instance]").fill(str(before["config"]["device_instance"]))
            page.locator("#review-config").click()
            expect(page.locator("#config-review")).to_be_visible()
            page.locator("#apply-config").click()
            expect(page.locator("#notice")).to_have_text("Settings saved. Restart and configuration verified.",timeout=110000)
            checks["protected_settings_and_same_config_restart"] = True
            page.locator("[data-view=maintenance]").click()
            with page.expect_download(timeout=20000) as downloaded:
                page.locator("#download").click()
            snapshot = json.loads(Path(downloaded.value.path()).read_text())
            assert "events" in snapshot and snapshot["boot_id"]
            checks["authenticated_diagnostic_download"] = True
            page.locator("#image-file").set_input_files(str(args.pico_uf2))
            page.locator("#signature-file").set_input_files(str(args.pico_uf2)+".sig.json")
            page.locator("#upload").click()
            expect(page.locator("#notice")).to_contain_text("does not match",timeout=20000)
            page.locator("#target").select_option("pico2")
            page.locator("#upload").click()
            expect(page.locator("#update-result")).to_contain_text("Pico 2 update verified.",timeout=180000)
            checks["wrong_target_blocked_and_signed_pico_update"] = True
            page.locator("#target").select_option("esp32p4")
            page.locator("#image-file").set_input_files(str(args.app_bin))
            page.locator("#signature-file").set_input_files(str(args.app_bin)+".sig.json")
            page.locator("#upload").click()
            expect(page.locator("#update-result")).to_have_text("ESP32-P4 update verified. Exact image is running and healthy.",timeout=180000)
            checks["signed_p4_update_exact_healthy_boot"] = True
            page.locator("#reboot").click()
            expect(page.locator("#notice")).to_have_text("Gateway restart verified.",timeout=110000)
            page.locator("#forget").click()
            expect(page.locator("#auth-state")).to_contain_text("Locked")
            expect(page.locator("#reboot")).to_be_disabled()
            page.reload(wait_until="networkidle")
            expect(page.locator("#auth-state")).to_contain_text("Locked")
            checks["authenticated_reboot_lock_and_reload"] = True
            assert not errors, errors
            browser.close()
        after = gateway.status()
        assert after["config"] == before["config"] and after["pico"]["qualified"]
        report["passed"] = True
    except BaseException as error:
        report.update(passed=False, error=repr(error));raise
    finally:
        args.output.write_text(json.dumps(report, indent=2)+"\n")
        print(json.dumps(report, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("host", "expected-mac"):
        parser.add_argument("--"+name, required=True)
    for name in ("pin-file", "token-file", "app-bin", "pico-uf2", "output"):
        parser.add_argument("--"+name, type=Path, required=True)
    parser.add_argument("--meter-disconnected", action="store_true")
    parser.add_argument("--browser-executable",type=Path,default=Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"))
    run(parser.parse_args())
