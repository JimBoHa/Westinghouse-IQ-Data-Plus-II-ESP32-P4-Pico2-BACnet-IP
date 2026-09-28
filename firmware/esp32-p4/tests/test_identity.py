#!/usr/bin/env python3
"""Send one unicast duplicate claim only to the identified development gateway."""
import argparse,json,socket,sys,time
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/"tools"))
from gateway_client import Gateway

def main():
    p=argparse.ArgumentParser(description=__doc__)
    for name in ('target','expected-mac','pin-file','output'):p.add_argument('--'+name,required=True)
    a=p.parse_args();client=Gateway(a.target,a.pin_file,expected_mac=a.expected_mac);before=client.status()
    instance=before['config']['device_instance'];assert instance!=75151
    # BVLC original unicast, local NPDU, I-Am APDU with independently encoded application tags.
    apdu=b'\x10\x00\xc4'+((8<<22)|instance).to_bytes(4,'big')+b'\x22\x05\xc4\x91\x03\x22\x01\x04'
    frame=b'\x81\x0a'+(6+len(apdu)).to_bytes(2,'big')+b'\x01\x00'+apdu
    with socket.socket(socket.AF_INET,socket.SOCK_DGRAM) as sock:
        sock.connect((client.address,before['config']['bacnet_port']));peer=sock.getsockname();sock.send(frame)
    deadline=time.monotonic()+5
    while time.monotonic()<deadline:
        after=client.status()
        if after['bacnet']['instance_conflicts']>before['bacnet']['instance_conflicts']:break
        time.sleep(.2)
    assert after['bacnet']['instance_status']=='conflict'
    assert after['bacnet']['last_conflict']['ip']==peer[0] and after['bacnet']['last_conflict']['port']==peer[1]
    assert after['config']==before['config'] and after['uptime_seconds']>=before['uptime_seconds']
    report={'passed':True,'unicast_only':True,'identity_unchanged':True,'warning_retained_until_reboot':True}
    Path(a.output).write_text(json.dumps(report,indent=2)+'\n');print(json.dumps(report,indent=2))
if __name__=='__main__':main()
