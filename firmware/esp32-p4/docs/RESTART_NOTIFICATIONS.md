# BACnet restart notifications

After startup health acceptance and Ethernet readiness, the Device sends one
UnconfirmedCOVNotification to its read-only default local-broadcast recipient.
The notification contains System_Status, Time_Of_Device_Restart, and
Last_Restart_Reason. Its process identifier is zero and its initiating and
monitored object is this Device. It uses a local BVLC broadcast, without a
global/routed NPDU destination. All BACnet properties remain read-only.

The boot timestamp is frozen once. If NTP is synchronized, it is UTC minus
monotonic uptime. Otherwise the notifier waits up to five seconds after startup
readiness and freezes **1990-01-01 00:00:00**, an explicitly unsynchronized
fallback, not a measured date. A later NTP synchronization updates Local_Date
and Local_Time but never rewrites this boot's restart timestamp. UTC offset is
zero and daylight saving is false. `/api/status` reports the timestamp source;
`boot_utc_ms` is null for the fallback.

Transport failure causes at most five attempts spaced one second apart.
Successful socket transmission stops attempts; it does not prove receipt by a
supervisor. Link loss pauses pending attempts; link return does not create
another boot notification. A real software restart or OTA boot starts a new
notification cycle. The restart reason distinguishes software restart, power
loss, watchdog, and panic reset.

Native sanitizer tests cover packet framing, property write denial, fixed
timestamps, delayed/no clock, readiness, link cycles, and bounded retry
exhaustion. `tests/test_restart.py` observes a real signed OTA boot with an
independent bacpypes3 decoder, then compares notification values with Device
ReadProperty responses. Supervisor-specific resubscription (including Metasys)
requires testing with that supervisor; it is not implied by packet delivery.
