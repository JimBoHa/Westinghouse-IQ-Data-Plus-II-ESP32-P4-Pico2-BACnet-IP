# Project commands and hardware constraints

The ESP32-P4 port is in `firmware/esp32-p4`; the original Raspberry Pi implementation remains as its reference. Start from upstream commit `1add86003bddecb69c1c7ac28c5efd70751febff`. Never push the port to the original upstream repository.

Use ESP-IDF **5.5.5**, Pico SDK **2.3.1**, and the pinned submodules/component lock. On this Mac, put `/opt/homebrew/bin` before MacPorts in PATH. Run commands from the repository root:

```sh
git submodule update --init --recursive
python3 firmware/esp32-p4/tools/generate_points.py --check
cmake -S firmware/esp32-p4/tests -B build/p4-native -G Ninja
cmake --build build/p4-native
ctest --test-dir build/p4-native --output-on-failure
# After sourcing ESP-IDF's export.sh:
idf.py -C firmware/esp32-p4 -B build/esp32-p4 -DIQ_RECOVERY_BUILD=OFF build
# PICO_SDK_PATH and PICO_TOOLCHAIN_PATH must point to the pinned SDK/toolchain:
cmake -S firmware/pico-live -B build/pico2 -G Ninja -DPICO_BOARD=pico2 -DCMAKE_BUILD_TYPE=Release
cmake --build build/pico2
build/p4-native/uf2_guard build/pico2/iqdata_pico_live.uf2
```

Do not access, reconfigure, reboot, flash, or query the working gateway at **192.168.75.151 / Device 75151**. Never copy its database. Check development-device identity before writes. Preserve firmware/configuration backups outside the repository. Never erase the whole chip routinely, burn eFuses, or enable secure boot/encryption/anti-rollback during development.

Board: **ESP32-P4-WIFI6-POE-ETH**, ESP32-P4 rev 1.3, 32 MiB flash and PSRAM, IP101 PHY at address 1; MDC 31, MDIO 52, reset 51. USB-C is programming UART; USB-A is the Pico host. Keep the Pico on USB-A: user requires its firmware updates through ESP32 Ethernet. The meter was absent at bring-up. Preserve polling-disabled commissioning until a meter address and safe electrical setup are established.

Meter wiring is proprietary CLK/RW/DATA/INT on Pico GP0–GP3. DATA/INT must remain LOW-or-released, never driven HIGH. No UART/MAX3232 substitution, P4 direct signal wiring, or meter 26 V connection. Keep Pico powered whenever meter signals are attached; detach signals before removing power. Firmware maintenance must abort/drain transactions and leave outputs released.

Never add fixture playback to production firmware. Keep 106 AI + 92 BI stable, with truthful freshness/reliability and read-only BACnet. No measurement persistence or database in P4 firmware. Keep credentials, site configurations, device backups and captures private. Report actual hardware tests separately from native/synthetic checks; do not claim full meter validation without the meter.
