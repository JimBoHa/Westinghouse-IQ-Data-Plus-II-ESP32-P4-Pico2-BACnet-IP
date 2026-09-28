"""Pinned HTTPS transport and one-use request authentication. No third-party packages."""
import hashlib
import hmac
import http.client
import ipaddress
import json
import os
from pathlib import Path
import secrets
import socket
import ssl


def private_key(path):
    path = Path(path)
    if path.stat().st_mode & 0o077:
        raise ValueError("Key file must have mode 0600")
    text = path.read_text().strip()
    if len(text) != 64 or any(c not in "0123456789abcdef" for c in text):
        raise ValueError("Key file must contain 64 lowercase hex characters")
    return bytes.fromhex(text)


def auth_headers(key, path, nonce, body, digest=None):
    digest = digest or hashlib.sha256(body).hexdigest()
    context = f"IQDATA-AUTH-V1\nPOST\n{path}\n{nonce}\n{len(body)}\n{digest}\n"
    return {"X-IQ-Nonce": nonce, "X-SHA256": digest,
            "X-IQ-Auth": hmac.new(key, context.encode(), hashlib.sha256).hexdigest(),
            "Content-Type": "application/octet-stream"}


class Gateway:
    def __init__(self, host, pin_file=None, token_file=None, expected_mac=None, timeout=10):
        if any(c in host for c in "/:@?#"):
            raise ValueError("Host must be a hostname or IPv4 address")
        addresses = {r[4][0] for r in socket.getaddrinfo(host, 443, socket.AF_INET, socket.SOCK_STREAM)}
        if len(addresses) != 1:
            raise ValueError("Host must resolve to exactly one IPv4 address")
        self.address = addresses.pop()
        ip = ipaddress.ip_address(self.address)
        if self.address == "192.168.75.151" or not ip.is_private or ip.is_multicast or ip.is_unspecified:
            raise ValueError("Use an identified development gateway on the private LAN")
        self.timeout = timeout
        self.key = private_key(token_file) if token_file else None
        self.expected_mac = expected_mac.lower() if expected_mac else None
        self.pin_path = Path(pin_file) if pin_file else None
        self.pin = json.loads(self.pin_path.read_text()) if self.pin_path and self.pin_path.exists() else None
        if self.pin and (self.pin.get("schema") != 1 or len(self.pin.get("certificate_sha256", "")) != 64):
            raise ValueError("Invalid certificate pin file")
        if self.pin and self.expected_mac and self.pin.get("ethernet_mac") != self.expected_mac:
            raise ValueError("Certificate pin belongs to a different Ethernet MAC")
        self.verified = False

    def raw(self, path, body=None, headers=None, *, pairing=False, timeout=None):
        if not path.startswith("/api/") or "\r" in path or "\n" in path:
            raise ValueError("Invalid API path")
        if not self.pin and not (pairing and path == "/api/pair"):
            raise ValueError("Pair first and supply --pin-file for pinned HTTPS")
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE  # Exact pin checked before sending a request.
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        connection = http.client.HTTPSConnection(self.address, timeout=timeout or self.timeout, context=context)
        try:
            connection.connect()
            fingerprint = hashlib.sha256(connection.sock.getpeercert(binary_form=True)).hexdigest()
            if self.pin and not hmac.compare_digest(fingerprint, self.pin["certificate_sha256"]):
                raise ValueError("HTTPS certificate differs from the commissioned pin")
            connection.request("POST" if body is not None else "GET", path, body, headers or {})
            response = connection.getresponse()
            data = response.read(2 * 1024 * 1024 + 1)
            if len(data) > 2 * 1024 * 1024:
                raise ValueError("Oversized gateway response")
            return response.status, data, fingerprint
        finally:
            connection.close()

    def pair(self):
        if not self.key or not self.expected_mac or not self.pin_path:
            raise ValueError("Pairing requires token file, expected MAC, and pin-file destination")
        nonce = secrets.token_hex(32)
        code, body, fingerprint = self.raw("/api/pair", nonce.encode(), pairing=True)
        if code != 200:
            raise ValueError(f"Pairing failed: HTTP {code}")
        reply = json.loads(body)
        context = f"IQDATA-TLS-PAIR-V1\n{nonce}\n{self.expected_mac}\n{fingerprint}\n"
        expected = hmac.new(self.key, context.encode(), hashlib.sha256).hexdigest()
        if reply.get("ethernet_mac") != self.expected_mac or reply.get("certificate_sha256") != fingerprint or not hmac.compare_digest(expected, reply.get("proof", "")):
            raise ValueError("Device could not prove its certificate with the existing admin key")
        pin = {"schema": 1, "ethernet_mac": self.expected_mac, "certificate_sha256": fingerprint}
        if self.pin and self.pin != pin:
            raise ValueError("Pairing would replace an existing device identity")
        if not self.pin:
            fd = os.open(self.pin_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w") as output:
                json.dump(pin, output, indent=2)
                output.write("\n")
        self.pin = pin
        return pin

    def request(self, path, body=None, *, authenticated=False, headers=None, timeout=None):
        headers = dict(headers or {})
        if authenticated:
            if not self.key or not self.expected_mac:
                raise ValueError("Writes require token file and expected Ethernet MAC")
            if not self.verified:
                self.status()
            if body is None:
                body = b""
            challenge = self.request("/api/auth/challenge")
            headers.update(auth_headers(self.key, path, challenge["nonce"], body))
        code, data, _ = self.raw(path, body, headers, timeout=timeout)
        if code != 200:
            raise RuntimeError(f"Gateway HTTP {code}: {data[:512].decode(errors='replace')}")
        return json.loads(data)

    def status(self):
        state = self.request("/api/status")
        mac = state.get("ethernet_mac", "").lower()
        if state.get("project") != "iqdata_p4_gateway" or state.get("config", {}).get("device_instance") == 75151:
            raise ValueError("Target is not the allowed IQData development gateway")
        if (self.expected_mac and mac != self.expected_mac) or mac != self.pin["ethernet_mac"]:
            raise ValueError("Target Ethernet MAC differs from the commissioned identity")
        self.verified = True
        return state


def image_signature(path, target):
    path = Path(path)
    data = path.read_bytes()
    signed = json.loads(Path(str(path) + ".sig.json").read_text())
    if signed.get("schema") != 1 or signed.get("target") != target or signed.get("bytes") != len(data) or signed.get("sha256") != hashlib.sha256(data).hexdigest():
        raise ValueError("Signature manifest does not match this image and target")
    signature = signed.get("signature", "")
    if not 128 <= len(signature) <= 144 or len(signature) % 2 or any(c not in "0123456789abcdef" for c in signature):
        raise ValueError("Invalid ECDSA signature encoding")
    return data, {"X-Image-Signature": signature}
