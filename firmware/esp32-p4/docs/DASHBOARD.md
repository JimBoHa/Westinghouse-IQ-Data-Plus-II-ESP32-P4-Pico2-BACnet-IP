# Browser dashboard

Open `https://<gateway-hostname>.local/` on the same network. Pair with the CLI
first and verify the browser certificate SHA-256 against the saved pin before
trusting the device certificate. Do not load a private key into an unverified
page. The dashboard uses only device-hosted assets and browser WebCrypto.

Overview reports Ethernet, Pico qualification, meter polling, duplicate Device
IDs, NTP, startup health, and restart notification outcomes. Points shows the
198 stable BACnet inputs, descriptions, engineering units, quality, and sample
age. Invalid values are hidden even when the API retains an older numeric
value. A connection failure clears displayed point values. Quality filters and
text search work locally. No measurement data is stored by the page or device.

Load the device's existing 64-hex-character private key file to unlock this
tab. WebCrypto imports it as a non-extractable HMAC key; requests bind the
single-use nonce, path, byte length, and body hash. The key itself is never sent
or put in cookies, localStorage, or sessionStorage. Lock management or close/
reload the tab to forget it. Browser access uses its verified HTTPS connection;
JavaScript cannot independently inspect the peer certificate like the CLI can.

Settings are reviewed before save. Saving safely stops the Pico interface and
restarts the gateway; the page verifies a changed boot ID, health acceptance,
and the saved configuration. For changed IP settings, reconnect by hostname
if the current page used the old IP. Keep polling disabled until meter wiring
and address are commissioned. Production Device 75151 and IP 192.168.75.151
remain protected by both page and firmware validation.

Maintenance accepts a firmware file and its matching `.sig.json` manifest.
The page checks target, size, hash and image header; the device verifies the
release signature and received bytes. ESP32-P4 success requires the exact
ELF hash in the other OTA slot with accepted startup health and a valid image.
Pico success requires the expected runtime identity to return and qualify.
The progress bar measures upload, not flash completion. Wait for the explicit
verified result. Recovery images expose gateway update and diagnostics only.

Diagnostics download and safe reboot also require nonce/HMAC authentication.
`manage.py ... reboot` exposes the same empty-body command. Diagnostic events
remain RAM-only. A random boot ID is exposed for precise reboot verification.

The physical acceptance test `tests/test_dashboard.py` requires
`playwright==1.55.0`, Chrome, existing private token/pin files, and both signed
images. It validates the TLS certificate pin before allowing that certificate's
SPKI in an isolated browser. It tests locked controls, bad-key rejection,
responsive layout, point filtering, protected configuration, same-settings
save/reboot, diagnostics, signed Pico/P4 updates, reboot, and key clearing.
