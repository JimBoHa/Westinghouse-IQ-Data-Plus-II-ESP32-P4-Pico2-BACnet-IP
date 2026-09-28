#!/usr/bin/env python3
"""Install checksum-pinned Linux CI tools into the ignored build directory."""
import hashlib
import json
import os
from pathlib import Path
import platform
import subprocess
import tarfile
import urllib.request

ROOT=Path(__file__).resolve().parents[3]
pins=json.loads((ROOT/"firmware/esp32-p4/toolchains.json").read_text())
if platform.system()!="Linux" or platform.machine()!="x86_64":
    raise SystemExit("This installer is for Linux x86_64 CI only")
destination=ROOT/"build/ci-tools";destination.mkdir(parents=True,exist_ok=True)
archive=destination/"arm-toolchain.tar.xz";sha=hashlib.sha256()
with urllib.request.urlopen(pins["arm_gnu"]["linux_x86_64_url"],timeout=60) as response, archive.open("wb") as output:
    while data:=response.read(1024*1024):
        sha.update(data);output.write(data)
if sha.hexdigest()!=pins["arm_gnu"]["sha256"]:raise SystemExit("Arm toolchain checksum mismatch")
with tarfile.open(archive) as contents:contents.extractall(destination,filter="data")
sdk=destination/"pico-sdk"
subprocess.run(["git","clone","--depth","1","--branch",pins["pico_sdk"]["version"],"https://github.com/raspberrypi/pico-sdk.git",str(sdk)],check=True)
actual=subprocess.check_output(["git","rev-parse","HEAD"],cwd=sdk,text=True).strip()
if actual!=pins["pico_sdk"]["commit"]:raise SystemExit("Pico SDK commit mismatch")
subprocess.run(["git","submodule","update","--init","lib/tinyusb"],cwd=sdk,check=True)
toolchain=destination/"arm-gnu-toolchain-15.2.rel1-x86_64-arm-none-eabi"
with open(os.environ["GITHUB_ENV"],"a") as environment:
    environment.write(f"PICO_SDK_PATH={sdk}\nPICO_TOOLCHAIN_PATH={toolchain}\n")
