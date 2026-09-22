# Supported meter interface and data

This project implements the IQ Data Plus II local CLK/RW/DATA/INT interface.
It does not use UART serial, Modbus or INCOM's external two-wire physical layer.
The command fields and measurement formats come from the linked manufacturer
protocol documents; the local wire handshake was established by physical capture.

## Transaction

The 24-bit request payload is `INST | COMM<<4 | ADDRESS<<8 | SCOMM<<20`.
The transmitted image is `5 | payload<<3`. A physical request contains28 clocks:
the27-bit image followed by a trailing zero. Meter writes have25 clocks:
bit0 is control, and bits1–24 are payload.

The tested meter first replies with control word `0x400027` (repeat request).
The Pico completes that write with DATA0 on one clock, waits20ms, and resends
once within the same interface session. It releases DATA for the meter's write
phase and completes each returned word. Control replies and host-transmitted
patterns never enter the measurement decoder. Every production request is
bounded to500ms; bad framing, unmatched requests or unreleased outputs fail
validation. Failed reads do not become fresh measurements.

| Normal read | Command | DATA contents |
| --- | --- | --- |
| `all_standard_repeat` | 3/0/3 | Capability map followed by supported buffers;18 words on tested meter |
| `flags_repeat` | 3/C/8 | Header and current alarm/trip/cause flags;2 words |
| `settings_repeat` | 3/C/9 | Header, firmware version/revision and SW1–SW6;3 words |
| `trip_repeat` | 3/C/A | Header, flags and retained measurement slots;18 words |

Address0 worked on the tested local interface. This does not identify a meter's
separate network/INCOM address. Use an explicitly documented address; the
production code does not sweep addresses.

## Scaling and interpretation

IMPACC analog payloads contain a16-bit mantissa and8-bit scale byte. The scale
defines signedness, validity, base2/base10 and a signed exponent. The decoder
preserves the raw representation and exact decimal result. Standard currents
and voltages already use A/V; do not multiply them again by CT/PT settings.
The fourth current slot is reserved on IQ Data Plus II, not another current.

Power1 contains W, demand W and scaled Wh. Power2 contains Hz, var and PF.
The separate energy buffer is an unsigned24-bit kWh counter with1kWh resolution;
the scaled Wh buffer can have much coarser resolution. Aggregate reads do not
prove simultaneous measurement sampling.

The meter defines negative PF as lagging, positive PF as leading. For positive
real power, negative var indicates inductive loading. Preserve this convention.
Settings expose the standard/alternate PF selection; neither selection proves
THD. `sqrt(P²+Q²)` and `abs(P)/sqrt(P²+Q²)` are labeled estimates, not harmonic
measurements. Missing, invalid or unsupported values remain unavailable.

Switch bits decode as **1=OFF, 0=ON**. The settings buffer reports configuration
without changing it. Current flags are separate from trip-buffer flags. Trip
measurements require an asserted valid trip flag and individual measurement
validity; the trip buffer supplies **no event timestamp**. Its acquisition time
is never presented as the event time.

## Decoder and capture tools

```sh
python3 live/decode_meter.py fast_status 0xB01482
python3 live/decode_diagnostics.py --help
.venv/bin/pip install -r requirements-analysis.txt
.venv/bin/python live/analyze_trial.py capture.sr --output /tmp/analysis.json
```

`analyze_trial.py` expects a24MHz, one-byte sigrok session using the documented
analyzer map: DATA0, CLK1, RW2, INT3. It scans complete captures and preserves
short/censored pulses separately from complete exchanges. It does not itself
prove which device drove DATA. `decode_request.py` checks an explicit request
image; a match is not a received meter response. `analysis/analyze_sr.py`
provides general sample/edge analysis with explicit channel options.

## References

Manufacturer documents hosted in the legacy Power Management Products archive:

- [IL17384 Part A, August2011](https://pps2.com/communications/files/INCOM/incom_17384_partA_8-2011.pdf): scaling, status and standard buffers.
- [IL17384 chapter5, March1998](https://pps2.com/communications/files/legacyPMP/files/il17384v30/parta/005_standardmasterslave.pdf): aggregate capability map.
- [IL17384 chapter108, November1998](https://pps2.com/communications/files/legacyPMP/files/il17384v30/partb/108_iqdataplusii.pdf): IQ Data Plus II identity, diagnostics and reserved fields.
- [TD17271A pp.11–20](https://pps2.com/communications/files/legacyPMP/products/iqdpii/docs/td17271a_pp11_20.pdf): measurement signs and behavior.
- [TD17271A pp.31–43](https://pps2.com/communications/files/legacyPMP/products/iqdpii/docs/td17271a_pp31_43.pdf): switch settings and PF selection.

The source links these documents; copyrighted manuals are not redistributed.
