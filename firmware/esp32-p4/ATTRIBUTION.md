# Source provenance

The meter decoding, diagnostics and transport rules are ported from [Westinghouse-IQ-Data-Plus-II-to-BACnet-IP](https://github.com/JimBoHa/Westinghouse-IQ-Data-Plus-II-to-BACnet-IP) at `1add86003bddecb69c1c7ac28c5efd70751febff`. Original Git history is retained. The initial plain Pico 2 build used the existing 0.4.7 source without timing/PIO changes. The local 0.4.8 update adds active PIO diagnostics and CLK qualification before advancing DATA or clearing INT. The retained Pico 2 W binary in `firmware/pico-live/dist` is the original 0.4.7 artifact, not the new source build.

The BACnet integration structure, UDP transport and compatibility headers were adapted from [ESP32-P4-Modbus-IP-to-BACnet-IP-Protocol-Converter](https://github.com/JimBoHa/ESP32-P4-Modbus-IP-to-BACnet-IP-Protocol-Converter) at `0729b7b96d0da99bfce14fd174cd39c21537ea2e`. Its 0BSD SPDX notices and MIT compatibility-header notices are retained. No production configuration, signing keys or production binaries were copied. The Modbus/ATS polling and its 128-point map are not part of this port.

Pinned dependencies retain their own licenses:

- BACnet Stack 1.6.0: `9bc3cfa07aab98852de432fa24079f4b4b6b7eed`; see its license notices and linking exception.
- cJSON 1.7.19: `c859b25da02955fef659d658b8f324b5cde87be3`; MIT; native tests only. Firmware uses ESP-IDF's cJSON component.
- ESP-IDF 5.5.5: `b774170ff46c393eeb5e495ea37936038d3f4f4f`; dependency licenses are in the SDK.
- Espressif CDC ACM 2.4.1, MSC 1.1.4, mDNS 1.9.1: pinned in `main/idf_component.yml` and `dependencies.lock`.
- Pico SDK 2.3.1 and its TinyUSB dependency: Raspberry Pi SDK license notices.

Board references: [Waveshare documentation](https://docs.waveshare.com/ESP32-P4-WIFI6-POE-ETH), [schematic](https://files.waveshare.com/wiki/ESP32-P4-WIFI6-POE-ETH/ESP32-P4-WIFI6-POE-ETH-Schematic.pdf), [Espressif USB host documentation](https://docs.espressif.com/projects/esp-idf/en/v5.5.4/esp32p4/api-reference/peripherals/usb_host.html), and [Raspberry Pi USB IDs](https://github.com/raspberrypi/usb-pid/blob/main/Readme.md).

The reference gateway traces its BACnet integration to JimBoHa's
`ESP32-S3-PoE-ETH-8DI-8RO-C-BACnet-IP-Firmware` at
`97c46a33dcc39ceee794c962dfbc0d94efd2788a`. The retained MIT compatibility header
uses the [included MIT permission text](third_party/bacnet-stack/license/MIT).
BACnet Stack's other per-file licenses and linking exception are preserved in
its `license/` directory.

## 0BSD permission text for reused project code

Permission to use, copy, modify, and/or distribute this software for any
purpose with or without fee is hereby granted.

THE SOFTWARE IS PROVIDED "AS IS" AND THE AUTHOR DISCLAIMS ALL WARRANTIES
WITH REGARD TO THIS SOFTWARE INCLUDING ALL IMPLIED WARRANTIES OF
MERCHANTABILITY AND FITNESS. IN NO EVENT SHALL THE AUTHOR BE LIABLE FOR
ANY SPECIAL, DIRECT, INDIRECT, OR CONSEQUENTIAL DAMAGES OR ANY DAMAGES
WHATSOEVER RESULTING FROM LOSS OF USE, DATA OR PROFITS, WHETHER IN AN ACTION
OF CONTRACT, NEGLIGENCE OR OTHER TORTIOUS ACTION, ARISING OUT OF OR IN
CONNECTION WITH THE USE OR PERFORMANCE OF THIS SOFTWARE.

## BACnet restart notification core

`main/bacnet_restart.c`, its header, and the standalone codec tests are adapted
from JimBoHa/ESP32-S3-PoE-ETH-8DI-8RO-C-BACnet-IP-Firmware commit
`97c46a33dcc39ceee794c962dfbc0d94efd2788a`, under 0BSD (license text above).
IQData integrates only the standard default local-broadcast recipient and keeps
all BACnet writes disabled. UTC clock selection and task integration are local.
