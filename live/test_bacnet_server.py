import asyncio
import copy
import math
import unittest
from bacnet_server import MeterApplication, MeterDevice, POINTS, announce, make_objects, point_value, refresh
from bacpypes3.basetypes import ServicesSupported
from unittest.mock import Mock


class ValueTests(unittest.TestCase):
    def test_broadcast_announcement_without_site_destination(self):
        app = Mock()
        announce(app)
        self.assertEqual(app.i_am.call_count, 1)
        self.assertEqual(str(app.i_am.call_args.kwargs["address"]), "*:*")

    def test_optional_directed_announcement_uses_configured_port(self):
        app = Mock()
        announce(app, "192.0.2.18:47809")
        self.assertEqual(app.i_am.call_count, 2)
        self.assertEqual(str(app.i_am.call_args.kwargs["address"]), "192.0.2.18:47809")

    def setUp(self):
        self.good = {"available": True, "protocol_valid": True, "age_seconds": 1,
                     "readings": {p[1]: {"valid": True, "stale": False, "value": 100,
                                           "age_seconds": 1} for p in POINTS}}

    def test_reject_missing_invalid_stale_nonfinite(self):
        for change in ({"valid": False}, {"stale": True}, {"value": None},
                       {"value": math.nan}, {"value": math.inf}, {"age_seconds": 6},
                       {"age_seconds": -1}, {"value": True}):
            doc = copy.deepcopy(self.good)
            doc["readings"]["P_W"].update(change)
            self.assertIsNone(point_value(doc, "P_W", .001, 5))
        self.assertIsNone(point_value(self.good, "THD", 1, 5))
        self.good["available"] = False
        self.assertIsNone(point_value(self.good, "P_W", .001, 5))

    def test_unit_scaling_and_meter_signs(self):
        self.assertEqual(point_value(self.good, "P_W", .001, 5), .1)
        self.good["readings"]["Q_var"]["value"] = -87000
        self.good["readings"]["PF"]["value"] = -.93
        self.assertEqual(point_value(self.good, "Q_var", .001, 5), -87)
        self.assertEqual(point_value(self.good, "PF", 1, 5), -.93)

    def test_fresh_stale_recovery_and_last_value(self):
        async def check():
            objects = make_objects()
            changes = []
            objects[0]["P_W"]._property_monitors["statusFlags"].append(
                lambda old, new: changes.append((list(old), list(new))))
            self.assertTrue(refresh(objects, self.good, 5))
            self.assertEqual(objects[0]["P_W"].presentValue, .1)
            self.good["readings"]["P_W"]["age_seconds"] = 6
            self.assertFalse(refresh(objects, self.good, 5))
            self.assertEqual(objects[0]["P_W"].presentValue, .1)
            self.assertEqual(objects[0]["P_W"].statusFlags[1], 1)
            self.good["readings"]["P_W"].update(age_seconds=1, value=200)
            self.assertTrue(refresh(objects, self.good, 5))
            self.assertEqual(objects[0]["P_W"].presentValue, .2)
            self.assertEqual(objects[0]["P_W"].statusFlags[1], 0)
            self.assertEqual([new[1] for old, new in changes], [0, 1, 0])
            await asyncio.sleep(0)
        asyncio.run(check())

    def test_advertised_object_types(self):
        async def check():
            device = MeterDevice(objectIdentifier=("device", 75151), objectName="test")
            supported = device.protocolObjectTypesSupported
            self.assertTrue(all(supported[i] for i in (0, 3, 8, 56)))
            self.assertEqual(sum(supported), 4)
            await asyncio.sleep(0)
        asyncio.run(check())

    def test_advertised_services_match_gateway(self):
        services = MeterApplication.get_services_supported(None)
        for name in ("readProperty", "readPropertyMultiple", "subscribeCOV", "whoIs", "iAm", "whoHas", "iHave"):
            self.assertEqual(services[getattr(ServicesSupported, name)], 1)
        self.assertEqual(sum(services), 7)
        for name in ("writeProperty", "atomicWriteFile", "acknowledgeAlarm"):
            self.assertEqual(services[getattr(ServicesSupported, name)], 0)


if __name__ == "__main__":
    unittest.main()
