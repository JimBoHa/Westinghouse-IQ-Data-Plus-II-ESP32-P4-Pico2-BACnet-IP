#!/usr/bin/env python3
"""Physical HTTPS/auth/signature rejection tests. No valid image is activated."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import time
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/"tools"))
from gateway_client import Gateway, auth_headers, image_signature


def main():
    p=argparse.ArgumentParser(description=__doc__)
    for name in ("target","expected-mac","token-file","pin-file","app-bin","pico-uf2","output"):
        p.add_argument("--"+name,required=True)
    p.add_argument("--signing-key-file",type=Path)
    p.add_argument("--expiry",action="store_true")
    a=p.parse_args();client=Gateway(a.target,a.pin_file,a.token_file,a.expected_mac)
    before=client.status();assert before["pico"]["qualified"]
    checks={}
    def send(name,path,body,expected,headers):
        code,reply,_=client.raw(path,body,headers,timeout=150)
        assert code==expected,(name,code,reply[:200])
        checks[name]={"http_status":code}
    def headers(path,body,digest=None,key=None):
        nonce=client.request("/api/auth/challenge")["nonce"]
        return auth_headers(client.key if key is None else key,path,nonce,body,digest)
    def sign(target,body):
        context=f"IQDATA-IMAGE-V1\n{target}\n{len(body)}\n{hashlib.sha256(body).hexdigest()}\n".encode()
        return subprocess.check_output(["openssl","dgst","-sha256","-sign",str(a.signing_key_file)],input=context).hex()
    path="/api/auth/check"
    send("missing_authentication",path,b"",401,{})
    h=headers(path,b"");wrong=dict(h);wrong["X-IQ-Auth"]="00"*32
    send("wrong_key",path,b"",401,wrong)
    send("valid_after_bad_guess",path,b"",200,h)
    send("replay_rejected",path,b"",401,h)
    send("path_bound",'/api/config',b"",401,headers(path,b""))
    body=b'{ }';send("body_hash_bound",'/api/config',body,400,headers('/api/config',body,hashlib.sha256(b'{}').hexdigest()))
    app,app_signature=image_signature(a.app_bin,'esp32p4');pico,pico_signature=image_signature(a.pico_uf2,'pico2')
    send("unsigned_p4",'/api/firmware',app,400,headers('/api/firmware',app))
    send("unsigned_pico",'/api/pico/firmware',pico,400,headers('/api/pico/firmware',pico))
    send("target_bound_signature",'/api/firmware',pico,400,headers('/api/firmware',pico)|pico_signature)
    modified=bytearray(pico);modified[-1]^=1
    send("pico_received_hash",'/api/pico/firmware',modified,400,headers('/api/pico/firmware',modified,hashlib.sha256(pico).hexdigest())|pico_signature)
    if a.signing_key_file:
        assert not a.signing_key_file.stat().st_mode&0o077
        wrong_project=bytearray(app[:512]);wrong_project[80:112]=b'unrelated-project'.ljust(32,b'\0')
        send("wrong_p4_project",'/api/firmware',wrong_project,400,headers('/api/firmware',wrong_project)|{"X-Image-Signature":sign('esp32p4',wrong_project)})
        wrong_family=bytearray(pico);offset=512 if int.from_bytes(pico[8:12],'little')==0xa000 else 0
        wrong_family[offset+28:offset+32]=b'\xff'*4
        send("wrong_pico_family",'/api/pico/firmware',wrong_family,400,headers('/api/pico/firmware',wrong_family)|{"X-Image-Signature":sign('pico2',wrong_family)})
    invalid=b'{"device_instance":75151,"name":"Forbidden"}'
    send("protected_configuration",'/api/config',invalid,400,headers('/api/config',invalid))
    if a.expiry:
        expired=headers(path,b'');time.sleep(61)
        send("expired_nonce",path,b'',401,expired)
    after=client.status();assert after['uptime_seconds']>=before['uptime_seconds']
    for key in ('config','version','elf_sha256','ota'):assert after[key]==before[key],key
    assert after['pico']['qualified'] and after['pico']['connections']==before['pico']['connections']
    report={'passed':True,'checks':checks,'configuration_and_boot_unchanged':True,'pico_connection_unchanged':True}
    Path(a.output).write_text(json.dumps(report,indent=2)+'\n');print(json.dumps(report,indent=2))

if __name__=='__main__':main()
