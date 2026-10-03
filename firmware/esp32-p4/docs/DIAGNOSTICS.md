# Diagnostic events and network time

The gateway retains the newest 32 system, Ethernet, USB, management, OTA, and
BACnet delivery events in RAM. Each event includes a sequence, boot-relative
uptime, component, event, numeric code, and bounded explanatory text. Successful
operations do not clear earlier failures. Once full, the oldest event is
overwritten and the response reports how many events were displaced.

Authenticated download:

```sh
python3 firmware/esp32-p4/tools/manage.py --host "$GATEWAY" \
  --expected-mac "$MAC" --token-file "$TOKEN_FILE" --pin-file "$PIN_FILE" \
  diagnostics > /private/path/diagnostics.json
```

The underlying endpoint is an authenticated empty `POST /api/diagnostics`.
Responses request `Cache-Control: no-store`. Access requires the same pinned
HTTPS and one-use request authentication as configuration. No admin token,
TLS private key or raw firmware is logged. Bounded meter protocol traces can
contain raw reply words and must be kept private; they are not validated
measurements.

## Meter transaction traces

Firmware 0.4.2 adds `meter_transactions` to the same authenticated response.
It retains the newest eight completed host attempts, including failures, with
up to 64 parsed events each. The transaction list is newest first; each event
trace is chronological and retains its newest events. Both levels report
overwritten/omitted counts. Downloading or refreshing diagnostics never starts
a meter read. The existing four allowlisted requests, 500 ms budget, retry
backoff and validation rules are unchanged. Pico 0.4.7 already supplies the
needed fields; no Pico firmware change is required.

Each transaction records its read kind/address, host elapsed time, outcome,
last observed progress, exact error and **all failed terminal checks**. The
host's parsed event/request/write/completion counts are separate from Pico's
terminal counters. Missing or invalid Pico fields are JSON `null`, not zero.
Failures before a terminal result have `terminal_checks_evaluated:false` and
retain the parsed prefix; parsing stops at the first error. `buffer_accepted`
means the transport and decoder accepted the buffer, not that every individual
measurement is valid. Use point quality for that decision.

Useful failed checks:

| Check | Evidence |
| --- | --- |
| `request_not_clocked_completely` | A request image was presented but its complete read was not observed. |
| `no_meter_data_words` | No meter DATA word reached the validated stream. A repeat-control response alone is not DATA. |
| `write_without_completion` | A captured meter write lacks a presented completion. |
| `completion_not_clocked_completely` | A presented completion lacks its expected clock acknowledgement. |
| `event_total_mismatch` / request, completion or write variants | Pico's reported totals differ from host-observed records, or the reported field is missing/invalid. |
| `image_still_active` | The read/completion image was still active at the terminal result. |

`pico_reported` also includes stop code, elapsed time, malformed writes,
startup retries, loop timing, RW/DATA/INT activity and initial/final pin masks.
`pin_levels` expands bits 0–3 as CLK/GP0, RW/GP1, DATA/GP2 and INT/GP3.
These are digital snapshots, not measured voltages or proof of correct wiring.
For the PIO engine, `clock_rises` counts completed program shifts and captured
writes; zero does **not** prove no physical CLK edges occurred. Event timestamps
are CPU service times. Request, completion and `read_poll` words are presented
images, not sampled meter reply data. Stop code 0 means the deadline expired;
only full validation establishes a successful read.

The dashboard's Maintenance section shows these traces and downloads the same
JSON. Overview exposes the last interface error even when USB is qualified.
The response includes firmware/source revision and boot ID to identify the
capture. All traces reset on reboot; there is no flash log, database, automatic
address scan or change to BACnet point identifiers.

## Passive pin observation

Firmware 0.4.3 adds an authenticated, empty `POST /api/pico/observe` and the
Maintenance **Observe pins (500 ms)** button. `manage.py ... observe` runs the
same operation. Polling must be disabled, Pico must be qualified, and no other
observation or firmware maintenance may be active. The endpoint queues one
fixed `observe 500` command on the USB worker; it accepts no command text,
address, pin, duration, or drive settings. No Pico firmware change is needed.

The worker reads `status` before and after, checking inactive capture, zero SIO
output enables, SIO pin function, LOW output overrides and forced-disabled CLK/RW
outputs against the qualified RP2350 firmware. This uses RP2350 CTRL layouts,
not RP2040 register bit positions. The CPU observer samples all four pins with
outputs released. Its `clock_rises` counts sampled rising edges even when a
27-clock exchange never completes. CPU sampling can miss sufficiently short
pulses; timing reports the maximum bracketed sample gap. Digital input levels
do not measure voltage, continuity, or which end of a wire is connected.

The last result is retained only in RAM under `passive_observation` in the
authenticated diagnostics response. `state:complete` means the report and
before/after GPIO checks were validated; it does not mean meter communication
works. Check `full_duration`, `result.stop_code` and `elapsed_us`: ambiguous
simultaneous CLK/RW transitions can stop the observer early (code 8). Missing
result counters are `null`, not zero. No event words become measurements and
no passive capture changes meter freshness, read counters, or BACnet quality.

The queue expires after two seconds if it cannot start. USB execution has an
eight-second host deadline, bounded 1024-byte lines and at most 4096 event
records. Disconnect, malformed responses and maintenance interrupt the probe;
failure is recorded, and the normal abort/drain procedure releases outputs.
The response's boot ID and observation sequence let clients reject results
from a restarted gateway or a replacement request. Refresh and download remain
read-only; only the explicit observation command starts a capture.

## Active PIO snapshots

P4 0.4.4 can qualify Pico 0.4.7 or 0.4.8; signed UF2 updates verify that the
specific uploaded version returns. Pico 0.4.8 adds `pio_diagnostics` records to
active reads. These are separate from measurement events and never supply DATA
words or relax transaction validation. Older Pico firmware reports null fields.

The records include CPU-sampled CLK/RW edges, counters at the last request,
and PIO program counters, instructions, FIFO level, IRQ/debug/control registers
and GPIO controls before output release. CPU sampling can miss short pulses;
the register reads are sequential snapshots, not a synchronous logic-analyzer
capture. Compare CPU activity with PIO progress to distinguish a silent bus from
a stalled state machine. Diagnostics remain bounded and RAM-only.

## Network time

An NTP client starts after the first allowed Ethernet address. The build's
`CONFIG_IQ_NTP_SERVER` selects a hostname or numeric IPv4 address; an empty
string disables synchronization. The default is `time.cloudflare.com`. Use a
site NTP address on networks without external access, and a numeric address
when static networking has no DNS. `/api/status.clock` reports synchronization,
UTC milliseconds, and age of the last synchronization. SNTP itself is not an
authenticated time source. Meter freshness, request deadlines, and authentication
expiry continue using the monotonic clock.

UTC is recorded only after a real, plausible NTP synchronization. Earlier
events retain `utc_ms:null` permanently. A random boot ID and uptime identify
those events; later synchronization never invents earlier calendar timestamps.
Sequence numbers order events even if the wall clock changes. The log resets
on reboot and does not persist measurements or diagnostic events to flash.
Save a private download before firmware maintenance when previous evidence is
needed. Startup records the reset cause, and successful OTA validation adds a
fresh event for the current boot.
