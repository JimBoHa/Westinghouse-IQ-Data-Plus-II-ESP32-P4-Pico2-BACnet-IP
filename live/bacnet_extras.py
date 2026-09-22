"""BACnet diagnostics objects and restart-safe local COV subscriptions.

Only reads decoded values; never talks to the meter or database. Subscription
state is a local restart cache, not a measurement trend. Restore only remembers
subscriptions actually received (or explicitly seeded from capture evidence).
"""
import asyncio
import datetime as dt
import json
import math
import os
import tempfile
import time
from pathlib import Path

from bacpypes3.basetypes import (
    DeviceAddress, COVSubscription, ListOfCOVSubscription,
    ObjectPropertyReference, Recipient, RecipientProcess,
)
from bacpypes3.local.analog import AnalogInputObject
from bacpypes3.local.binary import BinaryInputObject
from bacpypes3.pdu import Address
from bacpypes3.primitivedata import ObjectIdentifier
from bacpypes3.service.cov import Subscription


GROUP_MAX_AGE = {"flags": 30.0, "settings": 3600.0, "trip": 180.0, "derived": 5.0}


def build_extra_objects(specs):
    """Build stable read-only AI/BI objects; caller adds them to its application."""
    objects, identifiers, names = {}, set(), set()
    for spec in specs:
        kind = spec["type"]
        if kind not in ("analog", "binary"):
            raise ValueError("unsupported diagnostics object type")
        instance = spec["instance"]
        if isinstance(instance, bool) or not isinstance(instance, int) or not 0 <= instance < 4194303:
            raise ValueError("invalid diagnostics object instance")
        if (kind == "analog" and 1 <= instance <= 20) or (kind == "binary" and 1 <= instance <= 2):
            raise ValueError("diagnostics must preserve existing point identifiers")
        identifier = ("analogInput" if kind == "analog" else "binaryInput", instance)
        name = spec["name"]
        if not name.startswith("IQData-"):
            name = "IQData-" + name
        if spec["key"] in objects or identifier in identifiers or name in names:
            raise ValueError("duplicate diagnostics key, identifier or name")
        identifiers.add(identifier)
        names.add(name)
        common = dict(objectIdentifier=identifier, objectName=name,
                      description=spec["description"], statusFlags=[0, 1, 0, 0],
                      eventState="normal", outOfService=False,
                      reliability="communicationFailure")
        if kind == "analog":
            obj = AnalogInputObject(
                **common, presentValue=0.0, units=spec.get("units", "noUnits"),
                covIncrement=spec.get("cov_increment", 0.01))
        else:
            obj = BinaryInputObject(
                **common, presentValue="inactive", polarity="normal",
                activeText=spec.get("active_text", "Active"),
                inactiveText=spec.get("inactive_text", "Inactive"))
        objects[spec["key"]] = obj
    return objects


def _timestamp(value):
    if not isinstance(value, str):
        return None
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            return None
        return parsed.timestamp()
    except (ValueError, OverflowError, OSError):
        return None


def refresh_extra_objects(objects, specs, readings, now=None, max_ages=None):
    """Refresh {key: {value, valid, observed_utc, stale?}} and return validity.

    Missing/invalid/stale values retain their last good presentValue and set
    fault/reliability. Settings and trip reads use their own acquisition times;
    those times are not represented as event timestamps.
    """
    now = time.time() if now is None else float(now)
    ages = {**GROUP_MAX_AGE, **(max_ages or {})}
    quality = {}
    for spec in specs:
        entry = readings.get(spec["key"]) or {}
        value = entry.get("value")
        observed = _timestamp(entry.get("observed_utc"))
        age = None if observed is None else now - observed
        max_age = spec.get("max_age_seconds", ages.get(spec["group"], 5.0))
        good = (entry.get("valid") is True and entry.get("stale") is not True
                and age is not None and 0 <= age <= max_age)
        if spec["type"] == "binary":
            good = good and isinstance(value, (bool, int)) and value in (False, True)
            output = "active" if value else "inactive"
        else:
            good = (good and isinstance(value, (int, float)) and not isinstance(value, bool)
                    and math.isfinite(value))
            output = value * spec.get("scale", 1.0) if good else None
            good = good and math.isfinite(output) and abs(output) <= 3.4028234663852886e38
        obj = objects[spec["key"]]
        if good:
            obj.presentValue = output
        # BACpypes' getter computes statusFlags from reliability. Notify the
        # statusFlags monitor first, while its previous value is still visible.
        obj.statusFlags = [0, int(not good), 0, 0]
        obj.reliability = "noFaultDetected" if good else "communicationFailure"
        quality[spec["key"]] = bool(good)
    return quality


def _address_key(address):
    return address.addrType, address.addrNet, address.addrAddr


def _atomic_json(path, document):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix=path.name + ".", delete=False) as handle:
            temporary = Path(handle.name)
            json.dump(document, handle, sort_keys=True, allow_nan=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


class PersistentCOVMixin:
    """Place before Application in the MRO; configure after app construction.

    configure_cov_persistence(path, allowed_clients=None)
    restore_cov_subscriptions() after adding objects and refreshing their values.
    Save calls are automatic after SubscribeCOV and cancellation/expiration.
    Finite expiration is absolute UTC, so downtime never extends a lease.
    """

    def configure_cov_persistence(self, path, allowed_clients=None):
        self._cov_state_path = Path(path)
        self._cov_allowed_clients = (None if allowed_clients is None else
                                     {_address_key(Address(client)) for client in allowed_clients})
        self._cov_restoring = False
        self.cov_persistence_status = {"restored": 0, "expired": 0, "rejected": 0,
                                       "write_errors": 0, "last_error": None}

    def _subscriptions(self):
        for detection in self._cov_detections.values():
            yield from detection.cov_subscriptions

    @staticmethod
    def _remaining(cov, now_monotonic):
        if cov.cancel_handle is None:
            return None
        return max(0.0, cov.cancel_handle.when() - now_monotonic)

    def cov_subscription_snapshot(self):
        now, monotonic = time.time(), asyncio.get_running_loop().time()
        subscriptions = []
        for cov in self._subscriptions():
            remaining = self._remaining(cov, monotonic)
            if remaining is not None and remaining <= 0:
                continue
            subscriptions.append({
                "client": str(cov.client_addr), "process_id": int(cov.proc_id),
                "object_identifier": ["analogInput" if int(cov.obj_id[0]) == 0 else
                                      "binaryInput" if int(cov.obj_id[0]) == 3 else
                                      str(cov.obj_id[0]), int(cov.obj_id[1])],
                "confirmed": bool(cov.confirmed),
                "expires_at": None if remaining is None else now + remaining,
            })
        return {"version": 1, "saved_utc": dt.datetime.fromtimestamp(now, dt.timezone.utc).isoformat(),
                "subscriptions": subscriptions}

    def persist_cov_subscriptions(self):
        if not getattr(self, "_cov_state_path", None) or getattr(self, "_cov_restoring", False):
            return
        try:
            snapshot = self.cov_subscription_snapshot()
            _atomic_json(self._cov_state_path, snapshot)
            self.cov_persistence_status["active_count"] = len(snapshot["subscriptions"])
            self.cov_persistence_status["saved_utc"] = snapshot["saved_utc"]
            self.cov_persistence_status["last_error"] = None
        except (OSError, TypeError, ValueError) as exc:
            # A disk error must be visible, but must not stop live COV delivery.
            self.cov_persistence_status["write_errors"] += 1
            self.cov_persistence_status["last_error"] = str(exc)

    async def do_SubscribeCOVRequest(self, apdu):
        await super().do_SubscribeCOVRequest(apdu)
        # The pinned stack renews the lifetime but not this renewed parameter.
        if apdu.issueConfirmedNotifications is not None:
            for cov in self._subscriptions():
                if (cov.client_addr == apdu.pduSource and cov.proc_id == apdu.subscriberProcessIdentifier
                        and cov.obj_id == apdu.monitoredObjectIdentifier):
                    cov.confirmed = apdu.issueConfirmedNotifications
        self.persist_cov_subscriptions()

    def cancel_subscription(self, cov):
        super().cancel_subscription(cov)
        self.persist_cov_subscriptions()

    def restore_cov_subscriptions(self):
        """Restore valid unexpired subscriptions without synthesizing requests."""
        status = self.cov_persistence_status
        try:
            document = json.loads(self._cov_state_path.read_text())
            if document.get("version") != 1 or not isinstance(document.get("subscriptions"), list):
                raise ValueError("unsupported COV restart state")
        except FileNotFoundError:
            return dict(status)
        except (OSError, ValueError, TypeError, AttributeError) as exc:
            status["last_error"] = str(exc)
            return dict(status)
        loop = asyncio.get_running_loop()
        now = time.time()
        self._cov_restoring = True
        try:
            for record in document["subscriptions"]:
                try:
                    if not isinstance(record, dict) or not isinstance(record.get("client"), str):
                        raise ValueError("invalid subscription record")
                    client = Address(record["client"])
                    if (client.addrType not in (Address.localStationAddr, Address.remoteStationAddr)
                            or client.addrAddr is None):
                        raise ValueError("COV recipient must be a station")
                    if self._cov_allowed_clients is not None and _address_key(client) not in self._cov_allowed_clients:
                        raise ValueError("unapproved restored recipient")
                    process_id = record["process_id"]
                    if isinstance(process_id, bool) or not isinstance(process_id, int) or not 0 <= process_id < 2**32:
                        raise ValueError("invalid subscriber process identifier")
                    identifier = record["object_identifier"]
                    if (not isinstance(identifier, list) or len(identifier) != 2
                            or identifier[0] not in ("analogInput", "binaryInput")
                            or isinstance(identifier[1], bool) or not isinstance(identifier[1], int)):
                        raise ValueError("invalid subscribed object identifier")
                    obj = self.get_object_id(ObjectIdentifier(tuple(identifier)))
                    if obj is None or not getattr(obj, "_cov_criteria", None):
                        raise ValueError("subscribed object unavailable")
                    confirmed = record["confirmed"]
                    if not isinstance(confirmed, bool):
                        raise ValueError("invalid notification type")
                    expires = record["expires_at"]
                    if expires is None:
                        lifetime = 0
                    else:
                        if (isinstance(expires, bool) or not isinstance(expires, (int, float))
                                or not math.isfinite(expires)):
                            raise ValueError("invalid subscription expiration")
                        lifetime = math.floor(expires - now)
                        if lifetime <= 0:
                            status["expired"] += 1
                            continue
                        if lifetime >= 2**32:
                            raise ValueError("invalid subscription lifetime")
                    obj_id = obj.objectIdentifier
                    detection = self._cov_detections.get(obj_id)
                    if detection is None:
                        detection = obj._cov_criteria(obj)
                        self._cov_detections[obj_id] = detection
                    if any(cov.client_addr == client and cov.proc_id == process_id and cov.obj_id == obj_id
                           for cov in detection.cov_subscriptions):
                        raise ValueError("duplicate restored subscription")
                    cov = Subscription(obj, client, process_id, obj_id, confirmed, lifetime, None)
                    self.add_subscription(cov)
                    loop.call_soon(detection.send_cov_notifications, cov)
                    status["restored"] += 1
                except (KeyError, ValueError, TypeError, AttributeError, OverflowError):
                    status["rejected"] += 1
        finally:
            self._cov_restoring = False
        self.persist_cov_subscriptions()
        return dict(status)

    def get_active_cov_subscriptions(self):
        """Truthful Device.activeCovSubscriptions, absent in the pinned stack."""
        result = ListOfCOVSubscription()
        monotonic = asyncio.get_running_loop().time()
        for cov in self._subscriptions():
            remaining = self._remaining(cov, monotonic)
            if remaining is not None and remaining <= 0:
                continue
            result.append(COVSubscription(
                recipient=RecipientProcess(
                    recipient=Recipient(address=DeviceAddress(
                        networkNumber=cov.client_addr.addrNet or 0,
                        macAddress=cov.client_addr.addrAddr)),
                    processIdentifier=cov.proc_id),
                monitoredPropertyReference=ObjectPropertyReference(
                    objectIdentifier=cov.obj_id, propertyIdentifier="presentValue"),
                issueConfirmedNotifications=cov.confirmed,
                timeRemaining=0 if remaining is None else max(1, math.ceil(remaining)),
            ))
        return result
