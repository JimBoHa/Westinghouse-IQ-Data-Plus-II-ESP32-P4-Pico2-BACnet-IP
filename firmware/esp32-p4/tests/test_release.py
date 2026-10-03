#!/usr/bin/env python3
"""Release integrity tests. Synthetic images never leave this host test."""
import copy
import json
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/"tools"))
import package_release as release

SOURCE="a"*40


def application(recovery=False):
    data=bytearray(512);data[0]=0xe9;data[12:14]=b"\x12\x00";data[32:36]=bytes.fromhex("3254cdab")
    for start,text in ((48,"0.4.1-recovery" if recovery else "0.4.1"),(80,"iqdata_p4_gateway"),(144,"v5.5.5"),(320,SOURCE[:12])):
        data[start:start+len(text)]=text.encode()
    data[176:208]=b"\x01"*32;return bytes(data)


def pico():
    data=bytearray(1024)
    for index in range(2):
        struct.pack_into("<8I",data,index*512,0x0a324655,0x9e5d5157,0x2000,0x10000000+index*256,256,index,2,0xe48bff59)
        struct.pack_into("<I",data,index*512+508,0x0ab16f30)
    program=b"iqdata-pico-live\0pico2\x000.4.7\0";data[32:32+len(program)]=program
    return bytes(data)


def checksums(files):
    files["SHA256SUMS"]="".join(f"{release.digest(data)}  {name}\n" for name,data in sorted(files.items()) if name!="SHA256SUMS").encode()


def candidate():
    pem,fingerprint=release.public_key()
    files={"iqdata_p4_gateway.bin":application(),"iqdata_p4_recovery.bin":application(True),"iqdata_pico_live.uf2":pico(),"release-public-key.pem":pem}
    pins=json.loads((release.PORT/"toolchains.json").read_text())
    manifest={"schema":1,"source_commit":SOURCE,"signed":False,"signing_key_sha256":fingerprint,
        "images":{name:(release.p4_metadata(data,"recovery" in name) if name.endswith(".bin") else release.pico_metadata(data)) for name,data in files.items() if name in release.IMAGES},
        "builds":{target:{"target":target,"source_commit":SOURCE,"toolchains":pins} for target in ("application","recovery","pico2")},
        "files":{name:{"bytes":len(data),"sha256":release.digest(data)} for name,data in files.items()}}
    files["manifest.json"]=json.dumps(manifest).encode();checksums(files);return files


class ReleaseTests(unittest.TestCase):
    def test_candidate_requires_explicit_signing_for_ota(self):
        files=candidate();self.assertFalse(release.verify_files(files)["signed"])
        with self.assertRaises(ValueError):release.verify_files(files,True)

    def test_corrupt_missing_extra_and_metadata_mismatch(self):
        for operation in (lambda f:f.update({"private.key":b"forbidden"}),lambda f:f.pop("iqdata_pico_live.uf2"),
                          lambda f:f.update({"iqdata_p4_gateway.bin":b"bad"}),lambda f:f.update({"SHA256SUMS":b"wrong"})):
            with self.subTest(operation=operation):
                files=candidate();operation(files)
                with self.assertRaises((ValueError,KeyError)):release.verify_files(files)
        for field,value in (("source_commit","b"*40),("signed",True)):
            files=candidate();manifest=json.loads(files["manifest.json"]);manifest[field]=value
            files["manifest.json"]=json.dumps(manifest).encode();checksums(files)
            with self.assertRaises((ValueError,KeyError)):release.verify_files(files)

    def test_role_chip_family_and_image_metadata_rejected(self):
        data=pico().replace(b"0.4.7\0",b"0.4.8\0")
        self.assertEqual(release.pico_metadata(data)["version"],"0.4.8")
        with self.assertRaises(ValueError):release.pico_metadata(data.replace(b"0.4.8\0",b"0.4.9\0"))
        with self.assertRaises(ValueError):release.p4_metadata(application(True),False)
        for offset in (0,12,32,80):
            data=bytearray(application());data[offset]^=0x01
            with self.assertRaises(ValueError):release.p4_metadata(data)
        for offset in (0,8,12,16,20,24,28,508):
            data=bytearray(pico());data[offset]^=0x01
            with self.assertRaises(ValueError):release.pico_metadata(data)

    def test_target_bound_signature_and_manifest_authentication(self):
        with tempfile.TemporaryDirectory() as directory:
            key=Path(directory)/"temporary.key"
            subprocess.run(["openssl","ecparam","-name","prime256v1","-genkey","-noout","-out",str(key)],check=True,capture_output=True)
            pem=subprocess.check_output(["openssl","pkey","-in",str(key),"-pubout"])
            data=application();message=f"IQDATA-IMAGE-V1\nesp32p4\n{len(data)}\n{release.digest(data)}\n".encode()
            signature=subprocess.check_output(["openssl","dgst","-sha256","-sign",str(key)],input=message)
            manifest={"schema":1,"target":"esp32p4","bytes":len(data),"sha256":release.digest(data),"signature":signature.hex()}
            release.check_signature(data,manifest,"esp32p4",pem)
            with self.assertRaises(ValueError):release.check_signature(data,manifest,"pico2",pem)
            with self.assertRaises(ValueError):release.check_signature(data+b"x",manifest,"esp32p4",pem)
            release.verify_signature(message,signature,pem)
            with self.assertRaises(ValueError):release.verify_signature(message+b"x",signature,pem)

    def test_zip_path_and_duplicate_rejection(self):
        with tempfile.TemporaryDirectory() as directory:
            for name in ("../key","/absolute","bad\\name"):
                archive=Path(directory)/"bad.zip"
                with zipfile.ZipFile(archive,"w") as output:output.writestr(name,b"bad")
                with self.assertRaises(ValueError):release.read_package(archive)
            archive=Path(directory)/"valid.zip"
            with zipfile.ZipFile(archive,"w") as output:
                for name,data in candidate().items():output.writestr(name,data)
            _,manifest=release.read_package(archive);self.assertEqual(manifest["source_commit"],SOURCE)


if __name__=="__main__":unittest.main()
