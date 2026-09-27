# Westinghouse IQ Data Plus II to BACnet IP

Read a Westinghouse **IQ Data Plus II** meter through its proprietary local
interface, then expose measurements and diagnostics through a Raspberry Pi 5
as a read-only **BACnet/IP** device. A **Pico 2 W** handles the wire timing;
the Pi handles decoding, SQLite recording, HTTP and BACnet.

```text
IQ Data Plus II ── CLK / RW / DATA / INT ── Pico 2 W ── USB ── Pi 5 ── Ethernet / BACnet
                     │ parallel analyzer                    └── SQLite + HTTP
```

The deployed version was verified against real meter replies and a parallel
logic analyzer. Metasys discovered the device and its **198 points**. Firmware
is `0.4.7`; BACnet gateway is `1.1.0`. See [validation](docs/VALIDATION.md).

## Start here

1. Check [hardware and wiring](docs/WIRING.md). This DB9 is **not RS-232**.
2. Follow the [Pi setup guide](docs/SETUP.md), including firmware installation.
3. Run BACnet discovery using your chosen device instance; default example `75151`.
4. Use the [point map](live/BACNET_POINT_MAP.csv) and [BACnet/recording guide](docs/OPERATIONS.md).

Features include phase and neutral voltages, currents, real/reactive power,
meter PF, frequency, demand and energy; current alarm/trip flags; firmware and
configuration; retained trip readings; imbalance, extrema, excursion durations,
rolling demand/energy and communication health. Standard electrical readings
are recorded about every **1.05 seconds**, with occasional **2.1-second** gaps
for diagnostic reads. Additional diagnostics are live values, not database trends.

Rolling values require their complete 15-minute, one-hour or 24-hour window.
The trip buffer has no event timestamp. THD and harmonic waveforms are
unavailable. Device writes are rejected. The implementation has been tested
with one IQ Data Plus II installation; other models need their own validation.

## Project contents

| Path | Contents |
| --- | --- |
| `firmware/pico-live/` | Pico source, PIO transport and the tested Pico 2 W UF2 |
| `live/` | Poller, decoders, recorder, HTTP/BACnet services and regression tests |
| `scripts/install_pi.py` | Configurable systemd user-service generator/installer |
| `analysis/` | Independent sigrok capture analysis |
| `docs/` | Wiring, setup, protocol, operations and adaptation guides |

For another meter, start with [adaptation and hardware options](docs/OTHER_METERS.md).
Keep the application protocol and electrical interface separate: an RS-232
adapter cannot substitute for this meter's CLK/RW/DATA/INT interface.

Run the hardware-free checks after installing [requirements.txt](requirements.txt):

```sh
.venv/bin/python -m unittest discover -s live -p 'test_*.py' -v
.venv/bin/python -m unittest discover -s tests -p 'test_*.py' -v
.venv/bin/python live/poll_meter.py --self-test
```

The package contains no credentials, original-site IP configuration, database,
packet captures or meter logs. Configure your own network and USB device path.
