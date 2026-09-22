# Adapting the project to other meters

The firmware and decoders implement the IQ Data Plus II interface and reply
formats. Reusing the host recorder or BACnet layer does not establish that a
different meter has compatible wiring, timing, commands, scales or units.

## Identify the interface first

Record the exact model, option cards and firmware revision. Obtain the matching
manufacturer pinout, electrical limits and protocol documentation. A DB9 may
carry RS-232, RS-485 or a proprietary interface; its shape establishes none of
these. Never carry this project's terminal map over to another meter by guess.

| Documented interface / task | Candidate hardware and work required |
| --- | --- |
| IQ Data Plus II CLK/RW/DATA/INT | Pico 2 W / RP2350 with this firmware; its PIO implementation handles the measured protocol timing. Reassess voltage and grounding for the actual installation. |
| RS-232 | ESP32 or Pico UART plus a MAX3232 circuit powered for compatible logic levels. Follow the [TI MAX3232 datasheet](https://www.ti.com/lit/ds/symlink/max3232.pdf), including capacitors and pinout; MAX3232 is an RS-232 transceiver, not a generic level shifter. |
| RS-485 | ESP32 or Pico UART plus a transceiver with 3.3 V compatible logic, such as an appropriately supplied [TI THVD1450](https://www.ti.com/product/THVD1450). Determine duplex mode, direction control, termination and bias from the actual bus design. |
| Documented serial port with host polling | A suitably rated isolated USB-to-RS-232 or USB-to-RS-485 adapter can place the electrical interface at the host. Select the correct standard and isolation arrangement. It does not implement the meter's application protocol. |
| Arduino Nano or UNO R4 alternative | Requires a firmware port and voltage/timing assessment. Nano-family voltages vary; see [Arduino's Nano overview](https://support.arduino.cc/hc/en-us/articles/11264980365468-Nano-family-overview). The [UNO R4 Minima](https://docs.arduino.cc/hardware/uno-r4-minima/) operates at 5 V. Neither is a drop-in target for this RP2350 PIO firmware. |

## Establish evidence before decoding

1. Measure signal levels and ground relationships using suitable instruments.
   Account for power sequencing, transients and all instrument ground paths.
   Connect a common ground only when electrically appropriate; otherwise design
   for isolation. Keep unknown pins disconnected.
2. Capture passively with rated, high-impedance inputs. Preserve raw captures,
   pin mapping, sample rate and timestamps. Determine who drives each signal,
   idle levels, clock edges, frame boundaries and direction changes.
3. Compare observations with the exact product documentation. Build a hypothesis
   for one known read command and its response. Offline synthetic frames can
   test software, but they do not prove a meter ever sent those bytes.
4. Run one finite, read-only transaction with an explicit address, deadline and
   output-release cleanup. Change one variable per trial and stop on unexpected
   levels, contention or malformed framing. Do not blindly scan addresses or
   commands; exclude resets, settings writes and energy-clearing commands.
5. Independently capture the wire exchange. Separate the transmitted request,
   acknowledgements and actual meter reply. For this project's protocol, prove
   that DATA was released during the meter's RW-LOW write phase; a logged
   presented request image or a 25-clock count alone is not reply provenance.
6. Decode only words belonging to that verified transaction. Check exact counts,
   control bits, checksums where defined, scale/validity fields, reserved fields
   and units. Repeat reads and compare with contemporaneous front-panel values
   before publishing measurements to BACnet.

## Keep model-specific meaning explicit

Preserve raw replies beside decoded values and add captured regression vectors.
The IQ Data Plus II's reserved fourth current, PF/var sign conventions, CT/PT
handling and diagnostic layouts are model-specific. Unsupported or invalid
values stay unavailable, not zero. Keep current flags separate from retained
trip data; this meter's trip buffer supplies no event timestamp. The time of a
read must not become an invented trip time.

Implement the new transport and decoder behind the host's acquisition boundary,
then map validated values, units, freshness and availability into BACnet. The
existing [protocol and decoder guide](PROTOCOL.md) illustrates those checks;
they are not protocol specifications for another product.
