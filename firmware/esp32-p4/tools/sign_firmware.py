#!/usr/bin/env python3
"""Sign a target-bound firmware manifest with an external P-256 release key."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--key-file", type=Path, required=True)
    p.add_argument("--target", choices=("esp32p4", "pico2"), required=True)
    p.add_argument("image", type=Path)
    args = p.parse_args()
    if args.key_file.stat().st_mode & 0o077:
        p.error("Signing key must have mode 0600")
    data = args.image.read_bytes()
    if args.target == "esp32p4" and (len(data) < 512 or data[0] != 0xe9 or data[12:14] != b"\x12\x00" or data[80:112].split(b"\0")[0] != b"iqdata_p4_gateway"):
        p.error("Expected IQData ESP32-P4 application")
    if args.target == "pico2" and (len(data) < 1024 or len(data) % 512 or data[:8] != bytes.fromhex("5546320a57515d9e")):
        p.error("Expected Pico 2 UF2")
    digest = hashlib.sha256(data).hexdigest()
    context = f"IQDATA-IMAGE-V1\n{args.target}\n{len(data)}\n{digest}\n".encode()
    signature = subprocess.check_output(["openssl", "dgst", "-sha256", "-sign", str(args.key_file)], input=context)
    if not 64 <= len(signature) <= 72:
        p.error("Use an ECDSA P-256 signing key")
    output = Path(str(args.image) + ".sig.json")
    output.write_text(json.dumps({"schema": 1, "target": args.target, "bytes": len(data),
                                 "sha256": digest, "signature": signature.hex()}, indent=2) + "\n")
    print(output)


if __name__ == "__main__":
    main()
