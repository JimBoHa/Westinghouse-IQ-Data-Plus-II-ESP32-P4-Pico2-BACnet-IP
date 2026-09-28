# Ethernet management security

Firmware 0.3.0 serves management on HTTPS port 443. Port 80 only redirects GET
requests; it cannot configure or update firmware. Every device generates and
persists its own P-256 TLS key and certificate in the existing `iqconfig` NVS
partition. Invalid stored credentials stop startup validation; they are never
silently replaced. The release verification public key is compiled into the
application. No private release key or device TLS key is included in Git or a
release artifact.

## Pair and pin the device

The initial transition from 0.2.x uses its existing authenticated HTTP updater
once. Retain that client and the last working image privately before migration.
After transition, pair using the device's existing private token file:

```sh
python3 firmware/esp32-p4/tools/manage.py --host "$GATEWAY" \
  --expected-mac "$MAC" --token-file "$TOKEN_FILE" --pin-file "$PIN_FILE" pair
python3 firmware/esp32-p4/tools/manage.py --host "$GATEWAY" --pin-file "$PIN_FILE" status
```

Pairing sends a fresh public random challenge over TLS. The device proves its
exact TLS certificate fingerprint and Ethernet MAC with an HMAC using the
existing device token. The client verifies this proof against the certificate
actually presented on that connection before saving the pin. An intermediary
cannot substitute its own certificate and reuse the real device's proof.
The admin key is never transmitted. Later requests check the saved certificate
pin before sending any request. This replaces trust-on-first-use with proof of
possession of the previously provisioned key. It cannot remedy a key that was
already stolen before migration.

The browser uses normal HTTPS certificate trust. Verify the certificate SHA-256
against the paired pin before adding a device-specific browser exception or
trusting the certificate. Never enter the key into an unverified page. The
firmware does not install a system trust root on the commissioning computer.

## Request authentication

Mutations use a random, single-use challenge with a 60-second lifetime. Four
challenges may be outstanding. HMAC-SHA256 binds `IQDATA-AUTH-V1`, POST, exact
path, nonce, content length, and the body SHA-256, separated by newlines with a
final newline. The request uses `X-IQ-Nonce`, `X-IQ-Auth`, and `X-SHA256`.
Only a correctly authenticated request consumes a challenge. Body hashing
precedes configuration persistence or firmware activation. Replay, wrong key,
wrong path, wrong length, and modified payloads are rejected. `/api/auth/check`
tests an authenticated empty request without changing configuration.

## Signed firmware

Use a private ECDSA P-256 release key matching `main/iq_signing_key.h`:

```sh
python3 firmware/esp32-p4/tools/sign_firmware.py --key-file "$SIGNING_KEY" \
  --target esp32p4 build/esp32-p4/iqdata_p4_gateway.bin
python3 firmware/esp32-p4/tools/sign_firmware.py --key-file "$SIGNING_KEY" \
  --target pico2 build/pico2/iqdata_pico_live.uf2
```

Each image gets an adjacent `.sig.json` manifest. ECDSA/SHA-256 signs
`IQDATA-IMAGE-V1`, target (`esp32p4` or `pico2`), byte count, and file SHA-256,
newline separated with a final newline. The signature is DER encoded and sent
as lowercase hex in `X-Image-Signature`. Both endpoints require a valid release
signature before starting an update. Existing ESP chip/project and Pico UF2
family, image, and runtime identity checks remain in force. SHA-256 is checked
against the received bytes before P4 boot selection or any Pico reset/write.

All management commands now require `--pin-file`. Firmware updates additionally
need the adjacent signature manifest. Preserve matching private signing keys,
device token, pin, and a signed known-good application outside the checkout.
Signing-key rotation requires an explicitly staged firmware migration; replacing
the public key blindly would strand existing release clients.

This is application-level upload verification, not hardware Secure Boot.
Bootloader, partitions, eFuses, flash encryption, and hardware anti-rollback are
unchanged. BACnet/IP remains unauthenticated and read-only; use the site's
automation-network access controls. A physical flash owner can extract
unencrypted NVS. Authentication design follows the S3 reference's nonce/HMAC
pattern; signed HTTPS management follows the P4 reference described in
`ATTRIBUTION.md`.
