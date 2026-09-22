# Validation and scope

This repository packages the working Pico0.4.5 / BACnet1.1.0 implementation
from an actual IQ Data Plus II installation. Publication changes replace
site-specific paths, IP defaults and metadata with explicit configuration and
add the installer/documentation. Firmware source, PIO timing and supplied UF2
match the deployed version. They were not replaced by a simulated meter.

## Physical and network checks completed

- Pico2W hardware was independently identified; live firmware reported no
  synthetic host. CLK/RW were meter inputs and DATA was released during replies.
- Standard electrical reads repeated successfully. Three additional commands
  (flags, settings, trip) were independently captured at24MHz for4seconds each.
  Complete96million-sample scans matched every returned DATA word to USB evidence,
  including requests, repeat-control reply and one-clock completions.
- All200 BACnet objects and1307 property reads passed external verification.
  Writes were rejected and changing values arrived by confirmed COV.
- After a gateway restart, the building controller acknowledged575/575 captured
  confirmed updates across19 restored subscriptions;616 read responses contained
 3108 properties with no property errors.
- The user confirmed Metasys discovery of both original and additional points.
- A deployment audit covered469 successful standard reads and59 successful
  diagnostic reads with no failures/malformed writes. Diagnostic fields were
  absent from database trends. Normal measured intervals were about1.05seconds,
  with2.1-second intervals for diagnostic slots.
- Nine instantaneous calculations matched their exact electrical source sample.
  Rolling warmup fault/readiness behavior was checked through BACnet.

These are bounded observations of one installation. A contemporaneous meter
display comparison was not available. No Pi reboot test, other-meter hardware
qualification, isolation certification or complete24-hour rolling-window field
test is claimed. Arithmetic tests cover rolling windows and interruption cases.
The direct wiring is specific to the documented development arrangement.

## Reproduce software checks

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt -r requirements-analysis.txt
.venv/bin/python -m unittest discover -s live -p 'test_*.py' -v
.venv/bin/python -m unittest discover -s tests -p 'test_*.py' -v
.venv/bin/python live/poll_meter.py --self-test
.venv/bin/python analysis/analyze_sr.py --self-test
```

Publication checks passed82 runtime tests,10 installer tests, the five poller
self-tests and the analyzer self-test. Generated units also passed Linux
`systemd-analyze --user verify` on the Pi without installing or starting them.
Tests cover framing/provenance rejection, scale validity, configuration/trip
qualification, gap-aware derived calculations, SQLite recording, diagnostics
exclusion, BACnet quality/COV persistence, and portable unit generation. Tests
use anonymized fixtures; they never open the actual meter connection.

The original raw meter journals, packet captures, site configuration and database
are intentionally not in this public repository. The protocol guides link
manufacturer references; the included tests preserve non-identifying regression
vectors. See firmware checksums in [SHA256SUMS](../firmware/pico-live/SHA256SUMS).
