# Sustained Ethernet health monitoring

The read-only host monitor concurrently samples pinned HTTPS status/configuration
and a directed, instance-bounded BACnet Who-Is. It sends no broadcast discovery,
changes no configuration, requires no private management key, and records no
meter values. Use the previously commissioned certificate pin and expected MAC.
Never target the protected production gateway.

```sh
python3 firmware/esp32-p4/tools/soak_monitor.py \
  --host "$GATEWAY" --expected-mac "$MAC" --pin-file "$PIN_FILE" \
  --device 75201 --duration 86400 --interval 10 \
  --output /private/path/iqdata-soak.jsonl
python3 firmware/esp32-p4/tools/review_soak.py /private/path/iqdata-soak.jsonl
```

Keep the host awake and on the same LAN. The exclusive mode-0600 output must
not already exist. Every independent probe's outcome and latency are retained,
even if the other probe fails. Logs contain site identity/configuration and
health counters; keep them private and outside Git. Existing polling is
expected disabled unless `--poll-enabled` is explicitly supplied. This flag
only sets the expectation, and never starts meter polling.

Failures include identity/configuration/firmware/OTA-slot/boot changes, decreasing
or drifting uptime, link loss, unqualified Pico, stalled heartbeats, changed USB
failure/overflow/reconnection counters, duplicate instances, COV timeouts,
missing startup/restart acceptance, and loss of an initially synchronized clock.
Default internal heap floor is 64 KiB, with at most 64 KiB decline from the
initial sample. Both limits are explicit options. A disabled/unsynchronized
clock at baseline is recorded, not invented as synchronized.

Sampling uses host monotonic time, includes both the initial sample and the
exact duration boundary, and rejects excessive lateness or missing samples.
Ctrl-C produces an incomplete failure summary. A killed process leaves no final
summary and cannot pass offline review. The reviewer recomputes health, checks
sequence/schedule/completion, preserves recorded failures, and defaults to
requiring **86,400 actual seconds**. A short run can validate the monitor with
`--minimum-duration 60`; it cannot establish a 24-hour qualification.

The monitor tests gateway/transport stability, not electrical wiring, decoded
meter accuracy, or continuous live-meter history. Those require the physical
meter and separate commissioning evidence. Native tests exercise fabricated
health logs and malformed packet inputs only; firmware contains no playback.
The independent-probe and offline-review design follows the S3 reference's
soak tooling; this implementation checks the IQData P4/Pico health contract.
