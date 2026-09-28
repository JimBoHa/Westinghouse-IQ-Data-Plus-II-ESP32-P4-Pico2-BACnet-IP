#!/usr/bin/env python3
"""Independent bacpypes3 client for native loopback and the development P4."""
import asyncio
import csv
import json
import ipaddress
import time
from pathlib import Path
from bacpypes3.argparse import SimpleArgumentParser
from bacpypes3.app import Application
from bacpypes3.apdu import ErrorRejectAbortNack
from bacpypes3.basetypes import ErrorType
from bacpypes3.pdu import Address, GlobalBroadcast
from bacpypes3.primitivedata import ObjectIdentifier

ROOT = Path(__file__).resolve().parents[3]


async def run(args):
    if args.device == 75151 or args.target.split(":")[0] == "192.168.75.151":
        raise ValueError("The working production gateway is protected")
    app = Application.from_args(args)
    target, device = Address(args.target), ObjectIdentifier(("device", args.device))
    report = {"target": args.target, "device": args.device, "checks": {}}
    checks = report["checks"]
    try:
        for label, address in [("unicast_discovery", target)] + ([("broadcast_discovery", GlobalBroadcast())] if args.broadcast else []):
            found = await app.who_is(args.device, args.device, address=address, timeout=2)
            assert any(item.iAmDeviceIdentifier == device and item.pduSource == target for item in found), label
            checks[label] = True
        assert not await app.who_is(args.device + 1, args.device + 1, address=target, timeout=1)
        checks["who_is_range_filter"] = True
        objects = await app.read_property(target, device, "object-list")
        assert len(objects) == 200, len(objects)
        assert await app.read_property(target, device, "object-list", array_index=0) == 200
        for index, obj in enumerate(objects, 1):
            assert await app.read_property(target, device, "object-list", array_index=index) == obj
        rows = [r for r in csv.DictReader((ROOT / "live/BACNET_POINT_MAP.csv").open()) if r["is_point"] == "true"]
        expected = {(int(r["object_type_code"]), int(r["object_instance"])) for r in rows}
        actual = {(int(obj[0]), int(obj[1])) for obj in objects if int(obj[0]) in (0, 3)}
        assert actual == expected
        assert sum(obj[0] == 0 for obj in actual) == 106
        assert sum(obj[0] == 3 for obj in actual) == 92
        checks["object_catalog_and_indexed_list"] = 200
        for row in rows:
            obj = ObjectIdentifier((row["object_type"], int(row["object_instance"])))
            names = ["object-name", "description", "present-value", "status-flags", "reliability", "out-of-service"]
            if row["object_type"] == "analog-input":
                names += ["units", "cov-increment"]
            values = await app.read_property_multiple(target, [obj, names])
            properties = {str(prop): value for _, prop, _, value in values}
            assert all(not isinstance(value, ErrorType) for value in properties.values()), (obj, properties)
            assert str(properties["object-name"]) == row["object_name"], obj
            assert str(properties["description"]) == row["description"], obj
            assert not properties["out-of-service"], obj
            if row["object_type"] == "analog-input":
                assert int(properties["units"]) == int(row["units_code"]), obj
                assert abs(float(properties["cov-increment"]) - float(row["cov_increment"])) < 1e-5, obj
            # Every metered point is faulted until a fresh, qualified read exists.
            if args.no_meter and row["group"] in ("live", "flags", "settings", "trip", "derived"):
                assert int(properties["reliability"]) == 12, (obj, properties)
                assert properties["status-flags"][1], (obj, properties)
        checks["all_198_point_metadata_and_quality"] = True
        network_ports = [obj for obj in objects if int(obj[0]) == 56]
        assert len(network_ports) == 1
        network_port = network_ports[0]
        port_props = await app.read_property_multiple(target, [network_port, [
            "object-name", "network-type", "protocol-level", "network-number",
            "network-number-quality", "mac-address", "link-speed", "changes-pending",
            "bacnet-ip-mode", "ip-address", "bacnet-ip-udp-port", "ip-subnet-mask",
            "ip-default-gateway", "ip-dhcp-enable", "status-flags", "reliability"]])
        port = {str(prop): value for _, prop, _, value in port_props}
        assert all(not isinstance(value, ErrorType) for value in port.values()), port
        assert str(port["object-name"]) == "IQData-Ethernet"
        assert int(port["network-type"]) == 5  # ipv4
        assert int(port["protocol-level"]) == 2  # bacnet-application
        assert not port["changes-pending"]
        assert int(port["bacnet-ip-mode"]) == 0  # normal
        target_ip = ipaddress.IPv4Address(args.target.split(":")[0]).packed
        target_port = int(args.target.split(":")[1]) if ":" in args.target else 47808
        assert bytes(port["ip-address"]) == target_ip
        assert int(port["bacnet-ip-udp-port"]) == target_port
        assert bytes(port["mac-address"]) == target_ip + target_port.to_bytes(2, "big")
        assert int(port["reliability"]) == 0
        checks["network_port_properties"] = {key: str(value) for key, value in port.items()}
        multi = await app.read_property_multiple(target, [device, ["object-name", "protocol-version", "protocol-revision"], ObjectIdentifier("analog-input,1"), ["present-value", "status-flags"], ObjectIdentifier("binary-input,1"), ["present-value", "status-flags"]])
        assert len(multi) == 7 and all(not isinstance(value, ErrorType) for _, _, _, value in multi)
        checks["multiple_object_rpm"] = True
        for obj, prop in ((device, "object-name"), (ObjectIdentifier("analog-input,1"), "present-value"), (ObjectIdentifier("analog-input,1"), "out-of-service"), (ObjectIdentifier("binary-input,1"), "present-value"), (network_port, "bacnet-ip-udp-port")):
            old = await app.read_property(target, obj, prop)
            try:
                await app.write_property(target, obj, prop, old)
            except ErrorRejectAbortNack as error:
                assert "write-access-denied" in str(error), str(error)
            else:
                raise AssertionError(f"Write accepted: {obj} {prop}")
        checks["read_only_writes_denied"] = 5
        for confirmed in (False, True):
            async with app.change_of_value(target, ObjectIdentifier("analog-input,1"), issue_confirmed_notifications=confirmed, lifetime=4) as subscription:
                subscription.refresh_subscription_handle.cancel()
                props = {}
                while len(props) < 2:
                    prop, value = await asyncio.wait_for(subscription.get_value(), timeout=3)
                    props[str(prop)] = str(value)
                assert "present-value" in props and "status-flags" in props, props
                active = await app.read_property(target, device, "active-cov-subscriptions")
                assert len(active) >= 1
                await asyncio.sleep(5)
                active = await app.read_property(target, device, "active-cov-subscriptions")
                assert len(active) == 0, active
                checks["confirmed_cov_and_expiry" if confirmed else "unconfirmed_cov_and_expiry"] = props
        if args.native_changes:
            assert args.target.startswith("127.0.0.1:"), "Synthetic transitions exist only in the native server"
            async with app.change_of_value(target, ObjectIdentifier("analog-input,1"), lifetime=20) as subscription:
                seen = {"present-value": set(), "status-flags": set()}
                deadline = time.monotonic() + 10
                while time.monotonic() < deadline and any(len(values) < 2 for values in seen.values()):
                    prop, value = await asyncio.wait_for(subscription.get_value(), timeout=6)
                    if str(prop) in seen:
                        seen[str(prop)].add(str(value))
                assert all(len(values) >= 2 for values in seen.values()), seen
                checks["native_value_and_quality_cov_transitions"] = {key: sorted(value) for key, value in seen.items()}
        if args.load_seconds:
            assert 1 <= args.load_seconds <= 300
            count = 0
            deadline = time.monotonic() + args.load_seconds
            while time.monotonic() < deadline:
                value = await app.read_property(target, device, "object-name")
                assert str(value)
                count += 1
            checks["load_read_count"] = count
            checks["load_seconds"] = args.load_seconds
        report["passed"] = True
    except BaseException as error:
        report["passed"] = False
        report["error"] = repr(error)
        raise
    finally:
        app.close()
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, indent=2) + "\n")
        print(json.dumps(report, indent=2))


if __name__ == "__main__":
    parser = SimpleArgumentParser()
    parser.add_argument("--target", required=True)
    parser.add_argument("--device", type=int, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--broadcast", action="store_true")
    parser.add_argument("--no-meter", action="store_true")
    parser.add_argument("--native-changes", action="store_true")
    parser.add_argument("--load-seconds", type=int, default=0)
    asyncio.run(run(parser.parse_args()))
