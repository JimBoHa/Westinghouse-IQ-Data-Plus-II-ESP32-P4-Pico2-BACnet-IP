#!/usr/bin/env python3
"""Read-only BACnet/IP gateway for the existing IQ Data Plus II recorder.

Reads the recorder's atomic state file. Never opens USB or initiates meter polls.
"""
import argparse
import asyncio
import json
import math
import time
from pathlib import Path

from bacpypes3.app import Application
from bacpypes3.basetypes import ObjectTypesSupported, ServicesSupported
from bacpypes3.errors import ExecutionError
from bacpypes3.local.analog import AnalogInputObject
from bacpypes3.local.binary import BinaryInputObject
from bacpypes3.local.device import DeviceObject
from bacpypes3.local.networkport import NetworkPortObject
from bacpypes3.pdu import Address, GlobalBroadcast

from host import atomic_json, saved_json, telemetry, utc
from bacnet_extras import PersistentCOVMixin, build_extra_objects, refresh_extra_objects
from decode_diagnostics import FIELD_SPECS as METER_DIAGNOSTIC_SPECS
from live_diagnostics import DiagnosticAccumulator, FIELD_SPECS as DERIVED_DIAGNOSTIC_SPECS

VERSION = "1.1.0"


def announce(app, destination=None):
    """Normal subnet discovery plus an optional explicitly configured peer."""
    app.i_am(address=GlobalBroadcast())
    if destination:
        app.i_am(address=Address(destination))


# Stable BACnet point identifiers. Multiply source values by scale for BACnet units.
POINTS = [
    (1, "VAB", "Voltage-AB", "volts", 1, "Phase A-B RMS voltage"),
    (2, "VBC", "Voltage-BC", "volts", 1, "Phase B-C RMS voltage"),
    (3, "VCA", "Voltage-CA", "volts", 1, "Phase C-A RMS voltage"),
    (4, "IA", "Current-A", "amperes", 1, "Phase A RMS current"),
    (5, "IB", "Current-B", "amperes", 1, "Phase B RMS current"),
    (6, "IC", "Current-C", "amperes", 1, "Phase C RMS current"),
    (7, "VAN", "Voltage-AN", "volts", 1, "Phase A-neutral RMS voltage"),
    (8, "VBN", "Voltage-BN", "volts", 1, "Phase B-neutral RMS voltage"),
    (9, "VCN", "Voltage-CN", "volts", 1, "Phase C-neutral RMS voltage"),
    (10, "P_W", "Real-Power", "kilowatts", .001, "Meter real power"),
    (11, "Q_var", "Reactive-Power", "kilovoltAmperesReactive", .001,
     "Meter reactive power; positive W with negative var means inductive/lagging"),
    (12, "PF", "Power-Factor", "powerFactor", 1,
     "Meter PF: negative=lagging, positive=leading; calculation mode unverified"),
    (13, "FREQUENCY_Hz", "Frequency", "hertz", 1, "Meter frequency"),
    (14, "DEMAND_W", "Demand", "kilowatts", .001, "Meter demand"),
    (15, "ENERGY_kWh", "Energy", "kilowattHours", 1, "Meter unsigned 24-bit energy counter"),
    (16, "ENERGY_Wh", "Energy-Coarse", "kilowattHours", .001,
     "Meter scaled energy buffer; lower precision than Energy"),
    (17, "S_PQ_estimate_VA", "Apparent-Power-PQ-Estimate", "kilovoltAmperes", .001,
     "Estimate sqrt(P^2+Q^2); excludes separate distortion power"),
    (18, "PF_PQ_magnitude", "Power-Factor-PQ-Estimate", "powerFactor", 1,
     "Estimate abs(P)/sqrt(P^2+Q^2); not a THD measurement"),
]


class MeterDevice(DeviceObject):
    @property
    def protocolObjectTypesSupported(self):
        return ObjectTypesSupported([ObjectTypesSupported.analogInput, ObjectTypesSupported.binaryInput,
                                     ObjectTypesSupported.device, ObjectTypesSupported.networkPort])


class MeterApplication(PersistentCOVMixin, Application):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.diagnostics = {"started_utc": utc(), "who_is_count": 0,
                            "read_property_count": 0, "read_property_multiple_count": 0,
                            "clients": {}, "duplicate_instance": None}

    def get_services_supported(self):
        # BACpypes3 0.0.108 maps unconfirmed service choices through the
        # confirmed enum during introspection. Publish this gateway's actual
        # supported server services explicitly instead of those incorrect bits.
        return ServicesSupported([
            ServicesSupported.readProperty, ServicesSupported.readPropertyMultiple,
            ServicesSupported.subscribeCOV, ServicesSupported.whoIs, ServicesSupported.iAm,
            ServicesSupported.whoHas, ServicesSupported.iHave,
        ])

    def seen(self, service, apdu):
        self.diagnostics[service + "_count"] += 1
        client = str(apdu.pduSource)
        clients = self.diagnostics["clients"]
        if client not in clients and len(clients) >= 64:
            del clients[next(iter(clients))]
        clients[client] = {"last_service": service, "last_utc": utc()}

    async def do_WhoIsRequest(self, apdu):
        self.seen("who_is", apdu)
        low, high = apdu.deviceInstanceRangeLowLimit, apdu.deviceInstanceRangeHighLimit
        instance = self.device_object.objectIdentifier[1]
        # Library validates ranges and sends directed I-Am to the actual source,
        # including its port/routed address. Broadcast reply also aids Metasys.
        await super().do_WhoIsRequest(apdu)
        included = (low is None or low <= instance) and (high is None or instance <= high)
        if included and apdu.pduDestination and apdu.pduDestination.addrType in (
                Address.localBroadcastAddr, Address.globalBroadcastAddr, Address.remoteBroadcastAddr):
            self.i_am(address=GlobalBroadcast())
        self.diagnostics["last_who_is"] = {
            "utc": utc(), "source": str(apdu.pduSource),
            "destination": str(apdu.pduDestination), "low": low, "high": high,
            "instance_in_range": included}

    async def do_ReadPropertyRequest(self, apdu):
        self.seen("read_property", apdu)
        await super().do_ReadPropertyRequest(apdu)

    async def do_ReadPropertyMultipleRequest(self, apdu):
        self.seen("read_property_multiple", apdu)
        await super().do_ReadPropertyMultipleRequest(apdu)

    async def do_WritePropertyRequest(self, apdu):
        raise ExecutionError(errorClass="property", errorCode="writeAccessDenied")

    async def do_WritePropertyMultipleRequest(self, apdu):
        raise ExecutionError(errorClass="property", errorCode="writeAccessDenied")

    async def do_IAmRequest(self, apdu):
        if self.device_object and apdu.iAmDeviceIdentifier == self.device_object.objectIdentifier:
            self.diagnostics["duplicate_instance"] = {"source": str(apdu.pduSource), "utc": utc()}
            print(json.dumps({"event": "duplicate_instance", **self.diagnostics["duplicate_instance"]}), flush=True)
        await super().do_IAmRequest(apdu)


def point_value(document, key, scale, max_age):
    """Never turn a missing, invalid or stale sample into a valid zero."""
    item = (document.get("readings") or {}).get(key) or {}
    age = item.get("age_seconds", document.get("age_seconds"))
    value = item.get("value")
    good = (document.get("available") is True and document.get("protocol_valid") is True
            and item.get("valid") is True and item.get("stale") is False
            and isinstance(age, (int, float)) and 0 <= age <= max_age
            and isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value))
    return float(value) * scale if good else None


def make_objects():
    points = {}
    for instance, key, name, units, scale, description in POINTS:
        points[key] = AnalogInputObject(
            objectIdentifier=("analogInput", instance), objectName="IQData-" + name,
            description=description + "; decoded physical meter reply; display comparison pending",
            presentValue=0.0, units=units, statusFlags=[0, 1, 0, 0],
            eventState="normal", outOfService=False, reliability="communicationFailure",
            covIncrement=0.01 if units == "powerFactor" else 0.1,
        )
    age = AnalogInputObject(
        objectIdentifier=("analogInput", 20), objectName="IQData-Sample-Age",
        description="Seconds since the last meter sample; fault if timestamp unavailable",
        presentValue=0.0, units="seconds", statusFlags=[0, 1, 0, 0], eventState="normal",
        outOfService=False, reliability="communicationFailure", covIncrement=1.0)
    health = BinaryInputObject(
        objectIdentifier=("binaryInput", 1), objectName="IQData-Telemetry-Healthy",
        description="Active when all standard electrical readings are valid and no more than 5 seconds old; diagnostics carry individual quality",
        presentValue="inactive", statusFlags=[0, 0, 0, 0], eventState="normal",
        outOfService=False, polarity="normal", reliability="noFaultDetected",
        activeText="Fresh", inactiveText="Stale or failed")
    display = BinaryInputObject(
        objectIdentifier=("binaryInput", 2), objectName="IQData-Display-Verified",
        description="Active only after contemporaneous physical display comparison is recorded",
        presentValue="inactive", statusFlags=[0, 0, 0, 0], eventState="normal",
        outOfService=False, polarity="normal", reliability="noFaultDetected")
    return points, age, health, display


def refresh(objects, document, max_age):
    points, age_obj, health, display = objects
    all_good = True
    for _, key, _, _, scale, _ in POINTS:
        value = point_value(document, key, scale, max_age)
        good = value is not None
        all_good &= good
        obj = points[key]
        if good:
            obj.presentValue = value
        # Local BACpypes statusFlags is computed from reliability. Notify its
        # COV monitors while the old flags are still visible, then change the
        # backing reliability before the queued notification runs.
        obj.statusFlags = [0, int(not good), 0, 0]
        obj.reliability = "noFaultDetected" if good else "communicationFailure"
    age = document.get("age_seconds")
    age_valid = isinstance(age, (int, float)) and math.isfinite(age) and age >= 0
    if age_valid:
        age_obj.presentValue = age
    age_obj.statusFlags = [0, int(not age_valid), 0, 0]
    age_obj.reliability = "noFaultDetected" if age_valid else "communicationFailure"
    health.presentValue = "active" if all_good else "inactive"
    display.presentValue = "active" if document.get("display_verified") is True else "inactive"
    return all_good


HEALTH_FIELDS = [
    (400, "POLL_ATTEMPTS", "Measurement-Poll-Count", "noUnits", "measurements", "attempts"),
    (401, "POLL_FAILURES", "Measurement-Poll-Failures", "noUnits", "measurements", "failures"),
    (402, "POLL_FAILURE_PERCENT", "Measurement-Poll-Failure-Rate", "percent", "measurements", "failure_percent"),
    (403, "POLL_MALFORMED", "Malformed-Meter-Writes", "noUnits", "measurements", "malformed_writes"),
    (404, "POLL_DURATION", "Measurement-Poll-Duration", "seconds", "measurements", "last_duration_seconds"),
    (405, "DIAG_ATTEMPTS", "Diagnostic-Poll-Count", "noUnits", "diagnostics", "attempts"),
    (406, "DIAG_FAILURES", "Diagnostic-Poll-Failures", "noUnits", "diagnostics", "failures"),
    (407, "POLL_CONSECUTIVE_FAILURES", "Consecutive-Poll-Failures", "noUnits", "measurements", "consecutive_failures"),
    (408, "POLL_LAST_STOP", "Last-Poll-Stop-Code", "noUnits", "measurements", "last_stop_code"),
]
HEALTH_SPECS = [{"key": key, "name": name, "type": "analog", "instance": instance,
                 "units": units, "group": "health",
                 "description": "Recorder communication diagnostic: " + field + "; counters since poll_health state was created"}
                for instance, key, name, units, group, field in HEALTH_FIELDS]
EXTRA_SPECS = METER_DIAGNOSTIC_SPECS + DERIVED_DIAGNOSTIC_SPECS + HEALTH_SPECS


def auxiliary_readings(state, document, accumulator):
    diagnostic, error = saved_json(state / "meter_diagnostics.json")
    readings = dict(diagnostic.get("readings", {})) if not error else {}
    # Use actual readable meter configuration for software excursion checks.
    # These are software diagnostic thresholds, separate from meter protection.
    import datetime as dt
    now = dt.datetime.now(dt.timezone.utc)
    def nominal(key):
        item = readings.get(key, {})
        if not isinstance(item, dict):
            return None
        try:
            age = (now - dt.datetime.fromisoformat(item["observed_utc"])).total_seconds()
        except (KeyError, TypeError, ValueError):
            return None
        value = item.get("value")
        if (0 <= age <= 3600 and item.get("valid") is True and item.get("stale") is False
                and isinstance(value, (int, float)) and not isinstance(value, bool)
                and math.isfinite(value) and value > 0):
            return float(value)
        return None
    accumulator.set_nominals(nominal("CONFIG_NOMINAL_LL_V"), nominal("CONFIG_FREQUENCY_Hz"))
    readings.update(accumulator.update(document))
    health, error = saved_json(state / "poll_health.json")
    if not error:
        for _, key, _, _, group, field in HEALTH_FIELDS:
            value = health.get(group, {}).get(field)
            readings[key] = {"value": value, "valid": isinstance(value, (int, float)),
                             "stale": False, "observed_utc": health.get("updated_utc")}
    return readings


async def run(args):
    device = MeterDevice(
        objectIdentifier=("device", args.instance), objectName=args.name,
        description="IQ Data Plus II via Pico USB; read-only live meter gateway",
        vendorName="BACpypes / site integration", vendorIdentifier=999,
        modelName="IQ Data Plus II - Pi 5 Gateway", firmwareRevision="Pico 0.4.6",
        applicationSoftwareVersion=VERSION, location=args.address, databaseRevision=2,
    )
    network = NetworkPortObject(args.address, objectIdentifier=("networkPort", 1),
                                objectName="IQData-Ethernet", networkNumber=0,
                                networkNumberQuality="unknown")
    app = MeterApplication.from_object_list([device, network])
    app.configure_cov_persistence(args.state / "bacnet-cov-subscriptions.json")
    # The pinned stack retries failed binds. Do not report a healthy service or
    # queue announcements indefinitely when an address is absent at boot.
    try:
        binds = [task for link in app.link_layers.values() for task in link.server._transport_tasks]
        await asyncio.wait_for(asyncio.gather(*binds), timeout=15)
        await asyncio.gather(*(link.server._local_transport_ready.wait()
                               for link in app.link_layers.values()))
    except BaseException:
        app.close()
        raise
    objects = make_objects()
    for obj in [*objects[0].values(), *objects[1:]]:
        app.add_object(obj)
    extras = build_extra_objects(EXTRA_SPECS)
    for obj in extras.values():
        app.add_object(obj)
    accumulator = DiagnosticAccumulator()
    _, initial = telemetry(args.state)
    refresh(objects, initial, args.max_age)
    extra_values = auxiliary_readings(args.state, initial, accumulator)
    refresh_extra_objects(extras, EXTRA_SPECS, extra_values, max_ages={"health": 10})
    restored = app.restore_cov_subscriptions()
    app.diagnostics.update({"device_instance": args.instance, "object_name": args.name,
                            "address": args.address, "metasys": args.metasys,
                            "object_count": len(app.objectIdentifier), "version": VERSION,
                            "udp_bound": True, "cov_restore": restored})
    print(json.dumps({"event": "started", **app.diagnostics}), flush=True)
    announced = saved = 0.0
    was_healthy = None
    try:
        while True:
            # Existing function rereads the atomic state and recalculates freshness.
            _, document = telemetry(args.state)
            healthy = refresh(objects, document, args.max_age)
            extra_values = auxiliary_readings(args.state, document, accumulator)
            quality = refresh_extra_objects(extras, EXTRA_SPECS, extra_values, max_ages={"health": 10})
            if quality.get("CONFIG_ALTERNATE_PF"):
                mode = "alternate" if extra_values["CONFIG_ALTERNATE_PF"]["value"] else "standard W/Q"
                objects[0]["PF"].description = "Meter PF: negative=lagging, positive=leading; " + mode + " calculation selected"
            if quality.get("FIRMWARE_VERSION") and quality.get("FIRMWARE_REVISION"):
                device.firmwareRevision = "Meter %d rev %d / Pico 0.4.6" % (
                    extra_values["FIRMWARE_VERSION"]["value"], extra_values["FIRMWARE_REVISION"]["value"])
            if healthy != was_healthy:
                print(json.dumps({"event": "quality", "utc": utc(), "healthy": healthy}), flush=True)
                was_healthy = healthy
            now = time.monotonic()
            if now - announced >= 60:
                announce(app, args.metasys)
                app.diagnostics["last_announcement_utc"] = utc()
                announced = now
            if now - saved >= 5:
                app.diagnostics.update({"updated_utc": utc(), "telemetry_healthy": healthy,
                                        "observed_utc": document.get("observed_utc"),
                                        "extra_valid_points": sum(quality.values()),
                                        "extra_total_points": len(quality),
                                        "cov": app.cov_persistence_status})
                atomic_json(args.diagnostics, app.diagnostics)
                atomic_json(args.state / "live_diagnostics.json", {
                    "updated_utc": utc(), "readings": extra_values,
                    "quality": quality, "database_trending": False})
                saved = now
            await asyncio.sleep(.5)
    finally:
        app.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--address", required=True, help="This Pi's local IPv4/prefix:UDP-port, e.g. 192.0.2.151/24:47808")
    parser.add_argument("--instance", type=int, default=75151)
    parser.add_argument("--name", default="IQData-PlusII-Pi5")
    parser.add_argument("--metasys", help="Optional directed I-Am destination; normal broadcast discovery remains enabled")
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--diagnostics", type=Path, required=True)
    parser.add_argument("--max-age", type=float, default=5.0)
    args = parser.parse_args()
    if not 0 <= args.instance < 4194303 or not 0 < args.max_age <= 30:
        parser.error("invalid device instance or maximum age")
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
