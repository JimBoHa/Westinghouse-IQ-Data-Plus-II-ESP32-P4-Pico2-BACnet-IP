#!/usr/bin/env python3
"""Restart recipient/scheduler tests using pinned codecs, never a network socket."""
from pathlib import Path
import os
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "third_party/bacnet-stack/src"
SOURCES = [
    "bacaction.c", "bacaddr.c", "bacapp.c", "bacdcode.c", "bacdest.c",
    "bacdevobjpropref.c", "bacint.c", "bacreal.c", "bacstr.c", "bacerror.c", "bactext.c",
    "cov.c", "datetime.c", "hostnport.c", "indtext.c", "memcopy.c",
    "npdu.c", "proplist.c", "timestamp.c", "wp.c", "abort.c", "dcc.c", "reject.c",
    "basic/sys/days.c",
]

if __name__ == "__main__":
    with tempfile.TemporaryDirectory(prefix="bacnet-restart-") as temporary:
        executable = Path(temporary) / "restart"
        subprocess.run([
            os.environ.get("CC", "cc"), "-std=c11", "-g", "-O1", "-Wall", "-Wextra",
            "-Wno-unused-function", "-Wno-format", "-Wno-sign-compare",
            "-fsanitize=address,undefined", "-fno-omit-frame-pointer",
            "-DBACDL_BIP=1", "-DBACAPP_MINIMAL=1", "-DBACAPP_TIMESTAMP=1",
            "-DBACAPP_HOST_N_PORT=1", "-DBACNET_STACK_DEPRECATED_DISABLE=1",
            "-DBACNET_PROTOCOL_REVISION=28", "-DPRINT_ENABLED=0", "-DBBMD_ENABLED=0",
            "-I" + str(SRC), "-I" + str(ROOT / "main"),
            str(ROOT / "tests/test_bacnet_restart.c"), str(ROOT / "main/bacnet_restart.c"),
            *(str(SRC / "bacnet" / source) for source in SOURCES), "-lm", "-o", str(executable),
        ], check=True)
        subprocess.run([str(executable)], check=True, timeout=30)
