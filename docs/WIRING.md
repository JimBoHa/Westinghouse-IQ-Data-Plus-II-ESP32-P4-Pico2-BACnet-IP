# IQ Data Plus II wiring

This is the connection verified in the development setup: an **IQ Data Plus II**
and **Raspberry Pi Pico 2 W (RP2350)**, with a parallel logic analyzer. The meter's
DB9 carries its proprietary **CLK/RW/DATA/INT interface**. Its connector shape
does not make it RS-232; do not connect a serial adapter or MAX3232 to this port.

Use the numbered meter terminals and the Pico's printed GP labels. The physical
pin numbers below refer to the Pico header, not the RP2350 chip or a breakout.
Check connector numbering from the actual mating side before wiring.

| Meter terminal | Signal | Pico 2 W label | Pico physical pin | Parallel analyzer |
| --- | --- | --- | --- | --- |
| 2 | Ground | GND | 3 | GND |
| 3 | CLK, input to Pico | GP0 | 1 | D1 |
| 6 | RW, input to Pico | GP1 | 2 | D2 |
| 4 | DATA, LOW/release | GP2 | 4 | D0 |
| 5 | INT, LOW/release | GP3 | 5 | D3 |

```text
IQ Data Plus II             Pico 2 W                 Analyzer
terminal 2  GND --------+-- GND  physical 3 -------- GND
terminal 3  CLK --------+-- GP0  physical 1 -------- D1
terminal 6  RW  --------+-- GP1  physical 2 -------- D2
terminal 4  DATA -------+-- GP2  physical 4 -------- D0
terminal 5  INT --------+-- GP3  physical 5 -------- D3

terminal 1 (~26 V)          DISCONNECTED
terminals 7, 8, 9           DISCONNECTED
USB host ----------------- Pico USB power/data
```

Each diagram row is a separate net. No meter terminal connects to the Pico's
3V3, VSYS or VBUS pins. The host connects through USB, not host GPIO.

## Electrical limits and power order

This direct connection is **not galvanically isolated**. Connect the common
ground only after establishing that the meter communication ground, USB host
and analyzer can share a reference safely. USB and analyzer leads can create
additional ground paths. Where that is inappropriate, use an engineered
isolated interface that preserves signal direction and timing; this diagram
does not establish an isolation design.

GP0–GP3 are RP2350 FT pins. With **IOVDD powered at 3.3 V**, the datasheet permits
an input HIGH up to **5.5 V**; with IOVDD at 0 V, the absolute maximum is **3.63 V**.
The measured meter HIGH in this setup was approximately **5.10 V DC**. Power the
Pico before connecting meter signals and disconnect CLK/RW/DATA/INT before
removing USB power. See the [RP2350 datasheet, §§14.8–14.9](https://datasheets.raspberrypi.com/rp2350/rp2350-datasheet.pdf).

A DC reading does not establish transient peaks, source impedance or allowable
sink current. Verify those and the analyzer's input ratings for each connection.
GPIO drive-strength settings are not current limiters. Direct wiring has been
verified only in this development setup; it is not a general interface rating.

1. Leave meter signals disconnected; power the Pico by USB and load the live
   firmware. Confirm it is idle with DATA/INT released and internal pulls off.
2. Establish the approved common reference, then connect the four signal nets.
   Keep the analyzer in parallel while qualifying the connection.
3. Begin with a finite input-only observation. Check levels, pin mapping and
   timing before attempting a documented, bounded read transaction.
4. On shutdown, release DATA/INT, disconnect the four meter signals, then remove
   Pico USB power. Do not leave meter signals attached to an unpowered Pico.

The live firmware never drives CLK or RW. It drives DATA and INT only LOW or
releases them; during meter writes DATA must be released. The analyzer-confirmed
read handshake is described in [the protocol guide](PROTOCOL.md).
For a different board or meter, start with [adapting to other meters](OTHER_METERS.md).
