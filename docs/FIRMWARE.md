# Pico firmware

The supplied `firmware/pico-live/dist/iqdata_pico_live.uf2` is the tested
**Pico 2 W / RP2350** build, version **0.4.7**. Check the board's printed model.
It is not an original Pico/Pico W (RP2040) binary. Wi-Fi is unused; the Pi
connects by USB. This retained artifact predates the shared source's 0.4.8 CLK
qualification update. Plain Pico 2 builds and current physical test scope are
documented in the [P4 guide](../firmware/esp32-p4/README.md). The checksums below
cover the current source files and the separately retained 0.4.7 binary; that
legacy binary is not a build of the current source.

## Load the supplied image

Verify the shipped source and image checksums from the repository root:

```sh
(cd firmware/pico-live && sha256sum -c SHA256SUMS)
```

1. Disconnect the meter signal wires before disconnecting Pico power.
2. Hold BOOTSEL while connecting Pico USB to the Pi or another computer.
3. Copy `iqdata_pico_live.uf2` onto the **RP2350** USB storage volume. The board
   reboots into the USB serial application.
4. Find its device with `ls -l /dev/serial/by-id/`. Before running the recorder,
   use the following command, substituting the real device path and a fresh
   output filename:

```sh
.venv/bin/python live/host.py command info \
  --port /dev/serial/by-id/YOUR_PICO_DEVICE \
  --output /tmp/iqdata-info.json
cat /tmp/iqdata-info.json
```

Check `version: "0.4.7"`, `build_board: "pico2_w"`, `live_enabled: true` and
`synthetic_host: false`. The build-board field identifies the firmware target;
it is not independent identification of the attached hardware.
Then follow [power order and wiring](WIRING.md).
This is Raspberry Pi's documented [UF2/BOOTSEL process](https://www.raspberrypi.com/documentation/microcontrollers/c_sdk.html#your-first-binaries).

## Build from source on Raspberry Pi OS

The deployed build used Pico SDK **2.3.1**. These commands keep its checkout
separate from this repository:

```sh
sudo apt install cmake ninja-build gcc-arm-none-eabi libnewlib-arm-none-eabi libstdc++-arm-none-eabi-newlib libusb-1.0-0-dev build-essential pkg-config
git clone --branch 2.3.1 --depth 1 --recurse-submodules https://github.com/raspberrypi/pico-sdk.git "$HOME/pico-sdk"
export PICO_SDK_PATH="$HOME/pico-sdk"
cmake -S firmware/pico-live -B build/pico2w -G Ninja -DPICO_BOARD=pico2_w
cmake --build build/pico2w -j4
```

The UF2 is `build/pico2w/iqdata_pico_live.uf2`. SDK configuration can fetch/build
its compatible picotool when needed. Refer to the official
[C SDK guide](https://www.raspberrypi.com/documentation/microcontrollers/c_sdk.html)
and [picotool instructions](https://github.com/raspberrypi/picotool) for toolchain
installation or USB permissions on another operating system.

For an already running application, `picotool load -f -v FILE.uf2` is an
alternative to BOOTSEL. Stop the poll service first; it owns USB. The deployed
picotool load returned the application automatically; no second forced reboot
was needed. Do not flash or run a separate serial command while the poller runs.

## Timing and ownership

CLK/RW remain inputs. DATA/INT can pull LOW or release; firmware does not drive
them HIGH. Boot, timeout and completed trials release outputs. PIO handles
transaction edges; a watchdog bounds the worker. There is no synthetic host in
this implementation. The Pi requests a bounded operation over USB and checks
framing and reply provenance before decoding.

Normal acquisition uses `transact all_standard_repeat 0 rising 500` internally.
Extra reads are `flags_repeat`, `settings_repeat` and `trip_repeat`. Legacy
ordering probes remain development commands, not normal polling settings.
No meter settings/reset command is implemented. See [protocol](PROTOCOL.md).

## USB response delivery

Firmware 0.4.7 uses checked USB writes with a four-second deadline for the whole
response. Short writes resume at the unsent byte while the safety watchdog runs.
If the host stops reading or disconnects, the firmware releases meter outputs
and reboots to clear the incomplete stream; an incomplete trial cannot report
success. The meter transaction's own timing and deadline remain unchanged.

The transport loop has a native test for partial writes, repeated 250 ms reader
pauses, disconnection and deadline expiry:

```sh
cc -std=c11 -Wall -Wextra -Werror firmware/pico-live/usb_tx.c firmware/pico-live/test_usb_tx.c -o /tmp/iqdata-test-usb
/tmp/iqdata-test-usb
```

## Initial bus synchronization

Firmware 0.4.7 observes a fresh 20 µs idle interval after PIO setup. If RW falls
before capture has been enabled, it waits for another idle interval within the
original transaction deadline. It never begins capturing halfway through a
meter write. Once capture starts, ownership loss still stops the transaction.
The result includes `startup_retries_before` and `startup_retries_after` counters
for races before and during writer setup; no BACnet objects are added.

```sh
cc -std=c11 -Wall -Wextra -Werror firmware/pico-live/test_writer_arm.c -o /tmp/iqdata-test-arm
/tmp/iqdata-test-arm
```
