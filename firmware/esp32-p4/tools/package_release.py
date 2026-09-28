#!/usr/bin/env python3
"""Package and verify matching P4/Pico releases; optional signing stays local."""
import argparse
import ast
import hashlib
import json
from pathlib import Path
import re
import subprocess
import struct
import sys
import tempfile
import zipfile

ROOT = Path(__file__).resolve().parents[3]
PORT = ROOT/"firmware/esp32-p4"
IMAGES = {"iqdata_p4_gateway.bin":"esp32p4","iqdata_p4_recovery.bin":"esp32p4","iqdata_pico_live.uf2":"pico2"}


def digest(data):return hashlib.sha256(data).hexdigest()


def public_key():
    source = (PORT/"main/iq_signing_key.h").read_text()
    pem = ast.literal_eval(re.search(r'^#define IQ_SIGNING_PUBLIC_KEY (".*")$',source,re.M).group(1)).encode()
    fingerprint = re.search(r'IQ_SIGNING_KEY_SHA256 "([0-9a-f]{64})"',source).group(1)
    der = subprocess.check_output(["openssl","pkey","-pubin","-outform","DER"],input=pem)
    if digest(der) != fingerprint:raise ValueError("Compiled public-key fingerprint mismatch")
    return pem, fingerprint


def p4_metadata(data, recovery=False):
    if len(data)<512 or len(data)>4194304 or data[0]!=0xe9 or data[12:14]!=b"\x12\x00" or data[32:36]!=bytes.fromhex("3254cdab"):
        raise ValueError("Expected ESP32-P4 application within 4 MiB OTA slot")
    def field(start):return data[start:start+32].split(b"\0")[0].decode("ascii")
    if field(80)!="iqdata_p4_gateway" or not re.fullmatch(r"\d+\.\d+\.\d+"+(r"-recovery" if recovery else ""),field(48)):
        raise ValueError("Wrong project, version, or recovery/application role")
    if "5.5.5" not in field(144):raise ValueError("Wrong ESP-IDF version")
    return {"version":field(48),"elf_sha256":data[176:208].hex(),"idf_version":field(144)}


def pico_metadata(data):
    if len(data)<1024 or len(data)>1048576 or len(data)%512:raise ValueError("Invalid Pico UF2 size")
    headers=[struct.unpack_from("<8I",data,offset) for offset in range(0,len(data),512)]
    for index,header in enumerate(headers):
        if header[:2]!=(0x0a324655,0x9e5d5157) or struct.unpack_from("<I",data,index*512+508)[0]!=0x0ab16f30:
            raise ValueError("Invalid UF2 magic")
    skip=int(headers[0][2:]==(0xa000,0x10ffff00,256,0,2,0xe48bff57));blocks=headers[skip:]
    image=bytearray()
    for index,header in enumerate(blocks):
        if header[2:]!=(0x2000,0x10000000+index*256,256,index,len(blocks),0xe48bff59):
            raise ValueError("Wrong Pico family or noncontiguous UF2")
        offset=(index+skip)*512;image.extend(data[offset+32:offset+288])
    if not all(value in image for value in (b"iqdata-pico-live\0",b"0.4.7\0",b"pico2\0")):
        raise ValueError("Wrong Pico program, version or board")
    return {"version":"0.4.7","board":"pico2","sdk":"2.3.1","family":"RP2350 ARM Secure"}


def check_signature(data, signed, target, pem):
    if signed.get("schema")!=1 or signed.get("target")!=target or signed.get("bytes")!=len(data) or signed.get("sha256")!=digest(data):
        raise ValueError("Image signature manifest mismatch")
    signature = signed.get("signature","")
    if not re.fullmatch(r"(?:[0-9a-f]{2}){64,72}",signature):raise ValueError("Invalid signature encoding")
    message = f"IQDATA-IMAGE-V1\n{target}\n{len(data)}\n{digest(data)}\n".encode()
    verify_signature(message,bytes.fromhex(signature),pem)


def verify_signature(message, signature, pem):
    if not 64<=len(signature)<=72:raise ValueError("Invalid P-256 signature size")
    with tempfile.TemporaryDirectory() as temporary:
        directory=Path(temporary);(directory/"public.pem").write_bytes(pem);(directory/"signature").write_bytes(signature)
        verified=subprocess.run(["openssl","dgst","-sha256","-verify",str(directory/"public.pem"),"-signature",str(directory/"signature")],input=message,capture_output=True)
    if verified.returncode:raise ValueError("Release signature verification failed")


def verify_files(files, require_signatures=False):
    if "manifest.json" not in files or "SHA256SUMS" not in files:raise ValueError("Missing release manifest/checksums")
    manifest=json.loads(files["manifest.json"])
    if manifest.get("schema")!=1 or not re.fullmatch(r"[0-9a-f]{40}",manifest.get("source_commit","")):
        raise ValueError("Invalid source manifest")
    if type(manifest.get("signed")) is not bool:raise ValueError("Invalid signing status")
    pins=json.loads((PORT/"toolchains.json").read_text())
    for target in ("application","recovery","pico2"):
        metadata=manifest["builds"][target]
        if metadata.get("source_commit")!=manifest["source_commit"] or metadata.get("target")!=target or metadata.get("toolchains")!=pins:
            raise ValueError("Mismatched build provenance")
    expected=set(manifest["files"])|{"manifest.json","SHA256SUMS"}
    if manifest.get("signed") is True:expected.add("manifest.sig")
    if set(files)!=expected:raise ValueError("Unexpected or missing release files")
    for name, info in manifest["files"].items():
        if not isinstance(info,dict) or len(files[name])!=info.get("bytes") or digest(files[name])!=info.get("sha256"):
            raise ValueError(f"Checksum mismatch: {name}")
    sums="".join(f"{digest(files[name])}  {name}\n" for name in sorted(expected-{"SHA256SUMS"})).encode()
    if files["SHA256SUMS"]!=sums:raise ValueError("Checksum index mismatch")
    if require_signatures and manifest.get("signed") is not True:raise ValueError("Unsigned CI package cannot be used for OTA")
    pem,fingerprint=public_key()
    if files.get("release-public-key.pem")!=pem or manifest.get("signing_key_sha256")!=fingerprint:
        raise ValueError("Release key differs from the compiled trusted key")
    if manifest.get("signed") is True:verify_signature(files["manifest.json"],files["manifest.sig"],pem)
    for name,target in IMAGES.items():
        data=files[name]
        if target=="esp32p4":
            meta=p4_metadata(data,"recovery" in name)
            if manifest["images"][name]!=meta:raise ValueError("P4 image metadata mismatch")
            if manifest["source_commit"][:12].encode() not in data:raise ValueError("P4 embedded source revision differs from manifest")
        elif manifest["images"][name]!=pico_metadata(data):raise ValueError("Pico metadata mismatch")
        if manifest.get("signed") is True:
            check_signature(data,json.loads(files[name+".sig.json"]),target,pem)
        elif name+".sig.json" in files:raise ValueError("Ambiguous partially signed package")
    if manifest["images"]["iqdata_p4_recovery.bin"]["version"]!=manifest["images"]["iqdata_p4_gateway.bin"]["version"]+"-recovery":
        raise ValueError("P4 application and recovery versions do not match")
    return manifest


def read_package(path, require_signatures=False):
    with zipfile.ZipFile(path) as archive:
        entries=archive.infolist()
        if len(entries)>100 or sum(item.file_size for item in entries)>40*1024*1024:
            raise ValueError("Oversized release")
        if len({item.filename for item in entries})!=len(entries):raise ValueError("Duplicate archive entry")
        for item in entries:
            if item.is_dir() or item.filename.startswith("/") or ".." in Path(item.filename).parts or "\\" in item.filename:
                raise ValueError("Unsafe archive path")
        files={item.filename:archive.read(item) for item in entries}
    return files,verify_files(files,require_signatures)


def build(args):
    if args.output.exists():raise ValueError("Release destination already exists")
    source=subprocess.check_output(["git","rev-parse","HEAD"],cwd=ROOT,text=True).strip()
    if subprocess.check_output(["git","status","--porcelain","--untracked-files=no"],cwd=ROOT,text=True).strip():
        raise ValueError("Commit tracked source changes before packaging")
    pins=json.loads((PORT/"toolchains.json").read_text())
    inputs={"application":args.p4_build,"recovery":args.recovery_build,"pico2":args.pico_build}
    metadata={}
    for target,directory in inputs.items():
        meta=json.loads((directory/"build-metadata.json").read_text())
        if meta.get("schema")!=1 or meta.get("source_commit")!=source or meta.get("target")!=target or meta.get("toolchains")!=pins:
            raise ValueError(f"Mismatched source/toolchain provenance: {target}")
        metadata[target]=meta
    pem,fingerprint=public_key()
    if args.signing_key_file:
        if args.signing_key_file.stat().st_mode & 0o077:raise ValueError("Signing key must have mode 0600")
        der=subprocess.check_output(["openssl","pkey","-in",str(args.signing_key_file),"-pubout","-outform","DER"])
        if digest(der)!=fingerprint:raise ValueError("Private signing key does not match compiled verification key")
    paths={"iqdata_p4_gateway.bin":args.p4_build/"iqdata_p4_gateway.bin",
           "iqdata_p4_recovery.bin":args.recovery_build/"iqdata_p4_gateway.bin",
           "iqdata_pico_live.uf2":args.pico_build/"iqdata_pico_live.uf2",
           "bootloader.bin":args.p4_build/"bootloader/bootloader.bin",
           "partition-table.bin":args.p4_build/"partition_table/partition-table.bin",
           "ota_data_initial.bin":args.p4_build/"ota_data_initial.bin",
           "README.md":PORT/"docs/RELEASES.md","ATTRIBUTION.md":PORT/"ATTRIBUTION.md",
           "SECURITY.md":PORT/"docs/SECURITY.md","toolchains.json":PORT/"toolchains.json",
           "dependencies.lock":PORT/"dependencies.lock","partitions.csv":PORT/"partitions.csv"}
    for path in (PORT/"third_party/bacnet-stack/license").iterdir():
        if path.is_file():paths["licenses/bacnet-"+path.name]=path
    for path in (ROOT/"third_party").glob("*-LICENSE.txt"):
        paths["licenses/"+path.name]=path
    for path in (PORT/"licenses").iterdir():
        if path.is_file():paths["licenses/"+path.name]=path
    paths["licenses/cJSON-MIT.txt"]=PORT/"third_party/cJSON/LICENSE"
    files={name:path.read_bytes() for name,path in paths.items()}
    files["release-public-key.pem"]=pem
    # Reuse the actual production UF2 guard, including compatibility-block rules.
    subprocess.run([str(args.uf2_guard.resolve()),str(paths["iqdata_pico_live.uf2"].resolve())],check=True,stdout=subprocess.DEVNULL)
    image_meta={name:p4_metadata(files[name],"recovery" in name) for name in IMAGES if IMAGES[name]=="esp32p4"}
    image_meta["iqdata_pico_live.uf2"]=pico_metadata(files["iqdata_pico_live.uf2"])
    if args.signing_key_file:
        with tempfile.TemporaryDirectory() as temporary:
            for name,target in IMAGES.items():
                image=Path(temporary)/name;image.write_bytes(files[name])
                subprocess.run([sys.executable,str(PORT/"tools/sign_firmware.py"),"--key-file",str(args.signing_key_file.resolve()),"--target",target,str(image)],check=True,stdout=subprocess.DEVNULL)
                files[name+".sig.json"]=Path(str(image)+".sig.json").read_bytes()
    manifest={"schema":1,"source_commit":source,"signed":bool(args.signing_key_file),"signing_key_sha256":fingerprint,
        "images":image_meta,"builds":metadata,
        "compatibility":{"p4_board":"ESP32-P4-WIFI6-POE-ETH","silicon_revision":"1.0 through 2.x; pre-v3","flash_bytes":33554432,
                         "phy":"IP101 address 1","pico_board":"plain Pico 2","point_map":{"AI":106,"BI":92},
                         "ota_slot_bytes":4194304,"requires_existing_device_key_and_pinned_tls":True},
        "initial_flash":{"0x2000":"bootloader.bin","0x8000":"partition-table.bin","0xf000":"ota_data_initial.bin","0x20000":"iqdata_p4_gateway.bin"},
        "files":{name:{"bytes":len(data),"sha256":digest(data)} for name,data in sorted(files.items())}}
    files["manifest.json"]=(json.dumps(manifest,indent=2,sort_keys=True)+"\n").encode()
    if args.signing_key_file:
        files["manifest.sig"]=subprocess.check_output(["openssl","dgst","-sha256","-sign",str(args.signing_key_file.resolve())],input=files["manifest.json"])
    files["SHA256SUMS"]="".join(f"{digest(data)}  {name}\n" for name,data in sorted(files.items())).encode()
    verify_files(files,bool(args.signing_key_file))
    args.output.parent.mkdir(parents=True,exist_ok=True)
    with zipfile.ZipFile(args.output,"x",compression=zipfile.ZIP_DEFLATED) as archive:
        for name,data in sorted(files.items()):
            info=zipfile.ZipInfo(name,(2000,1,1,0,0,0));info.compress_type=zipfile.ZIP_DEFLATED
            info.external_attr=0o100644<<16;archive.writestr(info,data)
    print(json.dumps({"package":str(args.output),"source_commit":source,"signed":manifest["signed"],"sha256":digest(args.output.read_bytes())},indent=2))


def main():
    p=argparse.ArgumentParser(description=__doc__);sub=p.add_subparsers(dest="command",required=True)
    pack=sub.add_parser("build")
    for name in ("p4-build","recovery-build","pico-build","uf2-guard","output"):
        pack.add_argument("--"+name,type=Path,required=True)
    pack.add_argument("--signing-key-file",type=Path)
    check=sub.add_parser("verify");check.add_argument("package",type=Path);check.add_argument("--require-signatures",action="store_true")
    args=p.parse_args()
    if args.command=="build":build(args)
    else:
        _,manifest=read_package(args.package,args.require_signatures)
        print(json.dumps({"verified":True,"source_commit":manifest["source_commit"],"signed":manifest["signed"]},indent=2))


if __name__=="__main__":
    try:main()
    except (OSError,ValueError,KeyError,subprocess.CalledProcessError,zipfile.BadZipFile) as error:sys.exit(str(error))
