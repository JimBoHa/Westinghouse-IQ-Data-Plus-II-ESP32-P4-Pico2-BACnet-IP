import asyncio
import datetime as dt
import json
import math
import tempfile
import time
import unittest
from pathlib import Path

from bacpypes3.apdu import SubscribeCOVRequest
from bacpypes3.basetypes import StatusFlags
from bacpypes3.local.device import DeviceObject
from bacpypes3.pdu import Address
from bacpypes3.primitivedata import Real
from bacpypes3.service.cov import ChangeOfValueServices

from bacnet_extras import PersistentCOVMixin, build_extra_objects, refresh_extra_objects


SPECS = [
    {"key": "TEST_V", "name": "Test-Voltage", "type": "analog", "instance": 30,
     "units": "volts", "description": "Test voltage", "group": "derived"},
    {"key": "TEST_FLAG", "name": "IQData-Test-Flag", "type": "binary", "instance": 10,
     "description": "Test flag", "group": "flags"},
    {"key": "SETTING", "name": "Test-Setting", "type": "analog", "instance": 200,
     "units": "noUnits", "description": "Test setting", "group": "settings"},
]


def reading(value, age=0, valid=True):
    return {"value": value, "valid": valid,
            "observed_utc": dt.datetime.fromtimestamp(time.time() - age, dt.timezone.utc).isoformat()}


class MemoryApplication(PersistentCOVMixin, ChangeOfValueServices):
    """Real BACpypes COV detection/encoding with only network IO replaced."""
    def __init__(self, path):
        ChangeOfValueServices.__init__(self)
        self.notifications, self.responses = [], []
        self.points = build_extra_objects(SPECS)
        self.device = DeviceObject(objectIdentifier=("device", 75151), objectName="Test")
        self.objectIdentifier = {obj.objectIdentifier: obj for obj in [self.device, *self.points.values()]}
        for obj in self.objectIdentifier.values():
            obj._app = self
        self.configure_cov_persistence(path, allowed_clients=["192.0.2.37"])

    def get_object_id(self, identifier):
        return self.objectIdentifier.get(identifier)

    async def response(self, apdu):
        self.responses.append(apdu)

    def cov_notification(self, cov, apdu):
        self.notifications.append(apdu)

    def cleanup(self):
        for cov in list(self._subscriptions()):
            # End timers without deleting the persisted restart fixture.
            ChangeOfValueServices.cancel_subscription(self, cov)


def request(lifetime=60, confirmed=True, process_id=123, identifier=("analogInput", 30)):
    args = dict(subscriberProcessIdentifier=process_id, monitoredObjectIdentifier=identifier,
                source=Address("192.0.2.37"))
    if lifetime is not None:
        args.update(issueConfirmedNotifications=confirmed, lifetime=lifetime)
    return SubscribeCOVRequest(**args)


async def flush():
    await asyncio.sleep(0)
    await asyncio.sleep(0)


class PointTests(unittest.IsolatedAsyncioTestCase):
    async def test_values_fault_recovery_deliver_real_cov_notifications(self):
        with tempfile.TemporaryDirectory() as temporary:
            app = MemoryApplication(Path(temporary) / "cov.json")
            try:
                await app.do_SubscribeCOVRequest(request())
                await flush()
                self.assertEqual(len(app.device.activeCovSubscriptions), 1)
                initial = app.notifications[-1]
                self.assertEqual(initial.listOfValues[1].value.cast_out(StatusFlags)[1], 1)
                refresh_extra_objects(app.points, SPECS, {"TEST_V": reading(490)})
                await flush()
                self.assertAlmostEqual(app.notifications[-1].listOfValues[0].value.cast_out(Real), 490)
                self.assertEqual(app.notifications[-1].listOfValues[1].value.cast_out(StatusFlags)[1], 0)
                refresh_extra_objects(app.points, SPECS, {"TEST_V": reading(800, age=6)})
                await flush()
                self.assertAlmostEqual(app.notifications[-1].listOfValues[0].value.cast_out(Real), 490)
                self.assertEqual(app.notifications[-1].listOfValues[1].value.cast_out(StatusFlags)[1], 1)
                refresh_extra_objects(app.points, SPECS, {"TEST_V": reading(491)})
                await flush()
                self.assertEqual(app.notifications[-1].listOfValues[1].value.cast_out(StatusFlags)[1], 0)
                self.assertAlmostEqual(app.points["TEST_V"].presentValue, 491)
            finally:
                app.cleanup()

    async def test_per_group_freshness_and_invalid_values(self):
        points = build_extra_objects(SPECS)
        quality = refresh_extra_objects(points, SPECS, {
            "TEST_V": reading(490, age=6), "TEST_FLAG": reading(True, age=20),
            "SETTING": reading(16, age=3000)})
        self.assertEqual(quality, {"TEST_V": False, "TEST_FLAG": True, "SETTING": True})
        self.assertEqual(str(points["TEST_FLAG"].presentValue), "active")
        self.assertEqual(points["TEST_FLAG"].objectName, "IQData-Test-Flag")
        for bad in (math.inf, math.nan, True, "490", None, 1e100):
            self.assertFalse(refresh_extra_objects(points, SPECS, {"TEST_V": reading(bad)})["TEST_V"])
        for bad in (2, -1, "active", 1.0, None):
            self.assertFalse(refresh_extra_objects(points, SPECS, {"TEST_FLAG": reading(bad)})["TEST_FLAG"])
        self.assertFalse(refresh_extra_objects(points, SPECS, {"TEST_V": reading(490, age=-10)})["TEST_V"])
        malformed = reading(490)
        malformed["observed_utc"] = "2026-09-22T00:00:00"
        self.assertFalse(refresh_extra_objects(points, SPECS, {"TEST_V": malformed})["TEST_V"])

    async def test_reserved_and_duplicate_ids_rejected(self):
        with self.assertRaises(ValueError):
            build_extra_objects([{**SPECS[0], "instance": 1}])
        with self.assertRaises(ValueError):
            build_extra_objects([*SPECS, SPECS[0]])


class PersistenceTests(unittest.IsolatedAsyncioTestCase):
    async def test_snapshot_restore_preserves_recipient_and_expiration(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "cov.json"
            first = MemoryApplication(path)
            try:
                await first.do_SubscribeCOVRequest(request())
                await flush()
                snapshot = json.loads(path.read_text())
                self.assertEqual(len(snapshot["subscriptions"]), 1)
                record = snapshot["subscriptions"][0]
                self.assertEqual(record["client"], "192.0.2.37")
                self.assertEqual(record["object_identifier"], ["analogInput", 30])
                self.assertLessEqual(record["expires_at"], time.time() + 60)
                first.cleanup()
                second = MemoryApplication(path)
                try:
                    refresh_extra_objects(second.points, SPECS, {"TEST_V": reading(490)})
                    restored = second.restore_cov_subscriptions()
                    self.assertEqual(restored["restored"], 1)
                    await flush()
                    active = second.device.activeCovSubscriptions[0]
                    self.assertEqual(active.recipient.processIdentifier, 123)
                    self.assertEqual(active.recipient.recipient.address.macAddress, Address("192.0.2.37").addrAddr)
                    self.assertTrue(active.issueConfirmedNotifications)
                    self.assertLessEqual(active.timeRemaining, 60)
                    self.assertEqual(second.notifications[-1].subscriberProcessIdentifier, 123)
                    self.assertEqual(second.notifications[-1].listOfValues[0].value.cast_out(Real), 490)
                    newer = json.loads(path.read_text())["subscriptions"][0]
                    self.assertLessEqual(newer["expires_at"], record["expires_at"] + .01)
                finally:
                    second.cleanup()
            finally:
                first.cleanup()

    async def test_expired_invalid_and_unapproved_records_not_restored(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "cov.json"
            record = {"client": "192.0.2.37", "process_id": 1,
                      "object_identifier": ["analogInput", 30], "confirmed": True,
                      "expires_at": time.time() + 60}
            records = [{**record, "expires_at": time.time() - 1},
                       {**record, "client": "192.0.2.18"},
                       {**record, "client": "*"}, {**record, "process_id": -1},
                       {**record, "object_identifier": ["analogInput", 999]},
                       {**record, "expires_at": "tomorrow"}, {**record, "confirmed": "yes"}]
            path.write_text(json.dumps({"version": 1, "subscriptions": records}))
            app = MemoryApplication(path)
            try:
                status = app.restore_cov_subscriptions()
                self.assertEqual(status["expired"], 1)
                self.assertEqual(status["rejected"], 6)
                self.assertFalse(app.get_active_cov_subscriptions())
                await flush()
                self.assertFalse(app.notifications)
            finally:
                app.cleanup()

    async def test_renewal_cancel_permanent_and_automatic_expiry(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "cov.json"
            app = MemoryApplication(path)
            try:
                await app.do_SubscribeCOVRequest(request(60))
                await app.do_SubscribeCOVRequest(request(0, False))
                record = json.loads(path.read_text())["subscriptions"][0]
                self.assertIsNone(record["expires_at"])
                self.assertFalse(record["confirmed"])
                self.assertEqual(app.get_active_cov_subscriptions()[0].timeRemaining, 0)
                await flush()
                await app.do_SubscribeCOVRequest(request(None))
                self.assertFalse(json.loads(path.read_text())["subscriptions"])
                await app.do_SubscribeCOVRequest(request(1))
                await flush()
                await asyncio.sleep(1.05)
                self.assertFalse(app.get_active_cov_subscriptions())
                self.assertFalse(json.loads(path.read_text())["subscriptions"])
            finally:
                app.cleanup()

    async def test_bad_state_and_write_error_are_visible(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "cov.json"
            path.write_text("not json")
            app = MemoryApplication(path)
            try:
                self.assertIsNotNone(app.restore_cov_subscriptions()["last_error"])
                app._cov_state_path = Path(temporary)  # A directory cannot be replaced by a file.
                await app.do_SubscribeCOVRequest(request())
                self.assertEqual(app.cov_persistence_status["write_errors"], 1)
                self.assertEqual(len(app.get_active_cov_subscriptions()), 1)
            finally:
                app.cleanup()


if __name__ == "__main__":
    unittest.main()
