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
