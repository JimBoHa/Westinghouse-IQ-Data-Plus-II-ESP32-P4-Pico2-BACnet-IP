# Build, verify, and install an IQData firmware release

This bundle targets **ESP32-P4-WIFI6-POE-ETH, pre-v3 silicon (revisions 1.x/2.x),
32 MiB flash and PSRAM, IP101 PHY address 1**, with a **plain Pico 2** on USB-A.
MDC is GPIO31, MDIO GPIO52, PHY reset GPIO51, RMII clock input GPIO50.
It is not a build for Pico 2 W or an arbitrary ESP32 board. Preserve the stable
106 AI + 92 BI point map. The protected production gateway must not be accessed.

## CI and provenance

The `Firmware CI` workflow runs on pull requests and the port's default branch.
It builds native sanitizer tests, the full P4 application, Ethernet recovery
application, and Pico UF2. `toolchains.json` pins ESP-IDF 5.5.5 by commit and
container digest, Pico SDK 2.3.1 and picotool by commit, and Arm GNU 15.2.Rel1
by download SHA-256. Repository submodules and `dependencies.lock` pin the
remaining components. Actions are pinned to commit SHAs and have read-only
repository permissions. Builds never receive a private signing/device key.

CI artifacts are **unsigned candidates**. Their checksums detect corruption;
they are not authorized OTA releases. A release operator downloads the three
matching build artifacts, checks out their exact clean source commit, and signs
the package locally with the previously provisioned private release key:

```sh
python3 firmware/esp32-p4/tools/package_release.py build \
  --p4-build /private/path/p4-application \
  --recovery-build /private/path/p4-recovery \
  --pico-build /private/path/pico2 \
  --uf2-guard build/p4-native/uf2_guard \
  --signing-key-file "$SIGNING_KEY" \
  --output /private/path/iqdata-signed-release.zip
python3 firmware/esp32-p4/tools/package_release.py verify \
  /private/path/iqdata-signed-release.zip --require-signatures
```

`record_build.py` writes provenance for each clean local/CI build. Packaging
rejects mixed source commits/toolchains, wrong application/recovery roles,
oversized/wrong-chip images, bad UF2, and a private key that differs from the
compiled public key. `manifest.json` records exact versions, source, image ELF
hashes, compatibility, initial-flash offsets, and every payload's size/SHA-256.
Signed releases authenticate both individual images and the whole manifest.
`SHA256SUMS` covers payloads, manifest, and its signature. Verification rejects
missing/extra/duplicate files, unsafe paths, and corrupt or invalid signatures.
The ZIP is deterministic for identical inputs. It contains no NVS image,
device configuration, TLS private key, management token, or release private key.

Toolchain sources: [Espressif's IDF image](https://hub.docker.com/r/espressif/idf),
[Pico SDK 2.3.1](https://github.com/raspberrypi/pico-sdk/releases/tag/2.3.1), and
[Arm GNU installation guide](https://learn.arm.com/install-guides/gcc/arm-gnu/).
Retained dependency license texts and source provenance accompany the bundle.

## Ethernet updates

Verify the signed package before extracting it. Keep each application beside
its matching `.sig.json` file. Use the verified browser dashboard or:

```sh
python3 firmware/esp32-p4/tools/manage.py --host "$GATEWAY" \
  --expected-mac "$MAC" --token-file "$TOKEN_FILE" --pin-file "$PIN_FILE" \
  update /private/path/release/iqdata_p4_gateway.bin
python3 firmware/esp32-p4/tools/manage.py --host "$GATEWAY" \
  --expected-mac "$MAC" --token-file "$TOKEN_FILE" --pin-file "$PIN_FILE" \
  pico-update /private/path/release/iqdata_pico_live.uf2
```

Use only `iqdata_p4_gateway.bin` or the recovery application for P4 OTA.
Bootloader, partition table, and OTA-selection data are **not OTA payloads**.
The P4 client waits for the exact ELF hash in the other slot with a valid image;
the Pico client requires the expected runtime identity to return. Keep power
connected. No eFuses, hardware secure boot, encryption, anti-rollback, or chip
erase are part of this process. Existing settings/certificate/key remain in NVS.

## Recovery and initial serial flash

Retain a verified signed known-good bundle outside the checkout. To revert a
reachable gateway, upload its prior signed application. Pending-image startup
failure automatically rolls back; faults discovered after acceptance require
an explicit update. The signed `iqdata_p4_recovery.bin` provides Ethernet
management from existing provisioned NVS while BACnet/Pico operation is absent.
Install the full application afterward. Recovery itself has build coverage;
do not infer physical qualification unless a hardware report explicitly says so.

If management cannot be reached, identify the correct development board by
USB/serial/MAC and preserve its flash/configuration before using USB-C. With
the pinned ESP-IDF esptool environment, the existing partition layout can be
restored from the extracted directory using:

```sh
python -m esptool --chip esp32p4 --port "$P4_PORT" \
  --before default_reset --after hard_reset write_flash \
  --flash_mode dio --flash_size 32MB --flash_freq 40m \
  0x2000 bootloader.bin 0x8000 partition-table.bin \
  0xf000 ota_data_initial.bin 0x20000 iqdata_p4_gateway.bin
```

This resets OTA selection but does not write the `iqconfig` partition at
0x1000000. Do not erase the whole chip. Initial commissioning still needs its
unique identity/key. Keep meter signals disconnected during power/recovery
tests; Pico must stay powered whenever meter signals are attached.

Compilation, package verification, and network tests do not qualify physical
meter wiring or measurement accuracy. Record real hardware tests and their
duration separately. A short soak does not establish 24-hour stability.
