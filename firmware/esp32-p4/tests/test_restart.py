#!/usr/bin/env python3
"""Observe a real signed OTA boot using an independent BACnet decoder."""
import asyncio
import json
from pathlib import Path
import sys
import time
from bacpypes3.argparse import SimpleArgumentParser
from bacpypes3.app import Application
from bacpypes3.apdu import ErrorRejectAbortNack
from bacpypes3.basetypes import TimeStamp, RestartReason, DeviceStatus
from bacpypes3.pdu import Address
from bacpypes3.primitivedata import ObjectIdentifier
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
from gateway_client import Gateway, image_signature


class Observer(Application):
    async def do_UnconfirmedCOVNotificationRequest(self, apdu):
        await self.notifications.put(apdu)


async def run(args):
    gateway = Gateway(args.host, args.pin_file, args.token_file, args.expected_mac)
    before = await asyncio.to_thread(gateway.status)
    device_id = before["config"]["device_instance"]
    target = Address(gateway.address)
    device = ObjectIdentifier(("device", device_id))
    app = Observer.from_args(args)
    app.notifications = asyncio.Queue()
    report = {"target": gateway.address, "device": device_id, "checks": {}}
    checks = report["checks"]
    try:
        image, headers = image_signature(args.app_bin, "esp32p4")
        await asyncio.sleep(1)
        await asyncio.to_thread(gateway.request, "/api/firmware", image,
                                authenticated=True, headers=headers, timeout=150)
        deadline = time.monotonic() + 90
        while True:
            notice = await asyncio.wait_for(app.notifications.get(), deadline-time.monotonic())
            if notice.pduSource == target and notice.initiatingDeviceIdentifier == device:
                break
        assert notice.monitoredObjectIdentifier == device
        assert notice.subscriberProcessIdentifier == 0 and notice.timeRemaining == 0
        values = {str(item.propertyIdentifier): item.value for item in notice.listOfValues}
        types = {"system-status": DeviceStatus, "time-of-device-restart": TimeStamp,
                 "last-restart-reason": RestartReason}
        assert set(values) == set(types), values
        for name, datatype in types.items():
            notified = values[name].cast_out(datatype)
            current = await app.read_property(target, device, name)
            if name == "time-of-device-restart":
                assert notified.dateTime == current.dateTime, (notified, current)
            else:
                assert notified == current, (notified, current)
            checks[name] = str(current.dateTime if name == "time-of-device-restart" else current)
        recipients = await app.read_property(target, device, "restart-notification-recipients")
        assert len(recipients) == 1 and recipients[0].address.networkNumber == 0
        assert bytes(recipients[0].address.macAddress) == b""
        try:
            await app.write_property(target, device, "restart-notification-recipients", recipients)
        except ErrorRejectAbortNack as error:
            assert "write-access-denied" in str(error), error
        else:
            raise AssertionError("Recipient write was accepted")
        checks["default_local_broadcast_read_only"] = True
        after = await asyncio.to_thread(gateway.status)
        assert after["elf_sha256"] == image[176:208].hex()
        assert after["ota"]["running_slot"] != before["ota"]["running_slot"]
        assert after["ota"]["image_state"] == 2 and after["ota"]["startup_health"]["accepted"]
        restart = after["bacnet"]["restart_notification"]
        assert restart["timestamp_frozen"] and restart["sent"] == 1 and restart["failures"] == 0
        assert after["config"] == before["config"]
        await asyncio.sleep(5)
        while not app.notifications.empty():
            extra = app.notifications.get_nowait()
            assert extra.pduSource != target or extra.initiatingDeviceIdentifier != device, "Duplicate restart notification"
        checks["single_notification_and_exact_healthy_image"] = after["elf_sha256"]
        checks["timestamp_source"] = restart["timestamp_source"]
        report["passed"] = True
    except BaseException as error:
        report.update(passed=False, error=repr(error));raise
    finally:
        app.close()
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, indent=2) + "\n")
        print(json.dumps(report, indent=2))


if __name__ == "__main__":
    parser = SimpleArgumentParser()
    parser.add_argument("--host", required=True)
    parser.add_argument("--expected-mac", required=True)
    parser.add_argument("--pin-file", type=Path, required=True)
    parser.add_argument("--token-file", type=Path, required=True)
    parser.add_argument("--app-bin", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    asyncio.run(run(parser.parse_args()))
