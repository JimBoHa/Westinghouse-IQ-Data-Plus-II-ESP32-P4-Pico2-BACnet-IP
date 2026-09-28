#!/usr/bin/env python3
"""Record actual pinned build provenance beside firmware, never device state."""
import argparse
import json
import os
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[3]
PORT = ROOT/"firmware/esp32-p4"


def git(*args, cwd=ROOT):
    return subprocess.check_output(["git",*args],cwd=cwd,text=True).strip()


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("target",choices=("application","recovery","pico2"))
    p.add_argument("build",type=Path)
    args = p.parse_args()
    if git("status","--porcelain","--untracked-files=no"):
        p.error("Commit tracked source changes before recording release provenance")
    pins = json.loads((PORT/"toolchains.json").read_text())
    result = {"schema":1,"target":args.target,"source_commit":git("rev-parse","HEAD"),
              "submodules":git("submodule","status").splitlines(),"toolchains":pins}
    if args.target == "pico2":
        sdk = Path(os.environ["PICO_SDK_PATH"])
        toolchain = Path(os.environ["PICO_TOOLCHAIN_PATH"])
        assert git("rev-parse","HEAD",cwd=sdk) == pins["pico_sdk"]["commit"]
        assert git("rev-parse","HEAD",cwd=args.build/"_deps/picotool-src") == pins["pico_sdk"]["picotool_commit"]
        version = subprocess.check_output([str(toolchain/"bin/arm-none-eabi-gcc"),"--version"],text=True).splitlines()[0]
        assert "15.2.Rel1" in version and "15.2.1" in version, version
        result["compiler"] = version
        result["tinyusb_commit"] = git("rev-parse","HEAD",cwd=sdk/"lib/tinyusb")
        assert "PICO_BOARD:STRING=pico2" in (args.build/"CMakeCache.txt").read_text()
    else:
        assert git("rev-parse","HEAD",cwd=Path(os.environ["IDF_PATH"])) == pins["esp_idf"]["commit"]
        config = (PORT/"sdkconfig").read_text()
        for required in ("CONFIG_IDF_TARGET=\"esp32p4\"","CONFIG_ESP32P4_SELECTS_REV_LESS_V3=y",
                         "CONFIG_ESPTOOLPY_FLASHSIZE_32MB=y","CONFIG_BOOTLOADER_APP_ROLLBACK_ENABLE=y"):
            assert required in config, required
        for forbidden in ("CONFIG_SECURE_BOOT=y","CONFIG_SECURE_FLASH_ENC_ENABLED=y","CONFIG_BOOTLOADER_APP_ANTI_ROLLBACK=y"):
            assert forbidden not in config, forbidden
        result["idf_version"] = pins["esp_idf"]["version"]
    (args.build/"build-metadata.json").write_text(json.dumps(result,indent=2)+"\n")


if __name__ == "__main__":main()
