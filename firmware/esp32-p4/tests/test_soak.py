#!/usr/bin/env python3
"""Adversarial health-log and independent-probe tests; never use live sockets."""
import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/"tools"))
import soak_monitor as soak


def healthy_log(duration=20, interval=10):
    header = {"type":"header","schema":1,"duration":duration,"interval":interval,"timeout":2,
        "minimum_heap":65536,"maximum_heap_loss":65536,"poll_enabled":False,
        "target":{"ip":"192.0.2.1","mac":"02:00:00:00:00:01","device_instance":75201,"bacnet_port":47808}}
    status = {"project":"iqdata_p4_gateway","ethernet_mac":header["target"]["mac"],"version":"test",
        "source_revision":"test-revision","elf_sha256":"a"*64,"boot_id":"test-boot","reset_reason":3,
        "config":{"device_instance":75201,"name":"TEST","poll_enabled":False},
        "ethernet":{"link_up":True,"ready":True,"protected_address_blocked":False,"ip":"192.0.2.1"},
        "ota":{"startup_health":{"accepted":True},"image_state":2,"running_slot":"ota_0"},
        "internal_free_heap":200000,"uptime_seconds":100,
        "pico":{"connected":True,"qualified":True,"maintenance":False,"heartbeat_age_seconds":.1,
                "version":"Pico 0.4.7 / pico2","connections":1,"failures":0,"overflows":0},
        "bacnet":{"initialized":True,"heartbeat_age_seconds":.01,"analog_inputs":106,"binary_inputs":92,
                  "instance_conflicts":0,"cov_timeouts":0,"restart_notification":{"timestamp_frozen":True,"sent":1,"failures":0}},
        "clock":{"synchronized":True}}
    rows = [header]
    for sequence, offset in enumerate(soak.schedule(duration,interval)):
        sample = copy.deepcopy(status); sample["uptime_seconds"] += offset
        rows.append({"type":"sample","sequence":sequence,"scheduled_seconds":offset,"elapsed_seconds":offset,
                     "ok":True,"alerts":[],"probes":{"status":{"ok":True,"value":sample},
                     "bacnet":{"ok":True,"value":{"device_instance":75201}}}})
    rows.append({"type":"summary","completed":True,"passed":True,"sample_count":len(rows)-1})
    return rows


class SoakTests(unittest.TestCase):
    def test_fractional_schedule_and_final_boundary(self):
        self.assertEqual(soak.schedule(1,.3),[0,.3,.6,.8999999999999999,1])
        self.assertTrue(soak.review_records(healthy_log(1,.3),1)["passed"])
        for duration, interval in ((0,1),(1,0),(float("nan"),1),(1,float("inf")),(2000000,1)):
            with self.assertRaises(ValueError):soak.schedule(duration,interval)

    def test_healthy_run_cannot_claim_longer_duration(self):
        rows=healthy_log();self.assertTrue(soak.review_records(rows,20)["passed"])
        self.assertFalse(soak.review_records(rows,86400)["passed"])

    def test_truncated_interrupted_and_incomplete_fail(self):
        for mutate in (lambda r:r.pop(),lambda r:r.pop(2),lambda r:r[-1].update(completed=False),
                       lambda r:r[-1].update(passed=False),lambda r:r[2].update(ok=False),
                       lambda r:r[2].update(sequence=8),lambda r:r[2].update(elapsed_seconds=0),
                       lambda r:r[2].update(elapsed_seconds=19),lambda r:r[2].update(alerts=["previous-failure"])):
            with self.subTest(mutate=mutate):
                rows=healthy_log();mutate(rows);self.assertFalse(soak.review_records(rows)["passed"])
        with self.assertRaises(ValueError):soak.review_records(healthy_log()+[{"type":"sample"}])

    def test_every_health_failure_recomputed_even_if_saved_ok(self):
        changes = [(('boot_id',),'new'),(('version',),'different'),(('elf_sha256',),'b'*64),
                   (('config','name'),'changed'),(('internal_free_heap',),120000),
                   (('uptime_seconds',),1),(('pico','qualified'),False),(('pico','failures'),1),
                   (('pico','connections'),2),(('pico','overflows'),1),(('pico','heartbeat_age_seconds'),8),
                   (('bacnet','cov_timeouts'),1),(('bacnet','instance_conflicts'),1),
                   (('bacnet','heartbeat_age_seconds'),5),(('ethernet','link_up'),False),
                   (('clock','synchronized'),False),(('ota','image_state'),1),
                   (('bacnet','restart_notification','sent'),0),(('pico','qualified'),'false')]
        for path,value in changes:
            with self.subTest(path=path):
                rows=healthy_log();obj=rows[2]['probes']['status']['value']
                for name in path[:-1]:obj=obj[name]
                obj[path[-1]]=value
                self.assertFalse(soak.review_records(rows)['passed'])

    def test_probe_failures_preserve_other_evidence(self):
        def fails():raise TimeoutError('offline')
        result=soak.collect({'status':fails,'bacnet':lambda:{'device_instance':75201}})
        self.assertFalse(result['status']['ok']);self.assertTrue(result['bacnet']['ok'])
        rows=healthy_log();rows[2]['probes']=result
        self.assertFalse(soak.review_records(rows)['passed'])
        rows=healthy_log();rows[1]['probes']['status']={'ok':False,'error':'no initial status'}
        self.assertFalse(soak.review_records(rows)['passed'])

    def test_i_am_decoder_rejects_malformed_and_other_messages(self):
        body=bytes.fromhex('01001000c4020125c12205c491032100')
        frame=b'\x81\x0a'+(len(body)+4).to_bytes(2,'big')+body
        self.assertEqual(soak.parse_i_am(frame)['device_instance'],75201)
        for cut in range(len(frame)):
            with self.assertRaises(ValueError):soak.parse_i_am(frame[:cut])
        for offset in (0,1,2,3,4,5,6,7,8):
            bad=bytearray(frame);bad[offset]=0xff
            with self.assertRaises(ValueError):soak.parse_i_am(bad)

    def test_nonfinite_json_and_short_log_are_not_success(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'run.jsonl'
            path.write_text('{"type":"sample","elapsed_seconds":NaN}\n')
            with self.assertRaises(ValueError):soak.load_records(path)
            path.write_text('\n'.join(json.dumps(r) for r in healthy_log())+'\n')
            self.assertTrue(soak.review_records(soak.load_records(path))['passed'])


if __name__ == '__main__':unittest.main()
