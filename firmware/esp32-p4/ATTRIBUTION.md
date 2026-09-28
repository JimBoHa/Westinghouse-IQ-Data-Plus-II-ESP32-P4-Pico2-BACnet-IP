# Source provenance

The meter decoding, diagnostics and transport rules are ported from [Westinghouse-IQ-Data-Plus-II-to-BACnet-IP](https://github.com/JimBoHa/Westinghouse-IQ-Data-Plus-II-to-BACnet-IP) at `1add86003bddecb69c1c7ac28c5efd70751febff`. Original source and Git history are retained. The plain Pico 2 build uses the existing 0.4.7 source without timing/PIO changes.

The BACnet integration structure, UDP transport and compatibility headers were adapted from [ESP32-P4-Modbus-IP-to-BACnet-IP-Protocol-Converter](https://github.com/JimBoHa/ESP32-P4-Modbus-IP-to-BACnet-IP-Protocol-Converter) at `0729b7b96d0da99bfce14fd174cd39c21537ea2e`. Its 0BSD SPDX notices and MIT compatibility-header notices are retained. No production configuration, signing keys or production binaries were copied. The Modbus/ATS polling and its 128-point map are not part of this port.

Pinned dependencies retain their own licenses:

- BACnet Stack 1.6.0: `9bc3cfa07aab98852de432fa24079f4b4b6b7eed`; see its license notices and linking exception.
- cJSON 1.7.19: `c859b25da02955fef659d658b8f324b5cde87be3`; MIT; native tests only. Firmware uses ESP-IDF's cJSON component.
- ESP-IDF 5.5.5: `b774170ff46c393eeb5e495ea37936038d3f4f4f`; dependency licenses are in the SDK.
- Espressif CDC ACM 2.4.1, MSC 1.1.4, mDNS 1.9.1: pinned in `main/idf_component.yml` and `dependencies.lock`.
- Pico SDK 2.3.1 and its TinyUSB dependency: Raspberry Pi SDK license notices.

Board references: [Waveshare documentation](https://docs.waveshare.com/ESP32-P4-WIFI6-POE-ETH), [schematic](https://files.waveshare.com/wiki/ESP32-P4-WIFI6-POE-ETH/ESP32-P4-WIFI6-POE-ETH-Schematic.pdf), [Espressif USB host documentation](https://docs.espressif.com/projects/esp-idf/en/v5.5.4/esp32p4/api-reference/peripherals/usb_host.html), and [Raspberry Pi USB IDs](https://github.com/raspberrypi/usb-pid/blob/main/Readme.md).
