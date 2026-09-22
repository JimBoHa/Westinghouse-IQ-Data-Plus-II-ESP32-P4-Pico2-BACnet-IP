#!/usr/bin/env python3
"""Bounded Ethernet verification; does not change meter configuration."""
import asyncio
import json
from pathlib import Path
from bacpypes3.argparse import SimpleArgumentParser
from bacpypes3.app import Application
from bacpypes3.apdu import ErrorRejectAbortNack
from bacpypes3.basetypes import ErrorType
from bacpypes3.pdu import Address, GlobalBroadcast
from bacpypes3.primitivedata import ObjectIdentifier
from host import utc


async def run(args):
    app = Application.from_args(args)
    target = Address(args.target)
    device = ObjectIdentifier(("device", args.device))
    report = {"started_utc": utc(), "target": args.target, "checks": {}}
    try:
        for name, address in (("broadcast_discovery", GlobalBroadcast()), ("unicast_discovery", target)):
            found = await app.who_is(args.device, args.device, address=address, timeout=3)
            matches = [str(item.pduSource) for item in found if item.iAmDeviceIdentifier == device]
            assert str(target) in matches, (name, matches)
            report["checks"][name] = matches
        found = await app.who_is(args.device + 1, args.device + 1, address=target, timeout=2)
        assert not found, "out-of-range Who-Is returned a device"
        report["checks"]["excluded_range"] = "no reply"
        objects = await app.read_property(target, device, "object-list")
        count = await app.read_property(target, device, "object-list", array_index=0)
        assert count == len(objects)
        for index, obj in enumerate(objects, 1):
            assert await app.read_property(target, device, "object-list", array_index=index) == obj
        report["object_list"] = [str(obj) for obj in objects]
        report["checks"]["indexed_object_list"] = len(objects)
        parameters = [device, ["object-name", "description", "vendor-name", "vendor-identifier",
                               "model-name", "application-software-version", "protocol-version",
                               "protocol-revision", "protocol-services-supported", "protocol-object-types-supported",
                               "segmentation-supported", "max-apdu-length-accepted", "database-revision"]]
        for obj in objects:
            if int(obj[0]) in (0, 3):
                props = ["object-name", "description", "present-value", "status-flags", "reliability", "out-of-service"]
                if int(obj[0]) == 0:
                    props.append("units")
                parameters.extend([obj, props])
        # Bound request batches so large point maps do not exceed a peer's
        # advertised segmentation window. Metasys likewise reads per object.
        response = []
        for offset in range(0, len(parameters), 20):
            response.extend(await app.read_property_multiple(target, parameters[offset:offset+20]))
        for obj, prop, index, value in response:
            assert not isinstance(value, ErrorType), (obj, prop, str(value))
        report["properties"] = [{"object": str(obj), "property": str(prop), "index": index,
                                  "value": str(value)} for obj, prop, index, value in response]
        report["checks"]["read_property_multiple"] = len(response)
        name = await app.read_property(target, device, "object-name")
        try:
            # Same-name write proves access control without proposing a new value.
            await app.write_property(target, device, "object-name", name)
            raise AssertionError("read-only Device unexpectedly accepted a write")
        except ErrorRejectAbortNack as error:
            assert "write-access-denied" in str(error), str(error)
            report["checks"]["write_protection"] = str(error)
        cov_values = []
        async with app.change_of_value(target, ObjectIdentifier("analog-input,4"),
                                       issue_confirmed_notifications=True, lifetime=30) as subscription:
            deadline = asyncio.get_running_loop().time() + 12
            while asyncio.get_running_loop().time() < deadline:
                try:
                    prop, value = await asyncio.wait_for(subscription.get_value(),
                        max(.01, deadline - asyncio.get_running_loop().time()))
                except asyncio.TimeoutError:
                    break
                cov_values.append({"utc": utc(), "property": str(prop), "value": str(value)})
                if len({v["value"] for v in cov_values if v["property"] == "present-value"}) >= 2:
                    break
        assert any(v["property"] == "present-value" for v in cov_values), "no COV initial value"
        report["checks"]["confirmed_cov"] = cov_values
        report["passed"] = True
    except Exception as error:
        report["passed"] = False
        report["error"] = repr(error)
        raise
    finally:
        report["finished_utc"] = utc()
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, indent=2) + "\n")
        print(json.dumps({k: v for k, v in report.items() if k != "properties"}, indent=2))
        app.close()


if __name__ == "__main__":
    parser = SimpleArgumentParser()
    parser.add_argument("--target", required=True, help="Gateway IPv4 address, optionally with :UDP-port")
    parser.add_argument("--device", type=int, default=75151)
    parser.add_argument("--output", type=Path, required=True)
    asyncio.run(run(parser.parse_args()))
