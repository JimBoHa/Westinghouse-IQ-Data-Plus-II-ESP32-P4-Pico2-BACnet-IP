# Persistent diagnostic errors (P4 0.4.7)

Earlier firmware retained only a 32-entry RAM event ring, which was cleared at
restart and could be overwritten by successful hourly clock updates. The RAM
ring and eight transaction traces remain available, with a separate flash
journal for error events. Successful clock syncs, ordinary boots, and normal
polls never consume error-history space.

## Storage and retention

- Hard limit: **1,048,576 bytes (1 MiB)**, 3.125% of the board's 32 MiB flash.
- Partition `iqerrors`: offset `0x1100000`, size `0x100000`, data subtype `0x40`.
  Configuration at `0x1000000` and both 4 MiB OTA slots remain unchanged.
- One immutable 4 KiB ownership header and 4,080 fixed 256-byte record slots.
  Once full, the oldest 4 KiB sector (16 records) is recycled; retention during
  rotation is 4,065–4,080 records. Writes rotate through all 255 data sectors.
- The first instance of an error is queued for an asynchronous flash worker.
  Further instances with the same component/event/code are aggregated into a
  record at most once per minute, including occurrence count and latest detail.
  Sixteen aggregation buckets and a 16-record queue bound RAM use. Queue drops,
  pending repeats, storage errors and skipped corrupt records are visible.
- Normal requested restarts/OTA drain the logger, including pending repeats.
  Sudden power loss may lose queued events or the current repeat aggregate.
  Previously committed records survive; CRC and a final commit marker reject
  incomplete records. An interrupted wrap can lose the oldest sector being
  recycled, but does not rewrite the newer sectors. Unexpected reset causes
  (panic/watchdog/brownout) are recorded when the next boot reaches diagnostics.
- Records contain boot ID, uptime, UTC when synchronized, component/event/code,
  occurrence count, bounded error detail and firmware/source revision. UTC is
  null before synchronization; times are never reconstructed or invented.
  Detail is limited to 127 bytes, component 15, event 23, version/source 15.
- This is an error journal, not a measurement recorder: no raw meter buffers,
  measurement history, credentials, or site configuration are written here.
  Historical entries do not assert a fault is still active. Existing per-boot
  BACnet health counters and rolling windows continue to reset at restart.

The application and recovery firmware recognize a statically provisioned
partition. Existing devices upgraded over Ethernet register the identical flash
range at runtime through IDF's bounds/overlap-checked partition API. No live
partition table rewrite, configuration migration, full-chip erase or eFuse
operation is required. Initial ownership is established only on erased storage;
unrecognized data is not automatically erased. Failure disables flash logging
and is reported without stopping meter/BACnet operation. Older firmware can run
from its unchanged OTA slot without erasing the journal.

## Reading the history

In the web dashboard, unlock management and open **Maintenance → Saved error
history**. Refresh shows the newest 32 records; **Download all saved errors**
paginates through all retained records into one JSON file. The export reports
incomplete retention if older records rotate while downloading.

`GET /api/status` exposes `persistent_errors` storage health and counts without
event details. Authenticated `POST /api/diagnostics` includes the newest 16
persistent errors as well as the existing RAM diagnostics. Authenticated
`POST /api/error-log` accepts an empty body, or JSON such as
`{"before":125,"limit":32}`; it returns newest-first records with sequence less
than the exclusive `before` cursor. Limits are 1–32. Follow `next_before` while
`has_more` is true. Authentication and body integrity use the same one-use
nonce/HMAC scheme as existing management commands. There is no remote erase
endpoint. A reboot does not clear history.

Error sources include meter transaction failures, USB faults, Ethernet loss,
BACnet initialization/discovery/COV faults where instrumented, rejected
management authentication, firmware update failures, clock failures, rejected
configuration and unexpected reset reasons. Changes to invalid meter operands
are logged by point name/count without saving the measurements. Undefined
zero-load calculations and history warmup are explained by point quality and
are not treated as repeated communication errors.

## Verification

`persistent_error_journal` runs a fault-injected NOR-flash emulator under native
sanitizers: full 1 MiB wrap, reopen/readback, pagination, CRC corruption, every
interrupted record/header write byte, partial sector erases, unknown-data
preservation and repeated-fault aggregation. These are synthetic power-loss
checks, not physical power-cut qualification. Device qualification should
record a known error, verify it is committed, request a safe restart, then
compare the same sequence/boot ID/details after the new boot and verify live
meter/BACnet operation. Never power-cycle a connected meter/Pico interface just
to exercise this test.
