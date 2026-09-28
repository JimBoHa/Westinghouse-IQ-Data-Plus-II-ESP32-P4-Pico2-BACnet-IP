# Third-party notices

The ESP32-P4 target's pinned BACnet Stack, ESP-IDF components, USB host drivers,
and reused gateway code are documented in
[P4 source provenance](firmware/esp32-p4/ATTRIBUTION.md). Preserve the submodules'
license directories and the reused source notices when distributing this port.

The supplied UF2 was built with Raspberry Pi Pico SDK2.3.1. Notices for its
SDK, TinyUSB, printf and toolchain components are retained under `third_party/`.
These licenses describe those components; they do not grant additional rights
to unrelated project code or manufacturer documents.

- Raspberry Pi Pico SDK: [BSD3-clause notice](third_party/pico-sdk-LICENSE.txt).
- TinyUSB: [MIT notice](third_party/tinyusb-LICENSE.txt).
- Marco Paland's printf implementation: [MIT notice](third_party/printf-LICENSE.txt).
- Newlib: [component copyright/license notices](third_party/newlib-copyright.txt).
- GCC runtime/toolchain: [copyright and runtime exception](third_party/gcc-copyright.txt).

Python dependencies are installed separately: BACpypes3, pyserial, and optional
NumPy retain their respective upstream licenses. They are not vendored here.
The guides link manufacturer manuals rather than redistributing them.
