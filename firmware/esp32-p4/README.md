# IQ Data Plus II on ESP32-P4 and plain Pico 2

Development port derived from the original Raspberry Pi gateway. Source for the Raspberry Pi remains unchanged. This target runs C on ESP-IDF 5.5.5, hosts the existing Pico USB protocol, and exposes the original 198 BACnet points. See `ATTRIBUTION.md` for dependency and source provenance. Physical meter qualification is separate from the firmware and network tests.

See [validation status](VALIDATION.md) for the tests actually completed and the remaining meter qualification.

## Build

Initialize pinned submodules, install ESP-IDF 5.5.5, and source its `export.sh`. From the repository root:

```sh
git submodule update --init --recursive
python3 firmware/esp32-p4/tools/generate_points.py --check
idf.py -C firmware/esp32-p4 -B build/esp32-p4 -DIQ_RECOVERY_BUILD=OFF build
```

The application is `build/esp32-p4/iqdata_p4_gateway.bin`. Use that **application image** for Ethernet updates, not a merged flash image. The initial USB flash also installs the bootloader, partition table and OTA data:

```sh
idf.py -C firmware/esp32-p4 -B build/esp32-p4 -p "$P4_PORT" -b 921600 flash
```

Back up the identified device first. Initial flashing resets OTA selection; subsequent network updates preserve configuration. Two 4 MiB application slots are used. Configuration and a unique update token live in the separate 64 KiB `iqconfig` NVS partition at `0x1000000`. Firmware never erases NVS automatically on initialization errors.

For plain Pico 2, set `PICO_SDK_PATH` to SDK 2.3.1 and `PICO_TOOLCHAIN_PATH` to an Arm embedded toolchain directory (verified build: Arm GNU 15.2.Rel1, GCC 15.2.1):

```sh
cmake -S firmware/pico-live -B build/pico2 -G Ninja -DPICO_BOARD=pico2 -DCMAKE_BUILD_TYPE=Release
cmake --build build/pico2
```

Artifact: `build/pico2/iqdata_pico_live.uf2`, firmware 0.4.7, RP2350 ARM Secure image type. “ARM Secure” is the normal RP2350 execution state, not provisioning secure boot or OTP keys. Do not use the original Pico 2 W artifact for this target.

## Wiring and power

Target board: ESP32-P4-WIFI6-POE-ETH, pre-v3 silicon, 32 MiB flash and PSRAM. IP101 address 1 uses MDC GPIO31, MDIO GPIO52, reset GPIO51, external RMII clock GPIO50 and the IDF P4 default RMII data pins. USB-C is the CH343 programming/power connection; USB-A connects directly to Pico 2. Avoid a high-speed hub between the P4 and full-speed Pico. Ethernet/PoE powers the gateway and Pico after setup.

Follow the original `docs/WIRING.md` and `docs/PROTOCOL.md`. Pico GP0=CLK, GP1=RW, GP2=DATA, GP3=INT; DATA/INT are LOW-or-released. The meter connection is neither UART nor Modbus. Do not connect the meter's 26 V terminal. Keep Pico powered while meter signals are attached. Disconnect meter signals before removing Pico power or doing power-failure tests.

## Commissioning and Ethernet management

A new P4 starts with DHCP and hostname `iqdata-<last-three-base-MAC-bytes>.local`. It does not advertise BACnet until an identity is commissioned. Use DHCP leases or mDNS to find it. `/api/status` reports identity, network, firmware, free memory, Pico state and OTA slot. `/api/points` returns values together with validity; a retained numeric value with `valid:false` must not be treated as a reading.

At initial serial setup (115200 baud), `status` returns JSON; `token` returns the device token; `key <64 lowercase hex characters>` replaces it. Store the token in a private mode-0600 file outside the repository. Avoid capturing token responses in shared logs. `config <JSON>` saves settings and restarts. `reboot` restarts the P4 after releasing the Pico transaction.

Use the host management tool with the **Ethernet MAC from status** and a private token file:

```sh
python3 firmware/esp32-p4/tools/manage.py --host "$GATEWAY" status
python3 firmware/esp32-p4/tools/manage.py --host "$GATEWAY" --expected-mac "$MAC" --token-file "$TOKEN_FILE" configure /private/path/gateway.json
python3 firmware/esp32-p4/tools/manage.py --host "$GATEWAY" --expected-mac "$MAC" --token-file "$TOKEN_FILE" update build/esp32-p4/iqdata_p4_gateway.bin
python3 firmware/esp32-p4/tools/manage.py --host "$GATEWAY" --expected-mac "$MAC" --token-file "$TOKEN_FILE" pico-update build/pico2/iqdata_pico_live.uf2
```

Example configuration (select an unused identity and verify the meter address before enabling polling):

```json
{"device_instance":75201,"name":"IQData-P4-01","dhcp":true,"bacnet_port":47808,"meter_address":0,"poll_enabled":false}
```

For four gateways, reserve distinct instances such as **75201–75204**, names `IQData-P4-01` through `IQData-P4-04`, and one DHCP reservation or validated static address per MAC. Check these identities are unused on the deployment network. For static networking supply `dhcp:false`, `ip`, `mask`, and `gateway`. The working gateway's **192.168.75.151 / 75151** is explicitly protected. No copied production address is a default.

Management uses HTTP on a trusted local network with a per-device bearer token; it is not TLS. Restrict it to the management LAN. Uploads require the complete file's `X-SHA256`. The P4 updater checks target chip/project and image integrity before changing boot selection. The new image must pass local startup checks; otherwise the bootloader can roll back. Meter/Ethernet availability is not required for the local startup self-check.

Pico uploads are buffered in bounded RAM, hash checked, and restricted to contiguous plain-Pico-2 IQData 0.4.7 UF2 images. The P4 quiesces polling, aborts/drains output, uses the SDK reset interface to enter BOOTSEL, verifies RP2350 USB/volume identity, and copies the UF2 to the ROM volume. Pico remains powered. Success requires the expected firmware/board identity to reappear over CDC. The optional picotool absolute-family compatibility block is omitted; only RP2350 ARM application blocks are sent. This is not a Pico A/B rollback mechanism. An interrupted or nonbooting Pico application may require BOOTSEL recovery if it cannot expose its reset interface.

An optional Ethernet-only recovery application can be built with `-B build/p4-recovery -DIQ_RECOVERY_BUILD=ON`. It requires already-provisioned NVS and retains management access while BACnet and meter polling are unavailable. It is intended as a recovery step before restoring the full application.

Keep the last working application binary and its build revision outside the checkout. To revert a running gateway, use the same `update` command with that previous application image; configuration stays in NVS. Bootloader rollback covers a new application that resets before passing the local startup self-check. It does not undo a later-discovered behavioral fault after the image is marked valid. If Ethernet management is unavailable, reconnect the P4 programming USB-C and use the initial flash command with a known-good build; that rewrites OTA selection but leaves `iqconfig` untouched. Confirm board identity and preserve backups first. Do not routinely erase the full flash or change security fuses.

## Data behavior

`main/iq_points.c` is generated from `live/BACNET_POINT_MAP.csv`: 106 AI and 92 BI, plus Device and NetworkPort. Names, identifiers, descriptions, units, scales and COV increments remain stable. Discovery, RP, RPM, confirmed/unconfirmed COV and subscription expiry are implemented; all BACnet writes are denied. COV leases live in RAM and clients must resubscribe after a reboot. No BBMD/foreign-device management is provided.

Read transactions are limited to `all_standard`, `flags`, `settings`, and `trip`, with 500 ms Pico transactions and a bounded host deadline. Firmware checks firmware identity, ownership/provenance, request/completion images, counts, framing, repeat timing and terminal status. Unsupported or disconnected hardware never produces synthetic healthy meter readings. Diagnostic values fault independently; invalid fields do not contaminate valid siblings. Linux-only service-history health has no invented persistent equivalent.

Measurements and rolling histories exist only in RAM. Model storage is approximately 1.4 MiB, with bounded power/energy histories. Rolling 15-minute/hour/day values remain invalid until the corresponding continuous window is available. Restart, gaps, invalid sources and energy-counter decrease break the appropriate continuity. No database, measurement snapshot, recording or history-download feature is linked into the P4 application.

The exact CSV identifiers and descriptions are retained for compatibility. Health counters AI400–AI408 count this P4 session; the upstream descriptions refer to persistent `poll_health` state, which this database-free port does not retain across restarts. BI2 (display verified) stays inactive because this port has no recorded physical-display comparison. Linux service history has no P4 equivalent. These limitations must be considered when interpreting the unchanged point catalog.

## Tests

```sh
cmake -S firmware/esp32-p4/tests -B build/p4-native -G Ninja
cmake --build build/p4-native
ctest --test-dir build/p4-native --output-on-failure
build/p4-native/uf2_guard build/pico2/iqdata_pico_live.uf2
```

The native tests use address/undefined-behavior sanitizers, compare C decoding and calculations with the retained Python implementation, validate provenance and freshness, and reject malformed UF2 images. Synthetic inputs are native-only.

An accelerated 90,001-sample test spans more than 24 hours of synthetic time at 1.05-second cadence. It checks full-window warm-up, ring wrap, interpolation, counter decreases, transport gaps and recovery. This is not a physical 24-hour soak test.

The independent network client uses pinned `bacpypes3` from the original requirements. Specify a safe local interface and the commissioned development instance explicitly:

```sh
python3 firmware/esp32-p4/tests/test_bacnet.py --address "$CLIENT_IP/24:47808" --instance 75298 --target "$GATEWAY_IP" --device 75201 --broadcast --no-meter --output /private/path/bacnet-report.json
```

With meter signals physically disconnected and polling initially disabled,
the following test temporarily enables bounded requests, checks BACnet under
load, updates Pico during fault recovery, and restores the original configuration:

```sh
python3 firmware/esp32-p4/tests/test_no_meter.py --target "$GATEWAY_IP" --expected-mac "$MAC" --token-file "$TOKEN_FILE" --client-address "$CLIENT_IP/24:47808" --meter-disconnected --pico-uf2 build/pico2/iqdata_pico_live.uf2 --output /private/path/no-meter-report.json
```

Meter qualification still requires safe wiring verification, bounded real reads, raw/decoded/display comparison, fault recovery with Pico powered, a continuous 15-minute physical run, and separate hour/day/long-soak evidence. Compilation and native fixtures do not establish those results.
