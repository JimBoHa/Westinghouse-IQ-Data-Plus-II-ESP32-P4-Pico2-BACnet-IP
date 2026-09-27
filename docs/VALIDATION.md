# Validation and scope

The initial release packaged the working Pico0.4.5 / BACnet1.1.0 implementation
from an actual IQ Data Plus II installation. Publication changes replace
site-specific paths, IP defaults and metadata with explicit configuration and
add the installer/documentation. Firmware source, PIO timing and supplied UF2
matched that deployment. They were not replaced by a simulated meter.

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

## Maintenance checks: firmware 0.4.6

The USB delivery update passed native tests for short writes, prolonged reader
pauses, disconnects and a shared response deadline. The Pico 2 W build used
SDK 2.3.1. Physical standard reads returned all 18 measurement words with a
normal reader and with a deliberate 1.2-second reader pause.

The accompanying host fixes passed 86 runtime tests, 10 installer tests and
five poller self-tests. These include acquisition error preservation, interrupt
cleanup and independently expiring voltage/frequency settings. The new timer
waits one hour after each snapshot finishes and uses idle I/O priority.

## Maintenance checks: firmware 0.4.7

Initial synchronization now waits for a fresh idle boundary after setup. Native
checks cover RW already low, RW falling during setup and recovery at a later
clean boundary. The physical build passed normal and delayed-reader checks.
An independent four-second capture at 24 MHz matched all 19 reply words
(control reply plus 18 measurements) to the USB journal, in order, with no
malformed writes. This is a bounded capture, not a long-term reliability claim.

After deployment, all 198 BACnet point identifiers, names, descriptions and
units matched the saved map. Device identity and database revision remained
unchanged. Confirmed COV subscription, renewal and cancellation passed. A live
SIGINT during acquisition completed cleanup and exited 130 in 0.064 seconds
with one attempt and no retry. Rolling points still require their full
continuous windows after a gateway restart.
