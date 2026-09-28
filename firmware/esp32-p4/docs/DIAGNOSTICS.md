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
TLS private key, raw firmware, or meter measurement is logged.

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
