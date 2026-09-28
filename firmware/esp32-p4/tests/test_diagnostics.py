#!/usr/bin/env python3
"""Exercise the bounded diagnostic log on an identified development gateway."""
import argparse,json,sys,time
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/"tools"))
from gateway_client import Gateway

def main():
    p=argparse.ArgumentParser(description=__doc__)
    for name in ('target','expected-mac','token-file','pin-file','output'):p.add_argument('--'+name,required=True)
    a=p.parse_args();client=Gateway(a.target,a.pin_file,a.token_file,a.expected_mac)
    before=client.status();first=client.request('/api/diagnostics',b'',authenticated=True)
    assert first['capacity']==32 and first['boot_id'] and any(e['utc_ms'] is None for e in first['events'])
    deadline=time.monotonic()+120
    while not client.status()['clock']['synchronized'] and time.monotonic()<deadline:time.sleep(2)
    assert client.status()['clock']['synchronized'],'NTP synchronization not observed'
    for _ in range(40):
        code,_,_=client.raw('/api/auth/check',b'')
        assert code==401
    last=client.request('/api/diagnostics',b'',authenticated=True)
    assert len(last['events'])==32 and last['overwritten']>=8
    assert last['total']>=first['total']+40 and last['boot_id']==first['boot_id']
    assert all(e['event']=='auth_rejected' and e['utc_ms'] is not None for e in last['events'])
    seq=[e['sequence'] for e in last['events']];assert seq==list(range(seq[0],seq[0]-32,-1))
    after=client.status();assert after['config']==before['config'] and after['elf_sha256']==before['elf_sha256']
    report={'passed':True,'ntp_synchronized':True,'capacity':32,'overwritten':last['overwritten'],
            'pre_sync_events_had_null_utc':True,'configuration_unchanged':True}
    Path(a.output).write_text(json.dumps(report,indent=2)+'\n');print(json.dumps(report,indent=2))
if __name__=='__main__':main()
